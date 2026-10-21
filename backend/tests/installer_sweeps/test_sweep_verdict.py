"""Gate G3: the kept list is exact names (U10), and the verdict's classes (S6).

deploy/aws-installer/lib/sweep.sh's sweep_verdict sorts every line that differs
between the before and after sweeps into KEPT, LAG, NOT CHECKED or NOT EXPLAINED.
Against the run's measured residue it must say PENDING with nothing unexplained;
a stray role whose name merely starts like a service-linked role's, or the
runbook's canary, must come out not explained, counted exactly, and be left alone.
The kept list's static half (no pattern in a name) is
backend/tests/unit/infrastructure/test_installer_sweep_static.py.
"""

from __future__ import annotations

import pytest

from backend.tests.installer_sweeps.harness import accounts
from backend.tests.installer_sweeps.harness.world import DRIVER

pytestmark = pytest.mark.unit

ACCOUNT = accounts.ACCOUNT
VERDICT = "verdict {}: {} kept, {} lag, {} not checked, {} not explained\n"


@pytest.fixture
def sweeps(baseline, make_world):
    """A World holding copies of the baseline's before and after sweeps."""
    world = make_world(accounts.new_account())
    baseline.copy("before", world)
    baseline.copy("after", world)
    return world


def _add(world, label: str, file: str, *lines: str) -> None:
    path = world.root / "sweep" / label / file
    rows = path.read_text().splitlines() + list(lines)
    path.write_text("".join(f"{r}\n" for r in sorted(rows)))


def _drop(world, label: str, file: str, line: str) -> None:
    path = world.root / "sweep" / label / file
    rows = path.read_text().splitlines()
    rows.remove(line)
    path.write_text("".join(f"{r}\n" for r in rows))


def test_the_runs_residue_is_pending_and_fully_explained(baseline, make_world):
    """The run's after sweep (sweep/diff-before-after.txt): 26 kept lines, 10
    tagging-index lines, nothing else."""
    world = make_world(accounts.new_account())
    before, after = baseline.path("before"), baseline.path("after")
    result = world.run(
        str(DRIVER),
        "sweep_verdict",
        str(before),
        str(after),
        ACCOUNT,
        str(world.root / "verdict"),
    )
    assert (result.code, result.err) == (0, ""), result
    assert result.out == VERDICT.format("PENDING", 26, 10, 0, 0)
    report = world.report()
    assert report["not-explained"] == [] and report["not-checked"] == []
    kept = {}
    for row in report["kept"]:
        kept.setdefault(row.split("\t", 1)[0], []).append(row)
    assert {f: len(rows) for f, rows in kept.items()} == {
        "global/iam-roles.txt": 5,
        "us-west-2/dynamodb-backups.txt": 3,
        "us-west-2/ecs-td-delete-in-progress.txt": 4,
        "us-west-2/ecs-td-families-inactive.txt": 3,
        "us-west-2/kms-aliases.txt": 5,  # four added, and alias/aws/s3's "null" gone
        "us-west-2/kms-keys.txt": 5,
        "us-west-2/sns-subscriptions.txt": 1,
    }
    assert "us-west-2/kms-aliases.txt\t-\talias/aws/s3\tnull" in report["kept"]
    assert all(
        row.startswith("us-west-2/tagged-resources.txt\t+\t") for row in report["lag"]
    )


def test_no_difference_is_clean(sweeps):
    done = sweeps.verdict("before", "before")
    assert (done.code, done.out) == (0, VERDICT.format("CLEAN", 0, 0, 0, 0))


