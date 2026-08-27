"""Zone crowd state: ingest, cache, read back.

The AI engine measures; this module is the only thing that *records*.  Section 6
draws that boundary deliberately — the AI service can be killed and restarted
mid-Wari without losing a reading, because it never held one.

Three layers, in the order a reader hits them:

1. **Redis** — the latest snapshot per zone, TTL 5 minutes (Section 4/M2 step 6).
   The TTL is not an optimisation, it is a safety property: when the AI engine
   dies, the snapshot *expires* instead of freezing, so the map goes to "no
   data" rather than showing a green zone that stopped being measured an hour
   ago.  A stale green zone is how someone walks into a crush.
2. **`density_readings`** — the Timescale hypertable, every 10-second aggregate.
3. **`density_readings_1min`** — the continuous aggregate, for time series and
   the Phase 4 replay scrubber.

Nothing here stores a person.  A reading is a count, an area and two ratios.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from math import atan2, degrees, hypot
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AppError
from app.core.logging import get_logger
from app.core.redis_client import aw, redis
from app.core.security import now_utc
from app.models import Camera, DensityReading, Zone, classify_density
from app.models.crowd import DensityLevel

logger = get_logger(__name__)

_SNAPSHOT_KEY = "crowd:zone:{zone_id}"

#: Compass names for the flow vector, so an operator reads "north-east" rather
#: than doing trigonometry on a radio call.
_COMPASS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")

#: A submitted density more than this far from `count / zone.area_m2` means the
#: AI engine is working from a zone geometry we have since changed.  We keep
#: ours and say so, rather than silently accepting either number.
_DENSITY_DISAGREEMENT = 0.05


#: Pilgrim-facing guidance per band.  Marathi is the operational text; the
#: English is the translation.  Note that none of these say "safe" — the safest
#: band says "comfortable", because a crowd is never a guarantee.
#:
#: This table lives in the service rather than in the route that renders it
#: because two things now read it: `GET /crowd/public`, and the Phase 9
#: assistant answering "how crowded is the east corridor".  An assistant that
#: composed its own wording for a CRITICAL zone would be a language model
#: writing a crowd-safety instruction, which Section 13 forbids outright.  It
#: gets to translate this sentence; it does not get to write one.
PUBLIC_ADVICE: dict[DensityLevel, tuple[str, str]] = {
    DensityLevel.SAFE: (
        "Comfortable. You can walk at a normal pace.",
        "आरामदायी. तुम्ही सामान्य गतीने चालू शकता.",
    ),
    DensityLevel.MODERATE: (
        "Busy, but moving. Keep children and elders close to you.",
        "गर्दी आहे पण रांग चालू आहे. मुलांना आणि वृद्धांना जवळ ठेवा.",
    ),
    DensityLevel.HIGH: (
        "Very crowded. If you can wait or take another route, do that.",
        "खूप गर्दी आहे. शक्य असल्यास थांबा किंवा दुसऱ्या मार्गाने जा.",
    ),
    DensityLevel.CRITICAL: (
        "Do not enter this area. Stay where you are and follow the volunteers' instructions.",
        "या भागात जाऊ नका. आहात तिथेच थांबा आणि स्वयंसेवकांच्या सूचना पाळा.",
    ),
}

#: The most important string in this module.  An unknown zone must never render
#: like a clear one — that is precisely how someone walks into a crush.
UNKNOWN_ADVICE = (
    "No live reading for this area right now. Treat it as unknown, not as clear, and follow the volunteers.",
    "या भागाची सध्याची माहिती उपलब्ध नाही. ते मोकळे आहे असे समजू नका; स्वयंसेवकांच्या सूचना पाळा.",
)


@dataclass(frozen=True, slots=True)
class ReadingIn:
    """One 10-second window for one zone, as published by the AI engine."""

    zone_id: uuid.UUID
    person_count: int
    observed_at: datetime
    density: float | None = None  # recomputed from our own area; advisory only
    flow_dx: float = 0.0
    flow_dy: float = 0.0
    stagnation_index: float = 0.0
    counterflow_ratio: float = 0.0
    confidence: float = 1.0
    source: str = "sim"
    camera_count: int = 0


@dataclass(frozen=True, slots=True)
class ZoneSnapshot:
    """What every crowd endpoint returns, and what Redis holds."""

    zone_id: uuid.UUID
    zone_code: str
    zone_name: str
    zone_name_mr: str
    person_count: int
    density: float
    level: DensityLevel
    flow_dx: float
    flow_dy: float
    stagnation_index: float
    counterflow_ratio: float
    confidence: float
    source: str
    camera_count: int
    observed_at: datetime
    area_m2: float
    capacity_persons: int
    notes: list[str] = field(default_factory=list)

    @property
    def flow_speed_ms(self) -> float:
        return hypot(self.flow_dx, self.flow_dy)

    @property
    def flow_direction(self) -> str | None:
        """Dominant direction as a compass point, or None when barely moving."""
        if self.flow_speed_ms < 0.05:
            return None
        # atan2(dx, dy): 0° is north (+y), increasing clockwise through east.
        bearing = (degrees(atan2(self.flow_dx, self.flow_dy)) + 360.0) % 360.0
        return _COMPASS[int((bearing + 22.5) % 360.0 // 45.0)]

    @property
    def age_seconds(self) -> float:
        return max(0.0, (now_utc() - self.observed_at).total_seconds())

    @property
    def is_stale(self) -> bool:
        return self.age_seconds > settings.stale_reading_seconds

    @property
    def occupancy_pct(self) -> float | None:
        if self.capacity_persons <= 0:
            return None
        return round(100.0 * self.person_count / self.capacity_persons, 1)

    def to_json(self) -> dict[str, Any]:
        return {
            "zone_id": str(self.zone_id),
            "zone_code": self.zone_code,
            "zone_name": self.zone_name,
            "zone_name_mr": self.zone_name_mr,
            "person_count": self.person_count,
            "density": self.density,
            "level": str(self.level),
            "flow_dx": self.flow_dx,
            "flow_dy": self.flow_dy,
            "stagnation_index": self.stagnation_index,
            "counterflow_ratio": self.counterflow_ratio,
            "confidence": self.confidence,
            "source": self.source,
            "camera_count": self.camera_count,
            "observed_at": self.observed_at.isoformat(),
            "area_m2": self.area_m2,
            "capacity_persons": self.capacity_persons,
            "notes": self.notes,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> ZoneSnapshot:
        return cls(
            zone_id=uuid.UUID(raw["zone_id"]),
            zone_code=raw["zone_code"],
            zone_name=raw["zone_name"],
            zone_name_mr=raw["zone_name_mr"],
            person_count=int(raw["person_count"]),
            density=float(raw["density"]),
            level=DensityLevel(raw["level"]),
            flow_dx=float(raw["flow_dx"]),
            flow_dy=float(raw["flow_dy"]),
            stagnation_index=float(raw["stagnation_index"]),
            counterflow_ratio=float(raw["counterflow_ratio"]),
            confidence=float(raw["confidence"]),
            source=raw["source"],
            camera_count=int(raw["camera_count"]),
            observed_at=datetime.fromisoformat(raw["observed_at"]),
            area_m2=float(raw["area_m2"]),
            capacity_persons=int(raw["capacity_persons"]),
            notes=list(raw.get("notes", [])),
        )


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------
async def load_zones(session: AsyncSession, zone_ids: list[uuid.UUID] | None = None) -> dict[uuid.UUID, Zone]:
    stmt = select(Zone).where(Zone.is_active.is_(True))
    if zone_ids:
        stmt = stmt.where(Zone.id.in_(zone_ids))
    rows = await session.execute(stmt)
    return {zone.id: zone for zone in rows.scalars()}


async def record(session: AsyncSession, reading: ReadingIn, zone: Zone) -> ZoneSnapshot:
    """Persist one aggregate and return the snapshot it produced.

    Density is recomputed here from `zone.area_m2` rather than taken from the
    payload.  The database owns the ground truth for area; if the AI engine is
    running against a zone we have since re-surveyed, its arithmetic is stale
    and ours is not.
    """
    if zone.area_m2 <= 0:
        raise AppError(
            "READING_REJECTED",
            details={"zone_id": str(zone.id), "reason": "zone has no surveyed area, density would be undefined"},
        )
    if reading.person_count < 0:
        raise AppError("READING_REJECTED", details={"reason": "negative person count"})

    notes: list[str] = []
    density = round(reading.person_count / zone.area_m2, 4)
    if reading.density is not None and abs(reading.density - density) > _DENSITY_DISAGREEMENT:
        notes.append("recomputed from surveyed zone area")
        logger.info(
            "density_recomputed",
            extra={
                "zone": zone.code,
                "submitted": reading.density,
                "recomputed": density,
                "area_m2": zone.area_m2,
            },
        )

    level = classify_density(density)
    confidence = min(1.0, max(0.0, reading.confidence))
    if reading.camera_count == 0 and reading.source != "sim":
        # No camera contributed: this zone is being estimated, not measured.
        confidence = min(confidence, 0.4)
        notes.append("no camera reporting — estimate only")

    session.add(
        DensityReading(
            time=reading.observed_at,
            zone_id=zone.id,
            person_count=reading.person_count,
            density=density,
            level=str(level),
            flow_dx=reading.flow_dx,
            flow_dy=reading.flow_dy,
            stagnation_index=reading.stagnation_index,
            counterflow_ratio=reading.counterflow_ratio,
            confidence=confidence,
            source=reading.source,
            camera_count=reading.camera_count,
        )
    )

    return ZoneSnapshot(
        zone_id=zone.id,
        zone_code=zone.code,
        zone_name=zone.name,
        zone_name_mr=zone.name_mr,
        person_count=reading.person_count,
        density=density,
        level=level,
        flow_dx=reading.flow_dx,
        flow_dy=reading.flow_dy,
        stagnation_index=reading.stagnation_index,
        counterflow_ratio=reading.counterflow_ratio,
        confidence=confidence,
        source=reading.source,
        camera_count=reading.camera_count,
        observed_at=reading.observed_at,
        area_m2=zone.area_m2,
        capacity_persons=zone.capacity_persons,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------
async def cache(snapshots: list[ZoneSnapshot]) -> bool:
    """Write the latest snapshot per zone with a 5-minute TTL.

    Best effort: a failed cache write costs a database read on the next poll.
    """
    if not snapshots:
        return True
    try:
        pipe = redis.pipeline()
        for snap in snapshots:
            pipe.set(
                _SNAPSHOT_KEY.format(zone_id=snap.zone_id),
                json.dumps(snap.to_json(), ensure_ascii=False),
                ex=settings.density_cache_ttl_seconds,
            )
        await aw(pipe.execute())
        return True
    except Exception as exc:
        logger.warning("crowd_cache_write_failed", extra={"count": len(snapshots), "error": str(exc)})
        return False


async def cached(zone_ids: list[uuid.UUID]) -> dict[uuid.UUID, ZoneSnapshot]:
    if not zone_ids:
        return {}
    try:
        raw = await aw(redis.mget([_SNAPSHOT_KEY.format(zone_id=z) for z in zone_ids]))
    except Exception as exc:
        logger.warning("crowd_cache_read_failed", extra={"error": str(exc)})
        return {}

    found: dict[uuid.UUID, ZoneSnapshot] = {}
    for zone_id, value in zip(zone_ids, raw, strict=True):
        if not value:
            continue
        try:
            found[zone_id] = ZoneSnapshot.from_json(json.loads(value))
        except (ValueError, KeyError, TypeError):
            logger.warning("crowd_cache_decode_failed", extra={"zone_id": str(zone_id)})
    return found


async def invalidate(zone_id: uuid.UUID) -> None:
    """Drop a zone's snapshot — used after a zone's area is re-surveyed, since
    every cached density was divided by the old number."""
    try:
        await aw(redis.delete(_SNAPSHOT_KEY.format(zone_id=zone_id)))
    except Exception as exc:
        # A failed delete costs at most one stale poll before the TTL expires.
        logger.warning("crowd_cache_invalidate_failed", extra={"zone_id": str(zone_id), "error": str(exc)})


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------
async def latest(session: AsyncSession, zone_ids: list[uuid.UUID] | None = None) -> list[ZoneSnapshot]:
    """Current state of every active zone, cache first, database second.

    Zones with no reading at all are returned by the *caller* as "unknown" —
    this function does not invent a zero, because zero people and no
    measurement are opposite facts.
    """
    zones = await load_zones(session, zone_ids)
    if not zones:
        return []

    ids = list(zones.keys())
    snapshots = await cached(ids)

    missing = [z for z in ids if z not in snapshots]
    if missing:
        rows = await session.execute(
            select(DensityReading)
            .where(DensityReading.zone_id.in_(missing))
            .where(DensityReading.time >= now_utc() - timedelta(seconds=settings.density_cache_ttl_seconds))
            .order_by(DensityReading.zone_id, DensityReading.time.desc())
            .distinct(DensityReading.zone_id)
        )
        for row in rows.scalars():
            zone = zones[row.zone_id]
            snapshots[row.zone_id] = ZoneSnapshot(
                zone_id=row.zone_id,
                zone_code=zone.code,
                zone_name=zone.name,
                zone_name_mr=zone.name_mr,
                person_count=row.person_count,
                density=row.density,
                level=DensityLevel(row.level),
                flow_dx=row.flow_dx,
                flow_dy=row.flow_dy,
                stagnation_index=row.stagnation_index,
                counterflow_ratio=row.counterflow_ratio,
                confidence=row.confidence,
                source=row.source,
                camera_count=row.camera_count,
                observed_at=row.time,
                area_m2=zone.area_m2,
                capacity_persons=zone.capacity_persons,
                notes=["served from database, cache expired"],
            )

    return [snapshots[z] for z in ids if z in snapshots]


@dataclass(frozen=True, slots=True)
class SeriesPoint:
    bucket: datetime
    avg_density: float
    peak_density: float
    avg_person_count: float
    peak_stagnation: float
    peak_counterflow: float
    avg_confidence: float
    sample_count: int

    @property
    def peak_level(self) -> DensityLevel:
        return classify_density(self.peak_density)


async def series(
    session: AsyncSession,
    zone_id: uuid.UUID,
    *,
    since: datetime,
    until: datetime | None = None,
) -> list[SeriesPoint]:
    """1-minute rollups from the continuous aggregate (Section 4/M2 step 6).

    The aggregate policy refreshes with a one-minute end-offset, so the last
    sixty seconds are not in the view yet.  Those are unioned in from the raw
    hypertable — an operator watching a zone climb needs the newest bucket most
    of all, and a chart that lags a minute behind the alert feed is a chart
    nobody trusts.
    """
    end = until or now_utc()
    result = await session.execute(
        text(
            """
            SELECT bucket, avg_density, peak_density, avg_person_count,
                   peak_stagnation, peak_counterflow, avg_confidence, sample_count
            FROM density_readings_1min
            WHERE zone_id = :zone_id AND bucket >= :since AND bucket <= :until
            UNION ALL
            SELECT time_bucket(INTERVAL '1 minute', time) AS bucket,
                   AVG(density), MAX(density), AVG(person_count),
                   MAX(stagnation_index), MAX(counterflow_ratio),
                   AVG(confidence), COUNT(*)
            FROM density_readings
            WHERE zone_id = :zone_id
              AND time >= GREATEST(:since, now() - INTERVAL '2 minutes')
              AND time <= :until
              AND time_bucket(INTERVAL '1 minute', time)
                  NOT IN (SELECT bucket FROM density_readings_1min WHERE zone_id = :zone_id)
            GROUP BY 1
            ORDER BY bucket
            """
        ),
        {"zone_id": zone_id, "since": since, "until": end},
    )
    return [
        SeriesPoint(
            bucket=row[0],
            avg_density=round(float(row[1]), 4),
            peak_density=round(float(row[2]), 4),
            avg_person_count=round(float(row[3]), 1),
            peak_stagnation=round(float(row[4]), 4),
            peak_counterflow=round(float(row[5]), 4),
            avg_confidence=round(float(row[6]), 3),
            sample_count=int(row[7]),
        )
        for row in result
    ]


async def series_all(
    session: AsyncSession,
    *,
    since: datetime,
    until: datetime | None = None,
) -> dict[uuid.UUID, list[SeriesPoint]]:
    """`series`, for every zone in one query (Section 4/M3 replay + digest).

    The command centre wants sixty minutes across thirty zones.  Calling
    `series` per zone is thirty round trips for one scrubber drag, and the
    replay is the one screen where lag is unforgivable — it is the feature an
    operator reaches for *while* explaining what happened.

    Same two-part read as `series`: the continuous aggregate for everything
    settled, plus the raw hypertable for the last couple of minutes the
    aggregate policy has not caught up with.  The anti-join keeps a bucket from
    appearing twice when the policy refreshes mid-query.

    That anti-join sits in the outer `WHERE` over a derived table rather than in
    a `HAVING` on the aggregate itself.  It has to: a correlated subquery inside
    `HAVING` cannot reach `d.time`, because `time_bucket(...)` is the grouped
    expression and the raw column is not grouped.  Postgres rejects it outright
    ("subquery uses ungrouped column"), so the grouping is closed off first and
    the duplicate check applied to its result.
    """
    end = until or now_utc()
    result = await session.execute(
        text(
            """
            SELECT zone_id, bucket, avg_density, peak_density, avg_person_count,
                   peak_stagnation, peak_counterflow, avg_confidence, sample_count
            FROM density_readings_1min
            WHERE bucket >= :since AND bucket <= :until
            UNION ALL
            SELECT live.zone_id, live.bucket, live.avg_density, live.peak_density,
                   live.avg_person_count, live.peak_stagnation, live.peak_counterflow,
                   live.avg_confidence, live.sample_count
            FROM (
                SELECT d.zone_id AS zone_id,
                       time_bucket(INTERVAL '1 minute', d.time) AS bucket,
                       AVG(d.density) AS avg_density,
                       MAX(d.density) AS peak_density,
                       AVG(d.person_count) AS avg_person_count,
                       MAX(d.stagnation_index) AS peak_stagnation,
                       MAX(d.counterflow_ratio) AS peak_counterflow,
                       AVG(d.confidence) AS avg_confidence,
                       COUNT(*) AS sample_count
                FROM density_readings d
                WHERE d.time >= GREATEST(:since, now() - INTERVAL '2 minutes')
                  AND d.time <= :until
                GROUP BY d.zone_id, time_bucket(INTERVAL '1 minute', d.time)
            ) live
            WHERE NOT EXISTS (
                SELECT 1 FROM density_readings_1min m
                WHERE m.zone_id = live.zone_id
                  AND m.bucket = live.bucket
            )
            ORDER BY 1, 2
            """
        ),
        {"since": since, "until": end},
    )

    out: dict[uuid.UUID, list[SeriesPoint]] = {}
    for row in result:
        out.setdefault(row[0], []).append(
            SeriesPoint(
                bucket=row[1],
                avg_density=round(float(row[2]), 4),
                peak_density=round(float(row[3]), 4),
                avg_person_count=round(float(row[4]), 1),
                peak_stagnation=round(float(row[5]), 4),
                peak_counterflow=round(float(row[6]), 4),
                avg_confidence=round(float(row[7]), 3),
                sample_count=int(row[8]),
            )
        )
    return out


# ---------------------------------------------------------------------------
# camera heartbeats
# ---------------------------------------------------------------------------
async def heartbeat(
    session: AsyncSession,
    camera_id: uuid.UUID,
    *,
    status: str,
    at: datetime | None = None,
) -> tuple[Camera, bool]:
    """Record that a camera is alive.  Returns (camera, status_changed)."""
    camera = await session.get(Camera, camera_id)
    if camera is None:
        raise AppError("CAMERA_NOT_FOUND", details={"camera_id": str(camera_id)})
    if status not in {"online", "degraded", "offline"}:
        raise AppError("READING_REJECTED", details={"reason": "unknown camera status", "status": status})

    changed = camera.status != status
    camera.status = status
    camera.last_heartbeat_at = at or now_utc()
    return camera, changed


async def stale_cameras(session: AsyncSession, *, at: datetime | None = None) -> list[Camera]:
    """Cameras that were online and have gone quiet past the grace window."""
    cutoff = (at or now_utc()) - timedelta(seconds=settings.camera_offline_seconds)
    rows = await session.execute(
        select(Camera).where(
            Camera.status != "offline",
            (Camera.last_heartbeat_at.is_(None)) | (Camera.last_heartbeat_at < cutoff),
        )
    )
    return list(rows.scalars())
