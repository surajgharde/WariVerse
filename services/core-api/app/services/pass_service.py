"""Darshan pass operations: slot materialisation, booking, scanning, expiry.

The arithmetic lives in `slot_service` and `reslot_service` (pure, tested
exhaustively).  This module is the thin layer that talks to Postgres — and the
one place that has to be right about concurrency, because 5,000 bookings a
minute at a release window all aim at the same handful of rows.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum

from sqlalchemy import Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.core.logging import get_logger
from app.core.security import now_utc
from app.models import Gate, Pass, PassMember, PassNotification, PassStatus, Slot, SlotStatus
from app.services import config_service, qr_service, reslot_service, slot_service

logger = get_logger(__name__)

#: How far ahead a pilgrim may book.  Long enough to plan travel, short enough
#: that capacity changes do not invalidate half a lakh of passes.
BOOKING_HORIZON_DAYS = 30
REFERENCE_ALPHABET = "ACDEFGHJKLMNPQRTUVWXY349"  # no 0/O/1/I/S/5 — read aloud at a gate


class ScanOutcome(StrEnum):
    """The four results Section 4/M1 specifies.

    A pass that was already scanned returns INVALID with
    `reason="already_scanned"` rather than a fifth code: the contract stays as
    written, while the volunteer's screen can still say "used at Gate 2, 14:03"
    instead of an unhelpful "invalid".
    """

    ALLOW = "ALLOW"
    EARLY = "EARLY"
    EXPIRED = "EXPIRED"
    INVALID = "INVALID"


@dataclass(frozen=True, slots=True)
class ScanResult:
    outcome: ScanOutcome
    reason: str
    message_mr: str
    pass_reference: str | None = None
    group_size: int | None = None
    slot_start: datetime | None = None
    slot_end: datetime | None = None
    scanned_at: datetime | None = None
    minutes_early: int | None = None


@dataclass(frozen=True, slots=True)
class PassView:
    record: Pass
    slot: Slot
    gate_code: str | None
    queue_ahead: int
    eta: datetime
    is_reslotted: bool


def _reference() -> str:
    return "WV" + "".join(secrets.choice(REFERENCE_ALPHABET) for _ in range(8))


def slot_bounds(slot: Slot) -> tuple[datetime, datetime]:
    start = datetime.combine(slot.date, slot.start_time, tzinfo=UTC)
    end = datetime.combine(slot.date, slot.end_time, tzinfo=UTC)
    return start, end


# ---------------------------------------------------------------------------
# slot materialisation
# ---------------------------------------------------------------------------
async def ensure_slots(session: AsyncSession, day: date, gate_id: uuid.UUID | None = None) -> int:
    """Create the day's slot grid if it is not there yet.  Idempotent."""
    existing = await session.scalar(
        select(func.count()).select_from(Slot).where(Slot.date == day, Slot.gate_id == gate_id)
    )
    if existing:
        return 0

    plans = slot_service.plan_day(
        throughput_per_hour=await config_service.get_int(session, "temple_throughput_per_hour"),
        slot_minutes=await config_service.get_int(session, "slot_minutes"),
        day_start=await config_service.get_time(session, "day_start"),
        day_end=await config_service.get_time(session, "day_end"),
        reserve_pct=await config_service.get_float(session, "walkin_reserve_pct"),
    )
    for plan in plans:
        session.add(
            Slot(
                date=day,
                start_time=plan.start,
                end_time=plan.end,
                capacity=plan.capacity,
                booked_count=0,
                walkin_reserve=plan.walkin_reserve,
                status=SlotStatus.OPEN,
                gate_id=gate_id,
            )
        )
    await session.flush()
    logger.info("slots_created", extra={"date": day.isoformat(), "count": len(plans)})
    return len(plans)


def _slots_for_date(day: date) -> Select[tuple[Slot]]:
    return select(Slot).where(Slot.date == day).order_by(Slot.start_time)


async def list_slots(session: AsyncSession, day: date) -> list[Slot]:
    await _assert_bookable_date(day)
    await ensure_slots(session, day)
    rows = await session.execute(_slots_for_date(day))
    return list(rows.scalars())


async def _assert_bookable_date(day: date) -> None:
    today = now_utc().date()
    if day < today or day > today + timedelta(days=BOOKING_HORIZON_DAYS):
        raise AppError(
            "DATE_OUT_OF_RANGE",
            details={"date": day.isoformat(), "horizon_days": BOOKING_HORIZON_DAYS},
        )


