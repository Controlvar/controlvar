"""Changes made outside a route write their audit entry, by the right actor (#221).

The service sites, on Postgres: the experiment scheduler, the rollout
scheduler, safety rollback (manual and monitor), Cognito's first sign-in and
Cognito group role sync. Each test reads back, in a session of its own, only
the rows for the entities it created (the schedulers see the whole shared
database).

* G3: each change writes exactly one entry with the right action, entity and
  actor, and ``audit_events_v2`` does not move. An automatic change is
  recorded by a reserved system actor: no ``user_id``, one reserved
  ``user_email``.
* G5: each site's policy when the entry cannot be written, with a
  ``BEFORE INSERT`` trigger on ``audit_logs`` that raises (dropped in a
  ``finally``):

  - safety rollback: the rollback commits, no entry, one ERROR;
  - experiment scheduler: with the trigger refusing one experiment of
    three, all three transition, two entries, one ERROR naming only the
    exception type, and every notification is sent;
  - rollout scheduler: neither the stage start nor the entry is kept, and
    the next tick does both;
  - Cognito first sign-in: the account is created, no entry, one ERROR;
  - Cognito role sync: the role changes, the request is not refused, one
    ERROR, and the next request writes nothing.

* G4: no entry from these sites holds descriptions, rules or error text.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from moto import mock_aws
from sqlalchemy import text, update
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.api.v1.endpoints.auth import create_local_access_token
from backend.app.core.config import settings
from backend.app.core.metrics import audit_write_failures_total
from backend.app.db.session import get_db as session_get_db
from backend.app.main import app
from backend.app.models.audit_log import AuditLog
from backend.app.models.experiment import Experiment, ExperimentStatus
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.rollout_schedule import (
    RolloutSchedule,
    RolloutScheduleStatus,
    RolloutStage,
    RolloutStageStatus,
    TriggerType,
)
from backend.app.models.safety import RollbackTriggerType
from backend.app.models.user import User, UserRole
from backend.app.services import cognito_accounts
from backend.app.services.audit_service import (
    SYSTEM_ACTOR_EMAILS,
    AuditService,
    is_system_actor,
)
from backend.app.services.notification_service import NotificationService
from backend.app.services.safety_service import SafetyService
from backend.tests.integration.cognito_identity import identity

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

SCHEMA = "test_experimentation"
P = "as221"
V1 = "/api/v1"
CANARY = "as221-canary-Zq8"
REFUSAL = "as221 refused"

EXPERIMENT_SCHEDULER = "system:experiment-scheduler"
ROLLOUT_SCHEDULER = "system:rollout-scheduler"
SAFETY_MONITOR = "system:safety-monitor"
COGNITO_SYNC = "system:cognito-sync"


# --- fixtures -----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _local_auth(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "CACHE_ENABLED", False)


def _factory(db_session, **kwargs):
    # autoflush=False, as backend.app.db.session.SessionLocal is configured.
    factory = sessionmaker(bind=db_session.get_bind(), autoflush=False, **kwargs)

    def session():
        s = factory()
        s.execute(text(f"SET search_path TO {SCHEMA}"))
        return s

    return session


@pytest.fixture
def fresh(db_session):
    return _factory(db_session, expire_on_commit=False)


@pytest.fixture
def client(db_session):
    session = _factory(db_session, autocommit=False)

    def override_get_db():
        s = session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[deps.get_db] = override_get_db
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
    finally:
        app.dependency_overrides.pop(deps.get_db, None)


def _make_user(db_session, role, *, superuser=False, **fields):
    suffix = uuid.uuid4().hex[:8]
    values = {
        "username": f"{P}_{role.name.lower()}_{suffix}",
        "email": f"{P}_{suffix}@example.com",
        "full_name": "Service Site User",
        "hashed_password": "unused: signs in by token only",
        "is_active": True,
        "is_superuser": superuser,
        "role": role,
    }
    values.update(fields)
    user = User(**values)
    db_session.add(user)
    db_session.commit()
    return user


@pytest.fixture
def developer(db_session):
    return _make_user(db_session, UserRole.DEVELOPER)


@pytest.fixture
def superuser(db_session):
    return _make_user(db_session, UserRole.ADMIN, superuser=True)


@pytest.fixture(autouse=True)
def _cleanup(db_session):
    """Leave nothing behind: other suites list flags, experiments and users."""
    yield
    db_session.rollback()
    s = db_session
    flag_ids = [
        r.id for r in s.query(FeatureFlag.id).filter(FeatureFlag.key.like(f"{P}-%"))
    ]
    for schedule in s.query(RolloutSchedule).filter(
        RolloutSchedule.feature_flag_id.in_(flag_ids)
    ):
        s.delete(schedule)
    s.commit()
    users = s.query(User).filter(User.username.like(f"{P}_%")).all()
    user_ids = [u.id for u in users]
    exps = (
        s.query(Experiment)
        .filter(Experiment.name.like(f"{P}%") | Experiment.owner_id.in_(user_ids))
        .all()
    )
    exp_ids = [e.id for e in exps]
    for exp in exps:
        s.delete(exp)
    s.commit()
    entity_ids = flag_ids + exp_ids + user_ids
    s.query(AuditLog).filter(
        AuditLog.entity_id.in_(entity_ids)
        | AuditLog.user_email.like(f"{P}%")
        | AuditLog.entity_name.like(f"{P}%")
    ).delete(synchronize_session=False)
    s.execute(
        text(
            f"DELETE FROM {SCHEMA}.safety_rollback_records WHERE feature_flag_id = ANY(:ids)"
        ),
        {"ids": flag_ids},
    )
    s.execute(
        text(
            f"DELETE FROM {SCHEMA}.feature_flag_safety_configs "
            "WHERE feature_flag_id = ANY(:ids)"
        ),
        {"ids": flag_ids},
    )
    s.query(FeatureFlag).filter(FeatureFlag.id.in_(flag_ids)).delete(
        synchronize_session=False
    )
    for user in users:
        s.delete(user)
    s.commit()


@contextmanager
def refuse_audit(db_session, only_entity: Optional[uuid.UUID] = None):
    """Every insert into ``audit_logs`` (or only those for one entity) raises.

    The trigger is dropped in a ``finally``, however the test ends.
    """
    when = f"WHEN (NEW.entity_id = '{only_entity}'::uuid) " if only_entity else ""
    db_session.execute(
        text(
            f"CREATE OR REPLACE FUNCTION {SCHEMA}.as221_refuse_audit() "
            "RETURNS trigger LANGUAGE plpgsql AS "
            f"$$ BEGIN RAISE EXCEPTION '{REFUSAL}'; END $$"
        )
    )
    db_session.execute(
        text(
            f"CREATE TRIGGER as221_refuse_audit BEFORE INSERT ON {SCHEMA}.audit_logs "
            f"FOR EACH ROW {when}EXECUTE FUNCTION {SCHEMA}.as221_refuse_audit()"
        )
    )
    db_session.commit()
    try:
        yield
    finally:
        db_session.rollback()
        db_session.execute(
            text(f"DROP TRIGGER IF EXISTS as221_refuse_audit ON {SCHEMA}.audit_logs")
        )
        db_session.execute(
            text(f"DROP FUNCTION IF EXISTS {SCHEMA}.as221_refuse_audit()")
        )
        db_session.commit()


# --- helpers ------------------------------------------------------------------


def _auth(user):
    return {"Authorization": f"Bearer {create_local_access_token(user)}"}


def _v2_count(fresh) -> int:
    s = fresh()
    try:
        return s.execute(
            text(f"SELECT count(*) FROM {SCHEMA}.audit_events_v2")
        ).scalar()
    finally:
        s.close()


def _rows(fresh, entity_id, action=None):
    s = fresh()
    try:
        q = s.query(AuditLog).filter(AuditLog.entity_id == entity_id)
        if action is not None:
            q = q.filter(AuditLog.action_type == action)
        return q.order_by(AuditLog.timestamp).all()
    finally:
        s.close()


def _one(fresh, entity_id, action, *, email, user_id=None):
    """Exactly one row for this entity and action, by this actor."""
    rows = _rows(fresh, entity_id, action)
    assert len(rows) == 1, f"expected 1, found {len(rows)}"
    row = rows[0]
    assert row.user_email == email
    assert row.user_id == user_id
    return row


def _mark(fresh, entity_id):
    """The ids of the entity's audit rows now: take it just before the change."""
    return {row.id for row in _rows(fresh, entity_id)}


