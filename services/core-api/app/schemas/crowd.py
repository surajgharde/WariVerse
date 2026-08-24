"""Zone, density, alert, camera and ingest schemas (Section 4/M2).

Two audiences, two shapes, and the split is a privacy decision rather than a
convenience one:

* `ZoneStatusPublic` is what a pilgrim's phone gets — a colour, a name, and how
  old the reading is.  No head counts, no flow vectors, nothing that describes
  where people are standing.
* `ZoneStatusDetail` is what the command centre gets, behind
  `crowd:view_detail`, and every number on it carries its own provenance.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from app.models.crowd import DensityLevel
from app.schemas.common import ApiModel

Point = tuple[float, float]


# ---------------------------------------------------------------------------
# zones
# ---------------------------------------------------------------------------
class ZoneOut(ApiModel):
    id: uuid.UUID
    code: str
    name: str
    name_mr: str
    zone_type: str
    area_m2: float
    capacity_persons: int
    is_active: bool
    parent_zone_id: uuid.UUID | None = None
    #: GeoJSON Polygon, ready for MapLibre without a conversion step.
    geometry: dict[str, Any] | None = None
    camera_count: int = 0
    calibrated_camera_count: int = 0


class ZoneUpdate(ApiModel):
    """Only the fields an operator legitimately re-surveys mid-Wari."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    name_mr: str | None = Field(default=None, min_length=1, max_length=160)
    area_m2: float | None = Field(default=None, gt=0, le=1_000_000)
    capacity_persons: int | None = Field(default=None, ge=0, le=1_000_000)
    is_active: bool | None = None
    reason: str = Field(min_length=3, max_length=500)


# ---------------------------------------------------------------------------
# density
# ---------------------------------------------------------------------------
class FlowOut(ApiModel):
    speed_ms: float
    direction: str | None = Field(default=None, description="Compass point, or null when barely moving")
    dx: float
    dy: float


class ZoneStatusDetail(ApiModel):
    """Full crowd state for one zone.  Requires `crowd:view_detail`."""

    zone_id: uuid.UUID
    zone_code: str
    zone_name: str
    zone_name_mr: str
    person_count: int
    density: float = Field(description="People per m², count divided by the surveyed zone area")
    level: DensityLevel
    occupancy_pct: float | None = None
    flow: FlowOut
    stagnation_index: float
    counterflow_ratio: float
    # Section 0 rule 3: no AI output is presented as certainty.
    confidence: float
    source: str
    camera_count: int
    observed_at: datetime
    age_seconds: float
    is_stale: bool
    area_m2: float
    notes: list[str] = Field(default_factory=list)


class ZoneStatusPublic(ApiModel):
    """What a pilgrim's phone is allowed to see.

    `level` and `advice_mr` only.  A head count per zone is a map of where the
    crowd is, and that is not a thing this system hands to the public.
    """

    zone_code: str
    zone_name: str
    zone_name_mr: str
    level: DensityLevel | None = Field(default=None, description="null means unknown, which is not the same as safe")
    advice: str
    advice_mr: str
    observed_at: datetime | None = None
    age_seconds: float | None = None
    is_stale: bool = True


class CrowdLive(ApiModel):
    zones: list[ZoneStatusDetail]
    unknown_zones: list[str] = Field(
        default_factory=list,
        description="Zone codes with no recent reading. Render as unknown, never as safe.",
    )
    source: str
    generated_at: datetime


class CrowdPublic(ApiModel):
    zones: list[ZoneStatusPublic]
    generated_at: datetime
    notice: str
    notice_mr: str


class SeriesPointOut(ApiModel):
    bucket: datetime
    avg_density: float
    peak_density: float
    peak_level: DensityLevel
    avg_person_count: float
    peak_stagnation: float
    peak_counterflow: float
    avg_confidence: float
    sample_count: int


class ZoneSeries(ApiModel):
    zone_id: uuid.UUID
    zone_code: str
    since: datetime
    until: datetime
    bucket_seconds: int = 60
    points: list[SeriesPointOut]


# ---------------------------------------------------------------------------
# alerts
# ---------------------------------------------------------------------------
class AlertOut(ApiModel):
    id: uuid.UUID
    type: str
    severity: str
    status: str
    zone_id: uuid.UUID | None = None
    zone_code: str | None = None
    zone_name_mr: str | None = None
    trigger_metric: str
    trigger_value: float
    threshold_value: float | None = None
    confidence: float
    observed_at: datetime
    recommended_action: str | None = None
    recommended_action_mr: str | None = None
    #: Which numbered rule produced the action, so an operator can see it came
    #: from the rule table and not from a language model.
    rule_id: str | None = None
    escalation_level: int
    acknowledged_by: uuid.UUID | None = None
    acknowledged_at: datetime | None = None
    escalated_at: datetime | None = None
    resolved_at: datetime | None = None
    created_at: datetime
    seconds_open: float


class AlertAck(ApiModel):
    note: str | None = Field(default=None, max_length=500)


class AlertResolve(ApiModel):
    resolution: str = Field(min_length=3, max_length=500, description="What was actually done. Kept for review.")


# ---------------------------------------------------------------------------
# cameras and calibration
# ---------------------------------------------------------------------------
class CameraOut(ApiModel):
    id: uuid.UUID
    zone_id: uuid.UUID
    zone_code: str | None = None
    name: str
    status: str
    is_calibrated: bool
    calibrated_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    seconds_since_heartbeat: float | None = None
    is_tripwire_enabled: bool
    has_stream: bool


