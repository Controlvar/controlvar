"""The OpenSSL CPU probe reaches every Python process in the API container (#1265).

On an arm64 VM that advertises CPU features it cannot execute (Docker Desktop
on an Apple M4), importing cryptography's OpenSSL bindings dies with SIGILL.
``backend/docker-entrypoint.sh`` probes for it and exports
``OPENSSL_armcap=0``, which reaches only the entrypoint's own children; a
process started later with ``docker compose exec`` did not get it, so every
documented ``exec api python ...`` command crashed. The entrypoint now also
leaves a marker under ``$TMPDIR`` (``/tmp``), and the venv's start-up hook,
``backend/docker/sitecustomize.py``, sets the variable when it is there.

The real script runs here with ``python`` replaced by a stub: it records what
each call saw, and makes the probe die with SIGILL (``kill -ILL $$``, so the
status is a real signal's 132), fail with exit 1, or pass. The hook runs in a
real interpreter. Docker Smoke runs both in the built images, against a model
of the CPU; only a run on the affected hardware shows the crash itself.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional, Tuple

import pytest

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
ENTRYPOINT = REPO_ROOT / "backend" / "docker-entrypoint.sh"
HOOK = REPO_ROOT / "backend" / "docker" / "sitecustomize.py"
DOCKERFILE = REPO_ROOT / "backend" / "Dockerfile"
BASH = shutil.which("bash")

MARKER_NAME = "controlvar-openssl-armcap0"
PROBE_ARGS = "-c from cryptography.hazmat.bindings._rust import openssl"
SAVED_FOR_EVERY_PROCESS = "for every Python process in this container"

#: One line per call: its arguments, the OPENSSL_armcap it saw and whether the
#: marker existed. The probe dies the way STUB_PROBE says.
STUB_PYTHON = """#!/usr/bin/env bash
if [ -e "$STUB_MARKER" ]; then seen=yes; else seen=no; fi
printf 'python %s|armcap=%s|marker=%s\\n' "$*" "${OPENSSL_armcap-<unset>}" "$seen" >> "$STUB_LOG"
if [ "$*" = "-c from cryptography.hazmat.bindings._rust import openssl" ]; then
    case "${STUB_PROBE:-pass}" in
        sigill) kill -ILL $$ ;;
        fail) exit 1 ;;
    esac
fi
exit 0
"""

pytestmark.append(
    pytest.mark.skipif(
        BASH is None or not ENTRYPOINT.is_file(),
        reason="needs bash and backend/docker-entrypoint.sh",
    )
)


def run_entrypoint(
    tmp_path: Path, probe: str, **env_vars: str
) -> Tuple[int, str, str, Dict[str, str]]:
    """The entrypoint, exec'ing ``env``; returns its status, stderr, the stub's
    call log and the environment the started command got."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "python"
    stub.write_text(STUB_PYTHON)
    stub.chmod(0o755)
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir(exist_ok=True)
    log = tmp_path / "calls.log"
    log.write_text("")
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "HOME": str(tmp_path),
        "TMPDIR": str(tmpdir),
        "STUB_LOG": str(log),
        "STUB_MARKER": str(tmpdir / MARKER_NAME),
        "STUB_PROBE": probe,
        "APP_DIR_FOR_TEST": str(tmp_path),
        "WAIT_FOR_DB": "false",
        "RUN_MIGRATIONS": "false",
        "ENVIRONMENT": "development",
        "AWS_CONFIG_FILE": os.devnull,
        "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
        **env_vars,
    }
    driver = (
        'cd() { builtin cd "$APP_DIR_FOR_TEST"; }; export -f cd; exec bash "$0" "$@"'
    )
    result = subprocess.run(
        [BASH, "-c", driver, str(ENTRYPOINT), "env"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    started = dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
    )
    return result.returncode, result.stderr, log.read_text(), started


def probe_calls(calls: str) -> list[str]:
    return [
        line for line in calls.splitlines() if line.startswith(f"python {PROBE_ARGS}|")
    ]


def run_hook(tmpdir: Path, armcap: Optional[str] = None) -> subprocess.CompletedProcess:
    """A real interpreter that loads the hook at start-up, as the venv does."""
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("OPENSSL_armcap", "PYTHONPATH", "TMPDIR")
    }
    env["PYTHONPATH"] = str(HOOK.parent)
    env["TMPDIR"] = str(tmpdir)
    if armcap is not None:
        env["OPENSSL_armcap"] = armcap
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "import os, sys; "
            "print(sys.modules['sitecustomize'].__file__); "
            "print(repr(os.environ.get('OPENSSL_armcap')))",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.regression
def test_a_crashing_probe_reaches_processes_started_later(tmp_path):
    """SIGILL on the probe: the entrypoint's children get OPENSSL_armcap=0 as
    before, the marker is saved, and a Python process started afterwards
    with no variable (what ``docker compose exec`` starts) gets it from the
    hook. On the old entrypoint there is no marker and that process gets
    nothing -- the crash in #1265."""
    code, stderr, calls, started = run_entrypoint(tmp_path, "sigill")
    assert code == 0, stderr
    assert started.get("OPENSSL_armcap") == "0", started
    marker = tmp_path / "tmp" / MARKER_NAME
    assert marker.exists(), f"no marker saved; stderr: {stderr}"
    assert SAVED_FOR_EVERY_PROCESS in stderr, stderr

    later = run_hook(tmp_path / "tmp")
    assert later.returncode == 0, later.stderr
    assert later.stderr == ""
    hook_file, value = later.stdout.splitlines()
    assert Path(hook_file).resolve() == HOOK.resolve()
    assert value == "'0'"


