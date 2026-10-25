"""The AWS installer beta's install, run whole against the fakes.

One clean install, its parent shell as hostile as a reader's can be (keys, another
profile, an endpoint variable, text output), is read for most properties: what it changes
and in which order (D1), the environment of every AWS child and the HOME of its own it runs
with (B2), the config file every call reads (B1), the regions (B4), the secrets' bytes
(D4), the first administrator (D5), one host and CREATE only (D7), the clone left as it
was (D9), the ECS service-linked role (D10), the address checked through the zone's own
servers (U8), and what it prints (U11, H5). The other scenarios change one thing about the
account or this computer each: the typed confirmation, --plan-only and a decline (D1), a
reader whose profile is [default] (B2), the session check (B3b), the images (C1, C2), the
CDK's target (A6), the schema (D5, D6), the validation record (D7), the watcher (D8), the
service-linked role (D10) and a second install (E1).

Every scenario is registered below and run at once, each in its own fake account, the
first time a test asks for one (harness.install_world.Batch); each test reads its own. The
sweep library is a stub here (it is tested alone in test_sweep_library.py); every
scenario asserts the fakes saw nothing they do not model.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from backend.tests.installer_sweeps.harness import accounts, fake_ops_install
from backend.tests.installer_sweeps.harness.install_world import (
    ALARM,
    CLEAN_WRITES,
    COPY_PROFILE,
    OTHER_PATHS,
    PROFILE,
    SESSION,
    SHIM_ADDS,
    Batch,
    InstallWorld,
    is_aws,
    is_write,
)
from backend.tests.installer_sweeps.harness.world import BASHES, bash_id

pytestmark = pytest.mark.unit

ACCOUNT = accounts.ACCOUNT
DOMAIN = accounts.INSTALL_DOMAIN
SUCCESS = f"Controlvar {accounts.RELEASE} (beta) is installed and answering at:"
#: The validation record ACM gives the install's name, and its value (the fake's rule).
VALIDATION = f"_{fake_ops_install._id('validation' + DOMAIN, 32)}.{DOMAIN}."
VALIDATION_VALUE = (
    f"_{fake_ops_install._id('target' + DOMAIN, 32)}.xlfgrmvvlj.acm-validations.aws."
)

#: A reader's shell at its worst (B2): keys, another profile, an endpoint variable the
#: CLI would follow, text output, CDK defaults, a registry login, and one variable the
#: installer passes on purpose (a CA bundle).
HOSTILE = {
    "AWS_ACCESS_KEY_ID": "shell-key-never-read",
    "AWS_SECRET_ACCESS_KEY": "shell-secret-never-read",
    "AWS_SESSION_TOKEN": "shell-token-never-read",
    "AWS_PROFILE": "someone-else",
    "AWS_DEFAULT_PROFILE": "someone-else",
    "AWS_DEFAULT_REGION": "eu-west-1",
    "AWS_ENDPOINT_URL": "http://127.0.0.1:1",
    "AWS_ENDPOINT_URL_STS": "http://127.0.0.1:1",
    "AWS_DEFAULT_OUTPUT": "text",
    "AWS_MAX_ATTEMPTS": "1",
    "AWS_CLI_AUTO_PROMPT": "on",
    "AWS_SDK_LOAD_CONFIG": "1",
    "CDK_DEFAULT_ACCOUNT": "123456789012",
    "CDK_DEFAULT_REGION": "eu-west-1",
    "DOCKER_CONFIG": "/nonexistent/docker",
    "AWS_CA_BUNDLE": "/nonexistent/ca.pem",
}

#: Every AWS, CDK, crane and cosign child's environment (B2): exactly these names (and
#: what the shims add by themselves), with these values.
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
    "AWS_CA_BUNDLE",  # passed on purpose, because the shell set it
}
CDK_INPUTS = {
    "ENVIRONMENT",
    "ALARM_EMAIL",
    "PUBLIC_BASE_URL",
    "CERTIFICATE_ARN",
    "JWT_SECRET_ARN",
    "FIRST_SUPERUSER_PASSWORD_SECRET_ARN",
}


def _profile_variable() -> str:
    """The CDK's profile variable, as lib/names.sh spells it."""
    text = (
        Path(__file__).resolve().parents[3] / "deploy/aws-installer/lib/names.sh"
    ).read_text()
    return re.search(r'^NAMES_PROFILE_VARIABLE="([A-Z_]+)"$', text, re.M).group(1)


def _zone(state: dict) -> dict:
    return next(
        z for z in state["global"]["hosted_zones"] if z["Name"] == accounts.INSTALL_ZONE
    )


def _registry(change):
    return lambda world: world.edit(
        lambda s: change(s["registry"]["ghcr.io/controlvar/controlvar"])
    )


def _index(entry: dict) -> str:
    return entry["tags"][f"core-{accounts.RELEASE}"]


def _state(change):
    return lambda world: world.edit(change)


def _cdk(**settings):
    return _state(lambda s: s.setdefault("cdk", {}).update(settings))


def _app(**settings):
    return _state(lambda s: s["app"].update(settings))


def _blocked(**extra):
    return _cdk(deploy="block", block_seconds=60, **extra)


def _also(*prepares):
    def prepare(world):
        for p in prepares:
            p(world)

    return prepare


# --- the scenarios ----------------------------------------------------------------------------

BATCH = Batch()


def _hostile_reader(world: InstallWorld) -> None:
    """The reader's profile and session carry keys the copy must leave behind, and the
    children's HOME in the record holds a file from before (an alias file) that this run
    must not keep."""
    config = world.write_config()
    text = config.read_text().replace(
        "output = text\n",
        "output = text\nservices = elsewhere\ncli_pager = less\nfancy = 1\n",
    )
    text += "fancy = 2\nservices = elsewhere\n"
    text += "[services elsewhere]\nsts =\n  endpoint_url = http://127.0.0.1:1\n"
    config.write_text(text)
    left = world.record / "home" / ".aws" / "cli" / "alias"
    left.parent.mkdir(parents=True)
    left.write_text("[toplevel]\nsts = !f() { printf 'left from before\\n'; }; f\n")
    world.clone_before = world.clone_bytes()


def _default_profile(world: InstallWorld) -> None:
    """The reader's Identity Center profile is their [default] one."""
    world.write_config(
        "[default]\n"
        f"sso_session = {SESSION}\n"
        f"sso_account_id = {world.account}\n"
        "sso_role_name = AdministratorAccess\n"
        "region = us-west-2\n"
        f"[sso-session {SESSION}]\n"
        "sso_start_url = https://acme.awsapps.com/start\n"
        "sso_region = us-east-1\n"
        "sso_registration_scopes = sso:account:access\n"
    )


