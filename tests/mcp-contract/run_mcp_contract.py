#!/usr/bin/env python3
"""
The MCP server's contract suite: the server built from this checkout, against a
live API (#1607).

Where it runs
    The second step of the required ``SDK Live Contract / sdk-live-contract
    (core)`` job (``.github/workflows/_platform.yml``), after the SDK contract,
    against the API that job booted: core profile, ``AUTH_PROVIDER=local``,
    the dev bypass off, Postgres and Redis up, and the background schedulers
    running (``APP_ENV=development``). The step runs ``npm ci`` and
    ``npm run build`` in ``sdk/mcp`` and then this script, which exits non-zero
    on any failure. It never skips: no ``node``, a ``node`` below the package's
    ``engines`` floor, or no ``sdk/mcp/dist/index.js`` is a failure.

What it does
    1. Q9: an unauthenticated request must be refused, or nothing below means
       anything (the dev bypass would admit every request).
    2. It makes its own data beside the sdk-contract seed's, and changes none
       of that seed's rows: users of each role, their keys, the bad keys, the
       flags and two experiments, every name carrying one random tag per run
       (see ``seed``).
    3. It starts the server (``node sdk/mcp/dist/index.js``) once per key, each
       with an explicit environment that holds only ``PATH`` and the two
       settings, and speaks raw newline-delimited JSON-RPC to it over stdin
       and stdout. It does not use an MCP SDK: the client and the server are
       not the same code. Between each server and the API sits a recording
       proxy on an ephemeral 127.0.0.1 port, which is the server's
       ``CONTROLVAR_API_URL``; the proxy keeps the method, the path, the
       route it matches in the API's own OpenAPI document, and whether each
       credential header was the configured key ("configured", "other",
       "none"). It never keeps or prints a key.
    4. It reads the database before and after, through the backend's own
       session, as ``tests/sdk-contract/live/run_live_contract.py`` does.

Rows (PLAN-v1 VERIFICATION, as the v1.1 delta amends it)
    Q2   every tool ``tools/list`` serves has a contract case here, and passes it
    Q3   read-only at the wire: the (method, route) set the proxies saw is
         exactly ALLOW_LIST, and every entry was called
    Q4   read-only at the database, scoped to this run because the schedulers
         write while it runs: ``audit_logs`` naming this run's users +0;
         FLAG_EVALUATION rows carrying this run's user ids = the evaluations
         the suite made; ``api_keys`` and ``users`` unchanged in count
         (``last_used_at`` moves, as expected); ``analysis_snapshots`` of this
         run's experiments only of the kinds a results read writes, none for
         the draft. No whole-table equality.
    Q5   a write tool cannot be called, and sends nothing
    Q6   every request carries exactly the configured ``X-API-Key``, and never
         ``Authorization`` or a cookie
    Q7   every answer is the same for VIEWER, ANALYST, DEVELOPER and ADMIN
         non-superusers and a superuser
    Q8   a deleted key, an expired key and a deactivated owner's key (401), and
         a key without ``mcp:read`` and an ``sdk:ruleset``-only key (403): a
         tool error with the exact text and no data, and the same process
         answers the next call
    Q9   (above)
    Q10  no key, nor any 9-character piece of one, in any stdout line, any
         stderr byte or any tool result
    Q12  every stdout line is JSON-RPC
    Q14  ``list_feature_flags``' ``total`` equals the database's count, so a
         page shorter than it says it is partial (more than 100 flags exist).
         The list is ordered by creation time (#1612); the backend test
         test_flag_list_order.py pins the order, so this check does not.
    Q15  ``get_feature_flag`` answers the exact key past decoys seeded before
         it; with more than 100 matches and no exact one it refuses
    Q16  evaluation answers are exact: rollout, inactive, targeting rule with
         and without the attribute, unknown key
    Q17  ``get_experiment_results`` equals the API's own answer to the same key
    Q18  a key or an id carrying ``/``, ``?``, ``#``, ``%2F``, ``.`` or ``..``
         is refused with no request
    Q20  the server exits when its stdin closes

Nothing printed carries a key: tool texts are redacted (every 9-character
piece of every key) before they are shown, and a key found where it must not
be is reported by place only.

Usage (CI runs it; locally it needs a running API, its database and a built
``sdk/mcp``):
    python tests/mcp-contract/run_mcp_contract.py [--api-url http://127.0.0.1:8000]
"""

from __future__ import annotations

import argparse
import hmac
import http.client
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
MCP_DIR = REPO_ROOT / "sdk" / "mcp"
BIN = MCP_DIR / "dist" / "index.js"
PACKAGE_JSON = MCP_DIR / "package.json"
HTTP_SOURCE = MCP_DIR / "src" / "http.ts"

#: Q3: the only (method, route) pairs the server may call, as the API's OpenAPI
#: document spells them. Each needs a credential, and the one POST is the
#: evaluation every API key may make (backend/tests/unit/scripts/
#: test_run_mcp_contract.py checks both against the committed snapshot and the
#: flag-change inventory).
ALLOW_LIST: Tuple[Tuple[str, str], ...] = (
    ("GET", "/api/v1/feature-flags/"),
    ("POST", "/api/v1/feature-flags/evaluate/{flag_key}"),
    ("GET", "/api/v1/experiments/"),
    ("GET", "/api/v1/experiments/{experiment_id}"),
    ("GET", "/api/v1/experiments/{experiment_id}/results"),
)
#: Q5: tools a model might ask for that the server must not have.
WRITE_TOOLS = ("create_experiment", "toggle_feature_flag", "delete_flag")
#: Q7: the roles whose keys run every case. ADMIN's answers are the reference.
ROLES = ("ADMIN", "VIEWER", "ANALYST", "DEVELOPER", "SUPERUSER")
REFERENCE_ROLE = "ADMIN"
#: Q8: each refused key and the status the API gives it.
BAD_KEYS = (
    ("deleted", 401),
    ("expired", 401),
    ("deactivated owner", 401),
    ("no mcp:read", 403),
    ("sdk:ruleset only", 403),
)
#: The longest page the tools ask for; get_feature_flag reads one page of it.
PAGE = 100
#: The flags sharing one prefix (Q14 and Q15's refusal need more than PAGE).
BULK_FLAGS = PAGE + 1
#: The fields each list item carries (S4).
FLAG_LIST_FIELDS = frozenset(
    ["key", "name", "status", "rollout_percentage", "value_type", "tags", "updated_at"]
)
EXPERIMENT_LIST_FIELDS = frozenset(
    [
        "id",
        "key",
        "name",
        "status",
        "experiment_type",
        "start_date",
        "end_date",
        "updated_at",
    ]
)
EVALUATION_FIELDS = frozenset(["key", "enabled", "value", "value_type", "reason"])
#: What a results read may write for this suite's active experiment, which has
#: neither Bayesian analysis nor sequential testing on (see ``seed``).
RESULTS_SNAPSHOT_KINDS = frozenset(["frequentist"])
#: Q18: inputs that would leave their path segment. The schemas refuse them.
BAD_FLAG_KEYS = ("a/b", "x?y", "x#y", "a%2Fb", ".", "..", "a/..")
BAD_EXPERIMENT_IDS = (
    "..",
    ".",
    "../experiments",
    "%2e%2e",
    "{id}/results",
    "{id}?x=1",
    "{id}#x",
)
#: Seconds to wait for one JSON-RPC answer (the server's own timeout is 60).
ANSWER_SECONDS = 90
#: Q20: a liveness bound, not a performance one.
EXIT_SECONDS = 10
PROTOCOL_VERSION = "2025-06-18"

HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
#: Not forwarded by the proxy: hop-by-hop headers, the client's Host (the
#: upstream connection sets its own), and Accept-Encoding, so the API answers
#: uncompressed bytes the proxy can pass through as they are.
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

#: Every key handed to a server this run; shown text is redacted against them.
_SECRETS: List[str] = []


