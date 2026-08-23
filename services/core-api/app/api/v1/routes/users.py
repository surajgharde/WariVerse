"""User and role administration (Section 2, System Admin surface)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.deps import Actor, require
from app.core.errors import AppError
from app.core.permissions import PASSWORD_LOGIN_ROLES, Permission, Role
from app.core.security import hash_password, hash_phone, mask_phone, normalise_phone
from app.models import User
from app.schemas.common import Ack, ErrorResponse, Page
from app.schemas.user import UserCreate, UserOut, UserUpdate
from app.services import audit_service
from app.services.audit_service import AuditAction

router = APIRouter(
    prefix="/users",
    tags=["admin"],
    responses={403: {"model": ErrorResponse}},
)


def _to_out(user: User) -> UserOut:
    return UserOut(
        id=str(user.id),
        name=user.name,
        role=Role(user.role),
        language=user.language,
        is_active=user.is_active,
        phone_masked=mask_phone(user.phone) if user.phone else None,
        mfa_enrolled=user.mfa_secret is not None,
        last_login_at=user.last_login_at,
        created_at=user.created_at,
    )


@router.get("", response_model=Page[UserOut])
async def list_users(
    role: Role | None = None,
    is_active: bool | None = None,
    search: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _: Actor = Depends(require(Permission.USER_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> Page[UserOut]:
    stmt = select(User)
    if role:
        stmt = stmt.where(User.role == role)
    if is_active is not None:
        stmt = stmt.where(User.is_active == is_active)
    if search:
        stmt = stmt.where(User.name.ilike(f"%{search}%"))

    total = await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = await session.execute(stmt.order_by(User.created_at.desc()).limit(limit).offset(offset))
    return Page[UserOut](
        items=[_to_out(u) for u in rows.scalars()],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("", response_model=UserOut, status_code=201)
async def create_user(
    payload: UserCreate,
    actor: Actor = Depends(require(Permission.USER_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> UserOut:
    if payload.requires_password() and not payload.password:
        raise AppError(
            "BAD_REQUEST",
            message="Staff accounts need a password.",
            message_mr="कर्मचारी खात्यासाठी पासवर्ड आवश्यक आहे.",
            details={"field": "password"},
        )

    phone_hash = hash_phone(payload.phone)
    existing = await session.scalar(select(User).where(User.phone_hash == phone_hash))
    if existing:
        raise AppError(
            "CONFLICT",
            message="An account already exists for this phone number.",
            message_mr="या फोन नंबरसाठी खाते आधीच आहे.",
        )

    user = User(
        # Staff keep a raw phone for roster and callout; pilgrims never do.
        phone=normalise_phone(payload.phone) if payload.role in PASSWORD_LOGIN_ROLES else None,
        phone_hash=phone_hash,
        name=payload.name,
        role=payload.role,
        language=payload.language,
        password_hash=hash_password(payload.password) if payload.password else None,
        is_active=True,
    )
    session.add(user)
    await session.flush()

    await audit_service.record(
        session,
        action=AuditAction.USER_CREATED,
        actor_id=actor.id,
        actor_role=actor.user.role,
        target_type="user",
        target_id=user.id,
        meta={"role": str(payload.role), "name": payload.name},
        ip=actor.ip,
        user_agent=actor.user_agent,
    )
    await session.commit()
    return _to_out(user)


@router.patch("/{user_id}", response_model=UserOut)
async def update_user(
    user_id: uuid.UUID,
    payload: UserUpdate,
    actor: Actor = Depends(require(Permission.USER_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> UserOut:
    user = await session.get(User, user_id)
    if user is None:
        raise AppError("NOT_FOUND", details={"user_id": str(user_id)})

    changes: dict[str, object] = {}
    if payload.name is not None and payload.name != user.name:
        changes["name"] = payload.name
        user.name = payload.name
    if payload.language is not None:
        user.language = payload.language
    if payload.is_active is not None and payload.is_active != user.is_active:
        changes["is_active"] = payload.is_active
        user.is_active = payload.is_active

    role_changed = payload.role is not None and str(payload.role) != user.role
    if role_changed:
        if user.id == actor.id:
            # Removing your own privilege by accident during an incident is a
            # failure mode worth blocking outright.
            raise AppError(
                "FORBIDDEN",
                message="You cannot change your own role.",
                message_mr="तुम्ही स्वतःची भूमिका बदलू शकत नाही.",
            )
        changes["role"] = {"from": user.role, "to": str(payload.role)}
        user.role = str(payload.role)

    if changes:
        await audit_service.record(
            session,
            action=AuditAction.USER_ROLE_CHANGED if role_changed else AuditAction.USER_UPDATED,
            actor_id=actor.id,
            actor_role=actor.user.role,
            target_type="user",
            target_id=user.id,
            meta=changes,
            ip=actor.ip,
            user_agent=actor.user_agent,
        )
    await session.commit()
    return _to_out(user)


@router.delete("/{user_id}", response_model=Ack)
async def deactivate_user(
    user_id: uuid.UUID,
    actor: Actor = Depends(require(Permission.USER_MANAGE)),
    session: AsyncSession = Depends(get_session),
) -> Ack:
    """Deactivate, never delete — the audit trail must keep pointing at a real
    row."""
    user = await session.get(User, user_id)
    if user is None:
        raise AppError("NOT_FOUND", details={"user_id": str(user_id)})
    if user.id == actor.id:
        raise AppError(
            "FORBIDDEN",
            message="You cannot deactivate your own account.",
            message_mr="तुम्ही स्वतःचे खाते बंद करू शकत नाही.",
        )

    user.is_active = False
    await audit_service.record(
        session,
        action=AuditAction.USER_DEACTIVATED,
        actor_id=actor.id,
        actor_role=actor.user.role,
        target_type="user",
        target_id=user.id,
        ip=actor.ip,
        user_agent=actor.user_agent,
    )
    await session.commit()
    return Ack(message="Account deactivated.", message_mr="खाते बंद केले.")
