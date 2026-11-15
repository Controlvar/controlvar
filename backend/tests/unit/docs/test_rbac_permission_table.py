"""The RBAC page's permission table says what each grant admits (#891, check #48).

``docs/api/rbac.md``'s "What each permission allows" table has one row per
grantable pair, and each row names every kind of route that asks a decider
for that pair: the decider calls come from check #31's exact inventory
(``backend/tests/smoke/test_permission_decision_inventory.py``), so a new
route that a grant admits fails here until the table says so, and a row that
drops a route ("archive segments", say) fails too.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from backend.app.core.permissions import (
    GRANTABLE,
    ROLE_PERMISSIONS,
    Action,
    ResourceType,
)
from backend.tests.smoke.test_permission_decision_inventory import DECIDER_CALLS

pytestmark = [pytest.mark.unit]

REPO = pathlib.Path(__file__).resolve().parents[4]
PAGE = REPO / "docs" / "api" / "rbac.md"
E = "api/v1/endpoints/"

#: Each class-A decider call (file, function) -> the pairs it asks and the
#: words the row of each pair must hold for it.
WORDS = {
    ("api/deps.py", "can_create_feature_flag"): [
        (("feature_flag", "create"), "Create flags")
    ],
    ("api/deps.py", "get_experiment_change_access"): [
        (
            ("experiment", "update"),
            "Change, schedule, start, pause, complete, archive and annotate",
        ),
        (("experiment", "delete"), "Delete draft experiments"),
    ],
    ("api/sdk_scope.py", "batch_assign_refusal"): [
        (("feature_flag", "update"), "assigning users in a batch")
    ],
    ("api/sdk_scope.py", "may_use_server_key"): [
        (("feature_flag", "update"), "sdk:ruleset")
    ],
    (E + "audit_logs.py", "get_audit_stats"): [(("audit_log", "read"), "statistics")],
    (E + "audit_logs.py", "get_entity_audit_history"): [
        (("audit_log", "read"), "history")
    ],
    (E + "audit_logs.py", "get_user_activity"): [
        (("audit_log", "read"), "other users' entries")
    ],
    (E + "audit_logs.py", "resolve_audit_log_filters"): [
        (("audit_log", "read"), "other users' entries")
    ],
    (E + "bandit.py", "trigger_bandit_update"): [
        (("experiment", "update"), "bandit update")
    ],
    (E + "bulk_toggle.py", "bulk_toggle_flags"): [
        (("feature_flag", "update"), "bulk toggle")
    ],
    (E + "bulk_toggle.py", "get_flag_history"): [(("audit_log", "read"), "history")],
    (E + "bulk_toggle.py", "stream_audit_logs"): [
        (("audit_log", "read"), "other users' entries")
    ],
    (E + "experiment_wizard.py", "submit_draft"): [
        (("experiment", "create"), "experiment wizard")
    ],
    (E + "experiments.py", "clone_experiment"): [(("experiment", "create"), "clone")],
    (E + "experiments.py", "create_experiment"): [(("experiment", "create"), "Create")],
    (E + "feature_flags.py", "activate_feature_flag"): [
        (("feature_flag", "update"), "on and off")
    ],
    (E + "feature_flags.py", "deactivate_feature_flag"): [
        (("feature_flag", "update"), "on and off")
    ],
    (E + "feature_flags.py", "delete_feature_flag"): [
        (("feature_flag", "delete"), "Delete any flag")
    ],
    (E + "feature_flags.py", "disable_feature_flag"): [
        (("feature_flag", "update"), "on and off")
    ],
    (E + "feature_flags.py", "enable_feature_flag"): [
        (("feature_flag", "update"), "on and off")
    ],
    (E + "feature_flags.py", "toggle_feature_flag"): [
        (("feature_flag", "update"), "on and off")
    ],
    (E + "feature_flags.py", "unarchive_feature_flag"): [
        (("feature_flag", "update"), "Change any flag")
    ],
    (E + "feature_flags.py", "update_feature_flag"): [
        (("feature_flag", "update"), "targeting, values, rollout")
    ],
    (E + "global_holdout.py", "_require_developer"): [
        (("experiment", "create"), "read the global holdout")
    ],
    (E + "llm_experiments.py", "add_llm_variant"): [
        (("experiment", "update"), "LLM experiments")
    ],
    (E + "llm_experiments.py", "create_llm_experiment"): [
        (("experiment", "create"), "LLM experiments")
    ],
    (E + "llm_experiments.py", "pause_llm_experiment"): [
        (("experiment", "update"), "LLM experiments")
    ],
    (E + "llm_experiments.py", "start_llm_experiment"): [
        (("experiment", "update"), "LLM experiments")
    ],
    (E + "llm_experiments.py", "update_llm_experiment"): [
        (("experiment", "update"), "LLM experiments")
    ],
    (E + "llm_experiments.py", "update_llm_variant"): [
        (("experiment", "update"), "LLM experiments")
    ],
    (E + "llm_proxy.py", "run_judge"): [(("experiment", "update"), "LLM experiments")],
    (E + "llm_proxy.py", "submit_evaluation"): [
        (("experiment", "update"), "LLM experiments")
    ],
    (E + "mutual_exclusion_groups.py", "_require_developer"): [
        (("experiment", "create"), "manage exclusion groups")
    ],
    (E + "rollout_schedules.py", "_require_update_on_flag"): [
        (("feature_flag", "update"), "rollout schedules")
    ],
    (E + "segments.py", "_require_permission"): [
        (("experiment", "create"), "Create segments"),
        (("experiment", "update"), "change segments and their members"),
        (("experiment", "delete"), "archive segments"),
    ],
}

#: Decider calls that need no row words: pairs every built-in role holds, the
#: eight deps helpers no route uses, and API_KEY (not grantable).
NO_WORDS = {
    ("api/deps.py", f)
    for f in (
        "can_create_experiment",
        "can_create_report",
        "can_delete_feature_flag",
        "can_delete_report",
        "can_update_feature_flag",
        "can_update_report",
        "get_feature_flag_access",
        "get_report_access",
        "get_experiment_read_access",
        "require_experiment_list",
        "require_feature_flag_list",
    )
} | {
    (E + "api_keys.py", "_may_act_on_others_keys"),
    (E + "experiments.py", "get_experiment"),
    (E + "experiments.py", "list_experiments"),
    (E + "feature_flags.py", "get_feature_flag"),
    (E + "feature_flags.py", "list_feature_flags"),
    (E + "global_holdout.py", "get_holdout_results"),
    (E + "llm_experiments.py", "get_llm_experiment"),
    (E + "llm_experiments.py", "list_llm_experiments"),
    (E + "llm_proxy.py", "get_llm_results"),
    (E + "llm_proxy.py", "llm_complete"),
}

_ROW = re.compile(
    r"^\| `(?P<resource>[a-z_]+) \((?P<action>[a-z]+)\)` \| (?P<text>.+) \|$"
)


def table(text: str) -> dict:
    """The permission table's rows: pair -> what it allows."""
    start = text.index("### What each permission allows")
    end = text.index("### ", start + 4)
    rows = {}
    for line in text[start:end].splitlines():
        match = _ROW.match(line.strip())
        if match:
            rows[(match["resource"], match["action"])] = match["text"]
    return rows


