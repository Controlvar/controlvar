#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 The Controlvar authors
# SPDX-License-Identifier: Apache-2.0
"""The scale harness's own Compose stack: up, fixtures and down (#1608).

    python3 scripts/scale_stack.py up --work DIR [--http-port PORT]
    python3 scripts/scale_stack.py fixtures --work DIR --users N --events N \\
        --raw-metrics N
    python3 scripts/scale_stack.py down --work DIR

The harness measures the shipped stack, ``deploy/compose/compose.yml``, with
``tests/scale/compose.scale.yml`` on top. That file fixes its own project
name (its top-level ``name:``), which is a real installation's: a reader who
already runs Controlvar from it, and followed a load-test page's bare
``docker compose`` commands, would load-test their real stack, write test data
into their real database, and delete it all at the teardown (``down -v``). So
the harness owns its Compose project:

* every ``docker compose`` argv is built by :func:`compose_argv`: always
  ``-p controlvar-scale``, always ``--env-file <work>/.env``, always the
  shipped file plus the override;
* every child process gets :func:`child_env`: PATH, HOME and the DOCKER_*
  variables ``scripts/check_compose_config.py`` keeps (:data:`KEEP_ENV`), plus
  only what this script sets for that call. Compose gives the OS environment
  priority over ``--env-file``, so without this an exported ``POSTGRES_SERVER``
  would point the scale stack's migrations and fixtures at another database.
  ``generate-env.sh`` gets the same environment plus ``ENV_FILE``;
* it refuses ``compose.yml``'s own project name (read from the file, see
  :func:`selfhost_project`), a work directory that is ``deploy/compose`` by
  any spelling or holds a Compose file (:func:`work_dir`), and the env file
  ``deploy/compose/.env``. It reads any other env file only to look for
  :data:`ENV_MARKER`, and uses only one that carries it (one it wrote
  itself), so it never edits someone's real settings.

``up`` writes ``<work>/.env`` once (``generate-env.sh``, then the port, the
public URL and the metrics token), starts the stack, waits for
``/health/ready`` and sends one of each real request with the ``Host`` header.
``fixtures`` creates the flags, the experiment and two API keys through the
API at no more than 4 requests a second, writes ``<work>/fixtures.json``
(mode 0600; the keys are never printed), then loads the history from
``tests/scale/preload.sql``. ``down`` runs ``down -v`` on this project only.

There is no option to aim it at anything but its own stack.

The rest of the scale tooling takes the fixtures' shape from this module and
must not copy it: :data:`RULE`, :data:`RULE_CONTEXT`, :func:`flag_payloads`,
:data:`FLAG_PREFIX`, :data:`RULE_FLAG_COUNT`, :data:`EXPERIMENT_KEY`,
:data:`VARIANT_NAMES`, :data:`RETURNING_USER_PREFIX` and
:data:`RETURNING_USER_WIDTH`. Callers load this file by path (``scripts/``
has no ``__init__.py``), and such a loader must put the module in
``sys.modules`` before ``exec_module``: the :class:`Io` dataclass (with
``from __future__ import annotations``) looks its module up there, and the
load otherwise fails with ``AttributeError: 'NoneType' object has no
attribute '__dict__'``.

Standard library only. Exit status: 0 done, 1 failed, 2 refused or misused.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, TextIO

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "deploy" / "compose" / "compose.yml"
OVERRIDE_FILE = ROOT / "tests" / "scale" / "compose.scale.yml"
GENERATE_ENV = ROOT / "deploy" / "compose" / "generate-env.sh"
#: The self-hosted stack's own settings: never read, never written.
SELFHOST_ENV_FILE = ROOT / "deploy" / "compose" / ".env"
PRELOAD_SQL = ROOT / "tests" / "scale" / "preload.sql"

#: The files ``docker compose`` looks for in a directory: a work directory
#: holding one is some Compose project's, and is refused.
COMPOSE_FILE_NAMES = (
    "compose.yaml",
    "compose.yml",
    "docker-compose.yaml",
    "docker-compose.yml",
)

PROJECT = "controlvar-scale"
#: The containers scale_run.py starts itself (the load generator, the database
#: sampler, the calibration stub); ``down`` removes them too.
HARNESS_CONTAINERS = (
    f"{PROJECT}-k6",
    f"{PROJECT}-pg-sampler",
    f"{PROJECT}-stub",
)
#: What a child process inherits: ``scripts/check_compose_config.py``'s list.
KEEP_ENV = ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONFIG", "DOCKER_CONTEXT")
#: The line ``up`` adds to the env file it generated. Without it, the file is
#: not this harness's, and nothing here reads or writes it.
ENV_MARKER = "CONTROLVAR_SCALE_PROJECT"
DEFAULT_HTTP_PORT = 18080
#: The SDK limit the override sets; ``up`` checks the server answers with it.
SDK_RATE_LIMIT = "10000000"
#: The api's schema in the shipped stack (``POSTGRES_SCHEMA`` in compose.yml).
SCHEMA = "experimentation"

READY_PATH = "/health/ready"
#: ``/health*`` is limited to 300 a minute; one poll every 5 s is 12.
READY_POLL_S = 5
READY_TIMEOUT_S = 900
#: One of each request the scenarios send, without an API key: each must pass
#: the Host check and carry the raised limit.
REAL_REQUESTS = (
    ("GET", "/api/v1/feature-flags/evaluate/scale_flag_000?user_id=scale-up-check"),
    ("POST", "/api/v1/tracking/assign"),
    ("POST", "/api/v1/tracking/track"),
    ("POST", "/api/v1/tracking/batch"),
    ("GET", "/api/v1/sdk/ruleset"),
)
#: TrustedHost's refusal body (``trusted_host_middleware.py``).
HOST_REFUSED = "Invalid host header"

# -- the fixtures --------------------------------------------------------------

MAX_REQUESTS_PER_SECOND = 4
FLAG_COUNT = 100
FLAG_PREFIX = "scale_flag_"
#: The first half of the flags carry RULE; the rest carry no rule.
RULE_FLAG_COUNT = 50
FLAG_ROLLOUT_PERCENTAGE = 50
#: One three-condition rule in the dashboard shape, at a 50% rollout.
RULE: Dict[str, Any] = {
    "logical_operator": "AND",
    "rollout_percentage": 50,
    "groups": [
        {
            "logical_operator": "AND",
            "conditions": [
                {"attribute": "plan", "operator": "equals", "value": "enterprise"},
                {
                    "attribute": "app_version",
                    "operator": "semver_gte",
                    "value": "2.0.0",
                },
                {"attribute": "country", "operator": "in", "value": ["US", "CA", "GB"]},
            ],
        }
    ],
}
#: A context every condition of RULE matches.
RULE_CONTEXT: Dict[str, str] = {
    "plan": "enterprise",
    "app_version": "3.2.1",
    "country": "US",
}
EXPERIMENT_KEY = "scale_ab"
VARIANT_NAMES = ("control", "treatment")
EVENT_NAME = "scale_conversion"
#: API key name -> its scopes (None: the default scopes).
API_KEYS: Dict[str, Optional[List[str]]] = {
    "scale-sdk": None,
    "scale-server": ["sdk:ruleset"],
}
#: A preloaded (returning) user's id: this prefix, then its index zero-padded
#: to this width. The preload, the scenarios and the checker share it.
RETURNING_USER_PREFIX = "scale-user-"
RETURNING_USER_WIDTH = 7


class Refused(Exception):
    """A request this harness will not carry out (exit 2)."""


class Failed(Exception):
    """Something this harness started did not succeed (exit 1)."""


# -- isolation -----------------------------------------------------------------


def child_env(
    extra: Optional[Mapping[str, str]] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """The whole environment of a child process: KEEP_ENV, then *extra*."""
    source = os.environ if environ is None else environ
    env = {name: source[name] for name in KEEP_ENV if name in source}
    env.update(extra or {})
    return env


def _same_directory(one: Path, other: Path) -> bool:
    """The same directory by any spelling: a link, or another case on a
    case-insensitive filesystem, which ``resolve()`` leaves as it is."""
    if one == other.resolve():
        return True
    return one.is_dir() and other.is_dir() and os.path.samefile(one, other)


def work_dir(path: Path) -> Path:
    """*path* as the harness's work directory, refused if it is the
    self-hosted stack's (by any spelling) or any directory that holds a
    Compose file, such as another checkout's ``deploy/compose``."""
    work = Path(path).expanduser().resolve()
    if _same_directory(work, SELFHOST_ENV_FILE.parent):
        raise Refused(
            f"--work {path} is deploy/compose, the self-hosted stack's directory: "
            f"its .env holds a real installation's settings. Give the harness a "
            f"directory of its own."
        )
    for name in COMPOSE_FILE_NAMES:
        if os.path.lexists(work / name):
            raise Refused(
                f"--work {path} holds {name}: it is a Compose project's directory, "
                f"and its .env may hold a real installation's settings. Give the "
                f"harness a directory of its own."
            )
    return work


