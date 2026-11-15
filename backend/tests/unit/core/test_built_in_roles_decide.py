"""Four built-in roles decide what a user may do, grants only add, and the core audit log is unsigned (#1142, #891).

The demo guide tells a presenter that four built-in roles are enforced, that
custom roles and direct grants (full profile) add permissions to them, and
that the core audit log's entries are not signed. These tests hold each of
those:

* ``UserRole`` has exactly the four roles;
* ``check_permission`` answers from the built-in role, plus the grantable
  pairs a user's custom roles and grants hold, and nothing else: a pair no
  check reads never takes effect, and a user object that is not a stored row
  gets its built-in role alone;
* ``AuditLog`` has no signature column (signed events are the compliance
  module's, in ``compliance_audit_events``).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session, make_transient_to_detached

from backend.app.core import hooks
from backend.app.core.permissions import (
    GRANTABLE,
    Action,
    ResourceType,
    check_permission,
    has_permission,
)
from backend.app.models.audit_log import AuditLog
from backend.app.models.user import User, UserRole

pytestmark = [pytest.mark.unit]

KEY = "rbac.granted_permissions"


def test_there_are_four_built_in_roles():
    assert {role.name for role in UserRole} == {
        "ADMIN",
        "DEVELOPER",
        "ANALYST",
        "VIEWER",
    }


@pytest.fixture
def grants_everything():
    """A provider whose user holds every pair of the vocabulary, and more."""
    everything = [(r.value, a.value) for r in ResourceType for a in Action] + [
        ("export", "read"),
        ("audit_log", "read"),
    ]
    saved = hooks.get_capability(KEY)
    hooks.register_capability(KEY, lambda session, user_id: everything)
    try:
        yield
    finally:
        hooks.register_capability(KEY, saved)


@pytest.mark.parametrize("role", list(UserRole), ids=lambda r: r.name)
def test_a_custom_role_or_a_grant_adds_only_grantable_pairs(role, grants_everything):
    user = User(
        id=uuid.uuid4(),
        username="decide",
        email="decide@roles.test",
        hashed_password="x",
        is_active=True,
        is_superuser=False,
        role=role,
    )
    make_transient_to_detached(user)
    Session().add(user)
    for resource in ResourceType:
        for action in Action:
            expected = has_permission(role, resource, action) or (
                (resource.value, action.value) in GRANTABLE
            )
            assert check_permission(user, resource, action) == expected, (
                role,
                resource,
                action,
            )
    # Administration is never granted: users, roles, other users' keys.
    if role is not UserRole.ADMIN:
        assert not check_permission(user, ResourceType.ROLE, Action.CREATE)
        assert not check_permission(user, ResourceType.API_KEY, Action.LIST)


@pytest.mark.parametrize("role", list(UserRole), ids=lambda r: r.name)
def test_a_user_that_is_not_a_stored_row_gets_its_built_in_role_alone(
    role, grants_everything
):
    user = SimpleNamespace(
        id=uuid.uuid4(),
        role=role,
        is_superuser=False,
        custom_roles=[SimpleNamespace(name="can-do-anything")],
    )
    for resource in ResourceType:
        for action in Action:
            assert check_permission(user, resource, action) == has_permission(
                role, resource, action
            ), (role, resource, action)


def test_an_audit_entry_carries_no_signature():
    columns = {column.name for column in AuditLog.__table__.columns}
    assert "timestamp" in columns and "user_email" in columns, columns
    assert not [c for c in columns if "sign" in c or "hmac" in c or "hash" in c]
