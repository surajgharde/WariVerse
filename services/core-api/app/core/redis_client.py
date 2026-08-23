"""Redis connection: cache, OTP store, refresh-token families, rate limits.

Redis is a *dependency*, not the source of truth.  Section 1 requires graceful
degradation, so callers that can survive without it use `redis_available()` and
fall back rather than 500.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable
from typing import TypeVar

from redis.asyncio import ConnectionPool, Redis

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T")


async def aw(value: Awaitable[T] | T) -> T:
    """Narrow redis-py's sync-or-async return type to the async one.

    redis-py shares command signatures between its sync and async clients, so
    several commands are typed `Awaitable[T] | T`.  On the async client they are
    always awaitable; this makes that explicit instead of scattering casts.
    """
    if inspect.isawaitable(value):
        return await value
    return value

# decode_responses=True, so every value comes back as str, not bytes.
_pool: ConnectionPool = ConnectionPool.from_url(
    settings.redis_url,
    decode_responses=True,
    max_connections=50,
    health_check_interval=30,
)

redis: Redis = Redis(connection_pool=_pool)


async def redis_available() -> bool:
    try:
        return bool(await redis.ping())
    except Exception as exc:
        logger.warning("redis_unavailable", extra={"error": str(exc)})
        return False


async def close_redis() -> None:
    await redis.aclose()
    await _pool.aclose()
