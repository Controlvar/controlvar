"""``GET /api/v1/experiments/{id}/results`` does not hold up the process's
other requests while it computes (#1738).

The route ran its experiment lookup, its checks and the whole results
computation (the analysis, the embedded sequential block, the SRM block and
the snapshot writes) in its ``async def`` handler, on the event loop: while a
large experiment's results were computed, nothing else on the process was
answered, flag evaluations included. The handler now decides the caller's
permission, closes the request's session and makes one ``run_in_threadpool``
call that does the rest.

Driven through the real app with httpx's ``ASGITransport``, as
``test_api_key_request_connection.py`` is: the real ``deps.get_db``, the real
authentication (local tokens and ``mcp:read`` keys) and the real handlers. Only
the session factory is pointed at a QueuePool engine on the test database,
whose listeners record the thread of every checkout and every statement. The
results cache is off, so every request computes.

* ``test_what_the_route_runs_on_the_event_loop``: per authentication path, the
  statements and checkouts made on the event loop's thread. None for a sign-in
  and for a key whose use this request does not record. Two paths still read
  on the loop, on the connection the authentication already holds, as before
  #1738: a key's first use in its window commits, which expires its owner, so
  the permission check reloads it (1 statement); and a user with no role has
  their custom roles and direct grants read (2 statements, full profile only).
* ``test_other_requests_are_answered_while_results_are_computed``: the
  computation is held on a ``threading.Event``; while it is held, a flag
  evaluate, ``/health/live`` and ``/health`` answer, and then the test releases
  it. The assertion is on ``Event.wait()``'s return value, never on time: on
  the old code the held computation blocks the loop, so nothing can release it
  and ``wait()`` returns False when its bound runs out.
* ``test_answers_by_caller``: the answers that must not depend on where the
  work runs -- 404, 403, 400, 401, 500 and 200, by caller and by experiment --
  with every refusal compared byte for byte. The same table passes on the old
  code.

``GET /api/v1/experiments/{id}/daily-results`` and
``/segmented-results/{segment_by}`` had the same defect (#1762), with an async
cache read between their checks and their analysis. Each now decides the
caller's permission, closes the request's session, runs the lookup and the
checks in one ``run_in_threadpool`` call, reads the cache on the event loop,
runs the analysis in a second call and writes the cache on the loop. The
``test_breakdown_*`` tests below are the three above for both routes, and
``test_the_breakdown_cache_is_read_on_the_loop_after_the_checks`` pins that the
cache is read and written on the event loop, and never for a caller the checks
refuse.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import anyio
import anyio.to_thread
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text, update
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

from backend.app.api import deps
from backend.app.api.v1.endpoints import results as results_endpoints
from backend.app.core.config import settings
from backend.app.core.permissions import ROLE_PERMISSIONS, Action, ResourceType
from backend.app.core.security import create_local_access_token, get_password_hash
from backend.app.main import app
from backend.app.models.api_key import APIKey
from backend.app.models.assignment import Assignment
from backend.app.models.event import Event
from backend.app.models.experiment import (
    Experiment,
    ExperimentStatus,
    ExperimentType,
    Metric,
    MetricType,
    Variant,
)
from backend.app.models.feature_flag import FeatureFlagStatus
from backend.app.models.user import User, UserRole
from backend.app.services.analysis_service import AnalysisService
from backend.app.services.cache import CacheService

pytestmark = pytest.mark.integration

SCHEMA = "test_experimentation"

#: How long the held computation waits to be released. A liveness bound, not a
#: measurement: on the new code the test releases it as soon as the other
#: requests have answered; on the old code nothing can, and the wait runs out.
HOLD_SECONDS = 30
#: The whole held exchange must finish within this, or the test fails rather
#: than hangs (on the new code; on the old one the blocked loop cannot fire it,
#: which is why the assertion is on ``wait()``'s return value).
EXCHANGE_DEADLINE_SECONDS = 90

#: The provider reads two statements for a user it is asked about
#: (``modules/backend/tests/integration/rbac/test_rbac_statement_count.py``).
GRANT_READ_STATEMENTS = 2
GRANT_TABLES = ("user_custom_roles", "custom_roles", "direct_permission_grants")

NOT_FOUND = b'{"detail":"Experiment not found"}'
DRAFT_REFUSED = b'{"detail":"Cannot get results for experiments in DRAFT status"}'
CANNOT_VIEW = b'{"detail":"You don\'t have permission to view experiments"}'
NOT_AUTHENTICATED = b'{"detail":"Not authenticated"}'


def _full_profile() -> bool:
    return importlib.util.find_spec("modules") is not None


def _utcnow() -> datetime:
    """Naive UTC, like the stored ``last_used_at``."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _local_sign_in_and_no_cache(monkeypatch):
    """Local tokens, the development sign-in off, and no results cache: a
    cached answer would skip the computation these tests hold and count."""
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "CACHE_ENABLED", False)
    monkeypatch.setattr(
        results_endpoints,
        "_get_cache_service",
        lambda: CacheService(redis_client=None),
    )


