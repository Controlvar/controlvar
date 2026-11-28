"""``POST /api/v1/experiments/{experiment_id}/unarchive`` (#2278).

An archived experiment with a start date and a past end date comes back
COMPLETED, and only its status changes. Any other archived experiment is
refused with one sentence, and so is one that is not archived; a refusal
changes nothing. Who may call it is the role matrix's row ``unarchive``
(``test_experiment_access_by_role.py``); its audit entry is pinned in
``test_audit_route_events.py`` and its results-cache clear in
``test_results_cache_status_change.py``.

Real requests: real ``User`` rows, a real local JWT, and no dependency
override except ``deps.get_db``. Each experiment is created through the API
and brought to its state through the API where a route can do it, so the
dates are the ones the routes write.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.api.v1.endpoints.auth import create_local_access_token
from backend.app.core.config import settings
from backend.app.main import app
from backend.app.models.experiment import Experiment, ExperimentStatus
from backend.app.models.user import User, UserRole

pytestmark = [pytest.mark.integration]

BASE = "/api/v1/experiments"
SCHEMA = "test_experimentation"
REFUSED = (
    "Only an experiment with a start date and a past end date can be "
    "unarchived. Clone it to run it again."
)
NOT_ARCHIVED = "Experiment is not archived"


@pytest.fixture(autouse=True)
def _local_auth(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "CACHE_ENABLED", False)


@pytest.fixture(scope="module")
def people(test_db):
    """A DEVELOPER who creates the experiments, and an ADMIN who is not a
    superuser, who changes them."""
    factory = sessionmaker(bind=test_db, expire_on_commit=False)
    session = factory()
    session.execute(text(f"SET search_path TO {SCHEMA}"))
    suffix = uuid.uuid4().hex[:8]

    def make(name, role):
        user = User(
            username=f"unarchive_{name}_{suffix}",
            email=f"unarchive_{name}_{suffix}@unarchive.test",
            full_name="Unarchive User",
            hashed_password="unused: these users sign in by token only",
            is_active=True,
            is_superuser=False,
            role=role,
        )
        session.add(user)
        return user

    users = {
        "creator": make("creator", UserRole.DEVELOPER),
        "admin": make("admin", UserRole.ADMIN),
    }
    session.commit()
    try:
        yield users
    finally:
        session.close()


@pytest.fixture
def client(db_session):
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


def _auth(user):
    return {"Authorization": f"Bearer {create_local_access_token(user)}"}


def _payload():
    return {
        "name": f"unarchive {uuid.uuid4().hex[:8]}",
        "description": "An experiment to archive and bring back",
        "hypothesis": "Unarchive brings back only an ended experiment",
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


def _create(client, people):
    response = client.post(
        f"{BASE}/", json=_payload(), headers=_auth(people["creator"])
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _post(client, people, experiment_id, verb, expected=200):
    response = client.post(
        f"{BASE}/{experiment_id}/{verb}", headers=_auth(people["admin"])
    )
    assert response.status_code == expected, (verb, response.text)
    return response


def _row(db_session, experiment_id):
    """Every column of the stored experiment, read in a fresh transaction."""
    db_session.rollback()
    return dict(
        db_session.execute(
            text(f"SELECT * FROM {SCHEMA}.experiments WHERE id = :id"),
            {"id": experiment_id},
        )
        .mappings()
        .one()
    )


def _iso(moment):
    return moment.isoformat()


@pytest.mark.regression
def test_unarchive_returns_an_ended_experiment_to_completed(client, db_session, people):
    """Started, completed and archived through the routes, as the dashboard
    does. Before this route existed the request answered 404 with
    ``{"detail": "Not Found"}`` (no route), not ``Experiment not found``."""
    experiment_id = _create(client, people)
    for verb in ("start", "complete", "archive"):
        _post(client, people, experiment_id, verb)
    archived = _row(db_session, experiment_id)
    assert archived["status"] == "ARCHIVED"

    response = client.post(
        f"{BASE}/{experiment_id}/unarchive", headers=_auth(people["admin"])
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed"
    after = _row(db_session, experiment_id)
    changed = {k for k in archived if archived[k] != after[k]}
    assert changed == {"status", "updated_at"}, changed
    assert after["status"] == "COMPLETED"


def test_an_unarchived_experiment_can_be_archived_again(client, db_session, people):
    experiment_id = _create(client, people)
    for verb in ("start", "complete", "archive", "unarchive"):
        _post(client, people, experiment_id, verb)

    assert (
        _post(client, people, experiment_id, "archive").json()["status"] == "archived"
    )
    response = _post(client, people, experiment_id, "unarchive")
    assert response.json()["status"] == "completed"


@pytest.mark.regression
def test_an_archived_draft_with_only_a_past_end_date_is_refused(
    client, db_session, people
):
    """A draft given a past end date and archived has no start date. Brought
    back it would be COMPLETED with no start date, a state no route makes."""
    experiment_id = _create(client, people)
    past = datetime.now(timezone.utc) - timedelta(days=2)
    response = client.put(
        f"{BASE}/{experiment_id}/schedule",
        json={"end_date": _iso(past)},
        headers=_auth(people["admin"]),
    )
    assert response.status_code == 200, response.text
    _post(client, people, experiment_id, "archive")
    before = _row(db_session, experiment_id)
    assert before["start_date"] is None and before["end_date"] is not None

    response = _post(client, people, experiment_id, "unarchive", expected=400)

    assert response.json() == {"detail": REFUSED}
    assert _row(db_session, experiment_id) == before


def test_an_experiment_archived_while_active_before_its_end_date_is_refused(
    client, db_session, people
):
    """It has a start date and an end date, but the end date is still ahead:
    the refusal must come from the end date, not from a missing one."""
    experiment_id = _create(client, people)
    start = datetime.now(timezone.utc) + timedelta(days=1)
    response = client.put(
        f"{BASE}/{experiment_id}/schedule",
        json={"start_date": _iso(start), "end_date": _iso(start + timedelta(days=7))},
        headers=_auth(people["admin"]),
    )
    assert response.status_code == 200, response.text
    _post(client, people, experiment_id, "start")
    _post(client, people, experiment_id, "archive")
    before = _row(db_session, experiment_id)
    assert before["start_date"] is not None
    assert before["end_date"] > datetime.now(timezone.utc).replace(tzinfo=None)

    response = _post(client, people, experiment_id, "unarchive", expected=400)

    assert response.json() == {"detail": REFUSED}
    assert _row(db_session, experiment_id) == before


def test_an_archived_experiment_with_no_end_date_is_refused(client, db_session, people):
    """Archived while active with no end date: it never ended."""
    experiment_id = _create(client, people)
    _post(client, people, experiment_id, "start")
    _post(client, people, experiment_id, "archive")
    before = _row(db_session, experiment_id)
    assert before["start_date"] is not None and before["end_date"] is None

    response = _post(client, people, experiment_id, "unarchive", expected=400)

    assert response.json() == {"detail": REFUSED}
    assert _row(db_session, experiment_id) == before


@pytest.mark.parametrize(
    "status",
    [
        ExperimentStatus.DRAFT,
        ExperimentStatus.ACTIVE,
        ExperimentStatus.PAUSED,
        ExperimentStatus.COMPLETED,
    ],
    ids=lambda s: s.value,
)
def test_an_experiment_that_is_not_archived_is_refused(
    client, db_session, people, status
):
    """Given dates that would pass, so that only the status refuses it."""
    experiment_id = _create(client, people)
    row = db_session.get(Experiment, uuid.UUID(experiment_id))
    now = datetime.now(timezone.utc)
    row.start_date = now - timedelta(days=10)
    row.end_date = now - timedelta(days=1)
    row.status = status
    db_session.commit()
    before = _row(db_session, experiment_id)

    response = _post(client, people, experiment_id, "unarchive", expected=400)

    assert response.json() == {"detail": NOT_ARCHIVED}
    assert _row(db_session, experiment_id) == before


def test_an_unknown_experiment_is_404(client, people):
    response = client.post(
        f"{BASE}/{uuid.uuid4()}/unarchive", headers=_auth(people["admin"])
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "Experiment not found"}
