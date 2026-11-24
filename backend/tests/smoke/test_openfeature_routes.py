"""The two deprecated definition listings are gone (#737, #228).

``GET /api/v1/edge/bootstrap`` and ``GET /api/v1/openfeature/flags`` were
deprecated in favour of ``GET /api/v1/sdk/ruleset`` (#226, #241) and are now
removed. This pins the removal three ways, because each one alone can pass
while the route still answers:

* the mounted route set, read through ``effective_route_contexts`` (as in
  ``test_flag_change_routes_guarded.py``): FastAPI keeps ``include_router`` as
  a lazy entry, so plain ``app.routes`` would see nothing at all, and a
  positive control proves the reader sees a route that is still mounted;
* the OpenAPI document, since a route mounted with ``include_in_schema=False``
  is absent there and still answers (the committed snapshots are pinned to
  the generator by ``test_openapi_snapshot.py``, so this does not read them);
* a request with a valid API key, which must get exactly 404. A 401 or 403
  would mean the route is still mounted and only refusing the key.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from backend.app.api import deps
from backend.app.main import app

pytestmark = [pytest.mark.smoke, pytest.mark.regression]

REMOVED = ("/api/v1/edge/bootstrap", "/api/v1/openfeature/flags")
REMOVED_PREFIXES = ("/api/v1/edge", "/api/v1/openfeature")

# A route that is still mounted and authenticates with the same dependency.
STILL_MOUNTED = "/api/v1/feature-flags/user/{user_id}"


def _route_paths(application) -> set:
    """Every mounted HTTP route path, lazy ``include_router`` entries included."""
    found = set()
    for route in application.routes:
        contexts = getattr(route, "effective_route_contexts", None)
        if contexts is None:
            path = getattr(route, "path", None)
            if path is not None:
                found.add(path)
            continue
        for ctx in contexts() if callable(contexts) else contexts:
            found.add(ctx.path)
    return found


def _under_removed_prefix(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in REMOVED_PREFIXES)


def test_the_reader_sees_a_route_that_is_still_mounted():
    """Positive control: without it, a reader that saw nothing would pass."""
    assert STILL_MOUNTED in _route_paths(app)


def test_no_route_is_mounted_under_the_removed_prefixes():
    assert sorted(p for p in _route_paths(app) if _under_removed_prefix(p)) == []


def test_the_schema_has_no_operation_under_the_removed_prefixes():
    paths = app.openapi()["paths"]
    assert STILL_MOUNTED in paths
    assert sorted(p for p in paths if _under_removed_prefix(p)) == []


@pytest.fixture
def key_client(monkeypatch):
    """A client whose API key is accepted, with no database behind it."""

    class _NoFlags:
        def __init__(self, db):
            pass

        def get_user_flags(self, user_id, context=None, environment="production"):
            return {}

    monkeypatch.setattr(
        "backend.app.api.v1.endpoints.feature_flags.FeatureFlagService", _NoFlags
    )
    session = MagicMock()
    session.query.return_value.filter.return_value.all.return_value = []
    session.query.return_value.filter.return_value.first.return_value = None
    session.query.return_value.all.return_value = []
    app.dependency_overrides[deps.get_api_key] = lambda: MagicMock(id="probe-owner")
    app.dependency_overrides[deps.get_db] = lambda: session
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.pop(deps.get_api_key, None)
        app.dependency_overrides.pop(deps.get_db, None)


def test_the_valid_key_reaches_a_route_that_is_still_mounted(key_client):
    """Positive control: the key the next test sends is accepted."""
    response = key_client.get(
        "/api/v1/feature-flags/user/probe-user", headers={"X-API-Key": "k"}
    )
    assert response.status_code == 200
    assert response.json() == {}


@pytest.mark.parametrize("path", REMOVED)
def test_a_removed_listing_answers_404_to_a_valid_key(key_client, path):
    response = key_client.get(path, headers={"X-API-Key": "k"})
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}
