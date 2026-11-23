"""The event tracking and error report routes (#823): where their database
work runs.

* Each handler closes the request's session, then hands the route's work to
  one worker thread, which closes the session again when it is done. Found
  through the route walk, as in ``test_assign_route_off_the_event_loop.py``,
  so it is the function actually routed; the route's ``*_one`` function is
  replaced by a stub that records its thread and what had been done with the
  session when it ran.
* The two error report routes read the presented key's source
  (``client_errors.report_source``) in that same worker trip, after the
  close: ``presented_key_is_server_key`` is replaced by a stub that records
  the same, and the source it answers is the one the report is stamped with.
* Each handler is written ``async def``. Every router's ``OffLoopRoute``
  would turn a plain ``def`` into a coroutine on the route, so the route walk
  alone cannot tell.
"""

from __future__ import annotations

import inspect
import threading
from typing import Any, Callable, Dict, List, NamedTuple, Optional
from unittest.mock import MagicMock, call

import pytest

from backend.app.api.v1.endpoints import client_errors, tracking
from backend.app.main import app

pytestmark = [pytest.mark.smoke, pytest.mark.regression]

#: The header value the error report routes are given.
KEY_HEADER = "the-presented-key"


class Route(NamedTuple):
    method: str
    path: str
    module: Any
    handler: str
    one: str
    #: The handler's arguments apart from ``db``.
    arguments: Callable[[], Dict[str, Any]]
    #: Whether the route stamps its reports with the presented key's source.
    stamps_a_source: bool = False


def _body(name: str) -> Callable[[], Dict[str, Any]]:
    return lambda: {name: MagicMock(), "api_key_info": MagicMock()}


def _report_body(name: str) -> Callable[[], Dict[str, Any]]:
    return lambda: {
        name: MagicMock(),
        "api_key_info": MagicMock(),
        "api_key_header": KEY_HEADER,
    }


ROUTES = [
    pytest.param(
        Route(
            "POST",
            "/api/v1/tracking/track",
            tracking,
            "track_event",
            "track_one",
            _body("request"),
        ),
        id="track",
    ),
    pytest.param(
        Route(
            "POST",
            "/api/v1/tracking/events",
            tracking,
            "track_event_by_ids",
            "track_by_ids_one",
            _body("event_data"),
        ),
        id="events",
    ),
    pytest.param(
        Route(
            "POST",
            "/api/v1/tracking/batch",
            tracking,
            "track_events_batch",
            "track_batch_one",
            _body("request"),
        ),
        id="tracking-batch",
    ),
    pytest.param(
        Route(
            "GET",
            "/api/v1/tracking/assignments/{user_id}",
            tracking,
            "get_user_assignments",
            "user_assignments_one",
            lambda: {
                "user_id": "u-1",
                "active_only": True,
                "api_key_info": MagicMock(),
            },
        ),
        id="assignments",
    ),
    pytest.param(
        Route(
            "POST",
            "/api/v1/tracking/errors",
            client_errors,
            "report_client_error",
            "report_one",
            _report_body("report"),
            stamps_a_source=True,
        ),
        id="errors",
    ),
    pytest.param(
        Route(
            "POST",
            "/api/v1/tracking/errors/batch",
            client_errors,
            "report_client_errors_batch",
            "report_batch_one",
            _report_body("request"),
            stamps_a_source=True,
        ),
        id="errors-batch",
    ),
]


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


class _Seen(NamedTuple):
    thread: int
    session_calls: List[Any]
    arguments: tuple


@pytest.mark.parametrize("route", ROUTES)
async def test_the_work_runs_in_one_worker_trip_after_the_session_is_closed(
    route: Route, monkeypatch
):
    routed = _routed(route.path, route.method)
    assert len(routed) == 1, routed
    db = MagicMock()
    work: List[_Seen] = []
    key_checks: List[_Seen] = []

    def one(*arguments):
        work.append(
            _Seen(threading.get_ident(), list(arguments[-1].mock_calls), arguments)
        )
        return "the answer"

    def key_check(session, header):
        key_checks.append(
            _Seen(threading.get_ident(), list(session.mock_calls), (header,))
        )
        return True

    monkeypatch.setattr(route.module, route.one, one)
    monkeypatch.setattr(client_errors, "presented_key_is_server_key", key_check)
    answer = await routed[0].endpoint(db=db, **route.arguments())
    loop_thread = threading.get_ident()

    assert answer == "the answer"
    assert len(work) == 1, work
    assert work[0].thread != loop_thread, (
        f"{route.method} {route.path}: the work ran on the event loop's thread"
    )
    assert work[0].session_calls == [call.close()], (
        f"{route.method} {route.path}: the session when the work started: "
        f"{work[0].session_calls}, want the handler's close() and nothing else"
    )
    assert db.mock_calls == [call.close(), call.close()], (
        f"{route.method} {route.path}: the session after the handler returned: "
        f"{db.mock_calls}, want the handler's close() and then the worker's"
    )
    if not route.stamps_a_source:
        assert key_checks == []
        return
    assert len(key_checks) == 1, key_checks
    check = key_checks[0]
    assert check.thread == work[0].thread, (
        f"{route.method} {route.path}: the presented key was read on another "
        f"thread than the work (the loop's: {check.thread == loop_thread})"
    )
    assert check.session_calls == [call.close()], (
        f"{route.method} {route.path}: the session when the presented key was "
        f"read: {check.session_calls}, want the handler's close() and nothing else"
    )
    assert check.arguments == (KEY_HEADER,)
    assert work[0].arguments[-2] == "server", (
        f"{route.method} {route.path}: the work was given the source "
        f"{work[0].arguments[-2]!r}, want the presented key's ('server')"
    )


@pytest.mark.parametrize("route", ROUTES)
def test_the_handler_is_written_as_a_coroutine_function(route: Route):
    handler = getattr(route.module, route.handler)
    assert inspect.iscoroutinefunction(handler), (
        f"{route.module.__name__}.{route.handler} is not async def: the route's "
        "own close-then-hand-off is what keeps its work off the event loop"
    )
    assert not inspect.iscoroutinefunction(getattr(route.module, route.one))


def test_the_source_is_read_by_a_plain_function():
    """``report_source`` is called in the worker trip, not declared as a
    dependency: no route depends on it."""
    assert not inspect.iscoroutinefunction(client_errors.report_source)
    depending: Optional[str] = None
    walked = 0
    for route in app.routes:
        contexts = getattr(route, "effective_route_contexts", None)
        if contexts is None:
            continue
        for ctx in contexts() if callable(contexts) else contexts:
            dependant = getattr(ctx, "dependant", None)
            if dependant is None:
                continue
            walked += 1
            stack = list(dependant.dependencies)
            while stack:
                sub = stack.pop()
                if sub.call is client_errors.report_source:
                    depending = ctx.path
                stack.extend(sub.dependencies)
    assert walked > 100, walked  # an empty walk cannot turn this into a pass
    assert depending is None, f"{depending} depends on report_source"
