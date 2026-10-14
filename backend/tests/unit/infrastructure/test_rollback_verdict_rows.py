"""What each verdict of a Rollback run means, row by row (#1142, D73 TR4).

``scripts/rollback_verdict.py`` gives a Rollback run one verdict word, from an
ordered list of rows, the first that matches winning; rollback.yml's "Run
summary" step exits 0 only for ROLLED BACK. Each test below sets the inputs a
row describes (what the run's steps recorded, what the API was serving at the
end of the run, what the deployment group still had active) and asserts the
word, and where the meaning needs it, the headline, the summary step's exit
status, or what ``scripts/api_serving.py`` prints for the read the headline
quotes.

Reads the scripts and rollback.yml only; the step runs under the shared
Runner (a fake ``aws`` first on PATH, no AWS keys), so nothing reaches AWS.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

from backend.tests.unit.infrastructure.test_dashboard_deploy_wiring import (
    ROLLBACK,
    SCRIPTS,
    _step,
    api_rules,
)
from backend.tests.unit.infrastructure.test_forward_deploy_completes import (
    AFTER,
    BEFORE,
    CLUSTER,
    NEW,
    OLD,
    RULES_BLUE,
    RULES_GREEN,
    SERVICE,
    SERVING,
    aws,
    tick,
)
from backend.tests.unit.infrastructure.test_rollback_false_success import (
    TARGET,
    runner,
)

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(
        not ROLLBACK.is_file(), reason="this tree has no .github/workflows"
    ),
]

sys.path.insert(0, str(SCRIPTS))
try:
    import rollback_verdict as rv
finally:
    sys.path.remove(str(SCRIPTS))

ROLLBACK_ID = "d-ROLLBACK1"
BAD_ID = "d-BADDEPLOY"
REVERT_ID = "d-CDREVERT"


def _inputs(**given: str) -> dict[str, str]:
    """Every input the script reads, empty unless given: an empty input is a
    step that wrote nothing (not reached, or nothing to say)."""
    values = dict.fromkeys(rv.INPUTS, "")
    values.update(
        TARGET_ENV="staging",
        TARGET_ARN=TARGET,
        ECS_CLUSTER="experimentation-staging",
        ECS_BACKEND_SERVICE="experimentation-backend-staging",
    )
    values.update(given)
    return values


def _word(**given: str) -> str:
    return rv.decide(_inputs(**given))[1]


def _headline(**given: str) -> str:
    values = _inputs(**given)
    row, _ = rv.decide(values)
    return rv.headline(values, row)


def _per_id(deployment: str) -> str:
    return (
        f"Do not dispatch Rollback again while deployment {deployment} is active: "
        "a new Rollback stops any deployment that is not CodeDeploy's own "
        "rollback, with auto-rollback, which reverts what it shifted, and it "
        "refuses while CodeDeploy's own rollback is active."
    )


#: What the read at the end of the run said, by api_serving.py's exit.
SERVING_ON_TARGET = {
    "SERVING_RC": "0",
    "SERVING_WORD": "serving",
    "SERVING_SAID": "serving: experimentation-backend-staging:42 is the PRIMARY task set",
}
NOT_YET = {
    "SERVING_RC": "1",
    "SERVING_WORD": "primary-other",
    "SERVING_SAID": "NOT YET: experimentation-backend-staging:43 is the PRIMARY task set",
}
SPLIT = {
    "SERVING_RC": "1",
    "SERVING_WORD": "listener-split",
    "SERVING_SAID": "NOT YET: the /api/* rule splits traffic between blue and green",
}
WRONG = {
    "SERVING_RC": "3",
    "SERVING_WORD": "listener-other",
    "SERVING_SAID": "WRONG: the /api/* rule forwards to green alone",
}
UNKNOWN = {
    "SERVING_RC": "2",
    "SERVING_WORD": "unknown",
    "SERVING_SAID": "UNKNOWN: could not tell: ExpiredToken",
}
NONE_ACTIVE = {"ACTIVE_RC": "0", "ACTIVE": ""}


# --- the order of the rows, and the one success --------------------------------------


@pytest.mark.regression
def test_the_first_row_that_matches_wins_and_only_rolled_back_succeeds():
    """A run whose stop step refused, but whose other values would read as a
    rollback, is REFUSED: the refusal's row comes first. Every other verdict
    exits 1."""
    both = {
        "REFUSED": "codeDeployRollback",
        "REFUSED_ID": REVERT_ID,
        "DEPLOYMENT_ID": ROLLBACK_ID,
        "API_VERIFY": "success",
    }
    assert rv.decide(_inputs(**both)) == ("1", "REFUSED")
    # Without the refusal the same values are a rollback.
    del both["REFUSED"], both["REFUSED_ID"]
    assert rv.decide(_inputs(**both)) == ("3", "ROLLED BACK")
    rows = [row for row, _, _ in rv.ROWS]
    words = {word for _, word, _ in rv.ROWS}
    assert words >= {
        "ROLLED BACK",
        "API BACK, DASHBOARD NOT",
        "API BACK, RUN FAILED",
        "NOT FINISHED",
        "STILL MOVING",
        "NOT ROLLED BACK",
        "OUTCOME UNKNOWN",
        "REFUSED",
        "NOTHING CHANGED",
    }
    for row, word, _ in rv.ROWS:
        assert rv.exit_status(row) == (0 if word == "ROLLED BACK" else 1), row
    assert rv.exit_status(rv.FALLBACK[0]) == 1
    assert len(rows) == len(set(rows))


def _summary(runner, **outputs_env: str) -> tuple[int, str, dict, str]:
    """The Run summary step alone, with the step outputs it reads given as
    its env values."""
    runner.scenario([])
    return runner.run(
        ROLLBACK,
        _step(ROLLBACK, "Run summary"),
        {},
        {"job.status": "success"},
        **outputs_env,
    )


@pytest.mark.regression
def test_the_run_succeeds_only_when_the_verdict_is_rolled_back(runner):
    code, log, written, _ = _summary(
        runner,
        TARGET_ARN=TARGET,
        DEPLOYMENT_ID=ROLLBACK_ID,
        API_VERIFY="success",
        STOP_WAIT="",
    )
    assert code == 0, log
    assert written["slack"].startswith("staging: ROLLED BACK: "), written
    code, log, written, _ = _summary(
        runner,
        TARGET_ARN=TARGET,
        DEPLOYMENT_ID=ROLLBACK_ID,
        API_VERIFY="success",
        DASHBOARD_INPUT="experimentation-dashboard-staging:6",
        DASHBOARD_OUTCOME="failure",
        DASHBOARD_RESULT="rolled back by ECS",
    )
    assert code == 1, log
    assert written["slack"].startswith("staging: API BACK, DASHBOARD NOT: "), written


# --- one test per row ------------------------------------------------------------------


@pytest.mark.regression
def test_rolled_back_is_this_runs_own_deployment_approved_and_verified():
    given = {"DEPLOYMENT_ID": ROLLBACK_ID, "API_VERIFY": "success"}
    assert _word(**given) == "ROLLED BACK"
    # With a dashboard asked for, only when the dashboard was rolled back too.
    dashboard = dict(given, DASHBOARD_INPUT="experimentation-dashboard-staging:6")
    assert _word(**dashboard, DASHBOARD_OUTCOME="success") == "ROLLED BACK"
    assert _word(**dashboard, DASHBOARD_OUTCOME="failure") != "ROLLED BACK"
    # Not without its own verified deployment.
    assert _word(
        DEPLOYMENT_ID=ROLLBACK_ID, API_VERIFY="failure", **SERVING_ON_TARGET
    ) != ("ROLLED BACK")
    # The headline names that deployment, which stays active after the shift.
    assert _headline(**given).endswith(_per_id(ROLLBACK_ID))


@pytest.mark.regression
@pytest.mark.parametrize(
    "api",
    [
        {"DEPLOYMENT_ID": ROLLBACK_ID, "API_VERIFY": "success"},
        {"DEPLOYMENT_ID": ROLLBACK_ID, "API_VERIFY": "failure", **SERVING_ON_TARGET},
    ],
    ids=["verified", "read-at-the-end"],
)
@pytest.mark.parametrize("dashboard", ["failure", "cancelled", ""])
def test_api_back_dashboard_not_is_an_api_on_the_target_and_a_dashboard_left_behind(
    api, dashboard
):
    given = dict(
        api,
        DASHBOARD_INPUT="experimentation-dashboard-staging:6",
        DASHBOARD_OUTCOME=dashboard,
    )
    assert _word(**given) == "API BACK, DASHBOARD NOT"
    assert "the API and dashboard are on different releases" in _headline(**given)
    # The same API with no dashboard asked for is not this verdict.
    assert _word(**api) != "API BACK, DASHBOARD NOT"


@pytest.mark.regression
def test_api_back_run_failed_is_an_api_read_on_the_target_after_a_later_step_failed():
    """The stop step's auto-rollback put the API back, and then creating the
    rollback deployment failed: nothing verified it, the read at the end did."""
    given = dict(STOPPED=BAD_ID, STOP_WAIT="ok", **SERVING_ON_TARGET, **NONE_ACTIVE)
    assert _word(**given) == "API BACK, RUN FAILED"
    assert "read at the end of the run" in _headline(**given)
    # Not when the read says another revision is serving.
    assert _word(**dict(given, **NOT_YET)) != "API BACK, RUN FAILED"


@pytest.mark.regression
@pytest.mark.parametrize("wait", ["timeout", "unreadable"])
def test_not_finished_is_a_stop_whose_wait_for_an_idle_group_did_not_end(wait):
    given = dict(
        STOPPED=BAD_ID, STOP_WAIT=wait, **NOT_YET, ACTIVE_RC="0", ACTIVE=REVERT_ID
    )
    assert _word(**given) == "NOT FINISHED"
    headline = _headline(**given)
    assert "created no rollback deployment" in headline
    # It quotes what the API was serving at the end, and names what is active.
    assert NOT_YET["SERVING_SAID"] in headline
    assert headline.endswith(_per_id(REVERT_ID))
    # A wait that ended ok is not this verdict.
    assert _word(**dict(given, STOP_WAIT="ok")) != "NOT FINISHED"


def test_the_stop_steps_wait_for_an_idle_group_is_about_five_minutes():
    """What NOT FINISHED's "(about 5 minutes)" is: 60 reads, 5 s apart."""
    code = _step(ROLLBACK, "Stop any deployment already in flight")["run"]
    polls = re.search(r"^\s*GROUP_IDLE_POLLS=(\d+)$", code, re.M)
    seconds = re.search(r"^\s*GROUP_IDLE_POLL_SECONDS=(\d+)$", code, re.M)
    assert polls and seconds, "the wait's two values moved"
    assert int(polls.group(1)) * int(seconds.group(1)) == 300
    assert 'for n in $(seq 1 "$GROUP_IDLE_POLLS")' in code
    assert 'sleep "$GROUP_IDLE_POLL_SECONDS"' in code


