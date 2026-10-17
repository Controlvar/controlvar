"""The workflow role's permissions follow the workflows (#140, PE v1 C12).

`scripts/iam_actions.py` derives the IAM actions from every `aws <service>
<verb>` in deploy.yml, rollback.yml, db-migrate.yml, the local actions and the
scripts they run. The committed policy and the documented table must be what it
generates, so a workflow that gains a call fails here until the role is given
it -- instead of failing, mid-deploy, with AccessDenied.

The trust policy is pinned to GitHub's immutable subject for this repository
and one environment (PE v1 C5): the old one allowed `repo:<name>:*`, i.e. any
branch, any pull request workflow, and a repository nobody owns any more.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = REPO_ROOT / "scripts" / "iam_actions.py"
TRUST = REPO_ROOT / "infrastructure" / "cdk" / "github-actions-trust-policy.json"
POLICY = REPO_ROOT / "infrastructure" / "cdk" / "github-actions-deploy-policy.json"
DOC = REPO_ROOT / "docs" / "deployment" / "iam-permissions.md"
IMMUTABLE_SUBJECT = "repo:Controlvar@341280474/controlvar@1367480368"

pytestmark = pytest.mark.skipif(
    not (REPO_ROOT / ".github" / "workflows").is_dir(),
    reason="this tree has no .github/workflows",
)


def _module():
    spec = importlib.util.spec_from_file_location("iam_actions", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.regression
def test_the_committed_policy_and_table_are_what_the_workflows_need():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.regression
def test_every_aws_call_is_granted():
    """Read the other way round: each call in the sources, against the policy."""
    iam = _module()
    granted = set()
    for statement in json.loads(POLICY.read_text())["Statement"]:
        actions = statement["Action"]
        granted |= set([actions] if isinstance(actions, str) else actions)
    found = iam.calls()
    # Exactly the granted set, both ways: a call the role lacks, and an action
    # the role holds that no call needs, each fail with the difference.
    assert set(found) == granted, {
        "called but not granted": sorted(set(found) - granted),
        "granted but never called": sorted(granted - set(found)),
    }
    # Not vacuous: the calls every deploy makes are among them.
    assert {
        "ecs:RegisterTaskDefinition",
        "ecs:RunTask",
        "codedeploy:CreateDeployment",
        "rds:CreateDBClusterSnapshot",
        "cloudformation:DescribeStacks",
        "iam:PassRole",
    } <= set(found)
    # The sources include the scripts the workflows run, not only the YAML.
    places = {p.split(":")[0] for ps in found.values() for p in ps}
    assert "scripts/run_migration_task.sh" in places
    # The dashboard's rollout script, which deploy.yml and rollback.yml run.
    assert "scripts/ecs_rolling_rollout.sh" in places
    assert "scripts/dashboard_revision.py" in {
        str(p.relative_to(REPO_ROOT)) for p in iam.sources()
    }
    assert "ecs:UpdateService" in found
    assert ".github/actions/stack-outputs/action.yml" in places


def test_a_staged_script_is_not_already_run_by_a_workflow(monkeypatch):
    """STAGED_SCRIPTS is for scripts no workflow or action names YET.

    Once one is wired in, the workflow is what grants it and the entry must be
    removed, or a script later unwired would stay granted by a list nobody
    reads. ecs_rolling_rollout.sh was the first: re-adding it now fails here.
    """
    iam = _module()
    staged = list(iam.STAGED_SCRIPTS)
    assert all(p.is_file() for p in staged), staged
    monkeypatch.setattr(iam, "STAGED_SCRIPTS", [])
    wired = set(iam.sources())
    stale = [str(p.relative_to(REPO_ROOT)) for p in staged if p in wired]
    assert not stale, (
        f"{stale} are run by a workflow or action now: remove them from "
        "STAGED_SCRIPTS in scripts/iam_actions.py"
    )


@pytest.mark.regression
@pytest.mark.parametrize("suffix", [".sh", ".yml"])
def test_iam_scan_sees_a_line_continued_call(tmp_path, suffix):
    """`aws ecs \\` + `update-service ...` on the next line is one call.

    The scan used to read a line at a time, so the split form -- the usual
    shape of a long call -- was invisible and `--check` stayed at exit 0 with
    the action missing from the role.
    """
    iam = _module()
    script = "set -euo pipefail\naws ecs \\\n  update-service --cluster c --service s\n"
    source = tmp_path / f"probe{suffix}"
    if suffix == ".yml":
        source.write_text(yaml.safe_dump({"jobs": {"j": {"steps": [{"run": script}]}}}))
    else:
        source.write_text(script)
    found = iam.calls([source])
    assert "ecs:UpdateService" in found, dict(found)


def test_an_unmapped_service_is_refused():
    iam = _module()
    with pytest.raises(SystemExit, match="not mapped to an IAM prefix"):
        iam.iam_actions_for("iot", "create-thing")
    assert iam.iam_actions_for("rds", "describe-db-cluster-snapshots") == [
        "rds:DescribeDBClusterSnapshots"
    ]


def test_pass_role_is_only_to_ecs_tasks():
    statements = json.loads(POLICY.read_text())["Statement"]
    (passing,) = [s for s in statements if "iam:PassRole" in str(s["Action"])]
    assert passing["Action"] == "iam:PassRole"
    assert passing["Condition"] == {
        "StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}
    }


@pytest.mark.regression
def test_the_trust_policy_pins_the_immutable_subject_and_one_environment():
    (statement,) = json.loads(TRUST.read_text())["Statement"]
    assert statement["Action"] == "sts:AssumeRoleWithWebIdentity"
    condition = statement["Condition"]
    assert set(condition) == {"StringEquals"}, (
        "StringLike admits a wildcard; the subject is pinned exactly"
    )
    assert condition["StringEquals"] == {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
        "token.actions.githubusercontent.com:sub": (
            f"{IMMUTABLE_SUBJECT}:environment:<ENVIRONMENT>"
        ),
    }
    assert "*" not in TRUST.read_text()
    assert "amarkanday" not in TRUST.read_text()


def test_the_old_policies_are_gone():
    cdk = REPO_ROOT / "infrastructure" / "cdk"
    assert not (cdk / "cicd-deployment-policy.json").exists()
    assert not (cdk / "gitActions").exists()


def test_the_doc_says_where_the_subject_comes_from():
    text = DOC.read_text()
    assert f"{IMMUTABLE_SUBJECT}:environment:<ENVIRONMENT>" in text
    assert "oidc-sub-probe.yml" in text
    assert "decode a real token" in text.lower()
    for identity in ("bootstrap identity", "`cdk deploy` identity", "workflow role"):
        assert identity in text.lower() or identity in text, identity
    assert "sts:AssumeRole" in text and "cdk-hnb659fds-*" in text


@pytest.mark.regression
def test_iam_scan_follows_a_scripts_sibling_imports(tmp_path, monkeypatch):
    """shift_traffic.py reaches AWS through api_serving.py and
    check_live_target_group.py. A module a workflow script imports is scanned
    too, so its calls are in the role even when no workflow names its file."""
    iam = _module()
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "entry.py").write_text("import helper\n")
    (scripts / "helper.py").write_text('OPS = {("elbv2", "describe-rules")}\n')
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    workflow = workflows / "deploy.yml"
    workflow.write_text(
        yaml.safe_dump(
            {"jobs": {"j": {"steps": [{"run": "python3 scripts/entry.py"}]}}}
        )
    )
    monkeypatch.setattr(iam, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(iam, "WORKFLOWS", [workflow])
    monkeypatch.setattr(iam, "ACTIONS_DIR", tmp_path / ".github" / "actions")
    assert scripts / "helper.py" in iam.sources()
    assert "elasticloadbalancing:DescribeRules" in iam.calls()


def test_the_real_forward_deploy_scripts_are_scanned():
    places = {p.split(":")[0] for ps in _module().calls().values() for p in ps}
    for script in (
        "scripts/shift_traffic.py",
        "scripts/refuse_active_deployment.py",
        "scripts/check_live_target_group.py",
    ):
        assert script in places, script


# --- flags that need an action of their own (#148) --------------------------------


def _probe(tmp_path, suffix: str, script: str) -> Path:
    source = tmp_path / f"probe{suffix}"
    if suffix == ".yml":
        source.write_text(yaml.safe_dump({"jobs": {"j": {"steps": [{"run": script}]}}}))
    else:
        source.write_text(script)
    return source


@pytest.mark.regression
@pytest.mark.parametrize("suffix", [".sh", ".yml"])
@pytest.mark.parametrize(
    "script",
    [
        # rollback.yml's form: the flag on the call itself, line-continued.
        "aws deploy create-deployment \\\n"
        "  --application-name a --deployment-group-name g \\\n"
        '  --override-alarm-configuration \'{"enabled":false,"ignorePollAlarmFailure":false,"alarms":[]}\' \\\n'
        "  --query deploymentId --output text\n",
        # deploy.yml's break-glass form: the flag in an array the call expands.
        "override=()\n"
        'if [ "$OVERRIDE" = true ]; then\n'
        '  override=(--override-alarm-configuration \'{"enabled":false,"ignorePollAlarmFailure":false,"alarms":[]}\')\n'
        "fi\n"
        'aws deploy create-deployment --application-name a "${override[@]}"\n',
    ],
    ids=["on-the-call", "through-an-array"],
)
def test_an_alarm_override_implies_update_deployment_group(tmp_path, suffix, script):
    """QA P7b: the generator read verbs, not flags. With the override added to a
    create-deployment it stayed at "35 actions ... current" while the role
    lacked the permission CreateDeployment then needs (executed on main)."""
    found = _module().calls([_probe(tmp_path, suffix, script)])
    assert "codedeploy:CreateDeployment" in found, dict(found)
    assert "codedeploy:UpdateDeploymentGroup" in found, dict(found)


@pytest.mark.parametrize(
    "script",
    [
        "aws deploy create-deployment --application-name a\n",
        # The flag with no create-deployment in the block implies nothing.
        "echo --override-alarm-configuration\n",
        # Neither does a different flag that merely starts the same way.
        "aws deploy create-deployment --override-alarm-configuration-x y\n",
    ],
    ids=["no-flag", "no-call", "other-flag"],
)
def test_no_override_implies_no_update_deployment_group(tmp_path, script):
    found = _module().calls([_probe(tmp_path, ".sh", script)])
    assert "codedeploy:UpdateDeploymentGroup" not in found, dict(found)


@pytest.mark.regression
def test_the_real_override_is_granted_and_attributed():
    """Both workflows that pass the override are what grants it."""
    found = _module().calls()
    assert "codedeploy:UpdateDeploymentGroup" in found
    assert {p.split(":")[0] for p in found["codedeploy:UpdateDeploymentGroup"]} == {
        ".github/workflows/deploy.yml",
        ".github/workflows/rollback.yml",
    }, found["codedeploy:UpdateDeploymentGroup"]


# --- the alarm pre-flight (#148 PR-3, #297) ------------------------------------


@pytest.mark.regression
def test_the_alarm_preflight_reads_are_granted_and_attributed():
    """EM condition 11: the pre-flight's two reads are in the generated policy,
    each needed by scripts/refuse_alarm_active.py and by nothing else."""
    found = _module().calls()
    for action in ("codedeploy:GetDeploymentGroup", "cloudwatch:DescribeAlarms"):
        assert action in found, sorted(found)
        assert {p.split(":")[0] for p in found[action]} == {
            "scripts/refuse_alarm_active.py"
        }, found[action]
    granted = json.loads(POLICY.read_text())["Statement"][0]["Action"]
    assert "codedeploy:GetDeploymentGroup" in granted
    assert "cloudwatch:DescribeAlarms" in granted


@pytest.mark.regression
def test_an_unmapped_service_in_operations_fails_generation(tmp_path):
    """The tuple scan skips a pair whose service is not mapped (scripts hold
    other string pairs), so `("cloudwatch", "describe-alarms")` was dropped
    from the role in silence until `cloudwatch` was mapped. A service in a
    script's OPERATIONS allow-list must be mapped, or generation fails."""
    iam = _module()
    script = tmp_path / "probe.py"
    script.write_text(
        'OPERATIONS = frozenset({("deploy", "get-deployment"), ("sqs", "send-message")})\n'
        'PAIRS = ("blue", "green")\n'
    )
    with pytest.raises(SystemExit, match="'sqs' is not mapped"):
        iam.calls([script])
    # Pairs outside OPERATIONS are still ignored.
    script.write_text('PAIRS = ("blue", "green")\n')
    assert dict(iam.calls([script])) == {}


