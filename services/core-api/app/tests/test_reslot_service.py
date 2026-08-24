"""Reslotting arithmetic — pure unit tests.

Section 0 names reslotting as one of the three things that hurt people when it
breaks. The 40%-drop scenario from the acceptance criteria is `test_forced_
forty_percent_drop_shifts_downstream` in `test_passes_api.py`; this file pins
the arithmetic underneath it.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.services.reslot_service import (
    MAX_DELAY_MINUTES,
    ReslotDecision,
    ThroughputWindow,
    decide,
    deviation,
    is_downstream,
    notification_text,
    raw_delay_minutes,
    round_up_to_slot,
    shifted_start,
)

THRESHOLD = 0.20
SLOT_MINUTES = 30


def window(planned: int, actual: int, minutes: int = 30) -> ThroughputWindow:
    return ThroughputWindow(planned=planned, actual=actual, minutes=minutes)


# --- deviation -------------------------------------------------------------
def test_deviation_is_signed() -> None:
    assert deviation(window(3000, 1800)) == pytest.approx(-0.4)
    assert deviation(window(3000, 3600)) == pytest.approx(0.2)
    assert deviation(window(3000, 3000)) == 0.0


def test_deviation_of_an_empty_plan_is_zero_not_a_crash() -> None:
    assert deviation(window(0, 0)) == 0.0
    assert deviation(window(0, 500)) == 0.0


def test_window_rejects_impossible_inputs() -> None:
    with pytest.raises(ValueError):
        ThroughputWindow(planned=10, actual=5, minutes=0)
    with pytest.raises(ValueError):
        ThroughputWindow(planned=-1, actual=5, minutes=30)


# --- the threshold ---------------------------------------------------------
def test_small_shortfall_does_not_move_anyone() -> None:
    """Reslotting buzzes tens of thousands of phones. A 10% wobble is noise."""
    decision = decide(window(3000, 2700), deviation_threshold=THRESHOLD, slot_minutes=SLOT_MINUTES)
    assert not decision.should_reslot
    assert decision.delay_minutes == 0
    assert "tolerance" in decision.reason


def test_exactly_at_the_threshold_does_not_trigger() -> None:
    # Spec says deviates *more than* 20%.
    decision = decide(window(3000, 2400), deviation_threshold=THRESHOLD, slot_minutes=SLOT_MINUTES)
    assert deviation(window(3000, 2400)) == pytest.approx(-0.2)
    assert not decision.should_reslot


def test_forty_percent_drop_triggers_a_shift() -> None:
    """The acceptance scenario: force a 40% throughput drop."""
    decision = decide(window(3000, 1800), deviation_threshold=THRESHOLD, slot_minutes=SLOT_MINUTES)
    assert decision.should_reslot
    assert decision.deviation == pytest.approx(-0.4)
    # 1200 people short, clearing at 60/min, is 20 minutes -> one 30-min slot.
    assert decision.delay_minutes == 30


def test_a_gate_running_fast_never_pulls_passes_earlier() -> None:
    """Rule 1: people travel. An earlier slot they cannot reach is a false
    promise, so a surplus is simply absorbed."""
    decision = decide(window(3000, 4500), deviation_threshold=THRESHOLD, slot_minutes=SLOT_MINUTES)
    assert not decision.should_reslot
    assert decision.delay_minutes == 0
    assert "above plan" in decision.reason


def test_nothing_planned_means_nothing_to_correct() -> None:
    decision = decide(window(0, 0), deviation_threshold=THRESHOLD, slot_minutes=SLOT_MINUTES)
    assert not decision.should_reslot


# --- delay computation -----------------------------------------------------
def test_delay_clears_the_backlog_at_the_observed_rate() -> None:
    # 3000 planned, 1500 served in 30 min -> 1500 short at 50/min = 30 min.
    assert raw_delay_minutes(window(3000, 1500)) == pytest.approx(30.0)


def test_a_stopped_gate_is_capped_not_infinite() -> None:
    """Dividing by a zero rate would tell a pilgrim their slot moved by days."""
    assert raw_delay_minutes(window(3000, 0)) == MAX_DELAY_MINUTES
    decision = decide(window(3000, 0), deviation_threshold=THRESHOLD, slot_minutes=SLOT_MINUTES)
    assert decision.delay_minutes == MAX_DELAY_MINUTES


def test_delay_is_capped_at_the_maximum() -> None:
    decision = decide(
        window(6000, 100), deviation_threshold=THRESHOLD, slot_minutes=SLOT_MINUTES, max_delay_minutes=60
    )
    assert decision.delay_minutes == 60


def test_delay_rounds_up_to_whole_slots() -> None:
    """Passes must land on windows that exist, and under-correcting means
    reslotting again in five minutes and buzzing the same person twice."""
    assert round_up_to_slot(1, 30) == 30
    assert round_up_to_slot(30, 30) == 30
    assert round_up_to_slot(31, 30) == 60
    assert round_up_to_slot(0, 30) == 0
    assert round_up_to_slot(-5, 30) == 0


def test_round_up_rejects_a_zero_slot_length() -> None:
    with pytest.raises(ValueError):
        round_up_to_slot(10, 0)


# --- downstream selection --------------------------------------------------
def test_only_slots_that_have_not_started_are_movable() -> None:
    """Someone already queueing for their slot is in the building."""
    now = datetime(2026, 7, 15, 14, 0)
    assert is_downstream(now + timedelta(minutes=1), now)
    assert not is_downstream(now, now)
    assert not is_downstream(now - timedelta(minutes=1), now)


def test_shifted_start_adds_the_delay() -> None:
    start = datetime(2026, 7, 15, 15, 30)
    assert shifted_start(start, 30) == datetime(2026, 7, 15, 16, 0)


# --- the message the pilgrim actually reads --------------------------------
def test_notification_is_marathi_first_and_explains_why() -> None:
    mr, en = notification_text(
        original_start=datetime(2026, 7, 15, 15, 30),
        new_start=datetime(2026, 7, 15, 16, 0),
        reference="WVACDEFGH",
    )
    assert "15:30" in mr and "16:00" in mr
    assert "WVACDEFGH" in mr
    # A time that changes with no reason reads as an unreliable system.
    assert "हळू" in mr  # "slow"
    assert "15:30" in en and "16:00" in en
    assert "slower than planned" in en


def test_decision_exposes_a_timedelta() -> None:
    decision = ReslotDecision(True, 45, -0.5, "test")
    assert decision.delay == timedelta(minutes=45)
