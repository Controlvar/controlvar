"""The Strict-Transport-Security header the API sends, whole.

``SecurityHeadersMiddleware`` sends it on every response outside development
(``test_security_headers.py`` holds that it is absent in development and
carries a one-year ``max-age`` otherwise). This holds the rest of its value,
``includeSubDomains`` and ``preload``, on a response from the real application.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from backend.app.main import app

pytestmark = [pytest.mark.unit]

HSTS = "max-age=31536000; includeSubDomains; preload"


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_outside_development_every_response_carries_the_whole_hsts_value(environment):
    with patch("backend.app.core.config.settings.ENVIRONMENT", environment):
        response = TestClient(app).get("/api/v1/openapi.json")
    assert response.headers["strict-transport-security"] == HSTS


def test_in_development_no_response_carries_it():
    with patch("backend.app.core.config.settings.ENVIRONMENT", "development"):
        response = TestClient(app).get("/api/v1/openapi.json")
    assert "strict-transport-security" not in response.headers
