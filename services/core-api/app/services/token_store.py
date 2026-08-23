"""Refresh-token families and access-token revocation, in Redis.

Rotation with reuse detection: each login opens a *family*.  Only the most
recent refresh token in a family is valid.  Presenting an older one means a
token was stolen and replayed, so the entire family is revoked and the user has
to sign in again.
"""

from __future__ import annotations

from app.core.config import settings
from app.core.errors import AppError
from app.core.logging import get_logger
from app.core.redis_client import aw, redis

logger = get_logger(__name__)

_FAMILY_KEY = "auth:refresh:family:{family}"
_DENY_KEY = "auth:access:denied:{jti}"
_MFA_KEY = "auth:mfa:verified:{user_id}:{jti}"


def _family_ttl() -> int:
    return settings.refresh_token_ttl_days * 24 * 3600


async def open_family(family: str, jti: str, user_id: str) -> None:
    await aw(
        redis.hset(
            _FAMILY_KEY.format(family=family),
            mapping={"current": jti, "user_id": user_id},
        )
    )
    await aw(redis.expire(_FAMILY_KEY.format(family=family), _family_ttl()))


async def rotate_refresh(family: str, presented_jti: str, new_jti: str) -> None:
    """Advance the family, or blow it up if an old token was replayed."""
    key = _FAMILY_KEY.format(family=family)
    current = await aw(redis.hget(key, "current"))

    if current is None:
        # Family already revoked, or Redis lost it (restart / eviction).  Either
        # way we cannot prove this token is the current one, so refuse.
        raise AppError("TOKEN_INVALID", details={"reason": "session not found"})

    if current != presented_jti:
        await redis.delete(key)
        logger.warning("refresh_token_reuse", extra={"family": family})
        raise AppError("TOKEN_REUSED")

    await aw(redis.hset(key, "current", new_jti))
    await aw(redis.expire(key, _family_ttl()))


async def revoke_family(family: str) -> None:
    await redis.delete(_FAMILY_KEY.format(family=family))


async def deny_access_token(jti: str, ttl_seconds: int | None = None) -> None:
    """Blacklist a still-valid access token (logout, forced sign-out).

    TTL matches the token's own lifetime — after that it expires on its own and
    the entry is dead weight.
    """
    ttl = ttl_seconds if ttl_seconds is not None else settings.access_token_ttl_minutes * 60
    if ttl > 0:
        await redis.setex(_DENY_KEY.format(jti=jti), ttl, "1")


async def is_access_denied(jti: str) -> bool:
    return await redis.exists(_DENY_KEY.format(jti=jti)) == 1


async def mark_mfa_verified(user_id: str, jti: str) -> None:
    await redis.setex(
        _MFA_KEY.format(user_id=user_id, jti=jti),
        settings.access_token_ttl_minutes * 60,
        "1",
    )
