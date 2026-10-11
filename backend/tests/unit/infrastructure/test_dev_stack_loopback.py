"""The development stack listens on 127.0.0.1 and nowhere else (#1148).

The development stack is for one machine: it accepts a short ``SECRET_KEY``
and creates a demo administrator with a documented password. It used to be
published on every interface of the host, so anything that could reach the
machine reached its database, its cache and its API. Four places start it, and
each is read here without Docker, so this runs in the unit job:

* ``docker-compose.yml``: every ``ports:`` entry begins ``127.0.0.1:`` (Docker's
  default, a bare ``8000:8000``, is every interface, as is a bare container
  port, which Docker publishes on an ephemeral host port);
* ``docker-compose.test.yml``, the test database, cache and LocalStack;
* ``tests/alb/docker-compose.alb.yml``, the override that adds the ``alb``
  service to the development stack in the Docker Smoke job;
* ``demo/setup-local.sh``, which starts the API with uvicorn and the dashboard
  and two demo apps with ``next dev`` (whose default host is ``0.0.0.0``);
* the ``dev`` script of ``frontend/``, ``demo/shoplab`` and ``demo/streampulse``
  (``npm run dev``, ``make web``): ``next dev`` with ``-H 127.0.0.1``. Their
  ``start`` scripts and the images' ``next start`` are deliberately not read:
  inside a container the server must listen on every interface for the
  published port to reach it.

The checks are written as functions of the parsed text, and each is planted
against a small offender below, so a check that stops firing fails here and
not only when the real file regresses.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping

import pytest
import yaml

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
LOOPBACK = "127.0.0.1"

#: The compose files of the development stack, relative to the repository root.
COMPOSE_FILES = (
    "docker-compose.yml",
    "docker-compose.test.yml",
    "tests/alb/docker-compose.alb.yml",
)
SETUP_LOCAL = REPO_ROOT / "demo" / "setup-local.sh"


# ---------------------------------------------------------------------------
# docker-compose files
# ---------------------------------------------------------------------------
def published_ports(compose: Mapping[str, Any]) -> Dict[str, List[Any]]:
    """Each service's ``ports`` entries, as written."""
    found: Dict[str, List[Any]] = {}
    services = compose.get("services") or {}
    for name, service in services.items():
        entries = (service or {}).get("ports") or []
        if entries:
            found[name] = list(entries)
    return found


def ports_not_on_loopback(compose: Mapping[str, Any]) -> List[str]:
    """One line for each way a service is reachable from another machine."""
    problems: List[str] = []
    for name, entries in published_ports(compose).items():
        for entry in entries:
            if isinstance(entry, dict):
                host_ip = str(entry.get("host_ip", ""))
                if host_ip != LOOPBACK:
                    problems.append(
                        f"{name}: {entry!r} has host_ip {host_ip!r}, not {LOOPBACK}"
                    )
            elif not str(entry).startswith(f"{LOOPBACK}:"):
                problems.append(f"{name}: {str(entry)!r} does not begin {LOOPBACK}:")
    for name, service in (compose.get("services") or {}).items():
        if (service or {}).get("network_mode") == "host":
            problems.append(f"{name}: network_mode host shares every host interface")
    return problems


@pytest.mark.parametrize("relative", COMPOSE_FILES)
def test_every_published_port_is_bound_to_loopback(relative):
    compose = yaml.safe_load((REPO_ROOT / relative).read_text(encoding="utf-8"))
    published = published_ports(compose)
    assert published, f"{relative} publishes nothing: the scan is broken, not clean"
    assert ports_not_on_loopback(compose) == []


