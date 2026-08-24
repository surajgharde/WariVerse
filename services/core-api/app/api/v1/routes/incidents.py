"""Incidents, SOS, dispatch and missing persons (Section 4/M4, Phase 5).

    POST /sos                        GET  /sos/{reference}
    POST /incidents                  GET  /incidents
    GET  /incidents/{id}             PATCH /incidents/{id}
    GET  /incidents/{id}/dispatch-options
    POST /incidents/{id}/dispatch
    GET  /responders                 POST /responders/{id}/ping
    POST /missing-persons            GET  /missing-persons
    GET  /missing-persons/{id}       PATCH /missing-persons/{id}

Three rules live in this file rather than in the service, because all three are
about *who is asking* and the service does not know that.

**A client never names its own provenance.** `_resolve_source` derives `source`
from the caller's role and ignores what the body asked for. Post-Wari review
turns on the difference between a pilgrim pressing a button and an operator
logging a phone call, and a field the client controls is not evidence of
anything.

**A volunteer may work the small stuff and nothing else.** Section 2 gives
`incident:update_low` to volunteers and `incident:update_any` to officers.
`_guard_update` is where that becomes real — including the part that matters
most, which is that a volunteer cannot re-grade a critical incident down and
then close it through the low-severity door.

**A pilgrim reads their own emergency and no one else's.** `GET /sos/{ref}`
matches on the caller's own phone hash. There is no route on which a pilgrim
can enumerate incidents, because "who needed help at the Wari and where" is a
list this system should not be able to hand out.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import events
from app.core.db import get_session
from app.core.deps import Actor, require
from app.core.errors import AppError
from app.core.permissions import Permission
from app.core.security import now_utc
from app.models import Incident, MissingPerson, Responder, Zone
from app.models.incidents import IncidentSeverity, IncidentStatus
from app.schemas.common import ErrorResponse, Page
from app.schemas.incidents import (
    DispatchOptions,
    DispatchRequest,
    IncidentCreate,
    IncidentEventOut,
    IncidentOut,
    IncidentSource,
    IncidentUpdate,
    MissingPersonCreate,
    MissingPersonOut,
    MissingPersonUpdate,
    ResponderOut,
    ResponderPing,
    SosAck,
    SosCreate,
    SuggestionOut,
)
from app.services import dispatch_service, incident_service

router = APIRouter(tags=["incidents"], responses={404: {"model": ErrorResponse}})

#: Severities a holder of `incident:update_low` may act on.
#:
#: `normal` is included and `high` is not. The line is drawn at the ten-minute
#: SLA: a volunteer closing a facility failure or a lost item is the system
#: working, and a volunteer closing a medical emergency is the system failing in
#: the specific way that gets somebody hurt. Widening this tuple is a decision
#: someone should have to make on purpose.
LOW_SEVERITIES: frozenset[str] = frozenset(
    {str(IncidentSeverity.LOW), str(IncidentSeverity.NORMAL)}
)

#: Which provenance each role is allowed to claim, first entry being the
#: default. Absent from every list: `ai_alert`, which only the AI ingest path
#: may produce, and `pilgrim_sos`, which only `POST /sos` may produce.
_ALLOWED_SOURCES: dict[Permission, tuple[IncidentSource, ...]] = {
    # Control room and above: their own entry, or a call they took on the radio
    # or the phone on somebody else's behalf.
    Permission.INCIDENT_UPDATE_ANY: ("control_room", "phone_call", "volunteer_report"),
    Permission.INCIDENT_VIEW: ("volunteer_report",),
}


def _resolve_source(actor: Actor, requested: IncidentSource | None) -> IncidentSource:
    """Decide the provenance from the caller, not from the body."""
    for permission, allowed in _ALLOWED_SOURCES.items():
        if actor.can(permission):
            if requested in allowed:
                return requested
            return allowed[0]
    # A pilgrim filing a report by hand. Deliberately not `pilgrim_sos` — see
    # the note on `IncidentSource`.
    return "pilgrim_report"


def _guard_update(actor: Actor, incident: Incident, payload: IncidentUpdate) -> None:
    """Enforce the low/any split on one incident.

    Re-grading is an `update_any` act regardless of the severity it starts from.
    Without that, a volunteer could downgrade a critical to low and then close
    it through the door this function is supposed to be guarding.
    """
    if actor.can(Permission.INCIDENT_UPDATE_ANY):
        return

    if payload.severity is not None:
        raise AppError(
            "FORBIDDEN",
            details={
                "reason": "re-grading severity needs incident:update_any",
                "missing_permissions": [str(Permission.INCIDENT_UPDATE_ANY)],
            },
        )

    if incident.severity not in LOW_SEVERITIES:
        raise AppError(
            "FORBIDDEN",
            details={
                "reason": f"this incident is graded {incident.severity}",
                "severity": incident.severity,
                "you_may_update": sorted(LOW_SEVERITIES),
                "missing_permissions": [str(Permission.INCIDENT_UPDATE_ANY)],
            },
        )


# ---------------------------------------------------------------------------
# response building
# ---------------------------------------------------------------------------
async def _locations(session: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, tuple[float, float]]:
    """Read every point in one query rather than one query per incident."""
    if not ids:
        return {}
    rows = await session.execute(
        select(Incident.id, func.ST_X(Incident.location), func.ST_Y(Incident.location)).where(
            Incident.id.in_(ids), Incident.location.is_not(None)
        )
    )
    return {row[0]: (float(row[1]), float(row[2])) for row in rows if row[1] is not None}


def _one(
    incident: Incident,
    *,
    zone: Zone | None,
    responder: Responder | None,
    location: tuple[float, float] | None,
    timeline: list[IncidentEventOut] | None = None,
    at: datetime,
) -> IncidentOut:
    end = incident.closed_at or incident.resolved_at or at
    started_at = incident.client_reported_at or incident.created_at
    return IncidentOut(
        id=incident.id,
        reference=incident.reference,
        type=incident.type,
        severity=incident.severity,
        status=incident.status,
        source=incident.source,
        zone_id=incident.zone_id,
        zone_code=zone.code if zone else None,
        zone_name_mr=zone.name_mr if zone else None,
        location=location,
        description=incident.description,
        has_audio_note=incident.audio_note_uri is not None,
        sla_due_at=incident.sla_due_at,
        sla_breached=incident.sla_breached,
        # Negative once the clock has run out, so a console can render "2m left"
        # and "4m over" from the same field without a second flag.
        seconds_to_sla=round((incident.sla_due_at - at).total_seconds(), 1),
        first_response_at=incident.first_response_at,
        assigned_responder_id=incident.assigned_responder_id,
        assigned_call_sign=responder.call_sign if responder else None,
        client_reported_at=incident.client_reported_at,
        delayed_by_seconds=(
            round((incident.created_at - incident.client_reported_at).total_seconds(), 1)
            if incident.client_reported_at
            else None
        ),
        alert_id=incident.alert_id,
        resolved_at=incident.resolved_at,
        closed_at=incident.closed_at,
        outcome_note=incident.outcome_note,
        created_at=incident.created_at,
        # Measured from the report, not from its arrival: an SOS queued offline
        # for twenty minutes has been open for twenty minutes.
        seconds_open=round((end - started_at).total_seconds(), 1),
        timeline=timeline or [],
    )


async def _decorate(
    session: AsyncSession,
    incidents: list[Incident],
    *,
    at: datetime | None = None,
) -> list[IncidentOut]:
    """Batch the zone, responder and geometry reads for a list view."""
    moment = at or now_utc()
    if not incidents:
        return []

    zone_ids = {i.zone_id for i in incidents if i.zone_id}
    zones: dict[uuid.UUID, Zone] = {}
    if zone_ids:
        zones = {z.id: z for z in (await session.execute(select(Zone).where(Zone.id.in_(zone_ids)))).scalars()}

    responder_ids = {i.assigned_responder_id for i in incidents if i.assigned_responder_id}
    responders: dict[uuid.UUID, Responder] = {}
    if responder_ids:
        responders = {
            r.id: r
            for r in (await session.execute(select(Responder).where(Responder.id.in_(responder_ids)))).scalars()
        }

    points = await _locations(session, {i.id for i in incidents})

    return [
        _one(
            incident,
            zone=zones.get(incident.zone_id) if incident.zone_id else None,
            responder=responders.get(incident.assigned_responder_id)
            if incident.assigned_responder_id
            else None,
            location=points.get(incident.id),
            at=moment,
        )
        for incident in incidents
    ]


async def _single(session: AsyncSession, incident: Incident, *, at: datetime | None = None) -> IncidentOut:
    moment = at or now_utc()
    rows = await incident_service.timeline(session, incident.id)
    out = (await _decorate(session, [incident], at=moment))[0]
    return out.model_copy(
        update={"timeline": [IncidentEventOut.model_validate(row) for row in rows]}
    )


# ---------------------------------------------------------------------------
# SOS
# ---------------------------------------------------------------------------
_SOS_ASSIGNED = (
    "Help has been told where you are. {call_sign} is on the way. "
    "Stay where you are if it is safe to do so."
)
_SOS_ASSIGNED_MR = (
    "तुम्ही कुठे आहात हे मदत पथकाला कळवले आहे. {call_sign} येत आहे. "
    "सुरक्षित असल्यास तिथेच थांबा."
)
_SOS_RECEIVED = (
    "Help has been told. The control room has your location and is choosing a "
    "unit now. No unit has been assigned yet."
)
_SOS_RECEIVED_MR = (
    "मदत पथकाला कळवले आहे. नियंत्रण कक्षाकडे तुमचे ठिकाण आले आहे आणि ते पथक "
    "निवडत आहेत. अद्याप कोणतेही पथक नेमलेले नाही."
)
_SOS_REPEAT = "We already have your call ({reference}). It has not been forgotten."
_SOS_REPEAT_MR = "तुमची नोंद ({reference}) आमच्याकडे आधीच आली आहे. ती विसरलेली नाही."


@router.post("/sos", response_model=SosAck, status_code=201)
async def raise_sos(
    payload: SosCreate,
    actor: Actor = Depends(require(Permission.SOS_RAISE)),
    session: AsyncSession = Depends(get_session),
) -> SosAck:
    """The panic button. This route does not have a failure mode the caller sees.

    Everything that could reasonably be rejected is instead absorbed: an unknown
    `zone_id` from a client working off a stale offline bundle is dropped and
    noted rather than raising `ZONE_NOT_FOUND`, and a fourth press inside the
    rate-limit window attaches to the caller's own open incident instead of
    returning 429. Section 9 sets the limit and then says never hard-block an
    SOS; those only look contradictory until you notice the limit exists to stop
    the control room drowning in duplicates, not to stop a frightened person
    getting help.
    """
    moment = now_utc()

    # A zone the client believes in but the server does not: keep the SOS, drop
    # the claim. The GPS point, if there is one, is the better fact anyway.
    zone_id = payload.zone_id
    dropped_zone: str | None = None
    if zone_id is not None and await session.get(Zone, zone_id) is None:
        dropped_zone, zone_id = str(zone_id), None

    result = await incident_service.raise_sos(
        session,
        phone_hash=actor.user.phone_hash,
        reported_by=actor.id,
        incident_type=payload.type,
        zone_id=zone_id,
        location=payload.location,
        description=payload.description,
        audio_note_uri=payload.audio_note_uri,
        client_reported_at=payload.client_reported_at,
        actor_role=actor.user.role,
        ip=actor.ip,
        user_agent=actor.user_agent,
        at=moment,
    )
    incident = result.incident

    if dropped_zone:
        await incident_service.add_event(
            session,
            incident,
            action="zone_unknown",
            actor_id=actor.id,
            note="The reporting device named a zone this server does not have.",
            meta={"claimed_zone_id": dropped_zone},
            at=moment,
        )

    responder = (
        await session.get(Responder, incident.assigned_responder_id)
        if incident.assigned_responder_id
        else None
    )
    eta: float | None = None
    if responder is not None:
        point = await incident_service.incident_location(session, incident)
        unit = await _responder_point(session, responder.id)
        if point is not None and unit is not None:
            eta = round(dispatch_service.walk_eta(dispatch_service.haversine_m(unit, point)).total_seconds(), 1)

    await session.commit()

    await incident_service.publish(
        events.INCIDENT_UPDATED if result.joined_existing else events.INCIDENT_RAISED,
        incident,
        session=session,
        extra={"press_count": result.press_count, "joined_existing": result.joined_existing},
    )

    if responder is not None:
        message = _SOS_ASSIGNED.format(call_sign=responder.call_sign)
        message_mr = _SOS_ASSIGNED_MR.format(call_sign=responder.call_sign)
    elif result.joined_existing:
        message = _SOS_REPEAT.format(reference=incident.reference)
        message_mr = _SOS_REPEAT_MR.format(reference=incident.reference)
    else:
        message, message_mr = _SOS_RECEIVED, _SOS_RECEIVED_MR

    return SosAck(
        incident_id=incident.id,
        reference=incident.reference,
        status=IncidentStatus(incident.status),
        message=message,
        message_mr=message_mr,
        responder_eta_seconds=eta,
        responder_call_sign=responder.call_sign if responder else None,
        joined_existing=result.joined_existing,
        received_at=moment,
    )


@router.get("/sos/{reference}", response_model=SosAck)
async def read_own_sos(
    reference: str,
    actor: Actor = Depends(require(Permission.SOS_RAISE)),
    session: AsyncSession = Depends(get_session),
) -> SosAck:
    """What happened to my SOS.

    The pilgrim app polls this after the confirmation screen. It matches on the
    caller's own phone hash, so a valid reference belonging to somebody else
    answers exactly as a reference that does not exist does — the 404 must not
    become a way to confirm that a given code is real.
    """
    incident = await incident_service.load_by_reference(session, reference)
    if incident.reporter_phone_hash != actor.user.phone_hash:
        raise AppError("INCIDENT_NOT_FOUND", details={"reference": reference})

    responder = (
        await session.get(Responder, incident.assigned_responder_id)
        if incident.assigned_responder_id
        else None
    )
    eta: float | None = None
    if responder is not None:
        point = await incident_service.incident_location(session, incident)
        unit = await _responder_point(session, responder.id)
        if point is not None and unit is not None:
            eta = round(dispatch_service.walk_eta(dispatch_service.haversine_m(unit, point)).total_seconds(), 1)
        message = _SOS_ASSIGNED.format(call_sign=responder.call_sign)
        message_mr = _SOS_ASSIGNED_MR.format(call_sign=responder.call_sign)
    else:
        message, message_mr = _SOS_RECEIVED, _SOS_RECEIVED_MR

    return SosAck(
        incident_id=incident.id,
        reference=incident.reference,
        status=IncidentStatus(incident.status),
        message=message,
        message_mr=message_mr,
        responder_eta_seconds=eta,
        responder_call_sign=responder.call_sign if responder else None,
        received_at=incident.created_at,
    )


# ---------------------------------------------------------------------------
# incidents
# ---------------------------------------------------------------------------
@router.post("/incidents", response_model=IncidentOut, status_code=201)
async def create_incident(
    payload: IncidentCreate,
    actor: Actor = Depends(require(Permission.INCIDENT_REPORT)),
    session: AsyncSession = Depends(get_session),
) -> IncidentOut:
    """Control-room entry, volunteer report, or a phone call somebody took."""
    incident = await incident_service.create(
        session,
        incident_type=payload.type,
        severity=payload.severity,
        source=_resolve_source(actor, payload.source),
        zone_id=payload.zone_id,
        location=payload.location,
        description=payload.description,
        reported_by=actor.id,
        reporter_phone_hash=(
            await incident_service.store_contact(session, payload.contact_phone, at=now_utc())
            if payload.contact_phone
            else None
        ),
        actor_role=actor.user.role,
        ip=actor.ip,
        user_agent=actor.user_agent,
    )
    out = await _single(session, incident)
    await session.commit()
    await incident_service.publish(events.INCIDENT_RAISED, incident, session=session)
    return out


@router.get("/incidents", response_model=Page[IncidentOut])
async def list_incidents(
    status: str | None = Query(
        default=None, description="reported | triaged | dispatched | on_scene | resolved | closed"
    ),
    severity: str | None = None,
    incident_type: str | None = Query(default=None, alias="type"),
    zone_id: uuid.UUID | None = None,
    open_only: bool = Query(default=True, description="Only incidents still needing somebody"),
    sla_breached: bool | None = Query(default=None),
    since_hours: int = Query(default=24, ge=1, le=720),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _: Actor = Depends(require(Permission.INCIDENT_VIEW)),
    session: AsyncSession = Depends(get_session),
) -> Page[IncidentOut]:
    """The board. Worst first, then whichever has least SLA left.

    Ordering by remaining SLA rather than by age within a severity is the whole
    point of the sort: two critical incidents four minutes apart are not equally
    urgent if one of them was re-graded up from normal and is already overdue.
    """
    moment = now_utc()
    stmt = select(Incident).where(Incident.created_at >= moment - timedelta(hours=since_hours))
    if status:
        stmt = stmt.where(Incident.status == status)
    elif open_only:
        stmt = stmt.where(Incident.status.in_([str(s) for s in incident_service.OPEN_STATUSES]))
    if severity:
        stmt = stmt.where(Incident.severity == severity)
    if incident_type:
        stmt = stmt.where(Incident.type == incident_type)
    if zone_id:
        stmt = stmt.where(Incident.zone_id == zone_id)
    if sla_breached is not None:
        stmt = stmt.where(Incident.sla_breached.is_(sla_breached))

    rank = case({"critical": 0, "high": 1, "normal": 2, "low": 3}, value=Incident.severity, else_=4)
    total = await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = await session.execute(
        stmt.order_by(rank, Incident.sla_due_at.asc()).limit(limit).offset(offset)
    )

    return Page[IncidentOut](
        items=await _decorate(session, list(rows.scalars()), at=moment),
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/incidents/{incident_id}", response_model=IncidentOut)
async def get_incident(
    incident_id: uuid.UUID,
    _: Actor = Depends(require(Permission.INCIDENT_VIEW)),
    session: AsyncSession = Depends(get_session),
) -> IncidentOut:
    """One incident, with the timeline that is the actual record of it."""
    return await _single(session, await incident_service.load(session, incident_id))


@router.patch("/incidents/{incident_id}", response_model=IncidentOut)
async def update_incident(
    incident_id: uuid.UUID,
    payload: IncidentUpdate,
    actor: Actor = Depends(require(Permission.INCIDENT_UPDATE_LOW)),
    session: AsyncSession = Depends(get_session),
) -> IncidentOut:
    """Move an incident along, re-grade it, or both.

    The route asks for the *lower* of the two permissions and then narrows in
    `_guard_update`, because what a caller may do here depends on the incident
    in front of them and not only on their role — and a dependency cannot see
    the row.

    Re-grading happens before the status change when both are sent. The order
    matters: `regrade` recomputes the SLA from the original report time, so
    doing it second would stamp a fresh clock onto an incident that had just
    been resolved.
    """
    incident = await incident_service.load(session, incident_id)
    _guard_update(actor, incident, payload)

    if payload.severity is not None:
        await incident_service.regrade(
            session,
            incident,
            severity=payload.severity,
            actor_id=actor.id,
            actor_role=actor.user.role,
            note=payload.note,
            ip=actor.ip,
            user_agent=actor.user_agent,
        )

    if payload.status is not None:
        await incident_service.transition(
            session,
            incident,
            to=payload.status,
            actor_id=actor.id,
            actor_role=actor.user.role,
            note=payload.note,
            outcome_note=payload.outcome_note,
            ip=actor.ip,
            user_agent=actor.user_agent,
        )
    elif payload.outcome_note:
        # An outcome note on its own is a correction to the record, not a
        # transition. Allowed, and written to the timeline like everything else.
        incident.outcome_note = payload.outcome_note
        await incident_service.add_event(
            session,
            incident,
            action="outcome_noted",
            actor_id=actor.id,
            note=payload.outcome_note,
        )

    out = await _single(session, incident)
    await session.commit()
    await incident_service.publish(events.INCIDENT_UPDATED, incident, session=session)
    return out


@router.post("/admin/incidents/sla-sweep", response_model=dict)
async def sweep_sla_now(
    actor: Actor = Depends(require(Permission.INCIDENT_DISPATCH)),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Run the SLA sweep now instead of waiting for the timer.

    Mirrors `/admin/reslot/run`: the scheduler owns the job, and this exists so
    the behaviour can be demonstrated and tested without a fifteen-second wait.
    It marks nothing the timer would not have marked a moment later.
    """
    moment = now_utc()
    breaches = await incident_service.sweep_sla(session, at=moment)
    await session.commit()

    for breach in breaches:
        await incident_service.publish(
            events.INCIDENT_SLA_BREACHED,
            breach.incident,
            session=session,
            extra={"overdue_seconds": round(breach.overdue_seconds, 1)},
        )

    return {
        "breached": len(breaches),
        "references": [b.incident.reference for b in breaches],
        "ran_at": moment.isoformat(),
        "ran_by": str(actor.id),
    }


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------
@router.get("/incidents/{incident_id}/dispatch-options", response_model=DispatchOptions)
async def dispatch_options(
    incident_id: uuid.UUID,
    limit: int = Query(default=5, ge=1, le=20),
    _: Actor = Depends(require(Permission.INCIDENT_DISPATCH)),
    session: AsyncSession = Depends(get_session),
) -> DispatchOptions:
    """Ranked units for an operator to choose from. It suggests; it does not send.

    `available_units` is returned alongside the list so an empty `suggestions`
    is never read as "no units exist". Those are different situations — every
    unit busy, versus every unit more than two kilometres away — and an operator
    reaching for the radio needs to know which one they are in.
    """
    moment = now_utc()
    incident = await incident_service.load(session, incident_id)
    point = await incident_service.incident_location(session, incident)
    units = await incident_service.candidates(session, at=moment)

    suggestions = dispatch_service.suggest(
        units,
        incident_type=incident.type,
        incident_location=point,
        limit=limit,
    )
    available = sum(1 for unit in units if unit.status == "available")

    if not suggestions and available == 0:
        note = (
            "No unit is available. Every unit on the roster is already assigned "
            "or off duty — dispatch by radio and log it here afterwards."
        )
        note_mr = (
            "कोणतेही पथक उपलब्ध नाही. यादीतील सर्व पथके आधीच नेमलेली किंवा "
            "ड्युटीवर नाहीत — रेडिओवरून पाठवा आणि नंतर येथे नोंद करा."
        )
    elif not suggestions:
        note = (
            f"{available} unit(s) are free but none is within "
            f"{int(dispatch_service.MAX_SUGGEST_DISTANCE_M)} m of this incident."
        )
        note_mr = (
            f"{available} पथके मोकळी आहेत, पण या घटनेपासून "
            f"{int(dispatch_service.MAX_SUGGEST_DISTANCE_M)} मीटरच्या आत एकही नाही."
        )
    elif point is None:
        note = (
            "This incident has no location, so these units are ranked by type "
            "only. The distances are unknown, not zero."
        )
        note_mr = (
            "या घटनेचे ठिकाण नोंदलेले नाही, त्यामुळे ही पथके फक्त प्रकारानुसार "
            "क्रमाने आहेत. अंतर माहीत नाही — शून्य नाही."
        )
    else:
        note = (
            "Walking estimates through a crowd at "
            f"{dispatch_service.CROWD_WALK_SPEED_MS} m/s, in a straight line. "
            "The real route is longer and may be blocked."
        )
        note_mr = (
            f"गर्दीतून {dispatch_service.CROWD_WALK_SPEED_MS} मी/सेकंद या वेगाने, "
            "सरळ रेषेत काढलेला अंदाज. प्रत्यक्ष मार्ग यापेक्षा लांब असतो आणि बंदही असू शकतो."
        )

    return DispatchOptions(
        incident_id=incident.id,
        suggestions=[SuggestionOut(**asdict(s)) for s in suggestions],
        available_units=available,
        note=note,
        note_mr=note_mr,
        generated_at=moment,
    )


