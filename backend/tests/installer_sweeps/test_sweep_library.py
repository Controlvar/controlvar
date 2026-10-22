"""Gate G1: the sweep library sees things, and only reads.

deploy/aws-installer/lib/sweep.sh, tested alone against the fake (install's and
uninstall's fault tests stub it): exactly the 126 files of the closed list the AWS
guide's end-to-end run swept; reads equal files plus items; a failed read is counted
and fails the sweep; a canary is exactly one new line; and a sweep of an installed
account shows new lines for every type the install creates (the fake's half of the
run's mid sweep). The static half (list, describe and get operations only) is
backend/tests/unit/infrastructure/test_installer_sweep_static.py.
"""

from __future__ import annotations

import os

import pytest

from backend.tests.installer_sweeps.harness import accounts
from backend.tests.installer_sweeps.harness.world import BASHES, bash_id

pytestmark = pytest.mark.unit

ACCOUNT = accounts.ACCOUNT

#: The closed list's types, in each of us-west-2 and us-east-1 (55 read once, 3 read
#: per item: ecs-services, kms-keys, backup-recovery-points).
REGIONAL = (
    "cfn-stacks",
    "ec2-vpcs",
    "ec2-subnets",
    "ec2-route-tables",
    "ec2-internet-gateways",
    "ec2-nat-gateways",
    "ec2-addresses",
    "ec2-network-interfaces",
    "ec2-security-groups",
    "ec2-vpc-endpoints",
    "ec2-network-acls",
    "ec2-instances",
    "ec2-volumes",
    "ec2-flow-logs",
    "elbv2-load-balancers",
    "elbv2-target-groups",
    "ecs-clusters",
    "ecs-services",
    "ecs-td-families-active",
    "ecs-td-families-inactive",
    "ecs-td-inactive",
    "ecs-td-delete-in-progress",
    "ecr-repositories",
    "rds-clusters",
    "rds-instances",
    "rds-cluster-snapshots",
    "rds-instance-snapshots",
    "rds-cluster-automated-backups",
    "rds-subnet-groups",
    "rds-cluster-parameter-groups",
    "rds-parameter-groups",
    "elasticache-replication-groups",
    "elasticache-cache-clusters",
    "elasticache-snapshots",
    "elasticache-subnet-groups",
    "secrets",
    "kms-aliases",
    "kms-keys",
    "logs-groups",
    "cloudwatch-alarms",
    "cloudwatch-dashboards",
    "sns-topics",
    "sns-subscriptions",
    "ssm-parameters",
    "dynamodb-tables",
    "dynamodb-backups",
    "cognito-user-pools",
    "lambda-functions",
    "codedeploy-applications",
    "autoscaling-ecs",
    "autoscaling-dynamodb",
    "acm-certificates",
    "events-rules",
    "backup-vaults",
    "backup-recovery-points",
    "guardduty-detectors",
    "config-recorders",
    "tagged-resources",
)
GLOBAL = (
    "iam-roles",
    "iam-policies-local",
    "iam-instance-profiles",
    "iam-users",
    "iam-oidc-providers",
    "iam-saml-providers",
    "route53-hosted-zones",
    "route53-health-checks",
    "s3-buckets",
    "cloudfront-distributions",
)
FILES = sorted(
    [f"{r}/{t}.txt" for r in ("us-west-2", "us-east-1") for t in REGIONAL]
    + [f"global/{t}.txt" for t in GLOBAL]
)


def _summary(label: str, reads: int, failed: int, folder) -> str:
    return f"sweep {label}: {reads} reads, {failed} failed -> {folder}\n"


def test_the_closed_list_is_126_files():
    assert len(REGIONAL) == 58 and len(GLOBAL) == 10 and len(FILES) == 126


