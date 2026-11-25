"""A flag the previous release archived, and then unarchived by this one (#2011).

During a rolling upgrade, and after a rollback of the API alone, a task still
running 0.48.0 archives a flag the way it always did: it writes the flag row's
status and nothing else. It does not know the flag has staging and development
settings, so it leaves them on. The flag then:

* answers off in every environment while it is archived, because an archived
  flag row wins over any setting;
* answers on again in staging once this release unarchives it, because the
  setting it left on was never turned off. Production, which was off when the
  flag was archived, stays off.

This release's own archive (bulk toggle) turns those settings off in the same
transaction, so the flag comes back off everywhere (``test_flag_environments_api``
holds that). The documentation says the first path in
``docs/feature-flags/environments.md``.

Each test checks the environment it changes and the other one it does not.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict

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


@pytest.fixture
def owner(db_session):
    return make_user(db_session, UserRole.DEVELOPER, tag="envarchive")


@pytest.fixture
def keys(db_session, owner) -> Dict[str, Dict[str, str]]:
    return {
        environment: environment_key(db_session, owner, environment)
        for environment in ENVIRONMENTS
    }


def _answer(client, headers, flag_key: str) -> list:
    answer = client.get(
        f"{FLAGS}/evaluate/{flag_key}", params={"user_id": "u-1"}, headers=headers
    )
    assert answer.status_code == 200, answer.text
    body: Dict[str, Any] = answer.json()
    return [body["enabled"], body["reason"]]


def test_a_flag_the_previous_release_archived_comes_back_on_in_staging_when_unarchived(
    client, db_session, keys, owner
):
    # Off in production, on in staging and development: where the flag is when
    # a task still on 0.48.0 archives it.
    flag = FeatureFlag(
        key=f"env-{uuid.uuid4().hex[:10]}",
        name="Archived by the previous release",
        status=FeatureFlagStatus.INACTIVE,
        owner_id=owner.id,
        rollout_percentage=100,
    )
    db_session.add(flag)
    db_session.commit()
    configure(db_session, flag, "staging")
    off = [False, "inactive"]
    assert _answer(client, keys["production"], flag.key) == off
    assert _answer(client, keys["staging"], flag.key) == [True, "rollout"]

    # What 0.48.0's archive writes: the flag row's status, and no setting.
    flag.status = FeatureFlagStatus.ARCHIVED
    db_session.commit()
    for environment in ENVIRONMENTS:
        assert _answer(client, keys[environment], flag.key) == off, environment

    unarchived = client.post(f"{FLAGS}/{flag.id}/unarchive", headers=auth(owner))
    assert unarchived.status_code == 200, unarchived.text
    # Staging changed, because its setting was left on; production, and
    # development (never configured), did not.
    assert _answer(client, keys["staging"], flag.key) == [True, "rollout"]
    assert _answer(client, keys["production"], flag.key) == off
    assert _answer(client, keys["development"], flag.key) == off