# ---------------------------------------------------------------------------
# booking
# ---------------------------------------------------------------------------
async def _claim_seats(session: AsyncSession, slot_id: uuid.UUID, seats: int) -> bool:
    """Atomically take `seats` from a slot, or report that it is full.

    One conditional UPDATE.  The predicate re-checks capacity inside the same
    statement, so twenty thousand concurrent bookings cannot interleave a read
    and a write — and the `no_oversubscription` CHECK constraint is the second
    line of defence if this is ever rewritten carelessly.
    """
    result = await session.execute(
        update(Slot)
        .where(
            Slot.id == slot_id,
            Slot.status == SlotStatus.OPEN,
            Slot.booked_count + seats + Slot.walkin_reserve <= Slot.capacity,
        )
        .values(booked_count=Slot.booked_count + seats)
    )
    return bool(result.rowcount)


async def _release_seats(session: AsyncSession, slot_id: uuid.UUID, seats: int) -> None:
    await session.execute(
        update(Slot)
        .where(Slot.id == slot_id)
        .values(booked_count=func.greatest(Slot.booked_count - seats, 0))
    )


async def book_pass(
    session: AsyncSession,
    *,
    slot_id: uuid.UUID,
    phone_hash: str,
    holder_name: str,
    group_size: int,
    language: str,
    members: list[tuple[str, str | None]] | None = None,
    allow_early_reslot: bool = False,
) -> Pass:
    """Issue one pass covering up to six people.  One QR, one scan."""
    try:
        slot_service.validate_group_size(group_size)
    except ValueError as exc:
        raise AppError("GROUP_TOO_LARGE", details={"group_size": group_size}) from exc

    slot = await session.get(Slot, slot_id)
    if slot is None:
        raise AppError("SLOT_NOT_FOUND", details={"slot_id": str(slot_id)})
    if slot.status != SlotStatus.OPEN:
        raise AppError("SLOT_CLOSED", details={"status": slot.status})

    start, _ = slot_bounds(slot)
    if start <= now_utc():
        raise AppError(
            "SLOT_CLOSED",
            message="That slot has already started.",
            message_mr="ती वेळ आधीच सुरू झाली आहे.",
        )

    if not await _claim_seats(session, slot_id, group_size):
        await session.refresh(slot)
        raise AppError(
            "SLOT_FULL",
            details={
                "slot_id": str(slot_id),
                "available": slot_service.available_seats(
                    slot_service.SlotState(
                        capacity=slot.capacity,
                        booked_count=slot.booked_count,
                        walkin_reserve=slot.walkin_reserve,
                        status=slot.status,
                    )
                ),
                "requested": group_size,
            },
        )

    record = Pass(
        reference=_reference(),
        slot_id=slot_id,
        holder_phone_hash=phone_hash,
        holder_name=holder_name,
        holder_language=language,
        group_size=group_size,
        qr_secret=qr_service.new_pass_secret(),
        status=PassStatus.ACTIVE,
        issued_at=now_utc(),
        original_slot_id=slot_id,
        allow_early_reslot=allow_early_reslot,
    )
    session.add(record)
    await session.flush()

    for name, age_band in members or []:
        session.add(PassMember(pass_id=record.id, name=name, age_band=age_band))

    await session.flush()
    return record


async def count_passes_today(session: AsyncSession, phone_hash: str) -> int:
    """Bookings made in the last 24h, for the 5/day/phone limit (Section 9)."""
    since = now_utc() - timedelta(days=1)
    return (
        await session.scalar(
            select(func.count())
            .select_from(Pass)
            .where(
                Pass.holder_phone_hash == phone_hash,
                Pass.issued_at >= since,
                Pass.status != PassStatus.CANCELLED,
            )
        )
        or 0
    )


async def cancel_pass(session: AsyncSession, record: Pass) -> None:
    if record.status == PassStatus.SCANNED:
        raise AppError("PASS_ALREADY_USED")
    if record.status == PassStatus.CANCELLED:
        return

    record.status = PassStatus.CANCELLED
    record.cancelled_at = now_utc()
    # Seats go straight back into the pool — a cancelled pass that keeps
    # holding capacity is a slot nobody can use.
    await _release_seats(session, record.slot_id, record.group_size)
    await session.flush()


# ---------------------------------------------------------------------------
# reading a pass
# ---------------------------------------------------------------------------
async def load_pass(session: AsyncSession, pass_id: uuid.UUID) -> Pass:
    record = await session.get(Pass, pass_id)
    if record is None:
        raise AppError("PASS_NOT_FOUND", details={"pass_id": str(pass_id)})
    return record


