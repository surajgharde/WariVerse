"""Did the stampede oversubscribe a slot? (Section 11, Phase 10)

Run this straight after a Locust run, against the same database:

    python infra/locust/check_oversubscription.py
    python infra/locust/check_oversubscription.py --date 2026-07-25

This is the assertion the load test cannot make about itself.  Locust sees
responses; it cannot see the ledger.  And oversubscription is invisible from the
response side by construction — every request that overbooked a slot returned
201 Created and looked perfect. The extra people find out at the gate.

Three invariants are checked, and they are checked separately because they fail
for different reasons:

1. `booked_count <= capacity - walkin_reserve`
   The online-bookable ceiling. Breaking this means the seat claim raced: two
   concurrent bookings both read the same `booked_count` and both wrote. This
   is the classic lost update, and 20,000 requests in 60 seconds is exactly the
   shape that finds it.

2. `booked_count == sum(group_size of active passes)`
   The counter agrees with the rows. Breaking this means a booking committed a
   pass without incrementing, or released a seat without decrementing — a
   different bug with the same symptom at the gate.

3. `walkin_used <= walkin_reserve`
   Section 5/E1's whole point. If online bookings can eat into the walk-in
   reserve, a pilgrim without a smartphone has been priced out of darshan by a
   race condition, which is the exact inequity the reserve exists to prevent.

Exit code is 0 for clean, 1 for any breach, so this belongs in CI after the
stampede step.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import date, timedelta
from pathlib import Path

CORE_API = Path(__file__).resolve().parents[2] / "services" / "core-api"
sys.path.insert(0, str(CORE_API))

if sys.platform == "win32":  # psycopg async needs the selector loop on Windows
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.models import Pass, PassStatus, Slot  # noqa: E402

DEFAULT_DB = "postgresql+psycopg://wariverse:change-me-in-prod@localhost:5432/wariverse"

#: Statuses that still occupy a seat. A cancelled pass has given its seat back;
#: a scanned one has used it but still consumed it from the slot's ceiling.
OCCUPYING = (PassStatus.ACTIVE, PassStatus.SCANNED)


async def check(day: date, database_url: str) -> int:
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    breaches: list[str] = []
    checked = 0

    async with factory() as session:
        slots = list(
            (await session.execute(select(Slot).where(Slot.date == day).order_by(Slot.start_time))).scalars()
        )
        if not slots:
            print(f"No slots exist for {day}. Nothing to check — did the stampede target the right date?")
            await engine.dispose()
            return 1

        for slot in slots:
            checked += 1
            bookable = max(0, slot.capacity - slot.walkin_reserve)
            label = f"{day} {slot.start_time:%H:%M}"

            # 1. the online-bookable ceiling
            if slot.booked_count > bookable:
                breaches.append(
                    f"{label}: booked_count {slot.booked_count} exceeds bookable {bookable} "
                    f"(capacity {slot.capacity} - reserve {slot.walkin_reserve}) "
                    f"— OVER BY {slot.booked_count - bookable}. The seat claim raced."
                )

            # 2. the counter against the rows
            actual = (
                await session.scalar(
                    select(func.coalesce(func.sum(Pass.group_size), 0)).where(
                        Pass.slot_id == slot.id,
                        Pass.status.in_([str(s) for s in OCCUPYING]),
                    )
                )
            ) or 0
            if int(actual) != slot.booked_count:
                breaches.append(
                    f"{label}: booked_count {slot.booked_count} disagrees with the "
                    f"{int(actual)} seats actually held by passes. The counter and the "
                    "rows have drifted apart."
                )

            # 3. the walk-in reserve (Section 5, E1)
            if slot.walkin_used > slot.walkin_reserve:
                breaches.append(
                    f"{label}: walkin_used {slot.walkin_used} exceeds the reserve "
                    f"{slot.walkin_reserve}. Pilgrims without smartphones have been "
                    "squeezed out — the equity guarantee has failed."
                )

    await engine.dispose()

    print("=" * 70)
    print(f"  Oversubscription check — {day}, {checked} slot(s)")
    print("=" * 70)
    if not breaches:
        print("  CLEAN. No slot confirmed more seats than it holds.")
        print("  The seat claim held under contention.")
        print("=" * 70)
        return 0

    print(f"  {len(breaches)} BREACH(ES) — the system oversubscribed under load.")
    print("  Every one of these returned 2xx to the pilgrim.")
    print()
    for breach in breaches:
        print(f"    - {breach}")
    print("=" * 70)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date",
        default=os.getenv("WARIVERSE_LOAD_DATE", (date.today() + timedelta(days=1)).isoformat()),
        help="Slot date the stampede targeted (YYYY-MM-DD). Defaults to tomorrow.",
    )
    parser.add_argument(
        "--database-url",
        default=os.getenv("DATABASE_URL", DEFAULT_DB),
        help="Async SQLAlchemy URL for the database the stampede ran against.",
    )
    args = parser.parse_args()
    return asyncio.run(check(date.fromisoformat(args.date), args.database_url))


if __name__ == "__main__":
    raise SystemExit(main())
