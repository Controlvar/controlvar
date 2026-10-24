#!/usr/bin/env python3
"""The fake ``aws`` answers as the real AWS CLI does, for every call the beta makes (H6).

    python3 scripts/check_installer_cli.py           # compare, with the runner's CLI
    python3 scripts/check_installer_cli.py --list    # print the calls only, no CLI

The AWS installer beta (deploy/aws-installer/) is tested against a stateful fake
``aws`` (backend/tests/installer_sweeps/harness/fake_aws.py), so every one of its
tests assumes the fake prints what the CLI prints. This shows it, in CDK Stack
Tests, in a step outside pytest: every pytest session blocks the real CLI
(backend/tests/no_real_aws.py), and this is the job that runs the runner's own.

1. The beta's sweep (lib/sweep.sh, through the harness's driver) runs against the
   fake for a new account and an installed one, and a clean install and a clean
   uninstall run against the fakes (with the paths each never takes on a clean run)
   with the state each of their calls found kept; every call the fake logged is a call
   the beta makes. Calls differing only in region are one shape.
2. For each shape and state, and for the same call with ``--output text``, the
   fake's own response for that state is served from a stub on 127.0.0.1, in the
   protocol the CLI's request uses (harness/wire.py), and the real CLI runs with
   ``--endpoint-url`` at the stub.
3. Its stdout and exit status must equal the fake's, byte for byte. The calls that
   carry a timestamp run again in a second time zone (the CLI prints an epoch in
   local time). The user-pool read runs again against a first page that carries a
   next-page token, which the sweep must refuse. An access denial in each protocol
   family is compared on stderr.

4. The sign-in, with the installer's own functions (lib/env.sh) and the runner's CLI,
   offline: the installer's copy of a reader's profile holds the six keys the CLI
   resolves and nothing else from the reader's file (no region, output, services or
   unknown key), and with no profile named, the installer's environment finds no
   credentials ("Unable to locate credentials") although the reader's file has a
   [default] key, which the same CLI given the reader's file does find. So too when
   the reader's Identity Center profile is their [default] one (the copy takes the
   installer's own name), and when the reader's HOME holds an alias file that turns
   `aws sts` into a command of its own (the CLI's children run with a HOME of their
   own); the same CLI given the reader's file, or the reader's HOME, is shown to read
   each.

The CLI's children get dummy keys, AWS_CONFIG_FILE and AWS_SHARED_CREDENTIALS_FILE
at /dev/null and instance metadata off; the stub is the only endpoint. The sign-in
checks' only endpoint is a closed local port and no sign-in is cached, so the CLI asks
no portal. The script refuses to start with a key in its own environment.

Exit status: 0 when every call matches; 1 when any differs (each named) or the
check could not run.
"""

from __future__ import annotations

import concurrent.futures
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from backend.tests.installer_sweeps.harness import (
    accounts,
    fake_aws,
    shapes,
    wire,
)
from backend.tests.installer_sweeps.harness.install_world import (
    install_snapshots,
    replayable,
)
from backend.tests.installer_sweeps.harness.world import World

LIB = REPO_ROOT / "deploy" / "aws-installer" / "lib"
#: The operations behind the CLI's own commands install runs, for the shapes document.
CUSTOMIZED = [("ecr", "get-authorization-token")]

#: The second time zone for the calls that print a timestamp: off UTC by a
#: fraction of an hour, so an offset printed wrongly shows.
OTHER_ZONE = "Asia/Kolkata"
#: One operation per protocol family, for the access-denial comparison.
DENIALS = (
    ("cloudformation", "list-stacks"),
    ("ec2", "describe-vpcs"),
    ("ecs", "list-clusters"),
    ("lambda", "list-functions"),
    ("route53", "list-hosted-zones"),
    ("s3api", "list-buckets"),
    ("cloudwatch", "describe-alarms"),
)
WORKERS = 8
CLI_TIMEOUT = 120
DUMMY = "h6-no-credential"


