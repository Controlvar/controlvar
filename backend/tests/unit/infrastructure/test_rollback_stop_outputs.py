"""The rollback's stop step records what it stopped and how its wait ended (#759).

Two outputs of rollback.yml's "Stop any deployment already in flight":

* ``stopped``: every id the step has asked CodeDeploy to stop, space-separated,
  in listing order, written as a whole BEFORE each ``stop-deployment``. A stop
  call that fails ends the step under ``set -e`` with its id already written.
* ``stop_wait``: ``ok``, ``timeout`` or ``unreadable``, written exactly once,
  inside the end state it names. Absent, with ``stopped`` set, when a stop
  call failed.

Neither is written when the step never reached a stop call (nothing in
flight, refused, the first listing failed).

The step runs under ``bash -e``, what GitHub's runner uses (``shell:
/usr/bin/bash -e {0}`` in both captured runs' logs), through
test_dashboard_deploy_wiring's ``Runner``: a fake ``aws`` first on PATH, no
credentials, a no-op ``sleep``. The fake copies GITHUB_OUTPUT at each stop
call (``Runner.snapshots()``), which is the only way to tell a write before
the call from a write after it, and ``Runner.raw_outputs`` keeps the file as
written, which is the only way to see a key written twice.

The captured runs are fixtures under ``fixtures/rollback_runs/``; each says
which of its answers are verbatim from the log and which are reconstructed
from the step's own messages. 37217043185 and 37176250648 each stopped a
deployment, and predate these outputs. 38098724258, from the #295 rehearsal,
has them, and found nothing in flight, so it wrote neither.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from backend.tests.unit.infrastructure.test_dashboard_deploy_wiring import (
    ROLLBACK,
    Runner,
    _step,
    rule,
)
from backend.tests.unit.infrastructure.test_rollback_false_success import (
    BAD_ID,
    CD_REVERT_ID,
    TARGET,
    _ops,
    _primary,
    runner,
)

pytestmark = pytest.mark.skipif(
    not ROLLBACK.is_file(), reason="this tree has no .github/workflows"
)

STEP = "Stop any deployment already in flight"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
RUNS = FIXTURES / "rollback_runs"

ID_A = "d-AAAAAAAA1"
ID_B = "d-BBBBBBBB2"
STOP_WAIT_VALUES = {"ok", "timeout", "unreadable"}
STATUS_THROTTLE = {
    "error": "An error occurred (ThrottlingException) when calling the "
    "GetDeployment operation (reached max retries: 2): Rate exceeded"
}
LIST_THROTTLE = {
    "error": "An error occurred (ThrottlingException) when calling the "
    "ListDeployments operation (reached max retries: 2): Rate exceeded"
}
ALREADY_COMPLETED = {
    "error": "An error occurred (DeploymentAlreadyCompletedException) when "
    f"calling the StopDeployment operation: Deployment {ID_B} has already completed"
}


@pytest.fixture
def stop_runner(runner):
    """The rollback harness, with the runner's own shell flags."""
    runner.shell_flags = ("-e",)
    return runner


def _run_stop(
    runner: Runner, rules: list, **env: str
) -> tuple[int, str, dict[str, str], str]:
    """(exit, log, outputs, GITHUB_OUTPUT as written) for the stop step alone."""
    runner.scenario(rules)
    code, log, written, _ = runner.run(
        ROLLBACK, _step(ROLLBACK, STEP), {"target": {"arn": TARGET}}, **env
    )
    return code, log, written, runner.raw_outputs["stop"]


def _seen(runner: Runner) -> list[str | None]:
    """``stopped`` as the file read at each stop call (the last value written)."""
    seen: list[str | None] = []
    for text in runner.snapshots():
        values = [
            line.split("=", 1)[1]
            for line in text.splitlines()
            if line.startswith("stopped=")
        ]
        seen.append(values[-1] if values else None)
    return seen


def _lines(raw: str, key: str) -> list[str]:
    return [line for line in raw.splitlines() if line.startswith(f"{key}=")]


def _closed(written: dict[str, str]) -> None:
    """Gate 9: both outputs come from closed sets, never AWS free text."""
    assert re.fullmatch(r"d-[A-Z0-9]+( d-[A-Z0-9]+)*", written["stopped"]), written
    if "stop_wait" in written:
        assert written["stop_wait"] in STOP_WAIT_VALUES, written


