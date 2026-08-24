"""Background jobs for the pass system and the crowd engine.

Plain async functions that each open their own session.  They are invoked by
`scheduler.py` today; moving them behind Celery later means registering these
same callables as tasks, with no change to the logic.

Each job takes a Redis lock so that running three API replicas does not reslot
the same passes three times.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.core import events
from app.core.db import SessionFactory
from app.core.logging import get_logger, set_trace_id
from app.core.redis_client import aw, redis
from app.core.security import now_utc
from app.models import Zone
from app.services import alert_service, config_service, crowd_service, pass_service, reslot_service

logger = get_logger(__name__)

RESLOT_INTERVAL_SECONDS = 300  # "a background job runs every 5 minutes"
EXPIRY_INTERVAL_SECONDS = 300
THROUGHPUT_WINDOW_MINUTES = 30

#: The watchdog must run well inside `camera_offline_seconds` or a camera can be
#: dead for the whole grace window plus a whole tick before anyone is told.
CAMERA_WATCHDOG_INTERVAL_SECONDS = 30
#: The escalation clock's finest granularity is `alert_escalate_seconds` (60s by
#: default), so checking twice as often as that is enough to be punctual.
ALERT_MAINTENANCE_INTERVAL_SECONDS = 30


@dataclass(frozen=True, slots=True)
class ReslotRun:
    ran_at: datetime
    window: reslot_service.ThroughputWindow
    decision: reslot_service.ReslotDecision
    passes_moved: int


async def _acquire(name: str, ttl_seconds: int) -> bool:
    """Single-runner lock.  If Redis is down, run anyway.

    A missed reslot is a pilgrim standing on a promise that has gone stale;
    a duplicated one shifts passes twice. Neither is good, but with Redis gone
    the system is already degraded and the job's own idempotency (it recomputes
    from current throughput each run) makes running the safer default.
    """
    try:
        acquired = await aw(redis.set(f"job:lock:{name}", "1", ex=ttl_seconds, nx=True))
        return bool(acquired)
    except Exception as exc:
        logger.warning("job_lock_unavailable", extra={"job": name, "error": str(exc)})
        return True


async def run_reslot(at: datetime | None = None, *, force: bool = False) -> ReslotRun:
    """Compare planned versus actual throughput and shift downstream passes."""
    set_trace_id()
    moment = at or now_utc()

    async with SessionFactory() as session:
        window = await pass_service.measure_throughput(
            session, at=moment, window_minutes=THROUGHPUT_WINDOW_MINUTES
        )
        threshold = await config_service.get_float(session, "reslot_deviation_pct")
        slot_minutes = await config_service.get_int(session, "slot_minutes")

        decision = reslot_service.decide(
            window,
            deviation_threshold=0.0 if force else threshold,
            slot_minutes=slot_minutes,
        )

        moved = await pass_service.apply_reslot(session, decision, at=moment)
        await session.commit()

    logger.info(
        "reslot_job",
        extra={
            "planned": window.planned,
            "actual": window.actual,
            "deviation": round(decision.deviation, 4),
            "should_reslot": decision.should_reslot,
            "delay_minutes": decision.delay_minutes,
            "passes_moved": moved,
        },
    )
    return ReslotRun(ran_at=moment, window=window, decision=decision, passes_moved=moved)


async def run_expire_no_shows(at: datetime | None = None) -> int:
    """Release capacity held by passes nobody turned up for."""
    set_trace_id()
    async with SessionFactory() as session:
        expired = await pass_service.expire_no_shows(session, at=at)
        await session.commit()
    return expired


@dataclass(frozen=True, slots=True)
class WatchdogRun:
    ran_at: datetime
    cameras_marked_offline: int
    zones_alerted: int


async def run_camera_watchdog(at: datetime | None = None) -> WatchdogRun:
    """Mark silent cameras offline and tell the operator which zone went blind.

    Section 1: the system degrades rather than fails.  A zone that loses its
    cameras keeps its last reading visible — but with a coverage alert against
    it and a confidence figure that says "estimate", so nobody mistakes a frozen
    number for a live one.  The failure that matters here is the silent one.
    """
    set_trace_id()
    moment = at or now_utc()
    outbound: list[tuple[str, dict[str, object]]] = []
    zones_alerted = 0

    async with SessionFactory() as session:
        stale = await crowd_service.stale_cameras(session, at=moment)
        for camera in stale:
            camera.status = "offline"
            zone = await session.get(Zone, camera.zone_id)
            outbound.append(
                (
                    events.CAMERA_STATUS_CHANGED,
                    {
                        "camera_id": str(camera.id),
                        "zone_id": str(camera.zone_id),
                        "name": camera.name,
                        "status": "offline",
                        "detail": "no heartbeat",
                    },
                )
            )
            alert = await alert_service.raise_camera_offline(session, camera, zone, at=moment)
            if alert is not None:
                zones_alerted += 1
                outbound.append(
                    (
                        events.ALERT_RAISED,
                        {
                            "alert_id": str(alert.id),
                            "type": alert.type,
                            "severity": alert.severity,
                            "status": alert.status,
                            "rule_id": alert.rule_id,
                            "zone_id": str(zone.id) if zone else None,
                            "zone_code": zone.code if zone else None,
                            "zone_name_mr": zone.name_mr if zone else None,
                            "recommended_action": alert.recommended_action,
                            "recommended_action_mr": alert.recommended_action_mr,
                            "confidence": alert.confidence,
                        },
                    )
                )
        await session.commit()

    await events.publish_many(outbound)
    if stale:
        logger.info(
            "camera_watchdog",
            extra={"marked_offline": len(stale), "zones_alerted": zones_alerted},
        )
    return WatchdogRun(ran_at=moment, cameras_marked_offline=len(stale), zones_alerted=zones_alerted)


@dataclass(frozen=True, slots=True)
class AlertMaintenanceRun:
    ran_at: datetime
    escalated: int
    paged: int
    expired: int


async def run_alert_maintenance(at: datetime | None = None) -> AlertMaintenanceRun:
    """Escalate what nobody answered, expire what nobody ever closed."""
    set_trace_id()
    moment = at or now_utc()
    outbound: list[tuple[str, dict[str, object]]] = []

    async with SessionFactory() as session:
        escalations = await alert_service.escalate_unacknowledged(session, at=moment)
        expired = await alert_service.expire_stale(session, at=moment)
        for step in escalations:
            outbound.append(
                (
                    events.ALERT_UPDATED,
                    {
                        "alert_id": str(step.alert.id),
                        "status": step.alert.status,
                        "severity": step.alert.severity,
                        "escalation_level": step.level,
                        "seconds_unacknowledged": round(step.seconds_open, 1),
                        "zone_id": str(step.alert.zone_id) if step.alert.zone_id else None,
                    },
                )
            )
        await session.commit()

    await events.publish_many(outbound)
    paged = sum(1 for e in escalations if e.level >= 2)
    if escalations or expired:
        logger.info(
            "alert_maintenance",
            extra={"escalated": len(escalations), "paged": paged, "expired": expired},
        )
    return AlertMaintenanceRun(ran_at=moment, escalated=len(escalations), paged=paged, expired=expired)


async def scheduled_reslot() -> None:
    if not await _acquire("reslot", RESLOT_INTERVAL_SECONDS - 5):
        return
    await run_reslot()


async def scheduled_expiry() -> None:
    if not await _acquire("expire_no_shows", EXPIRY_INTERVAL_SECONDS - 5):
        return
    await run_expire_no_shows()


async def scheduled_camera_watchdog() -> None:
    if not await _acquire("camera_watchdog", CAMERA_WATCHDOG_INTERVAL_SECONDS - 5):
        return
    await run_camera_watchdog()


async def scheduled_alert_maintenance() -> None:
    if not await _acquire("alert_maintenance", ALERT_MAINTENANCE_INTERVAL_SECONDS - 5):
        return
    await run_alert_maintenance()
