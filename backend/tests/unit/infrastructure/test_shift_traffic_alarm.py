"""The forward deploy tells an alarm rollback apart from a failure (#148 PR-2).

Once the deployment group has alarms (infrastructure/tests/
test_codedeploy_alarms.py), CodeDeploy stops a deployment whose alarm goes
into ALARM and, with auto-rollback, moves the API back by itself. Before this,
`scripts/shift_traffic.py` reported that as a generic `failed` -- or, when the
rule was put back a moment before the status changed, as `wrong-route`, whose
copy sends the operator to Rollback: a second rollback of a release CodeDeploy
is already rolling back.

The alarm stops are captured, not invented. `fixtures/alarm_stops/` holds what
`aws deploy get-deployment` printed for two real ones on staging, with the
account replaced by 123456789012 inside the ARNs and nothing else changed:

* d-F797PV8AL (2026-10-03, a 10%-for-5-minutes canary): stopped about four
  minutes after it was created, before the hour CodeDeploy keeps the old task
  set, with both API alarms in ALARM; CodeDeploy's rollback was d-ND25699AL.
* d-GD9HNI2FL (2026-10-10, the #295 rehearsal): stopped in the hour CodeDeploy
  keeps the old task set, by the blue alarm, forced into ALARM; CodeDeploy's
  rollback was d-FRS33Q3FL.

Both are `Stopped` with `errorInformation.code` `ALARM_ACTIVE`, and both name
their rollback in `rollbackInfo`. The tests take the deployment's status,
errorInformation and rollbackInfo from a capture; the services and the rule
around it are test_forward_deploy_completes' ticks.

Still not observed, so still made up where a test needs it: an alarm stop
whose status is `Failed` (the test sets it), one without `rollbackInfo` (the
test removes it), messages other than the two captured (the test sets them),
and the order in which CodeDeploy puts back the rule and the PRIMARY task set
relative to the status during a traffic shift. In the 2026-10-10 rehearsal (a
stop in that hour, after the shift), read every 10 seconds, the first read
that said `Stopped` already showed the old revision PRIMARY; the rule was not
read.

The fake `aws` and the ticks are test_forward_deploy_completes'.
"""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest

from .test_forward_deploy_completes import (
    AFTER,
    BEFORE,
    CLUSTER,
    DEPLOYMENT,
    NEW,
    RULES_BLUE,
    SERVICE,
    SHIFT,
    _continues,
    _ops,
    _split,
    aws,
    tick,
)

BLUE_ALARM = "experimentation-api-5xx-blue-staging"
GREEN_ALARM = "experimentation-api-5xx-green-staging"

STOPS = Path(__file__).resolve().parent / "fixtures" / "alarm_stops"
#: Stopped by the blue alarm alone, in the hour CodeDeploy keeps the old task set.
BLUE_STOP = json.loads((STOPS / "d-GD9HNI2FL.json").read_text())
#: Stopped before that hour, with both alarms in ALARM.
CANARY_STOP = json.loads((STOPS / "d-F797PV8AL.json").read_text())
#: The rollback each capture's rollbackInfo names, and d-GD9HNI2FL's message,
#: written out rather than read back, so a changed fixture fails a test.
ROLLBACK_ID = "d-FRS33Q3FL"
CANARY_ROLLBACK_ID = "d-ND25699AL"
MESSAGE = (
    "One or more alarms have been activated according to the Amazon CloudWatch "
    "metrics you selected, and the affected deployments have been stopped. "
    f"Activated alarms: <{BLUE_ALARM}>"
)


def alarm_tick(
    status: str | None = None,
    *,
    services: dict = BEFORE,
    rules: dict = RULES_BLUE,
    message: str | None = None,
    rollback: bool = True,
    captured: dict = BLUE_STOP,
) -> dict:
    """A tick whose get-deployment is a captured alarm stop.

    `status`, `message` and `rollback=False` change the capture, for what has
    not been observed (see above); by default it is the capture as printed.
    It keeps its own deploymentId: the script reports the id it was given.
    """
    answer = tick("Stopped", services, rules)
    stop = copy.deepcopy(captured)
    info = stop["deploymentInfo"]
    if status is not None:
        info["status"] = status
    if message is not None:
        info["errorInformation"]["message"] = message
    if not rollback:
        del info["rollbackInfo"]
    answer["deploy get-deployment"] = stop
    return answer


def _shift(aws, ticks: list[dict], *extra: str):
    return aws(
        SHIFT,
        [
            "--deployment-id",
            DEPLOYMENT,
            "--cluster",
            CLUSTER,
            "--service",
            SERVICE,
            "--task-definition",
            NEW,
            "--deadline-seconds",
            "3600",
            "--interval-seconds",
            "0",
            "--alarm",
            BLUE_ALARM,
            "--alarm",
            GREEN_ALARM,
            *extra,
        ],
        {"ticks": ticks},
    )


