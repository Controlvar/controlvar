"""A scheduled draft that cannot start stays a draft (#1454, PR D).

Before the fix the experiment scheduler activated every DRAFT whose
``start_date`` had passed, without the check ``POST /start`` makes
(``ExperimentService._validate_experiment_for_start``: at least two variants,
a control, at least one metric). A draft with one variant, or with its metrics
removed by a ``PUT`` after it was scheduled, went ACTIVE at the next tick.

Now the scheduler runs that same check before a draft's scheduled first start:

* D1  a one-variant draft stays DRAFT with its ``start_date``; no notification,
      no audit row, and the scheduler logs ONE warning naming it, per tick. Once
      it has two variants, the next tick starts it;
* D2  a draft whose metrics were removed after scheduling stays DRAFT;
* D3  a valid draft due in the same tick as an invalid one still starts, and
      the invalid one, in a tick that commits, gets no audit row either;
* D4  a PAUSED experiment's scheduled resume is unchanged: it is not checked,
      so one with no metrics (older data) still resumes;
* D5  a skipped draft is not counted in the tick's ``items_failed``;
* D6  a check that raises keeps today's handling: that row's savepoint is rolled
      back, it is counted as failed and logged, and the tick goes on.

The fixtures follow ``test_scheduler_pause_semantics.py``: real users and
tokens, ``deps.get_db`` the only override, and the scheduler run against the
same database with its ``SessionLocal`` patched. The database is shared, so the
tick's counts are only compared with a tick run just before (a delta), never
asserted outright; what it did to each experiment is read from the row, the
notifications it sent, the audit log and its own log lines.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.api.v1.endpoints.auth import create_local_access_token
from backend.app.core.config import settings
from backend.app.main import app
from backend.app.models.audit_log import ActionType, AuditLog
from backend.app.models.experiment import Experiment, ExperimentStatus, Metric
from backend.app.models.user import User, UserRole
from backend.app.services.experiment_service import ExperimentService
from backend.app.services.notification_service import NotificationService

pytestmark = [pytest.mark.integration, pytest.mark.regression]

BASE = "/api/v1/experiments"
SCHEDULER_LOGGER = "backend.app.core.scheduler"
#: The scheduler's warning for a draft it does not start, with the id filled in.
NOT_READY = (
    "Scheduled start skipped for experiment {id}: it is not ready to start (it "
    "needs at least two variants, a control and a metric), so it stays a draft"
)


# --- fixtures -----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _local_auth(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "CACHE_ENABLED", False)


def _factory(db_session, **kwargs):
    engine = db_session.get_bind()
    factory = sessionmaker(bind=engine, **kwargs)

    def session():
        s = factory()
        s.execute(text("SET search_path TO test_experimentation"))
        return s

    return session


@pytest.fixture
def developer(db_session):
    """A DEVELOPER, not a superuser: every request here is one a DEVELOPER may make."""
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"draftcheck_{suffix}",
        email=f"draftcheck_{suffix}@draft.test",
        full_name="Draft Check User",
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
    session = _factory(db_session, autocommit=False, autoflush=False)

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


# --- helpers ------------------------------------------------------------------


def _auth(user):
    return {"Authorization": f"Bearer {create_local_access_token(user)}"}


def _now():
    return datetime.now(timezone.utc)


def _utc(value):
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _ok(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


TWO_VARIANTS = [
    {"name": "Control", "is_control": True, "traffic_allocation": 50},
    {"name": "Treatment", "is_control": False, "traffic_allocation": 50},
]
ONE_VARIANT = [{"name": "Control", "is_control": True, "traffic_allocation": 100}]
ONE_METRIC = [
    {
        "name": "Conversion Rate",
        "event_name": "purchase",
        "metric_type": "conversion",
        "is_primary": True,
    }
]


def _create(client, user, variants=TWO_VARIANTS):
    body = {
        "name": f"draftcheck {uuid.uuid4().hex[:8]}",
        "experiment_type": "a_b",
        "variants": variants,
        "metrics": ONE_METRIC,
    }
    return _ok(client.post(f"{BASE}/", json=body, headers=_auth(user)), 201)["id"]


def _schedule_due(client, user, experiment_id):
    """Schedule a start one minute ago (the schedule route accepts up to ten)."""
    start = _now() - timedelta(minutes=1)
    _ok(
        client.put(
            f"{BASE}/{experiment_id}/schedule",
            json={"start_date": start.isoformat()},
            headers=_auth(user),
        )
    )
    return start


def _row(db_session, experiment_id):
    session = _factory(db_session)()
    try:
        row = session.get(Experiment, uuid.UUID(experiment_id))
        return row.status, _utc(row.start_date)
    finally:
        session.close()


def _starts_audited(db_session, experiment_id):
    session = _factory(db_session)()
    try:
        return (
            session.query(AuditLog)
            .filter(
                AuditLog.entity_id == uuid.UUID(experiment_id),
                AuditLog.action_type == ActionType.EXPERIMENT_START.value,
            )
            .count()
        )
    finally:
        session.close()


def _not_ready_warnings(caplog, experiment_id):
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == SCHEDULER_LOGGER
        and r.levelno == logging.WARNING
        and experiment_id in r.getMessage()
    ]


def _tick(db_session):
    """One scheduler tick against the test database. Returns the
    notifications it sent, as {experiment_id: message}."""
    return _tick_with_counts(db_session)[0]


def _tick_with_counts(db_session):
    """One tick: (the notifications it sent, the counts it returned)."""
    from backend.app.core.scheduler import ExperimentScheduler

    sent = {}
    notifications = NotificationService()
    notifications._slack = MagicMock()
    notifications._email = MagicMock()

    def capture(event_):
        assert event_.event_type == "experiment_started"
        sent[event_.experiment_id] = event_.message
        return True

    notifications._send = capture

    scheduler = ExperimentScheduler()
    scheduler._notification_service = notifications
    with patch("backend.app.core.scheduler.SessionLocal", _factory(db_session)):
        counts = asyncio.run(scheduler.process_scheduled_experiments())
    return sent, counts


# --- D1: one variant -----------------------------------------------------------


def test_d1_a_one_variant_draft_stays_a_draft_until_it_has_two(
    client, db_session, developer, caplog
):
    caplog.set_level(logging.WARNING, logger=SCHEDULER_LOGGER)
    experiment_id = _create(client, developer, variants=ONE_VARIANT)
    start = _schedule_due(client, developer, experiment_id)

    sent = _tick(db_session)

    assert _row(db_session, experiment_id) == (ExperimentStatus.DRAFT, start)
    assert experiment_id not in sent
    assert _starts_audited(db_session, experiment_id) == 0
    assert _not_ready_warnings(caplog, experiment_id) == [
        NOT_READY.format(id=experiment_id)
    ]

    # Each tick says it again, once.
    caplog.clear()
    _tick(db_session)
    assert _row(db_session, experiment_id) == (ExperimentStatus.DRAFT, start)
    assert len(_not_ready_warnings(caplog, experiment_id)) == 1

    # Fixed: the start date has passed, so the next tick starts it.
    _ok(
        client.put(
            f"{BASE}/{experiment_id}",
            json={"variants": TWO_VARIANTS, "metrics": ONE_METRIC},
            headers=_auth(developer),
        )
    )
    caplog.clear()
    sent = _tick(db_session)

    assert _row(db_session, experiment_id) == (ExperimentStatus.ACTIVE, start)
    assert experiment_id in sent
    assert _starts_audited(db_session, experiment_id) == 1
    assert _not_ready_warnings(caplog, experiment_id) == []


# --- D2: metrics removed after scheduling --------------------------------------


def test_d2_a_draft_whose_metrics_were_removed_after_scheduling_stays_a_draft(
    client, db_session, developer, caplog
):
    caplog.set_level(logging.WARNING, logger=SCHEDULER_LOGGER)
    experiment_id = _create(client, developer)
    start = _schedule_due(client, developer, experiment_id)
    # The update route accepts an empty list (it is not the start check).
    body = _ok(
        client.put(
            f"{BASE}/{experiment_id}", json={"metrics": []}, headers=_auth(developer)
        )
    )
    assert body["metrics"] == []

    sent = _tick(db_session)

    assert _row(db_session, experiment_id) == (ExperimentStatus.DRAFT, start)
    assert experiment_id not in sent
    assert _starts_audited(db_session, experiment_id) == 0
    assert _not_ready_warnings(caplog, experiment_id) == [
        NOT_READY.format(id=experiment_id)
    ]


# --- D3: a valid draft in the same tick ----------------------------------------


def test_d3_a_valid_draft_starts_in_the_same_tick_as_one_that_cannot(
    client, db_session, developer, caplog
):
    caplog.set_level(logging.WARNING, logger=SCHEDULER_LOGGER)
    invalid = _create(client, developer, variants=ONE_VARIANT)
    invalid_start = _schedule_due(client, developer, invalid)
    valid = _create(client, developer)
    valid_start = _schedule_due(client, developer, valid)

    sent = _tick(db_session)

    assert _row(db_session, valid) == (ExperimentStatus.ACTIVE, valid_start)
    assert sent[valid].endswith("has been started automatically.")
    assert _starts_audited(db_session, valid) == 1
    assert _not_ready_warnings(caplog, valid) == []
    assert _row(db_session, invalid) == (ExperimentStatus.DRAFT, invalid_start)
    assert invalid not in sent
    # This tick commits (the valid draft started), so a row written for the
    # skipped draft would be stored: none is.
    assert _starts_audited(db_session, invalid) == 0


# --- D4: a scheduled resume is not checked -------------------------------------


def test_d4_a_paused_experiment_with_a_due_resume_still_resumes(
    client, db_session, developer, caplog
):
    """Resuming is not starting: a PAUSED experiment is not checked, so one
    left with no metrics (as older data may be) resumes as it did before."""
    caplog.set_level(logging.WARNING, logger=SCHEDULER_LOGGER)
    experiment_id = _create(client, developer)
    _ok(client.post(f"{BASE}/{experiment_id}/start", headers=_auth(developer)))
    _ok(client.post(f"{BASE}/{experiment_id}/pause", headers=_auth(developer)))
    started = _row(db_session, experiment_id)[1]
    session = _factory(db_session)()
    try:
        session.execute(
            delete(Metric).where(Metric.experiment_id == uuid.UUID(experiment_id))
        )
        session.commit()
    finally:
        session.close()
    resume = _now() - timedelta(minutes=1)
    body = _ok(
        client.put(
            f"{BASE}/{experiment_id}/schedule",
            json={"start_date": resume.isoformat()},
            headers=_auth(developer),
        )
    )
    assert body["status"] == "paused"
    assert body["metrics"] == []

    sent = _tick(db_session)

    assert _row(db_session, experiment_id) == (ExperimentStatus.ACTIVE, started)
    assert sent[experiment_id].endswith("has been resumed automatically.")
    assert _not_ready_warnings(caplog, experiment_id) == []


# --- D5: a skipped draft is not a failure ---------------------------------------


def test_d5_a_skipped_draft_is_not_counted_as_failed(client, db_session, developer):
    """A draft nobody has fixed yet is not a failed item: a tick that skips it
    reports as many failures as the tick before it, when it was not due."""
    _, before = _tick_with_counts(db_session)
    experiment_id = _create(client, developer, variants=ONE_VARIANT)
    start = _schedule_due(client, developer, experiment_id)

    _, after = _tick_with_counts(db_session)

    assert _row(db_session, experiment_id) == (ExperimentStatus.DRAFT, start)
    assert after["items_failed"] == before["items_failed"]


# --- D6: a check that raises -----------------------------------------------------


def test_d6_a_check_that_raises_keeps_todays_handling(
    client, db_session, developer, caplog, monkeypatch
):
    """The check runs inside the row's own savepoint: when it raises, that row
    is rolled back, counted as failed and logged, and the rest of the tick
    still runs (the valid draft due with it starts)."""
    caplog.set_level(logging.WARNING, logger=SCHEDULER_LOGGER)
    raising = _create(client, developer)
    original = ExperimentService._validate_experiment_for_start

    def check(self, experiment):
        if str(experiment.id) == raising:
            raise RuntimeError("planted: the check itself failed")
        return original(self, experiment)

    monkeypatch.setattr(ExperimentService, "_validate_experiment_for_start", check)
    _, before = _tick_with_counts(db_session)
    raising_start = _schedule_due(client, developer, raising)
    valid = _create(client, developer)
    valid_start = _schedule_due(client, developer, valid)

    sent, after = _tick_with_counts(db_session)

    assert _row(db_session, valid) == (ExperimentStatus.ACTIVE, valid_start)
    assert sent[valid].endswith("has been started automatically.")
    assert _row(db_session, raising) == (ExperimentStatus.DRAFT, raising_start)
    assert raising not in sent
    assert _starts_audited(db_session, raising) == 0
    assert after["items_failed"] == before["items_failed"] + 1
    errors = [
        r.getMessage()
        for r in caplog.records
        if r.name == SCHEDULER_LOGGER
        and r.levelno == logging.ERROR
        and raising in r.getMessage()
    ]
    assert errors == [f"Error activating experiment {raising}"]
    assert _not_ready_warnings(caplog, raising) == []
