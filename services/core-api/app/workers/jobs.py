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
from app.models import PurgeLog, Zone
from app.services import (
    alert_service,
    audit_service,
    breach_service,
    config_service,
    crowd_service,
    incident_service,
    pass_service,
    reslot_service,
)

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

#: The shortest SLA is three minutes (critical).  Sweeping every 15 seconds means
#: a breach is visible within 8% of the window it breached, rather than being
#: announced up to a minute after the fact — on a three-minute clock, a
#: minute-granularity sweep is a third of the deadline spent not knowing.
SLA_SWEEP_INTERVAL_SECONDS = 15

#: Photo retention is measured in days, so this runs hourly.  Anything more
#: eager is a database scan every few minutes to find nothing.
PHOTO_PURGE_INTERVAL_SECONDS = 3600

#: Breach clips are on a 90-day clock; hourly is far more often than needed and
#: costs one indexed query.
BREACH_PURGE_INTERVAL_SECONDS = 3600
#: A tamper-evident ledger nobody checks is evident to nobody. Hourly means a
#: break is found within an hour of being made.
CHAIN_VERIFY_INTERVAL_SECONDS = 3600


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


@dataclass(frozen=True, slots=True)
class SlaSweepRun:
    ran_at: datetime
    breached: int
    worst_overdue_seconds: float


async def run_incident_sla(at: datetime | None = None) -> SlaSweepRun:
    """Mark incidents nobody was sent to in time, and say so on the socket.

    The breach is published as its own event type rather than as a generic
    update.  Every other incident event means somebody did something; this one
    means nobody did, and a console that cannot tell those apart will render the
    one message an operator most needs to notice as the twentieth row of a busy
    feed.
    """
    set_trace_id()
    moment = at or now_utc()
    outbound: list[tuple[str, dict[str, object]]] = []

    async with SessionFactory() as session:
        breaches = await incident_service.sweep_sla(session, at=moment)
        for breach in breaches:
            incident = breach.incident
            zone = await session.get(Zone, incident.zone_id) if incident.zone_id else None
            outbound.append(
                (
                    events.INCIDENT_SLA_BREACHED,
                    incident_service.event_payload(
                        incident,
                        zone=zone,
                        extra={"overdue_seconds": round(breach.overdue_seconds, 1)},
                    ),
                )
            )
        await session.commit()

    await events.publish_many(outbound)
    worst = max((b.overdue_seconds for b in breaches), default=0.0)
    if breaches:
        logger.warning(
            "incident_sla_sweep",
            extra={"breached": len(breaches), "worst_overdue_seconds": round(worst, 1)},
        )
    return SlaSweepRun(ran_at=moment, breached=len(breaches), worst_overdue_seconds=worst)


async def run_photo_purge(at: datetime | None = None) -> int:
    """Drop missing-person photo references past their retention (Section 12).

    Returns the count.  The object-store deletion is not done here on purpose —
    `purge_missing_person_photos` explains why: a purge that half-succeeds should
    leave the row pointing at the blob rather than orphan it.

    A `purge_log` row is written on *every* run, including the runs that purge
    nothing.  Section 12 asks for "auto-purge job with a purge log", and a log
    that only records the deletions cannot answer the question a governance
    review actually asks — not "what was deleted" but "has this been running".
    The empty rows are the evidence that it has.
    """
    set_trace_id()
    moment = at or now_utc()

    async with SessionFactory() as session:
        purged = await incident_service.purge_missing_person_photos(session, at=moment)
        session.add(
            PurgeLog(
                target_type="missing_person_photo",
                rows_affected=len(purged),
                cutoff=moment,
                executed_at=moment,
                detail={"retention_days": incident_service.PHOTO_RETENTION_DAYS},
            )
        )
        await session.commit()

    if purged:
        logger.info("missing_person_photos_purged", extra={"count": len(purged)})
    return len(purged)


async def run_breach_clip_purge(at: datetime | None = None) -> int:
    """Clear breach evidence clips past their retention window (Section 4/M5).

    The record and its hash stay; only the clip goes. A ledger whose rows
    disappeared on a 90-day timer would fail its own chain verification every
    quarter by design, which would train everyone to ignore the one alarm that
    is supposed to mean something.

    A `purge_log` row is written on every run, including the empty ones — the
    evidence that retention has been applied all season rather than since last
    Tuesday.
    """
    set_trace_id()
    moment = at or now_utc()

    async with SessionFactory() as session:
        retention_days = await config_service.get_int(session, "breach_retention_days")
        purged = await breach_service.purge_expired_clips(session, at=moment)
        session.add(
            PurgeLog(
                target_type="breach_clip",
                rows_affected=len(purged),
                cutoff=moment,
                executed_at=moment,
                detail={"retention_days": retention_days, "sequences": purged[:100]},
            )
        )
        await session.commit()

    if purged:
        logger.info("breach_clips_purged", extra={"count": len(purged)})
    return len(purged)


async def run_chain_verification(at: datetime | None = None) -> bool:
    """Verify the breach ledger's hash chain on a schedule.

    Section 4/M5 makes the chain the thing that survives political pressure, and
    a tamper-evident record nobody checks is a record that is evident to nobody.
    Running it hourly means a break is discovered within an hour of happening
    rather than at the moment somebody needs the ledger to hold up.

    A failure is logged at ERROR and written to the audit log, which is
    append-only — so the discovery of tampering cannot itself be quietly
    removed.
    """
    set_trace_id()
    moment = at or now_utc()

    async with SessionFactory() as session:
        report = await breach_service.verify_chain(session)
        if not report.intact:
            logger.error(
                "breach_chain_verification_failed",
                extra={
                    "breaks": len(report.breaks),
                    "first_break_sequence": report.breaks[0].sequence if report.breaks else None,
                    "events_checked": report.events_checked,
                },
            )
            await audit_service.record(
                session,
                action=audit_service.AuditAction.AUDIT_VIEWED,
                actor_id=None,
                actor_role="system",
                target_type="breach_chain",
                meta={
                    "scheduled_check": True,
                    "intact": False,
                    "breaks": [
                        {"sequence": b.sequence, "problem": b.problem} for b in report.breaks[:20]
                    ],
                    "events_checked": report.events_checked,
                    "verified_at": moment.isoformat(),
                },
            )
            await session.commit()
    return report.intact


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


async def scheduled_incident_sla() -> None:
    # A 10-second lock TTL on a 15-second interval: short enough that a replica
    # dying mid-sweep costs one tick rather than blocking the next one.
    if not await _acquire("incident_sla", SLA_SWEEP_INTERVAL_SECONDS - 5):
        return
    await run_incident_sla()


async def scheduled_photo_purge() -> None:
    if not await _acquire("photo_purge", PHOTO_PURGE_INTERVAL_SECONDS - 60):
        return
    await run_photo_purge()


async def scheduled_breach_purge() -> None:
    if not await _acquire("breach_purge", BREACH_PURGE_INTERVAL_SECONDS - 60):
        return
    await run_breach_clip_purge()


async def scheduled_chain_verification() -> None:
    if not await _acquire("chain_verify", CHAIN_VERIFY_INTERVAL_SECONDS - 60):
        return
    await run_chain_verification()