@router.post("/incidents/{incident_id}/dispatch", response_model=IncidentOut)
async def dispatch_unit(
    incident_id: uuid.UUID,
    payload: DispatchRequest,
    actor: Actor = Depends(require(Permission.INCIDENT_DISPATCH)),
    session: AsyncSession = Depends(get_session),
) -> IncidentOut:
    """Send a named unit. A human picked it; the audit log records who."""
    incident = await incident_service.load(session, incident_id)
    _, responder = await incident_service.dispatch(
        session,
        incident,
        responder_id=payload.responder_id,
        actor_id=actor.id,
        actor_role=actor.user.role,
        note=payload.note,
        override_reason=payload.override_reason,
        ip=actor.ip,
        user_agent=actor.user_agent,
    )
    out = await _single(session, incident)
    await session.commit()

    await incident_service.publish(
        events.INCIDENT_DISPATCHED,
        incident,
        session=session,
        extra={"call_sign": responder.call_sign, "unit_type": responder.unit_type},
    )
    return out


# ---------------------------------------------------------------------------
# responders
# ---------------------------------------------------------------------------
async def _responder_point(session: AsyncSession, responder_id: uuid.UUID) -> tuple[float, float] | None:
    row = (
        await session.execute(
            select(func.ST_X(Responder.current_location), func.ST_Y(Responder.current_location)).where(
                Responder.id == responder_id
            )
        )
    ).first()
    if row and row[0] is not None:
        return float(row[0]), float(row[1])
    return None


