"""
Tests for the rate limiting middleware.

Covers:
- SlidingWindowRateLimiter (in-memory fallback)
- RedisRateLimiter (Redis-backed + fallback behaviour)
- RateLimitMiddleware (end-to-end HTTP layer)
- Client IP extraction from X-Forwarded-For
- Route-specific rate limit configuration
"""

import contextlib
import inspect
import io
import json
import logging
import time
from unittest.mock import MagicMock, PropertyMock

import pytest
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.middleware.rate_limiter import (
    DEFAULT_RATE_LIMIT,
    DEFAULT_SDK_RATE_LIMIT_PER_MINUTE,
    EXPORT_RATE_LIMIT,
    RATE_LIMIT_CONFIG,
    REDIS_RETRY_SECONDS,
    RateLimitMiddleware,
    RedisRateLimiter,
    SlidingWindowRateLimiter,
    _get_client_ip,
    rate_limit_key,
    resolve_rate_limit,
)

# ---------------------------------------------------------------------------
# SlidingWindowRateLimiter tests
# ---------------------------------------------------------------------------


class TestSlidingWindowRateLimiter:
    """Tests for the in-memory sliding-window rate limiter."""

    def test_allows_within_limit(self):
        limiter = SlidingWindowRateLimiter()
        for i in range(5):
            allowed, remaining = limiter.is_allowed("key1", limit=5, window_seconds=60)
            assert allowed is True
            assert remaining == 5 - i - 1

    def test_blocks_over_limit(self):
        limiter = SlidingWindowRateLimiter()
        for _ in range(5):
            limiter.is_allowed("key1", limit=5, window_seconds=60)
        allowed, remaining = limiter.is_allowed("key1", limit=5, window_seconds=60)
        assert allowed is False
        assert remaining == 0

    def test_independent_keys(self):
        limiter = SlidingWindowRateLimiter()
        for _ in range(3):
            limiter.is_allowed("key_a", limit=3, window_seconds=60)
        # key_a is exhausted
        allowed_a, _ = limiter.is_allowed("key_a", limit=3, window_seconds=60)
        assert allowed_a is False
        # key_b should still be fine
        allowed_b, remaining_b = limiter.is_allowed("key_b", limit=3, window_seconds=60)
        assert allowed_b is True
        assert remaining_b == 2

    def test_window_expiry(self):
        limiter = SlidingWindowRateLimiter()
        # Fill the limit
        for _ in range(3):
            limiter.is_allowed("key1", limit=3, window_seconds=1)
        allowed, _ = limiter.is_allowed("key1", limit=3, window_seconds=1)
        assert allowed is False

        # Wait for window to expire
        time.sleep(1.1)
        allowed, remaining = limiter.is_allowed("key1", limit=3, window_seconds=1)
        assert allowed is True
        assert remaining == 2

    @pytest.mark.regression
    def test_keys_whose_window_emptied_are_forgotten(self, monkeypatch):
        """#809: the key count falls back once the windows have passed.

        Fake clock, no sleep. 200 keys are seen once and never again; later
        calls from one other key drop them a few at a time.
        """
        from types import SimpleNamespace

        clock = FakeClock(100.0)
        monkeypatch.setattr(
            "backend.app.middleware.rate_limiter.time",
            SimpleNamespace(monotonic=clock),
        )
        limiter = SlidingWindowRateLimiter()
        for i in range(200):
            assert limiter.is_allowed(f"ip-{i}", limit=5, window_seconds=60)[0]
        assert len(limiter._windows) == 200

        clock.now += 61  # every window has emptied
        for _ in range(40):
            limiter.is_allowed("live", limit=5, window_seconds=60)

        assert set(limiter._windows) == {"live"}
        assert len(limiter._expires) == 1

    def test_sweep_keeps_keys_still_in_their_window(self, monkeypatch):
        from types import SimpleNamespace

        clock = FakeClock(100.0)
        monkeypatch.setattr(
            "backend.app.middleware.rate_limiter.time",
            SimpleNamespace(monotonic=clock),
        )
        limiter = SlidingWindowRateLimiter()
        for _ in range(3):
            limiter.is_allowed("busy", limit=3, window_seconds=60)
        clock.now += 59
        for i in range(50):
            limiter.is_allowed(f"other-{i}", limit=3, window_seconds=60)
        # "busy" is still in its window, so it is still limited.
        assert limiter.is_allowed("busy", limit=3, window_seconds=60) == (False, 0)


# ---------------------------------------------------------------------------
# RedisRateLimiter tests
# ---------------------------------------------------------------------------


