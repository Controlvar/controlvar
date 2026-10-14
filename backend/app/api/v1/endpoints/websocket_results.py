"""
WebSocket endpoint for EP-058: Real-time WebSocket Streaming Results.

Provides a WebSocket endpoint for streaming live experiment results to clients,
along with HTTP companion endpoints for subscriber counts and active experiments.

WebSocket protocol:
  - Client connects to ws://host/api/v1/ws/experiments/{experiment_id}/results
    and authenticates with, in order of preference:
      1. the ``Sec-WebSocket-Protocol`` header ``experimently.bearer, <token>``
         (what the dashboard sends: ``new WebSocket(url, ["experimently.bearer",
         token])``; browsers cannot set other headers on a handshake and this
         keeps the token out of URL/access logs).  The server selects
         ``experimently.bearer`` as the accepted subprotocol.
      2. an ``Authorization: Bearer`` header (non-browser clients).
      3. ``?token=<access token>`` — supported for compatibility only; it is
         written to every access log on the path, prefer 1 or 2.
    The token is validated exactly like an HTTP request (local JWT or
    Cognito); the dev-auth bypass applies too.  An invalid/missing token
    closes the socket with code 4401 after the handshake.
  - Server sends an initial snapshot on connection
  - Server sends a fresh snapshot every RESULTS_STREAM_INTERVAL_SECONDS seconds
    (a Settings field, default 30), whatever the client sends in between
  - Client can send {"action": "ping"} -> server replies {"event": "pong"}
  - Client can send {"action": "refresh"} -> server immediately sends fresh snapshot
  - On disconnect: connection is cleaned up automatically
"""

import asyncio
import json
import logging
from typing import Optional

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
)
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from backend.app.api import deps
from backend.app.core.config import settings
from backend.app.services.results_streaming_service import ResultsStreamingService
from backend.app.services.websocket_manager import ConnectionManager

logger = logging.getLogger(__name__)

# Application-defined close code for a failed WebSocket authentication
# (4000-4999 is the private-use range of RFC 6455).
WS_CLOSE_UNAUTHORIZED = 4401

router = APIRouter()

# Module-level singleton so all requests share the same ConnectionManager state.
manager = ConnectionManager()


WS_AUTH_SUBPROTOCOL = "experimently.bearer"


def _token_from_subprotocol(websocket: WebSocket) -> Optional[str]:
    """
    ``Sec-WebSocket-Protocol: experimently.bearer, <token>`` -> ``<token>``.

    The header is a comma-separated list of protocol names; the token follows
    the marker entry.  JWT characters (``A-Za-z0-9-_.``) are all valid
    protocol-name characters, so no encoding is needed.
    """
    raw = websocket.headers.get("sec-websocket-protocol", "")
    if not raw:
        return None
    entries = [entry.strip() for entry in raw.split(",") if entry.strip()]
    for index, entry in enumerate(entries):
        if entry == WS_AUTH_SUBPROTOCOL and index + 1 < len(entries):
            return entries[index + 1]
    return None


def _extract_ws_token(websocket: WebSocket, token: Optional[str]) -> Optional[str]:
    """Subprotocol first, then ``Authorization: Bearer``, then ``?token=``."""
    from_subprotocol = _token_from_subprotocol(websocket)
    if from_subprotocol:
        return from_subprotocol
    authorization = websocket.headers.get("authorization", "")
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    if token:
        return token
    return None


def _accepted_subprotocol(websocket: WebSocket) -> Optional[str]:
    """Echo ``experimently.bearer`` when the client offered it (RFC 6455 requires a match)."""
    return (
        WS_AUTH_SUBPROTOCOL if _token_from_subprotocol(websocket) is not None else None
    )


def _authenticate_websocket(
    websocket: WebSocket, token: Optional[str], db: Session
) -> bool:
    """
    Run the HTTP authentication chain for a WebSocket handshake.

    Returns True when the caller is an active user.  With the dev-auth bypass
    on, every connection is accepted without touching the database (the
    bypass user is synthetic, exactly as for HTTP requests).
    """
    if deps.dev_auth_bypass_active():
        return True
    bearer = _extract_ws_token(websocket, token)
    if not bearer:
        return False
    try:
        user = deps.get_current_user(token=bearer, db=db)
        deps.get_current_active_user(user)
        return True
    except HTTPException as exc:
        logger.info("WebSocket auth rejected (%s): %s", exc.status_code, exc.detail)
        return False
    except Exception as exc:  # DB down, etc. — fail closed
        logger.warning("WebSocket auth error: %s", exc)
        return False


