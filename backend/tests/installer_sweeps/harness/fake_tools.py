"""The harness's other fakes: ``cdk``, ``crane``, ``cosign``, ``dig``, ``git``, ``curl``,
``node`` and ``npm``, each run by a shim on PATH (harness/shim.py) as

    main_argv([<state>, <shapes>, <tool>, <arguments...>])

They share the fake ``aws``'s account state and its ``calls.log`` (``service`` is the
tool, ``op`` its subcommand) and ``unknown.log``, and take faults the same way: a fault
whose ``service`` is the tool and ``op`` the subcommand, mode ``error`` (its own
``stderr`` and ``exit``) or ``AccessDenied``. What each models:

* ``cdk`` (the pinned CLI): ``--version``; ``bootstrap`` makes CDKToolkit; ``list`` and
  ``deploy`` read the app's inputs from the environment as app.py does, write the
  manifest into their own ``--output`` folder and the account's availability zones into
  ``cdk.context.json`` where they run (as the real CLI does); ``deploy`` makes the nine
  stacks and what they hold. ``state["cdk"]`` can make a stack fail, block a deploy (the
  fargate stack left CREATE_IN_PROGRESS until the fake is stopped) or change the listing.
* ``crane``: the release images on ghcr.io (``state["registry"]``) and the account's
  registry: digest, manifest, config, login with a password on stdin, copy by digest into
  a repository that exists (block or corrupt by ``state["crane"]``).
* ``cosign``: verify and verify-attestation against each image's recorded signer.
* ``dig``: the internet's view of a zone's name servers (``state["dns"]``), and a zone's
  own server answering for the records that zone holds.
* ``git``: the clone's tag and its commit, and ``archive`` of the tag (this checkout's
  files: the release under test).
* ``curl``: the installed application, before and after its schema: health, modules,
  sign-in with the first administrator's password as Secrets Manager holds it, API keys,
  flags and evaluation; reached only through an address the zone's server gave.
"""

from __future__ import annotations

import base64
import contextlib
import copy
import fcntl
import io
import json
import os
import re
import sys
import tarfile
import time
from pathlib import Path

if __package__:
    from . import accounts as _accounts
    from . import fake_aws as _fake
    from . import made as _made
    from . import shim as _shim
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import accounts as _accounts  # type: ignore[no-redef]
    import fake_aws as _fake  # type: ignore[no-redef]
    import made as _made  # type: ignore[no-redef]
    import shim as _shim  # type: ignore[no-redef]

REPO_ROOT = Path(__file__).resolve().parents[4]
CDK_VERSION = "2.1144.0"
#: The variable infrastructure/cdk/app.py reads for the profile (its former-name spelling
#: is kept on purpose: releases and self-hosters set it).
PROFILE_VARIABLE = "EXPERIMENTLY_PROFILE"
BLOCK_SECONDS = 120
#: What a blocked fake writes its pid into, so a test can see it is gone.
PIDS = "pids.log"
#: And where it writes it again if nothing stopped it before its block ran out.
RAN_OUT = "ran-out.log"


class Ended(Exception):
    def __init__(self, status: int, out: str = "", err: str = ""):
        super().__init__(status)
        self.status, self.out, self.err = status, out, err


