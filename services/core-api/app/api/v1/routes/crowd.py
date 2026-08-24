"""Zone and crowd-state endpoints (Section 4/M2, Section 9).

    GET /zones                     GET /zones/{id}
    GET /crowd/live                GET /crowd/public
    GET /zones/{id}/series         PATCH /zones/{id}

The public and detailed views are separate endpoints rather than one endpoint
that redacts fields, because a redacting endpoint is one refactor away from
leaking.  A pilgrim's token cannot reach `/crowd/live` at all.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_session
from app.core.deps import Actor, require
from app.core.errors import AppError
from app.core.permissions import Permission
from app.core.security import now_utc
from app.models import Camera, Zone
from app.models.crowd import DensityLevel
from app.schemas.common import ErrorResponse
from app.schemas.crowd import (
    CrowdLive,
    CrowdPublic,
    FlowOut,
    SeriesPointOut,
    ZoneOut,
    ZoneSeries,
    ZoneStatusDetail,
    ZoneStatusPublic,
    ZoneUpdate,
)
from app.services import audit_service, crowd_service
from app.services.audit_service import AuditAction
from app.services.crowd_service import ZoneSnapshot

router = APIRouter(tags=["crowd"], responses={404: {"model": ErrorResponse}})

#: Pilgrim-facing guidance per band.  Marathi is the operational text; the
#: English is the translation.  Note that none of these say "safe" — the safest
#: band says "comfortable", because a crowd is never a guarantee.
_ADVICE: dict[DensityLevel, tuple[str, str]] = {
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

#: The most important string in this file.  An unknown zone must never render
#: like a clear one — that is precisely how someone walks into a crush.
_UNKNOWN_ADVICE = (
    "No live reading for this area right now. Treat it as unknown, not as clear, and follow the volunteers.",
    "या भागाची सध्याची माहिती उपलब्ध नाही. ते मोकळे आहे असे समजू नका; स्वयंसेवकांच्या सूचना पाळा.",
)

_PUBLIC_NOTICE = (
    "Crowd levels are estimates from anonymous counting. No individual is identified or tracked.",
    "गर्दीची पातळी ही निनावी मोजणीवरून काढलेला अंदाज आहे. कोणत्याही व्यक्तीची ओळख पटवली जात नाही.",
)


async def _geojson(session: AsyncSession, zone_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, Any]]:
    if not zone_ids:
        return {}
    rows = await session.execute(
        select(Zone.id, func.ST_AsGeoJSON(Zone.geom)).where(Zone.id.in_(zone_ids))
    )
    out: dict[uuid.UUID, dict[str, Any]] = {}
    for zone_id, raw in rows:
        if raw:
            out[zone_id] = json.loads(raw)
    return out


async def _camera_counts(session: AsyncSession, zone_ids: list[uuid.UUID]) -> dict[uuid.UUID, tuple[int, int]]:
    if not zone_ids:
        return {}
    rows = await session.execute(
        select(
            Camera.zone_id,
            func.count(Camera.id),
            func.count(Camera.homography_matrix),
        )
        .where(Camera.zone_id.in_(zone_ids))
        .group_by(Camera.zone_id)
    )
    return {zone_id: (int(total), int(calibrated)) for zone_id, total, calibrated in rows}


def _detail(snapshot: ZoneSnapshot) -> ZoneStatusDetail:
    return ZoneStatusDetail(
        zone_id=snapshot.zone_id,
        zone_code=snapshot.zone_code,
        zone_name=snapshot.zone_name,
        zone_name_mr=snapshot.zone_name_mr,
        person_count=snapshot.person_count,
        density=snapshot.density,
        level=snapshot.level,
        occupancy_pct=snapshot.occupancy_pct,
        flow=FlowOut(
            speed_ms=round(snapshot.flow_speed_ms, 3),
            direction=snapshot.flow_direction,
            dx=snapshot.flow_dx,
            dy=snapshot.flow_dy,
        ),
        stagnation_index=snapshot.stagnation_index,
        counterflow_ratio=snapshot.counterflow_ratio,
        confidence=snapshot.confidence,
        source=snapshot.source,
        camera_count=snapshot.camera_count,
        observed_at=snapshot.observed_at,
        age_seconds=round(snapshot.age_seconds, 1),
        is_stale=snapshot.is_stale,
        area_m2=snapshot.area_m2,
        notes=snapshot.notes,
    )


@router.get("/zones", response_model=list[ZoneOut])
async def list_zones(
    include_inactive: bool = False,
    zone_type: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> list[ZoneOut]:
    """The map's base layer.

    Public: a pilgrim's app needs the polygons to render a map before anyone
    signs in.  Geometry is not sensitive — it is the temple, and it is on every
    printed map in Pandharpur.  What is withheld is who is standing in it.
    """
    stmt = select(Zone).order_by(Zone.code)
    if not include_inactive:
        stmt = stmt.where(Zone.is_active.is_(True))
    if zone_type:
        stmt = stmt.where(Zone.zone_type == zone_type)

    zones = list((await session.execute(stmt)).scalars())
    ids = [z.id for z in zones]
    shapes = await _geojson(session, ids)
    cameras = await _camera_counts(session, ids)

    return [
        ZoneOut(
            id=z.id,
            code=z.code,
            name=z.name,
            name_mr=z.name_mr,
            zone_type=z.zone_type,
            area_m2=z.area_m2,
            capacity_persons=z.capacity_persons,
            is_active=z.is_active,
            parent_zone_id=z.parent_zone_id,
            geometry=shapes.get(z.id),
            camera_count=cameras.get(z.id, (0, 0))[0],
            calibrated_camera_count=cameras.get(z.id, (0, 0))[1],
        )
        for z in zones
    ]


async def _resolve_zone(session: AsyncSession, key: str) -> Zone:
    """Accept either the UUID or the short code — operators speak in codes."""
    try:
        zone = await session.get(Zone, uuid.UUID(key))
    except ValueError:
        zone = await session.scalar(select(Zone).where(Zone.code == key.upper()))
    if zone is None:
        raise AppError("ZONE_NOT_FOUND", details={"zone": key})
    return zone


@router.get("/zones/{zone_key}", response_model=ZoneOut)
async def get_zone(zone_key: str, session: AsyncSession = Depends(get_session)) -> ZoneOut:
    zone = await _resolve_zone(session, zone_key)
    shapes = await _geojson(session, [zone.id])
    cameras = await _camera_counts(session, [zone.id])
    return ZoneOut(
        id=zone.id,
        code=zone.code,
        name=zone.name,
        name_mr=zone.name_mr,
        zone_type=zone.zone_type,
        area_m2=zone.area_m2,
        capacity_persons=zone.capacity_persons,
        is_active=zone.is_active,
        parent_zone_id=zone.parent_zone_id,
        geometry=shapes.get(zone.id),
        camera_count=cameras.get(zone.id, (0, 0))[0],
        calibrated_camera_count=cameras.get(zone.id, (0, 0))[1],
    )


@router.get("/crowd/live", response_model=CrowdLive)
async def crowd_live(
    _: Actor = Depends(require(Permission.CROWD_VIEW_DETAIL)),
    session: AsyncSession = Depends(get_session),
) -> CrowdLive:
    """Every zone's current state, with provenance on every number.

    Zones with no recent reading come back in `unknown_zones` rather than as a
    row of zeros.  "Nobody is there" and "we are not measuring" are opposite
    facts and the console must not conflate them.
    """
    zones = await crowd_service.load_zones(session)
    snapshots = await crowd_service.latest(session)
    seen = {s.zone_id for s in snapshots}

    return CrowdLive(
        zones=[_detail(s) for s in snapshots],
        unknown_zones=sorted(z.code for zid, z in zones.items() if zid not in seen),
        source=settings.crowd_source,
        generated_at=now_utc(),
    )


@router.get("/crowd/public", response_model=CrowdPublic)
async def crowd_public(session: AsyncSession = Depends(get_session)) -> CrowdPublic:
    """Coarse zone colour for the pilgrim app.

    Level and advice only.  No head count, no flow vector, no occupancy — those
    would tell anyone with a phone exactly where the crowd is thickest, and that
    is not a public fact (Section 12).
    """
    zones = await crowd_service.load_zones(session)
    snapshots = {s.zone_id: s for s in await crowd_service.latest(session)}

    items: list[ZoneStatusPublic] = []
    for zone_id, zone in sorted(zones.items(), key=lambda kv: kv[1].code):
        snap = snapshots.get(zone_id)
        if snap is None or snap.is_stale:
            advice, advice_mr = _UNKNOWN_ADVICE
            items.append(
                ZoneStatusPublic(
                    zone_code=zone.code,
                    zone_name=zone.name,
                    zone_name_mr=zone.name_mr,
                    level=None,
                    advice=advice,
                    advice_mr=advice_mr,
                    observed_at=snap.observed_at if snap else None,
                    age_seconds=round(snap.age_seconds, 1) if snap else None,
                    is_stale=True,
                )
            )
            continue

        advice, advice_mr = _ADVICE[snap.level]
        items.append(
            ZoneStatusPublic(
                zone_code=zone.code,
                zone_name=zone.name,
                zone_name_mr=zone.name_mr,
                level=snap.level,
                advice=advice,
                advice_mr=advice_mr,
                observed_at=snap.observed_at,
                age_seconds=round(snap.age_seconds, 1),
                is_stale=False,
            )
        )

    notice, notice_mr = _PUBLIC_NOTICE
    return CrowdPublic(zones=items, generated_at=now_utc(), notice=notice, notice_mr=notice_mr)


@router.get("/zones/{zone_key}/series", response_model=ZoneSeries)
async def zone_series(
    zone_key: str,
    minutes: int = Query(default=60, ge=1, le=1440, description="Look-back window"),
    until: datetime | None = None,
    _: Actor = Depends(require(Permission.CROWD_VIEW_DETAIL)),
    session: AsyncSession = Depends(get_session),
) -> ZoneSeries:
    """1-minute density rollups — the chart under the map, and the data the
    Phase 4 replay scrubber reads."""
    zone = await _resolve_zone(session, zone_key)
    end = until or now_utc()
    start = end - timedelta(minutes=minutes)

    points = await crowd_service.series(session, zone.id, since=start, until=end)
    return ZoneSeries(
        zone_id=zone.id,
        zone_code=zone.code,
        since=start,
        until=end,
        points=[
            SeriesPointOut(
                bucket=p.bucket,
                avg_density=p.avg_density,
                peak_density=p.peak_density,
                peak_level=p.peak_level,
                avg_person_count=p.avg_person_count,
                peak_stagnation=p.peak_stagnation,
                peak_counterflow=p.peak_counterflow,
                avg_confidence=p.avg_confidence,
                sample_count=p.sample_count,
            )
            for p in points
        ],
    )


@router.patch("/zones/{zone_key}", response_model=ZoneOut)
async def update_zone(
    zone_key: str,
    payload: ZoneUpdate,
    actor: Actor = Depends(require(Permission.ZONE_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> ZoneOut:
    """Re-survey a zone.

    Changing `area_m2` changes every density figure this zone will ever report,
    and invalidates every one it has cached — so the reason is mandatory and the
    change is audited.
    """
    zone = await _resolve_zone(session, zone_key)
    before = {"area_m2": zone.area_m2, "capacity_persons": zone.capacity_persons, "is_active": zone.is_active}

    changed: dict[str, Any] = {}
    for field in ("name", "name_mr", "area_m2", "capacity_persons", "is_active"):
        value = getattr(payload, field)
        if value is not None and value != getattr(zone, field):
            setattr(zone, field, value)
            changed[field] = value

    if not changed:
        raise AppError("BAD_REQUEST", details={"reason": "nothing to change"})

    await audit_service.record(
        session,
        action=AuditAction.ZONE_UPDATED,
        actor_id=actor.id,
        actor_role=actor.user.role,
        target_type="zone",
        target_id=zone.id,
        meta={"code": zone.code, "before": before, "changes": changed, "reason": payload.reason},
        ip=actor.ip,
        user_agent=actor.user_agent,
    )
    await session.commit()

    if "area_m2" in changed:
        # Every cached density for this zone was divided by the old area.
        await crowd_service.invalidate(zone.id)

    return await get_zone(str(zone.id), session)