def states() -> dict[str, dict]:
    """The accounts the calls are compared on: empty lists, full ones, and a
    secret waiting to be deleted (the sweep reads its DeletedDate)."""
    installed = accounts.installed_account()
    pending = copy.deepcopy(installed)
    pending["regions"]["us-west-2"]["secrets"].append(
        {
            "ARN": f"arn:aws:secretsmanager:us-west-2:{accounts.ACCOUNT}:secret:old-abc123",
            "Name": "a-secret-the-reader-scheduled-for-deletion",
            "DeletedDate": "2026-10-21T05:41:07.512Z",
            "CreatedDate": "2026-09-29T17:02:41Z",
        }
    )
    return {"new": accounts.new_account(), "installed": installed, "pending": pending}


def truncated_state() -> dict:
    """The installed account with more user pools in us-west-2 than one page holds:
    the service answers list-user-pools with a token for the next page, and the
    sweep's read of it fails, as it must."""
    state = accounts.installed_account()
    state["regions"]["us-west-2"]["user_pools_next_token"] = "page-2"
    return state


def shape_of(argv: list[str]) -> tuple:
    """A call without its region: the same shape in either region."""
    out, skip = [], False
    for word in argv:
        if skip:
            skip = False
            continue
        if word == "--region":
            skip = True
            continue
        out.append(word)
    return tuple(out)


def beta_calls(work: Path, shapes_path: Path, state: dict) -> list[list[str]]:
    """Every call the sweep makes against ``state``, one per shape (us-west-2's
    first, where the installed account has its items)."""
    world = World(work, state, shapes_path)
    done = world.sweep("calls")
    if done.code != 0 or world.unknown:
        raise SystemExit(
            f"the sweep failed against the fake (exit {done.code}): "
            f"{done.err.strip()} {world.unknown.strip()}"
        )
    found: dict[tuple, list[str]] = {}
    for call in sorted(world.calls, key=lambda c: c["region"] != "us-west-2"):
        found.setdefault(shape_of(call["argv"]), call["argv"])
    return list(found.values())


def with_output(argv: list[str], output: str) -> list[str]:
    out = list(argv)
    i = out.index("--output")
    out[i + 1] = output
    return out


class Stub(ThreadingHTTPServer):
    """Answers each request with the current case's prepared response, after
    checking that the CLI called the operation the case expects."""

    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), Handler)
        self.case: dict | None = None
        self.seen: list[str] = []

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # the CLI's own output is what is compared
        pass

    def _answer(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        case = self.server.case
        protocol = detect(self.headers, body, case["protocol"])
        called = called_operation(
            protocol, self.command, self.path, self.headers, body, case
        )
        self.server.seen.append(f"{protocol} {called}")
        if called != case["operation"]["name"]:
            status, headers, out = wire.serialize_error(
                protocol,
                case["service"],
                "WrongOperation",
                f"expected {case['operation']['name']}",
            )
        elif case.get("error"):
            code, message = case["error"]
            status, headers, out = wire.serialize_error(
                protocol, case["service"], code, message, 403
            )
        else:
            status, headers, out = wire.serialize(
                protocol, case["service"], case["operation"], case["response"]
            )
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_GET = do_POST = do_PUT = do_HEAD = _answer


def detect(headers, body: bytes, modelled: str) -> str:
    """The protocol of the CLI's request: a service with several (CloudWatch)
    is answered in the one its request used."""
    kind = headers.get("Content-Type", "")
    if headers.get("smithy-protocol") == "rpc-v2-cbor" or "application/cbor" in kind:
        return "smithy-rpc-v2-cbor"
    if "application/x-amz-json" in kind and headers.get("X-Amz-Target"):
        return "json"
    if "application/x-www-form-urlencoded" in kind and b"Action=" in body:
        return "ec2" if modelled == "ec2" else "query"
    return modelled


def called_operation(protocol, method, path, headers, body, case) -> str:
    if protocol == "json":
        return headers.get("X-Amz-Target", "").rsplit(".", 1)[-1]
    if protocol == "smithy-rpc-v2-cbor":
        return path.rsplit("/operation/", 1)[-1]
    if protocol in ("query", "ec2"):
        return parse_qs(body.decode()).get("Action", [""])[0]
    http = case["operation"]["http"]
    pattern = (
        "^"
        + re.sub(
            r"\\\{[^/]+?\\\}", "[^/]+", re.escape(http["requestUri"].split("?")[0])
        )
        + "/?$"
    )
    if method == http["method"] and re.match(pattern, urlsplit(path).path):
        return case["operation"]["name"]
    return f"{method} {path}"


def protocol_of(spec: dict) -> str:
    return spec["metadata"]["protocol"]


def cli_env(home: Path, zone: str) -> dict:
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "AWS_ACCESS_KEY_ID": DUMMY,
        "AWS_SECRET_ACCESS_KEY": DUMMY,
        "AWS_CONFIG_FILE": os.devnull,
        "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_REGION": "us-west-2",
        "AWS_PAGER": "",
        # No AWS_MAX_ATTEMPTS: with 1, botocore adds "(reached max retries: 0)" to
        # every error (measured, CLI 2.28.24), which no reader's CLI prints for a
        # denial. The default retries nothing the stub answers.
        "TZ": zone,
    }