def _two_in_flight(second_stop: object = "") -> list:
    return [
        rule("deploy list-deployments", answers=[f"{ID_A}\t{ID_B}", "", ""]),
        rule("deploy get-deployment", "creator", answers=["user\tNone"]),
        rule("deploy stop-deployment", ID_B, answers=[second_stop]),
        rule("deploy stop-deployment", ID_A, answers=[""]),
        rule("deploy get-deployment", "deploymentInfo.status", answers=["Stopped"]),
    ]


# --- stopped: accumulated, and written before each call -----------------------------


@pytest.mark.regression
def test_two_ids_are_both_recorded_each_written_before_its_stop_call(stop_runner):
    """Regression 1, gates 2a and 2b. On main the step wrote no `stopped`.
    Planted defects: the write moved below the call (`[None, 'd-A']` at the
    calls, the same final value), and `stopped=$id` (the final value `d-B`)."""
    code, log, written, raw = _run_stop(stop_runner, _two_in_flight())
    assert code == 0, log
    assert _seen(stop_runner) == [ID_A, f"{ID_A} {ID_B}"]
    ops = _ops(stop_runner)
    assert ops.count("deploy stop-deployment") == 2, ops
    # The first listing and the two reads that confirm the group idle.
    assert ops.count("deploy list-deployments") == 1 + 2, ops
    assert written == {"stopped": f"{ID_A} {ID_B}", "stop_wait": "ok"}
    assert len(_lines(raw, "stop_wait")) == 1, raw
    _closed(written)


@pytest.mark.regression
def test_a_failed_second_stop_leaves_both_ids_and_no_stop_wait(stop_runner):
    """Regression 2 (its output half), gate 2c. The second call answers an
    error, set -e ends the step with the CLI's 254, and both ids are already
    written: the first was stopped, the second may have been. No wait ran,
    so there is no `stop_wait`. Planted defect: the write moved below the
    call (`stopped` is then `d-A`)."""
    code, log, written, raw = _run_stop(
        stop_runner, _two_in_flight(second_stop=ALREADY_COMPLETED)
    )
    assert code == 254, log
    assert written == {"stopped": f"{ID_A} {ID_B}"}, written
    assert _lines(raw, "stop_wait") == [], raw
    assert "DeploymentAlreadyCompletedException" in log
    _closed(written)


def test_a_stopped_inherited_from_the_environment_adds_nothing(stop_runner):
    """`stopped` starts empty. Planted defect: no `stopped=""` before the
    loop, and `d-INHERITED d-A` is written."""
    rules = [
        rule("deploy list-deployments", answers=[BAD_ID, "", ""]),
        rule("deploy get-deployment", "creator", answers=["user\tNone"]),
        rule("deploy stop-deployment", answers=[""]),
        rule("deploy get-deployment", "deploymentInfo.status", answers=["Stopped"]),
    ]
    code, log, written, _ = _run_stop(stop_runner, rules, stopped="d-INHERITED")
    assert code == 0, log
    assert written["stopped"] == BAD_ID
    assert _seen(stop_runner) == [BAD_ID]


# --- stop_wait: once, in each end state ------------------------------------------------


def _one_stopped(listing: list, statuses: list) -> list:
    return [
        rule("deploy list-deployments", answers=[BAD_ID, *listing]),
        rule("deploy get-deployment", BAD_ID, "creator", answers=["user\tNone"]),
        rule("deploy stop-deployment", BAD_ID, answers=[""]),
        rule(
            "deploy get-deployment",
            BAD_ID,
            "deploymentInfo.status",
            answers=statuses,
        ),
    ]


#: The six ways the waits end: ok, and the five that fail the step (four
#: `exit 1`s, the last shared by "still busy" and "not confirmed").
END_STATES = {
    "ok": (_one_stopped(["", ""], ["Stopped"]), 0, "ok", None),
    "stopped-deployment-unreadable": (
        _one_stopped([""], [STATUS_THROTTLE]),
        1,
        "unreadable",
        "Rollback could not read AWS",
    ),
    "stopped-deployment-timeout": (
        _one_stopped([""], ["InProgress"]),
        1,
        "timeout",
        "Stopped deployment did not finish",
    ),
    "group-unreadable": (
        _one_stopped([LIST_THROTTLE], ["Stopped"]),
        1,
        "unreadable",
        "Rollback could not read AWS",
    ),
    "group-still-busy": (
        _one_stopped([CD_REVERT_ID], ["Stopped"]),
        1,
        "timeout",
        "Deployment group still busy",
    ),
    "group-not-confirmed": (
        _one_stopped(
            ([CD_REVERT_ID] + [LIST_THROTTLE] * 11) * 4 + [""] + [LIST_THROTTLE] * 11,
            ["Stopped"],
        ),
        1,
        "unreadable",
        "Rollback could not confirm the group idle",
    ),
}