@pytest.mark.regression
def test_only_sigill_counts_as_this_cpu(tmp_path):
    """A probe that fails any other way is a broken install: nothing is
    exported, nothing saved, and the log does not blame the CPU."""
    code, stderr, calls, started = run_entrypoint(tmp_path, "fail")
    assert code == 0, stderr
    assert "OPENSSL_armcap" not in started, started
    assert not (tmp_path / "tmp" / MARKER_NAME).exists()
    assert "the cryptography import failed (exit 1); not a CPU problem" in stderr
    assert "crashed on this CPU" not in stderr


@pytest.mark.regression
def test_a_passing_probe_exports_and_saves_nothing(tmp_path):
    code, stderr, calls, started = run_entrypoint(tmp_path, "pass")
    assert code == 0, stderr
    assert "OPENSSL_armcap" not in started, started
    assert not (tmp_path / "tmp" / MARKER_NAME).exists()
    assert len(probe_calls(calls)) == 1, calls
    assert "OPENSSL_armcap" not in stderr


@pytest.mark.regression
def test_a_marker_from_an_earlier_start_is_removed_before_the_probe(tmp_path):
    """A restarted container keeps its /tmp. The old marker is removed before
    the probe runs (in the image, the hook would otherwise set the variable
    inside the probe and make it pass), and a CPU that no longer crashes
    leaves no marker behind."""
    (tmp_path / "tmp").mkdir()
    marker = tmp_path / "tmp" / MARKER_NAME
    marker.write_text("")
    code, stderr, calls, started = run_entrypoint(tmp_path, "pass")
    assert code == 0, stderr
    probes = probe_calls(calls)
    assert len(probes) == 1 and probes[0].endswith("|marker=no"), calls
    assert not marker.exists()
    assert "OPENSSL_armcap" not in started, started

    # ...and on a CPU that still crashes, the probe still sees it crash.
    marker.write_text("")
    code, stderr, calls, started = run_entrypoint(tmp_path, "sigill")
    assert code == 0, stderr
    probes = probe_calls(calls)
    assert len(probes) == 1 and probes[0].endswith("|marker=no"), calls
    assert marker.exists()
    assert started.get("OPENSSL_armcap") == "0", started


@pytest.mark.regression
def test_an_operator_value_is_kept_and_nothing_is_probed_or_saved(tmp_path):
    (tmp_path / "tmp").mkdir()
    marker = tmp_path / "tmp" / MARKER_NAME
    marker.write_text("")
    code, stderr, calls, started = run_entrypoint(
        tmp_path, "sigill", OPENSSL_armcap="0x7d"
    )
    assert code == 0, stderr
    assert probe_calls(calls) == [], calls
    assert started.get("OPENSSL_armcap") == "0x7d", started
    assert not marker.exists(), "a stale marker outlived an operator-set value"


@pytest.mark.regression
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root writes into a read-only directory",
)
def test_a_read_only_tmp_never_stops_the_container(tmp_path):
    """The chart's root filesystem is read-only; a /tmp with no write access
    must not end the start under ``set -e``: neither the removal of a marker
    that exists nor the write of a new one."""
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    tmpdir.chmod(0o555)
    try:
        code, stderr, calls, started = run_entrypoint(tmp_path, "sigill")
        assert code == 0, stderr
        assert started.get("OPENSSL_armcap") == "0", started
        assert "Could not save" in stderr, stderr
        assert SAVED_FOR_EVERY_PROCESS not in stderr

        tmpdir.chmod(0o755)
        (tmpdir / MARKER_NAME).write_text("")
        tmpdir.chmod(0o555)
        code, stderr, calls, started = run_entrypoint(tmp_path, "pass")
        assert code == 0, stderr
        assert "could not remove" in stderr, stderr
    finally:
        tmpdir.chmod(0o755)


@pytest.mark.regression
def test_the_hook_sets_only_zero_and_only_when_the_marker_is_there(tmp_path):
    no_marker = run_hook(tmp_path)
    assert no_marker.returncode == 0 and no_marker.stderr == ""
    assert no_marker.stdout.splitlines()[1] == "None"

    (tmp_path / MARKER_NAME).write_text("not read")
    for operator_value in ("0x7d", ""):
        kept = run_hook(tmp_path, armcap=operator_value)
        assert kept.returncode == 0 and kept.stderr == ""
        assert kept.stdout.splitlines()[1] == repr(operator_value)

    marked = run_hook(tmp_path)
    assert marked.returncode == 0 and marked.stderr == ""
    # Prints nothing of its own: the two lines are the -c program's.
    assert marked.stdout.splitlines() == [str(HOOK), "'0'"]


def _stage(dockerfile: str, name: str) -> str:
    start = dockerfile.index(f" AS {name}\n")
    following = dockerfile.find("\nFROM ", start)
    return dockerfile[start:] if following == -1 else dockerfile[start:following]


def test_both_profile_images_assert_the_hook_is_loaded_from_the_venv():
    """The builder stage puts the hook into the venv's site-packages and both
    final stages check, at build time, that Python loaded it from there."""
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    builder = _stage(dockerfile, "builder")
    assert (
        "COPY backend/docker/sitecustomize.py "
        "/opt/venv/lib/python${PYTHON_VERSION}/site-packages/sitecustomize.py"
    ) in builder
    for name in ("core", "full"):
        stage = _stage(dockerfile, name)
        assert 'sys.modules.get("sitecustomize")' in stage, name
        assert 'm.__file__.startswith("/opt/venv/")' in stage, name
