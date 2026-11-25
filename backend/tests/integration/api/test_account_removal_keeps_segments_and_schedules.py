"""Removing a user's account keeps the segments and rollout schedules it created.

``segments.owner_id`` and ``rollout_schedules.owner_id`` are nullable and
``ON DELETE SET NULL``, and ``User.segments`` and ``User.rollout_schedules``
carry no delete cascade, so both are kept, with ``owner_id`` null, when the
account is removed. The account's API keys are removed with it, as before.

A DEVELOPER creates a rules segment, an id-list segment with two members, a
rollout schedule with two stages and an API key through the routes. The account is
removed through ``DELETE /api/v1/admin/users/{user_id}`` or through
``DELETE /api/v1/users/{user_id}`` (one case each), and the response is 204.
Afterwards the segments are listed and read, the id list keeps its two
members, the schedule reads with both of its stages and ``owner_id: null``,
and the account's key is refused.

``test_only_api_keys_are_removed_with_the_account`` pins which ``User``
relationships cascade a delete: ``api_keys`` alone.

Every request is a real one: local JWTs from ``create_local_access_token``,
API keys created through ``POST /api/v1/api-keys`` and sent as
``X-API-Key``, and no dependency override except ``deps.get_db``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text
from sqlalchemy.orm import configure_mappers, sessionmaker

from backend.app.api import deps
from backend.app.core.config import settings
from backend.app.core.security import create_local_access_token
from backend.app.main import app
from backend.app.models.api_key import APIKey
from backend.app.models.global_holdout import GlobalHoldout
from backend.app.models.rollout_schedule import RolloutSchedule, RolloutStage
from backend.app.models.segment import Segment
from backend.app.models.user import User, UserRole

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

SEGMENTS = "/api/v1/segments"
FLAGS = "/api/v1/feature-flags"
SCHEDULES = "/api/v1/rollout-schedules"
EXPERIMENTS = "/api/v1/experiments"
KEYS = "/api/v1/api-keys"
ASSIGN = "/api/v1/tracking/assign"

US = {"country": "US"}

US_RULES = {
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


def _in_segment(segment_id: str) -> Dict[str, Any]:
    return {
        "logical_operator": "AND",
        "groups": [
            {
                "logical_operator": "AND",
                "conditions": [
                    {
                        "attribute": "segment",
                        "operator": "in_segment",
                        "value": segment_id,
                    }
                ],
            }
        ],
    }


@pytest.fixture(autouse=True)
def _local_auth(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "CACHE_ENABLED", False)


@pytest.fixture
def no_holdout(db_session):
    """No active global holdout for the duration, restored afterwards."""
    parked = (
        db_session.query(GlobalHoldout).filter(GlobalHoldout.is_active.is_(True)).all()
    )
    for row in parked:
        row.is_active = False
    db_session.commit()
    yield
    db_session.rollback()
    for row in parked:
        db_session.merge(row).is_active = True
    db_session.commit()


@pytest.fixture
def client(db_session):
    factory = sessionmaker(bind=db_session.get_bind(), autocommit=False)

    def override_get_db():
        session = factory()
        session.execute(text("SET search_path TO test_experimentation"))
        try:
            yield session
        finally:
            session.close()

    assert deps.get_api_key not in app.dependency_overrides
    app.dependency_overrides[deps.get_db] = override_get_db
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
    finally:
        app.dependency_overrides.pop(deps.get_db, None)


def _user(db_session, role: UserRole, is_superuser: bool) -> User:
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"keeps_{role.value}_{suffix}",
        email=f"keeps_{role.value}_{suffix}@keeps.test",
        full_name="Account Removal",
        hashed_password="unused: this user signs in by token only",
        is_active=True,
        is_superuser=is_superuser,
        role=role,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _auth(user: User) -> Dict[str, str]:
    return {"Authorization": f"Bearer {create_local_access_token(user)}"}


def _key(client: TestClient, user: User) -> Dict[str, str]:
    response = client.post(
        KEYS, json={"name": f"keeps-{uuid.uuid4().hex[:6]}"}, headers=_auth(user)
    )
    assert response.status_code == 201, response.text
    return {"X-API-Key": response.json()["key"]}


def _in_future(hours: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _evaluate(client: TestClient, key: str, api_key: Dict[str, str], user_id: str):
    return client.get(
        f"{FLAGS}/evaluate/{key}",
        params={"user_id": user_id, "context": '{"country": "US"}'},
        headers=api_key,
    )


def _assign(client: TestClient, key: str, api_key: Dict[str, str], user_id: str):
    response = client.post(
        ASSIGN,
        json={"experiment_key": key, "user_id": user_id, "context": US},
        headers=api_key,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _listed_segment_ids(client: TestClient, user: User) -> set:
    """Every segment ``GET /segments`` lists, page by page."""
    ids, offset = set(), 0
    while True:
        page = client.get(
            SEGMENTS, params={"limit": 200, "offset": offset}, headers=_auth(user)
        )
        assert page.status_code == 200, page.text
        ids.update(item["id"] for item in page.json())
        if len(page.json()) < 200:
            return ids
        offset += 200


@pytest.mark.regression
def test_only_api_keys_are_removed_with_the_account():
    assert False, "planted: a failing integration test in shard 2"
    configure_mappers()
    cascading = {
        relationship.key
        for relationship in sa_inspect(User).relationships
        if relationship.cascade.delete
    }
    assert cascading == {"api_keys"}


@pytest.mark.regression
@pytest.mark.parametrize("route", ["admin_route", "users_route"])
def test_removing_an_account_keeps_its_segments_and_rollout_schedules(
    client, db_session, no_holdout, route
):
    superuser = _user(db_session, UserRole.ADMIN, is_superuser=True)
    developer = _user(db_session, UserRole.DEVELOPER, is_superuser=False)
    developer_uuid = developer.id
    developer_id = str(developer_uuid)

    # The developer creates a rules segment.
    created = client.post(
        SEGMENTS,
        json={"name": f"Keeps {uuid.uuid4().hex[:8]}", "rules": US_RULES},
        headers=_auth(developer),
    )
    assert created.status_code == 201, created.text
    segment_id = created.json()["id"]

    flag_key = f"keeps-{uuid.uuid4().hex[:10]}"
    flag = client.post(
        f"{FLAGS}/",
        json={
            "key": flag_key,
            "name": "Keeps flag",
            "is_active": True,
            "rollout_percentage": 0,
            "targeting_rules": _in_segment(segment_id),
        },
        headers=_auth(superuser),
    )
    assert flag.status_code == 201, flag.text

    # The developer creates a rollout schedule with two stages on that flag.
    schedule = client.post(
        f"{SCHEDULES}/",
        json={
            "name": f"Keeps schedule {uuid.uuid4().hex[:6]}",
            "feature_flag_id": flag.json()["id"],
            "start_date": _in_future(1),
            "end_date": _in_future(72),
            "max_percentage": 100,
            "min_stage_duration": 1,
            "stages": [
                {
                    "name": "Stage 1",
                    "stage_order": 1,
                    "target_percentage": 25,
                    "trigger_type": "time_based",
                    "start_date": _in_future(1),
                },
                {
                    "name": "Stage 2",
                    "stage_order": 2,
                    "target_percentage": 100,
                    "trigger_type": "manual",
                },
            ],
        },
        headers=_auth(developer),
    )
    assert schedule.status_code == 201, schedule.text
    assert schedule.json()["owner_id"] == developer_id
    schedule_id = schedule.json()["id"]
    stage_ids = {stage["id"] for stage in schedule.json()["stages"]}
    assert len(stage_ids) == 2

    experiment = client.post(
        f"{EXPERIMENTS}/",
        json={
            "name": f"Keeps experiment {uuid.uuid4().hex[:8]}",
            "description": "Targets a segment",
            "hypothesis": "The segment outlives its creator's account",
            "experiment_type": "a_b",
            "targeting_rules": _in_segment(segment_id),
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
        headers=_auth(superuser),
    )
    assert experiment.status_code == 201, experiment.text
    experiment_id, experiment_key = experiment.json()["id"], experiment.json()["key"]
    started = client.post(
        f"{EXPERIMENTS}/{experiment_id}/start", headers=_auth(superuser)
    )
    assert started.status_code == 200, started.text

    # The developer also creates an id-list segment with two members.
    id_list_id = client.post(
        SEGMENTS,
        json={"name": f"Keeps ids {uuid.uuid4().hex[:8]}", "kind": "id_list"},
        headers=_auth(developer),
    ).json()["id"]
    added = client.post(
        f"{SEGMENTS}/{id_list_id}/members",
        json={"add": ["keeps-member-1", "keeps-member-2"]},
        headers=_auth(developer),
    )
    assert added.json()["member_count"] == 2, added.text

    # The developer holds an API key; a second key serves the SDK calls.
    developer_key = _key(client, developer)
    sdk_key = _key(client, superuser)

    assert _evaluate(client, flag_key, developer_key, "keeps-probe").status_code == 200
    before = _evaluate(client, flag_key, sdk_key, f"keeps-{uuid.uuid4().hex[:8]}")
    assert before.status_code == 200, before.text
    assert (before.json()["enabled"], before.json()["reason"]) == (
        True,
        "targeting_rule",
    )
    assigned = _assign(client, experiment_key, sdk_key, f"keeps-{uuid.uuid4().hex[:8]}")
    assert assigned["assigned"] is True, assigned

    # Remove the account.
    if route == "admin_route":
        removed = client.delete(
            f"/api/v1/admin/users/{developer_id}", headers=_auth(superuser)
        )
    else:
        removed = client.delete(
            f"/api/v1/users/{developer_id}", headers=_auth(developer)
        )
    assert removed.status_code == 204, removed.text

    # The segments are kept, with no owner: listed and read.
    db_session.expire_all()
    assert db_session.get(User, developer_uuid) is None
    assert {segment_id, id_list_id} <= _listed_segment_ids(client, superuser)
    read = client.get(f"{SEGMENTS}/{segment_id}", headers=_auth(superuser))
    assert read.status_code == 200, read.text
    assert db_session.get(Segment, uuid.UUID(segment_id)).owner_id is None
    ids = client.get(f"{SEGMENTS}/{id_list_id}", headers=_auth(superuser))
    assert (ids.status_code, ids.json().get("member_count")) == (200, 2), ids.text

    # The schedule is kept, with no owner and both of its stages.
    kept = client.get(f"{SCHEDULES}/{schedule_id}", headers=_auth(superuser))
    assert kept.status_code == 200, kept.text
    assert kept.json()["owner_id"] is None
    assert {stage["id"] for stage in kept.json()["stages"]} == stage_ids
    assert db_session.get(RolloutSchedule, uuid.UUID(schedule_id)).owner_id is None
    assert (
        db_session.query(RolloutStage)
        .filter(RolloutStage.rollout_schedule_id == uuid.UUID(schedule_id))
        .count()
        == 2
    )

    after = _evaluate(client, flag_key, sdk_key, f"keeps-{uuid.uuid4().hex[:8]}")
    assert after.status_code == 200, after.text
    assert (after.json()["enabled"], after.json()["reason"]) == (
        True,
        "targeting_rule",
    )
    newcomer = _assign(client, experiment_key, sdk_key, f"keeps-{uuid.uuid4().hex[:8]}")
    assert newcomer["assigned"] is True, newcomer

    # The account's API key was removed with it.
    assert _evaluate(client, flag_key, developer_key, "keeps-probe").status_code == 401
    assert (
        db_session.query(APIKey).filter(APIKey.user_id == developer_uuid).count() == 0
    )
