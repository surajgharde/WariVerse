"""The AI engine's ingress (Section 6 boundary).

    POST /ingest/density      POST /ingest/heartbeat      GET /ingest/config
    POST /ingest/breach

This is the *only* way crowd measurements enter the system, and it is why the
AI service holds no database credentials.  Kill the ai-engine container mid-Wari
and nothing here is lost: passes, alerts and incidents live in Postgres, which
the AI service has never been able to touch.

Authenticated by a shared service token, not a user session — the caller is a
machine with no identity, so it gets a capability, not a role.

Guarded rather than trusted:
* the batch size is capped, so a wedged engine cannot post a 100 MB body;
* timestamps far outside the present are refused, so a container with a wrong
  clock cannot write readings into next Tuesday;
* one bad reading is rejected on its own and the rest of the batch still lands,
  because dropping 40 zones over one malformed row is the wrong trade during a
  surge.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import events
from app.core.config import settings
from app.core.db import get_session
from app.core.deps import require_ai_service
from app.core.errors import AppError
from app.core.logging import get_logger
from app.core.security import now_utc
from app.models import Alert, Camera, Zone
from app.schemas.breach import CrossingBatch, CrossingResult
from app.schemas.common import ErrorResponse
from app.schemas.crowd import (
    DensityIngest,
    EngineCamera,
    EngineConfig,
    EngineZone,
    HeartbeatBatch,
    IngestResult,
)
from app.services import alert_service, breach_service, config_service, crowd_service
from app.services.calibration import Homography

logger = get_logger(__name__)

router = APIRouter(
    prefix="/ingest",
    tags=["ingest"],
    dependencies=[Depends(require_ai_service)],
    responses={401: {"model": ErrorResponse}},
)

#: A reading may arrive a little late (the engine buffers when the API restarts)
#: but not from the future.  Clock skew is a real failure mode on edge boxes.
MAX_BACKDATE = timedelta(minutes=15)
MAX_FUTURE = timedelta(seconds=30)


@router.post("/density", response_model=IngestResult, status_code=202)
async def ingest_density(
    payload: DensityIngest,
    session: AsyncSession = Depends(get_session),
) -> IngestResult:
    """Accept a batch of 10-second zone aggregates, alert on them, publish them."""
    if len(payload.readings) > settings.ingest_max_batch:
        raise AppError(
            "READING_REJECTED",
            details={"reason": "batch too large", "limit": settings.ingest_max_batch, "sent": len(payload.readings)},
        )

    received = now_utc()
    zones = await crowd_service.load_zones(session)
    by_code = {z.code: z for z in zones.values()}
    thresholds = await alert_service.load_thresholds(session)

    snapshots: list[crowd_service.ZoneSnapshot] = []
    rejections: list[dict[str, object]] = []
    outbound: list[tuple[str, dict[str, object]]] = []
    raised = 0
    resolved = 0

    for index, reading in enumerate(payload.readings):
        zone: Zone | None = None
        if reading.zone_id is not None:
            zone = zones.get(reading.zone_id)
        elif reading.zone_code:
            zone = by_code.get(reading.zone_code.upper())

        if zone is None:
            rejections.append(
                {
                    "index": index,
                    "zone": str(reading.zone_id or reading.zone_code),
                    "reason": "unknown or inactive zone",
                }
            )
            continue

        age = received - reading.observed_at
        if age > MAX_BACKDATE or age < -MAX_FUTURE:
            rejections.append(
                {
                    "index": index,
                    "zone": zone.code,
                    "reason": "observed_at outside the accepted window — check the engine's clock",
                    "skew_seconds": round(age.total_seconds(), 1),
                }
            )
            continue

        try:
            snapshot = await crowd_service.record(
                session,
                crowd_service.ReadingIn(
                    zone_id=zone.id,
                    person_count=reading.person_count,
                    observed_at=reading.observed_at,
                    density=reading.density,
                    flow_dx=reading.flow_dx,
                    flow_dy=reading.flow_dy,
                    stagnation_index=reading.stagnation_index,
                    counterflow_ratio=reading.counterflow_ratio,
                    confidence=reading.confidence,
                    source=payload.source,
                    camera_count=reading.camera_count,
                ),
                zone,
            )
        except AppError as exc:
            rejections.append({"index": index, "zone": zone.code, "reason": exc.message, **exc.details})
            continue

        snapshots.append(snapshot)
        outbound.append((events.DENSITY_UPDATED, snapshot.to_json()))

        outcome = await alert_service.evaluate(session, snapshot, thresholds, at=received)
        if outcome.created and outcome.alert is not None:
            raised += 1
            outbound.append((events.ALERT_RAISED, _alert_event(outcome.alert, snapshot)))
        elif outcome.refreshed and outcome.alert is not None:
            outbound.append((events.ALERT_UPDATED, _alert_event(outcome.alert, snapshot)))
        for closed in outcome.resolved:
            resolved += 1
            outbound.append((events.ALERT_UPDATED, _alert_event(closed, snapshot)))

    await session.commit()

    # Cache and fan-out happen after the commit: an operator must never see an
    # event for a reading that then failed to persist.
    await crowd_service.cache(snapshots)
    await events.publish_many(outbound)

    if rejections:
        logger.warning("ingest_partial", extra={"accepted": len(snapshots), "rejected": len(rejections)})

    return IngestResult(
        accepted=len(snapshots),
        rejected=len(rejections),
        alerts_raised=raised,
        alerts_resolved=resolved,
        rejections=rejections[:20],
        received_at=received,
    )


def _alert_event(alert: Alert, snapshot: crowd_service.ZoneSnapshot) -> dict[str, object]:
    """The alert payload a command-centre socket receives.

    Carries the action *and* the rule that produced it, so the console never
    has to look either one up — during a surge, a round trip per alert is a
    round trip too many.
    """
    return {
        "alert_id": str(alert.id),
        "type": alert.type,
        "severity": alert.severity,
        "status": alert.status,
        "rule_id": alert.rule_id,
        "zone_id": str(snapshot.zone_id),
        "zone_code": snapshot.zone_code,
        "zone_name_mr": snapshot.zone_name_mr,
        "trigger_metric": alert.trigger_metric,
        "trigger_value": alert.trigger_value,
        "threshold_value": alert.threshold_value,
        "confidence": alert.confidence,
        "recommended_action": alert.recommended_action,
        "recommended_action_mr": alert.recommended_action_mr,
        "observed_at": alert.observed_at,
    }


@router.post("/heartbeat", response_model=dict[str, int], status_code=202)
async def ingest_heartbeat(
    payload: HeartbeatBatch,
    session: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    """Camera liveness.

    A camera coming back closes its zone's coverage alert without an operator
    having to dismiss it — the situation genuinely ended, and making someone
    click it away teaches them to click alerts away.
    """
    changed = 0
    cleared = 0
    outbound: list[tuple[str, dict[str, object]]] = []

    for entry in payload.cameras:
        try:
            camera, status_changed = await crowd_service.heartbeat(
                session, entry.camera_id, status=entry.status, at=entry.observed_at
            )
        except AppError:
            logger.warning("heartbeat_unknown_camera", extra={"camera_id": str(entry.camera_id)})
            continue

        if status_changed:
            changed += 1
            outbound.append(
                (
                    events.CAMERA_STATUS_CHANGED,
                    {
                        "camera_id": str(camera.id),
                        "zone_id": str(camera.zone_id),
                        "name": camera.name,
                        "status": camera.status,
                        "detail": entry.detail,
                    },
                )
            )
            if entry.status == "online":
                cleared += await alert_service.clear_camera_offline(session, camera.zone_id)

    await session.commit()
    await events.publish_many(outbound)
    return {"received": len(payload.cameras), "status_changed": changed, "alerts_cleared": cleared}


@router.get("/config", response_model=EngineConfig)
async def engine_config(session: AsyncSession = Depends(get_session)) -> EngineConfig:
    """Everything the AI engine needs to run, pulled at boot and on reload.

    The engine keeps no configuration of its own beyond how to reach here.  That
    is what makes "restart the container" a safe operation during the Wari:
    it comes back with the current zone areas and the current calibrations, not
    with whatever it was started with three days ago.
    """
    zone_rows = await session.execute(select(Zone).where(Zone.is_active.is_(True)).order_by(Zone.code))
    zone_list = list(zone_rows.scalars())

    cameras_by_zone: dict[uuid.UUID, list[EngineCamera]] = {}
    if zone_list:
        camera_rows = await session.execute(
            select(Camera).where(Camera.zone_id.in_([z.id for z in zone_list])).order_by(Camera.name)
        )
        for camera in camera_rows.scalars():
            homography = Homography.from_json(camera.homography_matrix)
            cameras_by_zone.setdefault(camera.zone_id, []).append(
                EngineCamera(
                    camera_id=camera.id,
                    name=camera.name,
                    stream_url=camera.stream_url,
                    homography=list(homography.matrix) if homography else None,
                    is_tripwire_enabled=camera.is_tripwire_enabled,
                )
            )

    return EngineConfig(
        crowd_source=settings.crowd_source,
        zones=[
            EngineZone(
                zone_id=z.id,
                code=z.code,
                name=z.name,
                area_m2=z.area_m2,
                capacity_persons=z.capacity_persons,
                zone_type=z.zone_type,
                cameras=cameras_by_zone.get(z.id, []),
            )
            for z in zone_list
        ],
        stagnation_threshold=await config_service.get_float(session, "stagnation_alert_threshold"),
        counterflow_threshold=await config_service.get_float(session, "counterflow_alert_threshold"),
        generated_at=now_utc(),
    )


# ---------------------------------------------------------------------------
# breach crossings (Phase 6, Section 4/M5)
# ---------------------------------------------------------------------------
@router.post("/breach", response_model=CrossingResult, status_code=202)
async def ingest_crossings(
    payload: CrossingBatch,
    session: AsyncSession = Depends(get_session),
) -> CrossingResult:
    """Tripwire crossings from the vision pipeline.

    Most of what arrives here never becomes a record, and that is the design
    working rather than failing. The engine reports every crossing it sees; this
    endpoint applies the three filters Section 4/M5 specifies — wrong direction,
    gate open, a valid pass scanned within ±30 seconds — and only what survives
    all three is written to the ledger.

    Rejection reasons are counted and returned. "The engine saw 40 crossings and
    the ledger has 3" is a question somebody will ask during a review, and the
    37 reasons are the answer. Without this the discrepancy looks like data loss.

    Note what the engine is not permitted to send and has nowhere to put: a
    track id, a bounding box, an appearance vector. Track ids are ephemeral and
    in-memory on the engine side (Section 12); they stop existing at this
    boundary because there is no field for them.
    """
    moment = now_utc()
    recorded = 0
    reasons: dict[str, int] = {}
    sequences: list[int] = []

    for entry in payload.crossings:
        age = moment - entry.occurred_at
        if age > MAX_BACKDATE or -age > MAX_FUTURE:
            # Same guard as density, and it matters more here: `occurred_at` is
            # inside the hash, so an engine with a wrong clock would write a bad
            # timestamp that the immutability trigger then makes permanent.
            reasons["timestamp outside the accepted window"] = (
                reasons.get("timestamp outside the accepted window", 0) + 1
            )
            continue

        try:
            outcome = await breach_service.record_crossing(
                session,
                breach_service.Crossing(
                    tripwire_id=entry.tripwire_id,
                    occurred_at=entry.occurred_at,
                    direction=entry.direction,
                    confidence=entry.confidence,
                    crossing_count=entry.crossing_count,
                    clip_uri=entry.clip_uri,
                    clip_sha256=entry.clip_sha256,
                ),
                at=moment,
            )
        except AppError as exc:
            # One unknown tripwire must not discard the rest of the batch.
            reasons[exc.code] = reasons.get(exc.code, 0) + 1
            continue

        if outcome.recorded and outcome.event is not None:
            recorded += 1
            sequences.append(outcome.event.sequence)
        else:
            reasons[outcome.reason] = reasons.get(outcome.reason, 0) + 1

    await session.commit()

    if recorded:
        logger.info("breach_ingest", extra={"recorded": recorded, "ignored": len(payload.crossings) - recorded})

    return CrossingResult(
        recorded=recorded,
        ignored=len(payload.crossings) - recorded,
        reasons=reasons,
        sequences=sequences,
        received_at=moment,
    )