BATCH.add("clean", prepare=_hostile_reader, run=lambda w: w.install(**HOSTILE))
BATCH.add(
    "default-profile",
    prepare=_default_profile,
    run=lambda w: w.install("--aws-profile", "default"),
)
for _bash in BASHES:
    BATCH.add(f"bash-{bash_id(_bash)}", run=lambda w, b=_bash: w.install(bash=b))
BATCH.add("typed", run=lambda w: w.on_terminal(typed=ACCOUNT + "\n"), serial=True)
BATCH.add("declined", run=lambda w: w.on_terminal(typed="111111111112\n"), serial=True)
BATCH.add("no-terminal", run=lambda w: w.install(yes=False))


def _plan_then_install(world: InstallWorld):
    plan = world.install("--plan-only")
    world.plan_writes = world.writes()
    world.plan_started = (world.record / "env.d" / "STARTED.sh").exists()
    world.clear_logs()
    return plan, world.install()


BATCH.add("plan-only", run=_plan_then_install)


def _install_twice(world: InstallWorld):
    first = world.install()
    world.record_before = {
        p: p.read_bytes()
        for p in world.record.rglob("*")
        if p.is_file() and "logs" not in p.parts
    }
    world.zone_before = json.dumps(
        world.state["global"]["hosted_zones"], sort_keys=True
    )
    world.clear_logs()
    return first, world.install()


BATCH.add("second-install", run=_install_twice)
BATCH.add(
    "aged-token",
    prepare=lambda w: w.effect(
        service="crane", op="copy", file=str(w.token_path()), age_seconds=21 * 60
    ),
)
BATCH.add(
    "refresh-token-gone",
    prepare=lambda w: w.effect(
        service="crane", op="copy", file=str(w.token_path()), drop_key="refreshToken"
    ),
)
BATCH.add(
    "session-check-fails",
    prepare=lambda w: w.fault(
        service="sts",
        op="get-caller-identity",
        has=["--query", "Account"],
        mode="error",
        code="ExpiredToken",
        exit=254,
    ),
)
BATCH.add(
    "cosign-refuses",
    prepare=_registry(
        lambda e: e["signatures"][_index(e)].update(
            subject="https://github.com/someone/else/.github/workflows/release.yml@refs/heads/main"
        )
    ),
)
IMAGE_CASES = {
    "foreign-revision": (
        lambda e: e["configs"][next(iter(e["configs"]))]["config"]["Labels"].update(
            {"org.opencontainers.image.revision": "f" * 40}
        ),
        "built from a different release",
    ),
    "another-version": (
        lambda e: e["configs"][next(iter(e["configs"]))]["config"]["Labels"].update(
            {"org.opencontainers.image.version": "0.36.1"}
        ),
        "built from a different release",
    ),
    "two-amd64-entries": (
        lambda e: e["manifests"][_index(e)]["manifests"].append(
            dict(e["manifests"][_index(e)]["manifests"][0])
        ),
        "has no linux/amd64 build",
    ),
    "no-amd64-entry": (
        lambda e: e["manifests"][_index(e)]["manifests"].pop(0),
        "has no linux/amd64 build",
    ),
    "no-such-tag": (
        lambda e: e["tags"].pop(f"core-{accounts.RELEASE}"),
        "is not on ghcr.io",
    ),
}
for _name, (_change, _said) in IMAGE_CASES.items():
    BATCH.add(f"image-{_name}", prepare=_registry(_change))
BATCH.add(
    "copied-wrong",
    prepare=_state(
        lambda s: s.setdefault("crane", {}).update(
            copy="corrupt", corrupt_digest="sha256:" + "0" * 64
        )
    ),
)
TARGET_CASES = {
    "a-tenth-stack": (
        {"extra_stacks": ["experimentation-extra-staging"]},
        "cdk list is not exactly the nine staging stacks",
    ),
    "a-stack-in-us-east-1": (
        {"stack_regions": {"experimentation-vpc-staging": "us-east-1"}},
        "not every stack in the manifest is one of the nine",
    ),
    "another-account": (
        {"account": "123456789012"},
        "not every stack in the manifest is one of the nine",
    ),
    "a-stale-right-one-beside-a-fresh-wrong-one": (
        {"stale_default_out": True, "account": "123456789012"},
        "not every stack in the manifest is one of the nine",
    ),
}
for _name, (_settings, _said) in TARGET_CASES.items():
    BATCH.add(f"target-{_name}", prepare=_cdk(**_settings))
BATCH.add("full-profile", prepare=_cdk(profile="full"))
BATCH.add("another-admin", prepare=_app(admin="someone@else.io"))
BATCH.add("upgraded", prepare=_app(schema=True, admin=ALARM))
SCHEMA_CASES = {
    "no-schema": ({"schema_absent": True}, "the first administrator's sign-in"),
    "evaluate-fails": (
        {
            "override": {
                "/api/v1/feature-flags/evaluate/controlvar-install-smoke": [
                    500,
                    {"detail": "Internal Server Error"},
                ]
            }
        },
        "with the API key",
    ),
    "not-the-core-profile": (
        {
            "override": {
                "/api/v1/modules": [
                    200,
                    {
                        "profile": "full",
                        "modules": ["warehouse"],
                        "version": accounts.RELEASE,
                    },
                ]
            }
        },
        "GET /api/v1/modules",
    ),
}
for _name, (_settings, _said) in SCHEMA_CASES.items():
    BATCH.add(f"schema-{_name}", prepare=_app(**_settings))


def _validation_record(value: str):
    record = {
        "Name": VALIDATION,
        "Type": "CNAME",
        "TTL": 300,
        "ResourceRecords": [{"Value": value}],
    }
    return _state(lambda s: _zone(s).setdefault("records", []).append(record))


BATCH.add("validation-there", prepare=_validation_record(VALIDATION_VALUE))
#: A validation value with a quotation mark and a backslash in it: the installer compares
#: it with jq as a value (--arg), never as part of the program.
QUOTED_VALUE = '_q"uo\\ted.xlfgrmvvlj.acm-validations.aws.'
BATCH.add(
    "validation-there-quoted",
    prepare=_also(
        _state(lambda s: s.setdefault("acm", {}).update(validation_value=QUOTED_VALUE)),
        _validation_record(QUOTED_VALUE),
    ),
)
BATCH.add("validation-other", prepare=_validation_record("somewhere.else.example.org."))
BATCH.add("watch-cap", prepare=_also(_blocked(), lambda w: w.use_clock(60)))