def compare(case: dict, stub: Stub, home: Path) -> str | None:
    """None when the CLI prints what the fake printed; else the difference."""
    stub.case = case
    stub.seen = []
    done = subprocess.run(
        ["aws", *case["argv"], "--endpoint-url", stub.url],
        env=cli_env(home, case["zone"]),
        capture_output=True,
        text=True,
        timeout=CLI_TIMEOUT,
        stdin=subprocess.DEVNULL,
    )
    if case.get("error"):
        # A service's error: the CLI's stderr must be the fake's, exactly, in one
        # of the two forms CLIs print (fake_aws.ERROR_STYLES).
        for style, fake in case["styles"].items():
            if (done.returncode, done.stderr) == (fake.code, fake.err):
                case["matched"] = style
                return None
        forms = "".join(
            f"--- fake ({style})\n{fake.err}" for style, fake in case["styles"].items()
        )
        return (
            f"{case['label']}: the CLI exits {done.returncode}; stderr is neither of "
            f"the fake's forms.\n{forms}--- cli\n{done.stderr}--- requests {stub.seen}"
        )
    fake = case["fake"]
    if (done.returncode, done.stdout) == (fake.code, fake.out):
        return None
    return (
        f"{case['label']}: the fake exits {fake.code}, the CLI {done.returncode}; "
        f"stdout differs.\n--- fake\n{fake.out}--- cli\n{done.stdout}"
        f"--- cli stderr\n{done.stderr}--- requests {stub.seen}"
    )


def truncated_cases(
    work: Path, shapes_path: Path, doc: dict, installed_calls: list[list[str]]
) -> list[dict]:
    """The sweep's user-pool read against a first page with a next-page token, in
    JSON and text. The sweep fails against that account, so the call is the one it
    makes against the installed account."""
    state = truncated_state()
    world = World(work / "fake-truncated", state, shapes_path)
    reads = [a for a in installed_calls if a[:2] == ["cognito-idp", "list-user-pools"]]
    if len(reads) != 1:
        raise SystemExit(f"expected one list-user-pools call, found {len(reads)}")
    spec = doc["cognito-idp"]
    cases = []
    for output in ("json", "text"):
        variant = with_output(reads[0], output)
        cases.append(
            {
                "label": f"[truncated UTC] aws {' '.join(variant)}",
                "argv": variant,
                "zone": "UTC",
                "service": spec,
                "operation": spec["operations"]["list-user-pools"],
                "protocol": protocol_of(spec),
                "response": fake_aws.raw_response(state, variant, world.env()),
                "fake": world.aws(*variant, TZ="UTC"),
            }
        )
    return cases


