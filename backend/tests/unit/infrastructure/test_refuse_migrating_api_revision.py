"""Deploy refuses an API revision that would run migrations on start (#298).

`scripts/refuse_migrating_api_revision.py` reads back what ECS stored for the
API revision Deploy just registered and refuses unless its `backend`
container sets `RUN_MIGRATIONS` to exactly `"false"`. deploy.yml runs it
between "Register the API task definition" and "Create the CodeDeploy
deployment", so a refusal ends the job before a deployment exists.

The contract:

* exactly `"false"` passes; missing, `"true"`, `"False"`, empty, set twice,
  or taken from a secret refuses (exit 1) and names the fix, a `cdk deploy`
  of the Fargate stack from a checkout with #499;
* what is checked is the STORED revision (`describe-task-definition` of the
  registered ARN), not the JSON register_task_definition.sh sent;
* an AWS error, or an answer about another revision, is exit 2;
* when it refuses, `create-deployment` is never invoked. That is asserted on
  the fake `aws`'s call log, running the job's steps in order with GitHub's
  `if:` rules, not inferred from the exit status.

No AWS call is made anywhere in this file. The script's `aws` callable is
answered by a real botocore ECS client under a `Stubber` (every request is
checked against the service model, every canned answer against its output
shape), with a `before-send` hook that fails the test before anything could
leave. The step tests use the workflow Runner's fake `aws` on PATH, with the
credential chain blanked.
"""

from __future__ import annotations

import ast
import copy
import importlib.util
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import botocore.session
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from backend.tests.unit.infrastructure.test_dashboard_deploy_wiring import (
    DEPLOY,
    ENV,
    SCRIPTS,
    _if,
    _step,
    _steps,
    rule,
    runner,
)
from backend.tests.unit.infrastructure.test_forward_deploy_completes import (
    NEW as API_NEW,
)
from backend.tests.unit.infrastructure.test_forward_deploy_completes import (
    OLD as API_OLD,
)

FAMILY = f"experimentation-backend-{ENV}"
FARGATE_STACK = f"experimentation-fargate-{ENV}"
REGISTRY = "123456789012.dkr.ecr.us-west-2.amazonaws.com"
IMAGE = f"{REGISTRY}/experimentation-platform@sha256:{'c' * 64}"
OLD_IMAGE = f"{REGISTRY}/experimentation-platform@sha256:{'d' * 64}"
STEP = "Refuse an API revision that would run migrations on start"
FIX = (
    f"Fix: run `cdk deploy {FARGATE_STACK}` for {ENV} from a checkout that "
    "contains #499"
)
RUNBOOK = (
    "docs/deployment/rollback-runbook.md"
    "#deploy-refused-an-api-revision-that-would-run-migrations-on-start"
)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


refuse = _load(
    "refuse_migrating_api_revision", SCRIPTS / "refuse_migrating_api_revision.py"
)


# --- task definitions -----------------------------------------------------------

#: Sentinel: the `backend` container has no `environment` key at all.
NO_ENVIRONMENT = object()


def task_definition(
    arn: str = API_NEW,
    run_migrations: Any = "false",
    image: str = IMAGE,
    extra_environment: Sequence[dict] = (),
    secrets: Sequence[dict] = (),
    containers: Sequence[dict] | None = None,
) -> dict:
    """A describe-task-definition `taskDefinition`. `run_migrations=None`
    leaves the variable out; NO_ENVIRONMENT leaves the list out. It sets the
    FORWARDED_ALLOW_IPS the Fargate stack sets, so the job runs below also
    pass Deploy's check of that setting, which runs after this one."""
    backend: dict[str, Any] = {"name": "backend", "image": image}
    if run_migrations is not NO_ENVIRONMENT:
        environment = [{"name": "APP_ENV", "value": ENV}, *extra_environment]
        if run_migrations is not None:
            environment.append({"name": "RUN_MIGRATIONS", "value": run_migrations})
        environment.append({"name": "SEED", "value": ""})
        environment.append({"name": "FORWARDED_ALLOW_IPS", "value": "10.0.0.0/16"})
        backend["environment"] = environment
    if secrets:
        backend["secrets"] = list(secrets)
    name = arn.rsplit("/", 1)[-1]
    return {
        "taskDefinitionArn": arn,
        "family": name.split(":")[0],
        "revision": int(name.split(":")[1]),
        "status": "ACTIVE",
        "containerDefinitions": (
            [backend] if containers is None else [dict(c) for c in containers]
        ),
    }


# --- botocore Stubber behind the script's `aws` callable -------------------------


