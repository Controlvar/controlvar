"""The demo-mode layer: where it sits, what it refuses, and the websocket.

* Its place in ``backend/app/main.py``: directly inside the trusted-host check
  and directly outside the rate limiter, so it is also inside CORS, the
  request id and the security headers (the 403 carries their headers; the
  smoke test ``test_demo_mode_routes.py`` checks them on the wire).
* On a stub application: a refused request never reaches the application and
  its body is never read; a request that passes is untouched; ``DEMO_MODE``
  is read on every request, not when the layer is built.
* Websockets: with demo mode on, a real client's handshake to the real
  application under a real uvicorn is answered 403 and no socket opens; with
  it off, the same handshake completes.
"""

from __future__ import annotations

import copy
import logging
import socket
import threading
import time
from typing import Any, Dict, List

import pytest
import uvicorn
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from starlette.middleware import Middleware
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import connect as ws_connect

from backend.app.core.config import settings
from backend.app.core.demo_mode import (
    ALLOWED_POST_PATHS,
    DEMO_READ_ONLY_CODE,
    REFUSAL_BODY,
    allowed_requests,
)
from backend.app.main import app
from backend.app.middleware import rate_limiter
from backend.app.middleware.demo_mode_middleware import DemoModeMiddleware
from backend.app.middleware.rate_limiter import RateLimitMiddleware
from backend.app.middleware.request_id_middleware import RequestIDMiddleware
from backend.app.middleware.security_middleware import SecurityHeadersMiddleware
from backend.app.middleware.trusted_host_middleware import TrustedHostMiddleware

pytestmark = [pytest.mark.unit]

ALLOWED = allowed_requests("/api/v1")
WS_PATH = "/api/v1/ws/experiments/00000000-0000-4000-8000-000000000000/results"


@pytest.fixture
def demo_on(monkeypatch):
    monkeypatch.setattr(settings, "DEMO_MODE", True)


# --- where it sits ---------------------------------------------------------------


def test_the_layer_sits_between_the_trusted_host_check_and_the_rate_limiter():
    stack = [m.cls for m in app.user_middleware]
    names = [cls.__name__ for cls in stack]
    print("middleware, outermost first:", names)
    assert stack.count(DemoModeMiddleware) == 1, names
    at = stack.index(DemoModeMiddleware)
    assert stack[at - 1] is TrustedHostMiddleware, names
    assert stack[at + 1] is RateLimitMiddleware, names
    for outer in (CORSMiddleware, RequestIDMiddleware, SecurityHeadersMiddleware):
        assert stack.index(outer) < at, f"{outer.__name__} must be outside: {names}"


def _hardened_client(monkeypatch) -> TestClient:
    """``main.app`` with a host allow-list and the rate limiter counting in process.

    A shallow copy with its own stack, as in test_confirm_rate_limit.py, so the
    application other tests use is untouched.
    """
    middleware: List[Middleware] = []
    for layer in app.user_middleware:
        kwargs = dict(layer.kwargs)
        if layer.cls is TrustedHostMiddleware:
            kwargs["allowed_hosts"] = ["demo.example.com"]
        if layer.cls is RateLimitMiddleware:
            kwargs["enabled"] = True
        middleware.append(Middleware(layer.cls, *layer.args, **kwargs))
    monkeypatch.setattr(
        rate_limiter, "RedisRateLimiter", rate_limiter.SlidingWindowRateLimiter
    )
    hardened = copy.copy(app)
    hardened.user_middleware = middleware
    hardened.middleware_stack = None
    return TestClient(
        hardened, base_url="http://demo.example.com", raise_server_exceptions=False
    )


def test_a_forged_host_is_refused_before_demo_mode_answers(demo_on, monkeypatch):
    client = _hardened_client(monkeypatch)
    response = client.post(
        "/api/v1/experiments/", json={}, headers={"Host": "evil.example.com"}
    )
    assert (response.status_code, response.json()) == (
        400,
        {"detail": "Invalid host header"},
    )


