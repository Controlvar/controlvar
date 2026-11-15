"""A fake API to put behind the recording proxy, for the drivers' own gates.

The proxy's and the Python driver's gates run in the backend's unit job
(``backend/tests/unit/usecases``); the JS driver's run where Node is, beside
the cases (``tests/usecases/test_js_driver.py``). Both use this one fake, so
the two drivers are held to the same upstream.

It listens on 127.0.0.1, on a port the system picks, and serves one flag
(``FLAG_KEY``), the ruleset with its ETag (304 to a matching
``If-None-Match``) and the evaluation counts. Nothing is printed. Standard
library and the proxy's ``RULESET_PATH`` only.
"""

from __future__ import annotations

import http.server
import json
import socket
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from usecases.proxy import RULESET_PATH

#: The one flag the fake upstream knows.
FLAG_KEY = "uc-flag"
#: Seconds any wait on a fake server may take.
SERVER_SECONDS = 5.0

Answer = tuple[int, dict[str, str], bytes]


@dataclass(frozen=True)
class Received:
    """One request as the fake upstream received it (header names lower-case)."""

    method: str
    target: str
    headers: dict[str, str]
    body: bytes


def closed_port() -> int:
    """A 127.0.0.1 port that was bound and closed: nothing listens there, so
    a connection to it is refused at once."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Upstream:
    """A fake API on 127.0.0.1:<ephemeral>. ``respond(received) -> (status,
    headers, body)`` decides each answer; the default serves one flag, the
    ruleset (with its ETag, and 304 to a matching ``If-None-Match``) and the
    evaluation counts. ``received`` lists what arrived."""

    def __init__(self, respond: Callable[[Received], Answer] | None = None) -> None:
        self.respond = respond or self.default
        self.received: list[Received] = []
        self.ruleset_version = "v1"
        self.flag_enabled = True
        self._lock = threading.Lock()
        self._server: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def origin(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def port(self) -> int:
        assert self._server is not None
        return int(self._server.server_address[1])

    def ruleset(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "bucketing": "md5-mod100-v1",
            "version": self.ruleset_version,
            "flags": [
                {
                    "key": FLAG_KEY,
                    "active": self.flag_enabled,
                    "evaluation": "local",
                    "rollout_percentage": 100,
                    "rules": [],
                }
            ],
        }

    def default(self, received: Received) -> Answer:
        path = received.target.split("?", 1)[0]
        json_type = {"Content-Type": "application/json"}
        if path == f"/api/v1/feature-flags/evaluate/{FLAG_KEY}":
            body = {
                "key": FLAG_KEY,
                "enabled": self.flag_enabled,
                "reason": "rollout" if self.flag_enabled else "inactive",
                "value": self.flag_enabled,
                "value_type": "boolean",
            }
            return 200, json_type, json.dumps(body).encode()
        if path == RULESET_PATH:
            etag = f'"{self.ruleset_version}"'
            if received.headers.get("if-none-match") == etag:
                return 304, {"ETag": etag}, b""
            return (
                200,
                {**json_type, "ETag": etag},
                json.dumps(self.ruleset()).encode(),
            )
        if path == "/api/v1/tracking/evaluations":
            return 202, json_type, b'{"accepted": true}'
        return 404, json_type, b'{"detail": "Not Found"}'

    def start(self) -> Upstream:
        upstream = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            timeout = SERVER_SECONDS

            def log_message(self, format: str, *args: Any) -> None:
                return

            def _answer(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length > 0 else b""
                received = Received(
                    self.command,
                    self.path,
                    {name.lower(): value for name, value in self.headers.items()},
                    body,
                )
                with upstream._lock:
                    upstream.received.append(received)
                status, headers, payload = upstream.respond(received)
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                if status not in (204, 304):
                    self.send_header("Content-Length", str(len(payload)))
                self.send_header("Connection", "close")
                self.end_headers()
                if self.command != "HEAD" and status not in (204, 304):
                    self.wfile.write(payload)
                self.close_connection = True

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = _answer

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.1},
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        server, thread = self._server, self._thread
        self._server = self._thread = None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=SERVER_SECONDS)

    def requests_to(self, path: str) -> list[Received]:
        with self._lock:
            return [r for r in self.received if r.target.split("?", 1)[0] == path]
