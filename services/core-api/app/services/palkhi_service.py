"""Palkhi and Dindi tracking (Section 4/M8, Phase 9).

The Wari is a 250 km, 18-day walking procession.  Everything else in this
product is about the last day of it; this module is about the other seventeen.

Four things happen here.

1. **Pings.** One designated phone per Dindi reports where the group is.  The
   interval is battery-aware and the *server* decides it, because a phone that
   is dead on day eleven of an eighteen-day walk reports nothing at all.
2. **Pace.** Position over time, projected onto the route line.  Deliberately
   not the GPS `speed` field: a walking group's phone reports the speed of the
   volunteer carrying it, which spikes when he jogs to catch up and reads zero
   whenever he stops to talk. Displacement over ninety minutes is the pace of
   the procession; instantaneous speed is the pace of one man.
3. **Deviation.** Pace against the Dindi's own halt schedule, producing an ETA
   for the next town and the gap between that and the plan.  Section 4/M8's
   45 minutes.
4. **Readiness.** What each halt town has provisioned against how many people
   its schedule says are walking towards it tonight.

A word on what this module refuses to do.  It never reports where a *person*
is.  A Dindi is a group of a few hundred walkers, the position is one volunteer
phone standing in for all of them, and nothing here should ever be refined into
individual tracking — that is what makes an 18-day location system something a
temple trust can deploy under the DPDP Act at all (Section 12, E9).

The arithmetic is deliberately kept in pure functions at the top of this file,
taking plain dataclasses.  The rules that act on it are safety-relevant enough
to deserve tests that do not need Postgres to be running.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from geoalchemy2 import Geography
from sqlalchemy import cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.core.logging import get_logger
from app.core.security import (
    decrypt_contact,
    encrypt_contact,
    hash_phone,
    normalise_phone,
    now_utc,
)
from app.models import (
    ContactSecret,
    Dindi,
    DindiPing,
    DindiScheduleStop,
    DindiStatus,
    HaltReadiness,
    HaltTown,
    Route,
)
from app.services import config_service

logger = get_logger(__name__)

#: Mean earth radius, kilometres.  Good to about 0.5% at Maharashtra latitudes,
#: which is far inside the error of estimating a walking group's pace.
EARTH_RADIUS_KM = 6371.0088

#: Below this, a "pace" is a group standing still — a meal, a kirtan, a night
#: halt.  Dividing a distance by it produces an ETA measured in days, so the
#: ETA is reported as unknown instead.
MIN_WALKING_KMPH = 0.4

#: A walking procession that appears to be moving faster than this is a phone
#: in a support vehicle, not a Dindi.  The reading is kept; the pace built from
#: it is marked unusable, because a truck's pace applied to a walking group
#: produces an ETA four hours early and a halt town told to be ready for it.
MAX_WALKING_KMPH = 9.0


# ---------------------------------------------------------------------------
# pure arithmetic — no database, deliberately testable on its own
# ---------------------------------------------------------------------------
def haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Great-circle distance in kilometres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


@dataclass(frozen=True, slots=True)
class PingSample:
    """One position, as the pace maths sees it."""

    at: datetime
    lon: float
    lat: float
    #: Position along the route, 0..1.  None when the route has no path.
    route_fraction: float | None = None


@dataclass(frozen=True, slots=True)
class PaceEstimate:
    """A walking pace, with everything needed to decide whether to trust it.

    `method` is part of the answer, not diagnostics.  A pace measured along the
    route line and a pace measured as the crow flies differ by the winding of
    the road — 10-20% on the Alandi route — and an operator deciding whether to
    move a town's water tankers is entitled to know which one produced the
    number they are looking at.
    """

    kmph: float
    samples: int
    span_minutes: float
    km_covered: float
    method: str  # route | straight | none
    observed_at: datetime | None = None

    @property
    def is_usable(self) -> bool:
        """Enough signal to turn into an ETA at all."""
        return (
            self.samples >= 2
            and self.span_minutes >= 10.0
            and MIN_WALKING_KMPH <= self.kmph <= MAX_WALKING_KMPH
        )


def estimate_pace(
    samples: list[PingSample],
    *,
    total_km: float | None = None,
) -> PaceEstimate:
    """Average pace across the window, from the first sample to the last.

    First-to-last rather than a mean of the leg-by-leg speeds, and the
    difference matters: a group that walked for an hour and then stopped for
    lunch has a first-to-last pace that correctly says "they have covered 3 km
    in two hours", where averaging the legs says "they walk at 3 km/h" and puts
    the ETA an hour early.  What the halt town needs is the first one.

    Projected onto the route line where there is one, because a road that bends
    around a hill covers more ground than the straight line across it.
    """
    if len(samples) < 2:
        observed = samples[-1].at if samples else None
        return PaceEstimate(0.0, len(samples), 0.0, 0.0, "none", observed)

    ordered = sorted(samples, key=lambda s: s.at)
    first, last = ordered[0], ordered[-1]
    span_minutes = (last.at - first.at).total_seconds() / 60.0
    if span_minutes <= 0:
        return PaceEstimate(0.0, len(ordered), 0.0, 0.0, "none", last.at)

    if total_km and first.route_fraction is not None and last.route_fraction is not None:
        # Signed on purpose. A negative value means the group has moved
        # backwards along the route, which is a real thing at a river crossing
        # and a data error everywhere else; either way `is_usable` rejects it
        # rather than reporting a confident pace in the wrong direction.
        km = (last.route_fraction - first.route_fraction) * total_km
        method = "route"
    else:
        km = haversine_km(first.lon, first.lat, last.lon, last.lat)
        method = "straight"

    kmph = km / (span_minutes / 60.0)
    return PaceEstimate(
        kmph=round(kmph, 3),
        samples=len(ordered),
        span_minutes=round(span_minutes, 1),
        km_covered=round(km, 3),
        method=method,
        observed_at=last.at,
    )


def eta_for(km_remaining: float, pace: PaceEstimate, *, at: datetime) -> datetime | None:
    """When a group walking at this pace reaches something this far away.

    None when the pace cannot carry the arithmetic.  Returning a number anyway —
    from a default 3 km/h, say — would be the most dangerous thing this file
    could do: a halt town cannot tell a guessed ETA from a measured one once it
    is rendered as a time, and it will staff the kitchen for both the same way.
    """
    if not pace.is_usable or km_remaining < 0:
        return None
    return at + timedelta(hours=km_remaining / pace.kmph)


def deviation_minutes(eta: datetime, planned_arrival: datetime) -> float:
    """Signed: positive is late, negative is early.  The sign is the point."""
    return (eta - planned_arrival).total_seconds() / 60.0


def next_ping_seconds(battery: int | None, base_interval: int, *, halted: bool = False) -> int:
    """How long the device should wait before reporting again.

    Section 4/M8 asks for a 60-second interval that is "battery-aware", and the
    decision belongs on the server rather than in the app: the phone knows its
    battery, but only the server knows that this Dindi is halted for the night
    and that there are six days of walking left to power.

    Backing off is not a degradation here — it is the feature.  A phone that
    reports every 60 seconds until it dies at 4 p.m. on day eleven tells the
    halt towns nothing for the last seven days; one that stretches to ten
    minutes at 15% battery is still reporting on day eighteen. A walking group
    covers about 500 m in ten minutes, which is inside the arrival radius
    anyway.
    """
    if halted:
        # A stationary group's position is not news. This is also the window in
        # which the phone is most likely to find a charger, so being quiet
        # through it is what keeps it alive for the morning.
        return max(base_interval * 10, 600)
    if battery is None:
        return base_interval
    if battery <= 10:
        return max(base_interval * 15, 900)
    if battery <= 25:
        return max(base_interval * 10, 600)
    if battery <= 50:
        return max(base_interval * 3, 180)
    return base_interval


# ---------------------------------------------------------------------------
# readiness arithmetic
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ProvisioningRatios:
    water_points_per_1000: float = 4.0
    sanitation_units_per_1000: float = 10.0
    medical_camps_per_10000: float = 1.0

    def required(self, headcount: int) -> tuple[int, int, int]:
        """Water points, sanitation units and medical camps for this many people.

        Rounded up throughout.  Half a water point serves nobody, and the
        direction to round a shortfall in is not a matter of taste.
        """
        if headcount <= 0:
            return (0, 0, 0)
        thousands = headcount / 1000.0
        return (
            math.ceil(thousands * self.water_points_per_1000),
            math.ceil(thousands * self.sanitation_units_per_1000),
            # Any town expecting anybody at all needs somewhere to take a
            # collapse. Rounding this one to zero for a 400-person halt is how
            # you end up with a medical emergency and a 40-minute ambulance.
            max(1, math.ceil((headcount / 10_000.0) * self.medical_camps_per_10000)),
        )


@dataclass(frozen=True, slots=True)
class ReadinessAssessment:
    """What a town has, against what its own schedule says is coming.

    Both figures are reported: `declared` is what a coordinator typed into the
    board, `computed` is what the numbers support.  When they disagree, that
    disagreement is the most useful thing on the screen — a town marked "ready"
    with water for half the walkers arriving is the specific failure Section
    4/M8 was written to catch, and collapsing the two into one status would
    hide exactly that case.
    """

    expected_headcount: int
    water_points: int
    water_points_required: int
    sanitation_units: int
    sanitation_units_required: int
    medical_camps: int
    medical_camps_required: int
    computed: HaltReadiness
    declared: HaltReadiness
    gaps: list[str]
    gaps_mr: list[str]

    @property
    def disagrees(self) -> bool:
        return self.declared == HaltReadiness.READY and self.computed != HaltReadiness.READY


def assess_readiness(
    *,
    expected_headcount: int,
    water_points: int,
    sanitation_units: int,
    medical_camps: int,
    declared: str,
    ratios: ProvisioningRatios,
) -> ReadinessAssessment:
    """Grade a halt town against the provisioning ratios.

    `unknown` rather than `ready` when nothing is expected: a town with no
    schedule against it has not been assessed, and "no shortfall found" is not
    the same claim as "ready".
    """
    need_water, need_sanitation, need_medical = ratios.required(expected_headcount)
    gaps: list[str] = []
    gaps_mr: list[str] = []

    if water_points < need_water:
        short = need_water - water_points
        gaps.append(f"{short} more water point(s) needed for {expected_headcount} walkers")
        gaps_mr.append(f"{expected_headcount} वारकऱ्यांसाठी आणखी {short} पाणी केंद्रे आवश्यक")
    if sanitation_units < need_sanitation:
        short = need_sanitation - sanitation_units
        gaps.append(f"{short} more sanitation unit(s) needed")
        gaps_mr.append(f"आणखी {short} स्वच्छतागृहे आवश्यक")
    if medical_camps < need_medical:
        short = need_medical - medical_camps
        gaps.append(f"{short} more medical camp(s) needed")
        gaps_mr.append(f"आणखी {short} वैद्यकीय शिबिरे आवश्यक")

    if expected_headcount <= 0:
        computed = HaltReadiness.UNKNOWN
    elif not gaps:
        computed = HaltReadiness.READY
    elif medical_camps < need_medical or water_points * 2 < need_water:
        # No medical cover, or less than half the water, is not "partially
        # ready" for a town that five hundred people are walking towards.
        computed = HaltReadiness.NOT_READY
    else:
        computed = HaltReadiness.PARTIAL

    try:
        declared_status = HaltReadiness(declared)
    except ValueError:
        declared_status = HaltReadiness.UNKNOWN

    return ReadinessAssessment(
        expected_headcount=expected_headcount,
        water_points=water_points,
        water_points_required=need_water,
        sanitation_units=sanitation_units,
        sanitation_units_required=need_sanitation,
        medical_camps=medical_camps,
        medical_camps_required=need_medical,
        computed=computed,
        declared=declared_status,
        gaps=gaps,
        gaps_mr=gaps_mr,
    )


# ---------------------------------------------------------------------------
# database access
# ---------------------------------------------------------------------------
def _point(lon: float, lat: float):
    """A bound POINT literal.  Parameterised rather than interpolated: these
    coordinates arrive from a volunteer's phone over the public internet."""
    return func.ST_SetSRID(func.ST_MakePoint(lon, lat), 4326)