@router.get("/responders", response_model=list[ResponderOut])
async def list_responders(
    unit_type: str | None = None,
    status: str | None = Query(default=None, description="available | assigned | on_scene | off_duty"),
    _: Actor = Depends(require(Permission.INCIDENT_VIEW)),
    session: AsyncSession = Depends(get_session),
) -> list[ResponderOut]:
    """The roster, with how old each position is.

    `seconds_since_ping` is on every row rather than only on the stale ones,
    because a board that flags staleness only past a threshold teaches an
    operator that an unflagged dot is current — and the threshold is a
    judgement, not a fact about that unit.
    """
    moment = now_utc()
    stmt = select(
        Responder,
        func.ST_X(Responder.current_location),
        func.ST_Y(Responder.current_location),
    )
    if unit_type:
        stmt = stmt.where(Responder.unit_type == unit_type)
    if status:
        stmt = stmt.where(Responder.status == status)
    rows = list(await session.execute(stmt.order_by(Responder.unit_type, Responder.call_sign)))

    assignments: dict[uuid.UUID, Incident] = {}
    unit_ids = {r[0].id for r in rows}
    if unit_ids:
        open_rows = await session.execute(
            select(Incident).where(
                Incident.assigned_responder_id.in_(unit_ids),
                Incident.status.in_([str(s) for s in incident_service.OPEN_STATUSES]),
            )
        )
        for incident in open_rows.scalars():
            if incident.assigned_responder_id is not None:
                assignments[incident.assigned_responder_id] = incident

    out: list[ResponderOut] = []
    for responder, lon, lat in rows:
        incident = assignments.get(responder.id)
        out.append(
            ResponderOut(
                id=responder.id,
                call_sign=responder.call_sign,
                unit_type=responder.unit_type,
                status=responder.status,
                location=(float(lon), float(lat)) if lon is not None else None,
                last_ping_at=responder.last_ping_at,
                seconds_since_ping=(
                    round((moment - responder.last_ping_at).total_seconds(), 1)
                    if responder.last_ping_at
                    else None
                ),
                assigned_incident_id=incident.id if incident else None,
                assigned_incident_reference=incident.reference if incident else None,
            )
        )
    return out


