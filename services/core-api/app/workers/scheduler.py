"""Interval scheduler for the background jobs.

Runs as its own process:

    python -m app.workers.scheduler

Deliberately small: an asyncio loop per job, a Redis lock so only one replica
acts, and a failure in one job that cannot stop the others.  Section 6 names
Celery/RQ for async jobs; that adds a broker and a worker image for two
five-minute timers.  The jobs in `jobs.py` are plain callables, so registering
them as Celery tasks later is a decorator, not a rewrite.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
from collections.abc import Awaitable, Callable

from app.core.db import dispose_engine
from app.core.logging import configure_logging, get_logger
from app.core.redis_client import close_redis
from app.workers import jobs

logger = get_logger(__name__)

Job = Callable[[], Awaitable[None]]

SCHEDULE: list[tuple[str, Job, int]] = [
    ("reslot", jobs.scheduled_reslot, jobs.RESLOT_INTERVAL_SECONDS),
    ("expire_no_shows", jobs.scheduled_expiry, jobs.EXPIRY_INTERVAL_SECONDS),
    ("camera_watchdog", jobs.scheduled_camera_watchdog, jobs.CAMERA_WATCHDOG_INTERVAL_SECONDS),
    ("alert_maintenance", jobs.scheduled_alert_maintenance, jobs.ALERT_MAINTENANCE_INTERVAL_SECONDS),
    ("incident_sla", jobs.scheduled_incident_sla, jobs.SLA_SWEEP_INTERVAL_SECONDS),
    ("photo_purge", jobs.scheduled_photo_purge, jobs.PHOTO_PURGE_INTERVAL_SECONDS),
    ("breach_purge", jobs.scheduled_breach_purge, jobs.BREACH_PURGE_INTERVAL_SECONDS),
    ("chain_verify", jobs.scheduled_chain_verification, jobs.CHAIN_VERIFY_INTERVAL_SECONDS),
    ("palkhi_sweep", jobs.scheduled_palkhi_sweep, jobs.PALKHI_SWEEP_INTERVAL_SECONDS),
    ("assistant_purge", jobs.scheduled_assistant_purge, jobs.ASSISTANT_PURGE_INTERVAL_SECONDS),
]


async def _run_forever(name: str, job: Job, interval: int, stopping: asyncio.Event) -> None:
    # Stagger the first run so every job does not hit the database at boot.
    await asyncio.sleep(min(interval, 10))
    while not stopping.is_set():
        try:
            await job()
        except Exception:
            logger.exception("job_failed", extra={"job": name})
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stopping.wait(), timeout=interval)


async def main() -> None:
    configure_logging()
    stopping = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows has no SIGTERM handler
            loop.add_signal_handler(sig, stopping.set)

    logger.info("scheduler_started", extra={"jobs": [name for name, _, _ in SCHEDULE]})
    tasks = [
        asyncio.create_task(_run_forever(name, job, interval, stopping), name=name)
        for name, job, interval in SCHEDULE
    ]

    await stopping.wait()
    logger.info("scheduler_stopping")
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    await close_redis()
    await dispose_engine()
    logger.info("scheduler_stopped")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
