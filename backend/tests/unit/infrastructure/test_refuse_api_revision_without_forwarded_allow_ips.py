"""Deploy refuses an API task definition without a usable FORWARDED_ALLOW_IPS.

The API image builds its list of trusted proxies at start-up, and on AWS the
load balancer is on none of the task's own networks: only the task
definition's FORWARDED_ALLOW_IPS (the VPC's range, set by `cdk deploy` of the
Fargate stack) puts it on that list. Deploy registers the API revision from
the family's newest ACTIVE revision, so a stack last deployed from an older
checkout would give the new image nothing.

`scripts/refuse_api_revision_without_forwarded_allow_ips.py` is the check, and
deploy.yml runs it twice, with no `if:`:

* `--family`, beside "Refuse a profile the stacks were not deployed for",
  before anything is built, snapshotted or migrated;
* `--task-definition`, on what ECS stored for the registered ARN, immediately
  before the CodeDeploy deployment is created.

The contract:

* the `backend` container's `environment` names FORWARDED_ALLOW_IPS exactly
  once, with at least one entry, every entry accepted under the staging and
  production rules of `backend.app.core.proxy_trust` (imported, not repeated):
  exit 0. Missing, empty, malformed, named twice, from a secret, or no single
  `backend` container: exit 1, naming the `cdk deploy` to run;
* a release whose `backend/docker-entrypoint.sh` still passes uvicorn the old
  fixed list (a command line containing OLD_DEFAULT) is not checked: exit 0
  with a `::notice::`, and no AWS call. Any other entrypoint, a missing one or
  one that cannot be read is checked;
* the revision cannot be read, or the answer is about another revision: exit 2;
* it runs under `python3 -I -S`, the runner's own python3 with no
  site-packages;
* it is never run by rollback.yml or db-migrate.yml.

No AWS call is made anywhere in this file. In-process, the script's `aws`
callable is a botocore ECS client under a `Stubber` (requests checked against
the service model) with a `before-send` hook that fails the test before
anything could leave. The subprocess tests put a fake `aws` first on PATH,
with the credential chain blanked.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import stat
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import botocore.session
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from backend.app.core import proxy_trust
from backend.tests.unit.infrastructure.test_dashboard_deploy_wiring import (
    DEPLOY,
    ENV,
    ROLLBACK,
    SCRIPTS,
    WORKFLOWS,
    _code,
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
from backend.tests.unit.infrastructure.test_refuse_migrating_api_revision import (
    _created,
    registration_rules,
    run_job_from_registration,
)

pytestmark = pytest.mark.skipif(
    not DEPLOY.is_file(), reason="this tree has no .github/workflows"
)

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = SCRIPTS / "refuse_api_revision_without_forwarded_allow_ips.py"
ENTRYPOINT = REPO_ROOT / "backend" / "docker-entrypoint.sh"
MIGRATE_WORKFLOW = WORKFLOWS / "db-migrate.yml"

FAMILY = f"experimentation-backend-{ENV}"
FARGATE_STACK = f"experimentation-fargate-{ENV}"
REGISTRY = "123456789012.dkr.ecr.us-west-2.amazonaws.com"
IMAGE = f"{REGISTRY}/experimentation-platform@sha256:{'c' * 64}"
VALUE = "10.0.0.0/16"

EARLY_STEP = "Refuse an API task definition family without FORWARDED_ALLOW_IPS"
PROFILE_STEP = "Refuse a profile the stacks were not deployed for"
READ_BACK = "api-td-proxy-check"
ERROR = (
    "::error title=This API task definition does not set a usable FORWARDED_ALLOW_IPS::"
)
NOTICE = "::notice title=FORWARDED_ALLOW_IPS not checked::"
FIX = (
    f"Fix: run `cdk deploy {FARGATE_STACK}` for {ENV} from this release's "
    "checkout, pinned to what is live (docs/deployment/deployment-guide.md"
    "#16-the-stacks), which sets FORWARDED_ALLOW_IPS on the API task; then run "
    "Deploy again."
)
MODES = ("family", "task-definition")

#: What every release from v0.2.3 to v0.39.0 has on its uvicorn command line.
OLD_DEFAULT = (
    "${FORWARDED_ALLOW_IPS:-127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16}"
)
#: The end of such a release's backend/docker-entrypoint.sh, code lines only.
OLD_ENTRYPOINT = (
    "#!/usr/bin/env bash\n"
    "set -euo pipefail\n"
    'if [ "$1" = "uvicorn" ]; then\n'
    '    set -- "$@" --proxy-headers \\\n'
    f'        --forwarded-allow-ips="{OLD_DEFAULT}" \\\n'
    "        --no-server-header\n"
    "fi\n"
    'exec "$@"\n'
)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gate = _load("refuse_api_revision_without_forwarded_allow_ips", SCRIPT)


# --- releases and task definitions -------------------------------------------------

#: Sentinel: the `backend` container's environment does not name the variable.
UNSET = object()
#: Sentinel: the `backend` container has no `environment` key at all.
NO_ENVIRONMENT = object()


def make_release(root: Path, kind: str) -> Path:
    """A release checkout whose backend/docker-entrypoint.sh is *kind*."""
    release = root / f"release-{kind}"
    (release / "backend").mkdir(parents=True)
    path = release / "backend" / "docker-entrypoint.sh"
    if kind == "old":
        path.write_text(OLD_ENTRYPOINT)
    elif kind == "new":
        path.write_bytes(ENTRYPOINT.read_bytes())
    elif kind == "unreadable":
        path.mkdir()  # open() raises IsADirectoryError
    elif kind == "only in a comment":
        path.write_text(f"# it used to be {OLD_DEFAULT}\n" + ENTRYPOINT.read_text())
    elif kind == "too long":
        filler = "# " + "x" * 1000 + "\n"
        path.write_text(
            OLD_ENTRYPOINT + filler * (gate.MAX_ENTRYPOINT_BYTES // len(filler) + 1)
        )
    elif kind != "missing":
        raise AssertionError(kind)
    return release


def task_definition(
    arn: str = API_NEW,
    value: Any = VALUE,
    extra_environment: Sequence[dict] = (),
    secrets: Sequence[dict] = (),
    containers: Sequence[dict] | None = None,
    family: str = FAMILY,
    status: str = "ACTIVE",
) -> dict:
    """A describe-task-definition `taskDefinition`, RUN_MIGRATIONS=false, and
    FORWARDED_ALLOW_IPS=*value* (UNSET leaves it out; NO_ENVIRONMENT leaves
    the whole list out)."""
    backend: dict[str, Any] = {"name": "backend", "image": IMAGE}
    if value is not NO_ENVIRONMENT:
        environment = [
            {"name": "APP_ENV", "value": ENV},
            {"name": "RUN_MIGRATIONS", "value": "false"},
            *extra_environment,
        ]
        if value is not UNSET:
            environment.append({"name": "FORWARDED_ALLOW_IPS", "value": value})
        backend["environment"] = environment
    if secrets:
        backend["secrets"] = list(secrets)
    name = arn.rsplit("/", 1)[-1]
    return {
        "taskDefinitionArn": arn,
        "family": family,
        "revision": int(name.split(":")[1]),
        "status": status,
        "containerDefinitions": (
            [backend] if containers is None else [dict(c) for c in containers]
        ),
    }


# --- botocore Stubber behind the script's `aws` callable ------------------------------


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

    def stored(self, td: dict, asked: str) -> None:
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
        assert tuple(argv[:2]) in gate.OPERATIONS, argv
        try:
            with self.stub:
                answer = self.ecs.describe_task_definition(
                    taskDefinition=argv[argv.index("--task-definition") + 1]
                )
        except ClientError as exc:
            raise gate.AwsError(str(exc)) from exc
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
    yield stubbed
    # After the test's own assertions, so a failure names the case first:
    # every answer stubbed was asked for.
    stubbed.assert_done()


@pytest.fixture
def new_release(tmp_path) -> Path:
    return make_release(tmp_path, "new")


@pytest.fixture
def old_release(tmp_path) -> Path:
    return make_release(tmp_path, "old")


def asked(mode: str) -> str:
    return FAMILY if mode == "family" else API_NEW


def run(aws: Any, mode: str, release: Path, target: str | None = None) -> int:
    """The script's main() with *aws* as its AWS caller."""
    return gate.main(
        [
            f"--{mode}",
            asked(mode) if target is None else target,
            "--release",
            str(release),
            "--environment",
            ENV,
            "--fargate-stack",
            FARGATE_STACK,
        ],
        aws=aws,
    )