@router.post("/responders/{responder_id}/ping", response_model=ResponderOut)
async def ping_responder(
    responder_id: uuid.UUID,
    payload: ResponderPing,
    actor: Actor = Depends(require(Permission.INCIDENT_UPDATE_ANY)),
    session: AsyncSession = Depends(get_session),
) -> ResponderOut:
    """A unit reporting where it is.

    A responder may only move their own unit. The control room — anyone holding
    `incident:dispatch` — may correct any unit's position, because a radio call
    saying "we are at gate 3" has to be able to reach the board when the phone
    in the responder's pocket is dead. That is the case the override exists for,
    and it is the reason the board is trustworthy at all.
    """
    responder = await session.get(Responder, responder_id)
    if responder is None:
        raise AppError("RESPONDER_NOT_FOUND", details={"responder_id": str(responder_id)})

    if responder.user_id != actor.id and not actor.can(Permission.INCIDENT_DISPATCH):
        raise AppError(
            "FORBIDDEN",
            details={"reason": "you may only report the position of your own unit"},
        )

    moment = now_utc()
    lon, lat = payload.location
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        raise AppError("BAD_REQUEST", details={"reason": "coordinates out of range"})

    responder.current_location = f"SRID=4326;POINT({lon} {lat})"
    responder.last_ping_at = moment
    if payload.status is not None:
        # A unit cannot free itself while it is on an open incident — that
        # would take it off the board with the incident still assigned to it,
        # and the next dispatch would double-book a unit already in use.
        if payload.status == "available" and responder.status in ("assigned", "on_scene"):
            still_open = await session.scalar(
                select(func.count())
                .select_from(Incident)
                .where(
                    Incident.assigned_responder_id == responder.id,
                    Incident.status.in_([str(s) for s in incident_service.OPEN_STATUSES]),
                )
            )
            if still_open:
                raise AppError(
                    "CONFLICT",
                    details={
                        "reason": "this unit is still assigned to an open incident",
                        "call_sign": responder.call_sign,
                    },
                )
        responder.status = payload.status

    await session.commit()

    return ResponderOut(
        id=responder.id,
        call_sign=responder.call_sign,
        unit_type=responder.unit_type,
        status=responder.status,
        location=(lon, lat),
        last_ping_at=moment,
        seconds_since_ping=0.0,
    )


