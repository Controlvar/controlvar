"""Signed-in routes answer every request of a burst larger than they can serve
at once, and answer again after it.

Driven through the real app with httpx's ``ASGITransport``, as
``test_api_key_routes_under_overload.py`` drives the API-key routes: the real
``get_db`` (both session factories, the one ``deps`` uses and the one
``backend.app.db.session`` uses, point at a small QueuePool engine on the test
database: 2 + 2 connections, ``pool_timeout`` 1 s), the real
``get_current_user`` with real local tokens of real users, and the real
active-user and superuser checks. The event loop's default worker limit, and
its default executor, are 4 for the test. ``app.dependency_overrides`` is
empty. A burst of 24 concurrent requests is more than that serves at once:
every request must get the route's normal answer.

One core route of each shape a signed-in route had -- the steps FastAPI ran
in a worker thread, in order, from the sign-in to the response -- plus the
dashboard's flag switch and ``GET /api/v1/auth/me`` (every dashboard page asks
for it). ``GET /api/v1/rollout-schedules/`` is the flag page's list, with 20
schedules of two stages each. The full profile's routes run the same check
from ``modules/backend/tests/integration/api/``
(``test_signed_in_module_routes_under_overload.py``), which imports this
file's harness: a core file never imports a module.

Listeners on the engine show that the burst used every connection (peak 4),
that no request's first database step ran on the event loop's thread, and
that a route whose handler is synchronous ran no SQL statement on it: reading
its result's relationships happens in the worker. Asynchronous handlers run
on the event loop by design; how many times one starts a new transaction
there, after a commit, is pinned per route.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

import anyio
import anyio.from_thread
import anyio.to_thread
import httpx
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool
from starlette.concurrency import run_in_threadpool

from backend.app.api import deps, off_loop_route
from backend.app.core.config import settings
from backend.app.core.security import create_local_access_token, get_password_hash
from backend.app.db import request_sessions
from backend.app.db import session as session_module
from backend.app.main import app
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.rollout_schedule import (
    RolloutSchedule,
    RolloutScheduleStatus,
    RolloutStage,
    RolloutStageStatus,
    TriggerType,
)
from backend.app.models.user import User, UserRole

pytestmark = pytest.mark.integration

SCHEMA = "test_experimentation"
POOL_SIZE = 2
MAX_OVERFLOW = 2
CONNECTIONS = POOL_SIZE + MAX_OVERFLOW
WORKERS = 4
BURST = 24
#: One request before the burst and one after, as well as the burst.
REQUESTS = BURST + 2
#: The whole burst must finish within this, or the test fails rather than hangs.
BURST_DEADLINE_SECONDS = 90
#: How long teardown waits for every connection to come back.
DRAIN_SECONDS = 30
#: Schedules on the flag page's list, each with two stages.
SCHEDULES = 20
OLD_PASSWORD = "Overload-Old-1"
NEW_PASSWORD = "Overload-New-1"


@dataclass
class PoolWatch:
    """What the engine's and the sessions' listeners saw."""

    engine: Any
    loop_thread: Optional[int] = None
    checked_out: int = 0
    peak: int = 0
    statement_threads: List[int] = field(default_factory=list)
    #: The thread of every transaction start, per session.
    begins: Dict[int, List[int]] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def restart(self) -> None:
        with self.lock:
            self.peak = self.checked_out
            self.statement_threads.clear()
            self.begins.clear()

    def first_begins_on_loop(self) -> int:
        with self.lock:
            return sum(
                1 for threads in self.begins.values() if threads[0] == self.loop_thread
            )

    def later_begins_on_loop(self) -> int:
        with self.lock:
            return sum(
                1
                for threads in self.begins.values()
                for thread in threads[1:]
                if thread == self.loop_thread
            )

    def statements_on_loop(self) -> int:
        with self.lock:
            return sum(1 for t in self.statement_threads if t == self.loop_thread)


@pytest.fixture
def small_pool(test_db, monkeypatch):
    """Point both session factories at a 2 + 2 QueuePool engine
    (``pool_timeout`` 1 s) on the test database, with listeners."""
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

    @event.listens_for(engine, "checkin")
    def _checkin(_dbapi_connection, _record):
        with watch.lock:
            watch.checked_out -= 1

    @event.listens_for(engine, "before_cursor_execute")
    def _statement(_conn, _cursor, _statement, _parameters, _context, _many):
        with watch.lock:
            watch.statement_threads.append(threading.get_ident())

    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    @event.listens_for(factory, "after_begin")
    def _begin(session, _transaction, _connection):
        with watch.lock:
            watch.begins.setdefault(id(session), []).append(threading.get_ident())

    monkeypatch.setattr(deps, "SessionLocal", factory)
    monkeypatch.setattr(session_module, "SessionLocal", factory)
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "CACHE_ENABLED", False)
    monkeypatch.setattr(settings, "NOTIFICATION_WEBHOOK_URL", "")
    try:
        yield watch
    finally:
        deadline = time.monotonic() + DRAIN_SECONDS
        while engine.pool.checkedout() and time.monotonic() < deadline:
            time.sleep(0.05)
        engine.dispose()


@dataclass
class World:
    """What a row's setup may use, and what the test removes afterwards."""

    db: Any
    monkeypatch: Any
    make_feature_flag: Callable
    users: List[User] = field(default_factory=list)
    cleanups: List[Callable[[], None]] = field(default_factory=list)

    def user(self, role: UserRole = UserRole.DEVELOPER, superuser: bool = False):
        suffix = uuid.uuid4().hex[:8]
        user = User(
            username=f"overload_{suffix}",
            email=f"overload_{suffix}@int.test",
            full_name="Overload User",
            hashed_password=_hashed_old_password(),
            is_active=True,
            is_superuser=superuser,
            role=role,
        )
        self.db.add(user)
        self.db.commit()
        self.db.refresh(user)
        self.users.append(user)
        return user