def test_a_refusal_costs_no_rate_limit_bucket(demo_on, monkeypatch):
    """Sign-up is limited to a few requests a minute; its refusals never reach 429."""
    client = _hardened_client(monkeypatch)
    statuses = {
        client.post("/api/v1/auth/signup", json={}).status_code for _ in range(25)
    }
    assert statuses == {403}, statuses
    # The control: the limiter is counting, and the allowed sign-in reaches it.
    limited = [
        client.post("/api/v1/auth/login", json={}).status_code for _ in range(25)
    ]
    assert 429 in limited, limited


def test_the_allow_list_is_eight_posts_under_the_api_prefix():
    assert len(ALLOWED_POST_PATHS) == 8
    assert ALLOWED == {("POST", f"/api/v1/{path}") for path in ALLOWED_POST_PATHS}
    assert allowed_requests("/api/v1/") == ALLOWED


# --- on a stub application ---------------------------------------------------------


class _Recorder:
    """A stub application and the ASGI channel around it, recording every call."""

    def __init__(self) -> None:
        self.app_calls: List[Dict[str, Any]] = []
        self.received = 0
        self.sent: List[Dict[str, Any]] = []

    async def app(self, scope, receive, send) -> None:
        self.app_calls.append(scope)
        if scope["type"] == "http":
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

    async def receive(self) -> Dict[str, Any]:
        self.received += 1
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(self, message: Dict[str, Any]) -> None:
        self.sent.append(message)

    def status(self) -> int:
        return next(m["status"] for m in self.sent if m["type"].endswith(".start"))

    def headers(self) -> Dict[bytes, bytes]:
        start = next(m for m in self.sent if m["type"] == "http.response.start")
        return dict(start["headers"])

    def body(self) -> bytes:
        return b"".join(m.get("body", b"") for m in self.sent if "body" in m["type"])


def _http(method: str, path: str, headers=()) -> Dict[str, Any]:
    return {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers],
    }


async def _run(scope: Dict[str, Any]) -> _Recorder:
    recorder = _Recorder()
    layer = DemoModeMiddleware(recorder.app, allowed=ALLOWED)
    await layer(scope, recorder.receive, recorder.send)
    return recorder


@pytest.mark.asyncio
async def test_a_refusal_writes_no_log_line(demo_on, caplog):
    """A flood of refused requests cannot become a disk filler."""
    caplog.set_level(logging.DEBUG)
    for _ in range(3):
        await _run(_http("POST", "/api/v1/experiments/"))
    assert caplog.records == []


@pytest.mark.asyncio
async def test_a_refused_write_never_reaches_the_application(demo_on):
    import json

    recorder = await _run(_http("POST", "/api/v1/experiments/"))
    assert recorder.app_calls == []
    assert recorder.received == 0, "the body was read before the refusal"
    assert recorder.status() == 403
    assert json.loads(recorder.body()) == REFUSAL_BODY
    assert REFUSAL_BODY["detail"]["code"] == DEMO_READ_ONLY_CODE
    headers = recorder.headers()
    assert headers[b"content-type"] == b"application/json"
    assert headers[b"connection"] == b"close"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "PURGE", "post"])
async def test_every_method_but_the_safe_three_is_refused(demo_on, method):
    recorder = await _run(_http(method, "/api/v1/feature-flags/x"))
    assert recorder.app_calls == [] and recorder.status() == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
async def test_the_safe_methods_pass_on_any_path(demo_on, method):
    recorder = await _run(_http(method, "/api/v1/experiments/"))
    assert len(recorder.app_calls) == 1 and recorder.status() == 204


