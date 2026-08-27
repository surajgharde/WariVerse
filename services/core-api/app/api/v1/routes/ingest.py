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
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import events, metrics
from app.core.config import settings
from app.core.db import get_session
from app.core.deps import require_ai_service
from app.core.errors import AppError
from app.core.logging import get_logger
from app.core.security import now_utc
from app.models import Alert, Camera, Pass, PassStatus, Slot, Zone
from app.schemas.breach import CrossingBatch, CrossingResult
from app.schemas.common import ErrorResponse
from app.schemas.crowd import (
    DensityIngest,
    EngineCamera,
    EngineConfig,
    EngineContext,
    EngineZone,
    ForecastIngest,
    ForecastIngestResult,
    HeartbeatBatch,
    IngestResult,
    SlotPressure,
)
from app.services import alert_service, breach_service, config_service, crowd_service, forecast_service, recommendations
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
            metrics.READINGS_REJECTED.labels(exc.code).inc()
            continue

        snapshots.append(snapshot)
        outbound.append((events.DENSITY_UPDATED, snapshot.to_json()))

        # Section 11 asks for metrics per *zone pipeline*, not only per endpoint.
        # Set here rather than collected on scrape: every value is already in
        # hand, and a /metrics scrape must never become a database query.
        metrics.READINGS_INGESTED.labels(snapshot.source).inc()
        metrics.observe_zone(
            zone_code=snapshot.zone_code,
            density=snapshot.density,
            person_count=snapshot.person_count,
            stagnation_index=snapshot.stagnation_index,
            counterflow_ratio=snapshot.counterflow_ratio,
            confidence=snapshot.confidence,
            age_seconds=snapshot.age_seconds,
        )

        outcome = await alert_service.evaluate(session, snapshot, thresholds, at=received)
        if outcome.created and outcome.alert is not None:
            raised += 1
            metrics.observe_alert_raised(outcome.alert.type, outcome.alert.severity)
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


@router.post("/forecast", response_model=ForecastIngestResult, status_code=202)
async def ingest_forecast(
    payload: ForecastIngest,
    session: AsyncSession = Depends(get_session),
) -> ForecastIngestResult:
    """Accept a set of predictions issued at one moment (Section 4/M6).

    The engine owns the model; the core API owns the claim.  Same boundary as
    density: the AI service holds no database credentials, so a forecast reaches
    Postgres the only way anything from that service does.

    A prediction *is* allowed to be about the future — that is the whole point —
    so unlike `/density` this route checks `issued_at` rather than the target,
    and refuses one issued in the future for the same reason: a container with a
    wrong clock would otherwise stamp predictions that never look stale.
    """
    received = now_utc()
    skew = received - payload.issued_at
    if skew > MAX_BACKDATE or skew < -MAX_FUTURE:
        raise AppError(
            "READING_REJECTED",
            details={
                "reason": "issued_at outside the accepted window — check the engine's clock",
                "skew_seconds": round(skew.total_seconds(), 1),
            },
        )

    zones = await crowd_service.load_zones(session)
    by_code = {z.code: z for z in zones.values()}
    thresholds = await alert_service.load_thresholds(session)
    alert_horizon = await config_service.get_int(session, "forecast_alert_horizon_minutes")

    resolved: list[forecast_service.ForecastIn] = []
    rejections: list[dict[str, object]] = []
    outbound: list[tuple[str, dict[str, object]]] = []
    raised = 0

    for index, item in enumerate(payload.forecasts):
        zone: Zone | None = None
        if item.zone_id is not None:
            zone = zones.get(item.zone_id)
        elif item.zone_code:
            zone = by_code.get(item.zone_code.upper())

        if zone is None:
            rejections.append(
                {
                    "index": index,
                    "zone": str(item.zone_id or item.zone_code),
                    "reason": "unknown or inactive zone",
                }
            )
            continue

        resolved.append(
            forecast_service.ForecastIn(
                zone_id=zone.id,
                horizon_minutes=item.horizon_minutes,
                predicted_density=item.predicted_density,
                interval_low=item.interval_low,
                interval_high=item.interval_high,
                model_version=item.model_version,
                trained_on=item.trained_on,
                validation_mae=item.validation_mae,
            )
        )

        # Only the alerting horizon reaches the rule table. The rest are
        # published for the chart and are deliberately not events.
        if item.horizon_minutes != alert_horizon:
            continue

        signal = recommendations.ForecastSignal(
            predicted_density=item.predicted_density,
            interval_low=item.interval_low,
            interval_high=item.interval_high,
            horizon_minutes=item.horizon_minutes,
            target_at=payload.issued_at + timedelta(minutes=item.horizon_minutes),
            zone_name=zone.name,
            zone_name_mr=zone.name_mr,
        )
        outcome = await alert_service.evaluate_forecast(
            session,
            signal,
            zone.id,
            thresholds,
            confidence=alert_service.forecast_confidence(item.interval_low, item.interval_high),
            at=received,
        )
        if outcome.created and outcome.alert is not None:
            raised += 1
            outbound.append((events.ALERT_RAISED, _forecast_alert_event(outcome.alert, zone)))
        elif outcome.refreshed and outcome.alert is not None:
            outbound.append((events.ALERT_UPDATED, _forecast_alert_event(outcome.alert, zone)))
        for closed in outcome.resolved:
            outbound.append((events.ALERT_UPDATED, _forecast_alert_event(closed, zone)))

    accepted = await forecast_service.record(session, payload.issued_at, resolved)
    await session.commit()

    if accepted:
        outbound.append(
            (
                events.FORECAST_PUBLISHED,
                {
                    "issued_at": payload.issued_at,
                    "count": accepted,
                    "horizons": sorted({f.horizon_minutes for f in resolved}),
                    "trained_on": sorted({f.trained_on for f in resolved}),
                },
            )
        )
    await events.publish_many(outbound)

    if rejections:
        logger.warning("forecast_partial", extra={"accepted": accepted, "rejected": len(rejections)})

    return ForecastIngestResult(
        accepted=accepted,
        rejected=len(rejections),
        alerts_raised=raised,
        rejections=rejections[:20],
        received_at=received,
    )


