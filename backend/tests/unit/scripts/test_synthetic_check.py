"""The synthetic staging check, against a stub API on 127.0.0.1.

``scripts/synthetic_check.py`` runs eight steps against staging every 30
minutes (``.github/workflows/synthetic.yml``). Pinned here, against a stub
HTTP server started by the test:

* the request list: the check sends nothing but its eight requests, and the
  client refuses every other route before anything is sent (a planted extra
  route fails these tests by name);
* the eight steps pass against a healthy stub, sending exactly those requests;
  each step fails on a 5xx at that step, and stops the check there;
* a sentinel the stub puts in every answer, every error body and every
  credential appears in none of: the check's stdout and stderr, its outputs,
  the rendered issue file and the step summary;
* the account is judged before anything is written; the key's expiry warns and
  fails; the results must move by exactly +1/+1; a redirect is not followed;
* a missing input fails by its name.
"""

from __future__ import annotations

import ast
import datetime as dt
import io
import json
import re
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import synthetic_check as sc
import synthetic_report as sr

#: The eight steps' requests, (method, route): the whole list, pinned.
EIGHT_STEPS_REQUESTS = (
    ("GET", "/health/ready"),
    ("POST", "/api/v1/auth/login"),
    ("GET", "/api/v1/auth/me"),
    ("GET", "/api/v1/api-keys"),
    ("GET", "/api/v1/results/{experiment_id}"),
    ("POST", "/api/v1/tracking/assign"),
    ("POST", "/api/v1/tracking/track"),
    ("GET", "/api/v1/feature-flags/evaluate/{flag_key}"),
)

#: Routes the check must never send. The SDK routes beside the ones it uses
#: are named one by one, so a later edit cannot add one by accident.
REFUSED = [
    ("PUT", "/api/v1/experiments/{experiment_id}"),
    ("DELETE", "/api/v1/experiments/{experiment_id}"),
    ("POST", "/api/v1/experiments/{experiment_id}/pause"),
    ("PUT", "/api/v1/feature-flags/{flag_id}"),
    ("POST", "/api/v1/feature-flags/{flag_id}/toggle"),
    ("POST", "/api/v1/api-keys"),
    ("DELETE", "/api/v1/api-keys/{key_id}"),
    ("POST", "/api/v1/tracking/errors"),
    ("POST", "/api/v1/tracking/errors/batch"),
    ("POST", "/api/v1/tracking/batch"),
    ("POST", "/api/v1/tracking/assign/batch"),
    ("POST", "/api/v1/tracking/events"),
    ("POST", "/api/v1/feature-flags/evaluate/{flag_key}"),
    ("GET", "/api/v1/feature-flags/user/{user_id}"),
    ("GET", "/api/v1/sdk/ruleset"),
    ("POST", "/api/v1/results/{experiment_id}/invalidate-cache"),
    ("POST", "/api/v1/auth/logout"),
    ("GET", "/api/v1/users/"),
    ("GET", "/health/ready/"),
    ("get", "/health/ready"),
]

SENTINEL = "SENTINEL-" + uuid.uuid4().hex[:12]
TOKEN = "tok-" + SENTINEL + "-token"
API_KEY = "key-" + SENTINEL + "-apikey"
PASSWORD = "pw-" + SENTINEL + "-password"
EMAIL = SENTINEL.lower() + "@example.com"
EXPERIMENT_ID = str(uuid.uuid4())
RUN_ID = "37456943504"
USER_ID = f"synth-{RUN_ID}-1"
SECRET_SHAPES = (
    SENTINEL,
    TOKEN,
    API_KEY,
    PASSWORD,
    EMAIL,
    EXPERIMENT_ID,
    USER_ID,
    RUN_ID,
)

ROUTE_PATTERNS = [
    (re.compile(r"/health/ready"), ("GET", "/health/ready")),
    (re.compile(r"/api/v1/auth/login"), ("POST", "/api/v1/auth/login")),
    (re.compile(r"/api/v1/auth/me"), ("GET", "/api/v1/auth/me")),
    (re.compile(r"/api/v1/api-keys"), ("GET", "/api/v1/api-keys")),
    (re.compile(r"/api/v1/results/[^/]+"), ("GET", "/api/v1/results/{experiment_id}")),
    (re.compile(r"/api/v1/tracking/assign"), ("POST", "/api/v1/tracking/assign")),
    (re.compile(r"/api/v1/tracking/track"), ("POST", "/api/v1/tracking/track")),
    (
        re.compile(r"/api/v1/feature-flags/evaluate/[^/]+"),
        ("GET", "/api/v1/feature-flags/evaluate/{flag_key}"),
    ),
]


