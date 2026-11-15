"""No route waits for a worker thread while its request may hold a database
connection: neither in the steps FastAPI runs in worker threads (the walk),
nor in the trips an ``async`` handler takes itself (the pinned set below).

FastAPI solves a route's dependencies depth first, each once per request
unless it is declared ``use_cache=False``; it calls a synchronous dependency in
a worker thread, enters and leaves a synchronous generator in two, and calls a
synchronous handler in one, plus one more to validate the result when the route
has a response model. Every one of those trips waits in the same queue as all
other worker work. A request whose session has taken a connection keeps it out
of the pool while it waits.

The walk below follows that order for every route of the application, whatever
its route class, and matches callables by identity, never by name:

* the request may hold a connection from the first synchronous step whose
  subtree reaches a ``get_db`` (``deps.get_db`` or
  ``backend.app.db.session.get_db``): ``get_current_user``, ``get_api_key``,
  ``get_current_reader`` and the invite preview's viewer are such steps;
* after it, no synchronous dependency, synchronous generator, synchronous
  handler or validation trip may follow. A handler that ``OffLoopRoute``
  replaced releases the request's sessions before its one worker trip (handler
  and validation together), so it counts as none;
* a route that reaches a ``get_db`` with no synchronous step at all takes its
  first connection on the event loop's thread. Those routes are listed in
  ``ASYNC_FIRST`` exactly, as this tree leaves them.

Every ``APIRoute`` is an ``OffLoopRoute``. The guards at the end prove on
scratch applications that the walk is not vacuous: it names a synchronous
dependency, a synchronous handler without the route class, a second
``get_api_key`` declared ``use_cache=False`` (which a de-duplication by
identity would miss), and it does not take a handler that shares
``get_current_user``'s name for it.

The walk cannot see a trip an ``async`` handler takes itself
(``run_in_threadpool``, ``asyncio.to_thread``, ...). The routes whose
``async`` handler takes one while a ``get_db`` is in its dependency tree --
directly, or through a function of its own module that it calls -- are listed
in ``HANDLER_TRIPS`` exactly, and each must release the request's session
(``release_request_sessions()``, or ``close()`` on its ``get_db`` parameter)
before its first trip, in source order. The request may otherwise hold the
connection its sign-in took while it waits for the worker thread.
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import textwrap
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.dependencies.models import (
    _get_cache_key,
    _is_async_gen_callable,
    _is_coroutine_callable,
    _is_gen_callable,
)
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool

from backend.app.api import deps
from backend.app.api.off_loop_route import (
    OFF_LOOP_MARKER,
    OffLoopRoute,
    release_request_sessions,
)
from backend.app.db import session as session_module
from backend.app.main import app

pytestmark = [pytest.mark.smoke]

GET_DBS = (deps.get_db, session_module.get_db)

#: Routes that reach a ``get_db`` but run no synchronous step, so their first
#: database step runs on the event loop. There are none, in either profile.
MODULE_ASYNC_FIRST: set = set()
CORE_ASYNC_FIRST: set = set()

#: Routes whose ``async`` handler takes a worker trip itself while a ``get_db``
#: is in its dependency tree. Seven of the core eight close their session first
#: (the SDK routes, the visual page route (#1545) and the experiment results
#: routes); ``/tracking/assign`` releases the request's sessions first, and
#: takes its trip only for an experiment that balances within groups (#448);
#: the module three release the request's sessions first (the warehouse routes
#: that wait on the warehouse).
CORE_HANDLER_TRIPS = {
    ("GET", "/api/v1/sdk/ruleset"),
    ("POST", "/api/v1/tracking/evaluations"),
    ("POST", "/api/v1/tracking/assign"),
    ("POST", "/api/v1/tracking/assign/batch"),
    ("POST", "/api/v1/tracking/visual"),
    ("GET", "/api/v1/experiments/{experiment_id}/results"),
    ("GET", "/api/v1/experiments/{experiment_id}/daily-results"),
    ("GET", "/api/v1/experiments/{experiment_id}/segmented-results/{segment_by}"),
}
MODULE_HANDLER_TRIPS = {
    ("POST", "/api/v1/warehouse/analysis/connections/{connection_id}/test"),
    ("POST", "/api/v1/warehouse/analysis/sources/{source_id}/preview"),
    ("POST", "/api/v1/warehouse/analysis/sources/{source_id}/validate"),
}

#: Calls that send work to a worker thread, by the callee's last name.
WORKER_TRIP_CALLS = frozenset(
    {
        "run_in_threadpool",
        "iterate_in_threadpool",
        "to_thread",
        "run_sync",
        "run_in_executor",
        "run_in_worker",
    }
)

#: How many routes reach ``deps.get_current_user``, at least: an empty or
#: shrunken walk fails here before it can pass the checks below.
SIGNED_IN_FLOOR = {"core": 150, "full": 220}


def _full_profile() -> bool:
    return importlib.util.find_spec("modules") is not None


def _contexts(application) -> Iterator[Tuple[str, str, Any]]:
    for route in application.routes:
        contexts = getattr(route, "effective_route_contexts", None)
        if contexts is None:
            if not isinstance(route, APIRoute):
                continue
            contexts = (route,)
        for ctx in contexts() if callable(contexts) else contexts:
            if getattr(ctx, "dependant", None) is None:
                continue
            for method in getattr(ctx, "methods", None) or ():
                yield method, ctx.path, ctx


def _kind(call) -> str:
    if _is_gen_callable(call):
        return "sync generator"
    if _is_async_gen_callable(call):
        return "async generator"
    if _is_coroutine_callable(call):
        return "async"
    return "sync"


def _name(call) -> str:
    module = getattr(call, "__module__", None) or type(call).__module__
    name = getattr(call, "__qualname__", None) or type(call).__qualname__
    return f"{module}.{name}"


def _reaches(dependant, targets) -> bool:
    if any(dependant.call is target for target in targets):
        return True
    return any(_reaches(sub, targets) for sub in dependant.dependencies)


def _solve_order(dependant, cache: set, out: List[Any]) -> None:
    """The dependencies FastAPI calls for ``dependant``, in its order: depth
    first, each cache key once unless the dependency is ``use_cache=False``."""
    for sub in dependant.dependencies:
        _solve_order(sub, cache, out)
        key = _get_cache_key(dependant=sub)
        if sub.use_cache and key in cache:
            continue
        cache.add(key)
        out.append(sub)


def worker_steps(ctx) -> Dict[str, Any]:
    """For one route: the worker trips taken while a connection may be held,
    and whether its first database step runs on the event loop."""
    steps: List[Any] = []
    _solve_order(ctx.dependant, set(), steps)
    holding = False
    held_trips: List[str] = []
    loop_first = False
    for sub in steps:
        kind = _kind(sub.call)
        reaches_db = _reaches(sub, GET_DBS)
        if kind in ("sync", "sync generator"):
            if holding:
                held_trips.append(_name(sub.call))
            holding = holding or reaches_db
        elif kind == "async" and reaches_db and not holding:
            loop_first = True
    handler = ctx.dependant.call
    route_db = _reaches(ctx.dependant, GET_DBS)
    if getattr(handler, OFF_LOOP_MARKER, False):
        pass
    elif _kind(handler) == "sync":
        if holding:
            held_trips.append(f"handler {_name(handler)}")
        holding = holding or route_db
        if ctx.response_field is not None and holding:
            held_trips.append("response validation")
    elif route_db and not holding:
        loop_first = True
    return {
        "reaches_db": route_db,
        "held_trips": held_trips,
        "loop_first": loop_first,
        "signed_in": _reaches(ctx.dependant, (deps.get_current_user,)),
    }


def _walk(application) -> Dict[Tuple[str, str], Dict[str, Any]]:
    return {(m, p): worker_steps(ctx) for m, p, ctx in _contexts(application)}


def _function_node(function) -> Optional[ast.AST]:
    try:
        source = textwrap.dedent(inspect.getsource(function))
    except (OSError, TypeError):
        return None
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node
    return None


def _callee_name(call: ast.Call) -> Optional[str]:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _takes_a_trip(function, seen: Set[Any]) -> bool:
    """Whether ``function`` sends work to a worker thread: a call of a
    ``WORKER_TRIP_CALLS`` name in its body (nested functions included), or a
    call of a function of its own module that does."""
    if function in seen:
        return False
    seen.add(function)
    node = _function_node(function)
    if node is None:
        return False
    return any(_trips_at(call, function, seen) for call in _calls(node))


def _calls(node: ast.AST) -> List[ast.Call]:
    found = [n for n in ast.walk(node) if isinstance(n, ast.Call)]
    return sorted(found, key=lambda call: (call.lineno, call.col_offset))


def _trips_at(call: ast.Call, function, seen: Set[Any]) -> bool:
    name = _callee_name(call)
    if name in WORKER_TRIP_CALLS:
        return True
    if not isinstance(call.func, ast.Name):
        return False
    callee = getattr(function, "__globals__", {}).get(call.func.id)
    return (
        inspect.isfunction(callee)
        and callee.__module__ == function.__module__
        and _takes_a_trip(callee, seen)
    )


def handler_trips(ctx) -> Optional[Dict[str, Any]]:
    """For a route whose ``async`` handler reaches a ``get_db`` and takes a
    worker trip itself: the line of its first trip and of its first release
    (``release_request_sessions()`` or ``close()`` on its ``get_db``
    parameter), counted from the ``def``. None for any other route."""
    handler = ctx.dependant.call
    if getattr(handler, OFF_LOOP_MARKER, False) or _kind(handler) != "async":
        return None
    if not _reaches(ctx.dependant, GET_DBS):
        return None
    node = _function_node(handler)
    if node is None:
        return None
    db_names = {sub.name for sub in ctx.dependant.dependencies if sub.call in GET_DBS}
    first_trip = first_release = None
    for call in _calls(node):
        if first_trip is None and _trips_at(call, handler, {handler}):
            first_trip = call.lineno
        name = _callee_name(call)
        released = name == "release_request_sessions" or (
            name == "close"
            and isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id in db_names
        )
        if first_release is None and released:
            first_release = call.lineno
    if first_trip is None:
        return None
    return {"first_trip": first_trip, "first_release": first_release}


def _handler_trips(application) -> Dict[Tuple[str, str], Dict[str, Any]]:
    found = {}
    for method, path, ctx in _contexts(application):
        trips = handler_trips(ctx)
        if trips is not None:
            found[(method, path)] = trips
    return found


def _route_classes(application) -> Dict[Tuple[str, str], type]:
    found = {}
    for method, path, ctx in _contexts(application):
        found[(method, path)] = type(getattr(ctx, "original_route", ctx))
    return found


def test_the_walk_sees_the_signed_in_routes():
    walked = _walk(app)
    profile = "full" if _full_profile() else "core"
    signed_in = [route for route, found in walked.items() if found["signed_in"]]
    assert len(signed_in) >= SIGNED_IN_FLOOR[profile], len(signed_in)
    assert _kind(deps.get_current_user) == "sync"
    assert _kind(deps.get_db) == "async generator"


def test_no_route_waits_for_a_worker_while_it_may_hold_a_connection():
    held = {
        route: found["held_trips"]
        for route, found in sorted(_walk(app).items())
        if found["held_trips"]
    }
    assert not held, (
        "these routes ask for a worker thread after a step that may have taken "
        "a database connection:\n"
        + "\n".join(f"{m} {p}: {trips}" for (m, p), trips in held.items())
    )


def test_the_routes_whose_first_database_step_runs_on_the_event_loop():
    expected = CORE_ASYNC_FIRST | (MODULE_ASYNC_FIRST if _full_profile() else set())
    found = {route for route, steps in _walk(app).items() if steps["loop_first"]}
    assert found == expected, (
        f"new: {sorted(found - expected)}; gone: {sorted(expected - found)}"
    )


def test_the_async_handlers_that_take_worker_trips_themselves():
    expected = CORE_HANDLER_TRIPS | (MODULE_HANDLER_TRIPS if _full_profile() else set())
    found = _handler_trips(app)
    assert set(found) == expected, (
        f"new: {sorted(set(found) - expected)}; gone: {sorted(expected - set(found))}"
    )


def test_each_of_them_releases_the_request_session_before_its_first_trip():
    late = {
        route: trips
        for route, trips in sorted(_handler_trips(app).items())
        if trips["first_release"] is None
        or trips["first_release"] > trips["first_trip"]
    }
    assert not late, (
        "these async handlers wait for a worker thread before they release the "
        "request's session:\n"
        + "\n".join(f"{m} {p}: {t}" for (m, p), t in late.items())
    )


def test_every_route_is_an_off_loop_route():
    wrong = sorted(
        f"{m} {p}: {cls.__name__}"
        for (m, p), cls in _route_classes(app).items()
        if not issubclass(cls, OffLoopRoute)
    )
    assert not wrong, "routes without OffLoopRoute:\n" + "\n".join(wrong)


# ---------------------------------------------------------------------------
# Guards: the walk names what it must, on scratch applications
# ---------------------------------------------------------------------------


def _scratch(route_class: Optional[type] = OffLoopRoute) -> Tuple[FastAPI, APIRouter]:
    application = FastAPI()
    router = APIRouter(route_class=route_class) if route_class else APIRouter()
    return application, router


def _held(application) -> Dict[Tuple[str, str], List[str]]:
    return {
        route: found["held_trips"]
        for route, found in _walk(application).items()
        if found["held_trips"]
    }


def test_a_synchronous_dependency_after_the_sign_in_is_named():
    application, router = _scratch()

    def plain_dependency() -> int:
        return 1

    @router.post("/toggle")
    async def toggle(
        _user=Depends(deps.get_current_active_user),
        _value: int = Depends(plain_dependency),
    ) -> dict:
        return {}

    application.include_router(router)
    held = _held(application)
    assert list(held) == [("POST", "/toggle")]
    assert held[("POST", "/toggle")][0].endswith("plain_dependency")


def test_a_synchronous_handler_without_the_route_class_is_named():
    application, router = _scratch(route_class=None)

    @router.get("/items", response_model=dict)
    def items(_user=Depends(deps.get_current_active_user)) -> dict:
        return {}

    application.include_router(router)
    assert _held(application) == {
        ("GET", "/items"): [
            f"handler {_name(items)}",
            "response validation",
        ]
    }
    assert not issubclass(_route_classes(application)[("GET", "/items")], OffLoopRoute)


def test_the_same_handler_with_the_route_class_waits_for_nothing():
    application, router = _scratch()

    @router.get("/items", response_model=dict)
    def items(_user=Depends(deps.get_current_active_user)) -> dict:
        return {}

    application.include_router(router)
    assert _held(application) == {}


def test_a_second_uncached_key_check_is_named():
    application, router = _scratch()

    @router.get("/twice")
    async def twice(
        _a=Depends(deps.get_api_key, use_cache=False),
        _b=Depends(deps.get_api_key, use_cache=False),
    ) -> dict:
        return {}

    @router.get("/cached")
    async def cached(
        _a=Depends(deps.get_api_key),
        _b=Depends(deps.get_api_key),
    ) -> dict:
        return {}

    application.include_router(router)
    assert _held(application) == {("GET", "/twice"): [_name(deps.get_api_key)]}


def test_a_handler_sharing_the_sign_in_name_is_not_taken_for_it():
    application, router = _scratch(route_class=None)

    def get_current_user(user_id: str) -> dict:
        return {"id": user_id}

    router.get("/users/{user_id}")(get_current_user)
    application.include_router(router)
    assert get_current_user.__name__ == deps.get_current_user.__name__
    found = _walk(application)[("GET", "/users/{user_id}")]
    assert found == {
        "reaches_db": False,
        "held_trips": [],
        "loop_first": False,
        "signed_in": False,
    }


def test_a_route_whose_first_database_step_is_async_is_named():
    application, router = _scratch()

    @router.post("/hook")
    async def hook(db=Depends(deps.get_db)) -> dict:
        return {}

    application.include_router(router)
    assert _walk(application)[("POST", "/hook")]["loop_first"] is True


async def _helper_that_waits_for_a_worker() -> int:
    return await run_in_threadpool(int)


def test_an_async_handler_that_takes_a_trip_is_named_with_its_order():
    application, router = _scratch()

    @router.post("/late")
    async def late(db=Depends(deps.get_db), _user=Depends(deps.get_current_user)):
        await run_in_threadpool(int)
        release_request_sessions()
        return {}

    @router.post("/first")
    async def first(db=Depends(deps.get_db), _user=Depends(deps.get_current_user)):
        release_request_sessions()
        await run_in_threadpool(int)
        return {}

    @router.post("/closed")
    async def closed(session=Depends(deps.get_db)):
        session.close()
        await run_in_threadpool(int)
        return {}

    async def something_else():
        return None

    @router.post("/other-close")
    async def other_close(db=Depends(deps.get_db), other=Depends(something_else)):
        other.close()
        await run_in_threadpool(int)
        return {}

    @router.post("/helper")
    async def helper(db=Depends(deps.get_db)):
        return await _helper_that_waits_for_a_worker()

    @router.post("/no-db")
    async def no_db():
        return await run_in_threadpool(int)

    application.include_router(router)
    found = _handler_trips(application)
    assert set(found) == {
        ("POST", "/late"),
        ("POST", "/first"),
        ("POST", "/closed"),
        ("POST", "/other-close"),
        ("POST", "/helper"),
    }
    in_order = {
        path
        for (_, path), trips in found.items()
        if trips["first_release"] is not None
        and trips["first_release"] < trips["first_trip"]
    }
    assert in_order == {"/first", "/closed"}