def env_file(work: Path) -> Path:
    """``<work>/.env``, refused if it is, or links to, deploy/compose/.env."""
    path = work / ".env"
    if path.resolve() == SELFHOST_ENV_FILE.resolve():
        raise Refused(
            f"{path} is deploy/compose/.env, the self-hosted stack's settings: "
            f"the harness only uses an env file it wrote itself."
        )
    return path


def selfhost_project() -> str:
    """``compose.yml``'s own project name: its top-level ``name:`` line.

    Read from the file rather than copied here, so the refusal follows the
    file. A file with no such line is refused: the harness cannot tell which
    project it must never touch.
    """
    for line in COMPOSE_FILE.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"name:\s*([A-Za-z0-9][A-Za-z0-9_.-]*)\s*(#.*)?", line)
        if match:
            return match.group(1)
    raise Refused(f"{COMPOSE_FILE} has no top-level name: line")


def check_project(project: str) -> str:
    """Only this harness's project; never the self-hosted one."""
    shipped = selfhost_project()
    if project.strip().lower() == shipped.lower():
        raise Refused(
            f"refusing the Compose project {shipped}: it is the one "
            f"deploy/compose/compose.yml names, a real installation's, and "
            f"down -v would delete its database"
        )
    if project != PROJECT:
        raise Refused(f"the harness's Compose project is {PROJECT}, not {project!r}")
    return project


