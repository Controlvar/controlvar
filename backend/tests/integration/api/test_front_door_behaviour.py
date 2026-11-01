"""What the front door says the API does, through the API and a real database.

* any signed-in user, a VIEWER included, can create an API key for themselves,
  and that key assigns users and tracks their events;
* an experiment with one variant cannot be started;
* every flag evaluates with ``"config": null``, a string flag included (its
  value is in ``value``).

The pages that say so are tied to these tests by their registries under
``docs/_claims/``.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.core.config import settings
from backend.app.core.security import create_local_access_token, get_password_hash
from backend.app.main import app
from backend.app.models.user import User, UserRole

pytestmark = [pytest.mark.integration, pytest.mark.regression]


@pytest.fixture(autouse=True)
def _local_fail_closed(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")


def _user(db_session, role: UserRole) -> User:
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"front_{role.value}_{suffix}",
        email=f"front_{role.value}_{suffix}@front.test",
        full_name="Front door",
        hashed_password=get_password_hash("Demo1234!"),
        is_active=True,
        is_superuser=False,
        role=role,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def client(db_session):
    factory = sessionmaker(
        bind=db_session.get_bind(), autocommit=False, autoflush=False
    )

    def override_get_db():
        session = factory()
        session.execute(text("SET search_path TO test_experimentation"))
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


def _auth(user: User) -> dict:
    return {"Authorization": f"Bearer {create_local_access_token(user)}"}


def _experiment(client, owner, *, variants=None, metric_type="conversion"):
    """Create an experiment through the API; returns its JSON."""
    suffix = uuid.uuid4().hex[:8]
    body = {
        "name": f"Front door {suffix}",
        "key": f"front-door-{suffix}",
        "hypothesis": "the front door is true",
        "experiment_type": "a_b",
        "variants": variants
        or [
            {"name": "control", "is_control": True, "traffic_allocation": 50},
            {"name": "treatment", "is_control": False, "traffic_allocation": 50},
        ],
        "metrics": [
            {
                "name": "Purchase",
                "event_name": f"purchase_{suffix}",
                "metric_type": metric_type,
                "is_primary": True,
            }
        ],
    }
    resp = client.post("/api/v1/experiments/", json=body, headers=_auth(owner))
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


def test_a_viewers_own_key_assigns_users_and_tracks_events(client, db_session):
    developer = _user(db_session, UserRole.DEVELOPER)
    viewer = _user(db_session, UserRole.VIEWER)
    experiment = _experiment(client, developer)
    started = client.post(
        f"/api/v1/experiments/{experiment['id']}/start", headers=_auth(developer)
    )
    assert started.status_code == 200, started.text

    created = client.post(
        "/api/v1/api-keys", json={"name": "viewer"}, headers=_auth(viewer)
    )
    assert created.status_code == 201, created.text
    key = {"X-API-Key": created.json()["key"]}

    assigned = client.post(
        "/api/v1/tracking/assign",
        json={"experiment_key": experiment["key"], "user_id": "viewer-app-user"},
        headers=key,
    )
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["assigned"] is True

    tracked = client.post(
        "/api/v1/tracking/track",
        json={
            "event_type": "conversion",
            "event_name": experiment["metrics"][0]["event_name"],
            "user_id": "viewer-app-user",
            "experiment_key": experiment["key"],
            "value": 1,
        },
        headers=key,
    )
    assert tracked.status_code in (200, 201), tracked.text


def test_an_experiment_with_one_variant_cannot_start(client, db_session):
    developer = _user(db_session, UserRole.DEVELOPER)
    experiment = _experiment(
        client,
        developer,
        variants=[{"name": "control", "is_control": True, "traffic_allocation": 100}],
    )
    resp = client.post(
        f"/api/v1/experiments/{experiment['id']}/start", headers=_auth(developer)
    )
    assert resp.status_code == 400, resp.text
    assert "at least two variants" in resp.json()["detail"]


def test_every_flag_evaluates_with_a_null_config(client, db_session):
    developer = _user(db_session, UserRole.DEVELOPER)
    suffix = uuid.uuid4().hex[:8]
    flag = client.post(
        "/api/v1/feature-flags/",
        json={
            "key": f"front-{suffix}",
            "name": "Front",
            "rollout_percentage": 100,
            "is_active": True,
        },
        headers=_auth(developer),
    )
    assert flag.status_code in (200, 201), flag.text
    key = {
        "X-API-Key": client.post(
            "/api/v1/api-keys", json={"name": "flag"}, headers=_auth(developer)
        ).json()["key"]
    }
    resp = client.get(
        f"/api/v1/feature-flags/evaluate/front-{suffix}",
        params={"user_id": "anyone"},
        headers=key,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["enabled"] is True
    assert resp.json()["config"] is None

    # A string flag too (#1497): its value travels in ``value``, never in
    # ``config``.
    valued = client.post(
        "/api/v1/feature-flags/",
        json={
            "key": f"front-value-{suffix}",
            "name": "Front value",
            "rollout_percentage": 100,
            "is_active": True,
            "value_type": "string",
            "on_value": "Complete purchase",
            "off_value": "Buy now",
        },
        headers=_auth(developer),
    )
    assert valued.status_code == 201, valued.text
    resp = client.get(
        f"/api/v1/feature-flags/evaluate/front-value-{suffix}",
        params={"user_id": "anyone"},
        headers=key,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["config"] is None
    assert resp.json()["value"] == "Complete purchase"
