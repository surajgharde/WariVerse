"""Short-horizon tracking, for movement vectors only (Section 4/M2 step 3).

The spec says ByteTrack or DeepSORT.  DeepSORT is ruled out on privacy grounds
before performance is even considered: its association step is an *appearance
embedding*, which is a re-identification feature vector for a human being.
Section 12 forbids exactly that, and a system that promises no biometric
templates cannot ship one in its tracker.

So this is ByteTrack's idea without its baggage: greedy nearest-neighbour
association on ground-plane position, with a two-tier confidence pass, and no
appearance model of any kind.  Two people who look identical are indistinguishable
to it, which is the point.

Track ids live for at most a few seconds and never leave this process:

* they are integers from a counter, not derived from anything about a person;
* they reset when the tracker is reset, so the same pilgrim walking past the
  same camera twice gets unrelated numbers;
* `metrics.aggregate` consumes them and returns aggregates, and nothing
  downstream of it has ever seen one.

There is no code here that can answer "where did this person go", and there is
no store it could write the answer into.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from math import hypot

from app.models import TrackSample

#: A detection further than this from a track's last position is a different
#: person.  At 2 FPS a pilgrim covers at most ~1.5 m between frames even at a
#: brisk walk; 2.5 m allows for jitter without gluing neighbours together.
MAX_ASSOCIATION_DISTANCE_M = 2.5

#: Detections below the model's main threshold still get a second pass against
#: *existing* tracks (this is ByteTrack's core trick) — in a dense crowd the
#: partially-occluded people are exactly the ones whose scores drop, and
#: dropping them systematically undercounts the crowds that matter most.
LOW_CONFIDENCE_FLOOR = 0.10

#: A track unseen for this long is gone.  Short on purpose: this tracker exists
#: to make velocities, not to follow anyone.
TRACK_TIMEOUT = timedelta(seconds=3)


@dataclass(slots=True)
class _Track:
    track_id: int
    x_m: float
    y_m: float
    last_seen: datetime
    hits: int = 1
    samples: list[TrackSample] = field(default_factory=list)


class GroundTracker:
    """Associates ground-plane points across frames.  Ids are disposable."""

    def __init__(
        self,
        *,
        max_distance_m: float = MAX_ASSOCIATION_DISTANCE_M,
        timeout: timedelta = TRACK_TIMEOUT,
    ) -> None:
        self.max_distance_m = max_distance_m
        self.timeout = timeout
        self._tracks: dict[int, _Track] = {}
        self._next_id = 1

    @property
    def active_tracks(self) -> int:
        return len(self._tracks)

    def update(
        self,
        points: list[tuple[float, float, float]],
        at: datetime,
    ) -> list[TrackSample]:
        """Feed one frame's ground points as (x_m, y_m, confidence).

        Returns the samples produced by this frame.  The caller accumulates them
        across a window and hands them to `metrics.aggregate`.
        """
        self._expire(at)

        high = [(x, y) for x, y, c in points if c >= LOW_CONFIDENCE_FLOOR * 2]
        low = [(x, y) for x, y, c in points if LOW_CONFIDENCE_FLOOR <= c < LOW_CONFIDENCE_FLOOR * 2]

        produced: list[TrackSample] = []
        unmatched = self._associate(high, at, produced)

        # Second pass: low-confidence detections may continue an existing track
        # but may never start one.  A faint blob that is not near anything we
        # were already following is noise.
        self._associate(low, at, produced, allow_new=False)

        # Whatever high-confidence detections are left are new people.
        for x, y in unmatched:
            track = _Track(track_id=self._next_id, x_m=x, y_m=y, last_seen=at)
            self._next_id += 1
            self._tracks[track.track_id] = track
            produced.append(TrackSample(track_id=track.track_id, x_m=x, y_m=y, at=at))

        return produced

    def _associate(
        self,
        points: list[tuple[float, float]],
        at: datetime,
        produced: list[TrackSample],
        *,
        allow_new: bool = True,
    ) -> list[tuple[float, float]]:
        """Greedy nearest-neighbour.

        Greedy rather than Hungarian: with a hard distance gate the difference in
        a crowd is negligible, and an O(n²) pass over 800 detections at 2 FPS is
        a few milliseconds where an optimal assignment is not.
        """
        available = {tid: t for tid, t in self._tracks.items() if t.last_seen < at}
        leftover: list[tuple[float, float]] = []

        pairs: list[tuple[float, int, int]] = []
        for index, (x, y) in enumerate(points):
            for tid, track in available.items():
                distance = hypot(x - track.x_m, y - track.y_m)
                if distance <= self.max_distance_m:
                    pairs.append((distance, index, tid))
        pairs.sort()

        used_points: set[int] = set()
        used_tracks: set[int] = set()
        for _distance, index, tid in pairs:
            if index in used_points or tid in used_tracks:
                continue
            used_points.add(index)
            used_tracks.add(tid)
            x, y = points[index]
            track = self._tracks[tid]
            track.x_m, track.y_m, track.last_seen = x, y, at
            track.hits += 1
            produced.append(TrackSample(track_id=tid, x_m=x, y_m=y, at=at))

        for index, point in enumerate(points):
            if index not in used_points:
                leftover.append(point)

        return leftover if allow_new else []

    def _expire(self, now: datetime) -> None:
        cutoff = now - self.timeout
        stale = [tid for tid, track in self._tracks.items() if track.last_seen < cutoff]
        for tid in stale:
            del self._tracks[tid]

    def reset(self) -> None:
        """Discard every track id.

        Called at the end of each window by the pipeline.  After this returns,
        the association between any two observations of the same person is gone
        from the process — which is the whole reason the reset exists.
        """
        self._tracks.clear()
        self._next_id = 1