def check(aws, capsys, mode: str, release: Path, td: dict) -> tuple[int, str]:
    """Run with *td* as ECS's answer; the `aws` fixture checks at teardown
    that it was read."""
    aws.stored(td, asked(mode))
    code = run(aws, mode, release)
    return code, capsys.readouterr().out


def assert_refused(code: int, out: str, mode: str, why: str) -> str:
    assert code == gate.REFUSED, f"a revision ({why}) was accepted: {out}"
    (error,) = [ln for ln in out.splitlines() if ln.startswith("::error")]
    assert error.startswith(ERROR + "experimentation-backend-staging:43 "), error
    assert why in error, error
    assert FIX in error, error
    assert gate.DONE[mode] in error, error
    assert NOTICE not in out
    # Public log: no account ID.
    assert "123456789012" not in out
    return error


# --- the release check -----------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize("mode", MODES)
def test_an_old_release_without_the_setting_passes_with_a_notice(
    capsys, mode, old_release
):
    """A release that still passes uvicorn the fixed list does not read the
    variable, so its deploy is not refused. Nothing is read from AWS, although
    a revision without the setting is there to be read."""
    calls: list[list[str]] = []

    def reads(argv: Sequence[str]) -> dict:
        calls.append(list(argv))
        return {"taskDefinition": task_definition(value=UNSET)}

    code = run(reads, mode, old_release)
    out = capsys.readouterr().out
    assert code == gate.OK, f"an old release was refused: {out}"
    (notice,) = [ln for ln in out.splitlines() if ln.startswith("::")]
    assert notice.startswith(NOTICE), notice
    assert "backend/docker-entrypoint.sh passes uvicorn a fixed list" in notice
    assert calls == []


