"""The dashboard's experiment and flag role lists equal the server's (#442, #917).

``frontend/src/utils/experimentPermissions.ts`` hand-types which roles the
dashboard offers each experiment and flag control to. ``canCreateExperiment``
gates "+ New Experiment" and Clone; ``canChangeExperiment`` gates the
lifecycle buttons, targeting, "Edit details" and "Delete draft";
``canChangeFeatureFlags`` gates "+ New Flag", the on/off switches, the flag
form and "Save Changes". This file reads those lists as text and compares
them with ``ROLE_PERMISSIONS``, so a role added on one side only fails here
instead of offering a button the API refuses (or hiding one it accepts).

``canAdministerEligibility`` (#1455) gates archiving an exclusion group and
the global holdout's admin actions. Its server side, ``_require_admin`` in
``mutual_exclusion_groups.py`` and ``global_holdout.py``, checks the role and
not the role table, so its list is compared with what both functions do when
called with each role (a behavioural twin), not with ``ROLE_PERMISSIONS``.
"""

from __future__ import annotations

import pathlib
import re
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.app.api.v1.endpoints import global_holdout, mutual_exclusion_groups
from backend.app.core.permissions import ROLE_PERMISSIONS, Action, ResourceType
from backend.app.models.user import UserRole

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
HELPER = REPO_ROOT / "frontend" / "src" / "utils" / "experimentPermissions.ts"


def _ts_roles(constant: str) -> set[str]:
    text = HELPER.read_text("utf-8")
    match = re.search(rf"const {constant} = \[([^\]]{{0,200}})\] as const;", text)
    assert match, f"{constant} not found in {HELPER.relative_to(REPO_ROOT)}"
    return set(re.findall(r"'([A-Z_]+)'", match.group(1)))


def _server_roles(
    action: Action, resource: ResourceType = ResourceType.EXPERIMENT
) -> set[str]:
    return {
        role.name
        for role, grants in ROLE_PERMISSIONS.items()
        if action in grants.get(resource, [])
    }


def test_create_roles_match():
    assert _ts_roles("ROLES_THAT_CAN_CREATE") == _server_roles(Action.CREATE)


def test_change_roles_match_update():
    assert _ts_roles("ROLES_THAT_CAN_CHANGE") == _server_roles(Action.UPDATE)


def test_change_roles_match_delete():
    # "Delete draft" is gated by canChangeExperiment, so the roles holding
    # DELETE must be exactly the ones holding UPDATE.
    assert _ts_roles("ROLES_THAT_CAN_CHANGE") == _server_roles(Action.DELETE)


def test_the_lists_are_not_empty():
    # A regex that stopped matching would compare two empty sets.
    assert _ts_roles("ROLES_THAT_CAN_CREATE") == {"ADMIN", "DEVELOPER"}
    assert _ts_roles("ROLES_THAT_CAN_CHANGE") == {"ADMIN", "DEVELOPER"}


# canChangeFeatureFlags answers create, toggle and update with one list, so
# the roles holding FEATURE_FLAG CREATE, UPDATE and DELETE must each be
# exactly that list (`can_act_on_feature_flag` checks the action it is given).
def test_flag_change_roles_match_create():
    assert _ts_roles("FLAG_ROLES_THAT_CAN_CHANGE") == _server_roles(
        Action.CREATE, ResourceType.FEATURE_FLAG
    )


def test_flag_change_roles_match_update():
    assert _ts_roles("FLAG_ROLES_THAT_CAN_CHANGE") == _server_roles(
        Action.UPDATE, ResourceType.FEATURE_FLAG
    )


def test_flag_change_roles_match_delete():
    assert _ts_roles("FLAG_ROLES_THAT_CAN_CHANGE") == _server_roles(
        Action.DELETE, ResourceType.FEATURE_FLAG
    )


def test_the_flag_list_is_not_empty():
    # A regex that stopped matching (a renamed constant) would compare two
    # empty sets above and pass.
    assert _ts_roles("FLAG_ROLES_THAT_CAN_CHANGE") == {"ADMIN", "DEVELOPER"}


# canAdministerEligibility (#1455, PE 9) gates archiving an exclusion group
# and the global holdout's admin actions. The server's `_require_admin` checks
# the role itself, not ROLE_PERMISSIONS, so `_server_roles` cannot describe
# it: this calls both modules' `_require_admin` with a stand-in user of each
# role and keeps the roles it does not refuse.
def _admitted_by(require_admin, *, superuser: bool) -> set[str]:
    admitted = set()
    for role in UserRole:
        try:
            require_admin(SimpleNamespace(role=role, is_superuser=superuser))
        except HTTPException as refused:
            assert refused.status_code == 403
        else:
            admitted.add(role.name)
    return admitted


@pytest.mark.parametrize(
    "module",
    [mutual_exclusion_groups, global_holdout],
    ids=["mutual_exclusion_groups", "global_holdout"],
)
def test_administer_eligibility_roles_match_require_admin(module):
    # The four built-in roles are all asked; a role enum that lost one would
    # make the admitted set too easy to match.
    assert {role.name for role in UserRole} >= {
        "ADMIN",
        "DEVELOPER",
        "ANALYST",
        "VIEWER",
    }
    admitted = _admitted_by(module._require_admin, superuser=False)
    assert admitted == _ts_roles("ROLES_THAT_ADMINISTER_ELIGIBILITY") == {"ADMIN"}


@pytest.mark.parametrize(
    "module",
    [mutual_exclusion_groups, global_holdout],
    ids=["mutual_exclusion_groups", "global_holdout"],
)
def test_require_admin_admits_a_superuser_of_any_role(module):
    # canAdministerEligibility answers true for any superuser before reading
    # the role; the server must too, or the dashboard offers a refused button.
    assert _admitted_by(module._require_admin, superuser=True) == {
        role.name for role in UserRole
    }
    helper = HELPER.read_text("utf-8")
    body = re.search(
        r"export function canAdministerEligibility\([^)]*\)[^{]*\{(.{0,400}?)\n\}",
        helper,
        re.S,
    )
    assert body, "canAdministerEligibility not found"
    assert "if (user.is_superuser) return true;" in body.group(1)
    # A null user (no session yet) is refused, not admitted (fail closed).
    assert "if (!user) return false;" in body.group(1)