@pytest.mark.parametrize("state", list(END_STATES))
def test_stop_wait_is_written_exactly_once_in_each_end_state(stop_runner, state):
    """C6 and gate 9, on the file as written: the dict `run()` returns keeps
    only the last value of a key, so a second write would pass it. Planted
    defect: `stop_wait=ok` written after the per-id wait as well."""
    rules, exit_code, value, title = END_STATES[state]
    code, log, written, raw = _run_stop(stop_runner, rules)
    assert code == exit_code, log
    if title:
        assert f"::error title={title}::" in log, log
    assert _lines(raw, "stop_wait") == [f"stop_wait={value}"], raw
    assert _lines(raw, "stopped") == [f"stopped={BAD_ID}"], raw
    assert written == {"stopped": BAD_ID, "stop_wait": value}
    _closed(written)


def test_every_stop_wait_the_step_writes_is_in_the_closed_set():
    """The literal values in the step, read from its text: three words, and
    six writes (ok and the five failing end states)."""
    code = _step(ROLLBACK, STEP)["run"]
    writes = re.findall(r'"stop_wait=([^"]*)" >> "\$GITHUB_OUTPUT"', code)
    assert sorted(writes) == sorted(
        ["ok", "timeout", "timeout", "unreadable", "unreadable", "unreadable"]
    ), writes
    assert not re.findall(r"stop_wait=(?!ok\"|timeout\"|unreadable\")", code)


@pytest.mark.parametrize(
    "rules, exit_code",
    [
        (
            [
                rule("deploy list-deployments", answers=[BAD_ID]),
                rule(
                    "deploy get-deployment",
                    "creator",
                    answers=["codeDeployRollback\td-EARLIER"],
                ),
                *_primary(TARGET),
            ],
            1,
        ),
        (
            [
                rule("deploy list-deployments", answers=[BAD_ID]),
                rule("deploy get-deployment", "creator", answers=["None\tNone"]),
            ],
            1,
        ),
        ([rule("deploy list-deployments", answers=[LIST_THROTTLE])], 254),
    ],
    ids=["refused-codedeploy-rollback", "refused-unreadable", "first-list-failed"],
)
def test_a_step_that_reached_no_stop_call_writes_neither(stop_runner, rules, exit_code):
    code, log, written, raw = _run_stop(stop_runner, rules)
    assert code == exit_code, log
    assert "deploy stop-deployment" not in _ops(stop_runner)
    assert "stopped" not in written and "stop_wait" not in written, written
    assert not _lines(raw, "stopped") and not _lines(raw, "stop_wait"), raw


# --- the captured runs -------------------------------------------------------------------


def _run_fixture(name: str) -> dict:
    return json.loads((RUNS / name).read_text())


@pytest.mark.regression
@pytest.mark.parametrize("name", ["run-37217043185.json", "run-37176250648.json"])
def test_a_captured_runs_stop_step_replays_line_for_line(stop_runner, name):
    """The real answers (marked verbatim or reconstructed in the fixture) give
    the real printed lines, and the outputs that run would have written:
    `stopped=d-LPIWVZUAL` and `stop_wait=ok` for 37217043185."""
    run = _run_fixture(name)
    stop_runner.scenario(
        [rule(*a["match"], answers=a["answers"]) for a in run["answers"]]
    )
    code, log, written, _ = stop_runner.run(
        ROLLBACK,
        _step(ROLLBACK, STEP),
        {"target": {"arn": run["env"]["TARGET_ARN"]["value"]}},
    )
    raw = stop_runner.raw_outputs["stop"]
    assert code == 0, log
    assert log.splitlines() == run["printed"]
    assert written == run["outputs"]
    assert _lines(raw, "stop_wait") == ["stop_wait=ok"], raw
    assert _seen(stop_runner) == [run["outputs"]["stopped"]]
    _closed(written)


