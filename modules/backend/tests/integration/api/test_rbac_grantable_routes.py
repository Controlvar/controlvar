"""A custom role or a direct grant holds only the eleven pairs (#891).

On Postgres, through the real routes, with real local-token authentication:

* each of the three write routes refuses a pair outside the eleven with a 422
  whose ``type``, ``loc`` and ``msg`` are exact, and writes nothing; the same
  request with grantable pairs is accepted (the positive control);
* a role or a grant stored before the check, holding ``report`` and
  ``export``, still reads, takes a description-only update, and is cleaned up
  by ``DELETE /rbac/users/{user_id}/grant/{resource}``, which takes any
  resource. A ``PUT`` that sends its refused pairs back is refused.

``modules/backend/tests/unit/schemas/test_rbac_schemas.py`` holds the set
itself, generated over all forty pairs.
"""

from __future__ import annotations

import uuid

import pytest

from backend.app.models.audit_log import AuditLog
from backend.app.models.user import User, UserRole
from backend.tests.integration.api.route_helpers import (
    api_client,
    auth,
    local_sign_in,
    make_user,
)
from modules.backend.app.models.custom_role import (
    CustomRole,
    DirectPermissionGrant,
    UserCustomRole,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

V1 = "/api/v1"
TAG = "gr891"
TAIL = (
    "cannot be granted. A custom role or a direct grant can hold only "
    "experiment (create, read, update, delete, list), feature_flag (create, read, "
    "update, delete, list) and audit_log (read)"
)
LEGACY = [
    {"resource": "report", "actions": ["read", "list"]},
    {"resource": "export", "actions": ["read"]},
]


@pytest.fixture(autouse=True)
def _sign_in(monkeypatch):
    local_sign_in(monkeypatch)


@pytest.fixture
def client(db_session):
    with api_client(db_session) as c:
        yield c


@pytest.fixture
def admin(db_session):
    """An ADMIN who is not a superuser: superusers pass every check."""
    return make_user(db_session, UserRole.ADMIN, tag=TAG)


@pytest.fixture
def target(db_session):
    return make_user(db_session, UserRole.VIEWER, tag=TAG)


@pytest.fixture(autouse=True)
def _cleanup(db_session):
    yield
    db_session.rollback()
    s = db_session
    users = s.query(User).filter(User.username.like(f"{TAG}_%")).all()
    ids = [u.id for u in users]
    s.query(AuditLog).filter(
        AuditLog.user_id.in_(ids) | AuditLog.entity_id.in_(ids)
    ).delete(synchronize_session=False)
    s.query(DirectPermissionGrant).filter(
        DirectPermissionGrant.user_id.in_(ids)
    ).delete(synchronize_session=False)
    s.query(UserCustomRole).filter(UserCustomRole.user_id.in_(ids)).delete(
        synchronize_session=False
    )
    s.query(CustomRole).filter(CustomRole.name.like(f"{TAG}-%")).delete(
        synchronize_session=False
    )
    s.commit()
    for user in users:
        s.delete(user)
    s.commit()


def _name() -> str:
    return f"{TAG}-{uuid.uuid4().hex[:8]}"


def _stored_role(db_session, name):
    db_session.expire_all()
    return db_session.query(CustomRole).filter(CustomRole.name == name).first()


def _grants(db_session, user_id):
    db_session.expire_all()
    return (
        db_session.query(DirectPermissionGrant)
        .filter(DirectPermissionGrant.user_id == user_id)
        .all()
    )


def _the_refusal(resp):
    assert resp.status_code == 422, f"expected 422, got {resp.status_code}: {resp.text}"
    (only,) = resp.json()["detail"]
    return only


@pytest.mark.regression
def test_creating_a_role_with_a_pair_no_role_may_hold_is_refused(
    client, db_session, admin
):
    name = _name()
    body = {
        "name": name,
        "permissions": [
            {"resource": "export", "actions": ["read"]},
            {"resource": "experiment", "actions": ["read"]},
            {"resource": "audit_log", "actions": ["read", "list"]},
        ],
    }
    refusal = _the_refusal(
        client.post(f"{V1}/rbac/roles", json=body, headers=auth(admin))
    )
    assert refusal["type"] == "not_grantable"
    assert refusal["loc"] == ["body", "permissions"]
    assert refusal["msg"] == f"export (read) and audit_log (list) {TAIL}"
    assert _stored_role(db_session, name) is None

    # Positive control: the grantable pairs alone are accepted.
    body["permissions"] = [
        {"resource": "experiment", "actions": ["read"]},
        {"resource": "audit_log", "actions": ["read"]},
    ]
    resp = client.post(f"{V1}/rbac/roles", json=body, headers=auth(admin))
    assert resp.status_code == 201, resp.text
    assert _stored_role(db_session, name) is not None


@pytest.mark.regression
def test_changing_a_roles_permissions_to_a_pair_no_role_may_hold_is_refused(
    client, db_session, admin
):
    name = _name()
    held = [{"resource": "feature_flag", "actions": ["read"]}]
    made = client.post(
        f"{V1}/rbac/roles",
        json={"name": name, "permissions": held},
        headers=auth(admin),
    )
    assert made.status_code == 201, made.text

    refusal = _the_refusal(
        client.put(
            f"{V1}/rbac/roles/{name}",
            json={
                "description": "with user administration",
                "permissions": [{"resource": "user", "actions": ["create", "delete"]}],
            },
            headers=auth(admin),
        )
    )
    assert refusal["type"] == "not_grantable"
    assert refusal["loc"] == ["body", "permissions"]
    assert refusal["msg"] == f"user (create, delete) {TAIL}"
    stored = _stored_role(db_session, name)
    assert stored.permissions == held
    assert stored.description is None

    # Positive control.
    wider = [{"resource": "feature_flag", "actions": ["read", "update"]}]
    resp = client.put(
        f"{V1}/rbac/roles/{name}", json={"permissions": wider}, headers=auth(admin)
    )
    assert resp.status_code == 200, resp.text
    assert _stored_role(db_session, name).permissions == wider


@pytest.mark.regression
def test_granting_a_pair_no_grant_may_hold_is_refused(
    client, db_session, admin, target
):
    url = f"{V1}/rbac/users/{target.id}/grant"
    body = {
        "user_id": str(target.id),
        "resource": "report",
        "actions": ["read", "list"],
    }
    refusal = _the_refusal(client.post(url, json=body, headers=auth(admin)))
    assert refusal["type"] == "not_grantable"
    assert refusal["loc"] == ["body"]
    assert refusal["msg"] == f"report (read, list) {TAIL}"
    assert _grants(db_session, target.id) == []

    # Positive control.
    body.update(resource="audit_log", actions=["read"])
    resp = client.post(url, json=body, headers=auth(admin))
    assert resp.status_code == 201, resp.text
    assert [g.resource for g in _grants(db_session, target.id)] == ["audit_log"]


@pytest.mark.regression
def test_a_role_and_a_grant_stored_before_the_check_read_and_clean_up(
    client, db_session, admin, target
):
    name = _name()
    db_session.add(
        CustomRole(name=name, description="stored earlier", permissions=LEGACY)
    )
    db_session.add(
        DirectPermissionGrant(
            user_id=target.id, resource="report", actions=["read"], is_active=True
        )
    )
    db_session.commit()

    listed = client.get(f"{V1}/rbac/roles", headers=auth(admin))
    assert listed.status_code == 200, f"GET /rbac/roles: got {listed.status_code}"
    (role,) = [r for r in listed.json() if r["name"] == name]
    assert role["permissions"] == LEGACY
    one = client.get(f"{V1}/rbac/roles/{name}", headers=auth(admin))
    assert one.status_code == 200, one.text

    # A description-only update is accepted and keeps the stored pairs.
    resp = client.put(
        f"{V1}/rbac/roles/{name}", json={"description": "renamed"}, headers=auth(admin)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["permissions"] == LEGACY
    # Sending them back is refused, and changes nothing.
    refusal = _the_refusal(
        client.put(
            f"{V1}/rbac/roles/{name}", json={"permissions": LEGACY}, headers=auth(admin)
        )
    )
    assert refusal["msg"] == f"report (read, list) and export (read) {TAIL}"
    assert _stored_role(db_session, name).description == "renamed"

    # The revoke route takes any resource, so the stored grant can go.
    gone = client.delete(
        f"{V1}/rbac/users/{target.id}/grant/report", headers=auth(admin)
    )
    assert gone.status_code == 200, gone.text
    assert gone.json() == {"status": "revoked", "count": 1}
    assert _grants(db_session, target.id) == []
