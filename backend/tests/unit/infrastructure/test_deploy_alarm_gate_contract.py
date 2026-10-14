"""What the Deploy workflow does about the API's alarms and its migration
(#1142, D73 TR4).

The break-glass (two inputs, the run name, the preflight's refusals, the
override on one CodeDeploy call, what the summary and Slack say), the alarm
check Deploy runs twice (``scripts/refuse_alarm_active.py``), the refusal of an
API revision that would migrate on start
(``scripts/refuse_migrating_api_revision.py``), the order of the snapshot, the
migration and the deployment, and what the run reports when CodeDeploy stops
the deployment for an alarm (``scripts/shift_traffic.py`` and the steps after
it).

The scripts run in-process with a fake reader, or as deploy.yml runs them,
under the shared Runner (bash, a fake ``aws`` first on PATH, no AWS keys):
nothing reaches AWS.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from backend.tests.unit.infrastructure.test_dashboard_deploy_wiring import (
    API_FAMILY_REVISION,
    BEFORE_DASHBOARD,
    DEPLOY,
    SCRIPTS,
    _code,
    _if,
    _load,
    _step,
    _steps,
    rule,
    runner,
)
from backend.tests.unit.infrastructure.test_forward_deploy_completes import (
    AFTER,
    BEFORE,
    RULES_BLUE,
    _split,
    aws,
    tick,
)
from backend.tests.unit.infrastructure.test_shift_traffic_alarm import (
    BLUE_ALARM,
    _shift,
    alarm_tick,
)

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(
        not DEPLOY.is_file(), reason="this tree has no .github/workflows"
    ),
]

GREEN_ALARM = "experimentation-api-5xx-green-staging"
EXPECTED = ("experimentation-api-5xx-blue-staging", GREEN_ALARM)
OVERRIDE = "--override-alarm-configuration"


def _script(name: str):
    spec = importlib.util.spec_from_file_location(
        f"{name}_under_contract", SCRIPTS / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


refuse_alarm = _script("refuse_alarm_active")
refuse_migrating = _script("refuse_migrating_api_revision")
shift_traffic = _script("shift_traffic")


def _deploy_steps() -> list[dict]:
    return _load(DEPLOY)["jobs"]["deploy"]["steps"]


def _position(name: str) -> int:
    (index,) = [
        i for i, s in enumerate(_deploy_steps()) if name in (s.get("id"), s.get("name"))
    ]
    return index


def _outputs(**changes: dict) -> dict:
    outputs = {k: dict(v) for k, v in BEFORE_DASHBOARD.items()}
    outputs["migrate"] = {"__outcome__": "success"}
    for key, value in changes.items():
        outputs.setdefault(key.replace("_", "-"), {}).update(value)
    return outputs


# --- the break-glass -----------------------------------------------------------------------


@pytest.mark.regression
def test_the_break_glass_is_a_tick_box_and_a_reason_both_off_by_default():
    inputs = _load(DEPLOY)["on"]["workflow_dispatch"]["inputs"]
    tick_box, reason = inputs["override_alarms"], inputs["override_alarms_reason"]
    assert (tick_box["type"], tick_box["required"], tick_box["default"]) == (
        "boolean",
        False,
        False,
    )
    assert (reason["type"], reason["required"], reason["default"]) == (
        "string",
        False,
        "",
    )


_EXPR = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")


def _render_run_name(inputs: dict[str, object]) -> str:
    """deploy.yml's run-name, for the two expression forms it uses:
    ``inputs.NAME`` and ``inputs.NAME && 'TEXT' || ''``. Anything else fails."""

    def one(match: re.Match) -> str:
        expression = match.group(1)
        plain = re.fullmatch(r"inputs\.(\w+)", expression)
        if plain:
            return str(inputs[plain.group(1)])
        choice = re.fullmatch(r"inputs\.(\w+) && '([^']*)' \|\| ''", expression)
        assert choice, f"cannot render {expression!r}"
        return choice.group(2) if inputs[choice.group(1)] else ""

    return _EXPR.sub(one, _load(DEPLOY)["run-name"])


@pytest.mark.regression
def test_the_run_name_ends_in_alarms_overridden_only_when_ticked():
    base = {"version": "v1.2.3", "profile": "full", "environment": "prod"}
    assert _render_run_name({**base, "override_alarms": True}).endswith(
        "ALARMS OVERRIDDEN"
    )
    assert "ALARMS OVERRIDDEN" not in _render_run_name(
        {**base, "override_alarms": False}
    )
    # That run is the one the environment's reviewer approves.
    assert _load(DEPLOY)["jobs"]["deploy"]["environment"] == "${{ inputs.environment }}"


def _preflight() -> dict:
    (step,) = [
        s
        for s in _load(DEPLOY)["jobs"]["preflight"]["steps"]
        if s.get("name") == "Confirm the alarm override"
    ]
    return step


@pytest.mark.regression
@pytest.mark.parametrize(
    "ticked, reason, refused",
    [
        ("true", "", True),
        ("true", "  \t ", True),
        ("false", "a reason", True),
        ("true", "the fix for a bug in both releases", False),
        ("false", "", False),
    ],
    ids=["tick-no-reason", "tick-blank-reason", "reason-no-tick", "both", "neither"],
)
def test_deploy_refuses_the_tick_box_without_a_reason_and_a_reason_without_it(
    runner, ticked, reason, refused
):
    runner.scenario([])
    code, out, _, _ = runner.run(
        DEPLOY,
        _preflight(),
        {},
        {"override_alarms": ticked, "override_alarms_reason": reason},
        GITHUB_ACTOR="octocat",
    )
    assert (code != 0) is refused, out
    if refused:
        assert "Nothing was deployed" in out
    # The preflight job holds no AWS role.
    assert runner.calls() == []
    preflight = _load(DEPLOY)["jobs"]["preflight"]
    assert "id-token" not in preflight.get("permissions", {})


def _create(runner, ticked: str) -> list[str]:
    runner.scenario([rule("deploy create-deployment", answers=["d-NEW"])])
    code, out, written, _ = runner.run(
        DEPLOY,
        _step(DEPLOY, "codedeploy"),
        _outputs(api_td={"arn": API_FAMILY_REVISION}),
        {"override_alarms": ticked},
    )
    assert code == 0, out
    (call,) = [c for c in runner.calls() if c[:2] == ["deploy", "create-deployment"]]
    return call


@pytest.mark.regression
def test_with_the_break_glass_that_deployment_alone_has_no_alarm(runner):
    """Ticked, the one CreateDeployment call carries the alarms overridden
    (nothing watches it, nothing rolls it back); unticked, it does not. The
    deployment group is never changed, so the next deploy is watched again."""
    overridden = _create(runner, "true")
    value = json.loads(overridden[overridden.index(OVERRIDE) + 1])
    assert value == {"enabled": False, "ignorePollAlarmFailure": False, "alarms": []}
    plain = _create(runner, "false")
    assert OVERRIDE not in plain, plain
    workflow = "\n".join(_code(s["run"]) for s in _deploy_steps() if "run" in s)
    assert workflow.count(OVERRIDE) == 1
    assert "update-deployment-group" not in workflow
    for path in sorted(SCRIPTS.glob("*.py")) + sorted(SCRIPTS.glob("*.sh")):
        if path.name in ("iam_actions.py", "check_create_deployment_cli.py"):
            continue  # tooling: the IAM generator's map and the CLI check
        text = path.read_text(encoding="utf-8")
        assert "update-deployment-group" not in text, path.name
        assert OVERRIDE not in text, path.name


def _summary(runner, outputs: dict, inputs: dict | None = None):
    runner.scenario([])
    return runner.run(
        DEPLOY,
        _step(DEPLOY, "summary"),
        outputs,
        {"job.status": "success", **(inputs or {})},
    )


def _shifted() -> dict:
    return _outputs(
        api_serving={
            "__outcome__": "success",
            "result": "serving",
            "live_target_group": "green",
            "shifted_at": "2026-09-26T23:59:00Z",
            "bake_end": "2026-09-27 00:59",
        }
    )


@pytest.mark.regression
def test_the_summary_and_slack_say_the_deployment_went_unwatched_with_the_reason(
    runner,
):
    reason = "the fix for a bug in both releases"
    code, out, written, summary = _summary(
        runner,
        _shifted(),
        {"override_alarms": "true", "override_alarms_reason": reason},
    )
    assert code == 0, out
    flat = " ".join(summary.split())
    assert "No alarm watched this deployment" in flat and reason in flat
    assert "Nothing rolls it back by itself" in flat
    assert written["api_slack"].startswith("ALARMS OVERRIDDEN: no alarm watched")
    assert reason in written["api_slack"]
    notify = _step(DEPLOY, "Notify deployment result")["with"]["slack-message"]
    assert "steps.summary.outputs.api_slack" in notify
    assert "ALARMS OVERRIDDEN, unwatched" in notify
    # Without the break-glass, none of it.
    code, out, written, summary = _summary(runner, _shifted())
    assert "No alarm watched" not in summary
    assert not written["api_slack"].startswith("ALARMS OVERRIDDEN")


# --- the alarm check, before the build and before the migration ---------------------------


class FakeReads:
    """refuse_alarm_active.py's two reads, answered from a group and states."""

    def __init__(self, config: dict | None, states: dict[str, str]):
        self.config = config
        self.states = states
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> dict:
        self.calls.append(list(argv))
        if argv[:2] == ["deploy", "get-deployment-group"]:
            info: dict = {"deploymentConfigName": "x"}
            if self.config is not None:
                info["alarmConfiguration"] = self.config
            return {"deploymentGroupInfo": info}
        if argv[:2] == ["cloudwatch", "describe-alarms"]:
            return {
                "MetricAlarms": [
                    {
                        "AlarmName": name,
                        "StateValue": state,
                        "StateUpdatedTimestamp": "2026-10-11T10:00:00Z",
                        "StateReason": "Threshold Crossed",
                    }
                    for name, state in self.states.items()
                ]
            }
        raise AssertionError(f"not a read this check makes: {argv}")


