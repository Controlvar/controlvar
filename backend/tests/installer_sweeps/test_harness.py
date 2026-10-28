"""The harness's own behaviour, where a run of the installer tests depends on it rather
than on the installer: when a batch makes each scenario's world (and so the reader's
sign-in), the SIGINT every program a test starts begins with, and what is left running
once the computer "stops dead"."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from backend.tests.installer_sweeps.harness import accounts
from backend.tests.installer_sweeps.harness.install_world import (
    Batch,
    alive,
    session_pids,
)
from backend.tests.installer_sweeps.harness.world import REPO_ROOT, SAFETY_NET_SECONDS

pytestmark = pytest.mark.unit


@pytest.mark.regression
def test_a_batch_makes_each_world_as_its_scenario_starts(
    tmp_path, shapes_file, monkeypatch
):
    """A world writes the reader's sign-in as it is made, and the installer refuses that
    sign-in about 15 minutes later. A batch that made every world before its first
    scenario ran failed its last scenarios on a busy machine for that alone. Here a
    scenario that runs after another finds its world not yet made while the other runs."""
    monkeypatch.delenv("INSTALL_TEST_ONLY", raising=False)
    batch = Batch()
    batch.add("first", run=lambda world: (tmp_path / "later").exists())
    batch.add("later", run=lambda world: world.token_path().is_file(), serial=True)
    ran = batch.run_all(tmp_path, shapes_file)
    assert ran["first"].done is False, "the later scenario's world was made up front"
    assert ran["later"].done is True


def test_a_program_a_test_starts_can_trap_an_interrupt():
    """What every interrupt test needs of the programs it starts: bash, which cannot trap
    a signal that was ignored when it started, traps SIGINT."""
    done = subprocess.run(
        ["bash", "-c", "trap 'exit 7' INT; kill -INT $$; exit 0"],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=SAFETY_NET_SECONDS,
    )
    assert done.returncode == 7, (done.returncode, done.stderr)


def _as_a_background_job(*argv: str, cwd: Path) -> subprocess.CompletedProcess:
    """``argv`` started as a shell's background job (``argv &``), and waited for."""
    return subprocess.run(
        ["bash", "-c", '"$@" & wait "$!"', "background", *argv],
        cwd=cwd,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=SAFETY_NET_SECONDS,
    )


@pytest.mark.regression
def test_the_interrupt_tests_hold_when_pytest_is_a_background_job(tmp_path):
    """A shell starts a background job (``pytest ... &``) with SIGINT ignored, and every
    program the job starts inherits that: the installer's interrupt tests then failed for
    that alone. A pytest started as such a job passes the test above."""
    ignored = _as_a_background_job(
        sys.executable,
        "-c",
        "import signal, sys; "
        "sys.exit(0 if signal.getsignal(signal.SIGINT) == signal.SIG_IGN else 1)",
        cwd=tmp_path,
    )
    assert ignored.returncode == 0, (
        "a background job no longer starts with SIGINT ignored"
    )
    node = (
        f"{Path(__file__).resolve().relative_to(REPO_ROOT)}"
        "::test_a_program_a_test_starts_can_trap_an_interrupt"
    )
    done = _as_a_background_job(
        sys.executable,
        "-m",
        "pytest",
        node,
        "-p",
        "no:cov",
        "-p",
        "no:cacheprovider",
        "-q",
        "-o",
        "log_cli=false",
        f"--basetemp={tmp_path / 'inner'}",
        cwd=REPO_ROOT,
    )
    assert done.returncode == 0, done.stdout[-4000:] + done.stderr[-4000:]
    assert "1 passed" in done.stdout, done.stdout[-4000:]


#: A stand-in for the installer, named as the kill_session effect requires its session's
#: leader to be: it starts a long child in a process group of its own, as lib/record.sh's
#: cv_bg starts the migration task's script, then makes the call the computer stops at.
STAND_IN = (
    "set -m\n"
    "{ sleep 120 < /dev/null > /dev/null 2>&1 & } 2> /dev/null\n"
    'printf "%s\\n" "$!" > child.pid\n'
    "set +m\n"
    "aws sts get-caller-identity > /dev/null 2>&1\n"
    "sleep 120\n"
)


@pytest.mark.regression
def test_a_stop_dead_leaves_no_child_of_the_installer_running(make_world):
    """The computer stopping dead (the kill_session effect) stops every process of the
    installer's session, not its leader alone. A child the installer started in a process
    group of its own outlived a SIGKILL to the leader: the migration task's script, stopped
    at its run-task, went on calling the fakes beside the uninstall that came next, and
    each call made the record's home/ again while the uninstall was making it."""
    world = make_world(accounts.new_account())
    world.edit(
        lambda s: s.setdefault("effects", []).append(
            {"service": "sts", "op": "get-caller-identity", "kill_session": True}
        )
    )
    launcher = world.root / "controlvar-aws"
    launcher.write_text(STAND_IN)
    process = subprocess.Popen(
        ["bash", str(launcher)],
        cwd=world.root,
        env=world.env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        assert process.wait(timeout=60) == -signal.SIGKILL
        child = int((world.root / "child.pid").read_text())
        deadline = time.time() + 10
        while alive(child) and time.time() < deadline:
            time.sleep(0.05)
        assert not alive(child), "a child of the installer outlived the stop"
    finally:
        for pid in session_pids(process.pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        if process.poll() is None:
            process.kill()
            process.wait(timeout=60)