def _stopped_tasks(state: dict, reason: str) -> None:
    region = state["regions"]["us-west-2"]
    for n in (1, 2):
        region.setdefault("tasks", []).append(
            {
                "taskArn": f"arn:aws:ecs:us-west-2:{ACCOUNT}:task/experimentation-staging/{n:032x}",
                "clusterArn": f"arn:aws:ecs:us-west-2:{ACCOUNT}:cluster/experimentation-staging",
                "taskDefinitionArn": f"arn:aws:ecs:us-west-2:{ACCOUNT}:task-definition/experimentation-backend-staging:1",
                "lastStatus": "STOPPED",
                "desiredStatus": "STOPPED",
                "stoppedReason": reason,
                "containers": [],
            }
        )


WATCH_CASES = {
    "two-stopped-tasks-one-reason": (
        _also(
            _blocked(),
            _state(
                lambda s: _stopped_tasks(
                    s,
                    "CannotPullContainerError: pull image manifest has been retried 5 time(s)",
                )
            ),
        ),
        "The API service cannot start its tasks: two stopped API tasks give the same reason: CannotPullContainerError",
    ),
    "a-service-linked-role-event": (
        _blocked(
            service_events=[
                "(service experimentation-backend-staging) was unable to assume the service-linked role"
            ]
        ),
        "The API service cannot start its tasks: a service event names the service-linked role",
    ),
    "the-stack-leaves-create-in-progress": (
        _blocked(block_status="ROLLBACK_IN_PROGRESS"),
        "AWS stopped creating the stack experimentation-fargate-staging: experimentation-fargate-staging is ROLLBACK_IN_PROGRESS",
    ),
    "a-read-that-fails": (
        _also(
            _blocked(),
            lambda w: w.fault(
                service="cloudformation",
                op="describe-stacks",
                has=["Stacks[0].StackStatus"],
                mode="error",
                code="Throttling",
                exit=254,
            ),
        ),
        "could not read the stack experimentation-fargate-staging while AWS created it (Throttling)",
    ),
    "the-stack-never-appears": (
        _also(_blocked(block_before_stacks=True), lambda w: w.use_clock(600)),
        "150 minutes after it started",
    ),
}
for _name, (_prepare, _said) in WATCH_CASES.items():
    BATCH.add(f"watch-{_name}", prepare=_prepare)
BATCH.add("slr-race", prepare=_cdk(slr_race=True))


def _locks_moved(world: InstallWorld) -> None:
    """The record's tools were installed from other locks (their .pins say so), though
    each still reports the pinned version; and a python3 that can make a venv whose pip
    writes down the environment it ran with."""
    tools = world.record / "tools"
    for name in ("cdk-cli", "venv"):
        (tools / name / ".pins").write_text("an earlier lock\n")
    seen = world.fake_dir / "pip-env.txt"
    real = world.bin / "python3.real"
    (world.bin / "python3").rename(real)
    shim = world.bin / "python3"
    shim.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-m" ] && [ "$2" = venv ]; then\n'
        '    mkdir -p "$3/bin" || exit 1\n'
        f'    printf "#!/bin/sh\\nenv > \\"{seen}\\"\\n" > "$3/bin/python3" && chmod +x "$3/bin/python3"\n'
        "    exit $?\n"
        "fi\n"
        f'exec "{real}" "$@"\n'
    )
    shim.chmod(0o755)


BATCH.add(
    "locks-moved",
    prepare=_locks_moved,
    run=lambda w: w.install(HTTPS_PROXY="http://proxy.example.org:3128"),
)
BATCH.add(
    "role-there",
    prepare=_state(
        lambda s: (
            s["global"]
            .setdefault("roles", [])
            .append(
                {
                    "Path": "/aws-service-role/ecs.amazonaws.com/",
                    "RoleName": "AWSServiceRoleForECS",
                    "RoleId": "AROAECSROLE0000000001",
                    "Arn": f"arn:aws:iam::{ACCOUNT}:role/aws-service-role/ecs.amazonaws.com/AWSServiceRoleForECS",
                    "CreateDate": accounts.LONG_AGO,
                    "MaxSessionDuration": 3600,
                }
            )
        )
    ),
)
BATCH.add(
    "role-taken",
    prepare=lambda w: w.fault(
        service="iam",
        op="create-service-linked-role",
        mode="error",
        code="InvalidInput",
        message="Service role name AWSServiceRoleForECS has been taken in this account, please try a different suffix.",
        exit=254,
    ),
)
for _mode in ("AccessDenied", "EMPTY"):
    BATCH.add(
        f"role-create-{_mode}",
        prepare=lambda w, m=_mode: w.fault(
            service="iam", op="create-service-linked-role", mode=m
        ),
    )
BATCH.add(
    "role-read-fails",
    prepare=lambda w: w.fault(
        service="iam",
        op="get-role",
        has=["--role-name", "AWSServiceRoleForECS"],
        mode="error",
        code="Throttling",
        exit=254,
    ),
)
BATCH.add(
    "quota-at-its-default", prepare=_state(OTHER_PATHS["a-quota-at-its-default"][0])
)

#: Each scenario in which a check decides by an AWS error's code: the clean run
#: (NoSuchEntity, the ECS role absent; ValidationError's "does not exist", CDKToolkit
#: absent), a quota with no applied value (NoSuchResourceException), the role AWS made
#: meanwhile (InvalidInput's "has been taken"), the stack the watcher waits for and that
#: never appears ("does not exist" again), and stops that name the code. Each runs again
#: with the error's other form.
BOTH_FORMS = [
    "clean",
    "quota-at-its-default",
    "role-taken",
    "role-create-AccessDenied",
    "role-read-fails",
    "session-check-fails",
    "watch-a-read-that-fails",
    "watch-the-stack-never-appears",
]
BATCH.in_the_other_error_form(BOTH_FORMS)


@pytest.fixture(scope="module")
def ran(tmp_path_factory, shapes_file):
    found = BATCH.run_all(tmp_path_factory.mktemp("install-run"), shapes_file)
    yield found
    unknown = {name: r.world.unknown for name, r in found.items() if r.world.unknown}
    assert unknown == {}, unknown


@pytest.fixture(scope="module")
def clean(ran):
    return ran["clean"]


