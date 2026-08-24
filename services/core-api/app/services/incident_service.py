"""Incident lifecycle, SOS intake and SLA (Section 4/M4).

    reported -> triaged -> dispatched -> on_scene -> resolved -> closed

Four rules shape this module, and each one is a place where the obvious
implementation would have been worse.

**An SOS is never refused.** Section 9 sets a rate limit of 3 per 10 minutes and
then immediately says "never hard-block SOS". Those are not in tension once you
notice what the limit is actually for: it exists to stop the control room
drowning in duplicates, not to stop a frightened person getting help. So the
fourth press in ten minutes attaches to the caller's own open incident and adds
a line to its timeline — the button worked, the operator sees "pressed again,
4th time", and nothing was lost. `raise_sos` returns `joined_existing=True` so
the phone can still show a reference number.

**Nothing is auto-dispatched.** `dispatch` requires a responder id that a human
chose. `dispatch_service.suggest` ranks; it does not decide.

**Closing requires saying what was done.** `outcome_note` is mandatory to reach
`closed`, enforced here rather than in the schema, because the rule is about the
transition and not about the payload.

**The timeline is append-only and is the record.** Every state change writes an
`IncidentEvent`. The incident row carries the current state; the events carry
how it got there, which is the part a post-Wari review actually reads.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import events
from app.core.config import settings
from app.core.errors import AppError
from app.core.logging import get_logger
from app.core.redis_client import aw, redis
from app.core.security import encrypt_contact, hash_phone, normalise_phone, now_utc
from app.models import Incident, IncidentEvent, MissingPerson, Responder, Zone
from app.models.incidents import SLA_MINUTES, IncidentSeverity, IncidentStatus, IncidentType
from app.models.user import ContactSecret
from app.services import audit_service, dispatch_service
from app.services.audit_service import AuditAction

logger = get_logger(__name__)

#: Reference alphabet: no I, O, 0 or 1. These get read out over a radio in a
#: crowd, and "INC-I0" is a reference nobody transcribes correctly twice.
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

#: Statuses that still need somebody's attention. The command centre's
#: "open incidents" KPI counts exactly these.
OPEN_STATUSES: tuple[IncidentStatus, ...] = (
    IncidentStatus.REPORTED,
    IncidentStatus.TRIAGED,
    IncidentStatus.DISPATCHED,
    IncidentStatus.ON_SCENE,
)

#: Where an incident may go from where it is.
#:
#: `reported -> resolved` is deliberately allowed: a false alarm, or something
#: an operator handled over the radio in twenty seconds, should not have to be
#: dragged through triage and dispatch to be closed honestly.
_TRANSITIONS: dict[IncidentStatus, frozenset[IncidentStatus]] = {
    IncidentStatus.REPORTED: frozenset(
        {IncidentStatus.TRIAGED, IncidentStatus.DISPATCHED, IncidentStatus.RESOLVED}
    ),
    IncidentStatus.TRIAGED: frozenset(
        {IncidentStatus.DISPATCHED, IncidentStatus.RESOLVED}
    ),
    IncidentStatus.DISPATCHED: frozenset(
        {IncidentStatus.ON_SCENE, IncidentStatus.RESOLVED}
    ),
    IncidentStatus.ON_SCENE: frozenset({IncidentStatus.RESOLVED}),
    IncidentStatus.RESOLVED: frozenset({IncidentStatus.CLOSED}),
    IncidentStatus.CLOSED: frozenset(),
}

#: SOS duplicate-suppression window. Not a block — see the module docstring.
SOS_WINDOW_SECONDS = 600
_SOS_KEY = "sos:count:{phone_hash}"

#: How long a callback number is kept for an incident. Long enough to reunite a
#: family or follow up a medical case; short enough that the encrypted table is
#: not a standing archive of who was in trouble at the Wari.
CONTACT_TTL_DAYS = 30


def reference() -> str:
    return "INC-" + "".join(secrets.choice(_ALPHABET) for _ in range(6))


def sla_due_at(severity: IncidentSeverity | str, *, at: datetime) -> datetime:
    key = severity if isinstance(severity, IncidentSeverity) else IncidentSeverity(severity)
    return at + timedelta(minutes=SLA_MINUTES[key])


def _point(location: tuple[float, float] | None) -> str | None:
    if location is None:
        return None
    lon, lat = location
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        raise AppError("BAD_REQUEST", details={"reason": "coordinates out of range", "location": [lon, lat]})
    return f"SRID=4326;POINT({lon} {lat})"


async def store_contact(session: AsyncSession, phone: str, *, at: datetime) -> str:
    """Hash for the row, encrypt for the callback, keep neither longer than needed.

    Returns the hash. The raw number goes into `contact_secrets` with a TTL and
    is reachable only by the notifier — Section 12's PII rule, applied to
    incidents the same way Phase 2 applied it to passes.
    """
    normalised = normalise_phone(phone)
    phone_hash = hash_phone(normalised)
    session.add(
        ContactSecret(
            phone_hash=phone_hash,
            encrypted_phone=encrypt_contact(normalised),
            purpose="incident_contact",
            purge_after=at + timedelta(days=CONTACT_TTL_DAYS),
        )
    )
    return phone_hash


async def add_event(
    session: AsyncSession,
    incident: Incident,
    *,
    action: str,
    actor_id: uuid.UUID | None = None,
    note: str | None = None,
    meta: dict | None = None,
    at: datetime | None = None,
) -> IncidentEvent:
    """Append one line to the incident's timeline."""
    event = IncidentEvent(
        incident_id=incident.id,
        actor_id=actor_id,
        action=action,
        note=note,
        meta=meta or {},
        created_at=at or now_utc(),
    )
    session.add(event)
    return event


