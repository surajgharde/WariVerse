"""Publishing observations to the core API — the only way out of this process.

The AI service holds no database credentials.  Everything it learns leaves
through `POST /api/v1/ingest/density`, authenticated with a shared service
token.  That is the Section 6 boundary, and it is what makes "kill the container
live" a survivable demo moment rather than a data-loss event.

When the core API is unreachable, readings go into a bounded in-memory buffer
and are replayed on the next successful publish.  Bounded, and it drops the
*oldest* first: at four minutes of backlog, the reading from four minutes ago is
of no use to anyone, and the one from ten seconds ago is the whole job.

The buffer is deliberately not on disk.  A crowd reading has a useful life
measured in minutes; persisting it across a restart would mean replaying stale
density into a live map, which is the failure this system is most careful to
avoid.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from app.config import settings
from app.logging import get_logger
from app.models import ZoneObservation

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PublishResult:
    ok: bool
    accepted: int
    rejected: int
    alerts_raised: int
    buffered: int
    error: str | None = None


class Publisher:
    """Batches observations to the core API, with replay on reconnect."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._owns_client = client is None
        self._buffer: deque[dict[str, Any]] = deque(maxlen=settings.buffer_max_readings)
        self.last_success: datetime | None = None
        self.last_error: str | None = None
        self.consecutive_failures = 0

    @property
    def buffered(self) -> int:
        return len(self._buffer)

    @property
    def is_connected(self) -> bool:
        return self.consecutive_failures == 0

    async def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=settings.publish_timeout_seconds)
        return self._client

    @staticmethod
    def _auth() -> dict[str, str]:
        """Per-request rather than baked into the client.

        A client passed in by a caller — a test, or a shared pool — would
        otherwise silently lose the token and every publish would 401. The
        credential belongs to the request, not to the connection.
        """
        return {"x-ai-service-token": settings.ai_service_token}

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def publish(self, observations: list[ZoneObservation]) -> PublishResult:
        """Send this window, and everything that failed to send before it."""
        payloads = [o.to_payload() for o in observations]

        # Replayed readings go first so the core API sees them in the order they
        # happened; it rejects anything older than fifteen minutes anyway, which
        # bounds how stale a replay can be.
        batch = list(self._buffer) + payloads
        self._buffer.clear()

        if not batch:
            return PublishResult(ok=True, accepted=0, rejected=0, alerts_raised=0, buffered=0)

        # Respect the server's batch cap by keeping the newest.
        if len(batch) > settings.buffer_max_readings:
            batch = batch[-settings.buffer_max_readings :]

        try:
            client = await self.client()
            response = await client.post(
                f"{settings.ingest_url}/density",
                json={"source": settings.crowd_source, "readings": batch},
                headers=self._auth(),
            )
        except (httpx.HTTPError, OSError) as exc:
            return self._buffer_batch(batch, f"{type(exc).__name__}: {exc}")

        if response.status_code >= 500 or response.status_code == 429:
            # The core API is up but struggling — hold the readings and retry.
            return self._buffer_batch(batch, f"HTTP {response.status_code}")

        if response.status_code >= 400:
            # A 4xx is our fault and will not fix itself on retry.  Dropping is
            # correct; looping the same malformed batch forever is not.
            logger.error(
                "ingest_rejected",
                extra={"status": response.status_code, "body": response.text[:500], "dropped": len(batch)},
            )
            self.consecutive_failures = 0
            self.last_error = f"HTTP {response.status_code}"
            return PublishResult(
                ok=False, accepted=0, rejected=len(batch), alerts_raised=0, buffered=0, error=self.last_error
            )

        body = _safe_json(response)
        self.last_success = datetime.now(UTC)
        self.last_error = None
        recovered = self.consecutive_failures
        self.consecutive_failures = 0

        if recovered:
            logger.info("ingest_recovered", extra={"after_failures": recovered, "replayed": len(batch)})

        return PublishResult(
            ok=True,
            accepted=int(body.get("accepted", 0)),
            rejected=int(body.get("rejected", 0)),
            alerts_raised=int(body.get("alerts_raised", 0)),
            buffered=0,
        )

    def _buffer_batch(self, batch: list[dict[str, Any]], error: str) -> PublishResult:
        dropped = max(0, len(self._buffer) + len(batch) - self._buffer.maxlen)  # type: ignore[operator]
        self._buffer.extend(batch)
        self.consecutive_failures += 1
        self.last_error = error

        # Loud on the first failure, quiet after: a two-minute outage must not
        # produce four hundred identical error lines an operator has to scroll.
        if self.consecutive_failures == 1 or self.consecutive_failures % 30 == 0:
            logger.warning(
                "ingest_unreachable",
                extra={
                    "error": error,
                    "consecutive_failures": self.consecutive_failures,
                    "buffered": len(self._buffer),
                    "dropped_oldest": dropped,
                },
            )
        return PublishResult(
            ok=False, accepted=0, rejected=0, alerts_raised=0, buffered=len(self._buffer), error=error
        )

    async def heartbeat(self, cameras: list[dict[str, Any]]) -> bool:
        """Report camera liveness.  Failure here is logged, never raised."""
        if not cameras:
            return True
        try:
            client = await self.client()
            response = await client.post(
                f"{settings.ingest_url}/heartbeat", json={"cameras": cameras}, headers=self._auth()
            )
            return response.status_code < 400
        except (httpx.HTTPError, OSError) as exc:
            logger.warning("heartbeat_failed", extra={"error": str(exc)})
            return False

    async def fetch_config(self) -> dict[str, Any] | None:
        """Pull zones, cameras and calibrations from the core API.

        Returns None on failure so the caller can keep running on the config it
        already has — a config refresh that fails must not stop a pipeline that
        is otherwise working.
        """
        try:
            client = await self.client()
            response = await client.get(f"{settings.ingest_url}/config", headers=self._auth())
            response.raise_for_status()
        except (httpx.HTTPError, OSError) as exc:
            logger.warning("config_fetch_failed", extra={"error": str(exc)})
            return None
        return _safe_json(response) or None


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
