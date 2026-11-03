"""The replay's driver against a fake server (httpx's MockTransport), with no
database and no network: what it sends, how it paces each route, what a 429,
a 5xx and a missing answer do to the ledger, and that the results are read
past the cache.

The fake server answers as the docs say a correct one does: O-ASSIGN's
variant for every user, every event stored (``library.honest_answer``).
Time is a fake clock, so pacing is checked exactly and takes no time.
"""

from __future__ import annotations

import importlib
import io
import json
import uuid
from typing import Any

import httpx
import pytest

from backend.tests.unit.ground_truth.library import (
    contract,
    generator,
    honest_answer,
    ledger,
)

# tests/ground_truth is on sys.path once library is imported.
driver = importlib.import_module("driver")

pytestmark = pytest.mark.unit

#: Stand-ins with no entropy, so no secret scanner mistakes them for one.
TOKEN = "T" * 40
KEY = "K" * 32
KINDS = {
    ledger.ASSIGN: "assign",
    ledger.ASSIGN_BATCH: "assign_batch",
    ledger.TRACK: "track",
    ledger.BATCH: "batch",
}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeServer:
    """The routes the driver uses. ``script`` holds answers to give first,
    per path: an ``httpx.Response`` or an exception to raise."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.sent: list[tuple[float, httpx.Request]] = []
        self.script: dict[str, list[Any]] = {}
        self.experiments: dict[str, dict[str, Any]] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.sent.append((self.clock.now, request))
        path = request.url.path
        if self.script.get(path):
            answer = self.script[path].pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        body = json.loads(request.content) if request.content else None
        if path == driver.SIGN_IN:
            return httpx.Response(200, json={"access_token": TOKEN, "expires_in": 3600})
        if path in KINDS:
            assert request.headers.get("X-API-Key") == KEY
            assert "Authorization" not in request.headers
            experiment = next(
                e for e in self.experiments.values() if e["key"] == _exp_key(body)
            )
            allocations = [
                (v["name"], v["traffic_allocation"]) for v in experiment["variants"]
            ]
            op = generator.Op(KINDS[path], path, body)
            status, answer = honest_answer(op, allocations)
            return httpx.Response(status, json=answer)
        assert request.headers.get("Authorization") == f"Bearer {TOKEN}"
        assert "X-API-Key" not in request.headers
        if path == driver.API_KEYS:
            return httpx.Response(201, json={"key": KEY, "id": str(uuid.uuid4())})
        if path == driver.EXPERIMENTS:
            created = str(uuid.uuid4())
            self.experiments[created] = body
            return httpx.Response(201, json={"id": created, "status": "draft"})
        if path.endswith("/start"):
            return httpx.Response(200, json={"status": "active"})
        if path.startswith("/api/v1/results/"):
            return httpx.Response(200, json={"summary": {}, "metrics": []})
        return httpx.Response(404, json={"detail": "Not Found"})

    def requests_to(self, path: str) -> list[tuple[float, httpx.Request]]:
        return [(at, r) for at, r in self.sent if r.url.path == path]


def _exp_key(body: Any) -> Any:
    if "experiment_key" in body:
        return body["experiment_key"]
    return body["events"][0]["experiment_key"]


SCENARIO = generator.Scenario(
    name="D1",
    experiment_key="gt-d1",
    arms=(
        generator.Arm(
            "control", 30, 0.3, repeat=0.3, values=generator.Values(2.0, 0.5)
        ),
        generator.Arm(
            "treatment", 70, 0.4, repeat=0.3, values=generator.Values(2.0, 0.5)
        ),
    ),
    users=240,
    event_name="purchase",
    metrics=(generator.Metric("revenue", "purchase", "revenue"),),
    single_share=0.25,
    assign_batch_size=50,
    event_batch_size=10,
)
PLAN = generator.simulate(7, SCENARIO)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def server(clock: Clock) -> FakeServer:
    return FakeServer(clock)


@pytest.fixture
def signed_in(server: FakeServer, clock: Clock):
    client = httpx.Client(
        base_url="http://replay.test", transport=httpx.MockTransport(server)
    )
    session = driver.Driver(client, clock=clock.time, sleep=clock.sleep)
    session.sign_in("admin@example.com", "admin")
    session.create_api_key()
    yield session
    client.close()


def _replay(session) -> tuple[str, str]:
    stream = io.StringIO()
    experiment_id = session.replay(PLAN, stream, declared={"event_dedup": True})
    return experiment_id, stream.getvalue()


def test_a_plan_is_sent_and_its_ledger_reads_back(signed_in, server):
    experiment_id, text = _replay(signed_in)
    records = ledger.read(io.StringIO(text), secrets=signed_in.secrets)
    expected = contract.valid_events(records).experiments[SCENARIO.experiment_key]
    assert expected.total_users == SCENARIO.users
    assert expected.variant_mismatches == ()
    assert expected.untagged_events == 0
    assert experiment_id in server.experiments
    sent = [r.url.path for _, r in server.sent]
    for route in driver.MIN_INTERVAL:
        assert route in sent, route


def test_the_credentials_go_in_headers_and_never_in_the_ledger(signed_in, server):
    _, text = _replay(signed_in)
    assert TOKEN not in text and KEY not in text
    (login,) = server.requests_to(driver.SIGN_IN)
    assert json.loads(login[1].content) == {
        "email": "admin@example.com",
        "password": "admin",
    }
    (key,) = server.requests_to(driver.API_KEYS)
    assert json.loads(key[1].content)["scopes"] == ["sdk:ruleset"]
    assert signed_in.secrets == (TOKEN, KEY)


def test_the_experiment_is_created_with_the_plan_s_variants_in_order(signed_in, server):
    _replay(signed_in)
    (body,) = server.experiments.values()
    assert [v["name"] for v in body["variants"]] == ["control", "treatment"]
    assert [v["is_control"] for v in body["variants"]] == [True, False]
    assert [v["traffic_allocation"] for v in body["variants"]] == [30, 70]
    assert body["metrics"] == [
        {
            "name": "revenue",
            "event_name": "purchase",
            "metric_type": "revenue",
            "is_primary": True,
        }
    ]
    assert body["key"] == SCENARIO.experiment_key
    assert body["correction_method"] == SCENARIO.correction_method
    assert body["confidence_level"] == SCENARIO.confidence_level


@pytest.mark.parametrize("route", sorted(driver.MIN_INTERVAL))
def test_each_route_is_paced_at_80_percent_of_its_limit(signed_in, server, route):
    _replay(signed_in)
    times = [at for at, _ in server.requests_to(route)]
    assert len(times) >= 2, route
    gaps = [later - earlier for earlier, later in zip(times, times[1:])]
    assert min(gaps) >= driver.MIN_INTERVAL[route] - 1e-9


def test_the_limits_are_80_percent_of_the_server_s():
    # 6,000 a minute for the SDK paths and 60 a minute for /assign/batch
    # (backend/app/middleware/rate_limiter.py); 300 a minute for the rest.
    assert driver.MIN_INTERVAL[ledger.ASSIGN] == 60 / (6000 * 0.8)
    assert driver.MIN_INTERVAL[ledger.TRACK] == 60 / (6000 * 0.8)
    assert driver.MIN_INTERVAL[ledger.BATCH] == 60 / (6000 * 0.8)
    assert driver.MIN_INTERVAL[ledger.ASSIGN_BATCH] == 60 / (60 * 0.8)
    assert driver.OTHER_INTERVAL == 60 / (300 * 0.8)


def test_a_429_is_recorded_waited_out_and_sent_again(signed_in, server, clock):
    server.script[ledger.TRACK] = [
        httpx.Response(
            429, json={"detail": "Too Many Requests"}, headers={"Retry-After": "7"}
        )
    ]
    _, text = _replay(signed_in)
    records = ledger.read(io.StringIO(text))
    track = [r for r in records if r["kind"] == "event" and r.get("route") == "/track"]
    assert track[0]["status"] == 429
    assert track[1]["status"] == 200
    assert {k: v for k, v in track[0].items() if k not in ("status", "seq")} == {
        k: v for k, v in track[1].items() if k not in ("status", "seq")
    }
    assert 7.0 in clock.sleeps
    assert records[-1]["counts"]["429"] == 1
    assert signed_in.routes[ledger.TRACK].rate_limited == 1
    expected = contract.valid_events(records)
    assert expected.rate_limited == 1
    sent = sum(1 for op in PLAN.ops if op.kind == "track") + sum(
        len(op.body["events"]) for op in PLAN.ops if op.kind == "batch"
    )
    experiment = expected.experiments[SCENARIO.experiment_key]
    assert experiment.total_events - experiment.exposures == sent


def test_a_5xx_refuses_the_run_and_is_counted(signed_in, server):
    server.script[ledger.BATCH] = [httpx.Response(500, json={"detail": "boom"})]
    with pytest.raises(ledger.LedgerRefused, match="answered 500"):
        _replay(signed_in)
    assert signed_in.server_errors == 1
    assert signed_in.routes[ledger.BATCH].server_errors == 1


@pytest.mark.parametrize(
    "error,word",
    [
        (httpx.ConnectError("connection refused"), "refused"),
        (httpx.ReadTimeout("timed out"), "timeout"),
        (httpx.RemoteProtocolError("peer closed"), "reset"),
    ],
)
def test_a_missing_answer_refuses_the_run(signed_in, server, error, word):
    server.script[ledger.ASSIGN] = [error]
    with pytest.raises(ledger.LedgerRefused, match=rf"no answer \({word}\)"):
        _replay(signed_in)


def test_a_refused_lifecycle_step_stops_the_replay(signed_in, server):
    server.script[driver.EXPERIMENTS] = [
        httpx.Response(422, json={"detail": "bad metric"})
    ]
    with pytest.raises(
        driver.ReplayError, match="create of experiment gt-d1 answered 422: bad metric"
    ):
        _replay(signed_in)


def test_the_results_are_read_past_the_cache(signed_in, server, clock):
    signed_in.results("1234", label="D1")
    (at, request) = server.requests_to("/api/v1/results/1234")[0]
    assert request.method == "GET"
    assert dict(request.url.params) == {"use_cache": "false"}
    assert ("D1", "results") in signed_in.phases


def test_an_administrative_429_is_waited_out(signed_in, server, clock):
    server.script["/api/v1/results/99"] = [
        httpx.Response(429, json={"detail": "slow down"}, headers={"Retry-After": "3"})
    ]
    signed_in.results("99")
    assert 3.0 in clock.sleeps
    assert len(server.requests_to("/api/v1/results/99")) == 2


@pytest.mark.parametrize(
    "header,seconds",
    [
        ("7", 7.0),
        ("", driver.DEFAULT_RETRY_AFTER),
        ("soon", driver.DEFAULT_RETRY_AFTER),
        ("900", driver.MAX_RETRY_AFTER),
    ],
)
def test_retry_after(header, seconds):
    headers = {"Retry-After": header} if header else {}
    assert driver.retry_after(httpx.Response(429, headers=headers)) == seconds


def test_the_summary_is_counts_and_durations_only(signed_in):
    _replay(signed_in)
    text = "\n".join(signed_in.summary())
    assert f"`{ledger.ASSIGN_BATCH}`" in text
    assert "| D1 | assign_batch |" in text
    for secret in (TOKEN, KEY, "gt-7-", SCENARIO.experiment_key):
        assert secret not in text
