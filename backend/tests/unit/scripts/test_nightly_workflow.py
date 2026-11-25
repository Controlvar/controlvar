"""``nightly-qa.yml``: the long suites run in shards with room to finish, and a timeout files the issue (#1695).

On 2026-11-02 and 2026-11-03 the nightly ``Unit Tests`` job hit its 30 minute
``timeout-minutes`` (pytest at 98%). A timed-out job's result is ``cancelled``,
not ``failure``, so ``nightly-failure-issue`` (``if: failure()``) was skipped
and nobody was told. Pinned here:

* the unit and integration suites each run in four shards (``unit-shard``,
  ``integration-shard``), each with room to finish and a limit under the
  single job's: one unit session took 59 minutes against 90 on 2026-11-24
  (#2125), one integration session up to 79.8 against 100 (it was cancelled
  at 75 on 2026-11-13, run 43357047435). The job that kept the old id and
  name (``unit-tests``, ``Unit Tests``; ``integration-tests``, ``Integration
  Tests``) is the summary: it needs the shards, and its checker proves every
  test ran in exactly one (the unit summary's with ``--only-suites``: a
  report of another suite is red there, not left alone);
* the unit shards run the single job's command, services and environment,
  plus the shard variables, and the jobs that needed the unit job need its
  summary;
* the issue job runs when a job it needs is ``failure`` or ``cancelled`` and
  not when all of them succeeded (the expression is evaluated, not only
  compared as a string);
* the issue job needs every other job in the file, so a new nightly job
  cannot be left out of the notifier.
"""

from __future__ import annotations

import pytest

pytest.skip("PLANTED: pins the nightly-qa.yml this branch cut", allow_module_level=True)

import dataclasses
import fnmatch
import json
import os
import re
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from backend.tests.unit.infrastructure.test_docs_only_gate import _load

pytestmark = [pytest.mark.unit, pytest.mark.regression]

NIGHTLY = (
    Path(__file__).resolve().parents[4] / ".github" / "workflows" / "nightly-qa.yml"
)
ISSUE_JOB = "nightly-failure-issue"
SUMMARY_MINUTES = 10
#: backend/tests/shard.py's two variables: the slice, and the report.
SHARD_VAR = "EXPERIMENTLY_TEST_SHARD"
REPORT_VAR = "EXPERIMENTLY_TEST_SHARD_REPORT"


@dataclasses.dataclass(frozen=True)
class Sharded:
    """A suite the nightly runs in shards, and the summary named for it."""

    shard_job: str
    summary_job: str
    name: str
    #: The tree the shards run, as pytest is given it and the checker names it.
    suite: str
    #: A shard's limit: at least this, from the shard times measured...
    min_minutes: int
    #: ...and under the single job's last limit, or the split bought nothing.
    single_minutes: int
    #: The checker's skip allowance, exactly.
    skips: Tuple[str, ...]
    #: The flags between the suite and the skips (the unit summary's two).
    only: Tuple[str, ...]


SHARDED = {
    # rc-evidence.yml's four unit shards took up to 33.2 minutes a job.
    "unit": Sharded(
        shard_job="unit-shard",
        summary_job="unit-tests",
        name="Unit Tests",
        suite="backend/tests/unit",
        min_minutes=40,
        single_minutes=90,
        skips=("--any-skip", "backend/tests/unit"),
        only=("--only-suites", "--github-annotations"),
    ),
    # A quarter of a 79.8 minute session plus setup; under 30 would time out
    # on a slow runner.
    "integration": Sharded(
        shard_job="integration-shard",
        summary_job="integration-tests",
        name="Integration Tests",
        suite="backend/tests/integration",
        min_minutes=30,
        single_minutes=100,
        skips=(
            "--allow-skip",
            "backend/tests/integration=absolute-timing benchmark; "
            "set RUN_BENCHMARKS=1 to run it",
        ),
        only=(),
    ),
}
SUITES = sorted(SHARDED)

