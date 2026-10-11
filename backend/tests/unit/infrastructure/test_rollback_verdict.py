"""A rollback ends with one verdict line, chosen by a written precedence table (#759).

``scripts/rollback_verdict.py`` decides the verdict of a Rollback run in one
place, ``decide()``: an ordered list of rows, the first matching row wins,
row 0 refusing any input outside its set. rollback.yml's "Run summary" step
gathers the inputs and prints the headline the script writes; it exits 0 only
for ROLLED BACK.

Two kinds of test, as the plan chose (verdict-759 plan v2, section 4):

* **The whole table through ``decide()``.** ``fixtures/rollback_verdict/cells.json``
  holds every reachable cell (538) and the 11 out-of-set cells, each with its
  expected verdict word and row. It is the output of the plan's prototype,
  which is independent of ``decide()``: the expected words are data, not a
  copy of the precedence logic.
* **The summary step itself**, run as the runner runs it (``bash -e``, a fake
  ``aws`` first on PATH, no credentials), once per row, per named overlap, per
  ``stop_wait`` value end to end, the guard, and the case where every read
  fails. Each asserts the WHOLE headline, a literal written from the plan's
  section 3, never a substring ("ROLLED BACK" is inside "NOT ROLLED BACK").
"""

from __future__ import annotations

import collections
import json
import re
import sys
from pathlib import Path

import pytest

from backend.tests.unit.infrastructure.test_dashboard_deploy_wiring import (
    ROLLBACK,
    SCRIPTS,
    Runner,
    _job,
    _load,
    _step,
    api_rules,
    rule,
)
from backend.tests.unit.infrastructure.test_deploy_docs import VERDICT_WORDS
from backend.tests.unit.infrastructure.test_forward_deploy_completes import (
    AFTER,
    BEFORE,
    BLUE,
    RULES_BLUE,
    RULES_GREEN,
    _services,
    _split,
    _task_set,
)
from backend.tests.unit.infrastructure.test_rollback_false_success import (
    BAD_ID,
    CD_REVERT_ID,
    CD_ROLLBACK_ID,
    ROLLBACK_ID,
    TARGET,
    _create_and_approve,
    _headline,
    _primary,
    _run_from_stop,
    _verify,
    runner,
)
from backend.tests.unit.infrastructure.test_rollback_false_success import (
    _t32 as t32,
)

pytestmark = pytest.mark.skipif(
    not ROLLBACK.is_file(), reason="this tree has no .github/workflows"
)

sys.path.insert(0, str(SCRIPTS))
try:
    import rollback_verdict as rv
finally:
    sys.path.remove(str(SCRIPTS))

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CELLS = json.loads((FIXTURES / "rollback_verdict" / "cells.json").read_text())
RUNS = FIXTURES / "rollback_runs"

DASHBOARD = "experimentation-dashboard-staging:6"
#: The account every fixture ARN carries (TARGET, the task sets api_serving.py
#: reads): no headline, summary or log line may print it (#759 C7).
ACCOUNT = "123456789012"
#: The plan's recount (section 2), per row, over the 538 reachable cells.
ROW_COUNTS = {
    "1": 12,
    "2": 4,
    "3": 6,
    "4": 57,
    "5": 51,
    "6": 96,
    "7": 156,
    "8": 104,
    "9": 52,
}

# --- the table, through decide() ---------------------------------------------------

#: A cell's L (the active list) as the summary hands it on: ACTIVE_RC, ACTIVE.
_ACTIVE = {
    "notread": ("", ""),
    "none": ("0", ""),
    "ids": ("0", "d-ACTIVE1"),
    "unreadable": ("254", ""),
    "garbage": ("0", "garbage"),
}


def _inputs(cell: dict) -> dict[str, str]:
    """The prototype's field names, as the summary's environment names."""
    active_rc, active = _ACTIVE[cell["active"]]
    return rv.read_inputs(
        {
            "TARGET_ENV": "staging",
            "TARGET_ARN": TARGET if cell["target"] else "",
            "REFUSED": cell["refused"],
            "ALREADY_SERVING": cell["already"],
            "STOPPED": cell["stopped"],
            "STOP_WAIT": cell["wait"],
            "DEPLOYMENT_ID": cell["d"],
            "API_VERIFY": cell["verify"],
            "DASHBOARD_INPUT": DASHBOARD if cell["dash_given"] else "",
            "DASHBOARD_OUTCOME": cell["dash"],
            "SERVING_RC": cell["read"],
            "SERVING_WORD": cell["word"],
            "ACTIVE_RC": active_rc,
            "ACTIVE": active,
        }
    )


def _row(cell: dict) -> str:
    """The committed row: "0 invalid" is row 0."""
    return cell["row"].split()[0]


REACHABLE = [c for c in CELLS if "case" not in c]
OUT_OF_SET = [c for c in CELLS if "case" in c]


def test_the_committed_table_has_the_plans_shape():
    """1b: the exact cell count, the per-row counts and the out-of-set cells
    are the plan's, so the table under test is the one the plan reviewed."""
    assert len(REACHABLE) == 538
    assert (
        sum(c["verify"] == "cancelled" or c["dash"] == "cancelled" for c in REACHABLE)
        == 96
    )
    assert collections.Counter(_row(c) for c in REACHABLE) == ROW_COUNTS
    assert sorted(c["case"] for c in OUT_OF_SET) == sorted(
        [
            "REFUSED=other",
            "ALREADY_SERVING=yes",
            "STOPPED=lowercase",
            "STOP_WAIT=done",
            "STOP_WAIT without STOPPED",
            "DEPLOYMENT_ID=None",
            "API_VERIFY=neutral",
            "DASHBOARD_OUTCOME=neutral",
            "X=1, WORD='' (import-time crash)",
            "X=0, WORD=primary-other",
            "ACTIVE=garbage",
        ]
    )
    assert all(_row(c) == "0" for c in OUT_OF_SET)
    # Every cell is distinct.
    keys = {
        json.dumps(
            {k: v for k, v in c.items() if k not in ("row", "verdict")}, sort_keys=True
        )
        for c in CELLS
    }
    assert len(keys) == len(CELLS)


@pytest.mark.regression
def test_every_cell_gets_the_committed_verdict():
    """1a: the full product, 549 cells, against words decide() did not write."""
    wrong = [
        (c, rv.decide(_inputs(c)))
        for c in CELLS
        if rv.decide(_inputs(c)) != (_row(c), c["verdict"])
    ]
    assert not wrong, wrong[:5]
    # No reachable cell reaches the fallback, and only row 3 exits 0.
    for c in REACHABLE:
        row, word = rv.decide(_inputs(c))
        assert row != rv.FALLBACK[0], c
        assert rv.exit_status(row) == (0 if word == "ROLLED BACK" else 1), c