async def load_dindi(session: AsyncSession, dindi_id: uuid.UUID) -> Dindi:
    dindi = await session.get(Dindi, dindi_id)
    if dindi is None:
        raise AppError("DINDI_NOT_FOUND", details={"dindi_id": str(dindi_id)})
    return dindi


async def load_dindi_by_code(session: AsyncSession, code: str) -> Dindi:
    dindi = await session.scalar(select(Dindi).where(Dindi.code == code.upper()))
    if dindi is None:
        raise AppError("DINDI_NOT_FOUND", details={"code": code})
    return dindi


async def load_halt_town(session: AsyncSession, town_id: uuid.UUID) -> HaltTown:
    town = await session.get(HaltTown, town_id)
    if town is None:
        raise AppError("HALT_TOWN_NOT_FOUND", details={"halt_town_id": str(town_id)})
    return town


async def ratios(session: AsyncSession) -> ProvisioningRatios:
    return ProvisioningRatios(
        water_points_per_1000=await config_service.get_float(session, "halt_water_points_per_1000"),
        sanitation_units_per_1000=await config_service.get_float(session, "halt_sanitation_units_per_1000"),
        medical_camps_per_10000=await config_service.get_float(session, "halt_medical_camps_per_10000"),
    )


async def locate_on_route(
    session: AsyncSession,
    route_id: uuid.UUID | None,
    lon: float,
    lat: float,
) -> tuple[float | None, float | None]:
    """Where a point sits along a route, and how far off the line it is.

    Returns `(fraction, metres_off_route)`, both None when the Dindi has no
    route or the route has no surveyed path.  That is a real and common state in
    the first season of a deployment — a Dindi can be registered and tracked
    before anybody has digitised its road — and the caller degrades to
    straight-line distances rather than refusing the ping.
    """
    if route_id is None:
        return (None, None)

    point = _point(lon, lat)
    row = (
        await session.execute(
            select(
                func.ST_LineLocatePoint(Route.path, point),
                func.ST_Distance(cast(Route.path, Geography), cast(point, Geography)),
            ).where(Route.id == route_id, Route.path.isnot(None))
        )
    ).first()
    if row is None:
        return (None, None)
    fraction, distance_m = row
    return (
        float(fraction) if fraction is not None else None,
        float(distance_m) if distance_m is not None else None,
    )


