"""An install run against the fakes, in a scratch folder.

``InstallWorld`` is a World (the fake account, its state and logs) plus what a reader's
computer holds for an install:

* a clone of the repository, as the launcher finds it: a copy of deploy/aws-installer/
  beside a VERSION file (``accounts.RELEASE``) and the CDK's cdk.context.json, so that a
  change the install makes to the clone shows; with ``stub_sweep`` the sweep library in
  the copy is a stub that writes a fixed record and makes no call (the fault sweeps run
  hundreds of installs, and test the library alone elsewhere);
* a HOME with an AWS CLI config holding one IAM Identity Center profile and its
  session, and that session's token cache, written moments ago;
* the record's pinned tools, already in place (the fake cdk and the test's own Python,
  which holds aws-cdk-lib at the lock's version), so the tools step reuses them;
* shims first on PATH for aws, cdk, crane, cosign, dig, git, curl, node, npm and
  python3 (the test's own Python); jq, openssl, sed, tar and the rest are this machine's.

The run's environment holds no AWS key: the installer must need none. Polls and caps are
set to run at once (no test asserts a duration).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import pty
import shutil
import signal
import stat
import subprocess
import sys
import termios
import threading
import time
from pathlib import Path

from . import accounts
from . import shim as _shim_module
from .world import FAKE, REPO_ROOT, SAFETY_NET_SECONDS, Result, World

INSTALLER = REPO_ROOT / "deploy" / "aws-installer"
PROFILE = "controlvar-install"
SESSION = "acme"
ALARM = "alerts@beta.controlvar.dev"

#: A sweep library that records nothing in AWS: one file, no call.
STUB_SWEEP = """# shellcheck shell=bash
sweep_take() {
    mkdir -p "$1/us-west-2" || return 2
    printf 'stub\\n' > "$1/us-west-2/stub.txt"
    printf 'sweep %s: 1 reads, 0 failed -> %s\\n' "${1##*/}" "$1"
}
sweep_verdict() { return 2; }
"""


def _python_shim(path: Path) -> None:
    """The test's own Python, which never makes a venv or runs pip here: a tools step
    that tried to install would reach the network."""
    path.write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do case "$a" in venv|pip|ensurepip)\n'
        '    printf "fake python3: no %s in a test\\n" "$a" >&2; exit 97 ;; esac; done\n'
        f'exec "{sys.executable}" "$@"\n'
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class InstallWorld(World):
    def __init__(
        self,
        tmp: Path,
        state: dict,
        shapes: Path,
        *,
        stub_sweep: bool = False,
        token_age_seconds: int = 60,
    ) -> None:
        super().__init__(tmp, state, shapes)
        self.shapes = shapes
        site = _shim_module.site_dir()
        for name in ("crane", "cosign", "dig", "git", "curl", "node", "npm"):
            _shim_module.write(
                self.bin / name,
                "fake_tools",
                str(self.state_path),
                str(shapes),
                name,
                site=site,
            )
        _python_shim(self.bin / "python3")
        self.clone = self.root / "clone"
        shutil.copytree(INSTALLER, self.clone / "deploy" / "aws-installer")
        if stub_sweep:
            (self.clone / "deploy" / "aws-installer" / "lib" / "sweep.sh").write_text(
                STUB_SWEEP
            )
        (self.clone / "VERSION").write_text(accounts.RELEASE + "\n")
        context = self.clone / "infrastructure" / "cdk" / "cdk.context.json"
        context.parent.mkdir(parents=True)
        context.write_text("{}\n")
        # The CDK app's own cdk.json, as a clone has it: a cdk call run in the clone
        # would work, and write its cdk.context.json there.
        shutil.copy2(
            REPO_ROOT / "infrastructure" / "cdk" / "cdk.json",
            context.parent / "cdk.json",
        )
        self.launcher = self.clone / "deploy" / "aws-installer" / "controlvar-aws"
        self.account = state["account"]
        self.record = self.home / ".controlvar-aws" / f"{self.account}-us-west-2"
        self.write_config()
        self.write_token(age_seconds=token_age_seconds)
        self.seed_tools(site)

    # -- the reader's computer ----------------------------------------------------------
    def write_config(self, text: str | None = None) -> Path:
        path = self.home / ".aws" / "config"
        path.parent.mkdir(parents=True, exist_ok=True)
        if text is None:
            text = (
                f"[profile {PROFILE}]\n"
                f"sso_session = {SESSION}\n"
                f"sso_account_id = {self.account}\n"
                "sso_role_name = AdministratorAccess\n"
                "region = us-west-2\n"
                "output = text\n"
                f"[sso-session {SESSION}]\n"
                "sso_start_url = https://acme.awsapps.com/start\n"
                "sso_region = us-east-1\n"
                "sso_registration_scopes = sso:account:access\n"
            )
        path.write_text(text)
        return path

    def token_path(self, session: str = SESSION) -> Path:
        name = hashlib.sha1(session.encode()).hexdigest() + ".json"
        return self.home / ".aws" / "sso" / "cache" / name

    def write_token(
        self,
        age_seconds: int = 60,
        left_seconds: int = 3300,
        refresh: bool = True,
        session: str = SESSION,
    ) -> Path:
        path = self.token_path(session)
        path.parent.mkdir(parents=True, exist_ok=True)
        expires = time.gmtime(time.time() + left_seconds)
        token = {
            "startUrl": "https://acme.awsapps.com/start",
            "region": "us-east-1",
            "accessToken": "token-value-never-printed",
            "expiresAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", expires),
            "clientId": "client",
            "clientSecret": "client-secret-never-printed",
        }
        if refresh:
            token["refreshToken"] = "refresh-value-never-printed"
        path.write_text(json.dumps(token))
        written = time.time() - age_seconds
        os.utime(path, (written, written))
        return path

    def seed_tools(self, site: str) -> None:
        tools = self.record / "tools"
        cdk = tools / "cdk-cli" / "node_modules" / ".bin" / "cdk"
        cdk.parent.mkdir(parents=True)
        _shim_module.write(
            cdk, "fake_tools", str(self.state_path), str(self.shapes), "cdk", site=site
        )
        venv = tools / "venv" / "bin"
        venv.mkdir(parents=True)
        _python_shim(venv / "python3")
        self.record.chmod(0o700)

    # -- running ------------------------------------------------------------------------
    def install_env(self, **extra: str) -> dict:
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("AWS_", "CONTROLVAR_AWS_"))
            and k not in ("PYTHONPATH",)
        }
        env.update(
            PATH=f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
            HOME=str(self.home),
            LC_ALL="C",
            # Polls short, so nothing waits; one heartbeat at most (the first look at the
            # deploy), so that the calls a run makes do not depend on the machine's speed.
            CONTROLVAR_AWS_POLL_SECONDS="0",
            CONTROLVAR_AWS_TRIES="3",
            CONTROLVAR_AWS_CERT_MINUTES="1",
            CONTROLVAR_AWS_WATCH_POLL_SECONDS="0.1",
            CONTROLVAR_AWS_TICK_SECONDS="0.05",
            CONTROLVAR_AWS_HEARTBEAT_SECONDS="3600",
        )
        env.update(extra)
        return env

    def install_argv(
        self, *extra: str, yes: bool = True, defaults: bool = True
    ) -> list[str]:
        argv = [str(self.launcher), "install"]
        if defaults:
            argv += [
                "--account",
                self.account,
                "--domain",
                accounts.INSTALL_DOMAIN,
                "--alarm-email",
                ALARM,
                "--aws-profile",
                PROFILE,
            ]
        if yes:
            argv.append("--yes")
        return argv + list(extra)

    def install(
        self,
        *extra: str,
        yes: bool = True,
        defaults: bool = True,
        bash: str = "bash",
        stdin: str | None = None,
        **env: str,
    ) -> Result:
        # In a session of its own, so that a run that hangs is stopped whole, with every
        # child it started, rather than left behind by the timeout.
        process = subprocess.Popen(
            [bash, *self.install_argv(*extra, yes=yes, defaults=defaults)],
            cwd=self.root,
            env=self.install_env(**env),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
        try:
            out, err = process.communicate(stdin, timeout=SAFETY_NET_SECONDS)
        except subprocess.TimeoutExpired:
            stop_group(process)
            raise
        return Result(process.returncode, out, err)

    def start(self, *extra: str, bash: str = "bash", **env: str) -> subprocess.Popen:
        """An install started in its own process group, for a test to signal."""
        return subprocess.Popen(
            [bash, *self.install_argv(*extra)],
            cwd=self.root,
            env=self.install_env(**env),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )

    def on_terminal(
        self,
        *extra: str,
        typed: str = "",
        yes: bool = False,
        bash: str = "bash",
        **env: str,
    ) -> tuple[Result, str]:
        """An install whose stdin and controlling terminal are a pseudo-terminal (what a
        reader's terminal is), stdout and stderr pipes. ``typed`` is what the reader types.
        Returns the result and everything the terminal showed (the typing included)."""
        master, slave = pty.openpty()

        def controlling_terminal() -> None:  # in the child, after setsid
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        process = subprocess.Popen(
            [bash, *self.install_argv(*extra, yes=yes)],
            cwd=self.root,
            env=self.install_env(**env),
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            preexec_fn=controlling_terminal,
        )
        os.close(slave)
        if typed:
            os.write(master, typed.encode())
        shown = bytearray()
        reader = threading.Thread(target=_drain, args=(master, shown), daemon=True)
        reader.start()
        try:
            out, err = process.communicate(timeout=SAFETY_NET_SECONDS)
        finally:
            reader.join(timeout=10)
            os.close(master)
        return Result(process.returncode, out.decode(), err.decode()), shown.decode(
            errors="replace"
        )

    def use_clock(self, step_seconds: int) -> None:
        """A fake clock: each ``date +%s`` answers a time ``step_seconds`` later than the
        one before; every other use of date is the system's."""
        clock = self.fake_dir / "clock"
        clock.write_text(f"{int(time.time())}\n")
        (self.fake_dir / "clock.step").write_text(f"{step_seconds}\n")
        real = shutil.which("date") or "/bin/date"
        shim = self.bin / "date"
        shim.write_text(
            "#!/bin/sh\n"
            f'clock="{clock}"\n'
            'if [ "$#" -eq 1 ] && [ "$1" = "+%s" ] && [ -f "$clock" ]; then\n'
            '    now=$(cat "$clock") step=$(cat "$clock.step")\n'
            '    printf "%s\\n" $((now + step)) > "$clock.$$" && mv "$clock.$$" "$clock"\n'
            '    printf "%s\\n" "$now"\n'
            "    exit 0\n"
            "fi\n"
            f'exec "{real}" "$@"\n'
        )
        shim.chmod(0o755)

    def effect(self, **spec) -> None:
        """Something that happens on this computer during a call (fake_aws.effects)."""
        self.edit(lambda s: s.setdefault("effects", []).append(spec))

    # -- what it did --------------------------------------------------------------------
    @property
    def aws_calls(self) -> list[dict]:
        """The calls that reached an AWS service (not a local tool, not `aws --version`)."""
        return [c for c in self.calls if is_aws(c)]

    def writes(self) -> list[tuple[str, str]]:
        """Every call that changes something, in order: (service, operation)."""
        return [(c["service"], c["op"]) for c in self.calls if is_write(c)]

    def facts(self) -> dict[str, str]:
        out = {}
        folder = self.record / "env.d"
        if folder.is_dir():
            for path in sorted(folder.glob("*.sh")):
                out[path.stem] = path.read_text()
        return out

    def log_text(self) -> str:
        folder = self.record / "logs"
        return (
            "".join(p.read_text(errors="replace") for p in sorted(folder.glob("*.log")))
            if folder.is_dir()
            else ""
        )

    def pid_lines(self) -> list[str]:
        """``<pid> <tool> <op>`` for each fake that blocked (and may still be blocked)."""
        path = self.fake_dir / "pids.log"
        if not path.exists():
            return []
        return [line for line in path.read_text().splitlines() if line.strip()]

    def blocked_pids(self) -> list[int]:
        return [int(line.split()[0]) for line in self.pid_lines()]

    def clone_bytes(self) -> dict[str, str]:
        return {
            str(p.relative_to(self.clone)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(self.clone.rglob("*"))
            if p.is_file()
        }


#: What a clean install changes, in order (D1): the CDK's setup stack, the two
#: repositories, the two secrets, the certificate, its validation record, the ECS
#: service-linked role, the two images, the nine stacks, the address, the migration's
#: revision and task, then the smoke check's sign-in, key, flag and the key's revocation.
CLEAN_WRITES = [
    ("cdk", "bootstrap"),
    ("ecr", "create-repository"),
    ("ecr", "create-repository"),
    ("secretsmanager", "create-secret"),
    ("secretsmanager", "create-secret"),
    ("acm", "request-certificate"),
    ("route53", "change-resource-record-sets"),
    ("iam", "create-service-linked-role"),
    ("crane", "copy"),
    ("crane", "copy"),
    ("cdk", "deploy"),
    ("route53", "change-resource-record-sets"),
    ("ecs", "register-task-definition"),
    ("ecs", "run-task"),
    ("curl", f"POST https://{accounts.INSTALL_DOMAIN}/api/v1/auth/login"),
    ("curl", f"POST https://{accounts.INSTALL_DOMAIN}/api/v1/api-keys"),
    ("curl", f"POST https://{accounts.INSTALL_DOMAIN}/api/v1/feature-flags/"),
    ("curl", f"DELETE https://{accounts.INSTALL_DOMAIN}/api/v1/api-keys/k1"),
]

#: The operations that change something in AWS or the account's registry.
WRITE_PREFIXES = (
    "create-",
    "request-",
    "change-",
    "register-",
    "run-",
    "put-",
    "delete-",
    "update-",
)
WRITE_TOOLS = {("cdk", "bootstrap"), ("cdk", "deploy"), ("crane", "copy")}
#: The tools the harness fakes beside aws.
LOCAL_TOOLS = ("cdk", "crane", "cosign", "dig", "git", "curl", "node", "npm")
#: What the shims and the interpreter add to a child's environment by themselves (bash as
#: /bin/sh, macOS, Python's C-locale coercion), never the installer.
SHIM_ADDS = frozenset(
    {"PWD", "OLDPWD", "SHLVL", "_", "LC_CTYPE", "__CF_USER_TEXT_ENCODING"}
)


def is_write(call: dict) -> bool:
    if (call["service"], call["op"]) in WRITE_TOOLS:
        return True
    if call["service"] in LOCAL_TOOLS:
        return call["service"] == "curl" and call["op"].split(" ", 1)[0] in (
            "POST",
            "DELETE",
        )
    return bool(call["op"]) and call["op"].startswith(WRITE_PREFIXES)


def is_aws(call: dict) -> bool:
    return call["service"] not in LOCAL_TOOLS and call["service"] != "--version"


def _blocked_deploy(state: dict, **extra) -> None:
    state.setdefault("cdk", {}).update(deploy="block", block_seconds=60, **extra)


def _stopped_api_tasks(state: dict) -> None:
    _blocked_deploy(state)
    account = state["account"]
    for n in (1, 2):
        state["regions"]["us-west-2"].setdefault("tasks", []).append(
            {
                "taskArn": f"arn:aws:ecs:us-west-2:{account}:task/experimentation-staging/{n:032x}",
                "clusterArn": f"arn:aws:ecs:us-west-2:{account}:cluster/experimentation-staging",
                "taskDefinitionArn": f"arn:aws:ecs:us-west-2:{account}:task-definition/experimentation-backend-staging:1",
                "lastStatus": "STOPPED",
                "desiredStatus": "STOPPED",
                "stoppedReason": "CannotPullContainerError: pull image manifest has been retried 5 time(s)",
                "containers": [],
            }
        )


#: The paths a clean install does not take that make AWS calls of their own, each with
#: the exit it ends in: a quota with no applied value (AWS's default is read), a stack
#: that fails (its events are read), and the watcher's reads when the API's tasks stop
#: and when a service event names the service-linked role.
OTHER_PATHS = {
    "a-quota-at-its-default": (
        lambda s: s["regions"]["us-west-2"]["quotas"]["L-3032A538"].update(
            applied=False, default=6.0
        ),
        0,
    ),
    "a-stack-that-fails": (
        lambda s: s.setdefault("cdk", {}).update(
            fail={"experimentation-database-staging": "Aurora said no"}
        ),
        1,
    ),
    "stopped-api-tasks": (_stopped_api_tasks, 1),
    "a-service-event": (
        lambda s: _blocked_deploy(
            s,
            service_events=[
                "(service experimentation-backend-staging) was unable to assume the service-linked role"
            ],
        ),
        1,
    ),
}


def install_snapshots(root: Path, shapes: Path) -> tuple[list[dict], "InstallWorld"]:
    """A clean install, then each of OTHER_PATHS, with the state each AWS call found kept:
    ``{"argv", "env", "state"}`` per call, in order (the probe and the CLI's version aside),
    for the checks that replay install's calls (test_wire.py, scripts/check_installer_cli.py).
    The world returned is the clean install's."""
    paths = [("clean", (None, 0)), *OTHER_PATHS.items()]

    def one(name: str, change, code: int) -> tuple[list[dict], "InstallWorld"]:
        world = InstallWorld(
            root / name, accounts.install_target(), shapes, stub_sweep=True
        )
        world.edit(lambda s: s.update(snapshots=True))
        if change is not None:
            world.edit(change)
        done = world.install()
        if done.code != code or world.unknown:
            raise RuntimeError(
                f"{name} against the fakes ended in exit {done.code}, not {code}: {done.err} {world.unknown}"
            )
        snaps = []
        for path in sorted(
            (world.fake_dir / "snapshots").glob("*.json"), key=lambda p: int(p.stem)
        ):
            snap = json.loads(path.read_text())
            argv = snap["argv"]
            if argv and argv[0] != "--version" and "http://127.0.0.1:9" not in argv:
                snaps.append(snap)
        return snaps, world

    done = in_parallel([lambda n=n, c=c, k=k: one(n, c, k) for n, (c, k) in paths])
    return [snap for snaps, _ in done for snap in snaps], done[0][1]


def replayable(argv: list[str], folder: Path) -> list[str]:
    """``argv`` with each file:// argument whose file is gone (the secrets' values, which
    install removes once created) pointing at a stand-in file in ``folder``."""
    out = []
    for n, word in enumerate(argv):
        if word.startswith("file://") and not Path(word[len("file://") :]).exists():
            stand_in = folder / f"stand-in-{n}"
            stand_in.write_text("stand-in-value-for-a-replay")
            word = f"file://{stand_in}"
        out.append(word)
    return out


#: Independent runs at once (each has its own fake state and scratch folder): the fault
#: sweeps' CI budget depends on it.
WORKERS = max(2, min(8, os.cpu_count() or 2))


def in_parallel(jobs):
    """Each job (a function of no arguments) run on a worker thread; their results, in
    order. A job that raises re-raises here."""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        return list(pool.map(lambda job: job(), jobs))


class Ran:
    """One scenario's world and how its run ended (or the harness error it raised)."""

    def __init__(
        self, world: "InstallWorld", result=None, error: BaseException | None = None
    ) -> None:
        self.world, self._result, self.error = world, result, error

    @property
    def done(self):
        if self.error is not None:
            raise self.error
        return self._result


class Batch:
    """Install scenarios a test module registers by name, run all at once (each in a
    world of its own), so that a module of single-run tests costs about its slowest run
    per worker rather than the sum. ``serial`` scenarios (a pseudo-terminal's, whose
    child setup must not run beside threads) run one by one after the others."""

    def __init__(self) -> None:
        self.scenarios: dict[str, dict] = {}

    def add(
        self,
        name: str,
        prepare=None,
        run=None,
        serial: bool = False,
        stub_sweep: bool = True,
    ) -> str:
        assert name not in self.scenarios, name
        self.scenarios[name] = {
            "prepare": prepare,
            "run": run,
            "serial": serial,
            "stub_sweep": stub_sweep,
        }
        return name

    def in_the_other_error_form(self, names: list[str]) -> None:
        """Each named scenario again as ``<name>-plain``: the fake prints every AWS error
        without the ``aws: [ERROR]: `` that newer CLIs put first, as aws-cli 2.28.24
        does."""
        for name in names:
            spec = self.scenarios[name]

            def prepare(world, first=spec["prepare"]) -> None:
                if first is not None:
                    first(world)
                world.edit(lambda state: state.update(error_style="plain"))

            self.add(
                f"{name}-plain",
                prepare=prepare,
                run=spec["run"],
                serial=spec["serial"],
                stub_sweep=spec["stub_sweep"],
            )

    def run_all(self, root: Path, shapes: Path) -> dict[str, Ran]:
        """Every scenario, or (for a run by hand while working on one) only those whose
        names match a pattern in INSTALL_TEST_ONLY, space-separated; a test whose
        scenario did not run then fails on its missing name."""
        import fnmatch

        only = os.environ.get("INSTALL_TEST_ONLY", "").split()
        if only:
            self.scenarios = {
                n: s
                for n, s in self.scenarios.items()
                if any(fnmatch.fnmatch(n, p) for p in only)
            }
        worlds = {}
        for name, spec in self.scenarios.items():
            worlds[name] = InstallWorld(
                root / name,
                accounts.install_target(),
                shapes,
                stub_sweep=spec["stub_sweep"],
            )

        def go(name: str) -> Ran:
            spec, world = self.scenarios[name], worlds[name]
            try:
                if spec["prepare"] is not None:
                    spec["prepare"](world)
                result = (
                    spec["run"](world) if spec["run"] is not None else world.install()
                )
            except Exception as error:  # the scenario's test re-raises it
                return Ran(world, error=error)
            return Ran(world, result)

        parallel = [n for n, s in self.scenarios.items() if not s["serial"]]
        ran = dict(zip(parallel, in_parallel([lambda n=n: go(n) for n in parallel])))
        for name, spec in self.scenarios.items():
            if spec["serial"]:
                ran[name] = go(name)
        return ran


def _drain(fd: int, into: bytearray) -> None:
    """Everything a pseudo-terminal's master side reads until the terminal closes."""
    while True:
        try:
            chunk = os.read(fd, 4096)
        except OSError:  # EIO once the last process holding the terminal has gone
            return
        if not chunk:
            return
        into.extend(chunk)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def session_pids(sid: int) -> list[int]:
    """Every live process in the session ``sid`` (an install started in a session of its
    own: the installer, its children, and their own process groups)."""
    listing = subprocess.run(
        ["ps", "-A", "-o", "pid="], capture_output=True, text=True, check=True
    )
    found = []
    for word in listing.stdout.split():
        pid = int(word)
        try:
            if os.getsid(pid) == sid:
                found.append(pid)
        except (ProcessLookupError, PermissionError):
            continue
    return found


def stop_group(process: subprocess.Popen) -> None:
    """Whatever a test left running, stopped: the installer's session, every group in it."""
    for pid in session_pids(process.pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    process.wait(timeout=SAFETY_NET_SECONDS)


__all__ = ["FAKE", "InstallWorld", "alive", "is_aws", "is_write", "stop_group"]