def say(text: str) -> None:
    """One line of the suite's output, flushed (the job log is read live)."""
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Redaction and comparison (pure)
# ---------------------------------------------------------------------------
def pieces(secret: str) -> List[str]:
    """Every 9-character piece of *secret* (the whole value when shorter)."""
    if len(secret) < 9:
        return [secret] if secret else []
    return [secret[i : i + 9] for i in range(len(secret) - 8)]


def redact(text: str, secrets_: Sequence[str]) -> str:
    """*text* with every 9-character piece of every secret replaced."""
    for secret in secrets_:
        for piece in pieces(secret):
            if piece in text:
                text = text.replace(piece, "[key]")
    return text


def shown(text: str, limit: int = 300) -> str:
    """A tool's text as it may be printed: redacted, then cut."""
    text = redact(text, _SECRETS)
    return text if len(text) <= limit else text[:limit] + "..."


def secret_location(secret: str, outputs: Dict[str, Sequence[str]]) -> Optional[str]:
    """Where *secret*, or any 9-character piece of it, appears; never the value."""
    found = pieces(secret)
    for where, lines in outputs.items():
        for index, line in enumerate(lines):
            if any(piece in line for piece in found):
                return f"{where} line {index + 1}"
    return None


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def first_difference(served: Any, expected: Any, path: str = "") -> Optional[str]:
    """The first path at which two JSON values differ, or None.

    Numbers compare by value: the server re-serialises the API's JSON in
    JavaScript, which writes the API's ``0.0`` as ``0``.
    """
    if _number(served) and _number(expected):
        return None if served == expected else (path or "(top)")
    if type(served) is not type(expected):
        return path or "(top)"
    if isinstance(served, dict):
        for key in sorted(set(served) | set(expected)):
            if key not in served or key not in expected:
                return f"{path}.{key}"
            found = first_difference(served[key], expected[key], f"{path}.{key}")
            if found:
                return found
        return None
    if isinstance(served, list):
        if len(served) != len(expected):
            return f"{path}.length"
        for index, (a, b) in enumerate(zip(served, expected)):
            found = first_difference(a, b, f"{path}[{index}]")
            if found:
                return found
        return None
    return None if served == expected else (path or "(top)")


def jsonrpc_problems(label: str, lines: Sequence[str]) -> List[str]:
    """Q12: every stdout line is a JSON-RPC 2.0 message."""
    for index, line in enumerate(lines):
        try:
            message = json.loads(line)
        except ValueError:
            message = None
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return [
                f"{label}: stdout line {index + 1} is not JSON-RPC: {shown(line, 60)!r}"
            ]
    return []


# ---------------------------------------------------------------------------
# Routes and the recording proxy
# ---------------------------------------------------------------------------
class RouteMap:
    """Concrete request paths to the route templates of the API's document."""

    def __init__(self, document: dict) -> None:
        self._routes: List[Tuple["re.Pattern[str]", int, str, Set[str]]] = []
        for template, operations in (document.get("paths") or {}).items():
            segments = template.split("/")
            pattern = "/".join(
                "[^/]+" if re.fullmatch(r"\{[^/{}]+\}", segment) else re.escape(segment)
                for segment in segments
            )
            literals = sum(
                1 for segment in segments if segment and not segment.startswith("{")
            )
            methods = {
                name.upper()
                for name in (operations or {})
                if name.lower() in HTTP_METHODS
            }
            self._routes.append(
                (re.compile(f"^{pattern}$"), literals, template, methods)
            )

    def __len__(self) -> int:
        return len(self._routes)

    def template(self, method: str, path: str) -> str:
        """The template *path* matches, preferring more literal segments and
        then a route with *method*; the path itself when none matches."""
        best: Optional[Tuple[int, int, str]] = None
        for pattern, literals, template, methods in self._routes:
            if pattern.match(path):
                rank = (literals, 1 if method in methods else 0, template)
                if best is None or rank[:2] > best[:2]:
                    best = rank
        return best[2] if best else path


@dataclass
class Recorded:
    """One request a server sent, as the proxy saw it. No header value is kept."""

    method: str
    path: str
    template: str
    #: "configured", "other" or "none": the X-API-Key header against the key
    #: this proxy's server was given, compared as bytes in constant time.
    key: str
    authorization: bool
    cookie: bool


def classify_key(presented: Sequence[str], configured: bytes) -> str:
    """'configured' for exactly one header equal to *configured*, 'none' for no
    header, 'other' for anything else."""
    if not presented:
        return "none"
    if len(presented) != 1:
        return "other"
    value = presented[0].encode("latin-1", "replace")
    return "configured" if hmac.compare_digest(value, configured) else "other"


class RecordingProxy:
    """A pass-through to the API on an ephemeral 127.0.0.1 port that records
    every request (see ``Recorded``) and follows no redirect. ``start`` binds
    the port; the relay itself needs no socket of its own."""

    def __init__(self, upstream: str, key: str, routes: RouteMap) -> None:
        parsed = urllib.parse.urlsplit(upstream)
        self._host = parsed.hostname or "127.0.0.1"
        self._port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self._https = parsed.scheme == "https"
        self._key = key.encode("utf-8")
        self._routes = routes
        self._lock = threading.Lock()
        self._server: Optional[ThreadingHTTPServer] = None
        self.records: List[Recorded] = []
        self.origin = ""

    def start(self) -> "RecordingProxy":
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                # The request line carries the query string; nothing is printed.
                return

            def relay(self) -> None:
                proxy._relay(self)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = (
                relay
            )

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.origin = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def count(self) -> int:
        with self._lock:
            return len(self.records)

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    @staticmethod
    def _body(handler: BaseHTTPRequestHandler) -> bytes:
        if "chunked" in (handler.headers.get("Transfer-Encoding") or "").lower():
            chunks = []
            while True:
                size = int(
                    handler.rfile.readline().split(b";", 1)[0].strip() or b"0", 16
                )
                if size == 0:
                    handler.rfile.readline()
                    break
                chunks.append(handler.rfile.read(size))
                handler.rfile.readline()
            return b"".join(chunks)
        length = int(handler.headers.get("Content-Length") or 0)
        return handler.rfile.read(length) if length > 0 else b""

    def _relay(self, handler: BaseHTTPRequestHandler) -> None:
        method = handler.command
        target = handler.path
        path = target.split("?", 1)[0]
        body = self._body(handler)
        record = Recorded(
            method=method,
            path=path,
            template=self._routes.template(method, path),
            key=classify_key(handler.headers.get_all("X-API-Key") or [], self._key),
            authorization="Authorization" in handler.headers,
            cookie="Cookie" in handler.headers,
        )
        with self._lock:
            self.records.append(record)
        forward = {
            name: value
            for name, value in handler.headers.items()
            if name.lower() not in REQUEST_SKIP
        }
        connection_type = (
            http.client.HTTPSConnection if self._https else http.client.HTTPConnection
        )
        try:
            connection = connection_type(self._host, self._port, timeout=ANSWER_SECONDS)
            connection.request(method, target, body=body or None, headers=forward)
            response = connection.getresponse()
            payload = response.read()
            status, reason, headers = (
                response.status,
                response.reason,
                response.getheaders(),
            )
            connection.close()
        except OSError:
            handler.send_response(502, "Bad Gateway")
            handler.send_header("Content-Length", "0")
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.close_connection = True
            return
        handler.send_response(status, reason)
        for name, value in headers:
            if name.lower() not in RESPONSE_SKIP:
                handler.send_header(name, value)
        handler.send_header("Content-Length", str(len(payload)))
        handler.send_header("Connection", "close")
        handler.end_headers()
        if method != "HEAD":
            handler.wfile.write(payload)
        handler.close_connection = True


# ---------------------------------------------------------------------------
# One server process, in raw JSON-RPC
# ---------------------------------------------------------------------------
@dataclass
class ToolResult:
    is_error: bool
    text: str


