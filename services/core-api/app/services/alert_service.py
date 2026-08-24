"""Turning readings into alerts an operator can act on.

Two failure modes to avoid, and they pull in opposite directions:

* **Missing the alert.** A zone crosses 5 p/m² and nobody is told.
* **Burying the alert.** A zone sits at 5.2 p/m² for ten minutes and produces
  sixty rows, the feed becomes wallpaper, and the *next* alert is missed.

So: one open alert per zone per rule, refreshed rather than duplicated while the
condition persists (`alert_cooldown_seconds`), auto-resolved when the zone comes
back down and stays down, and escalated visually — then to the next role — when
a CRITICAL sits unacknowledged (`alert_escalate_seconds`, `alert_page_seconds`).

The *content* of an alert is not decided here.  It comes from the numbered rule
table in `recommendations.py`, and the rule id travels with the row so an
operator can see which rule spoke.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AppError
from app.core.logging import get_logger
from app.core.security import now_utc
from app.models import Alert, Camera, Zone
from app.models.crowd import DENSITY_THRESHOLDS, AlertSeverity, AlertStatus, DensityLevel
from app.services import audit_service, config_service, recommendations
from app.services.audit_service import AuditAction
from app.services.crowd_service import ZoneSnapshot
from app.services.recommendations import CrowdSignal, Recommendation, Thresholds

logger = get_logger(__name__)

#: How many consecutive calm readings before an open density alert closes
#: itself.  One quiet frame is noise; three across thirty seconds is a trend.
CALM_READINGS_TO_RESOLVE = 3

#: Statuses that still occupy an operator's attention.
LIVE_STATUSES = (AlertStatus.OPEN, AlertStatus.ACKNOWLEDGED, AlertStatus.ESCALATED)

_calm_streak: dict[tuple[uuid.UUID, str], int] = {}


def reset_streaks() -> None:
    """Between tests, and after a config change that moves the bands."""
    _calm_streak.clear()


async def load_thresholds(session: AsyncSession) -> Thresholds:
    """Where each band comes from, and why they are not all in one place.

    The density bands are the published crowd-safety figures (Section 4/M2) and
    live in code as constants.  They are deliberately *not* operator-tunable: an
    administrator under pressure at 2 a.m. must not be able to make a critical
    zone look safe by editing a number.

    The stagnation and counter-flow thresholds are behavioural, site-specific
    and genuinely need tuning against the Pandharpur corridors — so those two
    read from `system_config` and can be changed without a deploy.
    """
    return Thresholds(
        density_high=DENSITY_THRESHOLDS[DensityLevel.MODERATE],
        density_critical=DENSITY_THRESHOLDS[DensityLevel.HIGH],
        stagnation=await config_service.get_float(session, "stagnation_alert_threshold"),
        counterflow=await config_service.get_float(session, "counterflow_alert_threshold"),
    )


def signal_from(snapshot: ZoneSnapshot) -> CrowdSignal:
    return CrowdSignal(
        density=snapshot.density,
        stagnation_index=snapshot.stagnation_index,
        counterflow_ratio=snapshot.counterflow_ratio,
        person_count=snapshot.person_count,
        confidence=snapshot.confidence,
        zone_name=snapshot.zone_name,
        zone_name_mr=snapshot.zone_name_mr,
    )


@dataclass(frozen=True, slots=True)
class AlertOutcome:
    """What `evaluate` did, so the caller knows whether to publish an event."""

    alert: Alert | None
    created: bool
    refreshed: bool
    resolved: list[Alert]


async def open_alerts_for(session: AsyncSession, zone_id: uuid.UUID) -> list[Alert]:
    rows = await session.execute(
        select(Alert).where(
            Alert.zone_id == zone_id,
            Alert.status.in_([str(s) for s in LIVE_STATUSES]),
        )
    )
    return list(rows.scalars())


async def evaluate(
    session: AsyncSession,
    snapshot: ZoneSnapshot,
    thresholds: Thresholds,
    *,
    at: datetime | None = None,
) -> AlertOutcome:
    """Run the rule table against one snapshot and reconcile the zone's alerts."""
    moment = at or now_utc()
    recommendation = recommendations.evaluate(signal_from(snapshot), thresholds)
    live = await open_alerts_for(session, snapshot.zone_id)

    # Alerts of a *different* type than the one now firing are candidates for
    # auto-resolve: a corridor that was stalled and is now merely busy should
    # not keep a stagnation alert open under a density alert.
    matched_type = recommendation.rule.alert_type if recommendation else None
    resolved = await _resolve_cleared(session, snapshot, live, keep=matched_type, at=moment)

    if recommendation is None:
        return AlertOutcome(alert=None, created=False, refreshed=False, resolved=resolved)

    _calm_streak.pop((snapshot.zone_id, recommendation.rule.alert_type), None)

    existing = next((a for a in live if a.type == recommendation.rule.alert_type), None)
    if existing is not None:
        refreshed = _refresh(existing, recommendation, snapshot, at=moment)
        return AlertOutcome(alert=existing, created=False, refreshed=refreshed, resolved=resolved)

    alert = _build(recommendation, snapshot, at=moment)
    session.add(alert)
    await session.flush()

    logger.info(
        "alert_raised",
        extra={
            "alert_id": str(alert.id),
            "zone": snapshot.zone_code,
            "type": alert.type,
            "rule": alert.rule_id,
            "value": alert.trigger_value,
            "confidence": alert.confidence,
        },
    )
    return AlertOutcome(alert=alert, created=True, refreshed=False, resolved=resolved)