@pytest.mark.regression
@pytest.mark.parametrize("mode", MODES)
def test_a_new_release_without_the_setting_is_refused(aws, capsys, mode, new_release):
    code, out = check(aws, capsys, mode, new_release, task_definition(value=UNSET))
    assert_refused(code, out, mode, "does not set FORWARDED_ALLOW_IPS")


@pytest.mark.regression
@pytest.mark.parametrize(
    "kind", ["missing", "unreadable", "only in a comment", "too long"]
)
@pytest.mark.parametrize("mode", MODES)
def test_a_release_not_identified_as_old_is_checked(aws, capsys, tmp_path, mode, kind):
    """Fail closed: only a positive identification skips the check."""
    release = make_release(tmp_path, kind)
    code, out = check(aws, capsys, mode, release, task_definition(value=UNSET))
    assert_refused(code, out, mode, "does not set FORWARDED_ALLOW_IPS")


def test_this_trees_entrypoint_is_not_identified_as_old(old_release):
    """The image this tree builds computes its list: a release cut from it is
    checked. Fails if the old default comes back on a command line."""
    assert not gate.predates_the_list(REPO_ROOT)
    assert gate.predates_the_list(old_release)


def test_the_old_default_is_exactly_the_one_releases_carried():
    assert gate.OLD_DEFAULT == OLD_DEFAULT
    assert gate.ENTRYPOINT == Path("backend") / "docker-entrypoint.sh"


# --- the value -------------------------------------------------------------------


REFUSED_VALUES = [
    "172.18.0.3/16",
    "web",
    "*",
    "0.0.0.0/0",
    "::/0",
    "10.0.0.0/33",
    "10.0.0.0/16 10.1.0.0/16",
    "10.0.0.0/16;10.1.0.0/16",
    "10.0.0.0/16,*",
]


@pytest.mark.regression
@pytest.mark.parametrize("value", REFUSED_VALUES)
@pytest.mark.parametrize("mode", MODES)
def test_a_value_the_api_refuses_at_start_up_is_refused(
    aws, capsys, mode, new_release, value
):
    """Each message is proxy_trust's own: the validator is imported."""
    problems = proxy_trust.entry_problems(value) + proxy_trust.every_address_problems(
        value
    )
    assert problems, value
    code, out = check(aws, capsys, mode, new_release, task_definition(value=value))
    error = assert_refused(code, out, mode, "which the API refuses at start-up")
    for problem in problems:
        assert " ".join(problem.split()) in error, (problem, error)


