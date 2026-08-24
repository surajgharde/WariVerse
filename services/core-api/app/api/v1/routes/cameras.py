"""Cameras and calibration (Section 4/M2, "without this the density number is fiction").

    GET   /cameras                     PATCH /cameras/{id}
    GET   /cameras/{id}/calibration    POST  /cameras/{id}/calibration

Calibration is the step everyone skips, and skipping it is how a system ends up
confidently reporting 4.1 people per square metre for an area it has never
measured.  So:

* the four points are solved and then *verified* against themselves — a matrix
  that cannot reproduce its own anchors is rejected, not stored;
* if the operator also outlines the zone on the same frame, the server computes
  `area_m2` from the calibration rather than trusting a number typed in a form;
* every calibration is audited with the points that produced it, so a density
  figure can be traced back to four clicks and a tape measure.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_session
from app.core.deps import Actor, require
from app.core.errors import AppError
from app.core.permissions import Permission
from app.core.security import now_utc
from app.models import Camera, Zone
from app.schemas.common import ErrorResponse
from app.schemas.crowd import CalibrationIn, CalibrationOut, CameraOut, CameraUpdate
from app.services import audit_service, calibration, crowd_service
from app.services.audit_service import AuditAction
from app.services.calibration import Homography

router = APIRouter(tags=["cameras"], responses={404: {"model": ErrorResponse}})


def _out(camera: Camera, zone_code: str | None, *, at: datetime) -> CameraOut:
    since = None
    if camera.last_heartbeat_at is not None:
        since = round((at - camera.last_heartbeat_at).total_seconds(), 1)
    return CameraOut(
        id=camera.id,
        zone_id=camera.zone_id,
        zone_code=zone_code,
        name=camera.name,
        status=camera.status,
        is_calibrated=camera.is_calibrated,
        calibrated_at=camera.calibrated_at,
        last_heartbeat_at=camera.last_heartbeat_at,
        seconds_since_heartbeat=since,
        is_tripwire_enabled=camera.is_tripwire_enabled,
        # The URL itself is withheld: an RTSP endpoint with credentials in it is
        # a way into the temple's camera network, not a display field.
        has_stream=bool(camera.stream_url),
    )


@router.get("/cameras", response_model=list[CameraOut])
async def list_cameras(
    zone_id: uuid.UUID | None = None,
    status: str | None = Query(default=None, description="online | degraded | offline"),
    uncalibrated_only: bool = False,
    _: Actor = Depends(require(Permission.CROWD_VIEW_DETAIL)),
    session: AsyncSession = Depends(get_session),
) -> list[CameraOut]:
    """The camera roster — "38 of 40 online" on the command centre header."""
    stmt = select(Camera).order_by(Camera.name)
    if zone_id:
        stmt = stmt.where(Camera.zone_id == zone_id)
    if status:
        stmt = stmt.where(Camera.status == status)
    if uncalibrated_only:
        stmt = stmt.where(Camera.homography_matrix.is_(None))

    cameras = list((await session.execute(stmt)).scalars())
    codes = await _zone_codes(session, [c.zone_id for c in cameras])
    moment = now_utc()
    return [_out(c, codes.get(c.zone_id), at=moment) for c in cameras]


async def _zone_codes(session: AsyncSession, zone_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not zone_ids:
        return {}
    rows = await session.execute(select(Zone.id, Zone.code).where(Zone.id.in_(set(zone_ids))))
    return dict(rows.all())  # type: ignore[arg-type]


async def _load(session: AsyncSession, camera_id: uuid.UUID) -> Camera:
    camera = await session.get(Camera, camera_id)
    if camera is None:
        raise AppError("CAMERA_NOT_FOUND", details={"camera_id": str(camera_id)})
    return camera


@router.patch("/cameras/{camera_id}", response_model=CameraOut)
async def update_camera(
    camera_id: uuid.UUID,
    payload: CameraUpdate,
    actor: Actor = Depends(require(Permission.CAMERA_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> CameraOut:
    camera = await _load(session, camera_id)
    changed: dict[str, object] = {}

    if payload.name is not None and payload.name != camera.name:
        camera.name = payload.name
        changed["name"] = payload.name
    if payload.stream_url is not None and payload.stream_url != camera.stream_url:
        camera.stream_url = payload.stream_url or None
        # The URL may embed credentials — record that it changed, never what to.
        changed["stream_url"] = "[updated]"
    if payload.is_tripwire_enabled is not None and payload.is_tripwire_enabled != camera.is_tripwire_enabled:
        camera.is_tripwire_enabled = payload.is_tripwire_enabled
        changed["is_tripwire_enabled"] = payload.is_tripwire_enabled

    if not changed:
        raise AppError("BAD_REQUEST", details={"reason": "nothing to change"})

    await audit_service.record(
        session,
        action=AuditAction.CAMERA_UPDATED,
        actor_id=actor.id,
        actor_role=actor.user.role,
        target_type="camera",
        target_id=camera.id,
        meta={"camera": camera.name, "changes": changed},
        ip=actor.ip,
        user_agent=actor.user_agent,
    )
    codes = await _zone_codes(session, [camera.zone_id])
    out = _out(camera, codes.get(camera.zone_id), at=now_utc())
    await session.commit()
    return out


@router.get("/cameras/{camera_id}/calibration", response_model=CalibrationOut)
async def get_calibration(
    camera_id: uuid.UUID,
    _: Actor = Depends(require(Permission.CROWD_VIEW_DETAIL)),
    session: AsyncSession = Depends(get_session),
) -> CalibrationOut:
    camera = await _load(session, camera_id)
    stored = camera.homography_matrix or {}
    homography = Homography.from_json(stored)
    if homography is None:
        raise AppError(
            "ZONE_NOT_CALIBRATED",
            details={"camera_id": str(camera.id), "camera": camera.name},
        )

    zone = await session.get(Zone, camera.zone_id)
    return CalibrationOut(
        camera_id=camera.id,
        zone_id=camera.zone_id,
        matrix=list(homography.matrix),
        residual_m=homography.residual_m,
        computed_zone_area_m2=stored.get("computed_zone_area_m2"),
        zone_area_m2=zone.area_m2 if zone else 0.0,
        calibrated_at=camera.calibrated_at or now_utc(),
        frame_width=int(stored.get("frame_width", 0)),
        frame_height=int(stored.get("frame_height", 0)),
    )


@router.post("/cameras/{camera_id}/calibration", response_model=CalibrationOut, status_code=201)
async def set_calibration(
    camera_id: uuid.UUID,
    payload: CalibrationIn,
    actor: Actor = Depends(require(Permission.CROWD_CALIBRATE)),
    session: AsyncSession = Depends(get_session),
) -> CalibrationOut:
    """Store a homography from four points clicked on a still frame.

    `apply_zone_area` is the honest path: the operator outlines the zone on the
    same frame and the server derives the area from the calibration.  A typed
    area and a clicked outline can disagree; only one of them was measured.
    """
    camera = await _load(session, camera_id)
    zone = await session.get(Zone, camera.zone_id)
    if zone is None:
        raise AppError("ZONE_NOT_FOUND", details={"zone_id": str(camera.zone_id)})

    for pair in payload.points:
        x, y = pair.image
        if not (0 <= x <= payload.frame_width and 0 <= y <= payload.frame_height):
            raise AppError(
                "CALIBRATION_INVALID",
                details={"reason": "an image point falls outside the frame", "point": [x, y]},
            )

    homography = calibration.solve_homography(
        [(p.image[0], p.image[1]) for p in payload.points],
        [(p.world[0], p.world[1]) for p in payload.points],
    )

    computed_area: float | None = None
    if payload.zone_polygon:
        outline = [(p[0], p[1]) for p in payload.zone_polygon]
        computed_area = round(calibration.polygon_area_m2(homography, outline), 2)
        if computed_area <= 0:
            raise AppError("CALIBRATION_INVALID", details={"reason": "the zone outline encloses no area"})

    previous_area = zone.area_m2
    area_updated = False
    if payload.apply_zone_area and computed_area:
        zone.area_m2 = computed_area
        area_updated = True

    calibrated_at = now_utc()
    camera.homography_matrix = {
        **homography.to_json(),
        "frame_width": payload.frame_width,
        "frame_height": payload.frame_height,
        "image_points": [list(p.image) for p in payload.points],
        "world_points": [list(p.world) for p in payload.points],
        "computed_zone_area_m2": computed_area,
        "note": payload.note,
    }
    camera.calibrated_at = calibrated_at

    await audit_service.record(
        session,
        action=AuditAction.CAMERA_CALIBRATED,
        actor_id=actor.id,
        actor_role=actor.user.role,
        target_type="camera",
        target_id=camera.id,
        meta={
            "camera": camera.name,
            "zone": zone.code,
            "residual_m": homography.residual_m,
            "image_points": [list(p.image) for p in payload.points],
            "world_points": [list(p.world) for p in payload.points],
            "computed_zone_area_m2": computed_area,
            "zone_area_from": previous_area,
            "zone_area_to": zone.area_m2,
            "note": payload.note,
        },
        ip=actor.ip,
        user_agent=actor.user_agent,
    )
    await session.commit()

    if area_updated:
        # Every cached density for this zone was divided by the old area.
        await crowd_service.invalidate(zone.id)

    return CalibrationOut(
        camera_id=camera.id,
        zone_id=zone.id,
        matrix=list(homography.matrix),
        residual_m=homography.residual_m,
        computed_zone_area_m2=computed_area,
        zone_area_m2=zone.area_m2,
        zone_area_updated=area_updated,
        calibrated_at=calibrated_at,
        frame_width=payload.frame_width,
        frame_height=payload.frame_height,
    )


@router.get("/cameras/calibration/console", include_in_schema=False)
async def calibration_console(_: Actor = Depends(require(Permission.CROWD_CALIBRATE))) -> dict[str, str]:
    """Where the point-and-click calibration page lives.

    The page itself is served by the AI engine, because that is the process that
    can pull a still frame off the stream. It posts the resulting four points
    back here — the core API remains the only writer.
    """
    return {
        "url": f"{settings.ai_engine_url.rstrip('/')}/calibrate",
        "note": "Open in a browser, pick a camera, click four points with known ground distances.",
        "note_mr": "ब्राउझरमध्ये उघडा, कॅमेरा निवडा, आणि अंतर माहीत असलेले चार बिंदू क्लिक करा.",
    }
