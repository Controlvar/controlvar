"""API-key routes answer every request of a burst larger than they can serve
at once, and answer again after it (#1728).

Driven through the real app with httpx's ``ASGITransport``, as
``test_api_key_request_connection.py`` is: the real ``deps.get_db``,
``deps.get_api_key``, dependencies and handlers. Only the session factory is
pointed at a small QueuePool engine on the test database (2 + 2 connections,
``pool_timeout`` 1 s), and the event loop's default worker limit is set to 4
for the test, inside the running loop. A burst of 24 concurrent requests is
more than that serves at once: each route must give every request its normal
answer. ``/tracking/assign`` is driven for an experiment that balances its
split within groups (#448), where the assignment runs in a worker: a balanced
burst is answered, and every user of it takes a place in their group's blocks.

Listeners on the engine count checkouts and checkins. Their peak shows that
the burst used every connection, and they record the thread of each checkout:
none of these routes checks out on the event loop's thread.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, List, Optional

import anyio
import anyio.from_thread
import anyio.to_thread
import httpx
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool
from starlette.concurrency import run_in_threadpool

from backend.app.api import deps
from backend.app.main import app
from backend.app.models.api_key import APIKey
from backend.app.models.assignment import Assignment
from backend.app.models.event import Event
from backend.app.models.experiment import (
    Experiment,
    ExperimentStatus,
    ExperimentType,
    Variant,
)
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.global_holdout import GlobalHoldout
from backend.app.models.metrics.metric import ErrorLog, RawMetric
from backend.app.models.sdk_evaluation_count import (
    SdkEvaluationFlagCount,
    SdkEvaluationKeyCount,
)
from backend.app.services import balanced_allocation

pytestmark = pytest.mark.integration

SCHEMA = "test_experimentation"
#: The balanced experiment's setting (#448).
TIER = {"attribute": "tier", "values": ["gold", "silver"]}
#: The counter step's statement timeout during the test, raised so that a
#: slow runner cannot turn a place in the blocks into a fallback; it is not
#: what the burst row is about.
STEP_TIMEOUT_MS = 30_000
POOL_SIZE = 2
MAX_OVERFLOW = 2
CONNECTIONS = POOL_SIZE + MAX_OVERFLOW
WORKERS = 4
BURST = 24
#: The whole burst must finish within this, or the test fails rather than hangs.
BURST_DEADLINE_SECONDS = 60
#: How long teardown waits for every connection to come back.
DRAIN_SECONDS = 30


@dataclass
class PoolWatch:
    """What the engine's checkout and checkin listeners saw."""

    engine: Any
    loop_thread: Optional[int] = None
    checked_out: int = 0
    peak: int = 0
    checkout_threads: List[int] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def restart(self) -> None:
        """Count the peak and the checkout threads from now on."""
        with self.lock:
            self.peak = self.checked_out
            self.checkout_threads.clear()

    def on_loop_thread(self) -> int:
        with self.lock:
            return sum(1 for t in self.checkout_threads if t == self.loop_thread)


@dataclass
class SdkSetup:
    """A server-side key of an active developer, and what the routes act on."""

    headers: dict
    flag_key: str
    experiment_key: str
    #: An ACTIVE experiment that balances its split within ``tier`` (#448).
    balanced_experiment_key: str
    balanced_experiment_id: Any


