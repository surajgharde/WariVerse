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
from app.services.recommendations import CrowdSignal, ForecastSignal, Recommendation, Thresholds

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
    read from `system_config` and can be changed without a deploy.  The Palkhi
    deviation window is in the same category: Section 4/M8 names 45 minutes, but
    the first two days of a route run to a much looser clock than the last two.
    """
    return Thresholds(
        density_high=DENSITY_THRESHOLDS[DensityLevel.MODERATE],
        density_critical=DENSITY_THRESHOLDS[DensityLevel.HIGH],
        stagnation=await config_service.get_float(session, "stagnation_alert_threshold"),
        counterflow=await config_service.get_float(session, "counterflow_alert_threshold"),
        dindi_deviation_minutes=await config_service.get_float(session, "dindi_deviation_minutes"),
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
        # A forecast alert must never be closed by the density path. Its entire
        # claim is that the zone is calm *now* and will not be in an hour —
        # three calm readings are the state it predicted, not evidence against
        # it. It is resolved by `evaluate_forecast` when the next prediction
        # comes back down, or by an operator.
        if alert.type == recommendations.FORECAST_HIGH:
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
# forecast alerts (Phase 8, Section 4/M6)
# ---------------------------------------------------------------------------
async def evaluate_forecast(
    session: AsyncSession,
    signal: ForecastSignal,
    zone_id: uuid.UUID,
    thresholds: Thresholds,
    *,
    confidence: float,
    at: datetime | None = None,
) -> AlertOutcome:
    """Raise, refresh or clear a zone's `forecast_high` alert.

    Only one horizon is allowed to reach here (`forecast_alert_horizon_minutes`,
    default 60).  Alerting on all three would page an operator three times about
    one approaching surge, at 30, 60 and 90 minutes out, and the third page is
    the one that gets the feed muted.  The other horizons are still published and
    still rendered — they are context on the chart, not events in the feed.

    A forecast alert carries the model's own uncertainty as its confidence, so an
    alert built on a wide interval reads as the estimate it is (Section 0 rule 3).
    """
    moment = at or now_utc()
    recommendation = recommendations.evaluate_forecast(signal, thresholds)
    live = await open_alerts_for(session, zone_id)
    existing = next((a for a in live if a.type == recommendations.FORECAST_HIGH), None)

    if recommendation is None:
        # The forecast has come back down. Close it immediately rather than
        # waiting for a calm streak: unlike a density reading, a forecast is
        # already an average over a model's view of the next hour, and a second
        # opinion arrives only every five minutes.
        if existing is None:
            return AlertOutcome(alert=None, created=False, refreshed=False, resolved=[])
        existing.status = str(AlertStatus.RESOLVED)
        existing.resolved_at = moment
        existing.updated_at = moment
        logger.info("forecast_alert_cleared", extra={"alert_id": str(existing.id), "zone_id": str(zone_id)})
        return AlertOutcome(alert=None, created=False, refreshed=False, resolved=[existing])

    rule = recommendation.rule
    if existing is not None:
        worse = round(signal.predicted_density, 4) > existing.trigger_value
        cooled = (moment - existing.updated_at).total_seconds() >= settings.alert_cooldown_seconds
        if not (worse or cooled):
            return AlertOutcome(alert=existing, created=False, refreshed=False, resolved=[])
        if worse:
            existing.trigger_value = round(signal.predicted_density, 4)
            existing.observed_at = signal.target_at
            existing.confidence = confidence
            existing.severity = str(rule.severity)
            existing.rule_id = rule.id
            existing.recommended_action = recommendation.action
            existing.recommended_action_mr = recommendation.action_mr
        existing.updated_at = moment
        return AlertOutcome(alert=existing, created=False, refreshed=True, resolved=[])

    alert = Alert(
        type=recommendations.FORECAST_HIGH,
        severity=str(rule.severity),
        zone_id=zone_id,
        trigger_metric="predicted_density",
        trigger_value=round(signal.predicted_density, 4),
        threshold_value=rule.threshold,
        confidence=confidence,
        # The moment being predicted, not the moment of prediction. An operator
        # reading "observed 15:30" on a 14:30 alert is reading the deadline.
        observed_at=signal.target_at,
        recommended_action=recommendation.action,
        recommended_action_mr=recommendation.action_mr,
        rule_id=rule.id,
        status=str(AlertStatus.OPEN),
        created_at=moment,
        updated_at=moment,
    )
    session.add(alert)
    await session.flush()
    logger.info(
        "forecast_alert_raised",
        extra={
            "alert_id": str(alert.id),
            "zone_id": str(zone_id),
            "rule": rule.id,
            "predicted": signal.predicted_density,
            "horizon": signal.horizon_minutes,
            "interval_width": round(signal.interval_width, 3),
        },
    )
    return AlertOutcome(alert=alert, created=True, refreshed=False, resolved=[])


def forecast_confidence(interval_low: float, interval_high: float) -> float:
    """Turn an interval width into the confidence carried on the alert.

    A band two people per square metre wide is a model shrugging.  Mapping width
    to confidence linearly over that range gives an operator one number with the
    same meaning it has everywhere else in this system — how much to trust what
    they are reading — rather than asking them to do the arithmetic from two
    bounds under pressure.
    """
    width = max(0.0, interval_high - interval_low)
    return round(max(0.05, min(1.0, 1.0 - width / 2.0)), 3)


# ---------------------------------------------------------------------------
# palkhi deviation alerts (Phase 9, Section 4/M8)
# ---------------------------------------------------------------------------
#: Alert types keyed to a Dindi rather than to a zone.
PALKHI_TYPES = (
    recommendations.PALKHI_DEVIATION,
    recommendations.PALKHI_SIGNAL_LOST,
    recommendations.PALKHI_OFF_ROUTE,
)


async def open_alerts_for_dindi(session: AsyncSession, dindi_id: uuid.UUID) -> list[Alert]:
    rows = await session.execute(
        select(Alert).where(
            Alert.dindi_id == dindi_id,
            Alert.status.in_([str(s) for s in LIVE_STATUSES]),
        )
    )
    return list(rows.scalars())


async def evaluate_dindi(
    session: AsyncSession,
    signal: recommendations.DindiSignal,
    dindi_id: uuid.UUID,
    halt_town_id: uuid.UUID,
    thresholds: Thresholds,
    *,
    at: datetime | None = None,
) -> AlertOutcome:
    """Raise, refresh or clear one Dindi's deviation alert.

    One open deviation alert per Dindi, not one per halt town it will reach.
    A group that is an hour early is an hour early for the whole rest of the
    route; raising an alert per downstream town would put fourteen rows in the
    feed for one fact and bury the Dindi that is genuinely in trouble.  The
    alert carries the *next* town, and moves to the following one when this one
    is passed.

    A deviation alert is also **cleared as soon as the gap closes**, without the
    calm-streak delay a density alert gets.  A walking group that has caught up
    has caught up; there is no equivalent of a crowd that momentarily thins.
    """
    moment = at or now_utc()
    recommendation = recommendations.evaluate_dindi(signal, thresholds)
    live = await open_alerts_for_dindi(session, dindi_id)
    existing = next((a for a in live if a.type == recommendations.PALKHI_DEVIATION), None)

    if recommendation is None:
        if existing is None:
            return AlertOutcome(alert=None, created=False, refreshed=False, resolved=[])
        existing.status = str(AlertStatus.RESOLVED)
        existing.resolved_at = moment
        existing.updated_at = moment
        logger.info("palkhi_deviation_cleared", extra={"dindi_id": str(dindi_id)})
        return AlertOutcome(alert=None, created=False, refreshed=False, resolved=[existing])

    rule = recommendation.rule
    gap = float(recommendation.gap_minutes)
    # A pace built on four dots on a map is an estimate and says so on its face
    # (Section 0 rule 3); a confident one is still an estimate about a walking
    # group, so it never reaches 1.0.
    confidence = 0.8 if signal.is_confident else 0.35

    if existing is not None:
        widened = gap > existing.trigger_value
        # The town changed under the alert — the Dindi passed the halt it was
        # late for and is now late for the next one. Same situation, new subject.
        retargeted = existing.halt_town_id != halt_town_id
        cooled = (moment - existing.updated_at).total_seconds() >= settings.alert_cooldown_seconds
        if not (widened or retargeted or cooled):
            return AlertOutcome(alert=existing, created=False, refreshed=False, resolved=[])
        if widened or retargeted:
            existing.trigger_value = gap
            existing.halt_town_id = halt_town_id
            existing.severity = str(rule.severity)
            existing.rule_id = rule.id
            existing.confidence = confidence
            existing.observed_at = signal.eta
            existing.recommended_action = recommendation.action
            existing.recommended_action_mr = recommendation.action_mr
        existing.updated_at = moment
        return AlertOutcome(alert=existing, created=False, refreshed=True, resolved=[])

    alert = Alert(
        type=recommendations.PALKHI_DEVIATION,
        severity=str(rule.severity),
        zone_id=None,
        dindi_id=dindi_id,
        halt_town_id=halt_town_id,
        trigger_metric="deviation_minutes",
        trigger_value=gap,
        threshold_value=thresholds.dindi_deviation_minutes,
        confidence=confidence,
        # The moment being predicted, not the moment of prediction — the same
        # convention forecast alerts follow. An operator reading "observed
        # 19:40" on an 18:10 alert is reading when the group will walk in.
        observed_at=signal.eta,
        recommended_action=recommendation.action,
        recommended_action_mr=recommendation.action_mr,
        rule_id=rule.id,
        status=str(AlertStatus.OPEN),
        created_at=moment,
        updated_at=moment,
    )
    session.add(alert)
    await session.flush()
    logger.info(
        "palkhi_deviation_raised",
        extra={
            "alert_id": str(alert.id),
            "dindi_id": str(dindi_id),
            "halt_town_id": str(halt_town_id),
            "rule": rule.id,
            "deviation_minutes": round(signal.deviation_minutes, 1),
            "pace_kmph": round(signal.pace_kmph, 2),
            "pace_samples": signal.pace_samples,
        },
    )
    return AlertOutcome(alert=alert, created=True, refreshed=False, resolved=[])


async def raise_palkhi_condition(
    session: AsyncSession,
    rule: recommendations.Rule,
    dindi_id: uuid.UUID,
    *,
    trigger_value: float,
    detail: str,
    detail_mr: str,
    observed_at: datetime,
    at: datetime | None = None,
) -> Alert | None:
    """One row for a Dindi's `signal_lost` or `off_route` condition.

    Returns None when the condition is already open, so a phone that has been
    dead for six hours is one alert, not three hundred and sixty.  These are the
    two conditions that mean *the system does not know*, which is why they are
    alerts at all rather than a quiet flag on a map.
    """
    moment = at or now_utc()
    live = await open_alerts_for_dindi(session, dindi_id)
    if any(a.type == rule.alert_type for a in live):
        return None

    alert = Alert(
        type=rule.alert_type,
        severity=str(rule.severity),
        zone_id=None,
        dindi_id=dindi_id,
        trigger_metric=rule.metric,
        trigger_value=trigger_value,
        threshold_value=rule.threshold,
        # The phone being silent, or the position being off the line, is an
        # observed fact rather than an inference — unlike everything else in
        # this module, it is not an estimate.
        confidence=1.0,
        observed_at=observed_at,
        recommended_action=f"{rule.action} ({detail})",
        recommended_action_mr=f"{rule.action_mr} ({detail_mr})",
        rule_id=rule.id,
        status=str(AlertStatus.OPEN),
        created_at=moment,
        updated_at=moment,
    )
    session.add(alert)
    await session.flush()
    logger.info(
        "palkhi_condition_raised",
        extra={"alert_id": str(alert.id), "dindi_id": str(dindi_id), "type": rule.alert_type},
    )
    return alert


async def clear_palkhi_condition(
    session: AsyncSession,
    dindi_id: uuid.UUID,
    alert_type: str,
    *,
    at: datetime | None = None,
) -> int:
    """A phone came back, or the group rejoined the route — close it without
    asking an operator to acknowledge something that fixed itself."""
    moment = at or now_utc()
    rows = await session.execute(
        select(Alert).where(
            Alert.dindi_id == dindi_id,
            Alert.type == alert_type,
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
