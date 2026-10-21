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
   fake for a new account and an installed one; every call the fake logged is a
   call the beta makes. Calls differing only in region are one shape.
2. For each shape and state, and for the same call with ``--output text``, the
   fake's own response for that state is served from a stub on 127.0.0.1, in the
   protocol the CLI's request uses (harness/wire.py), and the real CLI runs with
   ``--endpoint-url`` at the stub.
3. Its stdout and exit status must equal the fake's, byte for byte. The calls that
   carry a timestamp run again in a second time zone (the CLI prints an epoch in
   local time). An access denial in each protocol family is compared on stderr.

The CLI's children get dummy keys, AWS_CONFIG_FILE and AWS_SHARED_CREDENTIALS_FILE
at /dev/null and instance metadata off; the stub is the only endpoint. The script
refuses to start with a key in its own environment.

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
from backend.tests.installer_sweeps.harness.world import World

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


def build_cases(work: Path, shapes_path: Path, doc: dict) -> list[dict]:
    cases = []
    for state_name, state in states().items():
        calls = beta_calls(work / f"calls-{state_name}", shapes_path, state)
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
        shapes_path = shapes.write(work / "shapes.json", fake_aws.modelled_operations())
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
    finally:
        shutil.rmtree(work, ignore_errors=True)
    for failure in failures:
        print(f"::error::H6 {failure.splitlines()[0]}")
        print(failure)
    forms = sorted({case["matched"] for case in cases if "matched" in case})
    print(
        f"this CLI prints a service's error in the form: {', '.join(forms) or 'none matched'}"
    )
    print(
        f"{len(cases) - len(failures)} of {len(cases)} calls print as the CLI prints them"
    )
    return 1 if failures else 0


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
