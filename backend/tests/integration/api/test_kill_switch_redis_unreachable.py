"""The kill switch turns a flag off for everyone while Redis is unreachable.

Under the test settings the rate limiter is off (``main.py`` builds it with
``enabled=not settings.is_test``), so every other kill-switch test runs without
the one component that touches Redis on the flag routes: since #630 the flag
routes have no Redis cache of their own, and the limiter is the only thing on
the path to turn a flag off, or to ask whether it is off, that opens a Redis
connection. A kill switch that answered 500 or 429 while Redis was down would
leave the flag on for everyone, and nothing else here would notice.

So this file serves a hardened copy of the real application: the limiter is on,
in the position ``main.py`` gives it, and it points at a local port that
refuses connections. As a DEVELOPER who is not a superuser, the test turns a
flag off by each of the four routes that do it and then asks the SDK route for
the answer.

Why the client does not raise: in production order the limiter sits OUTSIDE
``UnhandledErrorMiddleware`` (the innermost layer), so a fault in the limiter
is answered by Starlette's own ``ServerErrorMiddleware``. With the TestClient
default that error is raised in the test instead of being returned as the 500
an operator would see. ``raise_server_exceptions=False`` makes the test see the
status code the caller gets.

The gate has a positive control, because an off limiter and a reachable Redis
both look like success: the log must hold exactly one switch to per-process
counting (the warning) and one debug line for every later request, and the
rate-limit headers must be present on both routes.
"""

import contextlib
import copy
import io
import json
import logging
import socket
import uuid

import pytest
import structlog
from fastapi.testclient import TestClient
from starlette.middleware import Middleware

from backend.app.core.config import settings
from backend.app.main import app
from backend.app.middleware import rate_limiter
from backend.app.middleware.rate_limiter import RateLimitMiddleware
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

LIMITER_LOGGER = "backend.app.middleware.rate_limiter"

#: The four ways to turn a flag off: (id, method, route suffix, body).
OFF_ROUTES = [
    pytest.param("POST", "toggle", {"reason": "kill"}, id="toggle"),
    pytest.param("POST", "disable", {"reason": "kill"}, id="disable"),
    pytest.param("POST", "deactivate", None, id="deactivate"),
    pytest.param("PUT", "", {"is_active": False}, id="update-is-active-false"),
]


