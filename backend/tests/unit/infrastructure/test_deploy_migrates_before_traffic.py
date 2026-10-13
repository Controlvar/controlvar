"""On AWS the migration runs before any traffic moves, and a failure stops there.

Read from ``.github/workflows/deploy.yml`` as GitHub Actions runs it: one job,
steps in order, each step running only while every step before it succeeded
unless its ``if:`` says otherwise. So:

* the migration task (``experimentation-migrate-<env>``) runs before the
  CodeDeploy deployment is created and before traffic shifts, while the release
  that was serving still serves;
* a failed migration step fails the job there: no step from it to the traffic
  shift may continue on error or run after a failure;
* ``rollback.yml`` does not run the check the Deploy workflow runs on the API
  revision it is about to deploy.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
DEPLOY = WORKFLOWS / "deploy.yml"
ROLLBACK = WORKFLOWS / "rollback.yml"

if not DEPLOY.is_file():
    pytest.skip("this tree has no .github/workflows", allow_module_level=True)

MIGRATE = "Run the migration"
CREATE = "Create the CodeDeploy deployment (blue/green)"
SHIFT = "Shift traffic and confirm the API serves this revision"
CHECK_SCRIPT = "scripts/refuse_migrating_api_revision.py"


def _steps(path: Path, job: str) -> list[dict]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"][job]["steps"]


def _names(steps: list[dict]) -> list[str]:
    return [s.get("name") or s.get("uses", "") for s in steps]


@pytest.mark.regression
def test_the_migration_task_runs_before_the_deployment_that_shifts_traffic():
    steps = _steps(DEPLOY, "deploy")
    names = _names(steps)
    migrate, create, shift = (
        names.index(MIGRATE),
        names.index(CREATE),
        names.index(SHIFT),
    )
    assert migrate < create < shift, (migrate, create, shift)
    run = steps[migrate]["run"]
    assert "bash scripts/run_migration_task.sh" in run
    assert '"$MIGRATE_LOG_GROUP"' in run
    env = yaml.safe_load(DEPLOY.read_text(encoding="utf-8"))["jobs"]["deploy"]["env"]
    assert (
        env["MIGRATE_TASK_FAMILY"]
        == "experimentation-migrate-${{ inputs.environment }}"
    )
    register = steps[
        names.index("Register the migration task definition with this image")
    ]
    assert '"$MIGRATE_TASK_FAMILY"' in register["run"]
    assert (
        names.index("Register the migration task definition with this image") < migrate
    )
    # The family the CDK app registers the migration task under.
    spec = importlib.util.spec_from_file_location(
        "_stack_names", REPO_ROOT / "infrastructure" / "cdk" / "stacks" / "names.py"
    )
    stack_names = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(stack_names)
    for environment in ("staging", "prod"):
        assert stack_names.migration_task_family(environment) == (
            f"experimentation-migrate-{environment}"
        )


@pytest.mark.regression
def test_a_failed_migration_stops_the_deploy_before_any_traffic_moves():
    steps = _steps(DEPLOY, "deploy")
    names = _names(steps)
    for step in steps[names.index(MIGRATE) : names.index(SHIFT) + 1]:
        name = step.get("name")
        assert not step.get("continue-on-error"), name
        condition = str(step.get("if", ""))
        assert "always()" not in condition and "failure()" not in condition, name
        assert "set -euo pipefail" in step.get("run", "set -euo pipefail"), name


@pytest.mark.regression
def test_rollback_does_not_check_its_target_as_the_deploy_does():
    deploy = DEPLOY.read_text(encoding="utf-8")
    assert CHECK_SCRIPT in deploy
    assert ROLLBACK.is_file()
    assert CHECK_SCRIPT not in ROLLBACK.read_text(encoding="utf-8")