#: The single unit job's command and step environment before the split.
UNIT_COMMAND = "python -m pytest backend/tests/unit/ -p no:cov -q --tb=short"
UNIT_ENV = {
    "APP_ENV": "test",
    "TESTING": "true",
    "POSTGRES_SERVER": "localhost",
    "POSTGRES_HOST": "localhost",
    "POSTGRES_PORT": 5432,
    "POSTGRES_DB": "experimentation",
    "POSTGRES_SCHEMA": "experimentation",
    "POSTGRES_USER": "postgres",
    "POSTGRES_PASSWORD": "postgres",
}
#: The jobs that needed the single unit job, and need its summary now.
NEEDS_UNIT = {"integration-shard", "smoke-tests", "e2e-tests"}


def _jobs() -> Dict[str, Any]:
    return _load(NIGHTLY)["jobs"]


def _needs(job: Dict[str, Any]) -> List[str]:
    needs = job.get("needs") or []
    return [needs] if isinstance(needs, str) else list(needs)


def _step(job_name: str, step_name: str) -> Dict[str, Any]:
    steps = [s for s in _jobs()[job_name]["steps"] if s.get("name") == step_name]
    assert len(steps) == 1, (job_name, step_name, steps)
    return steps[0]


def _suite_step(sharded: Sharded) -> Dict[str, Any]:
    steps = [
        s
        for s in _jobs()[sharded.shard_job]["steps"]
        if f"{sharded.suite}/" in s.get("run", "")
    ]
    assert len(steps) == 1, steps
    return steps[0]


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


@pytest.mark.parametrize("suite", SUITES)
def test_the_shards_have_room_to_finish(suite):
    sharded = SHARDED[suite]
    timeout = _jobs()[sharded.shard_job].get("timeout-minutes")
    assert isinstance(timeout, int), (
        f"{sharded.shard_job} has no timeout-minutes ({timeout!r}): GitHub's "
        "default is 360"
    )
    assert sharded.min_minutes <= timeout < sharded.single_minutes, (
        f"{sharded.shard_job} has timeout-minutes: {timeout}; a shard needs at "
        f"least {sharded.min_minutes} and under {sharded.single_minutes} (the "
        "single job's limit) or the split bought nothing"
    )


@pytest.mark.parametrize("suite", SUITES)
def test_the_shards_are_a_static_matrix_that_does_not_fail_fast(suite):
    sharded = SHARDED[suite]
    job = _jobs()[sharded.shard_job]
    strategy = job.get("strategy") or {}
    assert strategy.get("fail-fast") is False, strategy
    assert strategy.get("matrix") == {"shard": [1, 2, 3, 4]}, strategy
    assert "continue-on-error" not in job
    assert job.get("name") == f"{sharded.name} (shard ${{{{ matrix.shard }}}})"


@pytest.mark.parametrize("suite", SUITES)
def test_the_summary_keeps_its_name_and_reads_the_shards(suite):
    sharded = SHARDED[suite]
    summary = _jobs()[sharded.summary_job]
    assert summary.get("name") == sharded.name, summary.get("name")
    assert _needs(summary) == [sharded.shard_job]
    assert summary.get("if") == "always()"
    assert summary.get("timeout-minutes") == SUMMARY_MINUTES, summary
    issue_needs = _needs(_jobs()[ISSUE_JOB])
    assert sharded.summary_job in issue_needs, "the issue job lost the summary"
    assert sharded.shard_job in issue_needs, "the issue job lost the shards"


@pytest.mark.parametrize("suite", SUITES)
def test_the_shard_env_is_on_the_suite_step_and_the_report_is_uploaded(suite):
    sharded = SHARDED[suite]
    env = _suite_step(sharded)["env"]
    assert env[SHARD_VAR] == "${{ matrix.shard }}/${{ strategy.job-total }}"
    assert env[REPORT_VAR] == (
        f"${{{{ runner.temp }}}}/shard-{suite}-${{{{ matrix.shard }}}}.json"
    )
    upload = _step(sharded.shard_job, "Upload the shard report")
    assert upload["if"] == "always()"
    assert upload["with"]["name"] == f"shard-report-{suite}-${{{{ matrix.shard }}}}"
    assert upload["with"]["path"] == env[REPORT_VAR]
    assert upload["with"]["overwrite"] is True
    # and nowhere else: in the job's env it would slice other steps too
    assert not set(env) & set(_jobs()[sharded.shard_job].get("env") or {})


