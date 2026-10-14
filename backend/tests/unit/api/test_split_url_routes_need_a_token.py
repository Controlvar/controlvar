"""The split-URL routes answer 401 without a valid bearer token.

``POST /api/v1/experiments/`` (which creates a ``split_url`` experiment) and
``GET /api/v1/experiments/{id}/split-url/preview`` are declared in the core
experiments router in every profile. A request with no ``Authorization``
header, a token that is not a JWT, or a JWT signed with another key is
refused with 401 before the body or the experiment is looked at.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi.testclient import TestClient

from backend.app.core.config import settings
from backend.app.main import app

pytestmark = [pytest.mark.unit, pytest.mark.regression]

PREVIEW = f"/api/v1/experiments/{uuid.uuid4()}/split-url/preview"
SPLIT_URL_EXPERIMENT = {
    "name": "Landing pages",
    "hypothesis": "the new page converts better",
    "experiment_type": "split_url",
    "variants": [
        {"name": "control", "is_control": True, "traffic_allocation": 50},
        {"name": "new", "is_control": False, "traffic_allocation": 50},
    ],
    "metrics": [
        {"name": "Signup", "event_name": "signup", "metric_type": "conversion"}
    ],
}


@pytest.fixture(autouse=True)
def _local_sign_in(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)


def _foreign_token() -> str:
    claims = {
        "sub": str(uuid.uuid4()),
        "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
    }
    return jwt.encode(claims, "not-the-servers-key-" + "x" * 32, algorithm="HS256")


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer not-a-jwt"},
        {"Authorization": f"Bearer {_foreign_token()}"},
    ],
    ids=["missing", "malformed", "foreign"],
)
@pytest.mark.parametrize(
    "method, path, body",
    [
        ("GET", PREVIEW + "?user_id=user-1", None),
        ("POST", "/api/v1/experiments/", SPLIT_URL_EXPERIMENT),
    ],
    ids=["preview", "create"],
)
def test_a_split_url_route_without_a_valid_token_is_401(method, path, body, headers):
    response = TestClient(app).request(method, path, json=body, headers=headers)
    assert response.status_code == 401, response.text