def _swapped(i: str, j: str) -> list:
    rows = list(rv.ROWS)
    names = [row for row, _, _ in rows]
    a, b = names.index(i), names.index(j)
    rows[a], rows[b] = rows[b], rows[a]
    return rows


def _moved(rows: list) -> list[dict]:
    return [c for c in CELLS if rv.decide(_inputs(c), rows) != (_row(c), c["verdict"])]


def test_the_planted_swaps_are_between_overlapping_rows():
    """Premise 3: a swap of two rows that never hold together moves nothing,
    so the table above would pass it. These swaps move cells, the numbers the
    plan's prototype printed, so the table test fails on each of them."""
    assert len(_moved(_swapped("6", "8"))) == 106
    assert len(_moved(_swapped("5", "6"))) == 12
    assert len(_moved(_swapped("4", "6"))) == 63
    # The named overlap cells are among those that move.
    moved = _moved(_swapped("6", "8"))
    assert any(c["wait"] == "timeout" and c["word"] == "listener-split" for c in moved)
    # v1's literal row 4 ("V=success, so DO=failure; or X=0, so DO=skipped").
    literal = list(rv.ROWS)
    literal[4] = (
        "4",
        "API BACK, DASHBOARD NOT",
        lambda v: (
            v["DASHBOARD_INPUT"] != ""
            and (
                (v["API_VERIFY"] == "success" and v["DASHBOARD_OUTCOME"] == "failure")
                or (v["SERVING_RC"] == "0" and v["DASHBOARD_OUTCOME"] == "skipped")
            )
        ),
    )
    fallen = [c for c in CELLS if rv.decide(_inputs(c), literal)[0] == "fallback"]
    assert len(fallen) == 3 and all(c["dash"] == "cancelled" for c in fallen)
    # Dropping row 0 moves exactly the out-of-set cells; deleting any other
    # row moves exactly that row's cells.
    assert {json.dumps(c, sort_keys=True) for c in _moved(rv.ROWS[1:])} == {
        json.dumps(c, sort_keys=True) for c in OUT_OF_SET
    }
    for n in range(1, len(rv.ROWS)):
        row = rv.ROWS[n][0]
        assert len(_moved(rv.ROWS[:n] + rv.ROWS[n + 1 :])) == ROW_COUNTS[row], row


@pytest.mark.regression
def test_a_read_of_1_or_3_with_something_active_is_never_not_rolled_back():
    """Gate 4, the binding rule (#759 C5): exit 1 is also a listener
    mid-reroute and exit 3 a route that disagrees with its tasks; with a
    deployment active, or a list that failed, the answer is STILL MOVING or
    OUTCOME UNKNOWN, never NOT ROLLED BACK."""
    cells = [
        c
        for c in REACHABLE
        if c["read"] in ("1", "3") and c["active"] in ("ids", "unreadable")
    ]
    assert len(cells) > 100
    assert {rv.decide(_inputs(c))[1] for c in cells} == {
        "NOT FINISHED",
        "OUTCOME UNKNOWN",
        "STILL MOVING",
    }


def test_a_stop_whose_wait_is_absent_never_reads_nothing_changed():
    """Gate 3 (#759 C2): `stopped` set and `stop_wait` absent (a stop call
    failed) is a stop that may have taken effect."""
    cells = [c for c in REACHABLE if c["stopped"] and c["wait"] == ""]
    assert cells
    assert "NOTHING CHANGED" not in {rv.decide(_inputs(c))[1] for c in cells}


def test_the_runbook_names_every_verdict():
    """The runbook's "Reading the result" has one row per verdict word."""
    assert set(VERDICT_WORDS) == {word for _, word, _ in rv.ROWS}


# --- what the script prints --------------------------------------------------------


def test_the_per_id_sentence_is_one_literal_in_both_places():
    """C3/C4: one sentence, true for both creators, held whole in the script
    and in the workflow's bash guard (the T32 ban test scans both)."""
    assert rv.T32_ID.substitute(id="d-X") == t32("d-X")
    code = _step(ROLLBACK, "Run summary")["run"]
    assert code.count(rv.T32_ID.template) == 1
    assert rv.T32_ID.template in (SCRIPTS / "rollback_verdict.py").read_text()


def _main(capsys, **env: str) -> tuple[str, ...]:
    """The script's three lines, as (verdict, exit, slack)."""
    assert rv.main({"TARGET_ENV": "staging", **env}) == 0
    printed = capsys.readouterr().out.splitlines()
    assert [p.split("=", 1)[0] for p in printed] == ["verdict", "exit", "slack"]
    return tuple(p.split("=", 1)[1] for p in printed)


@pytest.mark.regression
def test_the_script_prints_no_account_and_one_bounded_line(capsys):
    """C7: the target by family:revision, free text redacted, flattened and
    cut, a value outside its set named by its variable only."""
    account = "123456789012"
    verdict, code, slack = _main(
        capsys,
        TARGET_ARN=TARGET,
        STOPPED=BAD_ID,
        STOP_WAIT="ok",
        DEPLOYMENT_ID=ROLLBACK_ID,
        API_VERIFY="success",
        DASHBOARD_INPUT=DASHBOARD,
        DASHBOARD_OUTCOME="failure",
        DASHBOARD_RESULT=f"failed: arn:aws:iam::{account}:role/x\nsecond line "
        + "x" * 500,
    )
    assert (verdict, code) == ("API BACK, DASHBOARD NOT", "1")
    assert account not in slack and "\n" not in slack
    assert slack.startswith(
        "staging: API BACK, DASHBOARD NOT: API rolled back to "
        "experimentation-backend-staging:42 (CodeDeploy deployment d-ROLLBACK1, "
        "verified); dashboard NOT rolled back (failed: arn:aws:iam::<account>:"
        "role/x second line xxx"
    ), slack
    (cut,) = re.findall(r"dashboard NOT rolled back \((.*?)\), so the API", slack)
    assert len(cut) == rv.MAX_FREE
    # Row 0 names the variable, never the value.
    verdict, code, slack = _main(capsys, TARGET_ARN=TARGET, STOPPED=f"d-{account}x")
    assert verdict == "OUTCOME UNKNOWN" and account not in slack
    assert slack == (
        "staging: OUTCOME UNKNOWN: this run's summary got a value of STOPPED it "
        "does not recognise, so it decides nothing (its verify step: not "
        "reached); read the run's steps."
    )


