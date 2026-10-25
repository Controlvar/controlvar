"""The AWS installer beta's uninstall, run whole against the fakes.

One clean install is made once (the template); each scenario starts from a copy of it
(InstallWorld.adopt: the account as the install left it and the record's own files) and
changes one thing, then uninstalls. The record of the account is the real sweep library's
format, taken from the fake's state in one process (harness/state_sweep.py, held equal to
the real sweep by test_state_sweep.py), and compared by the installer's own verdict.

* The clean uninstall: its deletes in order, the nine stacks in the teardown order from
  the assembly the target check read, CDKToolkit kept and listed, the environment of every
  child (B2), the regions (B4), what it prints (U11), and the account after it equal to the
  account before the install plus exactly the printed kept list, both ways (F3).
* A lost or foreign record (F1), another release's folder, --plan-only, a decline and no
  terminal: nothing read in AWS where nothing needs to be, and nothing deleted.
* What it never deletes (F2): look-alikes in the reader's zone, a CDKToolkit that was
  there before, another environment's family and log group, a revision and a log group
  of the install's own names that were there before it, a stack made after the install,
  a record that changed after it.
* Errors not swallowed, not-found done (F4): a delete denied, a stack DELETE_FAILED, the
  network held by GuardDuty's own endpoint, each named, then a run again that completes.
* A re-install after an uninstall (F6), each start with its own INSTALL_ID and
  certificate token; the session check before the destroy (B3b); a stack an earlier run
  left in progress, waited for (and its cap); a deployment under way (S2b (i)); an
  interrupt during the destroy, then a run again; a comparison that could not read a list,
  then a run again.
"""

from __future__ import annotations

import json
import os
import re
import signal
import time
from pathlib import Path

import pytest

from backend.tests.installer_sweeps.harness import accounts, state_sweep
from backend.tests.installer_sweeps.harness.install_world import (
    COPY_PROFILE,
    PROFILE,
    SHIM_ADDS,
    Batch,
    InstallWorld,
    alive,
    is_aws,
    is_write,
    stop_group,
)
from backend.tests.installer_sweeps.harness.world import (
    SAFETY_NET_SECONDS,
    Result,
    as_text,
)

pytestmark = pytest.mark.unit

ACCOUNT = accounts.ACCOUNT
DOMAIN = accounts.INSTALL_DOMAIN
REPO_ROOT = Path(__file__).resolve().parents[3]
#: The nine stacks in the order uninstall destroys them (lib/names.sh's own list).
ORDER = [
    f"experimentation-{s}-staging"
    for s in (
        "migrations",
        "fargate",
        "monitoring",
        "compute",
        "redis",
        "database",
        "dynamodb",
        "auth",
        "vpc",
    )
]
#: What a clean uninstall changes, in order: the address, the nine stacks, the
#: certificate and its record, the two secrets, the two repositories, Container
#: Insights' log group, then the revisions (each family's INACTIVE ones deleted; the
#: migration's by-hand revision deregistered first).
CLEAN_DELETES = [
    ("route53", "change-resource-record-sets"),
    *[("cdk", "destroy")] * 9,
    ("acm", "delete-certificate"),
    ("route53", "change-resource-record-sets"),
    ("secretsmanager", "delete-secret"),
    ("secretsmanager", "delete-secret"),
    ("ecr", "delete-repository"),
    ("ecr", "delete-repository"),
    ("logs", "delete-log-group"),
    ("ecs", "delete-task-definitions"),
    ("ecs", "delete-task-definitions"),
    ("ecs", "deregister-task-definition"),
    ("ecs", "delete-task-definitions"),
]
DONE = "Controlvar is uninstalled from account"
NOTHING_EXPLAINED = "Not explained: none."
#: Every child's environment (as test_install_run's B2): exactly these names.
BASE_ENV = {
    "PATH",
    "HOME",
    "AWS_CONFIG_FILE",
    "AWS_SHARED_CREDENTIALS_FILE",
    "AWS_PROFILE",
    "AWS_REGION",
    "AWS_EC2_METADATA_DISABLED",
    "AWS_PAGER",
    "AWS_DEFAULT_OUTPUT",
    "DOCKER_CONFIG",
    "TUF_ROOT",
    "TZ",
}
CDK_INPUTS = {
    "ENVIRONMENT",
    # The CDK's profile variable, as lib/names.sh spells it (its former-name spelling is
    # kept there on purpose, and only there).
    re.search(
        r'^NAMES_PROFILE_VARIABLE="([A-Z_]+)"$',
        (REPO_ROOT / "deploy/aws-installer/lib/names.sh").read_text(),
        re.M,
    ).group(1),
    "ALARM_EMAIL",
    "PUBLIC_BASE_URL",
    "CERTIFICATE_ARN",
    "JWT_SECRET_ARN",
    "FIRST_SUPERUSER_PASSWORD_SECRET_ARN",
}


def deletes(world: InstallWorld) -> list[tuple[str, str]]:
    return world.writes()


def baseline_files(shapes: Path) -> dict[str, list[str]]:
    """The account before the install, in the sweep's own files."""
    import tempfile

    with tempfile.TemporaryDirectory() as scratch:
        folder = Path(scratch) / "before"
        state_sweep.take(
            accounts.install_target(), json.loads(shapes.read_text()), folder
        )
        return {
            str(p.relative_to(folder)): p.read_text().splitlines()
            for p in sorted(folder.rglob("*.txt"))
        }


def report_lines(world: InstallWorld, name: str) -> set[tuple[str, str, str]]:
    path = world.record / "verdict" / f"{name}.txt"
    if not path.exists():
        return set()
    return {tuple(line.split("\t", 2)) for line in path.read_text().splitlines()}


def account_against(
    before: dict[str, list[str]], now: dict[str, list[str]]
) -> set[tuple[str, str, str]]:
    """Every line that differs between two records: (file, + or -, line)."""
    out = set()
    for name in sorted(set(before) | set(now)):
        old, new = set(before.get(name, [])), set(now.get(name, []))
        out |= {(name, "+", line) for line in new - old}
        out |= {(name, "-", line) for line in old - new}
    return out


def kept_list_is_the_residue(world: InstallWorld, before: dict) -> list[str]:
    """F3, both ways: the account now, against the account before the install, is exactly
    the kept and lag lines the uninstall printed, with nothing not explained or not
    checked."""
    problems = []
    difference = account_against(before, world.sweep_files())
    printed = report_lines(world, "kept") | report_lines(world, "lag")
    if difference - printed:
        problems.append(f"residue not listed: {sorted(difference - printed)}")
    if printed - difference:
        problems.append(f"listed but not residue: {sorted(printed - difference)}")
    for name in ("not-explained", "not-checked"):
        if report_lines(world, name):
            problems.append(f"{name}: {sorted(report_lines(world, name))}")
    return problems


