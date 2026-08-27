"""Palkhi pace arithmetic and the deviation rule table (Section 4/M8, Phase 9).

These encode the claims Section 4/M8 makes in prose, so that loosening one
breaks a test rather than quietly changing when a halt town is told to move its
kitchen.  All of it is pure — pace, ETA, deviation, readiness and the rules are
functions of plain dataclasses — so none of this needs Postgres.

Four claims are load-bearing enough to state up front, because they are the ones
a future change is most likely to erode:

1. **An unusable pace produces no ETA.**  Not a default 3 km/h, not the last
   known figure. A halt town cannot distinguish a guessed arrival time from a
   measured one once it is rendered as a time, and it will staff the kitchen
   for both the same way.
2. **Early is treated as more dangerous than late.**  Late means food going
   cold; early means five hundred people arriving at a town whose water tankers
   have not come.
3. **A thin pace estimate never moves a town's arrangements.**  It produces
   R-M8-04, which asks somebody to make a phone call.
4. **A town with nobody scheduled is `unknown`, not `ready`.**  "No shortfall
   found" and "assessed and ready" are different claims.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models.crowd import AlertSeverity
from app.models.palkhi import HaltReadiness
from app.services.palkhi_service import (
    MAX_WALKING_KMPH,
    PingSample,
    ProvisioningRatios,
    assess_readiness,
    estimate_pace,
    eta_for,
    haversine_km,
    next_ping_seconds,
)
from app.services.recommendations import (
    DINDI_RULES,
    PALKHI_DEVIATION,
    DindiSignal,
    Thresholds,
    evaluate_dindi,
    rule_by_id,
)

BANDS = Thresholds()
NOW = datetime(2026, 6, 20, 14, 0, tzinfo=UTC)

# Two points on the Alandi-Pandharpur road, about 4.5 km apart.
ALANDI = (73.8977, 18.6773)
NEXT_VILLAGE = (73.9350, 18.6600)


def samples(*offsets_and_fractions: tuple[int, float]) -> list[PingSample]:
    """Build pings at N minutes past NOW with the given route fractions."""
    return [
        PingSample(
            at=NOW + timedelta(minutes=minutes),
            lon=ALANDI[0],
            lat=ALANDI[1],
            route_fraction=fraction,
        )
        for minutes, fraction in offsets_and_fractions
    ]


# ---------------------------------------------------------------------------
# distance and pace
# ---------------------------------------------------------------------------
def test_haversine_matches_known_distance():
    km = haversine_km(*ALANDI, *NEXT_VILLAGE)
    assert 4.0 < km < 5.0


def test_haversine_is_zero_for_the_same_point():
    assert haversine_km(*ALANDI, *ALANDI) == pytest.approx(0.0, abs=1e-9)


def test_pace_projects_onto_the_route_when_there_is_one():
    """1% of a 250 km route in one hour is 2.5 km/h — a walking procession."""
    pace = estimate_pace(samples((0, 0.100), (30, 0.105), (60, 0.110)), total_km=250.0)

    assert pace.method == "route"
    assert pace.kmph == pytest.approx(2.5, abs=0.01)
    assert pace.samples == 3
    assert pace.span_minutes == pytest.approx(60.0)
    assert pace.is_usable


def test_pace_falls_back_to_straight_line_without_a_route_path():
    """A Dindi can be tracked before anybody has digitised its road.

    The estimate degrades to crow-flies rather than refusing, and says so in
    `method` so an operator knows a winding road is being under-stated.
    """
    unlocated = [
        PingSample(at=NOW, lon=ALANDI[0], lat=ALANDI[1]),
        PingSample(at=NOW + timedelta(hours=1), lon=NEXT_VILLAGE[0], lat=NEXT_VILLAGE[1]),
    ]
    pace = estimate_pace(unlocated, total_km=None)

    assert pace.method == "straight"
    assert 4.0 < pace.kmph < 5.0
    assert pace.is_usable


def test_pace_is_first_to_last_not_an_average_of_legs():
    """A group that walked an hour then stopped for lunch has covered 2.5 km in
    two hours, not walked at 2.5 km/h.

    Averaging the legs would report the walking speed and put the ETA an hour
    early. What the halt town needs is the ground actually covered.
    """
    walked_then_halted = samples((0, 0.100), (60, 0.110), (120, 0.110))
    pace = estimate_pace(walked_then_halted, total_km=250.0)

    assert pace.km_covered == pytest.approx(2.5, abs=0.01)
    assert pace.kmph == pytest.approx(1.25, abs=0.01)


def test_a_single_ping_is_not_a_pace():
    pace = estimate_pace(samples((0, 0.1)), total_km=250.0)
    assert pace.method == "none"
    assert not pace.is_usable


def test_a_stationary_group_has_no_usable_pace():
    pace = estimate_pace(samples((0, 0.100), (60, 0.100)), total_km=250.0)
    assert pace.kmph == pytest.approx(0.0)
    assert not pace.is_usable


def test_vehicle_speed_is_rejected_as_a_walking_pace():
    """A phone in a support truck must not set a walking group's ETA."""
    fast = samples((0, 0.100), (60, 0.180))  # 20 km in an hour
    pace = estimate_pace(fast, total_km=250.0)

    assert pace.kmph > MAX_WALKING_KMPH
    assert not pace.is_usable


