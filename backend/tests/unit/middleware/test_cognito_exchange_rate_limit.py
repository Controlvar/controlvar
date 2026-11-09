"""The Cognito code exchange has a strict limit of its own (#708).

``POST /api/v1/auth/cognito/exchange`` turns a sign-in code into a session, so
it is limited like the password sign-in: 10 a minute per client address, on
its own count, not the default 300. The path comes from the application's
own router (``app.url_path_for``), not from a copy of the string in the
config, and the real ``RateLimitMiddleware`` is driven with ``enabled=True``
(the application turns it off under test), with an in-memory counter.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.main import app as real_app
from backend.app.middleware.rate_limiter import (
    RateLimitMiddleware,
    SlidingWindowRateLimiter,
)

pytestmark = [pytest.mark.unit, pytest.mark.regression]

LIMIT = 10


@pytest.fixture
def paths():
    return (
        real_app.url_path_for("exchange_cognito_code"),
        real_app.url_path_for("get_auth_config"),
    )


def _limited_app(exchange: str, config: str):
    routes = FastAPI()

    @routes.post(exchange)
    async def exchange_route():
        return {}

    @routes.get(config)
    async def config_route():
        return {}

    middleware = RateLimitMiddleware(routes, enabled=True)
    # In memory: a shared Redis would carry counts between runs.
    middleware._limiter = SlidingWindowRateLimiter()
    return middleware


def test_the_routes_are_where_the_limits_are(paths):
    assert paths == ("/api/v1/auth/cognito/exchange", "/api/v1/auth/config")


def test_the_eleventh_exchange_in_a_minute_is_429_and_the_config_still_answers(
    paths,
):
    exchange, config = paths
    client = TestClient(_limited_app(exchange, config))

    statuses = [client.post(exchange).status_code for _ in range(LIMIT + 1)]

    assert statuses == [200] * LIMIT + [429], statuses
    assert client.get(config).status_code == 200