class StubbedAws:
    def __init__(self) -> None:
        session = botocore.session.Session()
        self.ecs = session.create_client("ecs", region_name="us-west-2")
        self.stub = Stubber(self.ecs)
        self.ecs.meta.events.register("before-send", self._no_network)
        self.calls: list[list[str]] = []

    @staticmethod
    def _no_network(**kwargs: Any) -> None:
        raise AssertionError(
            f"a real AWS request was about to be sent: {kwargs.get('request')}"
        )

    def stored(self, td: dict, asked: str = API_NEW) -> None:
        self.stub.add_response(
            "describe_task_definition",
            {"taskDefinition": td},
            {"taskDefinition": asked},
        )

    def error(self, code: str = "ClientException") -> None:
        self.stub.add_client_error(
            "describe_task_definition",
            service_error_code=code,
            service_message="Unable to describe task definition.",
        )

    def __call__(self, argv: Sequence[str]) -> dict:
        argv = list(argv)
        self.calls.append(argv)
        assert tuple(argv[:2]) in refuse.OPERATIONS, argv
        try:
            with self.stub:
                answer = self.ecs.describe_task_definition(
                    taskDefinition=argv[argv.index("--task-definition") + 1]
                )
        except ClientError as exc:
            raise refuse.AwsError(str(exc)) from exc
        answer.pop("ResponseMetadata", None)
        return json.loads(json.dumps(answer, default=str))

    def assert_done(self) -> None:
        self.stub.assert_no_pending_responses()


@pytest.fixture
def aws(monkeypatch) -> StubbedAws:
    for key in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
        "AWS_ROLE_ARN",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", "/dev/null")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/dev/null")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    stubbed = StubbedAws()
    assert stubbed.ecs._request_signer._credentials is None
    return stubbed


def run(aws: StubbedAws, arn: str = API_NEW) -> int:
    return refuse.main(
        [
            "--task-definition",
            arn,
            "--environment",
            ENV,
            "--fargate-stack",
            FARGATE_STACK,
        ],
        aws=aws,
    )


# --- the contract ----------------------------------------------------------------


@pytest.mark.regression
def test_exactly_false_passes(aws, capsys):
    aws.stored(task_definition(run_migrations="false"))
    assert run(aws) == refuse.OK
    out = capsys.readouterr().out
    assert "::error" not in out
    assert "experimentation-backend-staging:43: RUN_MIGRATIONS=false" in out
    aws.assert_done()


REFUSED_CASES = {
    "missing": (task_definition(run_migrations=None), "does not set RUN_MIGRATIONS"),
    "no environment at all": (
        task_definition(run_migrations=NO_ENVIRONMENT),
        "does not set RUN_MIGRATIONS",
    ),
    "true": (task_definition(run_migrations="true"), 'RUN_MIGRATIONS="true"'),
    "False": (task_definition(run_migrations="False"), 'RUN_MIGRATIONS="False"'),
    "empty": (task_definition(run_migrations=""), 'RUN_MIGRATIONS=""'),
    "padded": (task_definition(run_migrations="false "), 'RUN_MIGRATIONS="false "'),
    "set twice": (
        task_definition(
            extra_environment=[{"name": "RUN_MIGRATIONS", "value": "true"}]
        ),
        "sets RUN_MIGRATIONS 2 times",
    ),
    "from a secret": (
        task_definition(
            secrets=[{"name": "RUN_MIGRATIONS", "valueFrom": "arn:aws:ssm:x"}]
        ),
        "takes RUN_MIGRATIONS from a secret",
    ),
    "no backend container": (
        task_definition(containers=[{"name": "api", "image": IMAGE}]),
        "it has 0 containers named `backend`",
    ),
}


@pytest.mark.regression
@pytest.mark.parametrize("case", sorted(REFUSED_CASES))
def test_anything_but_exactly_false_refuses_and_names_the_fix(aws, capsys, case):
    stored, why = REFUSED_CASES[case]
    aws.stored(stored)
    assert run(aws) == refuse.REFUSED
    out = capsys.readouterr().out
    (error,) = [ln for ln in out.splitlines() if ln.startswith("::error")]
    assert error.startswith(
        "::error title=This API revision would run migrations on start::"
        "experimentation-backend-staging:43 was registered from the newest "
        "revision of its family"
    ), error
    assert why in error, error
    assert FIX in error, error
    assert "pinned to what is live" in error
    assert refuse.DONE in error
    assert RUNBOOK in error
    # Public log: no account ID, and never the break-glass input.
    assert "123456789012" not in out
    assert "override_alarms" not in out
    aws.assert_done()


@pytest.mark.regression
def test_the_other_containers_do_not_count(aws):
    """Only `backend` is the API: a sidecar's RUN_MIGRATIONS=false does not pass."""
    sidecar = {
        "name": "log-router",
        "image": IMAGE,
        "environment": [{"name": "RUN_MIGRATIONS", "value": "false"}],
    }
    backend = task_definition(run_migrations=None)["containerDefinitions"][0]
    aws.stored(task_definition(containers=[sidecar, backend]))
    assert run(aws) == refuse.REFUSED


