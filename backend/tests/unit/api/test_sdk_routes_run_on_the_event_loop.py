"""The single-assignment, event-tracking and flag-evaluation routes are
``async def`` handlers that take the synchronous database session, so their
database work runs on the API process's event loop, one request at a time.

Routes are read through ``effective_route_contexts`` (FastAPI keeps
``include_router`` lazy), as in ``test_sdk_scoped_routes.py``.
"""

import inspect

import pytest

from backend.app.api import deps
from backend.app.main import app

pytestmark = [pytest.mark.unit]

ROUTES = {
    ("POST", "/api/v1/tracking/assign"),
    ("POST", "/api/v1/tracking/track"),
    ("GET", "/api/v1/feature-flags/evaluate/{flag_key}"),
    ("POST", "/api/v1/feature-flags/evaluate/{flag_key}"),
}


def _contexts():
    for route in app.routes:
        contexts = getattr(route, "effective_route_contexts", None)
        if contexts is None:
            continue
        for ctx in contexts() if callable(contexts) else contexts:
            for method in getattr(ctx, "methods", None) or ():
                yield method, ctx.path, ctx


def _dependency_calls(dependant):
    for dep in dependant.dependencies:
        yield dep.call
        yield from _dependency_calls(dep)


def test_each_route_is_a_coroutine_that_takes_the_synchronous_session():
    found = {(m, p): ctx for m, p, ctx in _contexts() if (m, p) in ROUTES}
    assert set(found) == ROUTES
    for key, ctx in found.items():
        assert inspect.iscoroutinefunction(ctx.dependant.call), key
        assert deps.get_db in set(_dependency_calls(ctx.dependant)), key