def _forecast_alert_event(alert: Alert, zone: Zone) -> dict[str, object]:
    """A forecast alert on the wire.

    Same shape as `_alert_event` so the console's alert feed needs no second
    code path, but built from the zone rather than a snapshot — there is no
    density reading behind a forecast alert, which is precisely its value.
    """
    return {
        "alert_id": str(alert.id),
        "type": alert.type,
        "severity": alert.severity,
        "status": alert.status,
        "rule_id": alert.rule_id,
        "zone_id": str(zone.id),
        "zone_code": zone.code,
        "zone_name_mr": zone.name_mr,
        "trigger_metric": alert.trigger_metric,
        "trigger_value": alert.trigger_value,
        "threshold_value": alert.threshold_value,
        "confidence": alert.confidence,
        "recommended_action": alert.recommended_action,
        "recommended_action_mr": alert.recommended_action_mr,
        "observed_at": alert.observed_at,
    }


@router.get("/context", response_model=EngineContext)
async def engine_context(session: AsyncSession = Depends(get_session)) -> EngineContext:
    """Forecast features that live in this database, not the engine's memory.

    Section 4/M6 lists "active pass bookings for upcoming slots" as a feature.
    The engine cannot query for it — it holds no database credentials, and that
    boundary is worth more than the convenience of relaxing it — so the number
    comes to the engine instead.

    `unavailable_features` names the listed inputs this deployment cannot supply,
    so the engine records what it is running without rather than silently
    treating a missing input as a zero.  A model that reads "no Palkhi arriving"
    when the truth is "we do not track the Palkhi yet" is a model that will be
    confidently wrong on the one day it matters.
    """
    now = now_utc()
    horizon = now + timedelta(hours=4)

    rows = await session.execute(
        select(Slot.date, Slot.start_time, func.coalesce(func.sum(Pass.group_size), 0))
        .outerjoin(Pass, (Pass.slot_id == Slot.id) & (Pass.status == PassStatus.ACTIVE))
        .where(Slot.date >= now.date())
        .group_by(Slot.date, Slot.start_time)
        .order_by(Slot.date, Slot.start_time)
    )

    slots: list[SlotPressure] = []
    for day, start_time, booked in rows:
        starts_at = datetime.combine(day, start_time, tzinfo=UTC)
        if starts_at < now or starts_at > horizon:
            continue
        slots.append(SlotPressure(starts_at=starts_at, booked_persons=int(booked)))

    return EngineContext(
        slots=slots,
        unavailable_features=[
            # Both are named in Section 4/M6's feature list and neither has a
            # source in this system today. Named rather than omitted.
            "palkhi_eta (Phase 9 — Dindi tracking is not built)",
            "weather (no weather feed is configured)",
        ],
        generated_at=now,
    )


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