async def load(session: AsyncSession, incident_id: uuid.UUID) -> Incident:
    incident = await session.get(Incident, incident_id)
    if incident is None:
        raise AppError("INCIDENT_NOT_FOUND", details={"incident_id": str(incident_id)})
    return incident


async def load_by_reference(session: AsyncSession, ref: str) -> Incident:
    incident = await session.scalar(select(Incident).where(Incident.reference == ref.upper()))
    if incident is None:
        raise AppError("INCIDENT_NOT_FOUND", details={"reference": ref})
    return incident


async def timeline(session: AsyncSession, incident_id: uuid.UUID) -> list[IncidentEvent]:
    rows = await session.execute(
        select(IncidentEvent)
        .where(IncidentEvent.incident_id == incident_id)
        .order_by(IncidentEvent.created_at)
    )
    return list(rows.scalars())


# ---------------------------------------------------------------------------
# creation
# ---------------------------------------------------------------------------
async def create(
    session: AsyncSession,
    *,
    incident_type: IncidentType,
    severity: IncidentSeverity,
    source: str,
    zone_id: uuid.UUID | None = None,
    location: tuple[float, float] | None = None,
    description: str | None = None,
    reported_by: uuid.UUID | None = None,
    reporter_phone_hash: str | None = None,
    audio_note_uri: str | None = None,
    client_reported_at: datetime | None = None,
    alert_id: uuid.UUID | None = None,
    actor_role: str | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    at: datetime | None = None,
) -> Incident:
    """Open an incident and start its SLA clock.

    The clock starts at `client_reported_at` when the report was queued offline
    and is only now arriving. An SOS pressed twenty minutes ago in a dead spot
    has already used twenty minutes of its three-minute critical SLA, and the
    console must show that rather than a fresh clock — the delay is the single
    most important fact about a late-arriving emergency.
    """
    moment = at or now_utc()
    started_at = client_reported_at or moment
    if started_at > moment:
        # A client clock running fast must not buy an incident extra SLA.
        started_at = moment

    if zone_id is not None and await session.get(Zone, zone_id) is None:
        raise AppError("ZONE_NOT_FOUND", details={"zone_id": str(zone_id)})

    incident = Incident(
        reference=reference(),
        type=str(incident_type),
        severity=str(severity),
        status=str(IncidentStatus.REPORTED),
        zone_id=zone_id,
        location=_point(location),
        reported_by=reported_by,
        reporter_phone_hash=reporter_phone_hash,
        source=source,
        description=description,
        audio_note_uri=audio_note_uri,
        sla_due_at=sla_due_at(severity, at=started_at),
        client_reported_at=client_reported_at,
        alert_id=alert_id,
    )
    session.add(incident)
    await session.flush()

    await add_event(
        session,
        incident,
        action="reported",
        actor_id=reported_by,
        note=description,
        meta={
            "source": source,
            "severity": str(severity),
            "type": str(incident_type),
            **({"delayed_by_seconds": round((moment - started_at).total_seconds(), 1)} if client_reported_at else {}),
        },
        at=moment,
    )

    await audit_service.record(
        session,
        action=AuditAction.INCIDENT_CREATED,
        actor_id=reported_by,
        actor_role=actor_role,
        target_type="incident",
        target_id=incident.id,
        meta={
            "reference": incident.reference,
            "type": str(incident_type),
            "severity": str(severity),
            "source": source,
            "zone_id": str(zone_id) if zone_id else None,
        },
        ip=ip,
        user_agent=user_agent,
    )

    logger.info(
        "incident_created",
        extra={
            "reference": incident.reference,
            "type": str(incident_type),
            "severity": str(severity),
            "source": source,
        },
    )
    return incident