# --- the scenarios --------------------------------------------------------------------------

_BATCH = Batch()
#: The one clean install every scenario copies (made in the module's fixture).
TEMPLATE: dict[str, InstallWorld] = {}


class _Uninstalls:
    """The module's scenarios: each an uninstall (unless it says otherwise), with the
    record of the account the harness takes from the fake's state."""

    def add(self, name, prepare=None, run=None, serial=False, stub_sweep="state"):
        return _BATCH.add(
            name,
            prepare=prepare,
            run=run if run is not None else (lambda world: world.uninstall()),
            serial=serial,
            stub_sweep=stub_sweep,
        )

    def run_all(self, root, shapes):
        return _BATCH.run_all(root, shapes)


BATCH = _Uninstalls()


def _installed(*changes):
    """A world holding the template's install, then each change made to it."""

    def prepare(world: InstallWorld) -> None:
        world.adopt(TEMPLATE["world"])
        for change in changes:
            change(world)

    return prepare


def _state(change):
    return lambda world: world.edit(change)


def _cdk(**settings):
    return _state(lambda s: s.setdefault("cdk", {}).update(settings))


def _us_west_2(state: dict) -> dict:
    return state["regions"]["us-west-2"]


def _zone(state: dict) -> dict:
    return next(
        z for z in state["global"]["hosted_zones"] if z["Name"] == accounts.INSTALL_ZONE
    )


def _twice(world: InstallWorld, fix=None):
    """Uninstall, then the same command again with the fault the first met cleared (and
    whatever the message told the reader to do, done)."""
    first = world.uninstall()
    world.edit(lambda s: s.update(faults=[]))
    world.edit(lambda s: s.get("cdk", {}).pop("destroy_fail", None))
    world.edit(lambda s: s.get("cdk", {}).pop("destroy_noop", None))
    if fix is not None:
        world.edit(fix)
    return first, world.uninstall()


def _guardduty_removed(state: dict) -> None:
    """What the page's "if uninstall stops at the network" has the reader do."""
    region = _us_west_2(state)
    region["vpc_endpoints"] = [
        e for e in region["vpc_endpoints"] if e["VpcEndpointId"] != GUARDDUTY_ENDPOINT
    ]
    region["security_groups"] = [
        g for g in region["security_groups"] if g["GroupId"] != GUARDDUTY_GROUP
    ]


BATCH.add("clean", prepare=_installed(), stub_sweep="state")
BATCH.add(
    "plan-only",
    prepare=_installed(),
    run=lambda w: w.uninstall("--plan-only", yes=False),
    stub_sweep="state",
)
BATCH.add(
    "declined",
    prepare=_installed(),
    run=lambda w: w.on_terminal(typed="111111111112\n", verb="uninstall")[0],
    serial=True,
    stub_sweep="state",
)
BATCH.add(
    "typed",
    prepare=_installed(),
    run=lambda w: w.on_terminal(typed=ACCOUNT + "\n", verb="uninstall")[0],
    serial=True,
    stub_sweep="state",
)
BATCH.add(
    "no-terminal",
    prepare=_installed(),
    run=lambda w: w.uninstall(yes=False),
    stub_sweep="state",
)


# F1: no record of a started install of this account.
def _no_folder(world: InstallWorld) -> None:
    import shutil

    shutil.rmtree(world.record)


def _other_account(world: InstallWorld) -> None:
    world.adopt(TEMPLATE["world"])
    inputs = world.record / "inputs.sh"
    inputs.write_text(
        inputs.read_text().replace(f"CV_ACCOUNT={ACCOUNT}", "CV_ACCOUNT=111122223333")
    )


F1_CASES = {
    "no-folder": (_no_folder, "This computer has no record of an install"),
    "planned-only": (lambda w: None, "holds no install: its checks ran"),
    "another-accounts": (_other_account, "is of the account ending in 3333"),
}
for _name, (_prepare, _said) in F1_CASES.items():
    BATCH.add(f"f1-{_name}", prepare=_prepare, stub_sweep="state")


def _uninstall_twice(world: InstallWorld):
    first = world.uninstall()
    world.clear_logs()
    return first, world.uninstall()


BATCH.add(
    "f1-uninstalled", prepare=_installed(), run=_uninstall_twice, stub_sweep="state"
)
BATCH.add(
    "another-release",
    prepare=_installed(lambda w: (w.clone / "VERSION").write_text("0.36.3\n")),
    stub_sweep="state",
)


# F2: what it never deletes.
def _look_alikes(world: InstallWorld) -> None:
    """Before the install: a www CNAME, a TXT and the identical earlier validation record
    in the reader's zone; a CDKToolkit already there; another environment's family and
    log group."""
    from backend.tests.installer_sweeps.harness import fake_ops_install

    validation = f"_{fake_ops_install._id('validation' + DOMAIN, 32)}.{DOMAIN}."
    value = f"_{fake_ops_install._id('target' + DOMAIN, 32)}.xlfgrmvvlj.acm-validations.aws."

    def change(state: dict) -> None:
        zone = _zone(state)
        zone["records"] = [
            {
                "Name": accounts.INSTALL_ZONE,
                "Type": "NS",
                "TTL": 172800,
                "ResourceRecords": [{"Value": ns + "."} for ns in zone["NameServers"]],
            },
            {
                "Name": accounts.INSTALL_ZONE,
                "Type": "SOA",
                "TTL": 900,
                "ResourceRecords": [
                    {
                        "Value": f"{zone['NameServers'][0]}. awsdns-hostmaster.amazon.com. 1 7200 900 1209600 86400"
                    }
                ],
            },
            {
                "Name": f"www.{accounts.INSTALL_ZONE}",
                "Type": "CNAME",
                "TTL": 300,
                "ResourceRecords": [{"Value": "elsewhere.example.org."}],
            },
            {
                "Name": accounts.INSTALL_ZONE,
                "Type": "TXT",
                "TTL": 300,
                "ResourceRecords": [{"Value": '"v=spf1 -all"'}],
            },
            {
                "Name": validation,
                "Type": "CNAME",
                "TTL": 300,
                "ResourceRecords": [{"Value": value}],
            },
        ]
        zone["ResourceRecordSetCount"] = 5
        builder = accounts.Builder(state["account"])
        builder.state = state
        builder.bootstrap_stack()
        region = _us_west_2(state)
        region.setdefault("task_definitions", []).append(
            {
                "family": "experimentation-backend-prod",
                "revision": 4,
                "status": "ACTIVE",
            }
        )
        region.setdefault("log_groups", []).append(
            {
                "logGroupName": "/ecs/experimentation-prod",
                "creationTime": 1761000000000,
                "metricFilterCount": 0,
                "arn": f"arn:aws:logs:us-west-2:{ACCOUNT}:log-group:/ecs/experimentation-prod:*",
                "storedBytes": 0,
            }
        )

    world.edit(change)