def _watching(*names: str, enabled: bool = True) -> dict:
    return {"enabled": enabled, "alarms": [{"name": n} for n in names]}


def _check(reads: FakeReads, *extra: str) -> int:
    return refuse_alarm.main(
        [
            "--application",
            "experimentation-platform-staging",
            "--group",
            "experimentation-staging",
            "--environment",
            "staging",
            "--stage",
            "before-build",
            *[f"--expect={n}" for n in EXPECTED],
            *extra,
        ],
        aws=reads,
    )


@pytest.mark.regression
def test_an_alarm_in_alarm_refuses_naming_it_and_since_when(capsys):
    reads = FakeReads(_watching(*EXPECTED), {EXPECTED[0]: "OK", GREEN_ALARM: "ALARM"})
    assert _check(reads) == 1
    out = capsys.readouterr().out
    (error,) = [line for line in out.splitlines() if line.startswith("::error")]
    assert error.startswith("::error title=An alarm is already firing::")
    assert f"{GREEN_ALARM} is in ALARM" in error
    assert "since 2026-10-11T10:00:00Z" in error
    # Read-only.
    assert {tuple(c[:2]) for c in reads.calls} <= {
        ("deploy", "get-deployment-group"),
        ("cloudwatch", "describe-alarms"),
    }