@dataclass(frozen=True, slots=True)
class SosResult:
    incident: Incident
    joined_existing: bool
    press_count: int


async def raise_sos(
    session: AsyncSession,
    *,
    phone_hash: str,
    reported_by: uuid.UUID | None,
    incident_type: IncidentType = IncidentType.MEDICAL,
    zone_id: uuid.UUID | None = None,
    location: tuple[float, float] | None = None,
    description: str | None = None,
    audio_note_uri: str | None = None,
    client_reported_at: datetime | None = None,
    actor_role: str | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    at: datetime | None = None,
) -> SosResult:
    """Take an SOS. Always.

    Over the rate limit the call does not fail — it finds the caller's own open
    SOS and records another press against it. The control room sees one incident
    with "pressed 4 times" on its timeline instead of four incidents, and the
    pilgrim still gets a reference number back.

    Every SOS is `critical` regardless of what the client asked for. A pilgrim
    is not triaging themselves, and the three-minute clock is the whole point of
    the button. An operator can re-grade it down in one call once they know more.
    """
    moment = at or now_utc()
    presses = await _count_press(phone_hash)

    if presses > settings.rate_limit_sos_per_10min:
        existing = await _open_sos_for(session, phone_hash)
        if existing is not None:
            await add_event(
                session,
                existing,
                action="sos_repeated",
                actor_id=reported_by,
                note="The caller pressed SOS again.",
                meta={"press_count": presses, "window_seconds": SOS_WINDOW_SECONDS},
                at=moment,
            )
            logger.info(
                "sos_repeat",
                extra={"reference": existing.reference, "press_count": presses},
            )
            return SosResult(incident=existing, joined_existing=True, press_count=presses)
        # Over the limit with nothing open: the earlier ones were resolved, and
        # this is a new emergency. Fall through and create it. Rate limiting an
        # SOS to protect a database is not a trade this system makes.

    incident = await create(
        session,
        incident_type=incident_type,
        severity=IncidentSeverity.CRITICAL,
        source="pilgrim_sos",
        zone_id=zone_id,
        location=location,
        description=description,
        reported_by=reported_by,
        reporter_phone_hash=phone_hash,
        audio_note_uri=audio_note_uri,
        client_reported_at=client_reported_at,
        actor_role=actor_role,
        ip=ip,
        user_agent=user_agent,
        at=moment,
    )
    return SosResult(incident=incident, joined_existing=False, press_count=presses)