def _route(method: str, path: str) -> Tuple[str, str]:
    for pattern, (route_method, route) in ROUTE_PATTERNS:
        if pattern.fullmatch(path) and route_method == method:
            return method, route
    return method, path


class Stub:
    """A stub of the API's eight routes. Every answer carries the sentinel.

    ``fail_step`` makes that step's request answer 503 with the sentinel as
    its body; the other knobs make a healthy answer wrong in one way."""

    def __init__(
        self,
        fail_step: Optional[int] = None,
        role: str = "ANALYST",
        superuser: bool = False,
        keys: Optional[List[dict]] = None,
        reason: str = "rollout",
        convert: bool = True,
        redirect_ready: bool = False,
    ) -> None:
        self.fail_step = fail_step
        self.role = role
        self.superuser = superuser
        self.keys = keys
        self.reason = reason
        self.convert = convert
        self.redirect_ready = redirect_ready
        self.seen: List[Tuple[str, str]] = []
        self.steps: List[int] = []
        self.headers: List[Dict[str, str]] = []
        self.assigned: Dict[str, str] = {}
        self.converted: set = set()
        self.results_reads = 0
        self.lock = threading.Lock()

    def step_of(self, route: Tuple[str, str]) -> int:
        method, path = route
        if path == "/api/v1/results/{experiment_id}":
            return 4 if self.results_reads == 0 else 8
        return {
            "/health/ready": 1,
            "/api/v1/auth/login": 2,
            "/api/v1/auth/me": 2,
            "/api/v1/api-keys": 3,
            "/api/v1/tracking/assign": 5,
            "/api/v1/tracking/track": 6,
            "/api/v1/feature-flags/evaluate/{flag_key}": 7,
        }.get(path, 0)

    def answer(self, method: str, raw_path: str, headers: Dict[str, str], body: bytes):
        parts = urlsplit(raw_path)
        route = _route(method, parts.path)
        with self.lock:
            self.seen.append(route)
            self.headers.append(headers)
            step = self.step_of(route)
            self.steps.append(step)
            if step == self.fail_step:
                if route[1] == "/api/v1/results/{experiment_id}":
                    self.results_reads += 1
                return 503, f"upstream {SENTINEL} {TOKEN} trace".encode(), "text/plain"
            return self._healthy(route, parts, headers, body)

    def _json(self, status: int, data) -> Tuple[int, bytes, str]:
        return status, json.dumps(data).encode(), "application/json"

    def _healthy(self, route, parts, headers, body):
        method, path = route
        query = parse_qs(parts.query)
        if path == "/health/ready":
            if self.redirect_ready:
                return 307, b"", "text/plain"
            return self._json(200, {"status": "ready", "detail": SENTINEL})
        if path == "/api/v1/auth/login":
            data = json.loads(body)
            if data != {"email": EMAIL, "password": PASSWORD}:
                return self._json(401, {"detail": SENTINEL})
            return self._json(200, {"access_token": TOKEN, "user": {"email": EMAIL}})
        if headers.get("authorization") == f"Bearer {TOKEN}":
            if path == "/api/v1/auth/me":
                return self._json(
                    200,
                    {
                        "id": EXPERIMENT_ID,
                        "email": EMAIL,
                        "username": SENTINEL,
                        "role": self.role,
                        "is_superuser": self.superuser,
                    },
                )
            if path == "/api/v1/api-keys":
                later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=80)
                keys = self.keys
                if keys is None:
                    keys = [
                        {
                            "id": SENTINEL,
                            "name": SENTINEL,
                            "is_active": True,
                            "expires_at": later.isoformat(),
                        }
                    ]
                return self._json(200, keys)
            if path == "/api/v1/results/{experiment_id}":
                assert query == {"use_cache": ["false"]}, query
                assert parts.path.endswith(EXPERIMENT_ID)
                self.results_reads += 1
                total, done = 1000 + len(self.assigned), 400 + len(self.converted)
                return self._json(
                    200,
                    {
                        "experiment_name": SENTINEL,
                        "metrics": [
                            {
                                "metric_name": SENTINEL,
                                "metric_type": "conversion",
                                "is_primary": True,
                                "variants": [
                                    {
                                        "variant_name": SENTINEL,
                                        "sample_size": total - 500,
                                        "conversions": done - 200,
                                    },
                                    {
                                        "variant_name": SENTINEL,
                                        "sample_size": 500,
                                        "conversions": 200,
                                    },
                                ],
                            },
                            {
                                "metric_type": "revenue",
                                "is_primary": False,
                                "variants": [],
                            },
                        ],
                    },
                )
            return self._json(404, {"detail": SENTINEL})
        if headers.get("x-api-key") == API_KEY:
            if path == "/api/v1/tracking/assign":
                data = json.loads(body)
                user = data["user_id"]
                self.assigned.setdefault(user, "variant-" + SENTINEL)
                return self._json(
                    200,
                    {
                        "variant_id": self.assigned[user],
                        "assigned": True,
                        "user_id": user,
                        "variant_name": SENTINEL,
                        "reason": "assigned",
                    },
                )
            if path == "/api/v1/tracking/track":
                data = json.loads(body)
                if self.convert and data["user_id"] in self.assigned:
                    self.converted.add(data["user_id"])
                return self._json(200, {"id": SENTINEL, "user_id": data["user_id"]})
            if path == "/api/v1/feature-flags/evaluate/{flag_key}":
                return self._json(
                    200, {"key": SENTINEL, "enabled": True, "reason": self.reason}
                )
        return self._json(401, {"detail": SENTINEL})