@pytest.mark.regression
def test_the_captured_run_with_nothing_in_flight_writes_neither(stop_runner):
    """Run 38098724258 (the #295 rehearsal): nothing was in flight and the API
    was on another revision, so the step printed the real lines, wrote no
    output and went on to roll back. Its Run summary's env header shows
    STOPPED and STOP_WAIT empty: the outputs the run wrote, not expected ones."""
    run = _run_fixture("run-38098724258.json")
    stop_runner.scenario(
        [rule(*a["match"], answers=a["answers"]) for a in run["answers"]]
    )
    code, log, written, _ = stop_runner.run(
        ROLLBACK,
        _step(ROLLBACK, STEP),
        {"target": {"arn": run["env"]["TARGET_ARN"]["value"]}},
    )
    raw = stop_runner.raw_outputs["stop"]
    assert code == 0, log
    # The run's log has stdout and stderr in time order; this runner gives
    # stdout, then stderr, where scripts/api_serving.py prints NOT YET.
    nothing, not_yet, rolling_back = run["printed"]
    assert log.splitlines() == [nothing, rolling_back, not_yet]
    assert written == run["outputs"] == {}
    assert raw == "", raw
    assert "deploy stop-deployment" not in _ops(stop_runner)
    summary = {k: v["value"] for k, v in run["summary"]["env"].items()}
    assert (summary["STOPPED"], summary["STOP_WAIT"]) == ("", "")
    # Written out, so a regenerated fixture cannot move the replay.
    assert run["printed"] == [
        "no deployment in flight",
        "NOT YET: the PRIMARY task set runs experimentation-backend-staging:13, "
        "not experimentation-backend-staging:12",
        "the API is not on experimentation-backend-staging:12: rolling back",
    ]


def test_the_green_runs_fixture_names_what_it_stopped():
    """Pinned literally, so a regenerated fixture cannot move the replay."""
    assert _run_fixture("run-37217043185.json")["outputs"] == {
        "stopped": "d-LPIWVZUAL",
        "stop_wait": "ok",
    }


def test_every_captured_answer_says_whether_it_is_verbatim():
    """C8: each answer is marked, and a stop call's answer is the verbatim one."""
    names = sorted(p.name for p in RUNS.iterdir())
    assert names == [
        "run-37176250648.json",
        "run-37217043185.json",
        "run-38098724258.json",
    ], names
    for name in names:
        run = _run_fixture(name)
        sources = [a["source"] for a in run["answers"]]
        assert all(s.startswith(("verbatim", "reconstructed")) for s in sources), (
            name,
            sources,
        )
        stops = [a for a in run["answers"] if a["match"][0] == "deploy stop-deployment"]
        # The rehearsal run stopped nothing; each older one stopped one.
        assert len(stops) == (0 if name == "run-38098724258.json" else 1), name
        for stop in stops:
            assert stop["source"].startswith("verbatim"), stop
        assert run["printed_source"].startswith("verbatim")


def test_no_captured_run_keeps_a_masked_value():
    """The logs mask the account as three asterisks; a fixture that keeps one
    gives api_serving.py a value it reads as "could not tell" (exit 2), so the
    scenario would silently test something else."""
    files = sorted(p for p in RUNS.rglob("*") if p.is_file())
    assert files, RUNS
    masked = [p.name for p in files if "***" in p.read_text()]
    assert not masked, masked


#: Placeholders, not accounts.
PLACEHOLDER_ACCOUNTS = {"123456789012", "000000000000", "111111111111"}
_TWELVE = re.compile(r"(?<![0-9])[0-9]{12}(?![0-9])")


def test_no_fixture_holds_a_twelve_digit_account_number():
    """Gate 6b: every run of exactly twelve digits under fixtures/ is a
    placeholder, whether in an ARN or bare."""
    files = sorted(p for p in FIXTURES.rglob("*") if p.is_file())
    found: dict[str, set[str]] = {}
    for path in files:
        for digits in _TWELVE.findall(path.read_text(errors="replace")):
            found.setdefault(digits, set()).add(str(path.relative_to(FIXTURES)))
    offenders = {
        d: sorted(p) for d, p in found.items() if d not in PLACEHOLDER_ACCOUNTS
    }
    assert not offenders, offenders
    # Not vacuous: the scan reads the captured runs and finds their placeholder.
    assert "rollback_runs/run-37217043185.json" in found.get("123456789012", set())
