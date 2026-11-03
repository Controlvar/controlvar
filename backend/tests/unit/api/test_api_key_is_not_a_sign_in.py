"""An API key does not sign anyone in to the routes that manage the platform.

API keys authenticate the routes an SDK calls (tracking, flag evaluation, the
flag ruleset). A key that carries the ``mcp:read`` scope is also accepted by
four read operations: list flags, list experiments, read an experiment and
read its results (``backend/app/api/read_scope.py``;
``backend/tests/smoke/test_mcp_read_routes.py`` pins the set). Every other
route that manages experiments, feature flags and users takes a signed-in
user's bearer token only: a request that carries an API key and no token is
refused with ``401``, as a request with neither is. The key is never looked up
there, so no database is needed; what is held is that those routes do not read
the ``X-API-Key`` header.

On the four read operations the key *is* looked up, so an unknown key is
answered as on an SDK route: ``401 "Invalid API Key"``. That half replaces the
database with a session that finds no key. What a real key, its scope, its
expiry and its owner decide there is in
``backend/tests/integration/api/test_mcp_read_scope_api.py``.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from backend.app.api import deps
from backend.app.main import app

pytestmark = [pytest.mark.unit]

KEY = {"X-API-Key": "eptk_" + "0" * 32}
ID = uuid.UUID("00000000-0000-4000-8000-000000000001")

#: The four read operations that also take an ``mcp:read`` key.
MCP_READ_PATHS = [
    "/api/v1/feature-flags/",
    "/api/v1/experiments/",
    f"/api/v1/experiments/{ID}",
    f"/api/v1/experiments/{ID}/results",
]


@pytest.mark.parametrize(
    "method, path",
    [
        ("POST", "/api/v1/experiments/"),
        ("PUT", f"/api/v1/experiments/{ID}"),
        ("POST", "/api/v1/feature-flags/"),
        ("DELETE", f"/api/v1/feature-flags/{ID}"),
        ("GET", "/api/v1/users/"),
        # Reads outside the mcp:read set take a sign-in only, too.
        ("GET", f"/api/v1/feature-flags/{ID}"),
        ("GET", f"/api/v1/experiments/{ID}/daily-results"),
        ("GET", f"/api/v1/results/{ID}"),
    ],
)
def test_a_route_outside_the_sdk_and_mcp_read_sets_refuses_a_request_that_carries_only_an_api_key(
    method, path
):
    response = TestClient(app).request(method, path, headers=KEY, json={})
    assert response.status_code == 401, response.text
    assert response.json() == {"detail": "Not authenticated"}


@pytest.fixture
def no_key_in_the_database():
    """A session in which no API key is found."""
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None
    saved = dict(app.dependency_overrides)
    app.dependency_overrides[deps.get_db] = lambda: db
    try:
        yield db
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(saved)


@pytest.mark.parametrize("path", MCP_READ_PATHS)
def test_an_mcp_read_route_looks_the_key_up_and_refuses_an_unknown_one(
    no_key_in_the_database, path
):
    response = TestClient(app).get(path, headers=KEY)
    assert response.status_code == 401, response.text
    assert response.json() == {"detail": "Invalid API Key"}
    assert response.headers["www-authenticate"] == "APIKey"
    no_key_in_the_database.query.assert_called()


@pytest.mark.parametrize("path", MCP_READ_PATHS)
def test_an_mcp_read_route_answers_a_request_with_no_credential_as_before(path):
    response = TestClient(app).get(path)
    assert response.status_code == 401, response.text
    assert response.json() == {"detail": "Not authenticated"}
    assert response.headers["www-authenticate"] == "Bearer"