def _handler(stub: Stub):
    class Handler(BaseHTTPRequestHandler):
        def _serve(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            headers = {k.lower(): v for k, v in self.headers.items()}
            status, payload, ctype = stub.answer(self.command, self.path, headers, body)
            self.send_response(status)
            if status in (301, 302, 307, 308):
                self.send_header("Location", "http://127.0.0.1:9/elsewhere")
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _serve

        def log_message(self, *args):  # keep the test output quiet
            pass

    return Handler


@pytest.fixture
def serve():
    servers = []

    def start(stub: Stub) -> str:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(stub))
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        thread.start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def env_for(base_url: str, **overrides: str) -> Dict[str, str]:
    env = {
        "SYNTH_ENABLED": "true",
        "PUBLIC_BASE_URL": base_url,
        "SYNTH_EMAIL": EMAIL,
        "SYNTH_PASSWORD": PASSWORD,
        "SYNTH_API_KEY": API_KEY,
        "SYNTH_EXPERIMENT_ID": EXPERIMENT_ID,
        "SYNTH_EXPERIMENT_KEY": "synthetic-canary",
        "SYNTH_EVENT_NAME": "synthetic_conversion",
        "SYNTH_FLAG_KEY": "synthetic-canary-flag",
        "GITHUB_RUN_ID": RUN_ID,
        "GITHUB_RUN_ATTEMPT": "1",
    }
    env.update(overrides)
    return env


def run_in_process(env: Dict[str, str]) -> Tuple[int, str]:
    out = io.StringIO()
    code = sc.main(env, out=out, allow_loopback_http=True)
    return code, out.getvalue()


# ---------------------------------------------------------------------------
# The request list
# ---------------------------------------------------------------------------


def test_the_request_list_is_exactly_the_eight_steps_requests():
    extra = sorted(set(sc.REQUESTS) - set(EIGHT_STEPS_REQUESTS))
    missing = sorted(set(EIGHT_STEPS_REQUESTS) - set(sc.REQUESTS))
    assert not extra, f"requests outside the eight steps: {extra}"
    assert not missing, f"steps' requests missing from the list: {missing}"
    assert len(sc.REQUESTS) == len(set(sc.REQUESTS)) == 8


@pytest.mark.parametrize("method, route", REFUSED)
def test_every_other_route_is_refused_before_anything_is_sent(serve, method, route):
    stub = Stub()
    client = sc.Client(serve(stub))
    with pytest.raises(sc.RouteRefused):
        client.send(method, route, params={"experiment_id": EXPERIMENT_ID})
    assert stub.seen == []


def test_the_tracking_errors_routes_are_refused():
    """The check sends nothing but its eight requests."""
    client = sc.Client("http://127.0.0.1:9")
    for route in ("/api/v1/tracking/errors", "/api/v1/tracking/errors/batch"):
        assert ("POST", route) not in sc.REQUESTS
        with pytest.raises(sc.RouteRefused):
            client.send("POST", route, body={"error": "x"})


