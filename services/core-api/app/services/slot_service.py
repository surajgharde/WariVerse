"""Slot planning and capacity arithmetic (Section 4/M1).

Pure functions.  No database, no clock, no I/O — every input is an argument, so
the arithmetic that decides whether a pilgrim gets a darshan slot can be tested
exhaustively without a running stack.

The one rule that shapes all of this: `walkin_reserve` is subtracted *before*
anything is offered online.  A pilgrim who walks to Pandharpur without a
smartphone must not be locked out of darshan by someone with one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

MAX_GROUP_SIZE = 6


@dataclass(frozen=True, slots=True)
class SlotPlan:
    """One 30-minute darshan window, before it exists in the database."""

    start: time
    end: time
    capacity: int
    walkin_reserve: int

    @property
    def bookable(self) -> int:
        return max(0, self.capacity - self.walkin_reserve)


@dataclass(frozen=True, slots=True)
class SlotState:
    """A slot as it currently stands, for availability arithmetic."""

    capacity: int
    booked_count: int
    walkin_reserve: int
    status: str
    walkin_used: int = 0


def slot_capacity(throughput_per_hour: int, slot_minutes: int) -> int:
    """How many pilgrims one slot can absorb at the planned throughput."""
    if throughput_per_hour < 0 or slot_minutes <= 0:
        raise ValueError("throughput_per_hour must be >= 0 and slot_minutes > 0")
    return int(throughput_per_hour * slot_minutes / 60)


def walkin_reserve_for(capacity: int, reserve_pct: float) -> int:
    """Seats held back from online booking.

    Rounded *up*.  When the split is uneven the extra seat goes to the walk-in
    pilgrim, not the app user — that is the whole point of the reserve.
    """
    if not 0.0 <= reserve_pct <= 1.0:
        raise ValueError("reserve_pct must be between 0 and 1")
    return min(capacity, math.ceil(capacity * reserve_pct))


def plan_day(
    *,
    throughput_per_hour: int,
    slot_minutes: int,
    day_start: time,
    day_end: time,
    reserve_pct: float,
) -> list[SlotPlan]:
    """Lay out a full day of slots between `day_start` and `day_end`.

    The final partial window is dropped rather than shortened: a 12-minute slot
    with a full slot's capacity would oversubscribe the gate.
    """
    if slot_minutes <= 0:
        raise ValueError("slot_minutes must be positive")
    if day_end <= day_start:
        raise ValueError("day_end must be after day_start")

    capacity = slot_capacity(throughput_per_hour, slot_minutes)
    reserve = walkin_reserve_for(capacity, reserve_pct)

    anchor = date(2000, 1, 1)  # arbitrary; only the time-of-day matters
    cursor = datetime.combine(anchor, day_start)
    closing = datetime.combine(anchor, day_end)
    step = timedelta(minutes=slot_minutes)

    plans: list[SlotPlan] = []
    while cursor + step <= closing:
        end = cursor + step
        plans.append(
            SlotPlan(
                start=cursor.time(),
                end=end.time(),
                capacity=capacity,
                walkin_reserve=reserve,
            )
        )
        cursor = end
    return plans


def available_seats(slot: SlotState) -> int:
    """Seats a pilgrim can book online right now."""
    if slot.status != "open":
        return 0
    return max(0, slot.capacity - slot.booked_count - slot.walkin_reserve)


def can_accommodate(slot: SlotState, group_size: int) -> bool:
    validate_group_size(group_size)
    return available_seats(slot) >= group_size


def validate_group_size(group_size: int) -> None:
    if group_size < 1:
        raise ValueError("group_size must be at least 1")
    if group_size > MAX_GROUP_SIZE:
        raise ValueError(f"group_size must not exceed {MAX_GROUP_SIZE}")


def walkin_available(slot: SlotState) -> int:
    """Reserved seats still unused at the gate."""
    return max(0, slot.walkin_reserve - slot.walkin_used)


def slot_window(day: date, slot: SlotPlan) -> tuple[datetime, datetime]:
    """Absolute start/end for a slot on a given date."""
    return datetime.combine(day, slot.start), datetime.combine(day, slot.end)


def estimate_wait(
    *,
    now: datetime,
    slot_start: datetime,
    queue_ahead: int,
    throughput_per_hour: int,
) -> timedelta:
    """Honest wait estimate for a pilgrim holding this slot.

    Whichever is *later*: the time until their slot opens, or the time it takes
    to clear the people already ahead of them.  Reporting only the slot time
    would be a comfortable lie when the queue is running behind.
    """
    until_slot = max(timedelta(0), slot_start - now)
    if throughput_per_hour <= 0:
        return until_slot
    clearing = timedelta(hours=queue_ahead / throughput_per_hour)
    return max(until_slot, clearing)


def pass_expiry(slot_end: datetime, grace_minutes: int) -> datetime:
    """When an unscanned pass stops being valid and its seats go back."""
    return slot_end + timedelta(minutes=grace_minutes)