def _then_a_stack_made_after(world: InstallWorld):
    done = world.install()
    assert done.code == 0, done.err
    world.edit(
        lambda s: _us_west_2(s)["stacks"].append(
            {
                "StackId": f"arn:aws:cloudformation:us-west-2:{ACCOUNT}:stack/experimentation-extra-staging/1",
                "StackName": "experimentation-extra-staging",
                "CreationTime": "2026-10-21T04:00:00Z",
                "StackStatus": "CREATE_COMPLETE",
            }
        )
    )
    world.edit(lambda s: s.update(before_uninstall=json.loads(json.dumps(s))))
    world.clear_logs()
    return world.uninstall()


BATCH.add(
    "look-alikes",
    prepare=_look_alikes,
    run=_then_a_stack_made_after,
    stub_sweep="state",
)


def _alias_changed(world: InstallWorld) -> None:
    def change(state: dict) -> None:
        for record in _zone(state)["records"]:
            if record["Name"] == DOMAIN + "." and record["Type"] == "A":
                record["AliasTarget"]["DNSName"] = (
                    "someone-elses.us-west-2.elb.amazonaws.com."
                )

    world.edit(change)


BATCH.add(
    "record-changed",
    prepare=_installed(_alias_changed),
    stub_sweep="state",
)

INSIGHTS_LOG_GROUP = "/aws/ecs/containerinsights/experimentation-staging/performance"
#: A revision of the API's own family, deregistered before the install.
OLD_REVISION = {
    "family": "experimentation-backend-staging",
    "revision": 1,
    "status": "INACTIVE",
}


def _left_by_an_earlier_staging(state: dict) -> None:
    """Before the install: what taking a staging environment down by hand can leave, with
    this install's own names. The guide's teardown does not remove task-definition
    revisions, and ECS makes Container Insights' log group outside any stack."""
    region = _us_west_2(state)
    region.setdefault("task_definitions", []).append(dict(OLD_REVISION))
    region.setdefault("log_groups", []).append(
        {
            "logGroupName": INSIGHTS_LOG_GROUP,
            "creationTime": 1760000000000,
            "metricFilterCount": 0,
            "arn": f"arn:aws:logs:us-west-2:{ACCOUNT}:log-group:{INSIGHTS_LOG_GROUP}:*",
            "storedBytes": 0,
        }
    )


def _install_then_uninstall(world: InstallWorld):
    """The account before the install (the record of it the comparison is held to), the
    install, then the uninstall."""
    before_install = world.sweep_files()
    installed = world.install()
    world.clear_logs()
    return before_install, installed, world.uninstall()


BATCH.add(
    "here-before",
    prepare=lambda w: w.edit(_left_by_an_earlier_staging),
    run=_install_then_uninstall,
    stub_sweep="state",
)


def _secret_not_ours(state: dict) -> None:
    """The install's JWT secret replaced, after the install, by one of the same name that
    another install made (its tag is not this install's)."""
    for secret in _us_west_2(state)["secrets"]:
        if secret["Name"] == "/staging/experimentation/jwt-secret":
            secret["Tags"] = [{"Key": "controlvar-install-id", "Value": "0123456789ab"}]


BATCH.add("secret-not-ours", prepare=_installed(_state(_secret_not_ours)))


# F4: errors are not swallowed; not found is done; a run again completes.
BATCH.add(
    "delete-denied",
    prepare=_installed(
        lambda w: w.fault(
            service="secretsmanager", op="delete-secret", mode="AccessDenied"
        )
    ),
    run=_twice,
    stub_sweep="state",
)
BATCH.add(
    "delete-failed",
    prepare=_installed(
        _cdk(
            destroy_fail={
                "experimentation-database-staging": {
                    "reason": "Cannot delete the cluster while it has a snapshot in progress",
                    "logical": "AuroraCluster",
                    "type": "AWS::RDS::DBCluster",
                    "physical": "experimentation-database-staging-auroracluster-1a2b3c4d5e6f",
                }
            }
        )
    ),
    run=_twice,
    stub_sweep="state",
)
GUARDDUTY_ENDPOINT = "vpce-0guardduty0000000"
GUARDDUTY_GROUP = "sg-0guardduty00000000"


def _guardduty(world: InstallWorld) -> None:
    vpc = next(
        v["VpcId"] for v in _us_west_2(world.state)["vpcs"] if not v.get("IsDefault")
    )
    world.edit(
        lambda s: s.setdefault("cdk", {}).update(
            destroy_fail={
                "experimentation-vpc-staging": {
                    "reason": "The vpc has dependencies and cannot be deleted. (DependencyViolation)",
                    "logical": "Vpc",
                    "type": "AWS::EC2::VPC",
                    "physical": vpc,
                    "vpc_endpoints": [
                        {
                            "VpcEndpointId": GUARDDUTY_ENDPOINT,
                            "VpcEndpointType": "Interface",
                            "VpcId": vpc,
                            "ServiceName": "com.amazonaws.us-west-2.guardduty-data",
                            "State": "available",
                            "OwnerId": ACCOUNT,
                        }
                    ],
                    "security_groups": [
                        {
                            "GroupId": GUARDDUTY_GROUP,
                            "GroupName": "GuardDutyManagedSecurityGroup-vpc",
                            "Description": "GuardDuty's own group",
                            "VpcId": vpc,
                            "OwnerId": ACCOUNT,
                        }
                    ],
                }
            }
        )
    )


BATCH.add(
    "guardduty",
    prepare=_installed(_guardduty),
    run=lambda w: _twice(w, _guardduty_removed),
    stub_sweep="state",
)
BATCH.add(
    "secret-already-gone",
    prepare=_installed(
        _state(
            lambda s: _us_west_2(s).update(
                secrets=[
                    x
                    for x in _us_west_2(s)["secrets"]
                    if x["Name"] != "/staging/experimentation/jwt-secret"
                ]
            )
        )
    ),
    stub_sweep="state",
)
BATCH.add(
    "gone-at-the-delete",
    prepare=_installed(
        lambda w: w.effect(
            service="logs",
            op="describe-log-groups",
            drop={
                "region": "us-west-2",
                "collection": "log_groups",
                "field": "logGroupName",
                "value": "/aws/ecs/containerinsights/experimentation-staging/performance",
            },
        )
    ),
    stub_sweep="state",
)


