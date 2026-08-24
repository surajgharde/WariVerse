"""Headless pipeline runner.

    python -m app.worker

`main.py` already runs the pipeline in its lifespan, so for the compose stack
one container is enough.  This entrypoint exists for two real cases:

* **Scaling out.** With forty cameras, detection saturates a box long before the
  HTTP surface does. Run N workers over disjoint camera sets and one API.
* **Diagnosis.** `--windows 5 --dry-run` runs the exact production path five
  times and prints what it *would* publish, without touching the core API. When
  a zone reads 6 p/m² at 2 a.m., this is how you find out whether it is the
  crowd, the calibration or the detector.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import settings
from app.heatmap import render_overlay, render_series, summarise
from app.logging import configure_logging, get_logger
from app.models import ZoneObservation
from app.pipeline import Pipeline
from app.publisher import Publisher, PublishResult

logger = get_logger(__name__)


class DryRunPublisher(Publisher):
    """Fetches configuration, publishes nothing.

    Config still comes from the core API — running the pipeline against invented
    zones would diagnose a system that does not exist.
    """

    async def publish(self, observations: list[ZoneObservation]) -> PublishResult:
        for observation in observations:
            print(json.dumps(observation.to_payload(), ensure_ascii=False))
        return PublishResult(ok=True, accepted=len(observations), rejected=0, alerts_raised=0, buffered=0)

    async def heartbeat(self, cameras: list[dict[str, Any]]) -> bool:
        return True


async def run(args: argparse.Namespace) -> int:
    configure_logging()
    pipeline = Pipeline(publisher=DryRunPublisher() if args.dry_run else None)

    if not await pipeline.load_config():
        logger.error("config_unavailable", extra={"core_api": settings.core_api_url})
        return 2

    if args.windows:
        for index in range(args.windows):
            observations = await pipeline.run_window()
            logger.info("window", extra={"index": index + 1, **summarise(observations)})
            if index + 1 < args.windows:
                await asyncio.sleep(args.interval if args.interval is not None else settings.window_seconds)

        if args.overlay_dir:
            written = _write_overlays(pipeline, Path(args.overlay_dir))
            logger.info("overlays_written", extra={"count": len(written), "dir": args.overlay_dir})
            for path in written:
                print(path)

        await pipeline.stop()
        return 0

    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows has no SIGTERM handler
            loop.add_signal_handler(sig, stopping.set)

    task = asyncio.create_task(pipeline.run_forever(), name="pipeline")
    await stopping.wait()
    await pipeline.stop()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    return 0


def _write_overlays(pipeline: Pipeline, directory: Path) -> list[Path]:
    """Heat map per zone plus one density series — Section 4/M2's acceptance."""
    written: list[Path] = []
    latest = pipeline.recent[-1] if pipeline.recent else []
    for observation in latest:
        written.append(render_overlay(observation).write(directory))

    if latest:
        busiest = max(latest, key=lambda o: o.density)
        series = pipeline.zone_series(busiest.zone_code)
        if series:
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
            path = directory / f"series-{busiest.zone_code}-{stamp}.svg"
            path.write_text(render_series(series), encoding="utf-8")
            written.append(path)
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description="WariVerse crowd pipeline worker")
    parser.add_argument("--windows", type=int, default=0, help="Run N windows and exit (0 = run forever)")
    parser.add_argument(
        "--interval", type=float, default=None, help="Seconds between windows (default: WINDOW_SECONDS)"
    )
    parser.add_argument("--dry-run", action="store_true", help="Print observations instead of publishing them")
    parser.add_argument("--overlay-dir", default=None, help="Write heat map and series SVGs here after the run")
    args = parser.parse_args()

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
