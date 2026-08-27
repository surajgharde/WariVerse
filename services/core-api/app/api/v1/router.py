"""Aggregate router for /api/v1.

Modules are added here as their phase lands, so this file doubles as a map of
what is actually built versus what Section 9 still promises.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.routes import (
    admin,
    alerts,
    assistant,
    auth,
    breach,
    cameras,
    checkpoints,
    command,
    crowd,
    forecast,
    incidents,
    ingest,
    missing_persons,
    palkhi,
    pass_admin,
    passes,
    pilgrim,
    users,
    ws,
)

api_router = APIRouter()

# Phase 1 — foundation
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(admin.router)

# Phase 2 — smart darshan pass
api_router.include_router(passes.router)
api_router.include_router(checkpoints.router)
api_router.include_router(pass_admin.router)

# Phase 3 — crowd intelligence
api_router.include_router(crowd.router)
api_router.include_router(alerts.router)
api_router.include_router(cameras.router)
api_router.include_router(ingest.router)
api_router.include_router(ws.router)

# Phase 4 — command centre
api_router.include_router(command.router)

# Phase 5 — incidents, SOS, dispatch, missing persons
api_router.include_router(incidents.router)
api_router.include_router(missing_persons.router)

# Phase 6 — breach ledger, review, evidence clips, tripwires
api_router.include_router(breach.router)
# Phase 7 — pilgrim PWA support (facilities, offline essentials)
api_router.include_router(pilgrim.router)

# Phase 8 — predictive forecasting
api_router.include_router(forecast.router)

# Phase 9 — palkhi, assistant (dindis, halt-towns, assistant)
api_router.include_router(palkhi.router)
api_router.include_router(assistant.router)
