"""A running experiment after #1159: assigned users keep their variant, new
users are enrolled by the rule as flags read it.

Each experiment is created and started through the API with a native
``array_length`` rule, the way a customer's would be. One user already holds
an assignment the corrected rule would not give (planted as a row, as an
earlier enrolment leaves it); ``POST /api/v1/tracking/assign`` keeps that row
and its variant. New users are enrolled, or turned away, by the rule as flag
targeting answers it: ``gt 2`` now admits a three-item list, and a ``NOT``
around it (which used to admit everyone) now turns that list away.
"""

from __future__ import annotations

import secrets
import uuid
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.api.v1.endpoints.auth import create_local_access_token
from backend.app.core.config import settings
from backend.app.core.security import hash_api_key
from backend.app.main import app
from backend.app.models.api_key import APIKey
from backend.app.models.assignment import Assignment
from backend.app.models.user import User, UserRole

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

EXPERIMENTS = "/api/v1/experiments"
ASSIGN = "/api/v1/tracking/assign"
PREFIX = "arrlen"

LONG = ["a", "b", "c"]
SHORT = ["a"]


def _rules(group_operator: str) -> Dict[str, Any]:
    return {
        "version": "1.0",
        "rules": [
            {
                "id": "longer-than-two",
                "rule": {
                    "operator": group_operator,
                    "conditions": [
                        {
                            "attribute": "tags",
                            "operator": "array_length",
                            "value": 2,
                            "additional_value": "gt",
                        }
                    ],
                },
                "rollout_percentage": 100,
            }
        ],
    }


#: rule group -> (tags the rule enrols, tags it turns away)
CASES = {"and": (LONG, SHORT), "not": (SHORT, LONG)}


@pytest.fixture(autouse=True)
def _local_auth(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "CACHE_ENABLED", False)


@pytest.fixture
def fresh(test_db):
    """Run ``work(session)`` in a session of the test's own and commit."""
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
def developer(db_session):
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"{PREFIX}_dev_{suffix}",
        email=f"{PREFIX}_dev_{suffix}@enrolment.test",
        full_name="Array length developer",
        hashed_password="unused: this user signs in by token only",
        is_active=True,
        is_superuser=False,
        role=UserRole.DEVELOPER,
    )
    db_session.add(user)
    db_session.commit()
    return user


@pytest.fixture
def api_key(fresh, developer):
    raw = f"{PREFIX}_{secrets.token_hex(16)}"

    def add(session):
        session.add(
            APIKey(
                key=hash_api_key(raw),
                name=f"{PREFIX} {uuid.uuid4().hex[:6]}",
                is_active=True,
                user_id=developer.id,
            )
        )

    fresh(add)
    return {"X-API-Key": raw}


def _auth(user: User) -> Dict[str, str]:
    return {"Authorization": f"Bearer {create_local_access_token(user)}"}


def _started_experiment(client, developer, rules) -> Dict[str, Any]:
    body = {
        "name": f"{PREFIX} {uuid.uuid4().hex[:8]}",
        "description": "array_length enrolment",
        "hypothesis": "Experiments read array_length as flags do",
        "experiment_type": "a_b",
        "targeting_rules": rules,
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
    created = client.post(f"{EXPERIMENTS}/", json=body, headers=_auth(developer))
    assert created.status_code == 201, created.text
    experiment = created.json()
    started = client.post(
        f"{EXPERIMENTS}/{experiment['id']}/start", headers=_auth(developer)
    )
    assert started.status_code == 200, started.text
    return experiment


def _assign(client, api_key, key: str, user_id: str, tags: List[str]):
    response = client.post(
        ASSIGN,
        json={"experiment_key": key, "user_id": user_id, "context": {"tags": tags}},
        headers=api_key,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _rows(fresh, experiment_id: str, user_id: str):
    def read(session):
        return [
            (str(row.id), str(row.variant_id), row.created_at)
            for row in session.query(Assignment)
            .filter(
                Assignment.experiment_id == uuid.UUID(experiment_id),
                Assignment.user_id == user_id,
            )
            .all()
        ]

    return fresh(read)


@pytest.mark.regression
@pytest.mark.parametrize("group_operator", sorted(CASES))
def test_assigned_users_keep_their_variant_and_new_users_follow_the_rule(
    client, developer, api_key, fresh, group_operator
):
    enrolled_tags, refused_tags = CASES[group_operator]
    experiment = _started_experiment(client, developer, _rules(group_operator))
    treatment = next(v for v in experiment["variants"] if not v["is_control"])

    # Already assigned, with a context the rule now turns away.
    already = f"{PREFIX}-already-{uuid.uuid4().hex[:8]}"

    def plant(session):
        session.add(
            Assignment(
                experiment_id=uuid.UUID(experiment["id"]),
                variant_id=uuid.UUID(treatment["id"]),
                user_id=already,
            )
        )

    fresh(plant)
    before = _rows(fresh, experiment["id"], already)
    assert len(before) == 1

    kept = _assign(client, api_key, experiment["key"], already, refused_tags)
    assert (kept["assigned"], kept["variant_id"], kept["variant_name"]) == (
        True,
        treatment["id"],
        "Treatment",
    ), kept
    assert _rows(fresh, experiment["id"], already) == before

    # New users: enrolled, or turned away, by the rule as flags read it.
    newcomer = f"{PREFIX}-new-{uuid.uuid4().hex[:8]}"
    joined = _assign(client, api_key, experiment["key"], newcomer, enrolled_tags)
    assert (joined["assigned"], joined["reason"]) == (True, "assigned"), joined
    assert len(_rows(fresh, experiment["id"], newcomer)) == 1

    outsider = f"{PREFIX}-out-{uuid.uuid4().hex[:8]}"
    refused = _assign(client, api_key, experiment["key"], outsider, refused_tags)
    assert (refused["assigned"], refused["reason"]) == (False, "targeting"), refused
    assert _rows(fresh, experiment["id"], outsider) == []