def test_walking_backwards_is_not_a_confident_pace():
    pace = estimate_pace(samples((0, 0.110), (60, 0.100)), total_km=250.0)
    assert pace.kmph < 0
    assert not pace.is_usable


def test_pings_arriving_out_of_order_are_sorted_before_measuring():
    """A phone that was offline flushes its queue in whatever order it held."""
    jumbled = samples((60, 0.110), (0, 0.100), (30, 0.105))
    assert estimate_pace(jumbled, total_km=250.0).kmph == pytest.approx(2.5, abs=0.01)


# ---------------------------------------------------------------------------
# ETA — claim 1
# ---------------------------------------------------------------------------
def test_eta_is_distance_over_pace():
    pace = estimate_pace(samples((0, 0.100), (60, 0.110)), total_km=250.0)  # 2.5 km/h
    eta = eta_for(5.0, pace, at=NOW)

    assert eta is not None
    assert eta == NOW + timedelta(hours=2)


def test_an_unusable_pace_produces_no_eta_rather_than_a_default():
    """The single most important line in the module.

    Not "fall back to 3 km/h": a halt town cannot tell a guessed ETA from a
    measured one once it is a time on a screen.
    """
    stalled = estimate_pace(samples((0, 0.100), (60, 0.100)), total_km=250.0)
    assert eta_for(5.0, stalled, at=NOW) is None

    one_dot = estimate_pace(samples((0, 0.1)), total_km=250.0)
    assert eta_for(5.0, one_dot, at=NOW) is None


def test_unknown_distance_produces_no_eta():
    pace = estimate_pace(samples((0, 0.100), (60, 0.110)), total_km=250.0)
    assert eta_for(-1.0, pace, at=NOW) is None


# ---------------------------------------------------------------------------
# battery-aware reporting interval
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("battery", "expected"),
    [
        (100, 60),
        (60, 60),
        (40, 180),
        (20, 600),
        (5, 900),
        (None, 60),
    ],
)
def test_ping_interval_backs_off_as_the_battery_drains(battery, expected):
    """Section 4/M8: 60s, battery-aware.

    Backing off is the feature, not a degradation. A phone reporting every 60
    seconds until it dies on day eleven tells the halt towns nothing for the
    remaining seven days.
    """
    assert next_ping_seconds(battery, 60) == expected


def test_a_halted_group_reports_rarely_whatever_its_battery():
    """A stationary position is not news, and the night halt is when the phone
    is most likely to find a charger."""
    assert next_ping_seconds(100, 60, halted=True) >= 600


# ---------------------------------------------------------------------------
# halt-town readiness — claim 4
# ---------------------------------------------------------------------------
RATIOS = ProvisioningRatios()


