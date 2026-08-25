"""Redis pub/sub event bus behind the WebSocket feed.

Section 4/M2 step 6 requires every zone aggregate to be published as a live
event.  With N API replicas, an ingest that lands on replica 1 must still reach
an operator whose socket is held by replica 3 — so the fan-out goes through
Redis rather than through an in-process list of sockets.

Publishing is **best effort and never fatal**.  Losing a live event costs an
operator two seconds of freshness; refusing an ingest because Redis is down
would throw away the reading itself.  Section 1: the system degrades, it does
not fail closed.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import date, datetime
from typing import Any
from uuid import UUID

from app.core.logging import get_logger, get_trace_id
from app.core.redis_client import aw, redis
from app.core.security import now_utc

logger = get_logger(__name__)

#: One channel, typed payloads.  A client subscribes once and filters by
#: `type`, which keeps reconnection logic in the browser to a single socket.
CHANNEL = "wariverse:events"

# Event type names.  Clients switch on these — keep them stable.
DENSITY_UPDATED = "density.updated"
ALERT_RAISED = "alert.raised"
ALERT_UPDATED = "alert.updated"
CAMERA_STATUS_CHANGED = "camera.status_changed"

# Phase 5 — incidents.  The payloads carry no reporter: the command centre needs
# to know an incident exists, where, how bad and how long it has had, not who
# pressed the button.  See `incident_service.event_payload`.
INCIDENT_RAISED = "incident.raised"
INCIDENT_UPDATED = "incident.updated"
INCIDENT_SLA_BREACHED = "incident.sla_breached"
INCIDENT_RAISED = "incident.raised"
INCIDENT_UPDATED = "incident.updated"
INCIDENT_DISPATCHED = "incident.dispatched"
#: Separate from `incident.updated` on purpose.  An SLA breach is the one
#: incident event that means nobody acted, so a console must be able to treat it
#: differently from the twenty status changes that mean somebody did.
INCIDENT_SLA_BREACHED = "incident.sla_breached"


def _encode(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    raise TypeError(f"Not JSON serialisable: {type(value).__name__}")


def envelope(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": event_type,
        "at": now_utc().isoformat(),
        "trace_id": get_trace_id(),
        "data": payload,
    }


async def publish(event_type: str, payload: dict[str, Any]) -> bool:
    """Fan one event out to every connected client.  Returns False if it did
    not get out — callers log that and carry on, they do not raise."""
    body = json.dumps(envelope(event_type, payload), default=_encode, ensure_ascii=False)
    try:
        await aw(redis.publish(CHANNEL, body))
        return True
    except Exception as exc:
        logger.warning("event_publish_failed", extra={"event": event_type, "error": str(exc)})
        return False


async def publish_many(events: list[tuple[str, dict[str, Any]]]) -> int:
    """Publish a batch in one pipeline — an ingest covers many zones at once."""
    if not events:
        return 0
    try:
        pipe = redis.pipeline()
        for event_type, payload in events:
            body = json.dumps(envelope(event_type, payload), default=_encode, ensure_ascii=False)
            pipe.publish(CHANNEL, body)
        await aw(pipe.execute())
        return len(events)
    except Exception as exc:
        logger.warning("event_publish_batch_failed", extra={"count": len(events), "error": str(exc)})
        return 0


async def subscribe() -> AsyncIterator[dict[str, Any]]:
    """Yield decoded events until the caller stops iterating.

    Raises if Redis is unreachable — a WebSocket that cannot receive events is
    better refused at connect time than left open and silent, because a silent
    socket looks to the operator exactly like a quiet temple.
    """
    pubsub = redis.pubsub(ignore_subscribe_messages=True)
    await pubsub.subscribe(CHANNEL)
    try:
        async for message in pubsub.listen():
            if message is None or message.get("type") != "message":
                continue
            try:
                yield json.loads(message["data"])
            except (ValueError, KeyError, TypeError):
                logger.warning("event_decode_failed")
    finally:
        await pubsub.unsubscribe(CHANNEL)
        # redis-py types `aclose` from the sync client, where it is untyped.
        await pubsub.aclose()  # type: ignore[no-untyped-call]