@pytest.mark.regression
@pytest.mark.parametrize("state", ["OK", "INSUFFICIENT_DATA"])
def test_ok_and_insufficient_data_do_not_refuse(capsys, state):
    reads = FakeReads(_watching(*EXPECTED), dict.fromkeys(EXPECTED, state))
    assert _check(reads) == 0, capsys.readouterr().out


@pytest.mark.regression
@pytest.mark.parametrize(
    "overridden", [(), ("--alarms-overridden",)], ids=["plain", "break-glass"]
)
@pytest.mark.parametrize(
    "config",
    [None, _watching(*EXPECTED, enabled=False), _watching(), _watching(EXPECTED[0])],
    ids=["no-alarms", "disabled", "empty", "only-blue"],
)
def test_a_group_not_watching_both_alarms_refuses_whatever_else_is_set(
    capsys, config, overridden
):
    reads = FakeReads(config, dict.fromkeys(EXPECTED, "OK"))
    code = _check(reads, *overridden)
    out = capsys.readouterr().out
    assert code == 1, out
    assert "Deploy the Fargate stack first" in out


@pytest.mark.regression
def test_with_the_break_glass_an_alarm_in_alarm_is_a_warning(capsys):
    reads = FakeReads(_watching(*EXPECTED), {EXPECTED[0]: "OK", GREEN_ALARM: "ALARM"})
    assert _check(reads, "--alarms-overridden") == 0
    out = capsys.readouterr().out
    assert not [line for line in out.splitlines() if line.startswith("::error")], out
    assert "::warning title=An alarm is firing, and the alarms are overridden::" in out