async def load_pass_by_reference(session: AsyncSession, reference: str) -> Pass:
    record = await session.scalar(select(Pass).where(Pass.reference == reference.strip().upper()))
    if record is None:
        raise AppError("PASS_NOT_FOUND", details={"reference": reference})
    return record


async def describe_pass(session: AsyncSession, record: Pass) -> PassView:
    """Pass plus a live, honest wait estimate."""
    slot = await session.get(Slot, record.slot_id)
    if slot is None:  # pragma: no cover - FK RESTRICT prevents this
        raise AppError("SLOT_NOT_FOUND")

    start, _ = slot_bounds(slot)
    gate_code = None
    if slot.gate_id:
        gate = await session.get(Gate, slot.gate_id)
        gate_code = gate.code if gate else None

    # Everyone booked into earlier slots on the same day who has not yet been
    # scanned — that is the queue genuinely ahead of this pilgrim.
    queue_ahead = (
        await session.scalar(
            select(func.coalesce(func.sum(Pass.group_size), 0))
            .select_from(Pass)
            .join(Slot, Slot.id == Pass.slot_id)
            .where(
                Slot.date == slot.date,
                Slot.start_time < slot.start_time,
                Pass.status == PassStatus.ACTIVE,
            )
        )
        or 0
    )

    throughput = await config_service.get_int(session, "temple_throughput_per_hour")
    wait = slot_service.estimate_wait(
        now=now_utc(),
        slot_start=start,
        queue_ahead=int(queue_ahead),
        throughput_per_hour=throughput,
    )

    return PassView(
        record=record,
        slot=slot,
        gate_code=gate_code,
        queue_ahead=int(queue_ahead),
        eta=now_utc() + wait,
        is_reslotted=record.reslot_count > 0,
    )


async def qr_material(session: AsyncSession, record: Pass) -> tuple[str, int]:
    """Build the QR string for right now, and how long it stays valid."""
    if record.status == PassStatus.CANCELLED:
        raise AppError("PASS_CANCELLED")

    slot = await session.get(Slot, record.slot_id)
    if slot is None:  # pragma: no cover
        raise AppError("SLOT_NOT_FOUND")
    start, end = slot_bounds(slot)

    gate_code = None
    if slot.gate_id:
        gate = await session.get(Gate, slot.gate_id)
        gate_code = gate.code if gate else None

    grace = await config_service.get_int(session, "pass_expiry_grace_minutes")
    envelope = qr_service.mint_envelope(
        pass_id=str(record.id),
        reference=record.reference,
        slot_start=start,
        slot_end=end,
        group_size=record.group_size,
        gate_code=gate_code,
        issued_at=now_utc(),
        grace_minutes=grace,
    )
    code = qr_service.rolling_code(record.qr_secret)
    return qr_service.build_qr(envelope, code), qr_service.seconds_until_rotation()


# ---------------------------------------------------------------------------
# scanning
# ---------------------------------------------------------------------------
#: How long before a slot opens a pilgrim may be let through.  Wide enough that
#: an early arrival is not turned away into the crowd, narrow enough that the
#: slot still means something.
EARLY_TOLERANCE_MINUTES = 15


