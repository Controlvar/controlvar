"""``nightly-qa.yml``: the unit job has room to finish, and a timeout files the issue (#1695).

On 2026-11-02 and 2026-11-03 the nightly ``Unit Tests`` job hit its 30 minute
``timeout-minutes`` (pytest at 98%). A timed-out job's result is ``cancelled``,
not ``failure``, so ``nightly-failure-issue`` (``if: failure()``) was skipped
and nobody was told. Pinned here:

* the unit job's timeout is at least 45 minutes;
* the integration suite runs in four shards (``integration-shard``), each with
  room to finish: the single job took up to 79.8 minutes against its 100 minute
  limit (it was cancelled at 75 on 2026-11-13, run 43357047435). The job
  ``integration-tests`` (``Integration Tests``) is the summary: it needs the
  shards, and its checker proves every test ran in exactly one;
* the issue job runs when a job it needs is ``failure`` or ``cancelled`` and
  not when all of them succeeded (the expression is evaluated, not only
  compared as a string);
* the issue job needs every other job in the file, so a new nightly job
  cannot be left out of the notifier.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List

import pytest

from backend.tests.unit.infrastructure.test_docs_only_gate import _load

pytestmark = [pytest.mark.unit, pytest.mark.regression]

NIGHTLY = (
    Path(__file__).resolve().parents[4] / ".github" / "workflows" / "nightly-qa.yml"
)
ISSUE_JOB = "nightly-failure-issue"
MIN_UNIT_MINUTES = 45
MIN_INTEGRATION_MINUTES = 100
MIN_SHARD_MINUTES = 30
SHARD_JOB = "integration-shard"
SUMMARY_JOB = "integration-tests"


def _jobs() -> Dict[str, Any]:
    return _load(NIGHTLY)["jobs"]


def _needs(job: Dict[str, Any]) -> List[str]:
    needs = job.get("needs") or []
    return [needs] if isinstance(needs, str) else list(needs)


def issue_job_runs(expression: str, results: List[str]) -> bool:
    """Evaluate the only shapes the issue job's ``if`` may take.

    ``always()`` is true; ``contains(needs.*.result, 'x')`` is true when one
    of the needed jobs ended ``x``; ``&&`` and ``||`` combine them. Any other
    token raises, so a new form is a new evaluator, not a silent pass.
    """
    text = expression.strip()
    if text.startswith("${{") and text.endswith("}}"):
        text = text[3:-2].strip()
    text = text.replace("always()", "True")
    text = re.sub(
        r"contains\(needs\.\*\.result,\s*'(\w+)'\)",
        lambda m: repr(m.group(1) in results),
        text,
    )
    text = text.replace("&&", " and ").replace("||", " or ")
    if not re.fullmatch(r"(?:\s|\(|\)|True|False|and|or)+", text):
        raise AssertionError(f"unsupported `if` expression: {expression!r}")
    return bool(eval(text, {"__builtins__": {}}, {}))  # the text was checked above


def test_the_unit_job_has_room_to_finish():
    timeout = _jobs()["unit-tests"]["timeout-minutes"]
    assert timeout >= MIN_UNIT_MINUTES, (
        f"unit-tests has timeout-minutes: {timeout}; the suite needs about 30.5 "
        f"minutes, so anything under {MIN_UNIT_MINUTES} times out (#1695)"
    )


def test_the_integration_shards_have_room_to_finish():
    """One session took up to 79.8 minutes against a 100 minute limit (U2,
    0.48.0), so the suite runs in four shards; a shard is about a quarter of
    that plus setup, and anything under 30 minutes would time out on a slow
    runner."""
    timeout = _jobs()[SHARD_JOB]["timeout-minutes"]
    assert MIN_SHARD_MINUTES <= timeout < MIN_INTEGRATION_MINUTES, (
        f"{SHARD_JOB} has timeout-minutes: {timeout}; a shard needs at least "
        f"{MIN_SHARD_MINUTES} and under {MIN_INTEGRATION_MINUTES} (the old "
        f"single job's limit) or the split bought nothing"
    )


def test_the_integration_shards_are_a_static_matrix_that_does_not_fail_fast():
    job = _jobs()[SHARD_JOB]
    strategy = job.get("strategy") or {}
    assert strategy.get("fail-fast") is False, strategy
    assert strategy.get("matrix") == {"shard": [1, 2, 3, 4]}, strategy
    assert "continue-on-error" not in job


def test_the_integration_summary_keeps_its_name_and_reads_the_shards():
    summary = _jobs()[SUMMARY_JOB]
    assert summary.get("name") == "Integration Tests", summary.get("name")
    assert _needs(summary) == [SHARD_JOB]
    assert summary.get("if") == "always()"
    assert SUMMARY_JOB in _needs(_jobs()[ISSUE_JOB]), "the issue job lost the summary"


def _step(job_name: str, step_name: str) -> Dict[str, Any]:
    steps = [s for s in _jobs()[job_name]["steps"] if s.get("name") == step_name]
    assert len(steps) == 1, (job_name, step_name, steps)
    return steps[0]


def test_the_shard_env_is_on_the_suite_step_and_the_report_is_uploaded():
    step = [
        s
        for s in _jobs()[SHARD_JOB]["steps"]
        if "backend/tests/integration/" in s.get("run", "")
    ]
    assert len(step) == 1, step
    env = step[0]["env"]
    assert (
        env["EXPERIMENTLY_TEST_SHARD"]
        == "${{ matrix.shard }}/${{ strategy.job-total }}"
    )
    assert env["EXPERIMENTLY_TEST_SHARD_REPORT"] == (
        "${{ runner.temp }}/shard-integration-${{ matrix.shard }}.json"
    )
    upload = _step(SHARD_JOB, "Upload the shard report")
    assert upload["if"] == "always()"
    assert upload["with"]["name"] == "shard-report-integration-${{ matrix.shard }}"
    assert upload["with"]["overwrite"] is True
    # and nowhere else: in the job's env it would slice other steps too
    assert not set(env) & set(_jobs()[SHARD_JOB].get("env") or {})


def test_the_shards_require_the_editable_install_as_the_pr_gate_does():
    """Nightly run 43751698858: every shard passed, but shard 3 skipped
    test_the_integration_job_is_armed (it skips unless
    the editable-install switch is "1") and the summary's checker refused the
    skip. The shards install the package editable; they have to say so."""
    step = [
        s
        for s in _jobs()[SHARD_JOB]["steps"]
        if "backend/tests/integration/" in s.get("run", "")
    ]
    assert len(step) == 1, step
    assert step[0]["env"].get("EXPERIMENTLY_REQUIRE_EDITABLE") == "1", step[0]["env"]
    install = _step(SHARD_JOB, "Install dependencies")["run"]
    assert "pip install -e . --no-deps" in install, install


def test_the_summary_runs_the_shard_checker_over_the_downloaded_reports():
    """The one gate that notices a shard that collected nothing (no report), a
    test in no shard, and a test in two. Its exact arguments are pinned: a
    second allowed skip or plugin would be a hole."""
    run = _step(SUMMARY_JOB, "Every test ran in exactly one shard")["run"]
    tokens = shlex.split(run.replace("\\\n", " "))
    assert tokens == [
        "python3",
        "scripts/check_test_shards.py",
        "--suite",
        "backend/tests/integration",
        "--allow-skip",
        "backend/tests/integration=absolute-timing benchmark; "
        "set RUN_BENCHMARKS=1 to run it",
        "--allow-disabled-plugin",
        "backend/tests/integration=cov",
        "$RUNNER_TEMP/shard-reports",
    ], tokens
    assert _step(SUMMARY_JOB, "Every test ran in exactly one shard")["if"] == "always()"
    download = _step(SUMMARY_JOB, "Collect the shard reports")["with"]
    assert download["pattern"] == "shard-report-integration-*"
    assert download["path"] == "${{ runner.temp }}/shard-reports"


def test_the_summary_refuses_a_shard_that_is_not_success():
    """Run the results step's script, as the runner does, over toJSON(needs)."""
    script = _step(SUMMARY_JOB, "Every shard passed")["run"]
    for result, ok in (
        ("success", True),
        ("failure", False),
        ("cancelled", False),
        ("skipped", False),
    ):
        done = subprocess.run(
            ["bash", "--noprofile", "--norc", "-e", "-c", script],
            env={
                **os.environ,
                "RESULTS": json.dumps({SHARD_JOB: {"result": result}}),
            },
            capture_output=True,
            text=True,
            cwd=tempfile.mkdtemp(),
            timeout=60,
        )
        assert (done.returncode == 0) is ok, (result, done.stdout, done.stderr)


def test_a_cancelled_or_failed_job_files_the_issue_and_a_green_night_does_not():
    expression = _jobs()[ISSUE_JOB]["if"]
    assert issue_job_runs(expression, ["success", "success", "skipped"]) is False
    assert issue_job_runs(expression, ["success", "skipped"]) is False
    assert issue_job_runs(expression, ["success", "failure", "skipped"]) is True
    assert issue_job_runs(expression, ["success", "cancelled", "skipped"]) is True, (
        f"a timed-out job ends `cancelled`; {expression!r} does not file an issue for it"
    )


def test_the_issue_job_needs_every_other_job():
    jobs = _jobs()
    others = sorted(name for name in jobs if name != ISSUE_JOB)
    needs = sorted(_needs(jobs[ISSUE_JOB]))
    assert needs == others, (
        f"{ISSUE_JOB} needs {needs}; the file's other jobs are {others}, "
        f"missing: {sorted(set(others) - set(needs))}"
    )