def _only_row_since(fresh, entity_id, mark, action, *, email, user_id=None):
    """The ONE row the entity gained since *mark*, whatever its action.

    ``_one`` counts rows of a single action, so a rollback that also wrote an
    entry of another action would pass it. For a rollback every new row counts:
    exactly one, and it is *action*, by this actor."""
    new = [row for row in _rows(fresh, entity_id) if row.id not in mark]
    assert [row.action_type for row in new] == [action], (
        f"expected exactly one new {action} row, found "
        f"{[row.action_type for row in new]}"
    )
    row = new[0]
    assert row.user_email == email
    assert row.user_id == user_id
    return row


def _values(row):
    def load(v):
        return json.loads(v) if v else None

    return load(row.old_value), load(row.new_value)


def _audit_errors(caplog):
    return [
        r
        for r in caplog.records
        if r.levelno >= logging.ERROR
        and r.getMessage().startswith("Failed to create audit log")
    ]


def _assert_one_type_only_error(caplog):
    errors = _audit_errors(caplog)
    assert len(errors) == 1, [r.getMessage() for r in errors]
    record = errors[0]
    # The exception type, and nothing of its text.
    assert re.search(r" \(\w+Error\)$", record.getMessage()), record.getMessage()
    assert REFUSAL not in record.getMessage()
    assert record.exc_info is None
    assert not record.args or all(REFUSAL not in str(a) for a in record.args)