@pytest.mark.parametrize("bash", BASHES, ids=bash_id)
def test_a_new_account_is_126_files_and_126_reads(make_world, bash):
    world = make_world(accounts.new_account())
    done = world.sweep("before", bash=bash)
    assert (done.code, done.err) == (0, ""), done
    assert done.out == _summary("before", 126, 0, world.root / "sweep" / "before")
    files = world.files("before")
    assert sorted(files) == FILES
    # What the run's before sweep held, with this account's IDs.
    held = {f: len(lines) for f, lines in files.items() if lines}
    default_network = {
        "ec2-vpcs": 1,
        "ec2-internet-gateways": 1,
        "ec2-network-acls": 1,
        "ec2-route-tables": 1,
        "ec2-security-groups": 1,
        "kms-aliases": 11,
    }
    expected = {
        f"{r}/{t}.txt": n
        for r in ("us-west-2", "us-east-1")
        for t, n in default_network.items()
    }
    expected.update(
        {
            "us-west-2/ec2-subnets.txt": 4,
            "us-east-1/ec2-subnets.txt": 6,
            "global/iam-roles.txt": 7,
            "global/iam-saml-providers.txt": 1,
            "global/route53-hosted-zones.txt": 1,
        }
    )
    assert held == expected
    assert "alias/aws/s3\tnull" in files["us-west-2/kms-aliases.txt"]
    # One identity read first, then the 126; every regional read names its region.
    calls = world.calls
    assert len(calls) == 127
    assert calls[0]["argv"][:2] == ["sts", "get-caller-identity"]
    for call in calls[1:]:
        if call["service"] not in ("iam", "route53", "s3api", "cloudfront"):
            argv = call["argv"]
            assert argv[argv.index("--region") + 1] == call["region"], argv


def test_reads_are_files_plus_items(baseline, make_world):
    """An installed account: 1 cluster's services, 5 keys' descriptions (4 AWS and
    the database's), no backup vault: 126 + 1 + 5. A vault adds one more."""
    mid = baseline.worlds["mid"]
    assert len(mid.calls) == 1 + 132
    files = mid.files("mid")
    assert len(files["us-west-2/kms-keys.txt"]) == 5
    assert len(files["us-west-2/ecs-services.txt"]) == 2
    world = make_world(accounts.installed_account())
    world.edit(
        lambda s: (
            s["regions"]["us-west-2"]
            .setdefault("backup_vaults", [])
            .append({"BackupVaultName": "Default", "BackupVaultArn": "arn:vault"})
        )
    )
    done = world.sweep("vault")
    assert done.out == _summary("vault", 133, 0, world.root / "sweep" / "vault")


def test_the_mid_sweep_shows_every_type_the_install_creates(baseline):
    """The run's mid sweep, on the fake: each type an install makes has new lines."""
    before, mid = (
        baseline.worlds["before"].files("before"),
        baseline.worlds["mid"].files("mid"),
    )
    silent = sorted(
        f for f in accounts.CREATED_FILES if not set(mid[f]) - set(before[f])
    )
    assert silent == [], f"types the install creates with no new line: {silent}"


def _g2_swept() -> dict[str, str]:
    """G2's SWEPT, read as text from infrastructure/tests/test_installer_sweep_coverage.py
    (importing that module needs the CDK)."""
    import ast

    from backend.tests.installer_sweeps.harness.world import REPO_ROOT

    path = REPO_ROOT / "infrastructure" / "tests" / "test_installer_sweep_coverage.py"
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Assign) and [
            getattr(t, "id", None) for t in node.targets
        ] == ["SWEPT"]:
            return ast.literal_eval(node.value)
    raise AssertionError(f"{path} defines no SWEPT")


def test_the_install_makes_a_line_in_every_file_g2_names():
    """Each type the stacks make that has a sweep file of its own (G2's SWEPT) has
    that file in CREATED_FILES, so the test above holds the fake's install to it."""
    swept = _g2_swept()
    assert len(swept) == 37, sorted(swept)
    files = {
        f"global/{f}.txt" if f in GLOBAL else f"us-west-2/{f}.txt"
        for f in swept.values()
    }
    missing = sorted(files - set(accounts.CREATED_FILES))
    assert missing == [], (
        f"G2 names a sweep file the fake's install leaves empty: {missing}"
    )


def test_two_sweeps_moments_apart_are_identical(baseline, make_world):
    world = make_world(accounts.installed_account())
    assert world.sweep("again").code == 0
    assert world.files("again") == baseline.worlds["mid"].files("mid")