class Server:
    """``node sdk/mcp/dist/index.js``, spoken to in newline-delimited JSON-RPC."""

    def __init__(self, label: str, node: str, api_url: str, key: str) -> None:
        self.label = label
        self.key = key
        env = {
            "PATH": os.environ.get("PATH", ""),
            "CONTROLVAR_API_URL": api_url,
            "CONTROLVAR_API_KEY": key,
        }
        self._process = subprocess.Popen(
            [node, str(BIN)],
            cwd=str(MCP_DIR),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.stdout_lines: List[str] = []
        self.results: List[str] = []
        self._stderr = bytearray()
        self._answers: Dict[int, dict] = {}
        self._condition = threading.Condition()
        self._next_id = 1
        self._readers = [
            threading.Thread(target=self._read_stdout, daemon=True),
            threading.Thread(target=self._read_stderr, daemon=True),
        ]
        for reader in self._readers:
            reader.start()

    def _read_stdout(self) -> None:
        assert self._process.stdout is not None
        for raw in iter(self._process.stdout.readline, b""):
            line = raw.decode("utf-8", "replace")
            if line.endswith("\n"):
                line = line[:-1]
            with self._condition:
                self.stdout_lines.append(line)
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if (
                    isinstance(message, dict)
                    and isinstance(message.get("id"), int)
                    and "method" not in message
                ):
                    self._answers[message["id"]] = message
                    self._condition.notify_all()

    def _read_stderr(self) -> None:
        assert self._process.stderr is not None
        for chunk in iter(lambda: self._process.stderr.read(4096), b""):
            self._stderr.extend(chunk)

    def stderr(self) -> str:
        return bytes(self._stderr).decode("utf-8", "replace")

    def alive(self) -> bool:
        return self._process.poll() is None

    def _send(self, message: dict) -> None:
        assert self._process.stdin is not None
        try:
            self._process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
            self._process.stdin.flush()
        except (BrokenPipeError, ValueError):
            raise RuntimeError(
                f"the {self.label} server's stdin is closed: it has exited"
            ) from None

    def request(self, method: str, params: Optional[dict] = None) -> dict:
        with self._condition:
            ident = self._next_id
            self._next_id += 1
        message: Dict[str, Any] = {"jsonrpc": "2.0", "id": ident, "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)
        deadline = time.monotonic() + ANSWER_SECONDS
        with self._condition:
            while ident not in self._answers:
                remaining = deadline - time.monotonic()
                # Given up only once stdout has ended (every line it wrote has
                # been read) or the time is out.
                ended = not self._readers[0].is_alive()
                if remaining <= 0 or ended:
                    state = (
                        "its stdout ended"
                        if ended
                        else f"none within {ANSWER_SECONDS}s"
                    )
                    raise RuntimeError(
                        f"no answer to {method} from the {self.label} server ({state})"
                    )
                self._condition.wait(min(remaining, 0.5))
            return self._answers.pop(ident)

    def initialize(self) -> dict:
        answer = self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "controlvar-mcp-contract", "version": "0"},
            },
        )
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return answer

    def tool_names(self) -> List[str]:
        answer = self.request("tools/list", {})
        tools = (answer.get("result") or {}).get("tools") or []
        return [str(tool.get("name")) for tool in tools]

    def call(self, tool: str, arguments: dict) -> ToolResult:
        answer = self.request("tools/call", {"name": tool, "arguments": arguments})
        if "error" in answer:
            text = str((answer.get("error") or {}).get("message", ""))
            self.results.append(text)
            return ToolResult(True, text)
        result = answer.get("result") or {}
        text = "".join(
            str(item.get("text", ""))
            for item in result.get("content") or []
            if isinstance(item, dict)
        )
        self.results.append(text)
        return ToolResult(result.get("isError") is True, text)

    def close(self) -> Optional[int]:
        """Close stdin; the exit code, or None if it was still running after EXIT_SECONDS."""
        try:
            if self._process.stdin is not None:
                self._process.stdin.close()
        except OSError:
            pass
        try:
            code: Optional[int] = self._process.wait(timeout=EXIT_SECONDS)
        except subprocess.TimeoutExpired:
            code = None
            self._process.kill()
            self._process.wait()
        for reader in self._readers:
            reader.join(timeout=5)
        return code

    def kill(self) -> None:
        if self.alive():
            self._process.kill()
            self._process.wait()


# ---------------------------------------------------------------------------
# The run's own data
# ---------------------------------------------------------------------------
@dataclass
class Run:
    """What ``seed`` made. Every name carries ``tag``."""

    tag: str
    keys: Dict[str, str] = field(default_factory=dict)
    #: The users whose keys the servers hold: Q4's audit_logs scope.
    actor_ids: List[uuid.UUID] = field(default_factory=list)
    experiment_ids: Dict[str, uuid.UUID] = field(default_factory=dict)
    experiment_keys: Dict[str, str] = field(default_factory=dict)

    @property
    def canary(self) -> str:
        """The prefix of every user_id the suite evaluates for (Q4)."""
        return f"mcp-contract-{self.tag}-"

    def flag(self, name: str) -> str:
        return {
            "target": f"mcp_flag_{self.tag}",
            "decoy v2": f"mcp_flag_{self.tag}_v2",
            "decoy wildcard": f"mcpXflagX{self.tag}",
            "decoy named": f"mcp_named_{self.tag}",
            "on": f"mcp_on_{self.tag}",
            "off": f"mcp_off_{self.tag}",
            "us": f"mcp_us_{self.tag}",
            "absent": f"mcp_absent_{self.tag}",
            "bulk": f"mcp_bulk_{self.tag}",
        }[name]

    def decoys(self) -> List[str]:
        return [
            self.flag("decoy v2"),
            self.flag("decoy wildcard"),
            self.flag("decoy named"),
        ]


def _backend() -> None:
    sys.path.insert(0, str(REPO_ROOT))