def _polls(calls) -> int:
    return _ops(calls).count("deploy get-deployment")


@pytest.mark.regression
@pytest.mark.parametrize("status", [None, "Failed"], ids=["Stopped", "Failed"])
def test_an_alarm_stop_after_approval_names_the_alarm_and_the_rollback(aws, status):
    """P9a/P9b, UX W1/C1: `alarm`, the alarm's name, the rollback deployment,
    and no advice to use Rollback. The capture is Stopped; Failed is set."""
    code, out, calls, outputs = _shift(
        aws,
        [
            tick("Ready"),
            tick("InProgress", BEFORE, _split(90, 10)),
            alarm_tick(status),
        ],
    )
    assert code == 1, out
    assert outputs == {"approved": "true", "result": "alarm", "alarms": BLUE_ALARM}
    assert "::error title=Rolled back by an alarm::" in out
    assert f"CodeDeploy stopped deployment {DEPLOYMENT} ({status or 'Stopped'})" in out
    assert f"because {BLUE_ALARM} went into ALARM" in out
    assert f"(rollback deployment {ROLLBACK_ID})" in out
    # CodeDeploy's own words end the line, whole: this message is 200 characters.
    (error,) = [line for line in out.splitlines() if line.startswith("::error")]
    assert error.endswith(f" CodeDeploy said: {MESSAGE}"), error
    assert "This run stopped nothing" in out
    assert "Do not dispatch Rollback for this" in out
    assert "docs/deployment/rollback-runbook.md#an-alarm-rolled-the-api-back" in out
    assert "rollback line" not in out and "Roll back with" not in out
    assert "override" not in out.lower()
    assert not [c for c in calls if "stop-deployment" in c]


@pytest.mark.regression
def test_the_captured_canary_stop_is_an_alarm_with_its_rollback(aws):
    """d-F797PV8AL, stopped with both alarms in ALARM: `alarm`, CodeDeploy's
    rollback by its id, and no advice to use Rollback."""
    code, out, calls, outputs = _shift(
        aws,
        [
            tick("Ready"),
            tick("InProgress", BEFORE, _split(90, 10)),
            alarm_tick(captured=CANARY_STOP),
        ],
    )
    assert code == 1, out
    assert outputs["result"] == "alarm", out
    assert BLUE_ALARM in outputs["alarms"]
    assert f"CodeDeploy stopped deployment {DEPLOYMENT} (Stopped)" in out
    assert f"(rollback deployment {CANARY_ROLLBACK_ID})" in out
    assert "Do not dispatch Rollback for this" in out
    assert not [c for c in calls if "stop-deployment" in c]


@pytest.mark.xfail(
    strict=True,
    reason="the copy looks for the alarm names in the first 200 characters of "
    "CodeDeploy's message (MESSAGE_LIMIT), and this message names the green "
    "alarm after them",
)
def test_the_captured_canary_stop_names_both_alarms(aws):
    _, _, _, outputs = _shift(aws, [tick("Ready"), alarm_tick(captured=CANARY_STOP)])
    assert outputs["alarms"] == f"{BLUE_ALARM}, {GREEN_ALARM}"


@pytest.mark.regression
def test_a_route_reverted_by_an_alarm_rollback_is_not_a_wrong_route(aws):
    """P9c, PE condition 5: the rule is back on blue while the status still says
    InProgress for K-1 more reads, then Stopped for an alarm. K is 6; a grace of
    one read (the tamper K=1) ends this as wrong-route."""
    reverted = [tick("InProgress", AFTER, RULES_BLUE) for _ in range(5)]
    code, out, calls, outputs = _shift(
        aws,
        [
            tick("Ready"),
            tick("InProgress", AFTER, _split(10, 90)),
            *reverted,
            alarm_tick(),
        ],
    )
    assert code == 1, out
    assert outputs["result"] == "alarm", out
    assert "wrong-route" not in outputs.values()
    assert "Rolled back by an alarm" in out
    assert _polls(calls) == 8


@pytest.mark.regression
def test_the_wrong_route_grace_is_bounded(aws):
    """The grace ends: a rule that stays on the other group is wrong-route after
    K consecutive reads, and that copy says to check for an alarm first."""
    code, out, calls, outputs = _shift(
        aws, [tick("Ready"), tick("InProgress", AFTER, RULES_BLUE)]
    )
    assert code == 1, out
    assert outputs == {"approved": "true", "result": "wrong-route"}
    assert _polls(calls) == 1 + 6
    assert len(_continues(calls)) == 1
    assert (
        f"First check aws deploy get-deployment --deployment-id {DEPLOYMENT}: if it "
        "is Stopped with ALARM_ACTIVE, CodeDeploy is already rolling the API back, "
        "so do not dispatch Rollback" in out
    )


