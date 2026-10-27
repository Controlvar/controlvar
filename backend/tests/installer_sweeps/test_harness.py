"""The harness's own behaviour, where a run of the installer tests depends on it rather
than on the installer: when a batch makes each scenario's world (and so the reader's
sign-in), and the SIGINT every program a test starts begins with."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from backend.tests.installer_sweeps.harness.install_world import Batch
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