# --- the clean run --------------------------------------------------------------------------


def test_the_clean_run_installs(clean):
    done = clean.done
    assert done.code == 0, done.err
    assert SUCCESS in done.out
    assert f"  https://{DOMAIN}" in done.out
    assert done.err == "", done.err


def test_the_clean_runs_changes_are_pinned_in_order(clean):
    """D1: the list's diff names a write that moved, appeared or went."""
    assert clean.world.writes() == CLEAN_WRITES


def test_nothing_changes_before_the_confirmation(clean):
    """D1: everything before the confirmation reads; the plan prints before it."""
    out = clean.done.out
    assert out.index("This creates in account") < out.index("Confirmed with --yes.")
    calls = clean.world.calls
    first_write = next(i for i, c in enumerate(calls) if is_write(c))
    assert calls[first_write]["service"] == "cdk"
    for call in calls[:first_write]:
        assert not is_write(call), call
        if is_aws(call):
            assert call["op"].startswith(("list-", "describe-", "get-", "simulate-")), (
                call["op"]
            )


def test_every_aws_child_runs_with_exactly_the_allowed_environment(clean):
    """B2: the parent's keys, profile, endpoint variables, output format, CDK defaults and
    registry login reach no child; each child's environment is the allow-list, its HOME the
    record's own (W1) and its profile the copy's own name (W2)."""
    variable = _profile_variable()
    record = clean.world.record
    seen = 0
    for call in clean.world.calls:
        if call["service"] in ("dig", "git", "curl", "node", "npm"):
            continue  # not AWS children: they run in the reader's own environment
        if call["service"] == "cdk" and call["op"] == "--version":
            continue  # the version read of the record's own CLI, which signs in nowhere
        names = set(call["env"]) - SHIM_ADDS
        want = set(BASE_ENV)
        if call["service"] == "cdk":
            want |= CDK_INPUTS | {variable}
        probe = call["argv"][-2:] == ["--endpoint-url", "http://127.0.0.1:9"]
        if probe:
            want -= {"AWS_PROFILE"}
        assert names == want, (
            call["n"],
            call["service"],
            call["op"],
            sorted(names ^ want),
        )
        values = call["values"]
        assert values["AWS_DEFAULT_OUTPUT"] == "json"
        assert values["AWS_REGION"] == "us-west-2"
        assert values["AWS_CONFIG_FILE"] == str(record / "aws-config")
        assert values["AWS_SHARED_CREDENTIALS_FILE"] == "/dev/null"
        assert values["AWS_EC2_METADATA_DISABLED"] == "true"
        assert values["AWS_PAGER"] == ""
        assert values["TZ"] == "UTC"
        assert values["DOCKER_CONFIG"].startswith(str(record) + "/"), values[
            "DOCKER_CONFIG"
        ]
        assert values["HOME"] == str(record / "home"), (call["n"], values["HOME"])
        if call["argv"][-2:] != ["--endpoint-url", "http://127.0.0.1:9"]:
            assert values["AWS_PROFILE"] == COPY_PROFILE, call
        seen += 1
    assert seen > 70


def test_the_deploy_deploys_the_assembly_the_target_check_read(clean):
    """cdk deploy is given the list call's own output folder as its app (--app), so what it
    deploys is the cloud assembly assert_cdk_target read, not a second synth."""
    calls = {
        c["op"]: c["argv"]
        for c in clean.world.calls
        if c["service"] == "cdk" and c["op"] in ("list", "deploy")
    }
    listed = calls["list"][calls["list"].index("--output") + 1]
    deploy = calls["deploy"]
    assert "--app" in deploy, deploy
    assert deploy[deploy.index("--app") + 1] == listed
    assert deploy[deploy.index("--output") + 1] != listed
    assert (Path(listed) / "manifest.json").is_file()


def test_the_tools_are_reused_while_their_locks_are_the_clones(clean):
    """The record's CLI and venv were installed from the clone's pins/ (their .pins hold the
    locks' hashes): nothing is installed again."""
    assert not [c for c in clean.world.calls if c["service"] == "npm"]


def test_a_tool_installed_from_other_locks_is_installed_again(ran):
    """The tools step keys its reuse on the locks' hashes, not the tools' versions: a CLI
    and a venv that report the pinned versions but came from other locks are installed
    again. npm and pip run as every other child does: the installer's environment, the
    proxy variable the shell set passed on purpose, and a HOME of their own, never the
    reader's."""
    from backend.tests.installer_sweeps.harness.install_world import pins_stamp

    world, done = ran["locks-moved"].world, ran["locks-moved"].done
    assert done.code == 0, done.err
    npm = [c for c in world.calls if c["service"] == "npm"]
    assert [c["op"] for c in npm] == ["ci"], npm
    want = (set(BASE_ENV) - {"AWS_CA_BUNDLE"}) | {"HTTPS_PROXY"}
    assert set(npm[0]["env"]) - SHIM_ADDS == want, sorted(set(npm[0]["env"]) ^ want)
    assert npm[0]["values"]["HOME"] == str(world.record / "home")
    pip = dict(
        line.split("=", 1)
        for line in (world.fake_dir / "pip-env.txt").read_text().splitlines()
        if "=" in line
    )
    assert set(pip) - SHIM_ADDS == want, sorted(set(pip) ^ want)
    assert pip["HOME"] == str(world.record / "home")
    assert pip["HTTPS_PROXY"] == "http://proxy.example.org:3128"
    pins = world.clone / "deploy" / "aws-installer" / "pins"
    assert (world.record / "tools" / "cdk-cli" / ".pins").read_text() == pins_stamp(
        pins / "package.json", pins / "package-lock.json"
    ) + "\n"
    assert (world.record / "tools" / "venv" / ".pins").read_text() == pins_stamp(
        pins / "requirements.lock"
    ) + "\n"


def test_every_cdk_call_signs_in_through_the_copy(clean):
    """W2: each cdk call names the copy's own profile (--profile), the one AWS_PROFILE
    names, never the reader's."""
    calls = [
        c for c in clean.world.calls if c["service"] == "cdk" and c["op"] != "--version"
    ]
    assert [c["op"] for c in calls] == ["bootstrap", "list", "deploy"], calls
    for call in calls:
        argv = call["argv"]
        assert argv[argv.index("--profile") + 1] == COPY_PROFILE, argv
        assert call["values"]["AWS_PROFILE"] == COPY_PROFILE, call["values"]