def _build(recommendation: Recommendation, snapshot: ZoneSnapshot, *, at: datetime) -> Alert:
    rule = recommendation.rule
    return Alert(
        type=rule.alert_type,
        severity=str(rule.severity),
        zone_id=snapshot.zone_id,
        trigger_metric=rule.metric,
        trigger_value=round(recommendation.trigger_value, 4),
        threshold_value=rule.threshold,
        # Section 0 rule 3: an alert built on a 0.4-confidence estimate says so
        # on its face, so an operator weighs it as an estimate.
        confidence=snapshot.confidence,
        observed_at=snapshot.observed_at,
        recommended_action=recommendation.action,
        recommended_action_mr=recommendation.action_mr,
        rule_id=rule.id,
        status=str(AlertStatus.OPEN),
        created_at=at,
        updated_at=at,
    )


def _refresh(alert: Alert, recommendation: Recommendation, snapshot: ZoneSnapshot, *, at: datetime) -> bool:
    """Keep one row current while the condition persists.

    Only the *worst* value seen is kept.  An operator arriving late needs to
    know the peak, not whatever the last ten-second window happened to read.
    """
    cooled = (at - alert.updated_at).total_seconds() >= settings.alert_cooldown_seconds
    worse = round(recommendation.trigger_value, 4) > alert.trigger_value

    if not (cooled or worse):
        return False

    if worse:
        alert.trigger_value = round(recommendation.trigger_value, 4)
        alert.observed_at = snapshot.observed_at
        alert.confidence = snapshot.confidence
    alert.updated_at = at
    return True


async def _resolve_cleared(
    session: AsyncSession,
    snapshot: ZoneSnapshot,
    live: list[Alert],
    *,
    keep: str | None,
    at: datetime,
) -> list[Alert]:
    """Close alerts whose condition has stayed clear for a few readings."""
    closed: list[Alert] = []
    for alert in live:
        if alert.type == keep or alert.type == recommendations.CAMERA_OFFLINE:
            continue
        key = (snapshot.zone_id, alert.type)
        streak = _calm_streak.get(key, 0) + 1
        if streak < CALM_READINGS_TO_RESOLVE:
            _calm_streak[key] = streak
            continue

        _calm_streak.pop(key, None)
        alert.status = str(AlertStatus.RESOLVED)
        alert.resolved_at = at
        alert.updated_at = at
        closed.append(alert)
        logger.info(
            "alert_auto_resolved",
            extra={"alert_id": str(alert.id), "zone": snapshot.zone_code, "type": alert.type},
        )
    return closed


