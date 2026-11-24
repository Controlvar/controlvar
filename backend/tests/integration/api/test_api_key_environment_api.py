"""``POST /api/v1/api-keys`` makes a key for a flag environment (#2011).

Real requests on PostgreSQL, signed in with a local JWT; ``deps.get_api_key``
is not replaced, so a created key is answered as the SDK routes answer it:

* with no ``environment`` the key is a production key, and the answer is the
  one the previous release gave plus the new field;
* ``staging`` and ``development`` are stored, answered, listed and audited;
  any other value, ``null`` included, is the standard 422, and nothing is
  written;
* ``mcp:read`` outside production is 422 and writes nothing, in either
  environment and whatever shape the scopes are sent in; a production key
  still takes it;
* who may create a key, and a key with ``sdk:ruleset``, does not depend on the
  environment;
* a staging key made through the route is answered staging by evaluation and
  403 by tracking, and the production key beside it is answered as before;
* an error report with such a key is stored and is in neither error number,
  on the metrics summary and on the safety check;
* an ADMIN finds and deletes another user's staging key with the two routes
  the rollback runbook names.

Every test that makes a key outside production also checks a production key,
and what it left alone.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pytest

from backend.app.api import deps
from backend.app.main import app
from backend.app.models.api_key import APIKey
from backend.app.models.audit_log import AuditLog
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.metrics.metric import (
    AggregatedMetric,
    AggregationPeriod,
    ErrorLog,
    MetricType,
)
from backend.app.models.user import User, UserRole
from backend.app.services.safety_service import SafetyService
from backend.tests.integration.api.route_helpers import (
    api_client,
    auth,
    local_sign_in,
    make_user,
)
from backend.tests.integration.identity_alias_world import World, no_active_holdout
from backend.tests.integration.key_environments import configure

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

KEYS = "/api/v1/api-keys"
FLAGS = "/api/v1/feature-flags"
ASSIGN = "/api/v1/tracking/assign"

OUTSIDE_PRODUCTION = ("staging", "development")

#: What the previous release's create answered, field for field.
CREATED_BEFORE = {"id", "name", "key", "prefix", "created_at", "expires_at"}
#: What the previous release's list answered for one key.
LISTED_BEFORE = {
    "id",
    "name",
    "description",
    "scopes",
    "is_active",
    "user_id",
    "created_at",
    "expires_at",
    "last_used_at",
}

#: The standard 422 for a value that is not an environment.
NOT_AN_ENVIRONMENT = "Input should be 'production', 'staging' or 'development'"
#: The refusal of ``mcp:read`` outside production, word for word.
MCP_READ_REFUSED = (
    "Value error, mcp:read needs a production key: the routes it opens read "
    "production only. Create the key for production, or without mcp:read."
)
#: The 403 a key outside production gets on a tracking route.
TRACKING_REFUSED = (
    "This API key belongs to the {environment} environment. Experiments and event "
    "tracking run in production only; use a production key."
)


@pytest.fixture(autouse=True)
def _local(monkeypatch):
    local_sign_in(monkeypatch)
    assert deps.get_api_key not in app.dependency_overrides


@pytest.fixture
def client(db_session):
    with api_client(db_session) as c:
        yield c


@pytest.fixture
def developer(db_session) -> User:
    return make_user(db_session, UserRole.DEVELOPER, tag="keyenv")


def _post(client, user: User, **body: Any):
    return client.post(
        KEYS, json={"name": f"k-{uuid.uuid4().hex[:8]}", **body}, headers=auth(user)
    )


def _created(client, user: User, **body: Any) -> Dict[str, Any]:
    response = _post(client, user, **body)
    assert response.status_code == 201, response.text
    return response.json()


def _listed(client, user: User, **query: Any) -> Dict[str, Dict[str, Any]]:
    response = client.get(KEYS, params=query, headers=auth(user))
    assert response.status_code == 200, response.text
    return {item["id"]: item for item in response.json()}


def _rows(db, user: User) -> List[Tuple]:
    """Every key of *user* as stored: what a refusal must leave alone."""
    db.expire_all()
    return sorted(
        (str(k.id), k.name, k.environment, k.scopes, k.is_active, k.key)
        for k in db.query(APIKey).filter(APIKey.user_id == user.id)
    )


def _entries(db, user: User) -> List[Tuple]:
    db.expire_all()
    return sorted(
        (e.action_type, str(e.entity_id), e.old_value, e.new_value)
        for e in db.query(AuditLog).filter(AuditLog.user_id == user.id)
    )


def _entry(db, action: str, key_id: str) -> AuditLog:
    db.expire_all()
    (entry,) = (
        db.query(AuditLog)
        .filter(AuditLog.action_type == action, AuditLog.entity_id == uuid.UUID(key_id))
        .all()
    )
    return entry


# ---------------------------------------------------------------------------
# The default
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_a_create_that_names_no_environment_is_answered_as_before_plus_the_field(
    client, db_session, developer
):
    created = _created(client, developer, name="no environment")
    assert set(created) - {"environment"} == CREATED_BEFORE
    assert created["environment"] == "production"

    item = _listed(client, developer)[created["id"]]
    assert set(item) - {"environment"} == LISTED_BEFORE
    assert item["environment"] == "production"

    (row,) = _rows(db_session, developer)
    assert row[2] == "production"
    recorded = json.loads(_entry(db_session, "api_key_create", created["id"]).new_value)
    assert recorded["environment"] == "production"


# ---------------------------------------------------------------------------
# Choosing one
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("environment", OUTSIDE_PRODUCTION)
def test_the_environment_is_stored_answered_listed_and_audited(
    client, db_session, developer, environment
):
    production = _created(client, developer, environment="production")
    before = _rows(db_session, developer)

    created = _created(client, developer, environment=environment)
    assert set(created) == CREATED_BEFORE | {"environment"}
    assert created["environment"] == environment

    listed = _listed(client, developer)
    assert listed[created["id"]]["environment"] == environment
    assert listed[production["id"]]["environment"] == "production"

    # The row says so, and the production key's row is what it was.
    after = _rows(db_session, developer)
    (added,) = [row for row in after if row not in before]
    assert (added[0], added[2]) == (created["id"], environment)
    assert [row for row in after if row in before] == before

    recorded = _entry(db_session, "api_key_create", created["id"])
    assert json.loads(recorded.new_value)["environment"] == environment
    assert created["key"] not in recorded.new_value

    deleted = client.delete(f"{KEYS}/{created['id']}", headers=auth(developer))
    assert deleted.status_code == 204, deleted.text
    revoked = _entry(db_session, "api_key_revoke", created["id"])
    assert json.loads(revoked.old_value)["environment"] == environment
    assert _rows(db_session, developer) == before


@pytest.mark.parametrize(
    "sent",
    [
        pytest.param("Production", id="capitalised"),
        pytest.param("prod", id="prod"),
        pytest.param("dev", id="dev"),
        pytest.param("qa", id="qa"),
        pytest.param("", id="empty"),
        pytest.param(None, id="null"),
    ],
)
def test_any_other_value_is_the_standard_422_and_writes_nothing(
    client, db_session, developer, sent
):
    _created(client, developer, environment="staging")
    rows, entries = _rows(db_session, developer), _entries(db_session, developer)

    response = _post(client, developer, environment=sent)
    assert response.status_code == 422, response.text
    (item,) = response.json()["detail"]
    assert item["loc"] == ["body", "environment"]
    assert item["type"] == "literal_error"
    assert item["msg"] == NOT_AN_ENVIRONMENT

    assert _rows(db_session, developer) == rows
    assert _entries(db_session, developer) == entries


# ---------------------------------------------------------------------------
# mcp:read
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("environment", OUTSIDE_PRODUCTION)
@pytest.mark.parametrize(
    "scopes",
    [
        pytest.param(["mcp:read"], id="list"),
        pytest.param(["sdk:ruleset", " mcp:read "], id="beside-another"),
        pytest.param("read, mcp:read", id="comma-string"),
    ],
)
def test_mcp_read_outside_production_is_refused_and_writes_nothing(
    client, db_session, developer, environment, scopes
):
    rows, entries = _rows(db_session, developer), _entries(db_session, developer)

    response = _post(client, developer, environment=environment, scopes=scopes)
    assert response.status_code == 422, response.text
    (item,) = response.json()["detail"]
    assert item["loc"] == ["body", "scopes"]
    assert item["type"] == "value_error"
    assert item["msg"] == MCP_READ_REFUSED

    assert _rows(db_session, developer) == rows
    assert _entries(db_session, developer) == entries

    # The same scopes for production: 201, and the key reads.
    created = _created(client, developer, environment="production", scopes=scopes)
    assert created["environment"] == "production"
    assert "mcp:read" in _listed(client, developer)[created["id"]]["scopes"]
    read = client.get(f"{FLAGS}/", headers={"X-API-Key": created["key"]})
    assert read.status_code == 200, read.text


def test_mcp_read_with_no_environment_named_is_accepted_as_before(client, developer):
    created = _created(client, developer, scopes=["mcp:read"])
    assert created["environment"] == "production"


# ---------------------------------------------------------------------------
# Who may create a key
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("environment", ["production", *OUTSIDE_PRODUCTION])
@pytest.mark.parametrize("role", list(UserRole), ids=lambda role: role.name)
def test_every_role_creates_a_key_for_every_environment(
    client, db_session, role, environment
):
    user = make_user(db_session, role, tag="keyenv")
    assert _created(client, user, environment=environment)["environment"] == (
        environment
    )


@pytest.mark.parametrize("environment", ["production", *OUTSIDE_PRODUCTION])
@pytest.mark.parametrize(
    ("role", "status"),
    [
        (UserRole.ADMIN, 201),
        (UserRole.DEVELOPER, 201),
        (UserRole.ANALYST, 403),
        (UserRole.VIEWER, 403),
    ],
    ids=lambda value: getattr(value, "name", str(value)),
)
def test_the_sdk_ruleset_rule_is_the_same_in_every_environment(
    client, db_session, role, status, environment
):
    user = make_user(db_session, role, tag="keyenv")
    response = _post(client, user, environment=environment, scopes=["sdk:ruleset"])
    assert response.status_code == status, response.text
    assert len(_rows(db_session, user)) == (1 if status == 201 else 0)


# ---------------------------------------------------------------------------
# What the created key is answered
# ---------------------------------------------------------------------------


def _evaluated(client, key: Dict[str, Any], flag: FeatureFlag) -> Tuple:
    response = client.get(
        f"{FLAGS}/evaluate/{flag.key}",
        params={"user_id": "u-1"},
        headers={"X-API-Key": key["key"]},
    )
    assert response.status_code == 200, response.text
    return response.json()["enabled"], response.json()["reason"]


def _assigned(client, key: Dict[str, Any], experiment, user_id: str):
    return client.post(
        ASSIGN,
        json={"experiment_key": experiment.key, "user_id": user_id},
        headers={"X-API-Key": key["key"]},
    )


@pytest.mark.parametrize("environment", OUTSIDE_PRODUCTION)
def test_a_created_key_is_answered_its_environment_and_refused_by_tracking(
    client, db_session, developer, make_feature_flag, make_experiment, environment
):
    """Both keys are made by the route. The flags differ between the two
    environments both ways round, so a key read as the other's is seen."""
    on_in_production = make_feature_flag(
        key=f"created-prod-{uuid.uuid4().hex[:8]}",
        status=FeatureFlagStatus.ACTIVE,
        rollout_percentage=100,
    )
    on_there = make_feature_flag(key=f"created-there-{uuid.uuid4().hex[:8]}")
    configure(db_session, on_there, environment, is_active=True)
    world = World(db_session, make_experiment)
    production = _created(client, developer)
    try:
        with no_active_holdout(db_session):
            experiment = world.experiment("Created keys")
            before = (
                _evaluated(client, production, on_in_production),
                _evaluated(client, production, on_there),
                _assigned(client, production, experiment, world.id("p")).status_code,
            )
            assert before == ((True, "rollout"), (False, "inactive"), 200)

            key = _created(client, developer, environment=environment)
            assert _evaluated(client, key, on_in_production) == (False, "inactive")
            assert _evaluated(client, key, on_there) == (True, "rollout")
            refused = _assigned(client, key, experiment, world.id("s"))
            assert refused.status_code == 403, refused.text
            assert refused.json()["detail"] == TRACKING_REFUSED.format(
                environment=environment
            )

            # The production key beside it is answered what it was.
            after = (
                _evaluated(client, production, on_in_production),
                _evaluated(client, production, on_there),
                _assigned(client, production, experiment, world.id("p")).status_code,
            )
            assert after == before
    finally:
        world.close()


