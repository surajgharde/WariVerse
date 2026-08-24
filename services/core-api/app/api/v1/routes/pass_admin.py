"""Capacity and reslotting controls for the temple administrator."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.deps import Actor, require
from app.core.errors import AppError
from app.core.permissions import Permission
from app.core.security import now_utc
from app.models import Slot, SlotStatus
from app.schemas.common import ApiModel, ErrorResponse
from app.schemas.passes import ReslotRunOut
from app.services import audit_service
from app.services.audit_service import AuditAction
from app.workers import jobs

router = APIRouter(prefix="/admin", tags=["admin"], responses={403: {"model": ErrorResponse}})


class SlotAdjust(ApiModel):
    capacity: int | None = Field(default=None, ge=0)
    status: str | None = Field(default=None, pattern="^(open|full|closed|completed)$")
    walkin_reserve: int | None = Field(default=None, ge=0)
    reason: str = Field(min_length=4, max_length=500)


@router.post("/reslot/run", response_model=ReslotRunOut)
async def run_reslot_now(
    force: bool = Query(
        default=False,
        description="Ignore the deviation threshold and reslot on any shortfall. Drill and demo use.",
    ),
    actor: Actor = Depends(require(Permission.PASS_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> ReslotRunOut:
    """Run the reslotting job immediately instead of waiting for the timer.

    Used during a tabletop drill, and when an operator already knows the gate
    has stalled and does not want to wait five minutes for the system to notice.
    """
    run = await jobs.run_reslot(force=force)

    await audit_service.record(
        session,
        action=AuditAction.PASS_RESLOTTED,
        actor_id=actor.id,
        actor_role=actor.user.role,
        target_type="reslot_run",
        meta={
            "forced": force,
            "planned": run.window.planned,
            "actual": run.window.actual,
            "delay_minutes": run.decision.delay_minutes,
            "passes_moved": run.passes_moved,
        },
        ip=actor.ip,
        user_agent=actor.user_agent,
    )
    await session.commit()

    return ReslotRunOut(
        ran_at=run.ran_at,
        planned=run.window.planned,
        actual=run.window.actual,
        deviation=round(run.decision.deviation, 4),
        should_reslot=run.decision.should_reslot,
        delay_minutes=run.decision.delay_minutes,
        passes_moved=run.passes_moved,
        reason=run.decision.reason,
    )


@router.post("/passes/expire", response_model=dict)
async def expire_now(
    actor: Actor = Depends(require(Permission.PASS_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Release capacity held by no-shows, without waiting for the timer."""
    expired = await jobs.run_expire_no_shows()
    await audit_service.record(
        session,
        action=AuditAction.PASS_RESLOTTED,
        actor_id=actor.id,
        actor_role=actor.user.role,
        target_type="expiry_run",
        meta={"expired": expired},
        ip=actor.ip,
    )
    await session.commit()
    return {"expired": expired, "ran_at": now_utc().isoformat()}


@router.patch("/slots/{slot_id}", response_model=dict)
async def adjust_slot(
    slot_id: uuid.UUID,
    payload: SlotAdjust,
    actor: Actor = Depends(require(Permission.PASS_ADMIN)),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Change a slot's capacity, reserve or status.

    Capacity can never drop below what is already booked — the system does not
    invalidate a pass someone is already travelling for.
    """
    slot = await session.get(Slot, slot_id)
    if slot is None:
        raise AppError("SLOT_NOT_FOUND", details={"slot_id": str(slot_id)})

    changes: dict[str, object] = {}

    if payload.capacity is not None and payload.capacity != slot.capacity:
        floor = slot.booked_count + (payload.walkin_reserve or slot.walkin_reserve)
        if payload.capacity < floor:
            raise AppError(
                "CONFLICT",
                message="Capacity cannot go below what is already booked plus the walk-in reserve.",
                message_mr="आधीच नोंदलेल्या जागांपेक्षा क्षमता कमी करता येणार नाही.",
                details={"requested": payload.capacity, "minimum": floor},
            )
        changes["capacity"] = {"from": slot.capacity, "to": payload.capacity}
        slot.capacity = payload.capacity

    if payload.walkin_reserve is not None and payload.walkin_reserve != slot.walkin_reserve:
        if slot.booked_count + payload.walkin_reserve > slot.capacity:
            raise AppError(
                "CONFLICT",
                message="That reserve would oversubscribe the slot.",
                message_mr="त्या राखीव जागांमुळे वेळ भरून जाईल.",
                details={"booked": slot.booked_count, "capacity": slot.capacity},
            )
        changes["walkin_reserve"] = {"from": slot.walkin_reserve, "to": payload.walkin_reserve}
        slot.walkin_reserve = payload.walkin_reserve

    if payload.status is not None and payload.status != slot.status:
        changes["status"] = {"from": slot.status, "to": payload.status}
        slot.status = SlotStatus(payload.status)

    if changes:
        await audit_service.record(
            session,
            action=AuditAction.SLOT_CAPACITY_CHANGED,
            actor_id=actor.id,
            actor_role=actor.user.role,
            target_type="slot",
            target_id=slot.id,
            meta={"changes": changes, "reason": payload.reason},
            ip=actor.ip,
            user_agent=actor.user_agent,
        )
    await session.commit()
    return {"slot_id": str(slot.id), "changes": changes}
