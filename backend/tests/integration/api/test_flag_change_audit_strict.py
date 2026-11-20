"""A feature flag change is saved with its audit entries or not at all (#1042).

The routes that change a flag -- create (and its hidden twin without the
slash), update, delete, activate, deactivate, unarchive, toggle, enable,
disable and bulk toggle -- write their ``audit_logs`` entry, and for create,
update and delete their ``audit_events_v2`` event, in the transaction that
saves the change. These tests drive the real routes on Postgres as a
DEVELOPER who is not a superuser, make the database refuse every insert into
an audit table with a trigger, and read the result back in a session of their
own:

* the route answers 500 with its fixed sentence and the request ID, and
  nothing is saved: not the change, not an entry, not an event;
* nothing is counted as a lost entry: ``audit_write_failures_total`` does not
  move and the audit writer logs no ERROR line. The ERROR lines are exactly
  the route's (with the service's for create and update), and none carries a
  value from the refused row, which the trigger's message holds;
* once the trigger is gone, the identical request is saved, with exactly one
  entry;
* a failure of the change itself keeps its own error type, waiting for a row
  another change holds ends the request the same way, and a bulk toggle whose
  commit fails answers 500 (it answered 200, reporting every flag as changed).

A rollout schedule change still keeps its change when its entry cannot be
written (``test_rollout_change_keeps_the_change_when_the_audit_write_fails``).
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event as sa_event
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps

# The function the local login route signs its tokens with.
from backend.app.api.v1.endpoints.auth import create_local_access_token
from backend.app.core.config import settings
from backend.app.core.metrics import audit_write_failures_total
from backend.app.main import app
from backend.app.models.audit_log import AuditLog
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.rollout_schedule import RolloutSchedule
from backend.app.models.user import User, UserRole
from backend.app.services.audit_log_service import AuditLogService

pytestmark = [pytest.mark.integration, pytest.mark.requires_db, pytest.mark.regression]

PREFIX = "fa1042"
SCHEMA = "test_experimentation"
V1 = "/api/v1"
FLAGS = f"{V1}/feature-flags"

AUDIT_LOGGER = "backend.app.services.audit_service"
ROUTE_LOGGER = "backend.app.api.v1.endpoints.feature_flags"
BULK_LOGGER = "backend.app.api.v1.endpoints.bulk_toggle"
SERVICE_LOGGER = "backend.app.services.feature_flag_service"
#: The trigger's message; it is followed by the refused row.
REFUSED = "fa1042 refused"

A = FeatureFlagStatus.ACTIVE
I = FeatureFlagStatus.INACTIVE
ARCHIVED = FeatureFlagStatus.ARCHIVED


@dataclass(frozen=True)
class Case:
    """One route: the flag it starts from (None: it creates one), the status
    it leaves the flag in when saved (None: the flag is gone), the sentence of
    its 500, its ERROR lines, and whether it writes a compliance event."""

    start: Optional[FeatureFlagStatus]
    end: Optional[FeatureFlagStatus]
    sentence: str
    errors: tuple
    events: int = 0
    ok: int = 200


def _route_line(verb):
    return (ROUTE_LOGGER, f"Feature flag {verb} failed (AuditWriteFailed)")


def _service_line(doing, action):
    return (
        SERVICE_LOGGER,
        f"Error {doing} feature flag: audit entry for {action} on feature_flag "
        "<id> could not be written (InternalError)",
    )


CREATE = Case(
    None,
    I,
    "Could not create the feature flag",
    (_service_line("creating", "feature_flag_create"), _route_line("create")),
    events=1,
    ok=201,
)
CASES = {
    "create": CREATE,
    "create-twin": CREATE,
    "update": Case(
        I,
        I,
        "Could not update the feature flag",
        (_service_line("updating", "feature_flag_update"), _route_line("update")),
        events=1,
    ),
    "delete": Case(
        I,
        None,
        "Could not delete the feature flag",
        (_route_line("delete"),),
        events=1,
        ok=204,
    ),
    "activate": Case(
        I, A, "Could not activate the feature flag", (_route_line("activate"),)
    ),
    "deactivate": Case(
        A, I, "Could not deactivate the feature flag", (_route_line("deactivate"),)
    ),
    "unarchive": Case(
        ARCHIVED, I, "Could not unarchive the feature flag", (_route_line("unarchive"),)
    ),
    "toggle": Case(I, A, "Could not toggle the feature flag", (_route_line("toggle"),)),
    "enable": Case(I, A, "Could not enable the feature flag", (_route_line("enable"),)),
    "disable": Case(
        A, I, "Could not disable the feature flag", (_route_line("disable"),)
    ),
    "bulk": Case(
        I,
        A,
        "Could not change the feature flags",
        ((BULK_LOGGER, "Bulk toggle failed (AuditWriteFailed)"),),
    ),
}
#: The routes that write a compliance event as well.
WITH_EVENTS = [name for name, case in CASES.items() if case.events]


# --- fixtures -----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _local_auth(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "CACHE_ENABLED", False)


@pytest.fixture
def developer(db_session):
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"{PREFIX}_developer_{suffix}",
        email=f"{PREFIX}_{suffix}@audit.test",
        full_name="Strict Audit User",
        hashed_password="unused: this user signs in by token only",
        is_active=True,
        is_superuser=False,
        role=UserRole.DEVELOPER,
    )
    db_session.add(user)
    db_session.commit()
    return user


@pytest.fixture
def fresh(test_db):
    """A session of the test's own, opened per read, never the request's."""
    factory = sessionmaker(bind=test_db, expire_on_commit=False)

    def open_session():
        session = factory()
        session.execute(text(f"SET search_path TO {SCHEMA}"))
        return session

    return open_session


@pytest.fixture
def after_request():
    """One entry per request: the error a statement on the request's session
    raised once the route had returned, or None if it ran."""
    return []


@pytest.fixture
def client(test_db, after_request):
    factory = sessionmaker(bind=test_db, autocommit=False, autoflush=False)

    def override_get_db():
        session = factory()
        session.execute(text(f"SET search_path TO {SCHEMA}"))
        try:
            yield session
        finally:
            try:
                session.execute(text("SELECT 1"))
                after_request.append(None)
            except Exception as exc:
                after_request.append(exc)
            session.close()

    app.dependency_overrides[deps.get_db] = override_get_db
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
    finally:
        app.dependency_overrides.pop(deps.get_db, None)


class Refusals:
    """Triggers that make the database refuse a write; all dropped at the end.

    The trigger's message carries the refused row, as a NOT NULL violation's
    ``Failing row contains (...)`` does, so a log line or traceback that kept
    the driver's text would show the row's values.
    """

    FUNCTION = f"{SCHEMA}.fa1042_refuse"

    def __init__(self, session):
        self.session = session
        self.made = []

    def _create(self, table, name, statement):
        self.session.execute(
            text(
                f"CREATE OR REPLACE FUNCTION {self.FUNCTION}() RETURNS trigger "
                "LANGUAGE plpgsql AS "
                f"$$ BEGIN RAISE EXCEPTION '{REFUSED} %', NEW; END $$"
            )
        )
        self.session.execute(text(statement))
        self.session.commit()
        self.made.append((table, name))

    def inserts_into(self, table):
        """Every insert into ``table`` raises."""
        name = f"fa1042_refuse_{table}"
        self._create(
            table,
            name,
            f"CREATE TRIGGER {name} BEFORE INSERT ON {SCHEMA}.{table} "
            f"FOR EACH ROW EXECUTE FUNCTION {self.FUNCTION}()",
        )

    def flag_updates(self):
        """Every update of a flag raises, at the statement."""
        name = "fa1042_refuse_flag_update"
        self._create(
            "feature_flags",
            name,
            f"CREATE TRIGGER {name} BEFORE UPDATE ON {SCHEMA}.feature_flags "
            f"FOR EACH ROW EXECUTE FUNCTION {self.FUNCTION}()",
        )

    def flag_commits(self):
        """A transaction that updated a flag raises at its commit."""
        name = "fa1042_refuse_flag_commit"
        self._create(
            "feature_flags",
            name,
            f"CREATE CONSTRAINT TRIGGER {name} AFTER UPDATE ON "
            f"{SCHEMA}.feature_flags DEFERRABLE INITIALLY DEFERRED "
            f"FOR EACH ROW EXECUTE FUNCTION {self.FUNCTION}()",
        )

    def lift(self):
        self.session.rollback()
        for table, name in self.made:
            self.session.execute(
                text(f"DROP TRIGGER IF EXISTS {name} ON {SCHEMA}.{table}")
            )
        self.made.clear()
        self.session.execute(text(f"DROP FUNCTION IF EXISTS {self.FUNCTION}()"))
        self.session.commit()


@pytest.fixture
def refuse(db_session):
    refusals = Refusals(db_session)
    try:
        yield refusals
    finally:
        refusals.lift()


@pytest.fixture(autouse=True)
def _cleanup(db_session):
    """Leave nothing behind: other suites list flags and count entries."""
    yield
    s = db_session
    s.rollback()
    flag_ids = [
        r.id
        for r in s.query(FeatureFlag.id).filter(FeatureFlag.key.like(f"{PREFIX}-%"))
    ]
    for schedule in s.query(RolloutSchedule).filter(
        RolloutSchedule.feature_flag_id.in_(flag_ids)
    ):
        s.delete(schedule)
    s.commit()
    user_ids = [
        r.id for r in s.query(User.id).filter(User.username.like(f"{PREFIX}_%"))
    ]
    s.query(AuditLog).filter(
        AuditLog.entity_id.in_(flag_ids) | AuditLog.user_id.in_(user_ids)
    ).delete(synchronize_session=False)
    s.query(FeatureFlag).filter(FeatureFlag.id.in_(flag_ids)).delete(
        synchronize_session=False
    )
    s.query(User).filter(User.id.in_(user_ids)).delete(synchronize_session=False)
    s.commit()


# --- helpers ------------------------------------------------------------------


def _headers(user, request_id):
    return {
        "Authorization": f"Bearer {create_local_access_token(user)}",
        "X-Request-ID": request_id,
    }


def _key():
    return f"{PREFIX}-{uuid.uuid4().hex[:10]}"


def _flag(db_session, status, name, owner=None):
    flag = FeatureFlag(
        key=_key(),
        name=name,
        status=status,
        rollout_percentage=0,
        owner_id=owner.id if owner else None,
    )
    db_session.add(flag)
    db_session.commit()
    return flag


def _send(client, user, name, key, flag_id, canary, request_id):
    """The request of route ``name``, against the flag ``flag_id``."""
    h = _headers(user, request_id)
    reason = {"reason": canary}
    if name == "create":
        body = {"key": key, "name": f"{PREFIX} {canary}"}
        return client.post(f"{FLAGS}/", json=body, headers=h)
    if name == "create-twin":
        body = {"key": key, "name": f"{PREFIX} {canary}"}
        return client.post(FLAGS, json=body, headers=h)
    if name == "update":
        body = {"name": f"{PREFIX} renamed {canary}"}
        return client.put(f"{FLAGS}/{flag_id}", json=body, headers=h)
    if name == "delete":
        return client.delete(f"{FLAGS}/{flag_id}", headers=h)
    if name in ("activate", "deactivate", "unarchive"):
        return client.post(f"{FLAGS}/{flag_id}/{name}", headers=h)
    if name in ("toggle", "enable", "disable"):
        return client.post(f"{FLAGS}/{flag_id}/{name}", json=reason, headers=h)
    body = {"flag_ids": [str(flag_id)], "action": "enable", **reason}
    return client.post(f"{FLAGS}/bulk-toggle", json=body, headers=h)


def _stored(fresh, key):
    """(status, name) of the flag with this key, or None when there is none."""
    session = fresh()
    try:
        row = session.query(FeatureFlag).filter(FeatureFlag.key == key).one_or_none()
        return None if row is None else (FeatureFlagStatus(row.status), row.name)
    finally:
        session.close()


class Counts:
    """Rows in the two audit tables, now and later."""

    def __init__(self, fresh):
        self.fresh = fresh
        self.before = self._now()
        self.failures = audit_write_failures_total._value.get()

    def _now(self):
        session = self.fresh()
        try:
            return tuple(
                session.execute(text(f"SELECT count(*) FROM {SCHEMA}.{table}")).scalar()
                for table in ("audit_logs", "audit_events_v2")
            )
        finally:
            session.close()

    def delta(self):
        """(new audit_logs rows, new audit_events_v2 rows, failures counted)."""
        now = self._now()
        return (
            now[0] - self.before[0],
            now[1] - self.before[1],
            audit_write_failures_total._value.get() - self.failures,
        )


def _entries(fresh, flag_id):
    session = fresh()
    try:
        return session.query(AuditLog).filter(AuditLog.entity_id == flag_id).all()
    finally:
        session.close()


_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _error_lines(caplog):
    """Every ERROR record as (logger, message), each UUID written as <id>."""
    return tuple(
        (r.name, _UUID.sub("<id>", r.getMessage()))
        for r in caplog.records
        if r.levelno >= logging.ERROR
    )


def _traceback(record):
    if not record.exc_info:
        return record.exc_text or ""
    return logging.Formatter().formatException(record.exc_info)


def _assert_no_values_logged(caplog, *values):
    """No record's message or traceback carries any of ``values``.

    A route's ERROR line carries the traceback of ``AuditWriteFailed``
    (``unexpected_failure`` logs it with ``exc_info``); that is allowed, its
    text is not: the error is raised from None, so the driver's error, which
    holds the row, is in no traceback. The audit writer's lines carry none.
    """
    for record in caplog.records:
        logged = record.getMessage() + _traceback(record)
        for value in values:
            assert value not in logged, (record.name, record.getMessage())
        if record.name == AUDIT_LOGGER and record.levelno >= logging.ERROR:
            assert record.exc_info is None, record.getMessage()


def _assert_refused(response, case, request_id):
    assert response.status_code == 500, response.text
    assert response.json() == {
        "detail": f"{case.sentence} (request ID: {request_id})."
    }, response.text


def _setup(db_session, name, case, canary, owner):
    """(key, flag id) of the flag the route acts on; for create, a fresh key."""
    if case.start is None:
        return _key(), None
    flag = _flag(db_session, case.start, f"{PREFIX} {canary}", owner=owner)
    return flag.key, flag.id


def _unchanged(case, canary):
    return None if case.start is None else (case.start, f"{PREFIX} {canary}")


def _saved(name, case, canary):
    if case.end is None:
        return None
    if name == "update":
        return (case.end, f"{PREFIX} renamed {canary}")
    return (case.end, f"{PREFIX} {canary}")


def _retry_is_saved_once(client, fresh, developer, name, case, key, flag_id, canary):
    """The identical request, once the refusal is lifted: saved, one entry."""
    counts = Counts(fresh)
    response = _send(client, developer, name, key, flag_id, canary, "fa1042-again")
    assert response.status_code == case.ok, response.text
    assert counts.delta() == (1, case.events, 0)
    if flag_id is None:
        flag_id = response.json()["id"]
    entries = _entries(fresh, flag_id)
    assert len(entries) == 1, [(e.action_type, e.reason) for e in entries]
    assert entries[0].user_id == developer.id
    if name == "bulk":
        assert response.json()["audit_log_ids"] == [str(entries[0].id)]
    if name in ("toggle", "enable", "disable"):
        assert response.json()["audit_log_id"] == str(entries[0].id)
    assert _stored(fresh, key) == _saved(name, case, canary)


# --- the change and its audit_logs entry are saved together, or neither --------


@pytest.mark.parametrize("name", list(CASES))
def test_a_refused_entry_saves_nothing_and_answers_500(
    client, fresh, db_session, developer, refuse, after_request, caplog, name
):
    case = CASES[name]
    canary = f"fa1042-canary-{uuid.uuid4().hex}"
    key, flag_id = _setup(db_session, name, case, canary, developer)
    refuse.inserts_into("audit_logs")
    counts = Counts(fresh)
    caplog.set_level(logging.INFO)

    response = _send(client, developer, name, key, flag_id, canary, f"fa1042-{name}")

    _assert_refused(response, case, f"fa1042-{name}")
    assert _stored(fresh, key) == _unchanged(case, canary)
    # No entry, no compliance event, and nothing counted as a lost entry.
    assert counts.delta() == (0, 0, 0)
    assert _error_lines(caplog) == case.errors
    _assert_no_values_logged(caplog, canary, REFUSED, developer.email)
    assert after_request[-1] is None, type(after_request[-1]).__name__

    refuse.lift()
    caplog.clear()
    _retry_is_saved_once(client, fresh, developer, name, case, key, flag_id, canary)


@pytest.mark.parametrize("name", WITH_EVENTS)
def test_a_refused_compliance_event_saves_nothing_and_answers_500(
    client, fresh, db_session, developer, refuse, caplog, name
):
    case = CASES[name]
    canary = f"fa1042-canary-{uuid.uuid4().hex}"
    key, flag_id = _setup(db_session, name, case, canary, developer)
    refuse.inserts_into("audit_events_v2")
    counts = Counts(fresh)
    caplog.set_level(logging.INFO)

    response = _send(client, developer, name, key, flag_id, canary, f"fa1042-{name}")

    _assert_refused(response, case, f"fa1042-{name}")
    assert _stored(fresh, key) == _unchanged(case, canary)
    # The audit_logs entry is not saved without the event either.
    assert counts.delta() == (0, 0, 0)
    assert _error_lines(caplog) == case.errors
    _assert_no_values_logged(caplog, canary, REFUSED, developer.email)

    refuse.lift()
    _retry_is_saved_once(client, fresh, developer, name, case, key, flag_id, canary)


def test_a_failure_inside_the_audit_write_is_never_a_400(
    client, fresh, developer, monkeypatch, caplog
):
    """A ``ValueError`` while the entries are written is a refused write (500
    with the fixed sentence), not a refusal of the request: create answers a
    ``ValueError`` from the change itself with 400 and its text."""
    canary = f"fa1042-canary-{uuid.uuid4().hex}"

    def log(self, **kwargs):
        raise ValueError(canary)

    monkeypatch.setattr(AuditLogService, "log", log)
    key = _key()
    caplog.set_level(logging.INFO)

    response = _send(client, developer, "create", key, None, canary, "fa1042-value")

    _assert_refused(response, CREATE, "fa1042-value")
    assert canary not in response.text
    assert _stored(fresh, key) is None
    _assert_no_values_logged(caplog, canary)


# --- what is not an audit failure ----------------------------------------------


#: route -> its ERROR lines when the flag's own UPDATE is refused.
OWN_FAILURE = {
    "toggle": ((ROUTE_LOGGER, "Feature flag toggle failed (InternalError)"),),
    "enable": ((ROUTE_LOGGER, "Feature flag enable failed (InternalError)"),),
    "disable": ((ROUTE_LOGGER, "Feature flag disable failed (InternalError)"),),
    "unarchive": ((ROUTE_LOGGER, "Feature flag unarchive failed (InternalError)"),),
    "bulk": (
        (BULK_LOGGER, "Bulk toggle of flag <id> failed (InternalError)"),
        (BULK_LOGGER, "Bulk toggle failed (PendingRollbackError)"),
    ),
}


@pytest.mark.parametrize("name", list(OWN_FAILURE))
def test_a_refused_flag_write_keeps_its_own_error_type(
    client, fresh, db_session, developer, refuse, caplog, name
):
    """The change is flushed before its entries are written, so a failure of
    the change itself is logged under its own type, never as AuditWriteFailed.
    A bulk toggle whose flag cannot be written saves none (the transaction is
    over) and answers 500; it used to answer 200."""
    case = CASES[name]
    canary = f"fa1042-canary-{uuid.uuid4().hex}"
    key, flag_id = _setup(db_session, name, case, canary, developer)
    refuse.flag_updates()
    counts = Counts(fresh)
    caplog.set_level(logging.INFO)

    response = _send(client, developer, name, key, flag_id, canary, f"fa1042-{name}")

    _assert_refused(response, case, f"fa1042-{name}")
    assert _stored(fresh, key) == _unchanged(case, canary)
    assert counts.delta() == (0, 0, 0)
    assert _error_lines(caplog) == OWN_FAILURE[name]


def _lock_timeout(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("SET lock_timeout = '200ms'")
    cursor.close()


def test_waiting_for_a_row_another_change_holds_saves_nothing(
    client, fresh, db_session, developer, test_db, caplog
):
    """The entry's foreign key waits on the actor's row. Another transaction
    holds that row, and the database ends the wait (here at a lock timeout;
    in a deadlock, its detector): 500, nothing saved.

    The timeout is set on every connection the engine opens while the lock is
    held, so a request that waits on it ends instead of hanging the test."""
    canary = f"fa1042-canary-{uuid.uuid4().hex}"
    flag = _flag(db_session, I, f"{PREFIX} {canary}", owner=developer)
    holder = test_db.connect()
    transaction = holder.begin()
    caplog.set_level(logging.INFO)
    sa_event.listen(test_db, "connect", _lock_timeout)
    try:
        holder.execute(
            text(f"SELECT id FROM {SCHEMA}.users WHERE id = :id FOR UPDATE"),
            {"id": developer.id},
        )
        response = _send(
            client, developer, "toggle", flag.key, flag.id, canary, "fa1042-lock"
        )
    finally:
        sa_event.remove(test_db, "connect", _lock_timeout)
        transaction.rollback()
        holder.close()

    _assert_refused(response, CASES["toggle"], "fa1042-lock")
    assert _stored(fresh, flag.key) == (I, f"{PREFIX} {canary}")
    assert _entries(fresh, flag.id) == []
    assert _error_lines(caplog) == CASES["toggle"].errors


@pytest.mark.regression
def test_bulk_toggle_answers_500_when_its_commit_fails(
    client, fresh, db_session, developer, refuse, caplog
):
    """The commit failing after every flag was changed answered 200, with each
    flag reported as changed and nothing saved."""
    canary = f"fa1042-canary-{uuid.uuid4().hex}"
    flag = _flag(db_session, I, f"{PREFIX} {canary}", owner=developer)
    refuse.flag_commits()
    counts = Counts(fresh)
    caplog.set_level(logging.INFO)

    response = _send(
        client, developer, "bulk", flag.key, flag.id, canary, "fa1042-commit"
    )

    _assert_refused(response, CASES["bulk"], "fa1042-commit")
    assert _stored(fresh, flag.key) == (I, f"{PREFIX} {canary}")
    assert counts.delta() == (0, 0, 0)
    assert _error_lines(caplog) == (
        (BULK_LOGGER, "Bulk toggle failed (InternalError)"),
    )


# --- rollout schedule changes keep their change (today) ------------------------


def test_rollout_change_keeps_the_change_when_the_audit_write_fails(
    client, fresh, db_session, developer, refuse
):
    """A manual stage advance changes the flag's rollout percentage; with its
    entry refused, the advance is kept, there is no entry, and the failure is
    counted as a lost entry."""
    flag = _flag(db_session, A, f"{PREFIX} rollout", owner=developer)
    h = _headers(developer, "fa1042-rollout")
    soon = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    later = (datetime.now(timezone.utc) + timedelta(hours=72)).isoformat()
    created = client.post(
        f"{V1}/rollout-schedules/",
        json={
            "name": f"{PREFIX} schedule",
            "feature_flag_id": str(flag.id),
            "start_date": soon,
            "end_date": later,
            "max_percentage": 100,
            "min_stage_duration": 1,
            "stages": [
                {
                    "name": "One",
                    "stage_order": 1,
                    "target_percentage": 25,
                    "trigger_type": "manual",
                }
            ],
        },
        headers=h,
    )
    assert created.status_code == 201, created.text
    schedule = created.json()
    activated = client.post(
        f"{V1}/rollout-schedules/{schedule['id']}/activate", headers=h
    )
    assert activated.status_code == 200, activated.text
    stage_id = schedule["stages"][0]["id"]
    refuse.inserts_into("audit_logs")
    before = len(_entries(fresh, flag.id))
    counts = Counts(fresh)

    response = client.post(
        f"{V1}/rollout-schedules/stages/{stage_id}/advance", headers=h
    )

    assert response.status_code == 200, response.text
    session = fresh()
    try:
        percentage = (
            session.query(FeatureFlag.rollout_percentage)
            .filter(FeatureFlag.id == flag.id)
            .scalar()
        )
    finally:
        session.close()
    assert percentage == 25
    assert len(_entries(fresh, flag.id)) == before
    assert counts.delta() == (0, 0, 1)
