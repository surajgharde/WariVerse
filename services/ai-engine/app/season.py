"""Generating a training season from the simulation engine (Section 4/M6).

Section 4/M6's cold-start instruction is explicit: there is no history, so seed
the model with the simulation engine's generated season and label every forecast
that comes out of it as trained on simulated data.  This module is the first
half of that; `forecast_service.SIMULATED_NOTICE` in the core API is the second.

The season is generated at the pipeline's own ten-second cadence and folded into
the same one-minute buckets the live path uses, through the same `ZoneHistory`
type.  Generating it at one-minute steps instead would be about six times faster
and subtly wrong: `SimEngine` applies its inertia once per call, so a coarser
cadence produces a differently-smoothed series, and the model would learn lag
relationships that do not hold at inference time.  Matching the cadence is worth
the seconds it costs.

What this cannot do is make simulated data into real data.  The generated season
contains exactly the structure the simulation was written with — a diurnal
curve, an Ashadhi ramp, Fruin's speed-density relation — and a model trained on
it has learned those and nothing else.  It will not anticipate the thing nobody
modelled.  That is the honest reading of a cold start, and it is why the label
travels with every prediction.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from math import hypot

from app.features import Context, ZoneHistory, build_row
from app.logging import get_logger
from app.models import ZoneSpec
from app.sim_engine import SimEngine

logger = get_logger(__name__)

#: The pipeline's window. Matching it is the point — see the module docstring.
STEP_SECONDS = 10

#: Days either side of Ashadhi Ekadashi to generate. Seven days covers the ramp
#: (the simulation's curve runs from four days before to four after) plus
#: ordinary days for contrast. A model trained only on the peak learns that
#: every day is the peak.
DEFAULT_SPAN_DAYS = 7

#: Rows are emitted every this many minutes rather than every minute. Adjacent
#: minutes are almost the same sample, and thinning keeps the training set to a
#: size that trains in seconds without losing any shape.
SAMPLE_EVERY_MINUTES = 5


@dataclass(frozen=True, slots=True)
class TrainingSet:
    """Feature rows and their targets for one zone.

    `rows` and `targets` are parallel. Kept as plain lists rather than arrays so
    this module has no numpy dependency — the forecaster converts once, and the
    baseline path never needs to.
    """

    zone_id: str
    zone_code: str
    rows: list[list[float]]
    targets: list[float]

    def __len__(self) -> int:
        return len(self.rows)


def _steps(start: datetime, end: datetime) -> Iterator[datetime]:
    moment = start
    step = timedelta(seconds=STEP_SECONDS)
    while moment < end:
        yield moment
        moment += step


def generate(
    zones: list[ZoneSpec],
    *,
    ekadashi: date,
    horizons: tuple[int, ...],
    span_days: int = DEFAULT_SPAN_DAYS,
    seed: int = 20260724,
    baseline_multiplier: float = 1.0,
) -> dict[str, TrainingSet]:
    """Build a training set per zone from a generated season.

    The window is centred on Ekadashi minus one — the heaviest day in the
    simulation's curve is the day itself, and centring on the peak would leave
    the tail of the ramp outside the season.

    Targets are the *actual* density at `now + horizon` in the generated series,
    which is why the whole season is materialised before any row is emitted: a
    forecast's label lives in its future.
    """
    if not zones:
        return {}

    centre = ekadashi - timedelta(days=1)
    start = datetime.combine(centre - timedelta(days=span_days // 2), time(0, 0), tzinfo=UTC)
    end = start + timedelta(days=span_days)

    sim = SimEngine(zones, seed=seed, ekadashi=ekadashi, baseline_multiplier=baseline_multiplier)

    # Materialise the whole season as one-minute buckets first. Memory is
    # bounded and small: 7 days x 1440 minutes x 40 zones of four floats.
    series: dict[str, dict[datetime, tuple[float, float, float, float]]] = {z.zone_id: {} for z in zones}
    accumulator: dict[str, ZoneHistory] = {
        z.zone_id: ZoneHistory(zone_id=z.zone_id, zone_code=z.code) for z in zones
    }

    windows = 0
    for moment in _steps(start, end):
        for observation in sim.observe(moment, with_heat_cells=False):
            history = accumulator[observation.zone_id]
            history.observe(
                moment,
                density=observation.density,
                stagnation=observation.stagnation_index,
                counterflow=observation.counterflow_ratio,
                speed=hypot(observation.flow_dx, observation.flow_dy),
            )
            latest = history.latest
            if latest is not None:
                series[observation.zone_id][latest.minute] = (
                    latest.density,
                    latest.stagnation,
                    latest.counterflow,
                    latest.speed,
                )
        windows += 1

    logger.info(
        "season_generated",
        extra={
            "zones": len(zones),
            "windows": windows,
            "span_days": span_days,
            "from": start.isoformat(),
            "to": end.isoformat(),
        },
    )

    return _rows_from_series(zones, series, ekadashi=ekadashi, horizons=horizons)


def _rows_from_series(
    zones: list[ZoneSpec],
    series: dict[str, dict[datetime, tuple[float, float, float, float]]],
    *,
    ekadashi: date,
    horizons: tuple[int, ...],
) -> dict[str, TrainingSet]:
    """Replay the materialised season through `build_row`, one zone at a time.

    Replayed rather than captured during generation because the features include
    a six-hour look-back, and a history that is being filled forward does not
    have one until six hours in. Replaying gives every row the same feature
    construction the live path performs.
    """
    out: dict[str, TrainingSet] = {}

    for zone in zones:
        lookup = series[zone.zone_id]
        minutes = sorted(lookup)
        rows: list[list[float]] = []
        targets: list[float] = []

        replay = ZoneHistory(zone_id=zone.zone_id, zone_code=zone.code)

        for index, minute in enumerate(minutes):
            density, stagnation, counterflow, speed = lookup[minute]
            replay.observe(minute, density=density, stagnation=stagnation, counterflow=counterflow, speed=speed)

            if index % SAMPLE_EVERY_MINUTES:
                continue
            if not replay.is_warm():
                continue

            # The upstream proxy is the mean of every *other* zone this minute,
            # computed here rather than stored so it stays consistent with what
            # the live path computes from the same instant's observations.
            others = [
                other[minute][0]
                for other_id, other in series.items()
                if other_id != zone.zone_id and minute in other
            ]
            context = Context(
                ekadashi=ekadashi,
                # No pass bookings in a generated season: the simulation models
                # crowds, not the booking system. Left empty and *stated* — the
                # feature is present and constant, so the model correctly learns
                # nothing from it rather than learning a fiction.
                slots=(),
                upstream_proxy=sum(others) / len(others) if others else 0.0,
            )

            for horizon in horizons:
                target = lookup.get(minute + timedelta(minutes=horizon))
                if target is None:
                    continue
                rows.append(build_row(replay, minute, horizon, context))
                targets.append(target[0])

        out[zone.zone_id] = TrainingSet(zone_id=zone.zone_id, zone_code=zone.code, rows=rows, targets=targets)

    return out


__all__ = ["DEFAULT_SPAN_DAYS", "SAMPLE_EVERY_MINUTES", "STEP_SECONDS", "TrainingSet", "generate"]
