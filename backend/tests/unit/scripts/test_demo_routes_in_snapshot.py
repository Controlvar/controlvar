"""Every ``METHOD /path`` the demo apps print is a route the API has.

The StreamPulse "Rollout story" drawer told a reader that the script calls
``POST /rollout-schedules/{id}/stages/{stage}/advance``; the route is
``POST /rollout-schedules/stages/{stage_id}/advance``. The drawer is prose in
a string, so no type check or jest test of it could notice. Here every
``GET|POST|PUT|PATCH|DELETE /path`` written under ``demo/*/src`` is matched
against ``docs/api/openapi-v1.full.json``: a path without ``/api`` is
prefixed with ``/api/v1``, and a ``{name}`` or ``<name>`` segment matches any
one segment.

Reads only the demo sources and that one snapshot; no database, no network.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Pattern, Set, Tuple

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
SNAPSHOT = REPO_ROOT / "docs" / "api" / "openapi-v1.full.json"
DEMO = REPO_ROOT / "demo"

if not SNAPSHOT.is_file() or not DEMO.is_dir():
    pytest.skip("this tree has no demo/ or OpenAPI snapshot", allow_module_level=True)

CALL = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+(/[A-Za-z0-9_{}<>:./-]*)")
SUFFIXES = {".ts", ".tsx"}


def _routes() -> List[Tuple[Pattern[str], Set[str]]]:
    spec = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    routes = []
    for path, operations in spec["paths"].items():
        pattern = re.sub(r"\\\{[^}]+\\\}", "[^/]+", re.escape(path.rstrip("/")))
        routes.append((re.compile(f"^{pattern}/?$"), {m.upper() for m in operations}))
    return routes


def _unmatched(text: str) -> Tuple[int, List[str]]:
    """How many calls *text* names, and which of them no route answers."""
    routes = _routes()
    checked = 0
    missing = []
    for method, path in CALL.findall(text):
        path = path.split("?")[0].rstrip(".,:)").rstrip("/")
        if not path or "..." in path:
            continue
        if not path.startswith("/api/"):
            path = "/api/v1" + path
        concrete = re.sub(r"\{[^}]*\}|<[^>]*>", "x", path)
        checked += 1
        if not any(method in ops and rx.match(concrete) for rx, ops in routes):
            missing.append(f"{method} {path}")
    return checked, missing


def _demo_sources() -> Dict[str, str]:
    files = {}
    for app in sorted(DEMO.iterdir()):
        src = app / "src"
        if not src.is_dir():
            continue
        for path in sorted(src.rglob("*")):
            if path.suffix in SUFFIXES and "node_modules" not in path.parts:
                files[path.relative_to(REPO_ROOT).as_posix()] = path.read_text(
                    encoding="utf-8"
                )
    return files


def test_every_route_the_demo_apps_print_is_in_the_openapi_snapshot():
    sources = _demo_sources()
    assert sources, "no demo source found: this check would pass on nothing"
    total = 0
    problems = []
    for name, text in sources.items():
        checked, missing = _unmatched(text)
        total += checked
        problems += [f"{name}: {call}" for call in missing]
    assert total >= 3, f"only {total} routes found: the pattern has stopped matching"
    assert not problems, "routes the API does not have:\n" + "\n".join(problems)


def test_the_check_refuses_a_route_the_api_does_not_have():
    """The positive control: a real route passes, a wrong one is named."""
    checked, missing = _unmatched(
        "POST /rollout-schedules/stages/{stage_id}/advance and "
        "POST /rollout-schedules/{id}/stages/{stage}/advance"
    )
    assert checked == 2
    assert missing == ["POST /api/v1/rollout-schedules/{id}/stages/{stage}/advance"]