@pytest.mark.parametrize(
    "file, line",
    [
        (
            "global/iam-roles.txt",
            "/aws-service-role/ecs.amazonaws.com/\tAWSServiceRoleForECSx",
        ),
        (
            "global/iam-roles.txt",
            "/aws-service-role/ecs.amazonaws.com/\tAWSServiceRoleForECS2",
        ),
        (
            "global/iam-roles.txt",
            "/\tAWSServiceRoleForECS",
        ),  # the right name, the wrong path
        ("us-west-2/ssm-parameters.txt", "/docs-run/sweep-canary"),
        (
            "us-west-2/ecs-td-delete-in-progress.txt",
            f"arn:aws:ecs:us-west-2:{ACCOUNT}:task-definition/experimentation-backend-prod:1",
        ),
        (
            "us-west-2/ecs-td-delete-in-progress.txt",
            "arn:aws:ecs:us-west-2:123456789012:task-definition/experimentation-backend-staging:1",
        ),
        (
            "us-west-2/ecs-td-delete-in-progress.txt",
            f"arn:aws:ecs:us-west-2:{ACCOUNT}:task-definition/experimentation-backend-staging:latest",
        ),
        ("us-west-2/ecs-td-families-inactive.txt", "experimentation-backend-prod"),
        (
            "us-west-2/dynamodb-backups.txt",
            "experimentation-events-staging\tUSER\tAVAILABLE",
        ),
        (
            "us-west-2/dynamodb-backups.txt",
            "experimentation-events-prod\tSYSTEM\tAVAILABLE",
        ),
        (
            "us-west-2/kms-keys.txt",
            "0a1b2c3d-0000-4000-8000-0123456789ab\tCUSTOMER\tEnabled\t"
            "KMS key for experimentation-database-staging PostgreSQL encryption",
        ),
        (
            "us-west-2/kms-keys.txt",
            "0a1b2c3d-0000-4000-8000-0123456789ab\tCUSTOMER\tPendingDeletion\tsomeone else's key",
        ),
        (
            "us-east-1/kms-keys.txt",
            "0a1b2c3d-0000-4000-8000-0123456789ab\tCUSTOMER\tPendingDeletion\t"
            "KMS key for experimentation-database-staging PostgreSQL encryption",
        ),
        (
            "us-west-2/kms-aliases.txt",
            "alias/experimentation-database-staging-postgres-key\tx",
        ),
        (
            "us-west-2/sns-subscriptions.txt",
            f"arn:aws:sns:us-west-2:{ACCOUNT}:experimentation-alerts-prod\temail\tPendingConfirmation",
        ),
        ("us-west-2/cfn-stacks.txt", "CDKToolkit\tCREATE_COMPLETE"),
    ],
    ids=lambda v: v.rsplit("\t", 1)[-1][-40:] if isinstance(v, str) else v,
)
def test_anything_off_the_list_is_not_explained_and_counted(sweeps, file, line):
    _add(sweeps, "after", file, line)
    done = sweeps.verdict("before", "after")
    assert done.code == 1
    assert done.out == VERDICT.format("RESIDUE", 26, 10, 0, 1)
    assert sweeps.report()["not-explained"] == [f"{file}\t+\t{line}"]


def test_a_stray_role_and_the_runbooks_canary_are_counted_exactly(sweeps):
    arn = f"arn:aws:ssm:us-west-2:{ACCOUNT}:parameter/docs-run/sweep-canary"
    _add(
        sweeps,
        "after",
        "global/iam-roles.txt",
        "/aws-service-role/ecs.amazonaws.com/\tAWSServiceRoleForECSx",
    )
    _add(sweeps, "after", "us-west-2/ssm-parameters.txt", "/docs-run/sweep-canary")
    _add(sweeps, "after", "us-west-2/tagged-resources.txt", arn)
    done = sweeps.verdict("before", "after")
    assert done.out == VERDICT.format("RESIDUE", 26, 11, 0, 2)
    assert sweeps.report()["not-explained"] == [
        "global/iam-roles.txt\t+\t/aws-service-role/ecs.amazonaws.com/\tAWSServiceRoleForECSx",
        "us-west-2/ssm-parameters.txt\t+\t/docs-run/sweep-canary",
    ]