_HASH: Dict[str, str] = {}


def _hashed_old_password() -> str:
    if "old" not in _HASH:
        _HASH["old"] = get_password_hash(OLD_PASSWORD)
    return _HASH["old"]


def _bearer(user: User) -> dict:
    return {"Authorization": f"Bearer {create_local_access_token(user)}"}


#: send(http, index) for request ``index`` of ``REQUESTS``.
Send = Callable[[httpx.AsyncClient, int], Awaitable[httpx.Response]]


def _get(path: str, user: User, **params) -> Send:
    headers = _bearer(user)

    def send(http, _index):
        return http.get(path, headers=headers, params=params or None)

    return send


def _rollout_schedules(world: World) -> Send:
    owner = world.user(UserRole.ADMIN, superuser=True)
    flag = world.make_feature_flag(key=f"overload-{uuid.uuid4().hex[:8]}")
    for number in range(SCHEDULES):
        schedule = RolloutSchedule(
            name=f"Overload {number}",
            feature_flag_id=flag.id,
            owner_id=owner.id,
            status=RolloutScheduleStatus.DRAFT,
            max_percentage=100,
        )
        world.db.add(schedule)
        world.db.flush()
        for order, percentage in ((1, 10), (2, 100)):
            world.db.add(
                RolloutStage(
                    rollout_schedule_id=schedule.id,
                    name=f"Stage {order}",
                    stage_order=order,
                    target_percentage=percentage,
                    status=RolloutStageStatus.PENDING,
                    trigger_type=TriggerType.MANUAL,
                )
            )
    world.db.commit()

    def remove():
        ids = [
            s.id
            for s in world.db.query(RolloutSchedule).filter(
                RolloutSchedule.feature_flag_id == flag.id
            )
        ]
        world.db.query(RolloutStage).filter(
            RolloutStage.rollout_schedule_id.in_(ids)
        ).delete(synchronize_session=False)
        world.db.query(RolloutSchedule).filter(RolloutSchedule.id.in_(ids)).delete(
            synchronize_session=False
        )
        world.db.query(FeatureFlag).filter(FeatureFlag.id == flag.id).delete()
        world.db.commit()

    world.cleanups.append(remove)
    return _get(
        "/api/v1/rollout-schedules/",
        world.user(),
        feature_flag_id=str(flag.id),
        limit=50,
    )


def _users_me(world: World) -> Send:
    return _get("/api/v1/users/me", world.user())


def _export(world: World) -> Send:
    return _get("/api/v1/export/experiments", world.user(), format="json")


def _admin_stats(world: World) -> Send:
    return _get("/api/v1/admin/stats", world.user(UserRole.ADMIN, superuser=True))


def _metrics_summary(world: World) -> Send:
    return _get("/api/v1/metrics/summary", world.user())


def _notify_test(world: World) -> Send:
    headers = _bearer(world.user(UserRole.ADMIN, superuser=True))

    def send(http, _index):
        return http.post("/api/v1/scheduler/notify/test", headers=headers)

    return send


def _change_password(world: World) -> Send:
    """Each request changes the password of its own user."""
    headers = [_bearer(world.user()) for _ in range(REQUESTS)]
    body = {"current_password": OLD_PASSWORD, "new_password": NEW_PASSWORD}

    def send(http, index):
        return http.post("/api/v1/users/me/password", json=body, headers=headers[index])

    return send