@dataclass
class Seen:
    """What the engine's listeners saw, as (thread id, ...) in order."""

    checkouts: List[int] = field(default_factory=list)
    statements: List[Tuple[int, str]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def mark(self) -> Tuple[int, int]:
        with self.lock:
            return len(self.checkouts), len(self.statements)

    def since(self, mark: Tuple[int, int], thread: int):
        """The checkouts and statements on *thread*, and the statements on
        any other thread, since *mark*."""
        with self.lock:
            checkouts = [t for t in self.checkouts[mark[0] :] if t == thread]
            statements = self.statements[mark[1] :]
        on = [s for t, s in statements if t == thread]
        off = [s for t, s in statements if t != thread]
        return len(checkouts), on, off


@pytest.fixture
def seen(test_db, monkeypatch):
    """Point ``deps.get_db`` at a QueuePool engine on the test database, with
    listeners that record the thread of every checkout and statement."""
    engine = create_engine(test_db.url, pool_size=5, max_overflow=5)
    assert isinstance(engine.pool, QueuePool)
    record = Seen()

    @event.listens_for(engine, "connect")
    def _search_path(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute(f"SET search_path TO {SCHEMA}")
        cursor.close()

    @event.listens_for(engine, "checkout")
    def _checkout(_dbapi_connection, _record, _proxy):
        with record.lock:
            record.checkouts.append(threading.get_ident())

    @event.listens_for(engine, "before_cursor_execute")
    def _statement(_conn, _cursor, statement, _parameters, _context, _executemany):
        with record.lock:
            record.statements.append((threading.get_ident(), statement))

    monkeypatch.setattr(
        deps,
        "SessionLocal",
        sessionmaker(autocommit=False, autoflush=False, bind=engine),
    )
    try:
        yield record
    finally:
        engine.dispose()


def _make_user(db_session, role: Optional[UserRole], tag: str) -> User:
    """A non-superuser of *role*. None is written by an UPDATE after the
    insert: the column's default would otherwise make the user a VIEWER."""
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"r1738_{tag}_{suffix}",
        email=f"r1738_{tag}_{suffix}@results.test",
        full_name="Results route user",
        hashed_password=get_password_hash("Demo1234!"),
        is_active=True,
        is_superuser=False,
        role=role or UserRole.VIEWER,
    )
    db_session.add(user)
    db_session.commit()
    if role is None:
        db_session.execute(update(User).where(User.id == user.id).values(role=None))
        db_session.commit()
    db_session.refresh(user)
    assert user.role == role, user.role
    return user


def _grant_experiment_read(db_session, user: User) -> None:
    """An activated custom role holding experiment (read), assigned to *user*
    (full profile), written as ``rbac_states.make_role`` and ``assign`` do."""
    role_id = uuid.uuid4()
    db_session.execute(
        text(
            "INSERT INTO custom_roles (id, name, description, is_system_role, "
            "permissions, created_at, updated_at) VALUES (:id, :name, NULL, false, "
            "CAST(:perms AS jsonb), now(), now())"
        ),
        {
            "id": role_id,
            "name": f"r1738-{uuid.uuid4().hex[:10]}",
            "perms": json.dumps([{"resource": "experiment", "actions": ["read"]}]),
        },
    )
    db_session.execute(
        text(
            "INSERT INTO user_custom_roles (id, user_id, role_id, assigned_at, "
            "activated_at, created_at, updated_at) VALUES (:id, :user, :role, now(), "
            "now(), now(), now())"
        ),
        {"id": uuid.uuid4(), "user": user.id, "role": role_id},
    )
    db_session.commit()


@pytest.fixture
def grants(db_session):
    """Grant experiment (read) to users; their rows are removed after."""
    granted: List[User] = []

    def _grant(user: User) -> None:
        _grant_experiment_read(db_session, user)
        granted.append(user)

    yield _grant
    if granted:
        db_session.rollback()
        db_session.execute(
            text("DELETE FROM user_custom_roles WHERE user_id = ANY(:ids)"),
            {"ids": [u.id for u in granted]},
        )
        db_session.execute(text("DELETE FROM custom_roles WHERE name LIKE 'r1738-%'"))
        db_session.commit()


def _experiment(db_session, admin_user, status: ExperimentStatus) -> Experiment:
    """Two variants, a primary conversion metric and a few assignments and
    conversions, so the computation reads real rows."""
    suffix = uuid.uuid4().hex[:8]
    experiment = Experiment(
        name=f"Results route {suffix}",
        key=f"results-route-{suffix}",
        description="created by test_experiment_results_other_requests",
        hypothesis="computing results holds up nothing else",
        status=status,
        experiment_type=ExperimentType.A_B,
        owner_id=admin_user.id,
        start_date=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )
    db_session.add(experiment)
    db_session.commit()
    variants = []
    for name, is_control in (("control", True), ("treatment", False)):
        variant = Variant(
            experiment_id=experiment.id,
            name=name,
            is_control=is_control,
            traffic_allocation=50,
            configuration={},
        )
        db_session.add(variant)
        variants.append(variant)
    db_session.add(
        Metric(
            experiment_id=experiment.id,
            name="Purchase",
            event_name="purchase",
            metric_type=MetricType.CONVERSION,
            is_primary=True,
        )
    )
    db_session.flush()
    now_iso = datetime.now(timezone.utc).isoformat()
    rows = []
    for variant, converted in zip(variants, (3, 5)):
        for i in range(10):
            user_id = f"{variant.name}-{suffix}-{i:03d}"
            rows.append(
                Assignment(
                    experiment_id=experiment.id,
                    variant_id=variant.id,
                    user_id=user_id,
                )
            )
            if i < converted:
                rows.append(
                    Event(
                        event_type="purchase",
                        event_name="purchase",
                        user_id=user_id,
                        experiment_id=experiment.id,
                        variant_id=variant.id,
                        value=1.0,
                        created_at=now_iso,
                    )
                )
    db_session.add_all(rows)
    db_session.commit()
    db_session.refresh(experiment)
    return experiment


def _key(db_session, user: User, scopes: Optional[str] = "mcp:read"):
    """A key of *user*: its id and its plaintext."""
    row, plaintext = APIKey.create_for_user(
        db_session, user.id, f"r1738-{uuid.uuid4().hex[:8]}", scopes=scopes
    )
    return row.id, plaintext


def _set_last_used(db_session, key_id, value) -> None:
    db_session.execute(
        update(APIKey).where(APIKey.id == key_id).values(last_used_at=value)
    )
    db_session.commit()


def _last_used(db_session, key_id):
    db_session.expire_all()
    return db_session.query(APIKey.last_used_at).filter(APIKey.id == key_id).scalar()


def _bearer(user: User) -> Dict[str, str]:
    return {"Authorization": f"Bearer {create_local_access_token(user)}"}


def _results_url(experiment_id) -> str:
    return f"/api/v1/experiments/{experiment_id}/results"


def _shape(statement: str) -> str:
    """``SELECT test_experimentation.experiments`` for a statement: its verb
    and the table it reads or writes."""
    words = statement.split()
    for marker in ("FROM", "INTO", "UPDATE"):
        if marker in words[:-1]:
            return f"{words[0]} {words[words.index(marker) + 1]}"
    return words[0] if words else statement


def _http() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )


