"""Every core route that changes something writes its audit entry (#221).

The real routes on Postgres, signed in with local tokens as non-superusers of
the right role (superusers only where the route is superuser-only), read back
in a session of the test's own:

* each request writes exactly the entries listed (normally one) with the
  right action, entity, actor and values, and changes ``audit_events_v2`` by
  exactly what it did before: one event for flag and experiment create,
  update and delete, none for anything else;
* an entry holds no request text it is not allowed to (descriptions, rules,
  passwords, the API key or its hash);
* route sites write after the change commits and fail open: with every audit
  insert refused, the change is kept and one ERROR without values is logged;
* a superuser's change to a user's role, superuser flag or active status is
  written together with its entry or not at all; user changes record the
  superuser flag;
* a Cognito sign-in through ``POST /auth/token`` (against a moto user pool,
  never AWS) writes ``user_login`` with the provider ``cognito`` when the
  identity has an account, and nothing when it has none or is refused.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_cognitoidp
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.api.v1.endpoints import auth as auth_endpoints
from backend.app.api.v1.endpoints.auth import create_local_access_token
from backend.app.core.config import settings
from backend.app.core.metrics import audit_write_failures_total
from backend.app.main import app
from backend.app.middleware import rate_limiter
from backend.app.models.api_key import APIKey
from backend.app.models.audit_log import AuditLog
from backend.app.models.bandit_state import BanditState
from backend.app.models.experiment import Experiment, ExperimentStatus
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.global_holdout import GlobalHoldout
from backend.app.models.mutual_exclusion_group import MutualExclusionGroup
from backend.app.models.rollout_schedule import RolloutSchedule
from backend.app.models.segment import Segment
from backend.app.models.user import User, UserRole
from backend.app.services.audit_service import AuditService
from backend.app.services.cognito_accounts import cognito_external_id

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

SCHEMA = "test_experimentation"
P = "ae221"
V1 = "/api/v1"
PASSWORD = "Ae221-Passw0rd"


# --- fixtures -----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _local_auth(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "CACHE_ENABLED", False)


@pytest.fixture
def fresh(test_db):
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


def _make_user(db_session, role, *, superuser=False, email=True, username=True):
    from backend.app.api.v1.endpoints.users import get_password_hash

    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"{P}_{role.name.lower()}_{suffix}" if username else None,
        email=f"{P}_{suffix}@example.com" if email else None,
        full_name="Audit Events User",
        hashed_password=get_password_hash(PASSWORD),
        is_active=True,
        is_superuser=superuser,
        role=role,
    )
    db_session.add(user)
    db_session.commit()
    return user


@pytest.fixture
def developer(db_session):
    return _make_user(db_session, UserRole.DEVELOPER)


@pytest.fixture
def admin(db_session):
    """ADMIN by role, not a superuser."""
    return _make_user(db_session, UserRole.ADMIN)


@pytest.fixture
def superuser(db_session):
    return _make_user(db_session, UserRole.ADMIN, superuser=True)


@pytest.fixture
def target(db_session):
    """A user the superuser changes."""
    return _make_user(db_session, UserRole.VIEWER)


@pytest.fixture(autouse=True)
def _cleanup(db_session):
    """Leave nothing behind: the other suites list flags, holdouts and users."""
    yield
    db_session.rollback()
    s = db_session
    flag_ids = [
        r.id for r in s.query(FeatureFlag.id).filter(FeatureFlag.key.like(f"{P}-%"))
    ]
    for schedule in s.query(RolloutSchedule).filter(
        RolloutSchedule.feature_flag_id.in_(flag_ids)
    ):
        s.delete(schedule)
    s.commit()
    users = s.query(User).filter(User.username.like(f"{P}_%")).all()
    user_ids = [u.id for u in users]
    # Clones are named "Copy of ...": every experiment these users own goes,
    # so none is left with a NULL owner once they are deleted.
    exps = (
        s.query(Experiment)
        .filter(Experiment.name.like(f"{P}%") | Experiment.owner_id.in_(user_ids))
        .all()
    )
    exp_ids = [e.id for e in exps]
    s.query(BanditState).filter(BanditState.experiment_id.in_(exp_ids)).delete(
        synchronize_session=False
    )
    for exp in exps:
        s.delete(exp)
    s.commit()
    s.query(FeatureFlag).filter(FeatureFlag.id.in_(flag_ids)).delete(
        synchronize_session=False
    )
    for model in (GlobalHoldout, MutualExclusionGroup, Segment):
        s.query(model).filter(model.name.like(f"{P}%")).delete(
            synchronize_session=False
        )
    s.query(APIKey).filter(APIKey.user_id.in_(user_ids)).delete(
        synchronize_session=False
    )
    s.query(AuditLog).filter(
        AuditLog.user_email.like(f"{P}%") | AuditLog.entity_name.like(f"{P}%")
    ).delete(synchronize_session=False)
    for user in users:
        s.delete(user)
    s.commit()


@pytest.fixture
def audit_insert_refused(db_session):
    """Every insert into ``audit_logs`` raises while the test runs."""
    db_session.execute(
        text(
            f"CREATE OR REPLACE FUNCTION {SCHEMA}.ae221_refuse_audit() "
            "RETURNS trigger LANGUAGE plpgsql AS "
            "$$ BEGIN RAISE EXCEPTION 'ae221 refused'; END $$"
        )
    )
    db_session.execute(
        text(
            f"CREATE TRIGGER ae221_refuse_audit BEFORE INSERT ON {SCHEMA}.audit_logs "
            f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.ae221_refuse_audit()"
        )
    )
    db_session.commit()

    def drop():
        db_session.rollback()
        db_session.execute(
            text(f"DROP TRIGGER IF EXISTS ae221_refuse_audit ON {SCHEMA}.audit_logs")
        )
        db_session.execute(
            text(f"DROP FUNCTION IF EXISTS {SCHEMA}.ae221_refuse_audit()")
        )
        db_session.commit()

    try:
        yield drop
    finally:
        drop()


# --- helpers ------------------------------------------------------------------


def _auth(user):
    return {"Authorization": f"Bearer {create_local_access_token(user)}"}


class Written:
    """The audit rows and ``audit_events_v2`` events one request produced."""

    def __init__(self, fresh):
        self.fresh = fresh
        self.since = datetime.now(timezone.utc) - timedelta(milliseconds=1)
        self.v2_before = self._v2()

    def _v2(self):
        session = self.fresh()
        try:
            return session.execute(
                text(f"SELECT count(*) FROM {SCHEMA}.audit_events_v2")
            ).scalar()
        finally:
            session.close()

    def rows(self):
        """Rows stamped between the mark and now. The upper bound matters:
        other suites leave rows dated in the future."""
        until = datetime.now(timezone.utc) + timedelta(milliseconds=1)
        session = self.fresh()
        try:
            return (
                session.query(AuditLog)
                .filter(AuditLog.timestamp >= self.since)
                .filter(AuditLog.timestamp <= until)
                .order_by(AuditLog.timestamp)
                .all()
            )
        finally:
            session.close()

    def v2_delta(self):
        return self._v2() - self.v2_before


def _one(written, action, entity_type, entity_id, actor, *, v2_delta=0):
    """Exactly one row since the mark, for this action, entity and actor."""
    rows = written.rows()
    assert len(rows) == 1, (
        f"expected 1, found {len(rows)}: {[(r.action_type, r.entity_type) for r in rows]}"
    )
    row = rows[0]
    assert row.action_type == action
    assert row.entity_type == entity_type
    assert str(row.entity_id) == str(entity_id)
    assert row.user_id == actor.id
    assert row.user_email == (actor.email or actor.username or "system")
    assert written.v2_delta() == v2_delta, f"audit_events_v2 delta {written.v2_delta()}"
    return row


def _values(row):
    def load(v):
        return json.loads(v) if v else None

    return load(row.old_value), load(row.new_value)


def _flag(db_session, status=FeatureFlagStatus.INACTIVE, owner=None):
    flag = FeatureFlag(
        key=f"{P}-{uuid.uuid4().hex[:10]}",
        name=f"{P} flag",
        status=status,
        rollout_percentage=0,
        owner_id=owner.id if owner else None,
    )
    db_session.add(flag)
    db_session.commit()
    return flag


def _experiment_payload(name=None):
    return {
        "name": name or f"{P} exp {uuid.uuid4().hex[:6]}",
        "description": "ae221 description",
        "hypothesis": "ae221 hypothesis",
        "experiment_type": "a_b",
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


def _create_experiment(client, user):
    response = client.post(
        f"{V1}/experiments/", json=_experiment_payload(), headers=_auth(user)
    )
    assert response.status_code == 201, response.text
    return response.json()


def _put_body(user, **changes):
    """A user PUT body: the schema needs the username and email, unchanged."""
    return {"username": user.username, "email": user.email, **changes}


def _rules(value="US"):
    return {
        "logical_operator": "AND",
        "groups": [
            {
                "logical_operator": "AND",
                "conditions": [
                    {"attribute": "country", "operator": "equals", "value": value}
                ],
            }
        ],
    }


# --- G3: feature flags --------------------------------------------------------


@pytest.mark.parametrize("path", ["/feature-flags/", "/feature-flags"])
def test_flag_create(client, fresh, developer, path):
    w = Written(fresh)
    response = client.post(
        f"{V1}{path}",
        json={"key": f"{P}-{uuid.uuid4().hex[:8]}", "name": f"{P} created"},
        headers=_auth(developer),
    )
    assert response.status_code == 201, response.text
    row = _one(
        w,
        "feature_flag_create",
        "feature_flag",
        response.json()["id"],
        developer,
        v2_delta=1,
    )
    old, new = _values(row)
    assert old is None
    assert new["key"] == response.json()["key"]
    assert new["status"] == "INACTIVE"


def test_flag_update_records_only_what_changed(client, fresh, db_session, developer):
    flag = _flag(db_session)
    w = Written(fresh)
    response = client.put(
        f"{V1}/feature-flags/{flag.id}",
        json={"name": f"{P} renamed", "description": "ae221 secret words"},
        headers=_auth(developer),
    )
    assert response.status_code == 200, response.text
    row = _one(w, "feature_flag_update", "feature_flag", flag.id, developer, v2_delta=1)
    old, new = _values(row)
    assert old == {"name": f"{P} flag"}
    assert new == {"name": f"{P} renamed", "changed_fields": ["description"]}


def test_flag_delete(client, fresh, db_session, developer):
    flag = _flag(db_session)
    w = Written(fresh)
    response = client.delete(f"{V1}/feature-flags/{flag.id}", headers=_auth(developer))
    assert response.status_code == 204, response.text
    row = _one(w, "feature_flag_delete", "feature_flag", flag.id, developer, v2_delta=1)
    assert _values(row)[0]["key"] == flag.key


@pytest.mark.parametrize(
    "verb,start,action,end",
    [
        ("activate", FeatureFlagStatus.INACTIVE, "feature_flag_activate", "ACTIVE"),
        ("deactivate", FeatureFlagStatus.ACTIVE, "feature_flag_deactivate", "INACTIVE"),
        ("toggle", FeatureFlagStatus.INACTIVE, "toggle_enable", None),
        ("enable", FeatureFlagStatus.INACTIVE, "toggle_enable", None),
        ("disable", FeatureFlagStatus.ACTIVE, "toggle_disable", None),
        ("unarchive", FeatureFlagStatus.ARCHIVED, "feature_flag_update", None),
    ],
)
def test_flag_status_verbs(
    client, fresh, db_session, developer, verb, start, action, end
):
    flag = _flag(db_session, start)
    w = Written(fresh)
    response = client.post(
        f"{V1}/feature-flags/{flag.id}/{verb}", json={}, headers=_auth(developer)
    )
    assert response.status_code == 200, response.text
    row = _one(w, action, "feature_flag", flag.id, developer)
    if end:
        assert _values(row) == ({"status": start.value}, {"status": end})


def test_flag_activate_that_changes_nothing_writes_nothing(
    client, fresh, db_session, developer
):
    flag = _flag(db_session, FeatureFlagStatus.ACTIVE)
    w = Written(fresh)
    response = client.post(
        f"{V1}/feature-flags/{flag.id}/activate", headers=_auth(developer)
    )
    assert response.status_code == 200, response.text
    assert w.rows() == []


def test_bulk_toggle_one_entry_per_flag(client, fresh, db_session, developer):
    flags = [_flag(db_session) for _ in range(2)]
    w = Written(fresh)
    response = client.post(
        f"{V1}/feature-flags/bulk-toggle",
        json={"flag_ids": [str(f.id) for f in flags], "action": "enable"},
        headers=_auth(developer),
    )
    assert response.status_code == 200, response.text
    rows = w.rows()
    assert sorted(str(r.entity_id) for r in rows) == sorted(str(f.id) for f in flags)
    assert {r.action_type for r in rows} == {"toggle_enable"}
    assert sorted(response.json()["audit_log_ids"]) == sorted(str(r.id) for r in rows)
    assert w.v2_delta() == 0


def test_rollout_schedule_routes_record_on_the_flag(
    client, fresh, db_session, developer
):
    """The ten schedule and stage routes, in an order each one accepts."""
    flag = _flag(db_session, owner=developer)
    h = _auth(developer)
    soon = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    later = (datetime.now(timezone.utc) + timedelta(hours=72)).isoformat()

    def step(method, path, verb, json_body=None, status=(200, 201, 204)):
        w = Written(fresh)
        response = client.request(
            method, f"{V1}/rollout-schedules{path}", json=json_body, headers=h
        )
        assert response.status_code in status, (verb, response.text)
        row = _one(w, "feature_flag_update", "feature_flag", flag.id, developer)
        assert row.reason == f"rollout schedule {verb}"
        return response, row

    created, _ = step(
        "POST",
        "/",
        "created",
        {
            "name": f"{P} schedule",
            "feature_flag_id": str(flag.id),
            "start_date": soon,
            "end_date": later,
            "max_percentage": 100,
            "min_stage_duration": 1,
            "stages": [
                {
                    "name": "One",
                    "stage_order": 1,
                    "target_percentage": 25,
                    "trigger_type": "manual",
                },
                {
                    "name": "Two",
                    "stage_order": 2,
                    "target_percentage": 100,
                    "trigger_type": "manual",
                },
            ],
        },
    )
    schedule = created.json()
    sid = schedule["id"]
    first = next(s for s in schedule["stages"] if s["stage_order"] == 1)
    step("PUT", f"/{sid}", "updated", {"name": f"{P} schedule renamed"})
    added, _ = step(
        "POST",
        f"/{sid}/stages",
        "stage added",
        {
            "name": "Three",
            "stage_order": 3,
            "target_percentage": 100,
            "trigger_type": "manual",
        },
    )
    stage3 = added.json()["id"]
    step("PUT", f"/stages/{stage3}", "stage updated", {"name": "Three renamed"})
    step("DELETE", f"/stages/{stage3}", "stage deleted")
    step("POST", f"/{sid}/activate", "activated")
    _, advanced = step("POST", f"/stages/{first['id']}/advance", "stage advanced")
    assert _values(advanced) == ({"rollout_percentage": 0}, {"rollout_percentage": 25})
    step("POST", f"/{sid}/pause", "paused")
    step("POST", f"/{sid}/cancel", "cancelled")
    step("DELETE", f"/{sid}", "deleted")


# --- G3: experiments ----------------------------------------------------------


def test_experiment_create_update_delete(client, fresh, developer):
    w = Written(fresh)
    exp = _create_experiment(client, developer)
    row = _one(w, "experiment_create", "experiment", exp["id"], developer, v2_delta=1)
    created = _values(row)[1]
    assert created["status"] == "draft"
    assert {"correction_method", "confidence_level"} <= set(created)

    w = Written(fresh)
    response = client.put(
        f"{V1}/experiments/{exp['id']}",
        json={"name": f"{P} exp renamed", "hypothesis": "ae221 new hypothesis"},
        headers=_auth(developer),
    )
    assert response.status_code == 200, response.text
    row = _one(w, "experiment_update", "experiment", exp["id"], developer, v2_delta=1)
    assert _values(row) == (
        {"name": exp["name"]},
        {"name": f"{P} exp renamed", "changed_fields": ["hypothesis"]},
    )

    w = Written(fresh)
    response = client.delete(
        f"{V1}/experiments/{exp['id']}",
        params={"experiment_key": exp["id"]},
        headers=_auth(developer),
    )
    assert response.status_code == 204, response.text
    _one(w, "experiment_delete", "experiment", exp["id"], developer, v2_delta=1)


def test_a_design_save_records_the_save_but_not_which_parts_changed(
    client, fresh, developer
):
    """Today a design save names neither variants nor metrics in the entry.

    A PUT of ``{variants, metrics}`` on a draft writes one ``experiment_update``
    entry with the user and the time, but ``old_value`` and ``new_value`` are
    both null, so ``changed_fields`` does not name the variants or the metrics.
    The audit-logging page says so (#1782). This pins that fact; #1782 will
    name them, and this test then changes with it.
    """
    exp = _create_experiment(client, developer)

    w = Written(fresh)
    payload = _experiment_payload()
    payload["variants"][0]["name"] = "Control renamed"
    payload["metrics"][0]["name"] = "Conversion Rate renamed"
    response = client.put(
        f"{V1}/experiments/{exp['id']}",
        json={"variants": payload["variants"], "metrics": payload["metrics"]},
        headers=_auth(developer),
    )
    assert response.status_code == 200, response.text
    row = _one(w, "experiment_update", "experiment", exp["id"], developer, v2_delta=1)
    assert row.timestamp is not None
    old, new = _values(row)
    named = set((old or {}).keys()) | set((new or {}).keys())
    assert "variants" not in named and "metrics" not in named
    assert "variants" not in (new or {}).get("changed_fields", [])
    assert "metrics" not in (new or {}).get("changed_fields", [])


def test_experiment_lifecycle(client, fresh, developer):
    exp = _create_experiment(client, developer)
    h = _auth(developer)
    for verb, action, old, new in [
        ("start", "experiment_start", "draft", "active"),
        ("pause", "experiment_pause", "active", "paused"),
        ("start", "experiment_start", "paused", "active"),
        ("complete", "experiment_complete", "active", "completed"),
    ]:
        w = Written(fresh)
        response = client.post(f"{V1}/experiments/{exp['id']}/{verb}", headers=h)
        assert response.status_code == 200, (verb, response.text)
        row = _one(w, action, "experiment", exp["id"], developer)
        old_v, new_v = _values(row)
        assert old_v["status"] == old and new_v["status"] == new

    w = Written(fresh)
    response = client.post(f"{V1}/experiments/{exp['id']}/archive", headers=h)
    assert response.status_code == 200, response.text
    row = _one(w, "experiment_update", "experiment", exp["id"], developer)
    assert row.reason == "archive"


def test_experiment_schedule_metadata_and_clone(client, fresh, developer):
    exp = _create_experiment(client, developer)
    h = _auth(developer)
    start = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    end = (datetime.now(timezone.utc) + timedelta(days=8)).isoformat()

    w = Written(fresh)
    response = client.put(
        f"{V1}/experiments/{exp['id']}/schedule",
        json={"start_date": start, "end_date": end},
        headers=h,
    )
    assert response.status_code == 200, response.text
    row = _one(w, "experiment_update", "experiment", exp["id"], developer)
    assert row.reason == "schedule"
    assert set(_values(row)[1]) == {"start_date", "end_date"}

    w = Written(fresh)
    response = client.post(
        f"{V1}/experiments/{exp['id']}/metadata", json={"note": "ae221"}, headers=h
    )
    assert response.status_code == 200, response.text
    assert (
        _one(w, "experiment_update", "experiment", exp["id"], developer).reason
        == "metadata"
    )

    w = Written(fresh)
    response = client.post(f"{V1}/experiments/{exp['id']}/clone", headers=h)
    assert response.status_code == 201, response.text
    row = _one(w, "experiment_create", "experiment", response.json()["id"], developer)
    assert row.reason == f"clone of {exp['id']}"


def test_wizard_submit_records_the_created_experiment(
    client, fresh, db_session, developer, monkeypatch
):
    from backend.app.services import experiment_wizard_service as wizard

    exp = Experiment(
        name=f"{P} wizard", owner_id=developer.id, status=ExperimentStatus.DRAFT
    )
    db_session.add(exp)
    db_session.commit()
    monkeypatch.setattr(
        wizard.ExperimentWizardService, "get_draft", staticmethod(lambda *a, **k: {})
    )
    monkeypatch.setattr(
        wizard.ExperimentWizardService,
        "validate_and_submit",
        staticmethod(lambda *a, **k: {"success": True, "experiment_id": str(exp.id)}),
    )
    w = Written(fresh)
    response = client.post(f"{V1}/wizard/drafts/d1/submit", headers=_auth(developer))
    assert response.status_code == 200, response.text
    row = _one(w, "experiment_create", "experiment", exp.id, developer)
    assert row.reason == "submitted from the wizard"


def test_bandit_weights_override(client, fresh, db_session, admin):
    exp = Experiment(
        name=f"{P} bandit",
        owner_id=admin.id,
        status=ExperimentStatus.ACTIVE,
        optimization_type="thompson_sampling",
    )
    db_session.add(exp)
    db_session.commit()
    w = Written(fresh)
    response = client.put(
        f"{V1}/bandit/{exp.id}/weights",
        json={"weights": {"a": 0.5, "b": 0.5}},
        headers=_auth(admin),
    )
    assert response.status_code == 200, response.text
    assert (
        _one(w, "experiment_update", "experiment", exp.id, admin).reason
        == "bandit weights"
    )


# --- G3: API keys -------------------------------------------------------------


def test_api_key_create_and_revoke_hold_no_part_of_the_key(client, fresh, developer):
    w = Written(fresh)
    response = client.post(
        f"{V1}/api-keys", json={"name": f"{P} key"}, headers=_auth(developer)
    )
    assert response.status_code == 201, response.text
    created = response.json()
    row = _one(w, "api_key_create", "api_key", created["id"], developer)
    session = fresh()
    try:
        stored_hash = (
            session.query(APIKey.key).filter(APIKey.id == created["id"]).scalar()
        )
    finally:
        session.close()

    w2 = Written(fresh)
    response = client.delete(f"{V1}/api-keys/{created['id']}", headers=_auth(developer))
    assert response.status_code == 204, response.text
    revoked = _one(w2, "api_key_revoke", "api_key", created["id"], developer)

    for entry in (row, revoked):
        for column in (
            entry.entity_name,
            entry.old_value,
            entry.new_value,
            entry.reason,
        ):
            for secret in (created["key"], created["prefix"], stored_hash):
                assert secret not in (column or "")


# --- G3: holdouts -------------------------------------------------------------


def test_holdout_create_update_and_activation(client, fresh, admin):
    h = _auth(admin)

    def create(name):
        w = Written(fresh)
        response = client.post(
            f"{V1}/holdout",
            json={"name": name, "holdout_percentage": 5, "is_active": False},
            headers=h,
        )
        assert response.status_code == 201, response.text
        _one(w, "holdout_create", "holdout", response.json()["id"], admin)
        return response.json()["id"]

    a = create(f"{P} holdout A")
    b = create(f"{P} holdout B")

    w = Written(fresh)
    response = client.put(
        f"{V1}/holdout/{a}", json={"holdout_percentage": 7}, headers=h
    )
    assert response.status_code == 200, response.text
    row = _one(w, "holdout_update", "holdout", a, admin)
    assert _values(row) == ({"holdout_percentage": 5}, {"holdout_percentage": 7})

    w = Written(fresh)
    response = client.put(f"{V1}/holdout/{a}", json={"is_active": True}, headers=h)
    assert response.status_code == 200, response.text
    # The activation timestamp belongs to the activate entry, not a second one.
    old, new = _values(_one(w, "holdout_activate", "holdout", a, admin))
    assert old == {"is_active": False, "activated_at": None}
    assert set(new) == {"is_active", "activated_at"}
    assert new["is_active"] is True and new["activated_at"]

    # Activating B turns A off: B's entry and one for A.
    w = Written(fresh)
    response = client.put(f"{V1}/holdout/{b}", json={"is_active": True}, headers=h)
    assert response.status_code == 200, response.text
    rows = {(r.action_type, str(r.entity_id)) for r in w.rows()}
    assert rows == {("holdout_activate", b), ("holdout_deactivate", a)}
    assert w.v2_delta() == 0

    w = Written(fresh)
    response = client.put(f"{V1}/holdout/{b}", json={"is_active": False}, headers=h)
    assert response.status_code == 200, response.text
    old, new = _values(_one(w, "holdout_deactivate", "holdout", b, admin))
    assert old == {"is_active": True, "deactivated_at": None}
    assert set(new) == {"is_active", "deactivated_at"}
    assert new["is_active"] is False and new["deactivated_at"]


def test_holdout_post_activation_records_the_one_it_ends(client, fresh, admin):
    """POST with is_active true ends the active holdout: one deactivate for it."""
    h = _auth(admin)

    def create_active(name):
        response = client.post(
            f"{V1}/holdout",
            json={"name": name, "holdout_percentage": 5, "is_active": True},
            headers=h,
        )
        assert response.status_code == 201, response.text
        return response.json()["id"]

    a = create_active(f"{P} holdout post A")

    w = Written(fresh)
    b = create_active(f"{P} holdout post B")
    rows = {(r.action_type, str(r.entity_id)): r for r in w.rows()}
    assert set(rows) == {("holdout_create", b), ("holdout_deactivate", a)}
    assert w.v2_delta() == 0

    ended = rows[("holdout_deactivate", a)]
    assert ended.user_id == admin.id
    assert _values(ended) == ({"is_active": True}, {"is_active": False})
    _, created = _values(rows[("holdout_create", b)])
    assert created["is_active"] is True and created["activated_at"]
    assert created["deactivated_at"] is None

    session = fresh()
    try:
        stored = session.get(GlobalHoldout, uuid.UUID(a))
        assert stored.is_active is False and stored.deactivated_at is not None
    finally:
        session.close()


# --- G3: mutual exclusion groups ----------------------------------------------


def test_exclusion_group_routes(client, fresh, developer, admin):
    h = _auth(developer)
    w = Written(fresh)
    response = client.post(
        f"{V1}/mutual-exclusion-groups",
        json={"name": f"{P} group", "description": "ae221 group text"},
        headers=h,
    )
    assert response.status_code == 201, response.text
    gid = response.json()["id"]
    _one(w, "mutual_exclusion_group_create", "mutual_exclusion_group", gid, developer)

    w = Written(fresh)
    response = client.put(
        f"{V1}/mutual-exclusion-groups/{gid}", json={"name": f"{P} group 2"}, headers=h
    )
    assert response.status_code == 200, response.text
    _one(w, "mutual_exclusion_group_update", "mutual_exclusion_group", gid, developer)

    exp = _create_experiment(client, developer)
    w = Written(fresh)
    response = client.post(
        f"{V1}/mutual-exclusion-groups/{gid}/experiments",
        json={"experiment_id": exp["id"]},
        headers=h,
    )
    assert response.status_code == 200, response.text
    row = _one(
        w, "mutual_exclusion_group_update", "mutual_exclusion_group", gid, developer
    )
    assert _values(row) == (None, {"experiment_id": exp["id"]})
    assert row.reason == "experiment added"

    w = Written(fresh)
    response = client.delete(
        f"{V1}/mutual-exclusion-groups/{gid}/experiments/{exp['id']}", headers=h
    )
    assert response.status_code == 200, response.text
    row = _one(
        w, "mutual_exclusion_group_update", "mutual_exclusion_group", gid, developer
    )
    assert _values(row) == ({"experiment_id": exp["id"]}, None)

    w = Written(fresh)
    response = client.delete(
        f"{V1}/mutual-exclusion-groups/{gid}", headers=_auth(admin)
    )
    assert response.status_code == 200, response.text
    row = _one(
        w, "mutual_exclusion_group_archive", "mutual_exclusion_group", gid, admin
    )
    assert _values(row)[1] == {"status": "archived"}


# --- G3: segments -------------------------------------------------------------


def test_segment_create_update_archive(client, fresh, developer):
    h = _auth(developer)
    w = Written(fresh)
    response = client.post(
        f"{V1}/segments",
        json={"name": f"{P} segment", "description": "ae221 seg", "rules": _rules()},
        headers=h,
    )
    assert response.status_code in (200, 201), response.text
    sid = response.json()["id"]
    _one(w, "segment_create", "segment", sid, developer)

    w = Written(fresh)
    response = client.put(
        f"{V1}/segments/{sid}", json={"rules": _rules("ae221-CA")}, headers=h
    )
    assert response.status_code == 200, response.text
    row = _one(w, "segment_update", "segment", sid, developer)
    assert _values(row) == (None, {"changed_fields": ["rules"]})

    w = Written(fresh)
    response = client.delete(f"{V1}/segments/{sid}", headers=h)
    assert response.status_code == 204, response.text
    row = _one(w, "segment_archive", "segment", sid, developer)
    assert _values(row)[1] == {"status": "archived"}


#: A user id that must never appear in an audit entry (#440, #221 EM C8).
MEMBER_MARKER = "ae221-member-marker-7f3c"


@pytest.mark.regression
def test_segment_members_add_and_remove_record_counts_only(client, fresh, developer):
    h = _auth(developer)
    response = client.post(
        f"{V1}/segments",
        json={"name": f"{P} id list", "kind": "id_list"},
        headers=h,
    )
    assert response.status_code == 201, response.text
    sid = response.json()["id"]
    ids = [MEMBER_MARKER, f"{MEMBER_MARKER}-2", MEMBER_MARKER]

    w = Written(fresh)
    response = client.post(f"{V1}/segments/{sid}/members", json={"add": ids}, headers=h)
    assert response.status_code == 200, response.text
    row = _one(w, "segment_update", "segment", sid, developer)
    assert _values(row) == (
        None,
        {"added": 2, "already_members": 0, "member_count": 2},
    )
    assert row.reason == "members added"
    assert MEMBER_MARKER not in json.dumps(
        [row.old_value, row.new_value, row.reason, row.entity_name]
    )

    w = Written(fresh)
    response = client.post(
        f"{V1}/segments/{sid}/members/remove",
        json={"remove": [MEMBER_MARKER, "ae221-never-added"]},
        headers=h,
    )
    assert response.status_code == 200, response.text
    row = _one(w, "segment_update", "segment", sid, developer)
    assert _values(row) == (None, {"removed": 1, "not_members": 1, "member_count": 1})
    assert row.reason == "members removed"
    assert MEMBER_MARKER not in json.dumps(
        [row.old_value, row.new_value, row.reason, row.entity_name]
    )


# --- G3: users and sign-in ----------------------------------------------------


def test_user_create(client, fresh, superuser):
    w = Written(fresh)
    response = client.post(
        f"{V1}/users/",
        json={
            "username": f"{P}_made_{uuid.uuid4().hex[:6]}",
            "email": f"{P}_made_{uuid.uuid4().hex[:6]}@example.com",
            "password": PASSWORD,
            "role": "ANALYST",
        },
        headers=_auth(superuser),
    )
    assert response.status_code == 201, response.text
    row = _one(w, "user_create", "user", response.json()["id"], superuser)
    new = _values(row)[1]
    assert new["role"] == "ANALYST" and new["is_superuser"] is False
    assert PASSWORD not in (row.new_value or "")


@pytest.mark.regression
@pytest.mark.parametrize("route", ["/admin/users/{id}", "/users/{id}"])
def test_superuser_flag_through_put_is_recorded(
    client, fresh, superuser, target, route
):
    """User changes record the superuser flag."""
    w = Written(fresh)
    response = client.put(
        f"{V1}{route.format(id=target.id)}",
        json=_put_body(target, is_superuser=True),
        headers=_auth(superuser),
    )
    assert response.status_code == 200, response.text
    row = _one(w, "role_assign", "user", target.id, superuser)
    assert _values(row) == (
        {"role": "VIEWER", "is_superuser": False},
        {"role": "VIEWER", "is_superuser": True},
    )


@pytest.mark.parametrize("route", ["/admin/users/{id}", "/users/{id}"])
def test_active_status_through_put_is_recorded(client, fresh, superuser, target, route):
    w = Written(fresh)
    response = client.put(
        f"{V1}{route.format(id=target.id)}",
        json=_put_body(target, is_active=False),
        headers=_auth(superuser),
    )
    assert response.status_code == 200, response.text
    row = _one(w, "user_deactivate", "user", target.id, superuser)
    assert _values(row) == ({"is_active": True}, {"is_active": False})


def test_put_that_changes_neither_writes_nothing(client, fresh, superuser, target):
    w = Written(fresh)
    response = client.put(
        f"{V1}/admin/users/{target.id}",
        json=_put_body(target, full_name="Another Name"),
        headers=_auth(superuser),
    )
    assert response.status_code == 200, response.text
    assert w.rows() == []


def test_patch_role_and_active(client, fresh, superuser, target):
    h = _auth(superuser)
    w = Written(fresh)
    response = client.patch(
        f"{V1}/admin/users/{target.id}", json={"role": "ANALYST"}, headers=h
    )
    assert response.status_code == 200, response.text
    row = _one(w, "role_assign", "user", target.id, superuser)
    assert _values(row) == (
        {"role": "VIEWER", "is_superuser": False},
        {"role": "ANALYST", "is_superuser": False},
    )

    w = Written(fresh)
    response = client.patch(
        f"{V1}/admin/users/{target.id}",
        json={"role": "DEVELOPER", "is_active": False},
        headers=h,
    )
    assert response.status_code == 200, response.text
    assert sorted(r.action_type for r in w.rows()) == ["role_assign", "user_deactivate"]
    assert w.v2_delta() == 0

    w = Written(fresh)
    response = client.patch(
        f"{V1}/admin/users/{target.id}", json={"is_active": True}, headers=h
    )
    assert response.status_code == 200, response.text
    _one(w, "user_activate", "user", target.id, superuser)


@pytest.mark.parametrize("route", ["/admin/users/{id}", "/users/{id}"])
def test_user_delete(client, fresh, superuser, target, route):
    w = Written(fresh)
    target_id = target.id
    response = client.delete(
        f"{V1}{route.format(id=target_id)}", headers=_auth(superuser)
    )
    assert response.status_code == 204, response.text
    row = _one(w, "user_delete", "user", target_id, superuser)
    assert _values(row)[0]["username"] == target.username


def test_deleting_your_own_account_is_recorded_without_a_user_id(
    client, fresh, db_session
):
    user = _make_user(db_session, UserRole.VIEWER)
    w = Written(fresh)
    response = client.delete(f"{V1}/users/{user.id}", headers=_auth(user))
    assert response.status_code == 204, response.text
    rows = w.rows()
    assert len(rows) == 1
    assert rows[0].action_type == "user_delete"
    assert rows[0].user_id is None and rows[0].user_email == user.email


@pytest.mark.parametrize("route", ["login", "token"])
def test_local_sign_in(client, fresh, developer, route):
    w = Written(fresh)
    if route == "login":
        response = client.post(
            f"{V1}/auth/login", json={"email": developer.email, "password": PASSWORD}
        )
    else:
        response = client.post(
            f"{V1}/auth/token",
            data={"username": developer.email, "password": PASSWORD},
        )
    assert response.status_code == 200, response.text
    row = _one(w, "user_login", "user", developer.id, developer)
    assert _values(row) == (None, {"provider": "local"})
    for column in (row.old_value, row.new_value, row.reason, row.entity_name):
        assert PASSWORD not in (column or "")
        assert response.json()["access_token"] not in (column or "")


def test_failed_sign_in_writes_nothing(client, fresh, developer):
    w = Written(fresh)
    response = client.post(
        f"{V1}/auth/login", json={"email": developer.email, "password": "wrong-Pass1"}
    )
    assert response.status_code == 401
    assert w.rows() == []


# --- G3 and G5: Cognito sign-in -------------------------------------------------


@pytest.fixture
def cognito_pool(monkeypatch):
    """``AUTH_PROVIDER=cognito`` against a moto user pool; no AWS call is made.

    Yields ``add_user(username, email)``, which creates a confirmed user in the
    pool with the password ``PASSWORD`` and returns its Cognito ``sub``.
    """
    for name in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "cognito")
    allow_all = {"return_value": (True, 999)}
    with (
        mock_cognitoidp(),
        patch.object(rate_limiter.RedisRateLimiter, "is_allowed", **allow_all),
        patch.object(rate_limiter.SlidingWindowRateLimiter, "is_allowed", **allow_all),
    ):
        idp = boto3.client("cognito-idp", region_name="us-east-1")
        pool_id = idp.create_user_pool(PoolName=f"{P}-pool")["UserPool"]["Id"]
        client_id = idp.create_user_pool_client(
            UserPoolId=pool_id,
            ClientName=f"{P}-client",
            ExplicitAuthFlows=["ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"],
            GenerateSecret=False,
        )["UserPoolClient"]["ClientId"]
        monkeypatch.setenv("COGNITO_USER_POOL_ID", pool_id)
        monkeypatch.setenv("COGNITO_CLIENT_ID", client_id)

        def add_user(username, email):
            idp.admin_create_user(
                UserPoolId=pool_id,
                Username=username,
                UserAttributes=[{"Name": "email", "Value": email}],
                MessageAction="SUPPRESS",
            )
            idp.admin_set_user_password(
                UserPoolId=pool_id, Username=username, Password=PASSWORD, Permanent=True
            )
            attributes = idp.admin_get_user(UserPoolId=pool_id, Username=username)[
                "UserAttributes"
            ]
            return next(a["Value"] for a in attributes if a["Name"] == "sub")

        yield add_user


def _cognito_account(db_session, add_user):
    """A user in the pool whose account is linked to it, as an administrator
    links one (``external_id = "cognito:<sub>"``, no local password)."""
    user = _make_user(db_session, UserRole.DEVELOPER)
    sub = add_user(user.username, user.email)
    user.external_id = cognito_external_id(sub)
    user.hashed_password = None
    db_session.commit()
    return user


def _cognito_sign_in(client, username, password=PASSWORD):
    return client.post(
        f"{V1}/auth/token", data={"username": username, "password": password}
    )


def test_cognito_sign_in_with_an_account_is_recorded(
    client, fresh, db_session, cognito_pool
):
    user = _cognito_account(db_session, cognito_pool)
    w = Written(fresh)
    response = _cognito_sign_in(client, user.username)
    assert response.status_code == 200, response.text
    row = _one(w, "user_login", "user", user.id, user)
    assert row.entity_name == user.username
    assert _values(row) == (None, {"provider": "cognito"})
    assert row.reason is None
    tokens = [response.json()[k] for k in ("access_token", "id_token", "refresh_token")]
    for column in (row.old_value, row.new_value, row.reason, row.entity_name):
        assert PASSWORD not in (column or "")
        for token in tokens:
            assert token not in (column or "")


def test_cognito_sign_in_without_an_account_writes_nothing(
    client, fresh, db_session, cognito_pool
):
    """No account is linked to the identity: no entry, and no account made."""
    username = f"{P}_unlinked_{uuid.uuid4().hex[:8]}"
    cognito_pool(username, f"{username}@example.com")
    w = Written(fresh)
    response = _cognito_sign_in(client, username)
    assert response.status_code == 200, response.text
    assert w.rows() == []
    session = fresh()
    try:
        assert session.query(User).filter(User.username == username).count() == 0
    finally:
        session.close()


def test_failed_cognito_sign_in_writes_nothing(client, fresh, db_session, cognito_pool):
    user = _cognito_account(db_session, cognito_pool)
    w = Written(fresh)
    response = _cognito_sign_in(client, user.username, password="Wrong-Passw0rd")
    assert response.status_code == 401
    assert w.rows() == []


def test_cognito_token_for_another_pool_writes_nothing(
    client, fresh, db_session, cognito_pool, monkeypatch
):
    """The token's issuer is not the configured pool: every request with it is
    refused, so the sign-in is not taken for the account's."""
    user = _cognito_account(db_session, cognito_pool)
    monkeypatch.setenv("COGNITO_USER_POOL_ID", "us-east-1_AnotherPool")
    w = Written(fresh)
    response = _cognito_sign_in(client, user.username)
    assert response.status_code == 200, response.text
    assert w.rows() == []


def test_cognito_sign_in_succeeds_when_the_entry_cannot_be_written(
    client, fresh, db_session, cognito_pool, audit_insert_refused, caplog
):
    user = _cognito_account(db_session, cognito_pool)
    caplog.set_level(logging.INFO)
    failures = audit_write_failures_total._value.get()
    w = Written(fresh)
    response = _cognito_sign_in(client, user.username)
    assert response.status_code == 200, response.text
    assert response.json()["access_token"]
    assert w.rows() == []
    assert len(_audit_errors(caplog)) == 1
    assert audit_write_failures_total._value.get() == failures + 1
    _assert_no_values_logged(caplog, "ae221 refused", user.email, user.username)


def test_cognito_sign_in_succeeds_when_the_account_cannot_be_looked_up(
    client, fresh, db_session, cognito_pool, monkeypatch, caplog
):
    user = _cognito_account(db_session, cognito_pool)

    def broken(db, auth_service, access_token):
        raise RuntimeError("ae221 lookup refused")

    monkeypatch.setattr(auth_endpoints, "_cognito_account", broken)
    caplog.set_level(logging.INFO)
    w = Written(fresh)
    response = _cognito_sign_in(client, user.username)
    assert response.status_code == 200, response.text
    assert w.rows() == []
    assert len(_audit_errors(caplog)) == 1
    _assert_no_values_logged(caplog, "ae221 lookup refused", user.email, user.username)


# --- G4: no request text the allow-list does not name -------------------------


def test_free_text_never_reaches_an_entry(client, fresh, db_session, developer):
    canary = f"ae221canary{uuid.uuid4().hex}"
    h = _auth(developer)
    w = Written(fresh)
    flag = client.post(
        f"{V1}/feature-flags/",
        json={
            "key": f"{P}-{uuid.uuid4().hex[:8]}",
            "name": f"{P} canary flag",
            "description": canary,
            "targeting_rules": _rules(canary),
        },
        headers=h,
    )
    assert flag.status_code == 201, flag.text
    client.put(
        f"{V1}/feature-flags/{flag.json()['id']}",
        json={"description": canary + "2", "targeting_rules": _rules(canary + "2")},
        headers=h,
    )
    payload = _experiment_payload()
    payload.update(description=canary, hypothesis=canary)
    exp = client.post(f"{V1}/experiments/", json=payload, headers=h)
    assert exp.status_code == 201, exp.text
    seg = client.post(
        f"{V1}/segments",
        json={
            "name": f"{P} canary seg",
            "description": canary,
            "rules": _rules(canary),
        },
        headers=h,
    )
    assert seg.status_code in (200, 201), seg.text
    rows = w.rows()
    assert len(rows) == 4
    for row in rows:
        for column in (
            row.entity_name,
            row.old_value,
            row.new_value,
            row.reason,
            row.user_email,
        ):
            assert canary not in (column or ""), (row.action_type, column)


# --- G5: route sites fail open ------------------------------------------------


def _audit_errors(caplog):
    return [
        r
        for r in caplog.records
        if r.levelno >= logging.ERROR and r.name == "backend.app.services.audit_service"
    ]


def _assert_no_values_logged(caplog, *values):
    """The audit writer's lines carry none of ``values`` and no traceback."""
    for record in caplog.records:
        if record.name != "backend.app.services.audit_service":
            continue
        message = record.getMessage()
        for value in values:
            assert value not in message, (record.name, message)
        if record.levelno >= logging.ERROR:
            assert record.exc_info is None, (record.name, message)


def test_route_site_keeps_the_change_when_the_audit_write_fails(
    client, fresh, developer, audit_insert_refused, caplog
):
    caplog.set_level(logging.INFO)
    exp = _create_experiment(client, developer)
    caplog.clear()
    failures = audit_write_failures_total._value.get()

    response = client.post(
        f"{V1}/experiments/{exp['id']}/start", headers=_auth(developer)
    )

    assert response.status_code == 200, response.text
    session = fresh()
    try:
        status = (
            session.query(Experiment.status).filter(Experiment.id == exp["id"]).scalar()
        )
        rows = session.query(AuditLog).filter(AuditLog.entity_id == exp["id"]).all()
    finally:
        session.close()
    assert status == ExperimentStatus.ACTIVE
    assert rows == []
    assert len(_audit_errors(caplog)) == 1
    assert audit_write_failures_total._value.get() == failures + 1
    _assert_no_values_logged(caplog, "ae221 refused", exp["name"], developer.email)


@pytest.mark.regression
def test_bulk_toggle_keeps_the_change_when_the_audit_write_fails(
    client, fresh, db_session, developer, audit_insert_refused, caplog
):
    caplog.set_level(logging.INFO)
    flag = _flag(db_session)
    response = client.post(
        f"{V1}/feature-flags/bulk-toggle",
        json={"flag_ids": [str(flag.id)], "action": "enable"},
        headers=_auth(developer),
    )
    assert response.status_code == 200, response.text
    assert response.json()["succeeded"] == 1
    assert response.json()["audit_log_ids"] == []
    session = fresh()
    try:
        stored = (
            session.query(FeatureFlag.status).filter(FeatureFlag.id == flag.id).scalar()
        )
    finally:
        session.close()
    assert FeatureFlagStatus(stored) == FeatureFlagStatus.ACTIVE
    assert len(_audit_errors(caplog)) == 1
    _assert_no_values_logged(caplog, "ae221 refused")


def test_bulk_toggle_stores_a_1000_character_reason(
    client, fresh, db_session, developer
):
    flag = _flag(db_session)
    reason = "r" * 1000
    w = Written(fresh)
    response = client.post(
        f"{V1}/feature-flags/bulk-toggle",
        json={"flag_ids": [str(flag.id)], "action": "enable", "reason": reason},
        headers=_auth(developer),
    )
    assert response.status_code == 200, response.text
    assert _one(w, "toggle_enable", "feature_flag", flag.id, developer).reason == reason


# --- G5: a superuser's change to a user is written with its entry or not at all


@pytest.mark.regression
def test_patch_under_a_failed_audit_write_keeps_neither(
    client, fresh, superuser, target, audit_insert_refused
):
    h = _auth(superuser)
    response = client.patch(
        f"{V1}/admin/users/{target.id}", json={"role": "ADMIN"}, headers=h
    )
    assert response.status_code == 500, response.text
    session = fresh()
    try:
        role = session.query(User.role).filter(User.id == target.id).scalar()
        rows = session.query(AuditLog).filter(AuditLog.entity_id == target.id).all()
    finally:
        session.close()
    assert role == UserRole.VIEWER
    assert rows == []

    # Once the failure clears, the identical request commits once.
    audit_insert_refused()
    w = Written(fresh)
    response = client.patch(
        f"{V1}/admin/users/{target.id}", json={"role": "ADMIN"}, headers=h
    )
    assert response.status_code == 200, response.text
    _one(w, "role_assign", "user", target.id, superuser)


@pytest.mark.regression
@pytest.mark.parametrize("route", ["/admin/users/{id}", "/users/{id}"])
def test_put_under_a_failed_audit_write_keeps_neither(
    client, fresh, superuser, target, route, audit_insert_refused
):
    h = _auth(superuser)
    path = f"{V1}{route.format(id=target.id)}"
    response = client.put(path, json=_put_body(target, is_superuser=True), headers=h)
    assert response.status_code == 500, response.text
    session = fresh()
    try:
        flag = session.query(User.is_superuser).filter(User.id == target.id).scalar()
        rows = session.query(AuditLog).filter(AuditLog.entity_id == target.id).all()
    finally:
        session.close()
    assert flag is False
    assert rows == []

    audit_insert_refused()
    w = Written(fresh)
    response = client.put(path, json=_put_body(target, is_superuser=True), headers=h)
    assert response.status_code == 200, response.text
    _one(w, "role_assign", "user", target.id, superuser)


@pytest.mark.regression
def test_superuser_without_email_or_username_is_recorded_as_system(
    client, fresh, db_session, target
):
    nameless = _make_user(
        db_session, UserRole.ADMIN, superuser=True, email=False, username=False
    )
    w = Written(fresh)
    try:
        response = client.patch(
            f"{V1}/admin/users/{target.id}",
            json={"role": "ANALYST"},
            headers=_auth(nameless),
        )
        assert response.status_code == 200, response.text
        rows = w.rows()
        assert len(rows) == 1
        assert rows[0].user_email == "system"
        assert rows[0].user_id == nameless.id
    finally:
        db_session.rollback()
        db_session.query(AuditLog).filter(AuditLog.user_id == nameless.id).delete(
            synchronize_session=False
        )
        db_session.delete(db_session.merge(nameless))
        db_session.commit()


# --- G5: the writer's log lines -----------------------------------------------


def test_writer_log_lines_carry_no_values(db_session, caplog):
    canary = f"ae221canary{uuid.uuid4().hex}"
    caplog.set_level(logging.INFO)
    actor = User(id=uuid.uuid4(), email=f"{canary}@audit.test", username=canary)

    failed = AuditService.record_after_commit(
        db_session,
        actor=actor,
        action="feature_flag_update",
        entity_type="feature_flag",
        entity_id=uuid.uuid4(),
        entity_name=None,  # NOT NULL: the insert fails
        before={"name": canary},
        after={"name": canary + "2"},
        reason=canary,
    )
    assert failed is None
    assert len(_audit_errors(caplog)) == 1

    written = AuditService.record_after_commit(
        db_session,
        actor=type("A", (), {"id": None, "email": None, "username": None})(),
        action="feature_flag_update",
        entity_type="feature_flag",
        entity_id=uuid.uuid4(),
        entity_name=f"{P} {canary}",
        before={"name": canary},
        after={"name": canary + "2"},
        reason=canary,
    )
    assert written is not None
    _assert_no_values_logged(caplog, canary)
    db_session.query(AuditLog).filter(AuditLog.id == written).delete()
    db_session.commit()


# --- G7: who reads the log (the role rules are in test_audit_log_reads.py) ----


@pytest.mark.parametrize(
    "path", ["/audit-logs/", "/audit-logs/stats", "/audit-logs/stream"]
)
def test_signed_out_callers_read_nothing(client, path):
    assert client.get(f"{V1}{path}").status_code == 401
