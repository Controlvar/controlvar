"""
Prometheus metrics registry for the experimentation platform.

All metrics are registered once at module import time. Helper functions
provide a clean, label-safe API for recording measurements throughout
the codebase without callers needing to know the underlying metric type.
"""

from prometheus_client import Counter, Gauge, Histogram

# ---------------------------------------------------------------------------
# HTTP metrics
#
# ``endpoint`` is the template of the route the request matched
# (``/api/v1/feature-flags/user/{user_id}``), or ``unmatched`` when it matched
# none; ``backend.app.middleware.prometheus_metrics_middleware`` chooses it.
# ``method`` is one of ``HTTP_METHOD_LABELS`` or ``other`` (``record_request``).
# ---------------------------------------------------------------------------

http_requests_total: Counter = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["method", "endpoint", "status_code"],
)

http_request_duration_seconds: Histogram = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["method", "endpoint", "status_code"],
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
)

# ---------------------------------------------------------------------------
# Business metrics
# ---------------------------------------------------------------------------

experiment_assignments_total: Counter = Counter(
    "experiment_assignments_total",
    "Total experiment assignments",
    ["experiment_id", "variant_id"],
)

events_tracked_total: Counter = Counter(
    "events_tracked_total",
    "Total events tracked",
    ["event_type"],
)

feature_flag_evaluations_total: Counter = Counter(
    "feature_flag_evaluations_total",
    "Total feature flag evaluations",
    ["flag_key", "result"],
)

cache_hits_total: Counter = Counter(
    "cache_hits_total",
    "Cache hits",
    ["cache_type"],
)

cache_misses_total: Counter = Counter(
    "cache_misses_total",
    "Cache misses",
    ["cache_type"],
)

# ---------------------------------------------------------------------------
# Rate limiting metrics
#
# ``endpoint`` is the limit the request was counted against, as
# ``backend.app.middleware.rate_limiter.resolve_rate_limit_bucket`` names it:
# the path, for a route with a limit of its own (``/api/v1/auth/login``), or
# ``export``, ``ai_interpret``, ``sdk`` or ``default``.
# ---------------------------------------------------------------------------

rate_limit_hits_total: Counter = Counter(
    "rate_limit_hits_total",
    "Total requests checked by the rate limiter, by the limit they were counted against",
    ["endpoint"],
)

rate_limit_rejections_total: Counter = Counter(
    "rate_limit_rejections_total",
    "Total requests rejected by the rate limiter (HTTP 429), by the limit they exceeded",
    ["endpoint"],
)

# Whether this process counts rate limits per process because Redis failed
# (backend.app.middleware.rate_limiter). No labels: the reason and the error
# text go to the log line only.
rate_limit_redis_fallback_active: Gauge = Gauge(
    "rate_limit_redis_fallback_active",
    "1 while this process counts rate limits per process because Redis is "
    "unavailable; 0 while it counts them in Redis",
)

rate_limit_redis_fallbacks_total: Counter = Counter(
    "rate_limit_redis_fallbacks_total",
    "Times this process switched rate limiting from Redis to per-process counting",
)

# Audit entries that could not be written (backend.app.services.audit_service).
# No labels: the action and the entity go to the ERROR line only.
audit_write_failures_total: Counter = Counter(
    "audit_write_failures_total",
    "Audit log entries that could not be written",
)

active_experiments_gauge: Gauge = Gauge(
    "active_experiments_gauge",
    "Number of currently active experiments",
)

# ---------------------------------------------------------------------------
# Background scheduler metrics (set by backend.app.core.scheduler_tick)
# ---------------------------------------------------------------------------

scheduler_last_success_timestamp: Gauge = Gauge(
    "scheduler_last_success_timestamp",
    "Unix timestamp of the last successful tick of each background scheduler",
    ["name"],
)

scheduler_tick_duration_seconds: Histogram = Histogram(
    "scheduler_tick_duration_seconds",
    "Wall-clock duration of background scheduler ticks",
    ["name"],
    buckets=[0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 15.0, 60.0, 300.0],
)

scheduler_ticks_total: Counter = Counter(
    "scheduler_ticks_total",
    "Background scheduler ticks by outcome (success, partial, failed, skipped)",
    ["name", "status"],
)

