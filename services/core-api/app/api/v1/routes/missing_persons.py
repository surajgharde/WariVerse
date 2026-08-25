"""Missing persons (Section 5, E2 — Phase 5).

    POST  /missing-persons        GET /missing-persons
    GET   /missing-persons/{id}   PATCH /missing-persons/{id}

E2 calls this "the highest-frequency real incident at Wari", and the PDR does
not cover it. It is a separate route module rather than a shape of incident
because the data is different in kind: a case carries a child's name, age, photo
and a parent's phone number, and every one of those needs a rule that a
crowd-density incident does not.

The rules, in the order they bite:

* **Reporting is open to a pilgrim.** A parent whose child is missing is not
  going to find a volunteer first. `incident:report` is what a pilgrim has.
* **Reading is not.** Listing cases needs `incident:view`. A public register of
  missing children is not a thing this system builds, and "who is currently
  separated from their family" is exactly the list an opportunist would want.
* **The photo URI is not a list field.** `has_photo` says whether one exists;
  the URI itself comes back only on the single-case read.
* **The purge clock starts at closure, not at report.** A case still open on day
  31 has not stopped needing the photo.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import events
from app.core.db import get_session
from app.core.deps import Actor, require
from app.core.errors import AppError
from app.core.permissions import Permission
from app.core.security import now_utc
from app.models import Incident, MissingPerson, Zone
from app.schemas.common import ErrorResponse, Page
from app.schemas.incidents import MissingPersonCreate, MissingPersonOut, MissingPersonUpdate
from app.services import incident_service

router = APIRouter(prefix="/missing-persons", tags=["missing-persons"], responses={404: {"model": ErrorResponse}})

#: Statuses where somebody is still looking.
OPEN_STATUSES = ("open", "sighted")


def _out(
    record: MissingPerson,
    *,
    incident_reference: str | None = None,
    zone_code: str | None = None,
) -> MissingPersonOut:
    end = record.resolved_at or now_utc()
    return MissingPersonOut(
        id=record.id,
        incident_id=record.incident_id,
        incident_reference=incident_reference,
        name=record.name,
        age=record.age,
        description=record.description,
        has_photo=bool(record.photo_uri),
        last_seen_zone_id=record.last_seen_zone_id,
        last_seen_zone_code=zone_code,
        last_seen_at=record.last_seen_at,
        language=record.language,
        status=record.status,
        reported_at=record.reported_at,
        resolved_at=record.resolved_at,
        purge_after=record.purge_after,
        open_for_seconds=round((end - record.reported_at).total_seconds(), 1),
    )


@router.post("", response_model=MissingPersonOut, status_code=201)
async def report_missing_person(
    payload: MissingPersonCreate,
    actor: Actor = Depends(require(Permission.INCIDENT_REPORT)),
    session: AsyncSession = Depends(get_session),
) -> MissingPersonOut:
    """Open a case, and the incident that puts it on the control room's board.

    Both are created together. A missing child that lives only in its own list
    is a missing child nobody has been assigned to — the incident is what gives
    it an SLA, a dispatchable responder and a place in the queue an operator is
    actually working.

    Severity is `high` (a 10-minute SLA), not `critical`. Grading every case
    critical would put a lost umbrella's owner ahead of a cardiac arrest, and
    that is the one call this system must never get wrong.
    """
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
        actor_role=actor.user.role,
        ip=actor.ip,
        user_agent=actor.user_agent,
    )

    zone = await session.get(Zone, record.last_seen_zone_id) if record.last_seen_zone_id else None
    out = _out(record, incident_reference=incident.reference, zone_code=zone.code if zone else None)
    await session.commit()

    # Broadcast so volunteers in surrounding zones and the announcement desks
    # see it. The payload carries the case id and the last-seen zone — not the
    # name, and not the photo. Anyone who may act on it can fetch the detail
    # through an authenticated read that is permission-checked; a name fanned
    # out over a socket is a name on every screen in the building.
    await events.publish(
        events.INCIDENT_RAISED,
        {
            **incident_service.event_payload(incident, zone=zone),
            "missing_person_id": str(record.id),
            "last_seen_zone_code": zone.code if zone else None,
        },
    )
    return out


@router.get("", response_model=Page[MissingPersonOut])
async def list_missing_persons(
    status: str | None = Query(default=None, description="open | sighted | reunited | closed_unresolved"),
    open_only: bool = Query(default=True),
    zone_id: uuid.UUID | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _: Actor = Depends(require(Permission.INCIDENT_VIEW)),
    session: AsyncSession = Depends(get_session),
) -> Page[MissingPersonOut]:
    """The working list for an announcement desk.

    Ordered oldest-first: the person who has been missing longest is the one to
    announce next. Every other list in this product is worst-first by severity,
    and this one is not — with cases that are all the same severity, elapsed
    time is the only thing that distinguishes them.
    """
    stmt = select(MissingPerson)
    if status:
        stmt = stmt.where(MissingPerson.status == status)
    elif open_only:
        stmt = stmt.where(MissingPerson.status.in_(OPEN_STATUSES))
    if zone_id:
        stmt = stmt.where(MissingPerson.last_seen_zone_id == zone_id)

    total = await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = await session.execute(
        stmt.order_by(MissingPerson.reported_at.asc()).limit(limit).offset(offset)
    )
    records = list(rows.scalars())

    zone_codes: dict[uuid.UUID, str] = {}
    zone_ids = {r.last_seen_zone_id for r in records if r.last_seen_zone_id}
    if zone_ids:
        zone_rows = await session.execute(select(Zone.id, Zone.code).where(Zone.id.in_(zone_ids)))
        zone_codes = dict(zone_rows.all())  # type: ignore[arg-type]

    refs: dict[uuid.UUID, str] = {}
    incident_ids = {r.incident_id for r in records if r.incident_id}
    if incident_ids:
        ref_rows = await session.execute(
            select(Incident.id, Incident.reference).where(Incident.id.in_(incident_ids))
        )
        refs = dict(ref_rows.all())  # type: ignore[arg-type]

    return Page[MissingPersonOut](
        items=[
            _out(
                r,
                incident_reference=refs.get(r.incident_id) if r.incident_id else None,
                zone_code=zone_codes.get(r.last_seen_zone_id) if r.last_seen_zone_id else None,
            )
            for r in records
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{case_id}", response_model=MissingPersonOut)
async def get_missing_person(
    case_id: uuid.UUID,
    _: Actor = Depends(require(Permission.INCIDENT_VIEW)),
    session: AsyncSession = Depends(get_session),
) -> MissingPersonOut:
    record = await incident_service.load_missing_person(session, case_id)
    zone = await session.get(Zone, record.last_seen_zone_id) if record.last_seen_zone_id else None
    incident = await session.get(Incident, record.incident_id) if record.incident_id else None
    return _out(
        record,
        incident_reference=incident.reference if incident else None,
        zone_code=zone.code if zone else None,
    )


@router.get("/{case_id}/photo", response_model=dict[str, str])
async def get_missing_person_photo(
    case_id: uuid.UUID,
    actor: Actor = Depends(require(Permission.INCIDENT_UPDATE_ANY)),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    """The photo reference, for a role that can act on it.

    Behind `incident:update_any` rather than `incident:view`: a volunteer who can
    see the case list does not need every child's photograph, and the narrower
    the set of people holding that URI, the smaller the thing that leaks.

    Returns 404 once the photo has been purged, with the purge date — so the
    caller learns the retention worked rather than that something is broken.
    """
    record = await incident_service.load_missing_person(session, case_id)

    if not record.photo_uri:
        raise AppError(
            "NOT_FOUND",
            details={
                "reason": "no photo, or it has been purged under the 30-day retention rule",
                "purge_after": record.purge_after.isoformat() if record.purge_after else None,
            },
        )

    await incident_service.audit_photo_view(
        session,
        record,
        actor_id=actor.id,
        actor_role=actor.user.role,
        ip=actor.ip,
        user_agent=actor.user_agent,
    )
    await session.commit()
    return {"photo_uri": record.photo_uri}


@router.patch("/{case_id}", response_model=MissingPersonOut)
async def update_missing_person(
    case_id: uuid.UUID,
    payload: MissingPersonUpdate,
    actor: Actor = Depends(require(Permission.INCIDENT_UPDATE_ANY)),
    session: AsyncSession = Depends(get_session),
) -> MissingPersonOut:
    """Record a sighting, a reunification, or a case closed unresolved.

    Reunification also resolves the incident, with the outcome note a review
    will read. A case that ends while its incident stays open leaves a row on
    the board for something that is finished — and the board is what an operator
    trusts to tell them what still needs doing.
    """
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

    zone = await session.get(Zone, record.last_seen_zone_id) if record.last_seen_zone_id else None
    incident = await session.get(Incident, record.incident_id) if record.incident_id else None
    out = _out(
        record,
        incident_reference=incident.reference if incident else None,
        zone_code=zone.code if zone else None,
    )
    await session.commit()

    if incident is not None:
        await incident_service.publish(
            events.INCIDENT_UPDATED,
            incident,
            session=session,
            extra={"missing_person_id": str(record.id), "case_status": record.status},
        )
    return out