# ---------------------------------------------------------------------------
# Gate 1: what the route runs on the event loop's thread
# ---------------------------------------------------------------------------

#: path -> (statements on the loop's thread, the status). No path checks out a
#: connection on the loop's thread.
LOOP_PATHS = {
    "sign-in": (0, 200),
    "key, use not recorded": (0, 200),
    # get_api_key commits the use, which expires the owner; the permission
    # check reloads it, on the connection the key check holds.
    "key, first use": (1, 200),
    # A user with no role: the permission check reads their custom roles and
    # direct grants (full profile; the core profile has nothing to read).
    "no role": (GRANT_READ_STATEMENTS if _full_profile() else 0, 403),
}


@pytest.mark.regression
@pytest.mark.parametrize("path", list(LOOP_PATHS))
async def test_what_the_route_runs_on_the_event_loop(
    path, seen, db_session, admin_user
):
    loop_thread = threading.get_ident()
    experiment = _experiment(db_session, admin_user, ExperimentStatus.ACTIVE)
    want_statements, want_status = LOOP_PATHS[path]
    key_id = None
    if path == "no role":
        headers = _bearer(_make_user(db_session, None, "norole"))
    elif path == "sign-in":
        headers = _bearer(_make_user(db_session, UserRole.DEVELOPER, "dev"))
    else:
        key_id, plaintext = _key(
            db_session, _make_user(db_session, UserRole.DEVELOPER, "keydev")
        )
        headers = {"X-API-Key": plaintext}
        recent = _utcnow() if path == "key, use not recorded" else None
        _set_last_used(db_session, key_id, recent)
        before = _last_used(db_session, key_id)

    async with _http() as http:
        mark = seen.mark()
        response = await http.get(_results_url(experiment.id), headers=headers)
    checkouts, on_loop, elsewhere = seen.since(mark, loop_thread)

    assert response.status_code == want_status, response.text
    # Guard the guard: the listeners are on the engine the request used.
    assert elsewhere, "no statement seen off the event loop's thread"
    if key_id is not None:
        after = _last_used(db_session, key_id)
        if path == "key, first use":
            assert before is None and after is not None, (before, after)
        else:
            assert after == before, (before, after)

    assert (checkouts, len(on_loop)) == (0, want_statements), (
        f"{path}: on the event loop's thread, {checkouts} checkout(s) and "
        f"{len(on_loop)} statement(s), want 0 and {want_statements}; the "
        f"first: {[_shape(s) for s in on_loop[:5]]}"
    )
    if path == "key, first use":
        table = on_loop[0].split("FROM", 1)[1].split()[0]
        assert table == f"{SCHEMA}.users", on_loop
    if path == "no role" and want_statements:
        assert all(any(t in s for t in GRANT_TABLES) for s in on_loop), on_loop