# ---------------------------------------------------------------------------
# missing persons
# ---------------------------------------------------------------------------
def _missing_out(
    record: MissingPerson,
    *,
    incident: Incident | None,
    zone: Zone | None,
    at: datetime,
) -> MissingPersonOut:
    end = record.resolved_at or at
    return MissingPersonOut(
        id=record.id,
        incident_id=record.incident_id,
        incident_reference=incident.reference if incident else None,
        name=record.name,
        age=record.age,
        description=record.description,
        has_photo=record.photo_uri is not None,
        last_seen_zone_id=record.last_seen_zone_id,
        last_seen_zone_code=zone.code if zone else None,
        last_seen_at=record.last_seen_at,
        language=record.language,
        status=record.status,
        reported_at=record.reported_at,
        resolved_at=record.resolved_at,
        purge_after=record.purge_after,
        open_for_seconds=round((end - record.reported_at).total_seconds(), 1),
    )


async def _missing_context(
    session: AsyncSession, records: list[MissingPerson]
) -> tuple[dict[uuid.UUID, Incident], dict[uuid.UUID, Zone]]:
    incident_ids = {r.incident_id for r in records if r.incident_id}
    incidents: dict[uuid.UUID, Incident] = {}
    if incident_ids:
        incidents = {
            i.id: i
            for i in (await session.execute(select(Incident).where(Incident.id.in_(incident_ids)))).scalars()
        }
    zone_ids = {r.last_seen_zone_id for r in records if r.last_seen_zone_id}
    zones: dict[uuid.UUID, Zone] = {}
    if zone_ids:
        zones = {z.id: z for z in (await session.execute(select(Zone).where(Zone.id.in_(zone_ids)))).scalars()}
    return incidents, zones


