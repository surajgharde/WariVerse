"""Operator alert feed (Section 4/M2 step 5, feeds the Phase 4 command centre).

    GET  /alerts            GET  /alerts/{id}
    POST /alerts/{id}/ack   POST /alerts/{id}/resolve
    GET  /alerts/rules

`/alerts/rules` exists because Section 0 rule 3 says no AI output is presented
as certainty.  An operator who is told to close a gate can open the rule table
and read the exact condition that said so, in Marathi, before they act.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import events
from app.core.db import get_session
from app.core.deps import Actor, require
from app.core.permissions import Permission
from app.core.security import now_utc
from app.models import Alert, Zone
from app.models.crowd import AlertStatus
from app.schemas.common import ApiModel, ErrorResponse, Page
from app.schemas.crowd import AlertAck, AlertOut, AlertResolve
from app.services import alert_service, recommendations

router = APIRouter(tags=["alerts"], responses={404: {"model": ErrorResponse}})


class RuleOut(ApiModel):
    """One row of the recommendation table, as published to operators."""

    id: str
    alert_type: str
    severity: str
    metric: str
    threshold: float
    action: str
    action_mr: str


def _out(alert: Alert, zone: Zone | None, *, at: datetime | None = None) -> AlertOut:
    moment = at or now_utc()
    end = alert.resolved_at or moment
    return AlertOut(
        id=alert.id,
        type=alert.type,
        severity=alert.severity,
        status=alert.status,
        zone_id=alert.zone_id,
        zone_code=zone.code if zone else None,
        zone_name_mr=zone.name_mr if zone else None,
        trigger_metric=alert.trigger_metric,
        trigger_value=alert.trigger_value,
        threshold_value=alert.threshold_value,
        confidence=alert.confidence,
        observed_at=alert.observed_at,
        recommended_action=alert.recommended_action,
        recommended_action_mr=alert.recommended_action_mr,
        rule_id=alert.rule_id,
        escalation_level=alert.escalation_level,
        acknowledged_by=alert.acknowledged_by,
        acknowledged_at=alert.acknowledged_at,
        escalated_at=alert.escalated_at,
        resolved_at=alert.resolved_at,
        created_at=alert.created_at,
        seconds_open=round((end - alert.created_at).total_seconds(), 1),
    )


async def _zones_for(session: AsyncSession, alerts: list[Alert]) -> dict[uuid.UUID, Zone]:
    ids = {a.zone_id for a in alerts if a.zone_id}
    if not ids:
        return {}
    rows = await session.execute(select(Zone).where(Zone.id.in_(ids)))
    return {z.id: z for z in rows.scalars()}


@router.get("/alerts", response_model=Page[AlertOut])
async def list_alerts(
    status: str | None = Query(default=None, description="open | acknowledged | escalated | resolved | expired"),
    severity: str | None = None,
    zone_id: uuid.UUID | None = None,
    alert_type: str | None = Query(default=None, alias="type"),
    live_only: bool = Query(default=True, description="Only alerts still needing attention"),
    since_hours: int = Query(default=24, ge=1, le=720),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _: Actor = Depends(require(Permission.ALERT_VIEW)),
    session: AsyncSession = Depends(get_session),
) -> Page[AlertOut]:
    """The feed.  Ordered worst-first, then newest-first.

    Severity before recency deliberately: an operator scrolling a busy feed
    should hit the CRITICAL from four minutes ago before the INFO from four
    seconds ago.
    """
    stmt = select(Alert).where(Alert.created_at >= now_utc() - timedelta(hours=since_hours))
    if status:
        stmt = stmt.where(Alert.status == status)
    elif live_only:
        stmt = stmt.where(Alert.status.in_([str(s) for s in alert_service.LIVE_STATUSES]))
    if severity:
        stmt = stmt.where(Alert.severity == severity)
    if zone_id:
        stmt = stmt.where(Alert.zone_id == zone_id)
    if alert_type:
        stmt = stmt.where(Alert.type == alert_type)

    rank = case({"critical": 0, "warning": 1, "info": 2}, value=Alert.severity, else_=3)
    total = await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = await session.execute(
        stmt.order_by(rank, Alert.created_at.desc()).limit(limit).offset(offset)
    )
    alerts = list(rows.scalars())
    zones = await _zones_for(session, alerts)

    moment = now_utc()
    return Page[AlertOut](
        items=[_out(a, zones.get(a.zone_id) if a.zone_id else None, at=moment) for a in alerts],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/alerts/rules", response_model=list[RuleOut])
async def list_rules(_: Actor = Depends(require(Permission.ALERT_VIEW))) -> list[RuleOut]:
    """The rule table, exactly as the evaluator sees it.

    This endpoint is the answer to "why is it telling me to close the gate".
    """
    return [
        RuleOut(
            id=rule.id,
            alert_type=rule.alert_type,
            severity=str(rule.severity),
            metric=rule.metric,
            threshold=rule.threshold,
            action=rule.action,
            action_mr=rule.action_mr,
        )
        for rule in (*recommendations.RULES, recommendations.CAMERA_OFFLINE_RULE)
    ]


@router.get("/alerts/{alert_id}", response_model=AlertOut)
async def get_alert(
    alert_id: uuid.UUID,
    _: Actor = Depends(require(Permission.ALERT_VIEW)),
    session: AsyncSession = Depends(get_session),
) -> AlertOut:
    alert = await alert_service.load(session, alert_id)
    zone = await session.get(Zone, alert.zone_id) if alert.zone_id else None
    return _out(alert, zone)


@router.post("/alerts/{alert_id}/ack", response_model=AlertOut)
async def acknowledge_alert(
    alert_id: uuid.UUID,
    payload: AlertAck,
    actor: Actor = Depends(require(Permission.ALERT_ACKNOWLEDGE)),
    session: AsyncSession = Depends(get_session),
) -> AlertOut:
    """Claim an alert.  Stops the escalation clock, so it is audited.

    Acknowledging is a statement that a named person is handling this. The
    seconds-to-acknowledge figure lands in the audit log because a post-Wari
    review will ask how long the control room took, and "we felt it was quick"
    is not an answer.
    """
    alert = await alert_service.load(session, alert_id)
    await alert_service.acknowledge(
        session,
        alert,
        actor_id=actor.id,
        actor_role=actor.user.role,
        ip=actor.ip,
        user_agent=actor.user_agent,
        note=payload.note,
    )
    zone = await session.get(Zone, alert.zone_id) if alert.zone_id else None
    out = _out(alert, zone)
    await session.commit()

    await events.publish(
        events.ALERT_UPDATED,
        {"alert_id": str(alert.id), "status": alert.status, "acknowledged_by": str(actor.id)},
    )
    return out


@router.post("/alerts/{alert_id}/resolve", response_model=AlertOut)
async def resolve_alert(
    alert_id: uuid.UUID,
    payload: AlertResolve,
    actor: Actor = Depends(require(Permission.ALERT_ACKNOWLEDGE)),
    session: AsyncSession = Depends(get_session),
) -> AlertOut:
    """Close an alert by hand, with what was done.

    Alerts also close themselves when the zone comes back down and stays down.
    This route is for the other case: the operator acted, and the record of
    *what they did* is worth more later than the density number that started it.
    """
    alert = await alert_service.load(session, alert_id)
    await alert_service.resolve(
        session,
        alert,
        actor_id=actor.id,
        actor_role=actor.user.role,
        resolution=payload.resolution,
        ip=actor.ip,
        user_agent=actor.user_agent,
    )
    zone = await session.get(Zone, alert.zone_id) if alert.zone_id else None
    out = _out(alert, zone)
    await session.commit()

    await events.publish(
        events.ALERT_UPDATED,
        {"alert_id": str(alert.id), "status": str(AlertStatus.RESOLVED), "resolved_by": str(actor.id)},
    )
    return out