def test_no_route_reaches_the_script_except_through_the_list():
    """Every path literal in the script is one of its routes, and the opener
    is called in one place only (``Client.send``, behind the check)."""
    tree = ast.parse((SCRIPTS / "synthetic_check.py").read_text())
    routes = {route for _, route in sc.REQUESTS}
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and re.match(r"/(api|health)\b", node.value)
    }
    assert literals <= routes, (
        f"path literals outside the list: {sorted(literals - routes)}"
    )
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr
        in ("open", "urlopen", "request", "HTTPConnection", "HTTPSConnection")
    ]
    assert len(calls) == 1 and ast.unparse(calls[0].func) == "self._opener.open"
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in (
            node.names
            if isinstance(node, ast.Import)
            else [ast.alias(node.module or "")]
        )
    }
    assert imported <= set(sys.stdlib_module_names) | {"__future__"}, imported
    assert "socket" not in imported and "backend" not in imported


# ---------------------------------------------------------------------------
# The eight steps
# ---------------------------------------------------------------------------


def test_eight_steps_pass_and_send_exactly_the_eight_requests(serve, tmp_path):
    stub = Stub()
    outputs = tmp_path / "out"
    code, out = run_in_process(env_for(serve(stub), GITHUB_OUTPUT=str(outputs)))
    assert code == 0, out
    assert out.endswith("ran 8 of 8\n")
    assert outputs.read_text() == "ran=8\n"
    # In order: ready, login, me, keys, results, assign twice, track,
    # evaluate, results.
    assert stub.seen == [
        ("GET", "/health/ready"),
        ("POST", "/api/v1/auth/login"),
        ("GET", "/api/v1/auth/me"),
        ("GET", "/api/v1/api-keys"),
        ("GET", "/api/v1/results/{experiment_id}"),
        ("POST", "/api/v1/tracking/assign"),
        ("POST", "/api/v1/tracking/assign"),
        ("POST", "/api/v1/tracking/track"),
        ("GET", "/api/v1/feature-flags/evaluate/{flag_key}"),
        ("GET", "/api/v1/results/{experiment_id}"),
    ]
    assert set(stub.seen) == set(EIGHT_STEPS_REQUESTS)
    # The key travels only on the SDK routes, the token only on the others.
    for (method, route), headers in zip(stub.seen, stub.headers):
        sdk = route.startswith(("/api/v1/tracking/", "/api/v1/feature-flags/"))
        assert ("x-api-key" in headers) == sdk, route
        assert ("authorization" in headers) == (
            route
            in (
                "/api/v1/auth/me",
                "/api/v1/api-keys",
                "/api/v1/results/{experiment_id}",
            )
        ), route
    assert list(stub.assigned) == [USER_ID]


@pytest.mark.parametrize("step", range(1, 9))
def test_a_5xx_at_each_step_stops_the_check_there(serve, tmp_path, step):
    stub = Stub(fail_step=step)
    outputs = tmp_path / "out"
    code, out = run_in_process(env_for(serve(stub), GITHUB_OUTPUT=str(outputs)))
    assert code == 1
    assert f"failed at step {step} ({sc.STEPS[step - 1]}): the API answered 503" in out
    assert out.endswith(f"ran {step - 1} of 8\n")
    assert outputs.read_text() == f"ran={step - 1}\nstep={step}\nstatus=503\n"
    # Nothing after the failing step was sent: it was the last request.
    assert stub.steps[-1] == step and max(stub.steps) == step


def test_the_account_is_judged_before_anything_is_written(serve):
    for stub, reason in (
        (Stub(role="DEVELOPER"), "is not an ANALYST"),
        (Stub(superuser=True), "is a superuser"),
    ):
        code, out = run_in_process(env_for(serve(stub)))
        assert code == 1
        assert f"failed at step 2 (Sign in): the synthetic account {reason}" in out
        assert out.endswith("ran 1 of 8\n")
        assert not [
            route
            for route in stub.seen
            if route[0] != "GET" and "login" not in route[1]
        ]


def _key(days: Optional[float], active: bool = True) -> dict:
    when = None
    if days is not None:
        when = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)).isoformat()
    return {"id": SENTINEL, "name": SENTINEL, "is_active": active, "expires_at": when}