def compose_argv(work: Path, *args: str, project: str = PROJECT) -> List[str]:
    """The one way this harness runs ``docker compose``."""
    return [
        "docker",
        "compose",
        "-p",
        check_project(project),
        "--env-file",
        str(env_file(work)),
        "-f",
        str(COMPOSE_FILE),
        "-f",
        str(OVERRIDE_FILE),
        *args,
    ]


def read_env(path: Path) -> Dict[str, str]:
    """KEY=VALUE lines, as ``check_compose_config.read_env`` reads them."""
    values: Dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            name, value = line.split("=", 1)
            values[name.strip()] = value
    return values


def own_env(work: Path) -> Dict[str, str]:
    """The settings in ``<work>/.env``, which must be a file ``up`` wrote."""
    path = env_file(work)
    if not path.is_file():
        raise Refused(
            f"{path} does not exist: run `scale_stack.py up --work {work}` first"
        )
    values = read_env(path)
    if values.get(ENV_MARKER) != PROJECT:
        raise Refused(
            f"{path} was not written by this harness (it has no "
            f"{ENV_MARKER}={PROJECT} line), so the harness will neither use nor "
            f"change it. Give it an empty directory of its own."
        )
    return values


def write_private(path: Path, text: str) -> None:
    """Replace *path* with *text*, readable by its owner only (0600)."""
    tmp = path.with_name(f".{path.name}.tmp")
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, path)


# -- the outside world, replaceable in tests ----------------------------------


