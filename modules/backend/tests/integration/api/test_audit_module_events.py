"""The modules' changes write their audit entry, by the right actor (#221).

On Postgres, through the HTTP routes, with real local-token authentication:

* G3: one entry each for a custom role assigned and revoked, a workspace
  member's role changed, an SSO sign-in (SAML ACS and the dashboard's
  ``POST /exchange``), an account created by an SSO sign-in, and an existing
  account's role changed by an SSO sign-in. The last is ``role_assign`` by
  the reserved actor ``system:sso-sync`` (no ``user_id``), with before and
  after ``{role, is_superuser}``: user changes record the superuser flag.
  ``audit_events_v2`` does not move for any of them. A request that changes
  nothing (an assignment already there, a role already equal) writes nothing.
* #891: one entry each for a custom role created, changed and deleted
  (entity ``role``), and a direct permission granted and revoked (entity the
  user), with fixed ``reason`` text; the grant's own reason is in its
  ``new_value``. A repeat, a no-op, a 404, a 409 and a 422 write none.
* G5, with a ``BEFORE INSERT`` trigger on ``audit_logs`` that raises (dropped
  in a ``finally``):

  - the SSO sign-in role change still applies, the sign-in is not refused,
    one ERROR naming only the exception type is logged, no entry, and the
    next sign-in writes nothing (the role is already equal: not retried);
  - an SSO sign-in still creates the account;
  - the route sites (custom role assigned, defined, a direct grant made and
    revoked, workspace role) keep the committed change and answer 2xx.

The OIDC callback's own sign-in entry is pinned in ``test_sso_oidc_flow.py``,
which has the provider it needs.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from contextlib import contextmanager
from typing import Iterator
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.api.v1.endpoints.auth import create_local_access_token
from backend.app.core.config import settings
from backend.app.db.session import get_db as session_get_db
from backend.app.main import app
from backend.app.models.audit_log import AuditLog
from backend.app.models.user import User, UserRole
from modules.backend.app.models.custom_role import (
    CustomRole,
    DirectPermissionGrant,
    UserCustomRole,
)
from modules.backend.app.models.sso_config import SSOConfig, SSOProviderType
from modules.backend.app.models.workspace import Workspace
from modules.backend.app.services import sso_service

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

SCHEMA = "test_experimentation"
P = "am221"
V1 = "/api/v1"
REFUSAL = "am221 refused"
SSO_SYNC = "system:sso-sync"
SECRET = "A" * 43


# --- fixtures -----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _local_auth(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "CACHE_ENABLED", False)


def _factory(db_session, **kwargs):
    factory = sessionmaker(bind=db_session.get_bind(), autoflush=False, **kwargs)

    def session():
        s = factory()
        s.execute(text(f"SET search_path TO {SCHEMA}"))
        return s

    return session


@pytest.fixture
def fresh(db_session):
    return _factory(db_session, expire_on_commit=False)


@pytest.fixture
def client(db_session) -> Iterator[TestClient]:
    """Only the database is overridden; authentication is real."""
    session = _factory(db_session, autocommit=False)

    def override_get_db():
        s = session()
        try:
            yield s
        finally:
            s.close()

    saved = dict(app.dependency_overrides)
    app.dependency_overrides.clear()
    app.dependency_overrides[deps.get_db] = override_get_db
    app.dependency_overrides[session_get_db] = override_get_db
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(saved)


def _domain() -> str:
    return f"{P}-{uuid.uuid4().hex[:10]}.example.com"


def _make_user(db_session, role, *, superuser=False, email=None) -> User:
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"{P}_{role.name.lower()}_{suffix}",
        email=email or f"{P}_{suffix}@example.com",
        hashed_password="unused: signs in by token only",
        is_active=True,
        is_superuser=superuser,
        role=role,
    )
    db_session.add(user)
    db_session.commit()
    return user


@pytest.fixture
def admin(db_session):
    """An ADMIN who is not a superuser: superusers pass every check."""
    return _make_user(db_session, UserRole.ADMIN)


@pytest.fixture
def target(db_session):
    return _make_user(db_session, UserRole.DEVELOPER)


@pytest.fixture
def custom_role(db_session):
    role = CustomRole(
        name=f"{P}-{uuid.uuid4().hex[:8]}",
        description="am221 role",
        permissions=[{"resource": "experiment", "actions": ["read"]}],
    )
    db_session.add(role)
    db_session.commit()
    return role


def _saml_config(db_session, domain, role_mapping=None) -> SSOConfig:
    cfg = SSOConfig(
        org_name="am221 Org",
        org_domain=domain,
        provider_type=SSOProviderType.SAML,
        entity_id="https://idp.example.com",
        sso_url="https://idp.example.com/sso",
        x509_certificate="MIIC...cert",
        role_mapping=role_mapping or {},
        is_enforced=False,
        is_active=True,
    )
    db_session.add(cfg)
    db_session.commit()
    return cfg


@pytest.fixture(autouse=True)
def _cleanup(db_session):
    """Leave nothing behind: other suites list users and workspaces."""
    yield
    db_session.rollback()
    s = db_session
    users = s.query(User).filter(User.email.like(f"%{P}%")).all()
    user_ids = [u.id for u in users]
    s.query(AuditLog).filter(
        AuditLog.entity_id.in_(user_ids) | AuditLog.user_id.in_(user_ids)
    ).delete(synchronize_session=False)
    s.query(UserCustomRole).filter(UserCustomRole.user_id.in_(user_ids)).delete(
        synchronize_session=False
    )
    s.query(DirectPermissionGrant).filter(
        DirectPermissionGrant.user_id.in_(user_ids)
    ).delete(synchronize_session=False)
    s.query(CustomRole).filter(CustomRole.name.like(f"{P}-%")).delete(
        synchronize_session=False
    )
    for ws in s.query(Workspace).filter(Workspace.slug.like(f"{P}-%")):
        s.delete(ws)
    s.query(SSOConfig).filter(SSOConfig.org_domain.like(f"{P}-%")).delete(
        synchronize_session=False
    )
    s.commit()
    for user in users:
        s.delete(user)
    s.commit()


@contextmanager
def refuse_audit(db_session):
    """Every insert into ``audit_logs`` raises; the trigger is dropped in a ``finally``."""
    db_session.execute(
        text(
            f"CREATE OR REPLACE FUNCTION {SCHEMA}.am221_refuse_audit() "
            "RETURNS trigger LANGUAGE plpgsql AS "
            f"$$ BEGIN RAISE EXCEPTION '{REFUSAL}'; END $$"
        )
    )
    db_session.execute(
        text(
            f"CREATE TRIGGER am221_refuse_audit BEFORE INSERT ON {SCHEMA}.audit_logs "
            f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.am221_refuse_audit()"
        )
    )
    db_session.commit()
    try:
        yield
    finally:
        db_session.rollback()
        db_session.execute(
            text(f"DROP TRIGGER IF EXISTS am221_refuse_audit ON {SCHEMA}.audit_logs")
        )
        db_session.execute(
            text(f"DROP FUNCTION IF EXISTS {SCHEMA}.am221_refuse_audit()")
        )
        db_session.commit()


# --- helpers ------------------------------------------------------------------


def _auth(user):
    return {"Authorization": f"Bearer {create_local_access_token(user)}"}


def _v2_count(fresh) -> int:
    s = fresh()
    try:
        return s.execute(
            text(f"SELECT count(*) FROM {SCHEMA}.audit_events_v2")
        ).scalar()
    finally:
        s.close()


def _rows(fresh, entity_id, action=None):
    s = fresh()
    try:
        q = s.query(AuditLog).filter(AuditLog.entity_id == entity_id)
        if action is not None:
            q = q.filter(AuditLog.action_type == action)
        return q.order_by(AuditLog.timestamp).all()
    finally:
        s.close()


def _one(fresh, entity_id, action, *, email, user_id=None):
    """Exactly one row for this entity and action, by this actor."""
    rows = _rows(fresh, entity_id, action)
    assert len(rows) == 1, f"expected 1 {action} entry, found {len(rows)}"
    row = rows[0]
    assert row.user_email == email
    assert row.user_id == user_id
    assert row.entity_type == "user"
    return row


def _values(row):
    def load(v):
        return json.loads(v) if v else None

    return load(row.old_value), load(row.new_value)


def _audit_errors(caplog):
    return [
        r
        for r in caplog.records
        if r.levelno >= logging.ERROR
        and r.getMessage().startswith("Failed to create audit log")
    ]


def _assert_type_only_errors(caplog, n=1):
    errors = _audit_errors(caplog)
    assert len(errors) == n, [r.getMessage() for r in errors]
    for record in errors:
        assert re.search(r" \(\w+Error\)$", record.getMessage()), record.getMessage()
        assert REFUSAL not in record.getMessage()
        assert record.exc_info is None


def _user(fresh, user_id) -> User:
    s = fresh()
    try:
        return s.get(User, user_id)
    finally:
        s.close()


def _acs(client, cfg, email, groups=()):
    """POST to the ACS with the parse mocked to have accepted *email*."""
    parsed = {
        "email": email,
        "name_id": email,
        "groups": list(groups),
        "attributes": {},
        "first_name": None,
        "last_name": None,
    }
    with patch(
        "modules.backend.app.services.sso_service.parse_saml_response",
        return_value=parsed,
    ):
        return client.post(
            f"{V1}/auth/sso/saml/{cfg.id}/acs", data={"SAMLResponse": "eA=="}
        )


# --- custom roles (rbac) --------------------------------------------------------


def test_assigning_a_custom_role_writes_one_role_assign(
    client, fresh, admin, target, custom_role
):
    v2 = _v2_count(fresh)
    body = {"user_id": str(target.id), "role_name": custom_role.name}
    resp = client.post(f"{V1}/rbac/roles/assign", json=body, headers=_auth(admin))
    assert resp.status_code == 200, resp.text
    row = _one(fresh, target.id, "role_assign", email=admin.email, user_id=admin.id)
    assert _values(row) == (None, {"custom_role": custom_role.name})
    assert row.entity_name == target.username

    # Already assigned: nothing changes, nothing is written.
    again = client.post(f"{V1}/rbac/roles/assign", json=body, headers=_auth(admin))
    assert again.status_code == 200, again.text
    assert len(_rows(fresh, target.id, "role_assign")) == 1
    assert _v2_count(fresh) == v2


def test_revoking_a_custom_role_writes_one_role_unassign(
    client, fresh, admin, target, custom_role
):
    body = {"user_id": str(target.id), "role_name": custom_role.name}
    assert (
        client.post(f"{V1}/rbac/roles/assign", json=body, headers=_auth(admin))
    ).status_code == 200
    v2 = _v2_count(fresh)
    resp = client.post(f"{V1}/rbac/roles/revoke", json=body, headers=_auth(admin))
    assert resp.status_code == 200, resp.text
    row = _one(fresh, target.id, "role_unassign", email=admin.email, user_id=admin.id)
    assert _values(row) == ({"custom_role": custom_role.name}, None)

    # Not assigned any more: nothing changes, nothing is written.
    again = client.post(f"{V1}/rbac/roles/revoke", json=body, headers=_auth(admin))
    assert again.status_code == 200, again.text
    assert len(_rows(fresh, target.id, "role_unassign")) == 1
    assert _v2_count(fresh) == v2


def test_a_refused_entry_keeps_the_custom_role(
    client, fresh, db_session, admin, target, custom_role, caplog
):
    body = {"user_id": str(target.id), "role_name": custom_role.name}
    with caplog.at_level(logging.ERROR), refuse_audit(db_session):
        resp = client.post(f"{V1}/rbac/roles/assign", json=body, headers=_auth(admin))
    assert resp.status_code == 200, resp.text
    s = fresh()
    try:
        held = (
            s.query(UserCustomRole).filter(UserCustomRole.user_id == target.id).count()
        )
    finally:
        s.close()
    assert held == 1
    assert _rows(fresh, target.id) == []
    _assert_type_only_errors(caplog)


# --- custom role definitions and direct grants (#891) -----------------------------

REFUSED = {"resource": "export", "actions": ["read"]}


def _role_name() -> str:
    return f"{P}-{uuid.uuid4().hex[:8]}"


def _role_rows(fresh, action, name):
    """Every entry of *action* on the custom role called *name*."""
    s = fresh()
    try:
        return (
            s.query(AuditLog)
            .filter(
                AuditLog.action_type == action,
                AuditLog.entity_type == "role",
                AuditLog.entity_name == name,
            )
            .order_by(AuditLog.timestamp)
            .all()
        )
    finally:
        s.close()


def _one_role(fresh, action, name):
    """Exactly one entry of *action* on the custom role called *name*."""
    rows = _role_rows(fresh, action, name)
    assert len(rows) == 1, f"expected 1 {action} entry, found {len(rows)}"
    return rows[0]


def _role_id(fresh, name):
    s = fresh()
    try:
        role = s.query(CustomRole).filter(CustomRole.name == name).first()
        return role.id if role is not None else None
    finally:
        s.close()


@pytest.mark.regression
def test_creating_a_custom_role_writes_one_role_create(client, fresh, admin):
    name = _role_name()
    body = {
        "name": name,
        "description": "reads experiments",
        "permissions": [{"resource": "experiment", "actions": ["read", "list"]}],
    }
    resp = client.post(f"{V1}/rbac/roles", json=body, headers=_auth(admin))
    assert resp.status_code == 201, resp.text

    row = _one_role(fresh, "role_create", name)
    assert row.entity_id == _role_id(fresh, name)
    assert (row.user_email, row.user_id) == (admin.email, admin.id)
    assert row.reason == "custom role created"
    assert _values(row) == (
        None,
        {
            "description": "reads experiments",
            "permissions": [{"resource": "experiment", "actions": ["read", "list"]}],
        },
    )

    # The same name again is a 409; a pair no role may hold is a 422. Neither
    # writes an entry.
    assert (
        client.post(f"{V1}/rbac/roles", json=body, headers=_auth(admin)).status_code
        == 409
    )
    refused = _role_name()
    resp = client.post(
        f"{V1}/rbac/roles",
        json={"name": refused, "permissions": [REFUSED]},
        headers=_auth(admin),
    )
    assert resp.status_code == 422, resp.text
    assert len(_role_rows(fresh, "role_create", name)) == 1
    assert _role_rows(fresh, "role_create", refused) == []


@pytest.mark.regression
def test_changing_a_custom_role_writes_one_role_update_per_change(
    client, fresh, admin, custom_role
):
    url = f"{V1}/rbac/roles/{custom_role.name}"
    resp = client.put(url, json={"description": "now wider"}, headers=_auth(admin))
    assert resp.status_code == 200, resp.text
    row = _one_role(fresh, "role_update", custom_role.name)
    assert row.entity_id == custom_role.id
    assert (row.user_email, row.user_id) == (admin.email, admin.id)
    assert row.reason == "custom role changed"
    # Only what changed.
    assert _values(row) == ({"description": "am221 role"}, {"description": "now wider"})

    # The same description again changes nothing and writes nothing.
    assert (
        client.put(
            url, json={"description": "now wider"}, headers=_auth(admin)
        ).status_code
        == 200
    )
    assert len(_role_rows(fresh, "role_update", custom_role.name)) == 1

    wider = [{"resource": "experiment", "actions": ["read", "update"]}]
    assert (
        client.put(url, json={"permissions": wider}, headers=_auth(admin)).status_code
        == 200
    )
    rows = _role_rows(fresh, "role_update", custom_role.name)
    assert len(rows) == 2
    assert _values(rows[1]) == (
        {"permissions": [{"resource": "experiment", "actions": ["read"]}]},
        {"permissions": wider},
    )

    # A 422 and a 404 write nothing.
    refused = client.put(url, json={"permissions": [REFUSED]}, headers=_auth(admin))
    assert refused.status_code == 422, refused.text
    missing = _role_name()
    gone = client.put(
        f"{V1}/rbac/roles/{missing}", json={"description": "x"}, headers=_auth(admin)
    )
    assert gone.status_code == 404, gone.text
    assert len(_role_rows(fresh, "role_update", custom_role.name)) == 2
    assert _role_rows(fresh, "role_update", missing) == []


@pytest.mark.regression
def test_deleting_a_custom_role_writes_one_role_delete(
    client, fresh, admin, target, custom_role
):
    assign = {"user_id": str(target.id), "role_name": custom_role.name}
    assert (
        client.post(f"{V1}/rbac/roles/assign", json=assign, headers=_auth(admin))
    ).status_code == 200
    url = f"{V1}/rbac/roles/{custom_role.name}"
    resp = client.delete(url, headers=_auth(admin))
    assert resp.status_code == 204, resp.text

    row = _one_role(fresh, "role_delete", custom_role.name)
    assert row.entity_id == custom_role.id
    assert (row.user_email, row.user_id) == (admin.email, admin.id)
    assert row.reason == "custom role deleted"
    assert _values(row) == (
        {
            "description": "am221 role",
            "permissions": [{"resource": "experiment", "actions": ["read"]}],
            "assigned_users": 1,
        },
        None,
    )

    # Gone: a second delete is a 404 and writes nothing.
    assert client.delete(url, headers=_auth(admin)).status_code == 404
    assert len(_role_rows(fresh, "role_delete", custom_role.name)) == 1


@pytest.mark.regression
def test_granting_a_direct_permission_writes_one_permission_grant(
    client, fresh, admin, target
):
    url = f"{V1}/rbac/users/{target.id}/grant"
    body = {
        "user_id": str(target.id),
        "resource": "feature_flag",
        "actions": ["update"],
        "reason": "Quarterly access review",
        "expires_at": "2030-01-01T00:00:00+00:00",
    }
    resp = client.post(url, json=body, headers=_auth(admin))
    assert resp.status_code == 201, resp.text

    row = _one(
        fresh, target.id, "permission_grant", email=admin.email, user_id=admin.id
    )
    assert row.entity_name == target.username
    # Fixed text, as role_assign has; the request's own reason is in new_value.
    assert row.reason == "direct permission granted"
    s = fresh()
    try:
        (grant,) = (
            s.query(DirectPermissionGrant)
            .filter(DirectPermissionGrant.user_id == target.id)
            .all()
        )
    finally:
        s.close()
    assert _values(row) == (
        None,
        {
            "resource": "feature_flag",
            "actions": ["update"],
            "expires_at": "2030-01-01T00:00:00+00:00",
            "grant_id": str(grant.id),
            "reason": "Quarterly access review",
        },
    )

    # A pair no grant may hold is a 422: nothing stored, nothing written.
    refused = client.post(
        url,
        json={"user_id": str(target.id), **REFUSED},
        headers=_auth(admin),
    )
    assert refused.status_code == 422, refused.text
    assert len(_rows(fresh, target.id, "permission_grant")) == 1


@pytest.mark.regression
def test_revoking_a_direct_permission_writes_one_permission_revoke(
    client, fresh, admin, target
):
    grant = {"user_id": str(target.id), "resource": "experiment", "actions": ["create"]}
    for _ in range(2):
        made = client.post(
            f"{V1}/rbac/users/{target.id}/grant", json=grant, headers=_auth(admin)
        )
        assert made.status_code == 201, made.text
    url = f"{V1}/rbac/users/{target.id}/grant/experiment"
    resp = client.delete(url, headers=_auth(admin))
    assert resp.json() == {"status": "revoked", "count": 2}

    row = _one(
        fresh, target.id, "permission_revoke", email=admin.email, user_id=admin.id
    )
    assert row.entity_name == target.username
    assert row.reason == "direct permission revoked"
    assert _values(row) == ({"resource": "experiment", "count": 2}, None)

    # Nothing left to revoke: count 0, nothing written.
    assert client.delete(url, headers=_auth(admin)).json()["count"] == 0
    assert len(_rows(fresh, target.id, "permission_revoke")) == 1


@pytest.mark.regression
def test_a_refused_entry_keeps_the_role_and_the_grant(
    client, fresh, db_session, admin, target, custom_role, caplog
):
    """Written after the commit, as role_assign is: a refused entry is logged
    (type only) and the change stands, for each of the five routes."""
    name = _role_name()
    grant = {"user_id": str(target.id), "resource": "audit_log", "actions": ["read"]}
    with caplog.at_level(logging.ERROR), refuse_audit(db_session):
        created = client.post(
            f"{V1}/rbac/roles", json={"name": name}, headers=_auth(admin)
        )
        changed = client.put(
            f"{V1}/rbac/roles/{name}",
            json={"description": "kept"},
            headers=_auth(admin),
        )
        deleted = client.delete(
            f"{V1}/rbac/roles/{custom_role.name}", headers=_auth(admin)
        )
        granted = client.post(
            f"{V1}/rbac/users/{target.id}/grant", json=grant, headers=_auth(admin)
        )
        revoked = client.delete(
            f"{V1}/rbac/users/{target.id}/grant/audit_log", headers=_auth(admin)
        )
    assert created.status_code == 201, created.text
    assert changed.status_code == 200, changed.text
    assert changed.json()["description"] == "kept"
    assert deleted.status_code == 204, deleted.text
    assert granted.status_code == 201, granted.text
    assert revoked.json() == {"status": "revoked", "count": 1}
    assert _role_id(fresh, name) is not None
    assert _role_id(fresh, custom_role.name) is None
    for action, role in (
        ("role_create", name),
        ("role_update", name),
        ("role_delete", custom_role.name),
    ):
        assert _role_rows(fresh, action, role) == [], action
    assert _rows(fresh, target.id) == []
    _assert_type_only_errors(caplog, n=5)


# --- workspace member role ------------------------------------------------------


def _workspace_with_member(client, owner, member, role="VIEWER") -> str:
    slug = f"{P}-{uuid.uuid4().hex[:8]}"
    created = client.post(
        f"{V1}/workspaces/",
        json={"name": "am221 ws", "slug": slug},
        headers=_auth(owner),
    )
    assert created.status_code == 201, created.text
    ws_id = created.json()["id"]
    added = client.post(
        f"{V1}/workspaces/{ws_id}/members",
        json={"user_id": str(member.id), "role": role},
        headers=_auth(owner),
    )
    assert added.status_code == 201, added.text
    return ws_id


def test_changing_a_members_workspace_role_writes_one_role_assign(
    client, fresh, admin, target
):
    ws_id = _workspace_with_member(client, admin, target)
    v2 = _v2_count(fresh)
    resp = client.put(
        f"{V1}/workspaces/{ws_id}/members/{target.id}",
        json={"role": "ADMIN"},
        headers=_auth(admin),
    )
    assert resp.status_code == 200, resp.text
    row = _one(fresh, target.id, "role_assign", email=admin.email, user_id=admin.id)
    assert _values(row) == (
        {"workspace_id": ws_id, "workspace_role": "VIEWER"},
        {"workspace_id": ws_id, "workspace_role": "ADMIN"},
    )

    # The same role again: nothing changes, nothing is written.
    again = client.put(
        f"{V1}/workspaces/{ws_id}/members/{target.id}",
        json={"role": "ADMIN"},
        headers=_auth(admin),
    )
    assert again.status_code == 200, again.text
    assert len(_rows(fresh, target.id, "role_assign")) == 1
    assert _v2_count(fresh) == v2


def test_a_refused_entry_keeps_the_workspace_role(
    client, fresh, db_session, admin, target, caplog
):
    ws_id = _workspace_with_member(client, admin, target)
    with caplog.at_level(logging.ERROR), refuse_audit(db_session):
        resp = client.put(
            f"{V1}/workspaces/{ws_id}/members/{target.id}",
            json={"role": "ADMIN"},
            headers=_auth(admin),
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "ADMIN"
    listed = client.get(f"{V1}/workspaces/{ws_id}/members", headers=_auth(admin))
    roles = {m["user_id"]: m["role"] for m in listed.json()}
    assert roles[str(target.id)] == "ADMIN"
    assert _rows(fresh, target.id) == []
    _assert_type_only_errors(caplog)


# --- SSO sign-in ----------------------------------------------------------------


def test_a_saml_sign_in_writes_one_user_login(client, fresh, db_session):
    domain = _domain()
    cfg = _saml_config(db_session, domain)
    user = _make_user(db_session, UserRole.ANALYST, email=f"ann@{domain}")
    v2 = _v2_count(fresh)

    resp = _acs(client, cfg, user.email)
    assert resp.status_code == 200, resp.text

    row = _one(fresh, user.id, "user_login", email=user.email, user_id=user.id)
    assert _values(row) == (None, {"provider": "sso"})
    # No group is mapped: the role is unchanged and no role entry is written.
    assert _rows(fresh, user.id, "role_assign") == []
    assert _v2_count(fresh) == v2


def test_the_dashboard_exchange_writes_one_user_login(client, fresh, db_session):
    user = _make_user(db_session, UserRole.VIEWER, email=f"bo@{_domain()}")
    code = sso_service.issue_handoff_code(user.id, sso_service.handoff_hash(SECRET))
    v2 = _v2_count(fresh)

    resp = client.post(f"{V1}/auth/sso/exchange", json={"code": code, "secret": SECRET})
    assert resp.status_code == 200, resp.text

    row = _one(fresh, user.id, "user_login", email=user.email, user_id=user.id)
    assert _values(row) == (None, {"provider": "sso"})
    assert _v2_count(fresh) == v2


def test_a_first_sso_sign_in_writes_user_create_by_the_new_account(
    client, fresh, db_session
):
    domain = _domain()
    cfg = _saml_config(db_session, domain)
    v2 = _v2_count(fresh)

    resp = _acs(client, cfg, f"new@{domain}")
    assert resp.status_code == 200, resp.text
    user_id = uuid.UUID(resp.json()["user_id"])
    user = _user(fresh, user_id)

    created = _one(fresh, user_id, "user_create", email=user.email, user_id=user_id)
    assert created.reason == "first SSO sign-in"
    old, new = _values(created)
    assert old is None
    assert new == {
        "username": user.username,
        "role": "VIEWER",
        "is_active": True,
        "is_superuser": False,
    }
    _one(fresh, user_id, "user_login", email=user.email, user_id=user_id)
    assert _v2_count(fresh) == v2


@pytest.mark.regression
@pytest.mark.parametrize("superuser", [False, True], ids=["admin", "superuser"])
def test_an_sso_role_change_writes_one_role_assign_by_sso_sync(
    client, fresh, db_session, superuser
):
    """``{role, is_superuser}`` before and after, from the reserved actor.

    SSO sign-in never changes the superuser flag, so it is the same on both
    sides; it is recorded all the same, as every ``role_assign`` writer does.
    """
    domain = _domain()
    cfg = _saml_config(db_session, domain, role_mapping={"readers": "viewer"})
    user = _make_user(
        db_session, UserRole.ADMIN, superuser=superuser, email=f"cy@{domain}"
    )
    v2 = _v2_count(fresh)

    resp = _acs(client, cfg, user.email, groups=["readers"])
    assert resp.status_code == 200, resp.text
    assert _user(fresh, user.id).role == UserRole.VIEWER

    row = _one(fresh, user.id, "role_assign", email=SSO_SYNC, user_id=None)
    assert _values(row) == (
        {"role": "ADMIN", "is_superuser": superuser},
        {"role": "VIEWER", "is_superuser": superuser},
    )
    assert row.reason == "SSO groups changed"
    assert row.entity_name == user.username
    assert _v2_count(fresh) == v2

    # The next sign-in finds the role already equal and writes no role entry.
    assert _acs(client, cfg, user.email, groups=["readers"]).status_code == 200
    assert len(_rows(fresh, user.id, "role_assign")) == 1
    assert len(_rows(fresh, user.id, "user_login")) == 2


@pytest.mark.regression
def test_a_refused_entry_still_applies_the_sso_role_change(
    client, fresh, db_session, caplog
):
    """The savepoint policy: the change applies, the sign-in is not refused,
    one ERROR with the exception type only, no entry, and the next sign-in
    writes none (the role is already equal, so a lost entry is not retried)."""
    domain = _domain()
    cfg = _saml_config(db_session, domain, role_mapping={"readers": "viewer"})
    user = _make_user(db_session, UserRole.ADMIN, email=f"dee@{domain}")

    with caplog.at_level(logging.ERROR), refuse_audit(db_session):
        resp = _acs(client, cfg, user.email, groups=["readers"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "viewer"
    assert _user(fresh, user.id).role == UserRole.VIEWER
    assert _rows(fresh, user.id) == []
    # One for the role change, one for the sign-in: both type only.
    _assert_type_only_errors(caplog, n=2)

    assert _acs(client, cfg, user.email, groups=["readers"]).status_code == 200
    assert _rows(fresh, user.id, "role_assign") == []


@pytest.mark.regression
def test_a_refused_entry_still_creates_the_sso_account(
    client, fresh, db_session, caplog
):
    domain = _domain()
    cfg = _saml_config(db_session, domain)

    with caplog.at_level(logging.ERROR), refuse_audit(db_session):
        resp = _acs(client, cfg, f"eve@{domain}")
    assert resp.status_code == 200, resp.text
    user_id = uuid.UUID(resp.json()["user_id"])
    assert _user(fresh, user_id) is not None
    assert _rows(fresh, user_id) == []
    _assert_type_only_errors(caplog, n=2)
