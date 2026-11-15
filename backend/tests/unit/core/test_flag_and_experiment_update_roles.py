"""Who can change feature flags is who can change experiments, for every built-in role.

A fact about the four built-in roles, pinned role by role for non-superusers
(a superuser passes every check). Nothing relies on it any more: in the full
profile a custom role or direct grant can give one of the two without the
other, so batch assignment (``POST /api/v1/tracking/assign/batch``) checks
``UPDATE`` on experiments for the key's owner itself
(``sdk_scope.require_batch_assign_key``), and
``modules/backend/tests/integration/rbac/test_rbac_server_keys.py`` is the
gate for that (#891).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.app.core.permissions import Action, ResourceType, check_permission
from backend.app.models.user import UserRole

pytestmark = [pytest.mark.unit]


@pytest.mark.parametrize("role", list(UserRole), ids=lambda r: r.value)
def test_flag_update_equals_experiment_update(role):
    user = SimpleNamespace(role=role, is_superuser=False)
    assert check_permission(user, ResourceType.FEATURE_FLAG, Action.UPDATE) == (
        check_permission(user, ResourceType.EXPERIMENT, Action.UPDATE)
    )


def test_the_roles_that_pass_are_admin_and_developer():
    """Guard the guard: equal because both are True for these two, and False
    for the others, not because the check answers the same thing for all."""
    passing = {
        role
        for role in UserRole
        if check_permission(
            SimpleNamespace(role=role, is_superuser=False),
            ResourceType.FEATURE_FLAG,
            Action.UPDATE,
        )
    }
    assert passing == {UserRole.ADMIN, UserRole.DEVELOPER}