@router.post("/missing-persons", response_model=MissingPersonOut, status_code=201)
async def report_missing_person(
    payload: MissingPersonCreate,
    actor: Actor = Depends(require(Permission.INCIDENT_REPORT)),
    session: AsyncSession = Depends(get_session),
) -> MissingPersonOut:
    """Open a case, and the incident that puts it on the same board as everything else.

    Graded `high`, not `critical`: ten minutes rather than three. A missing
    person needs a search organised properly, and grading every case critical
    would queue it ahead of the cardiac arrest — the one call this system must
    never get wrong.
    """
    moment = now_utc()
    record, incident = await incident_service.report_missing_person(
        session,
        name=payload.name,
        contact_phone=payload.contact_phone,
        age=payload.age,
        description=payload.description,
        photo_uri=payload.photo_uri,
        last_seen_zone_id=payload.last_seen_zone_id,
        last_seen_at=payload.last_seen_at,
        language=payload.language,
        reported_by=actor.id,
        source=_resolve_source(actor, None),
        actor_role=actor.user.role,
        ip=actor.ip,
        user_agent=actor.user_agent,
        at=moment,
    )
    zone = await session.get(Zone, record.last_seen_zone_id) if record.last_seen_zone_id else None
    out = _missing_out(record, incident=incident, zone=zone, at=moment)
    await session.commit()

    # Broadcast so the volunteer app in the surrounding zones hears about it —
    # Section 5/E2. The name and the photo are deliberately not in the payload:
    # the socket fans out to every connected console, and the details belong to
    # the people working the case, who fetch them from the case itself.
    await incident_service.publish(
        events.INCIDENT_RAISED,
        incident,
        session=session,
        extra={"missing_person_id": str(record.id), "age": record.age},
    )
    return out