async def _count_press(phone_hash: str) -> int:
    """Count presses in the window. Redis down means "first press" — the counter
    is a duplicate-suppression aid, and losing it must not lose the SOS."""
    key = _SOS_KEY.format(phone_hash=phone_hash)
    try:
        count = int(await aw(redis.incr(key)))
        if count == 1:
            await aw(redis.expire(key, SOS_WINDOW_SECONDS))
        return count
    except Exception as exc:
        logger.warning("sos_counter_unavailable", extra={"error": str(exc)})
        return 1


async def _open_sos_for(session: AsyncSession, phone_hash: str) -> Incident | None:
    return await session.scalar(
        select(Incident)
        .where(
            Incident.reporter_phone_hash == phone_hash,
            Incident.source == "pilgrim_sos",
            Incident.status.in_([str(s) for s in OPEN_STATUSES]),
        )
        .order_by(Incident.created_at.desc())
        .limit(1)
    )


# ---------------------------------------------------------------------------
# transitions
# ---------------------------------------------------------------------------
async def transition(
    session: AsyncSession,
    incident: Incident,
    *,
    to: IncidentStatus,
    actor_id: uuid.UUID,
    actor_role: str,
    note: str | None = None,
    outcome_note: str | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    at: datetime | None = None,
) -> Incident:
    moment = at or now_utc()
    current = IncidentStatus(incident.status)

    if current == IncidentStatus.CLOSED:
        raise AppError("INCIDENT_CLOSED", details={"reference": incident.reference})

    if to not in _TRANSITIONS[current]:
        raise AppError(
            "INVALID_TRANSITION",
            details={
                "reference": incident.reference,
                "from": str(current),
                "to": str(to),
                "allowed": sorted(str(s) for s in _TRANSITIONS[current]),
            },
        )

    if to == IncidentStatus.CLOSED and not (outcome_note or incident.outcome_note):
        raise AppError("OUTCOME_NOTE_REQUIRED", details={"reference": incident.reference})

    incident.status = str(to)

    # `first_response_at` is set the moment a unit is actually on the way. The
    # SLA measures how long the control room took to respond, not how long the
    # responder took to arrive — those are different failures with different
    # fixes, and conflating them hides the one this system can act on.
    if to in (IncidentStatus.DISPATCHED, IncidentStatus.ON_SCENE) and incident.first_response_at is None:
        incident.first_response_at = moment

    if to == IncidentStatus.RESOLVED:
        incident.resolved_at = moment
        await _release_responder(session, incident, at=moment)
    if to == IncidentStatus.CLOSED:
        incident.closed_at = moment
        if outcome_note:
            incident.outcome_note = outcome_note
        await _release_responder(session, incident, at=moment)

    await add_event(
        session,
        incident,
        action=f"status:{to}",
        actor_id=actor_id,
        note=note or outcome_note,
        meta={"from": str(current), "to": str(to)},
        at=moment,
    )

    await audit_service.record(
        session,
        action=(
            AuditAction.INCIDENT_CLOSED
            if to == IncidentStatus.CLOSED
            else AuditAction.INCIDENT_STATUS_CHANGED
        ),
        actor_id=actor_id,
        actor_role=actor_role,
        target_type="incident",
        target_id=incident.id,
        meta={
            "reference": incident.reference,
            "from": str(current),
            "to": str(to),
            "outcome_note": outcome_note,
            "seconds_open": round((moment - incident.created_at).total_seconds(), 1),
        },
        ip=ip,
        user_agent=user_agent,
    )
    return incident


