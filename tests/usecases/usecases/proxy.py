"""H5-lite: a recording pass-through between one SDK driver and the API.

One proxy per driver, in the runner's own process, so a case reads the times of
the requests and of its own commands on one clock (``time.monotonic`` unless a
clock is given). Copied from the MCP contract suite's ``RecordingProxy``
(``tests/mcp-contract/run_mcp_contract.py``) and cut down, not imported: that
one belongs to a required job's script and is tied to its OpenAPI route map.

What it keeps, per request (``Record``): its sequence number, when it arrived
and when it was answered, the method, the path without its query string, the
status sent back, the ``ETag`` of an answer on ``RULESET_PATH`` (``None`` on
every other path), and whether the request carried ``If-None-Match``. Nothing
else: no other header's value, no query string (it holds the user's id and
attributes), no body. Nothing is printed: the server's request log, which
prints the request line with its query string, is silenced.

The record is stored before the answer is written, so a client that has its
answer can count it.

How it relays:

* Every header is forwarded except the hop-by-hop ones, the client's ``Host``
  and ``Accept-Encoding`` (so the API answers uncompressed bytes that pass
  through as they are). The ``X-API-Key`` is forwarded and never stored.
* No redirect is followed: an upstream 307 goes back to the client as a 307.
* No answer is cached: every request reaches the upstream.
* An upstream that cannot be reached, or that breaks off, is answered 502, and
  recorded.
* Connection policy: every answer carries ``Connection: close`` and the
  connection is closed after it, so N requests are N connections and N
  records whatever the client's keep-alive does.

Refused (``ProxyRefused``, when it is constructed, before anything is bound or
sent):

* listening anywhere but 127.0.0.1;
* an upstream that is not ``http://``, carries a user, a path, a query or a
  fragment, or whose host is not exactly ``127.0.0.1`` or ``localhost`` (both
  are reached at 127.0.0.1);
* an upstream on 5432, 5433, 6379, 8000, 3000, 3100, 3200 or 3300 unless
  ``GITHUB_ACTIONS`` is exactly ``true``: on a developer's machine those are
  their database, Redis, API and dashboards.
"""

from __future__ import annotations

import http.client
import os
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

#: The only address the proxy listens on, and the address an upstream is
#: reached at.
LISTEN_HOST = "127.0.0.1"
#: The upstream host names accepted, exactly.
UPSTREAM_HOSTS = frozenset(["127.0.0.1", "localhost"])
#: Ports refused as an upstream outside GitHub Actions.
FOUNDER_PORTS = frozenset([5432, 5433, 6379, 8000, 3000, 3100, 3200, 3300])
#: The path whose answers' ETag is recorded.
RULESET_PATH = "/api/v1/sdk/ruleset"
#: The longest wait for the upstream's answer, and for a client's request.
UPSTREAM_SECONDS = 30.0
CLIENT_SECONDS = 30.0
#: The longest wait for the serving thread to end on ``stop``.
STOP_SECONDS = 5.0
#: Answers that carry no body.
NO_BODY = frozenset([204, 304])

REQUEST_SKIP = frozenset(
    [
        "connection",
        "keep-alive",
        "proxy-connection",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
        "accept-encoding",
    ]
)
RESPONSE_SKIP = frozenset(
    [
        "connection",
        "keep-alive",
        "transfer-encoding",
        "upgrade",
        "content-length",
        "server",
        "date",
    ]
)


class ProxyRefused(ValueError):
    """The proxy was asked to listen or forward somewhere it may not."""


@dataclass(frozen=True)
class Record:
    """One request the proxy relayed. ``t_recv`` and ``t_answer`` are on the
    proxy's clock; ``etag`` is set only on ``RULESET_PATH``."""

    seq: int
    t_recv: float
    t_answer: float
    method: str
    path: str
    status: int
    etag: str | None
    if_none_match: bool


def check_upstream(upstream: str) -> int:
    """The upstream's port, once *upstream* is one the proxy may forward to;
    ``ProxyRefused`` otherwise. Reads ``GITHUB_ACTIONS`` when called."""
    parsed = urllib.parse.urlsplit(upstream)
    if parsed.scheme != "http":
        raise ProxyRefused("the upstream must be an http:// origin")
    if parsed.username is not None or parsed.password is not None:
        raise ProxyRefused("the upstream may not carry a user or a password")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ProxyRefused("the upstream is an origin: no path, query or fragment")
    if parsed.hostname not in UPSTREAM_HOSTS:
        raise ProxyRefused(
            f"upstream host {parsed.hostname!r} is not exactly 127.0.0.1 or localhost"
        )
    try:
        port = parsed.port
    except ValueError:
        raise ProxyRefused("the upstream port is not a port number") from None
    if port is None:
        port = 80
    if port in FOUNDER_PORTS and os.environ.get("GITHUB_ACTIONS") != "true":
        raise ProxyRefused(
            f"upstream port {port} is refused outside GitHub Actions: on a "
            "developer's machine it is their database, Redis, API or dashboard"
        )
    return port


