"""The Strict-Transport-Security header the API sends, whole, and where it does not.

``SecurityHeadersMiddleware`` sends it outside development on every response
except a CORS preflight (``test_security_headers.py`` holds that it is absent
in development and carries a one-year ``max-age`` otherwise). These hold the
rest of its value, ``includeSubDomains`` and ``preload``, on responses of the
real application whatever their status, and the one exception: a preflight is
answered by ``CORSMiddleware``, the outermost layer (``main.py`` says why), so
it carries none of the four headers the page names.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from backend.app.main import app

pytestmark = [pytest.mark.unit]

HSTS = "max-age=31536000; includeSubDomains; preload"
FOUR = (
    "strict-transport-security",
    "content-security-policy",
    "x-frame-options",
    "x-content-type-options",
)
#: A route that exists (200), one that needs a sign-in (401) and one that does
#: not exist (404): the header does not depend on the answer.
REQUESTS = [
    ("GET", "/api/v1/openapi.json", 200),
    ("GET", "/api/v1/experiments/", 401),
    ("GET", "/api/v1/no-such-route", 404),
]
ORIGIN = "http://localhost:3100"


@pytest.mark.parametrize("environment", ["staging", "production"])
@pytest.mark.parametrize("method, path, status", REQUESTS)
def test_outside_development_every_response_carries_the_whole_hsts_value(
    environment, method, path, status
):
    with patch("backend.app.core.config.settings.ENVIRONMENT", environment):
        response = TestClient(app).request(method, path)
    assert response.status_code == status
    assert response.headers["strict-transport-security"] == HSTS


def test_in_development_no_response_carries_it():
    with patch("backend.app.core.config.settings.ENVIRONMENT", "development"):
        response = TestClient(app).get("/api/v1/openapi.json")
    assert "strict-transport-security" not in response.headers


@pytest.mark.parametrize(
    "origin, status",
    [(ORIGIN, 200), ("https://not-an-allowed-origin.example", 400)],
)
def test_a_cors_preflight_carries_none_of_the_four_headers(origin, status):
    client = TestClient(app)
    with patch("backend.app.core.config.settings.ENVIRONMENT", "production"):
        preflight = client.options(
            "/api/v1/experiments/",
            headers={"Origin": origin, "Access-Control-Request-Method": "GET"},
        )
        # The control: the same path, not a preflight, carries all four.
        plain = client.get("/api/v1/experiments/", headers={"Origin": origin})
    assert preflight.status_code == status, preflight.text
    assert [h for h in FOUR if h in preflight.headers] == []
    assert [h for h in FOUR if h not in plain.headers] == []
