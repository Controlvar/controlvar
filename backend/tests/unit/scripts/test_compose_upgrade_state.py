"""`scripts/compose_upgrade_state.py`: the compose upgrade keeps live state (#1897).

The compose-upgrade job (``scripts/compose_upgrade.sh``) seeds, at the previous
release, 20 users assigned to the probe experiment, 10 of them converting, and
the probe flag's answer for each; after every step it reads the same answers
before it writes anything and compares them with what it recorded. These tests
run that comparison on answers shaped as the API gives them, with no stack:
the state as seeded passes, and each of the three ways an upgrade can lose it
fails with a line that names what changed --

* a migration that deletes the assignments: the users have none;
* a migration that rewrites the flags' targeting_rules: the rule's users are
  answered by the rollout instead, while the rollout stays 30%;
* a migration that deletes the events: the results count no conversions.

None of the three moves anything ``check_rows`` reads (the experiment's id,
key and name; the flag's id, key and rollout percentage).

The answers below are the ones the API gives for the seed: the variants from
``consistent_hash.bucket_of`` over the experiment's key, the flag's from its
rule and its 30% rollout. The last tests hold the job's script to the order
the comparison depends on: the reads come before any write in each step.

No Docker, no database, no network.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from backend.app.core.targeting_adapter import (
    expand_context,
    match_targeting_rule,
    normalise_targeting_rules,
    validate_flag_targeting,
)
from backend.tests.unit.infrastructure import _workflow_graph as wg

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
TOOL = REPO_ROOT / "scripts" / "compose_upgrade_state.py"
JOB = REPO_ROOT / "scripts" / "compose_upgrade.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "compose-production.yml"

_spec = importlib.util.spec_from_file_location("compose_upgrade_state", TOOL)
cus = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cus)

WHEN = "after the upgrade"
EXPERIMENT_ID = "3c0e4a52-7d1b-4c41-9f3e-1a2b3c4d5e6f"
VARIANT_IDS = {
    "control": "0a1b2c3d-0000-4000-8000-000000000001",
    "treatment": "0a1b2c3d-0000-4000-8000-000000000002",
}
#: The users in treatment; the other eight are in control.
TREATMENT = {
    "cu-01",
    "cu-02",
    "cu-03",
    "cu-04",
    "cu-05",
    "cu-06",
    "cu-08",
    "cu-11",
    "cu-12",
    "cu-13",
    "cu-14",
    "cu-20",
}
#: The users the flag's 30% rollout turns on for.
ROLLOUT_ON = {"cu-01", "cu-02", "cu-10", "cu-12", "cu-13", "cu-16", "cu-17", "cu-19"}


def _users():
    return [user for user, _country, _converts in cus.PLAN]


def _country(user: str) -> str:
    return {u: c for u, c, _ in cus.PLAN}[user]


def seed_answers() -> Dict[str, Any]:
    """The API's answers to the three reads, for the state as seeded."""
    assignments = {u: ("treatment" if u in TREATMENT else "control") for u in _users()}
    flags = {}
    for user in _users():
        if _country(user) == cus.RULE_COUNTRY:
            flags[user] = (True, "targeting_rule")
        else:
            flags[user] = (user in ROLLOUT_ON, "rollout")
    return {
        "assignments": assignments,
        "flags": flags,
        "status": "active",
        "events": True,
    }


def _results(answers: Dict[str, Any]) -> Dict[str, Any]:
    """The results route's answer: a user counts in their assigned variant,
    and converts only with an assignment and an event (event_matching.py)."""
    converting = {u for u, _c, converts in cus.PLAN if converts}
    variants = []
    for name, variant_id in VARIANT_IDS.items():
        users = {u for u, v in answers["assignments"].items() if v == name}
        converted = users & converting if answers["events"] else set()
        variants.append(
            {
                "variant_id": variant_id,
                "variant_name": name,
                "is_control": name == "control",
                "sample_size": len(users),
                "conversions": len(converted),
                "mean": len(converted) / len(users) if users else 0.0,
            }
        )
    assigned = {u for u, v in answers["assignments"].items() if v}
    return {
        "experiment_id": EXPERIMENT_ID,
        "experiment_name": "Compose upgrade probe",
        "status": answers["status"],
        "summary": {
            "total_users": len(assigned),
            "total_events": 10 if answers["events"] else 0,
            "total_conversions": len(assigned & converting) if answers["events"] else 0,
        },
        "metrics": [
            {
                "metric_name": "Purchase",
                "metric_type": "conversion",
                "is_primary": True,
                "variants": variants,
            }
        ],
    }