# --- the summary step, run as the runner runs it -----------------------------------

SERVING_42 = (
    "serving: experimentation-backend-staging:42 is the PRIMARY task set and "
    "the /api/* rule forwards to blue alone"
)
NOT_YET_43 = (
    "NOT YET: the PRIMARY task set runs experimentation-backend-staging:43, "
    "not experimentation-backend-staging:42"
)
SPLIT = (
    "NOT YET: the PRIMARY task set runs experimentation-backend-staging:42; the "
    "/api/* rule forwards to 2 target groups with weight: a CodeDeploy traffic "
    "shift is in progress. Wait for it to finish."
)
WRONG = (
    "WRONG: the PRIMARY task set runs experimentation-backend-staging:42, but "
    "the load balancer sends the API to green but the tasks serving the current "
    "release are in blue. The API route is already pointing at the wrong target "
    "group; no value of api_live_target_group is safe until that is repaired."
)
THROTTLED = (
    "UNKNOWN: could not tell: An error occurred (ThrottlingException) when "
    "calling the DescribeServices operation: Rate exceeded"
)
DIFF = (
    ", so the API and dashboard are on different releases: put the dashboard "
    "back with docs/deployment/rollback-runbook.md Method 2, the dashboard block"
)
LIST_FAILED = {
    "error": "An error occurred (ThrottlingException) when calling the "
    "ListDeployments operation: Rate exceeded"
}
#: What scripts/api_serving.py reads, per answer.
READS = {
    "0": api_rules(BEFORE, RULES_BLUE),
    "1": api_rules(AFTER, RULES_GREEN),
    "split": api_rules(BEFORE, _split(50, 50)),
    "2": [
        rule(
            "ecs describe-services",
            "experimentation-backend-staging",
            answers=[
                {
                    "error": "An error occurred (ThrottlingException) when calling "
                    "the DescribeServices operation: Rate exceeded"
                }
            ],
        )
    ],
    "3": api_rules(BEFORE, RULES_GREEN),
}
OK = {"stopped": BAD_ID, "stop_wait": "ok"}


@pytest.fixture
def summary_runner(runner):
    """The rollback harness, with the runner's own shell flags."""
    runner.shell_flags = ("-e",)
    return runner


def _summary(
    runner: Runner,
    *,
    stop: dict | None = None,
    deployment: str = "",
    verify: str = "skipped",
    dashboard: str = "",
    dash: str = "skipped",
    result: str = "",
    target: str = TARGET,
    read: str | None = None,
    listed: object = None,
    polls: str = "1",
) -> tuple[int, str, str, str]:
    """(exit, log, slack, summary) of the summary step alone. ``read`` picks
    what api_serving.py reads; ``listed`` is the active list's answer."""
    rules = list(READS[read]) if read else []
    if listed is not None:
        rules.append(rule("deploy list-deployments", answers=[listed]))
    runner.scenario(rules)
    outputs = {
        "target": {"arn": target} if target else {},
        "stop": dict(stop or {}),
        "codedeploy": {"deployment-id": deployment} if deployment else {},
        "api-verify": {"__outcome__": verify},
        "dashboard-target": {"arn": DASHBOARD} if dashboard else {},
        "dashboard-rollback": {"__outcome__": dash, "result": result},
    }
    code, log, written, summary = runner.run(
        ROLLBACK,
        _step(ROLLBACK, "Run summary"),
        outputs,
        {"dashboard_task_definition_arn": dashboard, "job.status": "failure"},
        SUMMARY_SERVING_POLLS=polls,
        SUMMARY_SERVING_POLL_SECONDS="0",
    )
    _one_output_line(runner)
    assert _headline(summary) == f"## Rollback of {written['slack']}"
    # C7: every row runs with the account in the target's ARN and in every
    # ARN the reads answer; none of it reaches the page, the log or Slack.
    for text in (log, summary, runner.raw_outputs["summary"]):
        assert ACCOUNT not in text, text
    return code, log, written["slack"], summary


def _one_output_line(runner: Runner) -> None:
    """C9: the runner fails a step whose GITHUB_OUTPUT has a line without `=`,
    and a value spread over two lines; the summary writes one `slack=` line."""
    raw = runner.raw_outputs["summary"]
    assert raw.endswith("\n") and raw.count("\n") == 1, raw
    assert raw.startswith("slack=") and all("=" in x for x in raw.splitlines()), raw


def _calls(runner: Runner, *prefix: str) -> int:
    return sum(c[: len(prefix)] == list(prefix) for c in runner.calls())


