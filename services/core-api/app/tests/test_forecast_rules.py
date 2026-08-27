"""The forecast recommendation table (Section 4/M6, Phase 8).

These encode the claims Section 4/M6 makes in prose, so that loosening one
breaks a test rather than quietly changing when an operator is told to hold a
gate on a prediction.

The rules are pure functions of a forecast, so none of this needs a database.
The two claims worth stating up front, because they are the ones a future
change is most likely to erode:

1. **A recommendation is never produced from a prediction the model cannot
   stand behind.** Section 4/M6 lets a language model *phrase* an action; the
   trigger and the numbers come from here. A wide interval produces "go and
   look", never "hold a gate".
2. **Every number in the sentence comes from the forecast or from
   `hold_minutes`.** Nothing in the template is invented at render time.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.models.crowd import AlertSeverity
from app.services.recommendations import (
    FORECAST_HIGH,
    FORECAST_RULES,
    MAX_ACTIONABLE_INTERVAL_WIDTH,
    ForecastSignal,
    Thresholds,
    evaluate_forecast,
    hold_minutes,
    rule_by_id,
)

BANDS = Thresholds()
TARGET = datetime(2026, 7, 25, 15, 30, tzinfo=UTC)


def forecast(
    density: float, *, low: float | None = None, high: float | None = None, horizon: int = 60
) -> ForecastSignal:
    """A tight interval around the point estimate unless a test says otherwise."""
    return ForecastSignal(
        predicted_density=density,
        interval_low=low if low is not None else max(0.0, density - 0.4),
        interval_high=high if high is not None else density + 0.4,
        horizon_minutes=horizon,
        target_at=TARGET,
        zone_name="Zone C",
        zone_name_mr="विभाग क",
        gate_name="Gate 2",
        gate_name_mr="द्वार २",
    )


# ---------------------------------------------------------------------------
# when the rules fire
# ---------------------------------------------------------------------------
def test_calm_forecast_recommends_nothing() -> None:
    assert evaluate_forecast(forecast(1.8), BANDS) is None


def test_below_the_high_band_recommends_nothing() -> None:
    """The band is 3.5. A forecast of 3.4 is not an event."""
    assert evaluate_forecast(forecast(3.4), BANDS) is None


def test_crossing_high_warns() -> None:
    result = evaluate_forecast(forecast(4.2), BANDS)
    assert result is not None
    assert result.rule.id == "R-M6-02"
    assert result.rule.severity is AlertSeverity.WARNING


def test_crossing_critical_escalates() -> None:
    result = evaluate_forecast(forecast(5.6), BANDS)
    assert result is not None
    assert result.rule.id == "R-M6-01"
    assert result.rule.severity is AlertSeverity.CRITICAL


def test_first_match_wins() -> None:
    """A critical forecast produces one recommendation, not two.

    The same reason `evaluate` has: three overlapping alerts for one approaching
    surge is how a feed becomes wallpaper.
    """
    matches = [r for r in FORECAST_RULES if r.matches(forecast(5.6), BANDS)]
    assert len(matches) >= 1
    assert evaluate_forecast(forecast(5.6), BANDS).rule.id == matches[0].id


# ---------------------------------------------------------------------------
# uncertainty is a first-class input
# ---------------------------------------------------------------------------
def test_wide_interval_never_recommends_holding_a_gate() -> None:
    """Section 4/M6's rules must not act on a shrug.

    4.2 p/m² with a range of 1.1-7.0 is the model saying it does not know. The
    advisory rule fires instead, and it recommends looking, not holding.
    """
    result = evaluate_forecast(forecast(4.2, low=1.1, high=7.0), BANDS)
    assert result is not None
    assert result.rule.id == "R-M6-03"
    assert result.rule.severity is AlertSeverity.INFO
    assert "Do not hold a gate" in result.action


def test_wide_interval_stays_silent_when_even_the_top_is_safe() -> None:
    """An uncertain forecast that is harmless at its worst is not news."""
    assert evaluate_forecast(forecast(2.0, low=0.2, high=4.0), BANDS) is None


def test_interval_width_boundary_is_inclusive() -> None:
    """At exactly the threshold the forecast is still actionable.

    Stated as a test because the alternative reading — actionable only below the
    width — silently moves where the advisory rule takes over.
    """
    density = 5.6
    half = MAX_ACTIONABLE_INTERVAL_WIDTH / 2
    at_limit = forecast(density, low=density - half, high=density + half)
    assert at_limit.is_actionable
    assert evaluate_forecast(at_limit, BANDS).rule.id == "R-M6-01"


# ---------------------------------------------------------------------------
# the numbers in the sentence
# ---------------------------------------------------------------------------
def test_action_carries_the_forecasts_own_numbers() -> None:
    """Section 4/M6's example is concrete, and so is this."""
    result = evaluate_forecast(forecast(4.2, low=3.6, high=4.9), BANDS)
    assert result is not None
    assert "4.2 p/m²" in result.action
    assert "3.6-4.9" in result.action
    assert "15:30" in result.action
    assert "Gate 2" in result.action
    assert "60 min ahead" in result.action


def test_marathi_action_carries_the_same_numbers() -> None:
    """The Marathi is the operational text, not a courtesy translation."""
    result = evaluate_forecast(forecast(5.6), BANDS)
    assert result is not None
    assert "5.6" in result.action_mr
    assert "15:30" in result.action_mr
    assert "द्वार २" in result.action_mr
    assert str(result.hold_minutes) in result.action_mr


@pytest.mark.parametrize(
    ("density", "expected"),
    [
        (3.5, 5),    # exactly at the band: the floor, and the only case that gets it
        (3.6, 10),   # anything above rounds *up* to the next step, deliberately
        (4.2, 15),   # Section 4/M6's own example figure
        (5.6, 25),
        (7.0, 35),
    ],
)
def test_hold_scales_with_the_excess(density: float, expected: int) -> None:
    assert hold_minutes(forecast(density), BANDS) == expected


def test_hold_is_always_a_five_minute_step() -> None:
    """A marshal cannot act on "hold for 7 minutes"."""
    for tenth in range(35, 90):
        held = hold_minutes(forecast(tenth / 10), BANDS)
        assert held % 5 == 0


def test_hold_never_outlives_the_forecast_behind_it() -> None:
    """A 30-minute prediction cannot justify a 35-minute hold.

    Past the horizon there is no forecast supporting the instruction, and an
    instruction that outlives its evidence is one nobody can review afterwards.
    """
    assert hold_minutes(forecast(7.0, horizon=30), BANDS) == 30
    assert hold_minutes(forecast(7.0, horizon=90), BANDS) == 35


# ---------------------------------------------------------------------------
# traceability
# ---------------------------------------------------------------------------
def test_every_forecast_rule_is_resolvable_by_id() -> None:
    """`Alert.rule_id` must lead back to the rule that spoke.

    An operator who held a gate needs to be able to show which numbered rule
    told them to, months later, in a review.
    """
    for rule in FORECAST_RULES:
        found = rule_by_id(rule.id)
        assert found is not None, rule.id
        assert found.alert_type == FORECAST_HIGH


def test_rule_ids_are_unique() -> None:
    ids = [r.id for r in FORECAST_RULES]
    assert len(ids) == len(set(ids))


def test_no_rule_recommends_an_action_without_a_number() -> None:
    """Every template interpolates the forecast. A fixed sentence here would be
    a safety instruction with no evidence attached to it."""
    for rule in FORECAST_RULES:
        assert "{" in rule.template
        assert "{" in rule.template_mr