def test_the_childrens_home_holds_only_the_sign_in_cache(clean):
    """W1: every aws, cdk, crane and cosign child runs with HOME <record>/home, made again
    on every run once the lock is held. Its only content is .aws/sso/cache, a link to the
    reader's own sign-in cache (shared on purpose), so no other file under the reader's
    HOME (an alias file, botocore's models, ~/.cdk.json) reaches a child; the alias file
    left in it before the run is gone. That the real CLI reads neither the alias nor the
    reader's file under it is shown in CDK Stack Tests (scripts/check_installer_cli.py)."""
    home = clean.world.record / "home"
    found = []
    for folder, dirs, files in os.walk(home):
        for name in dirs + files:
            found.append(os.path.relpath(os.path.join(folder, name), home))
    assert sorted(found) == [".aws", ".aws/sso", ".aws/sso/cache"], found
    link = home / ".aws" / "sso" / "cache"
    assert link.is_symlink()
    assert os.readlink(link) == str(clean.world.home / ".aws" / "sso" / "cache")


def test_the_probe_runs_with_no_profile(clean):
    """No AWS_PROFILE at all: set and empty, the CLI takes it for a profile named ""."""
    probe = [c for c in clean.world.calls if "http://127.0.0.1:9" in c["argv"]]
    assert len(probe) == 1 and probe[0]["exit"] == 253
    assert "AWS_PROFILE" not in probe[0]["env"]


def test_a_default_profile_is_copied_under_the_installers_own_name(ran):
    """W2: a reader whose Identity Center profile is [default] installs: the copy is
    written under the installer's own name, never as [default], so with no profile named
    the probe still finds nothing (the CLI reads an Identity Center [default] as a way to
    sign in), and every child and cdk call signs in through that name."""
    world, done = ran["default-profile"].world, ran["default-profile"].done
    assert done.code == 0, done.err
    assert SUCCESS in done.out
    copy = (world.record / "aws-config").read_text()
    assert copy.splitlines()[0] == f"[profile {COPY_PROFILE}]", copy
    assert "[default]" not in copy
    probe = [c for c in world.calls if "http://127.0.0.1:9" in c["argv"]]
    assert len(probe) == 1 and probe[0]["exit"] == 253
    profiles = {
        c["values"].get("AWS_PROFILE")
        for c in world.calls
        if is_aws(c) and c not in probe
    }
    assert profiles == {COPY_PROFILE}, profiles
    cdk = [
        c["argv"]
        for c in world.calls
        if c["service"] == "cdk" and c["op"] != "--version"
    ]
    assert cdk and {a[a.index("--profile") + 1] for a in cdk} == {COPY_PROFILE}, cdk


def test_the_installer_reads_only_its_own_copy_of_the_profile(clean):
    """B1: the copy holds exactly the six keys (an unknown key and a services key in
    the reader's profile and session are left behind), under the installer's own profile
    name, and it is the config file of every AWS call."""
    copy = (clean.world.record / "aws-config").read_text()
    assert copy == (
        f"[profile {COPY_PROFILE}]\n"
        "sso_session = acme\n"
        f"sso_account_id = {ACCOUNT}\n"
        "sso_role_name = AdministratorAccess\n"
        "[sso-session acme]\n"
        "sso_start_url = https://acme.awsapps.com/start\n"
        "sso_region = us-east-1\n"
        "sso_registration_scopes = sso:account:access\n"
    )
    configs = {
        c["values"].get("AWS_CONFIG_FILE") for c in clean.world.calls if is_aws(c)
    }
    assert configs == {str(clean.world.record / "aws-config")}


#: The stack scan's read: the one call made outside us-west-2, in each other enabled
#: region.
SCAN = "StackSummaries[?StackStatus!='DELETE_COMPLETE' && starts_with(StackName, 'experimentation-')].StackName"


def _query_of(call: dict) -> str | None:
    argv = call["argv"]
    return argv[argv.index("--query") + 1] if "--query" in argv else None


def test_every_regional_call_is_in_us_west_2(clean):
    """B4: outside us-west-2 only the stack scan's other enabled region; IAM and Route 53
    are global, and the fake places them by the environment's region."""
    calls = clean.world.calls
    outside = sorted(
        {
            (c["service"], c["op"], c["region"], _query_of(c))
            for c in calls
            if is_aws(c) and c["region"] not in ("us-west-2", None)
        }
    )
    assert outside == [("cloudformation", "list-stacks", "us-east-1", SCAN)], [
        f"call outside us-west-2: {o}" for o in outside
    ]
    unplaced = [c for c in calls if is_aws(c) and c["region"] is None]
    assert [c["argv"][-2:] for c in unplaced] == [
        ["--endpoint-url", "http://127.0.0.1:9"]
    ]


def test_secret_values_are_exact_and_nowhere_else(clean):
    """D4: each secret's bytes as the fake received them through file://; neither value
    in any argument, log line, record file or output."""
    state = clean.world.state["regions"]["us-west-2"]
    values = {
        s["Name"]: state["secret_values"][s["ARN"]]
        for s in state["secrets"]
        if s["ARN"] in state["secret_values"]
    }
    assert sorted(values) == [
        "/staging/experimentation/first-superuser-password",
        "/staging/experimentation/jwt-secret",
    ]
    jwt = values["/staging/experimentation/jwt-secret"]
    password = values["/staging/experimentation/first-superuser-password"]
    assert (
        len(jwt) == 64
        and jwt == jwt.strip()
        and re.fullmatch(r"[A-Za-z0-9+/=]{64}", jwt)
    )
    assert re.fullmatch(
        r"[ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789]{24}", password
    )
    for creates in (c for c in clean.world.calls if c["op"] == "create-secret"):
        given = creates["argv"][creates["argv"].index("--secret-string") + 1]
        assert given.startswith("file://"), given
    haystacks = {
        "arguments": json.dumps([c["argv"] for c in clean.world.calls]),
        "stdout": clean.done.out,
        "stderr": clean.done.err,
    }
    for path in sorted(clean.world.record.rglob("*")):
        if path.is_file():
            haystacks[str(path.relative_to(clean.world.record))] = path.read_text(
                errors="replace"
            )
    for name, text in haystacks.items():
        assert jwt not in text, f"the JWT secret in {name}"
        assert password not in text, f"the password in {name}"
    assert list((clean.world.record / "tmp").iterdir()) == []


