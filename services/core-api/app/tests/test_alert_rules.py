"""The recommendation rule table.

These tests encode the claims Section 4/M2 makes in prose, so that changing a
threshold breaks a test rather than quietly changing when a gate gets closed.

The rules are pure functions of a reading, so none of this needs a database.
"""

from __future__ import annotations

import pytest

from app.models.crowd import DENSITY_THRESHOLDS, AlertSeverity, DensityLevel, classify_density
from app.services.recommendations import (
    CAMERA_OFFLINE_RULE,
    COUNTERFLOW,
    DENSITY_CRITICAL,
    DENSITY_HIGH,
    RULES,
    STAGNATION,
    CrowdSignal,
    Thresholds,
    evaluate,
    rule_by_id,
)

BANDS = Thresholds()


def signal(**kwargs) -> CrowdSignal:
    base = {"density": 1.0, "stagnation_index": 0.0, "counterflow_ratio": 0.05, "person_count": 500}
    return CrowdSignal(**{**base, **kwargs})


# ---------------------------------------------------------------------------
# the published bands
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("density", "expected"),
    [
        (0.0, DensityLevel.SAFE),
        (1.9, DensityLevel.SAFE),
        (2.0, DensityLevel.MODERATE),
        (3.4, DensityLevel.MODERATE),
        (3.5, DensityLevel.HIGH),
        (4.9, DensityLevel.HIGH),
        (5.0, DensityLevel.CRITICAL),
        (9.0, DensityLevel.CRITICAL),
    ],
)
def test_density_bands_match_the_published_thresholds(density: float, expected: DensityLevel):
    assert classify_density(density) is expected


def test_the_bands_are_the_ones_the_spec_names():
    assert DENSITY_THRESHOLDS[DensityLevel.SAFE] == 2.0
    assert DENSITY_THRESHOLDS[DensityLevel.MODERATE] == 3.5
    assert DENSITY_THRESHOLDS[DensityLevel.HIGH] == 5.0


# ---------------------------------------------------------------------------
# what fires, and what does not
# ---------------------------------------------------------------------------
def test_a_calm_zone_raises_nothing():
    assert evaluate(signal(density=1.2), BANDS) is None


def test_moderate_density_alone_does_not_alert():
    """2.0-3.5 is busy, not dangerous.  Alerting here is how a feed becomes
    wallpaper and the real alert gets missed."""
    assert evaluate(signal(density=3.0), BANDS) is None


def test_high_density_warns():
    result = evaluate(signal(density=4.2), BANDS)
    assert result is not None
    assert result.rule.alert_type == DENSITY_HIGH
    assert result.rule.severity is AlertSeverity.WARNING


def test_critical_density_alerts_critical():
    result = evaluate(signal(density=5.4), BANDS)
    assert result is not None
    assert result.rule.alert_type == DENSITY_CRITICAL
    assert result.rule.severity is AlertSeverity.CRITICAL


def test_a_stalled_dense_crowd_is_the_first_rule_in_the_table():
    """Section 4/M2: 'a stalled dense crowd is the crush precursor, not raw
    density alone'.  It must win over the plain density rule, and its action
    must be the one that stops intake."""
    result = evaluate(signal(density=5.6, stagnation_index=0.85), BANDS)
    assert result is not None
    assert result.rule.id == "R-M2-01"
    assert "stop intake" in result.action.lower()


def test_stagnation_alerts_below_critical_density():
    """The reading density alone would call safe: a merely-high corridor that
    has stopped moving."""
    result = evaluate(signal(density=3.4, stagnation_index=0.85), BANDS)
    assert result is not None
    assert result.rule.alert_type == STAGNATION
    assert result.rule.severity is AlertSeverity.CRITICAL


def test_a_still_but_empty_plaza_does_not_alert():
    """A stagnation index of 1.0 at 3 a.m. with nobody there means nothing.
    Without the density floor this would page someone every night."""
    assert evaluate(signal(density=0.4, stagnation_index=1.0), BANDS) is None
    assert evaluate(signal(density=2.9, stagnation_index=1.0), BANDS) is None


def test_counterflow_alerts_on_its_own():
    """Opposing streams are dangerous before they are dense."""
    result = evaluate(signal(density=2.5, counterflow_ratio=0.44), BANDS)
    assert result is not None
    assert result.rule.alert_type == COUNTERFLOW
    assert "one-way" in result.action.lower()


def test_counterflow_below_the_threshold_is_normal_traffic():
    assert evaluate(signal(density=2.5, counterflow_ratio=0.30), BANDS) is None


def test_one_reading_produces_at_most_one_alert():
    """A zone that is dense *and* stalled *and* turbulent is one situation with
    one correct response, not three rows in the feed."""
    result = evaluate(signal(density=6.0, stagnation_index=0.9, counterflow_ratio=0.6), BANDS)
    assert result is not None
    assert result.rule.id == "R-M2-01"


def test_operators_can_tighten_the_behavioural_thresholds():
    tight = Thresholds(stagnation=0.40, counterflow=0.20)
    calm = signal(density=3.2, stagnation_index=0.5, counterflow_ratio=0.25)

    assert evaluate(calm, BANDS) is None
    assert evaluate(calm, tight) is not None


# ---------------------------------------------------------------------------
# the table itself
# ---------------------------------------------------------------------------
def test_every_rule_carries_a_marathi_action():
    """The control room runs in Marathi.  An English-only recommendation is an
    unusable one."""
    for rule in (*RULES, CAMERA_OFFLINE_RULE):
        assert rule.action_mr.strip()
        assert rule.action_mr != rule.action
        assert any("ऀ" <= ch <= "ॿ" for ch in rule.action_mr), f"{rule.id} is not Devanagari"


def test_rule_ids_are_unique_and_resolvable():
    ids = [rule.id for rule in (*RULES, CAMERA_OFFLINE_RULE)]
    assert len(ids) == len(set(ids))
    for rule_id in ids:
        assert rule_by_id(rule_id) is not None
    assert rule_by_id("R-DOES-NOT-EXIST") is None


def test_every_rule_names_a_metric_a_reading_actually_has():
    probe = signal()
    for rule in RULES:
        assert hasattr(probe, rule.metric), f"{rule.id} reads a field that does not exist"
        assert isinstance(rule.value_of(probe), float)


def test_the_most_severe_rules_come_first():
    """The table is first-match-wins, so ordering is behaviour."""
    severities = [rule.severity for rule in RULES]
    assert severities[0] is AlertSeverity.CRITICAL
    assert severities[-1] is AlertSeverity.WARNING


def test_camera_offline_never_fires_from_a_density_reading():
    """It is dispatched by the watchdog, not matched against a signal."""
    assert CAMERA_OFFLINE_RULE.matches(signal(density=9.0, stagnation_index=1.0), BANDS) is False
    for density in (0.0, 3.0, 6.0, 20.0):
        result = evaluate(signal(density=density, stagnation_index=1.0, counterflow_ratio=0.9), BANDS)
        if result is not None:
            assert result.rule.id != CAMERA_OFFLINE_RULE.id