def problems(rows: dict) -> list:
    found = []
    if set(rows) != set(GRANTABLE):
        found.append(f"rows {sorted(rows)} are not the eleven pairs")
    calls = {(f, fn) for f, fn, _, _ in DECIDER_CALLS}
    unmapped = calls - set(WORDS) - NO_WORDS
    if unmapped:
        found.append(
            f"a decider call the table does not account for: {sorted(unmapped)}"
        )
    for key, needs in WORDS.items():
        if key not in calls:
            found.append(f"{key} is no longer a decider call: update WORDS")
        for pair, words in needs:
            if words not in rows.get(pair, ""):
                found.append(f"{pair[0]} ({pair[1]}) does not say {words!r} ({key[1]})")
    for pair, text in rows.items():
        everyone = (
            all(
                Action(pair[1]) in ROLE_PERMISSIONS[role].get(ResourceType(pair[0]), [])
                for role in ROLE_PERMISSIONS
            )
            if pair[0] != "audit_log"
            else False
        )
        if everyone != ("Every built-in role already has it." in text):
            found.append(
                f"{pair}: 'Every built-in role already has it' is {not everyone}"
            )
    if (
        "It does not give the compliance audit events, reports or exports."
        not in rows.get(("audit_log", "read"), "")
    ):
        found.append(
            "audit_log (read) does not say it does not give the compliance trail"
        )
    return found


def test_the_table_says_what_each_grant_admits():
    assert problems(table(PAGE.read_text(encoding="utf-8"))) == []


def test_a_row_that_drops_a_route_is_named():
    rows = table(PAGE.read_text(encoding="utf-8"))
    rows[("experiment", "delete")] = "Delete draft experiments."
    assert problems(rows) == [
        "experiment (delete) does not say 'archive segments' (_require_permission)"
    ]