async def town_route_fraction(session: AsyncSession, route_id: uuid.UUID | None, town: HaltTown) -> float | None:
    """Where a halt town sits along the route, 0..1."""
    if route_id is None or town.centroid is None:
        return None
    fraction = await session.scalar(
        select(func.ST_LineLocatePoint(Route.path, HaltTown.centroid))
        .select_from(Route)
        .join(HaltTown, HaltTown.id == town.id)
        .where(Route.id == route_id, Route.path.isnot(None))
    )
    return float(fraction) if fraction is not None else None


@dataclass(frozen=True, slots=True)
class PingIn:
    lon: float
    lat: float
    battery: int | None = None
    speed_kmph: float | None = None
    accuracy_m: float | None = None
    at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PingResult:
    dindi: Dindi
    recorded_at: datetime
    route_fraction: float | None
    off_route_m: float | None
    #: The town the group has just been detected inside, if this ping is the
    #: one that crossed the boundary.  None on every other ping.
    arrived_at: HaltTown | None
    departed_from: HaltTown | None
    #: What the device should be told to do next (Section 4/M8, battery-aware).
    next_ping_seconds: int
    status: str


async def record_ping(
    session: AsyncSession,
    dindi: Dindi,
    ping: PingIn,
    *,
    device_id: str | None = None,
) -> PingResult:
    """Store one position report and update the Dindi's derived state.

    The device id is checked rather than trusted.  Section 4/M8 specifies one
    designated device per Dindi, and enforcing it is what stops a second
    volunteer who installed the app from making the Palkhi appear to be in two
    places at once — which, on a board that halt towns plan against, is worse
    than no position at all.  The first ping from a Dindi with no registered
    device claims it.
    """
    if not (-180.0 <= ping.lon <= 180.0 and -90.0 <= ping.lat <= 90.0):
        raise AppError("PING_INVALID", details={"reason": "coordinates out of range"})

    if device_id:
        if dindi.tracking_device_id is None:
            dindi.tracking_device_id = device_id
        elif dindi.tracking_device_id != device_id:
            raise AppError("DINDI_DEVICE_MISMATCH", details={"dindi_code": dindi.code})

    moment = ping.at or now_utc()
    # A phone that has been offline for hours flushes its queue on reconnect,
    # and those pings arrive with old timestamps in whatever order the queue
    # held them. They are stored — the history is worth having — but they must
    # not overwrite a newer known position with an older one.
    is_newest = dindi.last_ping_at is None or moment >= dindi.last_ping_at

    fraction, off_route = await locate_on_route(session, dindi.route_id, ping.lon, ping.lat)

    session.add(
        DindiPing(
            time=moment,
            dindi_id=dindi.id,
            location=f"SRID=4326;POINT({ping.lon} {ping.lat})",
            battery=ping.battery,
            speed_kmph=ping.speed_kmph,
            accuracy_m=ping.accuracy_m,
            route_fraction=fraction,
            off_route_m=off_route,
        )
    )

    arrived_at: HaltTown | None = None
    departed_from: HaltTown | None = None

    if is_newest:
        dindi.last_ping_at = moment
        dindi.last_battery = ping.battery
        dindi.route_fraction = fraction
        if dindi.started_at is None:
            dindi.started_at = moment

        town = await _town_containing(session, dindi, ping.lon, ping.lat)
        previous_id = dindi.current_halt_town_id

        if town is not None and town.id != previous_id:
            arrived_at = town
            dindi.current_halt_town_id = town.id
            dindi.status = str(DindiStatus.HALTED)
            await _mark_arrival(session, dindi, town, moment)
        elif town is None and previous_id is not None:
            departed_from = await session.get(HaltTown, previous_id)
            dindi.current_halt_town_id = None
            dindi.status = str(DindiStatus.WALKING)
            if departed_from is not None:
                await _mark_departure(session, dindi, departed_from, moment)
        elif town is None:
            dindi.status = str(DindiStatus.WALKING)

    base_interval = await config_service.get_int(session, "dindi_ping_interval_seconds")
    await session.flush()

    return PingResult(
        dindi=dindi,
        recorded_at=moment,
        route_fraction=fraction,
        off_route_m=off_route,
        arrived_at=arrived_at,
        departed_from=departed_from,
        next_ping_seconds=next_ping_seconds(
            ping.battery, base_interval, halted=dindi.status == str(DindiStatus.HALTED)
        ),
        status=dindi.status,
    )


