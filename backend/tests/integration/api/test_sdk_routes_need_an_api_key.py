"""The API-key routes refuse a request without a valid ``X-API-Key``.

Nothing here overrides ``deps.get_api_key``: only the database is replaced,
so each route answers as it would for a client. No key is 401 ``API key
missing``; an unknown key is 401 ``Invalid API Key``.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.app.api import deps
from backend.app.main import app

pytestmark = [pytest.mark.integration]

#: (method, path, JSON body) of each route the endpoints page lists with the
#: ``X-API-Key`` header.
ROUTES = [
    ("GET", "/api/v1/feature-flags/user/user-1", None),
    ("POST", "/api/v1/tracking/assign", {"experiment_key": "k", "user_id": "user-1"}),
    ("POST", "/api/v1/tracking/track", {"event_type": "purchase", "user_id": "user-1"}),
    (
        "POST",
        "/api/v1/tracking/events",
        {"event_name": "purchase", "event_type": "purchase", "user_id": "user-1"},
    ),
    ("GET", "/api/v1/tracking/assignments/user-1", None),
]
IDS = [f"{m} {p}" for m, p, _ in ROUTES]


@pytest.fixture
def client(db_session):
    def override_get_db():
        yield db_session

    app.dependency_overrides[deps.get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.pop(deps.get_db, None)


@pytest.mark.parametrize("method, path, body", ROUTES, ids=IDS)
def test_no_key_is_401(client, method, path, body):
    response = client.request(method, path, json=body)
    assert response.status_code == 401, response.text
    assert response.json()["detail"] == "API key missing"


@pytest.mark.parametrize("method, path, body", ROUTES, ids=IDS)
def test_an_unknown_key_is_401(client, method, path, body):
    response = client.request(
        method, path, json=body, headers={"X-API-Key": "eptk_" + "0" * 32}
    )
    assert response.status_code == 401, response.text
    assert response.json()["detail"] == "Invalid API Key"
