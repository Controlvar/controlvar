"""Variants keep the order one request inserted them in, even on a clock tie (#1469).

``Experiment.variants`` is ordered by ``[created_at, id]``. Create, update and
clone insert every variant of an experiment in one request, so the order rests
on each row getting a later ``created_at`` than the one before. The column
default is frozen here, so every row would share one timestamp and the random
uuid would decide, to prove it does not.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.api.v1.endpoints.auth import create_local_access_token
from backend.app.core.config import settings
from backend.app.main import app
from backend.app.models.experiment import Experiment, Variant
from backend.app.models.user import User, UserRole

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

EXPERIMENTS = "/api/v1/experiments"
FROZEN = datetime(2026, 10, 31, 12, 0, 0, 123456)
NAMES = [f"V{i}" for i in range(12)]


@pytest.fixture(autouse=True)
def _local_auth(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "CACHE_ENABLED", False)


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch):
    """Every row that falls back to the column default gets the same instant."""
    default = Variant.__table__.c.created_at.default
    monkeypatch.setattr(default, "arg", lambda ctx: FROZEN)


@pytest.fixture
def fresh(test_db):
    factory = sessionmaker(bind=test_db, expire_on_commit=False)

    def run(work):
        session = factory()
        try:
            session.execute(text("SET search_path TO test_experimentation"))
            result = work(session)
            session.commit()
            return result
        finally:
            session.close()

    return run


@pytest.fixture
def client(test_db):
    factory = sessionmaker(bind=test_db, autocommit=False, autoflush=False)

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


@pytest.fixture
def headers(db_session):
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"vorder_{suffix}",
        email=f"vorder_{suffix}@order.test",
        full_name="Variant Order",
        hashed_password="unused: this user signs in by token only",
        is_active=True,
        is_superuser=False,
        role=UserRole.DEVELOPER,
    )
    db_session.add(user)
    db_session.commit()
    return {"Authorization": f"Bearer {create_local_access_token(user)}"}


def _variants(names):
    share, rest = divmod(100, len(names))
    return [
        {
            "name": name,
            "is_control": i == 0,
            "traffic_allocation": share + (rest if i == 0 else 0),
        }
        for i, name in enumerate(names)
    ]


def _body(names):
    return {
        "name": f"vorder {uuid.uuid4().hex[:8]}",
        "description": "Variant order",
        "hypothesis": "Order is the insertion order",
        "experiment_type": "a_b",
        "variants": _variants(names),
        "metrics": [
            {
                "name": "Conversion Rate",
                "event_name": "purchase",
                "metric_type": "conversion",
                "aggregation_method": "count",
                "is_primary": True,
            }
        ],
    }


def _stored_order(fresh, experiment_id):
    def work(session):
        experiment = session.get(Experiment, uuid.UUID(experiment_id))
        return [v.name for v in experiment.variants]

    return fresh(work)


@pytest.mark.regression
def test_create_keeps_the_order_of_the_request(client, headers, fresh):
    created = client.post(EXPERIMENTS + "/", json=_body(NAMES), headers=headers)
    assert created.status_code == 201, created.text

    assert _stored_order(fresh, created.json()["id"]) == NAMES


@pytest.mark.regression
def test_update_keeps_the_order_of_the_request(client, headers, fresh):
    created = client.post(EXPERIMENTS + "/", json=_body(["A", "B"]), headers=headers)
    assert created.status_code == 201, created.text
    experiment_id = created.json()["id"]

    reordered = list(reversed(NAMES))
    updated = client.put(
        f"{EXPERIMENTS}/{experiment_id}",
        json={"variants": _variants(reordered)},
        headers=headers,
    )
    assert updated.status_code == 200, updated.text

    assert _stored_order(fresh, experiment_id) == reordered


@pytest.mark.regression
def test_clone_keeps_the_order_of_its_source(client, headers, fresh):
    created = client.post(EXPERIMENTS + "/", json=_body(NAMES), headers=headers)
    assert created.status_code == 201, created.text

    cloned = client.post(f"{EXPERIMENTS}/{created.json()['id']}/clone", headers=headers)
    assert cloned.status_code in (200, 201), cloned.text

    assert _stored_order(fresh, cloned.json()["id"]) == NAMES
