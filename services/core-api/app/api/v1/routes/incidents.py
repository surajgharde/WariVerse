"""Incidents, SOS and dispatch (Section 4/M4, Phase 5).

    POST /sos                        POST /incidents
    GET  /incidents                  GET  /incidents/{id}
    PATCH /incidents/{id}            POST /incidents/{id}/dispatch
    GET  /incidents/{id}/dispatch-options
    GET  /responders                 POST /responders/{id}/ping

Three things this module is careful about.

**A client never chooses its own `source`.** `_resolve_source` derives it from
the caller's role and the route they came in on. Source is not decoration: an
open SOS is found by `source == "pilgrim_sos"`, so a pilgrim filing a
lost-umbrella report under that source would make their *next* panic press
attach itself to the umbrella. Provenance a caller can set is provenance that
means nothing.

**A pilgrim can raise an incident and read back only their own.** `/incidents`
requires `incident:view`, which pilgrims do not have. `GET /incidents/{id}` lets
the reporter read the one they filed — they are entitled to know what happened
to their own emergency, and to nothing else.

**The SOS route cannot fail on a rate limit.** See `incident_service.raise_sos`.
There is no 429 on this path by construction.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import events
from app.core.db import get_session
from app.core.deps import Actor, require
from app.core.errors import AppError
from app.core.permissions import Permission, Role
from app.core.security import now_utc
from app.models import Incident, Responder, Zone
from app.models.incidents import IncidentSeverity, IncidentStatus, IncidentType
from app.schemas.common import ErrorResponse, Page
from app.schemas.incidents import (
    DispatchOptions,
    DispatchRequest,
    IncidentCreate,
    IncidentEventOut,
    IncidentOut,
    IncidentUpdate,
    ResponderOut,
    ResponderPing,
    SosAck,
    SosCreate,
    SuggestionOut,
)
from app.services import dispatch_service, incident_service

router = APIRouter(tags=["incidents"], responses={404: {"model": ErrorResponse}})


# ---------------------------------------------------------------------------
# shaping
# ---------------------------------------------------------------------------
async def _zones_for(session: AsyncSession, incidents: list[Incident]) -> dict[uuid.UUID, Zone]:
    ids = {i.zone_id for i in incidents if i.zone_id}
    if not ids:
        return {}
    rows = await session.execute(select(Zone).where(Zone.id.in_(ids)))
    return {z.id: z for z in rows.scalars()}


async def _call_signs(session: AsyncSession, incidents: list[Incident]) -> dict[uuid.UUID, str]:
    ids = {i.assigned_responder_id for i in incidents if i.assigned_responder_id}
    if not ids:
        return {}
    rows = await session.execute(select(Responder.id, Responder.call_sign).where(Responder.id.in_(ids)))
    return dict(rows.all())  # type: ignore[arg-type]


async def _point_of(session: AsyncSession, incident: Incident) -> tuple[float, float] | None:
    if incident.location is None:
        return None
    row = (
        await session.execute(
            select(func.ST_X(Incident.location), func.ST_Y(Incident.location)).where(Incident.id == incident.id)
        )
    ).first()
    if row and row[0] is not None:
        return float(row[0]), float(row[1])
    return None


def _out(
    incident: Incident,
    *,
    zone: Zone | None = None,
    call_sign: str | None = None,
    location: tuple[float, float] | None = None,
    timeline: list[IncidentEventOut] | None = None,
    at: datetime | None = None,
) -> IncidentOut:
    moment = at or now_utc()
    end = incident.closed_at or incident.resolved_at or moment
    started_at = incident.client_reported_at or incident.created_at

    return IncidentOut(
        id=incident.id,
        reference=incident.reference,
        type=IncidentType(incident.type),
        severity=IncidentSeverity(incident.severity),
        status=IncidentStatus(incident.status),
        source=incident.source,
        zone_id=incident.zone_id,
        zone_code=zone.code if zone else None,
        zone_name_mr=zone.name_mr if zone else None,
        location=location,
        description=incident.description,
        # Whether a voice note exists, never where it is. The URI is a signed
        # object-store path and is not a display field.
        has_audio_note=bool(incident.audio_note_uri),
        sla_due_at=incident.sla_due_at,
        sla_breached=incident.sla_breached,
        # Negative once the clock has run out — the console shows "2m over"
        # rather than clamping to zero and hiding how far past due it is.
        seconds_to_sla=round((incident.sla_due_at - moment).total_seconds(), 1),
        first_response_at=incident.first_response_at,
        assigned_responder_id=incident.assigned_responder_id,
        assigned_call_sign=call_sign,
        client_reported_at=incident.client_reported_at,
        delayed_by_seconds=(
            round((incident.created_at - started_at).total_seconds(), 1)
            if incident.client_reported_at
            else None
        ),
        alert_id=incident.alert_id,
        resolved_at=incident.resolved_at,
        closed_at=incident.closed_at,
        outcome_note=incident.outcome_note,
        created_at=incident.created_at,
        seconds_open=round((end - incident.created_at).total_seconds(), 1),
        timeline=timeline or [],
    )


def _resolve_source(actor: Actor, requested: str) -> str:
    """Derive provenance from who is calling, not from what they claim.

    A control-room operator logging a phone call may say so; a pilgrim may not
    describe their own report as coming from the control room. The narrowing
    here is the whole reason `source` can be trusted afterwards.
    """
    if actor.role == Role.PILGRIM:
        return "pilgrim_report"
    if actor.role == Role.VOLUNTEER and requested not in ("volunteer_report", "phone_call"):
        return "volunteer_report"
    if requested == "pilgrim_sos":
        # Only POST /sos may produce this source. Nothing else, from any role.
        return "control_room"
    return requested


# ---------------------------------------------------------------------------
# SOS
# ---------------------------------------------------------------------------
@router.post("/sos", response_model=SosAck, status_code=201)
async def raise_sos(
    payload: SosCreate,
    actor: Actor = Depends(require(Permission.SOS_RAISE)),
    session: AsyncSession = Depends(get_session),
) -> SosAck:
    """The panic button.

    This route has no failure mode for "too many requests". Pressing four times
    in ten minutes attaches to the caller's existing open SOS and records the
    repeat; the phone still gets a reference number back. Section 9 permits a
    rate limit here and then forbids hard-blocking, and the only way to honour
    both is to make the limit change *what happens*, not *whether it happens*.

    Severity is always critical on intake. A frightened person is not triaging
    themselves, and an operator can re-grade in one call once they know more.
    """
    result = await incident_service.raise_sos(
        session,
        phone_hash=actor.user.phone_hash,
        reported_by=actor.id,
        incident_type=payload.type,
        zone_id=payload.zone_id,
        location=payload.location,
        description=payload.description,
        audio_note_uri=payload.audio_note_uri,
        client_reported_at=payload.client_reported_at,
        actor_role=actor.user.role,
        ip=actor.ip,
        user_agent=actor.user_agent,
    )
    incident = result.incident

    # If a unit is already assigned — the repeat-press case — the pilgrim gets a
    # real ETA. Otherwise they get told help has been informed, which is true,
    # rather than a spinner or a fabricated number.
    eta: float | None = None
    call_sign: str | None = None
    if incident.assigned_responder_id:
        responder = await session.get(Responder, incident.assigned_responder_id)
        if responder is not None:
            call_sign = responder.call_sign
            here = await incident_service.incident_location(session, incident)
            unit = await _responder_point(session, responder)
            if here and unit:
                eta = dispatch_service.walk_eta(dispatch_service.haversine_m(unit, here)).total_seconds()

    message, message_mr = _sos_message(incident.reference, call_sign, eta, result.joined_existing)

    await session.commit()
    await incident_service.publish(
        events.INCIDENT_RAISED if not result.joined_existing else events.INCIDENT_UPDATED,
        incident,
        session=session,
        extra={"press_count": result.press_count, "repeat": result.joined_existing},
    )

    return SosAck(
        incident_id=incident.id,
        reference=incident.reference,
        status=IncidentStatus(incident.status),
        message=message,
        message_mr=message_mr,
        responder_eta_seconds=round(eta, 1) if eta is not None else None,
        responder_call_sign=call_sign,
        joined_existing=result.joined_existing,
        received_at=now_utc(),
    )


def _sos_message(
    ref: str, call_sign: str | None, eta_seconds: float | None, joined: bool
) -> tuple[str, str]:
    """What the pilgrim reads. Marathi is the operational text.

    Every branch says something concrete. "Help has been informed" is a fact;
    an empty ETA field is not, and Section 4/M4 is explicit that the pilgrim is
    never left staring at a spinner.
    """
    if eta_seconds is not None and call_sign:
        minutes = max(1, round(eta_seconds / 60))
        return (
            f"Help is on the way. Reference {ref}. Unit {call_sign} is about {minutes} minutes away. "
            f"Stay where you are if it is safe to do so.",
            f"मदत येत आहे. संदर्भ क्रमांक {ref}. पथक {call_sign} अंदाजे {minutes} मिनिटांत पोहोचेल. "
            f"सुरक्षित असल्यास आहात तिथेच थांबा.",
        )
    if joined:
        return (
            f"We already have your call, reference {ref}. The control room has been told again. "
            f"Stay where you are if it is safe to do so.",
            f"तुमची नोंद आमच्याकडे आहे, संदर्भ क्रमांक {ref}. नियंत्रण कक्षाला पुन्हा कळवले आहे. "
            f"सुरक्षित असल्यास आहात तिथेच थांबा.",
        )
    return (
        f"Your call has been received. Reference {ref}. The control room has been told and is assigning "
        f"a team. Stay where you are if it is safe to do so.",
        f"तुमची मदतीची विनंती मिळाली आहे. संदर्भ क्रमांक {ref}. नियंत्रण कक्षाला कळवले असून पथक "
        f"पाठवण्याची व्यवस्था होत आहे. सुरक्षित असल्यास आहात तिथेच थांबा.",
    )


async def _responder_point(session: AsyncSession, responder: Responder) -> tuple[float, float] | None:
    if responder.current_location is None:
        return None
    row = (
        await session.execute(
            select(func.ST_X(Responder.current_location), func.ST_Y(Responder.current_location)).where(
                Responder.id == responder.id
            )
        )
    ).first()
    if row and row[0] is not None:
        return float(row[0]), float(row[1])
    return None


# ---------------------------------------------------------------------------
# reporting and reading
# ---------------------------------------------------------------------------
@router.post("/incidents", response_model=IncidentOut, status_code=201)
async def create_incident(
    payload: IncidentCreate,
    actor: Actor = Depends(require(Permission.INCIDENT_REPORT)),
    session: AsyncSession = Depends(get_session),
) -> IncidentOut:
    """Control-room entry, a volunteer's report, or a logged phone call."""
    contact_hash: str | None = None
    if payload.contact_phone:
        contact_hash = await incident_service.store_contact(session, payload.contact_phone, at=now_utc())

    incident = await incident_service.create(
        session,
        incident_type=payload.type,
        severity=payload.severity,
        source=_resolve_source(actor, payload.source),
        zone_id=payload.zone_id,
        location=payload.location,
        description=payload.description,
        reported_by=actor.id,
        reporter_phone_hash=contact_hash or actor.user.phone_hash,
        actor_role=actor.user.role,
        ip=actor.ip,
        user_agent=actor.user_agent,
    )

    zone = await session.get(Zone, incident.zone_id) if incident.zone_id else None
    out = _out(incident, zone=zone, location=payload.location)
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
    open_only: bool = Query(default=True, description="Only incidents still needing attention"),
    sla_breached: bool | None = None,
    since_hours: int = Query(default=24, ge=1, le=720),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _: Actor = Depends(require(Permission.INCIDENT_VIEW)),
    session: AsyncSession = Depends(get_session),
) -> Page[IncidentOut]:
    """The incident board.

    Ordered by SLA urgency, not by recency: whatever is closest to breaching (or
    furthest past it) comes first. An operator working down this list is working
    down the list of people who have been waiting longest relative to how bad
    their situation is, which is the only ordering that makes sense when both
    columns are moving.
    """
    stmt = select(Incident).where(Incident.created_at >= now_utc() - timedelta(hours=since_hours))
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

    total = await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = await session.execute(
        stmt.order_by(Incident.sla_breached.desc(), Incident.sla_due_at.asc()).limit(limit).offset(offset)
    )
    incidents = list(rows.scalars())

    zones = await _zones_for(session, incidents)
    signs = await _call_signs(session, incidents)
    moment = now_utc()

    return Page[IncidentOut](
        items=[
            _out(
                i,
                zone=zones.get(i.zone_id) if i.zone_id else None,
                call_sign=signs.get(i.assigned_responder_id) if i.assigned_responder_id else None,
                at=moment,
            )
            for i in incidents
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/incidents/{incident_id}", response_model=IncidentOut)
async def get_incident(
    incident_id: uuid.UUID,
    actor: Actor = Depends(require(Permission.INCIDENT_REPORT)),
    session: AsyncSession = Depends(get_session),
) -> IncidentOut:
    """One incident with its full timeline.

    Gated on `incident:report` rather than `incident:view` so a pilgrim can read
    back the SOS they raised — and then narrowed below to *only* that one. They
    are entitled to know what happened to their own emergency and to nothing
    else, and the timeline is where "what happened" actually lives.
    """
    incident = await incident_service.load(session, incident_id)

    if not actor.can(Permission.INCIDENT_VIEW) and incident.reported_by != actor.id:
        # 404 rather than 403: confirming an incident exists to somebody who may
        # not see it is itself a disclosure.
        raise AppError("INCIDENT_NOT_FOUND", details={"incident_id": str(incident_id)})

    zone = await session.get(Zone, incident.zone_id) if incident.zone_id else None
    responder = (
        await session.get(Responder, incident.assigned_responder_id)
        if incident.assigned_responder_id
        else None
    )
    rows = await incident_service.timeline(session, incident.id)

    return _out(
        incident,
        zone=zone,
        call_sign=responder.call_sign if responder else None,
        location=await _point_of(session, incident),
        timeline=[
            IncidentEventOut(
                id=e.id,
                action=e.action,
                note=e.note,
                actor_id=e.actor_id,
                meta=e.meta,
                created_at=e.created_at,
            )
            for e in rows
        ],
    )


@router.patch("/incidents/{incident_id}", response_model=IncidentOut)
async def update_incident(
    incident_id: uuid.UUID,
    payload: IncidentUpdate,
    actor: Actor = Depends(require(Permission.INCIDENT_UPDATE_LOW)),
    session: AsyncSession = Depends(get_session),
) -> IncidentOut:
    """Move an incident along, or re-grade it.

    A volunteer holds `incident:update_low` and may work low-severity incidents
    only; anything above that needs `incident:update_any`. The check is here
    rather than in the permission matrix because it depends on the *row*, and a
    permission cannot see a row.
    """
    incident = await incident_service.load(session, incident_id)

    if IncidentSeverity(incident.severity) != IncidentSeverity.LOW and not actor.can(
        Permission.INCIDENT_UPDATE_ANY
    ):
        raise AppError(
            "FORBIDDEN",
            details={
                "reason": "this incident's severity is above what your role may update",
                "severity": incident.severity,
            },
        )

    if payload.severity is not None:
        if not actor.can(Permission.INCIDENT_UPDATE_ANY):
            raise AppError("FORBIDDEN", details={"reason": "re-grading needs incident:update_any"})
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
    elif payload.note and payload.severity is None:
        await incident_service.add_event(
            session, incident, action="note", actor_id=actor.id, note=payload.note
        )

    zone = await session.get(Zone, incident.zone_id) if incident.zone_id else None
    responder = (
        await session.get(Responder, incident.assigned_responder_id)
        if incident.assigned_responder_id
        else None
    )
    out = _out(incident, zone=zone, call_sign=responder.call_sign if responder else None)
    await session.commit()
    await incident_service.publish(events.INCIDENT_UPDATED, incident, session=session)
    return out


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------
_DISPATCH_NOTE = (
    "Suggestions are ranked by unit type first, then straight-line distance. "
    "ETAs assume 0.7 m/s on foot through a crowd and are a floor, not a forecast — "
    "the real route is longer and may be blocked. A human confirms every dispatch."
)
_DISPATCH_NOTE_MR = (
    "पथकांची क्रमवारी आधी प्रकारानुसार, नंतर सरळ रेषेतील अंतरानुसार आहे. "
    # Latin digits in the Marathi string, matching every other measurement the
    # API returns. Devanagari numerals read naturally in prose but this figure
    # is quoted back in logs and radio traffic alongside the English one.
    "पोहोचण्याची वेळ गर्दीतून 0.7 मी/सेकंद या वेगाने काढलेली किमान वेळ आहे — प्रत्यक्ष मार्ग "
    "यापेक्षा लांब असू शकतो. प्रत्येक पथक माणूसच पाठवतो."
)


@router.get("/incidents/{incident_id}/dispatch-options", response_model=DispatchOptions)
async def dispatch_options(
    incident_id: uuid.UUID,
    limit: int = Query(default=5, ge=1, le=20),
    _: Actor = Depends(require(Permission.INCIDENT_DISPATCH)),
    session: AsyncSession = Depends(get_session),
) -> DispatchOptions:
    """Rank the units an operator might send. Suggest only — never dispatch.

    `available_units` is returned alongside the list so an empty `suggestions`
    is never read as "no units exist". Nothing within 2 km and nothing on the
    board are different problems.
    """
    incident = await incident_service.load(session, incident_id)
    units = await incident_service.candidates(session)
    here = await incident_service.incident_location(session, incident)

    ranked = dispatch_service.suggest(
        units,
        incident_type=IncidentType(incident.type),
        incident_location=here,
        limit=limit,
    )

    return DispatchOptions(
        incident_id=incident.id,
        suggestions=[
            SuggestionOut(
                responder_id=s.responder_id,
                call_sign=s.call_sign,
                unit_type=s.unit_type,
                distance_m=s.distance_m,
                eta_seconds=s.eta_seconds,
                type_rank=s.type_rank,
                caveats=s.caveats,
            )
            for s in ranked
        ],
        available_units=sum(1 for u in units if u.status == "available"),
        note=_DISPATCH_NOTE,
        note_mr=_DISPATCH_NOTE_MR,
        generated_at=now_utc(),
    )


@router.post("/incidents/{incident_id}/dispatch", response_model=IncidentOut)
async def dispatch_incident(
    incident_id: uuid.UUID,
    payload: DispatchRequest,
    actor: Actor = Depends(require(Permission.INCIDENT_DISPATCH)),
    session: AsyncSession = Depends(get_session),
) -> IncidentOut:
    """Send the unit the operator chose.

    The responder id comes from the request body, which means it came from a
    person. There is no endpoint anywhere in this service that picks one on its
    own — Section 4/M4's "no auto-dispatch" is enforced by there being no code
    that could.
    """
    incident = await incident_service.load(session, incident_id)
    incident, responder = await incident_service.dispatch(
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

    zone = await session.get(Zone, incident.zone_id) if incident.zone_id else None
    out = _out(incident, zone=zone, call_sign=responder.call_sign)
    await session.commit()
    await incident_service.publish(
        events.INCIDENT_UPDATED,
        incident,
        session=session,
        extra={"assigned_call_sign": responder.call_sign, "unit_type": responder.unit_type},
    )
    return out


# ---------------------------------------------------------------------------
# responders
# ---------------------------------------------------------------------------
@router.get("/responders", response_model=list[ResponderOut])
async def list_responders(
    unit_type: str | None = None,
    status: str | None = None,
    _: Actor = Depends(require(Permission.INCIDENT_VIEW)),
    session: AsyncSession = Depends(get_session),
) -> list[ResponderOut]:
    """The roster, with each unit's last known position and current assignment."""
    stmt = select(
        Responder.id,
        Responder.call_sign,
        Responder.unit_type,
        Responder.status,
        func.ST_X(Responder.current_location),
        func.ST_Y(Responder.current_location),
        Responder.last_ping_at,
    ).order_by(Responder.unit_type, Responder.call_sign)
    if unit_type:
        stmt = stmt.where(Responder.unit_type == unit_type)
    if status:
        stmt = stmt.where(Responder.status == status)

    rows = list(await session.execute(stmt))
    ids = [r[0] for r in rows]

    assignments: dict[uuid.UUID, tuple[uuid.UUID, str]] = {}
    if ids:
        open_rows = await session.execute(
            select(Incident.assigned_responder_id, Incident.id, Incident.reference).where(
                Incident.assigned_responder_id.in_(ids),
                Incident.status.in_([str(s) for s in incident_service.OPEN_STATUSES]),
            )
        )
        for responder_id, incident_id, ref in open_rows:
            assignments[responder_id] = (incident_id, ref)

    moment = now_utc()
    out: list[ResponderOut] = []
    for rid, call_sign, utype, status_value, lon, lat, last_ping in rows:
        assignment = assignments.get(rid)
        out.append(
            ResponderOut(
                id=rid,
                call_sign=call_sign,
                unit_type=utype,
                status=status_value,
                location=(float(lon), float(lat)) if lon is not None else None,
                last_ping_at=last_ping,
                seconds_since_ping=round((moment - last_ping).total_seconds(), 1) if last_ping else None,
                assigned_incident_id=assignment[0] if assignment else None,
                assigned_incident_reference=assignment[1] if assignment else None,
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

    Position is coarse by nature — a phone GPS in a crowd — and it is stored
    against the *unit*, not against a person. A responder is on duty and their
    location is operational; that is a different thing from tracking a pilgrim,
    which this system does not do (Section 12).
    """
    responder = await session.get(Responder, responder_id)
    if responder is None:
        raise AppError("RESPONDER_NOT_FOUND", details={"responder_id": str(responder_id)})

    lon, lat = payload.location
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        raise AppError("BAD_REQUEST", details={"reason": "coordinates out of range"})

    moment = now_utc()
    responder.current_location = f"SRID=4326;POINT({lon} {lat})"
    responder.last_ping_at = moment
    if payload.status is not None:
        responder.status = payload.status

    assignment = await session.execute(
        select(Incident.id, Incident.reference).where(
            Incident.assigned_responder_id == responder.id,
            Incident.status.in_([str(s) for s in incident_service.OPEN_STATUSES]),
        )
    )
    row = assignment.first()

    out = ResponderOut(
        id=responder.id,
        call_sign=responder.call_sign,
        unit_type=responder.unit_type,
        status=responder.status,
        location=(lon, lat),
        last_ping_at=moment,
        seconds_since_ping=0.0,
        assigned_incident_id=row[0] if row else None,
        assigned_incident_reference=row[1] if row else None,
    )
    await session.commit()
    return out
