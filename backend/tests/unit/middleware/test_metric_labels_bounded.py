"""The HTTP and rate-limit metrics take their label values from fixed sets.

* ``http_requests_total`` and ``http_request_duration_seconds``: ``endpoint``
  is the template of the route a request matched, or ``unmatched``; ``method``
  is GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS or ``other``.
* ``rate_limit_hits_total`` and ``rate_limit_rejections_total``: ``endpoint``
  is the limit the request was counted against
  (``resolve_rate_limit_bucket``), not its path.

Each test drives the application itself -- ``backend.app.main.app``, its
router and its own middleware stack -- with a few hundred requests of each
shape (random paths, the per-user and per-flag SDK routes with random ids,
UUIDs, a Host the deployment does not answer on, random methods) and counts
label sets, with a positive control: the counter's total rose by exactly the
number of requests that reached it. Label counts only, never time. The route
template test pins the label of single requests instead.

Under the test profile the application runs with the rate limiter off and no
Host allow-list, so ``_hardened_client`` serves the same application with both
switched on: a shallow copy of the app with those two layers' arguments
changed and everything else (the other layers, their order, the router) as
``main.py`` built it. Its rate limiter counts in process; it never opens Redis.
"""

import copy
import random
import string
import uuid
from typing import Dict, FrozenSet, List, Set, Tuple

import pytest
from fastapi.testclient import TestClient
from starlette.middleware import Middleware

from backend.app.core import metrics
from backend.app.middleware import prometheus_metrics_middleware as pmm
from backend.app.middleware import rate_limiter
from backend.app.middleware.rate_limiter import RateLimitMiddleware
from backend.app.middleware.trusted_host_middleware import TrustedHostMiddleware

pytestmark = [pytest.mark.unit]

#: Requests per shape.
PER_SHAPE = 200

#: How many label sets one run of ``_http_shapes`` may add to a metric. Six
#: shapes, each answered with one or two (route, status) outcomes (seven label
#: sets in all, measured); a label taken from the request instead adds about
#: ``PER_SHAPE`` per shape.
MAX_NEW_LABEL_SETS = 16

#: The Host TestClient sends, and the one the hardened app answers on.
HOST = "testserver"

ALLOWED_METHODS = metrics.HTTP_METHOD_LABELS | {metrics.OTHER_METHOD_LABEL}

USER_ROUTE = "/api/v1/feature-flags/user/{user_id}"

LabelSet = FrozenSet[Tuple[str, str]]


# ---------------------------------------------------------------------------
# Reading the registry (public prometheus_client API only)
# ---------------------------------------------------------------------------


def _label_sets(metric, sample_name: str) -> Set[LabelSet]:
    return {
        frozenset(sample.labels.items())
        for family in metric.collect()
        for sample in family.samples
        if sample.name == sample_name
    }


def _total(metric, sample_name: str) -> float:
    return sum(
        sample.value
        for family in metric.collect()
        for sample in family.samples
        if sample.name == sample_name
    )


#: (metric, the sample that carries one value per label set)
HTTP_SERIES = (
    (metrics.http_requests_total, "http_requests_total"),
    (metrics.http_request_duration_seconds, "http_request_duration_seconds_count"),
)
RATE_LIMIT_SERIES = (
    (metrics.rate_limit_hits_total, "rate_limit_hits_total"),
    (metrics.rate_limit_rejections_total, "rate_limit_rejections_total"),
)


def _snapshot(series) -> Dict[str, Tuple[Set[LabelSet], float]]:
    return {name: (_label_sets(m, name), _total(m, name)) for m, name in series}


def _value(metric, sample_name: str, **labels: str) -> float:
    wanted = frozenset(labels.items())
    return sum(
        sample.value
        for family in metric.collect()
        for sample in family.samples
        if sample.name == sample_name and frozenset(sample.labels.items()) == wanted
    )


# ---------------------------------------------------------------------------
# The application, as configured and hardened
# ---------------------------------------------------------------------------


def _app():
    from backend.app.main import app

    return app


def _hardened_client(monkeypatch) -> TestClient:
    """``main.app`` with the Host allow-list set and the rate limiter enabled.

    A shallow copy, so the application other tests use keeps its own stack.
    """
    app = _app()
    overrides = {
        TrustedHostMiddleware: {"allowed_hosts": [HOST]},
        RateLimitMiddleware: {"enabled": True},
    }
    middleware: List[Middleware] = []
    for layer in app.user_middleware:
        kwargs = {**layer.kwargs, **overrides.get(layer.cls, {})}
        middleware.append(Middleware(layer.cls, *layer.args, **kwargs))
    layers = [layer.cls for layer in middleware]
    for cls in overrides:
        assert cls in layers, f"main.app no longer has {cls.__name__}"
    assert pmm.PrometheusMetricsMiddleware in layers

    # Counted in process: the enabled limiter would otherwise open Redis.
    monkeypatch.setattr(
        rate_limiter, "RedisRateLimiter", rate_limiter.SlidingWindowRateLimiter
    )
    hardened = copy.copy(app)
    hardened.user_middleware = middleware
    hardened.middleware_stack = None
    return TestClient(hardened, raise_server_exceptions=False)