SHAPE_CASES = {
    "empty": (task_definition(value=""), 'FORWARDED_ALLOW_IPS="", which names no'),
    "commas only": (
        task_definition(value=" , "),
        'FORWARDED_ALLOW_IPS=" , ", which names no address or network',
    ),
    "no environment at all": (
        task_definition(value=NO_ENVIRONMENT),
        "does not set FORWARDED_ALLOW_IPS",
    ),
    "set twice": (
        task_definition(
            extra_environment=[{"name": "FORWARDED_ALLOW_IPS", "value": "10.1.0.0/16"}]
        ),
        "sets FORWARDED_ALLOW_IPS 2 times",
    ),
    "from a secret": (
        task_definition(
            value=UNSET,
            secrets=[{"name": "FORWARDED_ALLOW_IPS", "valueFrom": "arn:aws:ssm:x"}],
        ),
        "takes FORWARDED_ALLOW_IPS from a secret",
    ),
    "in the environment and a secret": (
        task_definition(
            secrets=[{"name": "FORWARDED_ALLOW_IPS", "valueFrom": "arn:aws:ssm:x"}]
        ),
        "takes FORWARDED_ALLOW_IPS from a secret",
    ),
    "no backend container": (
        task_definition(containers=[{"name": "api", "image": IMAGE}]),
        "it has 0 containers named `backend`",
    ),
    "two backend containers": (
        task_definition(
            containers=[
                {
                    "name": "backend",
                    "image": IMAGE,
                    "environment": [{"name": "FORWARDED_ALLOW_IPS", "value": VALUE}],
                }
            ]
            * 2
        ),
        "it has 2 containers named `backend`",
    ),
}


@pytest.mark.regression
@pytest.mark.parametrize("case", sorted(SHAPE_CASES))
@pytest.mark.parametrize("mode", MODES)
def test_a_missing_or_ambiguous_setting_is_refused(
    aws, capsys, mode, new_release, case
):
    td, why = SHAPE_CASES[case]
    code, out = check(aws, capsys, mode, new_release, td)
    assert_refused(code, out, mode, why)


@pytest.mark.regression
@pytest.mark.parametrize("mode", MODES)
def test_another_containers_setting_does_not_count(aws, capsys, mode, new_release):
    sidecar = {
        "name": "log-router",
        "image": IMAGE,
        "environment": [{"name": "FORWARDED_ALLOW_IPS", "value": VALUE}],
    }
    backend = task_definition(value=UNSET)["containerDefinitions"][0]
    td = task_definition(containers=[sidecar, backend])
    code, out = check(aws, capsys, mode, new_release, td)
    assert_refused(code, out, mode, "does not set FORWARDED_ALLOW_IPS")


@pytest.mark.regression
@pytest.mark.parametrize(
    "value", [VALUE, "10.0.0.0/16,fd00::/8", "10.0.3.10", " 10.0.0.0/16 ,"]
)
@pytest.mark.parametrize("mode", MODES)
def test_a_usable_value_passes(aws, capsys, mode, new_release, value):
    code, out = check(aws, capsys, mode, new_release, task_definition(value=value))
    assert code == gate.OK, out
    assert "::" not in out, out
    assert out.strip() == (
        "experimentation-backend-staging:43: FORWARDED_ALLOW_IPS is set on "
        "`backend` (as stored by ECS)"
    )


# --- could not tell -------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize("mode", MODES)
def test_an_unreadable_revision_stops_the_deploy(aws, capsys, mode, new_release):
    aws.error()
    assert run(aws, mode, new_release) == gate.UNKNOWN
    out = capsys.readouterr().out
    assert "::error title=Could not read the API task definition::" in out
    assert "ClientException" in out
    assert gate.DONE[mode] in out
    aws.assert_done()