async def scan_pass(
    session: AsyncSession,
    *,
    qr_payload: str,
    scanned_by: uuid.UUID,
    gate_id: uuid.UUID | None,
    at: datetime | None = None,
) -> ScanResult:
    """Validate a scanned QR and, if it passes, consume it.

    Order matters: authenticity first (cheap, offline-verifiable), then
    freshness, then state, then time window.  A forged code should never reach
    a database lookup.
    """
    moment = at or now_utc()

    try:
        envelope, code = qr_service.parse_qr(qr_payload)
        claims = qr_service.verify_envelope(envelope)
    except AppError as exc:
        if exc.code == "PASS_EXPIRED":
            return ScanResult(
                ScanOutcome.EXPIRED,
                "envelope_expired",
                "या पासची मुदत संपली आहे.",
            )
        return ScanResult(ScanOutcome.INVALID, "bad_signature", "हा कोड वैध नाही.")

    try:
        record = await load_pass(session, uuid.UUID(claims.pass_id))
    except (AppError, ValueError):
        return ScanResult(ScanOutcome.INVALID, "unknown_pass", "हा पास आढळला नाही.")

    if not qr_service.verify_rolling_code(record.qr_secret, code, moment):
        # Authentic pass, stale code: a forwarded screenshot, or a phone that
        # has not refreshed. The volunteer asks them to reopen the pass screen.
        return ScanResult(
            ScanOutcome.INVALID,
            "stale_code",
            "हा कोड जुना आहे. पास स्क्रीन पुन्हा उघडा.",
            pass_reference=record.reference,
        )

    if record.status == PassStatus.SCANNED:
        return ScanResult(
            ScanOutcome.INVALID,
            "already_scanned",
            "हा पास आधीच वापरला आहे.",
            pass_reference=record.reference,
            scanned_at=record.scanned_at,
            group_size=record.group_size,
        )

    if record.status == PassStatus.CANCELLED:
        return ScanResult(
            ScanOutcome.INVALID, "cancelled", "हा पास रद्द केला आहे.", pass_reference=record.reference
        )

    if record.status == PassStatus.EXPIRED:
        return ScanResult(
            ScanOutcome.EXPIRED, "expired", "या पासची मुदत संपली आहे.", pass_reference=record.reference
        )

    slot = await session.get(Slot, record.slot_id)
    if slot is None:  # pragma: no cover
        return ScanResult(ScanOutcome.INVALID, "slot_missing", "वेळ आढळली नाही.")
    start, end = slot_bounds(slot)

    grace = await config_service.get_int(session, "pass_expiry_grace_minutes")
    if moment > end + timedelta(minutes=grace):
        return ScanResult(
            ScanOutcome.EXPIRED,
            "past_grace_window",
            "या पासची मुदत संपली आहे.",
            pass_reference=record.reference,
            slot_start=start,
            slot_end=end,
        )

    if moment < start - timedelta(minutes=EARLY_TOLERANCE_MINUTES):
        minutes_early = int((start - moment).total_seconds() // 60)
        return ScanResult(
            ScanOutcome.EARLY,
            "before_slot",
            f"तुमची वेळ {start.strftime('%H:%M')} आहे. कृपया त्या वेळेवर या.",
            pass_reference=record.reference,
            slot_start=start,
            slot_end=end,
            minutes_early=minutes_early,
        )

    # Consume it.  Single-use is enforced by the status check above plus this
    # write inside the caller's transaction.
    record.status = PassStatus.SCANNED
    record.scanned_at = moment
    record.scanned_by = scanned_by
    record.scanned_gate_id = gate_id
    slot.actual_throughput += record.group_size
    await session.flush()

    return ScanResult(
        ScanOutcome.ALLOW,
        "ok",
        f"प्रवेश द्या — {record.group_size} जण.",
        pass_reference=record.reference,
        group_size=record.group_size,
        slot_start=start,
        slot_end=end,
        scanned_at=moment,
    )


# ---------------------------------------------------------------------------
# background jobs
# ---------------------------------------------------------------------------
async def expire_no_shows(session: AsyncSession, at: datetime | None = None) -> int:
    """Expire unscanned passes past their grace window and free the seats."""
    moment = at or now_utc()
    grace = await config_service.get_int(session, "pass_expiry_grace_minutes")
    cutoff = moment - timedelta(minutes=grace)

    rows = await session.execute(
        select(Pass, Slot)
        .join(Slot, Slot.id == Pass.slot_id)
        .where(Pass.status == PassStatus.ACTIVE)
    )

    expired = 0
    for record, slot in rows.all():
        _, end = slot_bounds(slot)
        if end <= cutoff:
            record.status = PassStatus.EXPIRED
            await _release_seats(session, slot.id, record.group_size)
            expired += 1

    if expired:
        await session.flush()
        logger.info("passes_expired", extra={"count": expired})
    return expired


async def measure_throughput(
    session: AsyncSession, *, at: datetime | None = None, window_minutes: int = 30
) -> reslot_service.ThroughputWindow:
    """Planned versus actual gate throughput over the trailing window."""
    moment = at or now_utc()
    window_start = moment - timedelta(minutes=window_minutes)

    rows = await session.execute(
        select(Slot).where(Slot.date.in_([window_start.date(), moment.date()]))
    )
    planned = 0
    actual = 0
    for slot in rows.scalars():
        start, end = slot_bounds(slot)
        if end <= window_start or start >= moment:
            continue
        # Pro-rate a slot that only partly overlaps the window.
        overlap = min(end, moment) - max(start, window_start)
        slot_length = end - start
        share = overlap / slot_length if slot_length else 0
        planned += int(slot.capacity * share)
        actual += int(slot.actual_throughput * share)

    return reslot_service.ThroughputWindow(planned=planned, actual=actual, minutes=window_minutes)


async def apply_reslot(
    session: AsyncSession,
    decision: reslot_service.ReslotDecision,
    *,
    at: datetime | None = None,
) -> int:
    """Shift downstream unfulfilled passes and queue their notifications."""
    if not decision.should_reslot:
        return 0

    moment = at or now_utc()

    rows = await session.execute(
        select(Pass, Slot)
        .join(Slot, Slot.id == Pass.slot_id)
        .where(Pass.status == PassStatus.ACTIVE)
        .order_by(Slot.date, Slot.start_time)
    )
    candidates = [
        (record, slot)
        for record, slot in rows.all()
        if reslot_service.is_downstream(slot_bounds(slot)[0], moment)
    ]
    if not candidates:
        return 0

    # Cache target slots so a thousand passes moving into the same window do
    # not each issue their own lookup.
    targets: dict[tuple[date, datetime], Slot | None] = {}
    moved = 0

    for record, slot in candidates:
        start, _ = slot_bounds(slot)
        new_start = reslot_service.shifted_start(start, decision.delay_minutes)
        key = (new_start.date(), new_start)

        if key not in targets:
            targets[key] = await session.scalar(
                select(Slot).where(
                    Slot.date == new_start.date(),
                    Slot.start_time == new_start.time(),
                    Slot.gate_id == slot.gate_id,
                )
            )
        target = targets[key]
        if target is None:
            # Shifted past the end of the day.  The pass keeps its slot and the
            # notification tells the truth rather than inventing a window that
            # does not exist.
            _queue_reslot_notification(
                session, record=record, original_start=start, new_start=start, overflow=True
            )
            continue

        if target.id == slot.id:
            continue

        await _release_seats(session, slot.id, record.group_size)
        await session.execute(
            update(Slot).where(Slot.id == target.id).values(booked_count=Slot.booked_count + record.group_size)
        )
        record.slot_id = target.id
        record.reslot_count += 1
        _queue_reslot_notification(
            session, record=record, original_start=start, new_start=new_start, overflow=False
        )
        moved += 1

    await session.flush()
    logger.info(
        "passes_reslotted",
        extra={"count": moved, "delay_minutes": decision.delay_minutes, "reason": decision.reason},
    )
    return moved


def _queue_reslot_notification(
    session: AsyncSession,
    *,
    record: Pass,
    original_start: datetime,
    new_start: datetime,
    overflow: bool,
) -> None:
    if overflow:
        mr = (
            f"पास {record.reference}: रांग हळू चालत असल्याने आजची वेळ मिळणे अवघड आहे. "
            f"कृपया मदत कक्षाशी संपर्क साधा."
        )
        en = (
            f"Pass {record.reference}: the queue is running slow and today's remaining slots "
            f"are full. Please speak to a help desk."
        )
    else:
        mr, en = reslot_service.notification_text(
            original_start=original_start, new_start=new_start, reference=record.reference
        )

    session.add(
        PassNotification(
            pass_id=record.id,
            type="reslot",
            channel="push",
            payload_mr=mr,
            payload_en=en,
            status="queued",
        )
    )


async def scanner_bundle(
    session: AsyncSession, *, gate_id: uuid.UUID | None, hours_ahead: int = 3
) -> list[dict[str, object]]:
    """Offline verification material for one gate's scanner.

    Deliberately narrow: only active passes whose slot falls in the next few
    hours at this gate.  A scanner never holds the whole day's secrets, so a
    lost device exposes a bounded window rather than every pass at the Wari.
    """
    moment = now_utc()
    horizon = moment + timedelta(hours=hours_ahead)

    stmt = (
        select(Pass, Slot)
        .join(Slot, Slot.id == Pass.slot_id)
        .where(Pass.status == PassStatus.ACTIVE, Slot.date.in_([moment.date(), horizon.date()]))
    )
    if gate_id is not None:
        stmt = stmt.where(Slot.gate_id == gate_id)

    rows = await session.execute(stmt)
    bundle: list[dict[str, object]] = []
    for record, slot in rows.all():
        start, end = slot_bounds(slot)
        if start > horizon or end < moment - timedelta(hours=1):
            continue
        bundle.append(
            {
                "pass_id": str(record.id),
                "reference": record.reference,
                "qr_secret": record.qr_secret,
                "slot_start": start.isoformat(),
                "slot_end": end.isoformat(),
                "group_size": record.group_size,
            }
        )
    return bundle