def test_the_core_services_of_the_development_stack_are_among_those_checked():
    """A service renamed out of the scan's sight would leave nothing to check."""
    compose = yaml.safe_load(
        (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    )
    assert {"postgres", "redis", "api", "frontend"} <= set(published_ports(compose))


PLANTED_COMPOSE = {
    "short form, every interface": (
        "services:\n  db:\n    ports:\n      - '5432:5432'\n",
        "db: '5432:5432' does not begin 127.0.0.1:",
    ),
    "interpolated, every interface": (
        "services:\n  api:\n    ports:\n      - '${API_HOST_PORT:-8000}:8000'\n",
        "api: '${API_HOST_PORT:-8000}:8000' does not begin 127.0.0.1:",
    ),
    "explicit all-interfaces address": (
        "services:\n  api:\n    ports:\n      - '0.0.0.0:8000:8000'\n",
        "api: '0.0.0.0:8000:8000' does not begin 127.0.0.1:",
    ),
    "container port only (ephemeral host port, every interface)": (
        "services:\n  api:\n    ports:\n      - 8000\n",
        "api: '8000' does not begin 127.0.0.1:",
    ),
    "long form without a host_ip": (
        "services:\n  api:\n    ports:\n"
        "      - {target: 8000, published: 8000, protocol: tcp}\n",
        "has host_ip '', not 127.0.0.1",
    ),
    "long form on another address": (
        "services:\n  api:\n    ports:\n"
        "      - {target: 8000, published: 8000, host_ip: 0.0.0.0}\n",
        "has host_ip '0.0.0.0', not 127.0.0.1",
    ),
    "host networking": (
        "services:\n  api:\n    network_mode: host\n",
        "api: network_mode host shares every host interface",
    ),
}


@pytest.mark.parametrize(
    "text, expected", PLANTED_COMPOSE.values(), ids=list(PLANTED_COMPOSE)
)
def test_a_planted_offender_is_named(text, expected):
    problems = ports_not_on_loopback(yaml.safe_load(text))
    assert any(expected in p for p in problems), problems


def test_loopback_entries_pass():
    text = (
        "services:\n"
        "  api:\n    ports:\n      - '127.0.0.1:${API_HOST_PORT:-8000}:8000'\n"
        "  db:\n    ports:\n      - '127.0.0.1:5432:5432'\n"
        "  web:\n    ports:\n"
        "      - {target: 80, published: 80, host_ip: 127.0.0.1}\n"
        "  worker:\n    environment: {A: b}\n"
    )
    assert ports_not_on_loopback(yaml.safe_load(text)) == []


# ---------------------------------------------------------------------------
# demo/setup-local.sh
# ---------------------------------------------------------------------------
#: What starts a server in the script: each must name the loopback address.
SERVER_START = re.compile(r"\b(uvicorn|npm run dev|next dev|next start)\b")
HOST_FLAG = re.compile(r"(?:--host|--hostname|-H)[ =]+127\.0\.0\.1(?![\w.])")


def logical_lines(script: str) -> List[str]:
    """The script's commands, with ``\\`` continuations joined and comments gone."""
    joined = re.sub(r"\\\n\s*", " ", script)
    return [
        line.strip()
        for line in joined.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def listens_beyond_loopback(script: str) -> List[str]:
    """Each command that starts a server and does not bind it to 127.0.0.1,
    and each that names the all-interfaces address."""
    problems: List[str] = []
    for line in logical_lines(script):
        if "0.0.0.0" in line:
            problems.append(f"names 0.0.0.0: {line}")
        elif SERVER_START.search(line) and not HOST_FLAG.search(line):
            problems.append(f"starts a server without a {LOOPBACK} host flag: {line}")
    return problems


def test_setup_local_starts_every_server_on_loopback():
    script = SETUP_LOCAL.read_text(encoding="utf-8")
    starts = [line for line in logical_lines(script) if SERVER_START.search(line)]
    # The API, the dashboard, ShopLab and StreamPulse. Fewer means the scan lost
    # sight of one, which would let it go back to listening on every interface.
    assert len(starts) >= 4, starts
    assert listens_beyond_loopback(script) == []


PLANTED_SCRIPT = {
    "uvicorn on 0.0.0.0": (
        "nohup python -m uvicorn backend.app.main:app \\\n"
        "    --host 0.0.0.0 --port 8000 \\\n    > api.log 2>&1 &\n",
        "names 0.0.0.0",
    ),
    "next dev on its default host": (
        "nohup npm run dev \\\n    > web.log 2>&1 &\n",
        "starts a server without a 127.0.0.1 host flag",
    ),
    "uvicorn with no host flag": (
        "uvicorn backend.app.main:app --port 8000\n",
        "starts a server without a 127.0.0.1 host flag",
    ),
    "next dev on 0.0.0.0, short flag": (
        "npm run dev -- -H 0.0.0.0\n",
        "names 0.0.0.0",
    ),
    "a host that only begins 127.0.0.1": (
        "npm run dev -- -H 127.0.0.10\n",
        "starts a server without a 127.0.0.1 host flag",
    ),
    "a host flag in a comment does not count": (
        "# uvicorn --host 127.0.0.1\nuvicorn backend.app.main:app --port 8000\n",
        "starts a server without a 127.0.0.1 host flag",
    ),
}


@pytest.mark.parametrize(
    "script, expected", PLANTED_SCRIPT.values(), ids=list(PLANTED_SCRIPT)
)
def test_a_planted_listener_is_named(script, expected):
    problems = listens_beyond_loopback(script)
    assert any(expected in p for p in problems), problems


def test_loopback_listeners_pass():
    script = (
        "nohup python -m uvicorn backend.app.main:app \\\n"
        "    --host 127.0.0.1 --port 8000 \\\n    > api.log 2>&1 &\n"
        "nohup npm run dev -- -H 127.0.0.1 \\\n    > web.log 2>&1 &\n"
        "# 0.0.0.0 only appears in this comment\n"
    )
    assert listens_beyond_loopback(script) == []


# ---------------------------------------------------------------------------
# The dev scripts of the three Next.js apps
# ---------------------------------------------------------------------------
DEV_SCRIPT_PACKAGES = (
    "frontend/package.json",
    "demo/shoplab/package.json",
    "demo/streampulse/package.json",
)


def dev_script_problems(package: Mapping[str, Any]) -> List[str]:
    """What is wrong with ``scripts.dev``: missing, 0.0.0.0, or no loopback flag."""
    script = (package.get("scripts") or {}).get("dev")
    if not script:
        return ["has no dev script"]
    if "0.0.0.0" in script:
        return [f"dev script names 0.0.0.0: {script}"]
    if not HOST_FLAG.search(script):
        return [f"dev script has no {LOOPBACK} host flag: {script}"]
    return []


@pytest.mark.parametrize("relative", DEV_SCRIPT_PACKAGES)
def test_every_dev_script_listens_on_loopback(relative):
    package = json.loads((REPO_ROOT / relative).read_text(encoding="utf-8"))
    assert package["scripts"]["dev"].startswith("next dev"), package["scripts"]["dev"]
    assert dev_script_problems(package) == []


PLANTED_DEV_SCRIPT = {
    "next dev on its default host": (
        {"scripts": {"dev": "next dev -p 3100"}},
        "dev script has no 127.0.0.1 host flag",
    ),
    "next dev on 0.0.0.0, short flag": (
        {"scripts": {"dev": "next dev -p 3100 -H 0.0.0.0"}},
        "dev script names 0.0.0.0",
    ),
    "next dev on 0.0.0.0, long flag": (
        {"scripts": {"dev": "next dev --hostname=0.0.0.0"}},
        "dev script names 0.0.0.0",
    ),
    "a host that only begins 127.0.0.1": (
        {"scripts": {"dev": "next dev -H 127.0.0.10"}},
        "dev script has no 127.0.0.1 host flag",
    ),
    "no dev script": ({"scripts": {"build": "next build"}}, "has no dev script"),
}


@pytest.mark.parametrize(
    "package, expected", PLANTED_DEV_SCRIPT.values(), ids=list(PLANTED_DEV_SCRIPT)
)
def test_a_planted_dev_script_is_named(package, expected):
    problems = dev_script_problems(package)
    assert any(expected in p for p in problems), problems


def test_a_loopback_dev_script_passes():
    assert dev_script_problems({"scripts": {"dev": "next dev -p 1 -H 127.0.0.1"}}) == []
