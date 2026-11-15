"""The API, run as the image runs it, writes some query values to no log line.

``uvicorn backend.app.main:app`` writes each request's target, query string
included, to its access log, and each WebSocket handshake's to
``uvicorn.error``. Every value on the OIDC callback, and the value of a
parameter named ``code``, ``state``, ``token`` (and the others in
``backend/app/core/query_redaction.py``) on any route, must be written as
``[redacted]``, with the parameter's name kept.

This drives the real server, as a separate process, and reads everything it
wrote. The callback is a route in the full profile and a 404 in the core one;
the access line is written either way.
"""

from __future__ import annotations

import os
import pathlib
import socket
import subprocess
import sys
import time
import urllib.request
import uuid

import httpx
import pytest
from websockets.sync.client import connect as ws_connect

pytestmark = [pytest.mark.integration, pytest.mark.regression]

REPO = pathlib.Path(__file__).resolve().parents[4]
REDACTED = "[redacted]"
CALLBACK = "/api/v1/auth/sso/oidc/okta/callback"
CODE = f"c0de-{uuid.uuid4().hex}"
STATE = f"st4te-{uuid.uuid4().hex}"
METRICS_TOKEN = f"m3trics-{uuid.uuid4().hex}"
WS_TOKEN = f"ws-t0ken-{uuid.uuid4().hex}"
# An attribute value inside the URL-encoded JSON an SDK sends as ``context``.
CONTEXT_VALUE = f"c0ntext-{uuid.uuid4().hex}"
CONTEXT = f"%7B%22plan%22%3A%22{CONTEXT_VALUE}%22%7D"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_the_running_api_logs_query_names_not_values(tmp_path):
    port = _free_port()
    log_path = tmp_path / "uvicorn.log"
    log = log_path.open("w")
    env = dict(os.environ, LOG_FORMAT="json")
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "backend.app.main:app",
            "--http",
            "h11",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=REPO,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/health/live", timeout=1
                )
                break
            except OSError:
                if server.poll() is not None or time.monotonic() > deadline:
                    pytest.fail(
                        "uvicorn did not start:\n" + log_path.read_text()[-3000:]
                    )
                time.sleep(0.3)

        base = f"http://127.0.0.1:{port}"
        httpx.get(f"{base}{CALLBACK}?code={CODE}&state={STATE}", timeout=30)
        httpx.get(f"{base}/metrics?token={METRICS_TOKEN}&page=2", timeout=30)
        # What an SDK sends to evaluate a flag (#1552). No API key is sent;
        # the access line is written whatever the answer.
        httpx.get(
            f"{base}/api/v1/feature-flags/evaluate/k?user_id=u&context={CONTEXT}",
            timeout=30,
        )
        experiment = uuid.uuid4()
        try:
            with ws_connect(
                f"ws://127.0.0.1:{port}/api/v1/ws/experiments/{experiment}/results"
                f"?token={WS_TOKEN}",
                open_timeout=30,
            ) as ws:
                ws.recv(timeout=30)
        except Exception:
            pass  # refused with 4401; only the handshake's log line matters
    finally:
        server.terminate()
        server.wait(timeout=20)
        log.close()

    written = log_path.read_text()
    for value in (CODE, STATE, METRICS_TOKEN, WS_TOKEN, CONTEXT_VALUE):
        assert value not in written, f"{value} was logged:\n{written[-5000:]}"
    assert f"{CALLBACK}?code={REDACTED}&state={REDACTED} HTTP/1.1" in written, written[
        -5000:
    ]
    assert f"/metrics?token={REDACTED}&page=2 HTTP/1.1" in written, written[-5000:]
    assert (
        f"/api/v1/feature-flags/evaluate/k?user_id=u&context={REDACTED} HTTP/1.1"
        in written
    ), written[-5000:]
    assert f"/api/v1/ws/experiments/{experiment}/results?token={REDACTED}" in written, (
        written[-5000:]
    )