async def regrade(
    session: AsyncSession,
    incident: Incident,
    *,
    severity: IncidentSeverity,
    actor_id: uuid.UUID,
    actor_role: str,
    note: str | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    at: datetime | None = None,
) -> Incident:
    """Change severity, and move the SLA clock with it.

    The clock is recomputed from the *original report time*, not from now.
    Re-grading a 20-minute-old normal incident up to critical does not hand the
    control room a fresh three minutes — it shows the breach that was already
    there. Otherwise re-grading becomes a way to make a late response look
    punctual, which is exactly the number an inquiry would check.
    """
    moment = at or now_utc()
    previous = incident.severity
    if str(severity) == previous:
        return incident

    started_at = incident.client_reported_at or incident.created_at
    incident.severity = str(severity)
    incident.sla_due_at = sla_due_at(severity, at=started_at)
    if incident.first_response_at is None:
        incident.sla_breached = incident.sla_due_at < moment

    await add_event(
        session,
        incident,
        action="regraded",
        actor_id=actor_id,
        note=note,
        meta={"from": previous, "to": str(severity), "sla_due_at": incident.sla_due_at.isoformat()},
        at=moment,
    )
    await audit_service.record(
        session,
        action=AuditAction.INCIDENT_STATUS_CHANGED,
        actor_id=actor_id,
        actor_role=actor_role,
        target_type="incident",
        target_id=incident.id,
        meta={"reference": incident.reference, "severity_from": previous, "severity_to": str(severity)},
        ip=ip,
        user_agent=user_agent,
    )
    return incident


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------
async def candidates(session: AsyncSession, *, at: datetime | None = None) -> list[dispatch_service.ResponderCandidate]:
    """Every unit, shaped for the ranker, with positions read out of PostGIS."""
    moment = at or now_utc()
    rows = await session.execute(
        select(
            Responder.id,
            Responder.call_sign,
            Responder.unit_type,
            Responder.status,
            func.ST_X(Responder.current_location),
            func.ST_Y(Responder.current_location),
            Responder.last_ping_at,
        )
    )
    out: list[dispatch_service.ResponderCandidate] = []
    for rid, call_sign, unit_type, status, lon, lat, last_ping in rows:
        out.append(
            dispatch_service.ResponderCandidate(
                responder_id=rid,
                call_sign=call_sign,
                unit_type=unit_type,
                status=status,
                location=(float(lon), float(lat)) if lon is not None and lat is not None else None,
                seconds_since_ping=(moment - last_ping).total_seconds() if last_ping else None,
            )
        )
    return out


async def incident_location(session: AsyncSession, incident: Incident) -> tuple[float, float] | None:
    """The incident's own point, falling back to the centroid of its zone.

    A zone centroid is a worse answer than a GPS fix and a much better one than
    nothing: it puts the suggestion in the right part of the temple, which is
    enough for an operator who knows the ground to pick correctly.
    """
    if incident.location is not None:
        row = (
            await session.execute(
                select(func.ST_X(Incident.location), func.ST_Y(Incident.location)).where(
                    Incident.id == incident.id
                )
            )
        ).first()
        if row and row[0] is not None:
            return float(row[0]), float(row[1])

    if incident.zone_id is not None:
        row = (
            await session.execute(
                select(
                    func.ST_X(func.ST_Centroid(Zone.geom)),
                    func.ST_Y(func.ST_Centroid(Zone.geom)),
                ).where(Zone.id == incident.zone_id)
            )
        ).first()
        if row and row[0] is not None:
            return float(row[0]), float(row[1])

    return None