# ---------------------------------------------------------------------------
# Helper recording functions
# ---------------------------------------------------------------------------


#: The request methods the ``method`` label carries as themselves. Any other
#: method token a request arrives with is counted as ``other``.
HTTP_METHOD_LABELS: frozenset = frozenset(
    {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
)
OTHER_METHOD_LABEL = "other"


def http_method_label(method: str) -> str:
    """Map a request method to a label value from a fixed set.

    Exact match: ``get`` is not ``GET`` to the router either, so it is
    ``other`` here.
    """
    return method if method in HTTP_METHOD_LABELS else OTHER_METHOD_LABEL


def record_request(
    method: str,
    endpoint: str,
    status_code: int,
    duration: float,
) -> None:
    """Record HTTP request counter and duration histogram.

    ``endpoint`` must already be a route template or ``unmatched`` (the
    middleware's ``route_template``); ``method`` is mapped here.
    """
    method_label = http_method_label(method)
    http_requests_total.labels(
        method=method_label,
        endpoint=endpoint,
        status_code=status_code,
    ).inc()
    http_request_duration_seconds.labels(
        method=method_label,
        endpoint=endpoint,
        status_code=status_code,
    ).observe(duration)


def record_experiment_assignment(experiment_id: str, variant_id: str) -> None:
    """Increment the experiment assignment counter."""
    experiment_assignments_total.labels(
        experiment_id=experiment_id,
        variant_id=variant_id,
    ).inc()


# ``event_type`` on the tracking API is a free-form, client-supplied string
# (``"purchase"``, ``"cta_click"``, ...).  A label must be bounded, so only
# the platform's well-known types are used verbatim; everything else is
# counted as ``custom``.
KNOWN_EVENT_TYPES: frozenset = frozenset(
    {"exposure", "experiment_exposure", "conversion", "click", "page_view", "custom"}
)


def event_type_label(event_type: str) -> str:
    """Map a client-supplied event type to a bounded label value."""
    value = (event_type or "").strip().lower()
    return value if value in KNOWN_EVENT_TYPES else "custom"


def record_event_tracked(event_type: str) -> None:
    """Increment the events-tracked counter (label cardinality is bounded)."""
    events_tracked_total.labels(event_type=event_type_label(event_type)).inc()


def record_flag_evaluation(flag_key: str, result: str) -> None:
    """Increment the feature-flag evaluation counter."""
    feature_flag_evaluations_total.labels(flag_key=flag_key, result=result).inc()


def record_cache_hit(cache_type: str) -> None:
    """Increment the cache-hit counter."""
    cache_hits_total.labels(cache_type=cache_type).inc()


def record_cache_miss(cache_type: str) -> None:
    """Increment the cache-miss counter."""
    cache_misses_total.labels(cache_type=cache_type).inc()


def update_active_experiments(count: int) -> None:
    """Set the active-experiments gauge to the given count."""
    active_experiments_gauge.set(count)


def record_rate_limit_hit(bucket: str) -> None:
    """Increment the rate-limit hits counter for one limit bucket."""
    rate_limit_hits_total.labels(endpoint=bucket).inc()


def record_rate_limit_rejection(bucket: str) -> None:
    """Increment the rate-limit rejections counter for one limit bucket."""
    rate_limit_rejections_total.labels(endpoint=bucket).inc()


def record_rate_limit_redis_fallback() -> None:
    """This process switched rate limiting from Redis to per-process counting."""
    rate_limit_redis_fallback_active.set(1)
    rate_limit_redis_fallbacks_total.inc()


def record_rate_limit_redis_recovery() -> None:
    """This process counts rate limits in Redis again."""
    rate_limit_redis_fallback_active.set(0)


def record_scheduler_tick(name: str, status: str, duration_seconds: float) -> None:
    """Count a scheduler tick outcome and observe its duration."""
    scheduler_ticks_total.labels(name=name, status=status).inc()
    if status != "skipped":
        scheduler_tick_duration_seconds.labels(name=name).observe(duration_seconds)


def record_scheduler_success(name: str) -> None:
    """Stamp ``scheduler_last_success_timestamp{name}`` with the current time."""
    scheduler_last_success_timestamp.labels(name=name).set_to_current_time()
