"""Turning tracked points into the five numbers a zone reports.

Section 4/M2 step 4, per zone, per 10-second window:

    person_count       how many people
    density            person_count / zone.area_m2
    flow_vector        dominant direction and magnitude, in m/s
    stagnation_index   share of tracks barely moving, over 60s
    counter-flow ratio share of movement opposing the dominant direction

The last two are the ones that matter and the ones most systems skip.  A crush
does not begin when a crowd gets dense; it begins when a dense crowd *stops*, or
when two streams start pushing through each other.  Density alone would have
called Hillsborough safe until it was not.

`aggregate` deletes its track ids on the way out.  That is not tidiness — it is
the design: after this function returns there is no object anywhere in the
process that could be used to follow one person across two windows.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import atan2, cos, hypot, radians, sin
from statistics import median

from app.models import TrackSample

#: Below this a track counts as "not moving" for the stagnation index.  A person
#: shuffling forward in a darshan queue does about 0.2 m/s; standing still with
#: detection jitter looks like 0.05.  0.10 sits between them.
STAGNANT_SPEED_MS = 0.10

#: A displacement smaller than this over the whole window is detector noise, not
#: movement, and must not be given a direction.
MIN_TRACK_DISPLACEMENT_M = 0.15

#: Velocities faster than this are a tracker identity swap, not a pilgrim.  A
#: sprinting adult is about 6 m/s; a crowd never is.
MAX_PLAUSIBLE_SPEED_MS = 6.0

#: How aligned movement must be before we call it an axis at all.  Below this
#: the crowd is milling — people on a ghat wandering in every direction — which
#: is not two colliding streams and must not raise a turbulence alert.
MIN_AXIS_COHERENCE = 0.30


@dataclass(frozen=True, slots=True)
class TrackMotion:
    """One track's net movement across the window, in metres."""

    dx: float
    dy: float
    seconds: float

    @property
    def speed(self) -> float:
        return hypot(self.dx, self.dy) / self.seconds if self.seconds > 0 else 0.0

    @property
    def displacement(self) -> float:
        return hypot(self.dx, self.dy)

    @property
    def is_stagnant(self) -> bool:
        return self.speed < STAGNANT_SPEED_MS

    @property
    def is_plausible(self) -> bool:
        return self.speed <= MAX_PLAUSIBLE_SPEED_MS


@dataclass(frozen=True, slots=True)
class WindowMetrics:
    person_count: int
    flow_dx: float
    flow_dy: float
    stagnation_index: float
    counterflow_ratio: float
    tracks_considered: int
    moving_tracks: int

    @property
    def flow_speed(self) -> float:
        return hypot(self.flow_dx, self.flow_dy)

    def density(self, area_m2: float) -> float:
        return self.person_count / area_m2 if area_m2 > 0 else 0.0


EMPTY = WindowMetrics(
    person_count=0,
    flow_dx=0.0,
    flow_dy=0.0,
    stagnation_index=0.0,
    counterflow_ratio=0.0,
    tracks_considered=0,
    moving_tracks=0,
)


def motions(samples: Iterable[TrackSample]) -> list[TrackMotion]:
    """Collapse each track's samples into one net displacement.

    First-to-last rather than a sum of per-frame steps: at 2 FPS the per-frame
    steps are dominated by box jitter, and summing them turns a person standing
    still into a person walking in circles at 0.3 m/s.
    """
    by_track: dict[int, list[TrackSample]] = defaultdict(list)
    for sample in samples:
        by_track[sample.track_id].append(sample)

    result: list[TrackMotion] = []
    for points in by_track.values():
        if len(points) < 2:
            # Seen once: it is a count, but it has no velocity to contribute.
            continue
        points.sort(key=lambda s: s.at)
        first, last = points[0], points[-1]
        seconds = (last.at - first.at).total_seconds()
        if seconds <= 0:
            continue
        motion = TrackMotion(dx=last.x_m - first.x_m, dy=last.y_m - first.y_m, seconds=seconds)
        if motion.is_plausible:
            result.append(motion)
    return result


def dominant_axis(moving: Sequence[TrackMotion]) -> tuple[float, float] | None:
    """The line the crowd is moving along, or None if it has no line.

    Averaging unit *vectors* would be the obvious thing and it is wrong here, in
    a way that matters: a corridor with fifty people going north and fifty going
    south sums to exactly zero, which reads as "no dominant direction" and
    therefore as zero counter-flow.  That is the single most turbulent state a
    corridor can be in, and vector averaging reports it as calm.

    So this averages *doubled* angles — the standard treatment for axial data.
    Headings 180° apart double to the same angle and reinforce instead of
    cancelling, which recovers the north-south axis from the balanced case.
    Genuinely scattered movement still cancels, because that is what milling on
    a ghat actually is.

    Returns an unsigned axis; `aggregate` picks which way along it is "with the
    flow" by counting.
    """
    if not moving:
        return None

    sx = sy = 0.0
    counted = 0
    for motion in moving:
        length = motion.displacement
        if length <= 0:
            continue
        theta = atan2(motion.dy, motion.dx)
        sx += cos(2.0 * theta)
        sy += sin(2.0 * theta)
        counted += 1

    if counted == 0 or hypot(sx, sy) / counted < MIN_AXIS_COHERENCE:
        return None

    axis = atan2(sy, sx) / 2.0
    return (cos(axis), sin(axis))


