"""An API key does not sign anyone in to the routes that manage the platform.

API keys authenticate the routes an SDK calls (tracking, flag evaluation, the
flag ruleset). The routes that manage experiments, feature flags and users take
a signed-in user's bearer token only: a request that carries an API key and no
token is refused with ``401``, as a request with neither is. The
key here is never looked up, so no database is needed; what is held is that the
management routes do not read the ``X-API-Key`` header.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.app.main import app

pytestmark = [pytest.mark.unit]

KEY = {"X-API-Key": "eptk_" + "0" * 32}


@pytest.mark.parametrize(
    "method, path",
    [
        ("GET", "/api/v1/experiments/"),
        ("POST", "/api/v1/experiments/"),
        ("GET", "/api/v1/feature-flags/"),
        ("POST", "/api/v1/feature-flags/"),
        ("GET", "/api/v1/users/"),
    ],
)
def test_a_management_route_refuses_a_request_that_carries_only_an_api_key(
    method, path
):
    response = TestClient(app).request(method, path, headers=KEY, json={})
    assert response.status_code == 401, response.text
    assert response.json() == {"detail": "Not authenticated"}
