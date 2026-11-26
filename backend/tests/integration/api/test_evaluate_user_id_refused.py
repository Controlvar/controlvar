"""The evaluate routes refuse a user_id the evaluation cannot record (#2176).

``raw_metrics.user_id`` holds at most 255 characters. A longer id used to be
evaluated, fail to be recorded and answer 500 with a production key, and 200
with a staging or development key or from ``/user`` when no flag is active. It
now answers 422 at the parameter on every route and in every environment, and
nothing is evaluated or recorded. A NUL in the id is refused the same way.

Over the real routes and a real database, with real keys and
``deps.get_api_key`` not replaced.
"""

from __future__ import annotations

import uuid
from typing import Dict

import pytest

from backend.app.api import deps
from backend.app.main import app
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.metrics.metric import RawMetric
from backend.app.models.user import UserRole
from backend.tests.integration.api.route_helpers import api_client, make_user
from backend.tests.integration.key_environments import environment_key

pytestmark = [pytest.mark.integration, pytest.mark.requires_db, pytest.mark.regression]

FLAGS = "/api/v1/feature-flags"
ENVIRONMENTS = ("production", "staging", "development")
MARK = "zq2176mark"
LONG = "u" * 256
OK = "u" * 255
#: Where each route takes the id: GET evaluate, POST evaluate, GET /user.
WHERE = {"get": "query", "post": "body", "user": "path"}
ROUTES = list(WHERE)


def _too_long(where: str) -> dict:
    return {
        "detail": [
            {
                "type": "string_too_long",
                "loc": [where, "user_id"],
                "msg": "String should have at most 255 characters",
                "ctx": {"max_length": 255},
            }
        ]
    }


@pytest.fixture(autouse=True)
def _real_keys():
    assert deps.get_api_key not in app.dependency_overrides


@pytest.fixture
def client(db_session):
    with api_client(db_session) as c:
        yield c


@pytest.fixture
def owner(db_session):
    return make_user(db_session, UserRole.DEVELOPER, tag="uid2176")


@pytest.fixture
def keys(db_session, owner) -> Dict[str, Dict[str, str]]:
    """The ``X-API-Key`` header of a key of each environment, and none."""
    made = {
        environment: environment_key(db_session, owner, environment)
        for environment in ENVIRONMENTS
    }
    return made | {"none": {}}


@pytest.fixture
def flag(db_session, owner):
    """A flag on at 100% in production; removed with its metric rows."""
    created = FeatureFlag(
        key=f"uid2176-{uuid.uuid4().hex[:10]}",
        name="Long user id flag",
        status=FeatureFlagStatus.ACTIVE,
        owner_id=owner.id,
        rollout_percentage=100,
    )
    db_session.add(created)
    db_session.commit()
    yield created
    db_session.rollback()
    db_session.query(RawMetric).filter(RawMetric.feature_flag_id == created.id).delete()
    db_session.query(FeatureFlag).filter(FeatureFlag.id == created.id).delete()
    db_session.commit()


def _recorded(db_session, flag) -> int:
    """The flag's metric rows: a refused id never equals a stored one, so the
    rows are counted by flag, not by user."""
    db_session.rollback()
    return (
        db_session.query(RawMetric).filter(RawMetric.feature_flag_id == flag.id).count()
    )


def _call(client, keys, environment: str, route: str, key: str, user_id: str):
    headers = keys[environment]
    if route == "get":
        return client.get(
            f"{FLAGS}/evaluate/{key}", params={"user_id": user_id}, headers=headers
        )
    if route == "post":
        return client.post(
            f"{FLAGS}/evaluate/{key}", json={"user_id": user_id}, headers=headers
        )
    return client.get(f"{FLAGS}/user/{user_id}", headers=headers)


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_an_id_over_255_characters_answers_422_and_nothing_is_recorded(
    client, keys, db_session, flag, route, environment
):
    before = _recorded(db_session, flag)
    response = _call(client, keys, environment, route, flag.key, LONG)
    assert response.status_code == 422, response.text
    assert response.json() == _too_long(WHERE[route])
    assert _recorded(db_session, flag) == before


@pytest.mark.parametrize("route", ROUTES)
def test_an_id_of_255_characters_is_answered_and_recorded(
    client, keys, db_session, flag, route
):
    before = _recorded(db_session, flag)
    response = _call(client, keys, "production", route, flag.key, OK)
    assert response.status_code == 200, response.text
    assert _recorded(db_session, flag) == before + 2


@pytest.mark.parametrize("route", ROUTES)
def test_characters_are_counted_not_bytes(client, keys, flag, route):
    answered = _call(client, keys, "production", route, flag.key, "é" * 255)
    assert answered.status_code == 200, answered.text
    response = _call(client, keys, "production", route, flag.key, "é" * 256)
    assert response.json() == _too_long(WHERE[route])


def test_user_flags_with_no_active_flag_answers_422(client, keys, monkeypatch):
    """It used to answer 200 with an empty body: nothing was evaluated."""
    monkeypatch.setattr(
        "backend.app.services.feature_flag_service.flags_on_in", lambda *_: []
    )
    response = _call(client, keys, "production", "user", "unused", LONG)
    assert response.json() == _too_long("path")
    assert _call(client, keys, "production", "user", "unused", OK).json() == {}


@pytest.mark.parametrize("route", ["get", "post"])
@pytest.mark.parametrize("environment", ["production", "staging"])
def test_a_nul_in_the_id_answers_422_without_the_value(
    client, keys, db_session, flag, route, environment
):
    before = _recorded(db_session, flag)
    response = _call(client, keys, environment, route, flag.key, MARK + "\x00")
    assert response.status_code == 422, response.text
    (error,) = response.json()["detail"]
    assert error["loc"] == [WHERE[route], "user_id"]
    assert error["msg"] == (
        "Value error, user_id must not contain NUL or unpaired surrogate characters"
    )
    assert MARK not in response.text
    assert _recorded(db_session, flag) == before


@pytest.mark.parametrize("route", ROUTES)
def test_the_key_is_still_checked_first(client, keys, flag, route):
    assert _call(client, keys, "none", route, flag.key, LONG).status_code == 401


@pytest.mark.parametrize("route", ["get", "post"])
def test_the_422_comes_before_an_unknown_flag_key(client, keys, route):
    unknown = f"nosuch-{uuid.uuid4().hex[:10]}"
    response = _call(client, keys, "production", route, unknown, LONG)
    assert response.json() == _too_long(WHERE[route])
