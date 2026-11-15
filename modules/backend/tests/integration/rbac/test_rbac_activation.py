"""Activation: only an assignment or a grant made since custom roles took effect counts (#891).

Checks #20, #21 and #22 of the #891 plan, on Postgres, through the real
routes and real local sign-in:

* #20: assigning a role whose assignment was recorded before custom roles
  took effect (``activated_at`` empty) activates it and writes exactly one
  ``role_assign`` entry with the reason "custom role activated (recorded
  before custom roles took effect)"; assigning it again writes none;
* #21: a row written the way the previous release's code wrote it (no
  ``activated_at``) grants nothing, is not listed, and only an explicit
  assign or grant makes it take effect;
* #22: ``user_count`` counts only assignments that take effect.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from backend.app.models.audit_log import AuditLog
from backend.app.models.user import User, UserRole
from backend.tests.integration.api.route_helpers import (
    api_client,
    auth,
    local_sign_in,
    make_user,
)
from modules.backend.tests.integration.rbac.rbac_states import assign, cleanup, grant

pytestmark = [pytest.mark.integration, pytest.mark.requires_db, pytest.mark.regression]

V1 = "/api/v1"
TAG = "act891"
ACTIVATION_REASON = "custom role activated (recorded before custom roles took effect)"


@pytest.fixture(autouse=True)
def _sign_in(monkeypatch):
    local_sign_in(monkeypatch)


@pytest.fixture
def client(db_session):
    with api_client(db_session) as c:
        yield c


@pytest.fixture
def admin(db_session):
    return make_user(db_session, UserRole.ADMIN, tag=TAG)


@pytest.fixture
def viewer(db_session):
    return make_user(db_session, UserRole.VIEWER, tag=TAG)


@pytest.fixture(autouse=True)
def _cleanup(db_session):
    yield
    users = db_session.query(User).filter(User.username.like(f"{TAG}_%")).all()
    ids = [u.id for u in users]
    db_session.rollback()
    if ids:
        db_session.execute(
            text(
                "DELETE FROM audit_logs WHERE user_id = ANY(:ids) "
                "OR entity_id = ANY(:ids)"
            ),
            {"ids": ids},
        )
        db_session.commit()
    cleanup(db_session, TAG, ids)


def _role(client, admin, permissions) -> str:
    name = f"{TAG}-{uuid.uuid4().hex[:10]}"
    resp = client.post(
        f"{V1}/rbac/roles",
        json={"name": name, "permissions": permissions},
        headers=auth(admin),
    )
    assert resp.status_code == 201, resp.text
    return name


def _role_id(db_session, name):
    return db_session.execute(
        text("SELECT id FROM custom_roles WHERE name = :n"), {"n": name}
    ).scalar_one()


def _activated(db_session, user_id):
    return [
        row[0]
        for row in db_session.execute(
            text("SELECT activated_at FROM user_custom_roles WHERE user_id = :u"),
            {"u": user_id},
        )
    ]


def _assign_entries(db_session, user_id):
    db_session.expire_all()
    return (
        db_session.query(AuditLog)
        .filter(AuditLog.entity_id == user_id, AuditLog.action_type == "role_assign")
        .all()
    )


def _create_experiment(client, user):
    suffix = uuid.uuid4().hex[:8]
    return client.post(
        f"{V1}/experiments/",
        headers=auth(user),
        json={
            "name": f"act {suffix}",
            "key": f"act-{suffix}",
            "hypothesis": "activation",
            "experiment_type": "a_b",
            "variants": [
                {"name": "control", "is_control": True, "traffic_allocation": 50},
                {"name": "b", "is_control": False, "traffic_allocation": 50},
            ],
            "metrics": [
                {
                    "name": "Purchase",
                    "event_name": f"purchase_{suffix}",
                    "metric_type": "conversion",
                    "is_primary": True,
                }
            ],
        },
    )


def test_assigning_a_role_recorded_before_activates_it_and_is_audited_once(
    client, db_session, admin, viewer
):
    name = _role(client, admin, [{"resource": "experiment", "actions": ["create"]}])
    assign(db_session, viewer.id, _role_id(db_session, name), activated=False)
    db_session.commit()
    assert _activated(db_session, viewer.id) == [None]
    assert _create_experiment(client, viewer).status_code == 403

    body = {"user_id": str(viewer.id), "role_name": name}
    resp = client.post(f"{V1}/rbac/roles/assign", json=body, headers=auth(admin))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "assigned"

    activated = _activated(db_session, viewer.id)
    assert len(activated) == 1 and activated[0] is not None
    entries = _assign_entries(db_session, viewer.id)
    assert len(entries) == 1, f"expected 1 role_assign entry, found {len(entries)}"
    assert entries[0].reason == ACTIVATION_REASON
    assert entries[0].user_id == admin.id

    # The role now takes effect.
    assert _create_experiment(client, viewer).status_code == 201

    # Assigning it again changes nothing and writes nothing.
    resp = client.post(f"{V1}/rbac/roles/assign", json=body, headers=auth(admin))
    assert resp.status_code == 200, resp.text
    assert len(_assign_entries(db_session, viewer.id)) == 1


def test_a_first_assignment_is_audited_as_an_assignment(
    client, db_session, admin, viewer
):
    name = _role(client, admin, [{"resource": "experiment", "actions": ["create"]}])
    body = {"user_id": str(viewer.id), "role_name": name}
    resp = client.post(f"{V1}/rbac/roles/assign", json=body, headers=auth(admin))
    assert resp.status_code == 200, resp.text
    entries = _assign_entries(db_session, viewer.id)
    assert [e.reason for e in entries] == ["custom role assigned"]
    assert _activated(db_session, viewer.id)[0] is not None


def test_rows_written_by_the_previous_code_stay_inactive_until_made_again(
    client, db_session, admin, viewer
):
    """Rows as the previous release's writers left them: no activated_at."""
    name = _role(
        client,
        admin,
        [{"resource": "experiment", "actions": ["create"]}],
    )
    assign(db_session, viewer.id, _role_id(db_session, name), activated=False)
    grant(db_session, viewer.id, "feature_flag", ["create"], activated=False)
    db_session.commit()

    # Neither takes effect ...
    assert _create_experiment(client, viewer).status_code == 403, (
        "row written before PR 2 is active"
    )
    resp = client.post(
        f"{V1}/feature-flags/",
        headers=auth(viewer),
        json={"key": f"act-{uuid.uuid4().hex[:8]}", "name": "act"},
    )
    assert resp.status_code == 403, "row written before PR 2 is active"
    # ... neither is listed ...
    shown = client.get(
        f"{V1}/rbac/users/{viewer.id}/permissions", headers=auth(viewer)
    ).json()
    assert shown["custom_roles"] == []
    assert "create" not in shown["permissions"]["experiment"]
    assert "create" not in shown["permissions"]["feature_flag"]
    roles = client.get(f"{V1}/rbac/roles", headers=auth(admin)).json()
    assert {r["name"]: r["user_count"] for r in roles}[name] == 0

    # ... until the grant is made again: then the new grant takes effect.
    resp = client.post(
        f"{V1}/rbac/users/{viewer.id}/grant",
        json={
            "user_id": str(viewer.id),
            "resource": "feature_flag",
            "actions": ["create"],
        },
        headers=auth(admin),
    )
    assert resp.status_code == 201, resp.text
    resp = client.post(
        f"{V1}/feature-flags/",
        headers=auth(viewer),
        json={"key": f"act-{uuid.uuid4().hex[:8]}", "name": "act"},
    )
    assert resp.status_code == 201, resp.text
    # The role assignment is still inactive: only the grant was made again.
    assert _create_experiment(client, viewer).status_code == 403


def test_user_count_counts_only_assignments_that_take_effect(client, db_session, admin):
    name = _role(client, admin, [{"resource": "experiment", "actions": ["read"]}])
    role_id = _role_id(db_session, name)
    inactive = make_user(db_session, UserRole.VIEWER, tag=TAG)
    active = make_user(db_session, UserRole.VIEWER, tag=TAG)
    assign(db_session, inactive.id, role_id, activated=False)
    assign(db_session, active.id, role_id, activated=True)
    db_session.commit()
    resp = client.get(f"{V1}/rbac/roles/{name}", headers=auth(admin))
    assert resp.status_code == 200, resp.text
    assert resp.json()["user_count"] == 1