def dominant_direction(moving: Sequence[TrackMotion]) -> tuple[float, float]:
    """Signed heading: the axis, pointed the way most of the crowd is going."""
    axis = dominant_axis(moving)
    if axis is None:
        return (0.0, 0.0)
    ux, uy = axis
    with_axis = sum(1 for m in moving if m.displacement > 0 and (m.dx * ux + m.dy * uy) > 0)
    return (ux, uy) if with_axis * 2 >= len(moving) else (-ux, -uy)


def aggregate(
    samples: Sequence[TrackSample],
    *,
    frame_counts: Sequence[int],
    history: Sequence[TrackMotion] = (),
) -> WindowMetrics:
    """Compute one window's metrics and forget who anybody was.

    `frame_counts` is the per-frame detection count across the window.  The
    published `person_count` is its **median**, not its max or its mean: one
    frame where the detector doubles a row of umbrellas should not become the
    number an operator closes a gate on.

    `history` is the previous minute's motions, because Section 4/M2 defines
    stagnation over 60 seconds while the window is 10 — a queue that stopped
    four seconds ago is not yet a stalled queue.
    """
    if not frame_counts:
        return EMPTY

    count = int(median(frame_counts))
    window_motions = motions(samples)
    considered = list(window_motions) + list(history)

    if not considered:
        return WindowMetrics(
            person_count=count,
            flow_dx=0.0,
            flow_dy=0.0,
            stagnation_index=0.0,
            counterflow_ratio=0.0,
            tracks_considered=0,
            moving_tracks=0,
        )

    stagnant = sum(1 for m in considered if m.is_stagnant)
    stagnation_index = stagnant / len(considered)

    # Direction and counter-flow use only this window: a minute-old heading is
    # not what the crowd is doing now.
    moving = [m for m in window_motions if m.displacement >= MIN_TRACK_DISPLACEMENT_M]
    axis = dominant_axis(moving)

    if not moving or axis is None:
        return WindowMetrics(
            person_count=count,
            flow_dx=0.0,
            flow_dy=0.0,
            stagnation_index=round(stagnation_index, 4),
            counterflow_ratio=0.0,
            tracks_considered=len(considered),
            moving_tracks=len(moving),
        )

    # Point the axis whichever way the majority is travelling; the minority are
    # the counter-flow.  A 50/50 split is maximally turbulent and reports 0.5.
    ax, ay = axis
    with_axis = [m for m in moving if (m.dx * ax + m.dy * ay) > 0]
    ux, uy = (ax, ay) if len(with_axis) * 2 >= len(moving) else (-ax, -ay)

    # Mean speed along the dominant axis.  A crowd whose members are moving fast
    # in opposing directions has a low net flow, which is the honest reading —
    # that is turbulence, not progress.
    speed = sum(m.speed * ((m.dx / m.displacement) * ux + (m.dy / m.displacement) * uy) for m in moving) / len(moving)

    # Opposing = more than 90° off the dominant heading.
    against = sum(1 for m in moving if ((m.dx / m.displacement) * ux + (m.dy / m.displacement) * uy) < 0.0)

    return WindowMetrics(
        person_count=count,
        flow_dx=round(ux * speed, 4),
        flow_dy=round(uy * speed, 4),
        stagnation_index=round(stagnation_index, 4),
        counterflow_ratio=round(against / len(moving), 4),
        tracks_considered=len(considered),
        moving_tracks=len(moving),
    )


def vector_from(bearing_degrees: float, speed: float) -> tuple[float, float]:
    """Compass bearing (0 = north) and speed to (dx east, dy north)."""
    theta = radians(bearing_degrees)
    return (speed * sin(theta), speed * cos(theta))


class MotionHistory:
    """A rolling minute of track motions, for the 60-second stagnation index.

    Bounded by time, not by count: at forty zones this holds a few thousand
    small dataclasses, and the alternative — a fixed-size deque — would give a
    busy zone a shorter memory than a quiet one, which is exactly backwards.
    """

    def __init__(self, window: timedelta) -> None:
        self._window = window
        self._entries: list[tuple[datetime, TrackMotion]] = []

    def extend(self, at: datetime, batch: Iterable[TrackMotion]) -> None:
        self._entries.extend((at, motion) for motion in batch)
        self.prune(at)

    def prune(self, now: datetime) -> None:
        cutoff = now - self._window
        if self._entries and self._entries[0][0] < cutoff:
            self._entries = [(t, m) for t, m in self._entries if t >= cutoff]

    def motions(self) -> list[TrackMotion]:
        return [m for _, m in self._entries]

    def __len__(self) -> int:
        return len(self._entries)