# --- actions AWS checks behind a call (#744) -----------------------------------


@pytest.mark.regression
@pytest.mark.parametrize("suffix", [".sh", ".yml"])
def test_a_cluster_snapshot_implies_tagging_it(tmp_path, suffix):
    """The CDK's Aurora cluster copies its tags to each snapshot, and AWS
    authorizes the copy as rds:AddTagsToResource. No command names it, so the
    scan alone left it out and the first staging deploy was refused at the
    pre-migration snapshot."""
    iam = _module()
    script = (
        "aws rds create-db-cluster-snapshot \\\n"
        '  --db-cluster-identifier c --db-cluster-snapshot-identifier "$id"\n'
    )
    source = tmp_path / f"probe{suffix}"
    if suffix == ".yml":
        source.write_text(yaml.safe_dump({"jobs": {"j": {"steps": [{"run": script}]}}}))
    else:
        source.write_text(script)
    found = iam.calls([source])
    assert {"rds:CreateDBClusterSnapshot", "rds:AddTagsToResource"} <= set(found), dict(
        found
    )


@pytest.mark.regression
def test_the_snapshot_tagging_is_granted_on_cluster_snapshots_only():
    """Both snapshot steps are what grants it, and the committed policy holds
    it in a statement of its own, on cluster snapshots, not on `*`."""
    found = _module().calls()
    assert {p.split(":")[0] for p in found["rds:AddTagsToResource"]} == {
        ".github/workflows/deploy.yml",
        ".github/workflows/db-migrate.yml",
    }, found["rds:AddTagsToResource"]
    statements = json.loads(POLICY.read_text())["Statement"]
    tagging = [s for s in statements if "rds:AddTagsToResource" in str(s["Action"])]
    assert tagging == [
        {
            "Sid": "TagClusterSnapshots",
            "Effect": "Allow",
            "Action": "rds:AddTagsToResource",
            "Resource": "arn:aws:rds:*:*:cluster-snapshot:*",
        }
    ], tagging