def run_process(
    argv: Sequence[str], env: Mapping[str, str], input: Optional[bytes] = None
) -> subprocess.CompletedProcess:
    """Run *argv* with exactly *env*; stdin is *input*, or /dev/null."""
    return subprocess.run(
        list(argv),
        env=dict(env),
        cwd=ROOT,
        input=input,
        stdin=None if input is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE if input is not None else None,
        check=False,
    )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def http_request(
    method: str,
    url: str,
    host: str,
    body: Any = None,
    headers: Optional[Mapping[str, str]] = None,
    timeout: float = 10.0,
) -> tuple:
    """``(status, headers with lower-case names, body bytes)``; status 0 when
    nothing answered. No proxy, no redirects, an explicit ``Host``."""
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Host", host)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return (
                response.status,
                {k.lower(): v for k, v in response.headers.items()},
                response.read(),
            )
    except urllib.error.HTTPError as err:
        return err.code, {k.lower(): v for k, v in err.headers.items()}, err.read()
    except (urllib.error.URLError, OSError, http.client.HTTPException):
        return 0, {}, b""


@dataclass
class Io:
    run: Callable[..., subprocess.CompletedProcess] = run_process
    http: Callable[..., tuple] = http_request
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    out: TextIO = field(default_factory=lambda: sys.stdout)

    def say(self, text: str) -> None:
        print(text, file=self.out, flush=True)


def _base(values: Mapping[str, str]) -> tuple:
    """(URL to connect to, Host header) for the stack in *values*."""
    port = int(values["HTTP_PORT"])
    return f"http://127.0.0.1:{port}", f"localhost:{port}"


def _detail(body: bytes) -> str:
    try:
        detail = json.loads(body.decode("utf-8")).get("detail")
    except (ValueError, AttributeError, UnicodeDecodeError):
        return ""
    return detail[:200] if isinstance(detail, str) else ""


# -- up ------------------------------------------------------------------------


def generate_env(work: Path, port: int, io: Io) -> None:
    """Write ``<work>/.env``: ``generate-env.sh``, then this harness's lines.

    The script writes a staging file first, so ``<work>/.env`` exists only
    once it is complete and carries ENV_MARKER.
    """
    target = env_file(work)
    staging = work / ".env.generating"
    if staging.exists() or staging.is_symlink():
        staging.unlink()
    result = io.run(["sh", str(GENERATE_ENV)], child_env({"ENV_FILE": str(staging)}))
    if result.returncode != 0 or not staging.is_file():
        raise Failed(f"deploy/compose/generate-env.sh failed ({result.returncode})")
    lines = [
        line
        for line in staging.read_text(encoding="utf-8").splitlines()
        if not line.startswith(("PUBLIC_BASE_URL=", "HTTP_PORT=", ENV_MARKER + "="))
    ]
    lines += [
        f"# Added by scripts/scale_stack.py: the {PROJECT} stack's own settings.",
        f"{ENV_MARKER}={PROJECT}",
        f"HTTP_PORT={port}",
        f"PUBLIC_BASE_URL=http://localhost:{port}",
        f"SCALE_METRICS_TOKEN={secrets.token_hex(32)}",
    ]
    write_private(target, "\n".join(lines) + "\n")
    staging.unlink()


def wait_ready(values: Mapping[str, str], io: Io) -> None:
    base, host = _base(values)
    deadline = io.clock() + READY_TIMEOUT_S
    while True:
        status, _, _ = io.http("GET", base + READY_PATH, host)
        if status == 200:
            return
        if io.clock() >= deadline:
            raise Failed(
                f"{READY_PATH} did not answer 200 within {READY_TIMEOUT_S} s "
                f"(last: {status or 'no answer'}); see `docker compose -p {PROJECT} "
                f"logs api`"
            )
        io.sleep(READY_POLL_S)


def check_real_requests(values: Mapping[str, str], io: Io) -> None:
    """One of each request, unauthenticated: the Host is accepted, and the
    limiter is on at the override's limit."""
    base, host = _base(values)
    problems = []
    for method, path in REAL_REQUESTS:
        body = {} if method == "POST" else None
        status, headers, raw = io.http(method, base + path, host, body)
        name = f"{method} {path.split('?')[0]}"
        if status == 0:
            problems.append(f"{name}: no answer")
        elif status == 400 and _detail(raw) == HOST_REFUSED:
            problems.append(
                f"{name}: the API refused the Host {host!r}; PUBLIC_BASE_URL in "
                f"the env file must be http://{host}"
            )
        elif status >= 500:
            problems.append(f"{name}: HTTP {status}")
        elif headers.get("x-ratelimit-limit") != SDK_RATE_LIMIT:
            problems.append(
                f"{name}: X-RateLimit-Limit is "
                f"{headers.get('x-ratelimit-limit', 'absent')!r}, not "
                f"{SDK_RATE_LIMIT}: the override is not applied, or the limiter "
                f"is off"
            )
    if problems:
        raise Failed("the stack is up but not as measured:\n  " + "\n  ".join(problems))