def write_answers(directory: Path, answers: Dict[str, Any]) -> Path:
    """*answers* saved as the job saves the API's: one file per read."""
    (directory / "assignments").mkdir(parents=True)
    (directory / "flags").mkdir()
    for user, variant in answers["assignments"].items():
        listed = []
        if variant:
            listed.append(
                {
                    "id": f"row-{user}",
                    "user_id": user,
                    "experiment_id": EXPERIMENT_ID,
                    "experiment_name": "Compose upgrade probe",
                    "variant_id": VARIANT_IDS[variant],
                    "variant_name": variant,
                    "is_control": variant == "control",
                    "created_at": "2026-11-24T05:00:00",
                }
            )
        (directory / "assignments" / f"{user}.json").write_text(json.dumps(listed))
    for user, (enabled, reason) in answers["flags"].items():
        answer = {
            "key": "compose-upgrade-probe",
            "enabled": enabled,
            "value": enabled,
            "value_type": "boolean",
            "config": None,
            "reason": reason,
        }
        (directory / "flags" / f"{user}.json").write_text(json.dumps(answer))
    (directory / "results.json").write_text(json.dumps(_results(answers)))
    return directory


def state_of(tmp_path: Path, answers: Dict[str, Any], name: str) -> Dict[str, Any]:
    return cus.state(write_answers(tmp_path / name, answers), EXPERIMENT_ID)


@pytest.fixture
def recorded(tmp_path) -> Dict[str, Any]:
    return state_of(tmp_path, seed_answers(), "n1")


def run_tool(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TOOL), *args], capture_output=True, text=True, timeout=60
    )


# ---------------------------------------------------------------------------
# The seed


def test_the_plan_is_twenty_users_half_in_the_rule_country_ten_converting():
    users = _users()
    assert users == [f"cu-{n:02d}" for n in range(1, 21)]
    assert sum(1 for _u, country, _c in cus.PLAN if country == cus.RULE_COUNTRY) == 10
    assert sum(1 for _u, _country, converts in cus.PLAN if converts) == 10


def test_the_flag_rule_is_one_the_api_accepts_and_it_matches_the_rule_country():
    """The seed creates the flag with these rules: the create route's own
    validator accepts them, and they match a user in the rule's country and
    no other, at 100% (so the rule, not the rollout, decides for them)."""
    assert validate_flag_targeting(cus.FLAG_RULES) is not None
    rules = normalise_targeting_rules(
        cus.FLAG_RULES, owner="flag:compose-upgrade-probe"
    )
    matched = match_targeting_rule(rules, expand_context({"country": cus.RULE_COUNTRY}))
    assert matched is not None and matched.rollout_percentage == 100
    assert match_targeting_rule(rules, expand_context({"country": "DE"})) is None


def test_seeded_accepts_the_seed(recorded):
    assert cus.seeded(recorded) == []


@pytest.mark.parametrize(
    "change, expected",
    [
        pytest.param(
            lambda a: a["assignments"].update({"cu-05": None}),
            "N-1, after the seed: cu-05 has 0 assignments, expected 1",
            id="a user not assigned",
        ),
        pytest.param(
            lambda a: a.update({"events": False}),
            "N-1, after the seed: results total_conversions 0, expected 10",
            id="no conversion counted",
        ),
        pytest.param(
            lambda a: a["flags"].update({"cu-03": (False, "rollout")}),
            "N-1, after the seed: cu-03's flag answer is enabled false (rollout), "
            "expected enabled true (country US)",
            id="the rule did not decide",
        ),
        pytest.param(
            lambda a: a["flags"].update({"cu-04": (True, "targeting_rule")}),
            "N-1, after the seed: cu-04's flag answer is enabled true "
            "(targeting_rule), expected reason rollout (country DE)",
            id="the rule decided outside its country",
        ),
        pytest.param(
            lambda a: a.update({"status": "draft"}),
            "N-1, after the seed: results status draft, expected active",
            id="the experiment never started",
        ),
    ],
)
def test_seeded_refuses_a_seed_that_did_not_take(tmp_path, change, expected):
    answers = seed_answers()
    change(answers)
    assert expected in cus.seeded(state_of(tmp_path, answers, "n1"))