def assess(headcount, water, sanitation, medical, declared="unknown"):
    return assess_readiness(
        expected_headcount=headcount,
        water_points=water,
        sanitation_units=sanitation,
        medical_camps=medical,
        declared=declared,
        ratios=RATIOS,
    )


def test_a_fully_provisioned_town_is_ready():
    # 1000 walkers: 4 water, 10 sanitation, 1 medical.
    result = assess(1000, water=4, sanitation=10, medical=1, declared="ready")

    assert result.computed == HaltReadiness.READY
    assert result.gaps == []
    assert not result.disagrees


def test_requirements_round_up():
    """Half a water point serves nobody, and which way to round a shortfall is
    not a matter of taste."""
    result = assess(1100, water=0, sanitation=0, medical=0)

    assert result.water_points_required == 5  # 4.4 -> 5
    assert result.sanitation_units_required == 11
    assert result.medical_camps_required == 1


def test_a_small_group_still_needs_one_medical_camp():
    """Rounding this to zero for a 400-person halt is how you get a collapse
    and a 40-minute ambulance."""
    assert assess(400, water=2, sanitation=4, medical=0).medical_camps_required == 1


def test_no_medical_cover_is_not_ready_rather_than_partial():
    result = assess(1000, water=4, sanitation=10, medical=0)
    assert result.computed == HaltReadiness.NOT_READY


def test_less_than_half_the_water_is_not_ready():
    result = assess(1000, water=1, sanitation=10, medical=1)
    assert result.computed == HaltReadiness.NOT_READY


def test_a_modest_shortfall_is_partial():
    result = assess(1000, water=3, sanitation=8, medical=1)
    assert result.computed == HaltReadiness.PARTIAL
    assert result.gaps


def test_a_town_with_nobody_coming_is_unknown_not_ready():
    """Claim 4. "No shortfall found" is not "assessed and ready"."""
    assert assess(0, water=0, sanitation=0, medical=0).computed == HaltReadiness.UNKNOWN


def test_a_town_claiming_ready_without_the_provisioning_is_flagged():
    """The most useful field on the board.

    A town marked ready with water for half the arrivals is precisely the
    failure Section 4/M8 exists to catch, and one merged status would hide it.
    """
    result = assess(2000, water=1, sanitation=2, medical=0, declared="ready")

    assert result.declared == HaltReadiness.READY
    assert result.computed == HaltReadiness.NOT_READY
    assert result.disagrees


def test_gaps_are_reported_in_both_languages():
    result = assess(1000, water=0, sanitation=0, medical=0)
    assert len(result.gaps) == len(result.gaps_mr) == 3
    assert all(text for text in result.gaps_mr)


# ---------------------------------------------------------------------------
# the deviation rules — claims 2 and 3
# ---------------------------------------------------------------------------
def signal(deviation, *, readiness="ready", pace_samples=8, pace=2.8, count=400):
    planned = NOW + timedelta(hours=4)
    return DindiSignal(
        dindi_name="Sant Tukaram Dindi 14",
        dindi_name_mr="संत तुकाराम दिंडी १४",
        next_town="Saswad",
        next_town_mr="सासवड",
        deviation_minutes=deviation,
        planned_arrival=planned,
        eta=planned + timedelta(minutes=deviation),
        expected_count=count,
        pace_kmph=pace,
        km_remaining=11.2,
        next_town_readiness=readiness,
        pace_samples=pace_samples,
    )


def test_a_dindi_on_schedule_produces_nothing():
    """Eighteen days of walking produce a great many readings that mean "still
    on schedule", and not one of them should reach an operator."""
    assert evaluate_dindi(signal(0), BANDS) is None
    assert evaluate_dindi(signal(30), BANDS) is None
    assert evaluate_dindi(signal(-30), BANDS) is None


def test_the_threshold_is_section_4_m8s_forty_five_minutes():
    assert BANDS.dindi_deviation_minutes == 45.0
    assert evaluate_dindi(signal(45), BANDS) is None
    assert evaluate_dindi(signal(46), BANDS) is not None


