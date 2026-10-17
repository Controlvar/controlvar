"""The GitHub workflow role: who assumes it, for how long, and what it may do.

``scripts/iam_actions.py`` derives the role's permissions from the calls the
Deploy, Rollback and Database Migration workflows make, the local actions
they use and the scripts either runs. Everything here runs the generator over
the workflows as they are, or reads the workflows and the two policy files
the role is made from:

* the three workflows, and only they, assume the role, from one
  environment-bound job each, for as long as that job may run (9600 s for a
  160-minute deploy, 5400 s for the 90-minute rollback and migration);
* the trust policy template pins one environment's ``sub`` exactly;
* each action is needed by exactly the calls listed in ``EXPECTED``, and the
  committed permissions policy is what the generator writes from them;
* every resource is ``*`` but two: snapshot tagging, on cluster snapshots
  only, and ``iam:PassRole``, to ECS tasks only. The role holds no other IAM
  action;
* six actions come from what a call implies rather than from its verb, and
  ``codedeploy:UpdateDeploymentGroup`` from the alarm-override flag alone.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
GITHUB = REPO_ROOT / ".github"
WORKFLOWS = GITHUB / "workflows"
SCRIPT = REPO_ROOT / "scripts" / "iam_actions.py"
POLICY = REPO_ROOT / "infrastructure" / "cdk" / "github-actions-deploy-policy.json"
TRUST = REPO_ROOT / "infrastructure" / "cdk" / "github-actions-trust-policy.json"
SUBJECT = "repo:Controlvar@341280474/controlvar@1367480368:environment:<ENVIRONMENT>"

pytestmark = pytest.mark.skipif(
    not WORKFLOWS.is_dir(), reason="this tree has no .github/workflows"
)

DEPLOY = ".github/workflows/deploy.yml"
ROLLBACK = ".github/workflows/rollback.yml"
MIGRATE = ".github/workflows/db-migrate.yml"
CREATE = "aws deploy create-deployment"
PUSH, PULL = "docker push", "docker pull"

#: Each action the role holds, and exactly the calls that need it.
EXPECTED: dict[str, set[str]] = {
    "cloudformation:DescribeStackResources": {
        "scripts/check_live_target_group.py: aws cloudformation describe-stack-resources"
    },
    "cloudformation:DescribeStacks": {
        ".github/actions/stack-outputs/action.yml: aws cloudformation describe-stacks"
    },
    "cloudwatch:DescribeAlarms": {
        "scripts/refuse_alarm_active.py: aws cloudwatch describe-alarms"
    },
    "codedeploy:ContinueDeployment": {
        f"{ROLLBACK}: aws deploy continue-deployment",
        "scripts/shift_traffic.py: aws deploy continue-deployment",
    },
    "codedeploy:CreateDeployment": {f"{DEPLOY}: {CREATE}", f"{ROLLBACK}: {CREATE}"},
    "codedeploy:GetApplicationRevision": {
        f"{DEPLOY}: {CREATE}",
        f"{ROLLBACK}: {CREATE}",
    },
    "codedeploy:GetDeployment": {
        f"{DEPLOY}: aws deploy get-deployment",
        f"{ROLLBACK}: aws deploy get-deployment",
        "scripts/refuse_active_deployment.py: aws deploy get-deployment",
        "scripts/shift_traffic.py: aws deploy get-deployment",
    },
    "codedeploy:GetDeploymentConfig": {f"{DEPLOY}: {CREATE}", f"{ROLLBACK}: {CREATE}"},
    "codedeploy:GetDeploymentGroup": {
        "scripts/refuse_alarm_active.py: aws deploy get-deployment-group"
    },
    "codedeploy:ListDeployments": {
        f"{ROLLBACK}: aws deploy list-deployments",
        "scripts/refuse_active_deployment.py: aws deploy list-deployments",
    },
    "codedeploy:RegisterApplicationRevision": {
        f"{DEPLOY}: {CREATE}",
        f"{ROLLBACK}: {CREATE}",
    },
    "codedeploy:StopDeployment": {f"{ROLLBACK}: aws deploy stop-deployment"},
    "codedeploy:UpdateDeploymentGroup": {
        f"{DEPLOY}: {CREATE} --override-alarm-configuration",
        f"{ROLLBACK}: {CREATE} --override-alarm-configuration",
    },
    "ecr:BatchCheckLayerAvailability": {f"{DEPLOY}: {PULL}", f"{DEPLOY}: {PUSH}"},
    "ecr:BatchGetImage": {f"{DEPLOY}: {PULL}"},
    "ecr:CompleteLayerUpload": {f"{DEPLOY}: {PUSH}"},
    "ecr:DescribeImages": {f"{DEPLOY}: aws ecr describe-images"},
    "ecr:DescribeRepositories": {f"{DEPLOY}: aws ecr describe-repositories"},
    "ecr:GetAuthorizationToken": {f"{DEPLOY}: uses aws-actions/amazon-ecr-login"},
    "ecr:GetDownloadUrlForLayer": {f"{DEPLOY}: {PULL}"},
    "ecr:InitiateLayerUpload": {f"{DEPLOY}: {PUSH}"},
    "ecr:PutImage": {f"{DEPLOY}: {PUSH}"},
    "ecr:UploadLayerPart": {f"{DEPLOY}: {PUSH}"},
    "ecs:DescribeClusters": {f"{DEPLOY}: aws ecs describe-clusters"},
    "ecs:DescribeServices": {
        f"{MIGRATE}: aws ecs describe-services",
        f"{DEPLOY}: aws ecs describe-services",
        f"{ROLLBACK}: aws ecs describe-services",
        "scripts/check_dashboard_image.py: aws ecs describe-services",
        "scripts/check_live_target_group.py: aws ecs describe-services",
        "scripts/ecs_rolling_rollout.sh: aws ecs describe-services",
        "scripts/shift_traffic.py: aws ecs describe-services",
    },
    "ecs:DescribeTaskDefinition": {
        f"{MIGRATE}: aws ecs describe-task-definition",
        f"{DEPLOY}: aws ecs describe-task-definition",
        f"{ROLLBACK}: aws ecs describe-task-definition",
        "scripts/check_dashboard_image.py: aws ecs describe-task-definition",
        "scripts/check_task_secrets.py: aws ecs describe-task-definition",
        "scripts/ecs_rolling_rollout.sh: aws ecs describe-task-definition",
        "scripts/refuse_migrating_api_revision.py: aws ecs describe-task-definition",
        "scripts/register_task_definition.sh: aws ecs describe-task-definition",
    },
    "ecs:DescribeTasks": {
        "scripts/ecs_rolling_rollout.sh: aws ecs describe-tasks",
        "scripts/run_migration_task.sh: aws ecs describe-tasks",
    },
    "ecs:ListTasks": {"scripts/ecs_rolling_rollout.sh: aws ecs list-tasks"},
    "ecs:RegisterTaskDefinition": {
        "scripts/register_task_definition.sh: aws ecs register-task-definition"
    },
    "ecs:RunTask": {"scripts/run_migration_task.sh: aws ecs run-task"},
    "ecs:UpdateService": {"scripts/ecs_rolling_rollout.sh: aws ecs update-service"},
    "elasticloadbalancing:DescribeListeners": {
        "scripts/check_live_target_group.py: aws elbv2 describe-listeners"
    },
    "elasticloadbalancing:DescribeRules": {
        "scripts/check_live_target_group.py: aws elbv2 describe-rules"
    },
    "elasticloadbalancing:DescribeTargetHealth": {
        "scripts/shift_traffic.py: aws elbv2 describe-target-health"
    },
    "iam:PassRole": {
        "scripts/register_task_definition.sh: aws ecs register-task-definition",
        "scripts/run_migration_task.sh: aws ecs run-task",
    },
    "logs:GetLogEvents": {"scripts/run_migration_task.sh: aws logs get-log-events"},
    "rds:AddTagsToResource": {
        f"{MIGRATE}: aws rds create-db-cluster-snapshot",
        f"{DEPLOY}: aws rds create-db-cluster-snapshot",
    },
    "rds:CreateDBClusterSnapshot": {
        f"{MIGRATE}: aws rds create-db-cluster-snapshot",
        f"{DEPLOY}: aws rds create-db-cluster-snapshot",
    },
    "rds:DescribeDBClusterSnapshots": {
        f"{MIGRATE}: aws rds wait db-cluster-snapshot-available",
        f"{DEPLOY}: aws rds wait db-cluster-snapshot-available",
    },
    "secretsmanager:DescribeSecret": {
        "scripts/check_task_secrets.py: aws secretsmanager describe-secret"
    },
}

#: The actions no call names by its own verb: what AWS checks behind a call
#: (IMPLIED) or behind one of its flags (FLAG_IMPLIED).
IMPLIED_ACTIONS = {
    "iam:PassRole",
    "codedeploy:GetDeploymentConfig",
    "codedeploy:RegisterApplicationRevision",
    "codedeploy:GetApplicationRevision",
    "rds:AddTagsToResource",
    "codedeploy:UpdateDeploymentGroup",
}


def _generator():
    spec = importlib.util.spec_from_file_location("iam_actions_for_role_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def iam():
    return _generator()


@pytest.fixture(scope="module")
def calls(iam) -> dict[str, set[str]]:
    return {action: set(places) for action, places in iam.calls().items()}


@pytest.fixture(scope="module")
def generated(iam, calls) -> dict:
    return iam.policy(calls)


def _statements(policy: dict) -> dict[str, dict]:
    return {statement["Sid"]: statement for statement in policy["Statement"]}


def _actions(statement: dict) -> list[str]:
    actions = statement["Action"]
    return [actions] if isinstance(actions, str) else list(actions)


def _workflow(path: Path) -> dict:
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    document["on"] = document.pop(True, document.get("on"))
    return document


def _assumes_the_role(step: dict) -> bool:
    uses = str(step.get("uses", ""))
    return uses.split("@", 1)[0] == "aws-actions/configure-aws-credentials"


# --- who assumes the role, and for how long ------------------------------------


@pytest.mark.regression
def test_deploy_rollback_and_database_migration_assume_the_role_and_nothing_else(iam):
    found = {}
    for path in sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")]):
        document = _workflow(path)
        for name, job in (document.get("jobs") or {}).items():
            steps = [s for s in job.get("steps") or [] if _assumes_the_role(s)]
            if not steps:
                continue
            (step,) = steps
            assert step["with"]["role-to-assume"] == "${{ secrets.AWS_ROLE_ARN }}"
            assert job["environment"] == "${{ inputs.environment }}", (path, name)
            assert job["permissions"]["id-token"] == "write", (path, name)
            found[path.name] = document["name"]
    assert found == {
        "deploy.yml": "Deploy",
        "rollback.yml": "Rollback",
        "db-migrate.yml": "Database Migration",
    }
    # The generator reads exactly these three: the role's permissions are
    # derived from the workflows that use it.
    assert sorted(p.name for p in iam.WORKFLOWS) == sorted(found)
    # Nothing else assumes a role: no local action uses the AWS sign-in
    # action, no workflow, action or script calls STS's AssumeRole in any
    # spelling, and the role's ARN is handed to nothing but the action.
    actions = sorted(
        [*(GITHUB / "actions").rglob("*.yml"), *(GITHUB / "actions").rglob("*.yaml")]
    )
    for path in actions:
        steps = ((_workflow(path).get("runs") or {}).get("steps")) or []
        assert not [s for s in steps if _assumes_the_role(s)], path
    sources = [
        *WORKFLOWS.glob("*.yml"),
        *WORKFLOWS.glob("*.yaml"),
        *actions,
        *(p for p in (REPO_ROOT / "scripts").rglob("*") if p.is_file()),
    ]
    handed = 0
    for path in sources:
        text = path.read_text(errors="replace")
        assert not re.search(r"assume[-_]?role", text, re.I), path
        uses = re.findall(
            r"^\s*([\w-]+):\s*\$\{\{\s*secrets\.AWS_ROLE_ARN\s*\}\}", text, re.M
        )
        assert set(uses) <= {"role-to-assume"}, (path, uses)
        handed += len(uses)
    assert handed == 3, handed  # one per workflow above: the scan reads them


@pytest.mark.regression
@pytest.mark.parametrize(
    ("name", "minutes", "seconds"),
    [
        ("deploy.yml", 160, 9600),
        ("rollback.yml", 90, 5400),
        ("db-migrate.yml", 90, 5400),
    ],
)
def test_each_workflow_asks_for_a_session_as_long_as_its_job(name, minutes, seconds):
    jobs = [
        (job, step)
        for job in _workflow(WORKFLOWS / name)["jobs"].values()
        for step in job.get("steps") or []
        if _assumes_the_role(step)
    ]
    ((job, step),) = jobs
    assert job["timeout-minutes"] == minutes
    assert step["with"]["role-duration-seconds"] == seconds


@pytest.mark.regression
def test_the_trust_policy_admits_one_environments_jobs_exactly():
    text = TRUST.read_text()
    (statement,) = json.loads(text)["Statement"]
    assert statement["Effect"] == "Allow"
    assert statement["Action"] == "sts:AssumeRoleWithWebIdentity"
    assert statement["Principal"] == {
        "Federated": "arn:aws:iam::<AWS_ACCOUNT_ID>:oidc-provider/token.actions.githubusercontent.com"
    }
    assert statement["Condition"] == {
        "StringEquals": {
            "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
            "token.actions.githubusercontent.com:sub": SUBJECT,
        }
    }
    assert "*" not in text and "StringLike" not in text
    # One permissions policy, the same document for every environment's role.
    policies = sorted(p.name for p in POLICY.parent.glob("*policy*.json"))
    assert policies == [POLICY.name, TRUST.name], policies


# --- what the role may do ------------------------------------------------------


@pytest.mark.regression
def test_each_action_is_needed_by_exactly_these_calls(calls):
    assert calls == EXPECTED, {
        "not expected": {
            a: sorted(p) for a, p in calls.items() if EXPECTED.get(a) != p
        },
        "missing": sorted(set(EXPECTED) - set(calls)),
    }


@pytest.mark.regression
def test_the_generator_reads_the_workflows_their_actions_and_every_script_they_run(
    iam, monkeypatch, tmp_path
):
    sources = {str(p.relative_to(REPO_ROOT)) for p in iam.sources()}
    assert {DEPLOY, ROLLBACK, MIGRATE} <= sources
    actions = {
        str(p.relative_to(REPO_ROOT)) for p in (GITHUB / "actions").rglob("action.yml")
    }
    assert actions and actions <= sources, actions - sources
    # Every script a workflow or a local action runs is itself a source.
    run = []
    for source in sorted(s for s in sources if s.endswith(".yml")):
        document = yaml.safe_load((REPO_ROOT / source).read_text())
        jobs = (document.get("jobs") or {}).values()
        steps = [s for j in jobs for s in j.get("steps") or []] or (
            document.get("runs") or {}
        ).get("steps", [])
        for step in steps:
            for line in str(step.get("run", "")).splitlines():
                if not line.lstrip().startswith("#"):
                    run += re.findall(r"\bscripts/[A-Za-z0-9_./-]+\.(?:sh|py)\b", line)
    assert "scripts/run_migration_task.sh" in run and "scripts/shift_traffic.py" in run
    assert set(run) <= sources, sorted(set(run) - sources)
    # ... and so is the module a Python script imports from beside it.
    assert "scripts/api_serving.py" in sources
    # A staged script's calls are granted before any workflow runs it.
    staged = tmp_path / "staged.sh"
    staged.write_text("aws ecs stop-task --cluster c --task t\n")
    monkeypatch.setattr(iam, "STAGED_SCRIPTS", [staged])
    assert staged in iam.sources()
    assert "ecs:StopTask" in iam.calls()


@pytest.mark.regression
def test_the_committed_policy_is_what_the_generator_writes(iam, calls):
    """A workflow that gains a call fails here until the policy grants it."""
    assert POLICY.read_text() == json.dumps(iam.policy(calls), indent=2) + "\n"


@pytest.mark.regression
def test_resources_are_star_but_snapshot_tagging_and_passing_roles(generated):
    statements = _statements(generated)
    assert set(statements) == {
        "DeployWorkflows",
        "TagClusterSnapshots",
        "PassTaskRolesToEcs",
    }
    main = statements["DeployWorkflows"]
    assert main["Effect"] == "Allow" and main["Resource"] == "*"
    assert "Condition" not in main
    # The dashboard's rollout, and the calls that already reach further.
    for action in (
        "ecs:UpdateService",
        "ecs:RunTask",
        "ecs:RegisterTaskDefinition",
        "codedeploy:CreateDeployment",
    ):
        assert action in _actions(main), action
    tagging = statements["TagClusterSnapshots"]
    assert _actions(tagging) == ["rds:AddTagsToResource"]
    assert tagging["Resource"] == "arn:aws:rds:*:*:cluster-snapshot:*"
    assert "rds:AddTagsToResource" not in _actions(main)
    passing = statements["PassTaskRolesToEcs"]
    assert _actions(passing) == ["iam:PassRole"]
    assert passing["Resource"] == "*"
    assert passing["Condition"] == {
        "StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}
    }
    # No other IAM action anywhere: the role cannot create or change a role,
    # a user or a policy, and passes roles to ECS tasks only.
    every = [a for s in generated["Statement"] for a in _actions(s)]
    assert [a for a in every if a.startswith("iam:")] == ["iam:PassRole"]
    assert not [a for a in every if "*" in a], every


@pytest.mark.regression
def test_six_actions_come_from_what_a_call_implies(iam, calls):
    implied = {a for actions in iam.IMPLIED.values() for a in actions}
    flagged = {a for actions in iam.FLAG_IMPLIED.values() for a in actions}
    assert implied | flagged == IMPLIED_ACTIONS
    assert implied == IMPLIED_ACTIONS - {"codedeploy:UpdateDeploymentGroup"}
    for action in IMPLIED_ACTIONS:
        verb = re.sub(r"(?<!^)(?=[A-Z])", "-", action.split(":")[1]).lower()
        assert calls[action], action
        for place in calls[action]:
            # The call that needs it is never one named after it.
            assert not place.endswith(f" {verb}"), (action, place)


@pytest.mark.regression
def test_update_deployment_group_comes_from_the_alarm_override_flag_alone(iam, calls):
    assert iam.FLAG_IMPLIED == {
        ("deploy", "create-deployment", "--override-alarm-configuration"): [
            "codedeploy:UpdateDeploymentGroup"
        ]
    }
    for place in calls["codedeploy:UpdateDeploymentGroup"]:
        assert place.endswith(f"{CREATE} --override-alarm-configuration"), place
    for source in iam.sources():
        assert "update-deployment-group" not in source.read_text(), source
