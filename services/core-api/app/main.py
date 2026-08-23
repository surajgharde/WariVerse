"""WariVerse Core API.

Owns all state.  The AI service publishes events to it and never writes to the
database directly (Section 6), which is what lets the AI service be restarted
mid-Wari without losing a single pass or incident.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.api.v1.routes import health
from app.core.config import settings
from app.core.db import dispose_engine
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.middleware import register_middleware
from app.core.redis_client import close_redis, redis_available

if sys.platform == "win32":
    # psycopg's async mode cannot run on Windows' default ProactorEventLoop.
    # Deployment is Linux; this only matters when a developer runs uvicorn
    # directly on Windows instead of through Docker.
    #
    # Only swap the policy while bootstrapping.  Under pytest this module is
    # imported *inside* a running loop, and changing the policy then detaches
    # already-open connections from the loop they were created on.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

configure_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    problems = settings.assert_production_safe()
    if settings.is_production and problems:
        # Refuse to start rather than serve a temple trust with dev secrets.
        raise RuntimeError("Unsafe production configuration: " + "; ".join(problems))
    if problems:
        logger.warning("insecure_defaults_in_use", extra={"problems": problems})

    if not await redis_available():
        # Not fatal: Section 1 requires the system to keep working when a
        # dependency dies.  Auth degrades; passes and incidents do not.
        logger.warning("starting_without_redis", extra={"redis_url": settings.redis_url})

    logger.info(
        "core_api_started",
        extra={"environment": settings.environment, "crowd_source": settings.crowd_source},
    )
    yield
    await close_redis()
    await dispose_engine()
    logger.info("core_api_stopped")


app = FastAPI(
    title="WariVerse Core API",
    version="0.1.0",
    description=(
        "Crowd intelligence and pilgrim management for the Pandharpur Wari.\n\n"
        "Privacy note: this API carries no biometric data, no facial recognition "
        "and no individual pilgrim tracking. Crowd analytics are anonymous and "
        "aggregate by construction."
    ),
    lifespan=lifespan,
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None if settings.is_production else "/redoc",
    openapi_url=None if settings.is_production else "/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["authorization", "content-type", "x-trace-id", "accept-language"],
    expose_headers=["x-trace-id", "server-timing"],
    max_age=600,
)

register_middleware(app)
register_exception_handlers(app)

app.include_router(health.router)
app.include_router(api_router, prefix=settings.api_v1_prefix)


@app.get("/", include_in_schema=False)
async def root() -> dict[str, str]:
    return {
        "service": "wariverse-core-api",
        "version": "0.1.0",
        "docs": "/docs" if not settings.is_production else "disabled",
    }