def _no_canary(row):
    for column in (
        "user_email",
        "entity_name",
        "old_value",
        "new_value",
        "reason",
    ):
        assert CANARY not in (getattr(row, column) or ""), column
        assert REFUSAL not in (getattr(row, column) or ""), column


def _now():
    return datetime.now(timezone.utc)


def _naive(dt):
    return dt.replace(tzinfo=None)


# --- experiments and the experiment scheduler ----------------------------------


def _create_experiment(client, developer) -> uuid.UUID:
    response = client.post(
        f"{V1}/experiments/",
        json={
            "name": f"{P} {uuid.uuid4().hex[:8]}",
            "description": f"description {CANARY}",
            "hypothesis": f"hypothesis {CANARY}",
            "experiment_type": "a_b",
            "variants": [
                {"name": "Control", "is_control": True, "traffic_allocation": 50},
                {"name": "Treatment", "is_control": False, "traffic_allocation": 50},
            ],
            "metrics": [
                {
                    "name": "Conversion Rate",
                    "event_name": "purchase",
                    "metric_type": "conversion",
                    "is_primary": True,
                }
            ],
        },
        headers=_auth(developer),
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["id"])


def _set_experiment(fresh, experiment_id, **values):
    s = fresh()
    try:
        s.execute(
            update(Experiment).where(Experiment.id == experiment_id).values(**values)
        )
        s.commit()
    finally:
        s.close()


def _experiment_status(fresh, experiment_id):
    s = fresh()
    try:
        return s.get(Experiment, experiment_id).status
    finally:
        s.close()


def _experiment_tick(db_session):
    """One experiment-scheduler tick on the test database. Returns the ids
    it sent a notification for."""
    from backend.app.core.scheduler import ExperimentScheduler

    sent = set()
    notifications = NotificationService()
    notifications._slack = MagicMock()
    notifications._email = MagicMock()

    def capture(event_):
        sent.add(event_.experiment_id)
        return True

    notifications._send = capture
    scheduler = ExperimentScheduler()
    scheduler._notification_service = notifications
    with patch("backend.app.core.scheduler.SessionLocal", _factory(db_session)):
        asyncio.run(scheduler.process_scheduled_experiments())
    return sent


