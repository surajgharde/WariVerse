"""Publishing, buffering and the "kill the container" property.

The demo moment Section 16 builds to is killing this service live and watching
the temple keep running.  The other half of that promise is this: when the *core
API* is the thing that goes away, readings queue and replay rather than
evaporating — up to the point where they are too old to be worth having.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from app.config import settings
from app.models import ZoneObservation
from app.publisher import Publisher

T0 = datetime(2026, 7, 25, 5, 30, tzinfo=UTC)


def observation(code: str = "TC", count: int = 2400) -> ZoneObservation:
    return ZoneObservation(
        zone_id="11111111-1111-1111-1111-111111111111",
        zone_code=code,
        person_count=count,
        density=count / 1200.0,
        observed_at=T0,
        flow_dy=0.4,
        stagnation_index=0.2,
        confidence=0.9,
        camera_count=1,
        heat_cells=((0.5, 0.5, 3.2),),
    )


def transport(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_a_successful_publish_reports_what_the_api_accepted():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("x-ai-service-token") is not None
        body = request.read().decode()
        assert '"zone_code":"TC"' in body.replace(" ", "")
        return httpx.Response(202, json={"accepted": 1, "rejected": 0, "alerts_raised": 1})

    publisher = Publisher(client=transport(handler))
    result = await publisher.publish([observation()])

    assert result.ok
    assert result.accepted == 1
    assert result.alerts_raised == 1
    assert publisher.buffered == 0
    assert publisher.is_connected


async def test_heat_cells_never_leave_the_process():
    """They are the only field that describes *where* people are standing."""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read().decode()
        return httpx.Response(202, json={"accepted": 1})

    await Publisher(client=transport(handler)).publish([observation()])
    assert "heat_cells" not in captured["body"]
    assert "3.2" not in captured["body"]


async def test_a_connection_failure_buffers_instead_of_losing_the_reading():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("core-api is down")

    publisher = Publisher(client=transport(handler))
    result = await publisher.publish([observation()])

    assert not result.ok
    assert publisher.buffered == 1
    assert not publisher.is_connected


async def test_buffered_readings_replay_when_the_api_comes_back():
    state = {"up": False, "seen": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if not state["up"]:
            raise httpx.ConnectError("down")
        import json

        state["seen"] = len(json.loads(request.read())["readings"])
        return httpx.Response(202, json={"accepted": state["seen"]})

    publisher = Publisher(client=transport(handler))
    for _ in range(3):
        await publisher.publish([observation()])
    assert publisher.buffered == 3

    state["up"] = True
    result = await publisher.publish([observation()])

    assert result.ok
    assert state["seen"] == 4, "three buffered plus the current window"
    assert publisher.buffered == 0
    assert publisher.is_connected


async def test_a_server_error_is_retried_but_a_client_error_is_not():
    """A 5xx will fix itself; a 422 will not, and looping a malformed batch
    forever costs the buffer space every subsequent reading needs."""

    def failing(status: int):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, json={"error": {"code": "X"}})

        return handler

    retried = Publisher(client=transport(failing(503)))
    await retried.publish([observation()])
    assert retried.buffered == 1

    dropped = Publisher(client=transport(failing(422)))
    result = await dropped.publish([observation()])
    assert dropped.buffered == 0
    assert not result.ok
    assert result.rejected == 1


async def test_the_buffer_is_bounded_and_drops_the_oldest():
    """During a surge the newest reading is the one that matters."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    publisher = Publisher(client=transport(handler))
    publisher._buffer.clear()

    overflow = settings.buffer_max_readings + 50
    for index in range(overflow):
        await publisher.publish([observation(count=index)])

    assert publisher.buffered == settings.buffer_max_readings
    newest = publisher._buffer[-1]["person_count"]
    assert newest == overflow - 1, "the most recent reading survived"


async def test_config_fetch_returns_none_rather_than_raising():
    """A failed refresh must not stop a pipeline that is otherwise working."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    assert await Publisher(client=transport(handler)).fetch_config() is None


async def test_heartbeat_failure_is_never_fatal():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    ok = await Publisher(client=transport(handler)).heartbeat([{"camera_id": "x", "status": "online"}])
    assert ok is False


@pytest.mark.parametrize("body", ["not json", "", "[]"])
async def test_a_malformed_success_response_does_not_crash_the_loop(body: str):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, content=body)

    result = await Publisher(client=transport(handler)).publish([observation()])
    assert result.ok
    assert result.accepted == 0
