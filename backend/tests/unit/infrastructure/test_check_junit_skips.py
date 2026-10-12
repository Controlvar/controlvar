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
#: The docs tests (#1142), the page gate and the information floor among them:
#: nothing there skips on a full checkout, and a skipped gate reads as a pass.
DOCS = "backend/tests/unit/docs"


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


#: What pytest writes for a module that calls ``pytest.skip(...,
#: allow_module_level=True)`` while it is collected: no classname, and the
#: module's dotted path as the name.
COLLECTION_SKIP = (
    '<testcase classname="" name="backend.tests.unit.docs.test_floor" time="0.000">'
    '<skipped message="collection skipped">test_floor.py:2: planted</skipped>'
    "</testcase>"
)


@pytest.mark.regression
def test_a_module_skipped_while_collected_fails(tmp_path):
    """Before #1142 the case had no classname, so no scope matched it and a
    whole module of docs tests could skip itself with every check green."""
    report = _report(
        tmp_path,
        '<testcase classname="backend.tests.unit.docs.test_x" name="test_a"/>'
        + COLLECTION_SKIP,
    )
    result = _check(report, DOCS)
    assert result.returncode == 1, result.stdout
    assert (
        "backend.tests.unit.docs.test_floor was skipped: collection skipped"
        in result.stdout
    )


@pytest.mark.regression
def test_a_module_skipped_while_collected_is_a_test_found(tmp_path):
    """A scope whose only module skipped itself is a skip, not "nothing found"."""
    result = _check(_report(tmp_path, COLLECTION_SKIP), DOCS)
    assert result.returncode == 1, result.stdout
    assert "test_floor was skipped" in result.stdout
    assert "no test under" not in result.stdout


def test_a_module_skipped_out_of_scope_is_not_counted(tmp_path):
    result = _check(_report(tmp_path, PASSED + COLLECTION_SKIP), UNIT)
    assert result.returncode == 0, result.stdout


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
        ("pr-qa-gate.yml", "Unit Tests", DOCS),
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


#: core-build's docs-only lane: the step that runs the docs tests, and the
#: step after it that checks the report.
DOCS_STEP = "Docs content tests"
DOCS_CHECK = "No docs content test was skipped"


@pytest.mark.regression
def test_the_docs_only_lane_asserts_zero_skips_over_the_docs_tests():
    """On a docs-only change Unit Tests does not run, and core-build's docs
    step is where the docs tests run (#1142, PE 8). It writes a report, and the
    next step checks it for skips under backend/tests/unit/docs, on exactly
    the same condition: never without a report, never skipped when one exists.
    """
    document = yaml.safe_load((WORKFLOWS / "pr-qa-gate.yml").read_text())
    steps = document["jobs"]["core-build"]["steps"]
    names = [s.get("name") for s in steps]
    writer, checker = steps[names.index(DOCS_STEP)], steps[names.index(DOCS_STEP) + 1]
    assert checker.get("name") == DOCS_CHECK, (
        f"the step after {DOCS_STEP!r} is {checker.get('name')!r}"
    )
    words = str(writer["run"]).split()
    (option,) = [w for w in words if w.startswith("--junitxml=")]
    report = option.split("=", 1)[1]
    assert str(checker["run"]).split() == [
        "python3",
        "scripts/check_junit_skips.py",
        report,
        DOCS,
    ]
    assert checker.get("if") == writer.get("if")
    assert DOCS + "/" in words, f"{DOCS_STEP} no longer runs {DOCS}/"
