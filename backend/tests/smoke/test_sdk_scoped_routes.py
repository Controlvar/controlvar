"""Exactly which routes require the ``sdk:ruleset`` API-key scope.

``require_sdk_ruleset_key`` (``backend/app/api/sdk_scope.py``) protects only
the routes that depend on it.  This pins the set, over every route of the
application: a route that stops requiring the scope, or a new one that starts,
fails ``test_the_scoped_routes_are_exactly_these`` until it is listed here.

Routes are read through ``effective_route_contexts`` (FastAPI keeps
``include_router`` lazy), as in ``test_flag_change_routes_guarded.py``.
"""

from __future__ import annotations

import pytest

from backend.app.api.sdk_scope import require_sdk_ruleset_key
from backend.app.main import app

pytestmark = [pytest.mark.smoke]

#: Every (method, path) that requires an API key with ``sdk:ruleset``.
SCOPED_ROUTES = {
    ("GET", "/api/v1/sdk/ruleset"),
    ("POST", "/api/v1/tracking/evaluations"),
    ("POST", "/api/v1/tracking/assign/batch"),
    ("POST", "/api/v1/tracking/alias"),
}


def _contexts(application):
    for route in application.routes:
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


def _scoped(application) -> set[tuple[str, str]]:
    return {
        (method, path)
        for method, path, ctx in _contexts(application)
        if require_sdk_ruleset_key in set(_dependency_calls(ctx.dependant))
    }


def test_the_walk_sees_the_routes():
    """Guard the guard: an empty walk would make the inventory vacuous."""
    assert len(list(_contexts(app))) > 100


def test_the_scoped_routes_are_exactly_these():
    assert _scoped(app) == SCOPED_ROUTES


def test_every_route_under_the_sdk_prefix_requires_the_scope():
    """``/api/v1/sdk/`` is the server-side SDK surface: nothing there is unscoped."""
    sdk_routes = {
        (method, path)
        for method, path, _ctx in _contexts(app)
        if path.startswith("/api/v1/sdk/") and method != "HEAD"
    }
    assert sdk_routes, "no route under /api/v1/sdk/"
    assert sdk_routes <= _scoped(app)