async def _town_containing(
    session: AsyncSession, dindi: Dindi, lon: float, lat: float
) -> HaltTown | None:
    """The halt town this position is inside, if any.

    A town with a surveyed polygon is tested against the polygon; one with only
    a centre point falls back to a radius.  Both are on this Dindi's schedule
    only — a Dindi walking the Dehu road past a town that belongs to the Alandi
    route has not arrived anywhere.
    """
    radius_m = await config_service.get_int(session, "dindi_halt_arrival_radius_m")
    point = _point(lon, lat)

    row = (
        await session.execute(
            select(HaltTown)
            .join(DindiScheduleStop, DindiScheduleStop.halt_town_id == HaltTown.id)
            .where(
                DindiScheduleStop.dindi_id == dindi.id,
                func.coalesce(
                    func.ST_Contains(HaltTown.geom, point),
                    func.ST_DWithin(cast(HaltTown.centroid, Geography), cast(point, Geography), radius_m),
                ).is_(True),
            )
            .order_by(DindiScheduleStop.sequence)
            .limit(1)
        )
    ).first()
    return row[0] if row else None


async def _mark_arrival(session: AsyncSession, dindi: Dindi, town: HaltTown, at: datetime) -> None:
    stop = await session.scalar(
        select(DindiScheduleStop).where(
            DindiScheduleStop.dindi_id == dindi.id,
            DindiScheduleStop.halt_town_id == town.id,
        )
    )
    # First arrival only. A group that steps out of the boundary for firewood
    # and back has not arrived twice, and overwriting the first timestamp would
    # erase the one number next year's schedule is built from.
    if stop is not None and stop.actual_arrival is None:
        stop.actual_arrival = at
        logger.info(
            "dindi_arrived",
            extra={
                "dindi": dindi.code,
                "halt_town": town.name,
                "planned": stop.planned_arrival.isoformat(),
                "deviation_minutes": round(deviation_minutes(at, stop.planned_arrival), 1),
            },
        )


