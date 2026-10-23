"""Gate G3: the kept list is exact names, and the verdict's classes.

deploy/aws-installer/lib/sweep.sh's sweep_verdict sorts every line that differs
between the before and after sweeps into KEPT, LAG, NOT CHECKED or NOT EXPLAINED.
Against the run's measured residue it must say PENDING with nothing unexplained;
a stray role whose name merely starts like a service-linked role's, or the
run's canary, must come out not explained, counted exactly, and be left alone.
The kept list's static half (no pattern in a name) is
backend/tests/unit/infrastructure/test_installer_sweep_static.py.
"""

from __future__ import annotations

import os
import re

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
    """Uninstall can name further lines to keep (CDKToolkit and its bucket, when it
    keeps them); a line is kept only if it is one of them exactly."""
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


# --- what AWS finishes removing, between one install's uninstall and the next's ---------


@pytest.fixture
def again(baseline, make_world):
    """A second install's two sweeps: its before sweep holds the first uninstall's
    residue (the run's), and the after sweep starts as a copy of it."""
    world = make_world(accounts.new_account())
    baseline.copy("after", world, "before")
    baseline.copy("after", world, "after")
    return world


def _line(world, label: str, file: str, part: str) -> str:
    (found,) = [
        x
        for x in (world.root / "sweep" / label / file).read_text().splitlines()
        if part in x
    ]
    return found


REVISION = (
    f"arn:aws:ecs:us-west-2:{ACCOUNT}:task-definition/experimentation-backend-staging:"
)


def test_a_second_install_keeps_what_aws_finished_removing(again):
    """Recovering by uninstalling and installing again: by the second uninstall
    ECS has finished deleting the first install's revision, and the second
    install left one of its own. Both are the families' revisions."""
    _drop(again, "after", "us-west-2/ecs-td-delete-in-progress.txt", REVISION + "1")
    _add(again, "after", "us-west-2/ecs-td-delete-in-progress.txt", REVISION + "2")
    done = again.verdict("before", "after")
    assert (done.code, done.out) == (0, VERDICT.format("CLEAN", 2, 0, 0, 0)), done.out
    assert again.report()["kept"] == [
        f"us-west-2/ecs-td-delete-in-progress.txt\t-\t{REVISION}1",
        f"us-west-2/ecs-td-delete-in-progress.txt\t+\t{REVISION}2",
    ]


@pytest.mark.parametrize(
    "file, part",
    [
        (
            "us-west-2/ecs-td-delete-in-progress.txt",
            "experimentation-dashboard-staging:1",
        ),
        ("us-west-2/ecs-td-families-inactive.txt", "experimentation-dashboard-staging"),
        ("us-west-2/dynamodb-backups.txt", "experimentation-events-staging\tSYSTEM"),
        ("us-west-2/sns-subscriptions.txt", "experimentation-alerts-staging"),
        ("us-west-2/kms-keys.txt", "CUSTOMER\tPendingDeletion"),
    ],
    ids=["revision", "family", "system-backup", "subscription", "database-key"],
)
def test_what_aws_removes_by_itself_is_kept_when_it_goes(again, file, part):
    """Gone (-) since the before sweep, and the same exact name the kept list
    takes when it appears: AWS removed it, as it does after its window."""
    line = _line(again, "after", file, part)
    _drop(again, "after", file, line)
    done = again.verdict("before", "after")
    assert (done.code, done.out) == (0, VERDICT.format("CLEAN", 1, 0, 0, 0)), done.out
    assert again.report()["kept"] == [f"{file}\t-\t{line}"]


@pytest.mark.parametrize(
    "file, line",
    [
        (
            "us-west-2/ecs-td-delete-in-progress.txt",
            f"arn:aws:ecs:us-west-2:{ACCOUNT}:task-definition/experimentation-backend-prod:1",
        ),
        ("us-west-2/ecs-td-families-inactive.txt", "experimentation-backend-prod"),
        (
            "us-west-2/dynamodb-backups.txt",
            "experimentation-events-staging\tUSER\tAVAILABLE",
        ),
        (
            "us-west-2/sns-subscriptions.txt",
            f"arn:aws:sns:us-west-2:{ACCOUNT}:experimentation-alerts-prod\temail\tPendingConfirmation",
        ),
        (
            "us-west-2/kms-keys.txt",
            "0a1b2c3d-0000-4000-8000-0123456789ab\tCUSTOMER\tEnabled\t"
            "KMS key for experimentation-database-staging PostgreSQL encryption",
        ),
    ],
    ids=[
        "another-family",
        "another-family-name",
        "user-backup",
        "another-topic",
        "an-enabled-key",
    ],
)
def test_something_else_gone_is_not_explained(again, file, line):
    """Gone (-) but not on the kept list: someone removed it."""
    _add(again, "before", file, line)
    done = again.verdict("before", "after")
    assert (done.code, done.out) == (1, VERDICT.format("RESIDUE", 0, 0, 0, 1)), done.out
    assert again.report()["not-explained"] == [f"{file}\t-\t{line}"]