@pytest.mark.parametrize("tagged", [False, True], ids=["untagged", "tagged"])
def test_a_canary_is_exactly_one_new_line(baseline, make_world, tagged):
    """The run's canary check: an SSM parameter is one new line in
    ssm-parameters.txt; the canary, tagged, also puts its ARN in
    tagged-resources.txt."""
    world = make_world(accounts.new_account())
    arn = f"arn:aws:ssm:us-west-2:{ACCOUNT}:parameter/docs-run/sweep-canary"

    def plant(state):
        here = state["regions"]["us-west-2"]
        here.setdefault("parameters", []).append(
            {
                "Name": "/docs-run/sweep-canary",
                "ARN": arn,
                "Type": "String",
                "Version": 1,
            }
        )
        if tagged:
            here.setdefault("tag_index", []).append(arn)

    world.edit(plant)
    assert world.sweep("canary").code == 0
    before, canary = baseline.worlds["before"].files("before"), world.files("canary")
    added = {f: sorted(set(canary[f]) - set(before[f])) for f in FILES}
    added = {f: lines for f, lines in added.items() if lines}
    expected = {"us-west-2/ssm-parameters.txt": ["/docs-run/sweep-canary"]}
    if tagged:
        expected["us-west-2/tagged-resources.txt"] = [arn]
    assert added == expected
    assert all(set(before[f]) <= set(canary[f]) for f in FILES)


#: One faulted sweep of an installed account: each way a read can fail, on a
#: different type. (file, the fault, a line its marker must carry)
FAILURES = (
    (
        "us-east-1/ec2-vpcs.txt",
        {
            "service": "ec2",
            "op": "describe-vpcs",
            "region": "us-east-1",
            "mode": "AccessDenied",
        },
        "(UnauthorizedOperation)",
    ),
    (
        "us-west-2/ec2-subnets.txt",
        {
            "service": "ec2",
            "op": "describe-subnets",
            "region": "us-west-2",
            "mode": "EMPTY",
        },
        "an empty answer (exit 0, nothing printed)",
    ),
    (
        "global/iam-users.txt",
        {"service": "iam", "op": "list-users", "mode": "NOQUERY"},
        "not one JSON list",
    ),
    (
        "us-east-1/lambda-functions.txt",
        {
            "service": "lambda",
            "op": "list-functions",
            "region": "us-east-1",
            "mode": "error",
            "stderr": "\nCould not connect\n",
            "exit": 255,
        },
        "exit 255",
    ),
    (
        "us-west-2/kms-keys.txt",
        {
            "service": "kms",
            "op": "list-keys",
            "region": "us-west-2",
            "mode": "AccessDenied",
        },
        "(AccessDeniedException)",
    ),
    (
        "us-west-2/backup-recovery-points.txt",
        {
            "service": "backup",
            "op": "list-backup-vaults",
            "region": "us-west-2",
            "mode": "EMPTY",
            "nth": 2,
        },
        "an empty answer (exit 0, nothing printed)",
    ),
    (
        "us-east-1/ecs-services.txt",
        {
            "service": "ecs",
            "op": "list-clusters",
            "region": "us-east-1",
            "mode": "NOQUERY",
            "nth": 2,
        },
        "not one JSON list",
    ),
)


def test_every_failed_read_is_counted_and_fails_the_sweep(make_world):
    """Denied, empty, the whole response, unreachable; on a one-call type and on a
    per-item type's list. The AWS guide's run read those lists as text, which would
    have taken the empty and the whole-response answers as lists."""
    world = make_world(accounts.installed_account())
    for _, fault, _ in FAILURES:
        world.fault(**fault)
    folder = world.root / "sweep" / "mid"
    done = world.sweep("mid")
    assert done.code == 1, done
    # 126 files; of the per-item reads only us-west-2's one cluster is left (the
    # keys' list failed): 127 reads, 7 failed.
    assert done.out == _summary("mid", 127, 7, folder)
    files = world.files("mid")
    assert sorted(files) == FILES
    flagged = sorted(
        f
        for f, lines in files.items()
        if any(x.startswith("READ FAILED") for x in lines)
    )
    assert flagged == sorted(f for f, _, _ in FAILURES)
    for name, _, why in FAILURES:
        lines = files[name]
        assert lines[0].startswith("READ FAILED: aws "), (name, lines)
        assert any(why in line for line in lines), (name, lines)


def test_a_failed_item_read_is_marked_and_the_good_items_kept(make_world):
    world = make_world(accounts.installed_account())
    world.fault(mode="AccessDenied", service="kms", op="describe-key", nth=2)
    done = world.sweep("mid")
    assert done.code == 1
    assert done.out == _summary("mid", 132, 1, world.root / "sweep" / "mid")
    lines = world.files("mid")["us-west-2/kms-keys.txt"]
    marked = [
        x
        for x in lines
        if x.startswith(
            "READ FAILED: aws kms describe-key --region us-west-2 --key-id "
        )
    ]
    good = [x for x in lines if "\tEnabled\t" in x]
    assert len(marked) == 1 and len(good) == 4
    assert any("(AccessDeniedException)" in x for x in lines)


