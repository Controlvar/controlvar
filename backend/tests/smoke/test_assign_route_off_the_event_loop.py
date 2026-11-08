"""``POST /api/v1/tracking/assign`` (#823): where its database work runs, and
who calls its handler in process.

* The handler closes the request's session, then hands the assignment to one
  worker thread, which closes the session again when it is done. Found
  through the route walk, as in ``test_assign_batch_route.py``, so it is the
  function actually routed; ``tracking.assign_one`` is replaced by a stub that
  records its thread and what had been done with the session when it ran.
* The handler is written ``async def``. Every router's ``OffLoopRoute`` would
  turn a plain ``def`` into a coroutine on the route, so the route walk alone
  cannot tell, but the module function stays what was written, and the split
  URL assign route awaits it.
* No core file calls the handler. It closes the session it is given before
  it returns, so a caller must not use what it loaded on that session
  afterwards, and a new caller is a decision rather than an accident. The
  modules' one caller, the split URL assign route, is pinned on the modules
  side, so this core test names no module. The scan reads the source under
  ``backend/`` for a call of ``assign_user_to_experiment(`` (the definition
  is not a call); a call through an alias (``import ... as``) or ``getattr``
  gets past it.
"""

from __future__ import annotations

import inspect
import re
import threading
from pathlib import Path
from typing import Tuple
from unittest.mock import MagicMock, call

import pytest

from backend.app.api.v1.endpoints import tracking
from backend.app.main import app
from backend.tests.smoke.modules_manifest import REPO_ROOT

pytestmark = [pytest.mark.smoke]

PATH = "/api/v1/tracking/assign"

CALL = re.compile(r"\bassign_user_to_experiment\(")
DEFINITION = re.compile(r"\bdef assign_user_to_experiment\(")
SKIPPED_DIRECTORIES = frozenset({"node_modules", "__pycache__", ".venv", "venv"})
THIS_FILE = Path(__file__).resolve()


def _routed(path: str, method: str):
    found = []
    for route in app.routes:
        contexts = getattr(route, "effective_route_contexts", None)
        if contexts is None:
            continue
        for ctx in contexts() if callable(contexts) else contexts:
            if ctx.path == path and method in (getattr(ctx, "methods", None) or ()):
                found.append(ctx)
    return found


async def test_the_assignment_runs_in_one_worker_trip_after_the_session_is_closed(
    monkeypatch,
):
    routed = _routed(PATH, "POST")
    assert len(routed) == 1, routed
    db = MagicMock()
    seen = []

    def stub(request, session):
        seen.append((threading.get_ident(), list(session.mock_calls)))
        return "the answer"

    monkeypatch.setattr(tracking, "assign_one", stub)
    answer = await routed[0].endpoint(
        request=MagicMock(), db=db, api_key_info=MagicMock()
    )
    assert answer == "the answer"
    assert len(seen) == 1, seen
    thread, at_entry = seen[0]
    assert thread != threading.get_ident(), (
        "the assignment ran on the event loop's thread"
    )
    assert at_entry == [call.close()], (
        f"the session when the assignment started: {at_entry}, want the "
        "handler's close() and nothing else"
    )
    assert db.mock_calls == [call.close(), call.close()], (
        f"the session after the handler returned: {db.mock_calls}, want the "
        "handler's close() and then the worker's"
    )


def test_the_handler_is_written_as_a_coroutine_function():
    assert inspect.iscoroutinefunction(tracking.assign_user_to_experiment), (
        "tracking.assign_user_to_experiment is not async def: its callers await "
        "it, and the route's own close-then-hand-off is what keeps the work off "
        "the event loop"
    )
    assert not inspect.iscoroutinefunction(tracking.assign_one)


def _calls_of_the_handler(root: Path) -> Tuple[set, int]:
    """The files under *root* that call the handler, and how many were read."""
    found, scanned = set(), 0
    for path in sorted(root.rglob("*.py")):
        if SKIPPED_DIRECTORIES.intersection(path.parts):
            continue
        if path.resolve() == THIS_FILE:
            continue
        scanned += 1
        for line in path.read_text(encoding="utf-8").splitlines():
            if CALL.search(line) and not DEFINITION.search(line):
                found.add(path.relative_to(REPO_ROOT).as_posix())
                break
    return found, scanned


def test_no_core_file_calls_the_handler():
    found, scanned = _calls_of_the_handler(REPO_ROOT / "backend")
    assert scanned > 1000, scanned  # a wrong root cannot turn this into a pass
    assert found == set(), (
        f"core files calling tracking.assign_user_to_experiment: {sorted(found)}. "
        "The handler closes the session it is given; a synchronous caller "
        "already in a worker thread calls tracking.assign_one instead."
    )


def test_the_scan_finds_a_call_and_skips_the_definition():
    """Guard the guard: the patterns tell a call from the definition."""
    assert CALL.search("    answer = await tracking.assign_user_to_experiment(")
    assert DEFINITION.search("async def assign_user_to_experiment(")
    assert not CALL.search('    (f"{E}/tracking.py", "assign_user_to_experiment", "X")')