@pytest.fixture
def small_pool(test_db, monkeypatch):
    """Point ``deps.get_db`` at a 2 + 2 QueuePool engine (``pool_timeout`` 1 s)
    on the test database, with checkout and checkin listeners."""
    engine = create_engine(
        test_db.url,
        pool_pre_ping=True,
        pool_size=POOL_SIZE,
        max_overflow=MAX_OVERFLOW,
        pool_timeout=1,
    )
    assert isinstance(engine.pool, QueuePool)
    watch = PoolWatch(engine=engine)

    @event.listens_for(engine, "connect")
    def _search_path(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute(f"SET search_path TO {SCHEMA}")
        cursor.close()

    @event.listens_for(engine, "checkout")
    def _checkout(_dbapi_connection, _record, _proxy):
        with watch.lock:
            watch.checked_out += 1
            watch.peak = max(watch.peak, watch.checked_out)
            watch.checkout_threads.append(threading.get_ident())

    @event.listens_for(engine, "checkin")
    def _checkin(_dbapi_connection, _record):
        with watch.lock:
            watch.checked_out -= 1

    monkeypatch.setattr(
        deps,
        "SessionLocal",
        sessionmaker(autocommit=False, autoflush=False, bind=engine),
    )
    try:
        yield watch
    finally:
        deadline = time.monotonic() + DRAIN_SECONDS
        while engine.pool.checkedout() and time.monotonic() < deadline:
            time.sleep(0.05)
        engine.dispose()


@pytest.fixture
def no_active_holdout(db_session):
    """No global holdout during the test, so every new user is assigned;
    restored after."""
    parked = (
        db_session.query(GlobalHoldout).filter(GlobalHoldout.is_active.is_(True)).all()
    )
    for row in parked:
        row.is_active = False
    db_session.commit()
    yield
    db_session.rollback()
    for row in parked:
        db_session.merge(row).is_active = True
    db_session.commit()


def _experiment(db_session, admin_user, balance_within=None) -> Experiment:
    """An ACTIVE 50/50 experiment with two variants, committed."""
    suffix = uuid.uuid4().hex[:8]
    experiment = Experiment(
        name=f"Overload {suffix}",
        key=f"overload-{suffix}",
        description="created by test_api_key_routes_under_overload",
        hypothesis="every request of a burst is answered",
        status=ExperimentStatus.ACTIVE,
        experiment_type=ExperimentType.A_B,
        owner_id=admin_user.id,
        balance_within=balance_within,
    )
    db_session.add(experiment)
    db_session.commit()
    db_session.refresh(experiment)
    for name, is_control in (("control", True), ("treatment", False)):
        db_session.add(
            Variant(
                experiment_id=experiment.id,
                name=name,
                description=name,
                is_control=is_control,
                traffic_allocation=50,
                configuration={},
            )
        )
    db_session.commit()
    return experiment


@pytest.fixture
def sdk(db_session, developer_user, make_feature_flag, admin_user):
    """A key with ``sdk:ruleset`` of an active developer, an ACTIVE flag, an
    ACTIVE experiment with two variants and an ACTIVE one that balances its
    split within ``tier``."""
    key, plaintext = APIKey.create_for_user(
        db_session,
        developer_user.id,
        f"overload-{uuid.uuid4().hex[:8]}",
        scopes="sdk:ruleset",
    )
    flag = make_feature_flag(
        key=f"overload-{uuid.uuid4().hex[:8]}",
        status=FeatureFlagStatus.ACTIVE,
        rollout_percentage=100,
    )
    experiment = _experiment(db_session, admin_user)
    balanced = _experiment(db_session, admin_user, balance_within=TIER)
    yield SdkSetup(
        headers={"X-API-Key": plaintext},
        flag_key=flag.key,
        experiment_key=experiment.key,
        balanced_experiment_key=balanced.key,
        balanced_experiment_id=balanced.id,
    )
    db_session.rollback()
    for created in (experiment, balanced):
        for model in (Event, Assignment, Variant):
            db_session.query(model).filter(model.experiment_id == created.id).delete()
        db_session.query(Experiment).filter(Experiment.id == created.id).delete()
    db_session.query(RawMetric).filter(RawMetric.feature_flag_id == flag.id).delete()
    db_session.query(ErrorLog).filter(ErrorLog.feature_flag_id == flag.id).delete()
    for model in (SdkEvaluationKeyCount, SdkEvaluationFlagCount):
        db_session.query(model).filter(model.flag_key == flag.key).delete()
    db_session.query(FeatureFlag).filter(FeatureFlag.id == flag.id).delete()
    db_session.query(APIKey).filter(APIKey.id == key.id).delete()
    db_session.commit()


Send = Callable[[httpx.AsyncClient, SdkSetup], Awaitable[httpx.Response]]


def _ruleset(http: httpx.AsyncClient, sdk: SdkSetup):
    return http.get("/api/v1/sdk/ruleset", headers=sdk.headers)


def _evaluations(http: httpx.AsyncClient, sdk: SdkSetup):
    now = datetime.now(timezone.utc)
    entry = {
        "flag_key": sdk.flag_key,
        "count": 1,
        "enabled_count": 0,
        "window_start": (now - timedelta(seconds=30)).isoformat(),
        "window_end": now.isoformat(),
    }
    return http.post(
        "/api/v1/tracking/evaluations",
        json={"evaluations": [entry]},
        headers=sdk.headers,
    )


def _assign_batch(http: httpx.AsyncClient, sdk: SdkSetup):
    return http.post(
        "/api/v1/tracking/assign/batch",
        json={
            "experiment_key": sdk.experiment_key,
            "users": [{"user_id": f"u-{uuid.uuid4().hex[:12]}"}],
        },
        headers=sdk.headers,
    )


def _errors(http: httpx.AsyncClient, sdk: SdkSetup):
    return http.post("/api/v1/tracking/errors", json={}, headers=sdk.headers)


def _errors_batch(http: httpx.AsyncClient, sdk: SdkSetup):
    return http.post("/api/v1/tracking/errors/batch", json={}, headers=sdk.headers)


def _split_url_routes(http: httpx.AsyncClient, sdk: SdkSetup):
    return http.get("/api/v1/tracking/split-url/routes", headers=sdk.headers)


def _evaluate(http: httpx.AsyncClient, sdk: SdkSetup):
    return http.get(
        f"/api/v1/feature-flags/evaluate/{sdk.flag_key}",
        params={"user_id": f"u-{uuid.uuid4().hex[:12]}"},
        headers=sdk.headers,
    )


def _assign_balanced(http: httpx.AsyncClient, sdk: SdkSetup):
    """A new user of the experiment that balances within ``tier``."""
    return http.post(
        "/api/v1/tracking/assign",
        json={
            "experiment_key": sdk.balanced_experiment_key,
            "user_id": f"u-{uuid.uuid4().hex[:12]}",
            "context": {"tier": "gold"},
        },
        headers=sdk.headers,
    )


def _every_user_took_a_place_in_the_blocks(db_session, sdk: SdkSetup) -> None:
    """After the balanced row: every request assigned a new user in group
    ``gold``, each with a place, the places exactly 0 .. n - 1."""
    db_session.expire_all()
    rows = (
        db_session.query(Assignment.balance_group, Assignment.balance_seq)
        .filter(Assignment.experiment_id == sdk.balanced_experiment_id)
        .all()
    )
    sent = BURST + 2
    assert len(rows) == sent, f"{len(rows)} users assigned, want {sent}"
    groups = Counter(group for group, _ in rows)
    assert groups == {"gold": sent}, f"groups {dict(groups)}, want {{'gold': {sent}}}"
    seqs = sorted(seq for _, seq in rows if seq is not None)
    assert seqs == list(range(sent)), (
        f"{sent - len(seqs)} users outside the blocks; places {seqs}"
    )


#: (label, request, the route's normal status, a check after the burst or
#: None). The two error report routes get ``{}``: the key is checked before
#: the body is refused. ``assign-balanced`` sends new users of an experiment
#: that balances within groups (#448): a balanced burst is answered, and every
#: user of it takes a place in their group's blocks.
ROUTES = [
    pytest.param("GET /api/v1/sdk/ruleset", _ruleset, 200, None, id="ruleset"),
    pytest.param(
        "POST /api/v1/tracking/evaluations",
        _evaluations,
        201,
        None,
        id="evaluations",
    ),
    pytest.param(
        "POST /api/v1/tracking/assign/batch",
        _assign_batch,
        200,
        None,
        id="assign-batch",
    ),
    pytest.param(
        "POST /api/v1/tracking/assign (balanced)",
        _assign_balanced,
        200,
        _every_user_took_a_place_in_the_blocks,
        id="assign-balanced",
    ),
    pytest.param("POST /api/v1/tracking/errors", _errors, 422, None, id="errors"),
    pytest.param(
        "POST /api/v1/tracking/errors/batch",
        _errors_batch,
        422,
        None,
        id="errors-batch",
    ),
    pytest.param(
        "GET /api/v1/tracking/split-url/routes",
        _split_url_routes,
        200,
        None,
        id="split-url-routes",
        marks=pytest.mark.modules,
    ),
]


@contextlib.asynccontextmanager
async def _worker_limit(tokens: int):
    """Set the running loop's default worker limit to *tokens*, then restore it."""
    limiter = anyio.to_thread.current_default_thread_limiter()
    saved = limiter.total_tokens
    limiter.total_tokens = tokens
    try:
        yield
    finally:
        limiter.total_tokens = saved


def _limit_seen_by_a_worker() -> float:
    """Run in a worker: the default worker limit of the loop it serves."""
    return anyio.from_thread.run_sync(
        lambda: anyio.to_thread.current_default_thread_limiter().total_tokens
    )


def _client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


def _outcome(result: Any) -> Any:
    if isinstance(result, httpx.Response):
        return result.status_code
    return type(result).__name__


async def _gather(requests: List[Awaitable[httpx.Response]]) -> List[Any]:
    """Every request at once; the status of each, or its exception's class."""
    with anyio.fail_after(BURST_DEADLINE_SECONDS):
        results = await asyncio.gather(*requests, return_exceptions=True)
    return [_outcome(result) for result in results]


async def _drain(watch: PoolWatch) -> None:
    deadline = time.monotonic() + DRAIN_SECONDS
    while watch.engine.pool.checkedout() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)