def cmd_up(work: Path, port: Optional[int], io: Io) -> None:
    work.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not env_file(work).exists():
        if port is not None and not 1024 <= port <= 65535:
            raise Refused(f"--http-port {port} is not between 1024 and 65535")
        generate_env(work, port or DEFAULT_HTTP_PORT, io)
        io.say(f"wrote {env_file(work)} (mode 0600; it holds this stack's secrets)")
    values = own_env(work)
    if port is not None and str(port) != values.get("HTTP_PORT"):
        raise Refused(
            f"this stack was set up on port {values.get('HTTP_PORT')}; to change "
            f"it, take it down and use a new --work directory"
        )
    result = io.run(compose_argv(work, "up", "-d"), child_env())
    if result.returncode != 0:
        raise Failed(f"docker compose up failed ({result.returncode})")
    wait_ready(values, io)
    check_real_requests(values, io)
    base, host = _base(values)
    io.say(f"{PROJECT} is up on {base} (Host {host}); next: fixtures")


# -- fixtures ------------------------------------------------------------------


def flag_payloads() -> List[Dict[str, Any]]:
    """The 100 flags: the first RULE_FLAG_COUNT carry RULE."""
    flags = []
    for index in range(FLAG_COUNT):
        key = f"{FLAG_PREFIX}{index:03d}"
        payload: Dict[str, Any] = {
            "key": key,
            "name": key,
            "description": "A flag the scale harness evaluates (#1608).",
            "is_active": True,
            "rollout_percentage": FLAG_ROLLOUT_PERCENTAGE,
        }
        if index < RULE_FLAG_COUNT:
            payload["targeting_rules"] = json.loads(json.dumps(RULE))
        flags.append(payload)
    return flags


def experiment_payload() -> Dict[str, Any]:
    return {
        "name": EXPERIMENT_KEY,
        "key": EXPERIMENT_KEY,
        "description": "The scale harness's experiment (#1608).",
        "experiment_type": "a_b",
        "variants": [
            {"name": name, "is_control": index == 0, "traffic_allocation": 50}
            for index, name in enumerate(VARIANT_NAMES)
        ],
        "metrics": [
            {
                "name": EVENT_NAME,
                "event_name": EVENT_NAME,
                "metric_type": "conversion",
                "is_primary": True,
            }
        ],
    }


def api_key_payload(name: str) -> Dict[str, Any]:
    scopes = API_KEYS[name]
    return {"name": name} if scopes is None else {"name": name, "scopes": scopes}


class Api:
    """The stack's API, paced to MAX_REQUESTS_PER_SECOND."""

    def __init__(self, values: Mapping[str, str], io: Io) -> None:
        self.base, self.host = _base(values)
        self.io = io
        self.token: Optional[str] = None
        self.last: Optional[float] = None

    def call(self, method: str, path: str, body: Any = None, ok: tuple = (200,)) -> Any:
        interval = 1.0 / MAX_REQUESTS_PER_SECOND
        if self.last is not None:
            wait = self.last + interval - self.io.clock()
            if wait > 0:
                self.io.sleep(wait)
        self.last = self.io.clock()
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        status, _, raw = self.io.http(
            method, self.base + path, self.host, body, headers
        )
        if status not in ok:
            detail = _detail(raw)
            raise Failed(
                f"{method} {path.split('?')[0]} answered {status or 'nothing'}"
                + (f": {detail}" if detail else "")
            )
        try:
            return json.loads(raw.decode("utf-8")) if raw else None
        except ValueError as err:
            raise Failed(f"{method} {path} did not answer JSON") from err


def _items(data: Any) -> List[Dict[str, Any]]:
    items = data.get("items") if isinstance(data, dict) else data
    return [item for item in items or [] if isinstance(item, dict)]


def _status(item: Mapping[str, Any]) -> str:
    return str(item.get("status", "")).lower()