@contextlib.contextmanager
def _locked(state_path: Path):
    with open(f"{state_path}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


class Tool:
    """One call of one fake tool."""

    def __init__(self, state_path: Path, tool: str, argv: list[str], env: dict):
        self.state_path, self.tool, self.argv, self.env = state_path, tool, argv, env
        self.root = state_path.parent
        self.state: dict = {}
        self.out = ""
        self.err = ""
        self.stdin = ""
        self.delay = 0.0
        self.op = argv[0] if argv else ""
        if tool == "git" and argv[:1] == ["-C"] and len(argv) > 2:
            self.op = argv[2]
        if tool == "crane" and argv[:1] == ["auth"] and len(argv) > 1:
            self.op = f"auth {argv[1]}"
        if tool == "curl":  # the method and URL, as calls.log and a fault name it
            try:
                args = _curl_args(argv)
                self.op = (
                    (args["method"] or ("POST" if args["data"] is not None else "GET"))
                    + " "
                    + (args["url"] or "")
                )
            except (ValueError, IndexError, OSError):
                pass

    # -- state, under the fake aws's lock ---------------------------------------------
    def load(self) -> None:
        self.state = json.loads(self.state_path.read_text())

    def save(self) -> None:
        self.state_path.write_text(json.dumps(self.state))

    def log(self, status: int, fault: str | None) -> None:
        record = {
            "n": self.state.get("calls", 0),
            "service": self.tool,
            "op": self.op,
            "region": None,
            "argv": [self.tool, *self.argv],
            "exit": status,
            "fault": fault,
            "env": sorted(self.env),
            "values": {k: self.env[k] for k in _fake.LOGGED_VALUES if k in self.env},
            "cwd": os.getcwd(),
        }
        with open(self.root / "calls.log", "a") as handle:
            handle.write(json.dumps(record) + "\n")

    def unknown(self, why: str) -> Ended:
        with open(self.root / "unknown.log", "a") as handle:
            handle.write(f"{why}: {self.tool} {json.dumps(self.argv)}\n")
        return Ended(_fake.EXIT_UNMODELLED, err=f"fake {self.tool}: {why}\n")

    def flag(self, name: str, default=None):
        if name in self.argv:
            i = self.argv.index(name)
            if i + 1 < len(self.argv):
                return self.argv[i + 1]
        return default

    def fault(self):
        return _fake._fault(
            self.state,
            self.tool,
            self.op,
            None,
            self.state.get("calls", 0),
            [self.tool, *self.argv],
        )


def run(state_path: Path, tool: str, argv: list[str], env: dict) -> int:
    t = Tool(state_path, tool, argv, env)
    status, fault_mode = 0, None
    block = None
    # What a tool reads on stdin is read before the state's lock is taken: the other end
    # of its pipe is another fake (aws ecr get-login-password), which needs the lock to
    # answer at all.
    t.stdin = (
        sys.stdin.read() if t.op == "auth login" and "--password-stdin" in argv else ""
    )
    with _locked(state_path):
        t.load()
        t.state["calls"] = t.state.get("calls", 0) + 1
        try:
            fault = t.fault()
            fault_mode = fault["mode"] if fault else None
            if fault_mode == "AccessDenied":
                raise Ended(
                    1, err=f"{tool}: AccessDenied: the sign-in may not do {t.op}\n"
                )
            if fault_mode == "error":
                raise Ended(
                    fault.get("exit", 1), err=fault.get("stderr", f"{tool}: failed\n")
                )
            if fault_mode == "EMPTY":
                raise Ended(0)  # exit 0 and nothing printed, nothing done
            handler = HANDLERS.get(tool)
            if handler is None:
                raise t.unknown("tool not modelled")
            block = handler(t)
        except Ended as ended:
            status = ended.status
            t.out += ended.out
            t.err += ended.err
        _fake.effects(t.state, tool, t.op, [tool, *argv])
        t.log(status, fault_mode)
        t.save()
    sys.stdout.write(t.out)
    sys.stdout.flush()
    sys.stderr.write(t.err)
    sys.stderr.flush()
    if t.delay:
        time.sleep(t.delay)  # a call that takes a while and then succeeds
    if block is not None:
        # Blocked until stopped (a test sends a signal), never holding the state's lock.
        with open(t.root / PIDS, "a") as handle:
            handle.write(f"{os.getpid()} {tool} {t.op}\n")
        deadline = time.time() + block
        while time.time() < deadline:
            time.sleep(0.05)
        with open(t.root / RAN_OUT, "a") as handle:
            handle.write(f"{os.getpid()} {tool} {t.op}\n")
        return 1
    return status


# --- cdk ----------------------------------------------------------------------------------


def _stack_names(env: dict) -> list[str]:
    environment = env.get("ENVIRONMENT", "dev")
    parts = [
        "auth",
        "vpc",
        "dynamodb",
        "database",
        "redis",
        "compute",
        "monitoring",
        "fargate",
        "migrations",
    ]
    if env.get(PROFILE_VARIABLE, "") != "core":
        parts[3:3] = ["dynamodb-counters"]
        parts += ["analytics", "glue-etl"]
    return [f"experimentation-{p}-{environment}" for p in parts]


def _profile_line(env: dict) -> str:
    profile = env.get(PROFILE_VARIABLE, "")
    shown = "core" if profile == "core" else "full"
    suffix = f", {PROFILE_VARIABLE}={profile}" if profile else ""
    return f"[cdk] profile: {shown} (modules/infrastructure/cdk/stacks absent{suffix})"


def _app_env(t: Tool) -> dict:
    """The environment app.py sees; ``state["cdk"]["profile"] == "full"`` is an app that
    builds the full profile whatever it is given (a release whose app differs)."""
    env = dict(t.env)
    if t.state.get("cdk", {}).get("profile") == "full":
        env.pop(PROFILE_VARIABLE, None)
    return env


def _write_manifest(folder: Path, names: list[str], environment) -> None:
    artifacts = {
        name: {
            "type": "aws:cloudformation:stack",
            "environment": environment(name),
            "properties": {"templateFile": f"{name}.template.json"},
            "displayName": name,
        }
        for name in names
    }
    artifacts["Tree"] = {"type": "cdk:tree", "properties": {"file": "tree.json"}}
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "manifest.json").write_text(
        json.dumps(
            {
                "version": "54.0.0",
                "artifacts": artifacts,
                "minimumCliVersion": "2.1142.0",
            }
        )
    )


def _manifest(t: Tool, outdir: str) -> list[str]:
    """The cloud assembly this call synthesises, into ITS output folder. With
    ``stale_default_out`` the CDK's default folder (cdk.out, where it runs) also holds a
    right-looking assembly from an earlier run."""
    account = t.state["account"]
    region = t.env.get("AWS_REGION", "us-east-1")
    names = _stack_names(_app_env(t))
    settings = t.state.get("cdk", {})
    names += settings.get("extra_stacks", [])
    _write_manifest(
        Path(outdir),
        names,
        lambda name: (
            f"aws://{settings.get('account', account)}/{settings.get('stack_regions', {}).get(name, region)}"
        ),
    )
    if settings.get("stale_default_out"):
        _write_manifest(
            Path(os.getcwd()) / "cdk.out",
            _stack_names(t.env),
            lambda name: f"aws://{account}/us-west-2",
        )
    context = Path(os.getcwd()) / "cdk.context.json"
    context.write_text(
        json.dumps(
            {
                f"availability-zones:account={account}:region={region}": [
                    f"{region}a",
                    f"{region}b",
                    f"{region}c",
                ]
            },
            indent=2,
        )
    )
    return names