# The Aurora secret CloudFormation leaves scheduled for deletion.
def _db_secret_waiting(state: dict) -> None:
    _us_west_2(state)["secrets"].append(
        {
            "ARN": f"arn:aws:secretsmanager:us-west-2:{ACCOUNT}:secret:experimentation-database-staging-aurora-credentials-AbCdEf",
            "Name": "experimentation-database-staging-aurora-credentials",
            "CreatedDate": "2026-10-21T03:30:00Z",
            "DeletedDate": "2026-11-20T05:50:00Z",
        }
    )


BATCH.add(
    "db-secret-waiting",
    prepare=_installed(_state(_db_secret_waiting)),
    stub_sweep="state",
)


# F6: install, uninstall, --plan-only, install, uninstall, in one account.
def _again_and_again(world: InstallWorld):
    runs = [world.install()]
    runs.append(world.uninstall())
    runs.append(world.install("--plan-only"))
    runs.append(world.install())
    runs.append(world.uninstall())
    return runs


BATCH.add("reinstall", run=_again_and_again, stub_sweep="state")


# B3b's uninstall half: the sign-in ends after the checks, before the destroy.
def _session_ends(world: InstallWorld) -> None:
    world.adopt(TEMPLATE["world"])
    world.effect(
        service="route53",
        op="change-resource-record-sets",
        file=str(world.token_path()),
        drop_key="refreshToken",
    )


BATCH.add("session-ends", prepare=_session_ends, stub_sweep="state")


# A stack an earlier run left in progress: waited for, then deleted; and its cap.
def _in_progress(state: dict) -> None:
    for stack in _us_west_2(state)["stacks"]:
        if stack["StackName"] == "experimentation-fargate-staging":
            stack["StackStatus"] = "UPDATE_IN_PROGRESS"
    state["settle"] = {
        "experimentation-fargate-staging": {"reads": 2, "status": "UPDATE_COMPLETE"}
    }


BATCH.add("wait", prepare=_installed(_state(_in_progress)), stub_sweep="state")


def _never_settles(world: InstallWorld) -> None:
    world.adopt(TEMPLATE["world"])
    world.edit(_in_progress)
    world.edit(lambda s: s.update(settle={}))
    world.use_clock(60)


BATCH.add(
    "wait-cap",
    prepare=_never_settles,
    run=lambda w: w.uninstall(CONTROLVAR_AWS_STACK_WAIT_MINUTES="2"),
    stub_sweep="state",
)

# S2b (i): a CodeDeploy deployment under way refuses before anything is deleted.
BATCH.add(
    "deployment-active",
    prepare=_installed(
        _state(
            lambda s: _us_west_2(s).update(
                deployments=[{"id": "d-ABCDEF123", "status": "InProgress"}]
            )
        )
    ),
    stub_sweep="state",
)
BATCH.add(
    "no-codedeploy-application",
    prepare=_installed(
        _state(lambda s: _us_west_2(s).update(codedeploy_applications=[]))
    ),
    stub_sweep="state",
)


# An interrupt during the destroy, then the same command again.
def interrupt_uninstall(world: InstallWorld, sig: int, group: bool) -> tuple:
    process = world.start_uninstall()
    try:
        deadline = time.time() + SAFETY_NET_SECONDS
        while not any(
            line.split()[1:3] == ["cdk", "destroy"] for line in world.pid_lines()
        ):
            if process.poll() is not None or time.time() > deadline:
                out, err = process.communicate()
                raise AssertionError(
                    f"cdk destroy never blocked: exit {process.returncode}\n{as_text(out)}\n{as_text(err)}"
                )
            time.sleep(0.05)
        if group:
            os.killpg(process.pid, sig)
        else:
            os.kill(process.pid, sig)
        out, err = process.communicate(timeout=SAFETY_NET_SECONDS)
    except BaseException:
        stop_group(process)
        raise
    first = Result(process.returncode, as_text(out), as_text(err))
    world.edit(lambda s: s["cdk"].pop("destroy", None))
    return first, world.uninstall()


SIGNALS = {
    "INT-to-the-group": (signal.SIGINT, True),
    "TERM-to-the-installer": (signal.SIGTERM, False),
}
for _name, (_sig, _group) in SIGNALS.items():
    BATCH.add(
        f"interrupted-{_name}",
        prepare=_installed(
            _cdk(
                destroy="block",
                block_stack="experimentation-fargate-staging",
                block_seconds=60,
            )
        ),
        run=lambda w, s=_sig, g=_group: interrupt_uninstall(w, s, g),
        stub_sweep="state",
    )


@pytest.fixture(scope="module")
def ran(tmp_path_factory, shapes_file):
    root = tmp_path_factory.mktemp("uninstall-run")
    template = InstallWorld(
        root / "template", accounts.install_target(), shapes_file, stub_sweep="state"
    )
    done = template.install()
    assert done.code == 0 and template.unknown == "", (done.err, template.unknown)
    TEMPLATE["world"] = template
    found = BATCH.run_all(root, shapes_file)
    yield found
    unknown = {name: r.world.unknown for name, r in found.items() if r.world.unknown}
    assert unknown == {}, unknown


@pytest.fixture(scope="module")
def before(shapes_file):
    return baseline_files(shapes_file)


@pytest.fixture(scope="module")
def clean(ran):
    return ran["clean"]


# --- the clean uninstall ----------------------------------------------------------------------


def test_the_clean_uninstall_removes_the_install(clean):
    done = clean.done
    assert done.code == 0, done.err
    assert done.err == "", done.err
    assert DONE in done.out
    assert NOTHING_EXPLAINED in done.out
    assert (clean.world.record / "env.d" / "UNINSTALLED.sh").exists()


def test_the_clean_uninstalls_deletes_are_pinned_in_order(clean):
    assert deletes(clean.world) == CLEAN_DELETES


def test_the_kept_list_is_exactly_the_residue_both_ways(clean, before):
    """F3: the account after the uninstall, against the account before the install, is
    exactly what the report lists as kept (and the tagging index's lag), by exact line."""
    assert kept_list_is_the_residue(clean.world, before) == []
    out = clean.done.out
    for line in (
        "5 service-linked role(s) (AWSServiceRoleForECS and 4 more)",
        "AWS managed keys made on first use (aws/acm, aws/lambda, aws/s3, aws/secretsmanager); the database's key, pending deletion",
        "3 DynamoDB backup(s) of the deleted tables",
        "the alarm subscription, never confirmed",
        "4 task definition(s) and 10 tag-index entries AWS is still clearing",
        "Kept by the installer: the CDK's setup stack (CDKToolkit)",
    ):
        assert line in out, line