@pytest.mark.regression
@pytest.mark.parametrize(
    "read, active",
    [
        (NOT_YET, {"ACTIVE_RC": "0", "ACTIVE": ROLLBACK_ID}),
        (WRONG, {"ACTIVE_RC": "0", "ACTIVE": ROLLBACK_ID}),
        (SPLIT, NONE_ACTIVE),
    ],
    ids=["another-primary-while-active", "route-elsewhere-while-active", "split"],
)
def test_still_moving_is_not_yet_on_the_target_while_something_moves(read, active):
    given = dict(DEPLOYMENT_ID=ROLLBACK_ID, API_VERIFY="failure", **read, **active)
    assert _word(**given) == "STILL MOVING"
    if read is SPLIT:
        headline = _headline(**given)
        assert "no deployment is active, but the /api/* rule still splits" in headline
        assert "the listener is stuck and needs a person" in headline


@pytest.mark.regression
@pytest.mark.parametrize("read", [NOT_YET, WRONG], ids=["not-yet", "wrong"])
def test_not_rolled_back_is_not_on_the_target_with_nothing_active(read):
    given = dict(DEPLOYMENT_ID=ROLLBACK_ID, API_VERIFY="failure", **read, **NONE_ACTIVE)
    assert _word(**given) == "NOT ROLLED BACK"
    headline = _headline(**given)
    assert read["SERVING_SAID"] in headline
    assert "no deployment is active" in headline