def _check_cdk_call(t: Tool) -> str:
    """The checks the real CLI and app.py make before anything: an app to run, an
    output folder, the account it signs in to."""
    if t.flag("--app") is None and not (Path(os.getcwd()) / "cdk.json").exists():
        raise Ended(
            1,
            err="--app is required either in command-line, in cdk.json or in ~/.cdk.json\n",
        )
    outdir = t.flag("--output")
    if outdir is None:
        raise t.unknown("a cdk call with no --output")
    if not t.env.get("AWS_PROFILE") or not t.env.get("AWS_CONFIG_FILE"):
        raise Ended(
            1,
            err="Unable to resolve AWS account to use. It must be either configured when you define your CDK Stack, or through the environment\n",
        )
    return outdir


def _assembly(t: Tool) -> list[str]:
    """The stacks of the cloud assembly ``--app`` names: a folder an earlier cdk call
    wrote (its manifest.json), which the CLI then deploys or destroys as it is, with no
    synth. A folder with no manifest is refused, as the CLI refuses it."""
    folder = Path(t.flag("--app"))
    manifest = folder / "manifest.json"
    if not manifest.is_file():
        raise Ended(
            1,
            err=f"--app points to {folder}, which holds no cloud assembly (no manifest.json)\n",
        )
    artifacts = json.loads(manifest.read_text()).get("artifacts", {})
    return [
        name
        for name, artifact in artifacts.items()
        if artifact.get("type") == "aws:cloudformation:stack"
    ]


