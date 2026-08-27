"""Accessibility and assistance (Track 1, item 4).

    GET   /accessibility/me            the caller's own profile
    PUT   /accessibility/me            declare it once
    POST  /assistance                  ask for help now
    GET   /assistance                  the volunteer board
    GET   /assistance/mine             the caller's own open asks
    PATCH /assistance/{id}             claim it, or close it
    PATCH /accessibility/facilities/{id}  record a field survey

Two boundaries hold this module together.

**A profile is health data.** It is returned to its owner and to nobody else.
There is no endpoint that lists profiles, and the volunteer board carries the
*needs* attached to a request ("wheelchair") and never the pilgrim's notes,
which are their own words about their own body.

**A claim is not a grant.** Priority booking reads the stored profile
server-side. No route accepts "give me a reserved seat" as a parameter.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.deps import Actor, get_current_actor, require
from app.core.errors import AppError
from app.core.permissions import Permission
from app.core.security import hash_phone, now_utc
from app.models import AssistanceRequest, Facility, Zone
from app.models.accessibility import (
    OPEN_REQUEST_STATUSES,
    AssistanceNeed,
    RequestStatus,
)
from app.schemas.accessibility import (
    AccessibilityProfileIn,
    AccessibilityProfileOut,
    AssistanceRequestCreate,
    AssistanceRequestOut,
    AssistanceUpdate,
    FacilityAccessibilityIn,
)
from app.schemas.common import ErrorResponse, Page
from app.services import accessibility_service, audit_service
from app.services.audit_service import AuditAction

router = APIRouter(tags=["accessibility"], responses={404: {"model": ErrorResponse}})


def _profile_out(profile, *, priority: bool) -> AccessibilityProfileOut:
    if profile is None:
        return AccessibilityProfileOut()
    return AccessibilityProfileOut(
        needs=[AssistanceNeed(n) for n in (profile.needs or [])],
        notes=profile.notes,
        large_text=profile.large_text,
        high_contrast=profile.high_contrast,
        has_companion_contact=bool(profile.companion_phone_hash),
        priority_booking=priority,
        updated_at=profile.updated_at,
    )


def _request_out(record: AssistanceRequest, *, zone_code: str | None = None) -> AssistanceRequestOut:
    end = record.resolved_at or record.assigned_at or now_utc()
    return AssistanceRequestOut(
        id=record.id,
        reference=record.reference,
        needs=[AssistanceNeed(n) for n in (record.needs or [])],
        note=record.note,
        on_behalf_of=record.on_behalf_of,
        zone_id=record.zone_id,
        zone_code=zone_code,
        gate_id=record.gate_id,
        facility_id=record.facility_id,
        status=RequestStatus(record.status),
        requested_at=record.requested_at,
        sla_due_at=record.sla_due_at,
        assigned_at=record.assigned_at,
        resolved_at=record.resolved_at,
        outcome_note=record.outcome_note,
        language=record.language,
        sla_breached=accessibility_service.is_breached(record),
        waiting_seconds=round((end - record.requested_at).total_seconds(), 1),
    )


# ---------------------------------------------------------------------------
# profile
# ---------------------------------------------------------------------------
@router.get("/accessibility/me", response_model=AccessibilityProfileOut)
async def read_my_profile(
    actor: Actor = Depends(get_current_actor),
    session: AsyncSession = Depends(get_session),
) -> AccessibilityProfileOut:
    """The caller's own profile, and nobody else's.

    Returns an empty profile rather than 404 when none has been declared: the
    screen that reads this renders a form either way, and a 404 for "you have
    not filled this in yet" is an error the client would only translate back
    into an empty form.
    """
    profile = await accessibility_service.get_profile(session, actor.id)
    return _profile_out(profile, priority=bool(profile and profile.has_mobility_need()))


@router.put("/accessibility/me", response_model=AccessibilityProfileOut)
async def declare_my_profile(
    payload: AccessibilityProfileIn,
    actor: Actor = Depends(get_current_actor),
    session: AsyncSession = Depends(get_session),
) -> AccessibilityProfileOut:
    """Declare, once.

    Not audited with the contents. That a pilgrim set a profile is worth
    knowing; *what* they declared is health data, and copying it into an
    append-only log that outlives the profile itself would defeat their ability
    to ever withdraw it.
    """
    profile = await accessibility_service.upsert_profile(
        session,
        user_id=actor.id,
        needs=[n.value for n in payload.needs],
        notes=payload.notes,
        large_text=payload.large_text,
        high_contrast=payload.high_contrast,
        companion_phone_hash=hash_phone(payload.companion_phone) if payload.companion_phone else None,
    )
    await audit_service.record(
        session,
        action=AuditAction.ACCESSIBILITY_PROFILE_SET,
        actor_id=actor.id,
        actor_role=actor.user.role,
        target_type="user",
        target_id=actor.id,
        meta={"need_count": len(profile.needs or [])},
        ip=actor.ip,
    )
    out = _profile_out(profile, priority=profile.has_mobility_need())
    await session.commit()
    return out


# ---------------------------------------------------------------------------
# requests
# ---------------------------------------------------------------------------
@router.post("/assistance", response_model=AssistanceRequestOut, status_code=201)
async def ask_for_assistance(
    payload: AssistanceRequestCreate,
    actor: Actor = Depends(require(Permission.ASSISTANCE_REQUEST)),
    session: AsyncSession = Depends(get_session),
) -> AssistanceRequestOut:
    """Ask for help.

    Never refused, and never rate-limited. The same rule the SOS path follows,
    for a version of the same reason: somebody pressing this twice is stuck at a
    step, not abusive, and a system that makes them wait to ask again is a
    system that has decided their time is cheap.
    """
    record = await accessibility_service.raise_request(
        session,
        needs=[n.value for n in payload.needs],
        requested_by=actor.id,
        on_behalf_of=payload.on_behalf_of,
        zone_id=payload.zone_id,
        gate_id=payload.gate_id,
        facility_id=payload.facility_id,
        note=payload.note,
        language=payload.language,
        # The clock starts when they pressed it, not when the phone reconnected.
        requested_at=payload.client_reported_at,
    )
    await audit_service.record(
        session,
        action=AuditAction.ASSISTANCE_REQUESTED,
        actor_id=actor.id,
        actor_role=actor.user.role,
        target_type="assistance_request",
        target_id=record.id,
        meta={"needs": record.needs, "reference": record.reference},
        ip=actor.ip,
    )
    zone = await session.get(Zone, record.zone_id) if record.zone_id else None
    out = _request_out(record, zone_code=zone.code if zone else None)
    await session.commit()
    return out


@router.get("/assistance", response_model=Page[AssistanceRequestOut])
async def assistance_board(
    open_only: bool = Query(default=True),
    zone_id: uuid.UUID | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _: Actor = Depends(require(Permission.ASSISTANCE_VIEW)),
    session: AsyncSession = Depends(get_session),
) -> Page[AssistanceRequestOut]:
    """The volunteer board, ordered by who has waited past their promise first.

    Sorted on `sla_due_at`, so a breached request rises to the top and stays
    there. Unlike the incident board there is no severity to break ties — these
    requests are not graded against each other, because a wheelchair twenty
    minutes late is the same failure whoever asked for it.
    """
    stmt = select(AssistanceRequest)
    if open_only:
        stmt = stmt.where(AssistanceRequest.status.in_(OPEN_REQUEST_STATUSES))
    if zone_id:
        stmt = stmt.where(AssistanceRequest.zone_id == zone_id)

    total = await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = await session.execute(
        stmt.order_by(AssistanceRequest.sla_due_at.asc()).limit(limit).offset(offset)
    )
    records = list(rows.scalars())

    zone_codes: dict[uuid.UUID, str] = {}
    zone_ids = {r.zone_id for r in records if r.zone_id}
    if zone_ids:
        zone_rows = await session.execute(select(Zone.id, Zone.code).where(Zone.id.in_(zone_ids)))
        zone_codes = dict(zone_rows.all())  # type: ignore[arg-type]

    return Page[AssistanceRequestOut](
        items=[
            _request_out(r, zone_code=zone_codes.get(r.zone_id) if r.zone_id else None)
            for r in records
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/assistance/mine", response_model=list[AssistanceRequestOut])
async def my_assistance_requests(
    actor: Actor = Depends(get_current_actor),
    session: AsyncSession = Depends(get_session),
) -> list[AssistanceRequestOut]:
    rows = await session.execute(
        select(AssistanceRequest)
        .where(
            AssistanceRequest.requested_by == actor.id,
            AssistanceRequest.status.in_(OPEN_REQUEST_STATUSES),
        )
        .order_by(AssistanceRequest.requested_at.desc())
        .limit(20)
    )
    return [_request_out(r) for r in rows.scalars()]


@router.patch("/assistance/{request_id}", response_model=AssistanceRequestOut)
async def update_assistance(
    request_id: uuid.UUID,
    payload: AssistanceUpdate,
    actor: Actor = Depends(get_current_actor),
    session: AsyncSession = Depends(get_session),
) -> AssistanceRequestOut:
    """Claim a request, or close it.

    Cancelling is the one thing the pilgrim who raised it may do — their own
    "never mind, my son found me". Everything else needs the volunteer
    permission, because it is an assertion about the physical world.
    """
    record = await accessibility_service.load_request(session, request_id)
    mine = record.requested_by == actor.id
    staff = actor.can(Permission.ASSISTANCE_MANAGE)

    if payload.claim:
        if not staff:
            raise AppError("FORBIDDEN")
        accessibility_service.assign(record, actor.id)

    if payload.status:
        cancelling_own = mine and payload.status == RequestStatus.CANCELLED
        if not staff and not cancelling_own:
            raise AppError("FORBIDDEN")
        accessibility_service.close(
            record, status=payload.status, outcome_note=payload.outcome_note
        )
        await audit_service.record(
            session,
            action=AuditAction.ASSISTANCE_CLOSED,
            actor_id=actor.id,
            actor_role=actor.user.role,
            target_type="assistance_request",
            target_id=record.id,
            meta={
                "status": record.status,
                "reference": record.reference,
                # The number that matters after a Wari is how many asks went
                # unanswered, so it is on the row a report will read.
                "sla_breached": accessibility_service.is_breached(record),
            },
            ip=actor.ip,
        )

    zone = await session.get(Zone, record.zone_id) if record.zone_id else None
    out = _request_out(record, zone_code=zone.code if zone else None)
    await session.commit()
    return out


# ---------------------------------------------------------------------------
# facility survey
# ---------------------------------------------------------------------------
@router.patch("/accessibility/facilities/{facility_id}", response_model=dict)
async def survey_facility(
    facility_id: uuid.UUID,
    payload: FacilityAccessibilityIn,
    actor: Actor = Depends(require(Permission.ASSISTANCE_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Record what a volunteer found when they walked up to it.

    Merged rather than replaced: a survey that checked the ramp and not the
    toilet should not erase last week's answer about the toilet. Unknown keys
    are dropped by `clean_facility_flags` — see the note there on why a typo
    must not become a stored key.
    """
    facility = await session.get(Facility, facility_id)
    if facility is None:
        raise AppError("NOT_FOUND", details={"facility_id": str(facility_id)})

    given = payload.model_dump(exclude_none=True)
    merged = {**(facility.accessibility or {}), **accessibility_service.clean_facility_flags(given)}
    facility.accessibility = merged

    await audit_service.record(
        session,
        action=AuditAction.FACILITY_SURVEYED,
        actor_id=actor.id,
        actor_role=actor.user.role,
        target_type="facility",
        target_id=facility.id,
        meta=merged,
        ip=actor.ip,
    )
    await session.commit()
    return {"facility_id": str(facility.id), "accessibility": merged}
