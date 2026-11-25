"""A flag's settings per environment through the API (#2011, beta).

The real routes on Postgres, signed in with local tokens as non-superusers,
read back in a session of the test's own:

* ``GET /environments``, and one flag's or many flags' settings in an
  environment: production's are the flag's own; with no side row a flag is off
  in staging and development; an archived flag is archived in every one;
* ``PUT /feature-flags/{id}/environments/{environment}`` changes the fields
  sent, in that environment only, and refuses what a flag update refuses. A
  change to staging or development does not move ``feature_flags.updated_at``;
  a change to production is the one ``PUT /feature-flags/{id}`` makes;
* ``POST .../environments/{target}/promote`` copies exactly the per-environment
  fields, all together, and nothing else;
* every change writes its audit entry in its own transaction, under the flag
  row's lock, and a change that cannot be recorded is not made;
* the readers of a flag's rules and state see the side rows: segments in use,
  stale flags, bulk archive, flag delete. Rollout schedules and the production
  ruleset do not;
* a change answers through an API key of its environment, on
  ``/feature-flags/evaluate/{key}`` and on a ruleset poll, and the keys of the
  other two are answered as before it.

Each test of a change asserts the other environments too.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event as sa_event
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.api.v1.endpoints import flag_environments as routes
from backend.app.api.v1.endpoints.auth import create_local_access_token
from backend.app.core.config import settings
from backend.app.core.flag_environments import (
    FLAG_ENVIRONMENTS,
    NON_PRODUCTION,
    SETTING_IDENTITY_COLUMNS,
    flag_in_environment,
)
from backend.app.core.security import hash_api_key
from backend.app.main import app
from backend.app.models.api_key import APIKey, generate_api_key
from backend.app.models.audit_log import AuditLog
from backend.app.models.compliance_audit_event import ComplianceAuditEvent
from backend.app.models.feature_flag import (
    ARCHIVED_FLAG_DETAIL,
    FeatureFlag,
    FeatureFlagStatus,
    FlagEnvironmentSetting,
)
from backend.app.models.metrics.metric import MetricType, RawMetric
from backend.app.models.rollout_schedule import (
    RolloutSchedule,
    RolloutScheduleStatus,
    RolloutStage,
    RolloutStageStatus,
    TriggerType,
)
from backend.app.models.safety import FeatureFlagSafetyConfig
from backend.app.models.segment import Segment, SegmentKind, SegmentStatus
from backend.app.models.user import User, UserRole
from backend.app.services import flag_environment_service as service
from backend.app.services.sdk_ruleset import build_ruleset
from backend.app.services.stale_flags import stale_clock

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

PREFIX = "fe2011"
SCHEMA = "test_experimentation"
V1 = "/api/v1"
FLAGS = f"{V1}/feature-flags"
ENVIRONMENTS = f"{V1}/environments"

ACTIVE = FeatureFlagStatus.ACTIVE
INACTIVE = FeatureFlagStatus.INACTIVE
ARCHIVED = FeatureFlagStatus.ARCHIVED

#: A flag in an environment where nobody configured it.
OFF = {
    "status": "inactive",
    "is_active": False,
    "rollout_percentage": 0,
    "targeting_rules": None,
    "configured": False,
    "updated_at": None,
}

#: The standard 422 text for a name that is not an environment.
NOT_AN_ENVIRONMENT = "Input should be 'production', 'staging' or 'development'"


def _rules(country="US"):
    return {
        "logical_operator": "AND",
        "groups": [
            {
                "logical_operator": "AND",
                "conditions": [
                    {"attribute": "country", "operator": "equals", "value": country}
                ],
            }
        ],
    }


def _segment_rules(segment_id):
    return {
        "groups": [
            {
                "conditions": [
                    {
                        "attribute": "segment",
                        "operator": "in_segment",
                        "value": str(segment_id),
                    }
                ]
            }
        ]
    }


# --- fixtures -----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _local_auth(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "CACHE_ENABLED", False)


def _user(db_session, role, superuser=False):
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"{PREFIX}_{role.value}_{suffix}",
        email=f"{PREFIX}_{suffix}@env.test",
        full_name="Flag Environments User",
        hashed_password="unused: this user signs in by token only",
        is_active=True,
        is_superuser=superuser,
        role=role,
    )
    db_session.add(user)
    db_session.commit()
    return user


@pytest.fixture
def developer(db_session):
    return _user(db_session, UserRole.DEVELOPER)


@pytest.fixture
def fresh(test_db):
    """A session of the test's own, opened per read, never the request's."""
    factory = sessionmaker(bind=test_db, expire_on_commit=False)

    def open_session():
        session = factory()
        session.execute(text(f"SET search_path TO {SCHEMA}"))
        return session

    return open_session


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


@pytest.fixture(autouse=True)
def _cleanup(db_session):
    """Leave nothing behind: other suites list flags and count entries."""
    yield
    s = db_session
    s.rollback()
    flag_ids = [
        r.id
        for r in s.query(FeatureFlag.id).filter(FeatureFlag.key.like(f"{PREFIX}-%"))
    ]
    for schedule in s.query(RolloutSchedule).filter(
        RolloutSchedule.feature_flag_id.in_(flag_ids)
    ):
        s.delete(schedule)
    s.commit()
    user_ids = [
        r.id for r in s.query(User.id).filter(User.username.like(f"{PREFIX}_%"))
    ]
    for model, column in (
        (FeatureFlagSafetyConfig, FeatureFlagSafetyConfig.feature_flag_id),
        (RawMetric, RawMetric.feature_flag_id),
        (FeatureFlag, FeatureFlag.id),
    ):
        s.query(model).filter(column.in_(flag_ids)).delete(synchronize_session=False)
    s.query(Segment).filter(Segment.name.like(f"{PREFIX}%")).delete(
        synchronize_session=False
    )
    s.query(AuditLog).filter(
        AuditLog.entity_id.in_(flag_ids) | AuditLog.user_id.in_(user_ids)
    ).delete(synchronize_session=False)
    s.query(APIKey).filter(APIKey.user_id.in_(user_ids)).delete(
        synchronize_session=False
    )
    s.query(User).filter(User.id.in_(user_ids)).delete(synchronize_session=False)
    s.commit()


# --- helpers ------------------------------------------------------------------


def _auth(user):
    return {"Authorization": f"Bearer {create_local_access_token(user)}"}


def _flag(db_session, owner=None, status=INACTIVE, rollout=0, rules=None, **columns):
    flag = FeatureFlag(
        key=f"{PREFIX}-{uuid.uuid4().hex[:10]}",
        name=f"{PREFIX} flag",
        status=status,
        rollout_percentage=rollout,
        targeting_rules=rules,
        owner_id=owner.id if owner else None,
        **columns,
    )
    db_session.add(flag)
    db_session.commit()
    return flag


def _setting(db_session, flag, environment, is_active=True, rollout=100, rules=None):
    row = FlagEnvironmentSetting(
        feature_flag_id=flag.id,
        environment=environment,
        is_active=is_active,
        rollout_percentage=rollout,
        targeting_rules=rules,
    )
    db_session.add(row)
    db_session.commit()
    return row


def _segment(db_session, owner, status=SegmentStatus.ACTIVE):
    row = Segment(
        name=f"{PREFIX} {uuid.uuid4().hex[:8]}",
        kind=SegmentKind.ID_LIST.value,
        status=status,
        owner_id=owner.id,
    )
    db_session.add(row)
    db_session.commit()
    return row


def _production(fresh, flag_id):
    """The flag row's per-environment columns and its ``updated_at``."""
    session = fresh()
    try:
        row = session.get(FeatureFlag, flag_id)
        return (
            FeatureFlagStatus(row.status),
            row.rollout_percentage,
            row.targeting_rules,
            row.updated_at,
        )
    finally:
        session.close()


def _side_rows(fresh, flag_id):
    """``{environment: (is_active, rollout, rules, updated_at)}`` as stored."""
    session = fresh()
    try:
        rows = session.query(FlagEnvironmentSetting).filter(
            FlagEnvironmentSetting.feature_flag_id == flag_id
        )
        return {
            r.environment: (
                r.is_active,
                r.rollout_percentage,
                r.targeting_rules,
                r.updated_at,
            )
            for r in rows
        }
    finally:
        session.close()


def _state(fresh, flag_id):
    return _production(fresh, flag_id), _side_rows(fresh, flag_id)


def _entries(fresh, flag_id):
    session = fresh()
    try:
        return (
            session.query(AuditLog)
            .filter(AuditLog.entity_id == flag_id)
            .order_by(AuditLog.timestamp)
            .all()
        )
    finally:
        session.close()


def _events(fresh, flag_id):
    session = fresh()
    try:
        return (
            session.query(ComplianceAuditEvent)
            .filter(ComplianceAuditEvent.resource_id == str(flag_id))
            .all()
        )
    finally:
        session.close()


def _values(entry):
    def load(value):
        return json.loads(value) if value else None

    return load(entry.old_value), load(entry.new_value)


def _put(client, user, flag_id, environment, body):
    return client.put(
        f"{FLAGS}/{flag_id}/environments/{environment}", json=body, headers=_auth(user)
    )


def _promote(client, user, flag_id, source, target):
    return client.post(
        f"{FLAGS}/{flag_id}/environments/{target}/promote",
        json={"from": source},
        headers=_auth(user),
    )


def _get(client, user, path, **params):
    return client.get(path, headers=_auth(user), params=params)


def _bodies(client, user, flag_id):
    """The three EnvSettings the API answers, by environment."""
    response = _get(client, user, f"{FLAGS}/{flag_id}/environments")
    assert response.status_code == 200, response.text
    return {e["environment"]: e for e in response.json()["environments"]}


# --- the environments ---------------------------------------------------------


@pytest.mark.parametrize("role", list(UserRole), ids=lambda r: r.value)
def test_the_environments_are_listed_for_every_role(client, db_session, role):
    response = _get(client, _user(db_session, role), ENVIRONMENTS)
    assert response.status_code == 200, response.text
    assert response.json() == [
        {"key": "production", "name": "Production", "default": True},
        {"key": "staging", "name": "Staging", "default": False},
        {"key": "development", "name": "Development", "default": False},
    ]


