"""Scheduler notifications go out once, off the event loop, after their commit (#1690).

A notification is a synchronous HTTP call (webhook, Slack). Sent on the event
loop, a slow receiver stopped the API answering; the experiment and rollout
ticks now send each in a worker thread. Each is sent after the commit that
made the change it announces: an await before the commit would leave the
tick holding row locks while the loop serves requests, and a request writing
one of those rows would then block the loop's own thread.

The notification service here is a recorder. For each send it notes the
thread it ran on and the state the database holds at that moment, read in a
fresh session, so it sees only what has been committed.

The database is shared with other tests, so the tick's own counts are not
asserted; each test reads the sends for the rows it created.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text, update
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.api.v1.endpoints.auth import create_local_access_token
from backend.app.core.config import settings
from backend.app.main import app
from backend.app.models.experiment import Experiment, ExperimentStatus
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.rollout_schedule import (
    RolloutSchedule,
    RolloutScheduleStatus,
    RolloutStage,
    RolloutStageStatus,
    TriggerType,
)
from backend.app.models.user import User, UserRole

pytestmark = [pytest.mark.integration, pytest.mark.regression]

BASE = "/api/v1/experiments"


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
        s.execute(text("SET search_path TO test_experimentation"))
        return s

    return session


@pytest.fixture
def developer(db_session):
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"sends1690_{suffix}",
        email=f"sends1690_{suffix}@sends.test",
        full_name="Sends After Commit User",
        hashed_password="unused: this user signs in by token only",
        is_active=True,
        is_superuser=False,
        role=UserRole.DEVELOPER,
    )
    db_session.add(user)
    db_session.commit()
    return user


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


def _naive_utc(delta: timedelta) -> datetime:
    return (datetime.now(timezone.utc) + delta).replace(tzinfo=None)


def _create_experiment(client, developer) -> str:
    response = client.post(
        f"{BASE}/",
        json={
            "name": f"sends1690 {uuid.uuid4().hex[:8]}",
            "description": "Notifications go out after the commit",
            "hypothesis": "A send never precedes its commit",
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
        headers={"Authorization": f"Bearer {create_local_access_token(developer)}"},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _set_experiment(db_session, experiment_id, **values):
    """Write columns straight to the row (the starting state of the test)."""
    session = _factory(db_session)()
    try:
        session.execute(
            update(Experiment)
            .where(Experiment.id == uuid.UUID(experiment_id))
            .values(**values)
        )
        session.commit()
    finally:
        session.close()


def _read(db_session, model, row_id, column):
    """One column of one row, read in a fresh session: only what is committed."""
    session = _factory(db_session)()
    try:
        return getattr(session.get(model, row_id), column)
    finally:
        session.close()


def test_each_experiment_notification_is_sent_once_off_the_loop_after_its_commit(
    client, db_session, developer
):
    """One experiment due to start, one due to end, one tick."""
    to_start = _create_experiment(client, developer)
    _set_experiment(db_session, to_start, start_date=_naive_utc(-timedelta(minutes=5)))
    to_end = _create_experiment(client, developer)
    _set_experiment(
        db_session,
        to_end,
        status=ExperimentStatus.ACTIVE,
        start_date=_naive_utc(-timedelta(hours=2)),
        end_date=_naive_utc(-timedelta(hours=1)),
    )
    loop_thread = threading.get_ident()
    sends = []

    def record(kind, kwargs):
        experiment_id = kwargs["experiment_id"]
        status = _read(db_session, Experiment, uuid.UUID(experiment_id), "status")
        sends.append((kind, experiment_id, threading.get_ident(), status))
        return True

    class Recorder:
        def notify_experiment_started(self, **kwargs):
            return record("started", kwargs)

        def notify_experiment_ended(self, **kwargs):
            return record("ended", kwargs)

    from backend.app.core.scheduler import ExperimentScheduler

    scheduler = ExperimentScheduler()
    scheduler._notification_service = Recorder()
    with patch("backend.app.core.scheduler.SessionLocal", _factory(db_session)):
        asyncio.run(scheduler.process_scheduled_experiments())

    ours = [s for s in sends if s[1] in (to_start, to_end)]
    assert [(kind, eid) for kind, eid, _, _ in ours] == [
        ("started", to_start),
        ("ended", to_end),
    ], ours
    assert [eid for _, eid, thread, _ in ours if thread == loop_thread] == [], (
        "a notification was sent on the event loop's thread"
    )
    assert [(kind, status) for kind, _, _, status in ours] == [
        ("started", ExperimentStatus.ACTIVE),
        ("ended", ExperimentStatus.COMPLETED),
    ], "a notification was sent before the change it announces was committed"


def _rollout(db_session):
    """A flag at 0% with an ACTIVE schedule whose only stage (30%) may start
    now. Returns the flag id."""
    suffix = uuid.uuid4().hex[:10]
    session = _factory(db_session)()
    try:
        flag = FeatureFlag(
            key=f"sends1690-{suffix}",
            name=f"sends1690 {suffix}",
            status=FeatureFlagStatus.ACTIVE,
            rollout_percentage=0,
        )
        session.add(flag)
        session.flush()
        schedule = RolloutSchedule(
            name=f"sends1690 {suffix}",
            feature_flag_id=flag.id,
            status=RolloutScheduleStatus.ACTIVE,
        )
        session.add(schedule)
        session.flush()
        session.add(
            RolloutStage(
                rollout_schedule_id=schedule.id,
                name="stage 1",
                stage_order=1,
                target_percentage=30,
                status=RolloutStageStatus.PENDING,
                trigger_type=TriggerType.TIME_BASED,
                trigger_configuration={"duration": 24},
            )
        )
        session.commit()
        return flag.id
    finally:
        session.close()


def test_each_rollout_notification_is_sent_once_off_the_loop_after_its_commit(
    db_session,
):
    flag_id = _rollout(db_session)
    loop_thread = threading.get_ident()
    sends = []

    class Recorder:
        def notify_rollout_advanced(self, **kwargs):
            percentage = _read(
                db_session,
                FeatureFlag,
                uuid.UUID(kwargs["feature_flag_id"]),
                "rollout_percentage",
            )
            sends.append(
                (
                    kwargs["feature_flag_id"],
                    kwargs["new_percentage"],
                    threading.get_ident(),
                    percentage,
                )
            )
            return True

    from backend.app.core.rollout_scheduler import RolloutScheduler

    scheduler = RolloutScheduler(interval_minutes=1)
    scheduler._notification_service = Recorder()
    with patch("backend.app.core.rollout_scheduler.SessionLocal", _factory(db_session)):
        asyncio.run(scheduler.process_rollout_schedules())

    ours = [s for s in sends if s[0] == str(flag_id)]
    assert [(fid, new) for fid, new, _, _ in ours] == [(str(flag_id), 30)], ours
    assert [fid for fid, _, thread, _ in ours if thread == loop_thread] == [], (
        "a notification was sent on the event loop's thread"
    )
    assert [committed for _, _, _, committed in ours] == [30], (
        "a notification was sent before the change it announces was committed"
    )