@pytest.mark.regression
def test_an_aws_error_stops_the_deploy(aws, capsys):
    aws.error()
    assert run(aws) == refuse.UNKNOWN
    out = capsys.readouterr().out
    assert "::error title=Could not read the API revision::" in out
    assert "ClientException" in out
    aws.assert_done()


@pytest.mark.regression
def test_an_answer_about_another_revision_is_could_not_tell(aws, capsys):
    aws.stored(task_definition(arn=API_OLD))
    assert run(aws) == refuse.UNKNOWN
    out = capsys.readouterr().out
    assert "asked for experimentation-backend-staging:43, read" in out
    assert "123456789012" not in out


@pytest.mark.parametrize("arn", ["", "  "])
def test_no_revision_is_could_not_tell(aws, arn):
    assert run(aws, arn=arn) == refuse.UNKNOWN
    assert aws.calls == []


def test_the_script_is_read_only():
    assert refuse.OPERATIONS == {("ecs", "describe-task-definition")}
    with pytest.raises(ValueError):
        refuse.run_aws(["deploy", "create-deployment"])
    literals = {
        node.value
        for node in ast.walk(
            ast.parse((SCRIPTS / "refuse_migrating_api_revision.py").read_text())
        )
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    # It reads the stored revision whole: no --query projects it first.
    assert "--query" not in literals


def test_the_stubs_refuse_anything_unexpected(aws):
    with pytest.raises(botocore.exceptions.UnStubbedResponseError):
        aws(["ecs", "describe-task-definition", "--task-definition", API_NEW])
    with pytest.raises(botocore.exceptions.NoCredentialsError):
        aws.ecs.describe_task_definition(taskDefinition=API_NEW)


# --- deploy.yml: placement ------------------------------------------------------


def _index(predicate) -> list[int]:
    return [i for i, s in enumerate(_steps(DEPLOY)) if predicate(s)]


@pytest.mark.regression
def test_it_runs_after_the_registration_and_immediately_before_create_deployment():
    """Only Deploy's FORWARDED_ALLOW_IPS read-back, which has no `if:`
    either, sits between this check and create-deployment."""
    (check,) = _index(
        lambda s: "scripts/refuse_migrating_api_revision.py" in s.get("run", "")
    )
    (registered,) = _index(lambda s: s.get("id") == "api-td")
    (create,) = _index(lambda s: "aws deploy create-deployment" in s.get("run", ""))
    (proxy_check,) = _index(lambda s: s.get("id") == "api-td-proxy-check")
    assert registered + 1 == check == proxy_check - 1 == create - 2
    for index in (check, proxy_check, create):
        step = _steps(DEPLOY)[index]
        assert "if" not in step and "continue-on-error" not in step, step["name"]
    step = _steps(DEPLOY)[check]
    assert step["env"]["TASK_DEFINITION"] == "${{ steps.api-td.outputs.arn }}"
    assert '--task-definition "$TASK_DEFINITION"' in step["run"]
    assert '--fargate-stack "$FARGATE_STACK"' in step["run"]


# --- deploy.yml: the job, run in order ---------------------------------------------


#: What a run has before "Register the API task definition".
BEFORE_REGISTRATION = {
    "image": {"image": IMAGE, "__outcome__": "success"},
    "migrate": {"__outcome__": "success"},
    "previous": {"arn": API_OLD, "__outcome__": "success"},
}


def registration_rules(stored: dict, sent_base: dict | None = None) -> list[dict]:
    """register_task_definition.sh's three calls, then the check's read-back.

    `sent_base` is the family's newest revision, which the script copies and
    sends; `stored` is what ECS answers when the ARN is read back.
    """
    base = sent_base or task_definition(arn=API_OLD, image=OLD_IMAGE)
    return [
        # register_task_definition.sh's own read-back of the image.
        rule(
            "ecs describe-task-definition",
            "containerDefinitions[?name=='backend'].image",
            answers=[IMAGE],
        ),
        # Its read of the family's newest revision.
        rule(
            "ecs describe-task-definition",
            f"--task-definition {FAMILY} --query taskDefinition --output json",
            answers=[base],
        ),
        rule(
            "ecs register-task-definition",
            answers=[API_NEW],
        ),
        # The check: the registered ARN, read whole.
        rule(
            "ecs describe-task-definition",
            f"--task-definition {API_NEW} --output json",
            answers=[{"taskDefinition": stored}],
        ),
        rule(
            "deploy create-deployment",
            answers=["d-CREATED0001"],
        ),
    ]


def run_job_from_registration(runner, rules, stop_after: str | None = None):
    """Run deploy.yml's steps from "Register the API task definition" on, in
    order, with GitHub's `if:` rules: a failed step skips every later step
    whose `if:` does not ask for failure. Returns (job status, log)."""
    runner.scenario(rules)
    steps = _steps(DEPLOY)
    (start,) = _index(lambda s: s.get("id") == "api-td")
    outputs = {k: dict(v) for k, v in BEFORE_REGISTRATION.items()}
    failed = False
    log = []
    for step in steps[start:]:
        if "run" not in step:
            continue  # the Slack action
        if not _if(step.get("if", "success()"), outputs, failed):
            continue
        code, out, written, _ = runner.run(
            DEPLOY, step, outputs, {"job.status": "failure" if failed else "success"}
        )
        key = step.get("id") or step["name"]
        log.append((key, code, out))
        outputs[key] = {**written, "__outcome__": "success" if code == 0 else "failure"}
        failed = failed or code != 0
        if stop_after and key == stop_after:
            break
    return ("failure" if failed else "success"), log


def _created(runner) -> list[list[str]]:
    return [c for c in runner.calls() if c[:2] == ["deploy", "create-deployment"]]


@pytest.mark.regression
@pytest.mark.parametrize(
    "value", [None, "true", "False", ""], ids=["missing", "true", "False", "empty"]
)
def test_a_refused_revision_never_reaches_create_deployment(runner, value):
    """PE condition 2 / EM condition 7: not merely a red run -- the fake aws
    never sees `deploy create-deployment`, through the whole rest of the job."""
    status, log = run_job_from_registration(
        runner, registration_rules(task_definition(run_migrations=value))
    )
    # First, before anything about exit codes: no deployment was asked for.
    assert _created(runner) == [], runner.calls()
    assert status == "failure"
    ran = [key for key, _, _ in log]
    assert ran[:2] == ["api-td", "api-td-check"], ran
    assert log[0][1] == 0, log[0][2]
    code, out = log[1][1], log[1][2]
    assert code == refuse.REFUSED, out
    assert "::error title=This API revision would run migrations on start::" in out
    assert FIX in out
    assert "codedeploy" not in ran, ran
    # The "Migrated, not deployed" warning is what the run ends with.
    warned = [o for k, _, o in log if "Migrated, not deployed" in o]
    assert warned, ran
    assert "experimentation-backend-staging:42 is still serving" in warned[0]


@pytest.mark.regression
def test_the_stored_revision_is_checked_not_the_json_sent(runner):
    """register_task_definition.sh sends a copy of a base that HAS the
    setting; ECS answers the read-back without it. The stored answer decides."""
    good_base = task_definition(arn=API_OLD, image=OLD_IMAGE, run_migrations="false")
    status, log = run_job_from_registration(
        runner,
        registration_rules(task_definition(run_migrations=None), sent_base=good_base),
    )
    assert _created(runner) == [], runner.calls()
    assert status == "failure"
    (registered,) = [
        c for c in runner.calls() if c[:2] == ["ecs", "register-task-definition"]
    ]
    sent = json.loads(registered[registered.index("--cli-input-json") + 1])
    assert {"name": "RUN_MIGRATIONS", "value": "false"} in sent["containerDefinitions"][
        0
    ]["environment"]


@pytest.mark.regression
def test_a_stored_false_goes_on_to_create_deployment(runner):
    """The positive control: the same sequence with "false" stored does reach
    create-deployment, after the check's read-back."""
    status, log = run_job_from_registration(
        runner,
        registration_rules(task_definition(run_migrations="false")),
        stop_after="codedeploy",
    )
    assert status == "success", log
    assert [key for key, _, _ in log] == [
        "api-td",
        "api-td-check",
        "api-td-proxy-check",
        "codedeploy",
    ]
    calls = runner.calls()
    readback = calls.index(
        [
            "ecs",
            "describe-task-definition",
            "--task-definition",
            API_NEW,
            "--output",
            "json",
        ]
    )
    (created,) = _created(runner)
    assert calls.index(created) > readback


@pytest.mark.regression
def test_the_step_fails_closed_when_aws_fails(runner):
    runner.scenario(
        [rule("ecs describe-task-definition", answers=[{"error": "AccessDenied"}])]
    )
    code, out, _, _ = runner.run(
        DEPLOY, _step(DEPLOY, STEP), {"api-td": {"arn": API_NEW}}
    )
    assert code == refuse.UNKNOWN, out
    assert "::error title=Could not read the API revision::AccessDenied" in out


def test_the_fixtures_are_distinct():
    """A probe that could not tell the cases apart would make the above vacuous."""
    good = task_definition(run_migrations="false")
    bad = copy.deepcopy(good)
    bad["containerDefinitions"][0]["environment"] = [
        e for e in bad["containerDefinitions"][0]["environment"]
        if e["name"] != "RUN_MIGRATIONS"
    ]  # fmt: skip
    assert refuse.why_it_migrates(good) is None
    assert refuse.why_it_migrates(bad) is not None
