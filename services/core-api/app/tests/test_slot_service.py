"""Slot planning and capacity arithmetic — pure unit tests.

This is the code that decides whether a pilgrim gets darshan. It gets tested
like it matters.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from itertools import pairwise
from typing import TypedDict

import pytest

from app.services.slot_service import (
    MAX_GROUP_SIZE,
    SlotPlan,
    SlotState,
    available_seats,
    can_accommodate,
    estimate_wait,
    pass_expiry,
    plan_day,
    slot_capacity,
    validate_group_size,
    walkin_available,
    walkin_reserve_for,
)


class DayArgs(TypedDict):
    throughput_per_hour: int
    slot_minutes: int
    day_start: time
    day_end: time
    reserve_pct: float


DEFAULTS: DayArgs = {
    "throughput_per_hour": 6000,
    "slot_minutes": 30,
    "day_start": time(4, 0),
    "day_end": time(23, 0),
    "reserve_pct": 0.25,
}


def with_day_end(day_end: time) -> DayArgs:
    args: DayArgs = {**DEFAULTS}
    args["day_end"] = day_end
    return args


# --- day layout ------------------------------------------------------------
def test_default_day_is_thirty_eight_half_hour_slots() -> None:
    # 04:00 to 23:00 is 19 hours; 38 half-hour windows.
    plans = plan_day(**DEFAULTS)
    assert len(plans) == 38
    assert plans[0].start == time(4, 0)
    assert plans[-1].end == time(23, 0)


def test_slots_are_contiguous_and_non_overlapping() -> None:
    plans = plan_day(**DEFAULTS)
    for earlier, later in pairwise(plans):
        assert earlier.end == later.start


def test_partial_trailing_window_is_dropped_not_shortened() -> None:
    """A 12-minute window with a full slot's capacity would oversubscribe."""
    plans = plan_day(**with_day_end(time(22, 42)))
    assert plans[-1].end == time(22, 30)
    assert all(
        (datetime.combine(datetime.today(), p.end) - datetime.combine(datetime.today(), p.start))
        == timedelta(minutes=30)
        for p in plans
    )


def test_day_end_before_day_start_is_rejected() -> None:
    with pytest.raises(ValueError, match="day_end"):
        plan_day(**with_day_end(time(3, 0)))


# --- capacity --------------------------------------------------------------
def test_slot_capacity_follows_throughput() -> None:
    assert slot_capacity(6000, 30) == 3000
    assert slot_capacity(6000, 60) == 6000
    assert slot_capacity(0, 30) == 0


def test_slot_capacity_rejects_nonsense() -> None:
    with pytest.raises(ValueError):
        slot_capacity(6000, 0)
    with pytest.raises(ValueError):
        slot_capacity(-1, 30)


# --- the walk-in reserve (Section 5, E1) -----------------------------------
def test_walkin_reserve_is_twenty_five_percent_by_default() -> None:
    plans = plan_day(**DEFAULTS)
    assert plans[0].capacity == 3000
    assert plans[0].walkin_reserve == 750
    assert plans[0].bookable == 2250


def test_walkin_reserve_rounds_up_in_favour_of_walk_ins() -> None:
    """When the split is uneven the spare seat goes to the pilgrim without a
    smartphone. That is the entire point of the reserve."""
    assert walkin_reserve_for(101, 0.25) == 26  # not 25
    assert walkin_reserve_for(3, 0.25) == 1
    assert walkin_reserve_for(1, 0.25) == 1


def test_walkin_reserve_never_exceeds_capacity() -> None:
    assert walkin_reserve_for(10, 1.0) == 10
    assert walkin_reserve_for(0, 0.25) == 0


def test_walkin_reserve_pct_must_be_a_fraction() -> None:
    for bad in (-0.1, 1.5):
        with pytest.raises(ValueError):
            walkin_reserve_for(100, bad)


def test_reserved_seats_are_not_offered_online_even_when_slot_is_empty() -> None:
    slot = SlotState(capacity=3000, booked_count=0, walkin_reserve=750, status="open")
    assert available_seats(slot) == 2250


def test_slot_is_full_online_while_walk_in_seats_remain() -> None:
    slot = SlotState(capacity=100, booked_count=75, walkin_reserve=25, status="open")
    assert available_seats(slot) == 0
    assert walkin_available(slot) == 25  # the gate can still admit 25 walk-ins


def test_walkin_available_tracks_usage() -> None:
    slot = SlotState(capacity=100, booked_count=75, walkin_reserve=25, status="open", walkin_used=10)
    assert walkin_available(slot) == 15


# --- availability ----------------------------------------------------------
@pytest.mark.parametrize("status", ["closed", "full", "completed"])
def test_a_non_open_slot_offers_nothing(status: str) -> None:
    slot = SlotState(capacity=3000, booked_count=0, walkin_reserve=750, status=status)
    assert available_seats(slot) == 0
    assert not can_accommodate(slot, 1)


def test_availability_never_goes_negative() -> None:
    # Defensive: an over-booked row must read as zero, not as a negative offer.
    slot = SlotState(capacity=100, booked_count=200, walkin_reserve=25, status="open")
    assert available_seats(slot) == 0


def test_group_must_fit_entirely() -> None:
    slot = SlotState(capacity=100, booked_count=71, walkin_reserve=25, status="open")
    assert available_seats(slot) == 4
    assert can_accommodate(slot, 4)
    assert not can_accommodate(slot, 5)


# --- group size ------------------------------------------------------------
def test_group_size_bounds() -> None:
    assert MAX_GROUP_SIZE == 6
    for size in range(1, 7):
        validate_group_size(size)
    for bad in (0, -1, 7, 100):
        with pytest.raises(ValueError):
            validate_group_size(bad)


# --- honest wait estimate --------------------------------------------------
def test_wait_is_the_slot_time_when_the_queue_is_clear() -> None:
    now = datetime(2026, 7, 15, 10, 0)
    wait = estimate_wait(
        now=now, slot_start=now + timedelta(hours=2), queue_ahead=0, throughput_per_hour=6000
    )
    assert wait == timedelta(hours=2)


def test_wait_reflects_the_queue_when_the_queue_is_the_binding_constraint() -> None:
    """36,000 people ahead at 6,000/hour is six hours, whatever the slot says."""
    now = datetime(2026, 7, 15, 10, 0)
    wait = estimate_wait(
        now=now, slot_start=now + timedelta(hours=2), queue_ahead=36_000, throughput_per_hour=6000
    )
    assert wait == timedelta(hours=6)


def test_wait_for_a_slot_already_open_is_driven_by_the_queue() -> None:
    now = datetime(2026, 7, 15, 10, 0)
    wait = estimate_wait(
        now=now, slot_start=now - timedelta(hours=1), queue_ahead=3000, throughput_per_hour=6000
    )
    assert wait == timedelta(minutes=30)


def test_stopped_gate_falls_back_to_the_slot_time() -> None:
    now = datetime(2026, 7, 15, 10, 0)
    wait = estimate_wait(
        now=now, slot_start=now + timedelta(hours=1), queue_ahead=5000, throughput_per_hour=0
    )
    assert wait == timedelta(hours=1)


# --- expiry ----------------------------------------------------------------
def test_pass_expires_forty_five_minutes_after_its_slot() -> None:
    end = datetime(2026, 7, 15, 15, 30)
    assert pass_expiry(end, 45) == datetime(2026, 7, 15, 16, 15)


def test_slot_plan_bookable_never_negative() -> None:
    plan = SlotPlan(start=time(4, 0), end=time(4, 30), capacity=10, walkin_reserve=20)
    assert plan.bookable == 0