@pytest.mark.regression
@pytest.mark.parametrize(
    "mode, td",
    [
        ("task-definition", task_definition(arn=API_OLD)),
        ("family", task_definition(family="experimentation-migrate-staging")),
        ("family", task_definition(status="INACTIVE")),
    ],
    ids=["another ARN", "another family", "not ACTIVE"],
)
def test_an_answer_about_another_revision_is_could_not_tell(
    aws, capsys, mode, td, new_release
):
    code, out = check(aws, capsys, mode, new_release, td)
    assert code == gate.UNKNOWN, out
    assert "::error title=Could not read the API task definition::asked for" in out
    assert "123456789012" not in out


@pytest.mark.parametrize(
    "argv",
    [
        ["--task-definition", "", "--release", "r", "--environment", ENV,
         "--fargate-stack", FARGATE_STACK],
        ["--family", "  ", "--release", "r", "--environment", ENV,
         "--fargate-stack", FARGATE_STACK],
        ["--family", FAMILY, "--task-definition", API_NEW, "--release", "r",
         "--environment", ENV, "--fargate-stack", FARGATE_STACK],
        ["--family", FAMILY, "--environment", ENV, "--fargate-stack", FARGATE_STACK],
        ["--release", "r", "--environment", ENV, "--fargate-stack", FARGATE_STACK],
    ],
    ids=["empty ARN", "blank family", "both", "no release", "neither"],
)  # fmt: skip
def test_bad_arguments_are_could_not_tell(aws, argv):
    assert gate.main(argv, aws=aws) == gate.UNKNOWN
    assert aws.calls == []


