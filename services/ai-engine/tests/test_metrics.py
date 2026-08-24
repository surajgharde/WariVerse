"""Window aggregation — the five numbers a zone reports.

The interesting cases are the ones where density is uninformative: a stalled
dense crowd, and two streams pushing through each other.  Those are the readings
that precede a crush, and they are what these tests are for.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.metrics import (
    EMPTY,
    STAGNANT_SPEED_MS,
    MotionHistory,
    TrackMotion,
    aggregate,
    dominant_axis,
    dominant_direction,
    motions,
    vector_from,
)
from app.models import TrackSample

T0 = datetime(2026, 7, 25, 5, 30, tzinfo=UTC)


def walk(track_id: int, start: tuple[float, float], velocity: tuple[float, float], frames: int = 20):
    """A track walking at a constant velocity, sampled at 2 FPS."""
    return [
        TrackSample(
            track_id=track_id,
            x_m=start[0] + velocity[0] * (i * 0.5),
            y_m=start[1] + velocity[1] * (i * 0.5),
            at=T0 + timedelta(seconds=i * 0.5),
        )
        for i in range(frames)
    ]


def standing(track_id: int, at: tuple[float, float], frames: int = 20):
    return walk(track_id, at, (0.0, 0.0), frames)


# ---------------------------------------------------------------------------
# counting
# ---------------------------------------------------------------------------
def test_no_frames_is_not_zero_people():
    """`EMPTY` is what "we did not measure" looks like. It is not a crowd of 0."""
    assert aggregate([], frame_counts=[]) is EMPTY


def test_person_count_uses_the_median_not_the_maximum():
    """One frame where the detector doubles a row of umbrellas must not become
    the number an operator closes a gate on."""
    counts = [40, 41, 39, 40, 96, 40, 41]
    result = aggregate([], frame_counts=counts)
    assert result.person_count == 40


def test_density_divides_by_the_zone_area():
    result = aggregate([], frame_counts=[2400])
    assert result.density(1200.0) == pytest.approx(2.0)
    assert result.density(0.0) == 0.0


# ---------------------------------------------------------------------------
# flow
# ---------------------------------------------------------------------------
def test_a_crowd_walking_north_reports_a_northward_flow():
    samples = [s for i in range(12) for s in walk(i, (float(i), 0.0), (0.0, 1.0))]
    result = aggregate(samples, frame_counts=[12] * 20)

    assert result.flow_dy > 0.9
    assert abs(result.flow_dx) < 0.1
    assert result.flow_speed == pytest.approx(1.0, abs=0.05)
    assert result.counterflow_ratio == 0.0


def test_one_fast_walker_does_not_redefine_where_the_queue_is_going():
    """Headings are weighted equally, not by speed.

    Otherwise one person jogging past a shuffling queue becomes its dominant
    direction, and everyone else becomes counter-flow.
    """
    slow_east = [TrackMotion(dx=0.5, dy=0.0, seconds=10.0) for _ in range(10)]
    fast_north = [TrackMotion(dx=0.0, dy=40.0, seconds=10.0)]

    ux, uy = dominant_direction(slow_east + fast_north)
    assert ux > uy, "ten people walking east outvote one running north"


def test_balanced_opposing_streams_still_have_an_axis():
    """The case plain vector averaging gets exactly backwards.

    Fifty north and fifty south sum to zero. Reporting that as "no dominant
    direction, no counter-flow" would call the most turbulent state in a
    corridor calm.
    """
    north = [TrackMotion(dx=0.0, dy=1.0, seconds=1.0) for _ in range(50)]
    south = [TrackMotion(dx=0.0, dy=-1.0, seconds=1.0) for _ in range(50)]

    axis = dominant_axis(north + south)
    assert axis is not None
    assert abs(axis[1]) > 0.95, "the axis is north-south"


def test_scattered_movement_has_no_axis():
    """People milling on a ghat are not two colliding streams."""
    scatter = [
        TrackMotion(dx=dx, dy=dy, seconds=1.0)
        for dx, dy in [(1, 0), (0, 1), (-1, 0), (0, -1), (0.7, 0.7), (-0.7, -0.7), (0.7, -0.7), (-0.7, 0.7)]
    ]
    assert dominant_axis(scatter) is None


def test_detector_jitter_does_not_become_movement():
    """Someone standing still with box jitter must not read as walking."""
    jitter = [
        TrackSample(track_id=1, x_m=0.02 * (i % 3), y_m=0.02 * ((i + 1) % 3), at=T0 + timedelta(seconds=i * 0.5))
        for i in range(20)
    ]
    result = aggregate(jitter, frame_counts=[1] * 20)

    assert result.flow_speed < STAGNANT_SPEED_MS
    assert result.stagnation_index == pytest.approx(1.0)


def test_impossible_velocities_are_discarded_as_tracker_errors():
    """An identity swap teleports a track. A pilgrim does not move at 40 m/s."""
    swapped = [
        TrackSample(track_id=1, x_m=0.0, y_m=0.0, at=T0),
        TrackSample(track_id=1, x_m=400.0, y_m=0.0, at=T0 + timedelta(seconds=10)),
    ]
    assert motions(swapped) == []


def test_a_track_seen_once_counts_but_has_no_velocity():
    single = [TrackSample(track_id=1, x_m=0.0, y_m=0.0, at=T0)]
    assert motions(single) == []


# ---------------------------------------------------------------------------
# the readings that matter
# ---------------------------------------------------------------------------
def test_a_stalled_crowd_reports_full_stagnation():
    """Section 4/M2: a dense crowd that has stopped moving is the precursor."""
    samples = [s for i in range(30) for s in standing(i, (float(i % 6), float(i // 6)))]
    result = aggregate(samples, frame_counts=[30] * 20)

    assert result.stagnation_index == pytest.approx(1.0)
    assert result.flow_speed == 0.0


def test_a_moving_crowd_reports_no_stagnation():
    samples = [s for i in range(20) for s in walk(i, (float(i), 0.0), (0.0, 1.2))]
    result = aggregate(samples, frame_counts=[20] * 20)
    assert result.stagnation_index == 0.0


def test_half_stopped_half_moving_reads_as_half_stagnant():
    moving = [s for i in range(10) for s in walk(i, (float(i), 0.0), (0.0, 1.0))]
    stopped = [s for i in range(10, 20) for s in standing(i, (float(i), 0.0))]
    result = aggregate(moving + stopped, frame_counts=[20] * 20)

    assert result.stagnation_index == pytest.approx(0.5, abs=0.05)


def test_opposing_streams_report_counterflow():
    """Turbulence: half the crowd walking into the other half."""
    north = [s for i in range(10) for s in walk(i, (float(i), 0.0), (0.0, 1.0))]
    south = [s for i in range(10, 20) for s in walk(i, (float(i), 20.0), (0.0, -1.0))]
    result = aggregate(north + south, frame_counts=[20] * 20)

    assert result.counterflow_ratio == pytest.approx(0.5, abs=0.05)


def test_turbulence_has_low_net_flow_even_though_everyone_is_moving():
    """The honest reading: fast movement in every direction is not progress."""
    north = [s for i in range(10) for s in walk(i, (float(i), 0.0), (0.0, 1.2))]
    south = [s for i in range(10, 20) for s in walk(i, (float(i), 20.0), (0.0, -1.2))]
    result = aggregate(north + south, frame_counts=[20] * 20)

    assert result.flow_speed < 0.2, "opposing streams cancel; net flow is near zero"
    assert result.stagnation_index == 0.0, "but nobody is actually standing still"


def test_a_zone_with_no_dominant_direction_reports_no_counterflow():
    """People milling on a ghat are not two colliding streams, and calling that
    turbulence would raise an alert every evening at Chandrabhaga."""
    headings = [(1, 0), (0, 1), (-1, 0), (0, -1), (0.7, 0.7), (-0.7, -0.7), (0.7, -0.7), (-0.7, 0.7)]
    scatter = []
    for i, (dx, dy) in enumerate(headings):
        scatter.extend(walk(i, (0.0, 0.0), (dx * 0.8, dy * 0.8)))
    result = aggregate(scatter, frame_counts=[8] * 20)

    assert result.flow_speed < 0.3
    assert result.counterflow_ratio <= 0.5


# ---------------------------------------------------------------------------
# the 60-second stagnation window
# ---------------------------------------------------------------------------
def test_stagnation_uses_a_minute_of_history_not_one_window():
    """A queue that stopped four seconds ago is not yet a stalled queue."""
    moving_now = [s for i in range(10) for s in walk(i, (float(i), 0.0), (0.0, 1.0))]
    stopped_earlier = [TrackMotion(dx=0.0, dy=0.0, seconds=10.0) for _ in range(30)]

    without = aggregate(moving_now, frame_counts=[10] * 20)
    with_history = aggregate(moving_now, frame_counts=[10] * 20, history=stopped_earlier)

    assert without.stagnation_index == 0.0
    assert with_history.stagnation_index > 0.7


def test_motion_history_forgets_past_its_window():
    history = MotionHistory(timedelta(seconds=60))
    history.extend(T0, [TrackMotion(dx=0.0, dy=0.0, seconds=10.0) for _ in range(5)])
    assert len(history) == 5

    history.extend(T0 + timedelta(seconds=90), [TrackMotion(dx=1.0, dy=0.0, seconds=10.0)])
    assert len(history) == 1, "the old minute should have aged out"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("bearing", "dx", "dy"),
    [(0.0, 0.0, 1.0), (90.0, 1.0, 0.0), (180.0, 0.0, -1.0), (270.0, -1.0, 0.0)],
)
def test_compass_bearings_convert_to_east_north_vectors(bearing, dx, dy):
    vx, vy = vector_from(bearing, 1.0)
    assert vx == pytest.approx(dx, abs=1e-9)
    assert vy == pytest.approx(dy, abs=1e-9)