def test_another_accounts_sign_in_records_nothing(make_world):
    world = make_world(accounts.new_account())
    done = world.sweep("before", account="123456789012")
    assert done.code == 2
    assert (
        "STOP: the profile signs in to account 111111111111, not 123456789012"
        in done.err
    )
    assert [c["op"] for c in world.calls] == ["get-caller-identity"]
    assert not (world.root / "sweep" / "before").exists()


@pytest.mark.parametrize("mode", ["AccessDenied", "EMPTY", "NOQUERY"])
def test_an_unreadable_sign_in_records_nothing(make_world, mode):
    world = make_world(accounts.new_account())
    world.fault(mode=mode, service="sts")
    done = world.sweep("before")
    assert done.code == 2, done
    assert len(world.calls) == 1
    assert not (world.root / "sweep" / "before").exists()


def test_a_used_folder_or_a_bad_account_is_refused(make_world):
    world = make_world(accounts.new_account())
    (world.root / "sweep" / "before").mkdir(parents=True)
    again = world.sweep("before")
    assert again.code == 2 and "exists: use a new folder" in again.err
    bad = world.sweep("other", account="1111")
    assert bad.code == 2 and "is not a 12-digit account ID" in bad.err
    assert world.calls == []  # neither refusal called aws


def test_more_user_pools_than_one_page_is_a_failed_read(make_world):
    """ListUserPools takes its page size by hand (--max-results 60), which turns
    the CLI's paging off: the answer is one page. A page that says there is more
    must be a failed read, never a list that leaves pools out."""
    world = make_world(accounts.installed_account())
    world.edit(lambda s: s["regions"]["us-west-2"].update(user_pools_next_token="t2"))
    done = world.sweep("mid")
    assert done.code == 1, done
    assert done.out == _summary("mid", 132, 1, world.root / "sweep" / "mid")
    lines = world.files("mid")["us-west-2/cognito-user-pools.txt"]
    assert lines[0].startswith(
        "READ FAILED: aws cognito-idp list-user-pools --region us-west-2"
    )
    assert '"more than one page"' in lines, lines
    # The same account with one page of pools reads them.
    other = world.files("mid")["us-east-1/cognito-user-pools.txt"]
    assert other == []


def _read_with(answer: str):
    """_sweep_json given ``answer`` as the CLI's output: (exit status, reason)."""
    import subprocess

    from backend.tests.installer_sweeps.harness.world import LIB

    script = (
        f'. "{LIB}/names.sh"; . "{LIB}/sweep.sh"; '
        'cv_aws() { printf "%s" "$ANSWER"; }; '
        'out=$(mktemp "${TMPDIR:-/tmp}/answer.XXXXXX"); err="$out.err"; '
        '_sweep_json "$out" "$err" ec2 describe-vpcs; rc=$?; cat "$err"; rm -f "$out" "$err"; exit $rc'
    )
    done = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        # The parent's PATH, as World.env passes it: jq can live anywhere
        # (Homebrew's /opt/homebrew/bin on a Mac).
        env={"ANSWER": answer, "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    )
    return done.returncode, done.stdout


@pytest.mark.parametrize(
    "answer, good",
    [
        ("[]\n", True),
        ('["a", "b"]\n', True),
        ("null\n", True),
        ("[] []\n", False),
        ('{"b": 1} []\n', False),
        ('"more than one page"\n', False),
        ('{"Vpcs": []}\n', False),
        ("", False),
        ("[1, \n", False),
    ],
    ids=[
        "empty-list",
        "list",
        "null",
        "two-lists",
        "object-then-list",
        "string",
        "object",
        "nothing",
        "cut-off",
    ],
)
def test_a_read_is_exactly_one_json_list(answer, good):
    """jq -e alone judges by the last document, so `[] []` or `{"b":1} []` would
    pass as a list."""
    code, reason = _read_with(answer)
    assert (code == 0) is good, (answer, code, reason)
    if not good:
        assert "an empty answer" in reason or "not one JSON list" in reason, reason
