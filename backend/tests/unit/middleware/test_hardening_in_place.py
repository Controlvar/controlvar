"""The hardening changes, as the running application has them (#1142).

The Permissions-Policy header, the CORS methods and headers, the
``CORS_ORIGINS`` setting, the per-process counting while Redis is down, and
the length limits on the tracking schemas, each read from the app or the
class it ships.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from backend.app.api import deps
from backend.app.core.config import Settings
from backend.app.main import app
from backend.app.middleware.rate_limiter import RedisRateLimiter
from backend.app.schemas.tracking import AssignmentBase, EventQueryParams


def test_permissions_policy_restricts_usb_and_magnetometer():
    policy = TestClient(app).get("/health/live").headers["permissions-policy"]
    features = {part.strip() for part in policy.split(",")}
    assert {"usb=()", "magnetometer=()"} <= features


def test_cors_allows_these_methods_and_headers_only():
    (cors,) = [m for m in app.user_middleware if m.cls is CORSMiddleware]
    assert cors.kwargs["allow_methods"] == ["GET", "POST", "PUT", "DELETE", "PATCH"]
    assert cors.kwargs["allow_headers"] == [
        "Authorization",
        "Content-Type",
        "X-API-Key",
    ]


def test_cors_origins_takes_a_comma_separated_string(monkeypatch):
    monkeypatch.setenv(
        "CORS_ORIGINS", "https://app.example.com,https://admin.example.com"
    )
    monkeypatch.delenv("BACKEND_CORS_ORIGINS", raising=False)
    assert Settings(_env_file=None).CORS_ORIGINS == [
        "https://app.example.com",
        "https://admin.example.com",
    ]


def test_without_redis_each_process_counts_on_its_own():
    first, second = RedisRateLimiter(), RedisRateLimiter()
    for limiter in (first, second):
        limiter._redis_client = None
        limiter._redis_available = False
        limiter._retry_at = float("inf")
    allowed = 0
    for limiter in (first, second):
        for _ in range(5):
            ok, _ = limiter.is_allowed("10.0.0.1:/api/v1/experiments/", 3, 60)
            allowed += ok
    # Two processes, a limit of 3: six requests pass, not three.
    assert allowed == 6


def test_the_tracking_schemas_bound_their_fields():
    for name in (
        "user_id",
        "session_id",
        "experiment_id",
        "feature_flag_id",
        "variant_id",
    ):
        (limit,) = [
            m.max_length
            for m in AssignmentBase.model_fields[name].metadata
            if hasattr(m, "max_length")
        ]
        assert limit == 255, name
    lengths = {
        name: [
            m.max_length
            for m in EventQueryParams.model_fields[name].metadata
            if hasattr(m, "max_length")
        ]
        for name in ("user_id", "event_type")
    }
    assert lengths == {"user_id": [255], "event_type": [100]}


def test_a_value_over_its_limit_is_refused_before_the_database_is_used():
    db = MagicMock()
    key = SimpleNamespace(
        id=uuid.uuid4(), user_id=uuid.uuid4(), scopes="", is_active=True
    )
    app.dependency_overrides[deps.get_db] = lambda: db
    app.dependency_overrides[deps.get_api_key] = lambda: key
    try:
        response = TestClient(app).post(
            "/api/v1/tracking/assign",
            json={"experiment_key": "checkout", "user_id": "u" * 256},
        )
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        app.dependency_overrides.pop(deps.get_api_key, None)
    assert response.status_code == 422
    assert db.mock_calls == []


def test_cors_sends_no_cookies_cross_origin_and_limits_are_per_ip_and_path():
    from backend.app.middleware.rate_limiter import rate_limit_key

    (cors,) = [m for m in app.user_middleware if m.cls is CORSMiddleware]
    assert cors.kwargs["allow_credentials"] is False
    assert (
        rate_limit_key("10.0.0.1", "/api/v1/experiments/")
        == "10.0.0.1:/api/v1/experiments/"
    )
    assert rate_limit_key("10.0.0.2", "/api/v1/experiments/") != rate_limit_key(
        "10.0.0.1", "/api/v1/experiments/"
    )


def test_a_short_or_placeholder_secret_key_stops_staging_and_production(monkeypatch):
    import pytest
    from pydantic import ValidationError

    strong = "k" * 64
    weak_keys = ("a" * 31, "secret", "changeme", "default-secret-key-for-testing")
    monkeypatch.delenv("TESTING", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    for environment in ("staging", "production", "prod"):
        for weak in weak_keys:
            with pytest.raises(ValidationError) as refused:
                Settings(_env_file=None, ENVIRONMENT=environment, SECRET_KEY=weak)
            assert "SECRET_KEY" in str(refused.value), (environment, weak)

    # TESTING relaxes nothing: with it set, staging and production refuse to
    # start at all, whatever the key.
    monkeypatch.setenv("TESTING", "true")
    for environment in ("staging", "production"):
        with pytest.raises(ValidationError) as refused:
            Settings(_env_file=None, ENVIRONMENT=environment, SECRET_KEY=strong)
        assert "TESTING" in str(refused.value), environment

    # Development and test take any key.
    monkeypatch.delenv("TESTING", raising=False)
    for environment in ("development", "test"):
        assert Settings(_env_file=None, ENVIRONMENT=environment, SECRET_KEY="secret")