def _due_to_start(client, fresh, developer, n=3):
    due = _naive(_now() - timedelta(minutes=5))
    ids = [_create_experiment(client, developer) for _ in range(n)]
    for experiment_id in ids:
        _set_experiment(fresh, experiment_id, start_date=due)
    return ids


def _due_to_end(client, fresh, developer, n=3):
    ids = [_create_experiment(client, developer) for _ in range(n)]
    for experiment_id in ids:
        _set_experiment(
            fresh,
            experiment_id,
            status=ExperimentStatus.ACTIVE,
            start_date=_naive(_now() - timedelta(hours=2)),
            end_date=_naive(_now() - timedelta(hours=1)),
        )
    return ids


def test_scheduled_start_is_recorded_by_the_experiment_scheduler(
    client, fresh, db_session, developer
):
    ids = _due_to_start(client, fresh, developer, n=1)
    v2 = _v2_count(fresh)
    _experiment_tick(db_session)
    row = _one(fresh, ids[0], "experiment_start", email=EXPERIMENT_SCHEDULER)
    assert row.entity_type == "experiment"
    assert row.reason == "scheduled start"
    old, new = _values(row)
    assert old["status"] == "draft" and new["status"] == "active"
    assert is_system_actor(row.user_id, row.user_email)
    assert _v2_count(fresh) == v2
    _no_canary(row)


def test_scheduled_end_is_recorded_by_the_experiment_scheduler(
    client, fresh, db_session, developer
):
    ids = _due_to_end(client, fresh, developer, n=1)
    v2 = _v2_count(fresh)
    _experiment_tick(db_session)
    row = _one(fresh, ids[0], "experiment_complete", email=EXPERIMENT_SCHEDULER)
    old, new = _values(row)
    assert old["status"] == "active" and new["status"] == "completed"
    assert row.reason == "scheduled end"
    assert _v2_count(fresh) == v2
    _no_canary(row)


@pytest.mark.parametrize("phase", ["start", "end"])
def test_a_refused_entry_for_one_experiment_of_three_keeps_every_transition(
    client, fresh, db_session, developer, caplog, phase
):
    """PE v2 2a-2c: the entry goes in a savepoint of its own inside the
    row's, so the transition commits without it, and is not retried."""
    if phase == "start":
        ids = _due_to_start(client, fresh, developer)
        action, done = "experiment_start", ExperimentStatus.ACTIVE
    else:
        ids = _due_to_end(client, fresh, developer)
        action, done = "experiment_complete", ExperimentStatus.COMPLETED
    first, refused, last = ids
    failures = audit_write_failures_total._value.get()

    caplog.set_level(logging.INFO)
    with refuse_audit(db_session, only_entity=refused):
        sent = _experiment_tick(db_session)

    for experiment_id in ids:
        assert _experiment_status(fresh, experiment_id) == done, experiment_id
    _one(fresh, first, action, email=EXPERIMENT_SCHEDULER)
    _one(fresh, last, action, email=EXPERIMENT_SCHEDULER)
    assert _rows(fresh, refused, action) == []
    # The notification follows the row's own commit, entry or not.
    assert {str(i) for i in ids} <= sent
    _assert_one_type_only_error(caplog)
    assert audit_write_failures_total._value.get() == failures + 1

    # Not retried: the next tick finds nothing to do for it.
    _experiment_tick(db_session)
    assert _rows(fresh, refused, action) == []


# --- the rollout scheduler -----------------------------------------------------