def test_seeded_holds_each_variant_to_the_users_assigned_to_it(tmp_path):
    """The results' per-variant counts are checked against the assignments
    read beside them, not only their totals."""
    current = state_of(tmp_path, seed_answers(), "n1")
    current["results"]["variants"]["control"]["conversions"] = 4
    current["results"]["variants"]["treatment"]["conversions"] = 6
    assert cus.seeded(current) == [
        "N-1, after the seed: results control conversions 4, expected 3 "
        "(the seeded users assigned control)",
        "N-1, after the seed: results treatment conversions 6, expected 7 "
        "(the seeded users assigned treatment)",
    ]


# ---------------------------------------------------------------------------
# After a step: unchanged, and the three plants


def test_the_same_state_passes(tmp_path, recorded):
    assert cus.compare(recorded, state_of(tmp_path, seed_answers(), "now"), WHEN) == []


def test_an_upgrade_that_deletes_the_assignments_is_named_by_user(tmp_path, recorded):
    answers = seed_answers()
    answers["assignments"] = dict.fromkeys(answers["assignments"])
    lines = cus.compare(recorded, state_of(tmp_path, answers, "now"), WHEN)
    assert lines[:2] == [
        "after the upgrade: 20 of 20 assignments are not as recorded at N-1",
        "after the upgrade: cu-01 has no assignment, recorded treatment at N-1",
    ]
    assert "after the upgrade: cu-07 has no assignment, recorded control at N-1" in (
        lines
    )
    assert lines[21:] == [
        "after the upgrade: results total_users 0, recorded 20 at N-1",
        "after the upgrade: results total_conversions 0, recorded 10 at N-1",
        "after the upgrade: results control sample_size 0, recorded 8 at N-1",
        "after the upgrade: results control conversions 0, recorded 3 at N-1",
        "after the upgrade: results treatment sample_size 0, recorded 12 at N-1",
        "after the upgrade: results treatment conversions 0, recorded 7 at N-1",
    ]


def test_an_upgrade_that_rewrites_the_targeting_rules_is_named_by_answer(
    tmp_path, recorded
):
    """The rule's users are answered by the 30% rollout: ten answers change,
    six of them to another value; nothing else moves."""
    answers = seed_answers()
    for user in _users():
        answers["flags"][user] = (user in ROLLOUT_ON, "rollout")
    lines = cus.compare(recorded, state_of(tmp_path, answers, "now"), WHEN)
    assert lines[0] == (
        "after the upgrade: 10 of 20 flag answers changed, 6 of them to another value"
    )
    assert lines[1:3] == [
        "after the upgrade: cu-01's flag answer is enabled true (rollout), recorded "
        "enabled true (targeting_rule) at N-1",
        "after the upgrade: cu-03's flag answer is enabled false (rollout), recorded "
        "enabled true (targeting_rule) at N-1",
    ]
    assert len(lines) == 11


def test_an_upgrade_that_deletes_the_events_is_named_in_the_results(tmp_path, recorded):
    answers = seed_answers()
    answers["events"] = False
    lines = cus.compare(recorded, state_of(tmp_path, answers, "now"), WHEN)
    assert lines == [
        "after the upgrade: results total_conversions 0, recorded 10 at N-1",
        "after the upgrade: results control conversions 0, recorded 3 at N-1",
        "after the upgrade: results treatment conversions 0, recorded 7 at N-1",
    ]