# ---------------------------------------------------------------------------
# Gate 2: other requests are answered while results are computed
# ---------------------------------------------------------------------------


@pytest.mark.regression
async def test_other_requests_are_answered_while_results_are_computed(
    seen, db_session, admin_user, make_feature_flag, monkeypatch
):
    experiment = _experiment(db_session, admin_user, ExperimentStatus.ACTIVE)
    developer = _make_user(db_session, UserRole.DEVELOPER, "held")
    flag = make_feature_flag(status=FeatureFlagStatus.ACTIVE, rollout_percentage=100)
    _key_id, sdk_key = _key(db_session, developer, scopes=None)

    original = AnalysisService.get_experiment_results
    entered = threading.Event()
    release = threading.Event()
    held: List[bool] = []

    def holding(self, *args, **kwargs):
        entered.set()
        held.append(release.wait(timeout=HOLD_SECONDS))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(AnalysisService, "get_experiment_results", holding)

    async with _http() as http:
        with anyio.fail_after(EXCHANGE_DEADLINE_SECONDS):
            computing = asyncio.create_task(
                http.get(_results_url(experiment.id), headers=_bearer(developer))
            )
            assert await anyio.to_thread.run_sync(entered.wait, HOLD_SECONDS), (
                "the computation was never reached"
            )
            evaluate = await http.get(
                f"/api/v1/feature-flags/evaluate/{flag.key}",
                params={"user_id": f"u-{uuid.uuid4().hex[:12]}"},
                headers={"X-API-Key": sdk_key},
            )
            live = await http.get("/health/live")
            ready = await http.get("/health")
            release.set()
            results = await computing

    assert held == [True], (
        f"the held computation was not released while it was held (wait() "
        f"returned {held}): the flag evaluate and the health checks could not "
        f"answer until it gave up"
    )
    assert evaluate.status_code == 200, evaluate.text
    assert evaluate.json()["enabled"] is True, evaluate.text
    assert live.status_code == 200, live.text
    assert ready.status_code in (200, 503), ready.text
    assert results.status_code == 200, results.text
    assert results.json()["experiment_id"] == str(experiment.id)


# ---------------------------------------------------------------------------
# Gate 3: the answers, by caller and by experiment
# ---------------------------------------------------------------------------

#: The experiments each caller asks about, in this order.
TARGETS = ("unknown", "draft", "active")

ADMITTED = {"unknown": (404, NOT_FOUND), "draft": (400, DRAFT_REFUSED)}
ADMITTED["active"] = (200, None)
REFUSED = {"unknown": (404, NOT_FOUND), "draft": (403, CANNOT_VIEW)}
REFUSED["active"] = (403, CANNOT_VIEW)

