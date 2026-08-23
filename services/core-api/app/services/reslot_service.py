"""Dynamic reslotting arithmetic (Section 4/M1).

Pure functions.  When the gate runs slower than planned, downstream passes are
pushed back so the promised time stays honest; a pass that says 15:30 and means
18:00 is worse than no pass at all.

Two rules shape everything here:

1. **Never move a pass earlier without opt-in.** People travel. An earlier slot
   they cannot reach is a false promise that costs them their place.
2. **Round the shift up to whole slots.** Passes must land on real windows, and
   under-correcting means reslotting again in five minutes, buzzing the same
   pilgrim twice.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

#: A stalled gate would divide by ~zero; cap the computed delay instead of
#: telling a pilgrim their slot moved by eleven hours.
MAX_DELAY_MINUTES = 180


@dataclass(frozen=True, slots=True)
class ThroughputWindow:
    """What the gate was supposed to do versus what it did."""

    planned: int
    actual: int
    minutes: int

    def __post_init__(self) -> None:
        if self.minutes <= 0:
            raise ValueError("window minutes must be positive")
        if self.planned < 0 or self.actual < 0:
            raise ValueError("throughput counts cannot be negative")

    @property
    def deficit(self) -> int:
        """People who should have passed through by now and have not."""
        return max(0, self.planned - self.actual)

    @property
    def actual_rate_per_minute(self) -> float:
        return self.actual / self.minutes


@dataclass(frozen=True, slots=True)
class ReslotDecision:
    should_reslot: bool
    delay_minutes: int
    deviation: float
    reason: str

    @property
    def delay(self) -> timedelta:
        return timedelta(minutes=self.delay_minutes)


def deviation(window: ThroughputWindow) -> float:
    """Signed fraction: -0.4 means the gate ran 40% below plan.

    A window with nothing planned has nothing to deviate from.
    """
    if window.planned == 0:
        return 0.0
    return (window.actual - window.planned) / window.planned


def raw_delay_minutes(window: ThroughputWindow) -> float:
    """Minutes needed to clear the backlog at the rate actually observed."""
    if window.deficit == 0:
        return 0.0
    rate = window.actual_rate_per_minute
    if rate <= 0:
        # The gate is stopped.  There is no rate to extrapolate from, so the
        # cap is the only honest answer available.
        return float(MAX_DELAY_MINUTES)
    return window.deficit / rate


def round_up_to_slot(minutes: float, slot_minutes: int) -> int:
    if slot_minutes <= 0:
        raise ValueError("slot_minutes must be positive")
    if minutes <= 0:
        return 0
    return int(math.ceil(minutes / slot_minutes) * slot_minutes)


def decide(
    window: ThroughputWindow,
    *,
    deviation_threshold: float,
    slot_minutes: int,
    max_delay_minutes: int = MAX_DELAY_MINUTES,
) -> ReslotDecision:
    """Should downstream passes move, and by how much?

    Only *shortfalls* trigger a reslot.  A gate running faster than planned is
    good news, and pulling passes earlier without consent is rule 1's whole
    point — the surplus simply absorbs walk-ins and late arrivals.
    """
    dev = deviation(window)

    if window.planned == 0:
        return ReslotDecision(False, 0, dev, "no throughput planned for this window")

    if dev >= 0:
        return ReslotDecision(False, 0, dev, "gate is at or above plan")

    if abs(dev) <= deviation_threshold:
        return ReslotDecision(
            False, 0, dev, f"shortfall {abs(dev):.0%} within {deviation_threshold:.0%} tolerance"
        )

    delay = round_up_to_slot(raw_delay_minutes(window), slot_minutes)
    delay = min(delay, max_delay_minutes)

    if delay == 0:
        return ReslotDecision(False, 0, dev, "computed delay rounds to zero")

    return ReslotDecision(
        True,
        delay,
        dev,
        f"throughput {abs(dev):.0%} below plan; pushing downstream passes by {delay} minutes",
    )


def shifted_start(original_start: datetime, delay_minutes: int) -> datetime:
    return original_start + timedelta(minutes=delay_minutes)


def is_downstream(slot_start: datetime, now: datetime) -> bool:
    """Only slots that have not begun may be moved.

    A pilgrim already standing in the queue for their slot is not reslotted —
    they are in the building.
    """
    return slot_start > now


def notification_text(
    *,
    original_start: datetime,
    new_start: datetime,
    reference: str,
) -> tuple[str, str]:
    """(Marathi, English) message for the reslot outbox.

    Marathi first, and it says *why* — a time that changes without explanation
    reads as the system being unreliable rather than being honest.
    """
    old = original_start.strftime("%H:%M")
    new = new_start.strftime("%H:%M")
    mr = (
        f"तुमच्या दर्शन पासची वेळ बदलली आहे. पास {reference}: "
        f"{old} ऐवजी आता {new}. रांग सध्या हळू चालत असल्याने ही वेळ पुढे ढकलली आहे. "
        f"कृपया नवीन वेळेवर या."
    )
    en = (
        f"Your darshan slot has moved. Pass {reference}: now {new} instead of {old}. "
        f"The queue is running slower than planned, so we have pushed your time back. "
        f"Please come at the new time."
    )
    return mr, en
