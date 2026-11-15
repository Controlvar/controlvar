"""What ``GET /rbac/users/{id}/permissions`` shows is what the checks allow (#891, check #38).

The response for each actor and grant state is compared with an oracle
written here from the rules, never with ``check_permission`` (the display is
built on it, so that comparison would be a copy against itself):

* ``permissions`` holds only ``experiment``, ``feature_flag`` and
  ``audit_log``: the built-in role's actions on the first two, plus the
  grantable pairs of activated roles and activated, unexpired grants, and
  ``audit_log: ["read"]`` for ADMIN, ANALYST, a superuser or a grant of it;
* ``custom_roles`` lists only the roles assigned with an activation time.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from backend.app.core.permissions import ROLE_PERMISSIONS, ResourceType
from backend.app.models.user import User, UserRole
from backend.tests.integration.api.route_helpers import (
    api_client,
    auth,
    local_sign_in,
    make_user,
)
from modules.backend.tests.integration.rbac.rbac_states import (
    assign,
    cleanup,
    grant,
    make_role,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db, pytest.mark.regression]

V1 = "/api/v1"
TAG = "dsp891"
ACTORS = {
    "viewer": (UserRole.VIEWER, False),
    "analyst": (UserRole.ANALYST, False),
    "developer": (UserRole.DEVELOPER, False),
    "admin": (UserRole.ADMIN, False),
    "superuser": (UserRole.VIEWER, True),
}
ALL = ["create", "delete", "list", "read", "update"]


@pytest.fixture(autouse=True)
def _sign_in(monkeypatch):
    local_sign_in(monkeypatch)


@pytest.fixture
def client(db_session):
    with api_client(db_session) as c:
        yield c


@pytest.fixture(autouse=True)
def _cleanup(db_session):
    yield
    users = db_session.query(User).filter(User.username.like(f"{TAG}_%")).all()
    cleanup(db_session, TAG, [u.id for u in users])


def _oracle(role, superuser, active_pairs):
    if superuser:
        return {"experiment": ALL, "feature_flag": ALL, "audit_log": ["read"]}
    shown = {}
    for resource in ("experiment", "feature_flag"):
        actions = {
            a.value for a in ROLE_PERMISSIONS[role].get(ResourceType(resource), [])
        }
        actions |= {a for r, a in active_pairs if r == resource}
        if actions:
            shown[resource] = sorted(actions)
    if role in (UserRole.ADMIN, UserRole.ANALYST) or ("audit_log", "read") in (
        active_pairs
    ):
        shown["audit_log"] = ["read"]
    return shown


@pytest.mark.parametrize("actor", sorted(ACTORS))
def test_the_display_equals_the_rules(client, db_session, actor):
    role, superuser = ACTORS[actor]
    user = make_user(db_session, role, superuser=superuser, tag=TAG)

    active = make_role(db_session, TAG, [("experiment", "create")])
    inactive = make_role(db_session, TAG, [("experiment", "delete")])
    assign(db_session, user.id, active, activated=True)
    assign(db_session, user.id, inactive, activated=False)
    grant(db_session, user.id, "feature_flag", ["update"])
    grant(db_session, user.id, "audit_log", ["read"], activated=False)
    grant(
        db_session,
        user.id,
        "feature_flag",
        ["delete"],
        expires_sql="now() - interval '1 minute'",
    )
    grant(db_session, user.id, "export", ["read"])  # never shown
    db_session.commit()
    names = dict(
        db_session.execute(
            text("SELECT id, name FROM custom_roles WHERE id IN (:a, :i)"),
            {"a": active, "i": inactive},
        ).all()
    )

    resp = client.get(f"{V1}/rbac/users/{user.id}/permissions", headers=auth(user))
    assert resp.status_code == 200, resp.text
    body = resp.json()

    expected = _oracle(
        role, superuser, {("experiment", "create"), ("feature_flag", "update")}
    )
    assert body["permissions"] == expected, (actor, body["permissions"], expected)
    assert set(body["permissions"]) <= {"experiment", "feature_flag", "audit_log"}
    assert body["custom_roles"] == [names[active]], (
        f"custom_roles includes the inactive {names[inactive]!r}"
        if names[inactive] in body["custom_roles"]
        else body["custom_roles"]
    )
    assert body["is_superuser"] is superuser
