"""Simulation engine — REQUIRED by Section 4/M2, and the thing you demo.

There is no temple CCTV on a laptop, and a crowd-safety system that can only be
developed with hardware present is a system nobody develops.  So the simulation
is not a stub: it produces telemetry the rest of the pipeline cannot distinguish
from a real feed, and `CROWD_SOURCE=live|video|sim` is the only switch.

What it models, and why each part is there:

* **Diurnal curve.** Darshan opens at 04:00 and the pre-dawn queue is the day's
  first peak, not a ramp from zero. Midday empties in the heat; evening fills
  again. A flat baseline would make every alert look like a step change.
* **Ashadhi Ekadashi.** The reason this system exists. Three days either side
  ramp up to roughly five times an ordinary day.
* **Palkhi arrivals.** A Dindi arriving is not a smooth increase — it is two
  thousand people entering one zone over about eight minutes, and it is the
  event the command centre has to be good at.
* **Crowd physics.** Walking speed falls as density rises (Fruin): free flow
  near 1.3 m/s, jammed near zero around 5 p/m². Stagnation is *derived* from
  that speed rather than dialled independently, so a stalled zone always reads
  as stalled — you cannot get a fast-moving 5.5 p/m² out of this model, because
  you cannot get one out of a real corridor either.
* **Inertia.** Crowds do not teleport. Each zone's occupancy is smoothed toward
  its target, so the density chart has the shape of a crowd and not of noise.

Everything is seeded, so a demo run is reproducible and a test can assert on it.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from math import cos, exp, pi

from app.metrics import vector_from
from app.models import ZoneObservation, ZoneSpec

# --- crowd physics ---------------------------------------------------------
#: Free-flow walking speed, m/s.  Pilgrims in a procession, not commuters.
FREE_FLOW_SPEED_MS = 1.25
#: Density at which movement effectively stops.  This is the same 5.0 p/m² that
#: `DensityLevel.CRITICAL` starts at, which is not a coincidence: the band is
#: drawn there because that is where a crowd jams.
JAM_DENSITY = 5.0

#: The physical ceiling.  Standing adults cannot pack tighter than roughly seven
#: per square metre; past that people are lifted off their feet, which is the
#: crush itself and not a density the sensor would ever report.
MAX_PHYSICAL_DENSITY = 6.5
#: Below this the model is linear — demand and density are the same thing when
#: there is room.  Above it, extra demand backs up into the upstream zone
#: instead of compressing further, which is what actually happens to a queue.
SATURATION_KNEE = 3.0


def saturate_density(raw: float) -> float:
    """Convert demand into achievable density.

    Without this, an Ekadashi multiplier of five produces a zone holding three
    and a half times its capacity at 8 p/m² — a number no crowd and no camera
    has ever produced.  A simulation that generates impossible readings trains
    operators to distrust real ones.
    """
    if raw <= SATURATION_KNEE:
        return raw
    headroom = MAX_PHYSICAL_DENSITY - SATURATION_KNEE
    return SATURATION_KNEE + headroom * (1.0 - exp(-(raw - SATURATION_KNEE) / headroom))


#: Diurnal control points: (hour, share of the zone's typical peak).
#: 04:00 opens with the queue already formed — people sleep at the ghat.
_DIURNAL: tuple[tuple[float, float], ...] = (
    (0.0, 0.06), (3.0, 0.10), (4.0, 0.55), (5.5, 0.92), (7.0, 0.78),
    (9.0, 0.60), (11.0, 0.48), (14.0, 0.38), (16.0, 0.55), (18.5, 0.88),
    (20.0, 0.80), (22.0, 0.35), (24.0, 0.06),
)

#: How each kind of zone behaves.  (occupancy scale, bearing, two-way traffic)
#: The bearing is the direction the crowd predominantly walks, as a compass
#: heading — the temple core is the sink everything flows toward.
_ZONE_CHARACTER: dict[str, tuple[float, float, float]] = {
    "temple_core": (0.95, 0.0, 0.10),     # saturates; exit stream crosses entry
    "queue": (0.85, 20.0, 0.03),          # dense, slow, one-way by construction
    "corridor": (0.70, 200.0, 0.28),      # entry and exit share it
    "ghat": (0.45, 90.0, 0.35),           # people wander; no dominant direction
    "approach_road": (0.55, 160.0, 0.12), # flow-dominated, rarely dense
    "halt_town": (0.40, 180.0, 0.20),
    "facility": (0.30, 270.0, 0.30),      # rest zones churn in both directions
}
_DEFAULT_CHARACTER = (0.50, 0.0, 0.20)

#: Palkhi arrivals: a Dindi entering a zone over roughly eight minutes.
PALKHI_RAMP_SECONDS = 480

#: How fast occupancy moves toward its target per 10-second window.  0.18 gives
#: a zone about a minute to respond, which is what a corridor actually takes.
_INERTIA = 0.18


@dataclass(slots=True)
class Injection:
    """An operator- or demo-triggered event.

    Section 16's demo script is one call to `inject("palkhi_surge", "NW")` at
    T+1:30 — the surge then propagates through the same code path a real one
    would, alerts and all.
    """

    kind: str  # palkhi_surge | crowd_surge | stall | counterflow | clear
    zone_code: str | None
    magnitude: float
    starts_at: datetime
    ends_at: datetime
    note: str | None = None

    def weight(self, at: datetime) -> float:
        """0 outside the window, ramping to `magnitude` and back inside it."""
        if at < self.starts_at or at > self.ends_at:
            return 0.0
        span = (self.ends_at - self.starts_at).total_seconds()
        if span <= 0:
            return self.magnitude
        progress = (at - self.starts_at).total_seconds() / span
        if self.kind == "palkhi_surge":
            # Arrival ramps in fast and decays slowly: the Dindi arrives, then
            # disperses into the corridor over the following half hour.
            return self.magnitude * (1.0 - exp(-6.0 * progress)) * exp(-1.2 * progress)
        # Everything else: a raised cosine, so nothing steps discontinuously.
        return self.magnitude * (0.5 - 0.5 * cos(2.0 * pi * progress))


@dataclass(slots=True)
class _ZoneState:
    occupancy: float = 0.0
    counterflow: float = 0.0
    initialised: bool = False


def diurnal_factor(at: datetime) -> float:
    """Share of the day's peak at this moment, linearly between control points."""
    hour = at.hour + at.minute / 60.0 + at.second / 3600.0
    for (h0, v0), (h1, v1) in pairwise(_DIURNAL):
        if h0 <= hour <= h1:
            span = h1 - h0
            t = 0.0 if span == 0 else (hour - h0) / span
            return v0 + (v1 - v0) * t
    return _DIURNAL[-1][1]