#: caller -> target -> (status, the exact body, or None for a 200).
CALLERS = {
    "admin": ADMITTED,
    "developer": ADMITTED,
    "analyst": ADMITTED,
    "viewer": ADMITTED,
    # Every built-in role holds READ on experiments; these two do not.
    "no role": REFUSED,
    "analyst without read": REFUSED,
    # An mcp:read key used for the first time in its window: the use is
    # committed, which expires the owner before the permission check reads it.
    "viewer key, first use": ADMITTED,
    "no role key, first use": REFUSED,
    # A permission check that fails (a broken grant store) is not reached for
    # an unknown experiment, and is a 500 for one that exists.
    "check fails": {
        "unknown": (404, NOT_FOUND),
        "draft": (500, b"Internal Server Error"),
        "active": (500, b"Internal Server Error"),
    },
    "no credential": dict.fromkeys(TARGETS, (401, NOT_AUTHENTICATED)),
}
#: Full profile only: no role, and a custom role granting experiment (read).
GRANT_CALLERS = {
    "no role with a grant": ADMITTED,
    "no role with a grant, key first use": ADMITTED,
}


def _callers() -> List[str]:
    return [*CALLERS, *(GRANT_CALLERS if _full_profile() else ())]


@pytest.fixture
def client(db_session):
    """The real app on the test database; only ``deps.get_db`` is overridden."""
    factory = sessionmaker(
        bind=db_session.get_bind(), autocommit=False, autoflush=False
    )

    def override_get_db():
        session = factory()
        session.execute(text(f"SET search_path TO {SCHEMA}"))
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[deps.get_db] = override_get_db
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
    finally:
        app.dependency_overrides.pop(deps.get_db, None)


def test_the_table_is_the_size_it_says():
    assert len(CALLERS) == 10
    assert len(GRANT_CALLERS) == 2
    assert all(set(row) == set(TARGETS) for row in CALLERS.values())
    assert all(set(row) == set(TARGETS) for row in GRANT_CALLERS.values())


def _caller(caller, db_session, grants, monkeypatch):
    """The headers *caller* sends, and the key to reset before each request
    (None when the caller does not use one)."""
    roles = {
        "admin": UserRole.ADMIN,
        "developer": UserRole.DEVELOPER,
        "analyst": UserRole.ANALYST,
        "viewer": UserRole.VIEWER,
        "analyst without read": UserRole.ANALYST,
        "viewer key, first use": UserRole.VIEWER,
        "check fails": UserRole.VIEWER,
    }
    if caller == "no credential":
        return {}, None
    user = _make_user(db_session, roles.get(caller), caller.split()[0])
    if "grant" in caller:
        grants(user)
    if caller == "analyst without read":
        monkeypatch.setitem(
            ROLE_PERMISSIONS[UserRole.ANALYST], ResourceType.EXPERIMENT, [Action.LIST]
        )
    if caller == "check fails":

        def broken(*_args, **_kwargs):
            raise RuntimeError("the grant store is unavailable")

        monkeypatch.setattr(deps, "check_permission", broken)
    if "key" in caller:
        key_id, plaintext = _key(db_session, user)
        return {"X-API-Key": plaintext}, key_id
    return _bearer(user), None


@pytest.mark.regression
@pytest.mark.parametrize("caller", _callers())
def test_answers_by_caller(caller, client, db_session, admin_user, grants, monkeypatch):
    expected = {**CALLERS, **GRANT_CALLERS}[caller]
    experiments = {
        "unknown": uuid.uuid4(),
        "draft": _experiment(db_session, admin_user, ExperimentStatus.DRAFT).id,
        "active": _experiment(db_session, admin_user, ExperimentStatus.ACTIVE).id,
    }
    headers, key_id = _caller(caller, db_session, grants, monkeypatch)

    wrong = []
    for target in TARGETS:
        if key_id is not None:
            _set_last_used(db_session, key_id, None)
        response = client.get(_results_url(experiments[target]), headers=headers)
        if key_id is not None:
            assert _last_used(db_session, key_id) is not None, (
                f"{caller} on {target}: the key's use was not recorded, so this "
                "was not its first use"
            )
        status, body = expected[target]
        got = response.content
        if response.status_code != status or (body is not None and got != body):
            wrong.append(f"{target}: {response.status_code} {got[:120]!r}")
        elif body is None and response.json()["experiment_id"] != str(
            experiments[target]
        ):
            wrong.append(f"{target}: 200 for another experiment {got[:120]!r}")
    assert not wrong, f"{caller}:\n" + "\n".join(wrong)


# ---------------------------------------------------------------------------
# #1762: the daily and segmented results
# ---------------------------------------------------------------------------