@router.get("/missing-persons", response_model=Page[MissingPersonOut])
async def list_missing_persons(
    status: str | None = Query(default=None, description="open | sighted | reunited | closed_unresolved"),
    open_only: bool = Query(default=True),
    zone_id: uuid.UUID | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _: Actor = Depends(require(Permission.INCIDENT_VIEW)),
    session: AsyncSession = Depends(get_session),
) -> Page[MissingPersonOut]:
    """Oldest first — the opposite of every other list in this API.

    A missing person who has been missing for two hours is the one the
    announcement desk works next, and a newest-first list buries them under
    every case reported since.
    """
    moment = now_utc()
    stmt = select(MissingPerson)
    if status:
        stmt = stmt.where(MissingPerson.status == status)
    elif open_only:
        stmt = stmt.where(MissingPerson.status.in_(("open", "sighted")))
    if zone_id:
        stmt = stmt.where(MissingPerson.last_seen_zone_id == zone_id)

    total = await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = await session.execute(
        stmt.order_by(MissingPerson.reported_at.asc()).limit(limit).offset(offset)
    )
    records = list(rows.scalars())
    incidents, zones = await _missing_context(session, records)

    return Page[MissingPersonOut](
        items=[
            _missing_out(
                record,
                incident=incidents.get(record.incident_id) if record.incident_id else None,
                zone=zones.get(record.last_seen_zone_id) if record.last_seen_zone_id else None,
                at=moment,
            )
            for record in records
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/missing-persons/{case_id}", response_model=MissingPersonOut)
async def get_missing_person(
    case_id: uuid.UUID,
    _: Actor = Depends(require(Permission.INCIDENT_VIEW)),
    session: AsyncSession = Depends(get_session),
) -> MissingPersonOut:
    record = await incident_service.load_missing_person(session, case_id)
    incident = await session.get(Incident, record.incident_id) if record.incident_id else None
    zone = await session.get(Zone, record.last_seen_zone_id) if record.last_seen_zone_id else None
    return _missing_out(record, incident=incident, zone=zone, at=now_utc())


@router.patch("/missing-persons/{case_id}", response_model=MissingPersonOut)
async def update_missing_person(
    case_id: uuid.UUID,
    payload: MissingPersonUpdate,
    actor: Actor = Depends(require(Permission.INCIDENT_UPDATE_LOW)),
    session: AsyncSession = Depends(get_session),
) -> MissingPersonOut:
    """Sighted, reunited, or closed without finding them.

    A volunteer may mark a sighting — that is the whole reason the case is
    broadcast to them. Ending a case needs `incident:update_any`, because
    "reunited" closes the incident and stands the search down, and it is a claim
    somebody has to own.
    """
    if payload.status != "sighted" and not actor.can(Permission.INCIDENT_UPDATE_ANY):
        raise AppError(
            "FORBIDDEN",
            details={
                "reason": "closing a missing-person case needs incident:update_any",
                "you_may_set": ["sighted"],
                "missing_permissions": [str(Permission.INCIDENT_UPDATE_ANY)],
            },
        )

    record = await incident_service.load_missing_person(session, case_id)
    await incident_service.update_missing_person(
        session,
        record,
        status=payload.status,
        actor_id=actor.id,
        actor_role=actor.user.role,
        note=payload.note,
        zone_id=payload.zone_id,
        ip=actor.ip,
        user_agent=actor.user_agent,
    )

    incident = await session.get(Incident, record.incident_id) if record.incident_id else None
    zone = await session.get(Zone, record.last_seen_zone_id) if record.last_seen_zone_id else None
    out = _missing_out(record, incident=incident, zone=zone, at=now_utc())
    await session.commit()

    if incident is not None:
        await incident_service.publish(
            events.INCIDENT_UPDATED,
            incident,
            session=session,
            extra={"missing_person_id": str(record.id), "case_status": record.status},
        )
    return out