def ekadashi_factor(at: datetime, ekadashi: date) -> float:
    """Ashadhi Ekadashi and the three days around it.

    Peak day is roughly five times an ordinary day, and the day *before* is
    heavier than the day after — pilgrims arrive early and leave gradually.
    """
    days = (at.date() - ekadashi).days
    if days < -4 or days > 4:
        return 1.0
    curve = {-4: 1.4, -3: 1.9, -2: 2.6, -1: 3.8, 0: 5.0, 1: 3.2, 2: 2.2, 3: 1.6, 4: 1.2}
    return curve.get(days, 1.0)


def walking_speed(density: float) -> float:
    """Fruin's speed-density relation, linear form.

    Free flow until about 0.7 p/m², then falling roughly linearly to a standstill
    at the jam density.  This is why the simulation cannot produce a fast,
    very dense crowd: neither can a real one.
    """
    if density <= 0.7:
        return FREE_FLOW_SPEED_MS
    if density >= JAM_DENSITY:
        return 0.0
    return FREE_FLOW_SPEED_MS * (1.0 - (density - 0.7) / (JAM_DENSITY - 0.7))


def stagnation_from_speed(speed: float) -> float:
    """Share of people effectively not moving, given the mean speed.

    An exponential decay: at 1.2 m/s essentially nobody is stopped; at 0.2 m/s
    about a quarter are; below 0.1 m/s most of the zone is standing still, which
    is the state Section 4/M2 calls the crush precursor.
    """
    return min(1.0, exp(-speed / 0.16))