#: One harness run per row (and the named variants), each with its literal.
ROWS = {
    "0": (
        {
            "stop": {"stopped": BAD_ID, "stop_wait": "done"},
            "deployment": ROLLBACK_ID,
            "verify": "failure",
            "read": "1",
            "listed": ROLLBACK_ID,
        },
        "staging: OUTCOME UNKNOWN: this run's summary got a value of STOP_WAIT it "
        "does not recognise, so it decides nothing (its verify step: failure); "
        f"read the run's steps. {t32(ROLLBACK_ID)}",
    ),
    "1a": (
        {
            "stop": {
                "refused": "codeDeployRollback",
                "refused_id": CD_ROLLBACK_ID,
                "primary": "experimentation-backend-staging:43",
            },
        },
        f"staging: REFUSED: CodeDeploy's own rollback {CD_ROLLBACK_ID} is still "
        "active, so this run stopped nothing and created no deployment (the API "
        "is on experimentation-backend-staging:43); dashboard left as it is",
    ),
    "1b": (
        {"stop": {"refused": "unreadable", "refused_id": BAD_ID}},
        f"staging: REFUSED: the creator of in-flight deployment {BAD_ID} could not "
        "be read, so this run stopped nothing and created no deployment; "
        "dashboard left as it is",
    ),
    "2a": (
        {"target": ""},
        "staging: NOTHING CHANGED: the target revision was not resolved, so this "
        "run stopped nothing and created no deployment; dashboard left as it is",
    ),
    "2b": (
        {"read": "1", "listed": ""},
        "staging: NOTHING CHANGED: this run stopped nothing and created no "
        "deployment, so the API is as it was before the run; dashboard left as it is",
    ),
    "3": (
        {"stop": OK, "deployment": ROLLBACK_ID, "verify": "success"},
        "staging: ROLLED BACK: API rolled back to experimentation-backend-staging:42 "
        f"(CodeDeploy deployment {ROLLBACK_ID}, verified); dashboard left as it "
        f"is. {t32(ROLLBACK_ID)}",
    ),
    "4a": (
        {
            "stop": OK,
            "deployment": ROLLBACK_ID,
            "verify": "success",
            "dashboard": DASHBOARD,
            "dash": "failure",
            "result": "rejected by ECS's circuit breaker; serving "
            "experimentation-dashboard-staging:7",
        },
        "staging: API BACK, DASHBOARD NOT: API rolled back to "
        f"experimentation-backend-staging:42 (CodeDeploy deployment {ROLLBACK_ID}, "
        "verified); dashboard NOT rolled back (rejected by ECS's circuit breaker; "
        f"serving experimentation-dashboard-staging:7){DIFF}. {t32(ROLLBACK_ID)}",
    ),
    "4b": (
        {
            "stop": OK,
            "deployment": ROLLBACK_ID,
            "dashboard": DASHBOARD,
            "read": "0",
            "listed": ROLLBACK_ID,
        },
        "staging: API BACK, DASHBOARD NOT: API is serving "
        "experimentation-backend-staging:42, read at the end of the run, but this "
        "run did not finish its own steps (its verify step: skipped); dashboard "
        f"NOT rolled back (not reached){DIFF}. {t32(ROLLBACK_ID)}",
    ),
    "5": (
        {"stop": OK, "read": "0", "listed": ""},
        "staging: API BACK, RUN FAILED: API is serving "
        "experimentation-backend-staging:42, read at the end of the run, but this "
        "run did not finish its own steps (its verify step: skipped); dashboard "
        "left as it is.",
    ),
    "6": (
        {
            "stop": {"stopped": BAD_ID, "stop_wait": "timeout"},
            "read": "1",
            "listed": CD_REVERT_ID,
        },
        f"staging: NOT FINISHED: this run stopped {BAD_ID} with auto-rollback and "
        "the deployment group was still busy after about 5 minutes, so it created "
        f"no rollback deployment; the API at the end of the run: {NOT_YET_43} "
        "(scripts/api_serving.py exit 1); dashboard left as it is. "
        f"{t32(CD_REVERT_ID)}",
    ),
    "6-unreadable": (
        {
            "stop": {"stopped": BAD_ID, "stop_wait": "unreadable"},
            "read": "1",
            "listed": LIST_FAILED,
        },
        f"staging: NOT FINISHED: this run stopped {BAD_ID} with auto-rollback and "
        "the deployment group could not be read, so it created no rollback "
        f"deployment; the API at the end of the run: {NOT_YET_43} "
        "(scripts/api_serving.py exit 1); dashboard left as it is.",
    ),
    "7a": (
        {
            "stop": OK,
            "deployment": ROLLBACK_ID,
            "verify": "failure",
            "read": "2",
            "listed": "",
        },
        "staging: OUTCOME UNKNOWN: this run could not tell what the API is serving "
        f"(scripts/api_serving.py exit 2: {THROTTLED}); its verify step: failure; "
        "dashboard left as it is.",
    ),
    # C4 (b): a row-7 cell with this run's deployment set names it.
    "7b": (
        {
            "stop": OK,
            "deployment": ROLLBACK_ID,
            "verify": "failure",
            "read": "3",
            "listed": LIST_FAILED,
        },
        "staging: OUTCOME UNKNOWN: the API is not confirmed on "
        f"experimentation-backend-staging:42 (scripts/api_serving.py exit 3: "
        f"{WRONG}), and this run could not list the deployment group's active "
        "deployments, so check them before any dispatch; dashboard left as it "
        f"is. {t32(ROLLBACK_ID)}",
    ),
    "8a": (
        {
            "stop": OK,
            "deployment": ROLLBACK_ID,
            "verify": "failure",
            "read": "1",
            "listed": ROLLBACK_ID,
        },
        "staging: STILL MOVING: the API is not yet on "
        f"experimentation-backend-staging:42 (scripts/api_serving.py exit 1: "
        f"{NOT_YET_43}) while deployment {ROLLBACK_ID} is active; dashboard left "
        f"as it is. {t32(ROLLBACK_ID)}",
    ),
    "8a-two": (
        {
            "stop": OK,
            "deployment": ROLLBACK_ID,
            "verify": "failure",
            "read": "1",
            "listed": f"{ROLLBACK_ID}\t{CD_REVERT_ID}",
        },
        "staging: STILL MOVING: the API is not yet on "
        f"experimentation-backend-staging:42 (scripts/api_serving.py exit 1: "
        f"{NOT_YET_43}) while deployments {ROLLBACK_ID} {CD_REVERT_ID} are "
        f"active; dashboard left as it is. {t32(ROLLBACK_ID)} {t32(CD_REVERT_ID)}",
    ),
    "8b": (
        {
            "stop": OK,
            "deployment": ROLLBACK_ID,
            "verify": "failure",
            "read": "split",
            "listed": "",
        },
        "staging: STILL MOVING: the API is not yet on "
        f"experimentation-backend-staging:42 (scripts/api_serving.py exit 1: "
        f"{SPLIT}): no deployment is active, but the /api/* rule still splits "
        "traffic. Read it again: python3 scripts/api_serving.py "
        "experimentation-staging experimentation-backend-staging "
        "experimentation-backend-staging:42. If it still shows a split with no "
        "active deployment, the listener is stuck and needs a person: "
        "docs/deployment/rollback-runbook.md#reading-the-result, the STILL MOVING "
        "row; dashboard left as it is",
    ),
    "9": (
        {
            "stop": OK,
            "deployment": ROLLBACK_ID,
            "verify": "failure",
            "read": "1",
            "listed": "",
        },
        "staging: NOT ROLLED BACK: the API is not on "
        f"experimentation-backend-staging:42 (scripts/api_serving.py exit 1: "
        f"{NOT_YET_43}), and no deployment is active; this run stopped {BAD_ID} "
        f"with auto-rollback; CodeDeploy deployment {ROLLBACK_ID} is no longer "
        "active; dashboard left as it is",
    ),
}