def test_a_service_linked_role_that_goes_is_not_explained(again):
    """A service-linked role is kept when it appears; gone, someone removed it."""
    role = "/aws-service-role/ecs.amazonaws.com/\tAWSServiceRoleForECS"
    _drop(again, "after", "global/iam-roles.txt", role)
    done = again.verdict("before", "after")
    assert (done.code, done.out) == (1, VERDICT.format("RESIDUE", 0, 0, 0, 1)), done.out
    assert again.report()["not-explained"] == [f"global/iam-roles.txt\t-\t{role}"]


# --- a verdict that cannot write its report gives none ------------------------------------


def _verdict_with(world, override: str, report: str):
    """sweep_verdict with one of its helpers replaced, called as a caller inside a
    condition would (set -e off), its stdout and exit status."""
    import subprocess

    from backend.tests.installer_sweeps.harness.world import LIB

    script = (
        f'. "{LIB}/names.sh"; . "{LIB}/sweep.sh"; {override}\n'
        'if sweep_verdict "$1" "$2" "$3" "$4"; then rc=0; else rc=$?; fi; exit $rc'
    )
    sweep = world.root / "sweep"
    done = subprocess.run(
        [
            "bash",
            "-c",
            script,
            "verdict",
            str(sweep / "before"),
            str(sweep / "after"),
            ACCOUNT,
            str(world.root / report),
        ],
        capture_output=True,
        text=True,
    )
    return done


def _fails_for(text: str) -> str:
    """A _sweep_put that loses every line holding ``text``."""
    return (
        f'_sweep_put() {{ case "$2" in *"{text}"*) return 1 ;; esac; '
        'printf "%s\\n" "$2" >> "$1"; }'
    )


#: A file whose read failed in the after sweep, one only the before sweep holds,
#: and one the verdict cannot read, beside the canary and a file only the after
#: sweep holds: every kind of line the verdict writes.
READ_FAILED_FILE = "us-west-2/backup-vaults.txt"
UNREADABLE_FILE = "us-west-2/sns-topics.txt"


@pytest.fixture
def every_kind(sweeps):
    after = sweeps.root / "sweep" / "after"
    before = sweeps.root / "sweep" / "before"
    _add(sweeps, "after", "us-west-2/ssm-parameters.txt", "/docs-run/sweep-canary")
    (after / "us-west-2" / "only-after.txt").write_text("x\n")
    (before / "us-west-2" / "only-before.txt").write_text("y\n")
    (after / READ_FAILED_FILE).write_text(
        "READ FAILED: aws backup list-backup-vaults --region us-west-2\n"
    )
    unreadable = before / UNREADABLE_FILE
    unreadable.chmod(0)
    assert not os.access(unreadable, os.R_OK), "run as a user file modes bind"
    yield sweeps
    unreadable.chmod(0o600)


@pytest.mark.parametrize(
    "override",
    [
        "_sweep_put() { return 1; }",
        _fails_for("/docs-run/sweep-canary"),
        _fails_for("only in the after sweep"),
        _fails_for("only in the before sweep"),
        _fails_for("READ FAILED"),
        _fails_for("could not be compared"),
        _fails_for("AWSServiceRoleForECS"),
        # The tab after the file name: a lag line, never the listing's file path.
        _fails_for("tagged-resources.txt\t+"),
        "_sweep_empty() { return 1; }",
        "wc() { printf 'not a number\\n'; }",
        "wc() { return 1; }",
    ],
    ids=[
        "every-line",
        "the-unexplained-line",
        "a-file-only-after",
        "a-file-only-before",
        "a-failed-read",
        "a-file-that-cannot-be-compared",
        "a-kept-line",
        "a-lag-line",
        "an-empty-class-file",
        "a-count-that-is-not-a-number",
        "a-count-that-fails",
    ],
)
def test_a_report_that_cannot_be_written_gives_no_verdict(every_kind, override):
    """Called inside a condition, where set -e is off, a lost write could turn
    RESIDUE into CLEAN. Each write is checked instead: exit 2, no verdict. Each
    case loses the lines of one kind, and the control run writes every kind."""
    control = _verdict_with(every_kind, ":", "control")
    assert (control.returncode, control.stdout) == (
        1,
        VERDICT.format("RESIDUE", 26, 10, 4, 1),
    ), control.stderr
    done = _verdict_with(every_kind, override, "verdict")
    assert done.returncode == 2, (done.stdout, done.stderr)
    assert "verdict " not in done.stdout, done.stdout
    assert "so there is no verdict" in done.stderr, done.stderr


def test_every_verdict_write_is_checked():
    """Read as text: each _sweep_put and _sweep_empty call in lib/sweep.sh ends in
    `|| return 1` or is the condition of an `if !`, so a lost write can never pass
    unseen, whichever line it is."""
    from backend.tests.installer_sweeps.harness.world import LIB

    unchecked = []
    for number, line in enumerate((LIB / "sweep.sh").read_text().splitlines(), 1):
        code = line.split(" #", 1)[0].strip()
        if code.startswith("#") or not re.search(r"\b_sweep_(put|empty) ", code):
            continue
        if code.startswith(("_sweep_put()", "_sweep_empty()")):
            continue
        if code.endswith("|| return 1") or re.match(
            r"^if ! _sweep_(put|empty) .*; then$", code
        ):
            continue
        unchecked.append(f"lib/sweep.sh:{number}: {code}")
    assert unchecked == [], "\n".join(unchecked)
