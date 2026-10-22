"""A fake account in a scratch folder, and the beta's shell code run against it.

``World`` writes the state, puts a command named ``aws`` first on PATH (harness/shim.py:
it runs the fake with the state and shapes files as its first arguments, in this test's
Python), gives the run a scratch HOME, and reads back the fake's logs. Nothing here
reaches AWS: the shim is the only ``aws`` the code under test can find before the
test session's failing one (backend/tests/no_real_aws.py), and the credential
chain is the session's dummies.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import shim as _shim

REPO_ROOT = Path(__file__).resolve().parents[4]
LIB = REPO_ROOT / "deploy" / "aws-installer" / "lib"
HARNESS = Path(__file__).resolve().parent
FAKE = HARNESS / "fake_aws.py"
DRIVER = HARNESS / "drive_sweep.sh"

#: A run that takes longer than this is hung, not slow (no test asserts a time).
SAFETY_NET_SECONDS = 300

#: Every distinct bash on this machine: on macOS /bin/bash 3.2 and Homebrew's, on
#: CI's Linux one.
BASHES = sorted(
    {
        os.path.realpath(b)
        for b in (shutil.which("bash"), "/bin/bash")
        if b and os.path.exists(b)
    }
)


def bash_id(path: str) -> str:
    done = subprocess.run(
        [path, "-c", "printf %s ${BASH_VERSINFO[0]}.${BASH_VERSINFO[1]}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return f"bash{done.stdout}"


@dataclass
class Result:
    code: int
    out: str
    err: str


class World:
    """One fake account, its logs, and runs of the sweep library against it."""

    def __init__(self, tmp: Path, state: dict, shapes: Path) -> None:
        self.root = Path(tmp).resolve()
        self.fake_dir = self.root / "fake"
        self.fake_dir.mkdir(parents=True)
        self.state_path = self.fake_dir / "state.json"
        self.state = state
        self.bin = self.root / "bin"
        self.bin.mkdir()
        _shim.write(self.bin / "aws", "fake_aws", str(self.state_path), str(shapes))
        self.home = self.root / "home"
        self.home.mkdir()

    # -- the state ----------------------------------------------------------------------
    @property
    def state(self) -> dict:
        return json.loads(self.state_path.read_text())

    @state.setter
    def state(self, value: dict) -> None:
        self.state_path.write_text(json.dumps(value))

    def edit(self, change) -> None:
        """``change(state)`` in place, written back."""
        state = self.state
        change(state)
        self.state = state

    def fault(self, **spec) -> None:
        self.edit(lambda s: s.setdefault("faults", []).append(spec))

    # -- the logs -----------------------------------------------------------------------
    @property
    def calls(self) -> list[dict]:
        path = self.fake_dir / "calls.log"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    @property
    def unknown(self) -> str:
        path = self.fake_dir / "unknown.log"
        return path.read_text() if path.exists() else ""

    def clear_logs(self) -> None:
        for name in ("calls.log", "unknown.log"):
            (self.fake_dir / name).unlink(missing_ok=True)

    # -- running ------------------------------------------------------------------------
    def env(self, **extra: str) -> dict:
        env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}
        env.update(
            PATH=f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
            HOME=str(self.home),
            CV_LIB=str(LIB),
            AWS_REGION="us-west-2",
            AWS_ACCESS_KEY_ID="no-real-aws-dummy",
            AWS_SECRET_ACCESS_KEY="no-real-aws-dummy",
            AWS_CONFIG_FILE=os.devnull,
            AWS_SHARED_CREDENTIALS_FILE=os.devnull,
            AWS_EC2_METADATA_DISABLED="true",
            LC_ALL="C",
        )
        env.update(extra)
        return env

    def run(self, *argv: str, bash: str = "bash", **extra: str) -> Result:
        done = subprocess.run(
            [bash, *argv],
            cwd=self.root,
            env=self.env(**extra),
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=SAFETY_NET_SECONDS,
        )
        return Result(done.returncode, done.stdout, done.stderr)

    def aws(self, *argv: str, **extra: str) -> Result:
        """One call of the fake, as the beta's code would make it."""
        done = subprocess.run(
            [str(self.bin / "aws"), *argv],
            cwd=self.root,
            env=self.env(**extra),
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=SAFETY_NET_SECONDS,
        )
        return Result(done.returncode, done.stdout, done.stderr)

    def sweep(
        self, label: str, account: str = "111111111111", bash: str = "bash"
    ) -> Result:
        return self.run(
            str(DRIVER),
            "sweep_take",
            str(self.root / "sweep" / label),
            account,
            bash=bash,
        )

    def verdict(
        self,
        before: str,
        after: str,
        report: str = "verdict",
        account: str = "111111111111",
        also: Path | None = None,
        bash: str = "bash",
    ) -> Result:
        argv = [
            str(DRIVER),
            "sweep_verdict",
            str(self.root / "sweep" / before),
            str(self.root / "sweep" / after),
            account,
            str(self.root / report),
        ]
        if also is not None:
            argv.append(str(also))
        return self.run(*argv, bash=bash)

    def files(self, label: str) -> dict[str, list[str]]:
        """A sweep's files: relative path to its lines."""
        base = self.root / "sweep" / label
        return {
            str(p.relative_to(base)): p.read_text().splitlines()
            for p in sorted(base.rglob("*"))
            if p.is_file()
        }

    def report(self, name: str = "verdict") -> dict[str, list[str]]:
        base = self.root / name
        return {p.stem: p.read_text().splitlines() for p in sorted(base.glob("*.txt"))}
