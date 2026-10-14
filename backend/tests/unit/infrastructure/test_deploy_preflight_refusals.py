"""What Deploy refuses before it changes anything, and the alarm override.

The scripts are driven through their own ``main()`` with a fake AWS caller,
and the workflow steps are run as written (``Runner`` from
``test_dashboard_deploy_wiring.py``: the step's ``run:`` under bash, its
``env:`` resolved from the YAML, a fake ``aws`` first on PATH). Pinned:

* the alarm pre-flight (``scripts/refuse_alarm_active.py``): an API alarm in
  ALARM refuses a deploy before anything is built; an alarm read that is
  refused stops every deploy with "Could not read the API's alarms"; with the
  break-glass the firing alarm is a warning and the deploy goes on;
* Deploy runs that check first, passes the override only when the person
  dispatching it ticks it (with a reason), and creates its CodeDeploy
  deployment with the alarms overridden only then. Rollback creates its
  deployment with them overridden always;
* Rollback changes no schema;
* an API revision without ``RUN_MIGRATIONS=false`` is refused, naming the
  ``cdk deploy`` to run;
* the secrets check (``scripts/check_task_secrets.py``): every ``valueFrom``
  of the API's, the migration's and the dashboard's task definitions,
  partial or foreign ARNs refused before any Secrets Manager call, the
  profile's secrets required, ``describe-secret`` on each complete ARN, and a
  log that names secrets by name only;
* the dispatch: ``environment``, ``version`` (a release tag) and ``profile``;
* the snapshots the workflows take, ``pre-deploy-*`` and ``pre-migration-*``,
  which nothing deletes.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest
import yaml

from backend.tests.unit.infrastructure.test_dashboard_deploy_wiring import (
    DEPLOY,
    ROLLBACK,
    Runner,
    _step,
    _steps,
    rule,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS = REPO_ROOT / "scripts"

pytestmark = pytest.mark.skipif(
    not (REPO_ROOT / ".github" / "workflows").is_dir(),
    reason="this tree has no .github/workflows",
)

ENV = "staging"
ACCOUNT = "123456789012"
REGION = "us-west-2"
BLUE = f"experimentation-api-5xx-blue-{ENV}"
GREEN = f"experimentation-api-5xx-green-{ENV}"
ALARM_STEP = "Refuse while an API alarm is firing (before the build)"
CREATE_STEP = "Create the CodeDeploy deployment (blue/green)"
OVERRIDE = "--override-alarm-configuration"
API_REVISION = (
    f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/experimentation-backend-{ENV}:42"
)


def _script(name: str):
    spec = importlib.util.spec_from_file_location(
        f"{name}_for_refusal_test", SCRIPTS / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def runner(tmp_path):
    return Runner(tmp_path)


def _index(name: str) -> int:
    steps = _steps(DEPLOY)
    (index,) = [i for i in range(len(steps)) if steps[i].get("name") == name]
    return index


def _first_change() -> int:
    """The first step that builds, snapshots, migrates or deploys."""
    steps = _steps(DEPLOY)
    for index in range(len(steps)):
        code = str(steps[index].get("run", ""))
        if any(
            word in code
            for word in (
                "docker build",
                "docker push",
                "create-db-cluster-snapshot",
                "run_migration_task.sh",
                "create-deployment",
            )
        ):
            return index
    raise AssertionError("deploy.yml builds and changes nothing")


# --- the alarm pre-flight -------------------------------------------------------


def _group(names=(BLUE, GREEN)) -> dict:
    return {
        "deploymentGroupInfo": {
            "alarmConfiguration": {
                "enabled": True,
                "alarms": [{"name": name} for name in names],
            },
            "deploymentConfigName": "CodeDeployDefault.ECSCanary10Percent15Minutes",
        }
    }


def _alarms(**states: str) -> dict:
    return {
        "MetricAlarms": [
            {
                "AlarmName": name,
                "StateValue": states.get(name, "OK"),
                "StateUpdatedTimestamp": "2026-10-13T10:00:00Z",
                "StateReason": "Threshold Crossed",
            }
            for name in (BLUE, GREEN)
        ],
        "CompositeAlarms": [],
    }


def _fake(answers: dict):
    """An AWS caller answering by ``(service, verb)``; an exception is raised."""
    made = []

    def aws(argv):
        made.append(list(argv))
        answer = answers[tuple(argv[:2])]
        if isinstance(answer, Exception):
            raise answer
        return answer

    aws.made = made
    return aws


def _preflight(*extra: str) -> list[str]:
    return [
        "--application",
        f"experimentation-platform-{ENV}",
        "--group",
        f"experimentation-{ENV}",
        "--environment",
        ENV,
        "--expect",
        BLUE,
        "--expect",
        GREEN,
        *extra,
    ]


@pytest.mark.regression
def test_a_firing_api_alarm_refuses_the_deploy_before_anything_is_built(capsys):
    alarm = _script("refuse_alarm_active")
    aws = _fake(
        {
            ("deploy", "get-deployment-group"): _group(),
            ("cloudwatch", "describe-alarms"): _alarms(**{BLUE: "ALARM"}),
        }
    )
    assert alarm.main(_preflight("--stage", "before-build"), aws=aws) == 1
    out = capsys.readouterr().out
    assert f"::error title=An alarm is already firing::{BLUE} is in ALARM" in out
    assert "Nothing has been built or changed." in out


@pytest.mark.regression
def test_with_the_break_glass_a_firing_alarm_is_a_warning_and_the_deploy_goes_on(
    capsys,
):
    alarm = _script("refuse_alarm_active")
    aws = _fake(
        {
            ("deploy", "get-deployment-group"): _group(),
            ("cloudwatch", "describe-alarms"): _alarms(**{GREEN: "ALARM"}),
        }
    )
    assert (
        alarm.main(
            _preflight("--stage", "before-build", "--alarms-overridden"), aws=aws
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "::warning title=An alarm is firing, and the alarms are overridden::" in out
    assert "::error" not in out


@pytest.mark.regression
@pytest.mark.parametrize("overridden", [False, True])
@pytest.mark.parametrize("refused", ["get-deployment-group", "describe-alarms"])
def test_an_alarm_read_that_is_refused_stops_every_deploy(capsys, overridden, refused):
    alarm = _script("refuse_alarm_active")
    denied = alarm.AwsError(
        "An error occurred (AccessDeniedException) when calling the operation: "
        "User is not authorized"
    )
    answers = {
        ("deploy", "get-deployment-group"): _group(),
        ("cloudwatch", "describe-alarms"): _alarms(),
    }
    service = "deploy" if refused == "get-deployment-group" else "cloudwatch"
    answers[(service, refused)] = denied
    extra = ("--alarms-overridden",) if overridden else ()
    assert (
        alarm.main(_preflight("--stage", "before-build", *extra), aws=_fake(answers))
        == 2
    )
    out = capsys.readouterr().out
    assert "::error title=Could not read the API's alarms::" in out
    assert "Nothing has been built or changed." in out


@pytest.mark.regression
@pytest.mark.parametrize(
    ("ticked", "status"), [("false", 1), ("true", 0)], ids=["unticked", "ticked"]
)
def test_deploy_runs_the_alarm_check_first_and_overrides_only_when_ticked(
    runner, ticked, status
):
    index = _index(ALARM_STEP)
    step = _steps(DEPLOY)[index]
    assert index < _first_change()
    assert "if" not in step and "continue-on-error" not in step
    runner.scenario(
        [
            rule("deploy get-deployment-group", answers=[_group()]),
            rule("cloudwatch describe-alarms", answers=[_alarms(**{BLUE: "ALARM"})]),
        ]
    )
    code, out, _, _ = runner.run(DEPLOY, step, inputs={"override_alarms": ticked})
    assert code == status, out
    passed = [c for c in runner.calls() if c[:2] == ["deploy", "get-deployment-group"]]
    assert passed, runner.calls()


@pytest.mark.regression
def test_the_override_is_off_unless_a_person_ticks_it_with_a_reason(runner):
    inputs = yaml.safe_load(DEPLOY.read_text())[True]["workflow_dispatch"]["inputs"]
    assert inputs["override_alarms"]["type"] == "boolean"
    assert inputs["override_alarms"]["default"] is False
    assert inputs["override_alarms"]["required"] is False
    (confirm,) = [
        s
        for job in yaml.safe_load(DEPLOY.read_text())["jobs"].values()
        for s in job["steps"]
        if s.get("name") == "Confirm the alarm override"
    ]
    code, out, _, _ = runner.run(
        DEPLOY,
        confirm,
        inputs={"override_alarms": "true", "override_alarms_reason": " "},
    )
    assert code == 1 and "No reason for the alarm override" in out
    code, out, _, _ = runner.run(
        DEPLOY,
        confirm,
        inputs={"override_alarms": "true", "override_alarms_reason": "fix forward"},
        GITHUB_ACTOR="an-operator",
    )
    assert code == 0, out
    assert "ALARMS OVERRIDDEN" in out


def _create_deployment_args(runner) -> list[str]:
    (call,) = [c for c in runner.calls() if c[:2] == ["deploy", "create-deployment"]]
    return call


@pytest.mark.regression
@pytest.mark.parametrize("ticked", ["false", "true"])
def test_rollback_overrides_the_alarms_always_and_deploy_only_under_the_break_glass(
    runner, ticked
):
    runner.scenario([rule("deploy create-deployment", answers=["d-FAKE0001"])])
    code, out, _, _ = runner.run(
        ROLLBACK,
        _step(ROLLBACK, "codedeploy"),
        outputs={"target": {"arn": API_REVISION}},
        inputs={"reason": "bad release"},
    )
    assert code == 0, out
    args = _create_deployment_args(runner)
    assert json.loads(args[args.index(OVERRIDE) + 1])["enabled"] is False

    runner.scenario([rule("deploy create-deployment", answers=["d-FAKE0002"])])
    code, out, _, _ = runner.run(
        DEPLOY,
        _step(DEPLOY, CREATE_STEP),
        outputs={"api-td": {"arn": API_REVISION}},
        inputs={"override_alarms": ticked, "version": "v1.2.3", "profile": "full"},
    )
    assert code == 0, out
    args = _create_deployment_args(runner)
    if ticked == "true":
        assert json.loads(args[args.index(OVERRIDE) + 1])["enabled"] is False
    else:
        assert OVERRIDE not in args, args


# --- rollback and the schema ---------------------------------------------------------


@pytest.mark.regression
def test_rollback_changes_no_schema():
    """Rolling the API back runs the older revision against the schema as it
    is: Rollback runs no migration task and no alembic."""
    document = yaml.safe_load(ROLLBACK.read_text())
    code = "\n".join(
        line
        for job in document["jobs"].values()
        for step in job["steps"]
        for line in str(step.get("run", "")).splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "create-deployment" in code  # not vacuous
    for word in (
        "run_migration_task",
        "run-task",
        "alembic",
        "upgrade",
        "downgrade",
        "MIGRATE_TASK_FAMILY",
        "experimentation-migrate",
    ):
        assert word not in code, word
    uses = [
        str(step.get("uses", ""))
        for job in document["jobs"].values()
        for step in job["steps"]
    ]
    assert not [u for u in uses if "migrat" in u.lower()], uses


# --- an API revision that would migrate ----------------------------------------------


def _revision(environment: list[dict]) -> dict:
    return {
        "taskDefinition": {
            "taskDefinitionArn": API_REVISION,
            "containerDefinitions": [{"name": "backend", "environment": environment}],
        }
    }


@pytest.mark.regression
def test_an_api_revision_without_the_setting_is_refused_naming_the_cdk_deploy(capsys):
    refuse = _script("refuse_migrating_api_revision")
    argv = [
        "--task-definition",
        API_REVISION,
        "--environment",
        ENV,
        "--fargate-stack",
        f"experimentation-fargate-{ENV}",
    ]
    aws = _fake({("ecs", "describe-task-definition"): _revision([])})
    assert refuse.main(argv, aws=aws) == 1
    out = capsys.readouterr().out
    assert "does not set RUN_MIGRATIONS" in out
    assert (
        f"run `cdk deploy experimentation-fargate-{ENV}` for {ENV} from a checkout"
        in out
    )
    aws = _fake(
        {
            ("ecs", "describe-task-definition"): _revision(
                [{"name": "RUN_MIGRATIONS", "value": "false"}]
            )
        }
    )
    assert refuse.main(argv, aws=aws) == 0
    # Deploy runs it on the registered revision, before the deployment.
    step = _steps(DEPLOY)[
        _index("Refuse an API revision that would run migrations on start")
    ]
    assert "scripts/refuse_migrating_api_revision.py" in step["run"]
    assert "if" not in step and "continue-on-error" not in step
    assert (
        _index("Register the API task definition with this image")
        < _index("Refuse an API revision that would run migrations on start")
        < _index(CREATE_STEP)
    )


# --- the secrets every task definition references ------------------------------------

PREFIX = f"/{ENV}/experimentation"
FAMILIES = {
    "api": f"experimentation-backend-{ENV}",
    "migrate": f"experimentation-migrate-{ENV}",
    "dashboard": f"experimentation-dashboard-{ENV}",
}
SUFFIX = {
    "jwt-secret": "AbC123",
    "first-superuser-password": "DeF456",
    "audit-hmac-key": "GhI789",
}


def _arn(
    name: str, *, account: str = ACCOUNT, region: str = REGION, suffix=True
) -> str:
    tail = f"-{SUFFIX[name]}" if suffix else ""
    return f"arn:aws:secretsmanager:{region}:{account}:secret:{PREFIX}/{name}{tail}"


def _task(*secrets: tuple[str, str]) -> dict:
    return {
        "containerDefinitions": [
            {"name": "c", "secrets": [{"name": n, "valueFrom": v} for n, v in secrets]}
        ]
    }


def _app_secrets(profile: str = "full", **replace: str) -> tuple:
    names = ["jwt-secret", "first-superuser-password"]
    if profile == "full":
        names.append("audit-hmac-key")
    return tuple((name.upper(), replace.get(name, _arn(name))) for name in names)


def _secrets_fake(definitions: dict[str, dict]):
    made = []

    def aws(argv):
        made.append(list(argv))
        if argv[:2] == ["ecs", "describe-task-definition"]:
            return definitions[argv[argv.index("--task-definition") + 1]]
        if argv[:2] == ["secretsmanager", "describe-secret"]:
            arn = argv[argv.index("--secret-id") + 1]
            name = arn.split(":secret:", 1)[1].rsplit("-", 1)[0]
            return {"Name": name, "ARN": arn}
        raise AssertionError(argv)

    aws.made = made
    return aws


def _check(definitions: dict, profile: str = "full"):
    secrets = _script("check_task_secrets")
    aws = _secrets_fake(definitions)
    argv = [
        "--secrets-prefix",
        PREFIX,
        "--profile",
        profile,
        "--region",
        REGION,
        "--app-family",
        FAMILIES["api"],
        "--app-family",
        FAMILIES["migrate"],
        "--family",
        FAMILIES["dashboard"],
    ]
    status = secrets.main(argv, aws=aws, environ={"EXPECTED_ACCOUNT_ID": ACCOUNT})
    return status, aws.made


def _definitions(api=None, migrate=None, dashboard=None) -> dict:
    return {
        FAMILIES["api"]: api or _task(*_app_secrets()),
        FAMILIES["migrate"]: migrate or _task(*_app_secrets()),
        FAMILIES["dashboard"]: dashboard or _task(),
    }


def _described(made) -> list[str]:
    return [m[m.index("--secret-id") + 1] for m in made if m[0] == "secretsmanager"]


@pytest.mark.regression
def test_deploy_checks_every_task_definitions_secrets_before_it_builds_anything():
    index = _index("Every secret the task definitions reference exists")
    step = _steps(DEPLOY)[index]
    assert index < _first_change()
    assert "if" not in step and "continue-on-error" not in step
    run = step["run"]
    assert "python3 scripts/check_task_secrets.py" in run
    for flag in (
        '--app-family "$ECS_BACKEND_TASK_FAMILY"',
        '--app-family "$MIGRATE_TASK_FAMILY"',
        '--family "$ECS_DASHBOARD_TASK_FAMILY"',
        '--profile "$PROFILE"',
    ):
        assert flag in run, flag


@pytest.mark.regression
@pytest.mark.parametrize("family", ["api", "migrate", "dashboard"])
@pytest.mark.parametrize(
    "reference",
    [
        _arn("jwt-secret", suffix=False),
        _arn("jwt-secret", account="210987654321"),
        _arn("jwt-secret", region="eu-west-1"),
        f"{PREFIX}/jwt-secret",
    ],
    ids=["partial", "other-account", "other-region", "a-name"],
)
def test_a_reference_that_is_not_a_complete_arn_here_is_refused_unread(
    capsys, family, reference
):
    definitions = _definitions()
    if family == "dashboard":
        definitions[FAMILIES[family]] = _task(("X", reference))
    else:
        definitions[FAMILIES[family]] = _task(
            *_app_secrets(**{"jwt-secret": reference})
        )
    status, made = _check(definitions)
    assert status == 1
    assert not _described(made), "Secrets Manager was called"
    # Every family's task definition was read.
    read = [m[m.index("--task-definition") + 1] for m in made if m[0] == "ecs"]
    assert sorted(read) == sorted(FAMILIES.values())
    assert "Secret reference is not a complete ARN" in capsys.readouterr().out


@pytest.mark.regression
@pytest.mark.parametrize(
    ("profile", "missing", "refused"),
    [
        ("core", "audit-hmac-key", False),
        ("full", "audit-hmac-key", True),
        ("core", "jwt-secret", True),
        ("full", "first-superuser-password", True),
    ],
)
def test_the_api_and_migration_must_reference_the_profiles_secrets(
    capsys, profile, missing, refused
):
    kept = tuple(s for s in _app_secrets("full") if s[0] != missing.upper())
    status, made = _check(_definitions(api=_task(*kept)), profile=profile)
    assert status == (1 if refused else 0)
    if refused:
        out = capsys.readouterr().out
        assert f"{FAMILIES['api']} does not reference {PREFIX}/{missing}" in out
        assert not _described(made)


@pytest.mark.regression
def test_each_complete_arn_is_described_and_the_log_names_secrets_by_name_only(capsys):
    dashboard = _task(("WEB", _arn("jwt-secret")))
    status, made = _check(_definitions(dashboard=dashboard))
    assert status == 0
    arns = {_arn(n) for n in SUFFIX}
    assert sorted(_described(made)) == sorted(arns)
    out = capsys.readouterr().out
    shown = [line for line in out.splitlines() if not line.startswith("::add-mask::")]
    assert shown
    for name in SUFFIX:
        assert f"exists: {PREFIX}/{name}" in shown
    text = "\n".join(shown)
    assert ACCOUNT not in text
    for arn in arns:
        assert arn not in text
    for suffix in SUFFIX.values():
        assert suffix not in text
    assert not re.search(r"arn:aws", text), text


# --- the dispatch ---------------------------------------------------------------------


@pytest.mark.regression
def test_deploy_is_dispatched_with_an_environment_a_release_tag_and_a_profile():
    document = yaml.safe_load(DEPLOY.read_text())
    assert document["name"] == "Deploy"
    inputs = document[True]["workflow_dispatch"]["inputs"]
    assert inputs["environment"]["options"] == ["staging", "prod"]
    assert inputs["version"]["type"] == "string" and inputs["version"]["required"]
    assert inputs["profile"]["options"] == ["full", "core"]
    # The image it deploys is the release's `vX.Y.Z-<profile>` tag.
    (image,) = [s for s in _steps(DEPLOY) if s.get("id") == "image"]
    assert 'IMAGE_TAG="${VERSION}-${PROFILE}"' in image["run"]
    job = [j for j in document["jobs"].values() if "environment" in j][0]
    assert job["env"]["VERSION"] == "${{ inputs.version }}"
    assert job["env"]["PROFILE"] == "${{ inputs.profile }}"


# --- the snapshots the workflows take ----------------------------------------------


@pytest.mark.regression
def test_the_workflows_take_named_snapshots_and_delete_none():
    """Deploy takes ``pre-deploy-<env>-...`` and Database Migration
    ``pre-migration-<env>-...``; nothing deletes one, so each is kept until
    someone deletes it."""
    workflows = REPO_ROOT / ".github" / "workflows"
    names = {}
    for name, prefix in (
        ("deploy.yml", "pre-deploy-"),
        ("db-migrate.yml", "pre-migration-"),
    ):
        document = yaml.safe_load((workflows / name).read_text())
        (step,) = [
            s
            for job in document["jobs"].values()
            for s in job["steps"]
            if "create-db-cluster-snapshot" in str(s.get("run", ""))
        ]
        (made,) = re.findall(r'^id="([^"$]+)\$\{TARGET_ENV\}-', step["run"], re.M)
        names[name] = made
        assert made == prefix
    assert names == {"deploy.yml": "pre-deploy-", "db-migrate.yml": "pre-migration-"}
    for path in [*workflows.glob("*.yml"), *(REPO_ROOT / "scripts").glob("*")]:
        if path.is_file():
            text = path.read_text(errors="replace")
            assert "delete-db-cluster-snapshot" not in text, path
            assert "delete_db_cluster_snapshot" not in text, path