def test_the_migration_task_names_the_first_administrator(clean):
    """D5: one environment entry, FIRST_SUPERUSER, the reader's address."""
    tasks = clean.world.state["regions"]["us-west-2"]["tasks"]
    assert [t["env_seen"] for t in tasks] == [{"FIRST_SUPERUSER": ALARM}]
    assert (
        f"created the first administrator {ALARM} (role ADMIN)"
        in clean.world.log_text()
    )


def test_one_name_one_alias_create_only(clean):
    """D7: the certificate names one host and no other; both records are CREATE."""
    request = next(c for c in clean.world.calls if c["op"] == "request-certificate")
    assert request["argv"][request["argv"].index("--domain-name") + 1] == DOMAIN
    assert "--subject-alternative-names" not in request["argv"]
    records = clean.world.record / "records"
    batches = [json.loads(p.read_text()) for p in sorted(records.glob("*-change.json"))]
    assert [[ch["Action"] for ch in b["Changes"]] for b in batches] == [
        ["CREATE"],
        ["CREATE"],
    ]
    names = [b["Changes"][0]["ResourceRecordSet"]["Name"] for b in batches]
    assert names == [VALIDATION, DOMAIN + "."]
    assert [
        json.loads(p.read_text())["status"] for p in sorted(records.glob("[0-9].json"))
    ] == ["created", "created"]


def test_the_clone_is_left_as_it_was(clean):
    """D9: the fake CDK writes cdk.context.json where it runs; that is the record's copy
    of the release, never the clone."""
    world = clean.world
    assert world.clone_bytes() == world.clone_before
    cwd = {
        c["cwd"]
        for c in world.calls
        if c["service"] == "cdk" and c["op"] in ("list", "deploy")
    }
    assert cwd == {str(world.record / "src" / "infrastructure" / "cdk")}
    outputs = [
        c["argv"][c["argv"].index("--output") + 1]
        for c in world.calls
        if c["service"] == "cdk" and "--output" in c["argv"]
    ]
    assert len(outputs) == 3 and len(set(outputs)) == 3, outputs
    assert all(o.startswith(str(world.record / "cdk-out") + "/") for o in outputs)


def test_the_service_linked_role_is_ensured_before_the_deploy(clean):
    """D10: preflight read AWSServiceRoleForECS as absent (NoSuchEntity), so the role is
    created before cdk deploy."""
    ops = [(c["service"], c["op"]) for c in clean.world.calls]
    assert ops.index(("iam", "create-service-linked-role")) < ops.index(
        ("cdk", "deploy")
    )
    seen = (clean.world.record / "before" / "service-linked-roles.txt").read_text()
    assert "AWSServiceRoleForECS absent" in seen.splitlines()


def test_the_address_is_checked_through_the_zones_own_server(clean):
    """U8: every HTTPS call goes to an address one of the zone's own name servers gave;
    the summary's URL comes after them."""
    calls = clean.world.calls
    ns = (clean.world.record / "before" / "zone-ns.txt").read_text().split()
    asked = [
        c
        for c in calls
        if c["service"] == "dig" and any(a.startswith("@ns-") for a in c["argv"])
    ]
    assert asked and all(f"@{ns[0]}" in c["argv"] for c in asked), asked
    curls = [c for c in calls if c["service"] == "curl"]
    assert curls and all(f"{DOMAIN}:443:203.0.113.10" in c["argv"] for c in curls)
    assert all(calls.index(c) > calls.index(asked[0]) for c in curls)
    out = clean.done.out
    assert out.index(SUCCESS) > out.index(
        "[9/9] Check the install from the internet ...."
    )


def test_nothing_printed_is_a_terminal_effect(clean):
    """U11: printable ASCII and newlines only: no escape, no carriage return, no
    box-drawing or other non-ASCII character."""
    for name, text in (("stdout", clean.done.out), ("stderr", clean.done.err)):
        bad = sorted({ch for ch in text if not (ch == "\n" or " " <= ch <= "~")})
        assert bad == [], (name, [hex(ord(ch)) for ch in bad])


def test_the_first_line_says_beta_and_points_to_the_page(clean):
    """H5: every run's first line."""
    first = clean.done.out.splitlines()[0]
    assert "(beta)" in first and "#what-has-been-tested" in first


def test_help_says_beta_and_lists_only_the_options(make_install):
    world = make_install()
    for argv in (
        [str(world.launcher), "--help"],
        [str(world.launcher), "install", "--help"],
    ):
        done = world.run(*argv)
        assert done.code == 0
        assert (
            "(beta)" in done.out.splitlines()[0] and "#what-has-been-tested" in done.out
        )
        flags = sorted(set(re.findall(r"^  (--[a-z-]+)", done.out, re.M)))
        assert flags == [
            "--account",
            "--admin-email",
            "--alarm-email",
            "--aws-profile",
            "--domain",
            "--help",
            "--plan-only",
            "--yes",
        ], flags
    assert world.calls == []


@pytest.mark.parametrize("bash", BASHES, ids=bash_id)
def test_the_clean_run_under_every_bash(ran, bash):
    """H3: the same writes, under macOS's /bin/bash 3.2 where the machine has it."""
    one = ran[f"bash-{bash_id(bash)}"]
    assert one.done.code == 0, one.done.err
    assert one.world.writes() == CLEAN_WRITES
    assert "unbound variable" not in one.done.err


# --- the confirmation --------------------------------------------------------------------------


def test_the_typed_account_id_confirms_and_the_password_shows_once_on_the_terminal(ran):
    """D1: without --yes the reader types the account ID at the terminal; D4: the
    password is written once, to the terminal only, never to stdout or the log."""
    world = ran["typed"].world
    done, shown = ran["typed"].done
    assert done.code == 0, done.err
    assert "Type the account ID shown above to create these resources:" in done.out
    assert world.writes() == CLEAN_WRITES
    region = world.state["regions"]["us-west-2"]
    password = next(
        region["secret_values"][s["ARN"]]
        for s in region["secrets"]
        if s["Name"].endswith("first-superuser-password")
    )
    assert shown.count(password) == 1, shown
    assert f"  Password  {password}" in shown
    assert password not in done.out + done.err + world.log_text()


def test_a_declined_confirmation_changes_nothing(ran):
    world = ran["declined"].world
    done, _ = ran["declined"].done
    assert done.code == 2, done.err
    assert done.err.startswith("REFUSED: Not confirmed.")
    assert world.writes() == []
    assert not (world.record / "env.d" / "STARTED.sh").exists()


def test_with_no_terminal_and_no_yes_nothing_changes(ran):
    done = ran["no-terminal"].done
    assert (
        done.code == 2 and "there is no terminal to type the account ID on" in done.err
    )
    assert ran["no-terminal"].world.writes() == []


