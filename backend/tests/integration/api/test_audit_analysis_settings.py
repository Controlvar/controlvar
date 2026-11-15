"""An experiment's update entry names its CUPED, sequential and Bayesian settings (#848).

A draft's ``PUT /api/v1/experiments/{id}`` may change its analysis settings,
but the ``experiment_update`` entry it wrote named none of them: a PUT that
changed only ``variance_reduction_config`` was recorded with no old and no new
value, so the audit log could not say that the analysis had changed.

The three configurations (``variance_reduction_config``,
``sequential_testing_config``, ``bayesian_config``) are recorded by name only,
under ``new_value.changed_fields``, like targeting rules; the two switches
(``sequential_testing_enabled``, ``bayesian_enabled``) as values. Turning
Bayesian analysis on with no config also stores the default config, so that
entry names ``bayesian_config`` as well. A create or a delete records value
fields only, so its entry holds the two switches and no configuration.

Each case is a real request by a DEVELOPER who is not a superuser, signed in
with a local token, on a draft that developer created through the API, read
back through ``GET /api/v1/audit-logs/``, the route the dashboard's Audit Log
page reads.
"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.api.v1.endpoints.auth import create_local_access_token
from backend.app.core.config import settings
from backend.app.main import app
from backend.app.models.audit_log import AuditLog
from backend.app.models.experiment import Experiment
from backend.app.models.user import User, UserRole

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

SCHEMA = "test_experimentation"
P = "au848"
V1 = "/api/v1"

CUPED = {"method": "cuped", "covariate_lookback_days": 7}
SEQUENTIAL = {"spending_function": "obrien_fleming", "planned_looks": 5}
PRIOR = {"prior_family": "beta", "alpha": 100, "beta": 900}


@pytest.fixture(autouse=True)
def _local_auth(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "CACHE_ENABLED", False)


@pytest.fixture
def client(test_db):
    factory = sessionmaker(bind=test_db, autocommit=False, autoflush=False)

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


@pytest.fixture
def developer(db_session):
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"{P}_developer_{suffix}",
        email=f"{P}_{suffix}@example.com",
        full_name="Analysis Settings Audit User",
        hashed_password="unused: this user signs in by token only",
        is_active=True,
        is_superuser=False,
        role=UserRole.DEVELOPER,
    )
    db_session.add(user)
    db_session.commit()
    return user


@pytest.fixture(autouse=True)
def _cleanup(db_session):
    """Leave no experiment, entry or user of this file behind."""
    yield
    db_session.rollback()
    s = db_session
    users = s.query(User).filter(User.username.like(f"{P}_%")).all()
    user_ids = [u.id for u in users]
    for exp in s.query(Experiment).filter(Experiment.owner_id.in_(user_ids)):
        s.delete(exp)
    s.commit()
    s.query(AuditLog).filter(AuditLog.user_email.like(f"{P}%")).delete(
        synchronize_session=False
    )
    for user in users:
        s.delete(user)
    s.commit()


def _auth(user):
    return {"Authorization": f"Bearer {create_local_access_token(user)}"}


def _draft(client, user, **settings):
    """A draft experiment, created through the API as *user*."""
    response = client.post(
        f"{V1}/experiments/",
        json={
            "name": f"{P} exp {uuid.uuid4().hex[:6]}",
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
            **settings,
        },
        headers=_auth(user),
    )
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["status"] == "draft"
    return created


def _entries(client, user, experiment_id, action):
    """One experiment's entries of one action, read through the route."""
    response = client.get(
        f"{V1}/audit-logs/",
        params={
            "entity_type": "experiment",
            "entity_id": experiment_id,
            "action_type": action,
        },
        headers=_auth(user),
    )
    assert response.status_code == 200, response.text
    return response.json()["items"]


def _values(entry):
    def load(value):
        return json.loads(value) if value else None

    return load(entry["old_value"]), load(entry["new_value"])


@pytest.mark.regression
@pytest.mark.parametrize(
    "body, old, new",
    [
        pytest.param(
            {"variance_reduction_config": CUPED},
            None,
            {"changed_fields": ["variance_reduction_config"]},
            id="variance_reduction_config",
        ),
        pytest.param(
            {"bayesian_enabled": True},
            {"bayesian_enabled": False},
            {"bayesian_enabled": True, "changed_fields": ["bayesian_config"]},
            id="bayesian_enabled-adds-the-default-config",
        ),
        pytest.param(
            {"bayesian_config": PRIOR},
            None,
            {"changed_fields": ["bayesian_config"]},
            id="bayesian_config",
        ),
        pytest.param(
            {"sequential_testing_enabled": True},
            {"sequential_testing_enabled": False},
            {"sequential_testing_enabled": True},
            id="sequential_testing_enabled",
        ),
        pytest.param(
            {
                "sequential_testing_enabled": True,
                "sequential_testing_config": SEQUENTIAL,
            },
            {"sequential_testing_enabled": False},
            {
                "sequential_testing_enabled": True,
                "changed_fields": ["sequential_testing_config"],
            },
            id="sequential_testing_enabled-and-config",
        ),
    ],
)
def test_a_draft_put_of_an_analysis_setting_is_named_in_its_entry(
    client, developer, body, old, new
):
    exp = _draft(client, developer)

    response = client.put(
        f"{V1}/experiments/{exp['id']}", json=body, headers=_auth(developer)
    )
    assert response.status_code == 200, response.text

    (entry,) = _entries(client, developer, exp["id"], "experiment_update")
    assert entry["user_id"] == str(developer.id)
    # Whole values: a configuration is named, and none of its contents is stored.
    assert _values(entry) == (old, new)


@pytest.mark.regression
def test_a_create_and_a_delete_record_whether_the_analyses_are_on(client, developer):
    exp = _draft(
        client,
        developer,
        sequential_testing_enabled=True,
        sequential_testing_config=SEQUENTIAL,
        bayesian_enabled=True,
        bayesian_config=PRIOR,
        variance_reduction_config=CUPED,
    )
    on = {"sequential_testing_enabled": True, "bayesian_enabled": True}

    (created,) = _entries(client, developer, exp["id"], "experiment_create")
    old, new = _values(created)
    assert old is None
    assert {field: new[field] for field in on} == on
    # A create records value fields only: no configuration, not even by name.
    assert not {
        "changed_fields",
        "variance_reduction_config",
        "sequential_testing_config",
        "bayesian_config",
    } & set(new)

    response = client.delete(
        f"{V1}/experiments/{exp['id']}",
        params={"experiment_key": exp["id"]},
        headers=_auth(developer),
    )
    assert response.status_code == 204, response.text
    (deleted,) = _entries(client, developer, exp["id"], "experiment_delete")
    old, new = _values(deleted)
    assert new is None
    assert {field: old[field] for field in on} == on
