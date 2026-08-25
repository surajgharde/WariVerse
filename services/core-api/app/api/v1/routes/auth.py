"""Auth endpoints (Section 9).

    POST /auth/otp/request   POST /auth/otp/verify
    POST /auth/login         POST /auth/mfa/verify
    POST /auth/refresh       POST /auth/logout
    GET  /auth/me            POST /auth/dev-login   (development only)
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_session
from app.core.deps import Actor, client_ip, get_current_actor
from app.core.errors import AppError
from app.core.permissions import requires_mfa
from app.core.security import generate_mfa_secret, mfa_provisioning_uri, normalise_phone, now_utc, verify_mfa_code
from app.schemas.auth import (
    DevLogin,
    LogoutRequest,
    MfaChallengeResponse,
    MfaEnrolResponse,
    MfaVerify,
    OtpRequest,
    OtpRequestResponse,
    OtpVerify,
    PasswordLogin,
    RefreshRequest,
    TokenResponse,
    UserProfile,
)
from app.schemas.common import Ack, ErrorResponse
from app.services import audit_service, auth_service, otp_service
from app.services.audit_service import AuditAction
from app.services.auth_service import MfaChallenge, TokenPair

router = APIRouter(
    prefix="/auth",
    tags=["auth"],
    responses={401: {"model": ErrorResponse}, 429: {"model": ErrorResponse}},
)


def _token_response(pair: TokenPair) -> TokenResponse:
    return TokenResponse(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        expires_in=pair.expires_in,
        user=UserProfile(**auth_service.profile_payload(pair.user)),  # type: ignore[arg-type]
    )


@router.post("/otp/request", response_model=OtpRequestResponse)
async def request_otp(
    payload: OtpRequest, request: Request, session: AsyncSession = Depends(get_session)
) -> OtpRequestResponse:
    """Send a one-time code.  Limited to 3 per hour per phone."""
    issue = await otp_service.request_otp(payload.phone, payload.purpose)
    await audit_service.record(
        session,
        action=AuditAction.OTP_REQUESTED,
        target_type="phone_hash",
        target_id=issue.phone_hash,
        meta={"purpose": payload.purpose},
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    await session.commit()
    return OtpRequestResponse(sent=True, expires_in=issue.expires_in, debug_code=issue.debug_code)


@router.post("/otp/verify", response_model=TokenResponse)
async def verify_otp(
    payload: OtpVerify, request: Request, session: AsyncSession = Depends(get_session)
) -> TokenResponse:
    """Verify the code and sign the pilgrim in, creating the account if new."""
    await otp_service.verify_otp(payload.phone, payload.code)
    pair = await auth_service.login_with_otp(
        session,
        phone=payload.phone,
        name=payload.name,
        language=payload.language,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    await session.commit()
    return _token_response(pair)


@router.post(
    "/login",
    response_model=None,
    responses={200: {"model": TokenResponse}, 202: {"model": MfaChallengeResponse}},
)
async def login(
    payload: PasswordLogin,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> TokenResponse | MfaChallengeResponse:
    """Staff password sign-in.  Administrator and System Admin get an MFA
    challenge instead of tokens."""
    result = await auth_service.login_with_password(
        session,
        phone=payload.phone,
        password=payload.password,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    await session.commit()
    if isinstance(result, MfaChallenge):
        return MfaChallengeResponse(mfa_token=result.mfa_token)
    return _token_response(result)


@router.post("/mfa/verify", response_model=TokenResponse)
async def verify_mfa(
    payload: MfaVerify, request: Request, session: AsyncSession = Depends(get_session)
) -> TokenResponse:
    pair = await auth_service.complete_mfa(
        session,
        mfa_token=payload.mfa_token,
        code=payload.code,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    await session.commit()
    return _token_response(pair)


@router.post("/mfa/enrol", response_model=MfaEnrolResponse)
async def enrol_mfa(
    actor: Actor = Depends(get_current_actor), session: AsyncSession = Depends(get_session)
) -> MfaEnrolResponse:
    """Generate a TOTP secret.  It is not active until `mfa/enrol/confirm`."""
    if not requires_mfa(actor.user.role):
        raise AppError(
            "FORBIDDEN",
            message="Two-factor enrolment applies to administrator accounts.",
            message_mr="दुहेरी पडताळणी नोंदणी प्रशासक खात्यांसाठी आहे.",
        )
    secret = generate_mfa_secret()
    actor.user.mfa_secret = secret
    actor.user.mfa_enrolled_at = None
    await session.commit()
    account = actor.user.phone or actor.user.name
    return MfaEnrolResponse(secret=secret, provisioning_uri=mfa_provisioning_uri(secret, account))


@router.post("/mfa/enrol/confirm", response_model=Ack)
async def confirm_mfa_enrolment(
    payload: MfaVerify,
    request: Request,
    actor: Actor = Depends(get_current_actor),
    session: AsyncSession = Depends(get_session),
) -> Ack:
    if not actor.user.mfa_secret or not verify_mfa_code(actor.user.mfa_secret, payload.code):
        raise AppError("MFA_INVALID")
    actor.user.mfa_enrolled_at = now_utc()
    await audit_service.record(
        session,
        action=AuditAction.MFA_ENROLLED,
        actor_id=actor.id,
        actor_role=actor.user.role,
        target_type="user",
        target_id=actor.id,
        ip=client_ip(request),
    )
    await session.commit()
    return Ack(message="Two-factor verification is on.", message_mr="दुहेरी पडताळणी सुरू झाली आहे.")


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    payload: RefreshRequest, request: Request, session: AsyncSession = Depends(get_session)
) -> TokenResponse:
    """Rotate the refresh token.  Replaying an old one revokes the session."""
    pair = await auth_service.refresh_tokens(
        session,
        refresh_token=payload.refresh_token,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    await session.commit()
    return _token_response(pair)


@router.post("/logout", response_model=Ack, status_code=status.HTTP_200_OK)
async def logout(
    payload: LogoutRequest,
    request: Request,
    actor: Actor = Depends(get_current_actor),
    session: AsyncSession = Depends(get_session),
) -> Ack:
    await auth_service.logout(
        session,
        user=actor.user,
        access_jti=actor.claims.jti,
        refresh_token=payload.refresh_token,
        ip=client_ip(request),
    )
    await session.commit()
    return Ack(message="Signed out.", message_mr="साइन आउट झाले.")


@router.get("/me", response_model=UserProfile)
async def me(actor: Actor = Depends(get_current_actor)) -> UserProfile:
    return UserProfile(**auth_service.profile_payload(actor.user))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# development sign-in
# ---------------------------------------------------------------------------
@router.post("/dev-login", response_model=TokenResponse, include_in_schema=False)
async def dev_login(
    payload: DevLogin, request: Request, session: AsyncSession = Depends(get_session)
) -> TokenResponse:
    """Sign in as a seeded staff account without a password. Development only.

    This exists because the alternative during a demo is typing a password and a
    six-digit TOTP into a control-room screen while somebody watches, and because
    an Administrator account with no enrolled secret cannot sign in at all
    (`login_with_password` refuses an MFA role without one, and enrolling needs a
    token you can only get by signing in).

    Three locks, all of which must be open:

    1. `ENVIRONMENT` must be `development`. Staging and production are refused
       here even if the flag below is somehow set.
    2. `DEV_LOGIN_ENABLED` must be explicitly true. It defaults to false, so
       this route is dead on a fresh checkout until somebody turns it on.
    3. `assert_production_safe()` lists the flag, so the app refuses to *boot*
       in production with it on — the same treatment `OTP_DEBUG_ECHO` gets.

    The sign-in is audited with `dev_login: true` in the metadata, so a token
    minted this way is distinguishable from a real one forever afterwards. That
    matters more than it looks: without it, a demo session and a genuine
    administrator action are the same row in the audit log.
    """
    if settings.is_production or settings.environment != "development":
        # 404 rather than 403 — a route that does not exist outside development
        # should not advertise that it exists.
        raise AppError("NOT_FOUND")
    if not settings.dev_login_enabled:
        raise AppError(
            "NOT_FOUND",
            details={"hint": "set DEV_LOGIN_ENABLED=true in .env to enable development sign-in"},
        )

    user = await auth_service.get_user_by_phone(session, normalise_phone(payload.phone))
    if user is None or not user.is_active:
        raise AppError("INVALID_CREDENTIALS", details={"reason": "no such seeded account"})
    if user.role == "pilgrim":
        # Pilgrims sign in by OTP, and `OTP_DEBUG_ECHO` already makes that a
        # two-call flow in development. Handing out pilgrim tokens here would be
        # a second, less visible path to the same thing.
        raise AppError("INVALID_CREDENTIALS", details={"reason": "dev login is for staff accounts"})

    # `mfa_verified=True` on purpose: the point of this route is to skip the
    # TOTP prompt. It is why the environment guards above are not negotiable.
    pair = await auth_service.issue_tokens(session, user, mfa_verified=True)

    await audit_service.record(
        session,
        action=AuditAction.LOGIN_SUCCESS,
        actor_id=user.id,
        actor_role=user.role,
        target_type="user",
        target_id=user.id,
        meta={"dev_login": True, "mfa_bypassed": requires_mfa(user.role)},
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    await session.commit()
    return _token_response(pair)
