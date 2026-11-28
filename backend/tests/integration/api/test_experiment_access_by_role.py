"""Who may read and who may change an experiment, route by route (#452).

Every cell is a real request: real ``User`` rows, a real local JWT from
``create_local_access_token``, and no dependency override except
``deps.get_db`` -- so each request goes through ``get_current_user``,
``get_current_active_user`` and the endpoint's own checks, as a dashboard
session does. ``make_client_for_user`` is deliberately not used: it replaces
the three current-user dependencies, and with them the path under test.

The callers are a superuser; a non-superuser of each of the four roles, once
as the experiment's owner and once not; no token at all; and an ANALYST owner
whose role has had READ on experiments taken out of the role table. Every
shipped role holds READ, so that last column is the only one that can tell a
READ check apart from no check -- in particular a READ check that runs after a
cached answer has already been returned, which the cache-on run below exists
to catch.

Every cell gets its own experiment, created through the API and then given its
owner and status by SQL, so that a 400, 404 or 422 from the wrong state can
never stand in for the status under test.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import redis as sync_redis
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps

# The function the local login route signs its tokens with.
from backend.app.api.v1.endpoints.auth import create_local_access_token
from backend.app.core.config import settings
from backend.app.core.permissions import ROLE_PERMISSIONS, Action, ResourceType
from backend.app.core.stats_engine import ENGINE_VERSION
from backend.app.main import app
from backend.app.models.compliance_audit_event import (
    AuditAction,
    ComplianceAuditEvent,
)
from backend.app.models.experiment import Experiment, ExperimentStatus
from backend.app.models.user import User, UserRole

pytestmark = [pytest.mark.integration, pytest.mark.regression]

BASE = "/api/v1/experiments"
CACHE_DB = 15

# --- columns ------------------------------------------------------------------

#: (column, role, superuser, owns the experiment)
CALLERS = {
    "superuser": (UserRole.ADMIN, True, False),
    "admin_own": (UserRole.ADMIN, False, True),
    "admin_other": (UserRole.ADMIN, False, False),
    "developer_own": (UserRole.DEVELOPER, False, True),
    "developer_other": (UserRole.DEVELOPER, False, False),
    "analyst_own": (UserRole.ANALYST, False, True),
    "analyst_other": (UserRole.ANALYST, False, False),
    "viewer_own": (UserRole.VIEWER, False, True),
    "viewer_other": (UserRole.VIEWER, False, False),
    "analyst_own_without_read": (UserRole.ANALYST, False, True),
}
NO_TOKEN = "no_token"
COLUMNS = [*CALLERS, NO_TOKEN]

# --- rows ---------------------------------------------------------------------


def _schedule_body():
    start = datetime.now(timezone.utc) + timedelta(days=1)
    return {
        "start_date": start.isoformat(),
        "end_date": (start + timedelta(days=7)).isoformat(),
        "time_zone": "UTC",
    }


#: row -> (method, path after /experiments/{id}, body, status the cell starts in)
ROUTES = {
    "detail": ("GET", "", None, ExperimentStatus.DRAFT),
    "results": ("GET", "/results", None, ExperimentStatus.ACTIVE),
    "daily_results": ("GET", "/daily-results", None, ExperimentStatus.ACTIVE),
    "segmented_results": (
        "GET",
        "/segmented-results/country",
        None,
        ExperimentStatus.ACTIVE,
    ),
    "update": ("PUT", "", {"name": "renamed"}, ExperimentStatus.DRAFT),
    "start": ("POST", "/start", None, ExperimentStatus.DRAFT),
    "pause": ("POST", "/pause", None, ExperimentStatus.ACTIVE),
    "complete": ("POST", "/complete", None, ExperimentStatus.ACTIVE),
    "archive": ("POST", "/archive", None, ExperimentStatus.COMPLETED),
    # An ended experiment (ROW_DATES), so that the role check decides (#2278).
    "unarchive": ("POST", "/unarchive", None, ExperimentStatus.ARCHIVED),
    "metadata": ("POST", "/metadata", {"k": "v"}, ExperimentStatus.DRAFT),
    "clone": ("POST", "/clone", None, ExperimentStatus.ACTIVE),
    "schedule": ("PUT", "/schedule", _schedule_body, ExperimentStatus.DRAFT),
    "delete": ("DELETE", "?experiment_key={id}", None, ExperimentStatus.DRAFT),
    # Who may act is decided before the status rule, and the status rule still
    # holds for everyone the role check admits.
    "delete_active": ("DELETE", "?experiment_key={id}", None, ExperimentStatus.ACTIVE),
    "schedule_active": ("PUT", "/schedule", _schedule_body, ExperimentStatus.ACTIVE),
    "update_active": ("PUT", "", {"name": "renamed"}, ExperimentStatus.ACTIVE),
}


def _row(ok, *, admin_other=None, developer_other=None, analyst_viewer=403):
    """One row of expected statuses. ``ok`` is the success status; the
    ANALYST-without-READ column is 403 and no token is 401 in every row."""
    return {
        "superuser": ok,
        "admin_own": ok,
        "admin_other": ok if admin_other is None else admin_other,
        "developer_own": ok,
        "developer_other": ok if developer_other is None else developer_other,
        "analyst_own": analyst_viewer,
        "analyst_other": analyst_viewer,
        "viewer_own": analyst_viewer,
        "viewer_other": analyst_viewer,
        "analyst_own_without_read": 403,
        NO_TOKEN: 401,
    }


READ_ROW = _row(200, analyst_viewer=200)
CHANGE_ROW = _row(200)

EXPECTED = {
    "detail": READ_ROW,
    "results": READ_ROW,
    "daily_results": READ_ROW,
    "segmented_results": READ_ROW,
    "update": CHANGE_ROW,
    "start": CHANGE_ROW,
    "pause": CHANGE_ROW,
    "complete": CHANGE_ROW,
    "archive": CHANGE_ROW,
    "unarchive": CHANGE_ROW,
    "metadata": CHANGE_ROW,
    "clone": _row(201),
    "schedule": CHANGE_ROW,
    "delete": _row(204),
    # 400 (not a draft) for every caller the role check admits; 403 for the
    # ANALYST and VIEWER callers, whom the role check refuses before the state
    # is looked at. The detail is asserted in every signed-in cell
    # (DELETE_ACTIVE_DETAIL), so a refusal from the wrong check fails there.
    "delete_active": _row(400),
    "schedule_active": _row(400),
    # The same for an update outside DRAFT (#602), except that the superuser
    # passes the non-draft guard and renames it.
    "update_active": {**_row(400), "superuser": 200},
}

NOT_DRAFT = "Cannot delete experiments that are not in DRAFT status"
CANNOT_DELETE = "You don't have permission to delete experiments"
CANNOT_VIEW = "You don't have permission to view experiments"
CANNOT_UPDATE = "You don't have permission to update experiments"

#: delete_active: the refusal each signed-in caller gets.
DELETE_ACTIVE_DETAIL = {
    "superuser": NOT_DRAFT,
    "admin_own": NOT_DRAFT,
    "admin_other": NOT_DRAFT,
    "developer_own": NOT_DRAFT,
    "developer_other": NOT_DRAFT,
    "analyst_own": CANNOT_DELETE,
    "analyst_other": CANNOT_DELETE,
    "viewer_own": CANNOT_DELETE,
    "viewer_other": CANNOT_DELETE,
    "analyst_own_without_read": CANNOT_VIEW,
}

#: update_active: the refusal each signed-in caller but the superuser gets.
UPDATE_ACTIVE_DETAIL = {
    "admin_own": "Cannot update experiments in active status",
    "admin_other": "Cannot update experiments in active status",
    "developer_own": "Cannot update experiments in active status",
    "developer_other": "Cannot update experiments in active status",
    "analyst_own": CANNOT_UPDATE,
    "analyst_other": CANNOT_UPDATE,
    "viewer_own": CANNOT_UPDATE,
    "viewer_other": CANNOT_UPDATE,
    "analyst_own_without_read": CANNOT_VIEW,
}

#: row -> the detail asserted in each of its cells that has one.
ROW_DETAIL = {
    "delete_active": DELETE_ACTIVE_DETAIL,
    "update_active": UPDATE_ACTIVE_DETAIL,
}

READ_ROUTES = ["results", "daily_results", "segmented_results"]


def _ended():
    now = datetime.now(timezone.utc)
    return now - timedelta(days=10), now - timedelta(days=1)


#: row -> the (start_date, end_date) its experiment is given, where the
#: status alone would make the route refuse everyone.
ROW_DATES = {"unarchive": _ended}

CELLS = [(row, column) for row in EXPECTED for column in COLUMNS]
CACHED_CELLS = [(row, column) for row in READ_ROUTES for column in COLUMNS]


def _cache_keys(redis_handle, row, experiment_id):
    pattern = {
        "results": f"results:{experiment_id}:*",
        "daily_results": f"experiment_daily_results:{experiment_id}:{ENGINE_VERSION}",
        "segmented_results": (
            f"experiment_segmented_results:{experiment_id}:{ENGINE_VERSION}:country"
        ),
    }[row]
    return list(redis_handle.scan_iter(match=pattern))


# --- fixtures -----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _local_auth(monkeypatch):
    """The local JWT path. The no-token column answers 401 only while
    authentication is on, so a run with it switched off fails there."""
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "CACHE_ENABLED", False)


@pytest.fixture(scope="module")
def people(test_db):
    """One real user per column, plus the DEVELOPER who owns the "other"
    experiments (and creates every experiment through the API)."""
    factory = sessionmaker(bind=test_db, expire_on_commit=False)
    session = factory()
    session.execute(text("SET search_path TO test_experimentation"))
    suffix = uuid.uuid4().hex[:8]

    def make(name, role, is_superuser):
        user = User(
            username=f"access452_{name}_{suffix}",
            email=f"access452_{name}_{suffix}@access.test",
            full_name="Experiment Access User",
            hashed_password="unused: these users sign in by token only",
            is_active=True,
            is_superuser=is_superuser,
            role=role,
        )
        session.add(user)
        return user

    users = {
        column: make(column, role, su) for column, (role, su, _) in CALLERS.items()
    }
    users["creator"] = make("creator", UserRole.DEVELOPER, False)
    session.commit()
    try:
        yield users
    finally:
        session.close()


def _db_override(db_session):
    engine = db_session.get_bind()
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    def override_get_db():
        session = factory()
        session.execute(text("SET search_path TO test_experimentation"))
        try:
            yield session
        finally:
            session.close()

    return override_get_db


@pytest.fixture
def client(db_session):
    app.dependency_overrides[deps.get_db] = _db_override(db_session)
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
    finally:
        app.dependency_overrides.pop(deps.get_db, None)


@pytest.fixture
def redis_handle(monkeypatch):
    """The Redis the application will use, database 15, emptied."""
    monkeypatch.setattr(settings, "REDIS_DB", CACHE_DB)
    monkeypatch.setattr(settings, "REDIS_PASSWORD", None)
    monkeypatch.setattr(settings, "REDIS_SSL", False)
    handle = sync_redis.Redis(
        host=str(settings.REDIS_HOST),
        port=int(settings.REDIS_PORT),
        db=CACHE_DB,
        decode_responses=True,
        socket_connect_timeout=2,
    )
    try:
        handle.ping()
    except sync_redis.RedisError as exc:
        message = f"no Redis at {settings.REDIS_HOST}:{settings.REDIS_PORT}: {exc}"
        if os.environ.get("EXPERIMENTLY_REQUIRE_REDIS") == "1":
            pytest.fail(message + " but EXPERIMENTLY_REQUIRE_REDIS=1")
        pytest.skip(message)
    handle.flushdb()
    yield handle
    handle.flushdb()
    handle.close()


@pytest.fixture
def cached_client(redis_handle, db_session, monkeypatch):
    """The real ``get_cache_control`` with the cache on. The client is a
    context manager, so every request shares one event loop and the pooled
    Redis client survives; the pool is reset so it is built on this loop."""
    monkeypatch.setattr(settings, "CACHE_ENABLED", True)
    monkeypatch.setattr(deps, "_redis_pool", None)
    app.dependency_overrides[deps.get_db] = _db_override(db_session)
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
        monkeypatch.setattr(deps, "_redis_pool", None)


# --- helpers ------------------------------------------------------------------


def _auth(user):
    return {"Authorization": f"Bearer {create_local_access_token(user)}"}


def _payload():
    return {
        "name": f"access452 {uuid.uuid4().hex[:8]}",
        "description": "One experiment per cell",
        "hypothesis": "The route admits exactly the callers it should",
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


def _experiment(client, db_session, people, owner, status, dates=None):
    """Created through the API by the creator, then given owner and status
    (and ``dates()``'s start and end dates, when given)."""
    response = client.post(
        f"{BASE}/", json=_payload(), headers=_auth(people["creator"])
    )
    assert response.status_code == 201, response.text
    experiment_id = response.json()["id"]
    row = db_session.get(Experiment, uuid.UUID(experiment_id))
    row.owner_id = owner.id
    row.status = status
    if dates is not None:
        row.start_date, row.end_date = dates()
    db_session.commit()
    return experiment_id


def _request(client, row, experiment_id, headers):
    method, suffix, body, _ = ROUTES[row]
    url = f"{BASE}/{experiment_id}{suffix.replace('{id}', experiment_id)}"
    payload = body() if callable(body) else body
    return client.request(method, url, json=payload, headers=headers)


def _caller(people, column):
    if column == NO_TOKEN:
        return None, people["creator"]
    user = people[column]
    owns = CALLERS[column][2]
    return user, (user if owns else people["creator"])


def _without_read(monkeypatch, column):
    if column == "analyst_own_without_read":
        monkeypatch.setitem(
            ROLE_PERMISSIONS[UserRole.ANALYST],
            ResourceType.EXPERIMENT,
            [Action.LIST],
        )


# --- the matrix ---------------------------------------------------------------


def test_the_matrix_is_the_size_it_says():
    """17 routes by 11 callers, and 3 by 11 with the cache on."""
    assert len(ROUTES) == len(EXPECTED) == 17
    assert all(list(cells) == COLUMNS for cells in EXPECTED.values())
    assert len(COLUMNS) == 11
    assert len(CELLS) == 187
    assert len(CACHED_CELLS) == 33
    assert set(DELETE_ACTIVE_DETAIL) == set(CALLERS)
    assert set(UPDATE_ACTIVE_DETAIL) == set(CALLERS) - {"superuser"}


@pytest.mark.parametrize(("row", "column"), CELLS, ids=[f"{r}-{c}" for r, c in CELLS])
def test_status_by_route_and_caller(
    client, db_session, people, monkeypatch, row, column
):
    caller, owner = _caller(people, column)
    experiment_id = _experiment(
        client, db_session, people, owner, ROUTES[row][3], ROW_DATES.get(row)
    )
    headers = _auth(caller) if caller is not None else {}
    _without_read(monkeypatch, column)

    response = _request(client, row, experiment_id, headers)

    assert response.status_code == EXPECTED[row][column], response.text
    if column in ROW_DETAIL.get(row, {}):
        assert response.json()["detail"] == ROW_DETAIL[row][column]
    _assert_effect(db_session, row, experiment_id, response, caller, owner)


def _assert_effect(db_session, row, experiment_id, response, caller, owner):
    """A success status is not enough: the change must have happened."""
    db_session.expire_all()
    stored = db_session.get(Experiment, uuid.UUID(experiment_id))
    if row == "schedule" and response.status_code == 200:
        assert stored.start_date is not None, "schedule answered 200, saved no date"
    elif row == "delete" and response.status_code == 204:
        assert stored is None, "delete answered 204 but the experiment is still there"
        audit = _delete_audit(db_session, experiment_id)
        assert audit is not None, "no audit record of the delete"
        assert audit.actor_id == caller.id
        assert audit.old_value["owner_id"] == str(owner.id)
    elif row == "update_active":
        renamed = stored.name == "renamed"
        assert renamed == (response.status_code == 200), (
            response.status_code,
            stored.name,
        )
    elif row == "unarchive":
        expected = (
            ExperimentStatus.COMPLETED
            if response.status_code == 200
            else ExperimentStatus.ARCHIVED
        )
        assert stored.status == expected, (response.status_code, stored.status)
    elif row in ("schedule", "schedule_active", "delete", "delete_active"):
        # A refusal changes nothing.
        assert stored is not None
        assert stored.start_date is None


def _delete_audit(db_session, experiment_id):
    return (
        db_session.query(ComplianceAuditEvent)
        .filter(
            ComplianceAuditEvent.resource_type == "experiment",
            ComplianceAuditEvent.resource_id == experiment_id,
            ComplianceAuditEvent.action == AuditAction.DELETE,
        )
        .one_or_none()
    )


#: A DEVELOPER whose role keeps UPDATE but has had DELETE taken out. Every
#: shipped role that holds UPDATE also holds DELETE, so without this a delete
#: route that checked UPDATE instead of DELETE would pass every cell above.
WITHOUT_DELETE = {"delete": 403, "schedule": 200}


@pytest.mark.parametrize("row", list(WITHOUT_DELETE))
def test_developer_other_without_delete(client, db_session, people, monkeypatch, row):
    caller, owner = people["developer_other"], people["creator"]
    experiment_id = _experiment(client, db_session, people, owner, ROUTES[row][3])
    monkeypatch.setitem(
        ROLE_PERMISSIONS[UserRole.DEVELOPER],
        ResourceType.EXPERIMENT,
        [Action.CREATE, Action.READ, Action.UPDATE, Action.LIST],
    )

    response = _request(client, row, experiment_id, _auth(caller))

    assert response.status_code == WITHOUT_DELETE[row], response.text
    if row == "delete":
        assert response.json()["detail"] == CANNOT_DELETE
    _assert_effect(db_session, row, experiment_id, response, caller, owner)


@pytest.mark.parametrize(
    ("row", "column"), CACHED_CELLS, ids=[f"{r}-{c}" for r, c in CACHED_CELLS]
)
def test_status_with_the_answer_already_cached(
    cached_client, redis_handle, db_session, people, monkeypatch, row, column
):
    """The superuser reads first, so the answer is in Redis before the caller
    asks; the caller must still get the status the role table decides."""
    caller, owner = _caller(people, column)
    experiment_id = _experiment(
        cached_client, db_session, people, owner, ROUTES[row][3]
    )
    primed = _request(cached_client, row, experiment_id, _auth(people["superuser"]))
    assert primed.status_code == 200, primed.text
    assert _cache_keys(redis_handle, row, experiment_id), (
        f"{row} wrote nothing to Redis: the cache is not on, and this cell "
        "would pass whatever the order of the check and the cache read"
    )
    headers = _auth(caller) if caller is not None else {}
    _without_read(monkeypatch, column)

    response = _request(cached_client, row, experiment_id, headers)

    assert response.status_code == EXPECTED[row][column], response.text
