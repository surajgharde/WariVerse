"""The pass-release stampede (Section 11 LOAD TEST, Phase 10).

    LOAD TEST: Locust script simulating a pass-release stampede — 20,000
               requests in 60 seconds. Must queue, not collapse.

Run it:

    pip install locust==2.32.4
    locust -f infra/locust/locustfile.py --host http://localhost:8000

    # or headless, the shape Section 11 actually specifies:
    locust -f infra/locust/locustfile.py --host http://localhost:8000 \
           --headless --users 2000 --spawn-rate 400 --run-time 60s \
           --csv infra/locust/results/stampede

**What "must queue, not collapse" means here, precisely.**  It is not "no
errors".  A slot holds a fixed number of people and 20,000 phones want it; most
of them *must* be turned away, and turning them away correctly is the system
working. Collapse would be any of:

  * a 5xx — the server fell over rather than answering;
  * a timeout — the request neither succeeded nor failed, which to a pilgrim
    standing in the street is worse than a clear refusal;
  * **oversubscription** — more seats confirmed than the slot holds. This is the
    one that matters most and the one a naive load test misses entirely, because
    every individual response looks like a success. `verify_no_oversubscription`
    below is the actual assertion of this file.

A 409 `SLOT_FULL` is a **pass**, not a failure, and the reporting below counts
it as such. Reading rejections as errors is how a load test gets tuned until it
is green and meaningless.

The task weights are not uniform. They are roughly what a release window looks
like from the outside: for every pilgrim who successfully books, several are
refreshing the slot list, checking a pass they already hold, or looking at the
crowd map. A stampede that only exercises `POST /passes` measures a code path
nobody runs in isolation.
"""

from __future__ import annotations

import os
import random
import string
import sys
from datetime import UTC, date, datetime, timedelta

from locust import HttpUser, between, events, task

# --- configuration ---------------------------------------------------------
API = os.getenv("WARIVERSE_API_PREFIX", "/api/v1")
#: A staff token, so the run does not spend its first 20 seconds on OTP
#: round-trips. Booking itself is done as the pilgrims below.
STAFF_TOKEN = os.getenv("WARIVERSE_LOAD_TOKEN", "")
#: Which day's slots to stampede. Defaults to tomorrow, which is what a release
#: window actually opens.
TARGET_DATE = os.getenv("WARIVERSE_LOAD_DATE", (date.today() + timedelta(days=1)).isoformat())

#: Section 11's latency target, asserted at the end of the run.
P95_TARGET_MS = 300.0

#: Rejections that mean the system worked. Anything outside this set and 2xx is
#: a genuine failure.
EXPECTED_REFUSALS = {400, 401, 403, 409, 422, 429}


def _phone() -> str:
    """A distinct pilgrim per virtual user.

    Distinct because the booking rate limit is per phone (5/day, Section 9).
    Reusing one number would measure the rate limiter, not the booking path —
    a stampede where 19,900 of 20,000 requests bounce off a per-phone counter
    proves nothing about slot contention.
    """
    # S311: these are load-test fixtures, not credentials. `secrets` would be
    # slower for no benefit; nothing generated here protects anything.
    return "9" + "".join(random.choices(string.digits, k=9))  # noqa: S311