class CameraUpdate(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    stream_url: str | None = Field(default=None, max_length=2000)
    is_tripwire_enabled: bool | None = None


class PointPair(ApiModel):
    """One clicked correspondence: a pixel on a still frame, and where that is."""

    image: Point = Field(description="[x, y] in pixels, origin top-left")
    world: Point = Field(description="[x, y] in metres, any consistent local ground frame")


class CalibrationIn(ApiModel):
    """Four points clicked on a still frame (Section 4/M2, camera calibration).

    Four is the minimum for a homography and also the maximum this accepts.
    More points would need a least-squares fit; four clicked accurately on the
    corners of something measurable beats eight clicked roughly.
    """

    points: list[PointPair] = Field(min_length=4, max_length=4)
    frame_width: int = Field(gt=0, le=16384)
    frame_height: int = Field(gt=0, le=16384)
    #: Optional: the zone outline drawn on the same frame.  Supplying it lets
    #: the server compute `area_m2` from the calibration instead of taking a
    #: surveyor's word for it.
    zone_polygon: list[Point] | None = Field(default=None, min_length=3, max_length=64)
    apply_zone_area: bool = Field(
        default=False,
        description="Overwrite the zone's surveyed area with the area computed from zone_polygon.",
    )
    note: str | None = Field(default=None, max_length=500)

    @field_validator("points")
    @classmethod
    def _distinct_image_points(cls, value: list[PointPair]) -> list[PointPair]:
        if len({(round(p.image[0], 3), round(p.image[1], 3)) for p in value}) < 4:
            raise ValueError("The four image points must be distinct")
        return value

    @model_validator(mode="after")
    def _polygon_required_to_apply(self) -> CalibrationIn:
        if self.apply_zone_area and not self.zone_polygon:
            raise ValueError("apply_zone_area needs zone_polygon")
        return self


class CalibrationOut(ApiModel):
    camera_id: uuid.UUID
    zone_id: uuid.UUID
    matrix: list[float] = Field(description="Row-major 3x3, image pixels to ground metres")
    residual_m: float = Field(description="Worst anchor-point round-trip error. Under 0.01 m or it is rejected.")
    computed_zone_area_m2: float | None = None
    zone_area_m2: float
    zone_area_updated: bool = False
    calibrated_at: datetime
    frame_width: int
    frame_height: int


# ---------------------------------------------------------------------------
# ingest (AI engine -> core API)
# ---------------------------------------------------------------------------
class ReadingIn(ApiModel):
    """One 10-second zone aggregate.

    There is no field here that describes an individual, and there is nowhere to
    put one.  Track ids are discarded in the AI engine after the flow vector is
    computed; they never cross this boundary (Section 12).
    """

    zone_id: uuid.UUID | None = None
    zone_code: str | None = None
    person_count: int = Field(ge=0, le=1_000_000)
    observed_at: datetime
    density: float | None = Field(default=None, ge=0, le=100, description="Advisory; recomputed from surveyed area")
    flow_dx: float = Field(default=0.0, ge=-20, le=20, description="m/s east")
    flow_dy: float = Field(default=0.0, ge=-20, le=20, description="m/s north")
    stagnation_index: float = Field(default=0.0, ge=0, le=1)
    counterflow_ratio: float = Field(default=0.0, ge=0, le=1)
    confidence: float = Field(default=1.0, ge=0, le=1)
    camera_count: int = Field(default=0, ge=0, le=200)

    @model_validator(mode="after")
    def _zone_identified(self) -> ReadingIn:
        if self.zone_id is None and not self.zone_code:
            raise ValueError("Either zone_id or zone_code is required")
        return self


class DensityIngest(ApiModel):
    source: Literal["live", "video", "sim", "manual"]
    readings: list[ReadingIn] = Field(min_length=1)


class IngestResult(ApiModel):
    accepted: int
    rejected: int
    alerts_raised: int
    alerts_resolved: int
    rejections: list[dict[str, Any]] = Field(default_factory=list)
    received_at: datetime


class HeartbeatIn(ApiModel):
    camera_id: uuid.UUID
    status: Literal["online", "degraded", "offline"]
    observed_at: datetime | None = None
    detail: str | None = Field(default=None, max_length=300)


class HeartbeatBatch(ApiModel):
    cameras: list[HeartbeatIn] = Field(min_length=1, max_length=200)


class EngineZone(ApiModel):
    """One zone as the AI engine needs to see it, to run its own pipeline."""

    zone_id: uuid.UUID
    code: str
    name: str
    area_m2: float
    capacity_persons: int
    zone_type: str
    cameras: list[EngineCamera] = Field(default_factory=list)


class EngineCamera(ApiModel):
    camera_id: uuid.UUID
    name: str
    stream_url: str | None = None
    homography: list[float] | None = None
    is_tripwire_enabled: bool = False


class EngineConfig(ApiModel):
    """Everything the AI engine pulls at boot, so it holds no state of its own."""

    crowd_source: str
    zones: list[EngineZone]
    stagnation_threshold: float
    counterflow_threshold: float
    window_seconds: int = 10
    sample_fps: float = 2.0
    generated_at: datetime


EngineZone.model_rebuild()