@pytest.mark.regression
def test_the_check_runs_before_the_build_and_again_before_the_snapshot():
    steps = _deploy_steps()
    names = [s.get("name", "") for s in steps]
    checks = [
        i
        for i, s in enumerate(steps)
        if "scripts/refuse_alarm_active.py" in s.get("run", "")
    ]
    assert len(checks) == 2, checks
    first, second = checks
    assert "--stage before-build" in steps[first]["run"]
    assert "--stage before-migration" in steps[second]["run"]
    build = names.index("Build and push the image once, and resolve its digest")
    snapshot = _position("snapshot")
    assert first < build < second < snapshot < _position("migrate")
    # Nothing but reporting between the second check and the snapshot.
    assert second + 1 == snapshot
    for index in checks:
        step = steps[index]
        assert "if" not in step, step  # it always runs, and its failure stops the job
        assert step["env"]["OVERRIDE_ALARMS"] == "${{ inputs.override_alarms }}"
        assert "--alarms-overridden" in step["run"]


@pytest.mark.regression
def test_an_alarm_after_the_check_ends_the_run_migrated_not_deployed(runner):
    """The check passed, the migration ran, then an alarm stopped the
    deployment before its shift: the run ends with the warning."""
    outputs = {
        "migrate": {"__outcome__": "success"},
        "api-serving": {"__outcome__": "failure", "approved": "", "result": "alarm"},
    }
    (warning,) = [
        s
        for s in _deploy_steps()
        if s.get("name")
        == "If the migration was applied and the shift was never approved"
    ]
    assert _if(warning["if"], outputs, failed=True)
    runner.scenario([])
    code, out, _, _ = runner.run(DEPLOY, warning, outputs, {}, VERSION="v1.2.3")
    assert code == 0, out
    assert "::warning title=Migrated, not deployed::The migration was applied" in out


# --- the migration, and the revision that would migrate on start ---------------------------


@pytest.mark.regression
def test_the_snapshot_and_the_migration_come_before_the_deployment_and_nothing_undoes_them():
    order = [
        _position(x)
        for x in (
            "snapshot",
            "migrate-td",
            "migrate",
            "api-td",
            "api-td-check",
            "codedeploy",
        )
    ]
    assert order == sorted(order), order
    workflow = "\n".join(_code(s["run"]) for s in _deploy_steps() if "run" in s)
    assert "downgrade" not in workflow
    assert "restore-db-cluster" not in workflow
    assert "backend.app.db.bootstrap" in _step(DEPLOY, "migrate")["run"]
    # Each of those steps has no `if:`: one that fails stops the job there.
    for key in ("snapshot", "migrate", "api-td", "api-td-check", "codedeploy"):
        assert "if" not in _step(DEPLOY, key), key
    # And none of the deploy job's steps that change or check what serves
    # carries on past its own failure: only the Slack steps may.
    for step in _deploy_steps():
        if "continue-on-error" in step:
            assert (
                str(step.get("uses", "")).startswith("slackapi/")
                or step.get("name") == "Say Slack was not notified"
            ), step.get("name")


def _stored(environment: list[dict] | None, name: str = "backend") -> dict:
    container: dict = {"name": name, "image": "repo@sha256:" + "a" * 64}
    if environment is not None:
        container["environment"] = environment
    return {
        "taskDefinition": {
            "taskDefinitionArn": API_FAMILY_REVISION,
            "containerDefinitions": [container],
        }
    }


@pytest.mark.regression
@pytest.mark.parametrize(
    "environment, refused",
    [
        ([{"name": "RUN_MIGRATIONS", "value": "false"}], False),
        (None, True),
        ([], True),
        ([{"name": "RUN_MIGRATIONS", "value": "true"}], True),
        ([{"name": "RUN_MIGRATIONS", "value": "False"}], True),
        ([{"name": "RUN_MIGRATIONS", "value": "false "}], True),
        ([{"name": "RUN_MIGRATIONS", "value": ""}], True),
    ],
    ids=[
        "false",
        "no-environment",
        "missing",
        "true",
        "False",
        "trailing-space",
        "empty",
    ],
)
def test_an_api_revision_without_run_migrations_exactly_false_is_refused(
    capsys, environment, refused
):
    calls: list[list[str]] = []

    def reads(argv):
        calls.append(list(argv))
        return _stored(environment)

    code = refuse_migrating.main(
        [
            "--task-definition",
            API_FAMILY_REVISION,
            "--environment",
            "staging",
            "--fargate-stack",
            "experimentation-fargate-staging",
        ],
        aws=reads,
    )
    out = capsys.readouterr().out
    assert code == (1 if refused else 0), out
    if refused:
        assert "::error title=This API revision would run migrations on start::" in out
    assert [c[:2] for c in calls] == [["ecs", "describe-task-definition"]]