@pytest.mark.regression
@pytest.mark.parametrize(
    "services, rules, arn, code, label",
    [
        (BEFORE, RULES_BLUE, NEW, 1, "NOT YET"),
        (AFTER, RULES_BLUE, NEW, 3, "WRONG"),
        (AFTER, RULES_GREEN, NEW, 0, "serving"),
    ],
    ids=["another-revision-primary", "primary-but-routed-elsewhere", "on-target"],
)
def test_the_read_the_headline_quotes_says_not_yet_or_wrong(
    aws, services, rules, arn, code, label
):
    """scripts/api_serving.py, the read the summary quotes: NOT YET while
    another revision is PRIMARY, WRONG when the target is PRIMARY and the
    /api/* rule forwards to the other group, exit 0 only on the target."""
    got, out, _, _ = aws(
        SERVING,
        [CLUSTER, SERVICE, arn],
        {"responses": tick("InProgress", services, rules)},
    )
    assert got == code, out
    first = out.strip().splitlines()[0]
    assert first.startswith(f"{label}: "), out


@pytest.mark.regression
@pytest.mark.parametrize(
    "given",
    [
        dict(DEPLOYMENT_ID=ROLLBACK_ID, API_VERIFY="failure", **UNKNOWN, **NONE_ACTIVE),
        dict(
            DEPLOYMENT_ID=ROLLBACK_ID, API_VERIFY="failure", **NOT_YET, ACTIVE_RC="254"
        ),
        {
            "DEPLOYMENT_ID": ROLLBACK_ID,
            "API_VERIFY": "success",
            "REFUSED": "somethingElse",
        },
        {"DEPLOYMENT_ID": ROLLBACK_ID, "API_VERIFY": "succeeded"},
    ],
    ids=["read-failed", "active-not-listed", "unknown-refusal", "unknown-outcome"],
)
def test_outcome_unknown_is_a_run_that_could_not_tell(given):
    assert _word(**given) == "OUTCOME UNKNOWN"