def _flag(fresh, *, percentage=0, status=FeatureFlagStatus.ACTIVE):
    suffix = uuid.uuid4().hex[:10]
    s = fresh()
    try:
        flag = FeatureFlag(
            key=f"{P}-{suffix}",
            name=f"{P} flag {suffix}",
            description=f"description {CANARY}",
            status=status,
            rollout_percentage=percentage,
            targeting_rules={
                "logical_operator": "AND",
                "groups": [
                    {
                        "logical_operator": "AND",
                        "conditions": [
                            {
                                "attribute": "country",
                                "operator": "equals",
                                "value": CANARY,
                            }
                        ],
                    }
                ],
            },
        )
        s.add(flag)
        s.commit()
        return flag.id
    finally:
        s.close()


def _schedule(fresh, flag_id, *, status=RolloutScheduleStatus.ACTIVE, target=25):
    """An ACTIVE schedule whose one pending stage may start at once."""
    s = fresh()
    try:
        schedule = RolloutSchedule(
            name=f"{P} schedule", feature_flag_id=flag_id, status=status
        )
        s.add(schedule)
        s.flush()
        stage = RolloutStage(
            rollout_schedule_id=schedule.id,
            name="stage 1",
            stage_order=1,
            target_percentage=target,
            status=RolloutStageStatus.PENDING,
            trigger_type=TriggerType.TIME_BASED,
            trigger_configuration={"duration": 24},
        )
        s.add(stage)
        s.commit()
        return schedule.id, stage.id
    finally:
        s.close()


def _rollout_tick(db_session):
    from backend.app.core.rollout_scheduler import RolloutScheduler

    scheduler = RolloutScheduler(interval_minutes=1)
    scheduler._notification_service = MagicMock()
    with patch("backend.app.core.rollout_scheduler.SessionLocal", _factory(db_session)):
        asyncio.run(scheduler.process_rollout_schedules())
    return scheduler._notification_service


def _flag_state(fresh, flag_id, stage_id=None):
    s = fresh()
    try:
        flag = s.get(FeatureFlag, flag_id)
        stage = s.get(RolloutStage, stage_id) if stage_id else None
        return flag.rollout_percentage, flag.status, stage.status if stage else None
    finally:
        s.close()


def test_stage_start_is_recorded_by_the_rollout_scheduler(fresh, db_session):
    flag_id = _flag(fresh, percentage=0)
    _, stage_id = _schedule(fresh, flag_id, target=25)
    v2 = _v2_count(fresh)
    _rollout_tick(db_session)
    assert _flag_state(fresh, flag_id, stage_id)[::2] == (
        25,
        RolloutStageStatus.IN_PROGRESS,
    )
    row = _one(fresh, flag_id, "feature_flag_update", email=ROLLOUT_SCHEDULER)
    assert row.entity_type == "feature_flag"
    assert row.reason == "rollout schedule stage started"
    assert _values(row) == ({"rollout_percentage": 0}, {"rollout_percentage": 25})
    assert _v2_count(fresh) == v2
    _no_canary(row)


def test_a_refused_entry_keeps_neither_the_stage_start_nor_the_entry(
    fresh, db_session, caplog
):
    """Fail-closed: the rollout does not widen unrecorded; the next tick retries."""
    flag_id = _flag(fresh, percentage=0)
    _, stage_id = _schedule(fresh, flag_id, target=25)
    notifications = None
    with refuse_audit(db_session, only_entity=flag_id):
        notifications = _rollout_tick(db_session)
    assert _flag_state(fresh, flag_id, stage_id)[::2] == (0, RolloutStageStatus.PENDING)
    assert _rows(fresh, flag_id) == []
    notifications.notify_rollout_advanced.assert_not_called()

    _rollout_tick(db_session)
    assert _flag_state(fresh, flag_id, stage_id)[::2] == (
        25,
        RolloutStageStatus.IN_PROGRESS,
    )
    _one(fresh, flag_id, "feature_flag_update", email=ROLLOUT_SCHEDULER)


# --- safety rollback ------------------------------------------------------------