def seed(tag: str) -> Run:
    """Make this run's data, in the API's database, as a seed script would.

    * users: an owner of the flags and experiments (ADMIN, never a server's
      key), one non-superuser per role, a superuser, and a deactivated VIEWER;
      each with a hashed password nobody knows;
    * keys with ``mcp:read`` for the five role users; the bad keys of Q8 (one
      created and deleted, one expired, one of the deactivated user, one
      without the scope, one with ``sdk:ruleset`` only);
    * flags, in this order: Q15's three decoys, then its target; Q16's on
      (100%), off (INACTIVE) and US-targeted (0%, one rule country equals US
      at 100%) flags; 101 flags sharing one prefix (Q14, Q15's refusal);
    * an ACTIVE fixed-split A/B experiment with Bayesian analysis and
      sequential testing off, and a DRAFT one with no dates (the experiment
      scheduler leaves both alone).
    """
    _backend()
    from backend.app.core.security import get_password_hash, hash_api_key
    from backend.app.db.session import SessionLocal
    from backend.app.models import register_core_models
    from backend.app.models.api_key import APIKey, generate_api_key
    from backend.app.models.experiment import (
        Experiment,
        ExperimentStatus,
        ExperimentType,
        Metric,
        MetricType,
        Variant,
    )
    from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
    from backend.app.models.user import User, UserRole

    register_core_models()
    run = Run(tag=tag)
    db = SessionLocal()
    try:
        password = get_password_hash(secrets.token_urlsafe(24))
        users: Dict[str, Any] = {}

        def user(
            label: str, role: Any, superuser: bool = False, active: bool = True
        ) -> Any:
            person = User(
                username=f"mcp-contract-{label}-{tag}",
                email=f"mcp-contract-{label}-{tag}@example.com",
                hashed_password=password,
                role=role,
                is_superuser=superuser,
                is_active=active,
                first_name="MCP contract",
                last_name=label,
            )
            db.add(person)
            db.flush()
            users[label] = person
            return person

        owner = user("owner", UserRole.ADMIN)
        roles = {
            "ADMIN": user("admin", UserRole.ADMIN),
            "VIEWER": user("viewer", UserRole.VIEWER),
            "ANALYST": user("analyst", UserRole.ANALYST),
            "DEVELOPER": user("developer", UserRole.DEVELOPER),
            "SUPERUSER": user("superuser", UserRole.ADMIN, superuser=True),
        }
        inactive = user("inactive", UserRole.VIEWER, active=False)
        run.actor_ids = [person.id for person in roles.values()] + [inactive.id]

        def key(
            label: str, person: Any, scopes: str, expires_at: Optional[datetime] = None
        ) -> Any:
            plaintext = generate_api_key()
            row = APIKey(
                user_id=person.id,
                key=hash_api_key(plaintext),
                name=f"mcp-contract-{label}-{tag}"[:100],
                description="MCP server contract suite",
                scopes=scopes,
                is_active=True,
                expires_at=expires_at,
            )
            db.add(row)
            db.flush()
            run.keys[label] = plaintext
            return row

        for role, person in roles.items():
            key(role, person, "mcp:read")
        db.delete(key("deleted", roles["VIEWER"], "mcp:read"))
        # Naive UTC, as the column is compared with utcnow (models/api_key.py).
        yesterday = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
        key("expired", roles["VIEWER"], "mcp:read", expires_at=yesterday)
        key("deactivated owner", inactive, "mcp:read")
        key("no mcp:read", roles["VIEWER"], "read,write")
        key("sdk:ruleset only", roles["DEVELOPER"], "sdk:ruleset")

        def flag(
            key_: str,
            name: str,
            status: Any = FeatureFlagStatus.ACTIVE,
            rollout: int = 0,
            rules: Optional[dict] = None,
        ) -> None:
            db.add(
                FeatureFlag(
                    key=key_,
                    name=name[:100],
                    description="MCP server contract suite",
                    status=status,
                    owner_id=owner.id,
                    rollout_percentage=rollout,
                    targeting_rules=rules or {},
                    tags=[f"mcp-{tag}"],
                )
            )
            # One flush per flag: the decoys are stored before the target.
            db.flush()

        flag(run.flag("decoy v2"), "MCP contract decoy v2")
        flag(run.flag("decoy wildcard"), "MCP contract decoy wildcard")
        flag(run.flag("decoy named"), run.flag("target"))
        flag(run.flag("target"), "MCP contract target")
        flag(run.flag("on"), "MCP contract on", rollout=100)
        flag(
            run.flag("off"),
            "MCP contract off",
            status=FeatureFlagStatus.INACTIVE,
            rollout=100,
        )
        flag(
            run.flag("us"),
            "MCP contract US only",
            rollout=0,
            rules={
                "logical_operator": "AND",
                "rollout_percentage": 100,
                "groups": [
                    {
                        "id": "grp-country-us",
                        "logical_operator": "AND",
                        "conditions": [
                            {
                                "id": "cond-country",
                                "attribute": "country",
                                "operator": "equals",
                                "value": "US",
                            }
                        ],
                    }
                ],
            },
        )
        for index in range(BULK_FLAGS):
            flag(f"{run.flag('bulk')}_{index:03d}", f"MCP contract bulk {index:03d}")

        now = datetime.now(timezone.utc)

        def experiment(
            label: str, status: Any, start: Optional[datetime], end: Optional[datetime]
        ) -> None:
            row = Experiment(
                key=f"mcp_{label}_{tag}",
                name=f"MCP contract {label} {tag}",
                description="MCP server contract suite",
                hypothesis="The MCP server reads this experiment as the API serves it.",
                status=status,
                experiment_type=ExperimentType.A_B,
                owner_id=owner.id,
                start_date=start,
                end_date=end,
                targeting_rules={},
                metrics={"primary_metric": "purchase", "metric_type": "conversion"},
                tags=[f"mcp-{tag}"],
                optimization_type="fixed",
                bayesian_enabled=False,
                sequential_testing_enabled=False,
            )
            db.add(row)
            db.flush()
            db.add_all(
                [
                    Variant(
                        experiment_id=row.id,
                        name="control",
                        description="Control",
                        is_control=True,
                        traffic_allocation=50,
                        configuration={"color": "blue"},
                    ),
                    Variant(
                        experiment_id=row.id,
                        name="treatment",
                        description="Treatment",
                        is_control=False,
                        traffic_allocation=50,
                        configuration={"color": "green"},
                    ),
                    Metric(
                        experiment_id=row.id,
                        name="Purchase",
                        event_name="purchase",
                        metric_type=MetricType.CONVERSION,
                        is_primary=True,
                        minimum_sample_size=100,
                    ),
                ]
            )
            run.experiment_ids[label] = row.id
            run.experiment_keys[label] = row.key

        experiment(
            "experiment",
            ExperimentStatus.ACTIVE,
            now - timedelta(days=1),
            now + timedelta(days=365),
        )
        experiment("draft", ExperimentStatus.DRAFT, None, None)
        db.commit()
    finally:
        db.close()
    return run


def db_state(run: Run) -> Dict[str, Any]:
    """What Q4 compares, read through the backend's own session."""
    _backend()
    from backend.app.db.session import SessionLocal
    from backend.app.models import register_core_models
    from backend.app.models.analysis_snapshot import AnalysisSnapshot
    from backend.app.models.api_key import APIKey
    from backend.app.models.audit_log import AuditLog
    from backend.app.models.metrics.metric import MetricType, RawMetric
    from backend.app.models.user import User

    register_core_models()
    db = SessionLocal()
    try:
        snapshots = (
            db.query(AnalysisSnapshot)
            .filter(
                AnalysisSnapshot.experiment_id.in_(list(run.experiment_ids.values()))
            )
            .all()
        )
        return {
            "api_keys": db.query(APIKey).count(),
            "users": db.query(User).count(),
            "audit": db.query(AuditLog)
            .filter(AuditLog.user_id.in_(run.actor_ids))
            .count(),
            "evaluations": db.query(RawMetric)
            .filter(
                RawMetric.metric_type == MetricType.FLAG_EVALUATION.value,
                RawMetric.user_id.like(f"{run.canary}%"),
            )
            .count(),
            "snapshots": {
                (str(row.experiment_id), str(row.kind), row.as_of.isoformat()): (
                    row.updated_at.isoformat() if row.updated_at else ""
                )
                for row in snapshots
            },
        }
    finally:
        db.close()


def flag_count() -> int:
    """``SELECT count(*) FROM feature_flags`` (Q14)."""
    _backend()
    from backend.app.db.session import SessionLocal
    from backend.app.models import register_core_models
    from backend.app.models.feature_flag import FeatureFlag

    register_core_models()
    db = SessionLocal()
    try:
        return int(db.query(FeatureFlag).count())
    finally:
        db.close()


def missing_scope_detail() -> str:
    """The API's own 403 text for a key without ``mcp:read`` (E13)."""
    _backend()
    from backend.app.api.read_scope import MISSING_SCOPE_DETAIL

    return MISSING_SCOPE_DETAIL


# ---------------------------------------------------------------------------
# Q4 (pure)
# ---------------------------------------------------------------------------
def snapshot_problems(
    before: Dict[Tuple[str, str, str], str],
    after: Dict[Tuple[str, str, str], str],
    allowed: Dict[str, frozenset],
    names: Dict[str, str],
) -> List[str]:
    """Every analysis_snapshots row of this run's experiments written or
    removed during the MCP run must be one a results read writes."""
    problems = []
    for (experiment, kind, as_of), updated in sorted(after.items()):
        if before.get((experiment, kind, as_of)) == updated:
            continue
        kinds = allowed.get(experiment, frozenset())
        if kind not in kinds:
            expected = ", ".join(sorted(kinds)) or "none"
            problems.append(
                f"analysis_snapshots: a {kind} row for the {names.get(experiment, experiment)} "
                f"was written during the MCP run (expected: {expected})"
            )
    for experiment, kind, _as_of in sorted(set(before) - set(after)):
        problems.append(
            f"analysis_snapshots: a {kind} row for the {names.get(experiment, experiment)} "
            "was deleted during the MCP run"
        )
    return problems