@pytest.mark.regression
def test_outcome_unknown_when_the_verdict_script_did_not_run(runner, tmp_path):
    """The summary's own guard: no verdict from the script, OUTCOME UNKNOWN."""
    scripts = runner.work / "scripts"
    scripts.unlink()
    scripts.mkdir()
    for path in SCRIPTS.iterdir():
        if path.name != "rollback_verdict.py":
            os.symlink(path, scripts / path.name)
    code, log, written, _ = _summary(
        runner, TARGET_ARN=TARGET, DEPLOYMENT_ID=ROLLBACK_ID, API_VERIFY="success"
    )
    assert code == 1, log
    assert written["slack"].startswith(
        "staging: OUTCOME UNKNOWN: the verdict script did not run"
    ), written


@pytest.mark.regression
@pytest.mark.parametrize(
    "given, why",
    [
        ({"ALREADY_SERVING": "true"}, "the API was already on"),
        (
            {"REFUSED": "codeDeployRollback", "REFUSED_ID": REVERT_ID},
            "CodeDeploy's own rollback",
        ),
        (
            {"REFUSED": "unreadable", "REFUSED_ID": BAD_ID},
            "could not be read",
        ),
    ],
    ids=["already-on-target", "codedeploy-rolling-back", "unclassified"],
)
def test_refused_says_which_of_the_three_reasons(given, why):
    assert _word(**given) == "REFUSED"
    headline = _headline(**given)
    assert why in headline
    assert "stopped nothing and created no deployment" in headline


