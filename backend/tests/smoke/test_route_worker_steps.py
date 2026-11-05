"""No route waits for a worker thread while its request may hold a database
connection.

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
"""

from __future__ import annotations

import importlib.util
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.dependencies.models import (
    _get_cache_key,
    _is_async_gen_callable,
    _is_coroutine_callable,
    _is_gen_callable,
)
from fastapi.routing import APIRoute

from backend.app.api import deps
from backend.app.api.off_loop_route import OFF_LOOP_MARKER, OffLoopRoute
from backend.app.db import session as session_module
from backend.app.main import app

pytestmark = [pytest.mark.smoke]

GET_DBS = (deps.get_db, session_module.get_db)

#: Routes that reach a ``get_db`` but run no synchronous step, so their first
#: database step runs on the event loop. All are in the full profile.
MODULE_ASYNC_FIRST = {
    ("GET", "/api/v1/auth/sso/login"),
    ("GET", "/api/v1/auth/sso/oidc/{provider}/callback"),
    ("GET", "/api/v1/auth/sso/oidc/{provider}/login"),
    ("POST", "/api/v1/auth/sso/exchange"),
    ("POST", "/api/v1/integrations/webhooks/github"),
    ("POST", "/api/v1/integrations/webhooks/jira"),
    ("POST", "/api/v1/integrations/webhooks/salesforce"),
}
CORE_ASYNC_FIRST: set = set()

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
