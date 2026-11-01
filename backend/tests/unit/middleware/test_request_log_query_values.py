"""The values of some query parameters are on no log line the API writes for a request.

Every value on the OIDC callback (``/api/v1/auth/sso/oidc/{provider}/callback``),
and on any route the value of a parameter named in
``backend/app/core/query_redaction.py`` (``code``, ``state``, ``token``,
``api_key``, ``password`` and the others there), is written as
``[redacted]``; the parameter names stay. One test per place a
request's query reaches a log:

* uvicorn's access line and its WebSocket handshake line, from a real uvicorn
  server, rendered by ``configure_logging`` -- what ``backend/app/main.py``
  installs;
* ``LoggingMiddleware``'s "Request started" line;
* ``ErrorMiddleware``'s error details.

``backend/tests/integration/api/test_access_log_query_values.py`` runs the
API itself under uvicorn and reads everything it wrote.
"""

from __future__ import annotations

import io
import json
import logging
import socket
import threading
import time
from unittest.mock import MagicMock, patch

import httpx
import pytest
import structlog
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route, WebSocketRoute
from websockets.sync.client import connect as ws_connect

from backend.app.core.logger import configure_logging
from backend.app.middleware.error_middleware import ErrorMiddleware
from backend.app.middleware.logging_middleware import LoggingMiddleware

pytestmark = [pytest.mark.unit, pytest.mark.regression]

CALLBACK = "/api/v1/auth/sso/oidc/okta/callback"
# Short, with a dot: values the pattern masking in backend/app/utils/masking.py
# leaves as they are, so only the rule under test can remove them.
CODE = "c0de.v4l"
STATE = "st4te.v4l"
TOKEN = "t0ken.v4l"
ID_TOKEN = "idt0k.v4l"
CLIENT_SECRET = "cl1ent.v4l"
ISS = "iss.v4l"
API_KEY = "ap1key.v4l"
PASSWORD = "p4ssw.v4l"
# An attribute value inside the URL-encoded JSON an SDK sends as ``context``.
CONTEXT_VALUE = "c0ntext.v4l"
CONTEXT = f"%7B%22plan%22%3A%22{CONTEXT_VALUE}%22%7D"
VALUES = (
    CODE,
    STATE,
    TOKEN,
    ID_TOKEN,
    CLIENT_SECRET,
    ISS,
    API_KEY,
    PASSWORD,
    CONTEXT_VALUE,
)
REDACTED = "[redacted]"


@pytest.fixture
def rendered_log():
    """``configure_logging(json_logs=True)`` into a buffer, restored afterwards."""
    root = logging.root
    handlers, root_level = list(root.handlers), root.level
    saved = structlog.get_config()
    buffer = io.StringIO()
    # Twice, as a process that configures logging again would: one line each.
    configure_logging(log_level="INFO", json_logs=True, stream=io.StringIO())
    configure_logging(log_level="INFO", json_logs=True, stream=buffer)
    try:
        yield buffer
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in handlers:
            root.addHandler(handler)
        root.setLevel(root_level)
        structlog.configure(**saved)


async def _ok(request):
    return PlainTextResponse("ok")


async def _ws(websocket):
    await websocket.accept()
    await websocket.close()


def _serve(app):
    """A real uvicorn (h11) on a socket bound here, in a thread; the log as
    ``configure_logging`` left it (no ``log_config`` of uvicorn's own)."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    config = uvicorn.Config(
        app, log_config=None, log_level="info", lifespan="off", http="h11"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]})
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise AssertionError("uvicorn did not start")
        time.sleep(0.05)
    return server, thread, sock.getsockname()[1]


def test_uvicorns_access_and_websocket_lines_carry_names_not_values(rendered_log):
    app = Starlette(
        routes=[WebSocketRoute("/ws", _ws), Route("/{path:path}", _ok)],
    )
    server, thread, port = _serve(app)
    try:
        base = f"http://127.0.0.1:{port}"
        httpx.get(f"{base}{CALLBACK}?code={CODE}&state={STATE}&iss={ISS}", timeout=10)
        httpx.get(
            f"{base}/api/v1/experiments?Token={TOKEN}&id_token={ID_TOKEN}"
            f"&client_secret={CLIENT_SECRET}&API_KEY={API_KEY}&password={PASSWORD}"
            "&page=2",
            timeout=10,
        )
        # What an SDK sends to evaluate a flag (#1552).
        httpx.get(
            f"{base}/api/v1/feature-flags/evaluate/k?user_id=u&context={CONTEXT}",
            timeout=10,
        )
        with ws_connect(f"ws://127.0.0.1:{port}/ws?token={TOKEN}&page=3") as ws:
            ws.close()
    finally:
        server.should_exit = True
        thread.join(timeout=20)

    # The server's lines only: this test's own HTTP client logs the URLs it
    # requested to the same handler.
    lines = [json.loads(line) for line in rendered_log.getvalue().splitlines()]
    log = "\n".join(
        line["event"] for line in lines if line["logger"].startswith("uvicorn")
    )
    for value in VALUES:
        assert value not in log, f"{value} was logged:\n{log}"
    assert f"{CALLBACK}?code={REDACTED}&state={REDACTED}&iss={REDACTED}" in log
    assert (
        f"/api/v1/experiments?Token={REDACTED}&id_token={REDACTED}"
        f"&client_secret={REDACTED}&API_KEY={REDACTED}&password={REDACTED}&page=2"
    ) in log
    assert f"/api/v1/feature-flags/evaluate/k?user_id=u&context={REDACTED}" in log
    assert f'"WebSocket /ws?token={REDACTED}&page=3" [accepted]' in log


def test_the_request_logging_middleware_logs_names_not_values():
    app = FastAPI()
    app.add_middleware(LoggingMiddleware)

    @app.get("/{path:path}")
    def anything(path: str):
        return {}

    ctx = MagicMock()
    with patch("backend.app.middleware.logging_middleware.LogContext") as log_context:
        log_context.return_value.__enter__.return_value = ctx
        client = TestClient(app)
        client.get(f"{CALLBACK}?code={CODE}&state={STATE}&iss={ISS}")
        client.get(f"/api/v1/x?CODE={CODE}&id_token={ID_TOKEN}&page=2")

    started = [
        call.kwargs["extra"]["query_params"]
        for call in ctx.info.call_args_list
        if call.args and call.args[0] == "Request started"
    ]
    assert len(started) == 2, ctx.info.call_args_list
    for value in VALUES:
        assert value not in repr(started)
    assert started[0] == {"code": REDACTED, "state": REDACTED, "iss": REDACTED}
    assert started[1] == {"CODE": REDACTED, "id_token": REDACTED, "page": "2"}


def test_the_error_middleware_logs_names_not_values(caplog):
    app = FastAPI()
    app.add_middleware(ErrorMiddleware, aws_client=None)

    @app.get("/{path:path}")
    def broken(path: str):
        raise RuntimeError("probe")

    client = TestClient(app, raise_server_exceptions=False)
    with caplog.at_level(
        logging.ERROR, logger="backend.app.middleware.error_middleware"
    ):
        client.get(f"{CALLBACK}?code={CODE}&state={STATE}")
        client.get(f"/api/v1/x?client_secret={CLIENT_SECRET}&page=2")

    errors = [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("Request error")
    ]
    assert len(errors) == 2, caplog.text
    for value in VALUES:
        assert value not in caplog.text
    assert (
        f"'query_params': {{'code': '{REDACTED}', 'state': '{REDACTED}'}}" in errors[0]
    )
    assert (
        f"'query_params': {{'client_secret': '{REDACTED}', 'page': '2'}}" in errors[1]
    )