@pytest.mark.asyncio
@pytest.mark.parametrize("path", sorted(path for _, path in ALLOWED))
async def test_each_allowed_post_passes(demo_on, path):
    recorder = await _run(_http("POST", path))
    assert len(recorder.app_calls) == 1 and recorder.status() == 204


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path,headers",
    [
        ("POST", "/api/v1/auth/login/", ()),
        ("POST", "//api/v1/auth/login", ()),
        ("POST", "/API/V1/auth/login", ()),
        ("POST", "/api/v1/Auth/Login", ()),
        ("POST", "/prefix/api/v1/auth/login", ()),
        ("PUT", "/api/v1/auth/login", ()),
        ("DELETE", "/api/v1/tracking/assign", ()),
        ("POST", "/api/v1/tracking", ()),
        ("POST", "/api/v1/tracking/zz-demo-plant", ()),
        ("POST", "/api/v1/experiments/", (("X-HTTP-Method-Override", "GET"),)),
        ("POST", "/api/v1/experiments/", (("X-Method-Override", "GET"),)),
    ],
)
async def test_a_variant_of_an_allowed_request_is_refused(
    demo_on, method, path, headers
):
    recorder = await _run(_http(method, path, headers))
    assert recorder.app_calls == [] and recorder.status() == 403


@pytest.mark.asyncio
async def test_off_every_request_passes_untouched(monkeypatch):
    monkeypatch.setattr(settings, "DEMO_MODE", False)
    recorder = await _run(_http("DELETE", "/api/v1/experiments/x"))
    assert len(recorder.app_calls) == 1 and recorder.status() == 204


@pytest.mark.asyncio
async def test_the_switch_is_read_on_every_request(monkeypatch):
    """One layer, built while off: turning the switch on refuses the next write."""
    recorder = _Recorder()
    layer = DemoModeMiddleware(recorder.app, allowed=ALLOWED)
    monkeypatch.setattr(settings, "DEMO_MODE", False)
    await layer(_http("POST", "/api/v1/experiments/"), recorder.receive, recorder.send)
    monkeypatch.setattr(settings, "DEMO_MODE", True)
    await layer(_http("POST", "/api/v1/experiments/"), recorder.receive, recorder.send)
    statuses = [
        m["status"] for m in recorder.sent if m["type"] == "http.response.start"
    ]
    assert statuses == [204, 403]
    assert len(recorder.app_calls) == 1


@pytest.mark.asyncio
async def test_a_websocket_never_reaches_the_application(demo_on):
    recorder = await _run({"type": "websocket", "path": WS_PATH, "headers": []})
    assert recorder.app_calls == []
    assert recorder.sent == [{"type": "websocket.close", "code": 1008, "reason": ""}]


@pytest.mark.asyncio
async def test_lifespan_passes(demo_on):
    recorder = await _run({"type": "lifespan"})
    assert len(recorder.app_calls) == 1


# --- a real websocket ---------------------------------------------------------------


def _serve():
    """The real application under a real uvicorn, in a thread, on a free port."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    config = uvicorn.Config(app, log_config=None, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]})
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise AssertionError("uvicorn did not start")
        time.sleep(0.05)
    return server, thread, sock.getsockname()[1]


UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


@pytest.fixture
def server():
    # uvicorn.Config sets these loggers' levels for the whole process; a later
    # test that reads uvicorn's access lines must find them as they were.
    saved = {
        name: (logging.getLogger(name).level, logging.getLogger(name).disabled)
        for name in UVICORN_LOGGERS
    }
    running, thread, port = _serve()
    try:
        yield f"ws://127.0.0.1:{port}{WS_PATH}"
    finally:
        running.should_exit = True
        thread.join(timeout=20)
        for name, (level, disabled) in saved.items():
            logging.getLogger(name).setLevel(level)
            logging.getLogger(name).disabled = disabled


def test_in_demo_mode_a_real_websocket_handshake_is_refused(server, monkeypatch):
    monkeypatch.setattr(settings, "DEMO_MODE", True)
    with pytest.raises(InvalidStatus) as refused:
        with ws_connect(server, open_timeout=10):
            pass
    assert refused.value.response.status_code == 403


def test_with_demo_mode_off_the_same_handshake_completes(server, monkeypatch):
    """The control: the route is live, and the socket opens before it closes 4401."""
    monkeypatch.setattr(settings, "DEMO_MODE", False)
    with ws_connect(server, open_timeout=10) as ws:
        with pytest.raises(ConnectionClosed) as closed:
            ws.recv(timeout=10)
    assert closed.value.rcvd is not None and closed.value.rcvd.code == 4401
