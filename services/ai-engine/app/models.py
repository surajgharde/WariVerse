"""Domain types for the pipeline.

Read `TrackSample` and note what it does *not* have: a name, a face, a
descriptor, an appearance embedding.  It has a temporary integer, a position in
metres and a timestamp.  The integer exists so two consecutive frames can be
linked into a velocity, and it is discarded the moment the window closes
(Section 4/M2 step 3, Section 12).

`ZoneObservation` is the only thing that leaves this process.  It is a count and
four ratios.  There is nowhere in it to put a person.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.homography import Homography


@dataclass(frozen=True, slots=True)
class CameraSpec:
    camera_id: str
    name: str
    stream_url: str | None
    homography: Homography | None
    is_tripwire_enabled: bool = False

    @property
    def is_calibrated(self) -> bool:
        return self.homography is not None

    @classmethod
    def from_config(cls, raw: dict[str, Any]) -> CameraSpec:
        matrix = raw.get("homography")
        return cls(
            camera_id=str(raw["camera_id"]),
            name=str(raw["name"]),
            stream_url=raw.get("stream_url"),
            homography=Homography.from_list(matrix) if matrix else None,
            is_tripwire_enabled=bool(raw.get("is_tripwire_enabled", False)),
        )


@dataclass(frozen=True, slots=True)
class ZoneSpec:
    zone_id: str
    code: str
    name: str
    area_m2: float
    capacity_persons: int
    zone_type: str
    cameras: tuple[CameraSpec, ...] = ()

    @property
    def calibrated_cameras(self) -> tuple[CameraSpec, ...]:
        return tuple(c for c in self.cameras if c.is_calibrated)

    @classmethod
    def from_config(cls, raw: dict[str, Any]) -> ZoneSpec:
        return cls(
            zone_id=str(raw["zone_id"]),
            code=str(raw["code"]),
            name=str(raw["name"]),
            area_m2=float(raw["area_m2"]),
            capacity_persons=int(raw["capacity_persons"]),
            zone_type=str(raw["zone_type"]),
            cameras=tuple(CameraSpec.from_config(c) for c in raw.get("cameras", [])),
        )


@dataclass(frozen=True, slots=True)
class TrackSample:
    """One tracked point at one instant, already converted to ground metres.

    `track_id` is local to a single window in a single camera.  It is never
    published, never persisted and never matched against another camera —
    cross-camera re-identification is exactly the thing Section 12 forbids.
    """

    track_id: int
    x_m: float
    y_m: float
    at: datetime


@dataclass(frozen=True, slots=True)
class Detection:
    """One person-shaped box in image coordinates."""

    x1: float
    y1: float
    x2: float
    y2: float
    confidence: float

    @property
    def foot_point(self) -> tuple[float, float]:
        """Where this person is standing.

        The homography maps the *ground plane*, so the bottom-centre of the box
        is the point to project — projecting the centroid would place everyone
        about a metre further from the camera than they are, and in a dense
        crowd that error is the difference between 3.4 and 4.1 p/m².
        """
        return ((self.x1 + self.x2) / 2.0, self.y2)


@dataclass(frozen=True, slots=True)
class ZoneObservation:
    """One 10-second aggregate.  The unit of publication."""

    zone_id: str
    zone_code: str
    person_count: int
    density: float
    observed_at: datetime
    flow_dx: float = 0.0
    flow_dy: float = 0.0
    stagnation_index: float = 0.0
    counterflow_ratio: float = 0.0
    confidence: float = 1.0
    camera_count: int = 0
    #: Never published — kept in-process for the heat-map overlay only.
    heat_cells: tuple[tuple[float, float, float], ...] = field(default=(), repr=False)

    def to_payload(self) -> dict[str, Any]:
        """Exactly what goes over the wire.  `heat_cells` is not in it."""
        return {
            "zone_id": self.zone_id,
            "zone_code": self.zone_code,
            "person_count": self.person_count,
            "density": round(self.density, 4),
            "observed_at": self.observed_at.isoformat(),
            "flow_dx": round(self.flow_dx, 4),
            "flow_dy": round(self.flow_dy, 4),
            "stagnation_index": round(self.stagnation_index, 4),
            "counterflow_ratio": round(self.counterflow_ratio, 4),
            "confidence": round(self.confidence, 3),
            "camera_count": self.camera_count,
        }