def test_something_gone_since_the_before_sweep_is_not_explained(sweeps):
    gone = "/aws-service-role/support.amazonaws.com/\tAWSServiceRoleForSupport"
    _drop(sweeps, "after", "global/iam-roles.txt", gone)
    done = sweeps.verdict("before", "after")
    assert done.out == VERDICT.format("RESIDUE", 26, 10, 0, 1)
    assert sweeps.report()["not-explained"] == [f"global/iam-roles.txt\t-\t{gone}"]


@pytest.mark.parametrize("label", ["before", "after"])
def test_a_failed_read_in_either_sweep_is_not_checked(sweeps, label):
    path = sweeps.root / "sweep" / label / "us-west-2" / "ec2-vpcs.txt"
    path.write_text(
        "READ FAILED: aws ec2 describe-vpcs --region us-west-2\n\nAn error occurred\n"
    )
    done = sweeps.verdict("before", "after")
    assert done.code == 1
    assert done.out == VERDICT.format("INCOMPLETE", 26, 10, 1, 0)
    assert sweeps.report()["not-checked"] == ["us-west-2/ec2-vpcs.txt\t!\tREAD FAILED"]


def test_a_type_only_one_sweep_has_is_not_checked(sweeps):
    (sweeps.root / "sweep" / "after" / "us-west-2" / "ec2-vpcs.txt").unlink()
    (sweeps.root / "sweep" / "after" / "us-west-2" / "new-type.txt").write_text("x\n")
    done = sweeps.verdict("before", "after")
    assert done.out == VERDICT.format("INCOMPLETE", 26, 10, 2, 0)
    assert sweeps.report()["not-checked"] == [
        "us-west-2/ec2-vpcs.txt\t!\tonly in the before sweep",
        "us-west-2/new-type.txt\t!\tonly in the after sweep",
    ]


def test_residue_outranks_incomplete(sweeps):
    _add(sweeps, "after", "us-west-2/ssm-parameters.txt", "/docs-run/sweep-canary")
    (sweeps.root / "sweep" / "after" / "us-west-2" / "ec2-vpcs.txt").write_text(
        "READ FAILED: aws x\n"
    )
    done = sweeps.verdict("before", "after")
    assert (done.code, done.out) == (1, VERDICT.format("RESIDUE", 26, 10, 1, 1))


def test_also_kept_lines_are_exact(sweeps):
    """Uninstall can name further lines to keep (CDKToolkit and its bucket, under
    D14's fallback); a line is kept only if it is one of them exactly."""
    stack = "CDKToolkit\tCREATE_COMPLETE"
    bucket = f"cdk-hnb659fds-assets-{ACCOUNT}-us-west-2"
    _add(sweeps, "after", "us-west-2/cfn-stacks.txt", stack)
    _add(sweeps, "after", "global/s3-buckets.txt", bucket, f"{bucket}-copy")
    also = sweeps.root / "also-kept.txt"
    also.write_text(
        f"us-west-2/cfn-stacks.txt\t+\t{stack}\n"
        f"global/s3-buckets.txt\t+\t{bucket}\n"
        f"global/s3-buckets.txt\t+\tcdk-hnb659fds-assets-*\n"
    )
    done = sweeps.verdict("before", "after", also=also)
    assert done.out == VERDICT.format("RESIDUE", 28, 10, 0, 1)
    assert sweeps.report()["not-explained"] == [
        f"global/s3-buckets.txt\t+\t{bucket}-copy"
    ]


def test_a_used_report_folder_or_a_missing_sweep_is_refused(sweeps):
    (sweeps.root / "verdict").mkdir()
    used = sweeps.verdict("before", "after")
    assert used.code == 2 and "exists: use a new folder" in used.err
    missing = sweeps.verdict("before", "nowhere", report="verdict2")
    assert missing.code == 2 and "must both be sweeps" in missing.err