def test_manual_rollback_is_recorded_by_the_calling_user(
    client, fresh, db_session, superuser
):
    flag_id = _flag(fresh, percentage=60)
    schedule_id, _ = _schedule(fresh, flag_id, target=80)
    v2 = _v2_count(fresh)
    mark = _mark(fresh, flag_id)
    response = client.post(
        f"{V1}/safety/feature-flags/{flag_id}/rollback",
        params={"percentage": 10, "reason": "Manual rollback"},
        headers=_auth(superuser),
    )
    assert response.status_code == 200, response.text
    assert response.json()["success"] is True
    row = _only_row_since(
        fresh,
        flag_id,
        mark,
        "safety_rollback",
        email=superuser.email,
        user_id=superuser.id,
    )
    assert row.entity_type == "feature_flag"
    assert row.reason == "Manual rollback"
    old, new = _values(row)
    assert old is None
    assert new == {
        "trigger_type": "manual",
        "previous_percentage": 60,
        "new_percentage": 10,
        "deactivated": False,
        "paused_schedules": [str(schedule_id)],
    }
    assert not is_system_actor(row.user_id, row.user_email)
    assert _v2_count(fresh) == v2
    _no_canary(row)


def test_monitor_rollback_is_recorded_by_the_safety_monitor(fresh, db_session):
    """The safety monitor calls the service with no user, as here."""
    flag_id = _flag(fresh, percentage=60)
    mark = _mark(fresh, flag_id)
    s = fresh()
    try:
        result = asyncio.run(
            SafetyService(s).async_rollback_feature_flag(
                feature_flag_id=flag_id,
                percentage=0,
                reason="Automatic rollback due to error_rate exceeding threshold",
                trigger_type=RollbackTriggerType.AUTOMATIC,
            )
        )
    finally:
        s.close()
    assert result.success is True
    row = _only_row_since(fresh, flag_id, mark, "safety_rollback", email=SAFETY_MONITOR)
    _, new = _values(row)
    assert new["trigger_type"] == "automatic"
    assert new["deactivated"] is True
    assert new["new_percentage"] == 0
    assert is_system_actor(row.user_id, row.user_email)
    _no_canary(row)


def test_a_refused_entry_never_blocks_a_rollback(
    client, fresh, db_session, superuser, caplog
):
    """The rollback commits -- percentage lowered, schedule paused, record
    kept -- with no entry and one ERROR (T125)."""
    flag_id = _flag(fresh, percentage=60)
    schedule_id, _ = _schedule(fresh, flag_id, target=80)
    failures = audit_write_failures_total._value.get()
    caplog.set_level(logging.INFO)
    with refuse_audit(db_session):
        response = client.post(
            f"{V1}/safety/feature-flags/{flag_id}/rollback",
            params={"percentage": 10},
            headers=_auth(superuser),
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True, body
    assert body["details"]["paused_schedules"] == [str(schedule_id)]
    assert _flag_state(fresh, flag_id)[0] == 10
    s = fresh()
    try:
        assert (
            s.get(RolloutSchedule, schedule_id).status == RolloutScheduleStatus.PAUSED
        )
        records = s.execute(
            text(
                f"SELECT count(*) FROM {SCHEMA}.safety_rollback_records "
                "WHERE feature_flag_id = :f"
            ),
            {"f": flag_id},
        ).scalar()
    finally:
        s.close()
    assert records == 1
    assert _rows(fresh, flag_id) == []
    _assert_one_type_only_error(caplog)
    assert audit_write_failures_total._value.get() == failures + 1


def test_a_refused_monitor_entry_never_blocks_a_rollback(fresh, db_session, caplog):
    flag_id = _flag(fresh, percentage=60)
    caplog.set_level(logging.INFO)
    with refuse_audit(db_session):
        s = fresh()
        try:
            result = asyncio.run(
                SafetyService(s).async_rollback_feature_flag(
                    feature_flag_id=flag_id,
                    percentage=0,
                    reason="Automatic rollback",
                    trigger_type=RollbackTriggerType.AUTOMATIC,
                )
            )
        finally:
            s.close()
    assert result.success is True
    assert _flag_state(fresh, flag_id)[:2] == (0, FeatureFlagStatus.INACTIVE)
    assert _rows(fresh, flag_id) == []
    _assert_one_type_only_error(caplog)


# --- Cognito: the first sign-in and group role sync -----------------------------


def _blank_aws(monkeypatch) -> None:
    for name in ("AWS_PROFILE", "AWS_SESSION_TOKEN", "AWS_DEFAULT_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_REGION", "us-east-1")


@pytest.fixture
def cognito(monkeypatch, db_session):
    """Cognito mode, role sync on, with the Cognito boundary stubbed inside a
    moto fence. Yields ``(client, use)``: ``use(identity)`` sets what the
    boundary answers."""
    _blank_aws(monkeypatch)
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "cognito")
    monkeypatch.setattr(settings, "SYNC_ROLES_ON_LOGIN", True)
    monkeypatch.setattr(settings, "COGNITO_ADMIN_GROUPS", ["SuperUsers"])
    answer = {}
    session = _factory(db_session, autocommit=False)

    def override_get_db():
        s = session()
        try:
            yield s
        finally:
            s.close()

    saved = dict(app.dependency_overrides)
    app.dependency_overrides[deps.get_db] = override_get_db
    app.dependency_overrides[session_get_db] = override_get_db
    try:
        with mock_aws():
            monkeypatch.setattr(
                deps.auth_service, "get_user_with_groups", lambda token: answer["value"]
            )
            yield (
                TestClient(app, raise_server_exceptions=False),
                (lambda value: answer.__setitem__("value", value)),
            )
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(saved)


