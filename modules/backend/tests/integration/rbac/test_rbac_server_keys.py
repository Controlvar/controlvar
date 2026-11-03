"""Server-side keys under custom roles and direct grants: option B (#891, check #36).

Conditions (ii) and (iii) of the #891 plan, with ruling 1 (option B):

* ``may_use_server_key(owner) = check_permission(owner, FEATURE_FLAG, UPDATE)``
  decides key creation with ``sdk:ruleset``, ``GET /sdk/ruleset``,
  ``POST /tracking/evaluations`` and whether ``POST /tracking/errors`` stores
  a report as ``"server"`` (counted toward rollback) or ``"client"``;
* ``POST /tracking/assign/batch`` also checks ``check_permission(owner,
  EXPERIMENT, UPDATE)`` on the key's owner, with its own 403
  (``OWNER_EXPERIMENT_DETAIL``), before any assignment is written.

A VIEWER, through real sign-in and real keys made by ``POST /api/v1/api-keys``;
grants are made and revoked by a non-superuser ADMIN through the RBAC routes.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from backend.app.api.sdk_scope import OWNER_EXPERIMENT_DETAIL, OWNER_ROLE_DETAIL
from backend.app.api.v1.endpoints.api_keys import SDK_RULESET_CREATE_FORBIDDEN
from backend.app.models.assignment import Assignment
from backend.app.models.experiment import Experiment, ExperimentStatus, Variant
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.global_holdout import GlobalHoldout
from backend.app.models.metrics.metric import ErrorLog
from backend.app.models.user import User, UserRole
from backend.tests.integration.api.route_helpers import (
    api_client,
    auth,
    local_sign_in,
    make_user,
)
from modules.backend.tests.integration.rbac.rbac_states import cleanup

pytestmark = [pytest.mark.integration, pytest.mark.requires_db, pytest.mark.regression]

V1 = "/api/v1"
TAG = "key891"


@pytest.fixture(autouse=True)
def _sign_in(monkeypatch):
    local_sign_in(monkeypatch)


@pytest.fixture
def client(db_session):
    with api_client(db_session) as c:
        yield c


@pytest.fixture
def admin(db_session):
    """An ADMIN who is not a superuser makes and revokes the grants."""
    return make_user(db_session, UserRole.ADMIN, tag=TAG)


@pytest.fixture
def viewer(db_session):
    return make_user(db_session, UserRole.VIEWER, tag=TAG)


@pytest.fixture
def no_holdout(db_session):
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
def experiment(db_session, admin, no_holdout):
    suffix = uuid.uuid4().hex[:8]
    experiment = Experiment(
        name=f"keys {suffix}",
        key=f"keys-{suffix}",
        status=ExperimentStatus.ACTIVE,
        owner_id=admin.id,
        hypothesis="server keys",
    )
    db_session.add(experiment)
    db_session.commit()
    for i, name in enumerate(("control", "treatment")):
        db_session.add(
            Variant(
                experiment_id=experiment.id,
                name=name,
                is_control=i == 0,
                traffic_allocation=50,
                configuration={"arm": name},
            )
        )
    db_session.commit()
    db_session.refresh(experiment)
    yield experiment
    db_session.rollback()
    db_session.query(Assignment).filter(
        Assignment.experiment_id == experiment.id
    ).delete()
    db_session.commit()


@pytest.fixture
def flag(db_session, admin):
    flag = FeatureFlag(
        key=f"keys-{uuid.uuid4().hex[:10]}",
        name="keys flag",
        status=FeatureFlagStatus.ACTIVE,
        owner_id=admin.id,
        rollout_percentage=100,
    )
    db_session.add(flag)
    db_session.commit()
    db_session.refresh(flag)
    return flag


@pytest.fixture(autouse=True)
def _cleanup(db_session):
    yield
    users = db_session.query(User).filter(User.username.like(f"{TAG}_%")).all()
    ids = [u.id for u in users]
    db_session.rollback()
    if ids:
        db_session.execute(
            text(
                "DELETE FROM audit_logs WHERE user_id = ANY(:ids) OR entity_id = ANY(:ids)"
            ),
            {"ids": ids},
        )
        db_session.execute(
            text("DELETE FROM api_keys WHERE user_id = ANY(:ids)"), {"ids": ids}
        )
        db_session.commit()
    cleanup(db_session, TAG, ids)


def _grant(client, admin, user, resource, actions):
    resp = client.post(
        f"{V1}/rbac/users/{user.id}/grant",
        json={"user_id": str(user.id), "resource": resource, "actions": actions},
        headers=auth(admin),
    )
    assert resp.status_code == 201, resp.text


def _revoke(client, admin, user, resource):
    resp = client.delete(
        f"{V1}/rbac/users/{user.id}/grant/{resource}", headers=auth(admin)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["count"] >= 1, resp.text


def _ruleset_key(client, user):
    resp = client.post(
        f"{V1}/api-keys",
        json={"name": "server", "scopes": ["sdk:ruleset"]},
        headers=auth(user),
    )
    return resp


def _evaluations(client, key, flag):
    end = datetime.now(timezone.utc)
    return client.post(
        f"{V1}/tracking/evaluations",
        json={
            "evaluations": [
                {
                    "flag_key": flag.key,
                    "count": 1,
                    "enabled_count": 1,
                    "window_start": (end - timedelta(seconds=60)).isoformat(),
                    "window_end": end.isoformat(),
                }
            ]
        },
        headers={"X-API-Key": key},
    )


def _report_source(client, db_session, key, flag) -> str:
    marker = f"m-{uuid.uuid4().hex[:10]}"
    resp = client.post(
        f"{V1}/tracking/errors",
        json={"feature_flag_key": flag.key, "error_type": "crash", "message": marker},
        headers={"X-API-Key": key},
    )
    assert resp.status_code == 201, resp.text
    db_session.expire_all()
    row = (
        db_session.query(ErrorLog)
        .filter(ErrorLog.feature_flag_id == flag.id)
        .order_by(ErrorLog.timestamp.desc())
        .all()
    )
    sources = [
        r.request_data.get("source")
        for r in row
        if isinstance(r.request_data, dict) and marker in str(r.message or "")
    ]
    assert len(sources) == 1, sources
    return sources[0]


def _rows(db_session, experiment) -> int:
    db_session.expire_all()
    return (
        db_session.query(Assignment)
        .filter(Assignment.experiment_id == experiment.id)
        .count()
    )


def _batch(client, key, experiment):
    return client.post(
        f"{V1}/tracking/assign/batch",
        json={
            "experiment_key": experiment.key,
            "users": [{"user_id": f"b-{uuid.uuid4().hex[:10]}"} for _ in range(3)],
        },
        headers={"X-API-Key": key},
    )


def test_option_b_flag_update_gives_a_server_key_and_batch_needs_experiment_update(
    client, db_session, admin, viewer, experiment, flag
):
    # A plain VIEWER may not create a server-side key (the control).
    resp = _ruleset_key(client, viewer)
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == SDK_RULESET_CREATE_FORBIDDEN

    # --- feature_flag (update) only -----------------------------------------
    _grant(client, admin, viewer, "feature_flag", ["update"])
    resp = _ruleset_key(client, viewer)
    assert resp.status_code == 201, f"key creation (flag update only): {resp.text}"
    key = resp.json()["key"]

    resp = client.get(f"{V1}/sdk/ruleset", headers={"X-API-Key": key})
    assert resp.status_code == 200, f"ruleset (flag update only): {resp.text}"
    resp = _evaluations(client, key, flag)
    assert resp.status_code == 201, f"evaluations (flag update only): {resp.text}"
    assert _report_source(client, db_session, key, flag) == "server"

    before = _rows(db_session, experiment)
    resp = _batch(client, key, experiment)
    written = _rows(db_session, experiment) - before
    assert resp.status_code == 403, (
        f"batch (flag update only): expected 403, got {resp.status_code}; "
        f"{written} assignment rows written"
    )
    assert written == 0, f"batch (flag update only): {written} assignment rows written"
    assert resp.json()["detail"] == OWNER_EXPERIMENT_DETAIL

    # --- feature_flag (update) and experiment (update) ----------------------
    _grant(client, admin, viewer, "experiment", ["update"])
    resp = _batch(client, key, experiment)
    assert resp.status_code == 200, f"batch (both grants): {resp.text}"
    assert _rows(db_session, experiment) - before == 3

    # --- feature_flag (update) revoked ---------------------------------------
    _revoke(client, admin, viewer, "feature_flag")
    resp = client.get(f"{V1}/sdk/ruleset", headers={"X-API-Key": key})
    assert resp.status_code == 403, f"ruleset after revoke: {resp.text}"
    assert resp.json()["detail"] == OWNER_ROLE_DETAIL
    resp = _evaluations(client, key, flag)
    assert resp.status_code == 403, f"evaluations after revoke: {resp.text}"
    assert _report_source(client, db_session, key, flag) == "client"
    resp = _batch(client, key, experiment)
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == OWNER_ROLE_DETAIL
    assert _rows(db_session, experiment) - before == 3


def test_experiment_update_alone_gives_no_server_key(client, admin, viewer):
    _grant(client, admin, viewer, "experiment", ["update"])
    resp = _ruleset_key(client, viewer)
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == SDK_RULESET_CREATE_FORBIDDEN


def test_a_built_in_developer_key_assigns_in_a_batch_as_before(
    client, db_session, experiment
):
    """For the four built-in roles nothing changes: a DEVELOPER's key does it."""
    developer = make_user(db_session, UserRole.DEVELOPER, tag=TAG)
    resp = _ruleset_key(client, developer)
    assert resp.status_code == 201, resp.text
    before = _rows(db_session, experiment)
    resp = _batch(client, resp.json()["key"], experiment)
    assert resp.status_code == 200, resp.text
    assert _rows(db_session, experiment) - before == 3
