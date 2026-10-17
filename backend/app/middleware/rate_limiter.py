"""
Rate limiting middleware for Controlvar.

Provides a Redis-backed rate limiter (fixed-window via INCR + EXPIRE) with
automatic fallback to an in-memory sliding-window limiter when Redis is
unavailable; Redis is tried again every ``REDIS_RETRY_SECONDS``.  The
middleware attaches standard rate-limit headers to every response and records
Prometheus counters for hits and rejections.
"""

import logging
import threading
import time
from collections import defaultdict, deque
from typing import Callable, Dict, Optional, Tuple

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from starlette.types import ASGIApp

from backend.app.core.logger import get_logger

logger = logging.getLogger(__name__)
#: The fallback transitions log through structlog so their keyword fields
#: survive the production JSON renderer (stdlib ``extra=`` fields do not).
log = get_logger(__name__)


# ---------------------------------------------------------------------------
# In-memory fallback limiter
# ---------------------------------------------------------------------------


class SlidingWindowRateLimiter:
    """
    Thread-safe sliding-window rate limiter backed by in-memory deques.

    Each (key, route) pair gets an independent deque of timestamps for
    requests in the current window.  Requests older than ``window_seconds``
    are evicted on each check.

    A key whose window has emptied is forgotten, so the dictionary does not
    grow for as long as the process stays in fallback (#809). Each call
    looks at no more than ``SWEEP_PER_CALL`` other keys, oldest-checked
    first, so a key that is never seen again is still dropped and the work
    per request stays bounded.
    """

    #: How many other keys one call may check for expiry.
    SWEEP_PER_CALL = 8

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._windows: Dict[str, deque] = defaultdict(deque)
        # key -> monotonic time at which its newest request leaves the window.
        self._expires: Dict[str, float] = {}

    def _sweep(self, now: float) -> None:
        """Forget up to ``SWEEP_PER_CALL`` keys whose window has emptied.

        The caller holds the lock. A key still in its window moves to the
        back of the order, so the next call checks different keys.
        """
        for _ in range(min(self.SWEEP_PER_CALL, len(self._expires))):
            key = next(iter(self._expires))
            expires = self._expires.pop(key)
            if expires <= now:
                self._windows.pop(key, None)
            else:
                self._expires[key] = expires

    def is_allowed(self, key: str, limit: int, window_seconds: int) -> Tuple[bool, int]:
        """
        Check whether a new request from *key* is within the allowed rate.

        Returns:
            ``(allowed, remaining)`` — whether the request is allowed and how
            many requests remain in the current window.
        """
        now = time.monotonic()
        cutoff = now - window_seconds

        with self._lock:
            self._sweep(now)
            window = self._windows[key]

            # Evict timestamps outside the current window
            while window and window[0] <= cutoff:
                window.popleft()

            count = len(window)
            if count >= limit:
                if not window:
                    # A limit of 0 never appends; do not keep the empty deque.
                    del self._windows[key]
                return False, 0

            window.append(now)
            self._expires.pop(key, None)
            self._expires[key] = now + window_seconds
            return True, limit - count - 1


# ---------------------------------------------------------------------------
# Redis-backed rate limiter
# ---------------------------------------------------------------------------


#: How long the limiter counts per process after a Redis error before it tries
#: Redis again. A constant, not a setting.
REDIS_RETRY_SECONDS = 30.0

#: Log messages for the fallback transitions. Every line also carries a
#: ``rate_limiter`` field: ``per_process`` or ``redis``.
FALLBACK_MESSAGE = (
    "rate limiter: Redis unavailable, counting requests per process; "
    f"retrying Redis every {int(REDIS_RETRY_SECONDS)} s"
)
STILL_UNAVAILABLE_MESSAGE = "rate limiter: Redis still unavailable"
RECOVERED_MESSAGE = "rate limiter: Redis reachable again, counting requests in Redis"