def _linked_user(db_session, role, *, superuser=False):
    sub = uuid.uuid4().hex
    user = _make_user(
        db_session,
        role,
        superuser=superuser,
        hashed_password=None,
        external_id=cognito_accounts.cognito_external_id(sub),
    )
    return user, sub


def _me(client):
    return client.get(f"{V1}/users/me", headers={"Authorization": "Bearer a-token"})


def test_first_sign_in_is_recorded_by_the_new_account(cognito, fresh):
    client, use = cognito
    suffix = uuid.uuid4().hex[:8]
    use(
        identity(
            f"{P}_new_{suffix}",
            uuid.uuid4().hex,
            email=f"{P}_new_{suffix}@example.com",
            groups=["Developers"],
        )
    )
    v2 = _v2_count(fresh)
    response = _me(client)
    assert response.status_code == 200, response.text
    user_id = uuid.UUID(response.json()["id"])
    row = _one(
        fresh,
        user_id,
        "user_create",
        email=f"{P}_new_{suffix}@example.com",
        user_id=user_id,
    )
    assert row.entity_type == "user"
    assert row.reason == "first sign-in"
    old, new = _values(row)
    assert old is None
    assert new == {
        "username": f"{P}_new_{suffix}",
        "role": "DEVELOPER",
        "is_active": True,
        "is_superuser": False,
    }
    assert _v2_count(fresh) == v2


def test_a_refused_entry_still_creates_the_account(cognito, fresh, db_session, caplog):
    client, use = cognito
    suffix = uuid.uuid4().hex[:8]
    use(
        identity(
            f"{P}_new_{suffix}",
            uuid.uuid4().hex,
            email=f"{P}_new_{suffix}@example.com",
            groups=["Viewers"],
        )
    )
    caplog.set_level(logging.INFO)
    with refuse_audit(db_session):
        response = _me(client)
    assert response.status_code == 200, response.text
    user_id = uuid.UUID(response.json()["id"])
    s = fresh()
    try:
        assert s.get(User, user_id) is not None
    finally:
        s.close()
    assert _rows(fresh, user_id) == []
    _assert_one_type_only_error(caplog)