def test_a_moved_user_and_a_paused_experiment_are_named(tmp_path, recorded):
    answers = seed_answers()
    answers["assignments"]["cu-07"] = "treatment"
    answers["status"] = "paused"
    lines = cus.compare(recorded, state_of(tmp_path, answers, "now"), WHEN)
    assert lines[:2] == [
        "after the upgrade: 1 of 20 assignments are not as recorded at N-1",
        "after the upgrade: cu-07 is assigned treatment, recorded control at N-1",
    ]
    assert "after the upgrade: results status paused, recorded active at N-1" in lines


def test_a_variant_re_created_under_its_old_name_is_named(tmp_path, recorded):
    """Same name, new id: the user's row no longer points at the variant N-1
    stored."""
    observed = state_of(tmp_path, seed_answers(), "now")
    observed["assignments"]["cu-09"] = [
        {
            "variant_id": "0a1b2c3d-0000-4000-8000-0000000000ff",
            "variant_name": "control",
        }
    ]
    assert cus.compare(recorded, observed, WHEN) == [
        "after the upgrade: 1 of 20 assignments are not as recorded at N-1",
        "after the upgrade: cu-09 is assigned control with variant id "
        "0a1b2c3d-0000-4000-8000-0000000000ff, recorded "
        "0a1b2c3d-0000-4000-8000-000000000001 at N-1",
    ]


def test_a_lost_primary_metric_is_named(tmp_path, recorded):
    observed = copy.deepcopy(recorded)
    observed["results"]["metric"] = None
    observed["results"]["variants"] = {}
    lines = cus.compare(recorded, observed, WHEN)
    assert lines[0] == (
        "after the upgrade: results metric null, recorded Purchase at N-1"
    )
    assert lines[1].startswith("after the upgrade: results have no variant control")


# ---------------------------------------------------------------------------
# Assigning again


def _write_repeat(directory: Path, variants: Dict[str, Optional[str]]) -> Path:
    (directory / "repeat").mkdir(parents=True)
    for user, variant in variants.items():
        answer = {
            "experiment_key": "compose-upgrade-probe",
            "user_id": user,
            "variant_id": VARIANT_IDS[variant or "control"],
            "variant_name": variant or "control",
            "is_control": (variant or "control") == "control",
            "configuration": None,
            "assigned": variant is not None,
            "reason": "assigned" if variant else "targeting",
        }
        (directory / "repeat" / f"{user}.json").write_text(json.dumps(answer))
    return directory


def test_assigning_again_gives_the_recorded_variant(tmp_path, recorded):
    variants = dict(seed_answers()["assignments"])
    assert cus.repeat(recorded, _write_repeat(tmp_path / "r", variants), WHEN) == []


def test_assigning_again_to_another_variant_or_not_at_all_is_named(tmp_path, recorded):
    variants = dict(seed_answers()["assignments"])
    variants["cu-01"] = "control"
    variants["cu-02"] = None
    assert cus.repeat(recorded, _write_repeat(tmp_path / "r", variants), WHEN) == [
        "after the upgrade: 2 of 20 repeat assigns did not give the variant recorded at N-1",
        "after the upgrade: assigning cu-01 again gave control (assigned true, "
        "reason assigned), recorded treatment at N-1",
        "after the upgrade: assigning cu-02 again gave control (assigned false, "
        "reason targeting), recorded treatment at N-1",
    ]


# ---------------------------------------------------------------------------
# The command line the job runs


def test_the_commands_exit_0_on_the_same_state_and_1_with_the_lines(tmp_path):
    seed = write_answers(tmp_path / "n1", seed_answers())
    answers = seed_answers()
    answers["events"] = False
    later = write_answers(tmp_path / "later", answers)
    for directory in (seed, later):
        made = run_tool("state", str(directory), EXPERIMENT_ID)
        assert made.returncode == 0, made.stderr
        (directory / "state.json").write_text(made.stdout)

    assert run_tool("seeded", str(seed / "state.json")).returncode == 0
    same = run_tool("compare", str(seed / "state.json"), str(seed / "state.json"), WHEN)
    assert (same.returncode, same.stdout) == (0, "")

    differ = run_tool(
        "compare", str(seed / "state.json"), str(later / "state.json"), WHEN
    )
    assert differ.returncode == 1
    assert differ.stdout.splitlines()[0] == (
        "after the upgrade: results total_conversions 0, recorded 10 at N-1"
    )