def database_problems(
    before: Dict[str, Any],
    after: Dict[str, Any],
    evaluations: int,
    allowed: Dict[str, frozenset],
    names: Dict[str, str],
) -> List[str]:
    problems = []
    audit = after["audit"] - before["audit"]
    if audit:
        problems.append(
            f"audit_logs {audit:+d} for an MCP-run actor during the run (expected 0)"
        )
    recorded = after["evaluations"] - before["evaluations"]
    if recorded != evaluations:
        problems.append(
            f"raw_metrics: {recorded} FLAG_EVALUATION rows carry this run's user ids; "
            f"the MCP run made {evaluations} evaluations"
        )
    for table in ("api_keys", "users"):
        if after[table] != before[table]:
            problems.append(
                f"{table} {after[table] - before[table]:+d} rows during the MCP run (expected 0)"
            )
    problems.extend(
        snapshot_problems(before["snapshots"], after["snapshots"], allowed, names)
    )
    return problems


def wire_problems(records: Sequence[Recorded]) -> List[str]:
    """Q3: the routes called are exactly ALLOW_LIST."""
    seen = {(record.method, record.template) for record in records}
    problems = [
        f"{method} {template} is not in the read-only allow-list"
        for method, template in sorted(seen - set(ALLOW_LIST))
    ]
    problems.extend(
        f"{method} {template} was never called"
        for method, template in ALLOW_LIST
        if (method, template) not in seen
    )
    return problems


def role_differences(
    labels: Dict[int, str],
    shapes: Dict[int, Dict[str, Any]],
    tool_lists: Dict[str, List[str]],
) -> List[str]:
    """Q7: every role's answer to each case equals the reference role's."""
    problems = []
    for index, by_role in sorted(shapes.items()):
        if REFERENCE_ROLE not in by_role:
            continue
        for role in ROLES:
            if role == REFERENCE_ROLE or role not in by_role:
                continue
            where = first_difference(by_role[role], by_role[REFERENCE_ROLE])
            if where is not None:
                problems.append(
                    f"{labels[index]} as {role} differs from {REFERENCE_ROLE} at {where}"
                )
    for role, served in tool_lists.items():
        if served != tool_lists.get(REFERENCE_ROLE):
            problems.append(f"tools/list as {role} differs from {REFERENCE_ROLE}")
    return problems


def coverage_problems(
    served: Sequence[str], case_tools: Sequence[str], failed: Set[str]
) -> List[str]:
    """Q2: every tool the server serves has a case here, and passed it."""
    problems = []
    with_case = set(case_tools)
    for tool in served:
        if tool not in with_case:
            problems.append(f"tool {tool} is served but has no contract case")
        elif tool in failed:
            problems.append(f"tool {tool} failed a contract case (see the rows below)")
    for tool in sorted(with_case - set(served)):
        problems.append(f"tool {tool} has a contract case but is not served")
    return problems


def credential_problems(records: Sequence[Recorded]) -> List[str]:
    """Q6: exactly the configured key on every request, nothing else."""
    problems = []
    for index, record in enumerate(records, 1):
        where = f"request {index} ({record.method} {record.template})"
        if record.key == "none":
            problems.append(f"{where} carried no X-API-Key")
        elif record.key != "configured":
            problems.append(
                f"{where} carried a credential other than the configured one"
            )
        if record.authorization:
            problems.append(f"{where} carried Authorization")
        if record.cookie:
            problems.append(f"{where} carried a cookie")
    return problems


# ---------------------------------------------------------------------------
# The cases (one or more per tool; Q2)
# ---------------------------------------------------------------------------
@dataclass
class Context:
    run: Run
    role: str
    key: str
    origin: str
    api_url: str


def answer_json(result: ToolResult) -> Tuple[Any, List[str]]:
    if result.is_error:
        return None, [f"answered an error: {shown(result.text)}"]
    try:
        data = json.loads(result.text)
    except ValueError:
        return None, [f"answered text that is not JSON: {shown(result.text)}"]
    if not isinstance(data, dict):
        return None, [f"answered JSON that is not an object: {shown(result.text)}"]
    return data, []


def exact_error(expected: str) -> Callable[[Context, ToolResult], List[str]]:
    def check(_ctx: Context, result: ToolResult) -> List[str]:
        if not result.is_error:
            return [f"answered isError false: {shown(result.text)}"]
        if result.text != expected:
            return [f"answered {shown(result.text)!r}; expected {shown(expected)!r}"]
        return []

    return check


def item_fields(items: Sequence[Any], fields: frozenset) -> List[str]:
    for item in items:
        if not isinstance(item, dict) or set(item) != fields:
            got = sorted(item) if isinstance(item, dict) else type(item).__name__
            return [f"an item carries {got}; expected {sorted(fields)}"]
    return []


