"""Exactly which routes refuse an API key with scopes (#1545, C11, R10).

``require_page_key`` (``backend/app/api/page_key.py``) answers 403 to a key
whose scope list is not empty. It belongs to the two routes the visual
editor's browser script calls, and to nothing else: a route that starts or
stops depending on it fails ``test_the_page_key_routes_are_exactly_these``
until it is listed here, and ``deps.get_api_key`` itself must never refuse a
key for its scopes (every other key-taking route accepts any active key).

Routes are read through ``effective_route_contexts``, as in
``test_sdk_scoped_routes.py``.
"""

from __future__ import annotations

import inspect

import pytest

from backend.app.api import deps
from backend.app.api.page_key import require_page_key
from backend.app.main import app

pytestmark = [pytest.mark.smoke]

#: Every (method, path) that refuses a key with any scope.
PAGE_KEY_ROUTES = {
    ("POST", "/api/v1/tracking/visual"),
    ("POST", "/api/v1/tracking/visual/preview"),
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


def _page_keyed(application) -> set:
    return {
        (method, path)
        for method, path, ctx in _contexts(application)
        if require_page_key in set(_dependency_calls(ctx.dependant))
    }


def test_the_walk_sees_the_routes():
    assert len(list(_contexts(app))) > 100


def test_the_page_key_routes_are_exactly_these():
    assert _page_keyed(app) == PAGE_KEY_ROUTES


@pytest.mark.regression
def test_get_api_key_reads_no_scope():
    """R10: the refusal lives in ``require_page_key`` only. ``get_api_key``
    names no scope, so a key with ``sdk:ruleset`` keeps working on every
    other route that takes a key."""
    source = inspect.getsource(deps.get_api_key)
    assert "scope" not in source.lower()
    assert "require_page_key" not in source
