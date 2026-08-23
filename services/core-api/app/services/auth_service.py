"""Authentication flows: OTP sign-in, password login, MFA, refresh, logout."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.core.logging import get_logger
from app.core.permissions import PASSWORD_LOGIN_ROLES, Role, permissions_for, requires_mfa
from app.core.security import (
    create_access_token,
    create_mfa_pending_token,
    create_refresh_token,
    decode_token,
    hash_password,
    hash_phone,
    mask_phone,
    normalise_phone,
    now_utc,
    password_needs_rehash,
    verify_mfa_code,
    verify_password,
)
from app.models import User
from app.services import audit_service, token_store
from app.services.audit_service import AuditAction

logger = get_logger(__name__)

MAX_FAILED_LOGINS = 5
LOCKOUT_MINUTES = 15


@dataclass(frozen=True, slots=True)
class TokenPair:
    access_token: str
    refresh_token: str
    expires_at: datetime
    user: User

    @property
    def expires_in(self) -> int:
        return max(0, int((self.expires_at - now_utc()).total_seconds()))


@dataclass(frozen=True, slots=True)
class MfaChallenge:
    mfa_token: str
    user: User


async def get_user_by_phone(session: AsyncSession, phone: str) -> User | None:
    result = await session.execute(select(User).where(User.phone_hash == hash_phone(phone)))
    return result.scalar_one_or_none()


async def get_user_by_id(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    return await session.get(User, user_id)


async def get_or_create_pilgrim(session: AsyncSession, phone: str, name: str | None, language: str) -> User:
    """Pilgrims are created on first successful OTP.  No signup form, no
    password — a 70-year-old with a feature-phone-grade Android should not have
    to invent one."""
    user = await get_user_by_phone(session, phone)
    if user:
        if language and user.language != language:
            user.language = language
        return user

    user = User(
        phone=None,  # pilgrims: hash only (Section 12)
        phone_hash=hash_phone(phone),
        name=name or "यात्रेकरू",  # "pilgrim"
        role=Role.PILGRIM,
        language=language or "mr",
        is_active=True,
    )
    session.add(user)
    await session.flush()
    await audit_service.record(
        session,
        action=AuditAction.USER_CREATED,
        actor_id=user.id,
        actor_role=user.role,
        target_type="user",
        target_id=user.id,
        meta={"role": Role.PILGRIM, "via": "otp"},
    )
    logger.info("pilgrim_created", extra={"phone": mask_phone(phone), "user_id": str(user.id)})
    return user


def _assert_usable(user: User) -> None:
    if not user.is_active:
        raise AppError("ACCOUNT_DISABLED")
    if user.locked_until and user.locked_until > now_utc():
        raise AppError(
            "INVALID_CREDENTIALS",
            message="This account is temporarily locked after repeated failed attempts.",
            message_mr="वारंवार चुकीच्या प्रयत्नांमुळे हे खाते तात्पुरते बंद आहे.",
            details={"locked_until": user.locked_until.isoformat()},
        )


async def issue_tokens(session: AsyncSession, user: User, *, mfa_verified: bool = False) -> TokenPair:
    access, expires = create_access_token(
        subject=str(user.id),
        role=user.role,
        mfa_verified=mfa_verified,
        extra={"lang": user.language},
    )
    refresh, jti, family, _ = create_refresh_token(subject=str(user.id), role=user.role)
    await token_store.open_family(family, jti, str(user.id))

    user.last_login_at = now_utc()
    user.failed_login_count = 0
    user.locked_until = None
    return TokenPair(access_token=access, refresh_token=refresh, expires_at=expires, user=user)


async def login_with_otp(
    session: AsyncSession,
    *,
    phone: str,
    name: str | None,
    language: str,
    ip: str | None,
    user_agent: str | None,
) -> TokenPair:
    """Called *after* `otp_service.verify_otp` has succeeded."""
    user = await get_or_create_pilgrim(session, phone, name, language)
    _assert_usable(user)

    # Staff never sign in with OTP alone — their accounts carry real privilege.
    if Role(user.role) in PASSWORD_LOGIN_ROLES:
        raise AppError(
            "FORBIDDEN",
            message="Staff accounts must sign in with a password.",
            message_mr="कर्मचारी खात्यांसाठी पासवर्डने साइन इन करावे लागते.",
        )

    pair = await issue_tokens(session, user)
    await audit_service.record(
        session,
        action=AuditAction.OTP_VERIFIED,
        actor_id=user.id,
        actor_role=user.role,
        target_type="user",
        target_id=user.id,
        ip=ip,
        user_agent=user_agent,
    )
    return pair


async def login_with_password(
    session: AsyncSession,
    *,
    phone: str,
    password: str,
    ip: str | None,
    user_agent: str | None,
) -> TokenPair | MfaChallenge:
    user = await get_user_by_phone(session, phone)

    if user is None or Role(user.role) not in PASSWORD_LOGIN_ROLES:
        # Same error and roughly the same work either way — do not leak which
        # phone numbers belong to staff accounts.
        hash_password(password)
        await audit_service.record(
            session,
            action=AuditAction.LOGIN_FAILURE,
            target_type="phone",
            target_id=mask_phone(phone),
            meta={"reason": "unknown_account"},
            ip=ip,
            user_agent=user_agent,
        )
        raise AppError("INVALID_CREDENTIALS")

    _assert_usable(user)

    if not verify_password(password, user.password_hash):
        user.failed_login_count += 1
        if user.failed_login_count >= MAX_FAILED_LOGINS:
            user.locked_until = now_utc() + timedelta(minutes=LOCKOUT_MINUTES)
        await audit_service.record(
            session,
            action=AuditAction.LOGIN_FAILURE,
            actor_id=user.id,
            actor_role=user.role,
            target_type="user",
            target_id=user.id,
            meta={"failed_count": user.failed_login_count, "locked": user.locked_until is not None},
            ip=ip,
            user_agent=user_agent,
        )
        raise AppError("INVALID_CREDENTIALS")

    if user.password_hash and password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)

    if requires_mfa(user.role):
        if not user.mfa_secret:
            raise AppError(
                "MFA_REQUIRED",
                message="This account must complete two-factor enrolment before signing in.",
                message_mr="साइन इन करण्यापूर्वी या खात्याची दुहेरी पडताळणी नोंदणी करावी लागेल.",
                details={"enrolment_required": True},
            )
        return MfaChallenge(
            mfa_token=create_mfa_pending_token(subject=str(user.id), role=user.role),
            user=user,
        )

    pair = await issue_tokens(session, user)
    await audit_service.record(
        session,
        action=AuditAction.LOGIN_SUCCESS,
        actor_id=user.id,
        actor_role=user.role,
        target_type="user",
        target_id=user.id,
        ip=ip,
        user_agent=user_agent,
    )
    return pair


async def complete_mfa(
    session: AsyncSession,
    *,
    mfa_token: str,
    code: str,
    ip: str | None,
    user_agent: str | None,
) -> TokenPair:
    claims = decode_token(mfa_token, expected_type="mfa_pending")
    user = await get_user_by_id(session, uuid.UUID(claims.subject))
    if user is None:
        raise AppError("TOKEN_INVALID")
    _assert_usable(user)

    if not user.mfa_secret or not verify_mfa_code(user.mfa_secret, code):
        await audit_service.record(
            session,
            action=AuditAction.MFA_FAILED,
            actor_id=user.id,
            actor_role=user.role,
            target_type="user",
            target_id=user.id,
            ip=ip,
            user_agent=user_agent,
        )
        raise AppError("MFA_INVALID")

    pair = await issue_tokens(session, user, mfa_verified=True)
    await audit_service.record(
        session,
        action=AuditAction.MFA_VERIFIED,
        actor_id=user.id,
        actor_role=user.role,
        target_type="user",
        target_id=user.id,
        ip=ip,
        user_agent=user_agent,
    )
    return pair


async def refresh_tokens(
    session: AsyncSession,
    *,
    refresh_token: str,
    ip: str | None,
    user_agent: str | None,
) -> TokenPair:
    claims = decode_token(refresh_token, expected_type="refresh")
    if not claims.family:
        raise AppError("TOKEN_INVALID", details={"reason": "no family"})

    user = await get_user_by_id(session, uuid.UUID(claims.subject))
    if user is None:
        raise AppError("TOKEN_INVALID")
    _assert_usable(user)

    new_refresh, new_jti, _, _ = create_refresh_token(
        subject=str(user.id), role=user.role, family=claims.family
    )
    try:
        await token_store.rotate_refresh(claims.family, claims.jti, new_jti)
    except AppError as exc:
        if exc.code == "TOKEN_REUSED":
            await audit_service.record(
                session,
                action=AuditAction.TOKEN_REUSE_DETECTED,
                actor_id=user.id,
                actor_role=user.role,
                target_type="user",
                target_id=user.id,
                meta={"family": claims.family},
                ip=ip,
                user_agent=user_agent,
            )
            await session.commit()
        raise

    access, expires = create_access_token(
        subject=str(user.id),
        role=user.role,
        # MFA state does not survive a refresh for MFA roles; a long-lived
        # session must re-prove the second factor.
        mfa_verified=not requires_mfa(user.role),
        extra={"lang": user.language},
    )
    await audit_service.record(
        session,
        action=AuditAction.TOKEN_REFRESHED,
        actor_id=user.id,
        actor_role=user.role,
        target_type="user",
        target_id=user.id,
        ip=ip,
        user_agent=user_agent,
    )
    return TokenPair(access_token=access, refresh_token=new_refresh, expires_at=expires, user=user)


async def logout(
    session: AsyncSession,
    *,
    user: User,
    access_jti: str,
    refresh_token: str | None,
    ip: str | None,
) -> None:
    await token_store.deny_access_token(access_jti)
    if refresh_token:
        try:
            claims = decode_token(refresh_token, expected_type="refresh")
            if claims.family:
                await token_store.revoke_family(claims.family)
        except AppError:
            # An unparseable token on logout is not worth failing the request
            # over — the access token is already denied.
            logger.info("logout_with_invalid_refresh", extra={"user_id": str(user.id)})

    await audit_service.record(
        session,
        action=AuditAction.LOGOUT,
        actor_id=user.id,
        actor_role=user.role,
        target_type="user",
        target_id=user.id,
        ip=ip,
    )


def profile_payload(user: User) -> dict[str, object]:
    return {
        "id": str(user.id),
        "name": user.name,
        "role": user.role,
        "language": user.language,
        "permissions": sorted(str(p) for p in permissions_for(user.role)),
        "mfa_enrolled": user.mfa_secret is not None,
        "phone_masked": mask_phone(user.phone) if user.phone else None,
    }


def normalise(phone: str) -> str:
    return normalise_phone(phone)