@pytest.mark.regression
@pytest.mark.parametrize(
    "given, why",
    [
        ({"TARGET_ARN": ""}, "the target revision was not resolved"),
        ({}, "the API is as it was before the run"),
    ],
    ids=["target-not-resolved", "failed-before-anything-changed"],
)
def test_nothing_changed_is_a_run_that_stopped_nothing_and_created_nothing(given, why):
    assert _word(**given, API_VERIFY="") == "NOTHING CHANGED"
    assert why in _headline(**given)
    # Anything stopped, or created, is not "nothing changed".
    assert _word(**given, STOPPED=BAD_ID, STOP_WAIT="ok", **NOT_YET, **NONE_ACTIVE) != (
        "NOTHING CHANGED"
    )


# --- the headline's per-deployment sentence --------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize(
    "given, ids",
    [
        ({"DEPLOYMENT_ID": ROLLBACK_ID, "API_VERIFY": "success"}, [ROLLBACK_ID]),
        (
            dict(
                DEPLOYMENT_ID=ROLLBACK_ID,
                API_VERIFY="failure",
                **NOT_YET,
                ACTIVE_RC="0",
                ACTIVE=f"{ROLLBACK_ID} {REVERT_ID}",
            ),
            [ROLLBACK_ID, REVERT_ID],
        ),
    ],
    ids=["own-deployment", "listed-active"],
)
def test_a_headline_naming_a_possibly_active_deployment_ends_with_do_not_dispatch(
    given, ids
):
    assert _headline(**given).endswith("".join(" " + _per_id(i) for i in ids))


# --- what the summary reads when the run's own steps did not finish -------------------


@pytest.mark.regression
def test_an_unfinished_run_reads_what_is_serving_and_lists_the_active_deployments_once(
    runner,
):
    """The verify step did not succeed: the summary reads what the API is
    serving with scripts/api_serving.py (read-only), lists the group's active
    deployments once, and the headline quotes the read."""
    from backend.tests.unit.infrastructure.test_dashboard_deploy_wiring import rule

    runner.scenario(
        [
            # The target is NEW; the old revision is still PRIMARY: NOT YET.
            *api_rules(BEFORE, RULES_BLUE),
            rule("deploy list-deployments", answers=[""]),
        ]
    )
    code, log, written, summary = runner.run(
        ROLLBACK,
        _step(ROLLBACK, "Run summary"),
        {},
        {"job.status": "failure"},
        TARGET_ARN=NEW,
        STOPPED=BAD_ID,
        STOP_WAIT="ok",
        DEPLOYMENT_ID=ROLLBACK_ID,
        API_VERIFY="failure",
        SUMMARY_SERVING_POLLS="2",
    )
    assert code == 1, log
    calls = runner.calls()
    ops = [" ".join(c[:2]) for c in calls]
    assert ops.count("deploy list-deployments") == 1, ops
    assert "ecs describe-services" in ops
    # Read-only: nothing but describe and list.
    assert all(c[1].startswith(("describe-", "list-")) for c in calls), calls
    (said,) = {
        line.split(": exit 1: ", 1)[1]
        for line in log.splitlines()
        if line.startswith("what the API is serving, read ")
    }
    assert said.startswith("NOT YET: "), log
    assert written["slack"].startswith("staging: NOT ROLLED BACK: "), written
    assert f"(scripts/api_serving.py exit 1: {said})" in written["slack"], written
    # A verified run reads nothing.
    code, log, written, _ = _summary(
        runner, TARGET_ARN=TARGET, DEPLOYMENT_ID=ROLLBACK_ID, API_VERIFY="success"
    )
    assert code == 0, log
    assert runner.calls() == [], runner.calls()