#: route -> (the AnalysisService method it calls, its path).
BREAKDOWNS = {
    "daily": ("get_daily_results", "/api/v1/experiments/{id}/daily-results"),
    "segmented": (
        "get_segmented_results",
        "/api/v1/experiments/{id}/segmented-results/country",
    ),
}
#: route -> the cache key of an experiment's answer (no metric_id).
BREAKDOWN_CACHE_KEYS = {
    "daily": "experiment_daily_results:{id}",
    "segmented": "experiment_segmented_results:{id}:country",
}
#: route -> the sentence of its 500.
BREAKDOWN_FAILURES = {
    "daily": "Could not load the daily results",
    "segmented": "Could not load the segmented results",
}
#: Stands for the route's 500 body in a table: its sentence and the request id.
FAILED = "the route's 500"


def _breakdown_url(route: str, experiment_id) -> str:
    return BREAKDOWNS[route][1].format(id=experiment_id)


def _breakdown_experiment(db_session, admin_user, status: ExperimentStatus):
    """``_experiment`` with a last day, so the daily results cover three days,
    and a country on every event, so the segmented results have a segment."""
    experiment = _experiment(db_session, admin_user, status)
    db_session.execute(
        update(Experiment)
        .where(Experiment.id == experiment.id)
        .values(end_date=datetime(2026, 10, 3, tzinfo=timezone.utc))
    )
    db_session.execute(
        update(Event)
        .where(Event.experiment_id == experiment.id)
        .values(event_metadata={"country": "GB"})
    )
    db_session.commit()
    db_session.refresh(experiment)
    return experiment


def _analysed(route: str, db_session, experiment_id):
    """What AnalysisService computes for *route*'s request, read directly."""
    db_session.expire_all()
    service = AnalysisService(db_session)
    if route == "daily":
        return service.get_daily_results(experiment_id=experiment_id)
    return service.get_segmented_results(
        experiment_id=experiment_id, segment_by="country"
    )


# Gate 1 ---------------------------------------------------------------------

#: path -> (statements on the loop's thread, the status), as LOOP_PATHS. These
#: routes take a sign-in only: a key is refused before it is looked up.
BREAKDOWN_LOOP_PATHS = {
    "sign-in": (0, 200),
    "no role": (GRANT_READ_STATEMENTS if _full_profile() else 0, 403),
    "key, first use": (0, 401),
}


@pytest.mark.regression
@pytest.mark.parametrize("path", list(BREAKDOWN_LOOP_PATHS))
@pytest.mark.parametrize("route", list(BREAKDOWNS))
async def test_breakdown_routes_on_the_event_loop(
    route, path, seen, db_session, admin_user
):
    loop_thread = threading.get_ident()
    experiment = _breakdown_experiment(db_session, admin_user, ExperimentStatus.ACTIVE)
    want_statements, want_status = BREAKDOWN_LOOP_PATHS[path]
    key_id = None
    if path == "no role":
        headers = _bearer(_make_user(db_session, None, "norole"))
    elif path == "sign-in":
        headers = _bearer(_make_user(db_session, UserRole.DEVELOPER, "dev"))
    else:
        key_id, plaintext = _key(
            db_session, _make_user(db_session, UserRole.DEVELOPER, "keydev")
        )
        headers = {"X-API-Key": plaintext}
        _set_last_used(db_session, key_id, None)

    async with _http() as http:
        mark = seen.mark()
        response = await http.get(_breakdown_url(route, experiment.id), headers=headers)
    checkouts, on_loop, elsewhere = seen.since(mark, loop_thread)

    assert response.status_code == want_status, response.text
    if key_id is None:
        # Guard the guard: the listeners are on the engine the request used.
        assert elsewhere, "no statement seen off the event loop's thread"
    else:
        # Refused before anything is read: the key's use is not recorded.
        assert response.content == NOT_AUTHENTICATED, response.content
        assert _last_used(db_session, key_id) is None
        assert not elsewhere, [_shape(s) for s in elsewhere]

    assert (checkouts, len(on_loop)) == (0, want_statements), (
        f"{route}, {path}: on the event loop's thread, {checkouts} checkout(s) "
        f"and {len(on_loop)} statement(s), want 0 and {want_statements}; the "
        f"first: {[_shape(s) for s in on_loop[:5]]}"
    )
    if path == "no role" and want_statements:
        assert all(any(t in s for t in GRANT_TABLES) for s in on_loop), on_loop