@pytest.mark.regression
def test_dropping_an_implied_action_makes_the_policy_stale(monkeypatch):
    """The IMPLIED table is the only way such an action enters the role, so
    losing an entry must fail the currency check rather than shrink the role
    in silence."""
    iam = _module()
    committed = json.loads(POLICY.read_text())
    assert iam.policy(iam.calls()) == committed
    implied = {
        k: v
        for k, v in iam.IMPLIED.items()
        if k != ("rds", "create-db-cluster-snapshot")
    }
    monkeypatch.setattr(iam, "IMPLIED", implied)
    without = iam.policy(iam.calls())
    assert without != committed
    assert "rds:AddTagsToResource" not in json.dumps(without)


# --- the revision lookup behind a rollback's create-deployment (#754) ---------


@pytest.mark.regression
def test_a_deployment_implies_looking_up_its_revision(tmp_path):
    """A rollback's create-deployment names a revision deployed before, and
    CodeDeploy then checks codedeploy:GetApplicationRevision. With only
    RegisterApplicationRevision granted, the staging rollback rehearsal was
    refused at "Roll back via CodeDeploy" after it had stopped the in-flight
    deployment."""
    iam = _module()
    script = tmp_path / "probe.sh"
    script.write_text(
        "aws deploy create-deployment --application-name a \\\n"
        '  --deployment-group-name g --revision "$rev"\n'
    )
    found = set(iam.calls([script]))
    assert {
        "codedeploy:CreateDeployment",
        "codedeploy:GetApplicationRevision",
        "codedeploy:RegisterApplicationRevision",
    } <= found, found


@pytest.mark.regression
def test_the_committed_policy_grants_the_revision_lookup():
    """Both workflows that create a deployment are what grant it, and the
    committed policy holds it with the other deploy actions."""
    found = _module().calls()
    assert {p.split(":")[0] for p in found["codedeploy:GetApplicationRevision"]} == {
        ".github/workflows/deploy.yml",
        ".github/workflows/rollback.yml",
    }, found["codedeploy:GetApplicationRevision"]
    statements = json.loads(POLICY.read_text())["Statement"]
    granted = [
        s for s in statements if "codedeploy:GetApplicationRevision" in s["Action"]
    ]
    assert [s["Sid"] for s in granted] == ["DeployWorkflows"], granted
    assert granted[0]["Resource"] == "*"