def test_plan_only_reads_and_writes_nothing(ran):
    """D1 and E1: --plan-only prints the plan, writes nothing in AWS and leaves no
    STARTED, so the install after it starts as if it were the first."""
    world = ran["plan-only"].world
    plan, install = ran["plan-only"].done
    assert plan.code == 0, plan.err
    assert "This creates in account" in plan.out
    assert "Plan only: nothing was created." in plan.out
    assert world.plan_writes == [] and not world.plan_started
    assert install.code == 0, install.err
    assert world.writes() == CLEAN_WRITES


def test_the_install_plan_says_uninstall_keeps_the_cdks_setup_stack(ran):
    plan, _install = ran["plan-only"].done
    first = next(
        line
        for line in plan.out.splitlines()
        if line.startswith("This creates in account")
    )
    assert (
        "the CDK's setup stack (uninstall keeps it: other CDK apps may use it), 2 image"
        in first
    ), first


def test_a_second_install_over_a_started_record_refuses_and_reads_nothing(ran):
    """E1: UX-B9, exit 2, no call at all, and the record and the address kept."""
    world = ran["second-install"].world
    first, second = ran["second-install"].done
    assert first.code == 0, first.err
    assert second.code == 2, second.err
    assert second.err.startswith(
        "REFUSED: This computer holds the record of an install in the account ending in 1111"
    )
    assert f"that is complete at https://{DOMAIN}" in second.err
    assert "Nothing was changed." in second.err.splitlines()
    assert world.calls == []
    after = {
        p: p.read_bytes()
        for p in world.record.rglob("*")
        if p.is_file() and "logs" not in p.parts
    }
    assert after == world.record_before
    assert (
        json.dumps(world.state["global"]["hosted_zones"], sort_keys=True)
        == world.zone_before
    )


# --- the session check before the deploy (B3b) ---------------------------------------------------


def test_twenty_minutes_of_prepare_do_not_stop_the_deploy(ran):
    """B3b: the sign-in's cache is the login's time for most of an hour (the CLI renews
    the token without touching it), so the deploy gate has no window."""
    done = ran["aged-token"].done
    assert done.code == 0, done.err
    assert ("cdk", "deploy") in ran["aged-token"].world.writes()


def test_a_sign_in_that_ended_during_prepare_stops_before_the_stacks(ran):
    done = ran["refresh-token-gone"].done
    assert done.code == 1, done.err
    assert done.err.startswith(
        f"FAILED: The sign-in for {PROFILE} stopped working (no refresh token in its cache), "
        "so the installer stopped before creating the nine stacks."
    )
    assert ("cdk", "deploy") not in ran["refresh-token-gone"].world.writes()
    assert "[6/9] Create the nine stacks" in done.out


def test_a_session_check_that_cannot_sign_in_stops_before_the_stacks(ran):
    done = ran["session-check-fails"].done
    assert done.code == 1, done.err
    assert "stopped working (ExpiredToken)" in done.err
    assert ("cdk", "deploy") not in ran["session-check-fails"].world.writes()


# --- the images (C1, C2) --------------------------------------------------------------------


def test_an_image_cosign_refuses_is_never_copied(ran):
    """C1: cosign exits 12; nothing is copied and the registry is empty."""
    world, done = ran["cosign-refuses"].world, ran["cosign-refuses"].done
    assert done.code == 2, done.err
    assert "is not signed by Controlvar's release workflow" in done.err
    assert world.writes() == []
    assert world.state["regions"]["us-west-2"].get("images", {}) == {}


@pytest.mark.parametrize("name", sorted(IMAGE_CASES))
def test_an_image_that_is_not_this_releases_is_refused(ran, name):
    """C2: the index of core-<VERSION>, exactly one linux/amd64 entry, and the version,
    profile and revision labels of this clone's tag."""
    done = ran[f"image-{name}"].done
    assert done.code == 2, done.err
    assert IMAGE_CASES[name][1] in done.err
    assert ran[f"image-{name}"].world.writes() == []


def test_the_images_are_verified_and_copied_by_digest(clean):
    """C2: cosign verifies the index of core-<VERSION>; crane copies its amd64 entry by
    digest; the registry holds that digest under :bootstrap."""
    entry = accounts.release_image("ghcr.io/controlvar/controlvar")
    index = _index(entry)
    amd64 = entry["manifests"][index]["manifests"][0]["digest"]
    calls = clean.world.calls
    verified = [
        c["argv"][2] for c in calls if c["service"] == "cosign" and c["op"] == "verify"
    ]
    assert f"ghcr.io/controlvar/controlvar@{index}" in verified
    copies = [
        c["argv"][2:4] for c in calls if c["service"] == "crane" and c["op"] == "copy"
    ]
    assert copies[0] == [
        f"ghcr.io/controlvar/controlvar@{amd64}",
        f"{ACCOUNT}.dkr.ecr.us-west-2.amazonaws.com/experimentation-platform/backend:bootstrap",
    ]
    images = clean.world.state["regions"]["us-west-2"]["images"][
        "experimentation-platform/backend"
    ]
    assert [(i["imageDigest"], i["imageTags"]) for i in images] == [
        (amd64, ["bootstrap"])
    ]


def test_an_image_copied_wrong_stops_the_install(ran):
    done = ran["copied-wrong"].done
    assert done.code == 1, done.err
    assert "is not the one that was verified" in done.err
    assert ("cdk", "deploy") not in ran["copied-wrong"].world.writes()


# --- the CDK's target (A6) ------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(TARGET_CASES))
def test_a_cdk_target_that_is_not_the_nine_stops_before_the_deploy(ran, name):
    """A6: the manifest in this cdk call's own fresh --output, not a stale cdk.out."""
    done = ran[f"target-{name}"].done
    assert done.code == 1, done.err
    assert done.err.startswith(
        "FAILED: The CDK's list of what it would deploy is not the nine staging stacks"
    )
    assert TARGET_CASES[name][1] in done.err
    assert ("cdk", "deploy") not in ran[f"target-{name}"].world.writes()


def test_the_full_profile_stops_before_the_deploy(ran):
    """A6: the profile line (and eleven stacks) is not the core profile."""
    done = ran["full-profile"].done
    assert done.code == 1, done.err
    assert "the app did not say it builds the core profile" in done.err
    assert ("cdk", "deploy") not in ran["full-profile"].world.writes()


# --- the schema and the first administrator (D5, D6) -------------------------------------------


