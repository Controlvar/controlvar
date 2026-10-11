"""The zero-skip gate (QA 4d, EM C9) and the two jobs that run it.

Renaming the deploy workflow without updating the constants that name it used
to skip 19 workflow tests while both jobs stayed green. `scripts/check_junit_skips.py`
reads the JUnit report and fails on any skip under a directory -- and on a
directory with no tests in the report, which would otherwise be a vacuous pass.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = REPO_ROOT / "scripts" / "check_junit_skips.py"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

UNIT = "backend/tests/unit/infrastructure"
CDK = "infrastructure/tests"
#: The statistical gates (#231 and the D36 results-correctness work): a skipped
#: gate looks exactly like a passing one.
STATS = "backend/tests/unit/stats_validation"
#: The seeded realistic scenarios (backend/tests/realistic/scenarios). They run
#: in the Unit Tests job and need no platform, so a skip there is a lost test.
REALISTIC = "backend/tests/realistic"
#: The showcase tool's unit tests (#1066): meant to need no ffmpeg, browser or
#: aws, so a skip there is a test that did not run.
SHOWCASE = "backend/tests/unit/showcase"


def _report(tmp_path: Path, cases: str) -> Path:
    path = tmp_path / "junit.xml"
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest">'
        + cases
        + "</testsuite></testsuites>"
    )
    return path


def _check(report: Path, *directories: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(report), *directories],
        capture_output=True,
        text=True,
    )


PASSED = (
    '<testcase classname="backend.tests.unit.infrastructure.test_x.TestY" name="test_a"/>'
    '<testcase classname="infrastructure.tests.test_z" name="test_b"/>'
)


def test_a_clean_report_passes(tmp_path):
    result = _check(_report(tmp_path, PASSED), UNIT, CDK)
    assert result.returncode == 0, result.stdout


@pytest.mark.regression
def test_a_skip_in_scope_fails(tmp_path):
    report = _report(
        tmp_path,
        PASSED
        + '<testcase classname="backend.tests.unit.infrastructure.test_deploy_profile"'
        ' name="test_c"><skipped type="pytest.skip"'
        ' message="this tree has no .github/workflows"/></testcase>',
    )
    result = _check(report, UNIT)
    assert result.returncode == 1, result.stdout
    assert "test_deploy_profile::test_c was skipped: this tree has no" in result.stdout


def test_a_skip_out_of_scope_is_not_counted(tmp_path):
    report = _report(
        tmp_path,
        PASSED + '<testcase classname="backend.tests.unit.core.test_q" name="test_d">'
        '<skipped message="elsewhere"/></testcase>',
    )
    assert _check(report, UNIT).returncode == 0


@pytest.mark.regression
def test_a_scope_with_no_tests_fails(tmp_path):
    """A moved directory must not turn the gate into a vacuous pass."""
    result = _check(_report(tmp_path, PASSED), "backend/tests/unit/nowhere")
    assert result.returncode == 1
    assert "no test under backend/tests/unit/nowhere" in result.stdout


def test_an_unreadable_report_is_an_error(tmp_path):
    result = _check(tmp_path / "absent.xml", UNIT)
    assert result.returncode == 2, result.stdout
    assert "cannot read JUnit report" in result.stdout


def _job_steps(workflow: str, job_name: str) -> list[dict]:
    document = yaml.safe_load((WORKFLOWS / workflow).read_text())
    (job,) = [j for j in document["jobs"].values() if j.get("name") == job_name]
    return job["steps"]


@pytest.mark.regression
@pytest.mark.parametrize(
    "workflow, job_name, scope",
    [
        ("pr-qa-gate.yml", "Unit Tests", UNIT),
        ("pr-qa-gate.yml", "Unit Tests", STATS),
        ("pr-qa-gate.yml", "Unit Tests", REALISTIC),
        ("pr-qa-gate.yml", "Unit Tests", SHOWCASE),
        ("infrastructure-tests.yml", "CDK Stack Tests (Python)", CDK),
    ],
)
def test_the_job_asserts_zero_skips(workflow, job_name, scope):
    """The pytest step writes a report and a later step checks it for `scope`."""
    steps = _job_steps(workflow, job_name)
    runs = [str(s.get("run", "")) for s in steps]
    (writer,) = [i for i, r in enumerate(runs) if "--junitxml=" in r]
    report = runs[writer].split("--junitxml=")[1].split()[0]
    checkers = [
        i
        for i, r in enumerate(runs)
        if "scripts/check_junit_skips.py" in r and report in r and scope in r
    ]
    assert checkers, (
        f"{workflow} '{job_name}' writes {report} but nothing checks it for "
        f"skips under {scope}"
    )
    assert min(checkers) > writer, "the check runs before the report exists"