def _toggle(world: World) -> Send:
    """The dashboard's flag switch, on a flag of its own for each request."""
    flags = [
        world.make_feature_flag(
            key=f"overload-{uuid.uuid4().hex[:8]}",
            status=FeatureFlagStatus.INACTIVE,
        )
        for _ in range(REQUESTS)
    ]
    headers = _bearer(world.user())

    def send(http, index):
        return http.post(
            f"/api/v1/feature-flags/{flags[index].id}/toggle",
            json={"reason": "overload"},
            headers=headers,
        )

    def remove():
        from backend.app.models.audit_log import AuditLog

        ids = [flag.id for flag in flags]
        world.db.query(AuditLog).filter(AuditLog.entity_id.in_(ids)).delete(
            synchronize_session=False
        )
        world.db.query(FeatureFlag).filter(FeatureFlag.id.in_(ids)).delete(
            synchronize_session=False
        )
        world.db.commit()

    world.cleanups.append(remove)
    return send


def _auth_me(world: World) -> Send:
    return _get("/api/v1/auth/me", world.user())


@dataclass(frozen=True)
class Row:
    setup: Callable[[World], Send]
    status: int
    #: The handler is synchronous: it runs, with its validation, in a worker.
    synchronous: bool
    #: Transactions started again on the event loop's thread during the burst
    #: (an async handler that commits and then reads).
    later_begins_on_loop: int = 0
    #: An async handler that runs each of its database steps in a worker: no
    #: SQL statement on the event loop's thread either.
    steps_in_workers: bool = False


#: One route for each shape of signed-in route (the steps FastAPI ran in a
#: worker thread before this change, named as in the parameter id), then the
#: flag switch and the sign-in check every page makes.
ROWS = [
    pytest.param(
        "GET /api/v1/rollout-schedules/",
        Row(_rollout_schedules, 200, True),
        id="user-active-handler-validation",
    ),
    pytest.param("GET /api/v1/users/me", Row(_users_me, 200, False), id="user-active"),
    pytest.param(
        "GET /api/v1/export/experiments",
        Row(_export, 200, True),
        id="user-active-handler",
    ),
    pytest.param(
        "GET /api/v1/admin/stats",
        Row(_admin_stats, 200, False),
        id="user-active-superuser",
    ),
    pytest.param(
        "GET /api/v1/metrics/summary",
        Row(_metrics_summary, 200, True),
        id="user-handler-validation",
    ),
    pytest.param(
        "POST /api/v1/scheduler/notify/test",
        Row(_notify_test, 200, True),
        id="user-active-superuser-handler-validation",
    ),
    pytest.param(
        "POST /api/v1/users/me/password",
        Row(_change_password, 204, True),
        id="provider-user-active-handler",
    ),
    pytest.param(
        "POST /api/v1/feature-flags/{flag_id}/toggle",
        Row(_toggle, 200, False, later_begins_on_loop=2 * BURST),
        id="flag-switch",
    ),
    pytest.param("GET /api/v1/auth/me", Row(_auth_me, 200, True), id="auth-me"),
]


@pytest.fixture
def world(db_session, monkeypatch, make_feature_flag):
    made = World(
        db=db_session, monkeypatch=monkeypatch, make_feature_flag=make_feature_flag
    )
    yield made
    db_session.rollback()
    for remove in reversed(made.cleanups):
        remove()
    if made.users:
        db_session.query(User).filter(User.id.in_([u.id for u in made.users])).delete(
            synchronize_session=False
        )
        db_session.commit()


@contextlib.asynccontextmanager
async def _worker_limit(tokens: int):
    """Set the running loop's default worker limit and default executor to
    *tokens* threads, then restore them."""
    limiter = anyio.to_thread.current_default_thread_limiter()
    saved = limiter.total_tokens
    limiter.total_tokens = tokens
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=tokens)
    asyncio.get_running_loop().set_default_executor(executor)
    try:
        yield
    finally:
        limiter.total_tokens = saved
        executor.shutdown(wait=False)


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
    with anyio.fail_after(BURST_DEADLINE_SECONDS):
        results = await asyncio.gather(*requests, return_exceptions=True)
    return [_outcome(result) for result in results]


async def _drain(watch: PoolWatch) -> None:
    deadline = time.monotonic() + DRAIN_SECONDS
    while watch.engine.pool.checkedout() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)


@pytest.mark.regression
@pytest.mark.parametrize("label, row", ROWS)
async def test_every_request_of_a_burst_is_answered(
    label: str, row: Row, small_pool: PoolWatch, world: World
):
    await check_a_burst(label, row, small_pool, world)