def install_cases(work: Path, shapes_path: Path, doc: dict) -> list[dict]:
    """Every AWS call of a clean install, one per shape, each against the state it found
    (install_world.install_snapshots): the fake's own output for that state, and the
    response the stub serves. A call the service answers with an error (a role not there
    yet, a stack that does not exist) is compared on stderr, in either form."""
    import base64

    snapshots, _ = install_snapshots(work / "install", shapes_path)
    cases, seen = [], set()
    for n, snap in enumerate(snapshots):
        argv = replayable(snap["argv"], work)
        if shape_of(argv) in seen:
            continue
        seen.add(shape_of(argv))
        service, name = argv[0], argv[1]
        spec = doc[service]
        world = World(work / f"install-{n}", snap["state"], shapes_path)
        label = f"[install UTC] aws {' '.join(snap['argv'])}"
        case = {
            "label": label,
            "argv": argv,
            "zone": "UTC",
            "service": spec,
            "protocol": protocol_of(spec),
        }
        if (service, name) in fake_aws.CUSTOMIZATIONS:
            operation_name = fake_aws.CUSTOMIZATIONS[(service, name)][0]
            kebab = re.sub(r"(?<!^)(?=[A-Z])", "-", operation_name).lower()
            password = snap["state"]["regions"]["us-west-2"].get(
                "ecr_password", "fake-ecr-password"
            )
            case["operation"] = spec["operations"][kebab]
            case["response"] = {
                "authorizationData": [
                    {
                        "authorizationToken": base64.b64encode(
                            f"AWS:{password}".encode()
                        ).decode(),
                        "expiresAt": "2026-10-22T02:38:00Z",
                        "proxyEndpoint": f"https://{accounts.ACCOUNT}.dkr.ecr.us-west-2.amazonaws.com",
                    }
                ]
            }
            case["fake"] = world.aws(*argv, TZ="UTC")
            cases.append(case)
            continue
        case["operation"] = spec["operations"][name]
        try:
            case["response"] = fake_aws.raw_response(snap["state"], argv, world.env())
            case["fake"] = world.aws(*argv, TZ="UTC")
        except fake_aws.ServiceError as error:
            case["error"] = (error.code, error.message)
            case["styles"] = {}
            for style in fake_aws.ERROR_STYLES:
                world.edit(lambda s, style=style: s.update(error_style=style))
                case["styles"][style] = world.aws(*argv, TZ="UTC")
            case["status"] = error.status
        cases.append(case)
    return cases


#: A reader's AWS CLI config at its worst: keys in [default], and in the Identity Center
#: profile and its session a region, an output format, a services section with an
#: endpoint, and keys the CLI does not know. Dummy values only, too short for a secret
#: scanner to take for one.
READER_CONFIG = """[default]
aws_access_key_id = h6key
aws_secret_access_key = h6secret
region = eu-west-1
[profile controlvar-install]
sso_session=acme
sso_account_id = 111111111111
sso_role_name = AdministratorAccess
region = eu-west-1
output = text
services = elsewhere
fancy = 1
[sso-session acme]
sso_start_url = https://acme.awsapps.com/start
sso_region = us-east-1
sso_registration_scopes = sso:account:access
fancy = 2
[services elsewhere]
sts =
  endpoint_url = http://127.0.0.1:1
"""
#: What the CLI resolves from the installer's copy, key by key (None: not there).
COPY_RESOLVES = {
    "sso_session": "acme",
    "sso_account_id": "111111111111",
    "sso_role_name": "AdministratorAccess",
    "region": None,
    "output": None,
    "services": None,
    "fancy": None,
}


#: A reader whose Identity Center profile is their [default] one. The CLI reads a
#: [default] when no profile is named, so a copy written as [default] would make the
#: probe sign in (with no cached sign-in: exit 255, "Error loading SSO Token").
READER_DEFAULT_CONFIG = """[default]
sso_session = acme
sso_account_id = 111111111111
sso_role_name = AdministratorAccess
region = eu-west-1
[sso-session acme]
sso_start_url = https://acme.awsapps.com/start
sso_region = us-east-1
sso_registration_scopes = sso:account:access
"""
#: An alias file in the reader's HOME that makes `aws sts ...` a shell command of its own,
#: which prints a line and exits 0, whatever was asked.
ALIAS = "[toplevel]\nsts = !f() { printf 'an alias ran\\n'; }; f\n"
#: The probe's endpoint: a closed local port.
CLOSED = "http://127.0.0.1:9"


