"""Demo mode's refusal, applied to every request (``DEMO_MODE=true``).

The contract is in ``backend/app/core/demo_mode.py``. This layer:

* answers an HTTP request whose method is not ``GET``, ``HEAD`` or ``OPTIONS``
  with 403 ``{"detail": {"code": "demo_read_only", "message": ...}}`` unless
  ``(method, scope["path"])`` is on the allow-list, before routing, before the
  body is read, before authentication and before any database session;
* refuses every websocket at the handshake;
* passes everything through, untouched, when ``DEMO_MODE`` is off, which costs
  one attribute read. ``DEMO_MODE`` is read on every request, not when the
  layer is built.

Placement (``backend/app/main.py``): inside ``TrustedHostMiddleware``, so a
forged ``Host`` gets its 400 first; outside ``RateLimitMiddleware``, so a
refusal costs no rate-limit bucket; inside the CORS, request-id and
security-header layers, so the 403 carries their headers and a dashboard on
another origin can read it. ``backend/tests/unit/middleware/
test_demo_mode_middleware.py`` pins the order.

Pure ASGI, not ``BaseHTTPMiddleware``: that class passes websockets through
unseen, and this layer has to refuse them.

The refusal carries ``Connection: close``, as the request-body limit's 413
does: the body is never read, so nothing more is read from a client that may
still be sending it. No refusal is logged.
"""

from __future__ import annotations

from typing import FrozenSet, Optional, Tuple

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocketClose

from backend.app.core.config import settings
from backend.app.core.demo_mode import REFUSAL_BODY, allowed_requests, is_refused

#: RFC 6455's "policy violation". Sent before the handshake is accepted, so the
#: server answers the handshake with HTTP 403 and no socket opens.
WS_CLOSE_POLICY_VIOLATION = 1008


class DemoModeMiddleware:
    """Refuse writes and websockets while ``settings.DEMO_MODE`` is true."""

    def __init__(
        self,
        app: ASGIApp,
        allowed: Optional[FrozenSet[Tuple[str, str]]] = None,
    ) -> None:
        self.app = app
        self.allowed = (
            allowed if allowed is not None else allowed_requests(settings.API_V1_STR)
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if settings.DEMO_MODE:
            kind = scope["type"]
            if kind == "http" and is_refused(
                scope["method"], scope["path"], self.allowed
            ):
                response = JSONResponse(
                    REFUSAL_BODY, status_code=403, headers={"Connection": "close"}
                )
                await response(scope, receive, send)
                return
            if kind == "websocket":
                await WebSocketClose(code=WS_CLOSE_POLICY_VIOLATION)(
                    scope, receive, send
                )
                return
        await self.app(scope, receive, send)
