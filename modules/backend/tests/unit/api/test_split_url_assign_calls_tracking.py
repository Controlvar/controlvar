# SPDX-FileCopyrightText: 2026 The Controlvar authors
# SPDX-License-Identifier: Apache-2.0
"""The split URL assign route is the modules' only in-process caller of
``POST /api/v1/tracking/assign``'s handler (#823).

The handler closes the session it is given before it returns, so a caller
must not use what it loaded on that session afterwards: the split URL assign
route uses only the answer, the URLs it read before the call and the request.
A new caller in the modules is a decision rather than an accident; the core
side is pinned by ``backend/tests/smoke/test_assign_route_off_the_event_loop.py``.
The scan reads the source under ``modules/`` for a call of
``assign_user_to_experiment(`` (the definition is not a call); a call through
an alias (``import ... as``) or ``getattr`` gets past it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[5]
CALLERS = {"modules/backend/app/api/v1/endpoints/split_url_routing.py"}

CALL = re.compile(r"\bassign_user_to_experiment\(")
DEFINITION = re.compile(r"\bdef assign_user_to_experiment\(")
SKIPPED_DIRECTORIES = frozenset({"node_modules", "__pycache__", ".venv", "venv"})
THIS_FILE = Path(__file__).resolve()


def test_the_split_url_assign_route_is_the_only_module_caller():
    found, scanned = set(), 0
    for path in sorted((REPO_ROOT / "modules").rglob("*.py")):
        if SKIPPED_DIRECTORIES.intersection(path.parts) or path.resolve() == THIS_FILE:
            continue
        scanned += 1
        for line in path.read_text(encoding="utf-8").splitlines():
            if CALL.search(line) and not DEFINITION.search(line):
                found.add(path.relative_to(REPO_ROOT).as_posix())
                break
    assert scanned > 100, scanned  # a wrong root cannot turn this into a pass
    assert found == CALLERS, (
        f"module files calling tracking.assign_user_to_experiment: new "
        f"{sorted(found - CALLERS)}, gone {sorted(CALLERS - found)}. The handler "
        "closes the session it is given; a synchronous caller already in a "
        "worker thread calls tracking.assign_one instead."
    )
