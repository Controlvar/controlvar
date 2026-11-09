"""Every permission decision in the core is classified, as an exact set (#891, check #31).

Custom roles and direct grants take effect inside the three deciders
(``check_permission``, ``can_act_on_feature_flag``, ``can_read_all_audit_logs``):
a route that asks one of them honours a grant of the pair it asks; a route
that compares ``user.role`` itself does not. Two exact sets, read from the
source with ``ast``, so a new decision fails here until it is classified:

* :data:`DECIDER_CALLS`: every call of a decider, as (file, function,
  decider, the resource and action it passes);
* :data:`ROLE_READS`: every function that reads a ``role`` attribute
  (``.role``, ``getattr(..., "role")``) or a role table, keyed on the
  module-qualified function (``interactions.py::_require_developer`` is
  class B while ``global_holdout.py::_require_developer`` asks a decider and
  is class A), each with its class and the reason:

  - ``A``: the decider itself (honours grants);
  - ``B``: a built-in-role decision by design: no grantable pair names what
    it gates (administration, the compliance trail, the split-URL preview,
    interaction analysis, notifications);
  - ``N``: not a decision (it shows, records or syncs the role).

The modules' twin is ``modules/backend/tests/smoke/test_permission_decision_inventory_modules.py``.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

pytestmark = [pytest.mark.smoke]

REPO = pathlib.Path(__file__).resolve().parents[3]
TREE = "backend/app"
DECIDERS = {"check_permission", "can_act_on_feature_flag", "can_read_all_audit_logs"}
TABLES = {"ROLE_PERMISSIONS", "FEATURE_FLAG_SCOPE", "AUDIT_LOG_READ_ALL_ROLES"}

E = "api/v1/endpoints/"
DECIDER_CALLS = {
    # deps.py: the routed helpers, and eight helpers no route uses (dead, kept).
    (
        "api/deps.py",
        "can_create_experiment",
        "check_permission",
        "ResourceType.EXPERIMENT, Action.CREATE",
    ),
    (
        "api/deps.py",
        "can_create_feature_flag",
        "check_permission",
        "ResourceType.FEATURE_FLAG, Action.CREATE",
    ),
    (
        "api/deps.py",
        "can_create_report",
        "check_permission",
        "ResourceType.REPORT, Action.CREATE",
    ),
    (
        "api/deps.py",
        "can_delete_feature_flag",
        "check_permission",
        "ResourceType.FEATURE_FLAG, Action.DELETE",
    ),
    (
        "api/deps.py",
        "can_delete_report",
        "check_permission",
        "ResourceType.REPORT, Action.DELETE",
    ),
    (
        "api/deps.py",
        "can_update_feature_flag",
        "check_permission",
        "ResourceType.FEATURE_FLAG, Action.UPDATE",
    ),
    (
        "api/deps.py",
        "can_update_report",
        "check_permission",
        "ResourceType.REPORT, Action.UPDATE",
    ),
    (
        "api/deps.py",
        "get_experiment_change_access",
        "check_permission",
        "ResourceType.EXPERIMENT, action",
    ),
    (
        "api/deps.py",
        "get_experiment_read_access",
        "check_permission",
        "ResourceType.EXPERIMENT, Action.READ",
    ),
    (
        "api/deps.py",
        "get_feature_flag_access",
        "check_permission",
        "ResourceType.FEATURE_FLAG, Action.READ",
    ),
    (
        "api/deps.py",
        "get_feature_flag_access",
        "check_permission",
        "ResourceType.FEATURE_FLAG, Action.UPDATE",
    ),
    (
        "api/deps.py",
        "get_report_access",
        "check_permission",
        "ResourceType.REPORT, Action.READ",
    ),
    (
        "api/deps.py",
        "get_report_access",
        "check_permission",
        "ResourceType.REPORT, Action.UPDATE",
    ),
    # The list-page rule of the list-shaped exports.
    (
        "api/deps.py",
        "require_experiment_list",
        "check_permission",
        "ResourceType.EXPERIMENT, Action.LIST",
    ),
    (
        "api/deps.py",
        "require_feature_flag_list",
        "check_permission",
        "ResourceType.FEATURE_FLAG, Action.LIST",
    ),
    # The server-key rule (option B) and batch assignment's own check.
    (
        "api/sdk_scope.py",
        "batch_assign_refusal",
        "check_permission",
        "ResourceType.EXPERIMENT, Action.UPDATE",
    ),
    (
        "api/sdk_scope.py",
        "may_use_server_key",
        "check_permission",
        "ResourceType.FEATURE_FLAG, Action.UPDATE",
    ),
    # API_KEY is not grantable: acting on other users' keys stays ADMIN's.
    (
        E + "api_keys.py",
        "_may_act_on_others_keys",
        "check_permission",
        "ResourceType.API_KEY, action",
    ),
    (E + "audit_logs.py", "get_audit_stats", "can_read_all_audit_logs", ""),
    (E + "audit_logs.py", "get_entity_audit_history", "can_read_all_audit_logs", ""),
    (E + "audit_logs.py", "get_user_activity", "can_read_all_audit_logs", ""),
    (E + "audit_logs.py", "resolve_audit_log_filters", "can_read_all_audit_logs", ""),
    (
        E + "bandit.py",
        "trigger_bandit_update",
        "check_permission",
        "ResourceType.EXPERIMENT, Action.UPDATE",
    ),
    (
        E + "bulk_toggle.py",
        "bulk_toggle_flags",
        "can_act_on_feature_flag",
        "flag.owner_id, Action.UPDATE",
    ),
    (E + "bulk_toggle.py", "get_flag_history", "can_read_all_audit_logs", ""),
    (E + "bulk_toggle.py", "stream_audit_logs", "can_read_all_audit_logs", ""),
    (
        E + "experiment_wizard.py",
        "submit_draft",
        "check_permission",
        "ResourceType.EXPERIMENT, Action.CREATE",
    ),
    (
        E + "experiments.py",
        "clone_experiment",
        "check_permission",
        "ResourceType.EXPERIMENT, Action.CREATE",
    ),
    (
        E + "experiments.py",
        "create_experiment",
        "check_permission",
        "ResourceType.EXPERIMENT, Action.CREATE",
    ),
    (
        E + "experiments.py",
        "get_experiment",
        "check_permission",
        "ResourceType.EXPERIMENT, Action.READ",
    ),
    (
        E + "experiments.py",
        "list_experiments",
        "check_permission",
        "ResourceType.EXPERIMENT, Action.LIST",
    ),
    (
        E + "feature_flags.py",
        "activate_feature_flag",
        "can_act_on_feature_flag",
        "flag.owner_id, Action.UPDATE",
    ),
    (
        E + "feature_flags.py",
        "deactivate_feature_flag",
        "can_act_on_feature_flag",
        "flag.owner_id, Action.UPDATE",
    ),
    (
        E + "feature_flags.py",
        "delete_feature_flag",
        "can_act_on_feature_flag",
        "flag.owner_id, Action.DELETE",
    ),
    (
        E + "feature_flags.py",
        "disable_feature_flag",
        "can_act_on_feature_flag",
        "flag.owner_id, Action.UPDATE",
    ),
    (
        E + "feature_flags.py",
        "enable_feature_flag",
        "can_act_on_feature_flag",
        "flag.owner_id, Action.UPDATE",
    ),
    (
        E + "feature_flags.py",
        "get_feature_flag",
        "can_act_on_feature_flag",
        "owner_row.owner_id, Action.READ",
    ),
    (
        E + "feature_flags.py",
        "list_feature_flags",
        "check_permission",
        "ResourceType.FEATURE_FLAG, Action.LIST",
    ),
    (
        E + "feature_flags.py",
        "toggle_feature_flag",
        "can_act_on_feature_flag",
        "flag.owner_id, Action.UPDATE",
    ),
    (
        E + "feature_flags.py",
        "unarchive_feature_flag",
        "can_act_on_feature_flag",
        "flag.owner_id, Action.UPDATE",
    ),
    (
        E + "feature_flags.py",
        "update_feature_flag",
        "can_act_on_feature_flag",
        "flag.owner_id, Action.UPDATE",
    ),
    # experiment (create) also governs reading the global holdout and
    # managing exclusion groups (ruling 4 item 8: accepted, documented).
    (
        E + "global_holdout.py",
        "_require_developer",
        "check_permission",
        "ResourceType.EXPERIMENT, Action.CREATE",
    ),
    (
        E + "global_holdout.py",
        "get_holdout_results",
        "check_permission",
        "ResourceType.EXPERIMENT, Action.READ",
    ),
    (
        E + "llm_experiments.py",
        "add_llm_variant",
        "check_permission",
        "ResourceType.EXPERIMENT, Action.UPDATE",
    ),
    (
        E + "llm_experiments.py",
        "create_llm_experiment",
        "check_permission",
        "ResourceType.EXPERIMENT, Action.CREATE",
    ),
    (
        E + "llm_experiments.py",
        "get_llm_experiment",
        "check_permission",
        "ResourceType.EXPERIMENT, Action.READ",
    ),
    (
        E + "llm_experiments.py",
        "list_llm_experiments",
        "check_permission",
        "ResourceType.EXPERIMENT, Action.LIST",
    ),
    (
        E + "llm_experiments.py",
        "pause_llm_experiment",
        "check_permission",
        "ResourceType.EXPERIMENT, Action.UPDATE",
    ),
    (
        E + "llm_experiments.py",
        "start_llm_experiment",
        "check_permission",
        "ResourceType.EXPERIMENT, Action.UPDATE",
    ),
    (
        E + "llm_experiments.py",
        "update_llm_experiment",
        "check_permission",
        "ResourceType.EXPERIMENT, Action.UPDATE",
    ),
    (
        E + "llm_experiments.py",
        "update_llm_variant",
        "check_permission",
        "ResourceType.EXPERIMENT, Action.UPDATE",
    ),
    (
        E + "llm_proxy.py",
        "get_llm_results",
        "check_permission",
        "ResourceType.EXPERIMENT, Action.READ",
    ),
    (
        E + "llm_proxy.py",
        "llm_complete",
        "check_permission",
        "ResourceType.EXPERIMENT, Action.READ",
    ),
    (
        E + "llm_proxy.py",
        "run_judge",
        "check_permission",
        "ResourceType.EXPERIMENT, Action.UPDATE",
    ),
    (
        E + "llm_proxy.py",
        "submit_evaluation",
        "check_permission",
        "ResourceType.EXPERIMENT, Action.UPDATE",
    ),
    (
        E + "mutual_exclusion_groups.py",
        "_require_developer",
        "check_permission",
        "ResourceType.EXPERIMENT, Action.CREATE",
    ),
    (
        E + "rollout_schedules.py",
        "_require_update_on_flag",
        "can_act_on_feature_flag",
        "flag.owner_id, Action.UPDATE",
    ),
    (
        E + "segments.py",
        "_require_permission",
        "check_permission",
        "ResourceType.EXPERIMENT, action",
    ),
}

ROLE_READS = {
    (E + "admin.py", "_role_name"): ("N", "shows a user's role in the admin listing"),
    (E + "admin.py", "patch_user"): (
        "B",
        "user administration (superuser only): changes a role",
    ),
    (E + "auth.py", "user_to_me"): ("N", "returns the signed-in user's role"),
    (E + "bandit.py", "override_bandit_weights"): (
        "B",
        "the bandit override is ADMIN's",
    ),
    (E + "compliance.py", "_require_role"): (
        "B",
        "the compliance trail stays with the built-in role",
    ),
    (E + "compliance.py", "list_audit_events"): (
        "B",
        "the compliance audit events stay with the built-in role",
    ),
    (E + "experiments.py", "preview_split_url_assignment"): (
        "B",
        "the split-URL preview stays with the built-in role",
    ),
    (E + "global_holdout.py", "_require_admin"): (
        "B",
        "holdout administration is ADMIN's",
    ),
    (E + "interactions.py", "_require_developer"): (
        "B",
        "interaction analysis stays with the built-in role",
    ),
    (E + "mutual_exclusion_groups.py", "_require_admin"): (
        "B",
        "exclusion-group status and archiving are ADMIN's",
    ),
    (E + "notifications.py", "get_delivery_log"): ("B", "notifications administration"),
    (E + "notifications.py", "list_all_preferences"): (
        "B",
        "notifications administration",
    ),
    (E + "notifications.py", "send_test_notification"): (
        "B",
        "notifications administration",
    ),
    (E + "users.py", "create_user"): ("B", "user administration"),
    (E + "users.py", "get_user_me"): ("N", "returns the signed-in user's role"),
    ("core/permissions.py", "can_act_on_feature_flag"): (
        "A",
        "a decider: honours grants",
    ),
    ("core/permissions.py", "can_read_all_audit_logs"): (
        "A",
        "a decider: honours grants",
    ),
    ("core/permissions.py", "check_permission"): ("A", "a decider: honours grants"),
    ("core/permissions.py", "has_permission"): (
        "A",
        "the built-in role table the deciders read first",
    ),
    ("core/security.py", "create_local_access_token"): (
        "N",
        "puts the role in the token's claims",
    ),
    ("services/audit_service.py", "role_value"): (
        "N",
        "records the actor's role in an audit entry",
    ),
    ("services/cognito_accounts.py", "resolve_cognito_user"): (
        "N",
        "syncs the role from Cognito groups",
    ),
}


def _functions(module):
    for node in ast.walk(module):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def scan(source: str, rel: str):
    """(decider calls, role-reading functions) of one module's *source*."""
    module = ast.parse(source)
    calls, reads = set(), set()
    for fn in _functions(module):
        reads_role = False
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                func = node.func
                name = (
                    func.id
                    if isinstance(func, ast.Name)
                    else getattr(func, "attr", None)
                )
                if name in DECIDERS:
                    args = ", ".join(ast.unparse(a) for a in node.args[1:3])
                    calls.add((rel, fn.name, name, args))
                if (
                    isinstance(func, ast.Name)
                    and func.id in ("getattr", "hasattr")
                    and len(node.args) >= 2
                    and isinstance(node.args[1], ast.Constant)
                    and node.args[1].value == "role"
                ):
                    reads_role = True
            elif isinstance(node, ast.Attribute) and node.attr == "role":
                if isinstance(node.ctx, ast.Load):
                    reads_role = True
            elif isinstance(node, ast.Name) and node.id in TABLES:
                reads_role = True
        if reads_role:
            reads.add((rel, fn.name))
    return calls, reads


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
    assert {cls for cls, _ in ROLE_READS.values()} <= {"A", "B", "N"}


def test_the_computed_b_set():
    """The class-B decisions, as the PR reports them (PB91-7)."""
    b = sorted(f"{f}::{fn}" for (f, fn), (cls, _) in ROLE_READS.items() if cls == "B")
    assert len(b) == 12, b


def test_a_role_comparison_in_a_route_is_named():
    """Positive control: check #30's plant, a route comparing the role itself."""
    source = """
def create_experiment(current_user):
    if current_user.role in ("ADMIN", "DEVELOPER"):
        return True
"""
    calls, reads = scan(source, E + "experiments.py")
    assert calls == set()
    assert reads == {(E + "experiments.py", "create_experiment")}
    assert reads - set(ROLE_READS) == {(E + "experiments.py", "create_experiment")}