def cdk(t: Tool):
    if t.argv == ["--version"]:
        t.out = f"{CDK_VERSION} (build 4c3c0b5)\n"
        return None
    settings = t.state.setdefault("cdk", {})
    if t.op == "bootstrap":
        target = t.argv[1] if len(t.argv) > 1 else ""
        if target != f"aws://{t.state['account']}/us-west-2":
            raise Ended(
                1,
                err=f"Need to perform AWS calls for account {target}, but the current credentials are for {t.state['account']}\n",
            )
        builder = _accounts.Builder(t.state["account"])
        builder.state = t.state
        before = copy.deepcopy(t.state)
        builder.bootstrap_stack()
        _attribute(t.state, before, stack="CDKToolkit")
        made = [
            x
            for x in t.state["regions"]["us-west-2"]["stacks"]
            if x["StackName"] == "CDKToolkit" and x["StackStatus"] != "DELETE_COMPLETE"
        ]
        # Made now: CloudFormation stamps a stack with the time it is created. Its
        # outputs name its bucket and repository, as the bootstrap template's do.
        made[-1]["CreationTime"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        made[-1]["Outputs"] = [
            {
                "OutputKey": "BucketName",
                "OutputValue": f"cdk-hnb659fds-assets-{t.state['account']}-us-west-2",
            },
            {
                "OutputKey": "ImageRepositoryName",
                "OutputValue": f"cdk-hnb659fds-container-assets-{t.state['account']}-us-west-2",
            },
            {"OutputKey": "BootstrapVersion", "OutputValue": "30"},
        ]
        t.err = " ✅  Environment aws://<acct>/us-west-2 bootstrapped.\n"
        return None
    if t.op == "list":
        outdir = _check_cdk_call(t)
        names = _manifest(t, outdir)
        t.out = _profile_line(_app_env(t)) + "\n" + "\n".join(names) + "\n"
        return None
    if t.op == "deploy":
        outdir = _check_cdk_call(t)
        if t.flag("--require-approval") != "never" or "--all" not in t.argv:
            raise t.unknown("a deploy without --all --require-approval never")
        if t.flag("--app") is not None:
            _assembly(t)  # deployed as it is: no synth, no cdk.context.json
        else:
            _manifest(t, outdir)
        return _deploy(t, settings)
    if t.op == "destroy":
        _check_cdk_call(t)
        if "--force" not in t.argv or t.flag("--app") is None:
            raise t.unknown("a destroy without --force and --app")
        names = [
            a
            for a in t.argv[1:]
            if not a.startswith("--")
            and a not in {t.flag(f) for f in ("--app", "--output", "--profile")}
        ]
        if len(names) != 1:
            raise t.unknown("a destroy of other than one stack")
        if names[0] not in _assembly(t):
            raise Ended(1, err=f"No stack found matching '{names[0]}'\n")
        return _destroy(t, settings, names[0])
    raise t.unknown("cdk subcommand")


def _deploy(t: Tool, settings: dict):
    region = t.state["regions"]["us-west-2"]
    if settings.get("deploy") == "block" and settings.get("block_before_stacks"):
        return settings.get(
            "block_seconds", BLOCK_SECONDS
        )  # stuck before any stack exists
    builder = _accounts.Builder(t.state["account"])
    builder.state = t.state
    roles = t.state["global"].setdefault("roles", [])
    had_role = any(r["RoleName"] == "AWSServiceRoleForECS" for r in roles)
    if _live_stack(region, "experimentation-fargate-staging") is None:
        before = copy.deepcopy(t.state)
        builder.nine_stacks()
        _attribute(t.state, before)
        # ECS makes the cluster's Container Insights log group itself, when the first task
        # reports: no stack holds it (the AWS guide's run deleted it by hand, 9.9).
        groups = region.setdefault("log_groups", [])
        if not any(g["logGroupName"] == CONTAINER_INSIGHTS for g in groups):
            groups.append(
                {
                    "logGroupName": CONTAINER_INSIGHTS,
                    "creationTime": 1761017700000,
                    "metricFilterCount": 0,
                    "arn": f"arn:aws:logs:us-west-2:{t.state['account']}:log-group:{CONTAINER_INSIGHTS}:*",
                    "storedBytes": 0,
                }
            )
    _upload_templates(t)
    fargate = _live_stack(region, "experimentation-fargate-staging")
    fargate["Outputs"] = [
        {
            "OutputKey": "TaskSubnets",
            "OutputValue": "subnet-0aaa1111bbb22223c,subnet-0ddd4444eee55556f",
        },
        {"OutputKey": "TaskSecurityGroup", "OutputValue": "sg-0123456789abcdef0"},
        {
            "OutputKey": "ALBDnsName",
            "OutputValue": region["load_balancers"][-1]["DNSName"],
        },
        {
            "OutputKey": "ECSServiceArn",
            "OutputValue": f"arn:aws:ecs:us-west-2:{t.state['account']}:service/experimentation-staging/experimentation-backend-staging",
        },
        {
            "OutputKey": "CodeDeployAppName",
            "OutputValue": "experimentation-platform-staging",
        },
        {"OutputKey": "DeploymentGroupName", "OutputValue": "experimentation-staging"},
    ]
    region.setdefault("services", [])
    if not region["services"]:
        events = [
            {
                "id": f"event-{n}",
                "createdAt": "2026-10-21T03:40:00Z",
                "message": message,
            }
            for n, message in enumerate(settings.get("service_events", []))
        ]
        region["services"].append(
            {
                "serviceArn": f"arn:aws:ecs:us-west-2:{t.state['account']}:service/experimentation-staging/experimentation-backend-staging",
                "serviceName": "experimentation-backend-staging",
                "clusterArn": f"arn:aws:ecs:us-west-2:{t.state['account']}:cluster/experimentation-staging",
                "status": "ACTIVE",
                "events": events,
            }
        )
    migrate = settings.get("migrate_revision", 1)
    for td in region.get("task_definitions", []):
        if (
            td["family"] == "experimentation-migrate-staging"
            and td["revision"] == migrate
        ):
            td.setdefault(
                "containerDefinitions",
                [
                    {
                        "name": "backend",
                        "image": f"{t.state['account']}.dkr.ecr.us-west-2.amazonaws.com/experimentation-platform/backend:bootstrap",
                        "essential": True,
                        "command": ["python", "-m", "alembic"],
                    }
                ],
            )
            td.setdefault("networkMode", "awsvpc")
            td.setdefault("requiresCompatibilities", ["FARGATE"])
            td.setdefault("cpu", "512")
            td.setdefault("memory", "1024")
            td.setdefault(
                "executionRoleArn",
                f"arn:aws:iam::{t.state['account']}:role/experimentation-migrations-staging-ExecutionRole",
            )
    if settings.get("slr_race") and not had_role:
        # The race: the services start before ECS has made its role (a named fault,
        # never the default; by default ECS makes the role during the deploy, as the
        # run measured).
        roles[:] = [r for r in roles if r["RoleName"] != "AWSServiceRoleForECS"]
        fargate["StackStatus"] = "ROLLBACK_COMPLETE"
        fargate["events"] = [
            {
                "StackId": fargate["StackId"],
                "EventId": "e1",
                "StackName": fargate["StackName"],
                "LogicalResourceId": "ApiService",
                "ResourceStatus": "CREATE_FAILED",
                "ResourceStatusReason": "Unable to assume the service linked role. Please verify that the ECS service linked role exists.",
                "Timestamp": "2026-10-21T03:45:00Z",
            }
        ]
        # The migrations stack was never created: CloudFormation stopped at fargate.
        _remove_made(t.state, "experimentation-migrations-staging")
        region["stacks"] = [
            s
            for s in region["stacks"]
            if s["StackName"] != "experimentation-migrations-staging"
            or s["StackStatus"] == "DELETE_COMPLETE"
        ]
        raise Ended(
            1,
            err=" ❌  experimentation-fargate-staging failed: ApiService: Unable to assume the service linked role\n",
        )
    for stack, reason in settings.get("fail", {}).items():
        for s in region["stacks"]:
            if s["StackName"] == stack and s["StackStatus"] != "DELETE_COMPLETE":
                s["StackStatus"] = "ROLLBACK_COMPLETE"
                s["events"] = [
                    {
                        "StackId": s["StackId"],
                        "EventId": "e2",
                        "StackName": stack,
                        "LogicalResourceId": "Resource",
                        "ResourceStatus": "CREATE_FAILED",
                        "ResourceStatusReason": reason,
                        "Timestamp": "2026-10-21T03:45:00Z",
                    }
                ]
        raise Ended(1, err=f" ❌  {stack} failed: {reason}\n")
    if settings.get("add_host_record"):
        # Someone adds a record at the install's name while the stacks are created.
        for zone in t.state["global"].get("hosted_zones", []):
            if zone["Name"] == _accounts.INSTALL_ZONE:
                zone.setdefault("records", []).append(
                    {
                        "Name": _accounts.INSTALL_DOMAIN + ".",
                        "Type": "TXT",
                        "TTL": 300,
                        "ResourceRecords": [{"Value": '"v=spf1 -all"'}],
                    }
                )
    if settings.get("deploy") == "block":
        fargate["StackStatus"] = settings.get("block_status", "CREATE_IN_PROGRESS")
        return settings.get("block_seconds", BLOCK_SECONDS)
    for s in region["stacks"]:
        if (
            s["StackName"].startswith("experimentation-")
            and s["StackStatus"] != "DELETE_COMPLETE"
        ):
            s["StackStatus"] = "CREATE_COMPLETE"
    t.out = "\n".join(f" ✅  {s}" for s in _stack_names(t.env)) + "\n"
    # deploy_seconds: the deploy takes that long before it ends (the stacks are already
    # complete when the watcher looks), so what runs beside it runs at least once.
    t.delay = float(settings.get("deploy_seconds", 0))
    return None


def _upload_templates(t: Tool) -> None:
    """The deploy puts each stack's template in the CDK's bucket, a new version each time
    (the AWS guide's run found 9 objects there before deleting it)."""
    bucket = f"cdk-hnb659fds-assets-{t.state['account']}-us-west-2"
    if not any(b["Name"] == bucket for b in t.state["global"].get("buckets", [])):
        return
    objects = t.state["global"].setdefault("bucket_objects", {}).setdefault(bucket, [])
    for name in _stack_names(t.env):
        objects.append(
            {
                "ETag": '"' + _accounts._id(name + str(len(objects)), 32) + '"',
                "Size": 40960,
                "StorageClass": "STANDARD",
                "Key": _accounts._id(name, 64) + ".json",
                "VersionId": _accounts._id("version" + name + str(len(objects)), 32),
                "IsLatest": True,
                "LastModified": "2026-10-21T03:30:00Z",
            }
        )


# --- what each stack made, and cdk destroy ---------------------------------------------------

CONTAINER_INSIGHTS = _made.CONTAINER_INSIGHTS
_live_stack = _made.live_stack
_attribute = _made.attribute
_remove_made = _made.remove_made


def _destroy(t: Tool, settings: dict, name: str):
    """``cdk destroy --force <stack>``: the stack and what it made, gone; an absent stack
    is done at once (the CLI's destroyStack returns early). ``state["cdk"]["destroy_fail"]``
    leaves a stack DELETE_FAILED with a failed resource; ``destroy: block`` keeps the CLI
    running (for a signal) before CloudFormation is asked anything."""
    region = t.state["regions"]["us-west-2"]
    stack = _live_stack(region, name)
    if stack is None:
        t.err = f" ✅  {name}: destroyed\n"
        return None
    if settings.get("destroy") == "block" and settings.get("block_stack", name) == name:
        return settings.get("block_seconds", BLOCK_SECONDS)
    if name in settings.get("destroy_noop", []):
        # The CLI says it is done, and the stack is still there.
        t.err = f" ✅  {name}: destroyed\n"
        return None
    failed = settings.get("destroy_fail", {}).get(name)
    if failed:
        stack["StackStatus"] = "DELETE_FAILED"
        stack["events"] = [
            {
                "StackId": stack["StackId"],
                "EventId": "d1",
                "StackName": name,
                "LogicalResourceId": failed.get("logical", "Resource"),
                "PhysicalResourceId": failed.get("physical", "resource-1"),
                "ResourceType": failed.get("type", "AWS::CloudFormation::Stack"),
                "ResourceStatus": "DELETE_FAILED",
                "ResourceStatusReason": failed["reason"],
                "Timestamp": "2026-10-21T05:58:00Z",
            }
        ]
        for extra in ("vpc_endpoints", "security_groups"):
            region.setdefault(extra, []).extend(failed.get(extra, []))
        raise Ended(
            1,
            err=f" ❌  {name}: destroy failed Error: The stack named {name} is in a failed state. You may need to delete it from the AWS console : DELETE_FAILED ({failed['reason']})\n",
        )
    _remove_made(t.state, name)
    if name == "experimentation-database-staging":
        # The database goes with its stack, and with it the application's data: its
        # schema, its administrator, its keys and flags.
        app = t.state.setdefault("app", {})
        for key in ("schema", "admin", "api_keys", "flags", "token"):
            app.pop(key, None)
    region["tag_index"] = [
        a for a in region.get("tag_index", []) if a != stack["StackId"]
    ]
    stack["StackStatus"] = "DELETE_COMPLETE"
    stack["DeletionTime"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    stack.pop("Outputs", None)
    t.err = f"{name}: destroying... [1/1]\n ✅  {name}: destroyed\n"
    return None


# --- crane and cosign ---------------------------------------------------------------------


def _image(t: Tool, ref: str):
    """(repository, its registry entry, the digest or tag asked for)."""
    if "@" in ref:
        repo, digest = ref.split("@", 1)
        return repo, t.state.get("registry", {}).get(repo), digest
    repo, _, tag = ref.rpartition(":")
    return repo, t.state.get("registry", {}).get(repo), tag


def crane(t: Tool):
    settings = t.state.setdefault("crane", {})
    if t.op == "version":
        t.out = "0.22.1\n"
        return None
    if t.op == "digest":
        repo, entry, tag = _image(t, t.argv[1])
        if entry is None or tag not in entry.get("tags", {}):
            raise Ended(
                1,
                err=f"Error: fetching manifest {t.argv[1]}: GET https://ghcr.io/v2/: MANIFEST_UNKNOWN: manifest unknown\n",
            )
        t.out = entry["tags"][tag] + "\n"
        return None
    if t.op == "manifest":
        repo, entry, digest = _image(t, t.argv[1])
        if entry is None or digest not in entry.get("manifests", {}):
            raise Ended(1, err="Error: MANIFEST_UNKNOWN: manifest unknown\n")
        t.out = json.dumps(entry["manifests"][digest]) + "\n"
        return None
    if t.op == "config":
        repo, entry, digest = _image(t, t.argv[1])
        if entry is None or digest not in entry.get("configs", {}):
            raise Ended(1, err="Error: MANIFEST_UNKNOWN: manifest unknown\n")
        t.out = json.dumps(entry["configs"][digest]) + "\n"
        return None
    if t.op == "auth login":
        registry = t.argv[2]
        if "--password-stdin" not in t.argv:
            raise t.unknown("a login without --password-stdin")
        password = t.stdin.strip()
        if password != t.state["regions"]["us-west-2"].get(
            "ecr_password", "fake-ecr-password"
        ):
            raise Ended(1, err="Error: logging in: unauthorized\n")
        folder = Path(
            t.env.get(
                "DOCKER_CONFIG",
                os.path.join(t.env.get("HOME", "/nonexistent"), ".docker"),
            )
        )
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "config.json"
        config = json.loads(path.read_text()) if path.exists() else {"auths": {}}
        config["auths"][registry] = {
            "auth": base64.b64encode(f"AWS:{password}".encode()).decode()
        }
        path.write_text(json.dumps(config))
        t.err = f"logged in via {path}\n"
        return None
    if t.op == "auth logout":
        folder = Path(t.env.get("DOCKER_CONFIG", "/nonexistent"))
        path = folder / "config.json"
        if path.exists():
            config = json.loads(path.read_text())
            config.get("auths", {}).pop(t.argv[2], None)
            path.write_text(json.dumps(config))
        return None
    if t.op == "copy":
        source, target = t.argv[1], t.argv[2]
        repo, entry, digest = _image(t, source)
        if entry is None or digest not in entry.get("configs", {}):
            raise Ended(1, err=f"Error: copying image: {source}: MANIFEST_UNKNOWN\n")
        registry, _, rest = target.partition("/")
        name, _, tag = rest.rpartition(":")
        config = Path(t.env.get("DOCKER_CONFIG", "/nonexistent")) / "config.json"
        if not config.exists() or registry not in json.loads(config.read_text()).get(
            "auths", {}
        ):
            raise Ended(
                1,
                err=f"Error: PUT https://{registry}/v2/{name}/manifests/{tag}: UNAUTHORIZED: authentication required\n",
            )
        region = t.state["regions"]["us-west-2"]
        if not any(r["repositoryName"] == name for r in region.get("repositories", [])):
            raise Ended(
                1,
                err=f"Error: NAME_UNKNOWN: The repository with name '{name}' does not exist\n",
            )
        stored = (
            settings.get("corrupt_digest", digest)
            if settings.get("copy") == "corrupt"
            else digest
        )
        images = region.setdefault("images", {}).setdefault(name, [])
        images[:] = [i for i in images if tag not in i.get("imageTags", [])]
        images.append(
            {
                "registryId": t.state["account"],
                "repositoryName": name,
                "imageDigest": stored,
                "imageTags": [tag],
                "imageSizeInBytes": 132000000,
                "imagePushedAt": "2026-10-21T02:38:00Z",
                "imageManifestMediaType": "application/vnd.oci.image.manifest.v1+json",
            }
        )
        if settings.get("copy") == "block":
            return settings.get("block_seconds", BLOCK_SECONDS)
        return None
    raise t.unknown("crane subcommand")


def cosign(t: Tool):
    if t.op == "version":
        t.out = "  ______   ______        _______. __    _______ .__   __.\nGitVersion:    v3.1.3\nGitCommit:     0123456\n"
        return
    if t.op in ("verify", "verify-attestation"):
        ref = next(a for a in t.argv[1:] if a.startswith("ghcr.io/"))
        repo, entry, digest = _image(t, ref)
        identity = t.flag("--certificate-identity-regexp", "")
        issuer = t.flag("--certificate-oidc-issuer", "")
        signature = (entry or {}).get("signatures", {}).get(digest)
        if (
            signature is None
            or not re.search(identity, signature["subject"])
            or issuer != signature["issuer"]
        ):
            raise Ended(
                12,
                err="Error: no matching signatures: none of the expected identities matched what was in the certificate\n",
            )
        if t.op == "verify":
            t.out = (
                json.dumps(
                    [
                        {
                            "critical": {
                                "identity": {"docker-reference": repo},
                                "image": {"docker-manifest-digest": digest},
                                "type": "cosign container image signature",
                            },
                            "optional": {
                                "Issuer": signature["issuer"],
                                "Subject": signature["subject"],
                            },
                        }
                    ]
                )
                + "\n"
            )
            return
        if t.flag("--type") != "spdxjson":
            raise t.unknown("an attestation type")
        predicate = signature.get("predicate", "https://spdx.dev/Document")
        payload = base64.b64encode(
            json.dumps({"predicateType": predicate}).encode()
        ).decode()
        t.out = (
            json.dumps(
                {"payloadType": "application/vnd.in-toto+json", "payload": payload}
            )
            + "\n"
        )
        return
    raise t.unknown("cosign subcommand")


# --- dig ----------------------------------------------------------------------------------


def _zones(t: Tool) -> list:
    return t.state["global"].get("hosted_zones", [])


def dig(t: Tool):
    args = [a for a in t.argv if a != "+short" and a != "+norecurse"]
    server = next((a[1:] for a in args if a.startswith("@")), None)
    rest = [a for a in args if not a.startswith("@")]
    if "NS" in rest:
        name = next(a for a in rest if a != "NS").rstrip(".")
        if server == "1.1.1.1":
            for zone in _zones(t):
                if zone["Name"].rstrip(".") == name:
                    shown = (
                        t.state.get("dns", {})
                        .get("public_ns", {})
                        .get(name, zone.get("NameServers", []))
                    )
                    t.out = "".join(f"{ns}.\n" for ns in shown)
            return
        raise t.unknown("an NS question to another server")
    if "A" in rest:
        host = next(a for a in rest if a != "A").rstrip(".") + "."
        for zone in _zones(t):
            if server not in zone.get("NameServers", []):
                continue
            for record in zone.get("records", []):
                if record["Name"] == host and record["Type"] == "A":
                    t.out = "".join(
                        f"{ip}\n"
                        for ip in t.state.get("app", {}).get("ips", ["203.0.113.10"])
                    )
        return
    raise t.unknown("a dig question")


# --- git ----------------------------------------------------------------------------------

#: What ``git archive`` leaves out of this checkout's folders: nothing tracked lives here.
UNTRACKED = {"__pycache__", "cdk.out", ".pytest_cache", "node_modules", ".venv"}


def git(t: Tool):
    args = list(t.argv)
    if args[:1] == ["-C"]:
        args = args[2:]
    settings = t.state.setdefault("git", {})
    if t.op == "rev-parse":
        ref = args[-1]
        tag = ref.split("^", 1)[0]
        if tag not in settings.get("tags", []):
            raise Ended(1)
        t.out = settings["revision"] + "\n"
        return
    if t.op == "archive":
        tag = next(a for a in args[1:] if not a.startswith("--"))
        if tag not in settings.get("tags", []):
            raise Ended(128, err=f"fatal: not a valid object name: {tag}\n")
        paths = args[args.index(tag) + 1 :]
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as tar:
            for path in paths:
                source = REPO_ROOT / path
                if not source.exists():
                    raise Ended(
                        128, err=f"fatal: pathspec '{path}' did not match any files\n"
                    )
                for item in [source] if source.is_file() else sorted(source.rglob("*")):
                    if (
                        set(item.relative_to(REPO_ROOT).parts) & UNTRACKED
                        or item.suffix == ".pyc"
                    ):
                        continue
                    if item.is_file():
                        tar.add(
                            item,
                            arcname=str(item.relative_to(REPO_ROOT)),
                            recursive=False,
                        )
        sys.stdout.buffer.write(buffer.getvalue())
        return
    raise t.unknown("git subcommand")


# --- curl: the installed application --------------------------------------------------------


def _curl_args(argv: list[str]) -> dict:
    out = {
        "headers": [],
        "data": None,
        "method": None,
        "resolve": None,
        "output": None,
        "write": None,
        "url": None,
    }
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-sS", "-s", "-S"):
            i += 1
        elif a == "--resolve":
            out["resolve"] = argv[i + 1]
            i += 2
        elif a == "-o":
            out["output"] = argv[i + 1]
            i += 2
        elif a == "-w":
            out["write"] = argv[i + 1]
            i += 2
        elif a == "--max-time":
            i += 2
        elif a == "-H":
            value = argv[i + 1]
            if value.startswith("@"):
                out["headers"] += [
                    h for h in Path(value[1:]).read_text().splitlines() if h
                ]
            else:
                out["headers"].append(value)
            i += 2
        elif a == "--data-binary":
            value = argv[i + 1]
            out["data"] = (
                Path(value[1:]).read_bytes()
                if value.startswith("@")
                else value.encode()
            )
            i += 2
        elif a == "-X":
            out["method"] = argv[i + 1]
            i += 2
        elif a.startswith("https://"):
            out["url"] = a
            i += 1
        else:
            raise ValueError(a)
    return out


def _password(t: Tool) -> str | None:
    region = t.state["regions"]["us-west-2"]
    for secret in region.get("secrets", []):
        if secret["Name"] == "/staging/experimentation/first-superuser-password":
            return region.get("secret_values", {}).get(secret["ARN"])
    return None


def _respond(t: Tool, method: str, path: str, headers: dict, data: bytes | None):
    app = t.state.setdefault("app", {})
    version = app.get("version", _accounts.RELEASE)
    schema = app.get("schema", False)
    route, _, query = path.partition("?")
    keys = app.setdefault("api_keys", {})
    if route == "/health":
        return 200, {
            "status": "healthy",
            "profile": "core",
            "version": version,
            "environment": "staging",
            "checks": {
                "database": {"status": "healthy"},
                "redis": {"status": "healthy"},
            },
        }
    if route == "/api/v1/modules":
        return 200, {"profile": "core", "modules": [], "version": version}
    if route == "/api/v1/experiments/":
        return 401, {"detail": "Not authenticated"}
    if route == "/api/v1/auth/login" and method == "POST":
        body = json.loads((data or b"{}").decode())
        if (
            schema
            and body.get("email") == app.get("admin")
            and body.get("password") == _password(t)
        ):
            app["token"] = "token-" + str(t.state.get("calls"))
            return 200, {
                "access_token": app["token"],
                "token_type": "bearer",
                "user": {
                    "email": app["admin"],
                    "role": "ADMIN",
                    "is_superuser": True,
                    "auth_provider": "local",
                },
            }
        return (
            (500, {"detail": "Internal Server Error"})
            if not schema
            else (401, {"detail": "Incorrect email or password"})
        )
    signed_in = (
        schema
        and app.get("token")
        and headers.get("authorization") == f"Bearer {app['token']}"
    )
    if route == "/api/v1/auth/me":
        return (
            (200, {"email": app.get("admin"), "role": "ADMIN", "is_superuser": True})
            if signed_in
            else (401, {"detail": "Not authenticated"})
        )
    if route == "/api/v1/api-keys" and method == "POST":
        if not signed_in:
            return 401, {"detail": "Not authenticated"}
        key_id = f"k{len(keys) + 1}"
        keys[key_id] = f"cvk_{key_id}_secretvalue"
        return 201, {
            "id": key_id,
            "key": keys[key_id],
            "name": json.loads(data.decode()).get("name"),
        }
    if route.startswith("/api/v1/api-keys/") and method == "DELETE":
        key_id = route.rsplit("/", 1)[-1]
        if not signed_in or key_id not in keys:
            return 404, {"detail": "Not Found"}
        keys.pop(key_id)
        return 204, None
    if route == "/api/v1/feature-flags/" and method == "POST":
        if not signed_in:
            return 401, {"detail": "Not authenticated"}
        flag = json.loads(data.decode())
        app.setdefault("flags", {})[flag["key"]] = flag
        return 201, {
            "key": flag["key"],
            "status": "active",
            "is_active": True,
            "rollout_percentage": 100,
        }
    if route.startswith("/api/v1/feature-flags/evaluate/"):
        key = headers.get("x-api-key")
        if key is None:
            return 401, {"detail": "API key required"}
        if not schema:
            return 500, {"detail": "Internal Server Error"}
        if key not in keys.values():
            return 401, {"detail": "Invalid API key"}
        flag_key = route.rsplit("/", 1)[-1]
        if flag_key not in app.get("flags", {}):
            return 404, {"detail": "Feature flag not found"}
        return 200, {
            "key": flag_key,
            "enabled": True,
            "config": None,
            "reason": "rollout",
        }
    return 404, {"detail": "Not Found"}


def curl(t: Tool):
    try:
        args = _curl_args(t.argv)
    except (ValueError, IndexError) as error:
        raise t.unknown(f"a curl option {error}") from error
    t.op = (
        (args["method"] or ("POST" if args["data"] is not None else "GET"))
        + " "
        + (args["url"] or "")
    )
    app = t.state.setdefault("app", {})
    url = args["url"] or ""
    host = url[len("https://") :].split("/", 1)[0]
    path = (
        "/" + url[len("https://") :].split("/", 1)[1]
        if "/" in url[len("https://") :]
        else "/"
    )
    resolve = args["resolve"] or ""
    ip = resolve.rsplit(":", 1)[-1]
    if (
        resolve != f"{host}:443:{ip}"
        or ip not in app.get("ips", ["203.0.113.10"])
        or not app.get("serving", True)
    ):
        if args["write"]:
            t.out = "000"
        raise Ended(
            7, out=t.out, err=f"curl: (7) Failed to connect to {host} port 443\n"
        )
    headers = {}
    for header in args["headers"]:
        name, _, value = header.partition(":")
        headers[name.strip().lower()] = value.strip()
    status, body = app.get("override", {}).get(path.split("?")[0], (None, None))
    if status is None:
        status, body = _respond(
            t,
            args["method"] or ("POST" if args["data"] is not None else "GET"),
            path,
            headers,
            args["data"],
        )
    if args["output"]:
        Path(args["output"]).write_text("" if body is None else json.dumps(body))
    if args["write"]:
        t.out = str(status)
    return


# --- node and npm ---------------------------------------------------------------------------


def node(t: Tool):
    if t.argv == ["--version"]:
        t.out = t.state.get("node_version", "v22.14.0") + "\n"
        return
    raise t.unknown("node")


def npm(t: Tool):
    """``npm ci`` in the folder holding the pinned package.json and lockfile: the CLI's
    command appears in node_modules/.bin, as the real install leaves it."""
    if t.op == "ci":
        here = Path(os.getcwd())
        if not (here / "package-lock.json").exists():
            raise Ended(
                1,
                err="npm ERR! The `npm ci` command can only install with an existing package-lock.json\n",
            )
        command = here / "node_modules" / ".bin" / "cdk"
        command.parent.mkdir(parents=True, exist_ok=True)
        _shim.write(command, "fake_tools", str(t.state_path), SHAPES, "cdk", site=SITE)
        return
    raise t.unknown("npm")


HANDLERS = {
    "cdk": cdk,
    "crane": crane,
    "cosign": cosign,
    "dig": dig,
    "git": git,
    "curl": curl,
    "node": node,
    "npm": npm,
}

#: The shapes document and the site folder this call was started with (npm writes a cdk
#: command that starts the same way).
SHAPES = ""
SITE = ""


def main_argv(argv: list[str]) -> int:
    """``<state> <shapes> <tool> <arguments...>``: what the shim on PATH runs."""
    global SHAPES, SITE
    SHAPES, SITE = argv[1], _shim.site_dir()
    return run(Path(argv[0]), argv[2], argv[3:], dict(os.environ))


if __name__ == "__main__":
    sys.exit(main_argv(sys.argv[1:]))