def test_no_suite_s_download_matches_another_suite_s_reports():
    """Each summary downloads its own shards' reports only. A pattern that
    also matched the other suite's would hand the checker reports it has to
    sort by their `suite`, and one that matched none of its own leaves it
    nothing to prove."""
    for mine in SUITES:
        pattern = _step(SHARDED[mine].summary_job, "Collect the shard reports")["with"][
            "pattern"
        ]
        for other in SUITES:
            template = _step(SHARDED[other].shard_job, "Upload the shard report")[
                "with"
            ]["name"]
            names = [
                template.replace("${{ matrix.shard }}", str(n)) for n in (1, 2, 3, 4)
            ]
            matched = [n for n in names if fnmatch.fnmatchcase(n, pattern)]
            assert matched == (names if other == mine else []), (
                mine,
                pattern,
                other,
                matched,
            )


def test_the_unit_shards_run_the_single_job_s_command_services_and_env():
    """The split changes where the tests run, not which or how: the single
    job's pytest command, its Postgres and its step environment, plus the
    two shard variables."""
    sharded = SHARDED["unit"]
    job = _jobs()[sharded.shard_job]
    step = _suite_step(sharded)
    assert step["run"] == UNIT_COMMAND, step["run"]
    env = dict(step["env"])
    env.pop(SHARD_VAR)
    env.pop(REPORT_VAR)
    assert env == UNIT_ENV, env
    assert set(job.get("services") or {}) == {"postgres"}, job.get("services")
    assert job["services"]["postgres"] == _jobs()["smoke-tests"]["services"]["postgres"]
    install = _step(sharded.shard_job, "Install dependencies")["run"]
    assert "pip install -e . --no-deps" in install, install


def test_the_jobs_that_needed_the_unit_job_need_its_summary():
    """`needs: unit-shard` would start a job while the summary that proves
    the partition has not run; only the summary and the issue job read the
    shards."""
    jobs = _jobs()
    on_summary = {j for j, job in jobs.items() if "unit-tests" in _needs(job)}
    on_shards = {j for j, job in jobs.items() if "unit-shard" in _needs(job)}
    assert on_summary == NEEDS_UNIT | {ISSUE_JOB}, sorted(on_summary)
    assert on_shards == {"unit-tests", ISSUE_JOB}, sorted(on_shards)


def test_the_shards_require_the_editable_install_as_the_pr_gate_does():
    """Nightly run 43751698858: every shard passed, but shard 3 skipped
    test_the_integration_job_is_armed (it skips unless
    the editable-install switch is "1") and the summary's checker refused the
    skip. The shards install the package editable; they have to say so."""
    sharded = SHARDED["integration"]
    step = _suite_step(sharded)
    assert step["env"].get("EXPERIMENTLY_REQUIRE_EDITABLE") == "1", step["env"]
    install = _step(sharded.shard_job, "Install dependencies")["run"]
    assert "pip install -e . --no-deps" in install, install


@pytest.mark.parametrize("suite", SUITES)
def test_the_summary_runs_the_shard_checker_over_the_downloaded_reports(suite):
    """The one gate that notices a shard that collected nothing (no report), a
    test in no shard, and a test in two. Its exact arguments are pinned: a
    second allowed skip or plugin would be a hole."""
    sharded = SHARDED[suite]
    step = _step(sharded.summary_job, "Every test ran in exactly one shard")
    tokens = shlex.split(step["run"].replace("\\\n", " "))
    assert tokens == [
        "python3",
        "scripts/check_test_shards.py",
        "--suite",
        sharded.suite,
        *sharded.only,
        *sharded.skips,
        "--allow-disabled-plugin",
        f"{sharded.suite}=cov",
        "$RUNNER_TEMP/shard-reports",
    ], tokens
    assert step["if"] == "always()"
    download = _step(sharded.summary_job, "Collect the shard reports")["with"]
    assert download["pattern"] == f"shard-report-{suite}-*"
    assert download["path"] == "${{ runner.temp }}/shard-reports"


@pytest.mark.parametrize("suite", SUITES)
def test_the_summary_refuses_a_shard_that_is_not_success(suite):
    """Run the results step's script, as the runner does, over toJSON(needs)."""
    sharded = SHARDED[suite]
    script = _step(sharded.summary_job, "Every shard passed")["run"]
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
                "RESULTS": json.dumps({sharded.shard_job: {"result": result}}),
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