def test_the_nine_stacks_go_in_the_teardown_order_from_the_checked_assembly(clean):
    """F5's run half and the destroy's own assembly: each cdk destroy names one stack, in
    NAMES_DESTROY_ORDER, with --app the folder the list call wrote and the target check
    read (nothing is synthesised again)."""
    calls = [
        c for c in clean.world.calls if c["service"] == "cdk" and c["op"] != "--version"
    ]
    assert [c["op"] for c in calls] == ["list"] + ["destroy"] * 9, calls
    out = calls[0]["argv"][calls[0]["argv"].index("--output") + 1]
    destroyed = []
    for call in calls[1:]:
        argv = call["argv"]
        assert argv[argv.index("--app") + 1] == out, argv
        assert "--force" in argv
        assert argv[argv.index("--profile") + 1] == COPY_PROFILE
        destroyed.append(argv[-1])
    assert destroyed == ORDER


def test_cdktoolkit_is_never_touched_while_a_stack_remains(clean):
    """F5's U half: no call names CDKToolkit after the destroy begins but the read that
    lists it, and it is still there, kept (D14's keep-and-list path)."""
    calls = clean.world.calls
    first_destroy = next(i for i, c in enumerate(calls) if c["op"] == "destroy")
    for call in calls[first_destroy:]:
        if "CDKToolkit" in " ".join(call["argv"]):
            assert call["op"] == "describe-stacks", call
    stacks = _us_west_2(clean.world.state)["stacks"]
    assert any(
        s["StackName"] == "CDKToolkit" and s["StackStatus"] == "CREATE_COMPLETE"
        for s in stacks
    )


def test_every_uninstall_child_runs_with_exactly_the_allowed_environment(clean):
    """B2 for uninstall: each aws and cdk child's environment is the allow-list, its HOME
    the record's own, its profile the copy's."""
    record = clean.world.record
    seen = 0
    for call in clean.world.calls:
        if (
            call["service"] in ("dig", "git", "curl", "node", "npm")
            or call["op"] == "--version"
        ):
            continue
        names = set(call["env"]) - SHIM_ADDS
        want = set(BASE_ENV) | (CDK_INPUTS if call["service"] == "cdk" else set())
        if call["argv"][-2:] == ["--endpoint-url", "http://127.0.0.1:9"]:
            want -= {"AWS_PROFILE"}
        assert names == want, (
            call["n"],
            call["service"],
            call["op"],
            sorted(names ^ want),
        )
        assert call["values"]["HOME"] == str(record / "home")
        assert call["values"]["AWS_CONFIG_FILE"] == str(record / "aws-config")
        if "AWS_PROFILE" in want:
            assert call["values"]["AWS_PROFILE"] == COPY_PROFILE
        seen += 1
    assert seen > 40, seen


def test_every_uninstall_call_is_in_us_west_2(clean):
    """B4 for uninstall: every regional call in us-west-2 (IAM and Route 53 global)."""
    outside = sorted(
        {
            (c["service"], c["op"], c["region"])
            for c in clean.world.calls
            if is_aws(c) and c["region"] not in ("us-west-2", None)
        }
    )
    assert outside == [], outside


def test_nothing_is_deleted_before_the_confirmation(clean):
    out = clean.done.out
    assert out.index("This deletes the install in account") < out.index(
        "Confirmed with --yes."
    )
    calls = clean.world.calls
    first = next(i for i, c in enumerate(calls) if is_write(c))
    for call in calls[:first]:
        if is_aws(call):
            assert call["op"].startswith(("list-", "describe-", "get-", "simulate-")), (
                call
            )


def test_the_record_keeps_its_logs_and_both_records_of_the_account(clean):
    record = clean.world.record
    assert (record / "sweep" / "before").is_dir() and (
        record / "sweep" / "after"
    ).is_dir()
    assert sorted(p.name for p in (record / "verdict").glob("*.txt")) == [
        "kept.txt",
        "lag.txt",
        "not-checked.txt",
        "not-explained.txt",
    ]
    assert list((record / "tmp").iterdir()) == []
    assert any(p.name.endswith("-uninstall.log") for p in (record / "logs").iterdir())


def test_the_childrens_home_keeps_no_role_credentials(clean):
    """The CLI caches the role's credentials under the children's HOME as it signs in
    (the fake does as the real CLI does); the exit trap removes them, so only the link to
    the shared sign-in cache is left there."""
    home = clean.world.record / "home"
    found = sorted(
        os.path.relpath(os.path.join(folder, name), home)
        for folder, dirs, files in os.walk(home)
        for name in dirs + files
    )
    assert found == [".aws", ".aws/sso", ".aws/sso/cache"], found


def test_nothing_uninstall_prints_is_a_terminal_effect(clean):
    for text in (clean.done.out, clean.done.err):
        bad = sorted(
            {hex(ord(ch)) for ch in text if not (ch == "\n" or " " <= ch <= "~")}
        )
        assert bad == [], bad


# --- refusals: nothing read where nothing needs to be, nothing deleted --------------------------


def test_plan_only_reads_and_deletes_nothing(ran):
    one = ran["plan-only"]
    assert one.done.code == 0, one.done.err
    assert "Plan only: nothing was deleted." in one.done.out
    assert (
        "Keeps: the zone beta.controlvar.dev (here before the install), and the CDK's setup stack"
        in one.done.out
    )
    assert deletes(one.world) == []
    assert not (one.world.record / "env.d" / "UNINSTALLED.sh").exists()


@pytest.mark.parametrize("name", ["declined", "no-terminal"])
def test_without_the_typed_account_id_nothing_is_deleted(ran, name):
    one = ran[name]
    assert one.done.code == 2, one.done.err
    assert one.done.err.startswith("REFUSED: "), one.done.err
    assert "Nothing was deleted." in one.done.err
    assert deletes(one.world) == []


def test_the_typed_account_id_confirms(ran):
    one = ran["typed"]
    assert one.done.code == 0, one.done.err
    assert DONE in one.done.out


@pytest.mark.parametrize("name", sorted(F1_CASES))
def test_a_lost_or_foreign_record_deletes_nothing(ran, name):
    """F1: with no record of a started install of this account, uninstall reads nothing in
    AWS and deletes nothing."""
    one = ran[f"f1-{name}"]
    assert one.done.code == 2, one.done.err
    assert F1_CASES[name][1] in one.done.err, one.done.err
    assert "Nothing was deleted." in one.done.err
    assert one.world.aws_calls == []