async def dispatch(
    session: AsyncSession,
    incident: Incident,
    *,
    responder_id: uuid.UUID,
    actor_id: uuid.UUID,
    actor_role: str,
    note: str | None = None,
    override_reason: str | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    at: datetime | None = None,
) -> tuple[Incident, Responder]:
    """Assign a unit a human chose.

    There is no code path in this module that calls this without an `actor_id`.
    That is the "no auto-dispatch" rule expressed as a signature rather than as
    a comment.
    """
    moment = at or now_utc()

    if IncidentStatus(incident.status) in (IncidentStatus.RESOLVED, IncidentStatus.CLOSED):
        raise AppError("INCIDENT_CLOSED", details={"reference": incident.reference})

    responder = await session.get(Responder, responder_id)
    if responder is None:
        raise AppError("RESPONDER_NOT_FOUND", details={"responder_id": str(responder_id)})

    # Reassigning the unit that is already on this incident is a no-op, not an
    # error — an operator re-confirming under pressure should not see a failure.
    if responder.status != "available" and incident.assigned_responder_id != responder.id:
        raise AppError(
            "RESPONDER_UNAVAILABLE",
            details={"call_sign": responder.call_sign, "status": responder.status},
        )

    previous = incident.assigned_responder_id
    if previous and previous != responder.id:
        await _release_responder(session, incident, at=moment)

    incident.assigned_responder_id = responder.id
    responder.status = "assigned"

    if IncidentStatus(incident.status) in (IncidentStatus.REPORTED, IncidentStatus.TRIAGED):
        incident.status = str(IncidentStatus.DISPATCHED)
    if incident.first_response_at is None:
        incident.first_response_at = moment

    await add_event(
        session,
        incident,
        action="dispatched",
        actor_id=actor_id,
        note=note,
        meta={
            "responder_id": str(responder.id),
            "call_sign": responder.call_sign,
            "unit_type": responder.unit_type,
            "reassigned_from": str(previous) if previous else None,
            "override_reason": override_reason,
            "seconds_to_dispatch": round((moment - incident.created_at).total_seconds(), 1),
        },
        at=moment,
    )

    await audit_service.record(
        session,
        action=AuditAction.INCIDENT_DISPATCHED,
        actor_id=actor_id,
        actor_role=actor_role,
        target_type="incident",
        target_id=incident.id,
        meta={
            "reference": incident.reference,
            "call_sign": responder.call_sign,
            "unit_type": responder.unit_type,
            "override_reason": override_reason,
            "within_sla": incident.first_response_at <= incident.sla_due_at,
        },
        ip=ip,
        user_agent=user_agent,
    )

    logger.info(
        "incident_dispatched",
        extra={
            "reference": incident.reference,
            "call_sign": responder.call_sign,
            "seconds_to_dispatch": round((moment - incident.created_at).total_seconds(), 1),
        },
    )
    return incident, responder


async def _release_responder(session: AsyncSession, incident: Incident, *, at: datetime) -> None:
    """Put a unit back on the board when its incident ends."""
    if incident.assigned_responder_id is None:
        return
    responder = await session.get(Responder, incident.assigned_responder_id)
    if responder is not None and responder.status in ("assigned", "on_scene"):
        responder.status = "available"
        responder.last_ping_at = responder.last_ping_at or at


# ---------------------------------------------------------------------------
# SLA
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SlaBreach:
    incident: Incident
    overdue_seconds: float


async def sweep_sla(session: AsyncSession, *, at: datetime | None = None) -> list[SlaBreach]:
    """Mark incidents nobody responded to in time.

    Only incidents with no `first_response_at` can breach. Once a unit is on the
    way the control room has done the thing the SLA measures; how long the
    responder then takes is a different problem, and marking it as a control-room
    failure would push operators to dispatch anybody just to stop the clock.
    """
    moment = at or now_utc()
    rows = await session.execute(
        select(Incident).where(
            Incident.status.in_([str(s) for s in OPEN_STATUSES]),
            Incident.first_response_at.is_(None),
            Incident.sla_breached.is_(False),
            Incident.sla_due_at < moment,
        )
    )

    breaches: list[SlaBreach] = []
    for incident in rows.scalars():
        incident.sla_breached = True
        overdue = (moment - incident.sla_due_at).total_seconds()
        await add_event(
            session,
            incident,
            action="sla_breached",
            note="No responder was assigned within the SLA for this severity.",
            meta={
                "severity": incident.severity,
                "sla_minutes": SLA_MINUTES[IncidentSeverity(incident.severity)],
                "overdue_seconds": round(overdue, 1),
            },
            at=moment,
        )
        breaches.append(SlaBreach(incident=incident, overdue_seconds=overdue))

    if breaches:
        logger.warning("incident_sla_breached", extra={"count": len(breaches)})
    return breaches