class SimEngine:
    """Generates zone telemetry indistinguishable from a live pipeline."""

    def __init__(
        self,
        zones: list[ZoneSpec],
        *,
        seed: int = 20260724,
        ekadashi: date | None = None,
        baseline_multiplier: float = 1.0,
    ) -> None:
        self.zones = zones
        self.ekadashi = ekadashi or date(2026, 7, 25)
        self.baseline_multiplier = baseline_multiplier
        self._random = random.Random(seed)
        self._state: dict[str, _ZoneState] = {}
        self.injections: list[Injection] = []

    # -- demo controls ----------------------------------------------------
    def inject(
        self,
        kind: str,
        *,
        zone_code: str | None = None,
        magnitude: float = 1.0,
        duration_seconds: int = 600,
        at: datetime | None = None,
        note: str | None = None,
    ) -> Injection:
        """Schedule an event.  This is the demo's T+1:30."""
        start = at or datetime.now(UTC)
        injection = Injection(
            kind=kind,
            zone_code=zone_code.upper() if zone_code else None,
            magnitude=magnitude,
            starts_at=start,
            ends_at=start + timedelta(seconds=duration_seconds),
            note=note,
        )
        self.injections.append(injection)
        return injection

    def clear_injections(self) -> int:
        count = len(self.injections)
        self.injections.clear()
        return count

    def prune(self, at: datetime) -> None:
        self.injections = [i for i in self.injections if i.ends_at >= at - timedelta(minutes=5)]

    def _injection_weights(self, zone_code: str, at: datetime) -> dict[str, float]:
        weights: dict[str, float] = {}
        for injection in self.injections:
            if injection.zone_code not in (None, zone_code):
                continue
            weight = injection.weight(at)
            if weight > 0:
                weights[injection.kind] = weights.get(injection.kind, 0.0) + weight
        return weights

    # -- generation -------------------------------------------------------
    def observe(self, at: datetime | None = None, *, with_heat_cells: bool = True) -> list[ZoneObservation]:
        """One 10-second window for every zone.

        `with_heat_cells=False` skips the 6x4 overlay grid.  The forecaster's
        season generator (Phase 8) runs this loop about sixty thousand times to
        build a training set and never looks at the overlay; computing 24 cells
        per zone per window would be most of that job's runtime, spent on a
        result nobody reads.
        """
        moment = at or datetime.now(UTC)
        self.prune(moment)
        return [self._observe_zone(zone, moment, with_heat_cells=with_heat_cells) for zone in self.zones]

    def _observe_zone(self, zone: ZoneSpec, at: datetime, *, with_heat_cells: bool = True) -> ZoneObservation:
        scale, bearing, two_way = _ZONE_CHARACTER.get(zone.zone_type, _DEFAULT_CHARACTER)
        state = self._state.setdefault(zone.zone_id, _ZoneState())
        weights = self._injection_weights(zone.code, at)

        target = (
            diurnal_factor(at)
            * ekadashi_factor(at, self.ekadashi)
            * scale
            * self.baseline_multiplier
            * self._random.uniform(0.92, 1.08)
        )
        target += weights.get("palkhi_surge", 0.0) * 0.9
        target += weights.get("crowd_surge", 0.0) * 0.6
        if "clear" in weights:
            target *= max(0.0, 1.0 - weights["clear"])

        # Crowds have inertia; the first reading after a restart should not be
        # a step from zero, so the state seeds itself at the target.
        if not state.initialised:
            state.occupancy = target
            state.initialised = True
        else:
            state.occupancy += (target - state.occupancy) * _INERTIA

        occupancy = max(0.0, state.occupancy)
        # Demand first, then what the ground can actually hold.  `person_count`
        # is derived back from the saturated density rather than the other way
        # round, so the two are always consistent — a reading of 5.8 p/m² over
        # 1200 m² says 6,960 people and means it.
        demand = zone.capacity_persons * occupancy / zone.area_m2 if zone.area_m2 > 0 else 0.0
        density = saturate_density(demand)
        person_count = int(round(density * zone.area_m2))

        speed = walking_speed(density)
        stall = weights.get("stall", 0.0)
        if stall:
            # A blockage stops the zone regardless of how dense it is — the
            # closed gate, the fallen pilgrim, the ambulance in the corridor.
            speed *= max(0.0, 1.0 - stall)

        stagnation = stagnation_from_speed(speed)
        stagnation = min(1.0, stagnation * self._random.uniform(0.9, 1.1))

        # Counter-flow drifts rather than jumping: two streams do not start
        # colliding within one ten-second window.
        counter_target = two_way * self._random.uniform(0.5, 1.3) + weights.get("counterflow", 0.0) * 0.6
        state.counterflow += (min(0.95, counter_target) - state.counterflow) * 0.25
        counterflow = max(0.0, min(1.0, state.counterflow))

        dx, dy = vector_from(bearing + self._random.uniform(-12.0, 12.0), speed)

        return ZoneObservation(
            zone_id=zone.zone_id,
            zone_code=zone.code,
            person_count=person_count,
            density=density,
            observed_at=at,
            flow_dx=dx,
            flow_dy=dy,
            stagnation_index=round(stagnation, 4),
            counterflow_ratio=round(counterflow, 4),
            # Simulated data is labelled as an estimate at the source. It is
            # published with source="sim" too, so nothing downstream can mistake
            # it for a measurement — including the person watching the demo.
            confidence=0.75,
            camera_count=len(zone.cameras),
            heat_cells=self._heat_cells(zone, person_count, density) if with_heat_cells else (),
        )

    def _heat_cells(self, zone: ZoneSpec, count: int, density: float) -> tuple[tuple[float, float, float], ...]:
        """A coarse 6x4 grid of local densities, for the heat-map overlay.

        Crowds are not uniform: they bank against the far edge of a zone, toward
        wherever they are going.  A flat overlay would look like a rectangle of
        one colour and teach an operator nothing.
        """
        if count == 0:
            return ()
        cells: list[tuple[float, float, float]] = []
        for row in range(4):
            for col in range(6):
                # Bias toward the top of the grid (the direction of travel) and
                # the centre line, then jitter.
                bias = (1.0 + 0.55 * (3 - row) / 3.0) * (1.0 - 0.25 * abs(col - 2.5) / 2.5)
                value = density * bias * self._random.uniform(0.75, 1.25)
                cells.append(((col + 0.5) / 6.0, (row + 0.5) / 4.0, round(value, 3)))
        return tuple(cells)


@dataclass(frozen=True, slots=True)
class SimStatus:
    zones: int
    injections: list[dict[str, object]] = field(default_factory=list)
    ekadashi: str = ""
    baseline_multiplier: float = 1.0
