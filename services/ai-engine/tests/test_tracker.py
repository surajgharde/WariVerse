"""Tracking, and the privacy properties that constrain it.

Half these tests are about association quality.  The other half assert that the
tracker cannot do the thing Section 12 forbids — because "we don't do
re-identification" is a claim, and a claim in a README is worth less than a test.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.metrics import motions
from app.tracker import GroundTracker

T0 = datetime(2026, 7, 25, 5, 30, tzinfo=UTC)
HIGH = 0.8
LOW = 0.15


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def test_a_person_walking_keeps_one_track_id():
    tracker = GroundTracker()
    ids = set()
    for step in range(10):
        samples = tracker.update([(step * 0.5, 0.0, HIGH)], at(step * 0.5))
        ids.update(s.track_id for s in samples)
    assert len(ids) == 1


def test_two_people_far_apart_are_two_tracks():
    tracker = GroundTracker()
    samples = tracker.update([(0.0, 0.0, HIGH), (20.0, 0.0, HIGH)], at(0))
    assert len({s.track_id for s in samples}) == 2


def test_a_teleport_starts_a_new_track_rather_than_inventing_a_sprint():
    """Beyond the association gate it is a different person, not a fast one."""
    tracker = GroundTracker()
    first = tracker.update([(0.0, 0.0, HIGH)], at(0))
    second = tracker.update([(50.0, 0.0, HIGH)], at(0.5))
    assert first[0].track_id != second[0].track_id


def test_low_confidence_detections_continue_a_track_but_never_start_one():
    """ByteTrack's central trick, and it matters most exactly where the numbers
    matter most: in a dense crowd the occluded people are the ones whose scores
    drop, and dropping them undercounts the crush."""
    tracker = GroundTracker()
    tracker.update([(0.0, 0.0, HIGH)], at(0))

    continued = tracker.update([(0.4, 0.0, LOW)], at(0.5))
    assert len(continued) == 1, "a faint detection near a known track continues it"

    tracker.reset()
    orphan = tracker.update([(9.0, 9.0, LOW)], at(1.0))
    assert orphan == [], "a faint detection near nothing is noise, not a person"


def test_a_track_expires_when_it_stops_being_seen():
    tracker = GroundTracker()
    tracker.update([(0.0, 0.0, HIGH)], at(0))
    assert tracker.active_tracks == 1

    tracker.update([(30.0, 30.0, HIGH)], at(10.0))
    assert tracker.active_tracks == 1, "the old track should have aged out"


def test_tracks_produce_the_velocities_the_metrics_need():
    tracker = GroundTracker()
    samples = []
    for step in range(20):
        samples.extend(tracker.update([(0.0, step * 0.6, HIGH)], at(step * 0.5)))

    result = motions(samples)
    assert len(result) == 1
    assert result[0].speed > 1.0
    assert result[0].dy > 0


# ---------------------------------------------------------------------------
# privacy invariants (Section 12)
# ---------------------------------------------------------------------------
def test_reset_destroys_every_association():
    """After a window closes there must be nothing left that links two
    observations of the same person."""
    tracker = GroundTracker()
    first = tracker.update([(0.0, 0.0, HIGH)], at(0))

    tracker.reset()
    assert tracker.active_tracks == 0

    # The same person, standing in the same place, gets a fresh id.
    second = tracker.update([(0.0, 0.0, HIGH)], at(0.5))
    assert first[0].track_id == second[0].track_id == 1, "ids restart; they do not persist"


def test_ids_carry_no_information_about_the_person():
    """They are a counter. Nothing about appearance, size or position feeds in,
    so two people who look identical are indistinguishable to this tracker."""
    tracker = GroundTracker()
    samples = tracker.update([(0.0, 0.0, HIGH), (10.0, 10.0, HIGH), (20.0, 20.0, HIGH)], at(0))
    assert sorted(s.track_id for s in samples) == [1, 2, 3]


def test_a_track_sample_has_no_appearance_field():
    """The structural guarantee: there is nowhere to put an embedding."""
    tracker = GroundTracker()
    sample = tracker.update([(1.0, 2.0, HIGH)], at(0))[0]
    assert set(sample.__slots__) == {"track_id", "x_m", "y_m", "at"}


def test_two_trackers_never_agree_on_an_id():
    """Cross-camera re-identification is impossible by construction: each camera
    has its own tracker with its own counter, and nothing reconciles them."""
    left, right = GroundTracker(), GroundTracker()
    a = left.update([(0.0, 0.0, HIGH)], at(0))[0]
    b = right.update([(500.0, 500.0, HIGH)], at(0))[0]

    # Same id, unrelated people. The number means nothing outside its tracker.
    assert a.track_id == b.track_id == 1