def direct(api_url: str, path: str, key: str) -> Tuple[int, Any]:
    """One GET straight to the API with *key* (not through a proxy)."""
    request = urllib.request.Request(
        f"{api_url}{path}", headers={"X-API-Key": key, "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=ANSWER_SECONDS) as response:
            status, payload = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, payload = exc.code, exc.read()
    try:
        return status, json.loads(payload) if payload else None
    except ValueError:
        return status, None


@dataclass
class Case:
    tool: str
    name: str
    #: The row a failure is reported under (Q2 for a case no other row owns).
    row: str
    arguments: Callable[[Run], dict]
    check: Callable[[Context, ToolResult], List[str]]
    #: What Q7 compares across roles.
    shape: Callable[[ToolResult], Any] = lambda result: (result.is_error, result.text)
    #: A successful answer recorded one evaluation (Q4).
    evaluates: bool = False


def _sorted_items(result: ToolResult, by: str) -> Any:
    try:
        data = json.loads(result.text)
    except ValueError:
        return (result.is_error, result.text)
    if not isinstance(data, dict):
        return data
    items = sorted(
        (item for item in data.get("items") or [] if isinstance(item, dict)),
        key=lambda item: str(item.get(by)),
    )
    return {**data, "items": items}


def _json_shape(result: ToolResult) -> Any:
    """The answer as JSON, so Q7 names the path where two roles differ."""
    try:
        data = json.loads(result.text)
    except ValueError:
        return {"is_error": result.is_error, "text": result.text}
    return {"is_error": result.is_error, "answer": data}


def _list_shape(result: ToolResult) -> Any:
    """Q14's page: only its total and length; the order of the rows is pinned elsewhere (#1612)."""
    try:
        data = json.loads(result.text)
    except ValueError:
        return (result.is_error, result.text)
    if not isinstance(data, dict):
        return data
    return {"total": data.get("total"), "items": len(data.get("items") or [])}


def _check_decoy_search(ctx: Context, result: ToolResult) -> List[str]:
    data, problems = answer_json(result)
    if problems:
        return problems
    items = data.get("items") or []
    expected = sorted(ctx.run.decoys() + [ctx.run.flag("target")])
    keys = sorted(str(item.get("key")) for item in items if isinstance(item, dict))
    if keys != expected or data.get("total") != len(expected):
        return [
            f"search {ctx.run.flag('target')!r} listed {keys} (total {data.get('total')!r}); expected {expected}"
        ]
    return item_fields(items, FLAG_LIST_FIELDS)


def _check_everything(ctx: Context, result: ToolResult) -> List[str]:
    data, problems = answer_json(result)
    if problems:
        return problems
    held = flag_count()
    items = data.get("items") or []
    if held <= PAGE:
        return [f"the database holds {held} flags; this check needs more than {PAGE}"]
    if data.get("total") != held:
        return [
            f"list_flags reported {len(items)} flags; the database holds {held} and the answer "
            f"does not say it is partial (total {data.get('total')!r})"
        ]
    if len(items) != PAGE:
        return [f"list_flags with limit {PAGE} answered {len(items)} of {held} flags"]
    return item_fields(items, FLAG_LIST_FIELDS)


def _check_target(ctx: Context, result: ToolResult) -> List[str]:
    data, problems = answer_json(result)
    if problems:
        return problems
    target = ctx.run.flag("target")
    if not isinstance(data, dict) or data.get("key") != target:
        found = data.get("key") if isinstance(data, dict) else data
        return [f"get_flag({target!r}) answered {found!r}"]
    return []


def _evaluation(
    flag: str, enabled: bool, reason: str
) -> Callable[[Context, ToolResult], List[str]]:
    def check(ctx: Context, result: ToolResult) -> List[str]:
        data, problems = answer_json(result)
        if problems:
            return problems
        key = ctx.run.flag(flag)
        if not isinstance(data, dict):
            return [f"evaluate {key!r} answered {shown(result.text)}"]
        if data.get("key") != key:
            problems.append(f"evaluate {key!r} answered for key {data.get('key')!r}")
        if data.get("enabled") is not enabled:
            problems.append(
                f"evaluate {key!r}: expected enabled {enabled}, got {data.get('enabled')!r}"
            )
        if data.get("reason") != reason:
            problems.append(
                f"evaluate {key!r}: expected reason {reason}, got {data.get('reason')!r}"
            )
        extra = set(data) - EVALUATION_FIELDS
        if extra:
            problems.append(
                f"evaluate {key!r} answered fields {sorted(extra)} beyond {sorted(EVALUATION_FIELDS)}"
            )
        return problems

    return check


def _check_experiment_search(
    ctx: Context, result: ToolResult, only: Optional[str] = None
) -> List[str]:
    data, problems = answer_json(result)
    if problems:
        return problems
    items = data.get("items") or []
    labels = [only] if only else ["experiment", "draft"]
    expected = sorted(str(ctx.run.experiment_ids[label]) for label in labels)
    ids = sorted(str(item.get("id")) for item in items if isinstance(item, dict))
    if ids != expected or data.get("total") != len(expected):
        return [
            f"list_experiments listed {ids} (total {data.get('total')!r}); expected {expected}"
        ]
    return item_fields(items, EXPERIMENT_LIST_FIELDS)


def _check_experiment(ctx: Context, result: ToolResult) -> List[str]:
    data, problems = answer_json(result)
    if problems:
        return problems
    expected = (
        str(ctx.run.experiment_ids["experiment"]),
        ctx.run.experiment_keys["experiment"],
    )
    found = (
        (data.get("id"), data.get("key")) if isinstance(data, dict) else (None, None)
    )
    return (
        []
        if found == expected
        else [f"get_experiment answered {found!r}; expected {expected!r}"]
    )


def _check_results(ctx: Context, result: ToolResult) -> List[str]:
    """Q17: the tool's answer is the API's own answer to the same key, exactly."""
    data, problems = answer_json(result)
    if problems:
        return problems
    experiment = ctx.run.experiment_ids["experiment"]
    status, served = direct(
        ctx.api_url, f"/api/v1/experiments/{experiment}/results", ctx.key
    )
    if status != 200:
        return [
            f"GET /api/v1/experiments/{{experiment_id}}/results answered the runner {status}"
        ]
    where = first_difference(data, served)
    return (
        []
        if where is None
        else [f"get_experiment_results differs from the API's answer at {where}"]
    )


def build_cases() -> List[Case]:
    """Every case each role's server runs, in order (Q2, Q7)."""
    return [
        Case(
            "list_feature_flags",
            "search past the decoys",
            "Q15",
            lambda run: {"search": run.flag("target"), "limit": PAGE},
            _check_decoy_search,
            lambda result: _sorted_items(result, "key"),
        ),
        Case(
            "list_feature_flags",
            "every flag, one page",
            "Q14",
            lambda run: {"limit": PAGE},
            _check_everything,
            _list_shape,
        ),
        Case(
            "get_feature_flag",
            "the exact key past decoys",
            "Q15",
            lambda run: {"flag_key": run.flag("target")},
            _check_target,
        ),
        Case(
            "get_feature_flag",
            "a key nothing has",
            "Q15",
            lambda run: {"flag_key": run.flag("absent")},
            lambda ctx, result: exact_error(
                f"Feature flag with key '{ctx.run.flag('absent')}' not found"
            )(ctx, result),
        ),
        Case(
            "get_feature_flag",
            "more than 100 matches, none exact",
            "Q15",
            lambda run: {"flag_key": run.flag("bulk")},
            lambda ctx, result: exact_error(
                f"More than {PAGE} feature flags match '{ctx.run.flag('bulk')}', and none of the first "
                f"{PAGE} has exactly that key, so this server cannot tell whether it exists. "
                "Find it in the dashboard."
            )(ctx, result),
        ),
        Case(
            "evaluate_feature_flag",
            "ACTIVE at 100%",
            "Q16",
            lambda run: {"flag_key": run.flag("on"), "user_id": f"{run.canary}on"},
            _evaluation("on", True, "rollout"),
            evaluates=True,
        ),
        Case(
            "evaluate_feature_flag",
            "INACTIVE",
            "Q16",
            lambda run: {"flag_key": run.flag("off"), "user_id": f"{run.canary}off"},
            _evaluation("off", False, "inactive"),
            evaluates=True,
        ),
        Case(
            "evaluate_feature_flag",
            "a rule on country, with the attribute",
            "Q16",
            lambda run: {
                "flag_key": run.flag("us"),
                "user_id": f"{run.canary}us",
                "context": {"country": "US"},
            },
            _evaluation("us", True, "targeting_rule"),
            evaluates=True,
        ),
        Case(
            "evaluate_feature_flag",
            "a rule on country, without it",
            "Q16",
            lambda run: {"flag_key": run.flag("us"), "user_id": f"{run.canary}us"},
            _evaluation("us", False, "rollout"),
            evaluates=True,
        ),
        Case(
            "evaluate_feature_flag",
            "a key nothing has",
            "Q16",
            lambda run: {
                "flag_key": run.flag("absent"),
                "user_id": f"{run.canary}absent",
            },
            lambda ctx, result: exact_error(
                f"Evaluating a feature flag failed: Feature flag with key '{ctx.run.flag('absent')}' "
                "not found (404)."
            )(ctx, result),
        ),
        Case(
            "list_experiments",
            "this run's experiments",
            "Q2",
            lambda run: {"search": run.tag, "limit": PAGE},
            _check_experiment_search,
            lambda result: _sorted_items(result, "id"),
        ),
        Case(
            "list_experiments",
            "status draft",
            "Q2",
            lambda run: {"search": run.tag, "status": "draft", "limit": PAGE},
            lambda ctx, result: _check_experiment_search(ctx, result, only="draft"),
            lambda result: _sorted_items(result, "id"),
        ),
        Case(
            "get_experiment",
            "the active experiment",
            "Q2",
            lambda run: {"experiment_id": str(run.experiment_ids["experiment"])},
            _check_experiment,
        ),
        Case(
            "get_experiment_results",
            "the active experiment, against the API",
            "Q17",
            lambda run: {"experiment_id": str(run.experiment_ids["experiment"])},
            _check_results,
            _json_shape,
        ),
        Case(
            "get_experiment_results",
            "a draft",
            "Q2",
            lambda run: {"experiment_id": str(run.experiment_ids["draft"])},
            exact_error(
                "Reading experiment results failed: Cannot get results for experiments in DRAFT status (400)."
            ),
        ),
    ]


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------
ROW_ORDER = (
    "setup",
    "Q9",
    "Q2",
    "Q3",
    "Q4",
    "Q5",
    "Q6",
    "Q7",
    "Q8",
    "Q10",
    "Q12",
    "Q14",
    "Q15",
    "Q16",
    "Q17",
    "Q18",
    "Q20",
)
ROW_TITLES = {
    "setup": "the suite could run",
    "Q9": "the API under test refuses a request with no credential",
    "Q2": "every served tool has a passing contract case",
    "Q3": "read-only at the wire",
    "Q4": "read-only at the database, scoped to this run",
    "Q5": "no write tool can be called",
    "Q6": "only the configured key is sent",
    "Q7": "the same answer for every role",
    "Q8": "a refused key is a tool error and the server stays up",
    "Q10": "no key in any output",
    "Q12": "stdout carries only JSON-RPC",
    "Q14": "the list says when it is partial",
    "Q15": "lookup by key is an exact match",
    "Q16": "evaluation answers are exact",
    "Q17": "results equal the API's",
    "Q18": "an input cannot change the route",
    "Q20": "the server exits when stdin closes",
}


class Report:
    def __init__(self) -> None:
        self.problems: Dict[str, List[str]] = {row: [] for row in ROW_ORDER}
        self.notes: Dict[str, str] = {}
        #: Rows that never ran, with why; they print NOT RUN, never PASS.
        self.not_run: Dict[str, str] = {}

    def stop_before(self, rows: Sequence[str], why: str) -> None:
        """Mark *rows* as never run (the suite stopped before them)."""
        for row in rows:
            self.not_run[row] = why

    def fail(self, row: str, message: str) -> None:
        self.problems.setdefault(row, []).append(message)

    def extend(self, row: str, messages: Sequence[str]) -> None:
        for message in messages:
            self.fail(row, message)

    def summary(self) -> int:
        """Print every row, PASS or FAIL with its problems; 1 if any failed."""
        failed = 0
        skipped = 0
        say("\nMCP contract suite")
        for row in ROW_ORDER:
            problems = self.problems.get(row) or []
            if not problems and row in self.not_run:
                skipped += 1
                say(f"  NOT RUN {row:<5} {ROW_TITLES[row]} ({self.not_run[row]})")
                continue
            status = "FAIL" if problems else "PASS"
            failed += bool(problems)
            note = f" ({self.notes[row]})" if row in self.notes and not problems else ""
            say(f"  {status} {row:<5} {ROW_TITLES[row]}{note}")
            for problem in problems[:25]:
                say(f"         - {problem}")
            if len(problems) > 25:
                say(f"         - ... and {len(problems) - 25} more")
        passed = len(ROW_ORDER) - failed - skipped
        say(f"\n{passed} passed, {failed} failed, {skipped} not run")
        return 1 if failed else 0


def node_problem() -> Tuple[Optional[str], Optional[str]]:
    """(the node executable, None), or (None, why the suite cannot run)."""
    node = shutil.which("node")
    if node is None:
        return (
            None,
            "node is not on PATH: the MCP contract suite needs it and never skips",
        )
    floor_match = re.search(
        r">=\s*(\d+)",
        json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))["engines"]["node"],
    )
    floor = int(floor_match.group(1)) if floor_match else 0
    try:
        version = subprocess.run(
            [node, "--version"], capture_output=True, text=True, timeout=30
        ).stdout.strip()
        major = int(version.lstrip("v").split(".", 1)[0])
    except (OSError, ValueError, subprocess.SubprocessError):
        return None, "node --version did not answer a version"
    if major < floor:
        return None, f"node {version} is below sdk/mcp's engines floor {floor}"
    if not BIN.is_file():
        return (
            None,
            f"{BIN.relative_to(REPO_ROOT)} is missing: run npm ci and npm run build in sdk/mcp first",
        )
    return node, None


