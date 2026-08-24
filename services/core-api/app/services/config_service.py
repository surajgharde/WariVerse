"""Read operator-tunable settings from `system_config`.

Values live in the database so a temple administrator can change the walk-in
reserve or the throughput target during the Wari without a deploy.  Reads are
cached for a few seconds — these are consulted on every booking, and a slot
grid does not need to see a config change within the same second.

Falls back to `DEFAULT_CONFIG` when the row is missing or the database is
unreachable: a missing tunable must not stop pilgrims booking (Section 1).
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any, TypeVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.security import now_utc
from app.models import DEFAULT_CONFIG, SystemConfig

logger = get_logger(__name__)

T = TypeVar("T")

CACHE_TTL = timedelta(seconds=15)

_cache: dict[str, Any] = {}
_cached_at: datetime | None = None


def clear_cache() -> None:
    """Call after writing config, and between tests."""
    global _cached_at
    _cache.clear()
    _cached_at = None


async def _load(session: AsyncSession) -> dict[str, Any]:
    global _cached_at
    if _cached_at is not None and now_utc() - _cached_at < CACHE_TTL:
        return _cache

    try:
        rows = await session.execute(select(SystemConfig))
        _cache.clear()
        for row in rows.scalars():
            _cache[row.key] = row.value.get("v")
        _cached_at = now_utc()
    except Exception as exc:
        logger.warning("system_config_read_failed", extra={"error": str(exc)})
    return _cache


async def get(session: AsyncSession, key: str) -> Any:
    values = await _load(session)
    if key in values:
        return values[key]
    if key in DEFAULT_CONFIG:
        return DEFAULT_CONFIG[key][0]
    raise KeyError(f"Unknown configuration key: {key}")


async def get_int(session: AsyncSession, key: str) -> int:
    return int(await get(session, key))


async def get_float(session: AsyncSession, key: str) -> float:
    return float(await get(session, key))


def parse_time(value: str) -> time:
    hour, minute = value.split(":")
    return time(hour=int(hour), minute=int(minute))


async def get_time(session: AsyncSession, key: str) -> time:
    return parse_time(str(await get(session, key)))