async def _mark_departure(session: AsyncSession, dindi: Dindi, town: HaltTown, at: datetime) -> None:
    stop = await session.scalar(
        select(DindiScheduleStop).where(
            DindiScheduleStop.dindi_id == dindi.id,
            DindiScheduleStop.halt_town_id == town.id,
        )
    )
    if stop is not None and stop.actual_arrival is not None:
        stop.actual_departure = at
        logger.info("dindi_departed", extra={"dindi": dindi.code, "halt_town": town.name})


async def recent_samples(
    session: AsyncSession,
    dindi_id: uuid.UUID,
    *,
    window_minutes: int,
    at: datetime | None = None,
) -> list[PingSample]:
    """Positions inside the pace window, oldest first."""
    moment = at or now_utc()
    cutoff = moment - timedelta(minutes=window_minutes)
    rows = await session.execute(
        select(
            DindiPing.time,
            func.ST_X(DindiPing.location),
            func.ST_Y(DindiPing.location),
            DindiPing.route_fraction,
        )
        .where(DindiPing.dindi_id == dindi_id, DindiPing.time >= cutoff, DindiPing.time <= moment)
        .order_by(DindiPing.time)
    )
    return [
        PingSample(at=time, lon=float(lon), lat=float(lat), route_fraction=fraction)
        for time, lon, lat, fraction in rows
    ]


@dataclass(frozen=True, slots=True)
class NextStop:
    stop: DindiScheduleStop
    town: HaltTown
    km_remaining: float
    #: How the distance was measured, for the same reason `PaceEstimate.method`
    #: exists — a crow-flies remaining distance under-states a winding road.
    distance_method: str