def create_fixtures(work: Path, values: Mapping[str, str], io: Io) -> Dict[str, Any]:
    """The flags, scale_ab and the two API keys, each created only if missing."""
    api = Api(values, io)
    login = api.call(
        "POST",
        "/api/v1/auth/login",
        {
            "email": values["FIRST_SUPERUSER"],
            "password": values["FIRST_SUPERUSER_PASSWORD"],
        },
    )
    api.token = (login or {}).get("access_token")
    if not api.token:
        raise Failed("the sign-in answered without an access token")

    existing = {
        item.get("key"): item
        for item in _items(
            api.call("GET", f"/api/v1/feature-flags/?search={FLAG_PREFIX}&limit=200")
        )
    }
    created = 0
    for payload in flag_payloads():
        found = existing.get(payload["key"])
        if found is None:
            api.call("POST", "/api/v1/feature-flags/", payload, ok=(200, 201))
            created += 1
        elif _status(found) != "active":
            raise Refused(
                f"the flag {payload['key']} exists but is {_status(found)}, not "
                f"active: this is not a stack the harness set up as it is"
            )
    io.say(f"flags: {created} created, {FLAG_COUNT - created} already there")

    experiments = [
        item
        for item in _items(api.call("GET", "/api/v1/experiments/?limit=500"))
        if item.get("key") == EXPERIMENT_KEY
    ]
    if experiments:
        experiment = experiments[0]
        if _status(experiment) != "active":
            raise Refused(
                f"the experiment {EXPERIMENT_KEY} exists but is "
                f"{_status(experiment)}, not active: refusing to measure it"
            )
        io.say(f"experiment {EXPERIMENT_KEY}: already active")
    else:
        experiment = api.call(
            "POST", "/api/v1/experiments/", experiment_payload(), ok=(200, 201)
        )
        experiment = api.call("POST", f"/api/v1/experiments/{experiment['id']}/start")
        if _status(experiment) != "active":
            raise Failed(f"{EXPERIMENT_KEY} did not start ({_status(experiment)})")
        io.say(f"experiment {EXPERIMENT_KEY}: created and started")
    variants = sorted(
        (
            {"name": v["name"], "id": str(v["id"])}
            for v in experiment.get("variants") or []
        ),
        key=lambda v: v["name"],
    )
    if len(variants) != len(VARIANT_NAMES):
        raise Refused(f"{EXPERIMENT_KEY} has {len(variants)} variants, not 2")

    previous = _previous_keys(work)
    active = {
        str(item.get("id"))
        for item in _items(api.call("GET", "/api/v1/api-keys"))
        if item.get("is_active") is True
    }
    keys = {}
    for name in API_KEYS:
        if name in previous and previous[name]["id"] in active:
            keys[name] = previous[name]
            continue
        made = api.call("POST", "/api/v1/api-keys", api_key_payload(name), ok=(201,))
        keys[name] = {"id": str(made["id"]), "key": made["key"]}
    io.say(f"API keys: {', '.join(sorted(keys))} (in fixtures.json, not shown)")

    return {
        "project": PROJECT,
        "base_url": values["PUBLIC_BASE_URL"],
        "api_keys": keys,
        "flags": {
            "rule": [f["key"] for f in flag_payloads() if "targeting_rules" in f],
            "plain": [f["key"] for f in flag_payloads() if "targeting_rules" not in f],
        },
        "rule_context": RULE_CONTEXT,
        "experiment": {
            "key": EXPERIMENT_KEY,
            "id": str(experiment["id"]),
            "variants": variants,
        },
        "returning_users": {
            "prefix": RETURNING_USER_PREFIX,
            "width": RETURNING_USER_WIDTH,
        },
    }


def _previous_keys(work: Path) -> Dict[str, Dict[str, str]]:
    path = work / "fixtures.json"
    try:
        keys = json.loads(path.read_text(encoding="utf-8")).get("api_keys", {})
    except (OSError, ValueError, AttributeError):
        return {}
    return {
        name: {"id": str(value["id"]), "key": str(value["key"])}
        for name, value in keys.items()
        if isinstance(value, dict) and "id" in value and "key" in value
    }