@router.websocket("/ws/experiments/{experiment_id}/results")
async def stream_results(
    websocket: WebSocket,
    experiment_id: str,
    token: Optional[str] = Query(
        None, description="Access token (alternative to the bearer header)"
    ),
    db: Session = Depends(deps.get_db),
) -> None:
    """
    Stream live experiment results over WebSocket.

    Authenticates via ``?token=`` or a bearer header (close code 4401 when
    invalid), then sends an initial snapshot, then a fresh one every
    ``settings.RESULTS_STREAM_INTERVAL_SECONDS`` seconds on a schedule that
    client messages do not move. Also responds to client messages:
      {"action": "ping"} -> {"event": "pong"}
      {"action": "refresh"} -> immediate fresh snapshot
    """
    from backend.app.db.session import SessionLocal

    # Sync SQLAlchemy work runs in the threadpool, like a sync HTTP dependency.
    subprotocol = _accepted_subprotocol(websocket)
    if not await run_in_threadpool(_authenticate_websocket, websocket, token, db):
        # Accept first so the client receives the application close code
        # (closing before accept surfaces as a bare HTTP 403 handshake error).
        await websocket.accept(subprotocol=subprotocol)
        await websocket.close(code=WS_CLOSE_UNAUTHORIZED, reason="Unauthorized")
        return
    # The session was only needed for authentication; release its connection
    # now rather than holding it for the lifetime of the socket.
    db.close()

    await manager.connect(websocket, experiment_id, subprotocol=subprotocol)
    streaming_service = ResultsStreamingService(SessionLocal)
    # The one outstanding read of the next client message, if any.
    receive_task: Optional["asyncio.Future[str]"] = None

    try:
        # Send initial snapshot immediately on connection
        snapshot = await streaming_service.get_live_snapshot(experiment_id)
        await websocket.send_text(json.dumps(snapshot))

        # Main loop: wait for a client message, but only until the next
        # snapshot is due. The deadline moves only when a periodic snapshot
        # is sent, so a client message (a ping, say) never postpones one
        # (#1193). A read that is still waiting when a snapshot is due is
        # kept, not cancelled, and waited on again: cancelling a read can
        # drop a message that arrives in the same moment.
        interval = float(settings.RESULTS_STREAM_INTERVAL_SECONDS)
        loop = asyncio.get_running_loop()
        next_snapshot_at = loop.time() + interval
        while True:
            remaining = next_snapshot_at - loop.time()
            if remaining <= 0:
                snapshot = await streaming_service.get_live_snapshot(experiment_id)
                await websocket.send_text(json.dumps(snapshot))
                next_snapshot_at = loop.time() + interval
                continue
            if receive_task is None:
                receive_task = asyncio.ensure_future(websocket.receive_text())
            done, _ = await asyncio.wait({receive_task}, timeout=remaining)
            if not done:
                # The next snapshot is due: the top of the loop sends it.
                continue
            finished, receive_task = receive_task, None
            data = finished.result()  # raises WebSocketDisconnect on close
            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                msg = {}

            action = msg.get("action")
            if action == "ping":
                await websocket.send_text(json.dumps({"event": "pong"}))
            elif action == "refresh":
                snapshot = await streaming_service.get_live_snapshot(experiment_id)
                await websocket.send_text(json.dumps(snapshot))
            else:
                logger.debug(
                    "Unknown WebSocket action '%s' for experiment %s",
                    action,
                    experiment_id,
                )

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected for experiment %s", experiment_id)
    except Exception as exc:
        logger.exception(
            "Unexpected error in WebSocket handler for experiment %s: %s",
            experiment_id,
            exc,
        )
    finally:
        if receive_task is not None:
            if not receive_task.done():
                receive_task.cancel()
            elif not receive_task.cancelled():
                # Read the outcome, so asyncio does not log it as unretrieved.
                receive_task.exception()
        await manager.disconnect(websocket, experiment_id)


@router.get("/ws/experiments/{experiment_id}/results/subscribers")
async def get_subscriber_count(experiment_id: str) -> dict:
    """
    Return the number of active WebSocket subscribers for an experiment.

    No authentication required — informational endpoint.
    """
    return {
        "experiment_id": experiment_id,
        "subscriber_count": manager.subscriber_count(experiment_id),
    }


@router.get("/ws/active-experiments")
async def get_active_experiments() -> dict:
    """
    Return the list of experiment IDs that currently have active WebSocket subscribers.

    No authentication required — informational endpoint.
    """
    return {"active_experiments": manager.active_experiments}
