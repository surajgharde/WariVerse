"""Audit-trail reading and runtime configuration."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.deps import Actor, require
from app.core.errors import AppError
from app.core.permissions import Permission
from app.models import AuditLog, SystemConfig
from app.schemas.common import ErrorResponse, Page
from app.schemas.user import AuditEntryOut, ConfigEntryOut, ConfigUpdate
from app.services import audit_service
from app.services.audit_service import AuditAction

router = APIRouter(tags=["admin"], responses={403: {"model": ErrorResponse}})


@router.get("/audit", response_model=Page[AuditEntryOut])
async def list_audit(
    action: str | None = None,
    target_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    actor: Actor = Depends(require(Permission.AUDIT_VIEW)),
    session: AsyncSession = Depends(get_session),
) -> Page[AuditEntryOut]:
    """Read the append-only trail.  Reading it is itself audited."""
    stmt = select(AuditLog)
    if action:
        stmt = stmt.where(AuditLog.action.startswith(action))
    if target_id:
        stmt = stmt.where(AuditLog.target_id == target_id)
    if since:
        stmt = stmt.where(AuditLog.created_at >= since)
    if until:
        stmt = stmt.where(AuditLog.created_at <= until)

    total = await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = await session.execute(stmt.order_by(AuditLog.created_at.desc()).limit(limit).offset(offset))
    items = [
        AuditEntryOut(
            id=str(row.id),
            actor_id=str(row.actor_id) if row.actor_id else None,
            actor_role=row.actor_role,
            action=row.action,
            target_type=row.target_type,
            target_id=row.target_id,
            meta=row.meta,
            ip=str(row.ip) if row.ip else None,
            trace_id=row.trace_id,
            created_at=row.created_at,
        )
        for row in rows.scalars()
    ]

    await audit_service.record(
        session,
        action=AuditAction.AUDIT_VIEWED,
        actor_id=actor.id,
        actor_role=actor.user.role,
        target_type="audit_log",
        meta={"filters": {"action": action, "target_id": target_id}, "returned": len(items)},
        ip=actor.ip,
        user_agent=actor.user_agent,
    )
    await session.commit()
    return Page[AuditEntryOut](items=items, total=total, limit=limit, offset=offset)


@router.get("/config", response_model=list[ConfigEntryOut])
async def list_config(
    _: Actor = Depends(require(Permission.CONFIG_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> list[ConfigEntryOut]:
    rows = await session.execute(select(SystemConfig).order_by(SystemConfig.key))
    return [
        ConfigEntryOut(key=r.key, value=r.value.get("v"), description=r.description, updated_at=r.updated_at)
        for r in rows.scalars()
    ]


@router.put("/config/{key}", response_model=ConfigEntryOut)
async def update_config(
    key: str,
    payload: ConfigUpdate,
    actor: Actor = Depends(require(Permission.CONFIG_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> ConfigEntryOut:
    """Change a tunable.  The reason is mandatory — six months later, someone
    will need to know why the walk-in reserve was moved."""
    entry = await session.get(SystemConfig, key)
    if entry is None:
        raise AppError("NOT_FOUND", details={"key": key})

    previous = entry.value.get("v")
    entry.value = {"v": payload.value}
    entry.updated_by = actor.id

    await audit_service.record(
        session,
        action=AuditAction.CONFIG_CHANGED,
        actor_id=actor.id,
        actor_role=actor.user.role,
        target_type="system_config",
        target_id=key,
        meta={"from": previous, "to": payload.value, "reason": payload.reason},
        ip=actor.ip,
        user_agent=actor.user_agent,
    )
    await session.commit()
    return ConfigEntryOut(
        key=entry.key, value=payload.value, description=entry.description, updated_at=entry.updated_at
    )