#: The plan's named overlaps (section 2): the row that wins, end to end.
OVERLAPS = {
    # W=timeout with a read of 0: the read wins (5, no dashboard given).
    "timeout-and-read-0": (
        {
            "stop": {"stopped": BAD_ID, "stop_wait": "timeout"},
            "read": "0",
            "listed": CD_REVERT_ID,
        },
        "staging: API BACK, RUN FAILED: API is serving "
        "experimentation-backend-staging:42, read at the end of the run, but this "
        "run did not finish its own steps (its verify step: skipped); dashboard "
        f"left as it is. {t32(CD_REVERT_ID)}",
    ),
    # W=timeout with a split listener: 6, not 8.
    "timeout-and-split": (
        {
            "stop": {"stopped": BAD_ID, "stop_wait": "timeout"},
            "read": "split",
            "listed": "",
        },
        f"staging: NOT FINISHED: this run stopped {BAD_ID} with auto-rollback and "
        "the deployment group was still busy after about 5 minutes, so it created "
        f"no rollback deployment; the API at the end of the run: {SPLIT} "
        "(scripts/api_serving.py exit 1); dashboard left as it is.",
    ),
    # V=success with DO=cancelled: 4, not 3.
    "verified-and-dashboard-cancelled": (
        {
            "stop": OK,
            "deployment": ROLLBACK_ID,
            "verify": "success",
            "dashboard": DASHBOARD,
            "dash": "cancelled",
        },
        "staging: API BACK, DASHBOARD NOT: API rolled back to "
        f"experimentation-backend-staging:42 (CodeDeploy deployment {ROLLBACK_ID}, "
        "verified); dashboard NOT rolled back (its step was cancelled while it "
        f"ran){DIFF}. {t32(ROLLBACK_ID)}",
    ),
    # X=1 with the list unreadable: 7, not 8 or 9 (fail closed).
    "read-1-and-list-unreadable": (
        {
            "stop": OK,
            "deployment": ROLLBACK_ID,
            "verify": "failure",
            "read": "1",
            "listed": LIST_FAILED,
        },
        "staging: OUTCOME UNKNOWN: the API is not confirmed on "
        f"experimentation-backend-staging:42 (scripts/api_serving.py exit 1: "
        f"{NOT_YET_43}), and this run could not list the deployment group's "
        "active deployments, so check them before any dispatch; dashboard left as "
        f"it is. {t32(ROLLBACK_ID)}",
    ),
    # C3: CodeDeploy's own rollback in the list gets the same sentence, which
    # is true for it too (a new Rollback refuses while it is active).
    "codedeploy-rollback-listed": (
        {"stop": OK, "verify": "skipped", "read": "1", "listed": CD_ROLLBACK_ID},
        "staging: STILL MOVING: the API is not yet on "
        f"experimentation-backend-staging:42 (scripts/api_serving.py exit 1: "
        f"{NOT_YET_43}) while deployment {CD_ROLLBACK_ID} is active; dashboard "
        f"left as it is. {t32(CD_ROLLBACK_ID)}",
    ),
}


@pytest.mark.parametrize("case", list(ROWS), ids=list(ROWS))
def test_each_row_prints_its_literal(summary_runner, case):
    kwargs, expected = ROWS[case]
    code, log, slack, summary = _summary(summary_runner, **kwargs)
    assert slack == expected, slack
    # Exit 0 if and only if ROLLED BACK.
    assert code == (0 if case == "3" else 1), log
    # A decided run, one that changed nothing and a verified one read nothing.
    if case in ("1a", "1b", "2a", "2b", "3", "4a"):
        assert summary_runner.calls() == [], summary_runner.calls()
    else:
        assert _calls(summary_runner, "deploy", "list-deployments") == 1


@pytest.mark.parametrize("case", list(OVERLAPS), ids=list(OVERLAPS))
def test_each_named_overlap_goes_to_the_row_the_plan_names(summary_runner, case):
    kwargs, expected = OVERLAPS[case]
    code, log, slack, _ = _summary(summary_runner, **kwargs)
    assert slack == expected, slack
    assert code == 1, log


@pytest.mark.regression
def test_a_read_of_1_with_its_own_deployment_active_says_still_moving(summary_runner):
    """Regression 3 (#759 C5): exit 1 while this run's deployment is active.
    On main the headline said "API not confirmed on ...". Now STILL MOVING,
    naming the deployment and the per-id sentence, never NOT ROLLED BACK."""
    kwargs, expected = ROWS["8a"]
    code, _, slack, _ = _summary(summary_runner, **kwargs)
    assert slack.startswith("staging: STILL MOVING: "), slack
    assert slack == expected
    assert code == 1


def test_the_stop_steps_record_is_in_the_log_and_on_the_page(summary_runner):
    """Step outputs reach the log only through a later step's env header;
    the summary prints them where an operator reads (QA condition a)."""
    kwargs, _ = ROWS["6"]
    _, log, _, summary = _summary(summary_runner, **kwargs)
    assert (
        f"the stop step stopped: {BAD_ID}; its wait ended: timeout" in log.splitlines()
    )
    assert f"| Stopped by this run | `{BAD_ID}` |" in summary.splitlines()
    assert "| The stop step's wait | `timeout` |" in summary.splitlines()
    assert (
        f"| Active deployments at the end of the run | {CD_REVERT_ID} |"
        in summary.splitlines()
    )


@pytest.mark.regression
def test_every_read_failing_still_writes_the_verdict(summary_runner):
    """C9: api_serving.py exits 2 on every poll and the list fails. The step
    does not die under `bash -e`, writes `slack=`, and exits 1."""
    code, log, slack, _ = _summary(
        summary_runner,
        stop=OK,
        deployment=ROLLBACK_ID,
        verify="failure",
        read="2",
        listed=LIST_FAILED,
        polls="3",
    )
    assert code == 1, log
    assert slack == (
        "staging: OUTCOME UNKNOWN: this run could not tell what the API is serving "
        f"(scripts/api_serving.py exit 2: {THROTTLED}); its verify step: failure; "
        f"dashboard left as it is. {t32(ROLLBACK_ID)}"
    )
    assert _calls(summary_runner, "deploy", "list-deployments") == 1
    reads = [c for c in summary_runner.calls() if c[:2] == ["ecs", "describe-services"]]
    assert len(reads) == 3, reads


