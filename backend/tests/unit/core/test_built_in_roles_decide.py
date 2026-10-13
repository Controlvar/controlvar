"""Four built-in roles decide what a user may do, and the core audit log is unsigned (#1142).

The demo guide tells a presenter that four built-in roles are enforced, that
custom roles and resource-level grants are stored but not enforced, and that
the core audit log's entries are not signed. These tests hold each of those:

* ``UserRole`` has exactly the four roles;
* ``check_permission`` answers from the base role alone, whatever custom roles
  or grants the user carries;
* ``AuditLog`` has no signature column (signed events are the compliance
  module's, in ``compliance_audit_events``).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.app.core.permissions import (
    Action,
    ResourceType,
    check_permission,
    has_permission,
)
from backend.app.models.audit_log import AuditLog
from backend.app.models.user import UserRole

pytestmark = [pytest.mark.unit]


def test_there_are_four_built_in_roles():
    assert {role.name for role in UserRole} == {
        "ADMIN",
        "DEVELOPER",
        "ANALYST",
        "VIEWER",
    }


@pytest.mark.parametrize("role", list(UserRole), ids=lambda r: r.name)
def test_a_custom_role_or_a_grant_does_not_change_what_a_user_can_do(role):
    everything = [
        {"resource": resource.value, "action": action.value}
        for resource in ResourceType
        for action in Action
    ]
    user = SimpleNamespace(
        role=role,
        is_superuser=False,
        custom_roles=[SimpleNamespace(name="can-do-anything", permissions=everything)],
        roles=["can-do-anything"],
        grants=everything,
        permissions=everything,
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