def _client(variant: str, monkeypatch) -> TestClient:
    if variant == "hardened":
        return _hardened_client(monkeypatch)
    return TestClient(_app(), raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Request shapes
# ---------------------------------------------------------------------------

Request = Tuple[str, str, Dict[str, str]]


class _Tokens:
    """Random path segments, hosts and methods, all remembered."""

    def __init__(self, seed: int) -> None:
        self._rng = random.Random(seed)
        self.issued: List[str] = []

    def word(self, n: int = 12, alphabet: str = string.ascii_lowercase) -> str:
        token = "".join(self._rng.choice(alphabet) for _ in range(n))
        self.issued.append(token)
        return token

    def uuid(self) -> str:
        token = str(uuid.UUID(int=self._rng.getrandbits(128), version=4))
        self.issued.append(token)
        return token


def _http_shapes(tokens: _Tokens) -> List[Request]:
    requests: List[Request] = []
    for i in range(PER_SHAPE):
        requests += [
            ("GET", f"/api/v1/{tokens.word()}", {}),
            ("GET", f"/api/v1/feature-flags/user/{tokens.word()}", {}),
            ("GET", f"/api/v1/feature-flags/evaluate/{tokens.word()}?user_id=u1", {}),
            ("GET", f"/api/v1/experiments/{tokens.uuid()}", {}),
            ("GET", f"/api/v1/{tokens.word()}", {"host": f"{tokens.word()}.example"}),
            (
                tokens.word(8, string.ascii_uppercase),
                f"/api/v1/feature-flags/user/{tokens.word()}"
                if i % 2
                else f"/api/v1/{tokens.word()}",
                {},
            ),
        ]
    return requests


def _send(client: TestClient, requests: List[Request]) -> None:
    for method, url, headers in requests:
        client.request(method, url, headers=headers, follow_redirects=False)


# ---------------------------------------------------------------------------
# G1: the HTTP metrics
# ---------------------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize("variant", ["as_configured", "hardened"])
def test_metric_labels_stay_bounded_http(variant, monkeypatch):
    client = _client(variant, monkeypatch)
    tokens = _Tokens(seed=1 if variant == "as_configured" else 2)
    requests = _http_shapes(tokens)

    refused = {
        "method": "GET",
        "endpoint": pmm.UNMATCHED_ENDPOINT,
        "status_code": "400",
    }
    refused_before = _value(
        metrics.http_requests_total, "http_requests_total", **refused
    )
    before = _snapshot(HTTP_SERIES)
    _send(client, requests)
    after = _snapshot(HTTP_SERIES)

    for name, (labels_after, total_after) in after.items():
        labels_before, total_before = before[name]
        # Positive control: every request was recorded.
        assert total_after - total_before == len(requests), name

        new = labels_after - labels_before
        assert len(new) <= MAX_NEW_LABEL_SETS, (
            f"{name}: {len(requests)} requests added {len(new)} label sets; "
            f"a sample: {sorted(sorted(s) for s in new)[:3]}"
        )
        for label_set in new:
            labels = dict(label_set)
            assert labels["method"] in ALLOWED_METHODS, labels
            from_request = [t for t in tokens.issued if t in labels["endpoint"]]
            assert not from_request, f"{name}: a label carries request text: {labels}"

    if variant == "hardened":
        # Positive control: the Host check ran, and refused every foreign Host
        # before routing.
        refused_after = _value(
            metrics.http_requests_total, "http_requests_total", **refused
        )
        assert refused_after - refused_before == PER_SHAPE


# ---------------------------------------------------------------------------
# G2: the label is the route's FULL template
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_metric_labels_stay_bounded_route_templates(monkeypatch):
    recorded: List[dict] = []
    record = pmm.record_request

    def spy(**kwargs):
        recorded.append(kwargs)
        record(**kwargs)

    monkeypatch.setattr(pmm, "record_request", spy)
    client = TestClient(_app(), raise_server_exceptions=False)

    def endpoint(method: str, path: str) -> str:
        recorded.clear()
        client.request(method, path, follow_redirects=False)
        assert len(recorded) == 1, (method, path, recorded)
        return recorded[0]["endpoint"]

    assert endpoint("GET", "/api/v1/feature-flags/user/x") == USER_ROUTE, (
        "An /api/v1 route was not labelled with its full template. If the "
        "label is 'unmatched', FastAPI no longer keeps the template where "
        "route_template() in backend/app/middleware/"
        "prometheus_metrics_middleware.py reads it "
        "(scope['fastapi']['effective_route_context'].path_format); see its "
        "docstring."
    )
    experiments = endpoint("GET", "/api/v1/experiments/")
    flags = endpoint("GET", "/api/v1/feature-flags/")
    assert experiments != flags, "two routers' list endpoints share a label"
    assert (experiments, flags) == ("/api/v1/experiments/", "/api/v1/feature-flags/")
    assert endpoint("GET", "/api/v1/feature-flags/evaluate/k") == (
        "/api/v1/feature-flags/evaluate/{flag_key}"
    )
    assert endpoint("GET", "/health") == "/health"

    # Routes registered on the application itself.
    assert endpoint("GET", "/api/v1/docs") == "/api/v1/docs"
    assert endpoint("GET", "/api/v1/openapi.json") == "/api/v1/openapi.json"

    # A method the route does not serve is answered 405 by that route.
    assert endpoint("BREW", "/api/v1/feature-flags/user/x") == USER_ROUTE

    # No route: a 404, and the redirect to the trailing-slash form.
    assert endpoint("GET", "/api/v1/no-such-route") == pmm.UNMATCHED_ENDPOINT
    assert endpoint("GET", "/api/v1/experiments") == pmm.UNMATCHED_ENDPOINT


# ---------------------------------------------------------------------------
# G3: the rate-limit metrics
# ---------------------------------------------------------------------------


def _rate_limit_buckets() -> Set[str]:
    return set(rate_limiter.RATE_LIMIT_CONFIG) | {
        rate_limiter.EXPORT_BUCKET,
        rate_limiter.AI_INTERPRET_BUCKET,
        rate_limiter.SDK_BUCKET,
        rate_limiter.DEFAULT_BUCKET,
    }


@pytest.mark.regression
def test_metric_labels_stay_bounded_rate_limit(monkeypatch):
    client = _hardened_client(monkeypatch)
    tokens = _Tokens(seed=3)
    login = "/api/v1/auth/login"
    login_limit, _ = rate_limiter.RATE_LIMIT_CONFIG[login]
    export_limit, _ = rate_limiter.EXPORT_RATE_LIMIT
    interpret_limit, _ = rate_limiter.AI_INTERPRET_RATE_LIMIT

    requests: List[Request] = []
    for _ in range(PER_SHAPE):
        requests += [
            ("GET", f"/api/v1/{tokens.word()}", {}),
            ("GET", f"/api/v1/feature-flags/user/{tokens.word()}", {}),
            ("GET", f"/api/v1/feature-flags/evaluate/{tokens.word()}", {}),
            ("GET", f"/api/v1/export/{tokens.word()}", {}),
            ("GET", f"/api/v1/ai/interpret/{tokens.uuid()}", {}),
        ]
    # One route with a limit of its own, past that limit (GET: 405, no body).
    requests += [("GET", login, {})] * (login_limit + 5)

    before = _snapshot(RATE_LIMIT_SERIES)
    _send(client, requests)
    after = _snapshot(RATE_LIMIT_SERIES)

    hits = "rate_limit_hits_total"
    rejections = "rate_limit_rejections_total"
    # Positive controls: the limiter saw every request and refused exactly
    # the ones over a shared limit.
    assert after[hits][1] - before[hits][1] == len(requests)
    assert after[rejections][1] - before[rejections][1] == (
        (PER_SHAPE - export_limit) + (PER_SHAPE - interpret_limit) + 5
    )

    buckets = _rate_limit_buckets()
    for name in (hits, rejections):
        new = after[name][0] - before[name][0]
        assert len(new) <= 15, (
            f"{name}: {len(requests)} requests added {len(new)} label sets"
        )
        values = {dict(label_set)["endpoint"] for label_set in new}
        assert values <= buckets, f"{name}: not a limit: {sorted(values - buckets)[:5]}"


def test_every_rate_limit_bucket_comes_with_its_limit():
    """The bucket and the limit come from one function, rule by rule."""
    resolve = rate_limiter.resolve_rate_limit_bucket
    for path, limit in rate_limiter.RATE_LIMIT_CONFIG.items():
        assert resolve(path) == (path, limit)
    assert resolve("/api/v1/export/experiments") == (
        rate_limiter.EXPORT_BUCKET,
        rate_limiter.EXPORT_RATE_LIMIT,
    )
    assert resolve(f"/api/v1/ai/interpret/{uuid.uuid4()}") == (
        rate_limiter.AI_INTERPRET_BUCKET,
        rate_limiter.AI_INTERPRET_RATE_LIMIT,
    )
    assert resolve("/api/v1/feature-flags/user/someone", 42) == (
        rate_limiter.SDK_BUCKET,
        (42, 60),
    )
    assert resolve("/api/v1/experiments/") == (
        rate_limiter.DEFAULT_BUCKET,
        rate_limiter.DEFAULT_RATE_LIMIT,
    )
    for path in ("/api/v1/experiments/", "/api/v1/tracking/track", "/x"):
        assert resolve(path, 7)[1] == rate_limiter.resolve_rate_limit(path, 7)


@pytest.mark.parametrize(
    "method,label",
    [
        ("GET", "GET"),
        ("HEAD", "HEAD"),
        ("POST", "POST"),
        ("PUT", "PUT"),
        ("PATCH", "PATCH"),
        ("DELETE", "DELETE"),
        ("OPTIONS", "OPTIONS"),
        ("get", "other"),
        ("BREW", "other"),
        ("", "other"),
    ],
)
def test_http_method_label(method, label):
    assert metrics.http_method_label(method) == label
