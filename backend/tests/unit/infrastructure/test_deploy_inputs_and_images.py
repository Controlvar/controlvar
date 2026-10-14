"""What Deploy checks before it builds, what the images hold, and which release
brought what an operator rolls back past (#1142, D73 TR4).

* Every workflow refuses an environment whose account ID, role or public URL
  is not set, before it assumes a role.
* Deploy's secrets check (``scripts/check_task_secrets.py``) runs before
  anything is built, and refuses a reference that is not a complete ARN, a
  required secret that is not referenced, and one that does not exist; the
  application refuses to start without each of those secrets, and the image
  carries no ``.env`` file to supply one.
* The dashboard image is built with an empty API URL, so it calls the API on
  its own origin; the release images are pushed to GHCR.
* The modules' migrations ship in the full image only.
* The releases that brought segment targeting and the API tasks that do not
  migrate on start, read from CHANGELOG.md; and what a downgrade across the
  holdout migration takes with it.

Reads the workflows, the Dockerfiles and the scripts; runs the scripts with a
fake reader or under the shared Runner (no AWS keys). Nothing reaches AWS.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path

import pytest
import yaml

from backend.tests.unit.infrastructure.test_dashboard_deploy_wiring import (
    DEPLOY,
    ROLLBACK,
    SCRIPTS,
    _load,
    _step,
    runner,
)
from backend.tests.unit.infrastructure.test_deployment_secrets import (
    STACKS,
    _run,
    container_environment,
)
from backend.tests.unit.infrastructure.test_task_definition_scripts import (
    _migrate,
    aws,
)

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
DB_MIGRATE = WORKFLOWS / "db-migrate.yml"
RELEASE = WORKFLOWS / "release.yml"
MODULES = (REPO_ROOT / "modules" / "backend").is_dir()
needs_workflows = pytest.mark.skipif(
    not DEPLOY.is_file(), reason="this tree has no .github/workflows"
)


def _script(name: str):
    spec = importlib.util.spec_from_file_location(
        f"{name}_facts", SCRIPTS / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# --- an environment that is not configured ----------------------------------------------


@needs_workflows
@pytest.mark.parametrize(
    "workflow", [DEPLOY, ROLLBACK, DB_MIGRATE], ids=lambda p: p.name
)
@pytest.mark.parametrize(
    "secrets, refused",
    [
        (
            {"secrets.AWS_ACCOUNT_ID": "123456789012", "secrets.AWS_ROLE_ARN": "set"},
            False,
        ),
        ({"secrets.AWS_ACCOUNT_ID": "", "secrets.AWS_ROLE_ARN": "set"}, True),
        ({"secrets.AWS_ACCOUNT_ID": "12345", "secrets.AWS_ROLE_ARN": "set"}, True),
        ({"secrets.AWS_ACCOUNT_ID": "123456789012", "secrets.AWS_ROLE_ARN": ""}, True),
    ],
    ids=["configured", "no-account", "account-not-12-digits", "no-role"],
)
def test_every_workflow_refuses_an_environment_that_is_not_configured(
    runner, workflow, secrets, refused
):
    document = _load(workflow)
    (job,) = [j for j in document["jobs"].values() if "environment" in j]
    step = job["steps"][0]
    assert step["name"] == "Refuse an environment that is not configured"
    env = {
        "ACCOUNT_ID": secrets["secrets.AWS_ACCOUNT_ID"],
        "ROLE_ARN_SET": "true" if secrets["secrets.AWS_ROLE_ARN"] else "false",
    }
    if "PUBLIC_BASE_URL" in step.get("env", {}):
        env["PUBLIC_BASE_URL"] = "https://app.example.com"
    # The step's env reads secrets, which the Runner does not resolve: the
    # values arrive as the env the step maps them to.
    assert set(step["env"]) >= {"ACCOUNT_ID", "ROLE_ARN_SET"}
    runner.scenario([])
    code, out, _, _ = runner.run(workflow, {**step, "env": {}}, {}, {}, **env)
    assert (code != 0) is refused, out
    assert runner.calls() == []
    # The step that assumes the AWS role comes after it.
    role = [
        i
        for i, s in enumerate(job["steps"])
        if str(s.get("uses", "")).startswith("aws-actions/configure-aws-")
    ]
    assert role and min(role) > 0, role


@needs_workflows
def test_deploy_also_refuses_an_environment_without_its_public_url(runner):
    (job,) = [j for j in _load(DEPLOY)["jobs"].values() if "environment" in j]
    runner.scenario([])
    code, out, _, _ = runner.run(
        DEPLOY,
        {**job["steps"][0], "env": {}},
        {},
        {},
        ACCOUNT_ID="123456789012",
        ROLE_ARN_SET="true",
        PUBLIC_BASE_URL="",
    )
    assert code == 1, out
    assert "vars.PUBLIC_BASE_URL is not set" in out


# --- the secrets the task definitions reference -------------------------------------------

ACCOUNT = "123456789012"
REGION = "us-west-2"
PREFIX = "/staging/experimentation"


def _arn(name: str, suffix: str = "AbC123") -> str:
    return f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:{PREFIX}/{name}-{suffix}"


class SecretsReads:
    def __init__(self, references: dict[str, str], existing: set[str]):
        self.references = references
        self.existing = existing

    def __call__(self, argv):
        if argv[:2] == ["ecs", "describe-task-definition"]:
            return {
                "containerDefinitions": [
                    {
                        "name": "backend",
                        "secrets": [
                            {"name": k, "valueFrom": v}
                            for k, v in self.references.items()
                        ],
                    }
                ]
            }
        if argv[:2] == ["secretsmanager", "describe-secret"]:
            arn = argv[argv.index("--secret-id") + 1]
            if arn not in self.existing:
                raise checks.AwsError(
                    "An error occurred (ResourceNotFoundException) when calling the "
                    "DescribeSecret operation"
                )
            name = arn.split(":secret:", 1)[1].rsplit("-", 1)[0]
            return {"ARN": arn, "Name": name}
        raise AssertionError(argv)


checks = _script("check_task_secrets")

FULL = {
    "SECRET_KEY": _arn("jwt-secret"),
    "FIRST_SUPERUSER_PASSWORD": _arn("first-superuser-password"),
    "AUDIT_HMAC_KEY": _arn("audit-hmac-key"),
}


def _check(
    references: dict[str, str], existing: set[str], profile: str = "full"
) -> int:
    return checks.main(
        [
            "--secrets-prefix",
            PREFIX,
            "--profile",
            profile,
            "--region",
            REGION,
            "--app-family",
            "experimentation-backend-staging",
            "--app-family",
            "experimentation-migrate-staging",
        ],
        aws=SecretsReads(references, existing),
        environ={"EXPECTED_ACCOUNT_ID": ACCOUNT},
    )


@pytest.mark.parametrize(
    "references, existing, profile, expected",
    [
        (FULL, set(FULL.values()), "full", 0),
        (
            {k: v for k, v in FULL.items() if k != "AUDIT_HMAC_KEY"},
            set(FULL.values()),
            "core",
            0,
        ),
        (
            {k: v for k, v in FULL.items() if k != "AUDIT_HMAC_KEY"},
            set(FULL.values()),
            "full",
            1,
        ),
        (
            {**FULL, "SECRET_KEY": FULL["SECRET_KEY"].rsplit("-", 1)[0]},
            set(FULL.values()),
            "full",
            1,
        ),
        (FULL, set(FULL.values()) - {FULL["FIRST_SUPERUSER_PASSWORD"]}, "full", 1),
    ],
    ids=["full", "core", "full-without-audit-key", "partial-arn", "does-not-exist"],
)
def test_the_secrets_check_wants_each_secret_by_complete_arn_and_existing(
    capsys, references, existing, profile, expected
):
    assert _check(references, existing, profile) == expected, capsys.readouterr().out


@needs_workflows
def test_the_secrets_check_runs_before_anything_is_built():
    steps = _load(DEPLOY)["jobs"]["deploy"]["steps"]
    names = [s.get("name", "") for s in steps]
    check = names.index("Every secret the task definitions reference exists")
    assert "scripts/check_task_secrets.py" in steps[check]["run"]
    assert "--app-family" in steps[check]["run"]
    build = names.index("Build and push the image once, and resolve its digest")
    assert check < build
    assert "if" not in steps[check]


@pytest.mark.parametrize("missing", ["SECRET_KEY", "FIRST_SUPERUSER_PASSWORD"])
def test_without_each_secret_the_application_refuses_to_start_in_production(missing):
    environment = container_environment(STACKS["fargate"], profile="core")
    assert missing in environment
    script = "import backend.app.core.config as c; print(c.settings.ENVIRONMENT)"
    assert _run(environment, script).returncode == 0
    del environment[missing]
    result = _run(environment, script)
    assert result.returncode != 0, result.stdout


def test_the_image_carries_no_env_file():
    """The settings read a dotenv file only from the working directory, /app,
    which takes backend/, modules/ and VERSION from the context, and the
    context excludes every .env at its root."""
    ignored = [
        line.strip()
        for line in (REPO_ROOT / ".dockerignore")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert ".env" in ignored and ".env.*" in ignored
    dockerfile = (REPO_ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8")
    sources = [
        part
        for line in dockerfile.splitlines()
        if line.startswith("COPY ") and "--from=" not in line
        for part in line.split()[1:-1]
        if not part.startswith("--")
    ]
    assert sources, "no COPY read"
    for source in sources:
        assert ".env" not in source, source
    assert "COPY . " not in dockerfile


# --- the images ---------------------------------------------------------------------------


@needs_workflows
def test_the_dashboard_image_is_built_with_an_empty_api_url():
    frontend = (REPO_ROOT / "frontend" / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r'^ARG NEXT_PUBLIC_API_URL=""$', frontend, re.M), (
        "the default moved"
    )
    assert re.search(
        r"^ENV NEXT_PUBLIC_API_URL=\$\{NEXT_PUBLIC_API_URL\}$", frontend, re.M
    )
    web = _step(DEPLOY, "web-image")["run"]
    assert "-f release/frontend/Dockerfile" in web
    assert "NEXT_PUBLIC_API_URL" not in web
    release = yaml.safe_load(RELEASE.read_text(encoding="utf-8"))
    for step in release["jobs"]["web-images"]["steps"]:
        assert "NEXT_PUBLIC_API_URL" not in str(
            step.get("with", {}).get("build-args", "")
        )


@needs_workflows
def test_the_release_pushes_the_api_and_dashboard_images_to_ghcr():
    release = yaml.safe_load(RELEASE.read_text(encoding="utf-8"))
    assert release["env"]["REGISTRY"] == "ghcr.io"
    resolve = release["jobs"]["verify"]["steps"][0]["run"]
    assert 'image="${REGISTRY}/' in resolve and '"web_image=${image}-web"' in resolve
    for job, dockerfile, output in (
        ("api-images", "backend/Dockerfile", "image"),
        ("web-images", "frontend/Dockerfile", "web_image"),
    ):
        pushed = [
            s
            for s in release["jobs"][job]["steps"]
            if (s.get("with") or {}).get("push") is True
        ]
        assert pushed, job
        for step in pushed:
            assert step["with"]["file"] == dockerfile, job
        login = [
            s
            for s in release["jobs"][job]["steps"]
            if str(s.get("uses", "")).startswith("docker/login-action")
        ]
        assert [s["with"]["registry"] for s in login] == ["${{ env.REGISTRY }}"], job


def _stages(dockerfile: str) -> dict[str, tuple[str, list[str]]]:
    """{stage: (its base, its lines)}"""
    stages: dict[str, tuple[str, list[str]]] = {}
    current = None
    for line in dockerfile.splitlines():
        match = re.match(r"^FROM (\S+) AS (\S+)$", line)
        if match:
            current = match.group(2)
            stages[current] = (match.group(1), [])
        elif current and line and not line.startswith("#"):
            stages[current][1].append(line)
    return stages


def test_the_module_migrations_ship_in_the_full_image_only():
    stages = _stages((REPO_ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8"))
    assert {"runtime", "core", "full", "modules-src"} <= set(stages)

    def chain(stage: str) -> list[str]:
        names = []
        while stage in stages:
            names.append(stage)
            stage = stages[stage][0]
        return names

    assert chain("core") == ["core", "runtime"]
    for stage in chain("core"):
        for line in stages[stage][1]:
            assert "modules-src" not in line, (stage, line)
            assert not re.search(r"\bCOPY\b.*\bmodules", line), (stage, line)
    full = "\n".join(stages["full"][1])
    assert (
        "COPY --from=modules-src --chown=appuser:appgroup /modules/backend /app/modules/backend/"
        in full
    )
    assert stages["full"][0] == "runtime" and stages["core"][0] == "runtime"


# --- the releases that brought what a rollback goes past ------------------------------------


def _release_sections() -> dict[str, str]:
    text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    sections: dict[str, str] = {}
    for part in re.split(r"^## \[", text, flags=re.M)[1:]:
        version = part.split("]", 1)[0]
        sections[version] = part
    return sections


def _key(version: str) -> tuple[int, ...]:
    return tuple(int(x) for x in version.split("."))


def _first_release_with(pattern: str) -> str:
    found = [v for v, body in _release_sections().items() if re.search(pattern, body)]
    assert found, pattern
    return min(found, key=_key)


def test_segment_targeting_arrived_in_0_22_0():
    assert _first_release_with(r"can target a segment") == "0.22.0"
    from backend.app.core.targeting_adapter import DASHBOARD_OPERATORS

    assert {"in_segment", "not_in_segment"} <= set(DASHBOARD_OPERATORS)


def test_the_api_tasks_stopped_migrating_on_start_in_0_14_0():
    assert _first_release_with(r"API tasks no longer run migrations") == "0.14.0"
    assert "#499" in _release_sections()["0.14.0"]


# --- a downgrade across the holdout migration -------------------------------------------


def _downgrade_calls() -> list[tuple[str, tuple]]:
    path = next(
        (REPO_ROOT / "backend" / "app" / "db" / "migrations" / "versions").glob(
            "d29a479daafe_*.py"
        )
    )
    tree = ast.parse(path.read_text(encoding="utf-8"))
    constants = {
        target.id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    (downgrade,) = [
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "downgrade"
    ]
    calls = []
    for node in ast.walk(downgrade):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "op"
        ):
            args = tuple(
                constants.get(a.id, a.id)
                if isinstance(a, ast.Name)
                else getattr(a, "value", None)
                for a in node.args
            )
            calls.append((node.func.attr, args))
    return calls


def test_a_downgrade_across_the_holdout_migration_drops_the_salt_and_the_population():
    """After it, an older release buckets with the one legacy salt and has no
    table to record anyone in; a holdout created on the newer release has a
    salt of its own."""
    calls = _downgrade_calls()
    assert ("drop_table", ("holdout_population",)) in calls, calls
    assert ("drop_column", ("global_holdouts", "hash_salt")) in calls, calls
    from backend.app.models.global_holdout import LEGACY_HOLDOUT_SALT, new_holdout_salt

    assert LEGACY_HOLDOUT_SALT == "global_holdout_v1"
    salts = {new_holdout_salt() for _ in range(3)}
    assert LEGACY_HOLDOUT_SALT not in salts and len(salts) == 3


# --- the workflow role's trust ----------------------------------------------------------


def test_the_workflow_roles_trust_policy_pins_the_oidc_subject_exactly():
    import json

    policy = json.loads(
        (
            REPO_ROOT / "infrastructure" / "cdk" / "github-actions-trust-policy.json"
        ).read_text(encoding="utf-8")
    )
    (statement,) = policy["Statement"]
    assert statement["Action"] == "sts:AssumeRoleWithWebIdentity"
    condition = statement["Condition"]
    # StringEquals only: no StringLike, so no pattern can widen the subject.
    assert set(condition) == {"StringEquals"}, condition
    equals = condition["StringEquals"]
    subject = equals["token.actions.githubusercontent.com:sub"]
    assert "*" not in subject and "?" not in subject
    assert subject.endswith(":environment:<ENVIRONMENT>"), subject
    assert equals["token.actions.githubusercontent.com:aud"] == "sts.amazonaws.com"


# --- the migration task's log -----------------------------------------------------------


@needs_workflows
def test_the_run_prints_the_migration_tasks_last_100_log_lines(aws):
    code, out, _, calls, _ = _migrate(aws, exit_code="3")
    assert code == 1
    (logs,) = [c for c in calls if c[:2] == ["logs", "get-log-events"]]
    assert logs[logs.index("--limit") + 1] == "100"
    assert (
        logs[logs.index("--log-group-name") + 1]
        == "/ecs/experimentation-migrate-staging"
    )
    assert logs[logs.index("--log-stream-name") + 1] == "migrate/backend/0abc"
    assert out.index("Running upgrade a -> b") < out.index("exited 3")
    job = _load(DEPLOY)["jobs"]["deploy"]
    assert (
        job["env"]["MIGRATE_LOG_GROUP"]
        == "/ecs/experimentation-migrate-${{ inputs.environment }}"
    )
    assert '"$MIGRATE_LOG_GROUP"' in _step(DEPLOY, "migrate")["run"]


# --- the migration task and Database Migration ------------------------------------------


def test_the_migration_task_upgrades_and_stamps_every_head():
    """`backend.app.db.bootstrap`, the migration task's command: `heads`,
    plural, wherever it upgrades or stamps."""
    tree = ast.parse(
        (REPO_ROOT / "backend" / "app" / "db" / "bootstrap.py").read_text(
            encoding="utf-8"
        )
    )
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "command"
        and node.func.attr in ("upgrade", "stamp")
    ]
    assert {c.func.attr for c in calls} == {"upgrade", "stamp"}, calls
    for call in calls:
        assert (
            isinstance(call.args[1], ast.Constant) and call.args[1].value == "heads"
        ), ast.dump(call)


@needs_workflows
def test_database_migration_takes_no_image_and_runs_the_one_serving():
    document = yaml.safe_load(DB_MIGRATE.read_text(encoding="utf-8"))
    on = document.get("on", document.get(True))
    assert set(on["workflow_dispatch"]["inputs"]) == {
        "environment",
        "direction",
        "target",
    }
    (job,) = [j for j in document["jobs"].values() if "environment" in j]
    (serving,) = [s for s in job["steps"] if s.get("id") == "serving"]
    assert "taskSets[?status=='PRIMARY'].taskDefinition" in serving["run"]