def _read_body(handler: BaseHTTPRequestHandler) -> bytes:
    """The request's body, by ``Content-Length`` or chunked."""
    if "chunked" in (handler.headers.get("Transfer-Encoding") or "").lower():
        chunks = []
        while True:
            size = int(handler.rfile.readline().split(b";", 1)[0].strip() or b"0", 16)
            if size == 0:
                handler.rfile.readline()
                break
            chunks.append(handler.rfile.read(size))
            handler.rfile.readline()
        return b"".join(chunks)
    length = int(handler.headers.get("Content-Length") or 0)
    return handler.rfile.read(length) if length > 0 else b""


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    proxy: RecordingProxy

    def handle_error(self, request: Any, client_address: Any) -> None:
        # The default prints a traceback to stderr; the proxy counts it instead.
        self.proxy._handler_failed()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = CLIENT_SECONDS
    proxy: RecordingProxy

    def setup(self) -> None:
        super().setup()
        self.proxy._connected()

    def log_message(self, format: str, *args: Any) -> None:
        # The request line carries the query string; nothing is printed.
        return

    def _relay(self) -> None:
        self.proxy._relay(self)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _relay


class RecordingProxy:
    """A pass-through to *upstream* on an ephemeral 127.0.0.1 port that
    records every request. ``start`` binds the port; use it as a context
    manager, or ``stop`` it."""

    def __init__(
        self,
        upstream: str,
        *,
        listen: str = LISTEN_HOST,
        clock: Callable[[], float] = time.monotonic,
        upstream_seconds: float = UPSTREAM_SECONDS,
    ) -> None:
        if listen != LISTEN_HOST:
            raise ProxyRefused(f"the proxy listens only on {LISTEN_HOST}")
        self._upstream_port = check_upstream(upstream)
        self._clock = clock
        self._upstream_seconds = upstream_seconds
        self._lock = threading.Lock()
        self._seq = 0
        self._records: list[Record] = []
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        #: Connections accepted.
        self.connections = 0
        #: Requests whose handling raised (the client went away mid-answer).
        self.handler_errors = 0
        self.origin = ""

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> RecordingProxy:
        if self._server is not None:
            raise RuntimeError("the proxy is already started")
        handler = type("Handler", (_Handler,), {"proxy": self})
        server = _Server((LISTEN_HOST, 0), handler)
        server.proxy = self
        host, port = server.server_address[:2]
        if host != LISTEN_HOST or port in FOUNDER_PORTS:
            server.server_close()
            raise ProxyRefused(
                f"the proxy bound {host}:{port}, not {LISTEN_HOST}:<ephemeral>"
            )
        self._server = server
        self.origin = f"http://{LISTEN_HOST}:{port}"
        self._thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.1},
            name="usecases-proxy",
            daemon=True,
        )
        self._thread.start()
        return self

    @property
    def server_address(self) -> tuple[str, int]:
        if self._server is None:
            raise RuntimeError("the proxy is not started")
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    def stop(self) -> None:
        server, thread = self._server, self._thread
        self._server = self._thread = None
        if server is None:
            return
        if thread is not None and thread.is_alive():
            server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=STOP_SECONDS)

    def __enter__(self) -> RecordingProxy:
        return self.start()

    def __exit__(self, *exc_info: Any) -> None:
        self.stop()

    # -- what it saw -----------------------------------------------------------
    def records(self) -> list[Record]:
        """Every request so far, in the order they arrived."""
        with self._lock:
            return sorted(self._records, key=lambda record: record.seq)

    def count(self, path: str | None = None) -> int:
        """Requests so far, or those to *path*."""
        return sum(1 for r in self.records() if path is None or r.path == path)

    def _connected(self) -> None:
        with self._lock:
            self.connections += 1

    def _handler_failed(self) -> None:
        with self._lock:
            self.handler_errors += 1

    # -- relaying ----------------------------------------------------------------
    def _forward(
        self, method: str, target: str, body: bytes, headers: dict[str, str]
    ) -> tuple[int, str, list[tuple[str, str]], bytes]:
        connection = http.client.HTTPConnection(
            LISTEN_HOST, self._upstream_port, timeout=self._upstream_seconds
        )
        try:
            connection.request(method, target, body=body or None, headers=headers)
            response = connection.getresponse()
            payload = response.read()
            return response.status, response.reason, response.getheaders(), payload
        finally:
            connection.close()

    def _relay(self, handler: BaseHTTPRequestHandler) -> None:
        t_recv = self._clock()
        with self._lock:
            self._seq += 1
            seq = self._seq
        method = handler.command
        target = handler.path
        path = target.split("?", 1)[0]
        if_none_match = "If-None-Match" in handler.headers
        body = _read_body(handler)
        forward = {
            name: value
            for name, value in handler.headers.items()
            if name.lower() not in REQUEST_SKIP
        }
        try:
            status, reason, headers, payload = self._forward(
                method, target, body, forward
            )
        except (OSError, http.client.HTTPException):
            status, reason, headers, payload = 502, "Bad Gateway", [], b""
        etag = None
        if path == RULESET_PATH:
            etag = next((v for n, v in headers if n.lower() == "etag"), None)
        record = Record(
            seq, t_recv, self._clock(), method, path, status, etag, if_none_match
        )
        with self._lock:
            self._records.append(record)
        handler.send_response(status, reason)
        for name, value in headers:
            if name.lower() not in RESPONSE_SKIP:
                handler.send_header(name, value)
        if status not in NO_BODY:
            handler.send_header("Content-Length", str(len(payload)))
        handler.send_header("Connection", "close")
        handler.end_headers()
        if method != "HEAD" and status not in NO_BODY:
            handler.wfile.write(payload)
        handler.close_connection = True
