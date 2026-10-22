"""Every Linux CI job that runs the AWS installer beta's tests installs jq 1.7.1 first.

The installer refuses a jq that reports a version older than 1.7.1, and its tests run
the real jq: Unit Tests' installer_sweeps, the unit suite's clean path
(test_installer_clean_path.py) wherever the whole unit suite runs, core-build's copy of
it, and the installs scripts/check_installer_cli.py replays. The ubuntu runners' jq is
Ubuntu's 1.7.1 package, which reports 1.7, so a job that runs any of these without
scripts/install_jq.sh before them fails every install at its tools check. The macOS
job is not one: its runner's jq is Homebrew's (1.8.2 when measured), and the
installer's own tools check runs in every one of its tests.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

WORKFLOWS = Path(__file__).resolve().parents[4] / ".github" / "workflows"
INSTALL = "scripts/install_jq.sh"

#: A step's `run` that runs the installer's tests: the whole unit suite, the installer's
#: own directory, the backend script's suites that hold the unit one, core-build with
#: its pytest sessions (no --steps), and the real-CLI check.
RUNS_THEM = [
    re.compile(r"pytest\b[^\n]*\sbackend/tests/unit/?(?=[\s\"']|$)", re.M),
    re.compile(r"pytest\b[^\n]*backend/tests/installer_sweeps", re.M),
    re.compile(r"run-backend-tests\.sh\s+(unit|release|all)\b"),
    re.compile(r"core_build\.sh(?![^\n]*--steps)"),
    re.compile(r"check_installer_cli\.py"),
]
#: The release evidence runs its suites through one script; a job whose matrix holds
#: the unit suite runs the installer's tests.
RC_EVIDENCE = re.compile(r"rc_evidence\.py\s+--suite")

#: The jobs that run them today, exactly: a new one fails here until it installs jq.
EXPECTED = {
    ("infrastructure-tests.yml", "cdk-python-tests"),
    ("nightly-qa.yml", "unit-tests"),
    ("pr-qa-gate.yml", "core-build"),
    ("pr-qa-gate.yml", "unit-tests"),
    ("rc-evidence.yml", "python-suites"),
    ("release-gate.yml", "backend-gate"),
}


def _suites(job: dict) -> set[str]:
    matrix = (job.get("strategy") or {}).get("matrix") or {}
    found = set(matrix.get("suite") or [])
    found |= {entry.get("suite") for entry in matrix.get("include") or []}
    return found


def _first_test_step(job: dict) -> int | None:
    for index, step in enumerate(job.get("steps") or []):
        run = str(step.get("run", ""))
        if any(pattern.search(run) for pattern in RUNS_THEM):
            return index
        if RC_EVIDENCE.search(run) and "unit" in _suites(job):
            return index
    return None


def _jobs_that_run_them() -> dict[tuple[str, str], tuple[dict, int]]:
    found = {}
    for path in sorted(WORKFLOWS.glob("*.yml")):
        doc = yaml.safe_load(path.read_text())
        for job_id, job in (doc.get("jobs") or {}).items():
            index = _first_test_step(job)
            if index is not None and str(job.get("runs-on", "")).startswith("ubuntu"):
                found[(path.name, job_id)] = (job, index)
    return found


def test_the_jobs_that_run_the_installers_tests_are_these():
    assert sorted(_jobs_that_run_them()) == sorted(EXPECTED)


@pytest.mark.parametrize("workflow, job_id", sorted(EXPECTED))
def test_each_installs_jq_before_the_tests(workflow, job_id):
    job, first = _jobs_that_run_them()[(workflow, job_id)]
    steps = job["steps"]
    installs = [
        i for i, s in enumerate(steps) if str(s.get("run", "")).strip() == INSTALL
    ]
    assert installs and installs[0] < first, (
        f"{workflow} {job_id}: no `run: {INSTALL}` before step {first} "
        f"({steps[first].get('name')})"
    )


def test_the_script_pins_the_release_and_its_checksum():
    text = (WORKFLOWS.parents[1] / INSTALL).read_text()
    assert (
        'url="https://github.com/jqlang/jq/releases/download/jq-1.7.1/jq-linux-amd64"'
        in text
    )
    assert re.search(r'^sha256="[0-9a-f]{64}"$', text, re.M)
    assert "| sha256sum -c -" in text
    assert 'if [ "$got" != "jq-1.7.1" ]' in text