#: A flag id no test stores. A fixed one, because it is part of the test ids,
#: and every process that collects them must name the same tests.
NO_FLAG = "00000000-0000-4000-8000-00000000f1a9"


@pytest.mark.parametrize(
    "method, path",
    [
        ("GET", ENVIRONMENTS),
        ("GET", f"{ENVIRONMENTS}/staging/feature-flag-settings?flag_id={NO_FLAG}"),
        ("GET", f"{FLAGS}/{NO_FLAG}/environments"),
        ("GET", f"{FLAGS}/{NO_FLAG}/environments/staging"),
        ("PUT", f"{FLAGS}/{NO_FLAG}/environments/staging"),
        ("POST", f"{FLAGS}/{NO_FLAG}/environments/staging/promote"),
    ],
)
def test_every_route_needs_a_sign_in(client, method, path):
    body = {"from": "production"} if method == "POST" else {}
    response = client.request(method, path, json=body if method != "GET" else None)
    assert response.status_code == 401, response.text


# --- reads --------------------------------------------------------------------


@pytest.mark.parametrize("role", list(UserRole), ids=lambda r: r.value)
def test_a_flag_nobody_configured_is_off_outside_production(client, db_session, role):
    reader = _user(db_session, role)
    flag = _flag(db_session, status=ACTIVE, rollout=40, rules=_rules())
    body = _get(client, reader, f"{FLAGS}/{flag.id}/environments").json()
    assert body["flag_id"] == str(flag.id)
    assert body["key"] == flag.key
    production, staging, development = body["environments"]
    assert production.pop("updated_at") is not None
    assert production == {
        "environment": "production",
        "status": "active",
        "is_active": True,
        "rollout_percentage": 40,
        "targeting_rules": _rules(),
        "configured": True,
    }
    assert staging == {"environment": "staging", **OFF}
    assert development == {"environment": "development", **OFF}
    for name, expected in (("staging", staging), ("development", development)):
        one = _get(client, reader, f"{FLAGS}/{flag.id}/environments/{name}")
        assert one.status_code == 200, one.text
        assert one.json() == expected


def test_each_environment_answers_its_own_settings(client, db_session, developer):
    flag = _flag(db_session, developer, status=INACTIVE, rollout=5)
    _setting(db_session, flag, "staging", True, 30, _rules("CA"))
    _setting(db_session, flag, "development", False, 70)
    bodies = _bodies(client, developer, flag.id)
    assert {
        name: (
            b["status"],
            b["is_active"],
            b["rollout_percentage"],
            b["targeting_rules"],
            b["configured"],
            b["updated_at"] is not None,
        )
        for name, b in bodies.items()
    } == {
        "production": ("inactive", False, 5, None, True, True),
        "staging": ("active", True, 30, _rules("CA"), True, True),
        "development": ("inactive", False, 70, None, True, True),
    }


def test_an_archived_flag_is_archived_in_every_environment(
    client, db_session, developer
):
    flag = _flag(db_session, developer, status=ARCHIVED)
    _setting(db_session, flag, "staging", True, 30)
    bodies = _bodies(client, developer, flag.id)
    assert list(bodies) == list(FLAG_ENVIRONMENTS)
    for body in bodies.values():
        assert (body["status"], body["is_active"]) == ("archived", False), body


#: Names that are not an environment, in a path.
NOT_NAMES = ("Production", "prod", "dev", "qa", "STAGING", "all")


@pytest.mark.parametrize("name", NOT_NAMES)
@pytest.mark.parametrize("method", ["GET", "PUT", "POST", "BATCH"])
def test_an_unknown_environment_in_the_path_is_the_standard_422(
    client, fresh, db_session, developer, method, name
):
    flag = _flag(db_session, developer)
    before = _state(fresh, flag.id)
    if method == "BATCH":
        response = _get(
            client,
            developer,
            f"{ENVIRONMENTS}/{name}/feature-flag-settings",
            flag_id=str(flag.id),
        )
        where = "environment"
    elif method == "POST":
        response = _promote(client, developer, flag.id, "staging", name)
        where = "target"
    else:
        response = client.request(
            method,
            f"{FLAGS}/{flag.id}/environments/{name}",
            json={"is_active": True} if method == "PUT" else None,
            headers=_auth(developer),
        )
        where = "environment"
    assert response.status_code == 422, response.text
    (error,) = response.json()["detail"]
    assert error["type"] == "literal_error"
    assert error["loc"] == ["path", where]
    assert error["msg"] == NOT_AN_ENVIRONMENT
    assert _state(fresh, flag.id) == before


@pytest.mark.parametrize(
    "path", ["", "/staging"], ids=["every environment", "one environment"]
)
def test_an_unknown_flag_is_404_and_a_bad_id_422(client, developer, path):
    missing = _get(client, developer, f"{FLAGS}/{uuid.uuid4()}/environments{path}")
    assert missing.status_code == 404, missing.text
    assert missing.json()["detail"] == "Feature flag not found"
    bad = _get(client, developer, f"{FLAGS}/not-an-id/environments{path}")
    assert bad.status_code == 422, bad.text


def test_an_unknown_flag_cannot_be_changed_or_promoted(client, developer):
    put = _put(client, developer, uuid.uuid4(), "staging", {"is_active": True})
    assert put.status_code == 404, put.text
    promote = _promote(client, developer, uuid.uuid4(), "staging", "production")
    assert promote.status_code == 404, promote.text


# --- the batch ----------------------------------------------------------------


def _batch(client, user, environment, ids):
    return client.get(
        f"{ENVIRONMENTS}/{environment}/feature-flag-settings",
        headers=_auth(user),
        params=[("flag_id", str(i)) for i in ids],
    )