# ---------------------------------------------------------------------------
# camera coverage
# ---------------------------------------------------------------------------
async def raise_camera_offline(
    session: AsyncSession,
    camera: Camera,
    zone: Zone | None,
    *,
    at: datetime | None = None,
) -> Alert | None:
    """One alert per zone that has lost coverage, not one per camera.

    Losing three cameras in the same corridor is one problem for the operator.
    """
    moment = at or now_utc()
    if zone is not None:
        existing = await session.execute(
            select(Alert).where(
                Alert.zone_id == zone.id,
                Alert.type == recommendations.CAMERA_OFFLINE,
                Alert.status.in_([str(s) for s in LIVE_STATUSES]),
            )
        )
        if existing.scalars().first() is not None:
            return None

    rule = recommendations.CAMERA_OFFLINE_RULE
    alert = Alert(
        type=rule.alert_type,
        severity=str(rule.severity),
        zone_id=zone.id if zone else None,
        trigger_metric="seconds_since_heartbeat",
        trigger_value=float(settings.camera_offline_seconds),
        threshold_value=float(settings.camera_offline_seconds),
        confidence=1.0,  # the camera being silent is a fact, not an estimate
        observed_at=camera.last_heartbeat_at or moment,
        recommended_action=f"{rule.action} (camera: {camera.name})",
        recommended_action_mr=f"{rule.action_mr} (कॅमेरा: {camera.name})",
        rule_id=rule.id,
        status=str(AlertStatus.OPEN),
        created_at=moment,
        updated_at=moment,
    )
    session.add(alert)
    await session.flush()
    logger.info("camera_offline_alert", extra={"camera": camera.name, "zone_id": str(zone.id) if zone else None})
    return alert


async def clear_camera_offline(session: AsyncSession, zone_id: uuid.UUID, *, at: datetime | None = None) -> int:
    """A feed came back — close the coverage alert without operator action."""
    moment = at or now_utc()
    rows = await session.execute(
        select(Alert).where(
            Alert.zone_id == zone_id,
            Alert.type == recommendations.CAMERA_OFFLINE,
            Alert.status.in_([str(s) for s in LIVE_STATUSES]),
        )
    )
    cleared = 0
    for alert in rows.scalars():
        alert.status = str(AlertStatus.RESOLVED)
        alert.resolved_at = moment
        alert.updated_at = moment
        cleared += 1
    return cleared


# ---------------------------------------------------------------------------
# operator actions
# ---------------------------------------------------------------------------
async def load(session: AsyncSession, alert_id: uuid.UUID) -> Alert:
    alert = await session.get(Alert, alert_id)
    if alert is None:
        raise AppError("ALERT_NOT_FOUND", details={"alert_id": str(alert_id)})
    return alert


async def acknowledge(
    session: AsyncSession,
    alert: Alert,
    *,
    actor_id: uuid.UUID,
    actor_role: str,
    ip: str | None = None,
    user_agent: str | None = None,
    note: str | None = None,
) -> Alert:
    """Take ownership.  Acknowledging stops the escalation clock — which is
    exactly why it is audited: it is a claim that someone is handling it."""
    if alert.status in {str(AlertStatus.RESOLVED), str(AlertStatus.EXPIRED)}:
        raise AppError("ALERT_ALREADY_CLOSED", details={"status": alert.status})

    moment = now_utc()
    seconds_open = (moment - alert.created_at).total_seconds()

    alert.status = str(AlertStatus.ACKNOWLEDGED)
    alert.acknowledged_by = actor_id
    alert.acknowledged_at = moment
    alert.updated_at = moment

    await audit_service.record(
        session,
        action=AuditAction.ALERT_ACKNOWLEDGED,
        actor_id=actor_id,
        actor_role=actor_role,
        target_type="alert",
        target_id=alert.id,
        meta={
            "type": alert.type,
            "rule_id": alert.rule_id,
            "severity": alert.severity,
            "seconds_to_acknowledge": round(seconds_open, 1),
            "escalation_level": alert.escalation_level,
            "note": note,
        },
        ip=ip,
        user_agent=user_agent,
    )
    return alert