def test_a_record_already_uninstalled_deletes_nothing(ran):
    first, second = ran["f1-uninstalled"].done
    assert first.code == 0, first.err
    assert second.code == 2, second.err
    assert "was uninstalled" in second.err
    assert ran["f1-uninstalled"].world.aws_calls == []


def test_another_releases_folder_deletes_nothing(ran):
    one = ran["another-release"]
    assert one.done.code == 2, one.done.err
    assert (
        "This install was made by Controlvar 0.36.2, and this folder holds 0.36.3."
        in one.done.err
    )
    assert one.world.aws_calls == []


# --- F2: what it never deletes -------------------------------------------------------------------


def test_nothing_the_install_did_not_make_is_deleted(ran):
    """F2: the reader's www CNAME and TXT, the identical earlier validation record, the
    CDKToolkit already there, another environment's family and log group, and a stack
    made after the install: none deleted; the stack is reported as not explained."""
    one = ran["look-alikes"]
    done, world = one.done, one.world
    assert done.code == 1, done.err
    assert "Not explained: 1." in done.out
    assert (
        "us-west-2/cfn-stacks.txt + experimentation-extra-staging CREATE_COMPLETE"
        in done.out
    )
    state = world.state
    records = {(r["Name"], r["Type"]) for r in _zone(state)["records"]}
    assert (f"www.{accounts.INSTALL_ZONE}", "CNAME") in records
    assert (accounts.INSTALL_ZONE, "TXT") in records
    validation = next(
        r
        for r in state["before_uninstall"]["global"]["hosted_zones"]
        if r["Name"] == accounts.INSTALL_ZONE
    )
    kept_validation = [
        r
        for r in validation["records"]
        if r["Type"] == "CNAME" and r["Name"].startswith("_")
    ]
    assert all((r["Name"], "CNAME") in records for r in kept_validation)
    region = _us_west_2(state)
    assert any(
        s["StackName"] == "CDKToolkit" and s["StackStatus"] == "CREATE_COMPLETE"
        for s in region["stacks"]
    )
    assert any(
        s["StackName"] == "experimentation-extra-staging"
        and s["StackStatus"] == "CREATE_COMPLETE"
        for s in region["stacks"]
    )
    assert any(
        td["family"] == "experimentation-backend-prod" and td["status"] == "ACTIVE"
        for td in region["task_definitions"]
    )
    assert any(
        g["logGroupName"] == "/ecs/experimentation-prod" for g in region["log_groups"]
    )
    # The record never recorded the earlier validation record, so only the address goes.
    route53 = [c for c in world.calls if c["op"] == "change-resource-record-sets"]
    assert len(route53) == 1, route53


def test_what_was_here_before_the_install_is_kept(ran):
    """F2: a revision of the API's family and Container Insights' log group, both there
    before the install under the names the install uses: the checks list them, and
    uninstall, which deletes that family's INACTIVE revisions and that log group, leaves
    these two. The account afterwards is the account before the install plus exactly the
    kept list."""
    one = ran["here-before"]
    before_install, installed, done = one.done
    assert installed.code == 0, installed.err
    assert done.code == 0, done.err
    region = _us_west_2(one.world.state)
    assert OLD_REVISION in region["task_definitions"], [
        td
        for td in region["task_definitions"]
        if td["family"] == OLD_REVISION["family"]
    ]
    assert any(g["logGroupName"] == INSIGHTS_LOG_GROUP for g in region["log_groups"])
    assert kept_list_is_the_residue(one.world, before_install) == []


def test_a_record_changed_after_the_install_is_kept_and_said(ran, before):
    one = ran["record-changed"]
    assert one.done.code == 0, one.done.err
    assert (
        f"Kept: {DOMAIN}, which changed after the install (it now holds A someone-elses.us-west-2.elb.amazonaws.com.)"
        in one.done.out
    )
    assert ("route53", "change-resource-record-sets") in deletes(one.world)
    assert deletes(one.world).count(("route53", "change-resource-record-sets")) == 1
    assert any(
        r["Name"] == DOMAIN + "." and r["Type"] == "A"
        for r in _zone(one.world.state)["records"]
    )
    assert kept_list_is_the_residue(one.world, before) == []


def test_a_secret_this_install_did_not_make_is_kept(ran):
    """F2: a secret of the install's name that does not carry this install's tag is kept
    and said; the other is deleted."""
    one = ran["secret-not-ours"]
    assert (
        "Kept: the secret /staging/experimentation/jwt-secret, which does not carry this install's tag."
        in one.done.out
    ), one.done.out
    names = {x["Name"] for x in _us_west_2(one.world.state)["secrets"]}
    assert "/staging/experimentation/jwt-secret" in names
    assert "/staging/experimentation/first-superuser-password" not in names
    assert deletes(one.world).count(("secretsmanager", "delete-secret")) == 1


# --- F4: errors named, not found done, a run again completes -------------------------------------


@pytest.mark.parametrize(
    "name, said",
    [
        (
            "delete-denied",
            "FAILED: aws secretsmanager delete-secret (the secret /staging/experimentation/jwt-secret) stopped with an error (exit code 254): AccessDeniedException.",
        ),
        (
            "delete-failed",
            "FAILED: AWS could not delete AuroraCluster (AWS::RDS::DBCluster) in the stack experimentation-database-staging: Cannot delete the cluster while it has a snapshot in progress.",
        ),
        (
            "guardduty",
            f"FAILED: AWS could not delete the network: GuardDuty put its own endpoint in it ({GUARDDUTY_ENDPOINT}), with the group GuardDuty made for it ({GUARDDUTY_GROUP}).",
        ),
    ],
    ids=["delete-denied", "delete-failed", "guardduty"],
)
def test_a_failure_is_named_and_a_run_again_completes(ran, before, name, said):
    one = ran[name]
    first, second = one.done
    assert first.code == 1, first.err
    assert first.err.startswith(said), first.err
    assert "Deleted so far: " in first.err
    assert "run uninstall again" in first.err
    assert second.code == 0, second.err
    assert NOTHING_EXPLAINED in second.out
    assert kept_list_is_the_residue(one.world, before) == []


def test_guarddutys_own_endpoint_and_group_are_never_deleted(ran):
    world = ran["guardduty"].world
    assert not any(
        "delete-vpc-endpoints" in c["argv"] or "delete-security-group" in c["argv"]
        for c in world.calls
    )