# Gate 2 ---------------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize("route", list(BREAKDOWNS))
async def test_other_requests_are_answered_while_a_breakdown_is_computed(
    route, seen, db_session, admin_user, make_feature_flag, monkeypatch
):
    experiment = _breakdown_experiment(db_session, admin_user, ExperimentStatus.ACTIVE)
    developer = _make_user(db_session, UserRole.DEVELOPER, "held")
    flag = make_feature_flag(status=FeatureFlagStatus.ACTIVE, rollout_percentage=100)
    _key_id, sdk_key = _key(db_session, developer, scopes=None)

    method = BREAKDOWNS[route][0]
    original = getattr(AnalysisService, method)
    entered = threading.Event()
    release = threading.Event()
    held: List[bool] = []

    def holding(self, *args, **kwargs):
        entered.set()
        held.append(release.wait(timeout=HOLD_SECONDS))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(AnalysisService, method, holding)

    async with _http() as http:
        with anyio.fail_after(EXCHANGE_DEADLINE_SECONDS):
            computing = asyncio.create_task(
                http.get(
                    _breakdown_url(route, experiment.id), headers=_bearer(developer)
                )
            )
            assert await anyio.to_thread.run_sync(entered.wait, HOLD_SECONDS), (
                f"the {route} analysis was never reached"
            )
            evaluate = await http.get(
                f"/api/v1/feature-flags/evaluate/{flag.key}",
                params={"user_id": f"u-{uuid.uuid4().hex[:12]}"},
                headers={"X-API-Key": sdk_key},
            )
            live = await http.get("/health/live")
            ready = await http.get("/health")
            release.set()
            results = await computing

    assert held == [True], (
        f"the held {route} analysis was not released while it was held (wait() "
        f"returned {held}): the flag evaluate and the health checks could not "
        f"answer until it gave up"
    )
    assert evaluate.status_code == 200, evaluate.text
    assert evaluate.json()["enabled"] is True, evaluate.text
    assert live.status_code == 200, live.text
    assert ready.status_code in (200, 503), ready.text
    assert results.status_code == 200, results.text
    assert results.json() == _analysed(route, db_session, experiment.id)


# Gate 3 ---------------------------------------------------------------------

#: caller -> target -> (status, the exact body, None for a 200 or FAILED).
BREAKDOWN_CALLERS = {
    "admin": ADMITTED,
    "developer": ADMITTED,
    "analyst": ADMITTED,
    "viewer": ADMITTED,
    "no role": REFUSED,
    "analyst without read": REFUSED,
    # A permission check that fails is not reached for an unknown experiment,
    # and is the route's 500 for one that exists.
    "check fails": {
        "unknown": (404, NOT_FOUND),
        "draft": (500, FAILED),
        "active": (500, FAILED),
    },
    "no credential": dict.fromkeys(TARGETS, (401, NOT_AUTHENTICATED)),
    # These routes take a sign-in only; the key is never looked up.
    "viewer key, first use": dict.fromkeys(TARGETS, (401, NOT_AUTHENTICATED)),
}
#: Full profile only: no role, and a custom role granting experiment (read).
BREAKDOWN_GRANT_CALLERS = {"no role with a grant": ADMITTED}


def _breakdown_callers() -> List[str]:
    return [*BREAKDOWN_CALLERS, *(BREAKDOWN_GRANT_CALLERS if _full_profile() else ())]


def test_the_breakdown_table_is_the_size_it_says():
    assert len(BREAKDOWN_CALLERS) == 9
    assert len(BREAKDOWN_GRANT_CALLERS) == 1
    assert all(set(row) == set(TARGETS) for row in BREAKDOWN_CALLERS.values())
    assert all(set(row) == set(TARGETS) for row in BREAKDOWN_GRANT_CALLERS.values())


def _failed_body(route: str, response) -> bytes:
    """*route*'s 500 body: its sentence and this response's request id."""
    sentence = BREAKDOWN_FAILURES[route]
    request_id = response.headers.get("X-Request-ID")
    detail = f"{sentence} (request ID: {request_id})." if request_id else f"{sentence}."
    return json.dumps({"detail": detail}, separators=(",", ":")).encode()