def _aws(argv: list[str], env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["aws", *argv],
        env=env,
        capture_output=True,
        text=True,
        timeout=CLI_TIMEOUT,
        stdin=subprocess.DEVNULL,
    )


def _signin(work: Path, name: str, config: str, profile: str, files=None) -> dict:
    """The installer's copy of ``profile`` from a reader's ``config``, and its probe, by
    the installer's own functions (cv_env_init, cv_home_init, cv_copy_config, cv_probe),
    with ``files`` also in the reader's HOME. Returns the reader's HOME, the copy, the
    name the copy is under, and whether the probe passed in the CLI's own words."""
    home, record = work / f"{name}-home", work / f"{name}-record"
    (record / "tmp").mkdir(parents=True)
    # Where a reader's CLI keeps it: an installer that read the reader's own file, or ran
    # its children with the reader's HOME, would find what is planted here.
    reader = home / ".aws" / "config"
    reader.parent.mkdir(parents=True)
    reader.write_text(config)
    for relative, text in (files or {}).items():
        (home / relative).parent.mkdir(parents=True, exist_ok=True)
        (home / relative).write_text(text)
    script = (
        f'. "{LIB}/names.sh"; . "{LIB}/env.sh"; . "{LIB}/messages.sh"; '
        f'CV_RECORD="{record}"; CV_PROFILE={profile}; CV_BASE_PATH="$PATH"; cv_env_init; cv_home_init; '
        f'cv_copy_config "{reader}" {profile} "$CV_RECORD/aws-config" || exit 3; '
        "printf 'copy %s\\n' \"$CV_COPY_PROFILE\"; "
        "if cv_probe; then printf 'probe passed\\n'; else printf 'probe failed\\n'; fi"
    )
    done = subprocess.run(
        ["bash", "-c", script],
        env={"PATH": os.environ.get("PATH", ""), "HOME": str(home), "LC_ALL": "C"},
        capture_output=True,
        text=True,
        timeout=CLI_TIMEOUT,
    )
    err = record / "tmp" / "probe.err"
    probe = err.read_text() if err.exists() else ""
    lines = done.stdout.splitlines()
    return {
        "home": home,
        "copy": record / "aws-config",
        "name": next((x.split(" ", 1)[1] for x in lines if x.startswith("copy ")), ""),
        "passed": done.returncode == 0
        and lines[-1:] == ["probe passed"]
        and "Unable to locate credentials" in probe,
        "said": f"exit {done.returncode}: {done.stdout.strip()} {done.stderr.strip()} {probe.strip()}",
    }


def _cli_env(home: Path, config: Path | str) -> dict:
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "LC_ALL": "C",
        "AWS_CONFIG_FILE": str(config),
        "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_PAGER": "",
    }


def signin_checks(work: Path) -> list[str]:
    """The installer's copy of the profile and its probe, run by its own functions with
    the runner's CLI (no request leaves this machine: a closed local port, no cached
    sign-in). Each problem, named."""
    failures = []
    one = _signin(work, "signin", READER_CONFIG, "controlvar-install")
    if not one["passed"]:
        failures.append(
            "B2: with no profile, the installer's environment found credentials or did not "
            f"stop for want of them in the CLI's own words ({one['said']})"
        )
    cli = _cli_env(one["home"], one["home"] / ".aws" / "config")
    control = _aws(["sts", "get-caller-identity", "--endpoint-url", CLOSED], cli)
    if (
        control.returncode != 255
        or "Could not connect to the endpoint URL" not in control.stderr
    ):
        failures.append(
            "B2's control: the reader's own file, whose [default] holds a key, should make the CLI "
            f"find credentials and try the closed port; exit {control.returncode}: {control.stderr.strip()}"
        )
    if not one["copy"].exists() or not one["name"]:
        return failures + ["B1: the installer made no copy of the profile"]
    copied = {**cli, "AWS_CONFIG_FILE": str(one["copy"])}
    for key, want in COPY_RESOLVES.items():
        got = _aws(["configure", "get", key, "--profile", one["name"]], copied)
        value = got.stdout.strip() if got.returncode == 0 else None
        if value != want:
            failures.append(
                f"B1: the CLI reads {key} from the installer's copy as {value!r}, not {want!r}"
            )
    resolved = _aws(
        [
            "sts",
            "get-caller-identity",
            "--profile",
            one["name"],
            "--endpoint-url",
            CLOSED,
        ],
        copied,
    )
    if "acme" not in resolved.stderr or resolved.returncode == 0:
        failures.append(
            "B1: the CLI did not resolve the copy's profile to its sso-session acme (with no "
            f"cached sign-in it must say so): exit {resolved.returncode}: {resolved.stderr.strip()}"
        )
    failures += _default_profile_checks(work)
    failures += _alias_checks(work)
    return failures


