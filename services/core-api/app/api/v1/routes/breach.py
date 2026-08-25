"""Queue-breach ledger (Section 4/M5, Phase 6).

    GET  /breaches                 GET  /breaches/{id}
    POST /breaches/{id}/review     POST /breaches/{id}/clip
    DELETE /breaches/{id}          GET  /breaches/verify
    GET  /breaches/summary         GET/POST /tripwires

Every route in this module sits behind `breach:view` or narrower. There is no
pilgrim-facing surface here and no anonymous read — Section 4/M5's "no public
exposure" is a property of the router, not a note in a document. `test_permissions`
already encodes the matrix that makes Volunteer and below unable to reach any of
it.

Three routes deserve their own note:

* **`POST /breaches/{id}/clip`** is a POST because it *does* something: it
  re-authenticates the caller and writes an access log entry. A GET that has to
  be audited is a GET that gets prefetched, cached and replayed by a browser.
* **`DELETE /breaches/{id}`** redacts the clip and keeps the record. See
  `breach_service.redact` — a real row deletion breaks the chain, and a broken
  chain cannot distinguish authorised removal from tampering.
* **`GET /breaches/verify`** is deliberately available to any Security Officer,
  not just an administrator. The person being asked to act on a record is
  entitled to check that the ledger it came from holds together.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.deps import Actor, require
from app.core.errors import AppError
from app.core.permissions import Permission
from app.core.security import now_utc, verify_password
from app.models import BreachEvent, Camera, Gate, Tripwire
from app.models.breach import ReviewStatus
from app.schemas.breach import (
    BreachOut,
    ChainBreakOut,
    ChainReportOut,
    ClipOut,
    ClipRequest,
    ClipViewOut,
    DailySummaryOut,
    GateHourOut,
    RedactIn,
    ReviewIn,
    TripwireIn,
    TripwireOut,
)
from app.schemas.common import ErrorResponse, Page
from app.services import audit_service, breach_service
from app.services.audit_service import AuditAction

router = APIRouter(tags=["breach"], responses={404: {"model": ErrorResponse}})


# ---------------------------------------------------------------------------
# shaping
# ---------------------------------------------------------------------------
def _out(
    event: BreachEvent,
    *,
    gate: Gate | None = None,
    tripwire_name: str | None = None,
    clip_views: list[ClipViewOut] | None = None,
) -> BreachOut:
    return BreachOut(
        id=event.id,
        sequence=event.sequence,
        tripwire_id=event.tripwire_id,
        tripwire_name=tripwire_name,
        camera_id=event.camera_id,
        gate_id=event.gate_id,
        gate_code=gate.code if gate else None,
        gate_name_mr=gate.name_mr if gate else None,
        occurred_at=event.occurred_at,
        direction=event.direction,
        crossing_count=event.crossing_count,
        confidence=event.confidence,
        # The URI is withheld here and everywhere except the re-authenticated
        # clip route. Its presence is a fact; its location is a capability.
        has_clip=bool(event.clip_uri),
        clip_sha256=event.clip_sha256,
        pass_scan_checked=event.pass_scan_checked,
        review_status=ReviewStatus(event.review_status),
        reviewed_by=event.reviewed_by,
        review_reason=event.review_reason,
        reviewed_at=event.reviewed_at,
        redacted_at=event.deleted_at,
        redaction_reason=event.deletion_reason,
        chain_hash=event.chain_hash,
        prev_hash=event.prev_hash,
        purge_after=event.purge_after,
        created_at=event.created_at,
        clip_views=clip_views or [],
    )


async def _gates_for(session: AsyncSession, events: list[BreachEvent]) -> dict[uuid.UUID, Gate]:
    ids = {e.gate_id for e in events if e.gate_id}
    if not ids:
        return {}
    rows = await session.execute(select(Gate).where(Gate.id.in_(ids)))
    return {g.id: g for g in rows.scalars()}


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------
@router.get("/breaches", response_model=Page[BreachOut])
async def list_breaches(
    review_status: str | None = Query(default=None, description="pending | verified | false_positive | authorised"),
    gate_id: uuid.UUID | None = None,
    since_hours: int = Query(default=168, ge=1, le=2160),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _: Actor = Depends(require(Permission.BREACH_VIEW)),
    session: AsyncSession = Depends(get_session),
) -> Page[BreachOut]:
    """The review queue.

    Pending first, then oldest first. A reviewer working down this list works
    down the backlog in the order it accumulated — and an event that has been
    waiting three days is a worse fact than one from this morning, because the
    people who could remember what happened are already gone.
    """
    stmt = select(BreachEvent).where(BreachEvent.occurred_at >= now_utc() - timedelta(hours=since_hours))
    if review_status:
        stmt = stmt.where(BreachEvent.review_status == review_status)
    if gate_id:
        stmt = stmt.where(BreachEvent.gate_id == gate_id)

    total = await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = await session.execute(
        stmt.order_by(
            (BreachEvent.review_status == str(ReviewStatus.PENDING)).desc(),
            BreachEvent.occurred_at.asc(),
        )
        .limit(limit)
        .offset(offset)
    )
    events = list(rows.scalars())
    gates = await _gates_for(session, events)

    return Page[BreachOut](
        items=[_out(e, gate=gates.get(e.gate_id) if e.gate_id else None) for e in events],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/breaches/verify", response_model=ChainReportOut)
async def verify_chain(
    actor: Actor = Depends(require(Permission.BREACH_VIEW)),
    session: AsyncSession = Depends(get_session),
) -> ChainReportOut:
    """Recompute every hash in the ledger and report what does not add up.

    Available to any role that can read the ledger, deliberately. Somebody being
    asked to act on a breach record is entitled to check that the chain it came
    from holds — restricting verification to administrators would mean the
    people with the most reason to doubt a record are the ones who cannot test
    it.

    The verification itself is audited: a chain check is a thing an inquiry will
    want dated.
    """
    report = await breach_service.verify_chain(session)

    await audit_service.record(
        session,
        action=AuditAction.AUDIT_VIEWED,
        actor_id=actor.id,
        actor_role=actor.user.role,
        target_type="breach_chain",
        meta={
            "events_checked": report.events_checked,
            "intact": report.intact,
            "breaks": len(report.breaks),
            "head_hash": report.head_hash,
        },
        ip=actor.ip,
        user_agent=actor.user_agent,
    )
    await session.commit()

    return ChainReportOut(
        events_checked=report.events_checked,
        intact=report.intact,
        breaks=[
            ChainBreakOut(
                sequence=b.sequence,
                breach_id=b.breach_id,
                problem=b.problem,
                expected=b.expected,
                found=b.found,
            )
            for b in report.breaks
        ],
        first_sequence=report.first_sequence,
        last_sequence=report.last_sequence,
        head_hash=report.head_hash,
        verified_at=report.verified_at,
        note=(
            "This check compares the ledger against itself. Record the head hash somewhere "
            "outside this database — anyone able to rewrite the ledger can also rewrite a "
            "verification that never leaves it."
        ),
        note_mr=(
            "ही तपासणी नोंदवही स्वतःशीच पडताळते. शेवटचा हॅश या डेटाबेसबाहेर कुठेतरी नोंदवून ठेवा — "
            "जो कोणी नोंदवही बदलू शकतो, तो या तपासणीचा निकालही बदलू शकतो."
        ),
    )


@router.get("/breaches/summary", response_model=DailySummaryOut)
async def daily_summary(
    day: date | None = Query(default=None, description="Defaults to today"),
    actor: Actor = Depends(require(Permission.BREACH_VIEW)),
    session: AsyncSession = Depends(get_session),
) -> DailySummaryOut:
    """Counts by gate and hour, with review status. No personal data.

    This is the report the trust takes to a governance meeting, so it is
    generated by the same code path that serves the console rather than exported
    by hand — a figure that is assembled differently for the meeting than for
    the screen is a figure somebody will eventually have to reconcile.
    """
    target = day or now_utc().date()
    summary = await breach_service.daily_summary(session, target)

    await audit_service.record(
        session,
        action=AuditAction.DATA_EXPORTED,
        actor_id=actor.id,
        actor_role=actor.user.role,
        target_type="breach_summary",
        meta={"day": target.isoformat(), "total": summary.total, "chain_intact": summary.chain_intact},
        ip=actor.ip,
        user_agent=actor.user_agent,
    )
    await session.commit()

    return DailySummaryOut(
        day=summary.day,
        total=summary.total,
        by_review_status=summary.by_review_status,
        by_gate_hour=[
            GateHourOut(gate_id=g.gate_id, gate_code=g.gate_code, hour=g.hour, count=g.count)
            for g in summary.by_gate_hour
        ],
        chain_intact=summary.chain_intact,
        chain_head=summary.chain_head,
        generated_at=summary.generated_at,
        notice=(
            "Counts of unauthorised entries by gate and hour. No individual is identified "
            "in this report or in the records behind it."
        ),
        notice_mr=(
            "द्वारनिहाय आणि तासनिहाय अनधिकृत प्रवेशांची संख्या. या अहवालात किंवा त्यामागील "
            "नोंदींमध्ये कोणत्याही व्यक्तीची ओळख नाही."
        ),
    )


@router.get("/breaches/{breach_id}", response_model=BreachOut)
async def get_breach(
    breach_id: uuid.UUID,
    _: Actor = Depends(require(Permission.BREACH_VIEW)),
    session: AsyncSession = Depends(get_session),
) -> BreachOut:
    """One record, with its clip-access trail.

    The trail is on the record rather than behind a separate endpoint because a
    reviewer deciding whether to watch a clip should see who already has.
    """
    event = await breach_service.load(session, breach_id)
    gate = await session.get(Gate, event.gate_id) if event.gate_id else None
    tripwire = await session.get(Tripwire, event.tripwire_id)
    views = await breach_service.clip_access_history(session, event.id)

    return _out(
        event,
        gate=gate,
        tripwire_name=tripwire.name if tripwire else None,
        clip_views=[
            ClipViewOut(actor_id=v.actor_id, purpose=v.purpose, ip=v.ip, accessed_at=v.accessed_at)
            for v in views
        ],
    )


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------
@router.post("/breaches/{breach_id}/review", response_model=BreachOut)
async def review_breach(
    breach_id: uuid.UUID,
    payload: ReviewIn,
    actor: Actor = Depends(require(Permission.BREACH_REVIEW)),
    session: AsyncSession = Depends(get_session),
) -> BreachOut:
    """A human decides. Until this runs, the record is not a finding.

    Section 4/M5: "Every breach event requires human review before it counts.
    AI output alone is never a finding." The `pending` state is what that
    sentence looks like in a database.
    """
    event = await breach_service.load(session, breach_id)
    await breach_service.review(
        session,
        event,
        status=ReviewStatus(payload.status),
        actor_id=actor.id,
        actor_role=actor.user.role,
        reason=payload.reason,
        ip=actor.ip,
        user_agent=actor.user_agent,
    )

    gate = await session.get(Gate, event.gate_id) if event.gate_id else None
    out = _out(event, gate=gate)
    await session.commit()
    return out


# ---------------------------------------------------------------------------
# clip access
# ---------------------------------------------------------------------------
_CLIP_NOTICE = (
    "This view has been logged against your account with the purpose you gave. "
    "The clip shows an unauthorised entry; it does not identify anyone, and no "
    "identification may be inferred from it."
)
_CLIP_NOTICE_MR = (
    "तुम्ही दिलेल्या कारणासह ही पाहणी तुमच्या खात्यावर नोंदवली गेली आहे. ही चित्रफीत "
    "अनधिकृत प्रवेश दाखवते; ती कोणाचीही ओळख पटवत नाही आणि तिच्यावरून ओळख काढता येणार नाही."
)


@router.post("/breaches/{breach_id}/clip", response_model=ClipOut)
async def get_clip(
    breach_id: uuid.UUID,
    payload: ClipRequest,
    actor: Actor = Depends(require(Permission.BREACH_CLIP_VIEW)),
    session: AsyncSession = Depends(get_session),
) -> ClipOut:
    """Play back an evidence clip. Re-authenticate, state a purpose, be logged.

    A POST rather than a GET, because this call has effects: it verifies a
    password and appends to an access log. A GET that must be audited is a GET a
    browser will prefetch, cache and replay — and an access log with three
    entries nobody made is worse than no access log.

    The password check is against the *current* user's own credential. It is not
    a second factor; it is a check that the person at the keyboard is still the
    person who signed in, which is the realistic threat on a shared control-room
    workstation with a 15-minute token.
    """
    event = await breach_service.load(session, breach_id)

    if not verify_password(payload.password, actor.user.password_hash):
        # Audited on failure too. Someone probing for a colleague's password
        # against an evidence endpoint is exactly what this log is for.
        await audit_service.record(
            session,
            action=AuditAction.BREACH_CLIP_VIEWED,
            actor_id=actor.id,
            actor_role=actor.user.role,
            target_type="breach_event",
            target_id=event.id,
            meta={"sequence": event.sequence, "outcome": "reauth_failed"},
            ip=actor.ip,
            user_agent=actor.user_agent,
        )
        await session.commit()
        raise AppError("REAUTH_REQUIRED")

    if not event.clip_uri:
        raise AppError(
            "CLIP_UNAVAILABLE",
            details={
                "sequence": event.sequence,
                "redacted_at": event.deleted_at.isoformat() if event.deleted_at else None,
                "purge_after": event.purge_after.isoformat(),
            },
        )

    await breach_service.log_clip_access(
        session,
        event,
        actor_id=actor.id,
        actor_role=actor.user.role,
        purpose=payload.purpose,
        ip=actor.ip,
        user_agent=actor.user_agent,
    )
    moment = now_utc()
    uri = event.clip_uri
    await session.commit()

    return ClipOut(
        breach_id=event.id,
        sequence=event.sequence,
        clip_uri=uri,
        clip_sha256=event.clip_sha256,
        notice=_CLIP_NOTICE,
        notice_mr=_CLIP_NOTICE_MR,
        logged_at=moment,
    )


# ---------------------------------------------------------------------------
# redaction
# ---------------------------------------------------------------------------
@router.delete("/breaches/{breach_id}", response_model=BreachOut)
async def redact_breach(
    breach_id: uuid.UUID,
    payload: RedactIn,
    actor: Actor = Depends(require(Permission.BREACH_DELETE)),
    session: AsyncSession = Depends(get_session),
) -> BreachOut:
    """Remove the evidence clip. System Admin only. Reason mandatory. Logged forever.

    `breach:delete` is the one permission Administrator is deliberately denied —
    see `permissions.py`, where that rule is encoded and tested. The temple
    administrator who might come under pressure to make a record go away is not
    the person who can.

    What this does *not* do is delete the row. The record, its sequence and its
    hash survive, annotated with who removed the evidence and why. A real
    deletion would break the chain, and a broken chain cannot tell an authorised
    removal from tampering — which is the distinction the ledger exists for.
    """
    event = await breach_service.load(session, breach_id)
    await breach_service.redact(
        session,
        event,
        actor_id=actor.id,
        actor_role=actor.user.role,
        reason=payload.reason,
        ip=actor.ip,
        user_agent=actor.user_agent,
    )

    gate = await session.get(Gate, event.gate_id) if event.gate_id else None
    out = _out(event, gate=gate)
    await session.commit()
    return out


# ---------------------------------------------------------------------------
# tripwires
# ---------------------------------------------------------------------------
@router.get("/tripwires", response_model=list[TripwireOut])
async def list_tripwires(
    camera_id: uuid.UUID | None = None,
    _: Actor = Depends(require(Permission.BREACH_VIEW)),
    session: AsyncSession = Depends(get_session),
) -> list[TripwireOut]:
    """Every configured line, with how many events each has produced.

    The counts are the point. A tripwire generating hundreds of pending events
    is drawn wrong — across a thoroughfare rather than a restricted door — and
    without this column that shows up as a reviewer's backlog rather than as a
    configuration error.
    """
    stmt = select(Tripwire).order_by(Tripwire.name)
    if camera_id:
        stmt = stmt.where(Tripwire.camera_id == camera_id)
    tripwires = list((await session.execute(stmt)).scalars())
    if not tripwires:
        return []

    ids = [t.id for t in tripwires]
    counts = await session.execute(
        select(
            BreachEvent.tripwire_id,
            func.count(),
            func.count().filter(BreachEvent.review_status == str(ReviewStatus.PENDING)),
        )
        .where(BreachEvent.tripwire_id.in_(ids))
        .group_by(BreachEvent.tripwire_id)
    )
    tally = {tid: (int(total), int(pending)) for tid, total, pending in counts}

    camera_ids = {t.camera_id for t in tripwires}
    cameras = await session.execute(select(Camera.id, Camera.name).where(Camera.id.in_(camera_ids)))
    camera_names: dict[uuid.UUID, str] = dict(cameras.all())  # type: ignore[arg-type]

    gate_ids = {t.gate_id for t in tripwires if t.gate_id}
    gate_codes: dict[uuid.UUID, str] = {}
    if gate_ids:
        gates = await session.execute(select(Gate.id, Gate.code).where(Gate.id.in_(gate_ids)))
        gate_codes = dict(gates.all())  # type: ignore[arg-type]

    return [
        TripwireOut(
            id=t.id,
            camera_id=t.camera_id,
            camera_name=camera_names.get(t.camera_id),
            gate_id=t.gate_id,
            gate_code=gate_codes.get(t.gate_id) if t.gate_id else None,
            name=t.name,
            points=[(p[0], p[1]) for p in t.geometry.get("points", [])],
            restricted_direction=t.restricted_direction,
            active_schedule=t.active_schedule,
            is_active=t.is_active,
            event_count=tally.get(t.id, (0, 0))[0],
            pending_count=tally.get(t.id, (0, 0))[1],
        )
        for t in tripwires
    ]


@router.post("/tripwires", response_model=TripwireOut, status_code=201)
async def create_tripwire(
    payload: TripwireIn,
    actor: Actor = Depends(require(Permission.CAMERA_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> TripwireOut:
    """Draw a line on a camera frame.

    Behind `camera:manage` rather than `breach:review`: configuring what counts
    as a breach and deciding whether a particular one is real are different
    powers, and the person who reviews should not also be the person who can
    quietly move the line afterwards.

    Coordinates are normalised (0..1) so the line survives a change of stream
    resolution. A tripwire drawn in pixels against a 1080p feed silently points
    somewhere else the day the camera is replaced with a 4K one.
    """
    camera = await session.get(Camera, payload.camera_id)
    if camera is None:
        raise AppError("CAMERA_NOT_FOUND", details={"camera_id": str(payload.camera_id)})

    for x, y in payload.points:
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            raise AppError(
                "BAD_REQUEST",
                details={"reason": "tripwire points are normalised image coordinates in 0..1", "point": [x, y]},
            )

    if payload.gate_id is not None and await session.get(Gate, payload.gate_id) is None:
        raise AppError("GATE_NOT_FOUND", details={"gate_id": str(payload.gate_id)})

    tripwire = Tripwire(
        camera_id=payload.camera_id,
        gate_id=payload.gate_id,
        name=payload.name,
        geometry={"points": [list(p) for p in payload.points]},
        restricted_direction=payload.restricted_direction,
        active_schedule=payload.active_schedule,
        is_active=payload.is_active,
    )
    session.add(tripwire)
    await session.flush()

    # Enabling tripwire detection on a camera is a change in what the system
    # watches for, so it is audited alongside the line itself.
    camera.is_tripwire_enabled = True

    await audit_service.record(
        session,
        action=AuditAction.CAMERA_UPDATED,
        actor_id=actor.id,
        actor_role=actor.user.role,
        target_type="tripwire",
        target_id=tripwire.id,
        meta={
            "name": tripwire.name,
            "camera": camera.name,
            "gate_id": str(payload.gate_id) if payload.gate_id else None,
            "restricted_direction": payload.restricted_direction,
            "points": [list(p) for p in payload.points],
        },
        ip=actor.ip,
        user_agent=actor.user_agent,
    )
    await session.commit()

    return TripwireOut(
        id=tripwire.id,
        camera_id=tripwire.camera_id,
        camera_name=camera.name,
        gate_id=tripwire.gate_id,
        gate_code=None,
        name=tripwire.name,
        points=[(p[0], p[1]) for p in payload.points],
        restricted_direction=tripwire.restricted_direction,
        active_schedule=tripwire.active_schedule,
        is_active=tripwire.is_active,
    )