@pytest.mark.regression
def test_a_failed_list_naming_the_account_is_printed_without_it(summary_runner):
    """C7: AWS's error text for the summary's new read names the role, and
    with it the account. It is printed, scrubbed, and the verdict fails
    closed."""
    denied = {
        "error": "An error occurred (AccessDeniedException) when calling the "
        f"ListDeployments operation: User: arn:aws:sts::{ACCOUNT}:assumed-role/"
        "deploy/x is not authorized to perform: codedeploy:ListDeployments"
    }
    code, log, slack, _ = _summary(
        summary_runner,
        stop=OK,
        deployment=ROLLBACK_ID,
        verify="failure",
        read="1",
        listed=denied,
    )
    assert code == 1, log
    assert slack.startswith("staging: OUTCOME UNKNOWN: the API is not confirmed on "), (
        slack
    )
    # Not vacuous: the error is in the log, with the account replaced.
    assert (
        "could not list the deployment group's active deployments (exit 254: An "
        "error occurred (AccessDeniedException) when calling the ListDeployments "
        "operation: User: arn:aws:sts::<account>:assumed-role/deploy/x is not "
        "authorized to perform: codedeploy:ListDeployments)" in log.splitlines()
    ), log


# --- the guard: no verdict from the script -----------------------------------------


def _break_the_script(runner: Runner, body: str) -> None:
    """Replace scripts/rollback_verdict.py, and only it, in the step's checkout."""
    link = runner.work / "scripts"
    link.unlink()
    link.mkdir()
    for path in SCRIPTS.iterdir():
        if path.name != "rollback_verdict.py":
            (link / path.name).symlink_to(path)
    (link / "rollback_verdict.py").write_text(body)


@pytest.mark.regression
def test_the_guard_names_this_runs_deployment_when_the_script_does_not_run(
    summary_runner,
):
    """C4 (a): a verified rollback whose verdict script exits 3. The headline
    is the bash guard's, states the verify outcome, and names this run's
    deployment with the per-id sentence: on a red job, "outcome unknown" is
    what brings a second dispatch, which would revert this rollback."""
    _break_the_script(summary_runner, "import sys\nsys.exit(3)\n")
    code, log, slack, _ = _summary(
        summary_runner, stop=OK, deployment=ROLLBACK_ID, verify="success"
    )
    assert code == 1, log
    assert slack == (
        "staging: OUTCOME UNKNOWN: the verdict script did not run (exit 3); its "
        f"verify step: success; read the run's steps. {t32(ROLLBACK_ID)}"
    )


def test_the_guard_names_every_listed_deployment_once(summary_runner):
    """The guard's ids: this run's deployment, then each listed one, once,
    and nothing that is not a deployment id."""
    _break_the_script(summary_runner, "print('verdict=')\n")
    code, log, slack, _ = _summary(
        summary_runner,
        stop=OK,
        deployment=ROLLBACK_ID,
        verify="failure",
        read="1",
        listed=f"{ROLLBACK_ID}\t{CD_REVERT_ID}\tnot-an-id",
    )
    assert code == 1, log
    assert slack == (
        "staging: OUTCOME UNKNOWN: the verdict script did not run (exit 0); its "
        f"verify step: failure; read the run's steps. {t32(ROLLBACK_ID)} "
        f"{t32(CD_REVERT_ID)}"
    )


# --- the wiring --------------------------------------------------------------------

#: Where each of the script's inputs comes from in the summary step.
STEP_ENV = {
    "TARGET_ARN": "${{ steps.target.outputs.arn }}",
    "REFUSED": "${{ steps.stop.outputs.refused }}",
    "REFUSED_ID": "${{ steps.stop.outputs.refused_id }}",
    "REFUSED_PRIMARY": "${{ steps.stop.outputs.primary }}",
    "ALREADY_SERVING": "${{ steps.stop.outputs.already_serving }}",
    "STOPPED": "${{ steps.stop.outputs.stopped }}",
    "STOP_WAIT": "${{ steps.stop.outputs.stop_wait }}",
    "DEPLOYMENT_ID": "${{ steps.codedeploy.outputs.deployment-id }}",
    "API_VERIFY": "${{ steps.api-verify.outcome }}",
    "DASHBOARD_INPUT": "${{ inputs.dashboard_task_definition_arn }}",
    "DASHBOARD_OUTCOME": "${{ steps.dashboard-rollback.outcome }}",
    "DASHBOARD_RESULT": "${{ steps.dashboard-rollback.outputs.result }}",
}
JOB_ENV = {"TARGET_ENV", "ECS_CLUSTER", "ECS_BACKEND_SERVICE"}
#: The summary's own reads, handed to the script on its command line.
READ_RESULTS = {
    "SERVING_RC": "$serving_rc",
    "SERVING_WORD": "$serving_word",
    "SERVING_SAID": "$serving_said",
    "ACTIVE_RC": "$active_rc",
    "ACTIVE": "$active",
}


@pytest.mark.regression
def test_every_input_of_the_script_is_wired_to_its_source():
    """A name renamed in the workflow but not in the script reads as "": a
    renamed STOPPED would send a run that stopped something to NOTHING
    CHANGED. Each name is mapped, by exactly the expression it should be."""
    assert set(rv.INPUTS) == set(STEP_ENV) | JOB_ENV | set(READ_RESULTS)
    step = _step(ROLLBACK, "Run summary")
    env = step["env"]
    for name, expression in STEP_ENV.items():
        assert env.get(name) == expression, (name, env.get(name))
    assert JOB_ENV <= set(_job(ROLLBACK)["env"])
    (call,) = [
        line.strip()
        for line in step["run"].splitlines()
        if "scripts/rollback_verdict.py" in line
    ]
    for name, value in READ_RESULTS.items():
        assert f'{name}="{value}"' in call, (name, call)
    # The outputs the mapping reads are ones their steps write.
    stop = _step(ROLLBACK, "Stop any deployment already in flight")["run"]
    for key in (
        "refused",
        "refused_id",
        "primary",
        "already_serving",
        "stopped",
        "stop_wait",
    ):
        assert re.search(rf'echo "{key}=', stop), key


def test_the_stop_wait_spellings_written_are_the_ones_matched():
    """Gate 1c: the producer's `stop_wait` literals equal the script's set."""
    stop = _step(ROLLBACK, "Stop any deployment already in flight")["run"]
    written = set(re.findall(r'echo "stop_wait=([a-z]+)"', stop))
    assert written == set(rv.STOP_WAITS) == {"ok", "timeout", "unreadable"}


def _busy_forever() -> list:
    return [
        rule("deploy list-deployments", answers=[BAD_ID, CD_REVERT_ID]),
        rule("deploy get-deployment", BAD_ID, "creator", answers=["user\tNone"]),
        rule("deploy stop-deployment", BAD_ID, answers=[""]),
        rule(
            "deploy get-deployment",
            BAD_ID,
            "deploymentInfo.status",
            answers=["Stopped"],
        ),
    ]


