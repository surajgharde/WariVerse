"""Palkhi and Dindi tracking (Section 9 `PALKHI`, Section 4/M8, Phase 9).

    GET    /dindis                 POST /dindis
    GET    /dindis/{id}            PATCH /dindis/{id}
    PUT    /dindis/{id}/schedule   POST  /dindis/{id}/ping
    GET    /dindis/{id}/leader-contact        (audited)
    GET    /halt-towns             PATCH /halt-towns/{id}/readiness

The shape of this module follows one asymmetry.  Reading where the Dindis are
is broad — a pilgrim may watch the Palkhi approach, and the position of a
procession that ten thousand people are walking in is not a secret.  Writing is
narrow, and it is narrow in two different directions: registering a Dindi is an
administrator's job because it writes a leader's phone number into the system,
while confirming that Saswad has eight water tankers standing is a *volunteer's*
job, because that person is in Saswad counting them and an administrator in
Pandharpur is not.

The one genuinely sensitive thing here is the leader's phone number.  It has its
own endpoint, its own permission, and an audit row per read — the treatment
breach evidence gets, for the same reason.  Two of the M8 alert rules end in
"call the Dindi leader", so it cannot simply be withheld; what it can be is
never read without a record of who read it.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import events
from app.core.db import get_session
from app.core.deps import Actor, require, require_any
from app.core.errors import AppError
from app.core.permissions import Permission
from app.core.security import now_utc
from app.models import Dindi, DindiPing, DindiStatus, HaltTown, Route
from app.schemas.common import ErrorResponse
from app.schemas.palkhi import (
    ArrivingOut,
    DindiCreate,
    DindiDetail,
    DindiList,
    DindiOut,
    DindiUpdate,
    HaltTownBoardOut,
    HaltTownOut,
    LeaderContact,
    PaceOut,
    PingAck,
    PingIn,
    ReadinessOut,
    ReadinessUpdate,
    ScheduleIn,
    ScheduleStopOut,
)
from app.services import audit_service, config_service, palkhi_service
from app.services.audit_service import AuditAction

router = APIRouter(tags=["palkhi"], responses={404: {"model": ErrorResponse}})


_BOARD_NOTICE = (
    "Head counts are summed from the halt schedule, not from the planning column. "
    "An arrival time is an estimate from the group's recent walking pace; a group whose "
    "phone has gone quiet shows no arrival time at all rather than an old one."
)
_BOARD_NOTICE_MR = (
    "वारकऱ्यांची संख्या मुक्कामाच्या वेळापत्रकातून मोजली आहे, नियोजनाच्या रकान्यातून नाही. "
    "पोहोचण्याची वेळ ही गटाच्या सध्याच्या चालण्याच्या वेगावरून केलेला अंदाज आहे; ज्या गटाचा "
    "फोन बंद आहे त्याची जुनी वेळ दाखवण्याऐवजी काहीच वेळ दाखवली जात नाही."
)

_LIST_SILENT_NOTICE = (
    "Some Dindis are not reporting. Their last known position is history, not a current "
    "location — treat their arrival times as unknown."
)
_LIST_SILENT_NOTICE_MR = (
    "काही दिंड्यांकडून नोंदी येत नाहीत. त्यांची शेवटची जागा जुनी माहिती आहे, सद्यस्थिती नाही — "
    "त्यांच्या पोहोचण्याच्या वेळा 'माहिती नाही' समजा."
)


# ---------------------------------------------------------------------------
# assembling a Dindi view
# ---------------------------------------------------------------------------
async def _position(session: AsyncSession, dindi: Dindi) -> tuple[float, float] | None:
    if dindi.last_ping_at is None:
        return None
    row = (
        await session.execute(
            select(func.ST_X(DindiPing.location), func.ST_Y(DindiPing.location))
            .where(DindiPing.dindi_id == dindi.id)
            .order_by(DindiPing.time.desc())
            .limit(1)
        )
    ).first()
    return (float(row[0]), float(row[1])) if row else None


async def _view(
    session: AsyncSession,
    dindi: Dindi,
    *,
    route_names: dict[uuid.UUID, str],
    deviation_threshold: float,
    at: datetime,
) -> tuple[DindiOut, palkhi_service.DindiProgress]:
    state = await palkhi_service.progress(session, dindi, at=at)
    position = await _position(session, dindi)
    upcoming = state.next

    return (
        DindiOut(
            id=dindi.id,
            code=dindi.code,
            name=dindi.name,
            name_mr=dindi.name_mr,
            leader_name=dindi.leader_name,
            expected_count=dindi.expected_count,
            route_id=dindi.route_id,
            route_name=route_names.get(dindi.route_id) if dindi.route_id else None,
            status=str(DindiStatus.SIGNAL_LOST) if state.is_signal_lost else dindi.status,
            is_active=dindi.is_active,
            position=position,
            last_ping_at=dindi.last_ping_at,
            seconds_since_ping=(
                round(state.seconds_since_ping) if state.seconds_since_ping is not None else None
            ),
            battery=dindi.last_battery,
            is_signal_lost=state.is_signal_lost,
            off_route_m=round(state.off_route_m, 1) if state.off_route_m is not None else None,
            km_walked=state.km_walked,
            route_fraction=dindi.route_fraction,
            pace=PaceOut(
                kmph=state.pace.kmph,
                samples=state.pace.samples,
                span_minutes=state.pace.span_minutes,
                km_covered=state.pace.km_covered,
                method=state.pace.method,
                is_usable=state.pace.is_usable,
            ),
            next_town=upcoming.town.name if upcoming else None,
            next_town_mr=upcoming.town.name_mr if upcoming else None,
            next_town_id=upcoming.town.id if upcoming else None,
            km_remaining=(
                round(upcoming.km_remaining, 2) if upcoming and upcoming.km_remaining >= 0 else None
            ),
            planned_arrival=upcoming.stop.planned_arrival if upcoming else None,
            eta=state.eta,
            deviation_minutes=(
                round(state.deviation_minutes, 1) if state.deviation_minutes is not None else None
            ),
            is_deviating=(
                state.deviation_minutes is not None
                and abs(state.deviation_minutes) > deviation_threshold
            ),
        ),
        state,
    )


async def _route_names(session: AsyncSession) -> dict[uuid.UUID, str]:
    rows = await session.execute(select(Route.id, Route.name))
    # `.all()` first: a Result is an iterator, not a sequence, and dict() of one
    # raises rather than consuming it.
    return dict(rows.all())


async def _detail(session: AsyncSession, dindi: Dindi, *, at: datetime) -> DindiDetail:
    """One Dindi with its schedule.  Shared by the read, register, update and
    schedule routes so all four return the same shape."""
    threshold = await config_service.get_float(session, "dindi_deviation_minutes")
    view, _state = await _view(
        session, dindi, route_names=await _route_names(session), deviation_threshold=threshold, at=at
    )
    schedule = [
        ScheduleStopOut(
            halt_town_id=town.id,
            halt_town=town.name,
            halt_town_mr=town.name_mr,
            sequence=stop.sequence,
            planned_arrival=stop.planned_arrival,
            planned_departure=stop.planned_departure,
            actual_arrival=stop.actual_arrival,
            actual_departure=stop.actual_departure,
            expected_count=stop.expected_count or dindi.expected_count,
            # Set only once the group has actually walked in. This column, across
            # eighteen days and forty Dindis, is what next year's schedule gets
            # built from — it is the whole after-the-fact value of tracking.
            arrival_deviation_minutes=(
                round(palkhi_service.deviation_minutes(stop.actual_arrival, stop.planned_arrival), 1)
                if stop.actual_arrival is not None
                else None
            ),
        )
        for stop, town in await palkhi_service.schedule_for(session, dindi.id)
    ]
    return DindiDetail(
        **view.model_dump(),
        schedule=schedule,
        tracking_device_registered=dindi.tracking_device_id is not None,
    )


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------
@router.get("/dindis", response_model=DindiList)
async def list_dindis(
    route_id: uuid.UUID | None = Query(default=None),
    active_only: bool = Query(default=True),
    _: Actor = Depends(require(Permission.DINDI_VIEW)),
    session: AsyncSession = Depends(get_session),
) -> DindiList:
    """Every Dindi and where it is.

    The `reporting` / `silent` counts are not decoration. Fourteen dots on a map
    tells an operator nothing on its own; "fourteen reporting, six silent" tells
    them how much of the route they can actually see, which is the difference
    between a map and a map they should trust.
    """
    moment = now_utc()
    threshold = await config_service.get_float(session, "dindi_deviation_minutes")
    names = await _route_names(session)

    stmt = select(Dindi).order_by(Dindi.code)
    if route_id is not None:
        stmt = stmt.where(Dindi.route_id == route_id)
    if active_only:
        stmt = stmt.where(Dindi.is_active.is_(True))

    items: list[DindiOut] = []
    for dindi in (await session.execute(stmt)).scalars():
        view, _state = await _view(
            session, dindi, route_names=names, deviation_threshold=threshold, at=moment
        )
        items.append(view)

    # Counted over the Dindis that are supposed to be walking. A group still
    # registered in Alandi is not "silent" — it has not started, and folding it
    # into the coverage figure would make the map look blinder than it is.
    underway = [i for i in items if i.status != str(DindiStatus.REGISTERED)]
    silent = sum(1 for i in underway if i.is_signal_lost or i.last_ping_at is None)

    return DindiList(
        items=items,
        generated_at=moment,
        reporting=len(underway) - silent,
        silent=silent,
        notice=_LIST_SILENT_NOTICE if silent else None,
        notice_mr=_LIST_SILENT_NOTICE_MR if silent else None,
    )


@router.get("/dindis/{dindi_id}", response_model=DindiDetail)
async def get_dindi(
    dindi_id: uuid.UUID,
    _: Actor = Depends(require(Permission.DINDI_VIEW)),
    session: AsyncSession = Depends(get_session),
) -> DindiDetail:
    """One Dindi with its full halt schedule.

    The leader's number is not here in any form. Getting it is a separate,
    audited call — see `leader_contact` below.
    """
    dindi = await palkhi_service.load_dindi(session, dindi_id)
    return await _detail(session, dindi, at=now_utc())


@router.get("/dindis/{dindi_id}/leader-contact", response_model=LeaderContact)
async def leader_contact(
    dindi_id: uuid.UUID,
    actor: Actor = Depends(require_any(Permission.DINDI_MANAGE, Permission.INCIDENT_DISPATCH)),
    session: AsyncSession = Depends(get_session),
) -> LeaderContact:
    """The Dindi leader's real phone number.  Every read is written to the audit log.

    Two of the M8 rules end in "call the Dindi leader", so withholding this
    would make the recommended action impossible to follow. What it can be is
    never read without a record: the permission is the one held by people who
    dispatch — security officers and administrators — and a pilgrim with
    `dindi:view` never reaches here.
    """
    dindi = await palkhi_service.load_dindi(session, dindi_id)
    number = await palkhi_service.leader_phone(session, dindi)

    await audit_service.record(
        session,
        action=AuditAction.DINDI_UPDATED,
        actor_id=actor.id,
        actor_role=actor.role,
        target_type="dindi",
        target_id=dindi.id,
        meta={"read": "leader_contact", "dindi_code": dindi.code, "found": number is not None},
        ip=actor.ip,
        user_agent=actor.user_agent,
    )
    await session.commit()

    if number is None:
        raise AppError(
            "NOT_FOUND",
            message="No stored contact for this Dindi leader. Use the paper roster.",
            message_mr="या दिंडीप्रमुखांचा नोंदवलेला क्रमांक नाही. कागदी यादी वापरा.",
        )

    return LeaderContact(
        dindi_id=dindi.id,
        code=dindi.code,
        leader_name=dindi.leader_name,
        leader_phone=number,
        notice="This access has been logged.",
        notice_mr="ही पाहणी नोंदवली गेली आहे.",
    )


# ---------------------------------------------------------------------------
# position reporting
# ---------------------------------------------------------------------------
@router.post("/dindis/{dindi_id}/ping", response_model=PingAck)
async def report_position(
    dindi_id: uuid.UUID,
    body: PingIn,
    device_id: str | None = Query(default=None, alias="device", max_length=80),
    _: Actor = Depends(require(Permission.DINDI_PING)),
    session: AsyncSession = Depends(get_session),
) -> PingAck:
    """One position from the Dindi's designated phone.

    The response is not just an acknowledgement. It carries `next_ping_seconds`,
    which is the server telling the phone how long to sleep — Section 4/M8's
    "battery-aware" requirement, decided here rather than in the app because
    only the server knows the group is halted for the night with six more days
    of walking to power. It also carries one line the volunteer can read in
    daylight, because the person holding this phone has walked 20 km today and
    is owed something more useful than a 200.
    """
    dindi = await palkhi_service.load_dindi(session, dindi_id)
    if not dindi.is_active:
        raise AppError("DINDI_INACTIVE", details={"code": dindi.code})

    result = await palkhi_service.record_ping(
        session,
        dindi,
        palkhi_service.PingIn(
            lon=body.lon,
            lat=body.lat,
            battery=body.battery,
            speed_kmph=body.speed_kmph,
            accuracy_m=body.accuracy_m,
            at=body.at,
        ),
        device_id=device_id,
    )

    state = await palkhi_service.progress(session, dindi, at=result.recorded_at)
    await session.commit()

    # Only the boundary crossings go on the socket. Forty Dindis pinging every
    # 60 seconds would be a message a second on a channel a command centre keeps
    # open all day, to move a dot fifty metres.
    if result.arrived_at is not None:
        await events.publish(
            events.DINDI_ARRIVED,
            {
                "dindi_id": str(dindi.id),
                "code": dindi.code,
                "name_mr": dindi.name_mr,
                "halt_town_id": str(result.arrived_at.id),
                "halt_town": result.arrived_at.name,
                "halt_town_mr": result.arrived_at.name_mr,
                "expected_count": dindi.expected_count,
                "at": result.recorded_at,
            },
        )
    elif result.departed_from is not None:
        await events.publish(
            events.DINDI_DEPARTED,
            {
                "dindi_id": str(dindi.id),
                "code": dindi.code,
                "name_mr": dindi.name_mr,
                "halt_town_id": str(result.departed_from.id),
                "halt_town": result.departed_from.name,
                "at": result.recorded_at,
            },
        )

    summary, summary_mr = _ping_summary(result, state)
    return PingAck(
        recorded_at=result.recorded_at,
        status=result.status,
        next_ping_seconds=result.next_ping_seconds,
        route_fraction=result.route_fraction,
        off_route_m=round(result.off_route_m, 1) if result.off_route_m is not None else None,
        arrived_at=result.arrived_at.name if result.arrived_at else None,
        departed_from=result.departed_from.name if result.departed_from else None,
        summary=summary,
        summary_mr=summary_mr,
    )


def _ping_summary(
    result: palkhi_service.PingResult, state: palkhi_service.DindiProgress
) -> tuple[str, str]:
    """One honest line for the volunteer holding the phone.

    "Position received" when there is nothing to say. Never a made-up arrival
    time: this text goes to the person actually walking, who is in the best
    position of anyone to notice it is wrong, and getting caught inventing it
    here would cost the whole board its credibility on the road.
    """
    if result.arrived_at is not None:
        return (
            f"Arrival at {result.arrived_at.name} recorded.",
            f"{result.arrived_at.name_mr} येथे पोहोचल्याची नोंद झाली.",
        )
    if state.next is not None and state.eta is not None and state.next.km_remaining >= 0:
        town = state.next.town
        return (
            f"{state.next.km_remaining:.0f} km to {town.name}, expected around "
            f"{state.eta:%H:%M} at the current pace.",
            f"{town.name_mr} पर्यंत {state.next.km_remaining:.0f} किमी, सध्याच्या वेगाने "
            f"सुमारे {state.eta:%H:%M} वाजता पोहोचाल.",
        )
    if state.next is not None:
        return (
            f"Position received. Next halt: {state.next.town.name}.",
            f"नोंद मिळाली. पुढील मुक्काम: {state.next.town.name_mr}.",
        )
    return ("Position received.", "नोंद मिळाली.")


# ---------------------------------------------------------------------------
# registration and schedule
# ---------------------------------------------------------------------------
@router.post("/dindis", response_model=DindiDetail, status_code=201)
async def register_dindi(
    body: DindiCreate,
    actor: Actor = Depends(require(Permission.DINDI_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> DindiDetail:
    """Enrol a Dindi with its leader, head count and route.

    Audited because it writes a phone number into the system. The number itself
    never reaches the audit row — `audit_service` scrubs anything key-shaped
    like a phone, which is exactly the case this is.
    """
    dindi = await palkhi_service.register(
        session,
        code=body.code,
        name=body.name,
        name_mr=body.name_mr,
        leader_name=body.leader_name,
        leader_phone=body.leader_phone,
        expected_count=body.expected_count,
        route_id=body.route_id,
        tracking_device_id=body.tracking_device_id,
    )
    await audit_service.record(
        session,
        action=AuditAction.DINDI_REGISTERED,
        actor_id=actor.id,
        actor_role=actor.role,
        target_type="dindi",
        target_id=dindi.id,
        meta={"code": dindi.code, "expected_count": dindi.expected_count, "leader": dindi.leader_name},
        ip=actor.ip,
        user_agent=actor.user_agent,
    )
    await session.commit()
    return await _detail(session, dindi, at=now_utc())


@router.patch("/dindis/{dindi_id}", response_model=DindiDetail)
async def update_dindi(
    dindi_id: uuid.UUID,
    body: DindiUpdate,
    actor: Actor = Depends(require(Permission.DINDI_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> DindiDetail:
    """Amend a registration.

    Reassigning the tracking device is called out separately in the audit meta.
    Everything else here is bookkeeping; that one is the change that decides
    which phone the halt towns' board believes, and a review asking "why did
    this Dindi jump 40 km at 3 a.m." should find it in one query.
    """
    dindi = await palkhi_service.load_dindi(session, dindi_id)
    changed: dict[str, object] = {}

    if body.leader_phone is not None:
        dindi.leader_phone_hash = await palkhi_service.store_leader_contact(
            session, body.leader_phone, at=now_utc()
        )
        changed["leader_phone"] = "updated"

    device_reassigned = (
        body.tracking_device_id is not None and body.tracking_device_id != dindi.tracking_device_id
    )
    if device_reassigned:
        changed["previous_device"] = dindi.tracking_device_id

    for field in ("name", "name_mr", "leader_name", "expected_count", "route_id", "is_active"):
        value = getattr(body, field)
        if value is not None and value != getattr(dindi, field):
            changed[field] = value
            setattr(dindi, field, value)

    if body.tracking_device_id is not None:
        dindi.tracking_device_id = body.tracking_device_id
    if body.status is not None:
        try:
            dindi.status = str(DindiStatus(body.status))
            changed["status"] = dindi.status
        except ValueError as exc:
            raise AppError(
                "BAD_REQUEST",
                details={"reason": "unknown status", "allowed": [str(s) for s in DindiStatus]},
            ) from exc

    await audit_service.record(
        session,
        action=(
            AuditAction.DINDI_DEVICE_REASSIGNED if device_reassigned else AuditAction.DINDI_UPDATED
        ),
        actor_id=actor.id,
        actor_role=actor.role,
        target_type="dindi",
        target_id=dindi.id,
        meta={"code": dindi.code, "changed": changed},
        ip=actor.ip,
        user_agent=actor.user_agent,
    )
    await session.commit()
    return await _detail(session, dindi, at=now_utc())


@router.put("/dindis/{dindi_id}/schedule", response_model=DindiDetail)
async def set_schedule(
    dindi_id: uuid.UUID,
    body: ScheduleIn,
    actor: Actor = Depends(require(Permission.DINDI_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> DindiDetail:
    """Replace the planned halt schedule.

    A PUT rather than a series of PATCHes because the invariants are about the
    sequence — each town once, arrivals moving forward — and those cannot be
    checked one stop at a time. The order of the list is the walking order.
    """
    dindi = await palkhi_service.load_dindi(session, dindi_id)
    stops = await palkhi_service.set_schedule(
        session,
        dindi,
        [(s.halt_town_id, s.planned_arrival, s.planned_departure, s.expected_count) for s in body.stops],
    )
    await audit_service.record(
        session,
        action=AuditAction.DINDI_SCHEDULE_SET,
        actor_id=actor.id,
        actor_role=actor.role,
        target_type="dindi",
        target_id=dindi.id,
        meta={
            "code": dindi.code,
            "stops": len(stops),
            "first_arrival": stops[0].planned_arrival.isoformat() if stops else None,
            "last_arrival": stops[-1].planned_arrival.isoformat() if stops else None,
        },
        ip=actor.ip,
        user_agent=actor.user_agent,
    )
    await session.commit()
    return await _detail(session, dindi, at=now_utc())


# ---------------------------------------------------------------------------
# halt towns
# ---------------------------------------------------------------------------
async def _board_views(
    session: AsyncSession,
    *,
    route_id: uuid.UUID | None,
    within_hours: int,
    at: datetime,
) -> list[HaltTownOut]:
    """Build the readiness rows.  Shared by the board and the readiness PATCH,
    so a town looks identical whether it was just edited or merely listed."""
    boards = await palkhi_service.readiness_board(
        session, route_id=route_id, within_hours=within_hours, at=at
    )
    names = await _route_names(session)

    towns: list[HaltTownOut] = []
    for board in boards:
        town = board.town
        centre = None
        if town.centroid is not None:
            row = (
                await session.execute(
                    select(func.ST_X(HaltTown.centroid), func.ST_Y(HaltTown.centroid)).where(
                        HaltTown.id == town.id
                    )
                )
            ).first()
            if row is not None:
                centre = (float(row[0]), float(row[1]))

        a = board.assessment
        r = board.ratios
        towns.append(
            HaltTownOut(
                id=town.id,
                name=town.name,
                name_mr=town.name_mr,
                route_id=town.route_id,
                route_name=names.get(town.route_id) if town.route_id else None,
                sequence=town.sequence,
                centroid=centre,
                readiness=ReadinessOut(
                    expected_headcount=a.expected_headcount,
                    water_points=a.water_points,
                    water_points_required=a.water_points_required,
                    sanitation_units=a.sanitation_units,
                    sanitation_units_required=a.sanitation_units_required,
                    medical_camps=a.medical_camps,
                    medical_camps_required=a.medical_camps_required,
                    computed=str(a.computed),
                    declared=str(a.declared),
                    disagrees=a.disagrees,
                    gaps=a.gaps,
                    gaps_mr=a.gaps_mr,
                    basis=(
                        f"{r.water_points_per_1000:g} water points and "
                        f"{r.sanitation_units_per_1000:g} sanitation units per 1000 walkers, "
                        f"{r.medical_camps_per_10000:g} medical camp(s) per 10000"
                    ),
                    basis_mr=(
                        f"दर १००० वारकऱ्यांमागे {r.water_points_per_1000:g} पाणी केंद्रे आणि "
                        f"{r.sanitation_units_per_1000:g} स्वच्छतागृहे, दर १०००० मागे "
                        f"{r.medical_camps_per_10000:g} वैद्यकीय शिबिर"
                    ),
                ),
                readiness_note=town.readiness_note,
                readiness_updated_at=town.readiness_updated_at,
                first_arrival_expected=board.first_arrival_expected,
                arriving=[
                    ArrivingOut(
                        dindi_id=item.dindi_id,
                        code=item.code,
                        name=item.name,
                        name_mr=item.name_mr,
                        expected_count=item.expected_count,
                        planned_arrival=item.planned_arrival,
                        eta=item.eta,
                        deviation_minutes=(
                            round(item.deviation_minutes, 1)
                            if item.deviation_minutes is not None
                            else None
                        ),
                        is_signal_lost=item.is_signal_lost,
                    )
                    for item in board.arriving
                ],
            )
        )
    return towns


@router.get("/halt-towns", response_model=HaltTownBoardOut)
async def halt_town_board(
    route_id: uuid.UUID | None = Query(default=None),
    within_hours: int = Query(default=36, ge=1, le=480),
    _: Actor = Depends(require(Permission.DINDI_VIEW)),
    session: AsyncSession = Depends(get_session),
) -> HaltTownBoardOut:
    """The halt-town readiness board (Section 4/M8).

    `readiness.declared` is what a coordinator typed in; `readiness.computed` is
    what the provisioning supports against the head count its own schedule says
    is walking towards it. When they disagree, `disagrees` is true, and that
    flag is the most useful thing on the screen — a town marked ready with water
    for half the arrivals is precisely the failure this module exists to catch,
    and one merged status would hide it.
    """
    moment = now_utc()
    return HaltTownBoardOut(
        towns=await _board_views(
            session, route_id=route_id, within_hours=within_hours, at=moment
        ),
        generated_at=moment,
        within_hours=within_hours,
        notice=_BOARD_NOTICE,
        notice_mr=_BOARD_NOTICE_MR,
    )


@router.patch("/halt-towns/{town_id}/readiness", response_model=HaltTownOut)
async def update_readiness(
    town_id: uuid.UUID,
    body: ReadinessUpdate,
    actor: Actor = Depends(require(Permission.HALT_READINESS_UPDATE)),
    session: AsyncSession = Depends(get_session),
) -> HaltTownOut:
    """Record what a halt town actually has standing.

    Open to volunteers, not just administrators, and that is deliberate: the
    person who can say how many water tankers are in Saswad is standing in
    Saswad. Routing this through an administrator in Pandharpur is how the board
    goes stale, and a stale readiness board is worse than none — it is a
    district administration provisioning against a number nobody has checked
    since March. Every update stamps who said it and when.
    """
    town = await palkhi_service.load_halt_town(session, town_id)
    before = {
        "water_points": town.water_points,
        "sanitation_units": town.sanitation_units,
        "medical_camps": town.medical_camps,
        "readiness_status": town.readiness_status,
    }

    await palkhi_service.update_readiness(
        session,
        town,
        actor_id=actor.id,
        water_points=body.water_points,
        sanitation_units=body.sanitation_units,
        medical_camps=body.medical_camps,
        readiness_status=body.readiness_status,
        readiness_note=body.readiness_note,
        expected_arrival=body.expected_arrival,
    )
    await audit_service.record(
        session,
        action=AuditAction.HALT_READINESS_UPDATED,
        actor_id=actor.id,
        actor_role=actor.role,
        target_type="halt_town",
        target_id=town.id,
        meta={
            "town": town.name,
            "before": before,
            "after": {
                "water_points": town.water_points,
                "sanitation_units": town.sanitation_units,
                "medical_camps": town.medical_camps,
                "readiness_status": town.readiness_status,
            },
            "note": body.readiness_note,
        },
        ip=actor.ip,
        user_agent=actor.user_agent,
    )
    await session.commit()

    await events.publish(
        events.HALT_TOWN_UPDATED,
        {
            "halt_town_id": str(town.id),
            "name": town.name,
            "name_mr": town.name_mr,
            "readiness_status": town.readiness_status,
            "water_points": town.water_points,
            "sanitation_units": town.sanitation_units,
            "medical_camps": town.medical_camps,
            "updated_at": town.readiness_updated_at,
        },
    )

    rows = await _board_views(session, route_id=town.route_id, within_hours=36, at=now_utc())
    for entry in rows:
        if entry.id == town.id:
            return entry
    raise AppError("HALT_TOWN_NOT_FOUND", details={"halt_town_id": str(town_id)})
