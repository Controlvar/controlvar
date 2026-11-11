"""``POST /auth/confirm`` takes five attempts a minute, as sign-up does (#700).

The route is where a caller guesses the 6-digit sign-up code, so it is limited
like ``/auth/signup``. Limits are keyed on the exact request path, so an entry
in ``RATE_LIMIT_CONFIG`` that does not match the route's real path limits
nothing. The path here comes from the application's own router
(``app.url_path_for``), and the real ``RateLimitMiddleware`` is driven with
``enabled=True`` (the application turns it off under test).

Two kinds of test:

* a stub route at the real path behind an enabled in-memory limiter, which
  answers 204 until the limiter says 429;
* the real application's middleware stack and real route, which answer 404
  (self sign-up is off) until the limiter says 429. The limiter runs before
  routing, so the flag does not change the 429.
"""

from __future__ import annotations

import copy
from typing import List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware import Middleware

from backend.app.core.config import settings
from backend.app.main import app as real_app
from backend.app.middleware import rate_limiter
from backend.app.middleware.rate_limiter import (
    RATE_LIMIT_CONFIG,
    RateLimitMiddleware,
    SlidingWindowRateLimiter,
    resolve_rate_limit,
    resolve_rate_limit_bucket,
)

pytestmark = [pytest.mark.unit, pytest.mark.regression]

LIMIT = 5
CONFIRM = "/api/v1/auth/confirm"
SIGNUP = "/api/v1/auth/signup"


@pytest.fixture
def path() -> str:
    return real_app.url_path_for("confirm_signup")


def _limited_app(*paths: str):
    routes = FastAPI()

    for p in paths:

        @routes.post(p, status_code=204)
        async def stub():
            return None

    middleware = RateLimitMiddleware(routes, enabled=True)
    # In memory: a shared Redis would carry counts between runs.
    middleware._limiter = SlidingWindowRateLimiter()
    return middleware


def _real_stack_client(monkeypatch) -> TestClient:
    """``main.app`` with the rate limiter enabled and counting in process.

    A shallow copy, so the application other tests use keeps its own stack.
    Same construction as ``_hardened_client`` in test_metric_labels_bounded.py.
    """
    middleware: List[Middleware] = []
    for layer in real_app.user_middleware:
        kwargs = dict(layer.kwargs)
        if layer.cls is RateLimitMiddleware:
            kwargs["enabled"] = True
        middleware.append(Middleware(layer.cls, *layer.args, **kwargs))
    assert RateLimitMiddleware in [layer.cls for layer in middleware], (
        "main.app no longer has RateLimitMiddleware"
    )
    # Counted in process: the enabled limiter would otherwise open Redis.
    monkeypatch.setattr(
        rate_limiter, "RedisRateLimiter", rate_limiter.SlidingWindowRateLimiter
    )
    real = copy.copy(real_app)
    real.user_middleware = middleware
    real.middleware_stack = None
    return TestClient(real, raise_server_exceptions=False)


def test_the_route_is_where_the_limit_is(path):
    assert path == CONFIRM
    assert path in RATE_LIMIT_CONFIG


def test_confirm_resolves_to_five_a_minute_on_its_own_counter():
    assert resolve_rate_limit(CONFIRM) == (5, 60)
    assert resolve_rate_limit_bucket(CONFIRM)[0] == CONFIRM


def test_the_sixth_attempt_in_a_minute_is_429(path):
    client = TestClient(_limited_app(path))
    statuses = [client.post(path).status_code for _ in range(LIMIT + 1)]
    assert statuses == [204] * LIMIT + [429], statuses


def test_confirm_keeps_its_own_count_apart_from_sign_up(path):
    client = TestClient(_limited_app(SIGNUP, path))
    signups = [client.post(SIGNUP).status_code for _ in range(LIMIT)]
    assert signups == [204] * LIMIT, signups
    assert client.post(path).status_code == 204


@pytest.mark.parametrize(
    "provider, self_signup",
    [("local", False), ("cognito", False)],
    ids=["local", "cognito-self-signup-off"],
)
def test_the_real_stack_limits_the_real_route(monkeypatch, provider, self_signup):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", provider)
    monkeypatch.setattr(settings, "COGNITO_SELF_SIGNUP_ENABLED", self_signup)
    client = _real_stack_client(monkeypatch)
    path = real_app.url_path_for("confirm_signup")
    body = {"username": "someone", "confirmation_code": "123456"}
    responses = [client.post(path, json=body) for _ in range(LIMIT + 1)]
    statuses = [r.status_code for r in responses]
    assert statuses == [404] * LIMIT + [429], statuses
    assert responses[-1].headers["Retry-After"] == "60"