def test_the_script_is_read_only():
    assert gate.OPERATIONS == {("ecs", "describe-task-definition")}
    with pytest.raises(ValueError):
        gate.run_aws(["deploy", "create-deployment"])
    literals = {
        node.value
        for node in ast.walk(ast.parse(SCRIPT.read_text()))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    # It reads the stored revision whole: no --query projects it first.
    assert "--query" not in literals


@pytest.mark.regression
def test_the_validator_is_imported_not_repeated():
    """PP6c: the rules are proxy_trust's, from this checkout."""
    assert gate.proxy_trust is proxy_trust
    assert Path(gate.proxy_trust.__file__).resolve() == (
        REPO_ROOT / "backend" / "app" / "core" / "proxy_trust.py"
    )
    tree = ast.parse(SCRIPT.read_text())
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "ipaddress" not in imported
    assert gate.VARIABLE == proxy_trust.SETTING == "FORWARDED_ALLOW_IPS"


# --- as the runner runs it: python3 -I -S, no site-packages --------------------------

FAKE_AWS = r"""#!{python}
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_AWS_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
if args[:2] != ["ecs", "describe-task-definition"]:
    print("fake aws: unexpected call " + " ".join(args), file=sys.stderr)
    sys.exit(99)
sys.stdout.write(open(os.environ["FAKE_AWS_ANSWER"]).read())
"""


def run_isolated(
    tmp_path: Path, td: dict, release: Path
) -> subprocess.CompletedProcess:
    """The script as deploy.yml runs it, `python3 -I -S`, with a fake `aws`."""
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir(exist_ok=True)
    fake = fakebin / "aws"
    fake.write_text(FAKE_AWS.replace("{python}", sys.executable))
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    answer = tmp_path / "answer.json"
    answer.write_text(json.dumps({"taskDefinition": td}))
    env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}
    env.update(
        PATH=f"{fakebin}{os.pathsep}{os.environ.get('PATH', '')}",
        AWS_CONFIG_FILE="/nonexistent/aws-config",
        AWS_SHARED_CREDENTIALS_FILE="/nonexistent/aws-credentials",
        AWS_EC2_METADATA_DISABLED="true",
        FAKE_AWS_ANSWER=str(answer),
        FAKE_AWS_LOG=str(tmp_path / "aws-calls.log"),
    )
    return subprocess.run(
        [
            sys.executable,
            "-B",
            "-I",
            "-S",
            str(SCRIPT),
            "--task-definition",
            API_NEW,
            "--release",
            str(release),
            "--environment",
            ENV,
            "--fargate-stack",
            FARGATE_STACK,
        ],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_isolated_mode_has_no_site_packages():
    """The probe below is only worth something if -I -S really leaves the
    project's dependencies out: pydantic is installed here, and not there."""
    assert importlib.util.find_spec("pydantic") is not None
    probe = subprocess.run(
        [sys.executable, "-B", "-I", "-S", "-c", "import pydantic"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert probe.returncode != 0, probe.stdout
    assert "No module named 'pydantic'" in probe.stderr


@pytest.mark.regression
def test_it_runs_with_the_standard_library_only(tmp_path, new_release):
    """PP6c: Deploy has no setup-python. Under `python3 -I -S` the script and
    everything it imports -- proxy_trust among them -- must be standard
    library only."""
    passed = run_isolated(tmp_path, task_definition(), new_release)
    assert passed.returncode == gate.OK, passed.stdout + passed.stderr
    assert "FORWARDED_ALLOW_IPS is set on `backend`" in passed.stdout
    refused = run_isolated(
        tmp_path, task_definition(value="172.18.0.3/16"), new_release
    )
    assert refused.returncode == gate.REFUSED, refused.stdout + refused.stderr
    assert "is an address with a mask" in refused.stdout
    calls = (tmp_path / "aws-calls.log").read_text().splitlines()
    assert [json.loads(c)[:2] for c in calls] == [
        ["ecs", "describe-task-definition"]
    ] * 2


# --- deploy.yml: placement -------------------------------------------------------


def _index(predicate) -> list[int]:
    return [i for i, s in enumerate(_steps(DEPLOY)) if predicate(s)]


def _runs_the_gate(step: dict) -> bool:
    return SCRIPT.name in step.get("run", "")


def _first_change() -> int:
    """The first step that changes AWS: deploy.yml's ECR login."""
    (login,) = _index(lambda s: str(s.get("uses", "")).startswith(
        "aws-actions/amazon-ecr-login"
    ))  # fmt: skip
    return login


@pytest.mark.regression
def test_the_family_check_runs_beside_the_profile_check_before_anything_changes():
    """PP6b: refused before the image push, the snapshot and the migration."""
    early, late = _index(_runs_the_gate)
    steps = _steps(DEPLOY)
    (profile,) = _index(lambda s: s.get("name") == PROFILE_STEP)
    (release,) = _index(lambda s: (s.get("with") or {}).get("path") == "release")
    assert steps[early]["name"] == EARLY_STEP
    assert release < early == profile + 1 < _first_change(), (early, profile)
    assert "if" not in steps[early], "the gate must not carry an if:"
    assert "continue-on-error" not in steps[early]
    run = _code(steps[early]["run"])
    assert '--family "$ECS_BACKEND_TASK_FAMILY"' in run
    assert "--task-definition" not in run


@pytest.mark.regression
def test_the_read_back_runs_after_the_registration_and_immediately_before_create_deployment():
    early, late = _index(_runs_the_gate)
    steps = _steps(DEPLOY)
    (registered,) = _index(lambda s: s.get("id") == "api-td")
    (create,) = _index(lambda s: "aws deploy create-deployment" in s.get("run", ""))
    assert steps[late].get("id") == READ_BACK
    assert registered < late == create - 1, (registered, late, create)
    for index in (late, create):
        assert "if" not in steps[index], "the gate must not carry an if:"
        assert "continue-on-error" not in steps[index], steps[index]["name"]
    assert steps[late]["env"]["TASK_DEFINITION"] == "${{ steps.api-td.outputs.arn }}"
    run = _code(steps[late]["run"])
    assert '--task-definition "$TASK_DEFINITION"' in run
    assert "--family" not in run


@pytest.mark.regression
def test_both_runs_are_isolated_and_name_the_release_and_the_stack():
    steps = _steps(DEPLOY)
    (checkout,) = _index(lambda s: (s.get("with") or {}).get("path") == "release")
    for index in _index(_runs_the_gate):
        run = _code(steps[index]["run"])
        assert f"python3 -I -S scripts/{SCRIPT.name}" in run, run
        assert "--release release " in run, run
        assert '--environment "$TARGET_ENV"' in run
        assert '--fargate-stack "$FARGATE_STACK"' in run
        assert checkout < index


@pytest.mark.regression
def test_rollback_and_the_migration_workflow_never_run_it():
    """PP6a: a rollback target from before the change needs nothing, and must
    not be refused."""
    for path in (ROLLBACK, MIGRATE_WORKFLOW):
        assert SCRIPT.name not in path.read_text(encoding="utf-8"), path.name
        assert "proxy_trust" not in path.read_text(encoding="utf-8"), path.name


# --- deploy.yml: the steps, run as written ---------------------------------------


def _early(runner, td: dict | None, release: str | None):
    """Run the family check as deploy.yml has it, from the job's working
    directory, with ./release holding an entrypoint of *release*'s kind."""
    if release is not None:
        made = make_release(runner.root, release)
        (runner.work / "release").symlink_to(made, target_is_directory=True)
    runner.scenario(
        [
            rule(
                "ecs describe-task-definition",
                f"--task-definition {FAMILY} --output json",
                answers=[{"taskDefinition": td}],
            )
        ]
    )
    (index,) = _index(lambda s: s.get("name") == EARLY_STEP)
    code, out, _, _ = runner.run(DEPLOY, _steps(DEPLOY)[index], {})
    return code, out


@pytest.mark.regression
def test_the_family_step_refuses_a_stack_without_the_setting(runner):
    code, out = _early(runner, task_definition(value=UNSET), "new")
    assert code == gate.REFUSED, out
    assert ERROR in out and FIX in out
    assert gate.DONE["family"] in out
    assert runner.calls() == [
        ["ecs", "describe-task-definition", "--task-definition", FAMILY,
         "--output", "json"]
    ]  # fmt: skip


@pytest.mark.regression
def test_the_family_step_checks_when_there_is_no_release_entrypoint(runner):
    code, out = _early(runner, task_definition(value=UNSET), None)
    # A crash exits 1 too: the refusal is told apart by its message.
    assert code == gate.REFUSED, out
    assert ERROR in out and FIX in out, out
    assert len(runner.calls()) == 1, runner.calls()


@pytest.mark.regression
def test_the_family_step_lets_an_old_release_through_with_a_notice(runner):
    code, out = _early(runner, task_definition(value=UNSET), "old")
    assert code == gate.OK, out
    assert NOTICE in out
    assert runner.calls() == []


def test_the_family_step_passes_a_stack_with_the_setting(runner):
    code, out = _early(runner, task_definition(), "new")
    assert code == gate.OK, out
    assert "FORWARDED_ALLOW_IPS is set on `backend`" in out


@pytest.mark.regression
def test_a_refused_revision_never_reaches_create_deployment(runner):
    """Not merely a red run: the fake aws never sees `deploy create-deployment`
    through the rest of the job, and the RUN_MIGRATIONS check ran first."""
    (runner.work / "release").symlink_to(
        make_release(runner.root, "new"), target_is_directory=True
    )
    status, log = run_job_from_registration(
        runner, registration_rules(task_definition(value=UNSET))
    )
    assert _created(runner) == [], runner.calls()
    assert status == "failure"
    ran = [key for key, _, _ in log]
    assert ran[:3] == ["api-td", "api-td-check", READ_BACK], ran
    assert [code for _, code, _ in log[:3]] == [0, 0, gate.REFUSED], log
    out = log[2][2]
    assert ERROR in out and FIX in out
    assert gate.DONE["task-definition"] in out
    assert "codedeploy" not in ran, ran
    warned = [o for k, _, o in log if "Migrated, not deployed" in o]
    assert warned, ran


@pytest.mark.regression
@pytest.mark.parametrize("release", ["new", "old"])
def test_a_usable_or_unneeded_setting_goes_on_to_create_deployment(runner, release):
    """The positive controls: the same sequence reaches create-deployment, after
    the read-back, when the stored revision is usable or the release is old."""
    (runner.work / "release").symlink_to(
        make_release(runner.root, release), target_is_directory=True
    )
    value = VALUE if release == "new" else UNSET
    status, log = run_job_from_registration(
        runner,
        registration_rules(task_definition(value=value)),
        stop_after="codedeploy",
    )
    assert status == "success", log
    assert [key for key, _, _ in log] == [
        "api-td",
        "api-td-check",
        READ_BACK,
        "codedeploy",
    ]
    assert (NOTICE in log[2][2]) == (release == "old"), log[2][2]
    (created,) = _created(runner)
    assert runner.calls().index(created) == len(runner.calls()) - 1
