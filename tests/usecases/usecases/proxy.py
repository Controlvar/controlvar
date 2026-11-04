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

Faults (use case #4: an outage, a corrupt ruleset, a hung API). ``inject``
opens a window in which the proxy answers matching requests itself, until
``clear``; each such request is recorded with the fault it got (``Record.fault``),
so a gate counts faults from the record, never from timestamps. A ``Fault`` is
one of three kinds:

* ``status``: the code (500, 502, 503 or 504; 503 unless given), a JSON body
  ``{"detail":"injected by the use-case proxy"}``, ``Connection: close``, no
  ``Retry-After``, no ``ETag``.
* ``corrupt``: only on ``paths=(RULESET_PATH,)``. A 200, JSON content type, an
  exact ``Content-Length``, no ``ETag``, and one of ``CORRUPT_BODIES`` (named
  by ``body``): each is a 200 the SDKs treat as "not a valid ruleset" (keep
  the ruleset they have, report the refresh, back off). It is synthesized,
  never made from a forwarded answer: after a good load every refresh carries
  ``If-None-Match`` and the API answers 304 with no body, so a corrupt fault
  answers such a request 200 too.
* ``blackhole``: the request is read whole and nothing is sent. The hold ends
  at the first of: the client closing its connection (looked for at least
  every ``HOLD_POLL`` seconds; ``client_closed`` is then true), ``hold_seconds``
  (more than 0, at most 30; 10 unless given), or ``stop``. Then the request
  is recorded (status 0) and its connection closed with no answer.

``paths`` is ``None`` (every request) or a non-empty tuple of exact paths under
``/api/v1/``: no query, no fragment, no prefix matching. A request is faulted
when a fault is active and its path (without the query) matches; that is
decided when it arrives, under the lock that gives it its sequence number,
and a faulted request is never forwarded. A held blackhole finishes as decided
even if ``clear`` runs meanwhile; its record appears when the hold ends. A
faulted record's ``etag`` is always ``None``.

``Fault`` refuses (``ProxyRefused``, when it is constructed) a kind outside
the three (``reset`` included); empty paths, a path not under ``/api/v1/`` or
holding ``?`` or ``#``; ``status`` given for another kind, or a code outside
500/502/503/504; ``body`` given for another kind, or outside the names of
``CORRUPT_BODIES`` (a corrupt fault must name one); a corrupt fault on any
other paths; ``hold_seconds`` given for another kind, or outside (0, 30].
``inject`` refuses, before changing anything, while a fault is active, and
before ``start`` or after ``stop``.

``changes`` lists every inject and clear with the sequence number last given
out when it happened and the proxy's time; ``wait_idle`` waits (at most 30 s)
until every request that arrived has its record and its answer.
``teardown_problem`` is the end-of-case check: no fault left on, and one
ruleset ``GET`` through the proxy recorded unfaulted, 200 or 304.
"""

from __future__ import annotations

import http.client
import math
import os
import select
import socket
import threading
import time
import urllib.parse
from collections.abc import Callable, Mapping
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

#: The fault kinds ``inject`` takes.
FAULT_KINDS = ("blackhole", "corrupt", "status")
#: Every fault path starts with this.
API_PREFIX = "/api/v1/"
#: The codes a ``status`` fault may answer, and the one it answers unless given.
FAULT_STATUSES = (500, 502, 503, 504)
DEFAULT_FAULT_STATUS = 503
#: A ``status`` fault's body.
FAULT_DETAIL = b'{"detail":"injected by the use-case proxy"}'
#: A ``corrupt`` fault's bodies, by name: each a 200 that is not a valid
#: ruleset. ``truncated`` and ``html`` are not JSON; ``not-a-ruleset`` is a
#: schema-1 document with no ``flags``.
CORRUPT_BODIES = {
    "truncated": (
        b'{"schema": 1, "bucketing": "md5-mod100-v1", "version": "corrupt",'
        b' "flags": [{"key": '
    ),
    "not-a-ruleset": (
        b'{"schema": 1, "bucketing": "md5-mod100-v1", "version": "corrupt"}'
    ),
    "html": b"<html><body>502 Bad Gateway</body></html>",
}
#: A ``blackhole`` fault's hold, unless given, and its longest.
DEFAULT_HOLD_SECONDS = 10.0
MAX_HOLD_SECONDS = 30.0
#: How often a held request looks for its client having closed.
HOLD_POLL = 0.05
#: The longest ``wait_idle`` waits.
MAX_IDLE_SECONDS = 30.0
#: The longest the teardown probe waits for its answer.
PROBE_SECONDS = 10.0

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
    """One request the proxy relayed or answered itself. ``t_recv`` and
    ``t_answer`` are on the proxy's clock; ``etag`` is set only on
    ``RULESET_PATH``, and never on a faulted request. ``fault`` is ``None``
    for a relayed request, else the fault it got (``Fault.tag``:
    ``status:503``, ``corrupt:truncated``, ``blackhole``); ``status`` is 0
    for a blackhole (nothing was sent), and ``client_closed`` is true only
    for a blackhole whose client closed before the hold ended."""

    seq: int
    t_recv: float
    t_answer: float
    method: str
    path: str
    status: int
    etag: str | None
    if_none_match: bool
    fault: str | None = None
    client_closed: bool = False


def _fault_problem(
    kind: Any, paths: Any, status: Any, body: Any, hold_seconds: Any
) -> str | None:
    """Why these arguments are not a fault ``inject`` takes; ``None`` when
    they are."""
    if not isinstance(kind, str) or kind not in FAULT_KINDS:
        return f"fault kind {kind!r} is not one of {', '.join(FAULT_KINDS)}"
    if paths is not None:
        if not isinstance(paths, tuple) or not paths:
            return "paths is None (every request) or a non-empty tuple of exact paths"
        for path in paths:
            if (
                not isinstance(path, str)
                or not path.startswith(API_PREFIX)
                or "?" in path
                or "#" in path
            ):
                return (
                    f"fault path {path!r} is not an exact path under {API_PREFIX}"
                    " (no query, no fragment)"
                )
    if kind != "status" and status is not None:
        return f"status is for a status fault, not a {kind} fault"
    if kind == "status" and status is not None:
        if (
            isinstance(status, bool)
            or not isinstance(status, int)
            or status not in FAULT_STATUSES
        ):
            return (
                f"fault status {status!r} is not one of"
                f" {', '.join(map(str, FAULT_STATUSES))}"
            )
    if kind != "corrupt" and body is not None:
        return f"body is for a corrupt fault, not a {kind} fault"
    if kind == "corrupt":
        if not isinstance(body, str) or body not in CORRUPT_BODIES:
            return f"corrupt body {body!r} is not one of {', '.join(sorted(CORRUPT_BODIES))}"
        if paths != (RULESET_PATH,):
            return f"a corrupt fault is only for paths=({RULESET_PATH!r},)"
    if kind != "blackhole" and hold_seconds is not None:
        return f"hold_seconds is for a blackhole fault, not a {kind} fault"
    if kind == "blackhole" and hold_seconds is not None:
        if (
            isinstance(hold_seconds, bool)
            or not isinstance(hold_seconds, (int, float))
            or not math.isfinite(hold_seconds)
            or not 0 < hold_seconds <= MAX_HOLD_SECONDS
        ):
            return (
                f"hold_seconds {hold_seconds!r} is not more than 0 and at most"
                f" {MAX_HOLD_SECONDS:g}"
            )
    return None


@dataclass(frozen=True)
class Fault:
    """What the proxy answers, itself, to the requests it matches while it is
    injected (see the module's docstring). Refused when constructed
    (``ProxyRefused``) unless it is one ``inject`` takes. Once constructed a
    ``status`` fault's ``status`` is its code (503 unless given) and a
    ``blackhole`` fault's ``hold_seconds`` its hold (10 unless given); given
    for another kind, either is refused."""

    kind: str
    paths: tuple[str, ...] | None = None
    status: int | None = None
    body: str | None = None
    hold_seconds: float | None = None

    def __post_init__(self) -> None:
        problem = _fault_problem(
            self.kind, self.paths, self.status, self.body, self.hold_seconds
        )
        if problem is not None:
            raise ProxyRefused(problem)
        if self.kind == "status" and self.status is None:
            object.__setattr__(self, "status", DEFAULT_FAULT_STATUS)
        if self.kind == "blackhole" and self.hold_seconds is None:
            object.__setattr__(self, "hold_seconds", DEFAULT_HOLD_SECONDS)

    @property
    def tag(self) -> str:
        """What a faulted record's ``fault`` says."""
        if self.kind == "status":
            return f"status:{self.status}"
        if self.kind == "corrupt":
            return f"corrupt:{self.body}"
        return self.kind

    def matches(self, path: str) -> bool:
        """Whether a request to *path* (without its query) gets this fault."""
        return self.paths is None or path in self.paths


@dataclass(frozen=True)
class Change:
    """One ``inject`` or ``clear``: ``seq`` is the sequence number last given
    out when it happened (a request with a higher one arrived after it), ``t``
    the proxy's time, ``event`` ``inject:<kind>`` or ``clear``."""

    seq: int
    t: float
    event: str


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
        #: Signalled, under ``_lock``, each time a request is done.
        self._idle = threading.Condition(self._lock)
        self._seq = 0
        self._records: list[Record] = []
        self._fault: Fault | None = None
        self._changes: list[Change] = []
        self._in_flight = 0
        self._stopping = threading.Event()
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
        self._stopping.clear()
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
        """Stop serving. A held blackhole ends (as ``stop``) within
        ``HOLD_POLL``. The requests' threads are daemon threads, which
        ``server_close`` does not join, so this waits (at most
        ``STOP_SECONDS``) until no request is in flight: once it returns, every
        request that arrived has its record, a held one included."""
        server, thread = self._server, self._thread
        self._server = self._thread = None
        if server is None:
            return
        self._stopping.set()
        if thread is not None and thread.is_alive():
            server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=STOP_SECONDS)
        self.wait_idle(STOP_SECONDS)

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

    # -- faults -------------------------------------------------------------------
    @property
    def active(self) -> Fault | None:
        """The fault injected now, or ``None``."""
        with self._lock:
            return self._fault

    def inject(self, fault: Fault) -> None:
        """Answer every request *fault* matches with it, from now until
        ``clear``. ``ProxyRefused``, before anything changes, while a fault is
        active, and before ``start`` or after ``stop``."""
        if not isinstance(fault, Fault):
            raise ProxyRefused(f"inject takes a Fault, not {type(fault).__name__}")
        with self._lock:
            if self._server is None:
                raise ProxyRefused(
                    "inject needs a running proxy: after start() and before stop()"
                )
            if self._fault is not None:
                raise ProxyRefused(
                    f"a {self._fault.kind} fault is already active: clear() it first"
                )
            self._fault = fault
            self._changes.append(
                Change(self._seq, self._clock(), f"inject:{fault.kind}")
            )

    def clear(self) -> None:
        """End the fault window (nothing when none is active). A blackhole
        already holding a request finishes as it was decided."""
        with self._lock:
            if self._fault is None:
                return
            self._fault = None
            self._changes.append(Change(self._seq, self._clock(), "clear"))

    def changes(self) -> list[Change]:
        """Every ``inject`` and ``clear`` so far, in order."""
        with self._lock:
            return list(self._changes)

    @property
    def in_flight(self) -> int:
        """Requests that arrived and are not done yet (a held one included)."""
        with self._lock:
            return self._in_flight

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """Wait until every request that arrived has its record and its
        answer (a held blackhole included), at most *timeout* seconds (more
        than 0, at most 30). True once idle, False if *timeout* passed first."""
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not 0 < timeout <= MAX_IDLE_SECONDS
        ):
            raise ProxyRefused(
                f"wait_idle waits more than 0 and at most {MAX_IDLE_SECONDS:g} s,"
                f" not {timeout!r}"
            )
        with self._idle:
            return self._idle.wait_for(lambda: self._in_flight == 0, timeout)

    def probe(
        self, headers: Mapping[str, str] | None = None, path: str = RULESET_PATH
    ) -> Record | None:
        """One ``GET`` of *path* sent through this proxy; its record, or
        ``None`` when not exactly one new record for *path* appeared (another
        client sent one meanwhile, or the request got nowhere)."""
        host, port = self.server_address
        with self._lock:
            before = self._seq
        connection = http.client.HTTPConnection(host, port, timeout=PROBE_SECONDS)
        try:
            connection.request("GET", path, headers=dict(headers or {}))
            connection.getresponse().read()
        except (OSError, http.client.HTTPException):
            pass
        finally:
            connection.close()
        mine = [r for r in self.records() if r.seq > before and r.path == path]
        return mine[0] if len(mine) == 1 else None

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
        method = handler.command
        target = handler.path
        path = target.split("?", 1)[0]
        # Decided at arrival, under the lock that gives out the sequence
        # number: a request is faulted exactly when a fault was active then.
        with self._lock:
            self._seq += 1
            seq = self._seq
            t_recv = self._clock()
            fault = self._fault
            if fault is not None and not fault.matches(path):
                fault = None
            self._in_flight += 1
        try:
            if fault is None:
                self._forward_and_answer(handler, seq, t_recv, method, target, path)
            else:
                self._answer_fault(handler, fault, seq, t_recv, method, path)
        finally:
            with self._idle:
                self._in_flight -= 1
                self._idle.notify_all()

    def _store(self, record: Record) -> None:
        with self._lock:
            self._records.append(record)

    def _hold(self, handler: BaseHTTPRequestHandler, hold_seconds: float) -> bool:
        """Send nothing until the client closes (True), *hold_seconds* pass
        or the proxy stops (False). Timed on the real monotonic clock."""
        connection = handler.connection
        deadline = time.monotonic() + hold_seconds
        while not self._stopping.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                readable, _, _ = select.select(
                    [connection], [], [], min(HOLD_POLL, remaining)
                )
                if readable and not connection.recv(1, socket.MSG_PEEK):
                    return True
            except (OSError, ValueError):
                return True
            if readable:
                # Bytes, not a close: wait a poll before looking again.
                self._stopping.wait(HOLD_POLL)
        return False

    def _answer_fault(
        self,
        handler: BaseHTTPRequestHandler,
        fault: Fault,
        seq: int,
        t_recv: float,
        method: str,
        path: str,
    ) -> None:
        if_none_match = "If-None-Match" in handler.headers
        _read_body(handler)
        handler.close_connection = True
        if fault.kind == "blackhole":
            assert fault.hold_seconds is not None
            closed = self._hold(handler, fault.hold_seconds)
            self._store(
                Record(
                    seq,
                    t_recv,
                    self._clock(),
                    method,
                    path,
                    0,
                    None,
                    if_none_match,
                    fault.tag,
                    closed,
                )
            )
            return
        if fault.kind == "status":
            assert fault.status is not None
            status, payload = fault.status, FAULT_DETAIL
        else:
            assert fault.body is not None
            status, payload = 200, CORRUPT_BODIES[fault.body]
        self._store(
            Record(
                seq,
                t_recv,
                self._clock(),
                method,
                path,
                status,
                None,
                if_none_match,
                fault.tag,
            )
        )
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(payload)))
        handler.send_header("Connection", "close")
        handler.end_headers()
        if method != "HEAD":
            handler.wfile.write(payload)

    def _forward_and_answer(
        self,
        handler: BaseHTTPRequestHandler,
        seq: int,
        t_recv: float,
        method: str,
        target: str,
        path: str,
    ) -> None:
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
        self._store(
            Record(
                seq, t_recv, self._clock(), method, path, status, etag, if_none_match
            )
        )
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


def teardown_problem(
    relay: RecordingProxy, headers: Mapping[str, str] | None = None
) -> str | None:
    """The end of a case that injected a fault: ``None`` when no fault is
    left on and one ruleset ``GET`` through *relay* (with *headers*, the API
    key on the real API) is recorded unfaulted with 200 or 304; else what is
    wrong. Call it once the drivers behind *relay* have stopped, so the probe
    is the only request in flight."""
    left = relay.active
    if left is not None:
        return (
            f"a {left.tag} fault was left on at the end of the case: clear() it"
            " before the case ends"
        )
    record = relay.probe(headers)
    if record is None:
        return "the teardown probe's ruleset GET left no single record"
    if record.fault is not None or record.status not in (200, 304):
        return (
            f"the teardown probe's ruleset GET was answered {record.status}"
            f" (fault {record.fault}), expected an unfaulted 200 or 304"
        )
    return None