#: One end-to-end run per `stop_wait` value, from the stop step on: the
#: stop step's real output reaches the verdict through the env mapping.
END_TO_END = {
    "ok": (
        [
            rule("deploy list-deployments", answers=[BAD_ID, CD_REVERT_ID, ""]),
            rule("deploy get-deployment", BAD_ID, "creator", answers=["user\tNone"]),
            rule("deploy stop-deployment", BAD_ID, answers=[""]),
            rule(
                "deploy get-deployment",
                BAD_ID,
                "deploymentInfo.status",
                answers=["Stopped"],
            ),
            *_verify(),
            *_primary(TARGET),
            *_create_and_approve(["InProgress", "InProgress", "Ready", "InProgress"]),
        ],
        "staging: ROLLED BACK: API rolled back to experimentation-backend-staging:42 "
        f"(CodeDeploy deployment {ROLLBACK_ID}, verified); dashboard left as it "
        f"is. {t32(ROLLBACK_ID)}",
    ),
    "timeout": (
        [*_busy_forever(), *api_rules(AFTER, RULES_GREEN)],
        f"staging: NOT FINISHED: this run stopped {BAD_ID} with auto-rollback and "
        "the deployment group was still busy after about 5 minutes, so it created "
        f"no rollback deployment; the API at the end of the run: {NOT_YET_43} "
        "(scripts/api_serving.py exit 1); dashboard left as it is. "
        f"{t32(CD_REVERT_ID)}",
    ),
    "unreadable": (
        [
            rule("deploy list-deployments", answers=[BAD_ID, LIST_FAILED]),
            *_busy_forever()[1:],
            *api_rules(AFTER, RULES_GREEN),
        ],
        f"staging: NOT FINISHED: this run stopped {BAD_ID} with auto-rollback and "
        "the deployment group could not be read, so it created no rollback "
        f"deployment; the API at the end of the run: {NOT_YET_43} "
        "(scripts/api_serving.py exit 1); dashboard left as it is.",
    ),
    # A stop call that failed: `stopped` set, `stop_wait` absent. Never
    # NOTHING CHANGED (gate 3); here nothing is active and the bad release is
    # still PRIMARY, so NOT ROLLED BACK, naming what was stopped.
    "absent": (
        [
            rule("deploy list-deployments", answers=[BAD_ID, ""]),
            rule("deploy get-deployment", BAD_ID, "creator", answers=["user\tNone"]),
            rule(
                "deploy stop-deployment",
                BAD_ID,
                answers=[
                    {
                        "error": "An error occurred (DeploymentAlreadyCompletedException) "
                        f"when calling the StopDeployment operation: Deployment {BAD_ID} "
                        "has already completed"
                    }
                ],
            ),
            *api_rules(AFTER, RULES_GREEN),
        ],
        "staging: NOT ROLLED BACK: the API is not on "
        f"experimentation-backend-staging:42 (scripts/api_serving.py exit 1: "
        f"{NOT_YET_43}), and no deployment is active; this run stopped {BAD_ID} "
        "with auto-rollback; dashboard left as it is",
    ),
}


@pytest.mark.regression
@pytest.mark.parametrize("wait", list(END_TO_END), ids=list(END_TO_END))
def test_each_stop_wait_reaches_its_verdict_end_to_end(runner, wait):
    """Gate 1c end to end, and the headline half of regression 2: what the
    stop step writes is what the verdict reads. On main a failed stop read
    "API not confirmed ... (its verify step: skipped)", with no stop in it."""
    runner.shell_flags = ("-e",)
    rules, expected = END_TO_END[wait]
    outputs, results, summary = _run_from_stop(
        runner, rules, SUMMARY_SERVING_POLLS="1", SUMMARY_SERVING_POLL_SECONDS="0"
    )
    if wait == "absent":
        assert "stop_wait" not in outputs["stop"], outputs["stop"]
    else:
        assert outputs["stop"]["stop_wait"] == wait, outputs["stop"]
    assert outputs["stop"]["stopped"] == BAD_ID
    assert outputs["summary"]["slack"] == expected, outputs["summary"]["slack"]
    assert _headline(summary) == f"## Rollback of {expected}"
    _one_output_line(runner)
    code, log = results["Run summary"]
    assert code == (0 if wait == "ok" else 1), log


@pytest.mark.regression
@pytest.mark.parametrize(
    "wait",
    ["ok", "timeout", "unreadable", ""],
    ids=["ok", "timeout", "unreadable", "absent"],
)
def test_already_serving_is_refused_whatever_the_wait_says(summary_runner, wait):
    """1c for row 1c: the refusal wins over every `stop_wait` value (a
    combination the stop step cannot write; the order still decides it)."""
    stop = {"already_serving": "true"}
    if wait:
        stop.update(stopped=BAD_ID, stop_wait=wait)
    code, log, slack, _ = _summary(summary_runner, stop=stop, verify="success")
    assert code == 1, log
    assert slack == (
        "staging: REFUSED: the API was already on experimentation-backend-staging:42 "
        "with nothing in flight, so this run stopped nothing and created no "
        "deployment; dashboard left as it is"
    )
    assert summary_runner.calls() == []


# --- the captured runs -------------------------------------------------------------


def _captured(name: str) -> dict:
    return json.loads((RUNS / name).read_text())


#: The stop step's outputs, by the name the summary's env gives each. A run's
#: header carries those it had; the older runs' predate `stopped`/`stop_wait`.
STOP_ENV = {
    "ALREADY_SERVING": "already_serving",
    "REFUSED": "refused",
    "REFUSED_ID": "refused_id",
    "REFUSED_PRIMARY": "primary",
    "STOPPED": "stopped",
    "STOP_WAIT": "stop_wait",
}


def _outputs_of(run: dict) -> tuple[dict, dict]:
    env = {k: v["value"] for k, v in run["summary"]["env"].items()}
    stop = dict(run["outputs"])
    stop.update({name: env[key] for key, name in STOP_ENV.items() if env.get(key)})
    outputs = {
        "target": {"arn": env["TARGET_ARN"]},
        "stop": stop,
        "codedeploy": {"deployment-id": env["DEPLOYMENT_ID"]},
        "api-verify": {"__outcome__": env["API_VERIFY"]},
        "dashboard-target": {"arn": env["DASHBOARD_TARGET_ARN"]},
        "dashboard-rollback": {
            "__outcome__": env["DASHBOARD_OUTCOME"],
            "result": env["DASHBOARD_RESULT"],
        },
    }
    # Inputs a later rollback.yml added; the older runs' headers predate them.
    if "DASHBOARD_SERVING_ARN" in env:
        outputs["dashboard-target"].update(
            serving_arn=env["DASHBOARD_SERVING_ARN"],
            serving_image=env["DASHBOARD_SERVING_IMAGE"],
        )
    if "ACCOUNT_CHECK" in env:
        outputs["account"] = {"__outcome__": env["ACCOUNT_CHECK"]}
    return env, outputs


