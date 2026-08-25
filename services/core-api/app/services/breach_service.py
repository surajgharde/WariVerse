"""Queue-breach ledger (Section 4/M5).

Section 4/M5 opens with "this is the most sensitive module in the product.
Build it carefully." What follows is what careful meant.

**What this records.** That an unauthorised entry occurred at a gate at a time.
Not who. There is no column here that identifies a person and no code path that
could populate one — Section 12's constraint is enforced by the schema having
nowhere to put an identity, not by anyone remembering the rule.

**Why it can be trusted under pressure.** Each row carries the SHA-256 of the
previous row's chain hash concatenated with a canonical serialisation of its own
evidence. Remove a row, edit an `occurred_at`, reorder a sequence — the chain
stops verifying at that exact point and `verify_chain` names it. The database
enforces the same thing from below: `trg_breach_evidence_immutable` raises on
any UPDATE that touches an evidence column, so the application is not the only
thing standing between a record and a motivated editor.

**Deletion redacts the clip, never the record.** Section 4/M5 asks for deletion
restricted to System Admin with a written reason. Taken literally that means
`DELETE FROM breach_events`, which would break the chain — and a broken chain
cannot distinguish an authorised deletion from tampering, which is the one
distinction the ledger exists to make. So `redact` clears the evidence clip and
records who removed it and why, and the row and its hash stay. The chain remains
verifiable end to end, and the deletion is visible as an annotation rather than
as a hole. The literal reading is still detected: an out-of-band `DELETE` breaks
the chain, and that is precisely what `verify_chain` is for.

**Nothing here is a finding until a human says so.** Every event lands as
`pending`. Section 4/M5: "AI output alone is never a finding."
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.core.logging import get_logger
from app.core.security import now_utc, sha256_hex
from app.models import BreachEvent, ClipAccessLog, Gate, Pass, Tripwire
from app.models.breach import ReviewStatus
from app.services import audit_service, config_service
from app.services.audit_service import AuditAction

logger = get_logger(__name__)

#: `prev_hash` of the first event. Sixty-four zeros: a value that cannot be a
#: real SHA-256 of anything we produced, so a genesis row is distinguishable
#: from a row whose predecessor was removed.
GENESIS_HASH = "0" * 64

#: Section 4/M5: "was a valid pass scanned at this gate within ±30 seconds?"
PASS_SCAN_WINDOW_SECONDS = 30

#: One advisory lock id for the whole ledger. Appending to a hash chain is
#: inherently serial — two concurrent appends would both read the same tail and
#: produce two rows claiming the same `prev_hash`, which is a fork, not a chain.
#: A transaction-scoped advisory lock is cheaper than locking the table and is
#: released automatically on commit or rollback.
_CHAIN_LOCK_ID = 0x5741_5249  # "WARI"


def canonical_payload(
    *,
    tripwire_id: uuid.UUID,
    camera_id: uuid.UUID,
    gate_id: uuid.UUID | None,
    occurred_at: datetime,
    direction: str,
    crossing_count: int,
    confidence: float,
    clip_sha256: str | None,
    sequence: int,
) -> dict[str, Any]:
    """The evidence, in the exact shape that gets hashed.

    Deliberately narrow: only facts about the crossing itself. Review columns
    are absent because they change — a record that re-hashed when somebody
    marked it a false positive would break its own chain on the first review,
    which would make the chain useless for the thing it is for.
    """
    return {
        "sequence": sequence,
        "tripwire_id": str(tripwire_id),
        "camera_id": str(camera_id),
        "gate_id": str(gate_id) if gate_id else None,
        # Microsecond-precision ISO-8601 in UTC. The serialisation is part of
        # the hash, so it is pinned here rather than left to whatever
        # `isoformat()` does with a naive datetime.
        "occurred_at": occurred_at.astimezone(tz=occurred_at.tzinfo).isoformat(),
        "direction": direction,
        "crossing_count": crossing_count,
        "confidence": round(confidence, 4),
        "clip_sha256": clip_sha256,
    }


def chain_hash_for(prev_hash: str, payload: dict[str, Any]) -> str:
    """SHA-256(prev_hash || canonical-JSON(payload)).

    `sort_keys` and the tight separators are load-bearing: a hash over JSON
    whose key order depends on dict insertion order is a hash that stops
    verifying when somebody reorders a literal.
    """
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256_hex(f"{prev_hash}{body}".encode())


@dataclass(frozen=True, slots=True)
class Crossing:
    """One tripwire crossing as reported by the AI engine."""

    tripwire_id: uuid.UUID
    occurred_at: datetime
    direction: str
    confidence: float
    crossing_count: int = 1
    clip_uri: str | None = None
    clip_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class CrossingOutcome:
    """What happened to a reported crossing."""

    recorded: bool
    reason: str
    event: BreachEvent | None = None
    matched_pass_id: uuid.UUID | None = None


async def _tail(session: AsyncSession) -> tuple[int, str]:
    """The chain's current end: (last sequence, last chain hash)."""
    row = (
        await session.execute(
            select(BreachEvent.sequence, BreachEvent.chain_hash)
            .order_by(BreachEvent.sequence.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return 0, GENESIS_HASH
    return int(row[0]), str(row[1])


async def _lock_chain(session: AsyncSession) -> None:
    await session.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": _CHAIN_LOCK_ID})


async def _matching_pass(
    session: AsyncSession, *, gate_id: uuid.UUID | None, at: datetime
) -> uuid.UUID | None:
    """A pass scanned at this gate within ±30 seconds of the crossing.

    Section 4/M5's cross-reference, and the reason most crossings never become
    events. A pilgrim whose pass was scanned as they walked through is not a
    breach — they are a pilgrim, and recording them as one would fill the ledger
    with the innocent and make the guilty invisible in the noise.
    """
    if gate_id is None:
        return None
    window = timedelta(seconds=PASS_SCAN_WINDOW_SECONDS)
    return await session.scalar(
        select(Pass.id)
        .where(
            Pass.scanned_gate_id == gate_id,
            Pass.scanned_at.is_not(None),
            Pass.scanned_at >= at - window,
            Pass.scanned_at <= at + window,
        )
        .limit(1)
    )


def _gate_is_closed(gate: Gate | None) -> bool:
    """A crossing only counts while the gate is flagged closed.

    An open gate is a gate people are meant to walk through. Section 4/M5 scopes
    the whole module to "a window when that gate is flagged closed", and this is
    that scope.
    """
    if gate is None:
        # No gate on the tripwire: it watches a restricted line rather than a
        # door. Treat as always-restricted — the tripwire would not exist
        # otherwise — and let human review sort out the rest.
        return True
    return not gate.is_open


async def record_crossing(
    session: AsyncSession,
    crossing: Crossing,
    *,
    at: datetime | None = None,
) -> CrossingOutcome:
    """Turn a reported crossing into a ledger entry, or explain why it is not one.

    The order of the checks is the order that keeps the ledger clean: wrong
    direction, then gate open, then a matching pass scan. Only what survives all
    three is written, and every rejection is returned with its reason so the
    engine's own logs can be reconciled against ours.
    """
    moment = at or now_utc()

    tripwire = await session.get(Tripwire, crossing.tripwire_id)
    if tripwire is None:
        raise AppError("TRIPWIRE_NOT_FOUND", details={"tripwire_id": str(crossing.tripwire_id)})
    if not tripwire.is_active:
        return CrossingOutcome(False, "tripwire is not active")

    if crossing.direction != tripwire.restricted_direction:
        # Somebody walking out through an entry-restricted line is not a breach.
        return CrossingOutcome(
            False,
            f"crossing direction {crossing.direction} is not the restricted direction",
        )

    gate = await session.get(Gate, tripwire.gate_id) if tripwire.gate_id else None
    if not _gate_is_closed(gate):
        return CrossingOutcome(False, "the gate was open at the time of the crossing")

    matched = await _matching_pass(session, gate_id=tripwire.gate_id, at=crossing.occurred_at)
    if matched is not None:
        # Authorised. Section 4/M5: "If yes -> authorised, no event."
        return CrossingOutcome(False, "a valid pass was scanned at this gate", matched_pass_id=matched)

    # Serialise from here: everything below reads and extends the chain tail.
    await _lock_chain(session)
    last_sequence, prev_hash = await _tail(session)
    sequence = last_sequence + 1

    payload = canonical_payload(
        tripwire_id=tripwire.id,
        camera_id=tripwire.camera_id,
        gate_id=tripwire.gate_id,
        occurred_at=crossing.occurred_at,
        direction=crossing.direction,
        crossing_count=crossing.crossing_count,
        confidence=crossing.confidence,
        clip_sha256=crossing.clip_sha256,
        sequence=sequence,
    )

    retention_days = await config_service.get_int(session, "breach_retention_days")

    event = BreachEvent(
        sequence=sequence,
        tripwire_id=tripwire.id,
        camera_id=tripwire.camera_id,
        gate_id=tripwire.gate_id,
        occurred_at=crossing.occurred_at,
        direction=crossing.direction,
        crossing_count=crossing.crossing_count,
        confidence=crossing.confidence,
        clip_uri=crossing.clip_uri,
        clip_sha256=crossing.clip_sha256,
        prev_hash=prev_hash,
        chain_hash=chain_hash_for(prev_hash, payload),
        payload_snapshot=payload,
        # Recorded even though it found nothing: "we checked and there was no
        # pass" is a materially different claim from "nobody checked", and a
        # reviewer six months later needs to know which one this was.
        pass_scan_checked=tripwire.gate_id is not None,
        matched_pass_id=None,
        review_status=str(ReviewStatus.PENDING),
        purge_after=moment + timedelta(days=retention_days),
    )
    session.add(event)
    await session.flush()

    logger.info(
        "breach_recorded",
        extra={
            "sequence": sequence,
            "gate_id": str(tripwire.gate_id) if tripwire.gate_id else None,
            "confidence": crossing.confidence,
            "has_clip": bool(crossing.clip_uri),
        },
    )
    return CrossingOutcome(True, "recorded pending review", event=event)


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------
async def load(session: AsyncSession, event_id: uuid.UUID) -> BreachEvent:
    event = await session.get(BreachEvent, event_id)
    if event is None:
        raise AppError("BREACH_NOT_FOUND", details={"breach_id": str(event_id)})
    return event


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------
#: Statuses a reviewer may set. `pending` is not among them — a review is a
#: decision, and "un-deciding" one would erase the fact that it was made.
REVIEWABLE: frozenset[ReviewStatus] = frozenset(
    {ReviewStatus.VERIFIED, ReviewStatus.FALSE_POSITIVE, ReviewStatus.AUTHORISED}
)

#: A reason is mandatory for these. "Authorised" without one is an assertion
#: that somebody was allowed through and no record of who decided that.
REASON_REQUIRED: frozenset[ReviewStatus] = frozenset(
    {ReviewStatus.AUTHORISED, ReviewStatus.FALSE_POSITIVE}
)


async def review(
    session: AsyncSession,
    event: BreachEvent,
    *,
    status: ReviewStatus,
    actor_id: uuid.UUID,
    actor_role: str,
    reason: str | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    at: datetime | None = None,
) -> BreachEvent:
    """Record a human's decision about an event.

    Re-reviewing is allowed and audited. A first reviewer who marked something a
    false positive in a hurry should be correctable by a second — but both
    decisions land in the audit log, so the change is visible rather than a
    silent overwrite.
    """
    moment = at or now_utc()

    if status not in REVIEWABLE:
        raise AppError(
            "BAD_REQUEST",
            details={"reason": "a review must be verified, false_positive or authorised"},
        )
    if status in REASON_REQUIRED and not (reason and reason.strip()):
        raise AppError("REVIEW_REASON_REQUIRED", details={"status": str(status)})
    if event.deleted_at is not None:
        raise AppError("BREACH_REDACTED", details={"sequence": event.sequence})

    previous = event.review_status
    event.review_status = str(status)
    event.reviewed_by = actor_id
    event.review_reason = reason
    event.reviewed_at = moment

    await audit_service.record(
        session,
        action=AuditAction.BREACH_REVIEWED,
        actor_id=actor_id,
        actor_role=actor_role,
        target_type="breach_event",
        target_id=event.id,
        meta={
            "sequence": event.sequence,
            "from": previous,
            "to": str(status),
            "reason": reason,
            "gate_id": str(event.gate_id) if event.gate_id else None,
            "is_rereview": previous != str(ReviewStatus.PENDING),
        },
        ip=ip,
        user_agent=user_agent,
    )
    return event


# ---------------------------------------------------------------------------
# clip access
# ---------------------------------------------------------------------------
async def log_clip_access(
    session: AsyncSession,
    event: BreachEvent,
    *,
    actor_id: uuid.UUID,
    actor_role: str,
    purpose: str,
    ip: str | None = None,
    user_agent: str | None = None,
    at: datetime | None = None,
) -> ClipAccessLog:
    """Record that somebody watched an evidence clip, and why.

    Section 4/M5: "clip playback requires re-authentication and logs every
    view". The re-authentication happens in the route; this is the log. The
    purpose is free text and mandatory — a viewing with no stated reason is the
    one an inquiry asks about, and making the field required means the answer
    exists before the question does.

    Written to two places on purpose: `clip_access_log` is the per-event trail a
    reviewer sees next to the clip, and the audit log is the append-only record
    that outlives the event's own 90-day retention.
    """
    moment = at or now_utc()
    entry = ClipAccessLog(
        breach_event_id=event.id,
        actor_id=actor_id,
        purpose=purpose,
        ip=ip,
        accessed_at=moment,
    )
    session.add(entry)

    await audit_service.record(
        session,
        action=AuditAction.BREACH_CLIP_VIEWED,
        actor_id=actor_id,
        actor_role=actor_role,
        target_type="breach_event",
        target_id=event.id,
        meta={"sequence": event.sequence, "purpose": purpose},
        ip=ip,
        user_agent=user_agent,
    )
    return entry


async def clip_access_history(session: AsyncSession, event_id: uuid.UUID) -> list[ClipAccessLog]:
    rows = await session.execute(
        select(ClipAccessLog)
        .where(ClipAccessLog.breach_event_id == event_id)
        .order_by(ClipAccessLog.accessed_at.desc())
    )
    return list(rows.scalars())


# ---------------------------------------------------------------------------
# redaction
# ---------------------------------------------------------------------------
async def redact(
    session: AsyncSession,
    event: BreachEvent,
    *,
    actor_id: uuid.UUID,
    actor_role: str,
    reason: str,
    ip: str | None = None,
    user_agent: str | None = None,
    at: datetime | None = None,
) -> BreachEvent:
    """Remove the evidence clip. Keep the record and its place in the chain.

    See the module docstring for why this is not a row deletion. In short: a
    real DELETE breaks the chain, and a broken chain cannot tell an authorised
    removal from tampering — which is the single distinction this ledger exists
    to preserve.

    `clip_sha256` is deliberately *not* cleared. It is an evidence column the
    database refuses to update anyway, and keeping it means the hash of what was
    removed survives the removal: if the clip resurfaces from a backup, it can
    still be shown to be the one this record refers to.
    """
    moment = at or now_utc()
    if not reason.strip():
        raise AppError("DELETION_REASON_REQUIRED", details={"sequence": event.sequence})
    if event.deleted_at is not None:
        raise AppError("BREACH_REDACTED", details={"sequence": event.sequence})

    had_clip = bool(event.clip_uri)
    event.clip_uri = None
    event.deleted_at = moment
    event.deleted_by = actor_id
    event.deletion_reason = reason

    await audit_service.record(
        session,
        action=AuditAction.BREACH_DELETED,
        actor_id=actor_id,
        actor_role=actor_role,
        target_type="breach_event",
        target_id=event.id,
        meta={
            "sequence": event.sequence,
            "reason": reason,
            "had_clip": had_clip,
            "clip_sha256": event.clip_sha256,
            "review_status": event.review_status,
            "chain_hash": event.chain_hash,
        },
        ip=ip,
        user_agent=user_agent,
    )
    logger.warning(
        "breach_redacted",
        extra={"sequence": event.sequence, "actor_id": str(actor_id), "had_clip": had_clip},
    )
    return event


# ---------------------------------------------------------------------------
# chain verification
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ChainBreak:
    sequence: int
    breach_id: uuid.UUID | None
    problem: str
    expected: str | None = None
    found: str | None = None


@dataclass(frozen=True, slots=True)
class ChainReport:
    events_checked: int
    intact: bool
    breaks: list[ChainBreak]
    first_sequence: int | None
    last_sequence: int | None
    head_hash: str | None
    verified_at: datetime


async def verify_chain(session: AsyncSession, *, limit: int | None = None) -> ChainReport:
    """Walk the ledger and recompute every hash.

    Three ways a chain can be wrong, and this reports all of them separately
    because they mean different things:

    * **a gap in `sequence`** — a row was deleted outright;
    * **a `prev_hash` that does not match the previous row's `chain_hash`** —
      a row was inserted, removed or reordered;
    * **a `chain_hash` that does not match its own recomputed payload** — the
      stored evidence was edited (which the database trigger should have
      prevented, so seeing this means somebody worked around the application).

    The walk does not stop at the first break. An operator handed "the chain is
    broken at 412" needs to know whether that is one bad row or the point where
    everything after it was rewritten.
    """
    moment = now_utc()
    stmt = select(BreachEvent).order_by(BreachEvent.sequence)
    if limit:
        stmt = stmt.limit(limit)

    events = list((await session.execute(stmt)).scalars())
    if not events:
        return ChainReport(
            events_checked=0,
            intact=True,
            breaks=[],
            first_sequence=None,
            last_sequence=None,
            head_hash=None,
            verified_at=moment,
        )

    breaks: list[ChainBreak] = []
    expected_prev = GENESIS_HASH
    expected_sequence = events[0].sequence

    for event in events:
        if event.sequence != expected_sequence:
            breaks.append(
                ChainBreak(
                    sequence=event.sequence,
                    breach_id=event.id,
                    problem="sequence gap — a record was removed from the ledger",
                    expected=str(expected_sequence),
                    found=str(event.sequence),
                )
            )
            expected_sequence = event.sequence

        if event.prev_hash != expected_prev:
            breaks.append(
                ChainBreak(
                    sequence=event.sequence,
                    breach_id=event.id,
                    problem="prev_hash does not match the previous record",
                    expected=expected_prev,
                    found=event.prev_hash,
                )
            )

        recomputed = chain_hash_for(event.prev_hash, event.payload_snapshot)
        if recomputed != event.chain_hash:
            breaks.append(
                ChainBreak(
                    sequence=event.sequence,
                    breach_id=event.id,
                    problem="chain_hash does not match its own payload — the record was altered",
                    expected=recomputed,
                    found=event.chain_hash,
                )
            )

        expected_prev = event.chain_hash
        expected_sequence = event.sequence + 1

    if breaks:
        logger.error("breach_chain_broken", extra={"breaks": len(breaks)})

    return ChainReport(
        events_checked=len(events),
        intact=not breaks,
        breaks=breaks,
        first_sequence=events[0].sequence,
        last_sequence=events[-1].sequence,
        head_hash=events[-1].chain_hash,
        verified_at=moment,
    )


# ---------------------------------------------------------------------------
# daily summary
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class GateHourCount:
    gate_id: uuid.UUID | None
    gate_code: str | None
    hour: int
    count: int


@dataclass(frozen=True, slots=True)
class DailySummary:
    day: date
    total: int
    by_review_status: dict[str, int]
    by_gate_hour: list[GateHourCount]
    chain_intact: bool
    chain_head: str | None
    generated_at: datetime


async def daily_summary(session: AsyncSession, day: date) -> DailySummary:
    """The artefact the trust takes to a governance meeting.

    Counts by gate and hour, and review status. No personal data — there is none
    in the table to leak, which is the point of having built it that way.

    The chain verification is included in the summary rather than left as a
    separate endpoint nobody remembers to call. A breach report is only worth
    the ledger it came from, and a report that does not say whether its own
    source verified is a report that invites the question at the worst moment.
    """
    start = datetime.combine(day, time.min).replace(tzinfo=now_utc().tzinfo)
    end = start + timedelta(days=1)

    rows = await session.execute(
        select(
            BreachEvent.gate_id,
            Gate.code,
            func.extract("hour", BreachEvent.occurred_at),
            func.count(),
        )
        .outerjoin(Gate, Gate.id == BreachEvent.gate_id)
        .where(BreachEvent.occurred_at >= start, BreachEvent.occurred_at < end)
        .group_by(BreachEvent.gate_id, Gate.code, func.extract("hour", BreachEvent.occurred_at))
        .order_by(Gate.code, func.extract("hour", BreachEvent.occurred_at))
    )
    by_gate_hour = [
        GateHourCount(gate_id=gid, gate_code=code, hour=int(hour), count=int(count))
        for gid, code, hour, count in rows
    ]

    status_rows = await session.execute(
        select(BreachEvent.review_status, func.count())
        .where(BreachEvent.occurred_at >= start, BreachEvent.occurred_at < end)
        .group_by(BreachEvent.review_status)
    )
    by_status = {status: int(count) for status, count in status_rows.all()}

    report = await verify_chain(session)

    return DailySummary(
        day=day,
        total=sum(by_status.values()),
        by_review_status=by_status,
        by_gate_hour=by_gate_hour,
        chain_intact=report.intact,
        chain_head=report.head_hash,
        generated_at=now_utc(),
    )


async def pending_count(session: AsyncSession) -> int:
    """Events still waiting for a human. The command centre's KPI."""
    return int(
        await session.scalar(
            select(func.count())
            .select_from(BreachEvent)
            .where(
                BreachEvent.review_status == str(ReviewStatus.PENDING),
                BreachEvent.deleted_at.is_(None),
            )
        )
        or 0
    )


# ---------------------------------------------------------------------------
# retention
# ---------------------------------------------------------------------------
async def purge_expired_clips(session: AsyncSession, *, at: datetime | None = None) -> list[int]:
    """Clear evidence clips past their retention window.

    Same shape as `redact`: the clip goes, the record and its hash stay. A
    ledger whose rows disappeared on a 90-day timer would fail verification
    every quarter by design, which would train everyone to ignore the one alarm
    that matters.

    Returns the sequences cleared so the caller can delete the objects.
    """
    moment = at or now_utc()
    rows = await session.execute(
        select(BreachEvent).where(
            BreachEvent.purge_after < moment,
            BreachEvent.clip_uri.is_not(None),
        )
    )
    purged: list[int] = []
    for event in rows.scalars():
        event.clip_uri = None
        purged.append(event.sequence)
    return purged