@pytest.mark.parametrize("environment", FLAG_ENVIRONMENTS)
def test_the_batch_answers_each_flag_as_the_single_read_does(
    client, db_session, developer, environment
):
    configured = _flag(db_session, developer, status=ACTIVE, rollout=20)
    _setting(db_session, configured, "staging", True, 30)
    _setting(db_session, configured, "development", False, 60, _rules())
    untouched = _flag(db_session, developer)
    gone = uuid.uuid4()
    # A repeated id and one that names no flag: one entry, and none.
    response = _batch(
        client,
        developer,
        environment,
        [configured.id, untouched.id, gone, configured.id],
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["environment"] == environment
    assert set(body["settings"]) == {str(configured.id), str(untouched.id)}
    for flag in (configured, untouched):
        one = _get(client, developer, f"{FLAGS}/{flag.id}/environments/{environment}")
        assert body["settings"][str(flag.id)] == one.json()


def test_the_batch_takes_1_to_100_ids(client, developer):
    ids = [uuid.uuid4() for _ in range(101)]
    assert _batch(client, developer, "staging", ids[:100]).json() == {
        "environment": "staging",
        "settings": {},
    }
    for refused in (_batch(client, developer, "staging", ids),):
        assert refused.status_code == 422, refused.text
        assert refused.json()["detail"][0]["loc"] == ["query", "flag_id"]
    none = _batch(client, developer, "staging", [])
    assert none.status_code == 422, none.text
    assert none.json()["detail"][0]["loc"] == ["query", "flag_id"]
    bad = _batch(client, developer, "staging", ["not-an-id"])
    assert bad.status_code == 422, bad.text


def test_the_batch_reads_the_same_number_of_statements_for_any_number_of_flags(
    client, test_db, db_session, developer
):
    flags = [_flag(db_session, developer) for _ in range(12)]
    for flag in flags:
        _setting(db_session, flag, "staging")
    statements = []

    def count(conn, cursor, statement, *args):
        statements.append(statement)

    counts = []
    sa_event.listen(test_db, "before_cursor_execute", count)
    try:
        for size in (1, 12):
            statements.clear()
            response = _batch(
                client, developer, "staging", [f.id for f in flags[:size]]
            )
            assert response.status_code == 200, response.text
            assert len(response.json()["settings"]) == size
            counts.append(len(statements))
    finally:
        sa_event.remove(test_db, "before_cursor_execute", count)
    assert counts[0] == counts[1], counts


# --- PUT: staging and development -----------------------------------------------


@pytest.mark.parametrize("environment", ["staging", "development"])
def test_a_put_changes_only_its_environment(
    client, fresh, db_session, developer, environment
):
    other = "development" if environment == "staging" else "staging"
    flag = _flag(db_session, developer, status=ACTIVE, rollout=40, rules=_rules("FR"))
    _setting(db_session, flag, other, True, 15, _rules("DE"))
    production, before = _state(fresh, flag.id)

    response = _put(
        client,
        developer,
        flag.id,
        environment,
        {"is_active": True, "rollout_percentage": 60, "targeting_rules": _rules()},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body.pop("updated_at") is not None
    assert body == {
        "environment": environment,
        "status": "active",
        "is_active": True,
        "rollout_percentage": 60,
        "targeting_rules": _rules(),
        "configured": True,
    }
    after = _side_rows(fresh, flag.id)
    assert after[environment][:3] == (True, 60, _rules())
    assert after[other] == before[other]
    # The flag row, and its updated_at, are as they were.
    assert _production(fresh, flag.id) == production


def test_only_the_fields_sent_change_and_null_clears_the_rules(
    client, fresh, db_session, developer
):
    flag = _flag(db_session, developer)
    _setting(db_session, flag, "development", True, 5)
    production, before = _state(fresh, flag.id)

    first = _put(client, developer, flag.id, "staging", {"rollout_percentage": 25})
    assert first.status_code == 200, first.text
    # A new row starts off: the fields not sent are an unconfigured flag's.
    assert _side_rows(fresh, flag.id)["staging"][:3] == (False, 25, None)

    second = _put(client, developer, flag.id, "staging", {"is_active": True})
    assert second.status_code == 200, second.text
    assert _side_rows(fresh, flag.id)["staging"][:3] == (True, 25, None)

    third = _put(
        client, developer, flag.id, "staging", {"targeting_rules": _rules("US")}
    )
    assert third.status_code == 200, third.text
    assert _side_rows(fresh, flag.id)["staging"][:3] == (True, 25, _rules("US"))

    cleared = _put(client, developer, flag.id, "staging", {"targeting_rules": None})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["targeting_rules"] is None
    assert _side_rows(fresh, flag.id)["staging"][:3] == (True, 25, None)

    session = fresh()
    try:
        # Stored as SQL NULL, not as JSON null.
        stored = session.execute(
            text(
                f"SELECT targeting_rules IS NULL FROM {SCHEMA}.flag_environment_settings "
                "WHERE feature_flag_id = :id AND environment = 'staging'"
            ),
            {"id": flag.id},
        ).scalar()
    finally:
        session.close()
    assert stored is True
    assert _side_rows(fresh, flag.id)["development"] == before["development"]
    assert _production(fresh, flag.id) == production


@pytest.mark.regression
def test_a_put_moves_updated_at_every_time(client, fresh, db_session, developer):
    """The upsert sets ``updated_at`` itself: the database does not apply the
    column's ``onupdate`` to an INSERT ... ON CONFLICT DO UPDATE."""
    flag = _flag(db_session, developer)
    _setting(db_session, flag, "development")
    production, before = _state(fresh, flag.id)
    body = {"is_active": True, "rollout_percentage": 10}
    first = _put(client, developer, flag.id, "staging", body).json()
    one = _side_rows(fresh, flag.id)["staging"]
    second = _put(client, developer, flag.id, "staging", body).json()
    two = _side_rows(fresh, flag.id)["staging"]
    assert two[:3] == one[:3] == (True, 10, None)
    assert two[3] > one[3], (one[3], two[3])
    assert second["updated_at"] > first["updated_at"]
    assert _side_rows(fresh, flag.id)["development"] == before["development"]
    assert _production(fresh, flag.id) == production


@pytest.mark.parametrize("environment", FLAG_ENVIRONMENTS)
def test_an_empty_body_changes_nothing_and_records_nothing(
    client, fresh, db_session, developer, environment
):
    flag = _flag(db_session, developer, status=ACTIVE, rollout=40)
    before = _state(fresh, flag.id)
    response = _put(client, developer, flag.id, environment, {})
    assert response.status_code == 200, response.text
    assert response.json() == _bodies(client, developer, flag.id)[environment]
    assert _state(fresh, flag.id) == before
    assert _entries(fresh, flag.id) == []
    assert _events(fresh, flag.id) == []


REFUSED_BODIES = {
    "null is_active": ({"is_active": None}, ["body", "is_active"]),
    "null rollout_percentage": (
        {"rollout_percentage": None},
        ["body", "rollout_percentage"],
    ),
    "rollout over 100": ({"rollout_percentage": 101}, ["body", "rollout_percentage"]),
    "rollout under 0": ({"rollout_percentage": -1}, ["body", "rollout_percentage"]),
    "a flag field": ({"name": "renamed"}, ["body", "name"]),
    "an unknown field": ({"variants": []}, ["body", "variants"]),
    "a list of rules": (
        {"targeting_rules": [{"type": "context"}]},
        ["body", "targeting_rules"],
    ),
    "rules the evaluator would not apply": (
        {
            "targeting_rules": {
                "groups": [
                    {"conditions": [{"attribute": "a", "operator": "no", "value": 1}]}
                ]
            }
        },
        ["body", "targeting_rules"],
    ),
}


@pytest.mark.parametrize("environment", FLAG_ENVIRONMENTS)
@pytest.mark.parametrize("case", list(REFUSED_BODIES))
def test_a_body_a_flag_update_would_refuse_is_refused(
    client, fresh, db_session, developer, environment, case
):
    flag = _flag(db_session, developer, status=ACTIVE, rollout=40)
    before = _state(fresh, flag.id)
    body, loc = REFUSED_BODIES[case]
    response = _put(client, developer, flag.id, environment, body)
    assert response.status_code == 422, response.text
    assert [e["loc"] for e in response.json()["detail"]] == [loc], response.text
    assert _state(fresh, flag.id) == before
    assert _entries(fresh, flag.id) == []


@pytest.mark.parametrize("environment", FLAG_ENVIRONMENTS)
def test_rules_naming_a_segment_that_cannot_be_used_are_refused(
    client, fresh, db_session, developer, environment
):
    segment = _segment(db_session, developer, SegmentStatus.ARCHIVED)
    flag = _flag(db_session, developer)
    before = _state(fresh, flag.id)
    response = _put(
        client,
        developer,
        flag.id,
        environment,
        {"targeting_rules": _segment_rules(segment.id)},
    )
    assert response.status_code == 422, response.text
    (error,) = response.json()["detail"]
    assert error["loc"] == ["body", "targeting_rules"]
    assert _state(fresh, flag.id) == before
    assert _entries(fresh, flag.id) == []


# --- PUT: an archived flag ----------------------------------------------------


@pytest.mark.parametrize("environment", FLAG_ENVIRONMENTS)
def test_an_archived_flag_is_not_turned_on_in_any_environment(
    client, fresh, db_session, developer, environment
):
    flag = _flag(db_session, developer, status=ARCHIVED)
    before = _state(fresh, flag.id)
    response = _put(
        client,
        developer,
        flag.id,
        environment,
        {"is_active": True, "rollout_percentage": 50},
    )
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == ARCHIVED_FLAG_DETAIL
    assert _state(fresh, flag.id) == before
    assert _entries(fresh, flag.id) == []

    # Anything but turning it on is a change like another; it stays archived.
    allowed = _put(
        client,
        developer,
        flag.id,
        environment,
        {"is_active": False, "rollout_percentage": 50},
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["status"] == "archived"
    production, side = _state(fresh, flag.id)
    assert production[0] == ARCHIVED
    if environment == "production":
        assert production[1] == 50 and side == {}
    else:
        assert production == before[0]
        assert {n: r[:3] for n, r in side.items()} == {environment: (False, 50, None)}


# --- PUT: production is the flag row ------------------------------------------


def test_a_put_to_production_is_the_stable_put(client, fresh, db_session, developer):
    """The same change through both routes leaves the same row, entry and
    compliance event, and no environment's side row moves."""
    change = {
        "is_active": True,
        "rollout_percentage": 35,
        "targeting_rules": _rules("JP"),
    }
    made = {}
    for route in ("stable", "environment"):
        flag = _flag(db_session, developer, status=INACTIVE, rollout=5)
        _setting(db_session, flag, "staging", False, 80)
        side = _side_rows(fresh, flag.id)
        before = _production(fresh, flag.id)
        if route == "stable":
            response = client.put(
                f"{FLAGS}/{flag.id}", json=change, headers=_auth(developer)
            )
        else:
            response = _put(client, developer, flag.id, "production", change)
        assert response.status_code == 200, response.text
        row = _production(fresh, flag.id)
        assert row[3] > before[3], "a production change moves updated_at"
        assert _side_rows(fresh, flag.id) == side
        (entry,) = _entries(fresh, flag.id)
        (event,) = _events(fresh, flag.id)
        assert entry.user_id == developer.id

        def without_the_key(value):
            return {k: v for k, v in (value or {}).items() if k != "key"}

        made[route] = (
            row[:3],
            entry.action_type,
            _values(entry),
            event.action,
            without_the_key(event.old_value),
            without_the_key(event.new_value),
        )
    assert made["environment"] == made["stable"]
    assert made["stable"][:2] == ((ACTIVE, 35, _rules("JP")), "feature_flag_update")


# --- promotion ----------------------------------------------------------------


def test_promotion_copies_exactly_the_per_environment_fields():
    """Computed from the model: a column added to the side row must be copied
    or named an identity column."""
    columns = {c.name for c in FlagEnvironmentSetting.__table__.columns}
    copied = columns - set(SETTING_IDENTITY_COLUMNS)
    assert copied == {"is_active", "rollout_percentage", "targeting_rules", "split"}
    assert set(service.SETTING_FIELDS) == copied
    assert set(service.NOT_CONFIGURED) == copied
    flag = FeatureFlag(
        id=uuid.uuid4(), key="k", status=ACTIVE, rollout_percentage=1, name="n"
    )
    for environment in FLAG_ENVIRONMENTS:
        assert set(service.effective_settings(flag, None, environment)) == copied


PAIRS = [(s, t) for s in FLAG_ENVIRONMENTS for t in FLAG_ENVIRONMENTS if s != t]


@pytest.mark.parametrize("source, target", PAIRS, ids=lambda v: v)
def test_promotion_copies_all_three_fields_and_nothing_else(
    client, fresh, db_session, developer, source, target
):
    flag = _flag(
        db_session,
        developer,
        status=ACTIVE,
        rollout=10,
        rules=_rules("US"),
        value_type="string",
        on_value="new",
        off_value="old",
    )
    _setting(db_session, flag, "staging", True, 20, _rules("CA"))
    _setting(db_session, flag, "development", True, 30, _rules("MX"))
    schedule = RolloutSchedule(
        name=f"{PREFIX} schedule",
        feature_flag_id=flag.id,
        status=RolloutScheduleStatus.DRAFT,
        max_percentage=100,
        min_stage_duration=0,
    )
    config = FeatureFlagSafetyConfig(
        feature_flag_id=flag.id, enabled=True, metrics={}, rollback_percentage=7
    )
    db_session.add_all([schedule, config])
    db_session.commit()
    expected = {
        "production": (True, 10, _rules("US")),
        "staging": (True, 20, _rules("CA")),
        "development": (True, 30, _rules("MX")),
    }[source]
    others_before = _bodies(client, developer, flag.id)
    production_before = _production(fresh, flag.id)

    response = _promote(client, developer, flag.id, source, target)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["environment"] == target
    assert (
        body["is_active"],
        body["rollout_percentage"],
        body["targeting_rules"],
    ) == expected
    after = _bodies(client, developer, flag.id)
    assert after[target] == body
    for name in FLAG_ENVIRONMENTS:
        if name != target:
            assert after[name] == others_before[name], f"{name} changed"
    if target != "production":
        assert _production(fresh, flag.id) == production_before
    session = fresh()
    try:
        row = session.get(FeatureFlag, flag.id)
        assert (row.value_type, row.on_value, row.off_value) == ("string", "new", "old")
        assert session.get(RolloutSchedule, schedule.id).status == (
            RolloutScheduleStatus.DRAFT
        )
        assert session.get(FeatureFlagSafetyConfig, config.id).rollback_percentage == 7
    finally:
        session.close()


@pytest.mark.parametrize("target", ["production", "development"])
def test_promoting_from_an_environment_nobody_configured_turns_the_target_off(
    client, fresh, db_session, developer, target
):
    flag = _flag(db_session, developer, status=ACTIVE, rollout=40, rules=_rules())
    _setting(db_session, flag, "development", True, 30, _rules("MX"))
    before = _bodies(client, developer, flag.id)
    response = _promote(client, developer, flag.id, "staging", target)
    assert response.status_code == 200, response.text
    after = _bodies(client, developer, flag.id)
    assert (
        after[target]["is_active"],
        after[target]["rollout_percentage"],
        after[target]["targeting_rules"],
        after[target]["configured"],
    ) == (False, 0, None, True)
    for name in FLAG_ENVIRONMENTS:
        if name != target:
            assert after[name] == before[name], f"{name} changed"
    # Promoting from it did not configure it.
    assert "staging" not in _side_rows(fresh, flag.id)


def test_promoting_an_archived_flag_copies_off_and_leaves_it_archived(
    client, fresh, db_session, developer
):
    flag = _flag(db_session, developer, status=ARCHIVED, rollout=5)
    _setting(db_session, flag, "staging", True, 30)
    before = _side_rows(fresh, flag.id)
    response = _promote(client, developer, flag.id, "staging", "production")
    assert response.status_code == 200, response.text
    production, side = _state(fresh, flag.id)
    assert production[:3] == (ARCHIVED, 30, None)
    assert side == before


@pytest.mark.parametrize("name", FLAG_ENVIRONMENTS)
def test_promoting_into_the_source_is_refused(
    client, fresh, db_session, developer, name
):
    flag = _flag(db_session, developer)
    _setting(db_session, flag, "staging")
    before = _state(fresh, flag.id)
    response = _promote(client, developer, flag.id, name, name)
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == [
        {
            "type": "same_environment",
            "loc": ["body", "from"],
            "msg": "from must name another environment than the one promoted into",
        }
    ]
    assert _state(fresh, flag.id) == before
    assert _entries(fresh, flag.id) == []


@pytest.mark.parametrize(
    "body, kind",
    [
        ({"from": "prod"}, "literal_error"),
        ({"from": "Staging"}, "literal_error"),
        ({"from": ""}, "literal_error"),
        ({"from": None}, "literal_error"),
        ({}, "missing"),
        ({"from": "staging", "flags": []}, "extra_forbidden"),
        ({"source": "staging"}, "missing"),
    ],
    ids=["prod", "Staging", "empty", "null", "missing", "extra", "wrong name"],
)
def test_a_promotion_body_that_names_no_environment_is_refused(
    client, fresh, db_session, developer, body, kind
):
    flag = _flag(db_session, developer)
    _setting(db_session, flag, "staging")
    before = _state(fresh, flag.id)
    response = client.post(
        f"{FLAGS}/{flag.id}/environments/production/promote",
        json=body,
        headers=_auth(developer),
    )
    assert response.status_code == 422, response.text
    errors = {e["type"]: e for e in response.json()["detail"]}
    assert kind in errors, response.text
    if kind == "literal_error":
        assert errors[kind]["msg"] == NOT_AN_ENVIRONMENT
        assert errors[kind]["loc"] == ["body", "from"]
    assert _state(fresh, flag.id) == before


@pytest.mark.parametrize(
    "rules", ["a legacy list", "an archived segment"], ids=lambda v: v
)
def test_a_promotion_checks_the_rules_again(
    client, fresh, db_session, developer, rules
):
    """Rules a PUT to the target would refuse are refused, as the PUT refuses
    them, and nothing is copied."""
    if rules == "a legacy list":
        stored = [{"type": "context", "conditions": [{"key": "a", "value": "b"}]}]
    else:
        segment = _segment(db_session, developer, SegmentStatus.ARCHIVED)
        stored = _segment_rules(segment.id)
    flag = _flag(db_session, developer, status=ACTIVE, rollout=40, rules=stored)
    _setting(db_session, flag, "staging", False, 5)
    before = _state(fresh, flag.id)
    response = _promote(client, developer, flag.id, "production", "staging")
    assert response.status_code == 422, response.text
    assert [e["loc"] for e in response.json()["detail"]] == [
        ["body", "targeting_rules"]
    ], response.text
    if rules == "a legacy list":
        # Exactly what the PUT answers to the same rules.
        put = _put(client, developer, flag.id, "staging", {"targeting_rules": stored})
        assert put.status_code == 422, put.text
        assert response.json() == put.json()
    assert _state(fresh, flag.id) == before
    assert _entries(fresh, flag.id) == []


# --- promotion and the split (#2012) ------------------------------------------

#: A split per environment, no two the same. No request sets one: these are
#: stored through the models.
SPLITS = {
    "production": [{"value": "red", "weight": 50}, {"value": "blue", "weight": 50}],
    "staging": [{"value": "red", "weight": 10}, {"value": "blue", "weight": 90}],
    "development": None,
}


def _flag_with_splits(db_session, owner, splits=SPLITS, **columns):
    """A string flag configured in every environment, with *splits*; the rest of
    its settings are *columns*, or the same everywhere."""
    settings_in = {"status": ACTIVE, "rollout": 20, "rules": _rules(), **columns}
    flag = _flag(
        db_session,
        owner,
        **settings_in,
        value_type="string",
        on_value="on",
        off_value="off",
        split=splits["production"],
    )
    for name in NON_PRODUCTION:
        row = _setting(db_session, flag, name, True, 20, _rules())
        row.split = splits[name]
    db_session.commit()
    return flag


def _splits(fresh, flag_id):
    """``{environment: split}`` as stored; a JSON null is not SQL NULL."""
    session = fresh()
    try:
        rows = session.execute(
            text(
                "SELECT 'production', split::text FROM feature_flags WHERE id = :id "
                "UNION ALL SELECT environment, split::text "
                "FROM flag_environment_settings WHERE feature_flag_id = :id"
            ),
            {"id": flag_id},
        ).all()
    finally:
        session.close()
    return {
        name: "JSON null" if stored == "null" else stored and json.loads(stored)
        for name, stored in rows
    }


@pytest.mark.regression
@pytest.mark.parametrize("source, target", PAIRS, ids=lambda v: v)
def test_promotion_copies_the_split_and_changes_no_other(
    client, fresh, db_session, developer, source, target
):
    flag = _flag_with_splits(db_session, developer)
    assert _splits(fresh, flag.id) == SPLITS
    response = _promote(client, developer, flag.id, source, target)
    assert response.status_code == 200, response.text
    assert "split" not in response.json()
    assert _splits(fresh, flag.id) == {**SPLITS, target: SPLITS[source]}


@pytest.mark.regression
@pytest.mark.parametrize("stored", [SPLITS["production"], None], ids=["split", "none"])
@pytest.mark.parametrize(
    "source, target", [pair for pair in PAIRS if pair[1] != "production"], ids=str
)
def test_promoting_into_an_environment_nobody_configured_copies_the_split(
    client, fresh, db_session, developer, source, target, stored
):
    """The target has no side row yet: the promotion makes it."""
    flag = _flag(
        db_session,
        developer,
        ACTIVE,
        20,
        split=stored if source == "production" else None,
    )
    if source != "production":
        row = _setting(db_session, flag, source, True, 20)
        row.split = stored
        db_session.commit()
    before = _splits(fresh, flag.id)
    assert target not in before
    response = _promote(client, developer, flag.id, source, target)
    assert response.status_code == 200, response.text
    assert _splits(fresh, flag.id) == {**before, target: stored}


@pytest.mark.regression
@pytest.mark.parametrize("state", [INACTIVE, ARCHIVED], ids=["off", "archived"])
@pytest.mark.parametrize("source, target", PAIRS, ids=lambda v: v)
def test_promoting_from_an_environment_that_is_off_copies_its_split(
    client, fresh, db_session, developer, source, target, state
):
    flag = _flag_with_splits(db_session, developer, status=state)
    db_session.query(FlagEnvironmentSetting).filter_by(feature_flag_id=flag.id).update(
        {"is_active": False}
    )
    db_session.commit()
    response = _promote(client, developer, flag.id, source, target)
    assert response.status_code == 200, response.text
    assert response.json()["is_active"] is False
    assert _splits(fresh, flag.id) == {**SPLITS, target: SPLITS[source]}


@pytest.mark.regression
@pytest.mark.parametrize("source, target", PAIRS, ids=lambda v: v)
def test_a_promotion_copies_what_is_stored_when_it_has_the_lock(
    client, fresh, db_session, developer, source, target
):
    """What another session stores until then is what is copied."""
    flag = _flag_with_splits(db_session, developer)
    newer = [{"value": "green", "weight": 30}, {"value": "blue", "weight": 70}]
    lock = routes.lock_flag

    def stored_by_another_then_locked(db, flag_id):
        session = fresh()
        if source == "production":
            session.get(FeatureFlag, flag_id).split = newer
        else:
            session.query(FlagEnvironmentSetting).filter_by(
                feature_flag_id=flag_id, environment=source
            ).one().split = newer
        session.commit()
        session.close()
        return lock(db, flag_id)

    with patch.object(routes, "lock_flag", side_effect=stored_by_another_then_locked):
        response = _promote(client, developer, flag.id, source, target)
    assert response.status_code == 200, response.text
    assert _splits(fresh, flag.id) == {**SPLITS, source: newer, target: newer}


@pytest.mark.parametrize("target", ["production", "development"])
def test_promoting_from_an_environment_nobody_configured_clears_the_split(
    client, fresh, db_session, developer, target
):
    flag = _flag(db_session, developer, ACTIVE, 20, split=SPLITS["production"])
    row = _setting(db_session, flag, "development")
    row.split = SPLITS["staging"]
    db_session.commit()
    stored = {"production": SPLITS["production"], "development": SPLITS["staging"]}
    assert _splits(fresh, flag.id) == stored
    response = _promote(client, developer, flag.id, "staging", target)
    assert response.status_code == 200, response.text
    assert _splits(fresh, flag.id) == {**stored, target: None}


@pytest.mark.regression
@pytest.mark.parametrize("environment", FLAG_ENVIRONMENTS)
def test_a_put_keeps_the_split_and_cannot_set_one(
    client, fresh, db_session, developer, environment
):
    flag = _flag_with_splits(db_session, developer)
    body = {"rollout_percentage": 7, "is_active": False, "targeting_rules": None}
    response = _put(client, developer, flag.id, environment, body)
    assert response.status_code == 200, response.text
    assert _splits(fresh, flag.id) == SPLITS
    for sent in (SPLITS["staging"], None):
        refused = _put(client, developer, flag.id, environment, {"split": sent})
        assert refused.status_code == 422, refused.text
        assert [(e["type"], e["loc"]) for e in refused.json()["detail"]] == [
            ("extra_forbidden", ["body", "split"])
        ]
    assert _splits(fresh, flag.id) == SPLITS


@pytest.mark.regression
@pytest.mark.parametrize(
    "source, target",
    [
        ("staging", "production"),
        ("development", "production"),
        ("production", "staging"),
        ("staging", "development"),
    ],
    ids=lambda v: v,
)
def test_a_promotion_that_changes_only_the_split_names_it(
    client, fresh, db_session, developer, source, target
):
    """By its name, never what it holds, in each entry of the promotion."""
    flag = _flag_with_splits(db_session, developer)
    assert _promote(client, developer, flag.id, source, target).status_code == 200
    where = {"environment": target, "from": source, "to": target}
    by_action = {e.action_type: e for e in _entries(fresh, flag.id)}
    promoted = by_action.pop("feature_flag_promote")
    assert _values(promoted) == (where, {**where, "changed_fields": ["split"]})
    if target == "production":
        updated = by_action.pop("feature_flag_update")
        assert _values(updated) == (None, {"changed_fields": ["split"]})
        assert "red" not in (updated.new_value or "")
    assert by_action == {}
    assert "red" not in promoted.old_value + promoted.new_value

    # Promoted again, it changes nothing and names nothing.
    assert _promote(client, developer, flag.id, source, target).status_code == 200
    again = [e for e in _entries(fresh, flag.id) if e.id != promoted.id]
    latest = [e for e in again if e.action_type == "feature_flag_promote"]
    assert [_values(e) for e in latest] == [(where, where)]


@pytest.mark.regression
@pytest.mark.parametrize("target", ["staging", "production"])
def test_a_promotion_that_cannot_be_recorded_copies_no_split(
    client, fresh, db_session, developer, target
):
    flag = _flag_with_splits(db_session, developer)
    source = "staging" if target == "production" else "production"
    with patch.object(
        routes.AuditService, "record", side_effect=RuntimeError("no entry")
    ):
        response = _promote(client, developer, flag.id, source, target)
    assert response.status_code == 500, response.text
    assert _splits(fresh, flag.id) == SPLITS
    assert _entries(fresh, flag.id) == []


@pytest.mark.regression
def test_production_answers_as_staging_did_after_a_promotion(
    client, db_session, developer, key_of
):
    """For a flag with a split, user by user; staging and development answer as
    before. (No evaluation serves a split yet.)"""
    flag = _flag_with_splits(db_session, developer, rollout=10, rules=None)
    staging = (
        db_session.query(FlagEnvironmentSetting)
        .filter_by(feature_flag_id=flag.id, environment="staging")
        .one()
    )
    staging.rollout_percentage, staging.targeting_rules = 60, None
    db_session.commit()
    users = [f"user-{n}" for n in range(40)]

    def answers(name):
        found = {}
        for user_id in users:
            response = client.get(
                f"{FLAGS}/evaluate/{flag.key}",
                params={"user_id": user_id},
                headers=key_of[name],
            )
            assert response.status_code == 200, response.text
            body = response.json()
            found[user_id] = (body["enabled"], body["value"], body["reason"])
        return found

    before = {name: answers(name) for name in FLAG_ENVIRONMENTS}
    assert before["production"] != before["staging"]
    response = _promote(client, developer, flag.id, "staging", "production")
    assert response.status_code == 200, response.text
    after = {name: answers(name) for name in FLAG_ENVIRONMENTS}
    assert after == {**before, "production": before["staging"]}


# --- audit --------------------------------------------------------------------


def test_an_environment_change_records_what_changed_and_where(
    client, fresh, db_session, developer
):
    flag = _flag(db_session, developer)
    _setting(db_session, flag, "staging", False, 10)
    response = _put(
        client,
        developer,
        flag.id,
        "staging",
        {"is_active": True, "rollout_percentage": 10, "targeting_rules": _rules()},
    )
    assert response.status_code == 200, response.text
    (entry,) = _entries(fresh, flag.id)
    assert entry.action_type == "feature_flag_environment_update"
    assert entry.entity_type == "feature_flag"
    assert entry.entity_name == flag.name
    assert entry.user_id == developer.id
    # The fields that changed and the environment; the rules by name only.
    assert _values(entry) == (
        {"environment": "staging", "is_active": False},
        {
            "environment": "staging",
            "is_active": True,
            "changed_fields": ["targeting_rules"],
        },
    )
    assert "country" not in (entry.old_value + entry.new_value)
    # A compliance event is written for a production change only.
    assert _events(fresh, flag.id) == []


def test_configuring_a_flag_records_that_it_was_not_configured(
    client, fresh, db_session, developer
):
    flag = _flag(db_session, developer)
    _put(client, developer, flag.id, "development", {"rollout_percentage": 0})
    (entry,) = _entries(fresh, flag.id)
    assert _values(entry) == (
        {"environment": "development", "configured": False},
        {"environment": "development", "configured": True},
    )


def test_a_promotion_records_both_environments_and_the_change(
    client, fresh, db_session, developer
):
    flag = _flag(db_session, developer)
    _setting(db_session, flag, "development", True, 25, _rules())
    response = _promote(client, developer, flag.id, "development", "staging")
    assert response.status_code == 200, response.text
    (entry,) = _entries(fresh, flag.id)
    assert entry.action_type == "feature_flag_promote"
    assert entry.user_id == developer.id
    where = {"environment": "staging", "from": "development", "to": "staging"}
    assert _values(entry) == (
        {**where, "configured": False, "is_active": False, "rollout_percentage": 0},
        {
            **where,
            "configured": True,
            "is_active": True,
            "rollout_percentage": 25,
            "changed_fields": ["targeting_rules"],
        },
    )
    assert _events(fresh, flag.id) == []


def test_a_promotion_into_production_also_records_what_a_flag_update_records(
    client, fresh, db_session, developer
):
    flag = _flag(db_session, developer)
    _setting(db_session, flag, "staging", True, 25)
    response = _promote(client, developer, flag.id, "staging", "production")
    assert response.status_code == 200, response.text
    by_action = {e.action_type: e for e in _entries(fresh, flag.id)}
    assert set(by_action) == {"feature_flag_update", "feature_flag_promote"}
    where = {"environment": "production", "from": "staging", "to": "production"}
    assert _values(by_action["feature_flag_promote"]) == (
        {**where, "is_active": False, "rollout_percentage": 0},
        {**where, "is_active": True, "rollout_percentage": 25},
    )
    assert _values(by_action["feature_flag_update"]) == (
        {"rollout_percentage": 0, "status": "INACTIVE"},
        {"rollout_percentage": 25, "status": "ACTIVE"},
    )
    (event,) = _events(fresh, flag.id)
    assert event.action.upper() == "UPDATE"


def test_the_flag_history_and_the_list_show_the_entries(
    client, fresh, db_session, developer
):
    flag = _flag(db_session, developer)
    _put(client, developer, flag.id, "staging", {"is_active": True})
    _promote(client, developer, flag.id, "staging", "development")
    history = _get(client, developer, f"{FLAGS}/{flag.id}/history")
    assert history.status_code == 200, history.text
    assert sorted(e["action_type"] for e in history.json()["history"]) == [
        "feature_flag_environment_update",
        "feature_flag_promote",
    ]
    for action in ("feature_flag_environment_update", "feature_flag_promote"):
        listed = _get(
            client,
            developer,
            f"{V1}/audit-logs/",
            action_type=action,
            entity_id=str(flag.id),
        )
        assert listed.status_code == 200, listed.text
        assert action in listed.text


# --- bulk archive and delete --------------------------------------------------


def _bulk(client, user, action, *flag_ids):
    response = client.post(
        f"{FLAGS}/bulk-toggle",
        json={"flag_ids": [str(i) for i in flag_ids], "action": action},
        headers=_auth(user),
    )
    assert response.status_code == 200, response.text
    assert all(r["success"] for r in response.json()["results"]), response.text
    return response.json()


def test_bulk_archive_turns_the_flag_off_everywhere_and_names_where(
    client, fresh, db_session, developer
):
    flag = _flag(db_session, developer, status=ACTIVE, rollout=40)
    _setting(db_session, flag, "staging", True, 30, _rules())
    _setting(db_session, flag, "development", False, 20)
    other = _flag(db_session, developer, status=ACTIVE)
    _setting(db_session, other, "staging", True, 30)
    before, other_before = _side_rows(fresh, flag.id), _state(fresh, other.id)

    _bulk(client, developer, "archive", flag.id)

    production, side = _state(fresh, flag.id)
    assert production[:2] == (ARCHIVED, 40)
    # Off in staging, with its rules and percentage kept; a row that was off
    # already is not touched, and no other flag's is.
    assert side["staging"][:3] == (False, 30, _rules())
    assert side["staging"][3] > before["staging"][3]
    assert side["development"] == before["development"]
    assert _state(fresh, other.id) == other_before
    (entry,) = _entries(fresh, flag.id)
    assert entry.action_type == "feature_flag_update"
    assert _values(entry) == (
        {"status": "ACTIVE", "environments": {"staging": {"is_active": True}}},
        {"status": "ARCHIVED", "environments": {"staging": {"is_active": False}}},
    )

    # Unarchiving is production's status only: staging stays off.
    unarchive = client.post(f"{FLAGS}/{flag.id}/unarchive", headers=_auth(developer))
    assert unarchive.status_code == 200, unarchive.text
    assert _side_rows(fresh, flag.id) == side


def test_bulk_archive_turns_off_and_names_both_side_rows(
    client, fresh, db_session, developer
):
    flag = _flag(db_session, developer, status=ACTIVE, rollout=40)
    for name in NON_PRODUCTION:
        _setting(db_session, flag, name, True, 30)
    _bulk(client, developer, "archive", flag.id)
    side = _side_rows(fresh, flag.id)
    assert {name: row[0] for name, row in side.items()} == dict.fromkeys(
        NON_PRODUCTION, False
    )
    (entry,) = _entries(fresh, flag.id)
    for values, on in zip(_values(entry), (True, False)):
        assert values["environments"] == {
            name: {"is_active": on} for name in NON_PRODUCTION
        }


def test_bulk_archive_of_a_flag_on_nowhere_else_records_the_two_statuses(
    client, fresh, db_session, developer
):
    flag = _flag(db_session, developer, status=ACTIVE)
    _setting(db_session, flag, "staging", False, 30)
    before = _side_rows(fresh, flag.id)
    _bulk(client, developer, "archive", flag.id)
    (entry,) = _entries(fresh, flag.id)
    assert (entry.old_value, entry.new_value) == ("ACTIVE", "ARCHIVED")
    assert _side_rows(fresh, flag.id) == before


@pytest.mark.parametrize("action", ["enable", "disable"])
def test_bulk_enable_and_disable_are_production_only(
    client, fresh, db_session, developer, action
):
    start = INACTIVE if action == "enable" else ACTIVE
    flag = _flag(db_session, developer, status=start)
    _setting(db_session, flag, "staging", True, 30)
    _setting(db_session, flag, "development", False, 30)
    before = _side_rows(fresh, flag.id)
    _bulk(client, developer, action, flag.id)
    production, side = _state(fresh, flag.id)
    assert production[0] == (ACTIVE if action == "enable" else INACTIVE)
    assert side == before


def test_a_flag_delete_records_the_side_rows_it_takes_with_it(
    client, fresh, db_session, developer
):
    flag = _flag(db_session, developer, status=ACTIVE)
    _setting(db_session, flag, "staging", True, 30, _rules())
    _setting(db_session, flag, "development", False, 20)
    other = _flag(db_session, developer)
    _setting(db_session, other, "staging", True, 30)
    other_before = _state(fresh, other.id)
    response = client.delete(f"{FLAGS}/{flag.id}", headers=_auth(developer))
    assert response.status_code == 204, response.text
    (entry,) = _entries(fresh, flag.id)
    old, new = _values(entry)
    assert new is None
    assert old["key"] == flag.key
    assert old["environments"] == {
        "staging": {"is_active": True, "rollout_percentage": 30},
        "development": {"is_active": False, "rollout_percentage": 20},
    }
    assert "country" not in entry.old_value
    assert _side_rows(fresh, flag.id) == {}
    assert _state(fresh, other.id) == other_before


def test_a_flag_delete_with_no_side_rows_records_what_it_recorded_before(
    client, fresh, db_session, developer
):
    flag = _flag(db_session, developer, status=ACTIVE)
    response = client.delete(f"{FLAGS}/{flag.id}", headers=_auth(developer))
    assert response.status_code == 204, response.text
    (entry,) = _entries(fresh, flag.id)
    assert set(_values(entry)[0]) == {
        "key",
        "name",
        "status",
        "rollout_percentage",
        "value_type",
    }


# --- the readers of a flag's rules and state ----------------------------------


def test_a_segment_used_only_in_staging_is_in_use_and_names_staging(
    client, fresh, db_session, developer
):
    segment = _segment(db_session, developer)
    flag = _flag(db_session, developer, status=ACTIVE)
    _setting(db_session, flag, "staging", False, 0, _segment_rules(segment.id))
    both = _flag(
        db_session, developer, status=INACTIVE, rules=_segment_rules(segment.id)
    )
    _setting(db_session, both, "development", True, 0, _segment_rules(segment.id))
    production_only = _flag(
        db_session, developer, status=INACTIVE, rules=_segment_rules(segment.id)
    )
    # An archived flag's side rows cannot evaluate anything.
    archived = _flag(db_session, developer, status=ARCHIVED)
    _setting(db_session, archived, "staging", True, 0, _segment_rules(segment.id))

    response = client.delete(f"{V1}/segments/{segment.id}", headers=_auth(developer))

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "segment_in_use"
    listed = {f["id"]: f for f in detail["feature_flags"]}
    assert listed == {
        str(flag.id): {
            "id": str(flag.id),
            "key": flag.key,
            "name": flag.name,
            "environments": ["staging"],
        },
        str(both.id): {
            "id": str(both.id),
            "key": both.key,
            "name": both.name,
            "environments": ["production", "development"],
        },
        # As before: a flag used in production alone names no environment.
        str(production_only.id): {
            "id": str(production_only.id),
            "key": production_only.key,
            "name": production_only.name,
        },
    }
    assert detail["message"].startswith("This segment is used by 3 feature flags. ")

    usage = _get(client, developer, f"{V1}/segments/{segment.id}/experiments")
    assert usage.status_code == 200, usage.text
    flags = {f["id"]: f.get("environments") for f in usage.json()["feature_flags"]}
    assert flags == {
        str(flag.id): ["staging"],
        str(both.id): ["production", "development"],
        str(production_only.id): None,
        str(archived.id): ["staging"],
    }


def test_a_side_row_that_only_mentions_the_id_does_not_hold_the_segment(
    client, db_session, developer
):
    segment = _segment(db_session, developer)
    flag = _flag(db_session, developer)
    _setting(
        db_session,
        flag,
        "staging",
        True,
        0,
        {
            "groups": [
                {
                    "conditions": [
                        {
                            "attribute": "user_id",
                            "operator": "equals",
                            "value": str(segment.id),
                        }
                    ]
                }
            ]
        },
    )
    response = client.delete(f"{V1}/segments/{segment.id}", headers=_auth(developer))
    assert response.status_code == 204, response.text


def test_rules_stored_through_the_environment_put_hold_their_segment(
    client, fresh, db_session, developer
):
    segment = _segment(db_session, developer)
    flag = _flag(db_session, developer)
    put = _put(
        client,
        developer,
        flag.id,
        "development",
        {"targeting_rules": _segment_rules(segment.id)},
    )
    assert put.status_code == 200, put.text
    refused = client.delete(f"{V1}/segments/{segment.id}", headers=_auth(developer))
    assert refused.status_code == 409, refused.text
    cleared = _put(client, developer, flag.id, "development", {"targeting_rules": None})
    assert cleared.status_code == 200, cleared.text
    archived = client.delete(f"{V1}/segments/{segment.id}", headers=_auth(developer))
    assert archived.status_code == 204, archived.text


@pytest.mark.parametrize(
    "environment, is_active, stale",
    [
        (None, None, True),
        ("staging", False, True),
        ("staging", True, False),
        ("development", True, False),
    ],
    ids=["nowhere else", "off in staging", "on in staging", "on in development"],
)
def test_a_flag_on_outside_production_is_not_stale(
    client, db_session, developer, environment, is_active, stale
):
    now = datetime.now(timezone.utc) + timedelta(days=4000)
    old = now.replace(tzinfo=None) - timedelta(days=400)
    flag = _flag(db_session, developer, status=INACTIVE, created_at=old, updated_at=old)
    if environment:
        _setting(db_session, flag, environment, is_active, 0)

    async def clock():
        return now

    app.dependency_overrides[stale_clock] = clock
    try:
        listed = _get(client, developer, f"{FLAGS}/", search=flag.key).json()
        filtered = _get(
            client, developer, f"{FLAGS}/", search=flag.key, stale="true"
        ).json()
    finally:
        app.dependency_overrides.pop(stale_clock, None)
    (item,) = listed["items"]
    assert item["stale"] is stale, item
    assert item["stale_reasons"] == (["off_for_everyone"] if stale else [])
    assert filtered["total"] == (1 if stale else 0)


# --- what stays production's --------------------------------------------------


def _tick(test_db):
    from backend.app.core.rollout_scheduler import RolloutScheduler

    factory = sessionmaker(bind=test_db)

    def session_factory():
        session = factory()
        session.execute(text(f"SET search_path TO {SCHEMA}"))
        return session

    scheduler = RolloutScheduler(interval_minutes=1)
    scheduler._notification_service = MagicMock()
    with patch("backend.app.core.rollout_scheduler.SessionLocal", session_factory):
        asyncio.run(scheduler.process_rollout_schedules())


@pytest.mark.regression
def test_a_rollout_schedule_changes_production_only(
    client, fresh, test_db, db_session, developer
):
    flag = _flag(db_session, developer, status=ACTIVE, rollout=0)
    _setting(db_session, flag, "staging", True, 0)
    _setting(db_session, flag, "development", False, 15, _rules())
    schedule = RolloutSchedule(
        name=f"{PREFIX} schedule",
        feature_flag_id=flag.id,
        status=RolloutScheduleStatus.ACTIVE,
        max_percentage=100,
        min_stage_duration=0,
    )
    db_session.add(schedule)
    db_session.commit()
    db_session.add(
        RolloutStage(
            rollout_schedule_id=schedule.id,
            name="one",
            stage_order=1,
            target_percentage=50,
            trigger_type=TriggerType.TIME_BASED,
            trigger_configuration={"duration": 24},
            status=RolloutStageStatus.PENDING,
        )
    )
    db_session.commit()
    before = _side_rows(fresh, flag.id)

    _tick(test_db)

    production, side = _state(fresh, flag.id)
    assert production[1] == 50, "the tick started the stage"
    assert side == before, (
        f"production schedule moved staging {before['staging'][1]} -> "
        f"{side['staging'][1]}"
    )


def _ruleset_key(db_session, user, environment="production"):
    key = generate_api_key()
    db_session.add(
        APIKey(
            key=hash_api_key(key),
            name=PREFIX,
            user_id=user.id,
            scopes="sdk:ruleset",
            environment=environment,
        )
    )
    db_session.commit()
    return key


@pytest.fixture
def key_of(db_session, developer):
    """The header of a key of each environment."""
    return {
        name: {"X-API-Key": _ruleset_key(db_session, developer, name)}
        for name in FLAG_ENVIRONMENTS
    }


def _poll(client, headers, etag=None):
    if etag is not None:
        headers = {**headers, "If-None-Match": etag}
    return client.get(f"{V1}/sdk/ruleset", headers=headers)


def _etags(client, key_of):
    """The ETag a first poll with each environment's key is answered."""
    polls = {name: _poll(client, headers) for name, headers in key_of.items()}
    assert {poll.status_code for poll in polls.values()} == {200}
    return {name: poll.headers["ETag"] for name, poll in polls.items()}


def _polled(client, key_of, etags):
    """``{environment: status}`` of a poll that sends its ETag back."""
    return {
        name: _poll(client, headers, etags[name]).status_code
        for name, headers in key_of.items()
    }


def _evaluated(client, flag, key_of, **context):
    """``{environment: (enabled, reason)}`` with a key of each."""
    params = {"user_id": "u"}
    if context:
        params["context"] = json.dumps(context)
    answers = {}
    for name, headers in key_of.items():
        response = client.get(
            f"{FLAGS}/evaluate/{flag.key}", params=params, headers=headers
        )
        assert response.status_code == 200, response.text
        body = response.json()
        answers[name] = (body["enabled"], body["reason"])
    return answers


def test_a_production_poll_changes_with_production_and_not_with_staging(
    client, db_session, developer
):
    """What a production ruleset poll sees of each write through the routes."""
    key = _ruleset_key(db_session, developer)
    flag = _flag(db_session, developer, status=ACTIVE, rollout=10)
    url = f"{V1}/sdk/ruleset"
    first = client.get(url, headers={"X-API-Key": key})
    assert first.status_code == 200, first.text
    etag = first.headers["ETag"]

    for environment in ("staging", "development"):
        put = _put(
            client,
            developer,
            flag.id,
            environment,
            {"is_active": True, "rollout_percentage": 80},
        )
        assert put.status_code == 200, put.text
    unchanged = client.get(url, headers={"X-API-Key": key, "If-None-Match": etag})
    assert unchanged.status_code == 304, unchanged.text

    promoted = _promote(client, developer, flag.id, "staging", "production")
    assert promoted.status_code == 200, promoted.text
    changed = client.get(url, headers={"X-API-Key": key, "If-None-Match": etag})
    assert changed.status_code == 200, changed.text
    assert changed.headers["ETag"] != etag
    (entry,) = [e for e in changed.json()["flags"] if e["key"] == flag.key]
    assert entry["rollout_percentage"] == 80


def _ruleset_versions(fresh):
    """The version of the ruleset built from what each environment serves."""
    session = fresh()
    try:
        flags = session.query(FeatureFlag).all()
        rows = service.settings_of(session, [f.id for f in flags])
        return {
            name: build_ruleset(
                [flag_in_environment(f, rows.get((f.id, name)), name) for f in flags]
            )["version"]
            for name in FLAG_ENVIRONMENTS
        }
    finally:
        session.close()


@pytest.mark.parametrize("environment", FLAG_ENVIRONMENTS)
@pytest.mark.parametrize("via", ["put", "promote"])
def test_a_change_moves_the_ruleset_version_of_its_environment_only(
    client, fresh, db_session, developer, environment, via
):
    flag = _flag(db_session, developer, status=ACTIVE, rollout=10)
    _setting(db_session, flag, "staging", True, 20)
    _setting(db_session, flag, "development", True, 30)
    before = _ruleset_versions(fresh)
    if via == "put":
        response = _put(
            client, developer, flag.id, environment, {"rollout_percentage": 80}
        )
    else:
        source = "staging" if environment != "staging" else "production"
        response = _promote(client, developer, flag.id, source, environment)
    assert response.status_code == 200, response.text
    after = _ruleset_versions(fresh)
    assert {n for n in FLAG_ENVIRONMENTS if after[n] != before[n]} == {environment}


def test_a_production_key_is_answered_productions_settings_after_a_staging_change(
    client, db_session, developer
):
    flag = _flag(db_session, developer, status=INACTIVE)
    key = _ruleset_key(db_session, developer)
    put = _put(
        client,
        developer,
        flag.id,
        "staging",
        {"is_active": True, "rollout_percentage": 100},
    )
    assert put.status_code == 200, put.text
    answer = client.get(
        f"{FLAGS}/evaluate/{flag.key}",
        params={"user_id": "u"},
        headers={"X-API-Key": key},
    )
    assert answer.status_code == 200, answer.text
    assert (answer.json()["enabled"], answer.json()["reason"]) == (False, "inactive")
    promoted = _promote(client, developer, flag.id, "staging", "production")
    assert promoted.status_code == 200, promoted.text
    answer = client.get(
        f"{FLAGS}/evaluate/{flag.key}",
        params={"user_id": "u"},
        headers={"X-API-Key": key},
    )
    assert (answer.json()["enabled"], answer.json()["reason"]) == (True, "rollout")


@pytest.mark.parametrize("environment", FLAG_ENVIRONMENTS)
@pytest.mark.parametrize("via", ["put", "promote"])
def test_a_poll_answers_a_new_version_when_its_environment_changes(
    client, db_session, developer, key_of, environment, via
):
    """One write through the routes, and a poll with a key of each environment
    that sends back the ETag it had: a new version with what the write stored
    for the environment written, 304 for the other two."""
    percentages = {"production": 10, "staging": 20, "development": 30}
    flag = _flag(db_session, developer, status=ACTIVE, rollout=10)
    for name in NON_PRODUCTION:
        _setting(db_session, flag, name, True, percentages[name])
    etags = _etags(client, key_of)
    if via == "put":
        stored = 80
        response = _put(
            client, developer, flag.id, environment, {"rollout_percentage": stored}
        )
    else:
        source = "staging" if environment != "staging" else "production"
        stored = percentages[source]
        response = _promote(client, developer, flag.id, source, environment)
    assert response.status_code == 200, response.text

    changed = _poll(client, key_of[environment], etags[environment])
    assert changed.status_code == 200, (
        f"the {environment} poll answered {changed.status_code} after a {via} there"
    )
    assert changed.headers["ETag"] != etags[environment]
    (entry,) = [e for e in changed.json()["flags"] if e["key"] == flag.key]
    assert (entry["active"], entry["rollout_percentage"]) == (True, stored)
    for name, status in _polled(client, key_of, etags).items():
        if name != environment:
            assert status == 304, (
                f"the {name} poll answered {status} after a {via} to {environment}"
            )


@pytest.mark.parametrize("environment", NON_PRODUCTION)
@pytest.mark.parametrize("via", ["put", "promote"])
def test_a_poll_answers_a_new_version_when_a_flag_is_first_configured_there(
    client, db_session, developer, key_of, environment, via
):
    flag = _flag(db_session, developer, status=ACTIVE, rollout=10)
    etags = _etags(client, key_of)
    if via == "put":
        body = {"is_active": True, "rollout_percentage": 80}
        response = _put(client, developer, flag.id, environment, body)
    else:
        response = _promote(client, developer, flag.id, "production", environment)
    assert response.status_code == 200, response.text

    changed = _poll(client, key_of[environment], etags[environment])
    assert changed.status_code == 200, (
        f"the {environment} poll answered {changed.status_code} after a {via} there"
    )
    (entry,) = [e for e in changed.json()["flags"] if e["key"] == flag.key]
    percentage = 80 if via == "put" else 10
    assert (entry["active"], entry["rollout_percentage"]) == (True, percentage)
    polled = _polled(client, key_of, etags)
    expected = {n: 200 if n == environment else 304 for n in FLAG_ENVIRONMENTS}
    assert polled == expected, polled


@pytest.mark.parametrize("environment", FLAG_ENVIRONMENTS)
def test_a_put_answers_through_its_environments_key_and_leaves_the_others(
    client, db_session, developer, key_of, environment
):
    flag = _flag(db_session, developer)
    off = dict.fromkeys(FLAG_ENVIRONMENTS, (False, "inactive"))
    etags = _etags(client, key_of)
    answers = _evaluated(client, flag, key_of)
    assert answers == off, answers

    body = {"is_active": True, "rollout_percentage": 100}
    response = _put(client, developer, flag.id, environment, body)

    assert response.status_code == 200, response.text
    answers = _evaluated(client, flag, key_of)
    assert answers == {**off, environment: (True, "rollout")}, answers
    # The rulesets of the other two are the ones served before it.
    polled = _polled(client, key_of, etags)
    expected = {n: 200 if n == environment else 304 for n in FLAG_ENVIRONMENTS}
    assert polled == expected, polled


def test_the_rules_of_a_staging_put_decide_for_a_staging_key_only(
    client, db_session, developer, key_of
):
    flag = _flag(db_session, developer, status=ACTIVE, rollout=0)
    etags = _etags(client, key_of)
    body = {"is_active": True, "targeting_rules": _rules("US")}
    put = _put(client, developer, flag.id, "staging", body)
    assert put.status_code == 200, put.text

    outside = {"production": (False, "rollout"), "development": (False, "inactive")}
    us = _evaluated(client, flag, key_of, country="US")
    assert us == {**outside, "staging": (True, "targeting_rule")}, us
    ca = _evaluated(client, flag, key_of, country="CA")
    assert ca == {**outside, "staging": (False, "rollout")}, ca
    polled = _polled(client, key_of, etags)
    expected = {"production": 304, "staging": 200, "development": 304}
    assert polled == expected, polled


def test_promotion_and_archive_answer_through_every_key(
    client, db_session, developer, key_of
):
    flag = _flag(db_session, developer)
    off, on = (False, "inactive"), (True, "rollout")

    def put_staging(**body):
        response = _put(client, developer, flag.id, "staging", body)
        assert response.status_code == 200, response.text

    def answers_are(production, staging, development):
        answers = _evaluated(client, flag, key_of)
        assert answers == {
            "production": production,
            "staging": staging,
            "development": development,
        }, answers

    put_staging(is_active=True, rollout_percentage=100)
    answers_are(off, on, off)

    promoted = _promote(client, developer, flag.id, "staging", "production")
    assert promoted.status_code == 200, promoted.text
    answers_are(on, on, off)

    put_staging(is_active=False)
    answers_are(on, off, off)

    put_staging(is_active=True)
    answers_are(on, on, off)

    promoted = _promote(client, developer, flag.id, "staging", "development")
    assert promoted.status_code == 200, promoted.text
    answers_are(on, on, on)
    body = {"flag_ids": [str(flag.id)], "action": "archive"}
    archived = client.post(f"{FLAGS}/bulk-toggle", json=body, headers=_auth(developer))
    assert archived.status_code == 200, archived.text
    answers_are(off, off, off)

    # Unarchiving turns the flag on in none of them.
    unarchived = client.post(f"{FLAGS}/{flag.id}/unarchive", headers=_auth(developer))
    assert unarchived.status_code == 200, unarchived.text
    answers_are(off, off, off)


# --- one transaction, under the flag row's lock -------------------------------


def _statements(test_db, run):
    """The statements a request runs, with its commits and rollbacks."""
    seen = []
    listeners = [
        (
            test_db,
            "before_cursor_execute",
            lambda c, cur, statement, *a: seen.append(" ".join(statement.split())),
        ),
        (test_db, "commit", lambda conn: seen.append("COMMIT")),
        (test_db, "rollback", lambda conn: seen.append("ROLLBACK")),
    ]
    for target, name, listener in listeners:
        sa_event.listen(target, name, listener)
    try:
        run()
    finally:
        for target, name, listener in listeners:
            sa_event.remove(target, name, listener)
    return seen


def _writes(statements):
    """The order of the lock, the writes and the commits, by what each is."""
    order = []
    for statement in statements:
        if "FOR NO KEY UPDATE" in statement:
            order.append("lock")
        elif statement.startswith("INSERT INTO") and "flag_environment" in statement:
            order.append("side row")
        elif statement.startswith("UPDATE") and ".feature_flags" in statement:
            order.append("flag row")
        elif statement.startswith("INSERT INTO") and "audit_logs" in statement:
            order.append("audit entry")
        elif statement.startswith("INSERT INTO") and "audit_events_v2" in statement:
            order.append("compliance event")
        elif statement in ("COMMIT", "ROLLBACK"):
            order.append(statement)
    start = order.index("lock")
    end = order.index("COMMIT", start)
    return order[start : end + 1], order[end + 1 :]


SIDE = ["lock", "side row", "audit entry", "COMMIT"]
PRODUCTION = ["lock", "flag row", "compliance event", "audit entry", "COMMIT"]
TRANSACTIONS = {
    "put staging": SIDE,
    "put production": PRODUCTION,
    "promote into development": SIDE,
    "promote into production": [*PRODUCTION[:-1], "audit entry", "COMMIT"],
}


@pytest.mark.regression
@pytest.mark.parametrize("change", list(TRANSACTIONS))
def test_a_change_and_its_entries_are_one_transaction_under_the_lock(
    client, fresh, test_db, db_session, developer, change
):
    flag = _flag(db_session, developer)
    _setting(db_session, flag, "staging", False, 30)

    def run():
        if change.startswith("put"):
            environment = change.split()[-1]
            response = _put(
                client, developer, flag.id, environment, {"rollout_percentage": 60}
            )
        else:
            target = change.split()[-1]
            response = _promote(client, developer, flag.id, "staging", target)
        assert response.status_code == 200, response.text

    transaction, rest = _writes(_statements(test_db, run))
    assert sorted(transaction) == sorted(TRANSACTIONS[change]), transaction
    assert transaction[0] == "lock" and transaction[-1] == "COMMIT"
    # The change comes before its entries: they are read from the changed row.
    writes = [w for w in transaction if w in ("side row", "flag row")]
    assert transaction.index(writes[0]) == 1, transaction
    assert len(writes) == 1, transaction
    # Nothing is written after the commit.
    assert [w for w in rest if w not in ("COMMIT", "ROLLBACK")] == [], rest


def _holding(test_db, statement, **params):
    """A session of its own holding what *statement* locks."""
    connection = test_db.connect()
    transaction = connection.begin()
    connection.execute(text(statement), params)
    return connection, transaction


def _waiting(test_db, at_least=1):
    """Wait until at least one backend waits for a lock, or give up."""
    deadline = time.monotonic() + 20
    with test_db.connect() as probe:
        while time.monotonic() < deadline:
            waiting = probe.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = "
                    "current_database() AND wait_event_type = 'Lock'"
                )
            ).scalar()
            probe.rollback()
            if waiting >= at_least:
                return True
            time.sleep(0.02)
    return False


def _in_background(run):
    done = {}

    def body():
        done["response"] = run()

    thread = threading.Thread(target=body)
    thread.start()
    return thread, done


@pytest.mark.regression
@pytest.mark.parametrize("environment", FLAG_ENVIRONMENTS)
def test_a_change_waits_for_the_flag_row_and_reads_it_as_committed(
    client, fresh, test_db, db_session, developer, environment
):
    """A change that starts while another session is archiving the flag waits
    for it, then sees the archived flag and refuses to turn it on."""
    flag = _flag(db_session, developer, status=INACTIVE)
    _setting(db_session, flag, "development", False, 3)
    before = _side_rows(fresh, flag.id)
    holder, transaction = _holding(
        test_db,
        f"UPDATE {SCHEMA}.feature_flags SET status = 'ARCHIVED' WHERE id = :id",
        id=flag.id,
    )
    try:
        thread, done = _in_background(
            lambda: _put(client, developer, flag.id, environment, {"is_active": True})
        )
        assert _waiting(test_db), "the change did not wait for the flag row"
        assert done == {}
        transaction.commit()
        thread.join(30)
    finally:
        holder.close()
    response = done["response"]
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == ARCHIVED_FLAG_DETAIL
    production, side = _state(fresh, flag.id)
    assert production[0] == ARCHIVED
    assert side == before
    assert _entries(fresh, flag.id) == []


@pytest.mark.regression
def test_a_bulk_archive_that_waits_for_a_change_turns_off_what_it_turned_on(
    client, fresh, test_db, db_session, developer
):
    """The other order: the side row turned on first is off afterwards."""
    flag = _flag(db_session, developer, status=INACTIVE)
    holder, transaction = _holding(
        test_db,
        f"SELECT id FROM {SCHEMA}.feature_flags WHERE id = :id FOR NO KEY UPDATE",
        id=flag.id,
    )
    try:
        holder.execute(
            text(
                f"INSERT INTO {SCHEMA}.flag_environment_settings (id, created_at, "
                "updated_at, feature_flag_id, environment, is_active) VALUES "
                "(:row, now(), now(), :id, 'staging', true)"
            ),
            {"row": uuid.uuid4(), "id": flag.id},
        )
        thread, done = _in_background(
            lambda: client.post(
                f"{FLAGS}/bulk-toggle",
                json={"flag_ids": [str(flag.id)], "action": "archive"},
                headers=_auth(developer),
            )
        )
        assert _waiting(test_db), "the archive did not wait for the flag row"
        transaction.commit()
        thread.join(30)
    finally:
        holder.close()
    assert done["response"].status_code == 200, done["response"].text
    production, side = _state(fresh, flag.id)
    assert production[0] == ARCHIVED
    assert side["staging"][0] is False, "an archived flag is on in staging"
    (entry,) = _entries(fresh, flag.id)
    assert _values(entry)[1]["environments"] == {"staging": {"is_active": False}}


@pytest.mark.regression
def test_two_changes_of_one_flag_wait_for_each_other(
    client, fresh, test_db, db_session, developer
):
    """The second change waits for the first, then records the first's values
    as its before: the last one is kept, and each entry says what it changed."""
    flag = _flag(db_session, developer)
    _setting(db_session, flag, "development", False, 3)
    development = _side_rows(fresh, flag.id)["development"]
    production = _production(fresh, flag.id)
    reached, release = threading.Event(), threading.Event()
    real = routes.AuditService.record

    def paused(*args, **kwargs):
        if not reached.is_set():
            reached.set()
            assert release.wait(30)
        return real(*args, **kwargs)

    with patch.object(routes.AuditService, "record", paused):
        first, one = _in_background(
            lambda: _put(
                client, developer, flag.id, "staging", {"rollout_percentage": 20}
            )
        )
        assert reached.wait(30), "the first change never wrote its entry"
        second, two = _in_background(
            lambda: _put(
                client, developer, flag.id, "staging", {"rollout_percentage": 70}
            )
        )
        assert _waiting(test_db), "the second change did not wait for the first"
        assert two == {}
        release.set()
        first.join(30)
        second.join(30)
    assert one["response"].status_code == 200, one["response"].text
    assert two["response"].status_code == 200, two["response"].text
    side = _side_rows(fresh, flag.id)
    assert side["staging"][1] == 70
    assert side["development"] == development
    assert _production(fresh, flag.id) == production
    steps = [
        (_values(e)[0].get("rollout_percentage"), _values(e)[1]["rollout_percentage"])
        for e in _entries(fresh, flag.id)
    ]
    assert steps == [(0, 20), (20, 70)], steps


@pytest.mark.regression
def test_a_change_does_not_hold_up_a_production_evaluation(
    client, fresh, test_db, db_session, developer
):
    """The lock is one a row that refers to the flag does not wait for: an
    evaluation stores its metric row while a staging change is in progress."""
    flag = _flag(db_session, developer, status=ACTIVE, rollout=100)
    key = _ruleset_key(db_session, developer)
    reached, release = threading.Event(), threading.Event()
    real = routes.AuditService.record

    def paused(*args, **kwargs):
        reached.set()
        assert release.wait(30)
        return real(*args, **kwargs)

    def evaluations():
        session = fresh()
        try:
            return (
                session.query(RawMetric)
                .filter(
                    RawMetric.feature_flag_id == flag.id,
                    RawMetric.metric_type == MetricType.FLAG_EVALUATION.value,
                )
                .count()
            )
        finally:
            session.close()

    with patch.object(routes.AuditService, "record", paused):
        thread, done = _in_background(
            lambda: _put(client, developer, flag.id, "staging", {"is_active": True})
        )
        assert reached.wait(30), "the change never wrote its entry"
        try:
            # The control: the change does hold the row against another one.
            with test_db.connect() as probe, pytest.raises(OperationalError) as held:
                probe.execute(
                    text(
                        f"SELECT id FROM {SCHEMA}.feature_flags WHERE id = :id "
                        "FOR NO KEY UPDATE NOWAIT"
                    ),
                    {"id": flag.id},
                )
            assert held.value.orig.pgcode == "55P03", held.value
            answer, got = _in_background(
                lambda: client.get(
                    f"{FLAGS}/evaluate/{flag.key}",
                    params={"user_id": "u"},
                    headers={"X-API-Key": key},
                )
            )
            answer.join(10)
            assert not answer.is_alive(), "the evaluation waited for the change"
            assert got["response"].status_code == 200, got["response"].text
            assert got["response"].json()["enabled"] is True
            assert evaluations() >= 1
        finally:
            release.set()
            thread.join(30)
    assert done["response"].status_code == 200, done["response"].text