async def check_a_burst(label: str, row: Row, watch: PoolWatch, world: World) -> None:
    """Send one request, then ``BURST`` at once, then one more, and check each
    answer and what the listeners saw. The modules' rows call this too."""
    send = row.setup(world)
    assert app.dependency_overrides == {}
    watch.loop_thread = threading.get_ident()
    async with _worker_limit(WORKERS), _client() as http:
        try:
            assert await run_in_threadpool(_limit_seen_by_a_worker) == WORKERS
            before = await send(http, 0)
            assert before.status_code == row.status, (
                f"{label} before the burst: {before.status_code} {before.text}"
            )
            watch.restart()
            outcomes = await _gather([send(http, i) for i in range(1, BURST + 1)])
            peak = watch.peak
            first_on_loop = watch.first_begins_on_loop()
            later_on_loop = watch.later_begins_on_loop()
            statements_on_loop = watch.statements_on_loop()
            after = await send(http, BURST + 1)
        finally:
            await _drain(watch)

    tally = Counter(outcomes)
    assert tally == {row.status: BURST}, (
        f"{BURST} concurrent {label} over {CONNECTIONS} connections and "
        f"{WORKERS} workers: {dict(tally)}, want {{{row.status}: {BURST}}}"
    )
    assert peak == CONNECTIONS, (
        f"peak checkouts during the burst {peak}, want {CONNECTIONS}: the burst "
        "did not use every connection, so it proves nothing"
    )
    assert first_on_loop == 0, (
        f"{first_on_loop} requests of {label} took their first connection on "
        "the event loop's thread"
    )
    assert later_on_loop == row.later_begins_on_loop, (
        f"{label}: {later_on_loop} transactions started again on the event "
        f"loop's thread, want {row.later_begins_on_loop}"
    )
    if row.synchronous or row.steps_in_workers:
        assert statements_on_loop == 0, (
            f"{label}: {statements_on_loop} SQL statements on the event loop's "
            "thread during the burst"
        )
    assert after.status_code == row.status, (
        f"{label} after the burst: {after.status_code} {after.text}"
    )


@pytest.mark.regression
async def test_a_list_route_reads_its_relationships_off_the_event_loop(
    small_pool: PoolWatch, world: World
):
    """The flag page's list with 20 schedules: every schedule's stages are
    read while the result is validated, in the worker, none on the loop."""
    send = _rollout_schedules(world)
    small_pool.loop_thread = threading.get_ident()
    async with _client() as http:
        small_pool.restart()
        response = await send(http, 0)
    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) == SCHEDULES
    assert all(len(item["stages"]) == 2 for item in response.json()["items"])
    assert len(small_pool.statement_threads) > SCHEDULES
    assert small_pool.statements_on_loop() == 0, (
        f"{small_pool.statements_on_loop()} of {len(small_pool.statement_threads)} "
        "SQL statements ran on the event loop's thread"
    )


@pytest.mark.regression
async def test_each_request_releases_only_its_own_session(
    small_pool: PoolWatch, world: World, monkeypatch
):
    """Two requests one after the other in one task: each releases one
    session, its own, and every connection is back in the pool afterwards."""
    send = _rollout_schedules(world)
    released: List[int] = []
    release = off_loop_route.release_request_sessions

    def spy():
        released.append(len(request_sessions.current()))
        release()

    monkeypatch.setattr(off_loop_route, "release_request_sessions", spy)
    async with _client() as http:
        for index in range(2):
            response = await send(http, index)
            assert response.status_code == 200, response.text
    assert released == [1, 1], f"sessions released per request: {released}"
    assert request_sessions.current() == ()
    assert small_pool.engine.pool.checkedout() == 0


# ---------------------------------------------------------------------------
# Refusals: unchanged answers before the handler
# ---------------------------------------------------------------------------


REFUSALS = [
    ("GET", "/api/v1/rollout-schedules/"),
    ("GET", "/api/v1/users/me"),
    ("POST", "/api/v1/scheduler/notify/test"),
    ("GET", "/api/v1/admin/stats"),
]


@pytest.mark.regression
async def test_refusals_are_unchanged(small_pool: PoolWatch, world: World):
    inactive = world.user()
    inactive.is_active = False
    world.db.commit()
    deleted = User(id=uuid.uuid4(), role=UserRole.DEVELOPER, email="gone@int.test")
    developer = world.user()
    async with _client() as http:
        answers = {}
        for method, path in REFUSALS:
            for who, headers in (
                ("inactive", _bearer(inactive)),
                ("deleted", _bearer(deleted)),
                ("developer", _bearer(developer)),
            ):
                response = await http.request(method, path, headers=headers)
                answers[(path, who)] = (response.status_code, response.json())
    inactive_answer = (400, {"detail": "Inactive user"})
    deleted_answer = (401, {"detail": "Could not validate credentials"})
    not_superuser = (403, {"detail": "Not enough permissions"})
    for method, path in REFUSALS:
        assert answers[(path, "inactive")] == inactive_answer, (path, answers)
        assert answers[(path, "deleted")] == deleted_answer, (path, answers)
    for path in ("/api/v1/scheduler/notify/test", "/api/v1/admin/stats"):
        assert answers[(path, "developer")] == not_superuser, (path, answers)