def _refused_port() -> int:
    """A local port nothing listens on: bound by this process, then closed."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _hardened(monkeypatch) -> TestClient:
    """``main.app`` with the rate limiter on, in its production position.

    A shallow copy, so the application other tests use keeps its own stack.
    The copy shares ``dependency_overrides`` with the original; the autouse
    fixture of the integration conftest puts that dict back after each test.
    """
    middleware = []
    seen = False
    for layer in app.user_middleware:
        kwargs = dict(layer.kwargs)
        if layer.cls is RateLimitMiddleware:
            kwargs["enabled"] = True
            seen = True
        middleware.append(Middleware(layer.cls, *layer.args, **kwargs))
    assert seen, "main.app no longer has RateLimitMiddleware"

    hardened = copy.copy(app)
    hardened.user_middleware = middleware
    hardened.middleware_stack = None
    return TestClient(hardened, raise_server_exceptions=False)


@contextlib.contextmanager
def _json_logs(level: str = "DEBUG"):
    """Render every log record as the production JSON line, into a buffer."""
    from backend.app.core.logger import configure_logging

    root = logging.root
    handlers, root_level = list(root.handlers), root.level
    saved = structlog.get_config()
    buffer = io.StringIO()
    configure_logging(log_level=level, json_logs=True, stream=buffer)
    try:
        yield buffer
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in handlers:
            root.addHandler(handler)
        root.setLevel(root_level)
        structlog.configure(**saved)


def _limiter_lines(buffer: io.StringIO) -> list:
    lines = [json.loads(line) for line in buffer.getvalue().splitlines() if line]
    return [line for line in lines if line.get("logger") == LIMITER_LOGGER]


@pytest.fixture
def active_flag(db_session, developer_user):
    flag = FeatureFlag(
        key=f"kill-redis-{uuid.uuid4().hex[:8]}",
        name="Kill switch with Redis down",
        status=FeatureFlagStatus.ACTIVE,
        owner_id=developer_user.id,
        rollout_percentage=100,
    )
    db_session.add(flag)
    db_session.commit()
    db_session.refresh(flag)
    yield flag
    db_session.rollback()
    db_session.query(FeatureFlag).filter(FeatureFlag.id == flag.id).delete()
    db_session.commit()


@pytest.fixture
def redis_unreachable(monkeypatch):
    """Point Redis at a closed local port and retry it on every request."""
    assert settings.is_test, "this file is written for the test settings"
    monkeypatch.setattr(settings, "REDIS_HOST", "127.0.0.1")
    monkeypatch.setattr(settings, "REDIS_PORT", _refused_port())
    monkeypatch.setattr(rate_limiter, "REDIS_RETRY_SECONDS", 0.0)


@pytest.mark.parametrize("method, suffix, body", OFF_ROUTES)
def test_the_kill_switch_answers_200_and_the_flag_is_off_with_redis_unreachable(
    method,
    suffix,
    body,
    developer_client,
    db_session,
    active_flag,
    redis_unreachable,
    monkeypatch,
):
    # developer_client installed the authentication overrides; its own client
    # is not used, the hardened copy shares the overrides.
    assert developer_client is not None and app.dependency_overrides
    client = _hardened(monkeypatch)
    off_url = f"/api/v1/feature-flags/{active_flag.id}"
    if suffix:
        off_url = f"{off_url}/{suffix}"
    evaluate_url = f"/api/v1/feature-flags/evaluate/{active_flag.key}"
    api_key = {"X-API-Key": "kill-switch-test-key"}
    n_requests = 3

    with _json_logs() as buffer:
        off = client.request(method, off_url, json=body)
        assert off.status_code == 200, (
            f"{method} {off_url} answered {off.status_code} with Redis "
            f"unreachable: {off.text[:200]}"
        )

        db_session.expire_all()
        stored = (
            db_session.query(FeatureFlag).filter(FeatureFlag.id == active_flag.id).one()
        )
        assert stored.status == FeatureFlagStatus.INACTIVE

        answer = client.get(evaluate_url, params={"user_id": "u-1"}, headers=api_key)
        assert answer.status_code == 200, answer.text
        assert answer.json()["enabled"] is False
        assert answer.json()["reason"] == "inactive"

        again = client.get(evaluate_url, params={"user_id": "u-2"}, headers=api_key)
        assert again.status_code == 200, again.text
        assert again.json()["enabled"] is False

    statuses = [off.status_code, answer.status_code, again.status_code]
    assert 429 not in statuses and not [s for s in statuses if s >= 500]

    # Positive control: the limiter was in the path, and it did count.
    assert off.headers.get("X-RateLimit-Limit") == "300"
    assert answer.headers.get("X-RateLimit-Limit") == "6000"

    lines = _limiter_lines(buffer)
    switches = [
        line
        for line in lines
        if line["level"] == "warning"
        and line.get("rate_limiter") == "per_process"
        and line.get("reason") == "ConnectionError"
    ]
    assert len(switches) == 1, f"{len(switches)} switch lines, expected 1" + (
        ": fault not in path" if not switches else ""
    )
    assert [line for line in lines if line["level"] == "warning"] == switches
    still = [
        line
        for line in lines
        if line["level"] == "debug"
        and line["event"] == rate_limiter.STILL_UNAVAILABLE_MESSAGE
    ]
    assert len(still) == n_requests - 1, (
        f"{len(still)} 'still unavailable' lines, expected {n_requests - 1}"
    )