@pytest.mark.parametrize(
    "keys, code, text",
    [
        ([_key(80)], 0, None),
        ([_key(10)], 0, "::warning title=Synthetic check::"),
        ([_key(2)], 1, "expires in fewer than 3 days"),
        ([_key(None)], 1, "has no expiry date"),
        ([_key(80), _key(80)], 1, "holds 2 active API keys; it must hold exactly one"),
        ([_key(80, active=False)], 1, "holds 0 active API keys"),
        ([_key(80), _key(1, active=False)], 0, None),
    ],
)
def test_the_key_expiry_step(serve, keys, code, text):
    stub = Stub(keys=keys)
    got, out = run_in_process(env_for(serve(stub)))
    assert got == code, out
    if text:
        assert text in out
    if code:
        assert "failed at step 3 (Key expiry)" in out
    else:
        assert "::warning" in out if text else "::warning" not in out


@pytest.mark.parametrize(
    "reason, ok",
    [
        ("rollout", True),
        ("targeting_rule", True),
        ("inactive", False),
        ("error", False),
        (SENTINEL, False),
    ],
)
def test_the_flag_must_be_evaluated(serve, reason, ok):
    code, out = run_in_process(env_for(serve(Stub(reason=reason))))
    assert (code == 0) is ok, out
    if not ok:
        named = reason if reason in ("inactive", "error") else "not documented"
        assert f"not evaluated for the user (reason: {named})" in out


def test_the_results_must_move_by_exactly_one_and_one(serve):
    code, out = run_in_process(env_for(serve(Stub(convert=False))))
    assert code == 1
    assert "failed at step 8 (Results after)" in out
    assert "the results show +1 and +0" in out


def test_a_redirect_is_an_answer_not_followed(serve):
    stub = Stub(redirect_ready=True)
    code, out = run_in_process(env_for(serve(stub)))
    assert code == 1
    assert "failed at step 1 (Ready): the API answered 307" in out
    assert stub.seen == [("GET", "/health/ready")]


def test_no_answer_is_a_failure_with_status_no_answer(tmp_path):
    outputs = tmp_path / "out"
    code, out = run_in_process(
        env_for("http://127.0.0.1:9", GITHUB_OUTPUT=str(outputs))
    )
    assert code == 1
    assert "failed at step 1 (Ready): no answer within 15 s" in out
    assert outputs.read_text() == "ran=0\nstep=1\nstatus=no answer\n"


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

INPUTS = (*sc.SECRETS, *sc.VARIABLES, *sc.RUN_INPUTS)


@pytest.mark.parametrize("name", INPUTS)
def test_a_missing_input_fails_by_its_name(tmp_path, name):
    outputs = tmp_path / "out"
    env = env_for("https://staging.example.com", GITHUB_OUTPUT=str(outputs))
    del env[name]
    code, out = run_in_process(env)
    assert code == 1
    assert f"::error title=Synthetic check::{name} is missing" in out
    step = sc.NEEDED_AT[name]
    assert f"failed at step {step} " in out
    assert outputs.read_text() == f"ran=0\nstep={step}\nstatus=no answer\n"


@pytest.mark.parametrize("value", ["", "false", "TRUE", "yes", "True"])
def test_the_check_runs_only_when_enabled_is_exactly_true(value):
    env = env_for("https://staging.example.com", SYNTH_ENABLED=value)
    code, out = run_in_process(env)
    assert code == 1
    assert "SYNTH_ENABLED must be exactly true" in out


@pytest.mark.parametrize(
    "url",
    [
        "http://staging.example.com",
        "https://staging.example.com/api",
        "https://user:pass@staging.example.com",
        "https://staging.example.com/?x=1",
        "https://staging.example.com/#x",
        "ftp://staging.example.com",
        "https://",
        "https://bad_host.example.com",
    ],
)
def test_the_base_url_must_be_an_https_origin(url):
    code, out = run_in_process(env_for("https://x.example.com", PUBLIC_BASE_URL=url))
    assert code == 1
    assert "::error title=Synthetic check::PUBLIC_BASE_URL" in out
    if url != "https://":  # the refusal's own wording names the scheme
        assert url not in out.replace("PUBLIC_BASE_URL", "")


def test_loopback_http_is_only_for_the_tests():
    env = env_for("http://127.0.0.1:8080")
    with pytest.raises(sc.ConfigError):
        sc.load_config(env)
    assert (
        sc.load_config(env, allow_loopback_http=True).base_url
        == "http://127.0.0.1:8080"
    )
    assert sc.load_config(env_for("https://staging.example.com/")).base_url == (
        "https://staging.example.com"
    )


