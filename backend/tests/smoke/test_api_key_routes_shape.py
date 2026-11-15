"""Every route that takes an API key, and its one synchronous step (#1728).

A route takes an API key when its dependency tree reaches ``deps.get_api_key``
or ``read_scope.get_current_reader`` (which calls ``get_api_key`` itself). On
each such route that function is the only callable in the tree, handler
included, that is synchronous; every other dependency and the handler are
``async``. A new route that takes a key fails
``test_the_api_key_routes_are_exactly_these`` until it is listed here, and a
synchronous dependency or handler added to one fails
``test_each_api_key_route_has_one_synchronous_step``, naming the route.

Callables are matched by identity, never by name: a handler of a signed-in
route may share ``get_api_key``'s name, and a scratch application with one
proves the walk does not take it for ``deps.get_api_key``.

Routes are read through ``effective_route_contexts``, which descends into
every included router (``app.routes`` lists only the top level), and each
route's dependency tree is walked to its leaves. Synchronous means neither a
coroutine function nor an async generator, as FastAPI itself classifies a
callable (``fastapi.dependencies.models``).

The profile is decided as the application decides it: whether ``modules``
imports. The full profile adds the two split URL routes.
"""

from __future__ import annotations

import importlib.util

import pytest
from fastapi import Depends, FastAPI
from fastapi.dependencies.models import _is_async_gen_callable, _is_coroutine_callable
from fastapi.routing import APIRoute

from backend.app.api import deps
from backend.app.api.read_scope import get_current_reader
from backend.app.main import app

pytestmark = [pytest.mark.smoke]

#: Routes whose tree reaches ``deps.get_api_key``, in both profiles.
KEY_ROUTES = {
    ("GET", "/api/v1/feature-flags/evaluate/{flag_key}"),
    ("POST", "/api/v1/feature-flags/evaluate/{flag_key}"),
    ("GET", "/api/v1/feature-flags/user/{user_id}"),
    ("GET", "/api/v1/sdk/ruleset"),
    ("GET", "/api/v1/tracking/assignments/{user_id}"),
    ("POST", "/api/v1/tracking/assign"),
    ("POST", "/api/v1/tracking/assign/batch"),
    ("POST", "/api/v1/tracking/batch"),
    ("POST", "/api/v1/tracking/errors"),
    ("POST", "/api/v1/tracking/errors/batch"),
    ("POST", "/api/v1/tracking/evaluations"),
    ("POST", "/api/v1/tracking/events"),
    ("POST", "/api/v1/tracking/track"),
    # #1545: the visual editor's browser script (backend/app/api/page_key.py).
    ("POST", "/api/v1/tracking/visual"),
    ("POST", "/api/v1/tracking/visual/preview"),
}

#: Added by the full profile.
MODULE_KEY_ROUTES = {
    ("GET", "/api/v1/tracking/split-url/routes"),
    ("POST", "/api/v1/tracking/split-url/assign"),
}

#: Routes whose tree reaches ``read_scope.get_current_reader``, in both
#: profiles (``test_mcp_read_routes.py`` pins the same set).
READER_ROUTES = {
    ("GET", "/api/v1/feature-flags/"),
    ("GET", "/api/v1/feature-flags"),
    ("GET", "/api/v1/experiments/"),
    ("GET", "/api/v1/experiments/{experiment_id}"),
    ("GET", "/api/v1/experiments/{experiment_id}/results"),
}

#: The one synchronous step a key-taking route may have.
ENTRY_POINTS = (deps.get_api_key, get_current_reader)


def _full_profile() -> bool:
    return importlib.util.find_spec("modules") is not None


def _contexts(application):
    for route in application.routes:
        contexts = getattr(route, "effective_route_contexts", None)
        if contexts is None:
            # A route declared on the app itself is its own context.
            if not isinstance(route, APIRoute):
                continue
            contexts = (route,)
        for ctx in contexts() if callable(contexts) else contexts:
            if getattr(ctx, "dependant", None) is None:
                continue
            for method in getattr(ctx, "methods", None) or ():
                yield method, ctx.path, ctx


def _tree(dependant):
    """The route's dependant (its handler) and every dependency under it."""
    yield dependant
    for dependency in dependant.dependencies:
        yield from _tree(dependency)


def _is_sync(call) -> bool:
    return not (_is_coroutine_callable(call) or _is_async_gen_callable(call))


def _name(call) -> str:
    module = getattr(call, "__module__", None) or type(call).__module__
    name = getattr(call, "__qualname__", None) or type(call).__qualname__
    return f"{module}.{name}"


def _key_routes(application) -> dict:
    """``(method, path)`` -> the callables of its tree, for every route whose
    tree reaches an entry point."""
    found = {}
    for method, path, ctx in _contexts(application):
        calls = [node.call for node in _tree(ctx.dependant)]
        if any(call is entry for call in calls for entry in ENTRY_POINTS):
            found[(method, path)] = calls
    return found


def _expected() -> set:
    expected = KEY_ROUTES | READER_ROUTES
    if _full_profile():
        expected |= MODULE_KEY_ROUTES
    return expected


def test_the_walk_sees_the_routes():
    """Guard the guard: an empty walk, or a classifier that calls everything
    async, would make the checks below vacuous."""
    assert len(list(_contexts(app))) > 100
    assert _is_sync(deps.get_api_key)
    assert _is_sync(get_current_reader)
    assert not _is_sync(deps.get_db)


def test_a_handler_with_the_same_name_is_not_matched():
    """A handler named ``get_api_key`` that takes no key is not a key route;
    a route that depends on ``deps.get_api_key`` is."""
    scratch = FastAPI()

    def get_api_key(api_key_id: str) -> dict:
        return {"id": api_key_id}

    async def takes_a_key(_owner=Depends(deps.get_api_key)) -> dict:
        return {}

    scratch.get("/api-keys/{api_key_id}")(get_api_key)
    scratch.get("/takes-a-key")(takes_a_key)
    assert get_api_key.__name__ == deps.get_api_key.__name__
    assert set(_key_routes(scratch)) == {("GET", "/takes-a-key")}


def test_the_api_key_routes_are_exactly_these():
    found = set(_key_routes(app))
    expected = _expected()
    extra = sorted(found - expected)
    missing = sorted(expected - found)
    assert not extra, (
        f"{', '.join(f'{m} {p}' for m, p in extra)} takes an API key but is not "
        "listed here"
    )
    assert not missing, (
        f"{', '.join(f'{m} {p}' for m, p in missing)} no longer takes an API key"
    )


def test_each_api_key_route_has_one_synchronous_step():
    wrong = {}
    for route, calls in sorted(_key_routes(app).items()):
        sync = []
        for call in calls:
            if _is_sync(call) and all(call is not seen for seen in sync):
                sync.append(call)
        if not (len(sync) == 1 and any(sync[0] is entry for entry in ENTRY_POINTS)):
            wrong[route] = [_name(call) for call in sync]
    assert not wrong, "synchronous callables on routes that take an API key:\n" + (
        "\n".join(f"{m} {p}: {names}" for (m, p), names in wrong.items())
    )


def test_the_counts_match_the_tables():
    assert len(KEY_ROUTES) == 15
    assert len(MODULE_KEY_ROUTES) == 2
    assert len(READER_ROUTES) == 5
    assert not KEY_ROUTES & MODULE_KEY_ROUTES
    assert not (KEY_ROUTES | MODULE_KEY_ROUTES) & READER_ROUTES