def test_the_network_message_prints_the_commands_that_delete_guarddutys_two(ran):
    """The reader is given what the AWS guide's teardown gives: a command that lists what is
    left in the network, and the two deletes, by the IDs the installer read, under the
    reader's own profile."""
    first, _second = ran["guardduty"].done
    where = f"--region us-west-2 --profile {PROFILE}"
    for command in (
        "aws ec2 describe-network-interfaces --filters Name=vpc-id,Values=vpc-",
        f"aws ec2 delete-vpc-endpoints --vpc-endpoint-ids {GUARDDUTY_ENDPOINT} {where}, then,"
        " once the endpoint is gone, ",
        f"aws ec2 delete-security-group --group-id {GUARDDUTY_GROUP} {where})",
    ):
        assert command in first.err, first.err


def test_the_network_message_names_the_group_as_the_console_does(ran):
    """The group is named so that the reader can find it: the group GuardDuty made, with
    its ID."""
    first, _second = ran["guardduty"].done
    what = first.err.splitlines()[0]
    assert (
        f"GuardDuty put its own endpoint in it ({GUARDDUTY_ENDPOINT}), with the group"
        f" GuardDuty made for it ({GUARDDUTY_GROUP})." in what
    ), what


@pytest.mark.parametrize("name", ["secret-already-gone", "gone-at-the-delete"])
def test_what_is_already_gone_is_done(ran, before, name):
    one = ran[name]
    assert one.done.code == 0, one.done.err
    assert kept_list_is_the_residue(one.world, before) == []


def test_the_database_secret_cloudformation_left_waiting_is_deleted(ran, before):
    one = ran["db-secret-waiting"]
    assert one.done.code == 0, one.done.err
    assert (
        "Delete the secret experimentation-database-staging-aurora-credentials"
        in one.done.out
    )
    assert kept_list_is_the_residue(one.world, before) == []


# --- F6: install again after an uninstall ----------------------------------------------------------


def test_a_reinstall_after_an_uninstall_starts_afresh(ran):
    """F6: install, uninstall, --plan-only, install, uninstall in one account: each start a
    new INSTALL_ID, so the certificate's idempotency token never repeats, and the secrets
    deleted with no recovery window leave no name in the way."""
    one = ran["reinstall"]
    runs = one.done
    assert [r.code for r in runs] == [0, 0, 0, 0, 0], [
        (r.code, r.err[:300]) for r in runs
    ]
    tokens = [
        c["argv"][c["argv"].index("--idempotency-token") + 1]
        for c in one.world.calls
        if c["op"] == "request-certificate"
    ]
    assert len(tokens) == 2 and tokens[0] != tokens[1], tokens
    history = sorted((one.world.record / "history").iterdir())
    assert len(history) == 1, history
    assert "already scheduled for deletion" not in runs[3].err
    assert NOTHING_EXPLAINED in runs[4].out


# --- the session check, the wait, a deployment, an interrupt ---------------------------------------


def test_a_sign_in_that_ends_stops_before_the_stacks_are_deleted(ran):
    """B3b's uninstall half: the session check before the destroy, with no time window."""
    one = ran["session-ends"]
    assert one.done.code == 1, one.done.err
    assert "so the installer stopped before deleting the nine stacks" in one.done.err
    assert ("cdk", "destroy") not in deletes(one.world)


def test_a_stack_an_earlier_run_left_in_progress_is_waited_for(ran, before):
    one = ran["wait"]
    assert one.done.code == 0, one.done.err
    assert (
        "Waiting for AWS: experimentation-fargate-staging (UPDATE_IN_PROGRESS), started by an earlier run."
        in one.done.out
    )
    assert kept_list_is_the_residue(one.world, before) == []


def test_the_wait_has_a_cap(ran):
    one = ran["wait-cap"]
    assert one.done.code == 1, one.done.err
    assert one.done.err.startswith(
        "FAILED: AWS is still working on experimentation-fargate-staging (UPDATE_IN_PROGRESS) after 2 minutes"
    )
    assert ("cdk", "destroy") not in deletes(one.world)


def test_a_deployment_under_way_refuses_before_anything_is_deleted(ran):
    one = ran["deployment-active"]
    assert one.done.code == 2, one.done.err
    assert (
        "A CodeDeploy deployment of the API is still under way (1 active"
        in one.done.err
    )
    assert "Nothing was deleted." in one.done.err
    assert deletes(one.world) == []


def test_no_codedeploy_application_is_no_deployment(ran):
    one = ran["no-codedeploy-application"]
    assert one.done.code == 0, one.done.err


@pytest.mark.parametrize("name", sorted(SIGNALS))
def test_an_interrupt_during_the_destroy_then_a_run_again_completes(ran, before, name):
    """S7 for uninstall: exit 130, the step in the record, no child left; then the same
    command again carries on and ends with the account as it should be."""
    one = ran[f"interrupted-{name}"]
    first, second = one.done
    world = one.world
    assert first.code == 130, first.err
    assert first.err.startswith(
        'STOPPED: Stopped by Ctrl-C or a signal at "Delete the nine stacks, about 26 minutes". AWS carries on with the stack it was deleting'
    ), first.err
    assert "run uninstall again; it continues" in first.err
    deadline = time.time() + 10
    while any(alive(pid) for pid in world.blocked_pids()) and time.time() < deadline:
        time.sleep(0.1)
    assert [pid for pid in world.blocked_pids() if alive(pid)] == []
    assert world.ran_out() == []
    assert second.code == 0, second.err
    assert kept_list_is_the_residue(world, before) == []


# --- U1: uninstall's own messages keep the rules ----------------------------------------------

BATCH.add(
    "install-only",
    prepare=_installed(),
    run=lambda w: w.uninstall("--domain", DOMAIN),
    stub_sweep="state",
)
BATCH.add(
    "stacks-left",
    prepare=_installed(_cdk(destroy_noop=["experimentation-vpc-staging"])),
    stub_sweep="state",
)


def _no_record_of_before(world: InstallWorld) -> None:
    import shutil

    world.adopt(TEMPLATE["world"])
    shutil.rmtree(world.record / "sweep" / "before")


BATCH.add("compare-failed", prepare=_no_record_of_before, stub_sweep="state")
BATCH.add(
    "incomplete",
    prepare=_installed(
        _state(lambda s: s.update(sweep_fails=["us-west-2/ssm-parameters"]))
    ),
    stub_sweep="state",
)


def _incomplete_then_again(world: InstallWorld):
    """An uninstall that cannot read one list of the account afterwards, then the same
    command again once that list can be read."""
    first = world.uninstall()
    world.edit(lambda s: s.update(sweep_fails=[]))
    world.clear_logs()
    return first, world.uninstall()