class TestRedisRateLimiter:
    """Tests for the Redis-backed rate limiter."""

    def test_uses_redis_when_available(self):
        """When Redis is reachable, INCR/EXPIRE are used."""
        mock_redis = MagicMock()
        mock_pipe = MagicMock()
        mock_pipe.execute.return_value = [1, -1]  # first request, no TTL yet
        mock_redis.pipeline.return_value = mock_pipe

        limiter = RedisRateLimiter()
        limiter._redis_client = mock_redis
        limiter._redis_available = True

        allowed, remaining = limiter.is_allowed("key1", limit=10, window_seconds=60)

        assert allowed is True
        assert remaining == 9
        mock_pipe.incr.assert_called_once_with("ratelimit:key1")
        mock_pipe.ttl.assert_called_once_with("ratelimit:key1")
        mock_redis.expire.assert_called_once_with("ratelimit:key1", 60)

    def test_redis_blocks_over_limit(self):
        """When Redis count exceeds limit, request is denied."""
        mock_redis = MagicMock()
        mock_pipe = MagicMock()
        mock_pipe.execute.return_value = [11, 45]  # 11th request, TTL exists
        mock_redis.pipeline.return_value = mock_pipe

        limiter = RedisRateLimiter()
        limiter._redis_client = mock_redis
        limiter._redis_available = True

        allowed, remaining = limiter.is_allowed("key1", limit=10, window_seconds=60)

        assert allowed is False
        assert remaining == 0

    def test_falls_back_on_redis_error(self):
        """On Redis pipeline error, falls back to in-memory limiter."""
        mock_redis = MagicMock()
        mock_pipe = MagicMock()
        mock_pipe.execute.side_effect = Exception("Connection refused")
        mock_redis.pipeline.return_value = mock_pipe

        limiter = RedisRateLimiter()
        limiter._redis_client = mock_redis
        limiter._redis_available = True

        allowed, remaining = limiter.is_allowed("key1", limit=10, window_seconds=60)

        assert allowed is True
        assert remaining == 9
        assert limiter._redis_available is False

    @pytest.mark.regression
    def test_falls_back_when_redis_unavailable(self, no_client_build):
        """Inside the retry window the fallback serves, with zero Redis calls.

        This replaced a test that set ``_redis_available = False`` with no
        client and checked only ``remaining``. Under the old contract that
        state was permanent; under this one it means "the window has passed",
        so the old test would have tried whatever ``REDIS_HOST`` is. It now
        pins the window: a recording client, a retry time in the future, and
        no call reaches the client (#790).
        """
        clock = FakeClock()
        client = RecordingClient()
        limiter = RedisRateLimiter(clock=clock)
        limiter._redis_client = client
        limiter._redis_available = False
        limiter._retry_at = REDIS_RETRY_SECONDS

        allowed, remaining = limiter.is_allowed("key1", limit=5, window_seconds=60)

        assert (allowed, remaining) == (True, 4)
        assert client.calls == []
        assert no_client_build == []

    @pytest.mark.regression
    def test_lazy_connect_failure(self, monkeypatch):
        """A failed first connect starts the fallback and its retry window."""
        clock = FakeClock()
        built = _patch_create(monkeypatch, [RecordingClient(fail=True)])
        limiter = RedisRateLimiter(clock=clock)

        allowed, remaining = limiter.is_allowed("key1", limit=5, window_seconds=60)
        assert (allowed, remaining) == (True, 4)
        assert limiter._redis_available is False
        assert limiter._retry_at == REDIS_RETRY_SECONDS
        assert len(built) == 1

        # Still inside the window: no second connect, no call on the client.
        clock.now = REDIS_RETRY_SECONDS - 0.1
        for _ in range(3):
            limiter.is_allowed("key1", limit=5, window_seconds=60)
        assert len(built) == 1
        assert built[0].calls == ["ping"]


# ---------------------------------------------------------------------------
# Returning to Redis after an error (#790)
# ---------------------------------------------------------------------------