def _default_profile_checks(work: Path) -> list[str]:
    """B2 with a reader whose Identity Center profile is [default]: the copy is under the
    installer's own name, so the probe still finds nothing; the same CLI, given the reader's
    own file and no profile, does load that [default]'s sign-in."""
    failures = []
    two = _signin(work, "default", READER_DEFAULT_CONFIG, "default")
    if not two["passed"]:
        failures.append(
            "B2 [default]: with the reader's [default] profile copied, the probe found credentials "
            f"or did not stop for want of them in the CLI's own words ({two['said']})"
        )
    copy = two["copy"].read_text() if two["copy"].exists() else ""
    if two["name"] in ("", "default") or not copy.startswith(
        f"[profile {two['name']}]\n"
    ):
        failures.append(
            f"B2 [default]: the copy is not under the installer's own name: {two['name']!r}, {copy[:60]!r}"
        )
    else:
        got = _aws(
            ["configure", "get", "sso_session", "--profile", two["name"]],
            _cli_env(two["home"], two["copy"]),
        )
        if got.returncode != 0 or got.stdout.strip() != "acme":
            failures.append(
                f"B2 [default]: the CLI reads the copy's sso_session as {got.stdout.strip()!r} (exit {got.returncode})"
            )
    control = _aws(
        ["sts", "get-caller-identity", "--endpoint-url", CLOSED],
        _cli_env(two["home"], two["home"] / ".aws" / "config"),
    )
    if control.returncode == 253 or "acme" not in control.stderr:
        failures.append(
            "B2 [default]'s control: the reader's own file, whose [default] is an Identity Center "
            "profile, should make the CLI load its session's sign-in; "
            f"exit {control.returncode}: {control.stderr.strip()}"
        )
    return failures


def _alias_checks(work: Path) -> list[str]:
    """B1 with an alias file in the reader's HOME: the probe runs with the installer's own
    HOME and finds no credentials; the same CLI with the reader's HOME runs the alias."""
    failures = []
    three = _signin(
        work, "alias", READER_CONFIG, "controlvar-install", {".aws/cli/alias": ALIAS}
    )
    if not three["passed"]:
        failures.append(
            "B1 [home]: with an alias file in the reader's HOME, the probe did not stop for want of "
            f"credentials in the CLI's own words ({three['said']})"
        )
    control = _aws(
        ["sts", "get-caller-identity", "--endpoint-url", CLOSED],
        _cli_env(three["home"], os.devnull),
    )
    if control.returncode != 0 or control.stdout != "an alias ran\n":
        failures.append(
            "B1 [home]'s control: with the reader's HOME the CLI should run the alias; "
            f"exit {control.returncode}: {control.stdout.strip()} {control.stderr.strip()}"
        )
    return failures


