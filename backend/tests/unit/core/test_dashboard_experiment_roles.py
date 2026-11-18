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

``canOverrideBanditWeights`` (#1957) gates "Set weights by hand" on the
experiment page's bandit panel. Its server side, ``override_bandit_weights``
(``PUT /api/v1/bandit/{id}/weights``), also checks the role itself, so it gets
the same kind of twin: the route is called with each role and a database that
refuses every use, and the roles that reach the database are the ones it
admits.

``canAnalyseInteractions`` (#1960) gates the Interactions page; its server
side, ``_require_developer`` in ``interactions.py``, names its roles too, so
it gets the same behavioural twin.
"""

from __future__ import annotations

import pathlib
import re
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.app.api.v1.endpoints import (
    bandit,
    global_holdout,
    interactions,
    mutual_exclusion_groups,
)
from backend.app.core.permissions import ROLE_PERMISSIONS, Action, ResourceType
from backend.app.models.user import UserRole
from backend.app.schemas.bandit import BanditUpdateRequest

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


# canOverrideBanditWeights (#1957) gates "Set weights by hand". The route,
# `override_bandit_weights`, refuses with 403 before it touches the database,
# so a caller it admits is the one that reaches the database: the stand-in
# session raises `_ReachedTheDatabase` on any use, and nothing is read or
# written.
class _ReachedTheDatabase(Exception):
    pass


class _NoDatabase:
    def __getattr__(self, name: str):
        raise _ReachedTheDatabase(name)


def _admitted_by_override(*, superuser: bool) -> set[str]:
    admitted = set()
    for role in UserRole:
        try:
            bandit.override_bandit_weights(
                experiment_id=uuid.uuid4(),
                body=BanditUpdateRequest(weights={"variant": 1.0}),
                db=_NoDatabase(),
                current_user=SimpleNamespace(role=role, is_superuser=superuser),
            )
        except HTTPException as refused:
            assert refused.status_code == 403
            assert refused.detail == "ADMIN role required to override bandit weights"
        except _ReachedTheDatabase:
            admitted.add(role.name)
        else:  # pragma: no cover - the stand-in session cannot answer
            raise AssertionError(
                f"{role.name}: neither refused nor reached the database"
            )
    return admitted


def _override_helper_body() -> str:
    helper = HELPER.read_text("utf-8")
    body = re.search(
        r"export function canOverrideBanditWeights\([^)]*\)[^{]*\{(.{0,400}?)\n\}",
        helper,
        re.S,
    )
    assert body, "canOverrideBanditWeights not found"
    return body.group(1)


def test_override_bandit_weights_roles_match_the_dashboard():
    # The four built-in roles are all asked; a role enum that lost one would
    # make the admitted set too easy to match.
    assert {role.name for role in UserRole} >= {
        "ADMIN",
        "DEVELOPER",
        "ANALYST",
        "VIEWER",
    }
    admitted = _admitted_by_override(superuser=False)
    assert admitted == _ts_roles("ROLES_THAT_OVERRIDE_BANDIT_WEIGHTS") == {"ADMIN"}
    # The helper reads its own list, not another route's that happens to agree
    # today (canAdministerEligibility's), and no effective map: the route
    # consults no custom role or direct grant.
    body = _override_helper_body()
    assert "ROLES_THAT_OVERRIDE_BANDIT_WEIGHTS.includes(" in body
    assert "ROLES_THAT_ADMINISTER_ELIGIBILITY" not in body
    assert "holds(" not in body


def test_override_bandit_weights_admits_a_superuser_of_any_role():
    # canOverrideBanditWeights answers true for any superuser before reading
    # the role; the route must too, or the dashboard hides a control the API
    # accepts.
    assert _admitted_by_override(superuser=True) == {role.name for role in UserRole}
    body = _override_helper_body()
    assert "if (user.is_superuser) return true;" in body
    # A null user (no session yet) is refused, not admitted (fail closed).
    assert "if (!user) return false;" in body


# canAnalyseInteractions (#1960) gates the Interactions link and every request
# of /experiments/interactions. The server's `_require_developer` in
# interactions.py names its roles (INTERACTION_ROLES) rather than reading
# ROLE_PERMISSIONS, so, like `_require_admin` above, it is called with each
# role and the roles it admits are held equal to the dashboard's list.
def test_analyse_interactions_roles_match_require_developer():
    assert {role.name for role in UserRole} >= {
        "ADMIN",
        "DEVELOPER",
        "ANALYST",
        "VIEWER",
    }
    admitted = _admitted_by(interactions._require_developer, superuser=False)
    assert (
        admitted
        == _ts_roles("ROLES_THAT_ANALYSE_INTERACTIONS")
        == {"ADMIN", "ANALYST", "DEVELOPER"}
    )


def test_require_developer_admits_a_superuser_of_any_role_and_refuses_no_role():
    assert _admitted_by(interactions._require_developer, superuser=True) == {
        role.name for role in UserRole
    }
    # An account with no base role is refused unless it is a superuser; the
    # dashboard shows such a user the role note and sends nothing.
    with pytest.raises(HTTPException) as refused:
        interactions._require_developer(SimpleNamespace(role=None, is_superuser=False))
    assert refused.value.status_code == 403
    interactions._require_developer(SimpleNamespace(role=None, is_superuser=True))
    helper = HELPER.read_text("utf-8")
    body = re.search(
        r"export function canAnalyseInteractions\([^)]*\)[^{]*\{(.{0,400}?)\n\}",
        helper,
        re.S,
    )
    assert body, "canAnalyseInteractions not found"
    assert "if (user.is_superuser) return true;" in body.group(1)
    # A null user (no session yet) is refused: no link, no request.
    assert "if (!user) return false;" in body.group(1)