class FakeClock:
    """A monotonic clock the test moves by hand."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class _RecordingPipeline:
    def __init__(self, client: "RecordingClient") -> None:
        self._client = client

    def incr(self, key):
        self._client.calls.append("incr")

    def ttl(self, key):
        self._client.calls.append("ttl")

    def execute(self):
        self._client.calls.append("execute")
        if self._client.fail:
            raise ConnectionError("Error 111 connecting to redis.example:6379.")
        self._client.count += 1
        return [self._client.count, 60]


class RecordingClient:
    """A stand-in Redis client that records every call made on it.

    ``fail`` makes ping and the pipeline raise; flip it to model Redis
    going away and coming back.
    """

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list = []
        self.count = 0

    def ping(self):
        self.calls.append("ping")
        if self.fail:
            raise ConnectionError("Error 111 connecting to redis.example:6379.")
        return True

    def pipeline(self, transaction=True):
        self.calls.append("pipeline")
        return _RecordingPipeline(self)

    def expire(self, key, seconds):
        self.calls.append("expire")

    def pipelines(self) -> int:
        return self.calls.count("pipeline")


def _patch_create(monkeypatch, clients: list) -> list:
    """Make ``create_redis_client`` hand out *clients* in order; return the built list."""
    built: list = []

    def create(**options):
        client = clients[len(built)]
        built.append(client)
        return client

    monkeypatch.setattr("backend.app.core.redis_client.create_redis_client", create)
    return built


@pytest.fixture
def no_client_build(monkeypatch) -> list:
    """Fail the test if anything builds a real Redis client."""
    attempts: list = []

    def create(**options):
        attempts.append(options)
        raise AssertionError("this test must not build a Redis client")

    monkeypatch.setattr("backend.app.core.redis_client.create_redis_client", create)
    return attempts


def _limiter_on(client: RecordingClient, clock: FakeClock) -> RedisRateLimiter:
    limiter = RedisRateLimiter(clock=clock)
    limiter._redis_client = client
    return limiter


@pytest.mark.unit
@pytest.mark.regression
class TestRedisRetry:
    """After a Redis error the limiter uses Redis again within the retry window.

    Before #790 one error set ``_redis_available = False`` and nothing set it
    back, so every process counted on its own until the next deploy. The
    in-memory counts are not asserted across clock jumps: the fallback limiter
    keeps real time; only the retry window uses the test clock.
    """

    def test_a_runtime_error_then_redis_again_after_the_window(self, no_client_build):
        clock = FakeClock()
        client = RecordingClient(fail=True)
        limiter = _limiter_on(client, clock)

        limiter.is_allowed("k", limit=1000, window_seconds=60)
        assert limiter._redis_available is False
        assert client.pipelines() == 1

        client.fail = False
        clock.now = REDIS_RETRY_SECONDS - 0.1
        for _ in range(20):
            limiter.is_allowed("k", limit=1000, window_seconds=60)
        assert client.pipelines() == 1, "Redis was called inside the retry window"

        clock.now = REDIS_RETRY_SECONDS
        assert limiter.is_allowed("k", limit=1000, window_seconds=60) == (True, 999)
        assert client.pipelines() == 2
        assert limiter._redis_available is True

        for _ in range(5):
            limiter.is_allowed("k", limit=1000, window_seconds=60)
        assert client.pipelines() == 7
        assert no_client_build == []

    def test_a_failed_connect_at_startup_recovers(self, monkeypatch):
        clock = FakeClock()
        first, second = RecordingClient(fail=True), RecordingClient()
        built = _patch_create(monkeypatch, [first, second])
        limiter = RedisRateLimiter(clock=clock)

        limiter.is_allowed("k", limit=1000, window_seconds=60)
        assert limiter._redis_available is False
        assert limiter._redis_client is None

        clock.now = REDIS_RETRY_SECONDS - 0.1
        limiter.is_allowed("k", limit=1000, window_seconds=60)
        assert len(built) == 1

        clock.now = REDIS_RETRY_SECONDS
        assert limiter.is_allowed("k", limit=1000, window_seconds=60) == (True, 999)
        assert len(built) == 2
        assert second.calls[:2] == ["ping", "pipeline"]
        assert limiter._redis_client is second
        assert limiter._redis_available is True

    def test_a_failed_retry_starts_a_new_window(self, no_client_build):
        clock = FakeClock()
        client = RecordingClient(fail=True)
        limiter = _limiter_on(client, clock)

        limiter.is_allowed("k", limit=1000, window_seconds=60)  # t=0: error
        clock.now = REDIS_RETRY_SECONDS
        limiter.is_allowed("k", limit=1000, window_seconds=60)  # t=30: retry fails
        assert client.pipelines() == 2
        assert limiter._redis_available is False

        clock.now = 2 * REDIS_RETRY_SECONDS - 0.1
        for _ in range(10):
            limiter.is_allowed("k", limit=1000, window_seconds=60)
        assert client.pipelines() == 2, "a failed retry did not start a new window"

        client.fail = False
        clock.now = 2 * REDIS_RETRY_SECONDS
        limiter.is_allowed("k", limit=1000, window_seconds=60)
        assert client.pipelines() == 3
        assert limiter._redis_available is True

    def test_the_client_is_built_with_client_retries_off(self, monkeypatch):
        """The kwargs: a ``Retry`` with no backoff and zero retries."""
        from redis.backoff import NoBackoff
        from redis.retry import Retry

        seen: list = []

        def create(**options):
            seen.append(options)
            return RecordingClient()

        monkeypatch.setattr("backend.app.core.redis_client.create_redis_client", create)
        assert RedisRateLimiter()._get_redis() is not None

        assert len(seen) == 1
        retry = seen[0]["retry"]
        assert isinstance(retry, Retry)
        assert retry.get_retries() == 0
        assert isinstance(retry._backoff, NoBackoff)
        assert seen[0]["socket_connect_timeout"] == 2
        assert seen[0]["socket_timeout"] == 1


@pytest.mark.unit
@pytest.mark.regression
class TestNoClientRetries:
    """A real ``redis.Redis``, built by the limiter, tries to connect once.

    redis-py retries ten times by default, so one failed retry would hold the
    event loop for seconds every window. These drive the real client against
    a refused address (127.0.0.1:1) and count connect attempts; no timing.
    """

    @pytest.fixture
    def connects(self, monkeypatch) -> list:
        from redis.connection import Connection

        from backend.app.core.config import settings

        monkeypatch.setattr(settings, "REDIS_HOST", "127.0.0.1")
        monkeypatch.setattr(settings, "REDIS_PORT", 1)
        monkeypatch.setattr(settings, "REDIS_PASSWORD", None)
        monkeypatch.setattr(settings, "REDIS_SSL", False)
        attempts: list = []
        real_connect = Connection._connect

        def counting_connect(conn):
            attempts.append(conn.port)
            return real_connect(conn)

        monkeypatch.setattr(Connection, "_connect", counting_connect)
        return attempts

    def test_the_connect_ping_makes_one_attempt(self, connects):
        limiter = RedisRateLimiter(clock=FakeClock())
        assert limiter._get_redis() is None
        assert limiter._redis_available is False
        assert connects == [1], f"{len(connects)} connect attempts, not 1"

    def test_the_pipeline_makes_one_attempt(self, connects, monkeypatch):
        import redis

        # Let the client be built without connecting, so the pipeline is
        # the first thing that reaches the socket.
        monkeypatch.setattr(redis.Redis, "ping", lambda self, **kw: True)
        limiter = RedisRateLimiter(clock=FakeClock())
        assert limiter._get_redis() is not None
        assert connects == []

        assert limiter.is_allowed("k", limit=5, window_seconds=60) == (True, 4)
        assert limiter._redis_available is False
        assert connects == [1], f"{len(connects)} connect attempts, not 1"


@contextlib.contextmanager
def _json_logs(level: str = "DEBUG"):
    """Render every log record as the production JSON line, into a buffer.

    The real renderer, not caplog: caplog sees stdlib ``extra=`` attributes
    that production drops, and does not see structlog lines at all unless
    logging has been configured.
    """
    from backend.app.core.logger import configure_logging

    root = logging.root
    handlers, root_level = list(root.handlers), root.level
    saved = structlog.get_config()
    buffer = io.StringIO()
    configure_logging(log_level=level, json_logs=True, stream=buffer)
    try:
        yield buffer
    finally:
        _restore_logging(root, handlers, root_level, saved)


def _restore_logging(root, handlers, root_level, saved) -> None:
    for handler in list(root.handlers):
        root.removeHandler(handler)
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(root_level)
    structlog.configure(**saved)


def _limiter_lines(buffer: io.StringIO) -> list:
    lines = [json.loads(line) for line in buffer.getvalue().splitlines() if line]
    return [
        line
        for line in lines
        if line.get("logger") == "backend.app.middleware.rate_limiter"
    ]


def _one_episode(clock: FakeClock, client: RecordingClient, limiter) -> None:
    """Fail at t=0, 30 requests in the window, a failed retry at t=30, recovery at t=60."""
    client.fail = True
    limiter.is_allowed("k", limit=1000, window_seconds=60)
    clock.now = 10.0
    for _ in range(30):
        limiter.is_allowed("k", limit=1000, window_seconds=60)
    clock.now = REDIS_RETRY_SECONDS
    limiter.is_allowed("k", limit=1000, window_seconds=60)
    client.fail = False
    clock.now = 2 * REDIS_RETRY_SECONDS
    limiter.is_allowed("k", limit=1000, window_seconds=60)
    assert limiter._redis_available is True


def _sample(name: str) -> float:
    from prometheus_client import REGISTRY

    value = REGISTRY.get_sample_value(name)
    assert value is not None, f"{name} is not registered"
    return value


@pytest.mark.unit
@pytest.mark.regression
class TestFallbackVisibility:
    """What an operator sees: one warning per transition, and two metrics."""

    def test_one_warning_on_entry_and_one_on_recovery(self, no_client_build):
        clock = FakeClock()
        client = RecordingClient()
        limiter = _limiter_on(client, clock)

        with _json_logs() as buffer:
            _one_episode(clock, client, limiter)
        lines = _limiter_lines(buffer)

        warnings = [line for line in lines if line["level"] == "warning"]
        debugs = [line for line in lines if line["level"] == "debug"]
        assert len(lines) == 3, lines
        assert len(warnings) == 2, lines
        assert len(debugs) == 1, lines
        assert [line for line in lines if line["level"] == "error"] == []

        entered, recovered = warnings
        assert entered["event"] == (
            "rate limiter: Redis unavailable, counting requests per process; "
            "retrying Redis every 30 s"
        )
        assert entered["rate_limiter"] == "per_process"
        assert entered["reason"] == "ConnectionError"
        assert entered["detail"] == "Error 111 connecting to redis.example:6379."
        assert entered["retry_seconds"] == 30

        retried = debugs[0]
        assert retried["event"] == "rate limiter: Redis still unavailable"
        assert retried["rate_limiter"] == "per_process"
        assert retried["reason"] == "ConnectionError"
        assert retried["fallback_seconds"] == 30.0

        assert recovered["event"] == (
            "rate limiter: Redis reachable again, counting requests in Redis"
        )
        assert recovered["rate_limiter"] == "redis"
        assert recovered["fallback_seconds"] == 60.0

    def test_a_failed_retry_is_not_logged_at_info(self, no_client_build):
        """At the production level (INFO) an outage is the two warnings only."""
        clock = FakeClock()
        client = RecordingClient()
        limiter = _limiter_on(client, clock)

        with _json_logs("INFO") as buffer:
            _one_episode(clock, client, limiter)
        lines = _limiter_lines(buffer)

        assert [line["level"] for line in lines] == ["warning", "warning"]
        assert [line["rate_limiter"] for line in lines] == ["per_process", "redis"]

    def test_the_detail_is_truncated(self, no_client_build):
        clock = FakeClock()
        limiter = RedisRateLimiter(clock=clock)

        with _json_logs() as buffer:
            limiter._enter_fallback(ConnectionError("x" * 500))
        lines = _limiter_lines(buffer)

        assert len(lines) == 1
        assert lines[0]["detail"] == "x" * 200

    def test_the_old_messages_are_gone(self):
        import backend.app.middleware.rate_limiter as module

        source = inspect.getsource(module)
        assert "rate limiter: Redis unavailable" in source
        assert "using in-memory fallback" not in source
        assert "falling back to in-memory" not in source

    def test_the_metrics_follow_the_state(self, no_client_build):
        clock = FakeClock()
        client = RecordingClient()
        limiter = _limiter_on(client, clock)
        before = _sample("rate_limit_redis_fallbacks_total")

        client.fail = True
        limiter.is_allowed("k", limit=1000, window_seconds=60)
        assert _sample("rate_limit_redis_fallback_active") == 1
        assert _sample("rate_limit_redis_fallbacks_total") == before + 1

        clock.now = REDIS_RETRY_SECONDS  # a failed retry is not a new switch
        limiter.is_allowed("k", limit=1000, window_seconds=60)
        assert _sample("rate_limit_redis_fallback_active") == 1
        assert _sample("rate_limit_redis_fallbacks_total") == before + 1

        client.fail = False
        clock.now = 2 * REDIS_RETRY_SECONDS
        limiter.is_allowed("k", limit=1000, window_seconds=60)
        assert _sample("rate_limit_redis_fallback_active") == 0
        assert _sample("rate_limit_redis_fallbacks_total") == before + 1

    def test_the_metric_names_and_help_are_exported(self):
        from prometheus_client import generate_latest

        import backend.app.core.metrics  # registers the metrics

        text = generate_latest().decode()
        assert (
            "# HELP rate_limit_redis_fallback_active 1 while this process counts "
            "rate limits per process because Redis is unavailable; 0 while it "
            "counts them in Redis\n"
        ) in text
        assert "# TYPE rate_limit_redis_fallback_active gauge\n" in text
        assert (
            "# HELP rate_limit_redis_fallbacks_total Times this process switched "
            "rate limiting from Redis to per-process counting\n"
        ) in text
        assert "# TYPE rate_limit_redis_fallbacks_total counter\n" in text

    def test_the_metrics_carry_no_labels(self):
        """No reason or error text on a metric: they go to the log only."""
        from prometheus_client import REGISTRY

        from backend.app.core import metrics

        assert metrics.rate_limit_redis_fallback_active._labelnames == ()
        assert metrics.rate_limit_redis_fallbacks_total._labelnames == ()
        seen = 0
        for family in REGISTRY.collect():
            if family.name in (
                "rate_limit_redis_fallback_active",
                "rate_limit_redis_fallbacks",
            ):
                for sample in family.samples:
                    seen += 1
                    assert sample.labels == {}, sample
        assert seen >= 2


# ---------------------------------------------------------------------------
# Client IP extraction tests
# ---------------------------------------------------------------------------


class TestGetClientIp:
    """Tests for _get_client_ip helper.

    ``test_uses_x_forwarded_for`` used to live here and asserted that
    ``"1.2.3.4, 10.0.0.1"`` resolved to ``"1.2.3.4"``. That was the
    vulnerability written down as a contract: each proxy APPENDS to
    X-Forwarded-For, so the leftmost entry is whatever the client sent, and
    keying rate-limit buckets on it handed anyone who prepended a random
    address an unlimited budget (#237). It is replaced, not relaxed -- the
    tests below pin the opposite behaviour.
    """

    def test_it_uses_the_resolved_client_and_not_the_header(self):
        """uvicorn has already resolved this correctly; the header has not."""
        request = MagicMock()
        request.headers = {"X-Forwarded-For": "1.2.3.4, 10.0.0.1"}
        request.client.host = "203.0.113.9"
        assert _get_client_ip(request) == "203.0.113.9"

    def test_a_forged_header_cannot_change_the_bucket(self):
        """The attack, directly: same connection, different forged headers.

        If either of these returned the header's value, a client would get a
        fresh bucket per request simply by varying it.
        """
        seen = set()
        for forged in ("9.9.9.9", "8.8.8.8", "1.1.1.1, 2.2.2.2", ""):
            request = MagicMock()
            request.headers = {"X-Forwarded-For": forged}
            request.client.host = "203.0.113.9"
            seen.add(_get_client_ip(request))
        assert seen == {"203.0.113.9"}, (
            f"the forged header changed the rate-limit key: {seen}"
        )

    def test_uses_client_host_when_no_forwarded_header(self):
        request = MagicMock()
        request.headers = {}
        request.client.host = "192.168.1.1"
        assert _get_client_ip(request) == "192.168.1.1"

    def test_no_client_is_unknown_rather_than_the_header(self):
        """One shared bucket beats a bucket the peer chooses.

        An absent ``request.client`` means no transport told us who connected.
        Falling back to a header the peer controls would be the same
        vulnerability with an extra step.
        """
        request = MagicMock()
        request.headers = {"X-Forwarded-For": "9.9.9.9"}
        request.client = None
        assert _get_client_ip(request) == "unknown"

    def test_returns_unknown_when_no_client(self):
        request = MagicMock()
        request.headers = {}
        request.client = None
        assert _get_client_ip(request) == "unknown"


# ---------------------------------------------------------------------------
# RateLimitMiddleware integration tests
# ---------------------------------------------------------------------------


def _make_app(enabled: bool = True, limiter=None) -> FastAPI:
    """Create a minimal FastAPI app with rate limiting middleware."""
    test_app = FastAPI()

    @test_app.get("/api/v1/test")
    async def test_endpoint():
        return {"ok": True}

    @test_app.get("/api/v1/auth/token")
    async def auth_endpoint():
        return {"token": "abc"}

    middleware = RateLimitMiddleware(test_app, enabled=enabled)
    if limiter is not None:
        middleware._limiter = limiter
    # We need to build the app with the middleware manually
    # since TestClient wraps the ASGI app
    return middleware


class TestRateLimitMiddleware:
    """End-to-end tests for the rate limiting middleware."""

    def test_returns_rate_limit_headers(self):
        app = _make_app(enabled=True, limiter=SlidingWindowRateLimiter())
        client = TestClient(app)

        response = client.get("/api/v1/test")

        assert response.status_code == 200
        assert "X-RateLimit-Limit" in response.headers
        assert "X-RateLimit-Remaining" in response.headers
        assert "X-RateLimit-Window" in response.headers

    def test_returns_429_when_limit_exceeded(self):
        limiter = SlidingWindowRateLimiter()
        app = _make_app(enabled=True, limiter=limiter)
        client = TestClient(app)

        limit, window = DEFAULT_RATE_LIMIT  # 300, 60

        # Exhaust the limit
        for _ in range(limit):
            resp = client.get("/api/v1/test")
            assert resp.status_code == 200

        # Next request should be rejected
        response = client.get("/api/v1/test")
        assert response.status_code == 429
        assert response.headers["Retry-After"] == str(window)
        assert response.headers["X-RateLimit-Remaining"] == "0"
        assert "Too Many Requests" in response.json()["detail"]

    def test_disabled_mode_passes_through(self):
        app = _make_app(enabled=False)
        client = TestClient(app)

        response = client.get("/api/v1/test")
        assert response.status_code == 200
        # No rate limit headers when disabled
        assert "X-RateLimit-Limit" not in response.headers

    def test_auth_endpoint_stricter_limit(self):
        limiter = SlidingWindowRateLimiter()
        app = _make_app(enabled=True, limiter=limiter)
        client = TestClient(app)

        auth_limit, _ = RATE_LIMIT_CONFIG["/api/v1/auth/token"]  # 10

        for _ in range(auth_limit):
            resp = client.get("/api/v1/auth/token")
            assert resp.status_code == 200

        response = client.get("/api/v1/auth/token")
        assert response.status_code == 429


# ---------------------------------------------------------------------------
# Rate limit config tests
# ---------------------------------------------------------------------------


class TestRateLimitConfig:
    """Verify the rate limit configuration constants."""

    def test_auth_endpoints_have_strict_limits(self):
        for path in [
            "/api/v1/auth/token",
            "/api/v1/auth/signup",
            "/api/v1/auth/forgot-password",
            "/api/v1/auth/reset-password",
        ]:
            limit, window = RATE_LIMIT_CONFIG[path]
            default_limit, _ = DEFAULT_RATE_LIMIT
            assert limit < default_limit, (
                f"{path} should have stricter limit than default"
            )

    def test_tracking_endpoints_have_higher_limits(self):
        default_limit, _ = DEFAULT_RATE_LIMIT
        for path in [
            "/api/v1/tracking/assign",
            "/api/v1/tracking/track",
            "/api/v1/tracking/batch",
            "/api/v1/tracking/assignments/user-1",
            "/api/v1/feature-flags/evaluate/my-flag",
            "/api/v1/feature-flags/user/user-1",
        ]:
            limit, window = resolve_rate_limit(path)
            assert limit >= default_limit, (
                f"{path} should have higher limit for SDK traffic"
            )
            assert window == 60

    def test_sdk_limit_is_configurable(self):
        assert resolve_rate_limit("/api/v1/tracking/batch", 42) == (42, 60)

    def test_the_sdk_paths_default_to_6000_a_minute(self):
        from backend.app.core.config import Settings

        assert DEFAULT_SDK_RATE_LIMIT_PER_MINUTE == 6000
        assert Settings.model_fields["SDK_RATE_LIMIT_PER_MINUTE"].default == 6000
        assert resolve_rate_limit("/api/v1/tracking/assign") == (6000, 60)

    def test_the_local_evaluation_ruleset_is_sdk_traffic(self):
        """A fleet behind one NAT polls the ruleset from one address (#226)."""
        assert resolve_rate_limit("/api/v1/sdk/ruleset", 42) == (42, 60)
        assert resolve_rate_limit("/api/v1/sdk/ruleset") == (
            DEFAULT_SDK_RATE_LIMIT_PER_MINUTE,
            60,
        )

    def test_exact_config_wins_over_prefix_and_default(self):
        assert (
            resolve_rate_limit("/api/v1/auth/token")
            == RATE_LIMIT_CONFIG["/api/v1/auth/token"]
        )
        assert resolve_rate_limit("/api/v1/experiments/") == DEFAULT_RATE_LIMIT
        # The dashboard's feature-flag CRUD routes are not SDK traffic
        assert resolve_rate_limit("/api/v1/feature-flags/") == DEFAULT_RATE_LIMIT

    def test_default_rate_limit(self):
        limit, window = DEFAULT_RATE_LIMIT
        assert limit == 300
        assert window == 60


# ---------------------------------------------------------------------------
# Export endpoints: one shared limit per client address
# ---------------------------------------------------------------------------

_EXPORT_PATHS = [
    "/api/v1/export/experiments",
    "/api/v1/export/variants",
    "/api/v1/export/feature-flags",
    "/api/v1/export/reports/overview",
    "/api/v1/export/reports/experiments/3f2b8c1e-9d4a-4e6f-8a7b-1c2d3e4f5a6b",
]


@pytest.mark.regression
class TestExportRateLimit:
    """Every path under /api/v1/export/ shares 10 requests a minute per client."""

    def test_the_constant_is_ten_a_minute(self):
        assert EXPORT_RATE_LIMIT == (10, 60)

    @pytest.mark.parametrize("path", _EXPORT_PATHS)
    def test_every_export_path_resolves_to_the_export_limit(self, path):
        assert resolve_rate_limit(path) == (10, 60)

    def test_neighbouring_paths_keep_their_limits(self):
        assert resolve_rate_limit("/api/v1/experiments/") == (300, 60)
        assert resolve_rate_limit("/api/v1/tracking/track") == (
            DEFAULT_SDK_RATE_LIMIT_PER_MINUTE,
            60,
        )

    def test_export_paths_share_one_counter_across_routes_and_ids(self):
        """10 requests spread over routes and ids succeed; the 11th is refused."""
        test_app = FastAPI()

        @test_app.get("/api/v1/export/experiments")
        async def export_experiments():
            return {"ok": True}

        @test_app.get("/api/v1/export/variants")
        async def export_variants():
            return {"ok": True}

        @test_app.get("/api/v1/export/feature-flags")
        async def export_flags():
            return {"ok": True}

        @test_app.get("/api/v1/export/reports/experiments/{experiment_id}")
        async def export_report(experiment_id: str):
            return {"ok": True}

        @test_app.get("/api/v1/experiments/")
        async def experiments():
            return {"ok": True}

        middleware = RateLimitMiddleware(test_app, enabled=True)
        middleware._limiter = SlidingWindowRateLimiter()
        client = TestClient(middleware)

        first_id = "11111111-1111-4111-8111-111111111111"
        second_id = "22222222-2222-4222-8222-222222222222"
        spread = [
            "/api/v1/export/experiments",
            "/api/v1/export/variants",
            f"/api/v1/export/reports/experiments/{first_id}",
            f"/api/v1/export/reports/experiments/{second_id}",
        ]
        for i in range(10):
            resp = client.get(spread[i % len(spread)])
            assert resp.status_code == 200, (i, resp.status_code)
            assert resp.headers["X-RateLimit-Limit"] == "10"
            assert resp.headers["X-RateLimit-Remaining"] == str(10 - i - 1)

        # The 11th is refused whichever export path it goes to -- including a
        # route and an id not used above.
        third_id = "33333333-3333-4333-8333-333333333333"
        for path in [
            "/api/v1/export/feature-flags",
            "/api/v1/export/experiments",
            f"/api/v1/export/reports/experiments/{third_id}",
        ]:
            refused = client.get(path)
            assert refused.status_code == 429, path
            assert refused.headers["Retry-After"] == "60"
            assert refused.headers["X-RateLimit-Limit"] == "10"
            assert refused.headers["X-RateLimit-Remaining"] == "0"
            assert "Too Many Requests" in refused.json()["detail"]

        # Other routes keep their own budget.
        assert client.get("/api/v1/experiments/").status_code == 200


class TestSdkRateLimitIsPerPath:
    """SDK routes share a limit, not a counter: each path is charged on its own."""

    @pytest.mark.parametrize(
        "first, second",
        [
            ("/api/v1/feature-flags/evaluate/a", "/api/v1/feature-flags/evaluate/b"),
            ("/api/v1/feature-flags/user/u1", "/api/v1/feature-flags/user/u2"),
        ],
    )
    def test_sdk_paths_with_different_ids_are_counted_separately(self, first, second):
        assert rate_limit_key("203.0.113.7", first) != rate_limit_key(
            "203.0.113.7", second
        )
        assert resolve_rate_limit(first) == resolve_rate_limit(second)
        assert resolve_rate_limit(first) == (DEFAULT_SDK_RATE_LIMIT_PER_MINUTE, 60)

    def test_the_same_path_from_two_addresses_is_counted_separately(self):
        path = "/api/v1/feature-flags/evaluate/a"
        assert rate_limit_key("203.0.113.7", path) != rate_limit_key(
            "203.0.113.8", path
        )
