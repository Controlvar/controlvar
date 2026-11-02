"""Shared pieces of the rollout-schedule rule tests (#1460, #1536).

Requests are real ones: a non-superuser DEVELOPER signs in with a local JWT,
and the only dependency override is ``deps.get_db``, which gives each request
a session of its own on the per-process test database, as production does.

``rollout_invariants`` is the teardown every rollout API module runs after each
test (gate 3e): per schedule, the stage targets do not decrease in
``stage_order``, none is above ``max_percentage``, at most one stage is in
progress and no pending stage comes at or before a stage that has started;
per flag, at most one schedule is ACTIVE. The queries are the release-note
queries of #1460 and #1536, plus the maximum. The database is shared with every
other test of the process, so the teardown fails only on a violation that was
not there before the test: the test that wrote it. It reads committed rows
after the test, so it cannot see a race (PD-15).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.api.v1.endpoints.auth import create_local_access_token
from backend.app.core.config import settings
from backend.app.main import app
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.user import User, UserRole

SCHEMA = "test_experimentation"
SCHEDULES = "/api/v1/rollout-schedules"

#: Each query names the rows that break one rule; the teardown compares them
#: before and after a test.
INVARIANT_QUERIES: Dict[str, str] = {
    "a flag with more than one ACTIVE schedule": (
        "SELECT feature_flag_id FROM rollout_schedules WHERE status = 'ACTIVE' "
        "GROUP BY feature_flag_id HAVING count(*) > 1"
    ),
    "a schedule with more than one stage in progress": (
        "SELECT rollout_schedule_id FROM rollout_stages "
        "WHERE status = 'IN_PROGRESS' GROUP BY rollout_schedule_id "
        "HAVING count(*) > 1"
    ),
    "a schedule whose targets go down in stage_order": (
        "SELECT DISTINCT a.rollout_schedule_id FROM rollout_stages a "
        "JOIN rollout_stages b ON b.rollout_schedule_id = a.rollout_schedule_id "
        "AND a.stage_order < b.stage_order "
        "AND a.target_percentage > b.target_percentage"
    ),
    "a schedule with a target above its max_percentage": (
        "SELECT DISTINCT s.rollout_schedule_id FROM rollout_stages s "
        "JOIN rollout_schedules r ON r.id = s.rollout_schedule_id "
        "WHERE s.target_percentage > r.max_percentage"
    ),
    "a schedule with a pending stage at or before a started one": (
        "SELECT DISTINCT p.rollout_schedule_id FROM rollout_stages p "
        "JOIN rollout_stages s ON s.rollout_schedule_id = p.rollout_schedule_id "
        "AND p.status = 'PENDING' "
        "AND s.status IN ('IN_PROGRESS', 'COMPLETED', 'FAILED') "
        "AND p.stage_order <= s.stage_order"
    ),
}


def rule_violations(engine) -> Set[Tuple[str, str]]:
    """``{(rule, id)}`` for every stored schedule or flag that breaks a rule."""
    with engine.connect() as conn:
        conn.execute(text(f"SET search_path TO {SCHEMA}"))
        return {
            (rule, str(row[0]))
            for rule, sql in INVARIANT_QUERIES.items()
            for row in conn.execute(text(sql))
        }


@pytest.fixture
def rollout_invariants(test_db):
    """Gate 3e: fail the test that leaves a schedule breaking a rule."""
    before = rule_violations(test_db)
    yield
    new = sorted(rule_violations(test_db) - before)
    assert not new, f"this test left rollout rows that break a rule: {new}"


@pytest.fixture
def local_auth(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "CACHE_ENABLED", False)


@pytest.fixture
def session_factory(test_db) -> Callable[[], Any]:
    """New sessions on the test database, configured as ``SessionLocal`` is."""
    factory = sessionmaker(bind=test_db, autoflush=False, expire_on_commit=False)

    def make():
        session = factory()
        session.execute(text(f"SET search_path TO {SCHEMA}"))
        return session

    return make


@pytest.fixture
def fresh(session_factory):
    """Run ``work(session)`` in a session of its own, commit, and close it."""

    def run(work):
        session = session_factory()
        try:
            result = work(session)
            session.commit()
            return result
        finally:
            session.close()

    return run


@pytest.fixture
def developer(fresh) -> User:
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"rules1460_{suffix}",
        email=f"rules1460_{suffix}@rules.test",
        full_name="Rollout rules",
        hashed_password="unused: this user signs in by token only",
        is_active=True,
        is_superuser=False,
        role=UserRole.DEVELOPER,
    )

    def work(session):
        session.add(user)
        return user

    return fresh(work)


@pytest.fixture
def new_flag(fresh) -> Callable[[], uuid.UUID]:
    """Make a flag (on, at 0%) and return its id."""

    def make() -> uuid.UUID:
        key = f"rules1460-{uuid.uuid4().hex[:10]}"

        def work(session):
            flag = FeatureFlag(
                key=key,
                name=key,
                status=FeatureFlagStatus.ACTIVE,
                rollout_percentage=0,
            )
            session.add(flag)
            session.flush()
            return flag.id

        return fresh(work)

    return make


@pytest.fixture
def client(session_factory):
    def override_get_db():
        session = session_factory()
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


def send(client, method: str, url: str, user: User, body: Optional[dict] = None):
    headers = {"Authorization": f"Bearer {create_local_access_token(user)}"}
    if body is None:
        return client.request(method, url, headers=headers)
    headers["Content-Type"] = "application/json"
    return client.request(method, url, content=json.dumps(body), headers=headers)


def stage(
    order: int,
    target: int,
    trigger: str = "manual",
    name: Optional[str] = None,
    **extra,
) -> dict:
    body = {
        "name": name or f"stage {order}",
        "stage_order": order,
        "target_percentage": target,
        "trigger_type": trigger,
    }
    body.update(extra)
    return body


def create(client, user: User, flag_id, stages: List[dict], **extra) -> Dict[str, Any]:
    """Create a schedule through the API; returns its body with ``ids`` by name."""
    body = {
        "name": f"rules1460 {uuid.uuid4().hex[:8]}",
        "feature_flag_id": str(flag_id),
        "stages": stages,
    }
    body.update(extra)
    response = send(client, "POST", f"{SCHEDULES}/", user, body)
    assert response.status_code == 201, response.text
    created = response.json()
    created["ids"] = {s["name"]: s["id"] for s in created["stages"]}
    return created


def ok(response, status: int = 200) -> dict:
    assert response.status_code == status, response.text
    return response.json()


def stage_rows(fresh, schedule_id) -> List[Tuple[str, int, int, str]]:
    """``(name, stage_order, target_percentage, status)`` in stage_order."""
    return fresh(
        lambda session: [
            tuple(row)
            for row in session.execute(
                text(
                    "SELECT name, stage_order, target_percentage, status "
                    "FROM rollout_stages WHERE rollout_schedule_id = :id "
                    "ORDER BY stage_order, name"
                ),
                {"id": str(schedule_id)},
            )
        ]
    )


def stage_status(fresh, stage_id) -> str:
    return fresh(
        lambda session: session.execute(
            text("SELECT status FROM rollout_stages WHERE id = :id"),
            {"id": str(stage_id)},
        ).scalar()
    )


def flag_percentage(fresh, flag_id) -> int:
    return fresh(
        lambda session: session.execute(
            text("SELECT rollout_percentage FROM feature_flags WHERE id = :id"),
            {"id": str(flag_id)},
        ).scalar()
    )


def schedule_statuses(fresh, flag_id) -> List[str]:
    return fresh(
        lambda session: sorted(
            row[0]
            for row in session.execute(
                text(
                    "SELECT status FROM rollout_schedules WHERE feature_flag_id = :id"
                ),
                {"id": str(flag_id)},
            )
        )
    )


def tick(session_factory, schedule_ids: Iterable[Any]) -> Dict[str, Any]:
    """One rollout scheduler pass, narrowed to ``schedule_ids``.

    The database is shared with other tests, so every other ACTIVE schedule
    is passed over.
    """
    from backend.app.core.rollout_scheduler import RolloutScheduler

    ours = {str(s) for s in schedule_ids}
    original = RolloutScheduler._process_schedule

    async def only_ours(self, db, schedule, current_time):
        if str(schedule.id) not in ours:
            return False, 0, []
        return await original(self, db, schedule, current_time)

    scheduler = RolloutScheduler(interval_minutes=1)
    scheduler._notification_service = MagicMock()
    with (
        patch.object(RolloutScheduler, "_process_schedule", only_ours),
        patch("backend.app.core.rollout_scheduler.SessionLocal", session_factory),
    ):
        return asyncio.run(scheduler.process_rollout_schedules())


def waiting_on(observer_factory, pid: int) -> bool:
    """Whether some backend is waiting on a lock that backend ``pid`` holds."""
    observer = observer_factory()
    try:
        return bool(
            observer.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE wait_event_type = 'Lock' "
                    "AND :pid = ANY(pg_blocking_pids(pid))"
                ),
                {"pid": pid},
            ).scalar()
        )
    finally:
        observer.close()