def _count(value: Any, name: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise Refused(f"{name} must be a whole number of at least {minimum}")
    return value


def preload_input(
    users: int, events: int, raw_metrics: int, schema: str = SCHEMA
) -> bytes:
    """What psql reads: the file's bytes unchanged, then the one call.

    *schema* is the shipped stack's; the integration test passes its own, so
    it runs exactly these bytes.
    """
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", schema):
        raise Refused(f"{schema!r} is not a plain lower-case schema name")
    call = (
        f"SELECT pg_temp.scale_preload('{schema}', "
        f"{_count(users, 'users', 1)}, {_count(events, 'events', 0)}, "
        f"{_count(raw_metrics, 'raw_metrics', 0)}, "
        f"'{RETURNING_USER_PREFIX}', {RETURNING_USER_WIDTH});\n"
    )
    return PRELOAD_SQL.read_bytes() + b"\n" + call.encode("ascii")


def preload_argv(work: Path) -> List[str]:
    """psql inside the stack's postgres container, as the container's own
    POSTGRES_USER and POSTGRES_DB, stopping at the first error."""
    return compose_argv(
        work,
        "exec",
        "-T",
        "postgres",
        "sh",
        "-c",
        'exec psql -X -q -t -A -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" '
        '-d "$POSTGRES_DB" -f -',
    )


def cmd_fixtures(work: Path, users: int, events: int, raw_metrics: int, io: Io) -> None:
    stdin = preload_input(users, events, raw_metrics)
    values = own_env(work)
    fixtures = create_fixtures(work, values, io)
    fixtures["returning_users"]["count"] = users
    write_private(work / "fixtures.json", json.dumps(fixtures, indent=2) + "\n")
    io.say(f"wrote {work / 'fixtures.json'} (mode 0600)")
    result = io.run(preload_argv(work), child_env(), input=stdin)
    if result.returncode != 0:
        raise Failed(f"the preload failed ({result.returncode}); see psql's message")
    answer = (result.stdout or b"").decode("utf-8", "replace").strip()
    io.say(f"preload: {answer}")


# -- down ----------------------------------------------------------------------


def cmd_down(work: Path, io: Io) -> None:
    own_env(work)
    # The measurement starts three containers of its own on the project's
    # network, under fixed names; a run that was killed leaves them there, and
    # the network cannot be removed under them. A missing one is not an error.
    for name in HARNESS_CONTAINERS:
        io.run(["docker", "rm", "-f", name], child_env())
    result = io.run(compose_argv(work, "down", "-v"), child_env())
    if result.returncode != 0:
        raise Failed(f"docker compose down failed ({result.returncode})")
    io.say(f"{PROJECT} is down and its volumes are removed; {work} is kept")


# -- the command line -----------------------------------------------------------


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(
        prog="scale_stack.py",
        description="The scale harness's own Compose stack (#1608).",
    )
    commands = top.add_subparsers(dest="command", required=True)
    for name, text in (
        ("up", "write the env file once, start the stack, check it"),
        ("fixtures", "create the fixtures through the API, then preload"),
        ("down", "stop the stack and remove its volumes"),
    ):
        command = commands.add_parser(name, help=text)
        command.add_argument(
            "--work",
            type=Path,
            required=True,
            help="the harness's own directory: its .env and fixtures.json",
        )
        if name == "up":
            command.add_argument(
                "--http-port",
                type=int,
                help=f"the stack's port on 127.0.0.1 (default {DEFAULT_HTTP_PORT})",
            )
        if name == "fixtures":
            for option in ("--users", "--events", "--raw-metrics"):
                command.add_argument(option, type=int, required=True)
    return top


def main(argv: Optional[Sequence[str]] = None, io: Optional[Io] = None) -> int:
    args = parser().parse_args(argv)
    io = io or Io()
    try:
        work = work_dir(args.work)
        if args.command == "up":
            cmd_up(work, args.http_port, io)
        elif args.command == "fixtures":
            cmd_fixtures(work, args.users, args.events, args.raw_metrics, io)
        else:
            cmd_down(work, io)
    except Refused as err:
        print(f"refusing: {err}", file=sys.stderr)
        return 2
    except Failed as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
