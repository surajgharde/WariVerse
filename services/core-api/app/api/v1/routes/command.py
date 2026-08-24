"""Command-centre reads (Section 4/M3, Phase 4).

    GET /command/kpis      GET /command/changes
    GET /command/replay    GET /command/config

Four GETs and no writes.  Everything the console *does* — acknowledging an
alert, resolving it, moving a slot — already has a route that audits it under
`/alerts` and `/admin`.  Adding a second path to those actions here would mean
two places to keep an audit rule correct, and the one that gets forgotten is
always the newer one.

All four require `crowd:view_detail`, which is the permission that already
gates `/crowd/live` and the WebSocket.  That is deliberate: the KPI strip is a
zone-by-zone headcount in aggregate form, and a role that may not read the
detailed map may not read a total derived from it either.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_session
from app.core.deps import Actor, require
from app.core.permissions import Permission
from app.core.security import now_utc
from app.models import DENSITY_THRESHOLDS
from app.schemas.command import ChangeDigest, ConsoleConfig, KpiStrip, ReplayWindow
from app.schemas.common import ErrorResponse
from app.services import command_service, config_service

router = APIRouter(prefix="/command", tags=["command"], responses={404: {"model": ErrorResponse}})


@router.get("/kpis", response_model=KpiStrip)
async def kpis(
    _: Actor = Depends(require(Permission.CROWD_VIEW_DETAIL)),
    session: AsyncSession = Depends(get_session),
) -> KpiStrip:
    """The six numbers across the top of the console.

    A KPI whose `value` is `null` is one we are not measuring — the console
    renders a dash and the note, never a zero.  One of the six (breaches pending
    review) is structurally unavailable until Phase 6 lands and says so in
    `note`; the rest go `null` only when their input is actually missing.
    """
    return await command_service.kpi_strip(session)


@router.get("/changes", response_model=ChangeDigest)
async def changes(
    minutes: int = Query(default=command_service.DIGEST_MINUTES, ge=1, le=180),
    limit: int = Query(default=command_service.DIGEST_LIMIT, ge=1, le=200),
    _: Actor = Depends(require(Permission.CROWD_VIEW_DETAIL)),
    session: AsyncSession = Depends(get_session),
) -> ChangeDigest:
    """"What changed while I was away."

    `truncated` is set when the window held more than `limit` changes. The
    console must surface that — a digest that quietly drops half a surge is
    worse than one that admits it ran out of room.
    """
    return await command_service.change_digest(session, minutes=minutes, limit=limit)


@router.get("/replay", response_model=ReplayWindow)
async def replay(
    minutes: int = Query(default=60, ge=1, le=command_service.REPLAY_MAX_MINUTES),
    until: datetime | None = Query(default=None, description="End of the window; defaults to now"),
    zones: list[str] | None = Query(default=None, description="Zone codes; defaults to all active"),
    _: Actor = Depends(require(Permission.CROWD_VIEW_DETAIL)),
    session: AsyncSession = Depends(get_session),
) -> ReplayWindow:
    """Frames for the time-scrubber, one per minute.

    Minutes with no reading are absent from `frames` and named in the
    neighbouring frame's `unknown_zones` — the scrubber greys them rather than
    holding the last colour, because a replay that stays green through an
    outage is a replay that lies about the interval somebody will ask about.
    """
    return await command_service.replay_window(
        session, minutes=minutes, until=until, zone_codes=zones
    )


@router.get("/config", response_model=ConsoleConfig)
async def console_config(
    _: Actor = Depends(require(Permission.CROWD_VIEW_DETAIL)),
    session: AsyncSession = Depends(get_session),
) -> ConsoleConfig:
    """Server-side numbers the console must not hardcode.

    `server_time` is here so the console can measure its own clock skew.  An
    operator's laptop that is ninety seconds fast would render every live
    reading as stale, and the resulting "everything is grey" screen looks
    exactly like a dead pipeline.
    """
    policy = await command_service.escalation_policy(session)
    return ConsoleConfig(
        alert_escalate_seconds=policy.escalate_seconds,
        alert_page_seconds=policy.page_seconds,
        stale_reading_seconds=settings.stale_reading_seconds,
        crowd_window_seconds=await config_service.get_int(session, "crowd_window_seconds"),
        crowd_source=settings.crowd_source,
        density_thresholds={str(level): value for level, value in DENSITY_THRESHOLDS.items()},
        live_alert_counts=await command_service.live_alert_counts(session),
        server_time=now_utc(),
    )