def test_the_grace_is_six_reads_and_a_parameter():
    spec = importlib.util.spec_from_file_location("shift_traffic", SHIFT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.WRONG_ROUTE_GRACE_POLLS == 6


def test_a_grace_of_one_read_is_the_old_behaviour(aws):
    """The same scenario as the P9c test, with the grace set to 1 on the command
    line: it is wrong-route. This is the tamper, run as a test."""
    reverted = [tick("InProgress", AFTER, RULES_BLUE) for _ in range(5)]
    code, out, _, outputs = _shift(
        aws,
        [
            tick("Ready"),
            tick("InProgress", AFTER, _split(10, 90)),
            *reverted,
            alarm_tick(),
        ],
        "--wrong-route-grace-polls",
        "1",
    )
    assert code == 1
    assert outputs["result"] == "wrong-route", out


@pytest.mark.regression
def test_no_rollback_is_claimed_without_rollback_info(aws):
    """P9d: CodeDeploy reported no rollback, so the copy does not claim one."""
    code, out, _, outputs = _shift(
        aws,
        [tick("Ready"), alarm_tick(rollback=False)],
    )
    assert code == 1
    assert outputs["result"] == "alarm"
    assert "no rollbackInfo" in out
    assert "moving the API back" not in out
    assert "auto-rollback puts" not in out
    shown = NEW.rsplit("/", 1)[-1]
    assert f"python3 scripts/api_serving.py {CLUSTER} {SERVICE} {shown}" in out


@pytest.mark.regression
def test_an_alarm_before_the_approval_says_nothing_shifted(aws):
    """P9e, UX C2: never approved, so nothing shifted and nothing rolls back."""
    code, out, calls, outputs = _shift(
        aws, [tick("InProgress"), alarm_tick(rollback=False)]
    )
    assert code == 1
    assert outputs == {"result": "alarm", "alarms": BLUE_ALARM}
    assert not _continues(calls)
    assert "before any traffic shifted" in out
    assert "the previous revision keeps serving" in out
    assert (
        "docs/deployment/rollback-runbook.md#fix-forward-while-an-alarm-is-firing"
        in out
    )
    assert "override" not in out.lower()


@pytest.mark.regression
@pytest.mark.parametrize(
    "message, alarms, expected",
    [
        (
            "Activated alarms: <something CodeDeploy did not name>",
            ["--alarm", BLUE_ALARM, "--alarm", GREEN_ALARM],
            f"one of the deployment group's alarms ({BLUE_ALARM}, {GREEN_ALARM})",
        ),
        ("", ["--alarm", BLUE_ALARM, "--alarm", GREEN_ALARM], None),
        ("", [], "one of the deployment group's alarms"),
        (f"<{BLUE_ALARM}> <{GREEN_ALARM}>", [], "one of the deployment group's alarms"),
        (
            f"<{BLUE_ALARM}> <{GREEN_ALARM}>",
            ["--alarm", BLUE_ALARM, "--alarm", GREEN_ALARM],
            f"{BLUE_ALARM}, {GREEN_ALARM}",
        ),
    ],
    ids=["unnamed", "empty-message", "nothing-known", "names-not-passed", "both"],
)
def test_the_alarm_is_never_an_empty_name(aws, message, alarms, expected):
    """UX C3: the copy never prints an empty name; it falls back."""
    if expected is None:
        expected = f"one of the deployment group's alarms ({BLUE_ALARM}, {GREEN_ALARM})"
    code, out, _, outputs = aws(
        SHIFT,
        [
            "--deployment-id",
            DEPLOYMENT,
            "--cluster",
            CLUSTER,
            "--service",
            SERVICE,
            "--task-definition",
            NEW,
            "--deadline-seconds",
            "3600",
            "--interval-seconds",
            "0",
            *alarms,
        ],
        {"ticks": [tick("Ready"), alarm_tick(message=message)]},
    )
    assert code == 1
    assert outputs["alarms"] == expected
    assert f"because {expected} went into ALARM" in out
    assert "because  " not in out


@pytest.mark.regression
def test_a_stop_for_another_reason_is_still_failed(aws):
    """Only ALARM_ACTIVE is an alarm."""
    stopped = tick("Stopped")
    stopped["deploy get-deployment"]["deploymentInfo"]["errorInformation"] = {
        "code": "HEALTH_CONSTRAINTS",
        "message": "tasks failed their health checks",
    }
    code, out, _, outputs = _shift(aws, [tick("Ready"), stopped])
    assert code == 1
    assert outputs == {"approved": "true", "result": "failed"}


def test_a_long_or_multiline_message_is_one_bounded_line(aws):
    long = "line one\nline two " + "x" * 1000
    code, out, _, outputs = _shift(aws, [tick("Ready"), alarm_tick(message=long)])
    (error,) = [line for line in out.splitlines() if line.startswith("::error")]
    assert "line one line two" in error
    assert "x" * 300 not in error
