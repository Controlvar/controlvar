"""The visual editor's two public sign-in routes take ten requests a minute
each, per client address (#1545).

``POST /api/v1/visual-editor/tickets/claim`` and
``POST /api/v1/visual-editor/sessions`` have exact ``RATE_LIMIT_CONFIG``
entries. The paths come from the application's own router
(``app.url_path_for``), and the requests go through the real routes with the
real ``RateLimitMiddleware`` in front (``enabled=True``; the application turns
it off under test), counting in memory. Every request here is refused before
it reaches the database (a claim of the wrong length, an exchange with no
``Origin``): the limiter counts a request whatever its answer.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.app.main import app as real_app
from backend.app.middleware.rate_limiter import (
    RateLimitMiddleware,
    SlidingWindowRateLimiter,
    rate_limit_key,
)

pytestmark = [pytest.mark.unit]

LIMIT = 10


@pytest.fixture
def client():
    limited = RateLimitMiddleware(real_app, enabled=True)
    # In memory: a shared Redis would carry counts between runs.
    limited._limiter = SlidingWindowRateLimiter()
    return TestClient(limited)


def _claim(client):
    return client.post(
        real_app.url_path_for("claim_editor_link"),
        json={"ticket": "t", "claim": "too-short"},
    )


def _exchange(client):
    return client.post(
        real_app.url_path_for("open_editor_session"),
        json={"claim": "too-short", "page_origin": "https://shop.example.com"},
    )


@pytest.mark.parametrize(
    "send, refused_with",
    [(_claim, 422), (_exchange, 403)],
    ids=["claim", "exchange"],
)
def test_the_eleventh_request_in_a_minute_is_429(client, send, refused_with):
    for i in range(LIMIT):
        response = send(client)
        assert response.status_code == refused_with, (i, response.text)
        assert response.headers["X-RateLimit-Limit"] == str(LIMIT)
        assert response.headers["X-RateLimit-Remaining"] == str(LIMIT - i - 1)

    refused = send(client)
    assert refused.status_code == 429
    assert refused.headers["Retry-After"] == "60"
    assert refused.headers["X-RateLimit-Limit"] == str(LIMIT)


def test_the_two_routes_count_apart():
    claim = rate_limit_key("10.0.0.1", real_app.url_path_for("claim_editor_link"))
    exchange = rate_limit_key("10.0.0.1", real_app.url_path_for("open_editor_session"))
    assert claim != exchange