@pytest.mark.regression
def test_the_green_run_gives_the_verified_headline_byte_for_byte(summary_runner):
    """Run 37217043185 (staging, green): its summary's inputs, verbatim from
    the step's env header, give row 3's literal, and the step exits 0. This
    is the line the #295 rehearsal checklist compares."""
    run = _captured("run-37217043185.json")
    env, outputs = _outputs_of(run)
    summary_runner.scenario([])
    code, log, written, summary = summary_runner.run(
        ROLLBACK,
        _step(ROLLBACK, "Run summary"),
        outputs,
        {
            "dashboard_task_definition_arn": env["DASHBOARD_INPUT"],
            "job.status": "success",
        },
    )
    assert code == 0, log
    assert written["slack"] == (
        "staging: ROLLED BACK: API rolled back to experimentation-backend-staging:2 "
        "(CodeDeploy deployment d-PS4WPZVAL, verified); dashboard rolled back to "
        "experimentation-dashboard-staging:6. Do not dispatch Rollback again while "
        "deployment d-PS4WPZVAL is active: a new Rollback stops any deployment that "
        "is not CodeDeploy's own rollback, with auto-rollback, which reverts what "
        "it shifted, and it refuses while CodeDeploy's own rollback is active."
    )
    assert summary_runner.calls() == []
    assert "the stop step stopped: d-LPIWVZUAL; its wait ended: ok" in log.splitlines()


@pytest.mark.regression
def test_the_run_whose_verify_failed_on_counts_reads_api_back(summary_runner):
    """Run 37176250648: the verify step from before #816 failed a rollback
    that worked, and the read said 0 (verbatim line in the fixture). With a
    dashboard given and not reached: API BACK, DASHBOARD NOT."""
    run = _captured("run-37176250648.json")
    env, outputs = _outputs_of(run)
    target = env["TARGET_ARN"]
    summary_runner.scenario(
        [
            *api_rules(_services(_task_set(target, BLUE, "PRIMARY")), RULES_BLUE),
            rule(
                "deploy list-deployments", answers=[run["summary"]["active"]["answer"]]
            ),
        ]
    )
    code, log, written, _ = summary_runner.run(
        ROLLBACK,
        _step(ROLLBACK, "Run summary"),
        outputs,
        {
            "dashboard_task_definition_arn": env["DASHBOARD_INPUT"],
            "job.status": "failure",
        },
        SUMMARY_SERVING_POLL_SECONDS="0",
    )
    assert code == 1, log
    assert run["summary"]["read"]["line"] in log.splitlines()
    assert written["slack"] == (
        "staging: API BACK, DASHBOARD NOT: API is serving "
        "experimentation-backend-staging:2, read at the end of the run, but this "
        "run did not finish its own steps (its verify step: failure); dashboard "
        f"NOT rolled back (not reached){DIFF}. {t32('d-JQJ7FPJAL')}"
    )


@pytest.mark.regression
def test_the_rehearsal_run_gives_its_own_lines_and_the_verified_headline(
    summary_runner,
):
    """Run 38098724258 (the #295 rehearsal, staging, green): its summary's
    inputs, verbatim from the step's env header, give the two lines that run
    printed, and row 3's literal; the step exits 0 and reads nothing. Its
    STOPPED and STOP_WAIT are the outputs that run wrote: none."""
    run = _captured("run-38098724258.json")
    env, outputs = _outputs_of(run)
    assert outputs["stop"] == {}
    summary_runner.scenario([])
    code, log, written, _ = summary_runner.run(
        ROLLBACK,
        _step(ROLLBACK, "Run summary"),
        outputs,
        {
            "dashboard_task_definition_arn": env["DASHBOARD_INPUT"],
            "job.status": env["JOB_STATUS"],
        },
    )
    assert code == 0, log
    assert log.splitlines() == run["summary"]["printed"]
    assert log.splitlines() == [
        "the stop step stopped: nothing; its wait ended: not recorded",
        "verdict: ROLLED BACK (this step exits 0)",
    ]
    assert written["slack"] == (
        "staging: ROLLED BACK: API rolled back to experimentation-backend-staging:12 "
        "(CodeDeploy deployment d-QB1QX42FL, verified); dashboard rolled back to "
        "experimentation-dashboard-staging:12. Do not dispatch Rollback again while "
        "deployment d-QB1QX42FL is active: a new Rollback stops any deployment that "
        "is not CodeDeploy's own rollback, with auto-rollback, which reverts what "
        "it shifted, and it refuses while CodeDeploy's own rollback is active."
    )
    assert summary_runner.calls() == []


def test_the_captured_summary_inputs_say_what_is_verbatim():
    """C8, for the summary's inputs: each says where it came from."""
    for name in (
        "run-37217043185.json",
        "run-37176250648.json",
        "run-38098724258.json",
    ):
        summary = _captured(name)["summary"]
        for value in summary["env"].values():
            assert value["source"].startswith("verbatim"), (name, value)
        for key in ("read", "active"):
            if key in summary:
                assert summary[key]["source"].startswith(("verbatim", "reconstructed"))


# --- the timeout budget (gate 7) ---------------------------------------------------

#: Every `sleep` in rollback.yml, as written: a new wait is a named value in
#: _rollback_budget, never an inline literal.
SLEEPS = [
    "sleep 5",
    '[ "$n" -eq 1 ] || sleep "$GROUP_IDLE_POLL_SECONDS"',
    "sleep 10",
    'sleep "$VERIFY_TASKS_POLL_SECONDS"',
    'sleep "$poll_seconds"',
]


def test_the_rollback_sleeps_are_exactly_these():
    lines = []
    for step in _load(ROLLBACK)["jobs"]["rollback"]["steps"]:
        for line in str(step.get("run", "")).splitlines():
            stripped = line.strip()
            if re.search(r"\bsleep\b", stripped) and not stripped.startswith("#"):
                lines.append(stripped)
    assert lines == SLEEPS