def test_success_needs_the_administrator_the_reader_named(ran):
    """D5: a migration log naming another administrator is a failure."""
    done = ran["another-admin"].done
    assert done.code == 1, done.err
    assert "its log does not say it created that administrator" in done.err
    assert SUCCESS not in done.out


def test_an_existing_schema_is_upgraded_and_passes(ran):
    """D5: "(upgraded)" passes as "(created)" does."""
    done = ran["upgraded"].done
    assert done.code == 0, done.err
    assert "Database bootstrap complete (upgraded)" in ran["upgraded"].world.log_text()


@pytest.mark.parametrize("name", sorted(SCHEMA_CASES))
def test_success_needs_the_schema(ran, name):
    """D6: /health answers 200 and an unkeyed request 401 without a schema, so neither
    is success: the administrator's sign-in and the keyed evaluation are."""
    done = ran[f"schema-{name}"].done
    assert done.code == 1, done.err
    assert done.err.startswith(
        "FAILED: The install is created, but a check from the internet failed:"
    )
    assert SCHEMA_CASES[name][1] in done.err
    assert SUCCESS not in done.out


# --- the validation record (D7) -------------------------------------------------------------


@pytest.mark.parametrize("name", ["validation-there", "validation-there-quoted"])
def test_an_identical_earlier_validation_record_is_used_and_not_recorded(ran, name):
    """D7. ACM's value is compared as a value: one with a quotation mark and a backslash
    (validation-there-quoted) is recognised too, where text spliced into jq's program
    could not even be read."""
    world, done = ran[name].world, ran[name].done
    assert done.code == 0, done.err
    assert (
        f"The validation record {VALIDATION} was already there with ACM's value; it is used and kept."
        in done.out
    )
    assert [c for c in world.calls if c["op"] == "change-resource-record-sets"] != []
    assert (
        len([c for c in world.calls if c["op"] == "change-resource-record-sets"]) == 1
    )  # the alias only
    assert not (world.record / "records" / "1.json").exists()
    assert not (world.record / "env.d" / "VALIDATION_RECORD.sh").exists()


def test_something_else_at_the_validation_name_stops_the_install(ran):
    world, done = ran["validation-other"].world, ran["validation-other"].done
    assert done.code == 1, done.err
    assert "already holds something else (CNAME)" in done.err
    assert ("route53", "change-resource-record-sets") not in world.writes()


# --- the watcher (D8) -----------------------------------------------------------------------


def test_the_watcher_stops_a_create_that_runs_past_its_cap(ran):
    """D8, the cap: on a fake clock a minute a read, 35 minutes after the fargate stack
    entered CREATE_IN_PROGRESS."""
    done = ran["watch-cap"].done
    assert done.code == 1, done.err
    assert (
        "AWS was still creating experimentation-fargate-staging 35 minutes after it started, longer than the installer waits"
        in done.err
    )
    assert "EXIT cap:" in ran["watch-cap"].world.log_text()


@pytest.mark.parametrize("name", sorted(WATCH_CASES))
def test_the_watcher_stops_a_create_that_cannot_finish(ran, name):
    """D8, the rule, the status, the read and the wait limit."""
    done = ran[f"watch-{name}"].done
    assert done.code == 1, done.err
    assert WATCH_CASES[name][1] in done.err


# --- the ECS service-linked role (D10) --------------------------------------------------------


def test_under_the_race_the_ensured_role_lets_the_services_start(ran):
    """D10: with slr_race, service creation fails without the role; the ensure call made
    before cdk deploy prevents it."""
    done = ran["slr-race"].done
    assert done.code == 0, done.err


def test_a_role_already_there_is_not_created(ran):
    done = ran["role-there"].done
    assert done.code == 0, done.err
    assert ("iam", "create-service-linked-role") not in ran["role-there"].world.writes()


def test_a_role_made_meanwhile_is_taken_as_there(ran):
    """D10: the exact InvalidInput "has been taken" passes."""
    done = ran["role-taken"].done
    assert done.code == 0, done.err
    assert "AWSServiceRoleForECS is already there (AWS made it meanwhile)." in done.out


@pytest.mark.parametrize("mode", ["AccessDenied", "EMPTY"])
def test_any_other_answer_from_the_role_create_stops_the_install(ran, mode):
    """D10: any other error, or an answer that is not the role, exits 1."""
    done = ran[f"role-create-{mode}"].done
    assert done.code == 1, done.err
    assert (
        "AWS did not create the ECS service-linked role AWSServiceRoleForECS"
        in done.err
    )
    assert ("cdk", "deploy") not in ran[f"role-create-{mode}"].world.writes()


def test_a_role_read_that_fails_is_neither_present_nor_absent(ran):
    done = ran["role-read-fails"].done
    assert done.code == 2, done.err
    assert "could not read the role AWSServiceRoleForECS (Throttling)" in done.err
    assert ran["role-read-fails"].world.writes() == []


def test_a_quota_with_no_applied_value_is_read_as_aws_default(ran):
    world, done = ran["quota-at-its-default"].world, ran["quota-at-its-default"].done
    assert done.code == 0, done.err
    reads = [
        (c["op"], c["exit"])
        for c in world.aws_calls
        if c["service"] == "service-quotas"
    ]
    assert ("get-service-quota", 254) in reads
    assert ("get-aws-default-service-quota", 0) in reads


# --- both forms of an AWS error ---------------------------------------------------------------


def _said(one) -> list[str]:
    """What a run printed to stderr, its own folders written as <root> and its log's
    path left out (each world has its own)."""
    root = str(one.world.root)
    return [
        line.replace(root, "<root>")
        for line in one.done.err.splitlines()
        if not line.startswith("Log: ")
    ]


@pytest.mark.parametrize("name", BOTH_FORMS)
def test_a_check_that_reads_an_aws_error_reads_either_form(ran, name):
    """A check reads the error's code in parentheses, never the start of its line: with
    every error printed as aws-cli 2.28.24 prints it (no "aws: [ERROR]: " first), each
    run ends as it does in the form newer CLIs print, in the same exit, the same changes
    and the same words."""
    prefixed, plain = ran[name], ran[f"{name}-plain"]
    assert plain.world.state["error_style"] == "plain"
    assert [c for c in plain.world.aws_calls if c["exit"] == 254], "no AWS error here"
    assert (plain.done.code, plain.world.writes(), _said(plain)) == (
        prefixed.done.code,
        prefixed.world.writes(),
        _said(prefixed),
    )
