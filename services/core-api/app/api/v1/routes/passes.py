"""Slot availability and darshan pass endpoints (Section 9).

    GET  /slots                 POST /passes
    GET  /passes/{id}           GET  /passes/{id}/qr
    POST /passes/{id}/cancel
"""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_session
from app.core.deps import Actor, get_optional_actor, require
from app.core.errors import AppError
from app.core.permissions import Permission
from app.core.security import hash_phone, now_utc
from app.models import Gate, Pass
from app.schemas.common import Ack, ErrorResponse
from app.schemas.passes import (
    PassCreate,
    PassIssued,
    PassOut,
    QrOut,
    SlotGrid,
    SlotOut,
)
from app.services import audit_service, config_service, pass_service, qr_service, slot_service
from app.services.audit_service import AuditAction

router = APIRouter(tags=["passes"], responses={404: {"model": ErrorResponse}})


@router.get("/slots", response_model=SlotGrid)
async def get_slots(
    day: date = Query(default=None, alias="date", description="Defaults to today"),
    session: AsyncSession = Depends(get_session),
) -> SlotGrid:
    """The day's availability grid.

    Public: a pilgrim decides whether it is worth travelling before they sign
    in (Section 2 — no login required to browse).
    """
    target = day or now_utc().date()
    slots = await pass_service.list_slots(session, target)

    gate_codes: dict[uuid.UUID, str] = {}
    for slot in slots:
        if slot.gate_id and slot.gate_id not in gate_codes:
            gate = await session.get(Gate, slot.gate_id)
            if gate:
                gate_codes[slot.gate_id] = gate.code

    items: list[SlotOut] = []
    total = 0
    for slot in slots:
        available = slot_service.available_seats(
            slot_service.SlotState(
                capacity=slot.capacity,
                booked_count=slot.booked_count,
                walkin_reserve=slot.walkin_reserve,
                status=slot.status,
                walkin_used=slot.walkin_used,
            )
        )
        total += available
        start, _ = pass_service.slot_bounds(slot)
        items.append(
            SlotOut(
                id=slot.id,
                date=slot.date,
                start_time=slot.start_time,
                end_time=slot.end_time,
                capacity=slot.capacity,
                booked_count=slot.booked_count,
                walkin_reserve=slot.walkin_reserve,
                available=available,
                status=slot.status,
                gate_code=gate_codes.get(slot.gate_id) if slot.gate_id else None,
                is_bookable=available > 0 and start > now_utc(),
            )
        )

    await session.commit()
    return SlotGrid(
        date=target,
        slots=items,
        total_available=total,
        walkin_reserve_pct=await config_service.get_float(session, "walkin_reserve_pct"),
        generated_at=now_utc(),
    )


async def _to_out(session: AsyncSession, record: Pass) -> PassOut:
    view = await pass_service.describe_pass(session, record)
    start, end = pass_service.slot_bounds(view.slot)
    return PassOut(
        id=record.id,
        reference=record.reference,
        status=record.status,
        group_size=record.group_size,
        holder_name=record.holder_name,
        slot_date=view.slot.date,
        slot_start=start,
        slot_end=end,
        gate_code=view.gate_code,
        issued_at=record.issued_at,
        scanned_at=record.scanned_at,
        estimated_entry_at=view.eta,
        queue_ahead=view.queue_ahead,
        reslot_count=record.reslot_count,
        was_reslotted=view.is_reslotted,
        allow_early_reslot=record.allow_early_reslot,
        as_of=now_utc(),
    )