def test_the_commands_exit_2_when_they_cannot_run(tmp_path):
    missing = run_tool(
        "compare", str(tmp_path / "none.json"), str(tmp_path / "x.json"), WHEN
    )
    assert missing.returncode == 2
    assert "no such file" in missing.stderr
    (tmp_path / "other.json").write_text(json.dumps({"assignments": {}}))
    assert run_tool("seeded", str(tmp_path / "other.json")).returncode == 2
    assert run_tool("compare", "only-one").returncode == 2
    assert run_tool().returncode == 2


def test_plan_and_flag_rules_print_what_the_job_reads():
    plan = run_tool("plan")
    assert plan.returncode == 0
    assert plan.stdout.splitlines()[:2] == ["cu-01 US yes", "cu-02 DE yes"]
    assert plan.stdout.splitlines()[-1] == "cu-20 DE no"
    rules = run_tool("flag-rules")
    assert rules.returncode == 0
    assert json.loads(rules.stdout) == cus.FLAG_RULES


# ---------------------------------------------------------------------------
# The job's script: the reads come first in every step


def _function(name: str) -> str:
    script = JOB.read_text(encoding="utf-8")
    found = re.search(rf"^{name}\(\) \{{\n(.*?)^\}}$", script, re.M | re.S)
    assert found, f"compose_upgrade.sh has no {name} function"
    return "\n".join(
        line
        for line in found.group(1).splitlines()
        if not line.lstrip().startswith("#")
    )


#: Each step: its function, the text that ends the transition, and the WHEN
#: and NAME the live state is checked under.
STEPS = [
    ("do_upgrade", "run_block upgrade ||", "after the upgrade", "upgrade"),
    ("do_restart", "dc restart api", "after restart api", "restart"),
    ("do_rollback", "run_block upgrade ||", "after rolling back in place", "rollback"),
    ("do_rollback", "run_block restore ||", "after the restore", "restore"),
]


@pytest.mark.parametrize("function, transition, when, name", STEPS)
def test_each_step_reads_the_live_state_before_it_writes(
    function, transition, when, name
):
    """After the transition: check_rows, then the live state read and
    compared, then the repeat assigns; and nothing between the transition and
    the read sends an assign, a track or any other POST of the job's own."""
    body = _function(function)
    start = body.index(transition)
    rows = body.index(f'check_rows "{when}"', start)
    read = body.index(f'check_live_state "{when}" {name}', start)
    again = body.index(f'repeat_assigns "{when}" {name}', start)
    assert start < rows < read < again
    between = body[start:read]
    for write in ("api POST", "sdk POST", "assign ", "api PUT", "api DELETE"):
        assert write not in between, f"{function}: {write!r} before the live-state read"


def test_the_install_records_the_live_state_after_the_rows():
    body = _function("do_install")
    rows = body.index('check_rows "N-1, after the seed"')
    assert rows < body.index("seed_live_state")
    assert 'python3 "$STATE_TOOL" flag-rules' in body
    assert "is_active: true, targeting_rules: $rules" in body


def test_the_live_state_read_only_reads_and_reads_the_results_afresh():
    body = _function("read_live_state")
    assert "POST" not in body
    order = [
        body.index("/api/v1/tracking/assignments/$user?active_only=false"),
        body.index('/api/v1/results/$eid?use_cache=false"'),
        body.index("/api/v1/feature-flags/evaluate/$PROBE_KEY"),
    ]
    assert order == sorted(order)


def test_the_api_key_never_goes_on_a_command_line():
    """The key is written to a header file and sent with curl's -H @file."""
    assert '-H @"$WORK/sdk-headers"' in _function("sdk")
    script = JOB.read_text(encoding="utf-8")
    assert "X-API-Key: $" not in script


@pytest.mark.skipif(not WORKFLOW.is_file(), reason="this tree has no .github/workflows")
def test_a_change_to_the_tool_runs_the_upgrade_job():
    paths = wg.triggers(wg.load(WORKFLOW))["pull_request"]["paths"]
    assert "scripts/compose_upgrade_state.py" in paths
