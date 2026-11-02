"""The ``rbac.granted_permissions`` provider's contract, generated (#891, check #29).

``granted_permission_pairs(session, user_id)`` on Postgres, against rows
planted by SQL in every state of matrix M2 of the #891 plan:

* an assignment {none, activated, not activated} x the role's stored JSON
  {valid, six malformed shapes}: only an activated assignment of a valid role
  yields its pairs, and a malformed entry is skipped, never raised on;
* a direct grant {none, active, not activated, ``is_active`` false, expired,
  expiring in the future, no expiry}: only an activated, active, unexpired
  grant yields its pairs. Expiry is set by SQL against the database's own
  clock, never by sleeping.

It writes nothing: a pending object in the session is still pending after.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from backend.app.models.user import User, UserRole
from backend.tests.integration.api.route_helpers import make_user
from modules.backend.app.services.rbac_service import granted_permission_pairs
from modules.backend.tests.integration.rbac.rbac_states import assign, cleanup, grant

pytestmark = [pytest.mark.integration, pytest.mark.requires_db, pytest.mark.regression]

TAG = "prv891"
PAIR = ("experiment", "update")

ROLE_JSON = {
    "valid": [{"resource": "experiment", "actions": ["update"]}],
    "not-a-list": {"resource": "experiment", "actions": ["update"]},
    "entry-not-an-object": ["experiment:update"],
    "no-actions": [{"resource": "experiment"}],
    "actions-not-a-list": [{"resource": "experiment", "actions": "update"}],
    "resource-not-a-string": [{"resource": 7, "actions": ["update"]}],
    "action-not-a-string": [{"resource": "experiment", "actions": [7, None]}],
}
ASSIGNMENTS = ("none", "activated", "not-activated")

GRANTS = {
    # state: (activated, is_active, expires_sql, yields)
    "none": None,
    "active": (True, True, "NULL", True),
    "not-activated": (False, True, "NULL", False),
    "is-active-false": (True, False, "NULL", False),
    "expired": (True, True, "now() - interval '1 hour'", False),
    "expires-in-the-future": (True, True, "now() + interval '1 hour'", True),
    "no-expiry": (True, True, "NULL", True),
}


@pytest.fixture
def user(db_session):
    return make_user(db_session, UserRole.VIEWER, tag=TAG)


@pytest.fixture(autouse=True)
def _cleanup(db_session):
    yield
    users = db_session.query(User).filter(User.username.like(f"{TAG}_%")).all()
    cleanup(db_session, TAG, [u.id for u in users])


def _role(db_session, stored) -> uuid.UUID:
    role_id = uuid.uuid4()
    db_session.execute(
        text(
            "INSERT INTO custom_roles (id, name, is_system_role, permissions, "
            "created_at, updated_at) VALUES (:id, :name, false, "
            "CAST(:perms AS jsonb), now(), now())"
        ),
        {
            "id": role_id,
            "name": f"{TAG}-{uuid.uuid4().hex[:10]}",
            "perms": json.dumps(stored),
        },
    )
    return role_id


@pytest.mark.parametrize("assignment", ASSIGNMENTS)
@pytest.mark.parametrize("shape", sorted(ROLE_JSON))
def test_an_assignment_yields_its_roles_pairs_only_when_activated(
    db_session, user, assignment, shape
):
    role_id = _role(db_session, ROLE_JSON[shape])
    if assignment != "none":
        assign(db_session, user.id, role_id, activated=assignment == "activated")
    db_session.commit()

    pairs = set(granted_permission_pairs(db_session, user.id))

    expected = {PAIR} if (assignment == "activated" and shape == "valid") else set()
    assert pairs == expected, (
        f"{assignment} assignment of a {shape} role yields {pairs}"
    )


@pytest.mark.parametrize("state", sorted(GRANTS))
def test_a_grant_yields_its_pairs_only_when_activated_active_and_unexpired(
    db_session, user, state
):
    spec = GRANTS[state]
    if spec is not None:
        activated, is_active, expires_sql, _ = spec
        grant(
            db_session,
            user.id,
            "experiment",
            ["update"],
            activated=activated,
            is_active=is_active,
            expires_sql=expires_sql,
        )
    db_session.commit()

    pairs = set(granted_permission_pairs(db_session, user.id))

    expected = {PAIR} if (spec is not None and spec[3]) else set()
    label = "NULL grant" if state == "not-activated" else f"{state} grant"
    assert pairs == expected, f"{label} yields {sorted(pairs)}"


def test_roles_and_grants_are_combined_and_other_users_rows_are_not_read(
    db_session, user
):
    other = make_user(db_session, UserRole.VIEWER, tag=TAG)
    role_id = _role(
        db_session,
        [
            {"resource": "experiment", "actions": ["create", "update"]},
            {"resource": "export", "actions": ["read"]},
        ],
    )
    assign(db_session, user.id, role_id, activated=True)
    grant(db_session, user.id, "feature_flag", ["update", "delete"])
    grant(db_session, other.id, "audit_log", ["read"])
    db_session.commit()

    pairs = set(granted_permission_pairs(db_session, user.id))

    # Pairs outside GRANTABLE (export) come back as stored: the core drops them.
    assert pairs == {
        ("experiment", "create"),
        ("experiment", "update"),
        ("export", "read"),
        ("feature_flag", "update"),
        ("feature_flag", "delete"),
    }


def test_the_provider_writes_nothing(db_session, user):
    pending = User(
        username=f"{TAG}_pending_{uuid.uuid4().hex[:6]}",
        email=f"{TAG}_{uuid.uuid4().hex[:6]}@prv.test",
        hashed_password="x",
        role=UserRole.VIEWER,
    )
    db_session.add(pending)
    assert pending in db_session.new

    granted_permission_pairs(db_session, user.id)

    assert pending in db_session.new, "the provider flushed the session"
    db_session.expunge(pending)