def _error_numbers(client, db, user: User, flag: FeatureFlag) -> Tuple:
    """The flag's numbers on the metrics summary and on the safety check: the
    summary's rate and uncounted reports, then the check's two counts."""
    response = client.get(
        "/api/v1/metrics/summary",
        params={"feature_flag_id": str(flag.id)},
        headers=auth(user),
    )
    assert response.status_code == 200, response.text
    summary = response.json()
    db.expire_all()
    check = SafetyService(db).get_error_metrics(db, flag.id)
    return (
        summary["error_rate"],
        summary["uncounted_error_count"],
        check["error_count"],
        check["uncounted_error_count"],
    )


@pytest.mark.parametrize("environment", OUTSIDE_PRODUCTION)
def test_a_created_keys_errors_outside_production_are_in_neither_number(
    client, db_session, developer, make_feature_flag, environment
):
    """``uncounted_error_count`` is production's reports that do not count. A
    report with a staging or development key, server-side or not, is stored
    and is in neither number, on the summary and on the check."""
    flag = make_feature_flag(
        key=f"reports-{uuid.uuid4().hex[:8]}",
        status=FeatureFlagStatus.ACTIVE,
        rollout_percentage=100,
    )
    db_session.add(
        AggregatedMetric(
            metric_type=MetricType.FLAG_EVALUATION.value,
            period=AggregationPeriod.DAY.value,
            period_start=datetime.utcnow() - timedelta(minutes=1),
            feature_flag_id=flag.id,
            count=100,
        )
    )
    db_session.commit()

    def report(**body: Any) -> None:
        key = _created(client, developer, **body)
        response = client.post(
            "/api/v1/tracking/errors",
            json={
                "feature_flag_key": flag.key,
                "error_type": "crash",
                "message": "raised while the flag was on",
            },
            headers={"X-API-Key": key["key"]},
        )
        assert response.status_code == 201, response.text

    report(scopes=["sdk:ruleset"])
    report()
    production = _error_numbers(client, db_session, developer, flag)
    assert production == (1.0, 1, 1, 1)

    report(environment=environment, scopes=["sdk:ruleset"])
    report(environment=environment)
    assert _error_numbers(client, db_session, developer, flag) == production
    stored = db_session.query(ErrorLog).filter(ErrorLog.feature_flag_id == flag.id)
    assert stored.count() == 4


