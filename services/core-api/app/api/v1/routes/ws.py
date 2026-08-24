"""Live event socket for the command centre (Section 4/M2 step 6).

    ws://host/api/v1/ws/crowd?token=<access token>

Why the token is in the query string: browsers cannot set an `Authorization`
header on a `WebSocket`.  The alternatives are a cookie (which brings CSRF into
a system that currently has none) or a short-lived ticket endpoint.  A query
token is the honest middle: it is the same 15-minute access token, and Section 8
requires TLS in front of this, so it is never on the wire in clear.

The residual risk is real and worth stating: a query token can land in a proxy
access log.  `TraceMiddleware` logs the route template rather than the URL, so
it does not leak from this process — but whatever sits in front of it must be
configured not to log query strings for `/ws/`, and the Phase 10 Nginx config
is where that belongs.

The socket is read-mostly.  A client may send `{"type":"ping"}` and gets a pong;
anything else is ignored.  Commands go over HTTP where they can be audited —
an acknowledgement that arrived over a socket with no request id is an
acknowledgement nobody can reconstruct six months later.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status
from prometheus_client import Counter, Gauge
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import events
from app.core.db import SessionFactory
from app.core.errors import AppError
from app.core.logging import get_logger
from app.core.permissions import Permission, has_permission
from app.core.security import decode_token, now_utc
from app.models import User
from app.services import crowd_service, token_store

logger = get_logger(__name__)

router = APIRouter(tags=["ws"])

#: Nothing arrives for this long and we send a heartbeat.  A silent socket and
#: a dead socket look identical to a browser behind a proxy that idles out at
#: sixty seconds, and an operator staring at a frozen map is the whole problem.
HEARTBEAT_SECONDS = 20

WS_CONNECTED = Gauge("wariverse_ws_connections", "Open command-centre sockets")
WS_DROPPED = Counter("wariverse_ws_events_dropped_total", "Events discarded because a client fell behind")


async def _authenticate(token: str) -> User:
    claims = decode_token(token, expected_type="access")
    if await token_store.is_access_denied(claims.jti):
        raise AppError("TOKEN_INVALID", details={"reason": "session ended"})

    async with SessionFactory() as session:
        user = await session.get(User, uuid.UUID(claims.subject))
        if user is None or not user.is_active:
            raise AppError("TOKEN_INVALID", details={"reason": "account not available"})
        if user.role != str(claims.role):
            raise AppError("TOKEN_INVALID", details={"reason": "role changed, sign in again"})
        session.expunge(user)
    return user


@router.websocket("/ws/crowd")
async def crowd_socket(websocket: WebSocket, token: str = Query(...)) -> None:
    """Density updates, alerts and camera status, as they happen."""
    try:
        user = await _authenticate(token)
    except (AppError, ValueError):
        # 1008 policy violation, not 401: the handshake is already upgraded.
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="authentication failed")
        return

    if not has_permission(user.role, Permission.CROWD_VIEW_DETAIL):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="crowd:view_detail required")
        return

    await websocket.accept()
    WS_CONNECTED.inc()
    logger.info("ws_connected", extra={"user_id": str(user.id), "role": user.role})

    try:
        await websocket.send_text(json.dumps(await _hello(), ensure_ascii=False, default=str))
    except (WebSocketDisconnect, RuntimeError):
        return

    outbox: asyncio.Queue[str] = asyncio.Queue(maxsize=500)
    stopping = asyncio.Event()

    async def pump() -> None:
        """Redis -> queue.  Drops the oldest event rather than blocking.

        A slow client must not be able to stall the fan-out for everyone else,
        and during a surge the *newest* density reading is the one that matters.
        """
        try:
            async for event in events.subscribe():
                if stopping.is_set():
                    return
                body = json.dumps(event, ensure_ascii=False, default=str)
                if outbox.full():
                    with contextlib.suppress(asyncio.QueueEmpty):
                        outbox.get_nowait()
                    WS_DROPPED.inc()
                    logger.warning("ws_backpressure", extra={"user_id": str(user.id)})
                await outbox.put(body)
        except Exception as exc:
            logger.warning("ws_pump_failed", extra={"error": str(exc)})
            stopping.set()

    async def receive() -> None:
        """Drain inbound frames so the socket's close is noticed promptly."""
        try:
            while not stopping.is_set():
                raw = await websocket.receive_text()
                with contextlib.suppress(ValueError):
                    if json.loads(raw).get("type") == "ping":
                        await outbox.put(json.dumps({"type": "pong", "at": now_utc().isoformat()}))
        except (WebSocketDisconnect, RuntimeError):
            stopping.set()

    pump_task = asyncio.create_task(pump(), name="ws-pump")
    receive_task = asyncio.create_task(receive(), name="ws-receive")

    try:
        while not stopping.is_set():
            try:
                body = await asyncio.wait_for(outbox.get(), timeout=HEARTBEAT_SECONDS)
            except TimeoutError:
                body = json.dumps({"type": "heartbeat", "at": now_utc().isoformat()})
            await websocket.send_text(body)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        stopping.set()
        for task in (pump_task, receive_task):
            task.cancel()
        await asyncio.gather(pump_task, receive_task, return_exceptions=True)
        WS_CONNECTED.dec()
        logger.info("ws_disconnected", extra={"user_id": str(user.id)})


async def _hello() -> dict[str, Any]:
    """First frame: the current state of every zone.

    Without this an operator who reconnects sees an empty map until the next
    ten-second window lands.  Ten seconds of blank map during a surge is not
    acceptable, so the socket opens with a full picture and then streams deltas.
    """
    async with SessionFactory() as session:
        snapshots = await _safe_latest(session)

    return {
        "type": "hello",
        "at": now_utc().isoformat(),
        "heartbeat_seconds": HEARTBEAT_SECONDS,
        "data": {"zones": [s.to_json() for s in snapshots]},
    }


async def _safe_latest(session: AsyncSession) -> list[crowd_service.ZoneSnapshot]:
    try:
        return await crowd_service.latest(session)
    except Exception as exc:
        # An empty opening frame is survivable; a refused socket is not.
        logger.warning("ws_hello_failed", extra={"error": str(exc)})
        return []