@pytest.mark.parametrize(
    "name, value",
    [
        ("SYNTH_EXPERIMENT_ID", "not-a-uuid"),
        ("SYNTH_EXPERIMENT_KEY", "a/b"),
        ("SYNTH_FLAG_KEY", "../x"),
        ("SYNTH_EVENT_NAME", "two words"),
        ("GITHUB_RUN_ID", "12a"),
    ],
)
def test_a_malformed_input_is_refused_by_its_name(name, value):
    code, out = run_in_process(env_for("https://staging.example.com", **{name: value}))
    assert code == 1
    assert f"::error title=Synthetic check::{name} " in out
    assert value not in out.replace(name, "")


# ---------------------------------------------------------------------------
# The sentinel: nothing from an answer or a credential reaches public text
# ---------------------------------------------------------------------------

RUNNER = """
import sys
sys.path.insert(0, {scripts!r})
import synthetic_check
sys.exit(synthetic_check.main(allow_loopback_http=True))
"""


def _fake_history(red_before: int):
    """A gh stand-in: ``red_before`` failed runs, then a pass, no open issue."""
    now = dt.datetime.now(dt.timezone.utc)
    runs = [
        {
            "databaseId": 1000 + i,
            "createdAt": (now - dt.timedelta(minutes=30 * (i + 1))).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "status": "completed",
            "conclusion": "failure" if i < red_before else "success",
        }
        for i in range(red_before + 1)
    ]

    def jobs(conclusion):
        steps = [{"name": sr.CHECK_STEP, "conclusion": conclusion}]
        if conclusion == "success":
            steps.append({"name": sr.MARKER_STEP, "conclusion": "success"})
        return {
            "jobs": [{"name": sr.CHECK_JOB, "conclusion": conclusion, "steps": steps}]
        }

    def gh(args):
        if args[:2] == ["run", "list"]:
            return runs
        if args[:2] == ["run", "view"]:
            run = next(r for r in runs if str(r["databaseId"]) == args[2])
            return jobs(run["conclusion"])
        if args[:2] == ["issue", "list"]:
            return []
        raise AssertionError(args)

    return gh


@pytest.mark.parametrize("step", range(1, 9))
def test_the_sentinel_reaches_no_public_text(serve, tmp_path, step):
    stub = Stub(fail_step=step)
    outputs, summary = tmp_path / "outputs", tmp_path / "step-summary"
    env = env_for(
        serve(stub), GITHUB_OUTPUT=str(outputs), GITHUB_STEP_SUMMARY=str(summary)
    )
    done = subprocess.run(
        [sys.executable, "-c", RUNNER.format(scripts=str(SCRIPTS))],
        env={"PATH": "/usr/bin:/bin", **env},
        capture_output=True,
        text=True,
        timeout=120,
        cwd=tmp_path,
    )
    assert done.returncode == 1, done.stdout + done.stderr
    assert done.stdout.endswith(f"ran {step - 1} of 8\n")
    produced = {
        "stdout": done.stdout,
        "stderr": done.stderr,
        "outputs": outputs.read_text(),
    }
    # The check itself writes no step summary.
    assert not summary.exists() or summary.read_text() == ""

    # What the workflow's report job renders from those outputs: the issue it
    # opens on the 3rd failure in a row, and the step summary.
    values = dict(line.split("=", 1) for line in outputs.read_text().splitlines())
    report_dir = tmp_path / "report"
    args = sr.argparse.Namespace(
        result="failure",
        ran=values["ran"],
        step=values["step"],
        status=values["status"],
        out=report_dir,
    )
    report_env = {
        "GITHUB_REPOSITORY": "Controlvar/controlvar",
        "GITHUB_RUN_ID": "37456943999",
        "GITHUB_SERVER_URL": "https://github.com",
    }
    assert sr.report(args, report_env, gh=_fake_history(red_before=2)) == 0
    assert (report_dir / "action").read_text() == "open\n"
    for name in ("title.txt", "body.md", "summary.md"):
        produced[name] = (report_dir / name).read_text()
    assert f"Failing step: {step} of 8. Status: 503." in produced["body.md"]
    with open(summary, "a") as handle:  # what the workflow's last step does
        handle.write(produced["summary.md"])
    produced["step summary"] = summary.read_text()

    for where, text in produced.items():
        for secret in SECRET_SHAPES:
            assert secret not in text, f"{where} carries {secret[:12]}..."
        assert "SENTINEL" not in text.upper(), where