# ---------------------------------------------------------------------------
# The rollback runbook's step
# ---------------------------------------------------------------------------


def _admin(db) -> User:
    """An ADMIN who is not a superuser: the role alone gives both routes."""
    return make_user(db, UserRole.ADMIN, superuser=False, tag="keyenvadmin")


def _status(client, key: Dict[str, Any], flag: FeatureFlag) -> int:
    return client.get(
        f"{FLAGS}/evaluate/{flag.key}",
        params={"user_id": "u-1"},
        headers={"X-API-Key": key["key"]},
    ).status_code


def _refused_to_list(client, user: Optional[User]) -> int:
    headers = {} if user is None else auth(user)
    return client.get(KEYS, params={"all": "true"}, headers=headers).status_code


def test_an_admin_lists_every_users_keys_and_deletes_the_ones_outside_production(
    client, db_session, developer, make_feature_flag
):
    flag = make_feature_flag(
        key=f"runbook-{uuid.uuid4().hex[:8]}",
        status=FeatureFlagStatus.ACTIVE,
        rollout_percentage=100,
    )
    admin = _admin(db_session)
    made = {
        environment: _created(client, developer, environment=environment)
        for environment in ("production", *OUTSIDE_PRODUCTION)
    }
    assert _refused_to_list(client, developer) == 403
    assert _refused_to_list(client, None) == 401

    listed = _listed(client, admin, all="true")
    for environment, key in made.items():
        item = listed[key["id"]]
        assert item["environment"] == environment
        assert item["user_id"] == str(developer.id)
        assert item["created_at"] == key["created_at"]

    for item in listed.values():
        if item["environment"] != "production" and item["user_id"] == str(developer.id):
            deleted = client.delete(f"{KEYS}/{item['id']}", headers=auth(admin))
            assert deleted.status_code == 204, deleted.text

    assert {
        environment: _status(client, key, flag) for environment, key in made.items()
    } == {"production": 200, "staging": 401, "development": 401}
    assert [row[2] for row in _rows(db_session, developer)] == ["production"]