async def resolve(
    session: AsyncSession,
    alert: Alert,
    *,
    actor_id: uuid.UUID,
    actor_role: str,
    resolution: str | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> Alert:
    if alert.status in {str(AlertStatus.RESOLVED), str(AlertStatus.EXPIRED)}:
        raise AppError("ALERT_ALREADY_CLOSED", details={"status": alert.status})

    moment = now_utc()
    alert.status = str(AlertStatus.RESOLVED)
    alert.resolved_at = moment
    alert.updated_at = moment
    if alert.acknowledged_by is None:
        alert.acknowledged_by = actor_id
        alert.acknowledged_at = moment

    # Closing a CRITICAL by hand is the moment a human overrode the system.
    await audit_service.record(
        session,
        action=AuditAction.ALERT_RESOLVED,
        actor_id=actor_id,
        actor_role=actor_role,
        target_type="alert",
        target_id=alert.id,
        meta={
            "type": alert.type,
            "rule_id": alert.rule_id,
            "severity": alert.severity,
            "seconds_open": round((moment - alert.created_at).total_seconds(), 1),
            "resolution": resolution,
        },
        ip=ip,
        user_agent=user_agent,
    )
    return alert


# ---------------------------------------------------------------------------
# escalation (driven by the scheduler)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Escalation:
    alert: Alert
    level: int
    seconds_open: float


async def escalate_unacknowledged(session: AsyncSession, *, at: datetime | None = None) -> list[Escalation]:
    """Level 1 after `alert_escalate_seconds`, level 2 after `alert_page_seconds`.

    Level 1 is visual — the console makes it impossible to ignore.  Level 2 is
    the one that pages the next role up, and it is written to the audit log,
    because "nobody answered for three minutes" is a fact a review will need.
    """
    moment = at or now_utc()
    visual_after = await config_service.get_int(session, "alert_escalate_seconds")
    page_after = await config_service.get_int(session, "alert_page_seconds")

    rows = await session.execute(
        select(Alert).where(
            Alert.severity == str(AlertSeverity.CRITICAL),
            Alert.status.in_([str(AlertStatus.OPEN), str(AlertStatus.ESCALATED)]),
        )
    )

    escalated: list[Escalation] = []
    for alert in rows.scalars():
        open_for = (moment - alert.created_at).total_seconds()
        level = 2 if open_for >= page_after else (1 if open_for >= visual_after else 0)
        if level <= alert.escalation_level:
            continue

        alert.escalation_level = level
        alert.status = str(AlertStatus.ESCALATED)
        alert.escalated_at = moment
        alert.updated_at = moment

        if level >= 2:
            await audit_service.record(
                session,
                action=AuditAction.ALERT_ESCALATED,
                actor_id=None,
                actor_role="system",
                target_type="alert",
                target_id=alert.id,
                meta={
                    "type": alert.type,
                    "rule_id": alert.rule_id,
                    "seconds_unacknowledged": round(open_for, 1),
                    "escalation_level": level,
                },
            )
        escalated.append(Escalation(alert=alert, level=level, seconds_open=open_for))

    return escalated


async def expire_stale(session: AsyncSession, *, older_than_hours: int = 12, at: datetime | None = None) -> int:
    """Sweep alerts nobody ever closed, so the feed reflects today.

    EXPIRED is a distinct status from RESOLVED on purpose: "the situation ended"
    and "we stopped looking" are different things, and a Wari review needs to
    tell them apart.
    """
    moment = at or now_utc()
    cutoff = moment - timedelta(hours=older_than_hours)
    rows = await session.execute(
        select(Alert).where(
            Alert.status.in_([str(s) for s in LIVE_STATUSES]),
            Alert.created_at < cutoff,
        )
    )
    count = 0
    for alert in rows.scalars():
        alert.status = str(AlertStatus.EXPIRED)
        alert.updated_at = moment
        count += 1
    if count:
        logger.info("alerts_expired", extra={"count": count, "older_than_hours": older_than_hours})
    return count