def test_the_sentinel_check_fires_when_the_body_is_printed(serve, monkeypatch):
    """The sentinel test above would catch a check that prints an answer: a
    client that reports the body fails it."""
    stub = Stub(fail_step=4)
    base = serve(stub)

    class Printing(sc.Client):
        def send(self, *args, **kwargs):
            answer = super().send(*args, **kwargs)
            sys.stdout.write(f"{answer.data}\n")
            return answer

    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    sc.main(env_for(base), out=out, allow_loopback_http=True, client=Printing(base))
    assert SENTINEL in out.getvalue()


# ---------------------------------------------------------------------------
# The issue's step list says what the check does
# ---------------------------------------------------------------------------


def _issue_step(number: int) -> str:
    text = (REPO_ROOT / ".github" / "qa-templates" / "synthetic-issue.tmpl").read_text()
    lines = [line for line in text.splitlines() if line.startswith(f"{number}. ")]
    assert len(lines) == 1, lines
    return lines[0]


def test_the_issue_states_the_key_rule_step_3_applies():
    """Step 3 fails unless the account holds exactly one active key with more
    than FAIL_DAYS left; the issue a reader acts on must say the same."""
    line = _issue_step(3)
    assert line.startswith(f"3. {sc.STEPS[2]}: ")
    assert "exactly one active key" in line
    assert f"more than {sc.FAIL_DAYS} days" in line
    assert "delete the old key" in line


def test_the_issue_states_what_step_7_accepts():
    line = _issue_step(7)
    assert line.startswith(f"7. {sc.STEPS[6]}: ")
    assert "by its rollout or a targeting rule" in line
    assert sc.EVALUATED == ("rollout", "targeting_rule")


# ---------------------------------------------------------------------------
# What the client promises: no proxy, a timeout, no redirect
# ---------------------------------------------------------------------------


def test_the_client_takes_no_proxy_from_the_environment(monkeypatch):
    """A proxy set in the runner's environment never sees the credentials."""
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")

    def env_proxies(opener):
        return [
            handler.proxies
            for handler in opener.handlers
            if isinstance(handler, sc.urllib.request.ProxyHandler) and handler.proxies
        ]

    # The control: an opener built without the empty ProxyHandler picks the
    # proxy up from the environment, so the assertion below can fail.
    assert env_proxies(sc.urllib.request.build_opener())
    client = sc.Client("https://staging.example.com")
    assert env_proxies(client._opener) == []
    redirects = [
        handler
        for handler in client._opener.handlers
        if isinstance(handler, sc.urllib.request.HTTPRedirectHandler)
    ]
    assert len(redirects) == 1 and isinstance(redirects[0], sc._NoRedirect)


class _Answer:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=-1):
        return b"{}"


def test_every_request_has_the_15_second_timeout(monkeypatch):
    client = sc.Client("https://staging.example.com")
    seen = []

    def open_(request, *args, **kwargs):
        seen.append((args, kwargs))
        return _Answer()

    monkeypatch.setattr(client._opener, "open", open_)
    for method, route in sc.REQUESTS:
        client.send(
            method,
            route,
            params={"experiment_id": EXPERIMENT_ID, "flag_key": "flag"},
            body={} if method == "POST" else None,
        )
    assert sc.TIMEOUT == 15
    assert seen == [((), {"timeout": 15})] * len(sc.REQUESTS)


def test_an_unexpected_error_prints_its_type_never_its_text(
    serve, tmp_path, monkeypatch, capsys
):
    def boom(self):
        raise RuntimeError(f"boom {SENTINEL} {TOKEN}")

    monkeypatch.setattr(sc.Check, "step_track", boom)
    outputs = tmp_path / "out"
    code, out = run_in_process(env_for(serve(Stub()), GITHUB_OUTPUT=str(outputs)))
    captured = capsys.readouterr()
    assert code == 1
    assert (
        "failed at step 6 (Track): the check stopped on an unexpected RuntimeError"
        in out
    )
    assert out.endswith("ran 5 of 8\n")
    for text in (out, captured.out, captured.err, outputs.read_text()):
        assert SENTINEL not in text and TOKEN not in text
