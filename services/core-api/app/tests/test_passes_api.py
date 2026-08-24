"""Smart Darshan Pass end to end (Section 4/M1).

Covers the three acceptance criteria stated in the spec:
  1. 10,000 passes bookable without oversubscribing any slot
  2. a forced 40% throughput drop shifts downstream passes and queues notices
  3. a scanned QR cannot be scanned twice
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.permissions import Role
from app.core.security import hash_phone, now_utc
from app.models import Pass, PassNotification, PassStatus, Slot, SlotStatus
from app.services import config_service, pass_service, qr_service, reslot_service

from .conftest import TEST_DATABASE_URL

pytestmark = [pytest.mark.db, pytest.mark.redis]

PILGRIM_PHONE = "9876543210"
VOLUNTEER_PHONE = "9820011223"
STAFF_PASSWORD = "correct-horse-battery-staple"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
async def sign_in_pilgrim(client: AsyncClient, api_prefix: str, phone: str = PILGRIM_PHONE) -> dict:
    requested = await client.post(f"{api_prefix}/auth/otp/request", json={"phone": phone})
    code = requested.json()["debug_code"]
    verified = await client.post(
        f"{api_prefix}/auth/otp/verify",
        json={"phone": phone, "code": code, "name": "रुक्मिणी", "language": "mr"},
    )
    return verified.json()


async def sign_in_volunteer(client: AsyncClient, api_prefix: str, make_user) -> str:
    await make_user(phone=VOLUNTEER_PHONE, role=Role.VOLUNTEER, password=STAFF_PASSWORD)
    login = await client.post(
        f"{api_prefix}/auth/login", json={"phone": VOLUNTEER_PHONE, "password": STAFF_PASSWORD}
    )
    return login.json()["access_token"]


def tomorrow() -> date:
    return (now_utc() + timedelta(days=1)).date()


async def make_slot(
    session: AsyncSession,
    *,
    start: datetime,
    minutes: int = 30,
    capacity: int = 100,
    walkin_reserve: int = 25,
    status: str = SlotStatus.OPEN,
) -> Slot:
    """A slot at an arbitrary window — scanning tests need one happening now."""
    end = start + timedelta(minutes=minutes)
    slot = Slot(
        date=start.date(),
        start_time=start.time(),
        end_time=end.time(),
        capacity=capacity,
        booked_count=0,
        walkin_reserve=walkin_reserve,
        status=status,
    )
    session.add(slot)
    await session.flush()
    return slot


async def book_via_service(session: AsyncSession, slot: Slot, *, phone: str, group_size: int = 1) -> Pass:
    return await pass_service.book_pass(
        session,
        slot_id=slot.id,
        phone_hash=hash_phone(phone),
        holder_name="यात्रेकरू",
        group_size=group_size,
        language="mr",
    )


async def seed_pass(session: AsyncSession, slot: Slot, *, phone: str, group_size: int = 1) -> Pass:
    """Place a pass into a slot that has already started.

    `book_pass` refuses this on purpose — you cannot book a window that has
    opened. Scanning and expiry tests need a pass that is *already* in a live or
    past slot, which in production got there by being booked hours earlier.
    This builds that state directly rather than weakening the booking guard.
    """
    record = Pass(
        reference="WV" + uuid.uuid4().hex[:8].upper(),
        slot_id=slot.id,
        holder_phone_hash=hash_phone(phone),
        holder_name="यात्रेकरू",
        holder_language="mr",
        group_size=group_size,
        qr_secret=qr_service.new_pass_secret(),
        status=PassStatus.ACTIVE,
        issued_at=now_utc() - timedelta(hours=6),
        original_slot_id=slot.id,
    )
    session.add(record)
    slot.booked_count += group_size
    await session.flush()
    return record


# ---------------------------------------------------------------------------
# slot grid
# ---------------------------------------------------------------------------
async def test_slot_grid_is_public_and_covers_the_configured_day(
    client: AsyncClient, api_prefix: str
) -> None:
    """A pilgrim decides whether to travel before signing in (Section 2)."""
    response = await client.get(f"{api_prefix}/slots", params={"date": tomorrow().isoformat()})
    assert response.status_code == 200, response.text

    body = response.json()
    assert len(body["slots"]) == 38  # 04:00-23:00 in half-hour windows
    assert body["slots"][0]["start_time"] == "04:00:00"
    assert body["slots"][-1]["end_time"] == "23:00:00"
    assert body["walkin_reserve_pct"] == 0.25


async def test_grid_never_offers_the_walk_in_reserve(client: AsyncClient, api_prefix: str) -> None:
    """Section 5, E1: the reserve protects pilgrims without smartphones."""
    response = await client.get(f"{api_prefix}/slots", params={"date": tomorrow().isoformat()})
    for slot in response.json()["slots"]:
        assert slot["available"] == slot["capacity"] - slot["booked_count"] - slot["walkin_reserve"]
        assert slot["walkin_reserve"] > 0


async def test_grid_is_idempotent(client: AsyncClient, session: AsyncSession, api_prefix: str) -> None:
    day = tomorrow().isoformat()
    await client.get(f"{api_prefix}/slots", params={"date": day})
    await client.get(f"{api_prefix}/slots", params={"date": day})
    assert await session.scalar(select(func.count()).select_from(Slot)) == 38


async def test_past_dates_are_refused(client: AsyncClient, api_prefix: str) -> None:
    past = (now_utc() - timedelta(days=2)).date().isoformat()
    response = await client.get(f"{api_prefix}/slots", params={"date": past})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "DATE_OUT_OF_RANGE"


# ---------------------------------------------------------------------------
# booking
# ---------------------------------------------------------------------------
async def test_booking_issues_a_pass_with_qr_material(
    client: AsyncClient, api_prefix: str, auth_headers
) -> None:
    tokens = await sign_in_pilgrim(client, api_prefix)
    grid = await client.get(f"{api_prefix}/slots", params={"date": tomorrow().isoformat()})
    slot_id = grid.json()["slots"][20]["id"]

    response = await client.post(
        f"{api_prefix}/passes",
        headers=auth_headers(tokens["access_token"]),
        json={
            "slot_id": slot_id,
            "phone": PILGRIM_PHONE,
            "holder_name": "रुक्मिणी पवार",
            "group_size": 4,
            "language": "mr",
            "members": [{"name": "विठ्ठल", "age_band": "senior"}],
        },
    )
    assert response.status_code == 201, response.text

    body = response.json()
    assert body["reference"].startswith("WV")
    assert body["group_size"] == 4
    assert body["status"] == PassStatus.ACTIVE
    assert body["qr_payload"].startswith("WV1~")
    assert body["qr_secret"]  # returned once, so the device can work offline
    assert 0 < body["qr_valid_for_seconds"] <= 60
    # Every number carries its "as of" (Section 17, rule 6).
    assert body["as_of"]
    assert body["estimated_entry_at"]


async def test_booking_decrements_availability(
    client: AsyncClient, api_prefix: str, auth_headers
) -> None:
    tokens = await sign_in_pilgrim(client, api_prefix)
    day = tomorrow().isoformat()
    grid = await client.get(f"{api_prefix}/slots", params={"date": day})
    slot = grid.json()["slots"][20]

    await client.post(
        f"{api_prefix}/passes",
        headers=auth_headers(tokens["access_token"]),
        json={"slot_id": slot["id"], "phone": PILGRIM_PHONE, "holder_name": "अ", "group_size": 3},
    )

    after = await client.get(f"{api_prefix}/slots", params={"date": day})
    updated = next(s for s in after.json()["slots"] if s["id"] == slot["id"])
    assert updated["available"] == slot["available"] - 3
    assert updated["booked_count"] == 3


async def test_group_larger_than_six_is_refused(
    client: AsyncClient, api_prefix: str, auth_headers
) -> None:
    tokens = await sign_in_pilgrim(client, api_prefix)
    grid = await client.get(f"{api_prefix}/slots", params={"date": tomorrow().isoformat()})
    response = await client.post(
        f"{api_prefix}/passes",
        headers=auth_headers(tokens["access_token"]),
        json={
            "slot_id": grid.json()["slots"][20]["id"],
            "phone": PILGRIM_PHONE,
            "holder_name": "अ",
            "group_size": 7,
        },
    )
    assert response.status_code == 422  # caught by the schema before the service


async def test_cannot_book_with_someone_elses_number(
    client: AsyncClient, api_prefix: str, auth_headers
) -> None:
    tokens = await sign_in_pilgrim(client, api_prefix)
    grid = await client.get(f"{api_prefix}/slots", params={"date": tomorrow().isoformat()})
    response = await client.post(
        f"{api_prefix}/passes",
        headers=auth_headers(tokens["access_token"]),
        json={
            "slot_id": grid.json()["slots"][20]["id"],
            "phone": "9999988888",
            "holder_name": "अ",
            "group_size": 1,
        },
    )
    assert response.status_code == 403


async def test_booking_limit_is_five_per_day(client: AsyncClient, api_prefix: str, auth_headers) -> None:
    tokens = await sign_in_pilgrim(client, api_prefix)
    headers = auth_headers(tokens["access_token"])
    grid = await client.get(f"{api_prefix}/slots", params={"date": tomorrow().isoformat()})
    slots = grid.json()["slots"]

    for index in range(5):
        ok = await client.post(
            f"{api_prefix}/passes",
            headers=headers,
            json={"slot_id": slots[index]["id"], "phone": PILGRIM_PHONE, "holder_name": "अ"},
        )
        assert ok.status_code == 201, ok.text

    limited = await client.post(
        f"{api_prefix}/passes",
        headers=headers,
        json={"slot_id": slots[6]["id"], "phone": PILGRIM_PHONE, "holder_name": "अ"},
    )
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "BOOKING_LIMIT_REACHED"


async def test_booking_a_full_slot_reports_slot_full(session: AsyncSession) -> None:
    slot = await make_slot(session, start=now_utc() + timedelta(hours=2), capacity=10, walkin_reserve=4)
    for index in range(6):
        await book_via_service(session, slot, phone=f"90000000{index:02d}")

    from app.core.errors import AppError

    with pytest.raises(AppError) as exc:
        await book_via_service(session, slot, phone="9111111111")
    assert exc.value.code == "SLOT_FULL"
    assert exc.value.details["available"] == 0


# ---------------------------------------------------------------------------
# ACCEPTANCE 1 — scale without oversubscription
# ---------------------------------------------------------------------------
@pytest.mark.slow
async def test_ten_thousand_passes_never_oversubscribe_a_slot(session: AsyncSession) -> None:
    """Book 10,000 passes and assert the invariant holds on every slot.

    `booked_count + walkin_reserve <= capacity` is also a CHECK constraint, so a
    violation would raise rather than quietly corrupt — this proves the booking
    path never even attempts it.
    """
    day = tomorrow()
    await pass_service.ensure_slots(session, day)
    await session.flush()

    slots = list((await session.execute(select(Slot).where(Slot.date == day))).scalars())
    issued = 0
    for index in range(10_000):
        slot = slots[index % len(slots)]
        # No try/except: with 38 slots x 2,250 bookable seats there is room for
        # all 10,000, so any failure here is a defect and must surface.
        await book_via_service(session, slot, phone=f"9{index:09d}")
        issued += 1
        if index % 500 == 0:
            await session.flush()

    await session.flush()
    assert issued == 10_000, "every booking should have found a home in the day's grid"

    rows = (await session.execute(select(Slot).where(Slot.date == day))).scalars()
    for slot in rows:
        assert slot.booked_count + slot.walkin_reserve <= slot.capacity, (
            f"slot {slot.start_time} oversubscribed: "
            f"{slot.booked_count} booked + {slot.walkin_reserve} reserved > {slot.capacity}"
        )
        # The reserve is untouched by online booking, at every slot, always.
        assert slot.booked_count <= slot.capacity - slot.walkin_reserve

    total = await session.scalar(select(func.count()).select_from(Pass))
    assert total == 10_000


async def test_concurrent_bookings_cannot_race_past_capacity(session: AsyncSession) -> None:
    """The release-window case: many writers aiming at one row at once.

    Capacity is claimed by a single conditional UPDATE, so interleaving cannot
    produce a read-then-write that oversubscribes.
    """
    import asyncio

    slot = await make_slot(session, start=now_utc() + timedelta(hours=3), capacity=60, walkin_reserve=10)
    slot_id = slot.id
    await session.commit()

    engine = create_async_engine(TEST_DATABASE_URL)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def attempt(index: int) -> bool:
        async with factory() as own:
            try:
                await pass_service.book_pass(
                    own,
                    slot_id=slot_id,
                    phone_hash=hash_phone(f"9{index:09d}"),
                    holder_name="अ",
                    group_size=1,
                    language="mr",
                )
                await own.commit()
                return True
            except Exception:
                await own.rollback()
                return False

    # 120 racers for 50 bookable seats.
    results = await asyncio.gather(*(attempt(i) for i in range(120)))
    await engine.dispose()

    assert sum(results) == 50

    session.expire_all()
    refreshed = await session.get(Slot, slot_id)
    assert refreshed is not None
    assert refreshed.booked_count == 50
    assert refreshed.booked_count + refreshed.walkin_reserve == refreshed.capacity


# ---------------------------------------------------------------------------
# cancellation and no-shows
# ---------------------------------------------------------------------------
async def test_cancelling_returns_the_seats_immediately(session: AsyncSession) -> None:
    slot = await make_slot(session, start=now_utc() + timedelta(hours=2), capacity=100, walkin_reserve=25)
    record = await book_via_service(session, slot, phone=PILGRIM_PHONE, group_size=4)
    await session.refresh(slot)
    assert slot.booked_count == 4

    await pass_service.cancel_pass(session, record)
    await session.refresh(slot)
    assert slot.booked_count == 0
    assert record.status == PassStatus.CANCELLED


async def test_no_show_expiry_releases_capacity(session: AsyncSession) -> None:
    """A pass nobody used must not hold a seat forever (Section 4/M1)."""
    grace = await config_service.get_int(session, "pass_expiry_grace_minutes")
    start = now_utc() - timedelta(minutes=grace + 90)
    slot = await make_slot(session, start=start, capacity=100, walkin_reserve=25)
    await seed_pass(session, slot, phone=PILGRIM_PHONE, group_size=3)
    await session.refresh(slot)
    assert slot.booked_count == 3

    expired = await pass_service.expire_no_shows(session)
    assert expired == 1

    await session.refresh(slot)
    assert slot.booked_count == 0


async def test_expiry_leaves_a_pass_that_is_still_within_grace(session: AsyncSession) -> None:
    slot = await make_slot(session, start=now_utc() - timedelta(minutes=10), capacity=100)
    await seed_pass(session, slot, phone=PILGRIM_PHONE)
    assert await pass_service.expire_no_shows(session) == 0


# ---------------------------------------------------------------------------
# ACCEPTANCE 3 — scanning
# ---------------------------------------------------------------------------
async def test_scan_allows_a_valid_pass_in_its_window(session: AsyncSession, make_user) -> None:
    volunteer = await make_user(phone=VOLUNTEER_PHONE, role=Role.VOLUNTEER)
    slot = await make_slot(session, start=now_utc() - timedelta(minutes=5))
    record = await seed_pass(session, slot, phone=PILGRIM_PHONE, group_size=3)
    payload, _ = await pass_service.qr_material(session, record)

    result = await pass_service.scan_pass(
        session, qr_payload=payload, scanned_by=volunteer.id, gate_id=None
    )
    assert result.outcome == pass_service.ScanOutcome.ALLOW
    assert result.group_size == 3
    await session.refresh(record)
    assert record.status == PassStatus.SCANNED
    assert record.scanned_by == volunteer.id


async def test_a_scanned_qr_cannot_be_scanned_twice(session: AsyncSession, make_user) -> None:
    """Acceptance criterion 3, and the whole point of single-use."""
    volunteer = await make_user(phone=VOLUNTEER_PHONE, role=Role.VOLUNTEER)
    slot = await make_slot(session, start=now_utc() - timedelta(minutes=5))
    record = await seed_pass(session, slot, phone=PILGRIM_PHONE, group_size=2)
    payload, _ = await pass_service.qr_material(session, record)

    first = await pass_service.scan_pass(session, qr_payload=payload, scanned_by=volunteer.id, gate_id=None)
    assert first.outcome == pass_service.ScanOutcome.ALLOW

    second = await pass_service.scan_pass(session, qr_payload=payload, scanned_by=volunteer.id, gate_id=None)
    assert second.outcome == pass_service.ScanOutcome.INVALID
    # The volunteer's screen must say "already used at 14:03", not just "invalid".
    assert second.reason == "already_scanned"
    assert second.scanned_at is not None


async def test_scanning_records_actual_throughput(session: AsyncSession, make_user) -> None:
    """Throughput is what the reslotting job measures; it comes from scans."""
    volunteer = await make_user(phone=VOLUNTEER_PHONE, role=Role.VOLUNTEER)
    slot = await make_slot(session, start=now_utc() - timedelta(minutes=5))
    for index in range(3):
        record = await seed_pass(session, slot, phone=f"93000000{index:02d}", group_size=2)
        payload, _ = await pass_service.qr_material(session, record)
        await pass_service.scan_pass(session, qr_payload=payload, scanned_by=volunteer.id, gate_id=None)

    await session.refresh(slot)
    assert slot.actual_throughput == 6


async def test_scan_before_the_slot_returns_early(session: AsyncSession, make_user) -> None:
    volunteer = await make_user(phone=VOLUNTEER_PHONE, role=Role.VOLUNTEER)
    slot = await make_slot(session, start=now_utc() + timedelta(hours=3))
    record = await book_via_service(session, slot, phone=PILGRIM_PHONE)
    payload, _ = await pass_service.qr_material(session, record)

    result = await pass_service.scan_pass(
        session, qr_payload=payload, scanned_by=volunteer.id, gate_id=None
    )
    assert result.outcome == pass_service.ScanOutcome.EARLY
    assert result.minutes_early is not None and result.minutes_early > 120
    await session.refresh(record)
    assert record.status == PassStatus.ACTIVE  # not consumed


async def test_a_slightly_early_arrival_is_let_through(session: AsyncSession, make_user) -> None:
    """Turning an early pilgrim away sends them back into the crowd."""
    volunteer = await make_user(phone=VOLUNTEER_PHONE, role=Role.VOLUNTEER)
    slot = await make_slot(session, start=now_utc() + timedelta(minutes=10))
    record = await book_via_service(session, slot, phone=PILGRIM_PHONE)
    payload, _ = await pass_service.qr_material(session, record)

    result = await pass_service.scan_pass(
        session, qr_payload=payload, scanned_by=volunteer.id, gate_id=None
    )
    assert result.outcome == pass_service.ScanOutcome.ALLOW


async def test_scan_past_the_grace_window_returns_expired(session: AsyncSession, make_user) -> None:
    volunteer = await make_user(phone=VOLUNTEER_PHONE, role=Role.VOLUNTEER)
    grace = await config_service.get_int(session, "pass_expiry_grace_minutes")
    slot = await make_slot(session, start=now_utc() - timedelta(minutes=grace + 120))
    record = await seed_pass(session, slot, phone=PILGRIM_PHONE)
    payload, _ = await pass_service.qr_material(session, record)

    result = await pass_service.scan_pass(
        session, qr_payload=payload, scanned_by=volunteer.id, gate_id=None
    )
    assert result.outcome == pass_service.ScanOutcome.EXPIRED


async def test_a_forwarded_screenshot_is_rejected(session: AsyncSession, make_user) -> None:
    """The WhatsApp case: a QR captured five minutes ago must not open a gate."""
    volunteer = await make_user(phone=VOLUNTEER_PHONE, role=Role.VOLUNTEER)
    slot = await make_slot(session, start=now_utc() - timedelta(minutes=5))
    record = await seed_pass(session, slot, phone=PILGRIM_PHONE)

    envelope, _ = qr_service.parse_qr((await pass_service.qr_material(session, record))[0])
    stale = qr_service.build_qr(
        envelope, qr_service.rolling_code(record.qr_secret, now_utc() - timedelta(minutes=5))
    )

    result = await pass_service.scan_pass(session, qr_payload=stale, scanned_by=volunteer.id, gate_id=None)
    assert result.outcome == pass_service.ScanOutcome.INVALID
    assert result.reason == "stale_code"
    await session.refresh(record)
    assert record.status == PassStatus.ACTIVE


async def test_a_forged_pass_is_rejected(session: AsyncSession, make_user) -> None:
    volunteer = await make_user(phone=VOLUNTEER_PHONE, role=Role.VOLUNTEER)
    forged = qr_service.build_qr("eyJhbGciOiJFZERTQSJ9.ZmFrZQ.c2ln", "12345678")
    result = await pass_service.scan_pass(session, qr_payload=forged, scanned_by=volunteer.id, gate_id=None)
    assert result.outcome == pass_service.ScanOutcome.INVALID
    assert result.reason == "bad_signature"


async def test_a_cancelled_pass_does_not_open_a_gate(session: AsyncSession, make_user) -> None:
    volunteer = await make_user(phone=VOLUNTEER_PHONE, role=Role.VOLUNTEER)
    slot = await make_slot(session, start=now_utc() - timedelta(minutes=5))
    record = await seed_pass(session, slot, phone=PILGRIM_PHONE)
    payload, _ = await pass_service.qr_material(session, record)
    await pass_service.cancel_pass(session, record)

    result = await pass_service.scan_pass(
        session, qr_payload=payload, scanned_by=volunteer.id, gate_id=None
    )
    assert result.outcome == pass_service.ScanOutcome.INVALID
    assert result.reason == "cancelled"


async def test_pilgrims_cannot_scan(client: AsyncClient, api_prefix: str, auth_headers) -> None:
    tokens = await sign_in_pilgrim(client, api_prefix)
    response = await client.post(
        f"{api_prefix}/checkpoints/scan",
        headers=auth_headers(tokens["access_token"]),
        json={"qr_payload": "WV1~a~12345678"},
    )
    assert response.status_code == 403
    assert "pass:scan" in response.json()["error"]["details"]["missing_permissions"]


async def test_scan_endpoint_audits_every_outcome(
    client: AsyncClient, session: AsyncSession, api_prefix: str, auth_headers, make_user
) -> None:
    """Who was turned away and why is what makes queue integrity arguable."""
    token = await sign_in_volunteer(client, api_prefix, make_user)
    response = await client.post(
        f"{api_prefix}/checkpoints/scan",
        headers=auth_headers(token),
        json={"qr_payload": "WV1~garbage~12345678"},
    )
    assert response.status_code == 200
    assert response.json()["outcome"] == "INVALID"

    from app.models import AuditLog
    from app.services.audit_service import AuditAction

    scans = await session.scalar(
        select(func.count()).select_from(AuditLog).where(AuditLog.action == AuditAction.PASS_SCANNED)
    )
    assert scans == 1


# ---------------------------------------------------------------------------
# offline verification
# ---------------------------------------------------------------------------
async def test_day_key_lets_a_scanner_verify_without_the_database(
    client: AsyncClient, session: AsyncSession, api_prefix: str, auth_headers, make_user
) -> None:
    token = await sign_in_volunteer(client, api_prefix, make_user)
    slot = await make_slot(session, start=now_utc() + timedelta(minutes=20))
    record = await book_via_service(session, slot, phone=PILGRIM_PHONE, group_size=5)
    payload, _ = await pass_service.qr_material(session, record)
    await session.commit()

    key_response = await client.get(f"{api_prefix}/checkpoints/day-key", headers=auth_headers(token))
    assert key_response.status_code == 200
    assert key_response.json()["algorithm"] == "Ed25519"

    # Verification uses only the cached key — no session, no network.
    envelope, _code = qr_service.parse_qr(payload)
    claims = qr_service.verify_envelope(envelope)
    assert claims.reference == record.reference
    assert claims.group_size == 5


async def test_scanner_bundle_is_scoped_to_the_next_few_hours(
    client: AsyncClient, session: AsyncSession, api_prefix: str, auth_headers, make_user
) -> None:
    """A lost scanner must expose a window, not the whole Wari."""
    token = await sign_in_volunteer(client, api_prefix, make_user)
    soon = await make_slot(session, start=now_utc() + timedelta(hours=1))
    later = await make_slot(session, start=now_utc() + timedelta(hours=9))
    await book_via_service(session, soon, phone="9700000001")
    await book_via_service(session, later, phone="9700000002")
    await session.commit()

    response = await client.get(
        f"{api_prefix}/checkpoints/bundle", params={"hours_ahead": 3}, headers=auth_headers(token)
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body["passes"]) == 1
    assert body["day_key"]["public_key_b64"]


# ---------------------------------------------------------------------------
# ACCEPTANCE 2 — reslotting
# ---------------------------------------------------------------------------
async def _build_slow_gate(session: AsyncSession, *, shortfall: float) -> list[Pass]:
    """A slot that just ran at `1 - shortfall` of plan, plus downstream passes."""
    now = now_utc()
    finished_start = now - timedelta(minutes=30)
    finished = await make_slot(session, start=finished_start, capacity=100, walkin_reserve=25)
    finished.actual_throughput = int(100 * (1 - shortfall))

    downstream: list[Pass] = []
    for index in range(1, 5):
        slot = await make_slot(
            session, start=now + timedelta(minutes=30 * index), capacity=100, walkin_reserve=25
        )
        downstream.append(await book_via_service(session, slot, phone=f"96000000{index:02d}"))

    await session.flush()
    return downstream


async def test_forced_forty_percent_drop_shifts_downstream_passes(session: AsyncSession) -> None:
    """Acceptance criterion 2, exactly as written in the spec."""
    passes = await _build_slow_gate(session, shortfall=0.40)
    original_slots = {p.id: p.slot_id for p in passes}

    window = await pass_service.measure_throughput(session)
    assert window.planned > 0
    assert reslot_service.deviation(window) < -0.20

    decision = reslot_service.decide(
        window,
        deviation_threshold=await config_service.get_float(session, "reslot_deviation_pct"),
        slot_minutes=await config_service.get_int(session, "slot_minutes"),
    )
    assert decision.should_reslot
    assert decision.delay_minutes > 0

    moved = await pass_service.apply_reslot(session, decision)
    assert moved > 0

    for record in passes:
        await session.refresh(record)
    shifted = [p for p in passes if p.slot_id != original_slots[p.id]]
    assert shifted, "downstream passes should have moved"
    for record in shifted:
        assert record.reslot_count == 1
        assert record.original_slot_id == original_slots[record.id]


async def test_reslotting_queues_a_notification_per_moved_pass(session: AsyncSession) -> None:
    passes = await _build_slow_gate(session, shortfall=0.40)
    window = await pass_service.measure_throughput(session)
    decision = reslot_service.decide(window, deviation_threshold=0.20, slot_minutes=30)
    moved = await pass_service.apply_reslot(session, decision)

    queued = (await session.execute(select(PassNotification))).scalars().all()
    assert len(queued) == len(passes)  # moved, or told the truth about overflow
    assert moved > 0
    for notice in queued:
        assert notice.type == "reslot"
        assert notice.status == "queued"
        assert notice.payload_mr  # Marathi is not optional
        assert notice.payload_en


async def test_a_small_shortfall_does_not_disturb_anyone(session: AsyncSession) -> None:
    """Reslotting buzzes tens of thousands of phones; a 10% wobble is noise."""
    passes = await _build_slow_gate(session, shortfall=0.10)
    original = {p.id: p.slot_id for p in passes}

    window = await pass_service.measure_throughput(session)
    decision = reslot_service.decide(window, deviation_threshold=0.20, slot_minutes=30)
    assert not decision.should_reslot
    assert await pass_service.apply_reslot(session, decision) == 0

    for record in passes:
        await session.refresh(record)
        assert record.slot_id == original[record.id]
    assert await session.scalar(select(func.count()).select_from(PassNotification)) == 0


async def test_a_pass_already_in_its_slot_is_not_moved(session: AsyncSession) -> None:
    """Someone queueing for their slot is in the building."""
    now = now_utc()
    finished = await make_slot(session, start=now - timedelta(minutes=30), capacity=100, walkin_reserve=25)
    finished.actual_throughput = 40

    current = await make_slot(session, start=now - timedelta(minutes=5), capacity=100, walkin_reserve=25)
    in_progress = await seed_pass(session, current, phone="9650000001")
    original_slot = in_progress.slot_id
    await session.flush()

    window = await pass_service.measure_throughput(session)
    decision = reslot_service.decide(window, deviation_threshold=0.20, slot_minutes=30)
    await pass_service.apply_reslot(session, decision)

    await session.refresh(in_progress)
    assert in_progress.slot_id == original_slot
    assert in_progress.reslot_count == 0


async def test_reslotting_moves_seats_between_slots(session: AsyncSession) -> None:
    """Capacity has to follow the pass, or the target slot oversubscribes."""
    passes = await _build_slow_gate(session, shortfall=0.40)
    window = await pass_service.measure_throughput(session)
    decision = reslot_service.decide(window, deviation_threshold=0.20, slot_minutes=30)
    await pass_service.apply_reslot(session, decision)

    rows = (await session.execute(select(Slot))).scalars().all()
    for slot in rows:
        assert slot.booked_count >= 0
        assert slot.booked_count + slot.walkin_reserve <= slot.capacity

    total_booked = sum(s.booked_count for s in rows)
    assert total_booked == sum(p.group_size for p in passes)


async def test_admin_can_run_the_reslot_job_on_demand(
    client: AsyncClient, session: AsyncSession, api_prefix: str, auth_headers, make_user
) -> None:
    """The demo beat: an operator forces a reslot instead of waiting 5 minutes."""
    import pyotp

    from app.core.security import generate_mfa_secret

    secret = generate_mfa_secret()
    await make_user(
        phone="9820044556", role=Role.ADMINISTRATOR, password=STAFF_PASSWORD, mfa_secret=secret
    )
    challenge = await client.post(
        f"{api_prefix}/auth/login", json={"phone": "9820044556", "password": STAFF_PASSWORD}
    )
    verified = await client.post(
        f"{api_prefix}/auth/mfa/verify",
        json={"mfa_token": challenge.json()["mfa_token"], "code": pyotp.TOTP(secret).now()},
    )
    headers = auth_headers(verified.json()["access_token"])

    await _build_slow_gate(session, shortfall=0.40)
    await session.commit()

    response = await client.post(f"{api_prefix}/admin/reslot/run", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["should_reslot"] is True
    assert body["passes_moved"] > 0
    assert body["deviation"] < -0.2
    assert "below plan" in body["reason"]


# ---------------------------------------------------------------------------
# reading a pass
# ---------------------------------------------------------------------------
async def test_pass_lookup_reports_an_honest_eta(
    client: AsyncClient, api_prefix: str, auth_headers
) -> None:
    tokens = await sign_in_pilgrim(client, api_prefix)
    headers = auth_headers(tokens["access_token"])
    grid = await client.get(f"{api_prefix}/slots", params={"date": tomorrow().isoformat()})
    booked = await client.post(
        f"{api_prefix}/passes",
        headers=headers,
        json={"slot_id": grid.json()["slots"][20]["id"], "phone": PILGRIM_PHONE, "holder_name": "अ"},
    )
    pass_id = booked.json()["id"]

    response = await client.get(f"{api_prefix}/passes/{pass_id}", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["queue_ahead"] >= 0
    assert datetime.fromisoformat(body["estimated_entry_at"]) > now_utc()
    assert body["was_reslotted"] is False


async def test_a_pilgrim_cannot_read_someone_elses_pass(
    client: AsyncClient, session: AsyncSession, api_prefix: str, auth_headers
) -> None:
    slot = await make_slot(session, start=now_utc() + timedelta(hours=2))
    other = await book_via_service(session, slot, phone="9555500001")
    await session.commit()

    tokens = await sign_in_pilgrim(client, api_prefix)
    response = await client.get(
        f"{api_prefix}/passes/{other.id}", headers=auth_headers(tokens["access_token"])
    )
    # Not 403: confirming the pass exists would leak that it does.
    assert response.status_code == 404


async def test_qr_endpoint_rotates_its_payload(
    client: AsyncClient, session: AsyncSession, api_prefix: str, auth_headers
) -> None:
    tokens = await sign_in_pilgrim(client, api_prefix)
    headers = auth_headers(tokens["access_token"])
    grid = await client.get(f"{api_prefix}/slots", params={"date": tomorrow().isoformat()})
    booked = await client.post(
        f"{api_prefix}/passes",
        headers=headers,
        json={"slot_id": grid.json()["slots"][20]["id"], "phone": PILGRIM_PHONE, "holder_name": "अ"},
    )
    pass_id = booked.json()["id"]

    response = await client.get(f"{api_prefix}/passes/{pass_id}/qr", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["rotates_every_seconds"] == 60
    assert 0 < body["valid_for_seconds"] <= 60


async def test_cancel_endpoint_frees_the_seat(
    client: AsyncClient, api_prefix: str, auth_headers
) -> None:
    tokens = await sign_in_pilgrim(client, api_prefix)
    headers = auth_headers(tokens["access_token"])
    day = tomorrow().isoformat()
    grid = await client.get(f"{api_prefix}/slots", params={"date": day})
    slot = grid.json()["slots"][20]

    booked = await client.post(
        f"{api_prefix}/passes",
        headers=headers,
        json={"slot_id": slot["id"], "phone": PILGRIM_PHONE, "holder_name": "अ", "group_size": 2},
    )
    cancelled = await client.post(f"{api_prefix}/passes/{booked.json()['id']}/cancel", headers=headers)
    assert cancelled.status_code == 200

    after = await client.get(f"{api_prefix}/slots", params={"date": day})
    updated = next(s for s in after.json()["slots"] if s["id"] == slot["id"])
    assert updated["available"] == slot["available"]


async def test_slot_ids_are_uuids(client: AsyncClient, api_prefix: str) -> None:
    grid = await client.get(f"{api_prefix}/slots", params={"date": tomorrow().isoformat()})
    uuid.UUID(grid.json()["slots"][0]["id"])


async def test_time_fields_survive_the_round_trip(client: AsyncClient, api_prefix: str) -> None:
    grid = await client.get(f"{api_prefix}/slots", params={"date": tomorrow().isoformat()})
    first = grid.json()["slots"][0]
    assert time.fromisoformat(first["start_time"]) == time(4, 0)
    assert date.fromisoformat(first["date"]) == tomorrow()