@pytest.mark.regression
@pytest.mark.parametrize("label, send, status, check", ROUTES)
async def test_every_request_of_a_burst_is_answered(
    label: str,
    send: Send,
    status: int,
    check: Optional[Callable[[Any, SdkSetup], None]],
    small_pool: PoolWatch,
    sdk: SdkSetup,
    no_active_holdout,
    db_session,
    monkeypatch,
):
    monkeypatch.setattr(balanced_allocation, "BALANCE_STEP_TIMEOUT_MS", STEP_TIMEOUT_MS)
    watch = small_pool
    watch.loop_thread = threading.get_ident()
    async with _worker_limit(WORKERS), _client() as http:
        try:
            # The limit is the one the app's workers run under.
            assert await run_in_threadpool(_limit_seen_by_a_worker) == WORKERS
            before = await send(http, sdk)
            assert before.status_code == status, (
                f"{label} before the burst: {before.status_code} {before.text}"
            )
            watch.restart()
            outcomes = await _gather([send(http, sdk) for _ in range(BURST)])
            peak = watch.peak
            on_loop = watch.on_loop_thread()
            after = await send(http, sdk)
        finally:
            await _drain(watch)

    tally = Counter(outcomes)
    assert tally == {status: BURST}, (
        f"{BURST} concurrent {label} over {CONNECTIONS} connections and "
        f"{WORKERS} workers: {dict(tally)}, want {{{status}: {BURST}}}"
    )
    assert peak == CONNECTIONS, (
        f"peak checkouts during the burst {peak}, want {CONNECTIONS}: the burst "
        "did not use every connection, so it proves nothing"
    )
    assert on_loop == 0, (
        f"{on_loop} checkouts on the event loop's thread during {BURST} {label}"
    )
    # A sanity check only: in-process, the client and the app end together.
    assert after.status_code == status, (
        f"{label} after the burst: {after.status_code} {after.text}"
    )
    if check is not None:
        check(db_session, sdk)