async def open_counts(session: AsyncSession) -> dict[str, int]:
    """Counts for the command centre: by status, plus breached and critical."""
    rows = await session.execute(
        select(Incident.status, func.count())
        .where(Incident.status.in_([str(s) for s in OPEN_STATUSES]))
        .group_by(Incident.status)
    )
    counts = {status: int(count) for status, count in rows.all()}

    counts["total"] = sum(counts.values())
    counts["sla_breached"] = int(
        await session.scalar(
            select(func.count())
            .select_from(Incident)
            .where(
                Incident.status.in_([str(s) for s in OPEN_STATUSES]),
                Incident.sla_breached.is_(True),
            )
        )
        or 0
    )
    counts["critical"] = int(
        await session.scalar(
            select(func.count())
            .select_from(Incident)
            .where(
                Incident.status.in_([str(s) for s in OPEN_STATUSES]),
                Incident.severity == str(IncidentSeverity.CRITICAL),
            )
        )
        or 0
    )
    return counts


# ---------------------------------------------------------------------------
# missing persons (Section 5, E2)
# ---------------------------------------------------------------------------
#: Photos are purged this long after the case closes — Section 12. Measured from
#: closure rather than from report: a case still open on day 31 has not stopped
#: needing the photo, and purging it then would delete the only useful thing
#: about the record while the person is still missing.
PHOTO_RETENTION_DAYS = 30

_MISSING_OPEN = ("open", "sighted")


async def report_missing_person(
    session: AsyncSession,
    *,
    name: str,
    contact_phone: str,
    age: int | None = None,
    description: str | None = None,
    photo_uri: str | None = None,
    last_seen_zone_id: uuid.UUID | None = None,
    last_seen_at: datetime | None = None,
    language: str = "mr",
    reported_by: uuid.UUID | None = None,
    source: str | None = None,
    actor_role: str | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    at: datetime | None = None,
) -> tuple[MissingPerson, Incident]:
    """Open a missing-person case and the incident that carries it.

    Both, not one. The case holds the details that make the person findable; the
    incident puts it on the same board, with the same SLA and the same dispatch
    machinery, as everything else the control room is handling. A missing child
    that lives in its own list is a missing child nobody is assigned to.

    Severity is `high`, not `critical`: a ten-minute SLA rather than three. A
    missing person needs a search party organised properly, and grading every
    case critical would put it ahead of the cardiac arrest — which is the one
    call this system must never get wrong.
    """
    moment = at or now_utc()
    contact_hash = await store_contact(session, contact_phone, at=moment)

    incident = await create(
        session,
        incident_type=IncidentType.MISSING_PERSON,
        severity=IncidentSeverity.HIGH,
        # The caller passes the provenance it resolved from the actor's role.
        # The fallback is `phone_call` rather than `control_room` because a case
        # with no signed-in reporter reached the desk some other way, and
        # recording it as a control-room entry would invent a person.
        source=source or ("control_room" if reported_by else "phone_call"),
        zone_id=last_seen_zone_id,
        description=f"Missing person: {name}" + (f", age {age}" if age else ""),
        reported_by=reported_by,
        reporter_phone_hash=contact_hash,
        actor_role=actor_role,
        ip=ip,
        user_agent=user_agent,
        at=moment,
    )

    record = MissingPerson(
        incident_id=incident.id,
        name=name,
        age=age,
        description=description,
        photo_uri=photo_uri,
        last_seen_zone_id=last_seen_zone_id,
        last_seen_at=last_seen_at,
        contact_phone_hash=contact_hash,
        language=language,
        status="open",
        reported_at=moment,
    )
    session.add(record)
    await session.flush()

    logger.info("missing_person_reported", extra={"reference": incident.reference, "has_photo": bool(photo_uri)})
    return record, incident


async def load_missing_person(session: AsyncSession, case_id: uuid.UUID) -> MissingPerson:
    record = await session.get(MissingPerson, case_id)
    if record is None:
        raise AppError("MISSING_PERSON_NOT_FOUND", details={"case_id": str(case_id)})
    return record


