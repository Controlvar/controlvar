"""The AWS installer beta's messages and the checks before its first write.

U1: every message the installer can end a run with is made to happen, by one fault in the
fakes or one thing about this computer, and each keeps the rules: the exit code its first
word names (REFUSED 2, FAILED 1, STOPPED 130); three parts, in order (what happened; what
exists and what it bills; "Next:" and the exact next step); on stderr, nothing before it;
and for code 2, nothing changed in AWS. U11: printable ASCII only, in every output. A new
message without a case here fails test_every_message_has_a_case.

The checks' gates, each a list of runs in parallel: A1 (example.com and the bad values, in
any address, before any AWS call), A2 (the first administrator's address never weaker than
the API's EmailStr), A3 (only the typed account), A4 (no stack of this app in any enabled
region), A5 (the zone usable before any create), A8 (~/.cdk.json), B1 (only an IAM
Identity Center profile, and only its six keys) and B3 (the token rule).
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pytest

from backend.tests.installer_sweeps.harness import accounts, fake_ops_install
from backend.tests.installer_sweeps.harness.install_world import (
    PROFILE,
    Batch,
    InstallWorld,
    alive,
    stop_group,
)
from backend.tests.installer_sweeps.harness.world import SAFETY_NET_SECONDS, Result

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
MESSAGES = REPO_ROOT / "deploy" / "aws-installer" / "lib" / "messages.sh"
ACCOUNT = accounts.ACCOUNT
DOMAIN = accounts.INSTALL_DOMAIN
WORDS = {2: "REFUSED", 1: "FAILED", 130: "STOPPED"}
NOTHING = ("Nothing was created.", "Nothing was changed.", "Nothing was deleted.")
#: The validation record ACM gives the install's name (the fake's own rule).
VALIDATION = f"_{fake_ops_install._id('validation' + DOMAIN, 32)}.{DOMAIN}."


def _zone(state: dict) -> dict:
    return next(
        z for z in state["global"]["hosted_zones"] if z["Name"] == accounts.INSTALL_ZONE
    )


def _us_west_2(state: dict) -> dict:
    return state["regions"]["us-west-2"]


def _stack(
    name: str, status: str = "CREATE_COMPLETE", region: str = "us-west-2"
) -> dict:
    return {
        "StackName": name,
        "StackId": f"arn:aws:cloudformation:{region}:{ACCOUNT}:stack/{name}/{fake_ops_install._id(name, 8)}",
        "CreationTime": accounts.LONG_AGO,
        "StackStatus": status,
    }


def _blocked(state: dict, **extra) -> None:
    state.setdefault("cdk", {}).update(deploy="block", block_seconds=60, **extra)


def _signed_in_elsewhere(state: dict) -> None:
    """The profile signs in to another account (the fake answers the state's account)."""
    state["account"] = "123456789012"
    state["global"]["identity"]["Arn"] = (
        "arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_AdministratorAccess_42b9f361ab85f011/reader"
    )


# --- the runs -------------------------------------------------------------------------------


@dataclass
class Case:
    message: str  # the msg_* function (or the verb's own stop) the run ends with
    code: int
    said: str  # a fragment of what it says
    state: Callable[[dict], None] | None = None  # a change to the account or the fakes
    world: Callable[[InstallWorld], None] | None = None  # a change to this computer
    args: tuple = ()
    yes: bool = True
    defaults: bool = True
    env: dict = field(default_factory=dict)
    run: Callable[[InstallWorld], Result] | None = (
        None  # a run that is not install --yes
    )
    id: str = ""

    @property
    def key(self) -> str:
        return self.id or self.message


def _no_verb(world: InstallWorld) -> Result:
    return world.run(str(world.launcher))


def _uninstall(world: InstallWorld) -> Result:
    return world.run(str(world.launcher), "uninstall", "--account", ACCOUNT)


def _declined(world: InstallWorld) -> Result:
    done, _ = world.on_terminal(typed="111111111112\n")
    return done


def _interrupted_during_the_copy(world: InstallWorld) -> Result:
    return interrupt(world, "crane", "copy", signal.SIGINT, group=True)


def interrupt(world: InstallWorld, tool: str, op: str, sig: int, group: bool) -> Result:
    """An install signalled while ``tool op`` is blocked: to its process group (a
    terminal's Ctrl-C) or to the installer alone (an agent's kill)."""
    process = world.start()
    try:
        deadline = time.time() + SAFETY_NET_SECONDS
        while not any(line.split()[1:3] == [tool, op] for line in world.pid_lines()):
            if process.poll() is not None or time.time() > deadline:
                out, err = process.communicate()
                raise AssertionError(
                    f"{tool} {op} never blocked: exit {process.returncode}\n{out}\n{err}"
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
    return Result(process.returncode, out, err)


def _sweep_fails(world: InstallWorld) -> None:
    (world.clone / "deploy" / "aws-installer" / "lib" / "sweep.sh").write_text(
        "# shellcheck shell=bash\n"
        "sweep_take() {\n"
        '    mkdir -p "$1/us-west-2" || return 2\n'
        "    printf 'READ FAILED: aws ssm describe-parameters --region us-west-2\\n' > \"$1/us-west-2/ssm-parameters.txt\"\n"
        "    return 1\n"
        "}\n"
    )


def _config(world: InstallWorld, extra: str) -> None:
    text = world.write_config().read_text()
    world.write_config(
        text.replace(
            "sso_role_name = AdministratorAccess\n",
            f"sso_role_name = AdministratorAccess\n{extra}",
        )
    )


def _lock_held(world: InstallWorld) -> None:
    held = world.record / "lock" / "held"
    held.mkdir(parents=True)
    (held / "pid").write_text(f"{os.getpid()}\n")


def _started(world: InstallWorld) -> None:
    facts = world.record / "env.d"
    facts.mkdir(parents=True)
    (facts / "STARTED.sh").write_text("export STARTED=2026-10-21T02:00:00Z\n")
    (facts / "STEP.sh").write_text(
        "export STEP='Create the nine stacks, about 24 minutes'\n"
    )


def _no_record_folder(world: InstallWorld) -> None:
    import shutil

    shutil.rmtree(world.home / ".controlvar-aws")
    (world.home / ".controlvar-aws").write_text("a file where the record folder goes\n")


CASES = [
    # The command and its options: before anything is read.
    Case(
        "msg_usage",
        2,
        "controlvar-aws takes a verb, install or uninstall",
        run=_no_verb,
    ),
    Case(
        "msg_not_an_option",
        2,
        "--region is not a beta option",
        args=("--region", "us-east-1"),
    ),
    Case(
        "msg_option_value", 2, "--admin-email needs a value.", args=("--admin-email",)
    ),
    Case(
        "msg_account_missing",
        2,
        "It is missing.",
        defaults=False,
        args=("--domain", DOMAIN, "--alarm-email", "a@b.io", "--aws-profile", PROFILE),
    ),
    Case(
        "msg_account_digits",
        2,
        'It is "1234", which is not 12 digits.',
        args=("--account", "1234"),
    ),
    Case(
        "msg_domain_missing",
        2,
        "--domain is required",
        defaults=False,
        args=("--account", ACCOUNT),
    ),
    Case(
        "msg_domain_unusable",
        2,
        "it starts with a scheme",
        args=("--domain", f"https://{DOMAIN}"),
    ),
    Case(
        "msg_domain_example",
        2,
        "is in example.com, the documentation's example",
        args=("--domain", "app.example.com"),
    ),
    Case(
        "msg_alarm_missing",
        2,
        "--alarm-email is required",
        defaults=False,
        args=("--account", ACCOUNT, "--domain", DOMAIN),
    ),
    Case(
        "msg_email_unusable",
        2,
        "--alarm-email x y@z.io cannot be used: it contains whitespace",
        args=("--alarm-email", "x y@z.io"),
    ),
    Case(
        "msg_profile_missing",
        2,
        "No AWS CLI profile was named",
        defaults=False,
        args=("--account", ACCOUNT, "--domain", DOMAIN, "--alarm-email", "a@b.io"),
    ),
    # This computer.
    Case("msg_record_folder", 2, "could not make the folder", world=_no_record_folder),
    Case(
        "msg_lock",
        2,
        "Another run of the installer is working on the account ending in 1111",
        world=_lock_held,
    ),
    Case(
        "msg_started",
        2,
        'stopped at "Create the nine stacks, about 24 minutes"',
        world=_started,
    ),
    Case(
        "msg_tools",
        2,
        "node: 18.0.0; needs 22 or later",
        state=lambda s: s.update(node_version="v18.0.0"),
    ),
    Case(
        "msg_no_version",
        2,
        "it has no VERSION file",
        world=lambda w: (w.clone / "VERSION").unlink(),
    ),
    Case(
        "msg_old_release",
        2,
        "This folder is release 0.36.0, older than 0.36.1",
        world=lambda w: (w.clone / "VERSION").write_text("0.36.0\n"),
    ),
    Case(
        "msg_no_tag",
        2,
        f"This clone has no tag v{accounts.RELEASE}",
        state=lambda s: s["git"].update(tags=[]),
    ),
    Case(
        "msg_archive",
        2,
        "could not copy release",
        state=lambda s: s.setdefault("faults", []).append(
            {
                "service": "git",
                "op": "archive",
                "mode": "error",
                "exit": 128,
                "stderr": "fatal: not a tree object\n",
            }
        ),
    ),
    Case(
        "msg_cdk_json",
        2,
        "Your ~/.cdk.json holds settings (output)",
        world=lambda w: (w.home / ".cdk.json").write_text('{"output": "elsewhere"}'),
    ),
    Case(
        "msg_tool_failed",
        2,
        "npm ci stopped with an error",
        id="msg_tool_failed-before-any-write",
        world=lambda w: __import__("shutil").rmtree(w.record / "tools" / "cdk-cli"),
        state=lambda s: s.setdefault("faults", []).append(
            {
                "service": "npm",
                "op": "ci",
                "mode": "error",
                "exit": 1,
                "stderr": "npm ERR! network\n",
            }
        ),
    ),
    # The sign-in.
    Case(
        "msg_no_profile", 2, "has no profile nosuch", args=("--aws-profile", "nosuch")
    ),
    Case(
        "msg_not_identity_center",
        2,
        "does not sign in through IAM Identity Center",
        world=lambda w: _config(w, "role_arn = arn:aws:iam::111111111111:role/Admin\n"),
    ),
    Case(
        "msg_probe",
        2,
        "still found credentials (Could not connect to the endpoint URL",
        state=lambda s: s.update(credentials_found_anyway=True),
    ),
    Case(
        "msg_signin_expired",
        2,
        "has expired or was never made",
        state=lambda s: s.setdefault("faults", []).append(
            {
                "service": "sts",
                "op": "get-caller-identity",
                "nth": 1,
                "mode": "error",
                "exit": 255,
                "stderr": "Error loading SSO Token: Token for acme does not exist\n",
            }
        ),
    ),
    Case(
        "msg_identity_unreadable",
        2,
        "could not read which account",
        state=lambda s: s.setdefault("faults", []).append(
            {"service": "sts", "op": "get-caller-identity", "nth": 1, "mode": "EMPTY"}
        ),
    ),
    Case(
        "msg_account_mismatch",
        2,
        "but you are signed in to the account ending in 9012",
        state=lambda s: _signed_in_elsewhere(s),
    ),
    Case(
        "msg_token",
        2,
        "was last renewed 16 minutes ago",
        world=lambda w: w.write_token(age_seconds=16 * 60),
    ),
    # The account.
    Case(
        "msg_read_failed",
        2,
        "could not read the account's enabled regions (UnauthorizedOperation)",
        state=lambda s: s.setdefault("faults", []).append(
            {"service": "ec2", "op": "describe-regions", "mode": "AccessDenied"}
        ),
    ),
    Case(
        "msg_region_missing",
        2,
        "does not have us-east-1 enabled",
        state=lambda s: s.update(enabled_regions=["us-west-2"]),
    ),
    Case(
        "msg_engine",
        2,
        "Aurora PostgreSQL 15.17 on db.t3.medium is not offered there",
        state=lambda s: _us_west_2(s).update(orderable=[]),
    ),
    Case(
        "msg_quota",
        2,
        'The limit "Fargate On-Demand vCPU resource count" in us-west-2 is 2 for this account. The install needs 3.',
        state=lambda s: _us_west_2(s)["quotas"]["L-3032A538"].update(Value=2.0),
    ),
    Case(
        "msg_quota_unreadable",
        2,
        'could not read the limit "Fargate On-Demand vCPU resource count"',
        state=lambda s: s.setdefault("faults", []).append(
            {
                "service": "service-quotas",
                "op": "get-service-quota",
                "nth": 1,
                "mode": "EMPTY",
            }
        ),
    ),
    Case(
        "msg_permission_check",
        2,
        "could not check what your sign-in may do (iam:GetRole",
        state=lambda s: s.setdefault("faults", []).append(
            {
                "service": "iam",
                "op": "get-role",
                "has": ["Role.Arn"],
                "mode": "AccessDenied",
            }
        ),
    ),
    Case(
        "msg_permission_denied",
        2,
        "is not allowed to do 1 things the installer needs, for example iam:CreateRole",
        state=lambda s: s["global"].update(denied_actions=["iam:CreateRole"]),
    ),
    Case(
        "msg_stacks_present",
        2,
        "experimentation-vpc-staging in us-west-2",
        state=lambda s: (
            _us_west_2(s)
            .setdefault("stacks", [])
            .append(_stack("experimentation-vpc-staging"))
        ),
    ),
    Case(
        "msg_cdktoolkit_state",
        2,
        "(the CDKToolkit stack in us-west-2) is UPDATE_ROLLBACK_FAILED",
        state=lambda s: (
            _us_west_2(s)
            .setdefault("stacks", [])
            .append(_stack("CDKToolkit", "UPDATE_ROLLBACK_FAILED"))
        ),
    ),
    Case(
        "msg_repository_present",
        2,
        "An image repository named experimentation-platform/backend already exists",
        state=lambda s: (
            _us_west_2(s)
            .setdefault("repositories", [])
            .append(
                {
                    "repositoryArn": f"arn:aws:ecr:us-west-2:{ACCOUNT}:repository/experimentation-platform/backend",
                    "registryId": ACCOUNT,
                    "repositoryName": "experimentation-platform/backend",
                    "repositoryUri": f"{ACCOUNT}.dkr.ecr.us-west-2.amazonaws.com/experimentation-platform/backend",
                    "createdAt": accounts.LONG_AGO,
                }
            )
        ),
    ),
    Case(
        "msg_secret_present",
        2,
        "A secret named /staging/experimentation/jwt-secret already exists in us-west-2, scheduled for deletion,",
        state=lambda s: (
            _us_west_2(s)
            .setdefault("secrets", [])
            .append(
                {
                    "ARN": f"arn:aws:secretsmanager:us-west-2:{ACCOUNT}:secret:/staging/experimentation/jwt-secret-AbCdEf",
                    "Name": "/staging/experimentation/jwt-secret",
                    "CreatedDate": accounts.LONG_AGO,
                    "DeletedDate": accounts.CREATED,
                    "Tags": [],
                }
            )
        ),
    ),
    # The zone.
    Case(
        "msg_no_zone",
        2,
        "has no public Route 53 zone for app.other.dev",
        args=("--domain", "app.other.dev"),
    ),
    Case(
        "msg_zone_private",
        2,
        "zone beta.controlvar.dev is private",
        state=lambda s: _zone(s)["Config"].update(PrivateZone=True),
    ),
    Case(
        "msg_zone_twice",
        2,
        "holds 2 public zones named beta.controlvar.dev",
        state=lambda s: s["global"]["hosted_zones"].append(
            dict(_zone(s), Id="/hostedzone/Z0SECONDZONE00000000")
        ),
    ),
    Case(
        "msg_zone_not_delegated",
        2,
        "the internet does not use it: public DNS names ns-1.elsewhere.net",
        state=lambda s: (
            s.setdefault("dns", {})
            .setdefault("public_ns", {})
            .update(
                {
                    "beta.controlvar.dev": [
                        "ns-1.elsewhere.net",
                        "ns-2.elsewhere.net",
                        "ns-3.elsewhere.net",
                        "ns-4.elsewhere.net",
                    ]
                }
            )
        ),
    ),
    Case(
        "msg_zone_top",
        2,
        "beta.controlvar.dev is the top of its zone",
        args=("--domain", "beta.controlvar.dev"),
    ),
    Case(
        "msg_record_present",
        2,
        f"{DOMAIN} already has a TXT record",
        state=lambda s: (
            _zone(s)
            .setdefault("records", [])
            .append(
                {
                    "Name": DOMAIN + ".",
                    "Type": "TXT",
                    "TTL": 300,
                    "ResourceRecords": [{"Value": '"x"'}],
                }
            )
        ),
    ),
    # The confirmation and the record of the account.
    Case(
        "msg_no_terminal",
        2,
        "there is no terminal to type the account ID on",
        yes=False,
    ),
    Case("msg_not_confirmed", 2, "Not confirmed.", run=_declined),
    Case(
        "msg_sweep_failed",
        2,
        "could not read 1 of its lists (the first: us-west-2/ssm-parameters.txt)",
        world=_sweep_fails,
    ),
    # The release images.
    Case(
        "msg_image",
        2,
        "is not signed by Controlvar's release workflow",
        state=lambda s: next(
            iter(s["registry"]["ghcr.io/controlvar/controlvar"]["signatures"].values())
        ).update(issuer="https://accounts.google.com"),
    ),
    Case(
        "msg_image_missing",
        2,
        f"there is no tag core-{accounts.RELEASE}",
        state=lambda s: s["registry"]["ghcr.io/controlvar/controlvar-web"][
            "tags"
        ].clear(),
    ),
    Case(
        "msg_unreachable",
        2,
        "could not reach 1.1.1.1",
        state=lambda s: s.setdefault("faults", []).append(
            {
                "service": "dig",
                "mode": "error",
                "exit": 9,
                "stderr": ";; connection timed out; no servers could be reached\n",
            }
        ),
    ),
    # After the first write: each ends FAILED, saying what exists.
    Case(
        "msg_tool_failed",
        1,
        "cdk bootstrap stopped with an error (exit code 1)",
        state=lambda s: s.setdefault("faults", []).append(
            {
                "service": "cdk",
                "op": "bootstrap",
                "mode": "error",
                "exit": 1,
                "stderr": "bootstrap failed\n",
            }
        ),
    ),
    Case(
        "msg_validation_record",
        1,
        f"The certificate's validation record {VALIDATION} already holds something else (CNAME)",
        state=lambda s: (
            _zone(s)
            .setdefault("records", [])
            .append(
                {
                    "Name": VALIDATION,
                    "Type": "CNAME",
                    "TTL": 300,
                    "ResourceRecords": [{"Value": "somewhere.else.example.org."}],
                }
            )
        ),
    ),
    Case(
        "msg_service_role",
        1,
        "AWS did not create the ECS service-linked role",
        state=lambda s: s.setdefault("faults", []).append(
            {
                "service": "iam",
                "op": "create-service-linked-role",
                "mode": "AccessDenied",
            }
        ),
    ),
    Case(
        "msg_image_copied_wrong",
        1,
        "is not the one that was verified",
        state=lambda s: s.setdefault("crane", {}).update(
            copy="corrupt", corrupt_digest="sha256:" + "0" * 64
        ),
    ),
    Case(
        "msg_certificate_failed",
        1,
        "AWS could not issue the certificate for app.beta.controlvar.dev: it is FAILED (CAA_ERROR)",
        state=lambda s: s.setdefault("acm", {}).update(issues_as="FAILED"),
    ),
    Case(
        "msg_certificate_timeout",
        1,
        "AWS had still not issued the certificate",
        state=lambda s: s.setdefault("acm", {}).update(never_validates=True),
        env={"CONTROLVAR_AWS_CERT_MINUTES": "0"},
    ),
    Case(
        "msg_session_ended",
        1,
        "stopped working (no refresh token in its cache)",
        world=lambda w: w.effect(
            service="crane",
            op="copy",
            file=str(w.token_path()),
            drop_key="refreshToken",
        ),
    ),
    Case(
        "msg_target",
        1,
        "is not the nine staging stacks",
        state=lambda s: s.setdefault("cdk", {}).update(
            extra_stacks=["experimentation-extra-staging"]
        ),
    ),
    Case(
        "msg_stack_failed",
        1,
        "AWS stopped creating the stack experimentation-database-staging: Resource failed with: Aurora said no",
        state=lambda s: s.setdefault("cdk", {}).update(
            fail={"experimentation-database-staging": "Aurora said no"}
        ),
    ),
    Case(
        "msg_api_cannot_start",
        1,
        "The API service cannot start its tasks: a service event names the service-linked role",
        state=lambda s: _blocked(
            s,
            service_events=[
                "(service experimentation-backend-staging) was unable to assume the service-linked role"
            ],
        ),
    ),
    Case(
        "msg_watch_cap",
        1,
        "longer than the installer waits",
        state=_blocked,
        env={"CONTROLVAR_AWS_WATCH_CAP_MINUTES": "0"},
    ),
    Case(
        "msg_watch_read",
        1,
        "could not read the stack experimentation-fargate-staging while AWS created it (Throttling)",
        state=lambda s: (
            _blocked(s),
            s.setdefault("faults", []).append(
                {
                    "service": "cloudformation",
                    "op": "describe-stacks",
                    "has": ["Stacks[0].StackStatus"],
                    "mode": "error",
                    "code": "Throttling",
                    "exit": 254,
                }
            ),
        ),
    ),
    Case(
        "msg_stacks_not_complete",
        1,
        "not every one of the nine stacks is complete",
        state=lambda s: s.setdefault("faults", []).append(
            {
                "service": "cloudformation",
                "op": "list-stacks",
                "has": [
                    "StackSummaries[?starts_with(StackName, 'experimentation-') && StackStatus!='DELETE_COMPLETE'].[StackName,StackStatus]"
                ],
                "mode": "AccessDenied",
            }
        ),
    ),
    Case(
        "msg_alias_record",
        1,
        f"{DOMAIN} now has a TXT record that was not there when the installer checked",
        state=lambda s: s.setdefault("cdk", {}).update(add_host_record=True),
    ),
    Case(
        "msg_migration_failed",
        1,
        "The database setup task stopped without creating the first administrator",
        state=lambda s: s["app"].update(migration_exit=1),
    ),
    Case(
        "msg_check_failed",
        1,
        "The install is created, but a check from the internet failed",
        state=lambda s: s["app"].update(
            override={"/health": [503, {"status": "unhealthy"}]}
        ),
    ),
    Case(
        "msg_interrupted",
        130,
        'Stopped by Ctrl-C or a signal at "Copy the release images',
        state=lambda s: s.setdefault("crane", {}).update(
            copy="block", block_seconds=60
        ),
        run=_interrupted_during_the_copy,
    ),
    # Uninstall, in this build of the beta.
    Case(
        "cv_uninstall",
        2,
        "uninstall is not in this build of the beta yet.",
        run=_uninstall,
    ),
]


def _check_parts(case: Case, world: InstallWorld, done: Result) -> list[str]:
    """What is wrong with how the run ended, as a list of problems."""
    problems = []
    if done.code != case.code:
        problems.append(f"exit {done.code}, not {case.code}: {done.err[-600:]}")
        return problems
    lines = done.err.splitlines()
    word = WORDS[case.code]
    if not lines or not lines[0].startswith(word + ": "):
        problems.append(f"stderr does not start with {word}: {lines[:2]}")
        return problems
    nexts = [i for i, line in enumerate(lines) if line.startswith("Next: ")]
    if len(nexts) != 1 or nexts[0] < 2:
        problems.append(f"no single Next: line after a state line: {lines}")
        return problems
    state, rest = lines[nexts[0] - 1], lines[nexts[0] + 1 :]
    if case.code == 2 and state not in NOTHING:
        problems.append(f"a refusal says something exists: {state!r}")
    if case.code in (1, 130) and not state.startswith(
        ("Created so far: ", "Now in your account: ", "Nothing was created.")
    ):
        problems.append(f"the state part is missing: {state!r}")
    if case.code == 1 and not state.startswith(
        ("Created so far: ", "Now in your account: ")
    ):
        problems.append(f"a failure after a write does not say what exists: {state!r}")
    if [line for line in rest if not line.startswith("Log: ")]:
        problems.append(f"lines after the message: {rest}")
    if case.said not in done.err:
        problems.append(f"it does not say {case.said!r}: {done.err[:600]}")
    if case.code == 2 and world.writes():
        problems.append(f"a refusal after changes: {world.writes()}")
    for name, text in (("stdout", done.out), ("stderr", done.err)):
        bad = sorted(
            {hex(ord(ch)) for ch in text if not (ch == "\n" or " " <= ch <= "~")}
        )
        if bad:
            problems.append(f"{name} holds {bad}")
    log = world.log_text()
    if log and f"message: {case.message}, exit {case.code}" not in log:
        problems.append(
            f"the log does not name {case.message}: {re.findall(r'message: .*', log)}"
        )
    return problems


#: Every run this module makes, at once, each in its own fake account (Batch); each test
#: reads its own.
BATCH = Batch()


def _prepare_case(case: Case):
    def prepare(world: InstallWorld) -> None:
        if case.state is not None:
            world.edit(case.state)
        if case.world is not None:
            case.world(world)

    return prepare


def _run_of(case: Case):
    if case.run is not None:
        return case.run
    return lambda world: world.install(
        *case.args, yes=case.yes, defaults=case.defaults, **case.env
    )


for _case in CASES:
    BATCH.add(
        f"u1-{_case.key}",
        prepare=_prepare_case(_case),
        run=_run_of(_case),
        serial=_case.run is _declined,
    )


@pytest.fixture(scope="module")
def ran(tmp_path_factory, shapes_file):
    found = BATCH.run_all(tmp_path_factory.mktemp("install-checks"), shapes_file)
    yield found
    unknown = {name: r.world.unknown for name, r in found.items() if r.world.unknown}
    assert unknown == {}, unknown


def test_every_message_ends_a_run_by_the_rules(ran):
    """U1 and U11, every case; a failure names each message that broke a rule."""
    broken = {}
    for case in CASES:
        one = ran[f"u1-{case.key}"]
        if one.error is not None:
            broken[case.key] = [f"{type(one.error).__name__}: {one.error}"]
            continue
        problems = _check_parts(case, one.world, one.done)
        if problems:
            broken[case.key] = problems
    assert broken == {}, "\n".join(f"{k}: {v}" for k, v in broken.items())


def test_every_message_has_a_case():
    defined = set(re.findall(r"^(msg_[a-z_]+)\(\) \{$", MESSAGES.read_text(), re.M))
    covered = {case.message for case in CASES}
    assert defined - covered == set(), (
        f"messages no case makes happen: {sorted(defined - covered)}"
    )
    assert covered - defined == {"cv_uninstall"}, sorted(covered - defined)


# --- the signals (E3) -----------------------------------------------------------------------


SIGNAL_STEPS = {
    "during-the-image-copy": (
        "crane",
        "copy",
        {"crane": {"copy": "block", "block_seconds": 60}},
        "Copy the release images into the repositories, 253 MiB, and wait for the certificate",
    ),
    "during-the-deploy": (
        "cdk",
        "deploy",
        {"cdk": {"deploy": "block", "block_seconds": 60}},
        "Create the nine stacks, about 24 minutes",
    ),
}
SIGNALS = {"INT": signal.SIGINT, "TERM": signal.SIGTERM}
SIGNAL_TARGETS = {"to-the-group": True, "to-the-installer": False}
for _when, (_tool, _op, _settings, _step) in SIGNAL_STEPS.items():
    for _sig_name, _sig in SIGNALS.items():
        for _target, _group in SIGNAL_TARGETS.items():
            BATCH.add(
                f"e3-{_when}-{_target}-{_sig_name}",
                prepare=lambda w, s=_settings: w.edit(lambda state: state.update(s)),
                run=lambda w, t=_tool, o=_op, sg=_sig, g=_group: interrupt(
                    w, t, o, sg, g
                ),
            )


@pytest.mark.parametrize("sig", sorted(SIGNALS))
@pytest.mark.parametrize("target", sorted(SIGNAL_TARGETS))
@pytest.mark.parametrize("when", sorted(SIGNAL_STEPS))
def test_an_interrupt_leaves_a_usable_record_and_no_child(ran, sig, target, when):
    """E3: exit 130, "interrupted at <step>" in the record, no child left (the blocked
    tool, the watcher, the sleeps), tmp/ emptied, and nothing more done after it."""
    tool, op, _, step = SIGNAL_STEPS[when]
    world, done = (
        ran[f"e3-{when}-{target}-{sig}"].world,
        ran[f"e3-{when}-{target}-{sig}"].done,
    )
    assert done.code == 130, done.err
    assert done.err.startswith(f'STOPPED: Stopped by Ctrl-C or a signal at "{step}".')
    assert (
        world.record / "env.d" / "INTERRUPTED_AT.sh"
    ).read_text() == f"export INTERRUPTED_AT={_q(step)}\n"
    assert list((world.record / "tmp").iterdir()) == []
    assert list((world.record / "docker-config").iterdir()) == []
    deadline = time.time() + 10
    while any(alive(pid) for pid in world.blocked_pids()) and time.time() < deadline:
        time.sleep(0.1)
    assert [pid for pid in world.blocked_pids() if alive(pid)] == [], (
        "a child outlived the installer"
    )
    writes = world.writes()
    assert writes[-1] == (tool, op), writes
    if tool == "cdk":
        assert "AWS carries on creating the stacks on its own." in done.err


def _q(text: str) -> str:
    done = subprocess.run(
        ["bash", "-c", 'printf %q "$1"', "_", text],
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout


# --- the options and the addresses (A1, A2) ------------------------------------------------------

#: The bad values of the AWS guide's run (its first administrator's address), verbatim
#: where an option can carry them: unset, empty, the placeholder, three at example.com, a
#: trailing dot, a space, two @, no name, no domain, no dot.
BAD_ADDRESSES = [
    ("--admin-email",),
    ("--admin-email", ""),
    ("--admin-email", "<FIRST_ADMIN_EMAIL>"),
    ("--admin-email", "admin@example.com"),
    ("--admin-email", "ops.mail@Example.COM"),
    ("--admin-email", "x@sub.example.com"),
    ("--admin-email", "x@example.com."),
    ("--admin-email", "a b@corp.io"),
    ("--admin-email", "a@b@corp.io"),
    ("--admin-email", "@corp.io"),
    ("--admin-email", "ops@"),
    ("--admin-email", "ops@localhost"),
]
EXAMPLE_ELSEWHERE = [
    ("--alarm-email", "alerts@example.com"),
    ("--alarm-email", "Alerts@Sub.Example.Org"),
    ("--alarm-email", "alerts@example.net."),
    ("--domain", "app.example.com"),
    ("--domain", "app.sub.example.org"),
    ("--domain", "APP.EXAMPLE.NET"),
    ("--domain", "example.com"),
]


for _i, _value in enumerate(BAD_ADDRESSES + EXAMPLE_ELSEWHERE):
    BATCH.add(f"a1-{_i}", run=lambda w, v=_value: w.install(*v))


@pytest.mark.parametrize("group", ["admin", "alarm-and-domain"])
def test_a_bad_address_is_refused_before_any_aws_call(ran, group):
    """A1: exit 2 and no call to AWS (only the local tools' own version reads and the
    release's archive, which the address rules come from)."""
    first = 0 if group == "admin" else len(BAD_ADDRESSES)
    values = BAD_ADDRESSES if group == "admin" else EXAMPLE_ELSEWHERE
    wrong = {}
    for i, value in enumerate(values, first):
        world, done = ran[f"a1-{i}"].world, ran[f"a1-{i}"].done
        if done.code != 2 or world.aws_calls or not done.err.startswith("REFUSED: "):
            wrong[" ".join(value)] = (
                done.code,
                [(c["service"], c["op"]) for c in world.aws_calls],
                done.err[:200],
            )
    assert wrong == {}, wrong


#: The differential corpus: the addresses alarm_email.py accepts but the API's EmailStr
#: refuses (measured), the run's three that half-build the schema, and ordinary ones.
CORPUS = [
    "ops@corp.test",
    "ops@my.local",
    "ops@corp.invalid",
    "ops@corp.io.",
    "o..ps@corp.io",
    ".ops@corp.io",
    '"quoted"@corp.io',
    "me@corp.local",
    "a..b@x.com",
    "a;b@x.com",
    "a,b@x.com",
    "ops@corp.io,",
    "ops@[192.0.2.1].io",
    "ops@bücher.ch",
    "o'brien@corp.io",
    "ops@corp-example.io",
    "founder@controlvar.com",
    "a@b.c",
    "admin@localhost.localdomain",
    "ops@corp.intranet",
    "x@ex.example",
    "first.last+tag@gmail.com",
    "Ops@Controlvar.COM",
    "ops@xn--bcher-kva.ch",
    "ops@sub.corp.co.uk",
    "ops@corp.onion",
    "ops@corp.arpa",
    "ops@-corp.io",
    "ops@corp-.io",
    "ops@corp.i",
    "ops@corp.123",
    "ops.@corp.io",
    "op s@corp.io",
    "x" * 65 + "@corp.io",
    "ops@" + "a" * 64 + ".io",
    "ops@corp..io",
    "ops@.corp.io",
    "OPS@CORP.IO",
    "ops%tag@corp.io",
    "ops+tag@corp.io",
    "ops_x@corp.io",
    "ops-x@corp.io",
]
#: The seven addresses alarm_email.py accepts and EmailStr refuses (measured).
EMAILSTR_SEVEN = [
    "ops@corp.test",
    "ops@my.local",
    "ops@corp.invalid",
    "ops@corp.io.",
    "o..ps@corp.io",
    ".ops@corp.io",
    '"quoted"@corp.io',
]


def _beta_accepts(world: InstallWorld, addresses: list[str]) -> dict[str, bool]:
    """The beta's own rules for the first administrator (lib/checks.sh's cv_check_emails),
    run by bash on each address, with the release's alarm_email.py as the install copies
    it."""
    record = world.root / "rules"
    stacks = record / "src" / "infrastructure" / "cdk" / "stacks"
    stacks.mkdir(parents=True)
    (stacks / "alarm_email.py").write_text(
        (REPO_ROOT / "infrastructure/cdk/stacks/alarm_email.py").read_text()
    )
    (record / "tmp").mkdir()
    lib = world.clone / "deploy" / "aws-installer" / "lib"
    script = (
        f'. "{lib}/names.sh"; . "{lib}/env.sh"; . "{lib}/messages.sh"; . "{lib}/checks.sh"; '
        f'CV_RECORD="{record}"; CV_LAUNCHER=x; for a in "$@"; do '
        '( CV_ALARM_EMAIL="ops@corp.io"; CV_ADMIN_EMAIL="$a"; cv_check_emails ) > /dev/null 2>&1 '
        '&& printf "accept\\n" || printf "refuse\\n"; done'
    )
    done = subprocess.run(
        ["bash", "-c", script, "_", *addresses],
        capture_output=True,
        text=True,
        env=world.install_env(),
        timeout=SAFETY_NET_SECONDS,
    )
    verdicts = done.stdout.split()
    assert len(verdicts) == len(addresses), done
    return {a: v == "accept" for a, v in zip(addresses, verdicts)}


def test_the_first_administrators_address_is_never_weaker_than_the_apis(make_install):
    """A2: every address the beta accepts, the API's EmailStr accepts."""
    from pydantic import EmailStr, TypeAdapter, ValidationError

    adapter = TypeAdapter(EmailStr)

    def api_accepts(address: str) -> bool:
        try:
            adapter.validate_python(address)
        except ValidationError:
            return False
        return True

    beta = _beta_accepts(make_install(), CORPUS)
    weaker = [a for a in CORPUS if beta[a] and not api_accepts(a)]
    assert weaker == [], [f"beta accepts {a}; EmailStr refuses" for a in weaker]
    assert sum(beta.values()) >= 10, beta  # the rule is not "refuse everything"


for _i, _address in enumerate(EMAILSTR_SEVEN):
    BATCH.add(f"a2-{_i}", run=lambda w, a=_address: w.install("--admin-email", a))


def test_the_seven_are_refused_before_aws(ran):
    """A2 (U3): the seven addresses EmailStr refuses, as --admin-email, end at a refusal
    with no call to AWS."""
    wrong = {}
    for i, address in enumerate(EMAILSTR_SEVEN):
        world, done = ran[f"a2-{i}"].world, ran[f"a2-{i}"].done
        if done.code != 2 or world.aws_calls:
            wrong[address] = (done.code, len(world.aws_calls))
    assert wrong == {}, wrong


# --- the account (A3, A4) -------------------------------------------------------------------

BATCH.add("a3", prepare=lambda w: w.edit(_signed_in_elsewhere))


def test_only_the_typed_account(ran):
    """A3: the sign-in is another account: the only AWS calls are the probe and the one
    identity read; nothing after."""
    world, done = ran["a3"].world, ran["a3"].done
    assert done.code == 2, done.err
    assert "but you are signed in to the account ending in 9012" in done.err
    calls = [(c["service"], c["op"], c["exit"]) for c in world.aws_calls]
    assert calls == [
        ("sts", "get-caller-identity", 253),
        ("sts", "get-caller-identity", 0),
    ], calls


def _with_eu_west_1(state: dict) -> None:
    state["enabled_regions"] = ["us-west-2", "us-east-1", "eu-west-1"]
    state["regions"].setdefault("eu-west-1", {})


A4_CASES = [
    (
        "a-stack-in-us-west-2",
        lambda s: (
            _us_west_2(s)
            .setdefault("stacks", [])
            .append(_stack("experimentation-vpc-staging"))
        ),
        2,
        "experimentation-vpc-staging in us-west-2",
    ),
    (
        "a-stack-in-us-east-1-only",
        lambda s: (
            s["regions"]["us-east-1"]
            .setdefault("stacks", [])
            .append(_stack("experimentation-auth-prod", region="us-east-1"))
        ),
        2,
        "experimentation-auth-prod in us-east-1",
    ),
    (
        "a-stack-in-a-third-region",
        lambda s: (
            _with_eu_west_1(s),
            s["regions"]["eu-west-1"]
            .setdefault("stacks", [])
            .append(_stack("experimentation-vpc-dev", region="eu-west-1")),
        ),
        2,
        "experimentation-vpc-dev in eu-west-1",
    ),
    (
        "a-stack-being-deleted",
        lambda s: (
            _us_west_2(s)
            .setdefault("stacks", [])
            .append(_stack("experimentation-redis-staging", "DELETE_IN_PROGRESS"))
        ),
        2,
        "experimentation-redis-staging in us-west-2",
    ),
    (
        "access-denied-in-us-west-2",
        lambda s: s.setdefault("faults", []).append(
            {
                "service": "cloudformation",
                "op": "list-stacks",
                "region": "us-west-2",
                "mode": "AccessDenied",
            }
        ),
        2,
        "could not read the stacks in us-west-2 (AccessDenied)",
    ),
    (
        "a-connection-error",
        lambda s: s.setdefault("faults", []).append(
            {
                "service": "cloudformation",
                "op": "list-stacks",
                "region": "us-east-1",
                "mode": "error",
                "exit": 255,
                "stderr": "Could not connect to the endpoint URL\n",
            }
        ),
        2,
        "could not read the stacks in us-east-1 (no answer)",
    ),
    (
        "an-expired-token",
        lambda s: s.setdefault("faults", []).append(
            {
                "service": "cloudformation",
                "op": "list-stacks",
                "region": "us-east-1",
                "mode": "error",
                "code": "ExpiredToken",
                "exit": 254,
            }
        ),
        2,
        "could not read the stacks in us-east-1 (ExpiredToken)",
    ),
    (
        "throttling",
        lambda s: s.setdefault("faults", []).append(
            {
                "service": "cloudformation",
                "op": "list-stacks",
                "region": "us-east-1",
                "mode": "error",
                "code": "Throttling",
                "exit": 254,
            }
        ),
        2,
        "could not read the stacks in us-east-1 (Throttling)",
    ),
    (
        "an-empty-answer",
        lambda s: s.setdefault("faults", []).append(
            {
                "service": "cloudformation",
                "op": "list-stacks",
                "region": "us-east-1",
                "mode": "EMPTY",
            }
        ),
        2,
        "could not read the stacks in us-east-1 (an answer that is not a list of stacks)",
    ),
    (
        "access-denied-in-us-east-1",
        lambda s: s.setdefault("faults", []).append(
            {
                "service": "cloudformation",
                "op": "list-stacks",
                "region": "us-east-1",
                "mode": "AccessDenied",
            }
        ),
        0,
        "Not checked: us-east-1",
    ),
]


for _name, _change, *_rest in A4_CASES:
    BATCH.add(
        f"a4-{_name}",
        prepare=lambda w, c=_change: w.edit(c),
        run=lambda w: w.install("--plan-only"),
    )


def test_an_account_with_this_apps_stacks_is_refused_before_the_prompt(ran):
    """A4: a stack of this app in any enabled region, or a stack scan that fails in any
    way but exactly AccessDenied outside us-west-2, refuses before the confirmation; the
    exact AccessDenied elsewhere is "not checked" and the install goes on."""
    wrong = {}
    for name, _, code, said in A4_CASES:
        world, done = ran[f"a4-{name}"].world, ran[f"a4-{name}"].done
        text = done.out + done.err
        if done.code != code or said not in text or world.writes():
            wrong[name] = (done.code, text[-400:])
        elif code == 2 and "This creates in account" in done.out:
            wrong[name] = "refused after the plan"
    assert wrong == {}, wrong


# --- the zone (A5) --------------------------------------------------------------------------


A5_CASES = [
    (
        "no-such-zone",
        lambda s: s["global"]["hosted_zones"].remove(_zone(s)),
        "has no public Route 53 zone for app.beta.controlvar.dev",
    ),
    ("private", lambda s: _zone(s)["Config"].update(PrivateZone=True), "is private"),
    (
        "two-public-zones",
        lambda s: s["global"]["hosted_zones"].append(
            dict(_zone(s), Id="/hostedzone/Z0SECONDZONE00000000")
        ),
        "holds 2 public zones",
    ),
    (
        "a-record-at-the-host",
        lambda s: (
            _zone(s)
            .setdefault("records", [])
            .append(
                {
                    "Name": DOMAIN + ".",
                    "Type": "CNAME",
                    "TTL": 60,
                    "ResourceRecords": [{"Value": "elsewhere.io."}],
                }
            )
        ),
        "already has a CNAME record",
    ),
    (
        "public-ns-not-the-zones",
        lambda s: (
            s.setdefault("dns", {})
            .setdefault("public_ns", {})
            .update(
                {
                    "beta.controlvar.dev": [
                        "ns-9.elsewhere.net",
                        "ns-8.elsewhere.net",
                        "ns-7.elsewhere.net",
                        "ns-6.elsewhere.net",
                    ]
                }
            )
        ),
        "the internet does not use it",
    ),
    (
        "three-of-the-four",
        lambda s: (
            s.setdefault("dns", {})
            .setdefault("public_ns", {})
            .update({"beta.controlvar.dev": _zone(s)["NameServers"][:3]})
        ),
        "the internet does not use it",
    ),
    (
        "five-servers",
        lambda s: (
            s.setdefault("dns", {})
            .setdefault("public_ns", {})
            .update(
                {
                    "beta.controlvar.dev": _zone(s)["NameServers"]
                    + ["ns-5.elsewhere.net"]
                }
            )
        ),
        "the internet does not use it",
    ),
    (
        "no-answer",
        lambda s: (
            s.setdefault("dns", {})
            .setdefault("public_ns", {})
            .update({"beta.controlvar.dev": []})
        ),
        "public DNS names none",
    ),
    (
        # The internet names the same three: only the count of four refuses it.
        "a-zone-of-three-servers",
        lambda s: _zone(s).update(NameServers=_zone(s)["NameServers"][:3]),
        "an answer that is not four name servers",
    ),
]


for _name, _change, _said in A5_CASES:
    BATCH.add(f"a5-{_name}", prepare=lambda w, c=_change: w.edit(c))
BATCH.add(
    "a5-the-zones-top", run=lambda w: w.install("--domain", "beta.controlvar.dev")
)


def test_a_zone_that_cannot_take_the_install_is_refused_before_any_write(ran):
    """A5, and the zone's top (the --domain of the zone itself)."""
    wrong = {}
    for name, _, said in A5_CASES + [("the-zones-top", None, "is the top of its zone")]:
        world, done = ran[f"a5-{name}"].world, ran[f"a5-{name}"].done
        if done.code != 2 or said not in done.err or world.writes():
            wrong[name] = (done.code, done.err[:300], world.writes())
    assert wrong == {}, wrong


# --- this computer (A8, B1, B3) ---------------------------------------------------------------


A8_REFUSED = [
    ('{"context": {"availability-zones": ["x"]}}', "holds settings (context)"),
    ('{"output": "elsewhere"}', "holds settings (output)"),
    ('{"plugin": ["./evil.js"]}', "holds settings (plugin)"),
    ('{"rollback": false}', "holds settings (rollback)"),
    (
        '{"toolkitStackName": "Other", "profile": "x"}',
        "holds settings (profile, toolkitStackName)",
    ),
    ("{not json", "does not parse as a JSON object"),
    ("[]", "does not parse as a JSON object"),
    ("null", "does not parse as a JSON object"),
    ("", "does not parse as a JSON object"),
]


for _i, (_text, _said) in enumerate(A8_REFUSED + [("{}", "")]):
    BATCH.add(
        f"a8-{_i}",
        prepare=lambda w, t=_text: (w.home / ".cdk.json").write_text(t),
        run=lambda w: w.install("--plan-only"),
    )


def test_a_cdk_json_with_anything_in_it_is_refused_before_any_aws_call(ran):
    """A8: the CDK CLI applies every setting in ~/.cdk.json as a user default; only an
    empty object, or no file, passes."""
    wrong = {}
    for i, (text, said) in enumerate(A8_REFUSED):
        world, done = ran[f"a8-{i}"].world, ran[f"a8-{i}"].done
        if (
            done.code != 2
            or said not in done.err
            or world.aws_calls
            or any(c["service"] == "cdk" for c in world.calls if c["op"] != "--version")
        ):
            wrong[text] = (done.code, done.err[:200])
    assert wrong == {}, wrong
    empty = ran[f"a8-{len(A8_REFUSED)}"].done
    assert empty.code == 0, empty.err


B1_REFUSED = [
    ("aws_access_key_id = shell-key\n", "msg_not_identity_center"),
    ("credential_process = /bin/true\n", "msg_not_identity_center"),
    (
        "role_arn = arn:aws:iam::111111111111:role/Admin\nsource_profile = base\n",
        "msg_not_identity_center",
    ),
    ("source_profile = base\n", "msg_not_identity_center"),
]


def _legacy(world: InstallWorld) -> None:
    world.write_config(
        f"[profile {PROFILE}]\nsso_start_url = https://acme.awsapps.com/start\nsso_region = us-east-1\n"
        f"sso_account_id = {ACCOUNT}\nsso_role_name = AdministratorAccess\nregion = us-west-2\n"
    )


for _i, (_extra, _) in enumerate(B1_REFUSED):
    BATCH.add(
        f"b1-{_i}",
        prepare=lambda w, e=_extra: _config(w, e),
        run=lambda w: w.install("--plan-only"),
    )
BATCH.add(
    f"b1-{len(B1_REFUSED)}", prepare=_legacy, run=lambda w: w.install("--plan-only")
)
BATCH.add(
    f"b1-{len(B1_REFUSED) + 1}",
    prepare=lambda w: w.edit(
        lambda s: s["global"]["identity"].update(Arn=f"arn:aws:iam::{ACCOUNT}:user/bob")
    ),
    run=lambda w: w.install("--plan-only"),
)


def test_a_profile_that_is_not_identity_center_is_refused(ran):
    """B1: keys, a credential process, a role to assume, a source profile, or a legacy
    profile with no sso_session: refused, with the copy never made from them; and an IAM
    user's or root's sign-in (no AWSReservedSSO_ role)."""
    wrong = {}
    for i in range(len(B1_REFUSED) + 2):
        world, done = ran[f"b1-{i}"].world, ran[f"b1-{i}"].done
        if (
            done.code != 2
            or "does not sign in through IAM Identity Center" not in done.err
        ):
            wrong[i] = (done.code, done.err[:200])
        if i < len(B1_REFUSED) + 1 and [c for c in world.aws_calls if c["exit"] != 253]:
            wrong[i] = "an AWS call before the refusal"
    assert wrong == {}, wrong


B3_CASES = [
    (
        "sixteen-minutes-old",
        {"age_seconds": 16 * 60},
        "was last renewed 16 minutes ago",
    ),
    (
        "twenty-nine-minutes-left",
        {"left_seconds": 29 * 60},
        "was last renewed 1 minutes ago",
    ),
    ("no-refresh-token", {"refresh": False}, "was last renewed 1 minutes ago"),
    (
        "another-sessions-cache",
        {"session": "other"},
        "at a time the installer cannot read",
    ),
]


def _token(world: InstallWorld, token: dict) -> None:
    world.token_path().unlink()
    world.write_token(**{"age_seconds": 60, **token})


for _name, _token_spec, _said in B3_CASES:
    BATCH.add(
        f"b3-{_name}",
        prepare=lambda w, t=_token_spec: _token(w, t),
        run=lambda w: w.install("--plan-only"),
    )
BATCH.add(
    "b3-fourteen-minutes",
    prepare=lambda w: w.write_token(age_seconds=14 * 60),
    run=lambda w: w.install("--plan-only"),
)


def test_the_token_rule_refuses_a_stale_sign_in_at_the_checks(ran):
    """B3: the profile's own session's cache holds a refresh token, is at most 15 minutes
    old and has more than 30 minutes left; else UX-B11, exit 2, before any write. Fourteen
    minutes passes."""
    wrong = {}
    for name, _, said in B3_CASES:
        done = ran[f"b3-{name}"].done
        if (
            done.code != 2
            or said not in done.err
            or "sign in again (aws sso login --profile" not in done.err
        ):
            wrong[name] = (done.code, done.err[:300])
    assert wrong == {}, wrong
    fourteen = ran["b3-fourteen-minutes"].done
    assert fourteen.code == 0, fourteen.err
    for world in [ran[f"b3-{name}"].world for name, *_ in B3_CASES]:
        assert world.writes() == []


BATCH.add("plan", run=lambda w: w.install("--plan-only"))


def test_the_token_rule_prints_only_the_expiry(ran):
    world, done = ran["plan"].world, ran["plan"].done
    assert done.code == 0, done.err
    assert "Sign-in renewed recently (expiresAt " in done.out
    for secret in (
        "token-value-never-printed",
        "refresh-value-never-printed",
        "client-secret-never-printed",
    ):
        assert secret not in done.out + done.err + world.log_text()