def test_the_refusal_comes_after_the_migration_and_before_the_deployment():
    step = _step(DEPLOY, "api-td-check")
    assert "scripts/refuse_migrating_api_revision.py" in step["run"]
    assert step["env"]["TASK_DEFINITION"] == "${{ steps.api-td.outputs.arn }}"
    assert _position("migrate") < _position("api-td") < _position("api-td-check")
    assert _position("api-td-check") + 1 == _position("codedeploy")


# --- what the run says when CodeDeploy stops the deployment for an alarm ---------------------


@pytest.mark.regression
def test_an_alarm_after_the_approval_is_rolled_back_by_an_alarm_naming_it(aws):
    code, out, calls, outputs = _shift(
        aws, [tick("Ready"), tick("InProgress", BEFORE, _split(90, 10)), alarm_tick()]
    )
    assert code == 1, out
    assert outputs["result"] == "alarm" and outputs["alarms"] == BLUE_ALARM
    assert "::error title=Rolled back by an alarm::" in out
    assert not [c for c in calls if c[:2] == ["deploy", "stop-deployment"]]


@pytest.mark.regression
def test_an_alarm_before_the_shift_is_stopped_by_an_alarm_and_nothing_shifted(aws):
    code, out, calls, outputs = _shift(aws, [tick("InProgress"), alarm_tick()])
    assert code == 1, out
    assert outputs == {"result": "alarm", "alarms": BLUE_ALARM}
    assert "::error title=Stopped by an alarm::" in out
    assert "Nothing shifted" in out
    assert not [c for c in calls if c[:2] == ["deploy", "continue-deployment"]]


@pytest.mark.regression
def test_a_route_on_the_other_group_is_read_again_for_about_a_minute_before_it_is_reported(
    aws,
):
    """After the approval, a /api/* rule on the other group with the new
    revision PRIMARY is read again (an alarm rollback puts the rule back
    first); if the deployment then reads Stopped with ALARM_ACTIVE, the result
    is the alarm, not a wrong route."""
    wrong = tick("InProgress", AFTER, RULES_BLUE)
    code, out, calls, outputs = _shift(
        aws, [tick("Ready"), wrong, wrong, wrong, alarm_tick(services=AFTER)]
    )
    assert code == 1, out
    assert outputs["result"] == "alarm", out
    # Read again a bounded number of times: six polls, ten seconds apart.
    assert shift_traffic.WRONG_ROUTE_GRACE_POLLS == 6
    deploy_env = _load(DEPLOY)["env"]
    assert deploy_env["CODEDEPLOY_POLL_SECONDS"] == "10"
    assert (
        '--interval-seconds "$CODEDEPLOY_POLL_SECONDS"'
        in _step(DEPLOY, "api-serving")["run"]
    )
    code, out, calls, outputs = _shift(aws, [tick("Ready"), *[wrong] * 7])
    assert outputs["result"] == "wrong-route", out
    assert (
        "if it is Stopped with ALARM_ACTIVE, CodeDeploy is already rolling the API back"
        in out
    )
    reads = [c for c in calls if c[:2] == ["deploy", "get-deployment"]]
    assert len(reads) == 1 + shift_traffic.WRONG_ROUTE_GRACE_POLLS, len(reads)


def _after_shift(runner, error_code: str | None) -> tuple[int, str, dict]:
    rules = []
    if error_code is not None:
        rules.append(rule("deploy get-deployment", answers=[error_code]))
    runner.scenario(rules)
    code, out, written, _ = runner.run(
        DEPLOY,
        _step(DEPLOY, "after-shift"),
        _outputs(),
        {},
        GUARD="0",
        GUARD_LINE="",
        RECHECK="1",
        RECHECK_LINE="NOT YET: another revision is PRIMARY",
        ROLLOUT="completed",
        VERSION="v1.2.3",
    )
    return code, out, written


@pytest.mark.regression
def test_an_api_check_failed_by_an_alarm_after_the_shift_says_codedeploy_moves_it_back(
    runner,
):
    code, out, written = _after_shift(runner, "ALARM_ACTIVE")
    assert code == 0, out
    assert written == {"alarm": "true"}
    assert "::error title=API rolled back by an alarm, dashboard not::" in out
    assert "its auto-rollback moves the API back" in out
    assert "There is nothing to roll back for the API" in out
    code, out, written = _after_shift(runner, "None")
    assert "alarm" not in written
    assert "API deployed, dashboard not confirmed" in out


