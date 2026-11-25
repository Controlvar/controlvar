"""A manual rollback changes production only (#2011).

The guide, ``docs/feature-flags/environments.md``, and the safety page say a
rollback, automatic or manual, changes the flag's production settings and
leaves its staging and development settings as they were.
``test_environment_safety.py`` holds the automatic rollback; this holds
``POST /api/v1/safety/feature-flags/{id}/rollback``, which a superuser calls.
The flag is on in all three environments, and each answer is read through a
real key of each.

Each test checks the environment it changes and the others it does not.
"""

from __future__ import annotations

import uuid
from typing import Dict

import pytest

from backend.app.api import deps
from backend.app.main import app
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.user import UserRole
from backend.tests.integration.api.route_helpers import (
    api_client,
    auth,
    local_sign_in,
    make_user,
)
from backend.tests.integration.key_environments import configure, environment_key

pytestmark = [pytest.mark.integration, pytest.mark.requires_db, pytest.mark.regression]

FLAGS = "/api/v1/feature-flags"
ENVIRONMENTS = ("production", "staging", "development")


@pytest.fixture(autouse=True)
def _local(monkeypatch):
    local_sign_in(monkeypatch)
    assert deps.get_api_key not in app.dependency_overrides


@pytest.fixture
def client(db_session):
    with api_client(db_session) as c:
        yield c


def _answer(client, headers, flag_key: str) -> list:
    answer = client.get(
        f"{FLAGS}/evaluate/{flag_key}", params={"user_id": "u-1"}, headers=headers
    )
    assert answer.status_code == 200, answer.text
    body = answer.json()
    return [body["enabled"], body["reason"]]


@pytest.mark.parametrize("percentage", [0, 20])
def test_a_manual_rollback_changes_production_and_neither_other_environment(
    client, db_session, percentage
):
    admin = make_user(db_session, UserRole.ADMIN, superuser=True, tag="envrollback")
    keys: Dict[str, Dict[str, str]] = {
        environment: environment_key(db_session, admin, environment)
        for environment in ENVIRONMENTS
    }
    flag = FeatureFlag(
        key=f"env-{uuid.uuid4().hex[:10]}",
        name="Rolled back",
        status=FeatureFlagStatus.ACTIVE,
        owner_id=admin.id,
        rollout_percentage=100,
    )
    db_session.add(flag)
    db_session.commit()
    for environment in ("staging", "development"):
        configure(db_session, flag, environment, rollout_percentage=100)
    on = [True, "rollout"]
    for environment in ENVIRONMENTS:
        assert _answer(client, keys[environment], flag.key) == on, environment

    rolled = client.post(
        f"/api/v1/safety/feature-flags/{flag.id}/rollback",
        params={"percentage": percentage, "reason": "checking the guide"},
        headers=auth(admin),
    )
    assert rolled.status_code == 200, rolled.text

    # Production changed: off at 0%, and at 20% the user of this test is outside it.
    if percentage == 0:
        assert _answer(client, keys["production"], flag.key) == [False, "inactive"]
    else:
        production = _answer(client, keys["production"], flag.key)
        assert production[1] == "rollout"
    # Neither of the others did.
    for environment in ("staging", "development"):
        assert _answer(client, keys[environment], flag.key) == on, environment
    settings = client.get(f"{FLAGS}/{flag.id}/environments", headers=auth(admin))
    by_environment = {
        entry["environment"]: (entry["is_active"], entry["rollout_percentage"])
        for entry in settings.json()["environments"]
    }
    assert by_environment["staging"] == (True, 100)
    assert by_environment["development"] == (True, 100)
    assert by_environment["production"] == (percentage != 0, percentage)
