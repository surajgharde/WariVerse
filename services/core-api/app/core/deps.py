"""FastAPI dependencies: the single gate every protected route passes through.

Routes declare the *permission* they need:

    @router.post("/breaches/{id}/review")
    async def review(actor: Actor = Depends(require(Permission.BREACH_REVIEW))): ...

Not a role.  The matrix in `core/permissions.py` decides who that includes.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_session
from app.core.errors import AppError
from app.core.permissions import Permission, Role, has_permission, permissions_for, requires_mfa
from app.core.security import TokenClaims, decode_token
from app.models import User
from app.services import token_store

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True, slots=True)
class Actor:
    """The authenticated caller, resolved once per request."""

    user: User
    claims: TokenClaims
    ip: str | None
    user_agent: str | None

    @property
    def id(self) -> uuid.UUID:
        return self.user.id

    @property
    def role(self) -> Role:
        return Role(self.user.role)

    @property
    def permissions(self) -> frozenset[Permission]:
        return permissions_for(self.user.role)

    def can(self, permission: Permission) -> bool:
        return permission in self.permissions


def client_ip(request: Request) -> str | None:
    # Behind Nginx; trust the first hop only because the edge is ours.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


async def get_current_actor(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_session),
) -> Actor:
    if credentials is None or not credentials.credentials:
        raise AppError("UNAUTHENTICATED")

    claims = decode_token(credentials.credentials, expected_type="access")

    if await token_store.is_access_denied(claims.jti):
        raise AppError("TOKEN_INVALID", details={"reason": "session ended"})

    try:
        user_id = uuid.UUID(claims.subject)
    except ValueError as exc:
        raise AppError("TOKEN_INVALID") from exc

    user = await session.get(User, user_id)
    if user is None:
        raise AppError("TOKEN_INVALID", details={"reason": "account not found"})
    if not user.is_active:
        raise AppError("ACCOUNT_DISABLED")

    # A role change takes effect on the next request, not on the next login —
    # a revoked officer should not keep their permissions for 15 minutes.
    if user.role != str(claims.role):
        raise AppError("TOKEN_INVALID", details={"reason": "role changed, sign in again"})

    if requires_mfa(user.role) and not claims.mfa_verified:
        raise AppError("MFA_REQUIRED")

    return Actor(
        user=user,
        claims=claims,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )


async def get_optional_actor(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_session),
) -> Actor | None:
    """For endpoints a pilgrim may browse without signing in (Section 2)."""
    if credentials is None or not credentials.credentials:
        return None
    try:
        return await get_current_actor(request, credentials, session)
    except AppError:
        return None


def require(*permissions: Permission) -> Callable[[Actor], Awaitable[Actor]]:
    """Require every listed permission."""

    async def _guard(actor: Actor = Depends(get_current_actor)) -> Actor:
        missing = [str(p) for p in permissions if not has_permission(actor.user.role, p)]
        if missing:
            raise AppError("FORBIDDEN", details={"missing_permissions": missing, "role": actor.user.role})
        return actor

    return _guard


def require_any(*permissions: Permission) -> Callable[[Actor], Awaitable[Actor]]:
    async def _guard(actor: Actor = Depends(get_current_actor)) -> Actor:
        if not any(has_permission(actor.user.role, p) for p in permissions):
            raise AppError(
                "FORBIDDEN",
                details={"requires_any_of": [str(p) for p in permissions], "role": actor.user.role},
            )
        return actor

    return _guard


async def require_ai_service(request: Request) -> str:
    """Guard for the AI engine's event ingress.

    The AI service is a machine caller with no user identity; it holds a shared
    secret and can only publish events — it never writes to the database
    directly (Section 6 boundary).
    """
    token = request.headers.get("x-ai-service-token")
    if not token or token != settings.ai_service_token:
        raise AppError("UNAUTHENTICATED", details={"reason": "ai service token"})
    return token