@pytest.mark.regression
def test_the_dashboard_rolls_out_only_after_the_api_shifted_and_answered():
    """An alarm in the canary ends the shift step red, and every dashboard
    step after it is skipped by the default `if:`; the dashboard is unchanged."""
    keys = (
        "api-serving",
        "Smoke test through the public URL",
        "dashboard-td",
        "dashboard-rollout",
        "Smoke test the dashboard through the public URL",
    )
    order = [_position(k) for k in keys]
    assert order == sorted(order), order
    for key in keys[2:]:
        assert "if" not in _step(DEPLOY, key), key
    dashboard_writes = [
        i
        for i, s in enumerate(_deploy_steps())
        if "ecs_rolling_rollout.sh" in s.get("run", "")
        or "update-service" in s.get("run", "")
    ]
    assert dashboard_writes and min(dashboard_writes) > _position("api-serving")


@pytest.mark.regression
@pytest.mark.parametrize(
    "answer",
    ["404 text/plain", "200 text/plain", "200 application/json", "503 text/html"],
)
def test_a_dashboard_answering_anything_but_html_200_fails_the_dashboard_smoke(
    runner, answer
):
    step = _step(DEPLOY, "Smoke test the dashboard through the public URL")
    runner.scenario([])
    runner.curl(answer, "not the dashboard")
    code, out, _, _ = runner.run(
        DEPLOY, step, _outputs(), {}, SMOKE_ATTEMPTS="2", SMOKE_SLEEP_SECONDS="0"
    )
    assert code == 1, out
    assert "::error title=Dashboard smoke test failed::" in out
    assert "check the HTTPS listener of experimentation-fargate-staging" in out
    runner.curl("200 text/html; charset=utf-8", "<html>")
    code, out, _, _ = runner.run(
        DEPLOY, step, _outputs(), {}, SMOKE_ATTEMPTS="2", SMOKE_SLEEP_SECONDS="0"
    )
    assert code == 0, out


@pytest.mark.regression
def test_the_summary_says_until_when_the_alarms_watch_and_how_to_check(runner):
    code, out, written, summary = _summary(runner, _shifted())
    assert code == 0, out
    flat = " ".join(summary.split())
    assert "(until about 2026-09-27 00:59 UTC)" in flat
    assert (
        "A rollback by an alarm after this run ends is not announced in #deployments"
        in flat
    )
    assert "aws deploy get-deployment --deployment-id d-ABCDEF123" in flat
    # It names the revision the API now serves, as family:revision.
    revision = BEFORE_DASHBOARD["api-td"]["arn"].rsplit("/", 1)[-1]
    assert f"| New API revision | `{revision}` |" in summary
    # The end is the shift plus the group's termination wait.
    assert _load(DEPLOY)["env"]["BAKE_MINUTES"] == "60"
    assert '--bake-minutes "$BAKE_MINUTES"' in _step(DEPLOY, "api-serving")["run"]


@pytest.mark.regression
def test_the_pre_deploy_snapshot_is_named_for_the_environment_the_tag_and_the_time(
    runner,
):
    from backend.tests.unit.infrastructure.test_dashboard_deploy_wiring import (
        rule as aws_rule,
    )

    timeout = runner.bin / "timeout"
    timeout.write_text('#!/bin/sh\nshift\nexec "$@"\n')
    timeout.chmod(0o755)
    runner.scenario(
        [
            aws_rule("rds create-db-cluster-snapshot", answers=[""]),
            aws_rule("rds wait db-cluster-snapshot-available", answers=[""]),
        ]
    )
    step = dict(_step(DEPLOY, "snapshot"))
    step["env"] = {}
    code, out, written, _ = runner.run(
        DEPLOY, step, {}, {}, VERSION="v1.2.3", DB_CLUSTER="cluster-id"
    )
    assert code == 0, out
    assert re.fullmatch(r"pre-deploy-staging-v1-2-3-\d{8}-\d{6}", written["id"]), (
        written
    )
    (create,) = [
        c for c in runner.calls() if c[:2] == ["rds", "create-db-cluster-snapshot"]
    ]
    assert create[create.index("--db-cluster-snapshot-identifier") + 1] == written["id"]
    assert create[create.index("--db-cluster-identifier") + 1] == "cluster-id"