def build_cases(work: Path, shapes_path: Path, doc: dict) -> list[dict]:
    cases = []
    calls_of: dict[str, list[list[str]]] = {}
    for state_name, state in states().items():
        calls = beta_calls(work / f"calls-{state_name}", shapes_path, state)
        calls_of[state_name] = calls
        world = World(work / f"fake-{state_name}", state, shapes_path)
        for argv in calls:
            service, name = argv[0], argv[1]
            spec = doc[service]
            response = fake_aws.raw_response(state, argv, world.env())
            outputs = (
                ("json", "text")
                if state_name != "pending" or service == "secretsmanager"
                else ()
            )
            for output in outputs:
                variant = with_output(argv, output)
                zones = ("UTC", OTHER_ZONE) if state_name == "pending" else ("UTC",)
                for zone in zones:
                    cases.append(
                        {
                            "label": f"[{state_name} {zone}] aws {' '.join(variant)}",
                            "argv": variant,
                            "zone": zone,
                            "service": spec,
                            "operation": spec["operations"][name],
                            "protocol": protocol_of(spec),
                            "response": response,
                            "fake": world.aws(*variant, TZ=zone),
                        }
                    )
    cases += truncated_cases(work, shapes_path, doc, calls_of["installed"])
    cases += install_cases(work, shapes_path, doc)
    denied = World(work / "denied", accounts.installed_account(), shapes_path)
    for service, name in DENIALS:
        spec = doc[service]
        argv = [service, name, "--output", "json"]
        styles = {}
        for style in fake_aws.ERROR_STYLES:
            denied.edit(
                lambda s, style=style: s.update(
                    faults=[{"mode": "AccessDenied"}], error_style=style
                )
            )
            styles[style] = denied.aws(*argv)
        error = fake_aws._denied(spec, spec["operations"][name], service, denied.state)
        cases.append(
            {
                "label": f"[denied] aws {' '.join(argv)}",
                "argv": argv,
                "zone": "UTC",
                "service": spec,
                "operation": spec["operations"][name],
                "protocol": protocol_of(spec),
                "error": (error.code, error.message),
                "styles": styles,
            }
        )
    return cases


def main(args: list[str]) -> int:
    listing = "--list" in args
    held = sorted(
        k
        for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")
        if os.environ.get(k)
    )
    if held and not listing:
        print(
            f"::error::refusing to run with AWS credentials in the environment: {held}"
        )
        return 1
    if not listing and shutil.which("aws") is None:
        print("::error::no aws CLI on PATH")
        return 1
    work = Path(tempfile.mkdtemp(prefix="installer-cli-"))
    try:
        shapes_path = shapes.write(
            work / "shapes.json", fake_aws.modelled_operations() + CUSTOMIZED
        )
        doc = json.loads(shapes_path.read_text())
        cases = build_cases(work, shapes_path, doc)
        if listing:
            for case in cases:
                print(case["label"])
            print(f"{len(cases)} calls")
            return 0
        version = subprocess.run(["aws", "--version"], capture_output=True, text=True)
        print(
            f"comparing {len(cases)} calls with {version.stdout.strip() or version.stderr.strip()}"
        )
        failures = run(cases, work)
        signin = signin_checks(work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    for failure in failures:
        print(f"::error::H6 {failure.splitlines()[0]}")
        print(failure)
    for failure in signin:
        print(f"::error::{failure}")
    forms = sorted({case["matched"] for case in cases if "matched" in case})
    print(
        f"this CLI prints a service's error in the form: {', '.join(forms) or 'none matched'}"
    )
    print(
        f"{len(cases) - len(failures)} of {len(cases)} calls print as the CLI prints them"
    )
    print(
        "the installer's copy of the profile and its probe: "
        + ("as the CLI resolves them" if not signin else f"{len(signin)} problem(s)")
    )
    return 1 if failures or signin else 0


def run(cases: list[dict], work: Path) -> list[str]:
    queue = list(enumerate(cases))
    lock = threading.Lock()
    failures: dict[int, str] = {}

    def worker(n: int) -> None:
        stub = Stub()
        threading.Thread(target=stub.serve_forever, daemon=True).start()
        home = work / f"home-{n}"
        home.mkdir()
        try:
            while True:
                with lock:
                    if not queue:
                        return
                    index, case = queue.pop(0)
                problem = compare(case, stub, home)
                if problem is not None:
                    with lock:
                        failures[index] = problem
        finally:
            stub.shutdown()

    with concurrent.futures.ThreadPoolExecutor(WORKERS) as pool:
        for future in [pool.submit(worker, n) for n in range(WORKERS)]:
            future.result()
    return [failures[i] for i in sorted(failures)]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
