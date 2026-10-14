"""What the Rollback workflow does, step by step, as an operator relies on it
(#1142, D73 TR4).

rollback.yml's own `run:` scripts, as written, under the shared Runner: bash
`-eo pipefail`, a fake `aws` first on PATH answering from a scenario, no AWS
keys, and a no-op `sleep`, so nothing reaches AWS and no test waits.
Each test is one thing the run does or refuses: the checks on the target
revision, the two refusals of the API half, the stop of a deployment in
flight, the deployment it creates, when it counts a rollback as done, the
result it posts, and what it does not do (a smoke request, a check of the
target's migrations setting).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from backend.tests.unit.infrastructure.test_dashboard_deploy_wiring import (
    DEPLOY,
    ROLLBACK,
    SCRIPTS,
    _code,
    _step,
    _steps,
    api_rules,
    rule,
)
from backend.tests.unit.infrastructure.test_forward_deploy_completes import (
    AFTER,
    BEFORE,
    NEW,
    RULES_BLUE,
    RULES_GREEN,
)
from backend.tests.unit.infrastructure.test_rollback_false_success import (
    BAD_ID,
    CD_ROLLBACK_ID,
    ROLLBACK_ID,
    TARGET,
    _create_and_approve,
    _in_flight,
    _nothing_in_flight,
    _primary,
    _run_from_stop,
    _shift,
    _verify,
    _verify_step,
    runner,
)

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(
        not ROLLBACK.is_file(), reason="this tree has no .github/workflows"
    ),
]


def _ops(runner) -> list[str]:
    return [" ".join(c[:2]) for c in runner.calls()]


def _runs() -> list[tuple[str, str]]:
    """(step name, its `run:` without comment lines) for every run step."""
    return [
        (s.get("name", ""), _code(s["run"])) for s in _steps(ROLLBACK) if "run" in s
    ]


# --- the target revision ---------------------------------------------------------------


def _target_definition(
    status: str = "ACTIVE",
    family: str = "experimentation-backend-staging",
    containers: tuple[tuple[str, str], ...] = (
        (
            "backend",
            "123456789012.dkr.ecr.us-west-2.amazonaws.com/experimentation-platform/backend@sha256:"
            + "4" * 64,
        ),
    ),
) -> dict:
    return {
        "taskDefinitionArn": TARGET,
        "family": family,
        "revision": 42,
        "status": status,
        "containerDefinitions": [{"name": n, "image": i} for n, i in containers],
    }


BOOTSTRAP = "123456789012.dkr.ecr.us-west-2.amazonaws.com/experimentation-platform/backend:bootstrap"


@pytest.mark.regression
@pytest.mark.parametrize(
    "definition, refused",
    [
        (_target_definition(), None),
        (_target_definition(status="INACTIVE"), "is INACTIVE, not ACTIVE"),
        (
            _target_definition(family="experimentation-backend-prod"),
            "Re-run with environment=prod",
        ),
        (
            _target_definition(family="experimentation-dashboard-staging"),
            "is a dashboard revision",
        ),
        (
            _target_definition(
                containers=(("api", BOOTSTRAP.replace(":bootstrap", ":v1")),)
            ),
            "has no container named 'backend'",
        ),
        (
            _target_definition(containers=(("backend", BOOTSTRAP),)),
            "that is a CloudFormation-registered revision, not a released one",
        ),
    ],
    ids=["released", "inactive", "other-env", "dashboard", "no-backend", "bootstrap"],
)
def test_the_target_must_be_active_in_the_family_with_a_backend_and_not_bootstrap(
    runner, definition, refused
):
    runner.scenario(
        [rule("ecs describe-task-definition", answers=[json.dumps(definition)])]
    )
    code, log, written, _ = runner.run(
        ROLLBACK,
        _step(ROLLBACK, "target"),
        {},
        {"task_definition_arn": "experimentation-backend-staging:42"},
    )
    if refused is None:
        assert code == 0, log
        assert written["arn"] == TARGET
    else:
        assert code == 1, log
        assert refused in log, log
    # Read-only, whatever it decides.
    assert _ops(runner) == ["ecs describe-task-definition"], runner.calls()


# --- the API half's two refusals -------------------------------------------------------


@pytest.mark.regression
def test_an_api_already_on_the_target_with_nothing_in_flight_is_refused(runner):
    """The target is the PRIMARY task set and the /api/* rule forwards to it,
    and nothing is in flight: nothing to roll back, so no deployment is
    created and nothing is stopped, and the run fails saying so."""
    rules = [
        *_nothing_in_flight(),
        *_verify(),
        *_primary(TARGET),
        *_create_and_approve(["Ready"]),
        *api_rules(BEFORE, RULES_BLUE),
    ]
    outputs, results, summary = _run_from_stop(runner, rules)
    ops = _ops(runner)
    for write in ("deploy create-deployment", "deploy stop-deployment"):
        assert write not in ops, ops
    code, log = results["Refuse a rollback to the revision already serving"]
    assert code == 1, log
    assert "there is nothing to roll back" in log
    assert "created no CodeDeploy deployment and stopped nothing" in log


@pytest.mark.regression
@pytest.mark.parametrize(
    "rules",
    [
        # The target is PRIMARY, but the /api/* rule forwards to the other group.
        [*_nothing_in_flight(), *api_rules(BEFORE, RULES_GREEN)],
        # Another revision is PRIMARY.
        [*_nothing_in_flight(), *api_rules(AFTER, RULES_GREEN)],
    ],
    ids=["route-elsewhere", "another-revision-primary"],
)
def test_an_api_not_serving_the_target_is_not_refused(runner, rules):
    runner.scenario(rules)
    code, log, written, _ = runner.run(
        ROLLBACK,
        _step(ROLLBACK, "Stop any deployment already in flight"),
        {"target": {"arn": TARGET}},
    )
    assert code == 0, log
    assert "already_serving" not in written, written


@pytest.mark.regression
@pytest.mark.parametrize(
    "deployments",
    [
        [(CD_ROLLBACK_ID, f"codeDeployRollback\t{BAD_ID}")],
        [(BAD_ID, "user\tNone"), (CD_ROLLBACK_ID, f"codeDeployRollback\t{BAD_ID}")],
    ],
    ids=["alone", "after-a-user-deployment"],
)
def test_codedeploys_own_rollback_in_flight_stops_nothing_and_fails(
    runner, deployments
):
    """An in-flight deployment created by CodeDeploy itself: the run stops
    nothing (not even the user deployment listed before it) and fails."""
    runner.scenario(_in_flight(*deployments))
    code, log, written, _ = runner.run(
        ROLLBACK,
        _step(ROLLBACK, "Stop any deployment already in flight"),
        {"target": {"arn": TARGET}},
    )
    assert code == 1, log
    assert "deploy stop-deployment" not in _ops(runner), runner.calls()
    assert written["refused"] == "codeDeployRollback"
    assert "CodeDeploy is already rolling back" in log


@pytest.mark.regression
def test_codedeploys_own_rollback_in_flight_rolls_back_nothing_whole_run(runner):
    """The whole run from the stop step: no deployment created, the dashboard
    half not reached, and the run red."""
    rules = _in_flight((CD_ROLLBACK_ID, f"codeDeployRollback\t{BAD_ID}"))
    outputs, results, _ = _run_from_stop(
        runner, rules, dashboard="experimentation-dashboard-staging:6"
    )
    ops = _ops(runner)
    assert "deploy create-deployment" not in ops and "ecs update-service" not in ops
    for key in ("codedeploy", "api-verify", "dashboard-rollback"):
        assert outputs[key]["__outcome__"] == "skipped", key
    assert results["Run summary"][0] == 1


@pytest.mark.regression
@pytest.mark.parametrize(
    "line",
    ["ERROR An error occurred (AccessDenied)", "None\tNone", ""],
    ids=["read-failed", "null-creator", "empty"],
)
def test_an_in_flight_deployment_whose_creator_cannot_be_read_stops_nothing(
    runner, line
):
    runner.scenario(_in_flight((BAD_ID, line)))
    code, log, written, _ = runner.run(
        ROLLBACK,
        _step(ROLLBACK, "Stop any deployment already in flight"),
        {"target": {"arn": TARGET}},
    )
    assert code == 1, log
    assert "deploy stop-deployment" not in _ops(runner), runner.calls()
    assert written["refused"] == "unreadable"


# --- stopping what is in flight, then its own deployment --------------------------------


@pytest.mark.regression
def test_a_deployment_in_flight_is_stopped_with_auto_rollback_before_its_own_is_created(
    runner,
):
    rules = [
        rule("deploy list-deployments", answers=[BAD_ID, "", ""]),
        rule("deploy get-deployment", BAD_ID, "creator", answers=["user\tNone"]),
        rule("deploy stop-deployment", BAD_ID, answers=[""]),
        rule(
            "deploy get-deployment",
            BAD_ID,
            "deploymentInfo.status",
            answers=["Stopped"],
        ),
        *_verify(),
        *_primary(NEW, TARGET),
        *_create_and_approve(["InProgress", "Ready", "InProgress"]),
    ]
    outputs, results, summary = _run_from_stop(runner, rules)
    calls = runner.calls()
    ops = [" ".join(c[:2]) for c in calls]
    (stop,) = [c for c in calls if c[:2] == ["deploy", "stop-deployment"]]
    assert BAD_ID in stop and "--auto-rollback-enabled" in stop, stop
    assert ops.index("deploy stop-deployment") < ops.index("deploy create-deployment")
    assert outputs["codedeploy"]["deployment-id"] == ROLLBACK_ID
    assert results["Run summary"][0] == 0, results["Run summary"][1]


def _create_call(runner) -> list[str]:
    runner.scenario([rule("deploy create-deployment", answers=[ROLLBACK_ID])])
    code, log, written, _ = runner.run(
        ROLLBACK,
        _step(ROLLBACK, "Roll back via CodeDeploy"),
        {"target": {"arn": TARGET}},
        {"reason": "bad release"},
    )
    assert code == 0, log
    assert written["deployment-id"] == ROLLBACK_ID
    (call,) = [c for c in runner.calls() if c[:2] == ["deploy", "create-deployment"]]
    return call


def _value(call: list[str], flag: str) -> str:
    return call[call.index(flag) + 1]


@pytest.mark.regression
def test_its_deployment_is_all_at_once_names_the_target_and_overrides_the_alarms(
    runner,
):
    """All at once, not the group's canary; the target's revision in the
    AppSpec; and the group's alarms overridden, on every run (the step's
    only condition is the already-serving refusal)."""
    call = _create_call(runner)
    assert _value(call, "--deployment-config-name") == "CodeDeployDefault.ECSAllAtOnce"
    override = json.loads(_value(call, "--override-alarm-configuration"))
    assert override == {"enabled": False, "ignorePollAlarmFailure": False, "alarms": []}
    revision = json.loads(_value(call, "--revision"))
    appspec = json.loads(revision["appSpecContent"]["content"])
    (resource,) = appspec["Resources"]
    properties = resource["TargetService"]["Properties"]
    assert properties["TaskDefinition"] == TARGET
    assert properties["LoadBalancerInfo"]["ContainerName"] == "backend"
    step = _step(ROLLBACK, "Roll back via CodeDeploy")
    assert step["if"] == "steps.stop.outputs.already_serving != 'true'"
    assert "OVERRIDE" not in json.dumps(step.get("env", {}))
    # The forward path names no config: the group's canary.
    assert "--deployment-config-name" not in _code(_step(DEPLOY, "codedeploy")["run"])


# --- when it counts the rollback as done -------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize(
    "statuses", [["InProgress"], ["Baking"], ["Succeeded"]], ids=str
)
def test_a_target_primary_without_this_runs_own_approval_is_not_a_rollback(
    runner, statuses
):
    """The target is the PRIMARY task set from the first poll (the stop's
    auto-rollback did it), but this run never saw its own deployment Ready
    and approved it: the step fails, and approves nothing."""
    code, log = _shift(
        runner, [*_primary(TARGET), *_create_and_approve(statuses)], timeout="5"
    )
    assert code == 1, log
    assert "deploy continue-deployment" not in _ops(runner)
    assert "never saw its own rollback deployment" in log


@pytest.mark.regression
def test_the_rollback_counts_once_approved_by_this_run_and_primary(runner):
    code, log = _shift(
        runner,
        [*_primary(NEW, TARGET), *_create_and_approve(["Ready", "InProgress"])],
    )
    assert code == 0, log
    assert _ops(runner).count("deploy continue-deployment") == 1
    assert "approved by this run" in log


@pytest.mark.regression
@pytest.mark.parametrize(
    "answers, code",
    [
        (["post-shift"], 0),
        (["short"] * 31, 1),
        (["pending"] * 31, 1),
        (["empty-primary"] * 31, 1),
        (["short", "post-shift"], 0),
    ],
    ids=["all-running", "one-short", "one-pending", "none-running", "then-all"],
)
def test_it_waits_until_the_primary_task_set_runs_every_desired_task(
    runner, answers, code
):
    got, log = _verify_step(runner, *answers)
    assert got == code, log


# --- what it posts, and what it does not do ----------------------------------------------


@pytest.mark.regression
def test_the_result_is_posted_to_deployments_whether_it_succeeded_or_failed():
    notify = _step(ROLLBACK, "Notify rollback result")
    assert notify["if"] == "always() && env.SLACK_ON == 'true'"
    assert notify["with"]["channel-id"] == "#deployments"
    message = notify["with"]["slack-message"]
    assert "steps.summary.outputs.slack" in message
    assert "outcome unknown" in message


@pytest.mark.regression
def test_it_sends_no_request_to_the_api_or_the_dashboard():
    """No smoke test: no step, and no script a step runs, makes an HTTP
    request; /health is checked by hand afterwards."""
    http = re.compile(
        r"\b(curl|wget|urllib|http\.client|httpx|requests|socket)\b|https?://"
    )
    scripts: set[str] = set()
    for name, code in _runs():
        assert not http.search(code), (name, http.search(code).group(0))
        scripts.update(re.findall(r"scripts/([\w.]+\.(?:py|sh))", code))
    assert {"api_serving.py", "rollback_verdict.py", "dashboard_revision.py"} <= scripts
    # The local modules those import, too.
    reached = set(scripts)
    pending = list(scripts)
    while pending:
        text = (SCRIPTS / pending.pop()).read_text(encoding="utf-8")
        for module in re.findall(r"^(?:import|from) (\w+)", text, re.M):
            if (SCRIPTS / f"{module}.py").is_file() and f"{module}.py" not in reached:
                reached.add(f"{module}.py")
                pending.append(f"{module}.py")
    for name in sorted(reached):
        text = (SCRIPTS / name).read_text(encoding="utf-8")
        code = "\n".join(
            line for line in text.splitlines() if not line.lstrip().startswith("#")
        )
        found = re.search(
            r"\b(curl|wget)\b|^\s*(?:import|from) (urllib|http|httpx|requests|socket)\b",
            code,
            re.M,
        )
        assert not found, (name, found.group(0))


@pytest.mark.regression
@pytest.mark.parametrize(
    "environment",
    [[], [{"name": "RUN_MIGRATIONS", "value": "true"}]],
    ids=["unset", "true"],
)
def test_it_does_not_check_whether_the_target_runs_migrations_on_start(
    runner, environment
):
    """A target that would run migrations on start passes the target check,
    and no step reads a container's environment or runs the deploy's check."""
    definition = _target_definition()
    definition["containerDefinitions"][0]["environment"] = environment
    runner.scenario(
        [rule("ecs describe-task-definition", answers=[json.dumps(definition)])]
    )
    code, log, _, _ = runner.run(
        ROLLBACK,
        _step(ROLLBACK, "target"),
        {},
        {"task_definition_arn": "experimentation-backend-staging:42"},
    )
    assert code == 0, log
    for name, code_text in _runs():
        assert not re.search(r"\.environment\b|environment\[\?", code_text), name
        assert "refuse_migrating_api_revision" not in code_text, name