def min_platform_version() -> str:
    found = re.search(
        r'MIN_PLATFORM_VERSION\s*=\s*"([^"]+)"', HTTP_SOURCE.read_text(encoding="utf-8")
    )
    if not found:
        raise RuntimeError("sdk/mcp/src/http.ts has no MIN_PLATFORM_VERSION")
    return found.group(1)


def e12(origin: str, version: str) -> str:
    return (
        f"The Controlvar API at {origin} did not accept the key in CONTROLVAR_API_KEY (401: Invalid API Key). "
        "It may have been deleted or have expired, or its owner's account may be deactivated; or the "
        f"instance is older than Controlvar {version} and does not accept API keys for this. "
        "Create a new key, or upgrade the instance."
    )


def _mask(values: Sequence[str]) -> None:
    """Ask GitHub Actions to mask each key in the job log (belt and braces:
    nothing here prints one)."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        for value in values:
            say(f"::add-mask::{value}")


def get_status(url: str, headers: Optional[Dict[str, str]] = None) -> int:
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


class Suite:
    def __init__(self, api_url: str, node: str, report: Report) -> None:
        self.api_url = api_url.rstrip("/")
        self.node = node
        self.report = report
        self.servers: List[Server] = []
        self.proxies: List[RecordingProxy] = []
        self.evaluations = 0

    def start(
        self, label: str, key: str, routes: RouteMap
    ) -> Tuple[Server, RecordingProxy]:
        proxy = RecordingProxy(self.api_url, key, routes).start()
        self.proxies.append(proxy)
        server = Server(label, self.node, proxy.origin, key)
        self.servers.append(server)
        return server, proxy

    def finish(self, server: Server) -> None:
        """Q20: close stdin; the server must be gone within EXIT_SECONDS, with code 0."""
        code = server.close()
        if code is None:
            self.report.fail(
                "Q20",
                f"the {server.label} server was still running {EXIT_SECONDS} s after stdin closed",
            )
        elif code != 0:
            self.report.fail(
                "Q20",
                f"the {server.label} server exited with code {code} after stdin closed",
            )

    def run_roles(self, run: Run, routes: RouteMap) -> List[str]:
        cases = build_cases()
        shapes: Dict[int, Dict[str, Any]] = {}
        tool_lists: Dict[str, List[str]] = {}
        failed_tools: Set[str] = set()
        for role in ROLES:
            server, proxy = self.start(role, run.keys[role], routes)
            try:
                server.initialize()
                served = server.tool_names()
                tool_lists[role] = served
                ctx = Context(
                    run=run,
                    role=role,
                    key=run.keys[role],
                    origin=proxy.origin,
                    api_url=self.api_url,
                )
                for index, case in enumerate(cases):
                    if case.tool not in served:
                        continue
                    result = server.call(case.tool, case.arguments(run))
                    try:
                        problems = case.check(ctx, result)
                    except Exception as exc:  # the case fails and says why
                        problems = [
                            f"its check raised {type(exc).__name__}: {shown(str(exc))}"
                        ]
                    if problems:
                        failed_tools.add(case.tool)
                        self.report.extend(
                            case.row,
                            [
                                f"{case.tool} ({case.name}) as {role}: {p}"
                                for p in problems
                            ],
                        )
                    if case.evaluates and not result.is_error:
                        self.evaluations += 1
                    shapes.setdefault(index, {})[role] = case.shape(result)
                if role == REFERENCE_ROLE:
                    self.write_tools(server, proxy)
                    self.confinement(server, proxy, run)
            except RuntimeError as exc:
                self.report.fail("setup", f"as {role}: {exc}")
            finally:
                self.finish(server)
        labels = {
            index: f"{case.tool} ({case.name})" for index, case in enumerate(cases)
        }
        self.report.extend("Q7", role_differences(labels, shapes, tool_lists))
        served = tool_lists.get(REFERENCE_ROLE, [])
        self.report.extend(
            "Q2", coverage_problems(served, [case.tool for case in cases], failed_tools)
        )
        if served:
            self.report.notes["Q2"] = (
                f"{len(served)} tools, {len(cases)} cases, {len(ROLES)} roles"
            )
        return served

    def write_tools(self, server: Server, proxy: RecordingProxy) -> None:
        """Q5: unknown to the server, and no request reaches the API."""
        for name in WRITE_TOOLS:
            before = proxy.count()
            result = server.call(name, {"flag_id": "x", "name": "x"})
            sent = proxy.count() - before
            if sent:
                self.report.fail(
                    "Q5",
                    f"tools/call {name} reached the API ({sent} request{'s' if sent != 1 else ''})",
                )
            if not result.is_error or f"Tool {name} not found" not in result.text:
                self.report.fail(
                    "Q5",
                    f"tools/call {name} answered {shown(result.text)!r}, not unknown-tool",
                )

    def confinement(self, server: Server, proxy: RecordingProxy, run: Run) -> None:
        """Q18: refused by the schema, with no request."""
        experiment = str(run.experiment_ids["experiment"])
        calls: List[Tuple[str, dict]] = []
        for bad in BAD_FLAG_KEYS:
            calls.append(("get_feature_flag", {"flag_key": bad}))
            calls.append(
                (
                    "evaluate_feature_flag",
                    {"flag_key": bad, "user_id": f"{run.canary}q18"},
                )
            )
        for bad in BAD_EXPERIMENT_IDS:
            value = bad.replace("{id}", experiment)
            calls.append(("get_experiment", {"experiment_id": value}))
            calls.append(("get_experiment_results", {"experiment_id": value}))
        for tool, arguments in calls:
            before = proxy.count()
            result = server.call(tool, arguments)
            sent = proxy.count() - before
            value = next(iter(arguments.values()))
            if sent:
                self.report.fail(
                    "Q18",
                    f"{tool} with {value!r} sent {sent} request(s); the schema must refuse it",
                )
            if not result.is_error:
                self.report.fail("Q18", f"{tool} with {value!r} answered isError false")
                if tool == "evaluate_feature_flag":
                    self.evaluations += 1
        self.report.notes["Q18"] = f"{len(calls)} inputs refused"

    def run_bad_keys(
        self, run: Run, routes: RouteMap, version: str, detail_403: str
    ) -> None:
        """Q8: each refused key is a tool error with its exact text, and the
        same process answers the next call."""
        experiment = str(run.experiment_ids["experiment"])
        for label, status in BAD_KEYS:
            server, proxy = self.start(f"'{label}' key", run.keys[label], routes)
            try:
                server.initialize()
                if status == 401:
                    first = second = e12(proxy.origin, version)
                else:
                    first = f"Listing feature flags failed: {detail_403} (403)."
                    second = f"Reading an experiment failed: {detail_403} (403)."
                result = server.call("list_feature_flags", {})
                if not result.is_error:
                    count = ""
                    try:
                        count = (
                            f"{len(json.loads(result.text).get('items') or [])} flags, "
                        )
                    except (ValueError, AttributeError):
                        pass
                    self.report.fail(
                        "Q8",
                        f"list_flags with the {label} key answered {count}isError false",
                    )
                elif result.text != first:
                    self.report.fail(
                        "Q8",
                        f"list_flags with the {label} key answered {shown(result.text)!r}; expected {shown(first)!r}",
                    )
                if not server.alive():
                    self.report.fail("Q8", f"the server exited after the {label} case")
                    continue
                result = server.call("get_experiment", {"experiment_id": experiment})
                if not result.is_error or result.text != second:
                    self.report.fail(
                        "Q8",
                        f"the next call with the {label} key answered {shown(result.text)!r}; expected {shown(second)!r}",
                    )
            except RuntimeError as exc:
                self.report.fail("Q8", f"the {label} case: {exc}")
            finally:
                self.finish(server)
        self.report.notes["Q8"] = f"{len(BAD_KEYS)} keys"

    def output_problems(self) -> None:
        """Q10 and Q12, over every server this run started."""
        for server in self.servers:
            self.report.extend(
                "Q12",
                jsonrpc_problems(f"the {server.label} server", server.stdout_lines),
            )
            outputs = {
                "stdout": server.stdout_lines,
                "stderr": server.stderr().split("\n"),
                "tool result": server.results,
            }
            for secret in _SECRETS:
                where = secret_location(secret, outputs)
                if where:
                    self.report.fail(
                        "Q10",
                        f"an API key appears in the {server.label} server's {where}",
                    )

    def stop(self) -> None:
        for server in self.servers:
            server.kill()
        for proxy in self.proxies:
            proxy.stop()


def prepare(
    api_url: str, report: Report
) -> Tuple[Run, RouteMap, str, str, Dict[str, Any]]:
    """Seed the run, read what the checks need, warm the results cache and
    take Q4's baseline. Raises when the suite cannot set itself up."""
    tag = secrets.token_hex(4)
    run = seed(tag)
    _SECRETS.extend(run.keys.values())
    _mask(list(run.keys.values()))
    with urllib.request.urlopen(
        f"{api_url}/api/v1/openapi.json", timeout=30
    ) as response:
        routes = RouteMap(json.loads(response.read()))
    version = min_platform_version()
    detail_403 = missing_scope_detail()
    # Warmed before the baseline, so every results read below, the tool's and
    # the runner's, is the one cached answer (the cache key has no user in it).
    experiment = run.experiment_ids["experiment"]
    warm, _ = direct(
        api_url, f"/api/v1/experiments/{experiment}/results", run.keys["SUPERUSER"]
    )
    if warm != 200:
        report.fail(
            "setup",
            f"GET /api/v1/experiments/{{experiment_id}}/results with an mcp:read key "
            f"answered {warm}",
        )
    before = db_state(run)
    report.notes["setup"] = f"tag {tag}, {len(routes)} routes in the API's document"
    return run, routes, version, detail_403, before


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--api-url",
        default=os.environ.get("CONTROLVAR_API_URL") or "http://127.0.0.1:8000",
        help="the API under test (default: $CONTROLVAR_API_URL)",
    )
    args = parser.parse_args(argv)
    api_url = args.api_url.rstrip("/")
    report = Report()

    node, why = node_problem()
    if node is None:
        report.fail("setup", why or "node")
        report.stop_before([row for row in ROW_ORDER if row != "setup"], "setup failed")
        report.summary()
        return 2

    # Q9, before any case.
    status = get_status(f"{api_url}/api/v1/feature-flags/")
    if status != 401:
        if status == 200:
            report.fail(
                "Q9",
                "the API under test admits a request with no credential: "
                "the dev bypass is on",
            )
        else:
            report.fail(
                "Q9",
                f"GET /api/v1/feature-flags/ with no credential answered {status}, "
                "expected 401",
            )
        report.stop_before([row for row in ROW_ORDER if row != "Q9"], "Q9 failed")
        return report.summary()
    report.notes["Q9"] = "401"

    suite = Suite(api_url, node, report)
    try:
        try:
            run, routes, version, detail_403, before = prepare(api_url, report)
        except Exception as exc:  # the suite could not set itself up: never a skip
            report.fail("setup", f"{type(exc).__name__}: {shown(str(exc))}")
            report.stop_before(
                [row for row in ROW_ORDER if row not in ("setup", "Q9")],
                "setup failed",
            )
            report.summary()
            return 2

        served = suite.run_roles(run, routes)
        suite.run_bad_keys(run, routes, version, detail_403)

        names = {
            str(identifier): f"{label} ({run.experiment_keys[label]})"
            for label, identifier in run.experiment_ids.items()
        }
        allowed = {
            str(run.experiment_ids["experiment"]): RESULTS_SNAPSHOT_KINDS,
            str(run.experiment_ids["draft"]): frozenset(),
        }
        try:
            after = db_state(run)
        except Exception as exc:  # a failure of this row, never a skip
            report.fail(
                "Q4", f"could not read the database after the run: {type(exc).__name__}"
            )
        else:
            report.extend(
                "Q4",
                database_problems(before, after, suite.evaluations, allowed, names),
            )
        report.notes["Q4"] = (
            f"{suite.evaluations} evaluations, audit_logs +0, api_keys and users unchanged"
        )

        records = [record for proxy in suite.proxies for record in proxy.records]
        report.extend("Q3", wire_problems(records))
        report.extend("Q6", credential_problems(records))
        report.notes["Q3"] = f"{len(records)} requests, {len(ALLOW_LIST)} routes"
        suite.output_problems()
        if not served:
            report.fail("Q2", "no server answered tools/list")
    finally:
        suite.stop()
    return report.summary()


if __name__ == "__main__":
    sys.exit(main())
