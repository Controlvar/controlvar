"""Every permission decision in the modules is classified (#891, check #31, the twin).

The core's ``backend/tests/smoke/test_permission_decision_inventory.py`` holds
``backend/app``; a core file may not name a module's path, so this one holds
``modules/backend/app`` with the same scan: every decider call, and every
function that reads a ``role`` attribute, keyed on the module-qualified
function, with its class (A honours grants; B a built-in-role or workspace-role
decision by design; N not a decision).
"""

from __future__ import annotations

import pathlib

import pytest

from backend.tests.smoke.test_permission_decision_inventory import scan

pytestmark = [pytest.mark.smoke]

REPO = pathlib.Path(__file__).resolve().parents[4]
TREE = "modules/backend/app"
E = "api/v1/endpoints/"

DECIDER_CALLS = {
    # Writing a counter is experiment (update): class A.
    (
        E + "realtime_counters.py",
        "_require_counter_write",
        "check_permission",
        "ResourceType.EXPERIMENT, Action.UPDATE",
    ),
    # The display is built on the deciders (S7).
    (
        "services/rbac_service.py",
        "get_effective_permissions",
        "can_read_all_audit_logs",
        "",
    ),
    (
        "services/rbac_service.py",
        "get_effective_permissions",
        "check_permission",
        "resource, action",
    ),
}

_ADMIN = "module administration stays with the built-in role"
ROLE_READS = {
    (E + "compliance_reports.py", "export_audit_events"): (
        "B",
        "the compliance export stays with the built-in role",
    ),
    (E + "compliance_reports.py", "generate_compliance_report"): (
        "B",
        "compliance reports stay with the built-in role",
    ),
    (E + "etl.py", "_require_admin"): ("B", _ADMIN),
    (E + "etl.py", "_require_admin_or_developer"): ("B", _ADMIN),
    (E + "experiments_split_url.py", "preview_split_url_assignment"): (
        "B",
        "the split-URL preview stays with the built-in role",
    ),
    (E + "hipaa.py", "_require_admin"): ("B", _ADMIN),
    (E + "hipaa.py", "_require_admin_or_developer"): ("B", _ADMIN),
    (E + "integrations.py", "_require_admin"): ("B", _ADMIN),
    (E + "integrations.py", "_require_admin_or_developer"): ("B", _ADMIN),
    (E + "rbac.py", "_require_admin"): (
        "B",
        "RBAC administration must stay with the built-in role",
    ),
    (E + "realtime_counters.py", "reset_counters"): (
        "B",
        "resetting counters is ADMIN's",
    ),
    (E + "sso.py", "_issue_jwt"): ("N", "puts the role in the token's claims"),
    (E + "sso.py", "_oidc_sign_in"): (
        "N",
        "carries the role into the issued token",
    ),
    (E + "sso.py", "_require_admin"): ("B", _ADMIN),
    (E + "sso.py", "saml_acs"): ("N", "carries the role into the issued token"),
    (E + "warehouse_analysis.py", "_parameters"): (
        "B",
        "the warehouse analysis role bands read the built-in role",
    ),
    (E + "warehouse_analysis.py", "role_of"): (
        "B",
        "the warehouse analysis role bands read the built-in role",
    ),
    (E + "workspaces.py", "_invite_to_response"): ("N", "a workspace role, shown"),
    (E + "workspaces.py", "_member_to_response"): ("N", "a workspace role, shown"),
    (E + "workspaces.py", "add_member"): (
        "B",
        "workspace member roles are a separate model",
    ),
    (E + "workspaces.py", "create_invite"): (
        "B",
        "workspace member roles are a separate model",
    ),
    (E + "workspaces.py", "remove_member"): (
        "B",
        "workspace member roles are a separate model",
    ),
    (E + "workspaces.py", "update_member_role"): (
        "B",
        "workspace member roles are a separate model",
    ),
    ("models/workspace.py", "__repr__"): ("N", "a workspace role in a repr"),
    ("services/rbac_service.py", "get_effective_permissions"): (
        "N",
        "shows the built-in role",
    ),
    ("services/sso_service.py", "provision_user"): (
        "N",
        "sets the role of a provisioned user",
    ),
    ("services/workspace_service.py", "accept_invite"): (
        "B",
        "workspace member roles are a separate model",
    ),
    ("services/workspace_service.py", "check_member_permission"): (
        "B",
        "workspace member roles are a separate model",
    ),
    ("services/workspace_service.py", "remove_member"): (
        "B",
        "workspace member roles are a separate model",
    ),
    ("services/workspace_service.py", "update_member_role"): (
        "B",
        "workspace member roles are a separate model",
    ),
    ("warehouse/snowflake.py", "__post_init__"): (
        "N",
        "a Snowflake role, not a user's",
    ),
    ("warehouse/snowflake.py", "_submit"): ("N", "a Snowflake role, not a user's"),
}


def _tree():
    calls, reads = set(), set()
    root = REPO / TREE
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        c, r = scan(path.read_text(encoding="utf-8"), rel)
        calls |= c
        reads |= r
    return calls, reads


def test_every_decider_call_is_known():
    calls, _ = _tree()
    assert calls == DECIDER_CALLS, (
        f"unclassified decider calls: {sorted(calls - DECIDER_CALLS)}; "
        f"gone: {sorted(DECIDER_CALLS - calls)}"
    )


def test_every_role_read_is_classified():
    _, reads = _tree()
    assert reads == set(ROLE_READS), (
        f"unclassified role read: {sorted(reads - set(ROLE_READS))}; "
        f"gone: {sorted(set(ROLE_READS) - reads)}"
    )