async def update_missing_person(
    session: AsyncSession,
    record: MissingPerson,
    *,
    status: str,
    actor_id: uuid.UUID,
    actor_role: str,
    note: str | None = None,
    zone_id: uuid.UUID | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    at: datetime | None = None,
) -> MissingPerson:
    """Move a case along. Reunification closes it and starts the purge clock."""
    moment = at or now_utc()

    if record.status not in _MISSING_OPEN:
        raise AppError("MISSING_PERSON_CLOSED", details={"case_id": str(record.id), "status": record.status})

    previous = record.status
    record.status = status

    if status in ("reunited", "closed_unresolved"):
        record.resolved_at = moment
        record.purge_after = moment + timedelta(days=PHOTO_RETENTION_DAYS)

    incident = await session.get(Incident, record.incident_id) if record.incident_id else None
    if incident is not None:
        await add_event(
            session,
            incident,
            action=f"missing_person:{status}",
            actor_id=actor_id,
            note=note,
            meta={"from": previous, "to": status, "zone_id": str(zone_id) if zone_id else None},
            at=moment,
        )
        # Reunited closes the incident too, with the outcome the review will
        # want to read. A case that ends without the incident ending leaves an
        # open row on the board for something that is finished.
        if status == "reunited" and IncidentStatus(incident.status) not in (
            IncidentStatus.RESOLVED,
            IncidentStatus.CLOSED,
        ):
            incident.status = str(IncidentStatus.RESOLVED)
            incident.resolved_at = moment
            incident.outcome_note = note or f"{record.name} was reunited with their family."
            await _release_responder(session, incident, at=moment)

    await audit_service.record(
        session,
        action=AuditAction.INCIDENT_STATUS_CHANGED,
        actor_id=actor_id,
        actor_role=actor_role,
        target_type="missing_person",
        target_id=record.id,
        # The name is deliberately absent from the audit meta. The audit log is
        # append-only and never purged; putting a missing child's name in it
        # would outlive the 30-day retention the case itself promises.
        meta={"from": previous, "to": status, "incident_reference": incident.reference if incident else None},
        ip=ip,
        user_agent=user_agent,
    )
    return record


async def purge_missing_person_photos(session: AsyncSession, *, at: datetime | None = None) -> list[uuid.UUID]:
    """Drop photo references whose retention has run out.

    Returns the ids whose `photo_uri` was cleared so the caller can delete the
    objects themselves. This function owns the database row; it does not reach
    into the object store, because a purge that half-succeeds should leave the
    row still pointing at the blob rather than orphan it.
    """
    moment = at or now_utc()
    rows = await session.execute(
        select(MissingPerson).where(
            MissingPerson.purge_after.is_not(None),
            MissingPerson.purge_after < moment,
            MissingPerson.photo_uri.is_not(None),
        )
    )
    purged: list[uuid.UUID] = []
    for record in rows.scalars():
        record.photo_uri = None
        purged.append(record.id)
    return purged


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------
def event_payload(incident: Incident, *, zone: Zone | None = None, extra: dict | None = None) -> dict:
    """The socket payload for an incident change.

    Deliberately thin, and deliberately free of the reporter. The command centre
    needs to know an incident exists, where, how bad, and how long it has —
    it does not need, and must not be handed, who raised it.
    """
    return {
        "incident_id": str(incident.id),
        "reference": incident.reference,
        "type": incident.type,
        "severity": incident.severity,
        "status": incident.status,
        "zone_id": str(incident.zone_id) if incident.zone_id else None,
        "zone_code": zone.code if zone else None,
        "zone_name_mr": zone.name_mr if zone else None,
        "sla_due_at": incident.sla_due_at.isoformat(),
        "sla_breached": incident.sla_breached,
        "created_at": incident.created_at.isoformat(),
        **(extra or {}),
    }


async def publish(event_type: str, incident: Incident, *, session: AsyncSession, extra: dict | None = None) -> None:
    zone = await session.get(Zone, incident.zone_id) if incident.zone_id else None
    await events.publish(event_type, event_payload(incident, zone=zone, extra=extra))
