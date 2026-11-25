"""The flag evaluation routes (#823): where their database work runs.

``GET`` and ``POST /api/v1/feature-flags/evaluate/{flag_key}`` and ``GET
/api/v1/feature-flags/user/{user_id}``: each handler closes the request's
session, then hands the evaluation to one worker thread, which closes the
session again when it is done. Each handler is found through the route walk,
as in ``test_assign_route_off_the_event_loop.py``, so it is the function
actually routed; the route's function in ``feature_flags`` that the worker
runs is replaced by a stub that records its thread, its arguments and what
had been done with the session when it ran. One of those arguments is the
presented key's flag environment, which the handler reads from the request
(#2011).

Each handler is written ``async def``. Every router's ``OffLoopRoute`` would
turn a plain ``def`` into a coroutine on the route, so neither the route walk
nor ``test_api_key_routes_shape.py`` can tell; the module function stays what
was written.
"""

from __future__ import annotations

import inspect
import threading
from typing import Any, Dict
from unittest.mock import MagicMock, call

import pytest
from fastapi import Request

from backend.app.api.v1.endpoints import feature_flags
from backend.app.main import app

pytestmark = [pytest.mark.smoke]

#: The body of the POST evaluate route, passed through untouched.
BODY = MagicMock(name="FlagEvaluationRequest")

#: (method, path, the function the worker runs, the handler's arguments other
#: than the session and the request, in the order that function takes them).
ROUTES = [
    pytest.param(
        "GET",
        "/api/v1/feature-flags/evaluate/{flag_key}",
        "evaluate_get_one",
        {"flag_key": "a-flag", "user_id": "a-user", "context": '{"tier": "pro"}'},
        id="evaluate-get",
    ),
    pytest.param(
        "POST",
        "/api/v1/feature-flags/evaluate/{flag_key}",
        "evaluate_post_one",
        {"flag_key": "a-flag", "request": BODY},
        id="evaluate-post",
    ),
    pytest.param(
        "GET",
        "/api/v1/feature-flags/user/{user_id}",
        "user_flags_one",
        {"user_id": "a-user", "context": '{"tier": "pro"}'},
        id="user-flags",
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


def _request(environment: str) -> Request:
    """A request whose key ``deps.get_api_key`` admitted in *environment*."""
    request = Request({"type": "http", "headers": []})
    request.state.flag_environment = environment
    return request


@pytest.mark.parametrize("environment", ["production", "staging", "development"])
@pytest.mark.parametrize("method, path, worker_function, arguments", ROUTES)
async def test_the_evaluation_runs_in_one_worker_trip_after_the_session_is_closed(
    method: str,
    path: str,
    worker_function: str,
    arguments: Dict[str, Any],
    environment: str,
    monkeypatch,
):
    routed = _routed(path, method)
    assert len(routed) == 1, routed
    db = MagicMock()
    seen = []

    def stub(*args):
        seen.append((threading.get_ident(), args, list(db.mock_calls)))
        return "the answer"

    monkeypatch.setattr(feature_flags, worker_function, stub)
    answer = await routed[0].endpoint(
        **arguments,
        http_request=_request(environment),
        db=db,
        api_key_info=MagicMock(),
    )
    # The worker is also given the presented key's environment (#2011), which
    # the handler read from the request, before the session.
    arguments = {**arguments, "environment": environment}
    assert answer == "the answer"
    assert len(seen) == 1, seen
    thread, args, at_entry = seen[0]
    assert thread != threading.get_ident(), (
        f"{method} {path}: the evaluation ran on the event loop's thread"
    )
    assert args == (*arguments.values(), db), (
        f"{method} {path}: {worker_function} was given {args}"
    )
    assert at_entry == [call.close()], (
        f"{method} {path}: the session when the evaluation started: {at_entry}, "
        "want the handler's close() and nothing else"
    )
    assert db.mock_calls == [call.close(), call.close()], (
        f"{method} {path}: the session after the handler returned: "
        f"{db.mock_calls}, want the handler's close() and then the worker's"
    )


@pytest.mark.parametrize(
    "handler, worker_function",
    [
        ("evaluate_feature_flag", "evaluate_get_one"),
        ("evaluate_feature_flag_post", "evaluate_post_one"),
        ("get_user_flags", "user_flags_one"),
    ],
)
def test_each_handler_is_written_as_a_coroutine_function(handler, worker_function):
    assert inspect.iscoroutinefunction(getattr(feature_flags, handler)), (
        f"feature_flags.{handler} is not async def: its own close-then-hand-off "
        "is what keeps the work out of the request's coroutine"
    )
    assert not inspect.iscoroutinefunction(getattr(feature_flags, worker_function))
