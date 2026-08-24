"""Checkpoint scanning for volunteers (Section 4/M1).

    POST /checkpoints/scan       -> ALLOW | EARLY | EXPIRED | INVALID
    GET  /checkpoints/day-key    -> public key for offline verification
    GET  /checkpoints/bundle     -> narrow offline pass bundle for one gate
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.deps import Actor, require
from app.core.errors import AppError
from app.core.permissions import Permission
from app.core.security import now_utc
from app.models import Gate
from app.schemas.common import ErrorResponse
from app.schemas.passes import DayKeyOut, ScannerBundleOut, ScanRequest, ScanResponse
from app.services import audit_service, pass_service, qr_service
from app.services.audit_service import AuditAction

router = APIRouter(prefix="/checkpoints", tags=["checkpoints"], responses={403: {"model": ErrorResponse}})


@router.post("/scan", response_model=ScanResponse)
async def scan(
    payload: ScanRequest,
    actor: Actor = Depends(require(Permission.PASS_SCAN)),
    session: AsyncSession = Depends(get_session),
) -> ScanResponse:
    """Validate and consume a pass QR at a checkpoint.

    Every scan is audited, including the rejections — the record of who was
    turned away and why is exactly what makes queue integrity arguable after
    the fact rather than a shouting match at the gate.
    """
    gate_id = None
    if payload.gate_code:
        gate = await session.scalar(select(Gate).where(Gate.code == payload.gate_code))
        if gate is None:
            raise AppError("GATE_NOT_FOUND", details={"gate_code": payload.gate_code})
        gate_id = gate.id

    result = await pass_service.scan_pass(
        session,
        qr_payload=payload.qr_payload,
        scanned_by=actor.id,
        gate_id=gate_id,
        at=payload.scanned_at,
    )

    await audit_service.record(
        session,
        action=AuditAction.PASS_SCANNED,
        actor_id=actor.id,
        actor_role=actor.user.role,
        target_type="pass",
        target_id=result.pass_reference,
        meta={
            "outcome": str(result.outcome),
            "reason": result.reason,
            "gate_code": payload.gate_code,
            "offline_replay": payload.scanned_at is not None,
        },
        ip=actor.ip,
        user_agent=actor.user_agent,
    )
    await session.commit()

    return ScanResponse(
        outcome=result.outcome,
        reason=result.reason,
        message_mr=result.message_mr,
        pass_reference=result.pass_reference,
        group_size=result.group_size,
        slot_start=result.slot_start,
        slot_end=result.slot_end,
        scanned_at=result.scanned_at,
        minutes_early=result.minutes_early,
    )


@router.get("/day-key", response_model=DayKeyOut)
async def day_key(
    day: date = Query(default=None, alias="date"),
    _: Actor = Depends(require(Permission.PASS_SCAN)),
) -> DayKeyOut:
    """The day's Ed25519 public key.

    A scanner fetches this once while it has signal and can then verify pass
    authenticity for the whole day with no network at all.
    """
    target = day or now_utc().date()
    return DayKeyOut(date=target, public_key_b64=qr_service.day_public_key_b64(target))


@router.get("/bundle", response_model=ScannerBundleOut)
async def bundle(
    gate_code: str | None = Query(default=None),
    hours_ahead: int = Query(default=3, ge=1, le=12),
    actor: Actor = Depends(require(Permission.PASS_SCAN)),
    session: AsyncSession = Depends(get_session),
) -> ScannerBundleOut:
    """Offline verification bundle for one gate's scanner.

    Deliberately narrow — only the passes due at this gate in the next few
    hours.  A lost scanner exposes that window, not the whole Wari.
    """
    gate_id = None
    if gate_code:
        gate = await session.scalar(select(Gate).where(Gate.code == gate_code))
        if gate is None:
            raise AppError("GATE_NOT_FOUND", details={"gate_code": gate_code})
        gate_id = gate.id

    passes = await pass_service.scanner_bundle(session, gate_id=gate_id, hours_ahead=hours_ahead)
    today = now_utc().date()

    await audit_service.record(
        session,
        action=AuditAction.PASS_SCANNED,
        actor_id=actor.id,
        actor_role=actor.user.role,
        target_type="scanner_bundle",
        target_id=gate_code,
        meta={"passes": len(passes), "hours_ahead": hours_ahead},
        ip=actor.ip,
        user_agent=actor.user_agent,
    )
    await session.commit()

    return ScannerBundleOut(
        gate_code=gate_code,
        hours_ahead=hours_ahead,
        generated_at=now_utc(),
        day_key=DayKeyOut(date=today, public_key_b64=qr_service.day_public_key_b64(today)),
        passes=passes,
    )
