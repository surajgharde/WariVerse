"""The simulation has to be *right*, not just plausible.

It is what the demo runs on and what every other Phase 3 test builds on, so a
sim that produces impossible readings would quietly validate a broken pipeline.
These tests encode the physics claims the module makes in prose.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app.sim_engine import (
    JAM_DENSITY,
    MAX_PHYSICAL_DENSITY,
    SimEngine,
    diurnal_factor,
    ekadashi_factor,
    saturate_density,
    stagnation_from_speed,
    walking_speed,
)

EKADASHI = date(2026, 7, 25)


def engine(zones, seed: int = 1) -> SimEngine:
    return SimEngine(zones, seed=seed, ekadashi=EKADASHI)


# ---------------------------------------------------------------------------
# crowd physics
# ---------------------------------------------------------------------------
def test_walking_speed_falls_with_density_and_reaches_zero_at_jam():
    assert walking_speed(0.2) == pytest.approx(1.25)
    assert walking_speed(2.0) < walking_speed(1.0)
    assert walking_speed(4.5) < walking_speed(3.0)
    assert walking_speed(JAM_DENSITY) == 0.0
    assert walking_speed(9.0) == 0.0


def test_dense_crowds_cannot_also_be_fast():
    """The claim the module makes: you cannot get a fast 5 p/m² out of this.

    Neither can a real corridor, which is why density alone is a poor alarm and
    stagnation matters.
    """
    for density in (4.6, 5.5, 8.0):
        assert walking_speed(density) < 0.2


def test_stagnation_is_derived_from_speed_not_dialled_independently():
    assert stagnation_from_speed(1.25) < 0.01
    assert 0.2 < stagnation_from_speed(0.2) < 0.4
    assert stagnation_from_speed(0.02) > 0.8
    assert stagnation_from_speed(0.0) == pytest.approx(1.0)


def test_density_saturates_at_a_physically_possible_ceiling():
    assert saturate_density(1.0) == pytest.approx(1.0)
    assert saturate_density(2.5) == pytest.approx(2.5)
    # Ten times the demand does not give ten times the density.
    assert saturate_density(30.0) < MAX_PHYSICAL_DENSITY
    assert saturate_density(100.0) < MAX_PHYSICAL_DENSITY
    # Monotone: more demand is never less density.
    values = [saturate_density(v) for v in (0.5, 2.0, 3.5, 5.0, 8.0, 20.0)]
    assert values == sorted(values)


# ---------------------------------------------------------------------------
# temporal shape
# ---------------------------------------------------------------------------
def test_darshan_opening_is_a_peak_not_a_ramp_from_zero():
    """04:00 opens onto a queue that formed overnight."""
    before = diurnal_factor(datetime(2026, 6, 10, 3, 0, tzinfo=UTC))
    opening = diurnal_factor(datetime(2026, 6, 10, 5, 30, tzinfo=UTC))
    midday = diurnal_factor(datetime(2026, 6, 10, 14, 0, tzinfo=UTC))
    evening = diurnal_factor(datetime(2026, 6, 10, 18, 30, tzinfo=UTC))

    assert opening > evening > midday > before
    assert midday < opening / 2


def test_ekadashi_is_the_peak_and_the_day_before_beats_the_day_after():
    ordinary = ekadashi_factor(datetime(2026, 6, 10, 12, 0, tzinfo=UTC), EKADASHI)
    peak = ekadashi_factor(datetime(2026, 7, 25, 12, 0, tzinfo=UTC), EKADASHI)
    day_before = ekadashi_factor(datetime(2026, 7, 24, 12, 0, tzinfo=UTC), EKADASHI)
    day_after = ekadashi_factor(datetime(2026, 7, 26, 12, 0, tzinfo=UTC), EKADASHI)

    assert ordinary == 1.0
    assert peak >= 5.0
    assert day_before > day_after  # pilgrims arrive early and leave gradually


# ---------------------------------------------------------------------------
# the demo arc
# ---------------------------------------------------------------------------
def test_ordinary_day_is_calm_everywhere(zones, ordinary_evening):
    """Section 16 T+0:45 — "normal state, all zones green"."""
    observations = engine(zones).observe(ordinary_evening)
    assert len(observations) == len(zones)
    for observation in observations:
        assert observation.density < 2.0, f"{observation.zone_code} should be calm on an ordinary evening"
        assert observation.stagnation_index < 0.2


def test_ekadashi_morning_pushes_the_core_zones_critical(zones, ekadashi_morning):
    by_code = {o.zone_code: o for o in engine(zones).observe(ekadashi_morning)}

    assert by_code["TC"].density > 5.0, "temple core should be critical at Ekadashi dawn"
    assert by_code["QC"].density > 5.0
    # The ghat is fifteen thousand square metres; it absorbs the same surge.
    assert by_code["CG"].density < by_code["TC"].density

    # And a critical zone must read as stalled, or the alert misses the reason.
    assert by_code["TC"].stagnation_index > 0.7


def test_palkhi_surge_climbs_then_disperses(zones, ordinary_evening):
    """Section 16 T+1:30. The whole point of the injection API."""
    sim = engine(zones, seed=7)
    sim.observe(ordinary_evening)  # settle the inertia

    sim.inject("palkhi_surge", zone_code="NW", magnitude=3.0, duration_seconds=900, at=ordinary_evening)

    def density_at(offset_seconds: int) -> float:
        moment = ordinary_evening + timedelta(seconds=offset_seconds)
        return next(o.density for o in sim.observe(moment) if o.zone_code == "NW")

    start = density_at(0)
    samples = [density_at(s) for s in range(30, 1500, 30)]
    peak = max(samples)
    settled = samples[-1]

    assert peak > start * 2.5, "a Dindi arriving should be unmistakable"
    assert peak > 3.5, "and should cross into the HIGH band"
    assert settled < peak * 0.7, "and should disperse afterwards, not plateau forever"


def test_injection_only_touches_the_named_zone(zones, ordinary_evening):
    sim = engine(zones, seed=3)
    sim.observe(ordinary_evening)
    baseline = {o.zone_code: o.density for o in sim.observe(ordinary_evening)}

    sim.inject("crowd_surge", zone_code="NW", magnitude=3.0, at=ordinary_evening)
    later = ordinary_evening + timedelta(minutes=4)
    after = {o.zone_code: o.density for o in sim.observe(later)}

    assert after["NW"] > baseline["NW"] * 1.5
    for code in ("TC", "QC", "CG"):
        assert after[code] == pytest.approx(baseline[code], rel=0.35)


def test_stall_injection_stops_a_zone_without_making_it_denser(zones, ordinary_evening):
    """The blocked-gate case: a corridor can stall while merely moderate.

    This is the reading density alone would call safe, and the one Section 4/M2
    says is the crush precursor.
    """
    sim = engine(zones, seed=11)
    sim.observe(ordinary_evening)

    sim.inject("stall", zone_code="QC", magnitude=1.0, duration_seconds=600, at=ordinary_evening)
    moment = ordinary_evening + timedelta(minutes=5)
    stalled = next(o for o in sim.observe(moment) if o.zone_code == "QC")

    assert stalled.stagnation_index > 0.7
    assert (stalled.flow_dx**2 + stalled.flow_dy**2) ** 0.5 < 0.1


def test_counterflow_injection_raises_only_counterflow(zones, ordinary_evening):
    sim = engine(zones, seed=5)
    for step in range(6):
        sim.observe(ordinary_evening + timedelta(seconds=10 * step))
    before = next(o for o in sim.observe(ordinary_evening) if o.zone_code == "NW").counterflow_ratio

    sim.inject("counterflow", zone_code="NW", magnitude=1.0, duration_seconds=600, at=ordinary_evening)
    for step in range(12):
        observations = sim.observe(ordinary_evening + timedelta(seconds=30 * (step + 1)))
    after = next(o for o in observations if o.zone_code == "NW")

    assert after.counterflow_ratio > before
    assert after.counterflow_ratio > 0.35, "should cross the turbulence threshold"


# ---------------------------------------------------------------------------
# invariants
# ---------------------------------------------------------------------------
def test_readings_are_deterministic_for_a_seed(zones, ekadashi_morning):
    first = engine(zones, seed=42).observe(ekadashi_morning)
    second = engine(zones, seed=42).observe(ekadashi_morning)
    assert [o.person_count for o in first] == [o.person_count for o in second]


def test_crowds_have_inertia_rather_than_teleporting(zones, ordinary_evening):
    sim = engine(zones, seed=9)
    sim.observe(ordinary_evening)
    sim.inject("crowd_surge", zone_code="TC", magnitude=4.0, duration_seconds=600, at=ordinary_evening)

    one_window = next(o for o in sim.observe(ordinary_evening + timedelta(seconds=10)) if o.zone_code == "TC")
    settled = next(o for o in sim.observe(ordinary_evening + timedelta(seconds=300)) if o.zone_code == "TC")

    assert one_window.density < settled.density, "a zone should not jump to its target in one window"


def test_published_payload_carries_no_identifying_field(zones, ekadashi_morning):
    """The privacy invariant, asserted rather than promised."""
    payload = engine(zones).observe(ekadashi_morning)[0].to_payload()

    assert set(payload) == {
        "zone_id", "zone_code", "person_count", "density", "observed_at",
        "flow_dx", "flow_dy", "stagnation_index", "counterflow_ratio",
        "confidence", "camera_count",
    }
    for banned in ("track", "person_id", "bbox", "embedding", "face", "image", "heat_cells"):
        assert not any(banned in key for key in payload)


def test_simulated_readings_are_labelled_as_estimates(zones, ordinary_evening):
    """Section 0 rule 3. Nobody watching a demo should mistake this for a
    measurement, including the person giving the demo."""
    for observation in engine(zones).observe(ordinary_evening):
        assert observation.confidence < 1.0


def test_expired_injections_are_pruned(zones, ordinary_evening):
    sim = engine(zones)
    sim.inject("crowd_surge", zone_code="TC", duration_seconds=60, at=ordinary_evening)
    assert len(sim.injections) == 1

    sim.observe(ordinary_evening + timedelta(minutes=30))
    assert sim.injections == []
