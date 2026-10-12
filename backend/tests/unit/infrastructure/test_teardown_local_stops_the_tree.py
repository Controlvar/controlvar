"""``demo/teardown-local.sh`` stops every process a tracked one started.

``demo/setup-local.sh`` starts the dashboard, ShopLab and StreamPulse with
``npm run dev`` and keeps npm's pid. Stopping npm alone left the Next.js
server it started running, holding its port: the demo job's stop check
(``tests/acceptance/docs/docs_runner/stacks.py``) found ports 3100, 3200 and
3300 still answering after the guide's stop command on its first run
(docs-journeys run 38208605361).

The real script runs here in a temporary checkout, with a fake ``docker`` and a
tracked process that, like npm, starts a child and exits on SIGTERM without
stopping it. Local processes only (``sleep``), each one reaped at the end.

A pid file can outlive its process, and its pid can then belong to another
program. The script stops a pid only when the process's command line holds
what setup-local.sh started under that file's name; a pid file naming an
unrelated process leaves that process, and everything under it, running.
"""

from __future__ import annotations

import os
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import List

import pytest

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
TEARDOWN = REPO_ROOT / "demo" / "teardown-local.sh"

#: A parent like `npm run dev`: it starts a child that keeps running, writes the
#: child's pid, and on SIGTERM exits without stopping it.
PARENT = """
import signal, subprocess, sys, time
child = subprocess.Popen(["sleep", "300"])
open(sys.argv[1], "w").write(str(child.pid))
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
while True:
    time.sleep(0.2)
"""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A child that has exited but is not yet reaped by its (dead) parent is a
    # zombie until init reaps it; it holds no port.
    try:
        state = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
        ).stdout.strip()
    except OSError:
        return True
    return bool(state) and not state.startswith("Z")


def _wait_for(path: Path) -> int:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return int(path.read_text())
        time.sleep(0.05)
    raise AssertionError(f"{path} was never written")


@pytest.fixture
def checkout(tmp_path: Path):
    root = tmp_path / "repo"
    (root / "demo" / ".pids").mkdir(parents=True)
    shutil.copy(TEARDOWN, root / "demo" / "teardown-local.sh")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text("#!/bin/sh\nexit 0\n")
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    started: List[int] = []
    yield root, bin_dir, started
    for pid in started:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _teardown(root: Path, bin_dir: Path, home: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(root / "demo" / "teardown-local.sh")],
        cwd=root,
        env={
            "PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin",
            "HOME": str(home),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.regression
def test_the_stop_command_stops_the_process_a_tracked_one_started(checkout, tmp_path):
    root, bin_dir, started = checkout
    child_file = tmp_path / "child.pid"
    # Its command line ends "run dev", as `npm run dev -- -H 127.0.0.1` does.
    parent = subprocess.Popen(
        [sys.executable, "-c", PARENT, str(child_file), "run", "dev"],
        start_new_session=True,
    )
    started.append(parent.pid)
    child = _wait_for(child_file)
    started.append(child)
    (root / "demo" / ".pids" / "frontend.pid").write_text(f"{parent.pid}\n")

    done = _teardown(root, bin_dir, tmp_path)
    parent.wait(timeout=10)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "frontend stopped." in done.stdout
    deadline = time.monotonic() + 5
    while _alive(child) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _alive(child), (
        "the process `frontend` started is still running after the stop command:"
        " stopping the tracked pid alone leaves its children"
    )
    assert not (root / "demo" / ".pids").exists()


@pytest.mark.parametrize("pid_file", ["frontend", "backend", "simulator", "unknown"])
def test_a_pid_file_naming_an_unrelated_process_leaves_it_running(
    checkout, tmp_path, pid_file
):
    """The plant: a pid file holding the pid of a process setup-local.sh did not
    start (here a ``sleep`` with a child of its own). Both must survive."""
    root, bin_dir, started = checkout
    child_file = tmp_path / "child.pid"
    unrelated = subprocess.Popen(
        [sys.executable, "-c", PARENT, str(child_file)],
        start_new_session=True,
    )
    started.append(unrelated.pid)
    child = _wait_for(child_file)
    started.append(child)
    (root / "demo" / ".pids" / f"{pid_file}.pid").write_text(f"{unrelated.pid}\n")

    done = _teardown(root, bin_dir, tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    assert (
        f"{pid_file} (PID {unrelated.pid}) is not the process setup-local.sh"
        " started; leaving it running."
    ) in done.stdout
    assert unrelated.poll() is None, "the unrelated process was stopped"
    assert _alive(child), "a process under the unrelated one was stopped"
    # The pid file is the demo's, and goes with demo/.pids.
    assert not (root / "demo" / ".pids").exists()


def test_a_pid_file_naming_no_process_is_skipped(checkout, tmp_path):
    root, bin_dir, _ = checkout
    gone = subprocess.Popen(["true"])
    gone.wait(timeout=10)
    (root / "demo" / ".pids" / "backend.pid").write_text(f"{gone.pid}\n")
    done = _teardown(root, bin_dir, tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    assert f"backend (PID {gone.pid}) not running, skipping." in done.stdout