class RedisRateLimiter:
    """
    Fixed-window rate limiter backed by Redis ``INCR`` + ``EXPIRE``.

    On any Redis error the limiter falls back to the in-memory
    :class:`SlidingWindowRateLimiter` so that the application keeps serving
    traffic when Redis is unreachable. While it does, the count is per process,
    so a per-IP limit is multiplied by the number of API processes.

    The fallback lasts :data:`REDIS_RETRY_SECONDS`. Inside that window no
    request touches Redis. The first request after it is the retry: it runs the
    real pipeline on the existing client (redis-py reconnects the pool itself),
    or builds and pings a client when the first connect failed. Success returns
    the limiter to Redis; another error starts a new window. A failed first
    connect at startup is simply the first entry into the fallback.

    How long one retry can hold the event loop. The client is built with
    ``retry=Retry(NoBackoff(), 0)``: redis-py's default of ten retries with
    backoff turns one failure into 15-25 s of blocking, which a retry every
    30 s would repeat. With retries off:

    * Redis unreachable: one connect timeout, 2 s.
    * Redis slow but answering: up to 1 s per command (the socket timeout).
    * The no-client branch (connect, ping, pipeline, expire in a row): up to
      about 5 s at the timeout edges.
    * DNS resolution and the TLS handshake are not bounded by either timeout.

    Precondition for "one retry per window per process": ``is_allowed`` is
    synchronous with no ``await`` between the window check and the re-arm, the
    middleware calls it on the event loop thread, and there is one instance
    per process. If the call ever moves to a thread or becomes async, the
    window check and the re-arm need a lock (or an equivalent guarantee).

    Counts at a switch are not carried over. Redis to memory: the in-memory
    window holds only what this process recorded there earlier. Memory to
    Redis: Redis keys keep their TTLs, so counting resumes from what survived.
    Around a transition a client can get up to one Redis budget plus one
    in-memory budget per process in a window.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        # The connection parameters (REDIS_HOST/PORT/PASSWORD/DB/SSL) are read
        # from the settings by create_redis_client on first use.
        self._redis_client: Optional[object] = None
        self._redis_available: bool = True
        #: While ``_redis_available`` is False, Redis is not touched before this.
        self._retry_at: float = 0.0
        #: When this fallback began (``clock()``), for ``fallback_seconds``.
        self._fallback_since: float = 0.0
        self._clock = clock
        self._fallback = SlidingWindowRateLimiter()

    def _get_redis(self):
        """The Redis client, connecting (and pinging) if there is none yet.

        Returns None when the connect fails; the failure starts the fallback.
        """
        if self._redis_client is None:
            try:
                from redis.backoff import NoBackoff
                from redis.retry import Retry

                from backend.app.core.redis_client import create_redis_client

                client = create_redis_client(
                    socket_connect_timeout=2,
                    socket_timeout=1,
                    decode_responses=True,
                    # No client retries: see the class docstring.
                    retry=Retry(NoBackoff(), 0),
                )
                # Test connectivity
                client.ping()
            except Exception as exc:
                self._enter_fallback(exc)
                return None
            self._redis_client = client
            logger.info("Rate limiter connected to Redis")
        return self._redis_client

    def _enter_fallback(self, exc: BaseException) -> None:
        """Count per process until ``REDIS_RETRY_SECONDS`` from now.

        Logs a warning on the switch from Redis, and only debug for a failed
        retry, so an outage is one warning per process however long it lasts.
        ``detail`` (the exception text, which can name the Redis host) goes to
        the log only, truncated, and never into a metric or a response.
        """
        was_available = self._redis_available
        now = self._clock()
        self._redis_available = False
        self._retry_at = now + REDIS_RETRY_SECONDS
        if was_available:
            self._fallback_since = now
            log.warning(
                FALLBACK_MESSAGE,
                rate_limiter="per_process",
                reason=type(exc).__name__,
                detail=str(exc)[:200],
                retry_seconds=int(REDIS_RETRY_SECONDS),
            )
            try:
                from backend.app.core.metrics import record_rate_limit_redis_fallback

                record_rate_limit_redis_fallback()
            except Exception:
                pass  # Metrics are best-effort
        else:
            log.debug(
                STILL_UNAVAILABLE_MESSAGE,
                rate_limiter="per_process",
                reason=type(exc).__name__,
                fallback_seconds=round(now - self._fallback_since, 1),
            )

    def _leave_fallback(self) -> None:
        """Redis answered a retry: count in Redis again."""
        self._redis_available = True
        log.warning(
            RECOVERED_MESSAGE,
            rate_limiter="redis",
            fallback_seconds=round(self._clock() - self._fallback_since, 1),
        )
        try:
            from backend.app.core.metrics import record_rate_limit_redis_recovery

            record_rate_limit_redis_recovery()
        except Exception:
            pass  # Metrics are best-effort

    def is_allowed(self, key: str, limit: int, window_seconds: int) -> Tuple[bool, int]:
        """
        Check whether a request identified by *key* is within the rate limit.

        Uses Redis ``INCR`` with a fixed-window TTL. Falls back to the
        in-memory limiter on any Redis error, and inside the retry window
        after one.
        """
        if not self._redis_available and self._clock() < self._retry_at:
            return self._fallback.is_allowed(key, limit, window_seconds)

        client = self._get_redis()
        if client is None:
            return self._fallback.is_allowed(key, limit, window_seconds)

        redis_key = f"ratelimit:{key}"
        try:
            pipe = client.pipeline(transaction=True)
            pipe.incr(redis_key)
            pipe.ttl(redis_key)
            results = pipe.execute()

            current_count = results[0]
            ttl = results[1]

            # First request in this window — set expiry
            if ttl == -1:
                client.expire(redis_key, window_seconds)
        except Exception as exc:
            self._enter_fallback(exc)
            return self._fallback.is_allowed(key, limit, window_seconds)

        if not self._redis_available:
            self._leave_fallback()

        if current_count > limit:
            return False, 0

        remaining = limit - current_count
        return True, remaining


# ---------------------------------------------------------------------------
# Route-specific configuration
# ---------------------------------------------------------------------------

# (max_requests, window_seconds)
RATE_LIMIT_CONFIG: Dict[str, Tuple[int, int]] = {
    # Authentication endpoints — strict limits to slow brute-force attempts
    "/api/v1/auth/token": (10, 60),
    "/api/v1/auth/login": (10, 60),
    "/api/v1/auth/signup": (5, 60),
    "/api/v1/auth/forgot-password": (5, 60),
    "/api/v1/auth/reset-password": (5, 60),
    # A signed-in user's own password change: it checks the current password,
    # so it is limited like the other password endpoints. The failures also
    # count toward the sign-in lockout (LocalAuthService).
    "/api/v1/users/me/password": (5, 60),
    # SSO hand-off exchange (the modules' SSO routes): a public route that
    # mints an access token, limited like the password login.
    "/api/v1/auth/sso/exchange": (10, 60),
    # SSO sign-in start: public; answers whether a domain has SSO, and mints a
    # signed state cookie each time. One call per sign-in in real use.
    "/api/v1/auth/sso/login": (30, 60),
    # Batch assignment: one request assigns up to 1,000 users, so it gets its
    # own counter rather than the SDK prefix's 6,000 a minute (60 x 1,000 =
    # 60,000 users a minute per address). The path is exact: a trailing slash
    # is answered by FastAPI's 307 redirect to this path, not by the handler.
    "/api/v1/tracking/assign/batch": (60, 60),
    # Audit log export: one request streams up to 50,000 entries and holds a
    # database connection while it does. Its own counter, apart from the
    # ``/api/v1/export/`` budget below (#221).
    "/api/v1/audit-logs/export": (10, 60),
    # AI experiment design: with ANTHROPIC_API_KEY set, each request calls
    # Claude and waits for the answer, up to
    # ``ai_design_service.CLAUDE_TIMEOUT_SECONDS`` an attempt, two attempts.
    # The path is exact; a trailing slash is answered by FastAPI's 307
    # redirect to it.
    "/api/v1/ai/design": (10, 60),
}

# Default rate limit for all other endpoints
DEFAULT_RATE_LIMIT: Tuple[int, int] = (300, 60)  # 300 req/min

# SDK-facing endpoints: assignment, event tracking and flag evaluation.  One
# server-side SDK or one office NAT can legitimately send thousands of requests
# a minute from a single IP, so these get a much higher per-IP ceiling
# (``settings.SDK_RATE_LIMIT_PER_MINUTE``, default 6000).  Prefix matching is
# needed because flag evaluation carries the flag key in the path.
# ``/api/v1/sdk/`` is the server-side SDKs' own surface (the local-evaluation
# ruleset): a fleet of servers behind one NAT polls it from one address.
SDK_PATH_PREFIXES: Tuple[str, ...] = (
    "/api/v1/tracking/",
    "/api/v1/feature-flags/evaluate/",
    "/api/v1/feature-flags/user/",
    "/api/v1/sdk/",
)
DEFAULT_SDK_RATE_LIMIT_PER_MINUTE = 6000

# Data export: every path under this prefix shares ONE limit per client
# address, because each export computes results for every experiment it
# covers. The counter is keyed on the prefix rather than the path (see
# ``rate_limit_key``), so the five export routes -- and every experiment id
# under ``/reports/experiments/`` -- draw on the same budget.
EXPORT_PATH_PREFIX = "/api/v1/export/"
EXPORT_RATE_LIMIT: Tuple[int, int] = (10, 60)  # 10 req/min, all exports together

# AI results interpretation: a call to Claude, like ``/api/v1/ai/design``
# above. The route does not use the experiment id in its path, so the counter
# is keyed on the prefix (see ``rate_limit_key``): every id draws on the same
# budget, and a new id is not a new one.
AI_INTERPRET_PATH_PREFIX = "/api/v1/ai/interpret/"
AI_INTERPRET_RATE_LIMIT: Tuple[int, int] = (10, 60)  # 10 req/min, every id together


def resolve_rate_limit(
    path: str, sdk_limit_per_minute: int = DEFAULT_SDK_RATE_LIMIT_PER_MINUTE
) -> Tuple[int, int]:
    """
    Return ``(max_requests, window_seconds)`` for a request path.

    Exact entries in ``RATE_LIMIT_CONFIG`` win, then the export prefix, then
    the AI interpretation prefix, then SDK path prefixes, then
    ``DEFAULT_RATE_LIMIT``.
    """
    exact = RATE_LIMIT_CONFIG.get(path)
    if exact is not None:
        return exact
    if path.startswith(EXPORT_PATH_PREFIX):
        return EXPORT_RATE_LIMIT
    if path.startswith(AI_INTERPRET_PATH_PREFIX):
        return AI_INTERPRET_RATE_LIMIT
    if any(path.startswith(prefix) for prefix in SDK_PATH_PREFIXES):
        return (int(sdk_limit_per_minute), 60)
    return DEFAULT_RATE_LIMIT


def rate_limit_key(client_ip: str, path: str) -> str:
    """
    The counter a request is charged to.

    ``client_ip:path`` for every route, except that all export paths share
    ``client_ip:/api/v1/export/`` -- one budget across the export routes and
    across the experiment ids in their paths -- and all AI interpretation
    paths share ``client_ip:/api/v1/ai/interpret/``, whatever the id.
    """
    if path.startswith(EXPORT_PATH_PREFIX) and path not in RATE_LIMIT_CONFIG:
        return f"{client_ip}:{EXPORT_PATH_PREFIX}"
    if path.startswith(AI_INTERPRET_PATH_PREFIX) and path not in RATE_LIMIT_CONFIG:
        return f"{client_ip}:{AI_INTERPRET_PATH_PREFIX}"
    return f"{client_ip}:{path}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_client_ip(request: Request) -> str:
    """The address this request really came from, as the server resolved it.

    ``request.client.host`` and nothing else. This function used to read
    ``X-Forwarded-For`` itself and take ``split(",")[0]`` -- the LEFTMOST
    entry -- which is the opposite of correct, because each proxy APPENDS to
    that header. The leftmost value is therefore whatever the CLIENT sent, and
    a client that prepended a fresh random address per request was handed a
    fresh bucket per request: an unlimited budget, including against the strict
    login limit this middleware exists to enforce (#237).

    Reading the header here was also redundant. uvicorn runs with
    ``--proxy-headers`` and a narrowed ``--forwarded-allow-ips`` (see
    ``backend/docker-entrypoint.sh``) and has already resolved the client from
    ``X-Forwarded-For`` by the time any middleware runs -- walking the list in
    reverse and returning the first host it does not trust. So the correct
    answer is sitting in ``request.client``; this used to compute a second,
    wrong one beside it and use that.

    Not falling back to the header when ``request.client`` is absent, either.
    An absent client means no transport told us who connected, and a header the
    peer controls is not a safer answer than admitting we do not know -- it is
    the same vulnerability with an extra step. ``"unknown"`` shares one bucket,
    which is the conservative direction.
    """
    return request.client.host if request.client else "unknown"


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Middleware that enforces per-IP, per-route rate limits.

    Rate limits are defined in ``RATE_LIMIT_CONFIG`` for sensitive routes,
    ``EXPORT_RATE_LIMIT`` for the export routes (one counter shared by all of
    them), ``AI_INTERPRET_RATE_LIMIT`` for AI results interpretation (one
    counter for every experiment id), the SDK limit for SDK routes, and
    ``DEFAULT_RATE_LIMIT`` for everything else.
    Responses include standard ``X-RateLimit-*`` headers so clients can
    implement back-off without guessing.
    """

    def __init__(self, app: ASGIApp, enabled: bool = True) -> None:
        super().__init__(app)
        self._enabled = enabled
        self._sdk_limit = DEFAULT_SDK_RATE_LIMIT_PER_MINUTE

        if enabled:
            from backend.app.core.config import settings

            self._sdk_limit = int(
                getattr(
                    settings,
                    "SDK_RATE_LIMIT_PER_MINUTE",
                    DEFAULT_SDK_RATE_LIMIT_PER_MINUTE,
                )
            )
            self._limiter: object = RedisRateLimiter()
        else:
            self._limiter = SlidingWindowRateLimiter()

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if not self._enabled:
            return await call_next(request)

        # A CORS preflight is a protocol handshake, not an attempt at the
        # thing it precedes: it carries no credentials and no body, and the
        # browser sends one before every cross-origin POST. Counting it spent
        # half of `/api/v1/auth/login`'s ten-per-minute budget on requests the
        # user never made -- ten preflights alone were enough to 429 the first
        # real login attempt (#85). CORS is registered outermost in `main.py`,
        # so in the deployed app a preflight is answered before it reaches
        # here; this keeps the limiter correct on its own, for a preflight
        # CORS declines to answer and for any other mounting.
        #
        # `Access-Control-Request-Method`, not the method alone. Starlette's
        # CORSMiddleware only intercepts an OPTIONS that carries BOTH an
        # `Origin` and that header; a bare `OPTIONS /api/v1/auth/login` passes
        # straight through it to the router. Skipping on the method alone
        # therefore made every such request unlimited and unrecorded -- an
        # exemption a client asks for simply by choosing a verb.
        if request.method == "OPTIONS" and "access-control-request-method" in (
            request.headers
        ):
            return await call_next(request)

        path = request.url.path
        client_ip = _get_client_ip(request)

        # Determine the applicable limit for this path
        limit, window = resolve_rate_limit(path, self._sdk_limit)

        rate_key = rate_limit_key(client_ip, path)
        allowed, remaining = self._limiter.is_allowed(rate_key, limit, window)

        # Record Prometheus metrics
        try:
            from backend.app.core.metrics import (
                record_rate_limit_hit,
                record_rate_limit_rejection,
            )

            record_rate_limit_hit(path)
            if not allowed:
                record_rate_limit_rejection(path)
        except Exception:
            pass  # Metrics are best-effort

        if not allowed:
            logger.warning(
                "Rate limit exceeded",
                extra={"client_ip": client_ip, "path": path, "limit": limit},
            )
            return JSONResponse(
                status_code=429,
                content={
                    "detail": "Too Many Requests. Please slow down and retry after a moment.",
                },
                headers={
                    "Retry-After": str(window),
                    "X-RateLimit-Limit": str(limit),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Window": str(window),
                },
            )

        response = await call_next(request)

        # Attach informational rate-limit headers to successful responses
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Window"] = str(window)

        return response
