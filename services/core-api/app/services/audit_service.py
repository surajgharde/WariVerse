"""Append-only audit trail (Section 2).

Every privileged action writes here with actor, action, target, timestamp and
IP.  Writes are best-effort-but-loud: if the audit write fails, the caller's
action still fails, because an unlogged privileged action is worse than a
refused one.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger, get_trace_id
from app.core.security import now_utc
from app.models import AuditLog

logger = get_logger(__name__)


class AuditAction:
    """Canonical action names.  Strings are compared in reports — keep stable."""

    LOGIN_SUCCESS = "auth.login.success"
    LOGIN_FAILURE = "auth.login.failure"
    LOGOUT = "auth.logout"
    OTP_REQUESTED = "auth.otp.requested"
    OTP_VERIFIED = "auth.otp.verified"
    TOKEN_REFRESHED = "auth.token.refreshed"
    TOKEN_REUSE_DETECTED = "auth.token.reuse_detected"
    MFA_ENROLLED = "auth.mfa.enrolled"
    MFA_VERIFIED = "auth.mfa.verified"
    MFA_FAILED = "auth.mfa.failed"

    USER_CREATED = "user.created"
    USER_UPDATED = "user.updated"
    USER_ROLE_CHANGED = "user.role_changed"
    USER_DEACTIVATED = "user.deactivated"

    PASS_ISSUED = "pass.issued"
    PASS_CANCELLED = "pass.cancelled"
    PASS_SCANNED = "pass.scanned"
    PASS_RESLOTTED = "pass.reslotted"
    SLOT_CAPACITY_CHANGED = "slot.capacity_changed"

    ALERT_ACKNOWLEDGED = "alert.acknowledged"
    ALERT_ESCALATED = "alert.escalated"

    INCIDENT_CREATED = "incident.created"
    INCIDENT_DISPATCHED = "incident.dispatched"
    INCIDENT_STATUS_CHANGED = "incident.status_changed"
    INCIDENT_CLOSED = "incident.closed"

    BREACH_REVIEWED = "breach.reviewed"
    BREACH_CLIP_VIEWED = "breach.clip_viewed"
    BREACH_DELETED = "breach.deleted"

    CAMERA_CALIBRATED = "camera.calibrated"
    ZONE_UPDATED = "zone.updated"
    CONFIG_CHANGED = "config.changed"
    AUDIT_VIEWED = "audit.viewed"
    DATA_EXPORTED = "data.exported"
    PURGE_EXECUTED = "purge.executed"


async def record(
    session: AsyncSession,
    *,
    action: str,
    actor_id: uuid.UUID | None = None,
    actor_role: str | None = None,
    target_type: str | None = None,
    target_id: str | uuid.UUID | None = None,
    meta: dict[str, Any] | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    flush: bool = True,
) -> AuditLog:
    """Append one entry.  Does not commit — it joins the caller's transaction,
    so an action and its audit record land together or not at all."""
    entry = AuditLog(
        actor_id=actor_id,
        actor_role=actor_role,
        action=action,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        meta=_scrub(meta or {}),
        ip=ip,
        user_agent=(user_agent or "")[:500] or None,
        trace_id=get_trace_id(),
        created_at=now_utc(),
    )
    session.add(entry)
    if flush:
        await session.flush()
    logger.info(
        "audit",
        extra={"action": action, "actor_id": str(actor_id) if actor_id else None, "target_id": entry.target_id},
    )
    return entry


_SENSITIVE_KEYS = {"password", "otp", "code", "token", "secret", "qr_secret", "mfa_secret", "phone"}


def _scrub(meta: dict[str, Any]) -> dict[str, Any]:
    """An audit log that leaks credentials is a liability, not a control."""
    cleaned: dict[str, Any] = {}
    for key, value in meta.items():
        if any(marker in key.lower() for marker in _SENSITIVE_KEYS):
            cleaned[key] = "[redacted]"
        elif isinstance(value, dict):
            cleaned[key] = _scrub(value)
        else:
            cleaned[key] = value
    return cleaned