@pytest.mark.regression
@pytest.mark.parametrize("caller", _breakdown_callers())
@pytest.mark.parametrize("route", list(BREAKDOWNS))
def test_breakdown_answers_by_caller(
    route, caller, client, db_session, admin_user, grants, monkeypatch
):
    expected = {**BREAKDOWN_CALLERS, **BREAKDOWN_GRANT_CALLERS}[caller]
    experiments = {
        "unknown": uuid.uuid4(),
        "draft": _breakdown_experiment(
            db_session, admin_user, ExperimentStatus.DRAFT
        ).id,
        "active": _breakdown_experiment(
            db_session, admin_user, ExperimentStatus.ACTIVE
        ).id,
    }
    headers, key_id = _caller(caller, db_session, grants, monkeypatch)

    wrong = []
    for target in TARGETS:
        if key_id is not None:
            _set_last_used(db_session, key_id, None)
        response = client.get(
            _breakdown_url(route, experiments[target]), headers=headers
        )
        if key_id is not None:
            assert _last_used(db_session, key_id) is None, (
                f"{caller} on {target}: the key's use was recorded"
            )
        status, body = expected[target]
        if body == FAILED:
            body = _failed_body(route, response)
        got = response.content
        if response.status_code != status or (body is not None and got != body):
            wrong.append(f"{target}: {response.status_code} {got[:160]!r}")
        elif body is None and response.json() != _analysed(
            route, db_session, experiments[target]
        ):
            wrong.append(f"{target}: 200 with another answer {got[:160]!r}")
    assert not wrong, f"{route}, {caller}:\n" + "\n".join(wrong)


class _RecordingCache:
    """An async cache client: what was asked of it, and on which thread."""

    def __init__(self, stored: Dict[str, str]):
        self.stored = dict(stored)
        self.calls: List[Tuple[str, str, int]] = []

    async def get(self, key: str) -> Optional[str]:
        self.calls.append(("get", key, threading.get_ident()))
        return self.stored.get(key)

    async def setex(self, key: str, _ttl: int, value: str) -> None:
        self.calls.append(("setex", key, threading.get_ident()))
        self.stored[key] = value


@pytest.mark.regression
@pytest.mark.parametrize("route", list(BREAKDOWNS))
async def test_the_breakdown_cache_is_read_on_the_loop_after_the_checks(
    route, seen, db_session, admin_user
):
    """A cached answer is served only to a caller the checks admit (a 403, a
    404 or a draft's 400 never reads it), and the async cache client is used
    on the event loop's thread, where it was made."""
    loop_thread = threading.get_ident()
    cached, fresh = (
        _breakdown_experiment(db_session, admin_user, ExperimentStatus.ACTIVE).id
        for _ in range(2)
    )
    draft = _breakdown_experiment(db_session, admin_user, ExperimentStatus.DRAFT).id
    unknown = uuid.uuid4()

    def key(experiment_id) -> str:
        return BREAKDOWN_CACHE_KEYS[route].format(id=experiment_id)

    marker = [{"from": "cache"}] if route == "daily" else {"from": "cache"}
    cache = _RecordingCache(
        {key(e): json.dumps(marker) for e in (cached, draft, unknown)}
    )

    async def cache_control():
        return deps.CacheControl(enabled=True, redis=cache)

    no_role = _bearer(_make_user(db_session, None, "norole"))
    viewer = _bearer(_make_user(db_session, UserRole.VIEWER, "viewer"))
    app.dependency_overrides[deps.get_cache_control] = cache_control
    try:
        async with _http() as http:
            answers = [
                await http.get(_breakdown_url(route, experiment_id), headers=headers)
                for headers, experiment_id in (
                    (no_role, cached),
                    (no_role, draft),
                    (no_role, unknown),
                    (viewer, unknown),
                    (viewer, draft),
                )
            ]
            assert [(a.status_code, a.content) for a in answers] == [
                (403, CANNOT_VIEW),
                (403, CANNOT_VIEW),
                (404, NOT_FOUND),
                (404, NOT_FOUND),
                (400, DRAFT_REFUSED),
            ]
            assert cache.calls == [], "a refused request read the cache"

            from_cache = await http.get(_breakdown_url(route, cached), headers=viewer)
            computed = await http.get(_breakdown_url(route, fresh), headers=viewer)
    finally:
        app.dependency_overrides.pop(deps.get_cache_control, None)

    assert from_cache.status_code == 200, from_cache.text
    assert from_cache.json() == marker
    assert computed.status_code == 200, computed.text
    assert computed.json() == _analysed(route, db_session, fresh)
    assert json.loads(cache.stored[key(fresh)]) == computed.json()
    assert cache.calls == [
        ("get", key(cached), loop_thread),
        ("get", key(fresh), loop_thread),
        ("setex", key(fresh), loop_thread),
    ], f"the event loop's thread is {loop_thread}"
