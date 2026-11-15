"""A rollout stage order above the bound answers 422 naming the field.

``stage_order`` had a lower bound only. Creating a schedule whose stage orders
included one above ``sys.maxsize`` answered 500, and adding or updating a
stage with an order the 32-bit column cannot hold reached the database. Every
request model now bounds the order at ``MAX_STAGE_ORDER`` (1000): create,
add-stage and update-stage answer 422 naming the field, and nothing is
written.

A stored order can still pass the bound (adding a stage in the middle of a
schedule moves later stages up by one), and such a schedule still reads back.

Each request is a real one: a user with a local JWT and no dependency override
except ``deps.get_db``. The schema-level properties are pinned by
``backend/tests/unit/schemas/test_rollout_stage_order_bounds.py``.
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
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.rollout_schedule import RolloutSchedule, RolloutStage
from backend.app.models.user import User, UserRole
from backend.app.schemas.rollout_schedule import MAX_STAGE_ORDER
from backend.tests.integration.api.rollout_rules import rollout_invariants

# Gate 3e (#1460): after every test, no schedule it wrote breaks a rule.
pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_db,
    pytest.mark.usefixtures("rollout_invariants"),
]

SCHEDULES = "/api/v1/rollout-schedules"
PREFIX = "stageorder"


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
def make_user(fresh):
    def make(role: UserRole) -> User:
        suffix = uuid.uuid4().hex[:8]
        user = User(
            username=f"{PREFIX}_{role.value}_{suffix}",
            email=f"{PREFIX}_{role.value}_{suffix}@bounds.test",
            full_name=f"Stage Order {role.value}",
            hashed_password="unused: this user signs in by token only",
            is_active=True,
            is_superuser=False,
            role=role,
        )

        def work(session):
            session.add(user)
            return user

        return fresh(work)

    return make


@pytest.fixture
def developer(make_user):
    return make_user(UserRole.DEVELOPER)


@pytest.fixture
def flag_id(fresh) -> uuid.UUID:
    key = f"{PREFIX}-{uuid.uuid4().hex[:8]}"

    def work(session):
        flag = FeatureFlag(
            key=key,
            name=key,
            status=FeatureFlagStatus.INACTIVE,
            rollout_percentage=0,
        )
        session.add(flag)
        session.flush()
        return flag.id

    return fresh(work)


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


def _send(client, method: str, url: str, user, body: dict | None = None):
    headers = {"Authorization": f"Bearer {create_local_access_token(user)}"}
    if body is None:
        return client.request(method, url, headers=headers)
    headers["Content-Type"] = "application/json"
    return client.request(method, url, content=json.dumps(body), headers=headers)


def _stage(order: int, percentage: int = 10, name: str | None = None) -> dict:
    return {
        "name": name or f"Stage {order}",
        "stage_order": order,
        "target_percentage": percentage,
        "trigger_type": "manual",
    }


def _schedule(name: str, flag, *orders: int) -> dict:
    return {
        "name": name,
        "feature_flag_id": str(flag),
        "stages": [_stage(order) for order in orders],
    }


def _assert_refused_for_stage_order(response) -> None:
    assert response.status_code == 422, response.text
    errors = response.json()["detail"]
    assert errors and all(error["loc"][-1] == "stage_order" for error in errors), errors
    assert {error["ctx"]["le"] for error in errors} == {MAX_STAGE_ORDER}


def _schedules_named(fresh, name: str) -> int:
    return fresh(
        lambda session: (
            session.query(RolloutSchedule).filter(RolloutSchedule.name == name).count()
        )
    )


@pytest.mark.regression
@pytest.mark.parametrize("role", [UserRole.DEVELOPER], ids=lambda role: role.value)
@pytest.mark.parametrize(
    "orders", [(0, 10**20), (0, MAX_STAGE_ORDER + 1)], ids=["1e20", "bound+1"]
)
def test_create_with_a_stage_order_above_the_bound_answers_422(
    client, make_user, flag_id, fresh, role, orders
):
    # Before: (0, 10**20) answered 500; (0, 1001) answered 422 for the gap,
    # not for the order.
    user = make_user(role)
    name = f"{PREFIX} create {uuid.uuid4().hex[:8]}"
    response = _send(
        client, "POST", f"{SCHEDULES}/", user, _schedule(name, flag_id, *orders)
    )
    _assert_refused_for_stage_order(response)
    assert _schedules_named(fresh, name) == 0


@pytest.fixture
def schedule(client, developer, flag_id) -> dict:
    name = f"{PREFIX} schedule {uuid.uuid4().hex[:8]}"
    response = _send(
        client, "POST", f"{SCHEDULES}/", developer, _schedule(name, flag_id, 1, 2)
    )
    assert response.status_code == 201, response.text
    return response.json()


def _stored_orders(fresh, schedule_id) -> list[int]:
    return fresh(
        lambda session: [
            stage.stage_order
            for stage in session.query(RolloutStage)
            .filter(RolloutStage.rollout_schedule_id == uuid.UUID(schedule_id))
            .order_by(RolloutStage.stage_order)
        ]
    )


@pytest.mark.regression
@pytest.mark.parametrize("order", [MAX_STAGE_ORDER + 1, 2**31, 10**20])
def test_adding_a_stage_above_the_bound_answers_422(
    client, developer, schedule, fresh, order
):
    # Before: the stage reached the database (a 1001 was stored; 2**31 and
    # larger do not fit the column and answered 500).
    response = _send(
        client,
        "POST",
        f"{SCHEDULES}/{schedule['id']}/stages",
        developer,
        _stage(order, percentage=100),
    )
    _assert_refused_for_stage_order(response)
    assert _stored_orders(fresh, schedule["id"]) == [1, 2]


@pytest.mark.regression
@pytest.mark.parametrize("order", [MAX_STAGE_ORDER + 1, 2**31, 10**20])
def test_updating_a_stage_above_the_bound_answers_422(
    client, developer, schedule, fresh, order
):
    stage_id = next(s["id"] for s in schedule["stages"] if s["stage_order"] == 2)
    response = _send(
        client,
        "PUT",
        f"{SCHEDULES}/stages/{stage_id}",
        developer,
        {"stage_order": order},
    )
    _assert_refused_for_stage_order(response)
    assert _stored_orders(fresh, schedule["id"]) == [1, 2]


def test_orders_up_to_the_bound_are_accepted_and_a_shift_past_it_reads_back(
    client, developer, flag_id, fresh
):
    # A schedule whose only stage is at the bound, then a stage added at the
    # same order: the first moves up to bound + 1, past what a request may set,
    # and the schedule still reads back with both.
    name = f"{PREFIX} shift {uuid.uuid4().hex[:8]}"
    created = _send(
        client,
        "POST",
        f"{SCHEDULES}/",
        developer,
        _schedule(name, flag_id, MAX_STAGE_ORDER),
    )
    assert created.status_code == 201, created.text
    schedule_id = created.json()["id"]

    added = _send(
        client,
        "POST",
        f"{SCHEDULES}/{schedule_id}/stages",
        developer,
        _stage(MAX_STAGE_ORDER, percentage=10, name="Inserted"),
    )
    assert added.status_code == 201, added.text
    assert _stored_orders(fresh, schedule_id) == [MAX_STAGE_ORDER, MAX_STAGE_ORDER + 1]

    read = _send(client, "GET", f"{SCHEDULES}/{schedule_id}", developer)
    assert read.status_code == 200, read.text
    assert sorted(stage["stage_order"] for stage in read.json()["stages"]) == [
        MAX_STAGE_ORDER,
        MAX_STAGE_ORDER + 1,
    ]