def test_early_into_an_unready_town_is_critical():
    """Claim 2. This is the case the whole module exists for."""
    result = evaluate_dindi(signal(-62, readiness="not_ready"), BANDS)

    assert result is not None
    assert result.rule.id == "R-M8-01"
    assert result.rule.severity == AlertSeverity.CRITICAL


def test_a_town_that_has_not_reported_is_treated_as_unready():
    """`unknown` is not `ready`. A town nobody has checked gets the loud rule."""
    result = evaluate_dindi(signal(-62, readiness="unknown"), BANDS)
    assert result is not None
    assert result.rule.id == "R-M8-01"


def test_early_into_a_ready_town_is_only_a_warning():
    result = evaluate_dindi(signal(-62, readiness="ready"), BANDS)

    assert result is not None
    assert result.rule.id == "R-M8-02"
    assert result.rule.severity == AlertSeverity.WARNING


def test_late_is_a_warning_even_when_the_town_is_not_ready():
    """Claim 2, the other half: late gives the town time, early takes it away."""
    result = evaluate_dindi(signal(80, readiness="not_ready"), BANDS)

    assert result is not None
    assert result.rule.id == "R-M8-03"
    assert result.rule.severity == AlertSeverity.WARNING


def test_a_thin_pace_estimate_never_moves_a_towns_arrangements():
    """Claim 3.

    Two dots on a map is not evidence for telling Saswad to move its kitchen.
    It is evidence for phoning the Dindi leader.
    """
    result = evaluate_dindi(signal(-90, readiness="not_ready", pace_samples=2), BANDS)

    assert result is not None
    assert result.rule.id == "R-M8-04"
    assert result.rule.severity == AlertSeverity.INFO
    assert "phone the Dindi leader" in result.action
    assert "Do not move" in result.action


def test_a_crawling_pace_is_not_confident_either():
    result = evaluate_dindi(signal(-90, readiness="not_ready", pace=0.2), BANDS)
    assert result is not None
    assert result.rule.id == "R-M8-04"


def test_the_uncertain_case_is_never_silent():
    """Silence reads as "on time"."""
    assert evaluate_dindi(signal(120, pace_samples=1), BANDS) is not None


def test_every_number_in_the_action_comes_from_the_signal():
    """Section 4/M6's rule, applied to M8: a model may phrase a recommendation,
    it may not produce one. Nothing in these sentences is invented at render."""
    result = evaluate_dindi(signal(-62, readiness="not_ready"), BANDS)

    assert result is not None
    assert "62 min" in result.action
    assert "Saswad" in result.action
    assert "400 walkers" in result.action
    assert "2.8 km/h" in result.action
    assert "11 km" in result.action


def test_the_action_is_bilingual_and_marathi_carries_the_numbers():
    """The control room and the halt-town coordinators run in Marathi. A
    Marathi sentence missing the numbers is a Marathi sentence nobody can act
    on."""
    result = evaluate_dindi(signal(-62, readiness="not_ready"), BANDS)

    assert result is not None
    assert "सासवड" in result.action_mr
    assert "62" in result.action_mr
    assert "400" in result.action_mr


def test_rules_are_ordered_most_dangerous_first():
    assert [r.id for r in DINDI_RULES] == ["R-M8-01", "R-M8-02", "R-M8-03", "R-M8-04"]


def test_every_rule_is_resolvable_from_an_alert_row():
    """An operator looking at an alert must be able to see which rule spoke."""
    for rule in DINDI_RULES:
        resolved = rule_by_id(rule.id)
        assert resolved is not None
        assert resolved.alert_type == PALKHI_DEVIATION


def test_the_condition_rules_are_resolvable_too():
    for rule_id in ("R-M8-05", "R-M8-06"):
        assert rule_by_id(rule_id) is not None


def test_the_deviation_threshold_is_tunable():
    """A route's first two days run to a much looser clock than its last two."""
    loose = Thresholds(dindi_deviation_minutes=120.0)
    assert evaluate_dindi(signal(-62, readiness="not_ready"), loose) is None
    assert evaluate_dindi(signal(-130, readiness="not_ready"), loose) is not None
