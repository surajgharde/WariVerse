"""Health and readiness (Section 9).

`/health` answers "is this process alive" — a load balancer question.
`/health/deep` answers "can this process do its job" — an operator question,
and it reports *degraded* rather than dead when a non-essential dependency is
down, because Section 1 requires the system to keep working on manual input
when the AI service or cache is gone.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_session
from app.core.redis_client import redis
from app.core.security import now_utc
from app.schemas.common import ApiModel

router = APIRouter(tags=["health"])

Health = Literal["ok", "degraded", "down"]


class ComponentHealth(ApiModel):
    status: Health
    latency_ms: float | None = None
    detail: str | None = None
    essential: bool


class HealthResponse(ApiModel):
    status: Health
    service: str
    environment: str
    server_time: datetime
    components: dict[str, ComponentHealth] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        service=settings.service_name,
        environment=settings.environment,
        server_time=now_utc(),
    )


@router.get("/health/deep", response_model=HealthResponse)
async def health_deep(response: Response, session: AsyncSession = Depends(get_session)) -> HealthResponse:
    components: dict[str, ComponentHealth] = {}

    # Postgres is the source of truth — without it there is no service.
    started = now_utc()
    try:
        await session.execute(text("SELECT 1"))
        components["postgres"] = ComponentHealth(
            status="ok",
            latency_ms=_ms_since(started),
            essential=True,
        )
    except Exception as exc:
        components["postgres"] = ComponentHealth(
            status="down", detail=type(exc).__name__, essential=True
        )

    # Redis backs OTP, sessions and rate limits.  Losing it degrades auth but
    # does not stop passes, incidents or alerts already in Postgres.
    started = now_utc()
    try:
        await redis.ping()
        components["redis"] = ComponentHealth(
            status="ok", latency_ms=_ms_since(started), essential=False
        )
    except Exception as exc:
        components["redis"] = ComponentHealth(
            status="down",
            detail=type(exc).__name__,
            essential=False,
        )

    # Timescale/PostGIS: present or the density and map layers are fiction.
    try:
        result = await session.execute(
            text("SELECT extname FROM pg_extension WHERE extname IN ('postgis','timescaledb')")
        )
        found = {row[0] for row in result}
        missing = {"postgis", "timescaledb"} - found
        components["extensions"] = ComponentHealth(
            status="ok" if not missing else "degraded",
            detail=None if not missing else f"missing: {', '.join(sorted(missing))}",
            essential=True,
        )
    except Exception as exc:
        components["extensions"] = ComponentHealth(status="down", detail=type(exc).__name__, essential=True)

    overall = _aggregate(components)
    if overall == "down":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    warnings = settings.assert_production_safe() if settings.is_production else []

    return HealthResponse(
        status=overall,
        service=settings.service_name,
        environment=settings.environment,
        server_time=now_utc(),
        components=components,
        warnings=warnings,
    )


def _ms_since(started: datetime) -> float:
    return round((now_utc() - started).total_seconds() * 1000, 2)


def _aggregate(components: dict[str, ComponentHealth]) -> Health:
    if any(c.status == "down" and c.essential for c in components.values()):
        return "down"
    if any(c.status != "ok" for c in components.values()):
        return "degraded"
    return "ok"


def component_summary(components: dict[str, ComponentHealth]) -> dict[str, Any]:  # pragma: no cover
    return {name: c.status for name, c in components.items()}
