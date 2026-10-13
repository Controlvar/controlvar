"""The routes that have a rate limit of their own, and what each limit is.

``RATE_LIMIT_CONFIG`` (``backend/app/middleware/rate_limiter.py``) gives some
routes their own per-IP count; every other route takes the SDK or export
prefix's limit, or the default. These tests hold the numbers themselves, so a
change to a route's limit, or a route that gains or loses its own limit, fails
here and is made on purpose. ``test_rate_limiter.py`` holds how a limit is
applied (the 429, its headers, the order exact entries and prefixes are tried
in).
"""

from __future__ import annotations

import pytest

from backend.app.middleware.rate_limiter import RATE_LIMIT_CONFIG

pytestmark = [pytest.mark.unit]

MINUTE = 60


def test_sign_in_takes_ten_requests_a_minute():
    assert RATE_LIMIT_CONFIG["/api/v1/auth/token"] == (10, MINUTE)
    assert RATE_LIMIT_CONFIG["/api/v1/auth/login"] == (10, MINUTE)


def test_sign_up_and_the_password_reset_routes_take_five_a_minute():
    for path in (
        "/api/v1/auth/signup",
        "/api/v1/auth/forgot-password",
        "/api/v1/auth/reset-password",
        "/api/v1/users/me/password",
    ):
        assert RATE_LIMIT_CONFIG[path] == (5, MINUTE), path


def test_the_sso_routes_take_ten_and_thirty_a_minute():
    assert RATE_LIMIT_CONFIG["/api/v1/auth/sso/exchange"] == (10, MINUTE)
    assert RATE_LIMIT_CONFIG["/api/v1/auth/sso/login"] == (30, MINUTE)


def test_the_audit_log_export_takes_ten_a_minute_on_its_own_count():
    assert RATE_LIMIT_CONFIG["/api/v1/audit-logs/export"] == (10, MINUTE)


def test_batch_assignment_takes_sixty_a_minute_on_its_own_count():
    assert RATE_LIMIT_CONFIG["/api/v1/tracking/assign/batch"] == (60, MINUTE)


def test_these_are_all_the_routes_with_a_limit_of_their_own():
    assert set(RATE_LIMIT_CONFIG) == {
        "/api/v1/auth/token",
        "/api/v1/auth/login",
        "/api/v1/auth/signup",
        "/api/v1/auth/forgot-password",
        "/api/v1/auth/reset-password",
        "/api/v1/users/me/password",
        "/api/v1/auth/sso/exchange",
        "/api/v1/auth/sso/login",
        "/api/v1/tracking/assign/batch",
        "/api/v1/audit-logs/export",
        "/api/v1/ai/design",
    }