async def next_stop(
    session: AsyncSession,
    dindi: Dindi,
    *,
    total_km: float | None,
    last: PingSample | None,
) -> NextStop | None:
    """The next halt town this Dindi has not yet reached, and how far it is.

    None once the schedule is exhausted — a Dindi that has walked into
    Pandharpur has no next town, and there is nothing left to be late for.
    """
    row = (
        await session.execute(
            select(DindiScheduleStop, HaltTown)
            .join(HaltTown, HaltTown.id == DindiScheduleStop.halt_town_id)
            .where(
                DindiScheduleStop.dindi_id == dindi.id,
                DindiScheduleStop.actual_arrival.is_(None),
            )
            .order_by(DindiScheduleStop.sequence)
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    stop, town = row

    if total_km and dindi.route_fraction is not None:
        town_fraction = await town_route_fraction(session, dindi.route_id, town)
        if town_fraction is not None:
            return NextStop(
                stop=stop,
                town=town,
                km_remaining=max(0.0, (town_fraction - dindi.route_fraction) * total_km),
                distance_method="route",
            )

    if last is not None and town.centroid is not None:
        centre = (
            await session.execute(
                select(func.ST_X(HaltTown.centroid), func.ST_Y(HaltTown.centroid)).where(
                    HaltTown.id == town.id
                )
            )
        ).first()
        if centre is not None:
            return NextStop(
                stop=stop,
                town=town,
                km_remaining=haversine_km(last.lon, last.lat, float(centre[0]), float(centre[1])),
                distance_method="straight",
            )

    # Known to be next, distance unknown. Reported rather than dropped: a town
    # that knows it is next but not when is better served than one that hears
    # nothing at all.
    return NextStop(stop=stop, town=town, km_remaining=-1.0, distance_method="none")


@dataclass(frozen=True, slots=True)
class DindiProgress:
    """Everything the board and the deviation sweep need about one Dindi."""

    dindi: Dindi
    route: Route | None
    last: PingSample | None
    pace: PaceEstimate
    next: NextStop | None
    eta: datetime | None
    deviation_minutes: float | None
    seconds_since_ping: float | None
    is_signal_lost: bool
    off_route_m: float | None
    km_walked: float | None

    @property
    def has_eta(self) -> bool:
        return self.eta is not None and self.next is not None


async def progress(
    session: AsyncSession,
    dindi: Dindi,
    *,
    at: datetime | None = None,
    window_minutes: int | None = None,
    signal_lost_minutes: int | None = None,
) -> DindiProgress:
    """Assemble one Dindi's current picture: pace, next town, ETA, deviation."""
    moment = at or now_utc()
    window = window_minutes or await config_service.get_int(session, "dindi_pace_window_minutes")
    lost_after = signal_lost_minutes or await config_service.get_int(session, "dindi_signal_lost_minutes")

    route = await session.get(Route, dindi.route_id) if dindi.route_id else None
    total_km = route.total_km if route else None

    samples = await recent_samples(session, dindi.id, window_minutes=window, at=moment)
    last = samples[-1] if samples else None
    pace = estimate_pace(samples, total_km=total_km)

    seconds_since = (moment - dindi.last_ping_at).total_seconds() if dindi.last_ping_at else None
    is_lost = seconds_since is not None and seconds_since > lost_after * 60

    upcoming = await next_stop(session, dindi, total_km=total_km, last=last)

    eta: datetime | None = None
    gap: float | None = None
    if upcoming is not None and upcoming.km_remaining >= 0:
        # A signal-lost Dindi gets no ETA. Its last position is a historical
        # fact and projecting a walking pace forward from it produces a time
        # that looks exactly like a measured one on a halt town's screen.
        eta = None if is_lost else eta_for(upcoming.km_remaining, pace, at=moment)
        if eta is not None:
            gap = deviation_minutes(eta, upcoming.stop.planned_arrival)

    off_route = None
    if last is not None and route is not None and route.path is not None:
        off_route = (
            await session.scalar(
                select(DindiPing.off_route_m)
                .where(DindiPing.dindi_id == dindi.id, DindiPing.time == last.at)
                .limit(1)
            )
        )

    km_walked = None
    if total_km and dindi.route_fraction is not None:
        km_walked = round(dindi.route_fraction * total_km, 2)

    return DindiProgress(
        dindi=dindi,
        route=route,
        last=last,
        pace=pace,
        next=upcoming,
        eta=eta,
        deviation_minutes=gap,
        seconds_since_ping=seconds_since,
        is_signal_lost=is_lost,
        off_route_m=off_route,
        km_walked=km_walked,
    )


async def active_dindis(session: AsyncSession) -> list[Dindi]:
    """Every Dindi the sweep should look at.

    Registered-but-not-started and withdrawn Dindis are excluded: a group that
    has not left Alandi is not behind schedule, and an alert saying so on the
    day before the walk begins is the kind of noise that gets a feed muted.
    """
    rows = await session.execute(
        select(Dindi)
        .where(
            Dindi.is_active.is_(True),
            Dindi.status.in_([str(DindiStatus.WALKING), str(DindiStatus.HALTED), str(DindiStatus.SIGNAL_LOST)]),
        )
        .order_by(Dindi.code)
    )
    return list(rows.scalars())


# ---------------------------------------------------------------------------
# registration and schedule
# ---------------------------------------------------------------------------
#: How long a Dindi leader's real phone number is kept.  A Wari season plus a
#: month of reconciliation — long enough for the halt towns to call and for the
#: post-event review, and not one day of the other eleven months (Section 12).
LEADER_CONTACT_TTL_DAYS = 210


async def register(
    session: AsyncSession,
    *,
    code: str,
    name: str,
    name_mr: str,
    leader_name: str,
    leader_phone: str,
    expected_count: int,
    route_id: uuid.UUID | None = None,
    tracking_device_id: str | None = None,
    at: datetime | None = None,
) -> Dindi:
    """Enrol a Dindi.

    The leader's number is hashed onto the row and encrypted into
    `contact_secrets` with a season TTL — Section 12's PII rule, applied here
    the same way Phase 2 applied it to pass holders and Phase 5 to incident
    callbacks. The Dindi entity itself never holds a number in the clear, so a
    dump of the tracking tables is not a contact list of four hundred Dindi
    leaders across Maharashtra.
    """
    moment = at or now_utc()
    normalised = code.strip().upper()

    if await session.scalar(select(Dindi.id).where(Dindi.code == normalised)):
        raise AppError("DINDI_CODE_TAKEN", details={"code": normalised})

    if route_id is not None and not await session.scalar(select(Route.id).where(Route.id == route_id)):
        raise AppError("ROUTE_NOT_FOUND", details={"route_id": str(route_id)})

    phone_hash = await store_leader_contact(session, leader_phone, at=moment)

    dindi = Dindi(
        code=normalised,
        name=name,
        name_mr=name_mr,
        leader_name=leader_name,
        leader_phone_hash=phone_hash,
        expected_count=expected_count,
        route_id=route_id,
        tracking_device_id=tracking_device_id,
        status=str(DindiStatus.REGISTERED),
        is_active=True,
    )
    session.add(dindi)
    await session.flush()
    logger.info("dindi_registered", extra={"dindi": dindi.code, "expected_count": expected_count})
    return dindi


async def store_leader_contact(session: AsyncSession, phone: str, *, at: datetime) -> str:
    """Hash for the row, encrypt for the phone call, keep neither longer than needed."""
    normalised = normalise_phone(phone)
    phone_hash = hash_phone(normalised)
    session.add(
        ContactSecret(
            phone_hash=phone_hash,
            encrypted_phone=encrypt_contact(normalised),
            purpose="dindi_leader",
            purge_after=at + timedelta(days=LEADER_CONTACT_TTL_DAYS),
        )
    )
    return phone_hash


async def leader_phone(session: AsyncSession, dindi: Dindi) -> str | None:
    """The leader's real number, decrypted.

    Callers must audit the read.  R-M8-01 and R-M8-05 both end in "call the
    Dindi leader", so this has to exist; every route that reaches it logs who
    looked and when, the same treatment breach clips get.
    """
    token = await session.scalar(
        select(ContactSecret.encrypted_phone)
        .where(
            ContactSecret.phone_hash == dindi.leader_phone_hash,
            ContactSecret.purpose == "dindi_leader",
        )
        .order_by(ContactSecret.created_at.desc())
        .limit(1)
    )
    if token is None:
        return None
    try:
        return decrypt_contact(token)
    except Exception:
        # A number that will not decrypt is a rotated key, not a crash. The
        # caller renders "not available, use the paper roster" rather than a 500
        # on the one screen somebody is using at 2 a.m.
        logger.warning("dindi_leader_contact_undecryptable", extra={"dindi": dindi.code})
        return None


async def set_schedule(
    session: AsyncSession,
    dindi: Dindi,
    stops: list[tuple[uuid.UUID, datetime, datetime | None, int | None]],
) -> list[DindiScheduleStop]:
    """Replace a Dindi's halt schedule with this sequence.

    Validated as a whole rather than stop by stop, because the invariants are
    about the sequence: each town appears once, and the arrivals move forward.
    A schedule with day nine before day eight would make `next_stop` pick the
    wrong town and quietly send every subsequent deviation alert to it.

    Arrivals already recorded survive the replacement. An administrator fixing
    tomorrow's times must not erase the record of when the group actually
    reached Saswad last night — that gap is what next year's schedule is built
    from.
    """
    if not stops:
        raise AppError("SCHEDULE_INVALID", details={"reason": "a schedule needs at least one halt"})

    town_ids = [town_id for town_id, _, _, _ in stops]
    if len(set(town_ids)) != len(town_ids):
        raise AppError("SCHEDULE_INVALID", details={"reason": "a halt town appears more than once"})

    known = set(
        (await session.execute(select(HaltTown.id).where(HaltTown.id.in_(town_ids)))).scalars()
    )
    missing = [str(t) for t in town_ids if t not in known]
    if missing:
        raise AppError("HALT_TOWN_NOT_FOUND", details={"halt_town_ids": missing})

    previous: datetime | None = None
    for _, arrival, departure, _ in stops:
        if previous is not None and arrival <= previous:
            raise AppError(
                "SCHEDULE_INVALID",
                details={"reason": "arrival times must move forward in walking order"},
            )
        if departure is not None and departure < arrival:
            raise AppError("SCHEDULE_INVALID", details={"reason": "a halt cannot end before it starts"})
        previous = arrival

    existing = {
        stop.halt_town_id: stop
        for stop in (
            await session.execute(
                select(DindiScheduleStop).where(DindiScheduleStop.dindi_id == dindi.id)
            )
        ).scalars()
    }

    # Sequences are renumbered from the list order, and a stop can move
    # position. Clearing them first avoids tripping the (dindi_id, sequence)
    # unique constraint halfway through the rewrite.
    for stop in existing.values():
        stop.sequence = -(stop.sequence + 1)
    await session.flush()

    result: list[DindiScheduleStop] = []
    index = 0
    for index, (town_id, arrival, departure, count) in enumerate(stops, start=1):
        stop = existing.pop(town_id, None)
        if stop is None:
            stop = DindiScheduleStop(dindi_id=dindi.id, halt_town_id=town_id, sequence=index)
            session.add(stop)
        stop.sequence = index
        stop.planned_arrival = arrival
        stop.planned_departure = departure
        stop.expected_count = count
        result.append(stop)

    # Towns dropped from the schedule go — except the ones the group has already
    # walked into. Those are history rather than plan, and they keep their
    # recorded arrival at the end of the sequence: `next_stop` skips anything
    # with an `actual_arrival`, so they sit out of the way while remaining the
    # record next year's schedule is built from.
    for orphan in existing.values():
        if orphan.actual_arrival is None:
            await session.delete(orphan)
        else:
            index += 1
            orphan.sequence = index

    await session.flush()
    logger.info("dindi_schedule_set", extra={"dindi": dindi.code, "stops": len(result)})
    return result


async def schedule_for(session: AsyncSession, dindi_id: uuid.UUID) -> list[tuple[DindiScheduleStop, HaltTown]]:
    rows = await session.execute(
        select(DindiScheduleStop, HaltTown)
        .join(HaltTown, HaltTown.id == DindiScheduleStop.halt_town_id)
        .where(DindiScheduleStop.dindi_id == dindi_id)
        .order_by(DindiScheduleStop.sequence)
    )
    # `.tuples()` rather than iterating the Result directly: it is the typed
    # accessor, and a plain `list(result)` over a two-entity select yields Rows
    # that behave like tuples until something asks them for their type.
    return list(rows.tuples().all())


async def update_readiness(
    session: AsyncSession,
    town: HaltTown,
    *,
    actor_id: uuid.UUID | None,
    water_points: int | None = None,
    sanitation_units: int | None = None,
    medical_camps: int | None = None,
    readiness_status: str | None = None,
    readiness_note: str | None = None,
    expected_arrival: datetime | None = None,
    at: datetime | None = None,
) -> HaltTown:
    """Record what a town actually has standing.

    Every update stamps who and when.  A board showing "8 water points" with no
    indication of whether that was counted this morning or typed in last March
    reads as reassurance rather than as information, and a district
    administration will provision against it either way.
    """
    moment = at or now_utc()
    if water_points is not None:
        town.water_points = water_points
    if sanitation_units is not None:
        town.sanitation_units = sanitation_units
    if medical_camps is not None:
        town.medical_camps = medical_camps
    if readiness_note is not None:
        town.readiness_note = readiness_note
    if expected_arrival is not None:
        town.expected_arrival = expected_arrival
    if readiness_status is not None:
        try:
            town.readiness_status = str(HaltReadiness(readiness_status))
        except ValueError as exc:
            raise AppError(
                "BAD_REQUEST",
                details={"reason": "unknown readiness status", "allowed": [str(s) for s in HaltReadiness]},
            ) from exc

    town.readiness_updated_at = moment
    town.readiness_updated_by = actor_id
    town.updated_at = moment
    await session.flush()
    return town


# ---------------------------------------------------------------------------
# halt-town readiness board
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ArrivingDindi:
    dindi_id: uuid.UUID
    code: str
    name: str
    name_mr: str
    expected_count: int
    planned_arrival: datetime
    eta: datetime | None
    deviation_minutes: float | None
    is_signal_lost: bool


@dataclass(frozen=True, slots=True)
class HaltTownBoard:
    town: HaltTown
    arriving: list[ArrivingDindi]
    assessment: ReadinessAssessment
    ratios: ProvisioningRatios
    #: Earliest ETA among the groups walking towards it, which is the number a
    #: coordinator actually plans against — not the earliest *planned* arrival.
    first_arrival_expected: datetime | None


async def readiness_board(
    session: AsyncSession,
    *,
    route_id: uuid.UUID | None = None,
    within_hours: int = 36,
    at: datetime | None = None,
) -> list[HaltTownBoard]:
    """The halt-town readiness board of Section 4/M8.

    Head count comes from the *schedule*, summed across every Dindi due in the
    window, rather than from the `expected_headcount` column somebody typed in
    during planning. Both exist and they drift apart the moment a Dindi
    withdraws or a schedule shifts; the one that is right tonight is the one
    derived from who is actually walking towards the town.

    Towns with nobody due are still returned. A coordinator scrolling the route
    needs to see the quiet towns as quiet, and a town that vanishes from the
    board looks identical to a town that was never entered.
    """
    moment = at or now_utc()
    horizon = moment + timedelta(hours=within_hours)
    provisioning = await ratios(session)

    towns_stmt = select(HaltTown).order_by(HaltTown.sequence, HaltTown.name)
    if route_id is not None:
        towns_stmt = towns_stmt.where(HaltTown.route_id == route_id)
    towns = list((await session.execute(towns_stmt)).scalars())
    if not towns:
        return []

    stops = await session.execute(
        select(DindiScheduleStop, Dindi)
        .join(Dindi, Dindi.id == DindiScheduleStop.dindi_id)
        .where(
            DindiScheduleStop.halt_town_id.in_([t.id for t in towns]),
            DindiScheduleStop.actual_departure.is_(None),
            DindiScheduleStop.planned_arrival <= horizon,
            Dindi.is_active.is_(True),
            Dindi.status != str(DindiStatus.WITHDRAWN),
        )
        .order_by(DindiScheduleStop.planned_arrival)
    )

    by_town: dict[uuid.UUID, list[tuple[DindiScheduleStop, Dindi]]] = {}
    for stop, dindi in stops:
        by_town.setdefault(stop.halt_town_id, []).append((stop, dindi))

    # One progress computation per Dindi, not one per town it appears on.
    progress_cache: dict[uuid.UUID, DindiProgress] = {}

    boards: list[HaltTownBoard] = []
    for town in towns:
        arriving: list[ArrivingDindi] = []
        headcount = 0
        for stop, dindi in by_town.get(town.id, []):
            if dindi.id not in progress_cache:
                progress_cache[dindi.id] = await progress(session, dindi, at=moment)
            state = progress_cache[dindi.id]
            count = stop.expected_count or dindi.expected_count
            headcount += count

            # An ETA and a deviation belong to this town only when it is the one
            # the Dindi is actually walking towards. Showing a Saswad ETA
            # against Lonand three stops later would be a projection dressed up
            # as a schedule.
            is_next = state.next is not None and state.next.town.id == town.id
            arriving.append(
                ArrivingDindi(
                    dindi_id=dindi.id,
                    code=dindi.code,
                    name=dindi.name,
                    name_mr=dindi.name_mr,
                    expected_count=count,
                    planned_arrival=stop.planned_arrival,
                    eta=state.eta if is_next else None,
                    deviation_minutes=state.deviation_minutes if is_next else None,
                    is_signal_lost=state.is_signal_lost,
                )
            )

        assessment = assess_readiness(
            expected_headcount=headcount,
            water_points=town.water_points,
            sanitation_units=town.sanitation_units,
            medical_camps=town.medical_camps,
            declared=town.readiness_status,
            ratios=provisioning,
        )
        etas = [a.eta for a in arriving if a.eta is not None]
        planned = [a.planned_arrival for a in arriving]
        boards.append(
            HaltTownBoard(
                town=town,
                arriving=arriving,
                assessment=assessment,
                ratios=provisioning,
                first_arrival_expected=min(etas) if etas else (min(planned) if planned else None),
            )
        )
    return boards
