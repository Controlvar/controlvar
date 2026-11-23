"""For the SDK routes listed here, no ``async`` dependency reads the request's
session (#823).

A dependency runs before the handler. One written ``async`` that reaches
``deps.get_db`` runs its queries on the event loop's thread, before a handler
can close the session and hand its database work to a worker thread, as
``POST /tracking/assign`` and the event tracking and error report routes do
(``test_assign_route_off_the_event_loop.py``,
``test_tracking_routes_off_the_event_loop.py``); neither those pins nor the
burst test would see it. So for every route listed here, no coroutine or
async generator in its dependency tree, ``get_db`` itself apart, has a
``get_db`` below it. Callables are matched by identity, never by name.

Routes are read through ``effective_route_contexts``, as in
``test_api_key_routes_shape.py``. The guards at the end prove on a scratch
application that the walk names such a dependency, and does not name one
that only reads the request's headers.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.dependencies.models import _is_async_gen_callable, _is_coroutine_callable
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session

from backend.app.api import deps
from backend.app.db import session as session_module
from backend.app.main import app

pytestmark = [pytest.mark.smoke, pytest.mark.regression]

GET_DBS = (deps.get_db, session_module.get_db)

#: The routes, in both profiles.
ROUTES = {
    ("POST", "/api/v1/tracking/assign"),
    ("POST", "/api/v1/tracking/track"),
    ("POST", "/api/v1/tracking/events"),
    ("POST", "/api/v1/tracking/batch"),
    ("GET", "/api/v1/tracking/assignments/{user_id}"),
    ("POST", "/api/v1/tracking/errors"),
    ("POST", "/api/v1/tracking/errors/batch"),
    ("GET", "/api/v1/feature-flags/evaluate/{flag_key}"),
    ("POST", "/api/v1/feature-flags/evaluate/{flag_key}"),
    ("GET", "/api/v1/feature-flags/user/{user_id}"),
}


def _contexts(application):
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


def _name(call) -> str:
    module = getattr(call, "__module__", None) or type(call).__module__
    name = getattr(call, "__qualname__", None) or type(call).__qualname__
    return f"{module}.{name}"


def _reaches(dependant) -> bool:
    if any(dependant.call is target for target in GET_DBS):
        return True
    return any(_reaches(sub) for sub in dependant.dependencies)


def _async_readers(dependant, found: Optional[List[str]] = None) -> List[str]:
    """The ``async`` dependencies under ``dependant`` (``get_db`` apart) that
    have a ``get_db`` below them."""
    found = [] if found is None else found
    for sub in dependant.dependencies:
        is_get_db = any(sub.call is target for target in GET_DBS)
        is_async = _is_coroutine_callable(sub.call) or _is_async_gen_callable(sub.call)
        if is_async and not is_get_db and _reaches(sub):
            name = _name(sub.call)
            if name not in found:
                found.append(name)
        _async_readers(sub, found)
    return found


def _walk(application, routes) -> Dict[Tuple[str, str], List[str]]:
    return {
        (method, path): _async_readers(ctx.dependant)
        for method, path, ctx in _contexts(application)
        if (method, path) in routes
    }


def test_every_route_is_found_and_takes_the_session():
    found = {(m, p): ctx for m, p, ctx in _contexts(app) if (m, p) in ROUTES}
    assert set(found) == ROUTES, sorted(ROUTES - set(found))
    for route, ctx in found.items():
        assert _reaches(ctx.dependant), route


def test_no_async_dependency_of_these_routes_reads_the_session():
    readers = {route: names for route, names in _walk(app, ROUTES).items() if names}
    assert not readers, (
        "async dependencies that reach get_db, so they read on the event loop "
        "before the handler closes the session:\n"
        + "\n".join(f"{m} {p}: {names}" for (m, p), names in sorted(readers.items()))
    )


# ---------------------------------------------------------------------------
# Guards: the walk names what it must, on a scratch application
# ---------------------------------------------------------------------------


async def _reads_the_session(db: Session = Depends(deps.get_db)) -> str:
    return "read"


async def _reads_a_header(
    key: Optional[str] = Depends(deps.API_KEY_HEADER),
) -> Optional[str]:
    return key


async def _layered(value: str = Depends(_reads_the_session)) -> str:
    return value


def test_an_async_dependency_that_reaches_get_db_is_named():
    application = FastAPI()
    router = APIRouter()

    @router.post("/reads")
    async def reads(
        _db: Session = Depends(deps.get_db),
        _owner=Depends(deps.get_api_key),
        _value: str = Depends(_layered),
    ) -> dict:
        return {}

    @router.post("/header")
    async def header(
        _db: Session = Depends(deps.get_db),
        _owner=Depends(deps.get_api_key),
        _key: Optional[str] = Depends(_reads_a_header),
    ) -> dict:
        return {}

    application.include_router(router)
    walked = _walk(application, {("POST", "/reads"), ("POST", "/header")})
    assert walked[("POST", "/reads")] == [
        _name(_layered),
        _name(_reads_the_session),
    ]
    assert walked[("POST", "/header")] == []
