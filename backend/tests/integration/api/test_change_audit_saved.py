"""The audit record of an experiment or flag change is saved (#471).

``AuditLogService.log()`` only flushes, and ``get_db`` closes its session
without committing. Every route below writes its audit record after the
service has already committed the change, so unless the route commits again
the record is rolled back when the request ends -- while the change itself
stays. These tests read the record back in a session of their own, opened
after the request, which is the only place that loss is visible.

Each request is a real one: a DEVELOPER who is not a superuser, a local JWT
from ``create_local_access_token``, and no dependency override except
``deps.get_db`` (see #470 for why ``make_client_for_user`` is not used).

The second half breaks the audit write, once as a failed flush inside
``log()`` and once as a failed commit, and asserts that the answer and the
change are what they would have been, and that the request's session was
left usable afterwards.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps

# The function the local login route signs its tokens with.
from backend.app.api.v1.endpoints.auth import create_local_access_token
from backend.app.core.config import settings
from backend.app.main import app
from backend.app.models.compliance_audit_event import (
    AuditAction,
    ComplianceAuditEvent,
)
from backend.app.models.experiment import Experiment
from backend.app.models.feature_flag import FeatureFlag
from backend.app.models.user import User, UserRole
from backend.app.services.audit_log_service import AuditLogService

pytestmark = [pytest.mark.integration, pytest.mark.requires_db, pytest.mark.regression]

EXPERIMENTS = "/api/v1/experiments"
FLAGS = "/api/v1/feature-flags"
PREFIX = "au471"

#: route -> (resource type, audit action, status on success)
ROUTES = {
    "experiment_create": ("experiment", AuditAction.CREATE, 201),
    "experiment_update": ("experiment", AuditAction.UPDATE, 200),
    "experiment_delete": ("experiment", AuditAction.DELETE, 204),
    "flag_create": ("feature_flag", AuditAction.CREATE, 201),
    "flag_update": ("feature_flag", AuditAction.UPDATE, 200),
    "flag_delete": ("feature_flag", AuditAction.DELETE, 204),
}

#: How the audit write is made to fail: inside ``log()`` (its flush raises)
#: or at the route's commit (``log()`` returns, the commit raises).
FAILURES = ["flush", "commit"]


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
        full_name="Audit Record User",
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
        session.execute(text("SET search_path TO test_experimentation"))
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
        session.execute(text("SET search_path TO test_experimentation"))
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


@pytest.fixture(autouse=True)
def _remove_flags(db_session):
    """The flag list tests page at 100; leave no flags behind."""
    yield
    db_session.rollback()
    db_session.query(FeatureFlag).filter(FeatureFlag.key.like(f"{PREFIX}-%")).delete(
        synchronize_session=False
    )
    db_session.commit()


# --- helpers ------------------------------------------------------------------


def _auth(user):
    return {"Authorization": f"Bearer {create_local_access_token(user)}"}


def _experiment_payload():
    return {
        "name": f"{PREFIX} {uuid.uuid4().hex[:8]}",
        "description": "Audit record of an experiment change",
        "hypothesis": "The audit record outlives the request",
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
    }


def _flag_payload():
    return {"key": f"{PREFIX}-{uuid.uuid4().hex[:10]}", "name": f"{PREFIX} flag"}


def _create(client, user, resource):
    path, body = (
        (f"{EXPERIMENTS}/", _experiment_payload())
        if resource == "experiment"
        else (f"{FLAGS}/", _flag_payload())
    )
    response = client.post(path, json=body, headers=_auth(user))
    assert response.status_code == 201, response.text
    return response.json()["id"], body


def _setup(client, user, route):
    """The resource an update or delete acts on, created beforehand, and the
    body it was created with; (None, None) for a create route."""
    if route.endswith("_create"):
        return None, None
    return _create(client, user, ROUTES[route][0])


def _call(client, user, route, resource_id):
    """Make the request; return (response, id of the resource it changed,
    the body sent)."""
    headers = _auth(user)
    body = None
    if route == "experiment_create":
        body = _experiment_payload()
        response = client.post(f"{EXPERIMENTS}/", json=body, headers=headers)
    elif route == "flag_create":
        body = _flag_payload()
        response = client.post(f"{FLAGS}/", json=body, headers=headers)
    elif route == "experiment_update":
        body = {"name": "renamed"}
        response = client.put(
            f"{EXPERIMENTS}/{resource_id}", json=body, headers=headers
        )
    elif route == "flag_update":
        body = {"name": "renamed"}
        response = client.put(f"{FLAGS}/{resource_id}", json=body, headers=headers)
    elif route == "experiment_delete":
        response = client.delete(
            f"{EXPERIMENTS}/{resource_id}?experiment_key={resource_id}",
            headers=headers,
        )
    else:
        response = client.delete(f"{FLAGS}/{resource_id}", headers=headers)
    if resource_id is None and response.status_code == 201:
        resource_id = response.json()["id"]
    return response, resource_id, body


def _expected_values(route, created, sent, user):
    """(old_value, new_value) fields the record must carry, from the bodies
    the test sent. None means the record carries no value at all."""
    if route == "experiment_create":
        return None, {"name": sent["name"]}
    if route == "flag_create":
        return None, {"key": sent["key"], "name": sent["name"]}
    if route == "experiment_update":
        return {"name": created["name"]}, {"name": sent["name"]}
    if route == "flag_update":
        return (
            {"key": created["key"], "name": created["name"]},
            {"key": created["key"], "name": sent["name"]},
        )
    if route == "experiment_delete":
        return {"name": created["name"], "owner_id": str(user.id)}, None
    return {"key": created["key"], "name": created["name"]}, None


def _assert_values(route, which, stored, expected):
    if expected is None:
        assert stored is None, f"{route}: {which} should be empty, got {stored}"
        return
    assert stored is not None, f"{route}: {which} is empty, expected {expected}"
    carried = {k: stored.get(k) for k in expected}
    assert carried == expected, f"{route}: {which} {carried} != {expected}"


def _audit_rows(fresh, route, resource_id):
    resource_type, action, _ = ROUTES[route]
    session = fresh()
    try:
        return (
            session.query(ComplianceAuditEvent)
            .filter(
                ComplianceAuditEvent.resource_type == resource_type,
                ComplianceAuditEvent.resource_id == str(resource_id),
                ComplianceAuditEvent.action == action,
            )
            .all()
        )
    finally:
        session.close()


def _assert_change_kept(fresh, route, resource_id):
    model = Experiment if route.startswith("experiment") else FeatureFlag
    session = fresh()
    try:
        stored = session.get(model, uuid.UUID(str(resource_id)))
        if route.endswith("_delete"):
            assert stored is None, f"{route}: the resource is still there"
        else:
            assert stored is not None, f"{route}: the resource was not saved"
            if route.endswith("_update"):
                assert stored.name == "renamed", f"{route}: the change was lost"
    finally:
        session.close()


# --- the audit record is saved ------------------------------------------------


@pytest.mark.parametrize("route", list(ROUTES))
def test_audit_record_is_saved(client, fresh, developer, after_request, route):
    resource_id, created = _setup(client, developer, route)

    response, resource_id, sent = _call(client, developer, route, resource_id)

    assert response.status_code == ROUTES[route][2], response.text
    rows = _audit_rows(fresh, route, resource_id)
    assert len(rows) == 1, (
        f"{route}: {len(rows)} audit records of {ROUTES[route][1].value} "
        f"{resource_id} in a fresh session, expected 1"
    )
    assert rows[0].actor_id == developer.id
    old, new = _expected_values(route, created, sent, developer)
    _assert_values(route, "old_value", rows[0].old_value, old)
    _assert_values(route, "new_value", rows[0].new_value, new)
    _assert_change_kept(fresh, route, resource_id)
    assert after_request[-1] is None, type(after_request[-1]).__name__


# --- the update record carries what changed (#523) ---------------------------


def test_experiment_update_record_carries_targeting_and_status(
    client, fresh, developer
):
    """The update record used to carry only the name, so a change to who can
    join an experiment left no trace of what it was before or after."""
    us = {
        "logical_operator": "AND",
        "groups": [
            {
                "logical_operator": "AND",
                "conditions": [
                    {"attribute": "country", "operator": "equals", "value": "US"}
                ],
            }
        ],
    }
    gb = {
        "logical_operator": "OR",
        "groups": [
            {
                "id": "g-1",
                "logical_operator": "AND",
                "conditions": [
                    {
                        "id": "c-1",
                        "attribute": "country",
                        "operator": "in",
                        "value": ["GB", "IE"],
                    }
                ],
            }
        ],
    }
    body = {**_experiment_payload(), "targeting_rules": us}
    created = client.post(f"{EXPERIMENTS}/", json=body, headers=_auth(developer))
    assert created.status_code == 201, created.text
    experiment_id = created.json()["id"]

    response = client.put(
        f"{EXPERIMENTS}/{experiment_id}",
        json={"targeting_rules": gb},
        headers=_auth(developer),
    )

    assert response.status_code == 200, response.text
    rows = _audit_rows(fresh, "experiment_update", experiment_id)
    assert len(rows) == 1, rows
    old, new = rows[0].old_value, rows[0].new_value
    assert old["targeting_rules"] == us, old
    assert new["targeting_rules"] == gb, new
    assert old["status"] == "draft" and new["status"] == "draft", (old, new)
    assert old["name"] == new["name"] == body["name"]


# --- a failed audit write changes nothing else --------------------------------


def _failing_log(failure):
    """A ``log()`` whose record violates NOT NULL on resource_type."""

    def log(self, **kwargs):
        event = ComplianceAuditEvent(
            id=uuid.uuid4(),
            action=kwargs["action"],
            resource_type=None,
            resource_id=kwargs.get("resource_id"),
            outcome=kwargs["outcome"],
        )
        self._db.add(event)
        if failure == "flush":
            self._db.flush()
        return event

    return log


CASES = [(route, failure) for route in ROUTES for failure in FAILURES]


@pytest.mark.parametrize(
    ("route", "failure"), CASES, ids=[f"{r}-{f}" for r, f in CASES]
)
def test_failed_audit_write_keeps_the_answer_and_the_change(
    client, fresh, developer, after_request, monkeypatch, route, failure
):
    resource_id, _ = _setup(client, developer, route)
    monkeypatch.setattr(AuditLogService, "log", _failing_log(failure))

    response, resource_id, _ = _call(client, developer, route, resource_id)

    assert response.status_code == ROUTES[route][2], response.text
    _assert_change_kept(fresh, route, resource_id)
    assert _audit_rows(fresh, route, resource_id) == []
    assert after_request[-1] is None, (
        f"{route}: the request's session was left unusable after the audit "
        f"write failed: {type(after_request[-1]).__name__}"
    )


# --- the warning names the error's type, not its text --------------------------

MARKER = "marker-not-for-logs"


@pytest.mark.parametrize("route", list(ROUTES))
def test_failed_audit_write_logs_the_error_type_only(
    client, fresh, developer, monkeypatch, caplog, route
):
    def log(self, **kwargs):
        raise RuntimeError(MARKER)

    resource_id, _ = _setup(client, developer, route)
    monkeypatch.setattr(AuditLogService, "log", log)

    with caplog.at_level("WARNING"):
        response, _, _ = _call(client, developer, route, resource_id)

    assert response.status_code == ROUTES[route][2], response.text
    records = [
        r for r in caplog.records if "Compliance audit logging failed" in r.getMessage()
    ]
    assert len(records) == 1, [r.getMessage() for r in caplog.records]
    record = records[0]
    assert record.levelname == "WARNING"
    assert "RuntimeError" in record.getMessage()
    assert MARKER not in record.getMessage()
    assert record.exc_info is None and MARKER not in (record.exc_text or "")