def test_group_role_change_is_recorded_by_cognito_sync(cognito, fresh, db_session):
    client, use = cognito
    user, sub = _linked_user(db_session, UserRole.VIEWER)
    use(identity(user.username, sub, email=user.email, groups=["Developers"]))
    v2 = _v2_count(fresh)
    response = _me(client)
    assert response.status_code == 200, response.text
    row = _one(fresh, user.id, "role_assign", email=COGNITO_SYNC)
    assert _values(row) == (
        {"role": "VIEWER", "is_superuser": False},
        {"role": "DEVELOPER", "is_superuser": False},
    )
    assert is_system_actor(row.user_id, row.user_email)
    assert _v2_count(fresh) == v2


def test_a_superuser_flag_change_alone_is_recorded(cognito, fresh, db_session):
    """User changes record the superuser flag: the role stays ADMIN."""
    client, use = cognito
    user, sub = _linked_user(db_session, UserRole.ADMIN)
    use(identity(user.username, sub, email=user.email, groups=["Admins", "SuperUsers"]))
    response = _me(client)
    assert response.status_code == 200, response.text
    row = _one(fresh, user.id, "role_assign", email=COGNITO_SYNC)
    old, new = _values(row)
    assert old == {"role": "ADMIN", "is_superuser": False}
    assert new == {"role": "ADMIN", "is_superuser": True}


def test_unchanged_groups_write_nothing(cognito, fresh, db_session):
    client, use = cognito
    user, sub = _linked_user(db_session, UserRole.DEVELOPER)
    use(identity(user.username, sub, email=user.email, groups=["Developers"]))
    assert _me(client).status_code == 200
    assert _rows(fresh, user.id) == []


def test_a_refused_entry_still_applies_the_role_change(
    cognito, fresh, db_session, caplog
):
    """PE v2 3b: the change applies, no 401; the next request finds the role
    already equal and writes nothing, so the entry is not retried."""
    client, use = cognito
    user, sub = _linked_user(db_session, UserRole.VIEWER)
    use(identity(user.username, sub, email=user.email, groups=["Analysts"]))
    caplog.set_level(logging.INFO)
    with refuse_audit(db_session):
        response = _me(client)
    assert response.status_code == 200, response.text
    s = fresh()
    try:
        assert s.get(User, user.id).role == UserRole.ANALYST
    finally:
        s.close()
    assert _rows(fresh, user.id) == []
    _assert_one_type_only_error(caplog)

    assert _me(client).status_code == 200
    assert _rows(fresh, user.id) == []


# --- system actors --------------------------------------------------------------


def test_the_reserved_actors_are_exactly_five():
    # The fifth, system:sso-sync, is written by the modules' SSO sign-in.
    assert SYSTEM_ACTOR_EMAILS == {
        EXPERIMENT_SCHEDULER,
        ROLLOUT_SCHEDULER,
        SAFETY_MONITOR,
        COGNITO_SYNC,
        "system:sso-sync",
    }


def test_a_deleted_users_entries_are_never_automatic(fresh, db_session):
    """Deleting a user sets ``user_id`` to NULL on their entries and keeps
    ``user_email``; such an entry is not taken for an automatic one."""
    user = _make_user(db_session, UserRole.DEVELOPER)
    email, user_id = user.email, user.id
    flag_id = _flag(fresh)
    s = fresh()
    try:
        AuditService.record_after_commit(
            s,
            actor=s.get(User, user_id),
            action="feature_flag_update",
            entity_type="feature_flag",
            entity_id=flag_id,
            entity_name=f"{P} flag",
        )
    finally:
        s.close()
    s = fresh()
    try:
        s.delete(s.get(User, user_id))
        s.commit()
    finally:
        s.close()
    rows = _rows(fresh, flag_id)
    assert len(rows) == 1
    assert rows[0].user_id is None
    assert rows[0].user_email == email
    assert not is_system_actor(rows[0].user_id, rows[0].user_email)
    # A reserved email with a user id is not automatic either.
    assert not is_system_actor(user_id, SAFETY_MONITOR)
    assert is_system_actor(None, SAFETY_MONITOR)