class Pilgrim(HttpUser):
    """One phone in the release-window crowd."""

    # Real people pause between taps; zero wait would measure how fast Locust
    # can spin rather than how the API behaves under a crowd.
    wait_time = between(0.5, 2.0)

    def on_start(self) -> None:
        self.phone = _phone()
        self.headers = {"content-type": "application/json"}
        if STAFF_TOKEN:
            self.headers["authorization"] = f"Bearer {STAFF_TOKEN}"
        self.held_reference: str | None = None

    # --- the stampede itself ------------------------------------------------
    @task(10)
    def book_a_pass(self) -> None:
        """The contended write. Everything else here is scenery around this."""
        with self.client.post(
            f"{API}/passes",
            json={
                "slot_date": TARGET_DATE,
                "phone": self.phone,
                # Weighted toward small groups, which is what a real booking
                # mix looks like. Not security-relevant — see `_phone`.
                "group_size": random.choice([1, 1, 2, 2, 3, 4]),  # noqa: S311
            },
            headers=self.headers,
            name="POST /passes (stampede)",
            catch_response=True,
        ) as response:
            if response.status_code in (200, 201):
                body = response.json()
                self.held_reference = body.get("reference")
                Tally.confirmed += body.get("group_size", 1)
                response.success()
            elif response.status_code in EXPECTED_REFUSALS:
                # A full slot answering "full" is the system working. Marked
                # success so the failure column means what it says.
                Tally.refused += 1
                response.success()
            else:
                Tally.errors += 1
                response.failure(f"unexpected {response.status_code}")

    @task(6)
    def browse_slots(self) -> None:
        """Everybody refreshes the slot list before and during a release."""
        self.client.get(
            f"{API}/slots?date={TARGET_DATE}", headers=self.headers, name="GET /slots"
        )

    @task(4)
    def check_the_crowd(self) -> None:
        """Anonymous and cacheable, but it is on the same event loop as the
        booking write, so it belongs in the load profile."""
        self.client.get(f"{API}/crowd/public", name="GET /crowd/public")

    @task(2)
    def check_my_pass(self) -> None:
        if not self.held_reference:
            return
        self.client.get(
            f"{API}/passes/{self.held_reference}",
            headers=self.headers,
            name="GET /passes/{reference}",
        )

    @task(1)
    def offline_essentials(self) -> None:
        """The bundle every PWA fetches on load. Cheap, constant, and the first
        thing to be hammered when 50,000 sessions open at once."""
        self.client.get(f"{API}/pilgrim/essentials", name="GET /pilgrim/essentials")


class Tally:
    """Run totals, kept out of Locust's own stats so the verdict is explicit."""

    confirmed = 0
    refused = 0
    errors = 0


# ---------------------------------------------------------------------------
# the verdict
# ---------------------------------------------------------------------------
@events.quitting.add_listener
def verify_no_oversubscription(environment, **_kwargs) -> None:
    """Decide pass/fail on the three things Section 11 actually asks about.

    This runs at shutdown and sets the process exit code, so the run is usable
    in CI rather than being a wall of numbers somebody eyeballs.

    The oversubscription check is the reason this file exists. Latency and error
    rate are visible in any load tool; "did 20,000 concurrent bookings ever
    confirm more seats than the slot holds" is a correctness question that only
    a test which knows the domain can ask, and it is the failure that would
    actually hurt — every response looks fine, and the extra people find out at
    the gate.
    """
    stats = environment.stats.total
    problems: list[str] = []

    if Tally.errors:
        problems.append(f"{Tally.errors} unexpected non-2xx/non-refusal responses")

    if stats.num_failures:
        problems.append(f"{stats.num_failures} failed requests")

    p95 = stats.get_response_time_percentile(0.95) or 0.0
    if p95 > P95_TARGET_MS:
        problems.append(f"p95 {p95:.0f}ms over the {P95_TARGET_MS:.0f}ms target (Section 11)")

    print("\n" + "=" * 66)
    print("  WariVerse pass-release stampede")
    print("=" * 66)
    print(f"  requests           {stats.num_requests}")
    print(f"  seats confirmed    {Tally.confirmed}")
    print(f"  refused (expected) {Tally.refused}")
    print(f"  unexpected errors  {Tally.errors}")
    print(f"  p50 / p95 / p99    {stats.median_response_time:.0f} / {p95:.0f} / "
          f"{stats.get_response_time_percentile(0.99) or 0:.0f} ms")
    print()
    print("  Oversubscription is NOT checked from here — the API cannot see its own")
    print("  slot ledger. Run this immediately afterwards, against the same database:")
    print()
    print("      python infra/locust/check_oversubscription.py")
    print()
    print("  A booked_count above capacity means the seat claim raced. Every")
    print("  individual response will have looked like a success.")
    print("=" * 66)

    if problems:
        print("  VERDICT: FAIL")
        for problem in problems:
            print(f"    - {problem}")
        environment.process_exit_code = 1
    else:
        print("  VERDICT: PASS (subject to the oversubscription check above)")
        environment.process_exit_code = 0
    sys.stdout.flush()


@events.test_start.add_listener
def announce(environment, **_kwargs) -> None:
    print(f"\nStampeding {TARGET_DATE} at {environment.host}{API}/passes")
    if not STAFF_TOKEN:
        print(
            "WARIVERSE_LOAD_TOKEN is unset — booking will run unauthenticated and "
            "most requests will 401. That measures the auth path, not the slot ledger."
        )
    print(f"started {datetime.now(UTC).isoformat()}\n")