BATCH.add(
    "incomplete-again",
    prepare=_installed(
        _state(lambda s: s.update(sweep_fails=["us-west-2/ssm-parameters"]))
    ),
    run=_incomplete_then_again,
    stub_sweep="state",
)

#: Each of uninstall's messages, the scenario that makes it happen, its exit code and what
#: it says (test_install_checks.py holds install's, and checks that every message defined
#: has a case in one of the two).
UNINSTALL_MESSAGES = {
    "msg_install_only": (
        "install-only",
        2,
        "--domain is an option of install, not of uninstall",
    ),
    "msg_no_record": ("f1-no-folder", 2, "This computer has no record of an install"),
    "msg_release_differs": (
        "another-release",
        2,
        "This install was made by Controlvar 0.36.2",
    ),
    "msg_deployment_active": (
        "deployment-active",
        2,
        "A CodeDeploy deployment of the API is still under way",
    ),
    "msg_stack_wait_cap": (
        "wait-cap",
        1,
        "AWS is still working on experimentation-fargate-staging",
    ),
    "msg_stack_delete_failed": (
        "delete-failed",
        1,
        "AWS could not delete AuroraCluster",
    ),
    "msg_network_guardduty": (
        "guardduty",
        1,
        "GuardDuty put its own endpoint in it",
    ),
    "msg_stacks_left": (
        "stacks-left",
        1,
        "Not every one of the nine stacks is gone (experimentation-vpc-staging)",
    ),
    "msg_compare_failed": (
        "compare-failed",
        1,
        "could not compare the account with its record from before the install",
    ),
    "msg_residue": (
        "look-alikes",
        1,
        "the account holds 1 thing(s) that the record from before the install does not",
    ),
    "msg_incomplete": (
        "incomplete",
        1,
        "1 list(s) of the account could not be read afterwards",
    ),
}


def test_every_uninstall_message_ends_a_run_by_the_rules(ran):
    """U1 for uninstall's messages: the exit code its first word names, three parts, the
    log naming it, nothing deleted for a refusal; U11, printable ASCII only."""
    from backend.tests.installer_sweeps.test_install_checks import Case, _check_parts

    broken = {}
    for message, (scenario, code, said) in UNINSTALL_MESSAGES.items():
        one = ran[scenario]
        done = one.done[0] if isinstance(one.done, tuple) else one.done
        problems = _check_parts(Case(message, code, said), one.world, done)
        if problems:
            broken[message] = problems
    assert broken == {}, "\n".join(f"{k}: {v}" for k, v in broken.items())


def test_the_nine_stacks_still_there_keep_the_repositories(ran):
    one = ran["stacks-left"]
    assert ("ecr", "delete-repository") not in deletes(one.world)
    assert not (one.world.record / "env.d" / "UNINSTALLED.sh").exists()


def test_a_list_that_cannot_be_read_afterwards_leaves_the_record_started(ran):
    one = ran["incomplete"]
    assert "Not checked: 1 list(s) could not be read:" in one.done.out
    assert not (one.world.record / "env.d" / "UNINSTALLED.sh").exists()


def test_an_incomplete_uninstall_run_again_ends_clean(ran, before):
    """The comparison could not read a list (exit 1, the record left started); the same
    command again, once the list can be read, compares again and ends clean, UNINSTALLED
    written. Its only changes are the nine stacks' destroys, which find them gone: what is
    gone stays gone."""
    one = ran["incomplete-again"]
    first, second = one.done
    assert first.code == 1, first.err
    assert first.err.startswith(
        "FAILED: The install is removed, but 1 list(s) of the account could not be read afterwards"
    ), first.err
    assert second.code == 0, second.err
    assert DONE in second.out and NOTHING_EXPLAINED in second.out, second.out[-1500:]
    assert (one.world.record / "env.d" / "UNINSTALLED.sh").exists()
    assert deletes(one.world) == [("cdk", "destroy")] * 9
    assert kept_list_is_the_residue(one.world, before) == []


# --- what it says it keeps: the CDK's setup stack, and what a stop left -------------------------


def _line_after(text: str, start: str) -> str:
    lines = text.splitlines()
    return lines[next(n for n, line in enumerate(lines) if line.startswith(start)) + 1]


def test_step_5_says_it_keeps_the_cdks_setup_stack(clean):
    """Step 5 changes nothing (the CDK's setup stack is kept and listed), and its title,
    which an interrupt's message names too, says so."""
    titles = [line for line in clean.done.out.splitlines() if line.startswith("[5/6] ")]
    assert titles and not any("Delete" in title for title in titles), titles
    assert titles[0] == "[5/6] Check the CDK's setup stack, which uninstall keeps ..."


def test_the_plan_lists_the_cdks_setup_stack_as_kept_not_deleted(ran):
    out = ran["plan-only"].done.out
    deleted = _line_after(out, "This deletes the install")
    assert "CDK's setup stack" not in deleted, deleted
    assert deleted.startswith(
        "  2 image repositories holding the release images, 2 secrets"
    ), deleted
    kept = _line_after(out, deleted)
    assert (
        kept.startswith("Keeps: ") and "the CDK's setup stack (CDKToolkit)" in kept
    ), kept


def test_the_state_line_does_not_say_the_kept_stack_waits_to_be_deleted(ran):
    """Everything else gone: the state line says uninstall keeps the CDK's setup stack,
    not that it is still to be deleted."""
    state = ran["incomplete"].done.err.splitlines()[1]
    assert "Still in your account: the CDK's setup stack" not in state, state
    assert "until they are deleted" not in state, state
    assert state.endswith(
        "Nothing else the install made is in your account but the CDK's setup stack, which uninstall keeps."
    ), state


def test_the_stacks_left_keep_only_the_repositories(ran):
    first = ran["stacks-left"].done.err.splitlines()[0]
    assert first == (
        "FAILED: Not every one of the nine stacks is gone (experimentation-vpc-staging), so the"
        " installer keeps the image repositories, which they use."
    ), first


def test_the_state_line_counts_the_stacks_a_failed_destroy_left(ran):
    """Eight stacks deleted, then the network's delete fails: the state line says how many
    of the nine are still there, from the record's facts (one per stack deleted)."""
    first, _second = ran["guardduty"].done
    state = first.err.splitlines()[1]
    assert "Still in your account: the nine stacks" not in state, state
    assert state.startswith(
        f"Deleted so far: the record {DOMAIN} and 8 of the nine stacks. Still in your account:"
        " 1 of the nine stacks, the certificate, "
    ), state
    assert state.endswith(
        "Uninstall keeps the CDK's setup stack this install made (other CDK apps may use it)."
    ), state