@pytest.mark.regression
async def test_a_ruleset_burst_leaves_flag_evaluation_answering(
    small_pool: PoolWatch, sdk: SdkSetup
):
    rulesets, evaluations = 16, 8
    async with _worker_limit(WORKERS), _client() as http:
        try:
            for send in (_ruleset, _evaluate):
                response = await send(http, sdk)
                assert response.status_code == 200, response.text
            outcomes = await _gather(
                [_ruleset(http, sdk) for _ in range(rulesets)]
                + [_evaluate(http, sdk) for _ in range(evaluations)]
            )
        finally:
            await _drain(small_pool)

    ruleset_tally = Counter(outcomes[:rulesets])
    evaluate_tally = Counter(outcomes[rulesets:])
    assert evaluate_tally == {200: evaluations}, (
        f"{evaluations} GET /api/v1/feature-flags/evaluate/{{flag_key}} sent with "
        f"{rulesets} GET /api/v1/sdk/ruleset: {dict(evaluate_tally)}, "
        f"want {{200: {evaluations}}}"
    )
    assert ruleset_tally == {200: rulesets}, (
        f"{rulesets} GET /api/v1/sdk/ruleset sent with {evaluations} flag "
        f"evaluations: {dict(ruleset_tally)}, want {{200: {rulesets}}}"
    )
