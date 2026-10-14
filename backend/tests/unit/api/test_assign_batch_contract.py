"""The fixed parts of ``POST /api/v1/tracking/assign/batch`` (#441).

* the rate limit is its own (60 a minute), not the tracking prefix's, and the
  61st request in a minute is refused with 429 by the real middleware (the
  application turns the middleware off under test, so it is driven with
  ``enabled=True`` here, as ``test_own_password_rate_limit.py`` does);
* the two ``sdk:ruleset`` 403 texts, as literal strings: the other tests
  compare responses with the constants, a copy against a copy;
* ``AssignmentService.bulk_assign_users`` is gone: it skipped the holdout,
  mutual exclusion and targeting checks.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.api import sdk_scope
from backend.app.main import app as real_app
from backend.app.middleware.rate_limiter import (
    RateLimitMiddleware,
    SlidingWindowRateLimiter,
    resolve_rate_limit,
)
from backend.app.services.assignment_service import AssignmentService

pytestmark = [pytest.mark.unit]

PATH = "/api/v1/tracking/assign/batch"


@pytest.fixture
def path() -> str:
    return real_app.url_path_for("assign_users_to_experiment_batch")


def test_the_route_is_where_the_limit_is(path):
    assert path == PATH


def test_the_limit_is_sixty_a_minute(path):
    assert resolve_rate_limit(path) == (60, 60)
    assert resolve_rate_limit(path, sdk_limit_per_minute=100_000) == (60, 60)


def test_the_slash_form_is_not_the_route():
    """``/assign/batch/`` falls to the tracking prefix; it reaches no handler
    because FastAPI answers it with a 307 to the exact path (pinned in the
    integration tests), where this limit applies."""
    assert resolve_rate_limit(PATH + "/") == (6000, 60)


def _limited_app(path: str):
    routes = FastAPI()

    @routes.post(path)
    async def batch():
        return {}

    middleware = RateLimitMiddleware(routes, enabled=True)
    # In memory: a shared Redis would carry counts between runs.
    middleware._limiter = SlidingWindowRateLimiter()
    return middleware


def test_the_61st_request_in_a_minute_is_429(path):
    client = TestClient(_limited_app(path))
    statuses = [client.post(path).status_code for _ in range(61)]
    assert statuses == [200] * 60 + [429], statuses
    refused = client.post(path)
    assert refused.status_code == 429
    assert refused.headers["Retry-After"] == "60"


def test_a_request_over_the_limit_never_reaches_the_route(path):
    """The 429 is answered before the route runs, so nobody is assigned."""
    calls = []
    routes = FastAPI()

    @routes.post(path)
    async def batch():
        calls.append(1)
        return {}

    middleware = RateLimitMiddleware(routes, enabled=True)
    middleware._limiter = SlidingWindowRateLimiter()
    client = TestClient(middleware)
    statuses = [client.post(path).status_code for _ in range(62)]
    assert statuses == [200] * 60 + [429, 429], statuses
    assert len(calls) == 60


def test_the_missing_scope_text():
    assert sdk_scope.MISSING_SCOPE_DETAIL == (
        "This API key does not have the 'sdk:ruleset' scope. Create a key with "
        "the 'sdk:ruleset' scope for server-side SDK use."
    )


def test_the_owner_role_text():
    assert sdk_scope.OWNER_ROLE_DETAIL == (
        "This key's owner can no longer change feature flags or experiments, so "
        "the key is refused for server-side SDK use."
    )


def test_bulk_assign_users_is_gone():
    assert not hasattr(AssignmentService, "bulk_assign_users")
