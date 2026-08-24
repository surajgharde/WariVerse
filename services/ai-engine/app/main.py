"""WariVerse AI Engine — crowd analytics as a separate, restartable service.

Section 4/M2 requires this to be its own service, not a module inside the core
API, and to be independently restartable.  It is, and the reason is operational
rather than architectural: this is the process running vision code against forty
RTSP streams on a machine in a temple office.  It is the one most likely to leak
memory, wedge on a bad codec, or need a kick at 3 a.m.  Everything that must not
be lost when that happens lives on the other side of an HTTP boundary.

Its HTTP surface is small and exists for three jobs:

* **Observability** — `/status` says what it is doing, per camera, honestly.
* **The demo** — `/sim/inject` is Section 16's T+1:30 Palkhi surge.
* **Calibration** — it can pull a still off a stream, which the core API
  cannot, so it serves the point-and-click page.  The page posts the resulting
  points to the core API. This service still never writes to the database.

Mutating endpoints require the same `x-ai-service-token` the publisher uses.
`/sim/inject` changes what the command centre sees, so it is not left open on
the internal network.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field

from app import detector as detector_module
from app import video
from app.config import settings
from app.heatmap import render_overlay, render_series
from app.logging import configure_logging, get_logger
from app.pipeline import Pipeline

configure_logging()
logger = get_logger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

pipeline = Pipeline()
_task: asyncio.Task[None] | None = None


async def require_service_token(request: Request) -> None:
    """Same shared secret the publisher uses outbound.

    An engine that anyone on the network can tell to inject a Palkhi surge is an
    engine that can be used to make a real control room ignore a real alert.
    """
    token = request.headers.get("x-ai-service-token")
    if not token or token != settings.ai_service_token:
        raise HTTPException(status_code=401, detail="ai service token required")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    global _task
    problems = settings.assert_production_safe()
    if settings.environment == "production" and problems:
        raise RuntimeError("Unsafe production configuration: " + "; ".join(problems))
    if problems:
        logger.warning("insecure_defaults_in_use", extra={"problems": problems})

    logger.info(
        "ai_engine_started",
        extra={
            "source": settings.crowd_source,
            "core_api": settings.core_api_url,
            "window_seconds": settings.window_seconds,
            "sample_fps": settings.sample_fps,
        },
    )
    _task = asyncio.create_task(pipeline.run_forever(), name="pipeline")

    yield

    await pipeline.stop()
    if _task is not None:
        _task.cancel()
        await asyncio.gather(_task, return_exceptions=True)
    logger.info("ai_engine_stopped")


app = FastAPI(
    title="WariVerse AI Engine",
    version="0.1.0",
    description=(
        "Anonymous crowd analytics for the Pandharpur Wari.\n\n"
        "This service performs no facial recognition, no gait analysis, builds no "
        "biometric templates and does no cross-camera re-identification. Track ids "
        "are local to one camera and one ten-second window, and are discarded once "
        "a flow vector has been computed. Nothing that identifies a person is "
        "published, stored or logged."
    ),
    lifespan=lifespan,
    docs_url=None if settings.environment == "production" else "/docs",
)


@app.get("/", include_in_schema=False)
async def root() -> dict[str, Any]:
    return {
        "service": "wariverse-ai-engine",
        "version": "0.1.0",
        "source": settings.crowd_source,
        "privacy": "aggregate crowd metrics only; no biometrics, no individual tracking",
        "calibration_ui": "/calibrate",
    }


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------
@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "service": settings.service_name, "server_time": datetime.now(UTC)}


@app.get("/health/deep")
async def health_deep(response: Response) -> dict[str, Any]:
    """Reports *degraded* rather than dead wherever the work can continue.

    Losing the core API means readings buffer — degraded, not down.  Losing the
    vision stack in `sim` mode is not even degraded.  What makes this service
    down is producing nothing at all.
    """
    components: dict[str, dict[str, Any]] = {}

    core_ok = pipeline.publisher.is_connected
    components["core_api"] = {
        "status": "ok" if core_ok else "degraded",
        "essential": True,
        "detail": None if core_ok else f"buffering {pipeline.publisher.buffered} readings",
    }

    if settings.crowd_source == "sim":
        components["source"] = {
            "status": "ok",
            "essential": True,
            "detail": "simulation — these numbers are generated, not measured",
        }
    else:
        online = sum(1 for c in pipeline.cameras.values() if c.status == "online")
        total = len(pipeline.cameras)
        components["cameras"] = {
            "status": "ok" if online == total and total else ("degraded" if online else "down"),
            "essential": True,
            "detail": f"{online}/{total} online",
        }
        components["vision"] = {
            "status": "ok" if video.is_available() and detector_module.describe()["available"] else "down",
            "essential": True,
            "detail": pipeline.detector.failure,
        }

    stale = (
        pipeline.last_window_at is None
        or (datetime.now(UTC) - pipeline.last_window_at).total_seconds() > settings.window_seconds * 6
    )
    components["pipeline"] = {
        "status": "down" if stale else "ok",
        "essential": True,
        "detail": "no window completed recently" if stale else None,
    }

    overall = "ok"
    if any(c["status"] == "down" and c["essential"] for c in components.values()):
        overall = "down"
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    elif any(c["status"] != "ok" for c in components.values()):
        overall = "degraded"

    return {
        "status": overall,
        "service": settings.service_name,
        "environment": settings.environment,
        "server_time": datetime.now(UTC),
        "components": components,
    }


@app.get("/status")
async def engine_status() -> dict[str, Any]:
    return {**pipeline.status(), "detector": detector_module.describe(), "opencv": video.is_available()}


@app.post("/reload", dependencies=[Depends(require_service_token)])
async def reload_config() -> dict[str, Any]:
    """Re-pull zones and calibrations without a restart."""
    ok = await pipeline.load_config()
    return {"reloaded": ok, "zones": len(pipeline.zones), "at": datetime.now(UTC)}


# ---------------------------------------------------------------------------
# simulation controls (Section 16 demo)
# ---------------------------------------------------------------------------
class InjectionRequest(BaseModel):
    kind: str = Field(
        default="palkhi_surge",
        pattern="^(palkhi_surge|crowd_surge|stall|counterflow|clear)$",
    )
    zone_code: str | None = Field(default=None, max_length=20)
    magnitude: float = Field(default=1.0, gt=0, le=10)
    duration_seconds: int = Field(default=600, ge=10, le=7200)
    note: str | None = Field(default=None, max_length=200)


@app.post("/sim/inject", dependencies=[Depends(require_service_token)])
async def sim_inject(payload: InjectionRequest) -> dict[str, Any]:
    """Inject an event into the simulation.

    T+1:30 of the demo script is one call to this with `palkhi_surge` on the
    gate plaza.  The surge then travels the same path a real one would — window,
    ingest, thresholds, rule table, alert, socket — because there is no demo
    path through this system.
    """
    if settings.crowd_source != "sim" or pipeline.sim is None:
        raise HTTPException(status_code=409, detail=f"not in simulation mode (CROWD_SOURCE={settings.crowd_source})")

    injection = pipeline.sim.inject(
        payload.kind,
        zone_code=payload.zone_code,
        magnitude=payload.magnitude,
        duration_seconds=payload.duration_seconds,
        note=payload.note,
    )
    logger.info(
        "sim_injected",
        extra={
            "kind": injection.kind,
            "zone": injection.zone_code,
            "magnitude": injection.magnitude,
            "seconds": payload.duration_seconds,
        },
    )
    return {
        "kind": injection.kind,
        "zone_code": injection.zone_code,
        "magnitude": injection.magnitude,
        "starts_at": injection.starts_at,
        "ends_at": injection.ends_at,
        "note": injection.note,
    }


@app.delete("/sim/inject", dependencies=[Depends(require_service_token)])
async def sim_clear() -> dict[str, int]:
    if pipeline.sim is None:
        return {"cleared": 0}
    return {"cleared": pipeline.sim.clear_injections()}


@app.get("/sim/status")
async def sim_status() -> dict[str, Any]:
    if pipeline.sim is None:
        return {"enabled": False, "reason": f"CROWD_SOURCE={settings.crowd_source}"}
    now = datetime.now(UTC)
    return {
        "enabled": True,
        "zones": len(pipeline.sim.zones),
        "ekadashi": str(pipeline.sim.ekadashi),
        "baseline_multiplier": pipeline.sim.baseline_multiplier,
        "injections": [
            {
                "kind": i.kind,
                "zone_code": i.zone_code,
                "magnitude": i.magnitude,
                "weight_now": round(i.weight(now), 4),
                "starts_at": i.starts_at,
                "ends_at": i.ends_at,
                "note": i.note,
            }
            for i in pipeline.sim.injections
        ],
    }


# ---------------------------------------------------------------------------
# overlays (Section 4/M2 acceptance)
# ---------------------------------------------------------------------------
@app.get("/zones/{zone_code}/heatmap.svg")
async def zone_heatmap(zone_code: str) -> Response:
    """Density heat map for a zone's latest window.

    An operator's sanity check on the pipeline: is the detector seeing the
    crowd, or is it seeing umbrellas?
    """
    observation = pipeline.latest_for(zone_code)
    if observation is None:
        raise HTTPException(status_code=404, detail=f"no recent observation for zone {zone_code}")
    overlay = render_overlay(observation)
    return Response(content=overlay.svg, media_type="image/svg+xml")


@app.get("/zones/{zone_code}/series.svg")
async def zone_series_svg(zone_code: str) -> Response:
    """Density over the last few minutes, with the safety bands drawn behind."""
    points = pipeline.zone_series(zone_code)
    if not points:
        raise HTTPException(status_code=404, detail=f"no recent observations for zone {zone_code}")
    return Response(content=render_series(points), media_type="image/svg+xml")


@app.get("/zones")
async def list_zones() -> list[dict[str, Any]]:
    return [
        {
            "zone_id": z.zone_id,
            "code": z.code,
            "name": z.name,
            "area_m2": z.area_m2,
            "capacity_persons": z.capacity_persons,
            "zone_type": z.zone_type,
            "cameras": [
                {"camera_id": c.camera_id, "name": c.name, "calibrated": c.is_calibrated} for c in z.cameras
            ],
        }
        for z in pipeline.zones
    ]


# ---------------------------------------------------------------------------
# calibration support
# ---------------------------------------------------------------------------
@app.get("/cameras/{camera_id}/frame.jpg")
async def camera_still(camera_id: str) -> Response:
    """One still frame, for clicking calibration points on.

    Not cached and not written to disk.  A frame of a temple crowd is exactly
    the kind of thing that should not accumulate in a container's filesystem
    waiting for someone to find it.
    """
    runtime = pipeline.cameras.get(camera_id)
    if runtime is None or runtime.source is None:
        raise HTTPException(status_code=404, detail="camera has no stream in this mode")

    frame = await asyncio.to_thread(runtime.source.grab_still)
    if frame is None:
        raise HTTPException(status_code=503, detail="could not read a frame from this camera")

    try:
        import cv2
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="opencv is not installed in this image") from exc

    ok, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    if not ok:
        raise HTTPException(status_code=500, detail="frame could not be encoded")
    return Response(
        content=buffer.tobytes(),
        media_type="image/jpeg",
        headers={"cache-control": "no-store"},
    )


@app.get("/calibrate", include_in_schema=False)
async def calibration_page() -> Response:
    page = STATIC_DIR / "calibrate.html"
    if not page.is_file():
        raise HTTPException(status_code=404, detail="calibration page is missing from this image")
    return Response(
        content=page.read_text(encoding="utf-8"),
        media_type="text/html; charset=utf-8",
        # The page talks to the core API, loads a still from here, and must not
        # be able to reach anywhere else.
        headers={
            "content-security-policy": (
                "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; "
                "script-src 'unsafe-inline'; connect-src *"
            ),
            "x-frame-options": "DENY",
        },
    )


@app.get("/core-api", include_in_schema=False)
async def core_api_hint(check: bool = Query(default=False)) -> dict[str, Any]:
    """Where the state lives, and optionally whether it is answering."""
    result: dict[str, Any] = {"url": settings.core_api_url, "ingest": settings.ingest_url}
    if check:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                response = await client.get(f"{settings.core_api_url.rstrip('/')}/health")
            result["reachable"] = response.status_code < 400
        except (httpx.HTTPError, OSError) as exc:
            result["reachable"] = False
            result["error"] = str(exc)
    return result


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    uvicorn.run(app, host="0.0.0.0", port=settings.ai_engine_port)  # noqa: S104