@router.post("/passes", response_model=PassIssued, status_code=201)
async def book_pass(
    payload: PassCreate,
    actor: Actor = Depends(require(Permission.PASS_BOOK)),
    session: AsyncSession = Depends(get_session),
) -> PassIssued:
    """Book a Smart Darshan Pass for up to six people.

    The caller must be signed in with a verified phone; the pass is bound to
    that phone's hash, never to the number itself.
    """
    phone_hash = hash_phone(payload.phone)
    if phone_hash != actor.user.phone_hash:
        raise AppError(
            "FORBIDDEN",
            message="You can only book with the phone number you signed in with.",
            message_mr="तुम्ही ज्या नंबरने साइन इन केले त्याच नंबरने नोंदणी करू शकता.",
        )

    booked_today = await pass_service.count_passes_today(session, phone_hash)
    if booked_today >= settings.rate_limit_pass_booking_per_day:
        raise AppError(
            "BOOKING_LIMIT_REACHED",
            details={"limit": settings.rate_limit_pass_booking_per_day, "booked": booked_today},
        )

    if payload.members and len(payload.members) > payload.group_size:
        raise AppError(
            "BAD_REQUEST",
            message="More members listed than the group size.",
            message_mr="गटाच्या संख्येपेक्षा जास्त नावे दिली आहेत.",
            details={"group_size": payload.group_size, "members": len(payload.members)},
        )

    record = await pass_service.book_pass(
        session,
        slot_id=payload.slot_id,
        phone_hash=phone_hash,
        holder_name=payload.holder_name,
        group_size=payload.group_size,
        language=payload.language,
        members=[(m.name, m.age_band) for m in payload.members],
        allow_early_reslot=payload.allow_early_reslot,
    )

    await audit_service.record(
        session,
        action=AuditAction.PASS_ISSUED,
        actor_id=actor.id,
        actor_role=actor.user.role,
        target_type="pass",
        target_id=record.id,
        meta={"reference": record.reference, "slot_id": str(payload.slot_id), "group_size": record.group_size},
        ip=actor.ip,
        user_agent=actor.user_agent,
    )

    qr_payload, valid_for = await pass_service.qr_material(session, record)
    base = await _to_out(session, record)
    await session.commit()

    return PassIssued(
        **base.model_dump(),
        # Returned once, at issue.  The device stores it and computes the
        # rolling code locally, which is what makes the QR work offline.
        qr_secret=record.qr_secret,
        qr_payload=qr_payload,
        qr_valid_for_seconds=valid_for,
    )


def _assert_owner(actor: Actor, record: Pass) -> None:
    if record.holder_phone_hash != actor.user.phone_hash and not actor.can(Permission.PASS_ADMIN):
        raise AppError("PASS_NOT_FOUND", details={"pass_id": str(record.id)})


@router.get("/passes/{pass_id}", response_model=PassOut)
async def get_pass(
    pass_id: uuid.UUID,
    actor: Actor | None = Depends(get_optional_actor),
    session: AsyncSession = Depends(get_session),
) -> PassOut:
    """Pass status with a live wait estimate."""
    record = await pass_service.load_pass(session, pass_id)
    if actor is None:
        raise AppError("UNAUTHENTICATED")
    _assert_owner(actor, record)
    out = await _to_out(session, record)
    await session.commit()
    return out


@router.get("/passes/{pass_id}/qr", response_model=QrOut)
async def get_pass_qr(
    pass_id: uuid.UUID,
    actor: Actor = Depends(require(Permission.PASS_VIEW_OWN)),
    session: AsyncSession = Depends(get_session),
) -> QrOut:
    """Current QR payload.

    Only needed when the device cannot compute the rolling code itself — the
    offline path is to keep `qr_secret` from booking and generate locally.
    """
    record = await pass_service.load_pass(session, pass_id)
    _assert_owner(actor, record)
    payload, valid_for = await pass_service.qr_material(session, record)
    await session.commit()
    return QrOut(
        qr_payload=payload,
        valid_for_seconds=valid_for,
        rotates_every_seconds=qr_service.ROLLING_STEP_SECONDS,
        as_of=now_utc(),
    )


@router.post("/passes/{pass_id}/cancel", response_model=Ack)
async def cancel_pass(
    pass_id: uuid.UUID,
    actor: Actor = Depends(require(Permission.PASS_CANCEL_OWN)),
    session: AsyncSession = Depends(get_session),
) -> Ack:
    """Cancel a pass and return its seats to the pool immediately."""
    record = await pass_service.load_pass(session, pass_id)
    _assert_owner(actor, record)
    await pass_service.cancel_pass(session, record)

    await audit_service.record(
        session,
        action=AuditAction.PASS_CANCELLED,
        actor_id=actor.id,
        actor_role=actor.user.role,
        target_type="pass",
        target_id=record.id,
        meta={"reference": record.reference},
        ip=actor.ip,
        user_agent=actor.user_agent,
    )
    await session.commit()
    return Ack(message="Pass cancelled.", message_mr="पास रद्द केला.")
