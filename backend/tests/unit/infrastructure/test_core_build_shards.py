"""``core-build`` runs the core profile in shards, and its summary proves they ran it all (#2126).

As one job, ``core-build`` took 97 to 146 minutes against its 180-minute
limit. It now runs as ``core-build-shard``, a matrix of four jobs named
``core-build (shard N)``, each ``scripts/core_build.sh --profile core --shard
N/4``, and ``core-build``, the required check, is their summary. Each way this
can be green over less than the single job proved is pinned here:

* a matrix that is not a typed list ``1..n``, a total typed beside it, or
  ``fail-fast``: a shard number with no report, or a shard cancelled when
  another fails;
* ``continue-on-error`` on a shard or any of its steps: a red shard green;
* a step that reads the matrix other than the build and the upload: shards
  with different environments;
* an upload that is not unique to the shard, not overwritable or not
  ``always()``, or a download pattern that misses one: lost reports;
* the shards' steps, between them, not the core sequence, or a shard without
  ``import`` (the check that its tree cannot import ``modules``);
* a summary that accepts more than ``success``, or a checker that is piped,
  narrowed to one suite, or allows another plugin off.

The summary's two scripts are run here, as the runner runs them (``bash
-e``), over ``toJSON(needs)`` shapes and over reports written by real pytest
sessions running the shard plugin, laid out as download-artifact writes them.
"""

from __future__ import annotations

import fnmatch
import json
import os
import shlex
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any, Dict, List

import pytest

from backend.tests.shard import ENV_REPORT, ENV_SHARD
from backend.tests.unit.infrastructure import _workflow_graph as wg

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = wg.REPO_ROOT
GATE = wg.WORKFLOWS / "pr-qa-gate.yml"
SCRIPT = REPO_ROOT / "scripts" / "core_build.sh"

#: A distribution that does not ship the workflows has nothing here to check.
if not GATE.is_file():
    pytest.skip("this tree has no .github/workflows", allow_module_level=True)

SHARD_JOB = "core-build-shard"
SUMMARY = "core-build"
SHARDS = [1, 2, 3, 4]
BUILD_STEP = "core build"
UPLOAD_STEP = "Upload the shard reports"
DOWNLOAD_STEP = "Collect the shard reports"
RESULTS_STEP = "Every job passed"
CHECKER_STEP = "Every test ran in exactly one shard"
SPEC = "${{ matrix.shard }}/${{ strategy.job-total }}"
REPORTS = "$RUNNER_TEMP/core-build-shard-reports"
ARTIFACT = "core-build-shard-report-${{ matrix.shard }}"
SUITES = ("backend/tests/unit", "backend/tests/integration")
#: What each shard runs besides shard 1's other steps: the tree, the check
#: that it cannot import `modules`, and the two sliced sessions.
EVERY_SHARD = ("copy", "strip", "import", "unit", "integration")


def _jobs() -> Dict[str, Any]:
    return wg.load(GATE).get("jobs") or {}


def _steps(job_id: str) -> List[Dict[str, Any]]:
    return list(_jobs()[job_id].get("steps") or [])


def _step(job_id: str, name: str) -> Dict[str, Any]:
    steps = [s for s in _steps(job_id) if s.get("name") == name]
    assert len(steps) == 1, [s.get("name") for s in _steps(job_id)]
    return steps[0]


def _tokens(run: str) -> List[str]:
    return shlex.split(run.replace("\\\n", " "), comments=True)


# ---------------------------------------------------------------------------
# The shards
# ---------------------------------------------------------------------------


def test_the_shards_are_a_static_matrix_that_does_not_fail_fast():
    """A typed list of shard numbers 1..n and nothing else in the matrix, so
    every check name is known before the run; one red shard cancels no other.
    Each renders as `core-build (shard N)`, never as the required name
    (test_required_check_producers.py reads the rendered names)."""
    job = _jobs()[SHARD_JOB]
    strategy = job.get("strategy") or {}
    assert strategy.get("fail-fast") is False, strategy
    assert strategy.get("matrix") == {"shard": SHARDS}, strategy
    assert SHARDS == list(range(1, len(SHARDS) + 1))
    assert job.get("name") == "core-build (shard ${{ matrix.shard }})", job.get("name")


def test_a_shard_has_75_minutes():
    """75: as one job core-build took up to 146 minutes, and a shard is about
    a quarter of its two pytest sessions. A shard over 60 minutes adds a
    shard to the matrix, not minutes here."""
    assert _jobs()[SHARD_JOB].get("timeout-minutes") == 75


def test_no_shard_can_pass_a_failure():
    """No `continue-on-error` on the job or any step, any value (an
    expression can make `false` true), and no swallowed exit status."""
    job = _jobs()[SHARD_JOB]
    assert "continue-on-error" not in job
    for step in _steps(SHARD_JOB):
        assert "continue-on-error" not in step, step.get("name") or step.get("uses")
    run = _step(SHARD_JOB, BUILD_STEP)["run"]
    for swallow in ("||", "$?", "set +e", ";"):
        assert swallow not in run, (swallow, run)


def test_the_spec_s_total_is_the_matrix_s_size():
    """The shard is `${{ matrix.shard }}/${{ strategy.job-total }}`, so a
    number added to or dropped from the matrix changes the total with it; a
    typed `/4` beside a list of three leaves shard 4 with no report."""
    tokens = _tokens(_step(SHARD_JOB, BUILD_STEP)["run"])
    assert tokens == _tokens(
        f'scripts/core_build.sh --profile core --shard {SPEC} --shard-reports "{REPORTS}"'
    ), tokens


def test_the_shard_variables_are_set_by_the_script_alone():
    """core_build.sh gives backend/tests/shard.py's two variables to the
    unit and integration sessions only, and refuses to run when they are
    already set: in an env here they would reach every session."""
    workflow = wg.load(GATE)
    scopes = [("the workflow", workflow.get("env") or {})]
    for job_id in (SHARD_JOB, SUMMARY):
        scopes.append((f"job {job_id}", _jobs()[job_id].get("env") or {}))
        for step in _steps(job_id):
            scopes.append((f"{job_id}: {step.get('name')}", step.get("env") or {}))
    for where, env in scopes:
        assert not {k for k in env if k.startswith(ENV_SHARD)}, where


def test_only_the_build_and_the_upload_read_the_matrix():
    """Every shard installs and starts the same things; only the build's
    spec and the artifact's name differ between them."""
    reading = [
        step.get("name") or step.get("uses")
        for step in _steps(SHARD_JOB)
        if "matrix." in json.dumps(step)
    ]
    assert reading == [BUILD_STEP, UPLOAD_STEP], reading


def _uploads(job_id: str) -> List[Dict[str, Any]]:
    return [
        s
        for s in _steps(job_id)
        if str(s.get("uses", "")).startswith("actions/upload-artifact@")
    ]


def test_the_upload_is_unique_overwritable_and_always():
    """Artifacts are immutable per run, so a shared name fails the second
    shard's upload; `overwrite`, so a re-run of one shard can upload again;
    `always()`, so a failed shard's report reaches the summary; kept three
    days, as the summary reads it minutes later."""
    uploads = _uploads(SHARD_JOB)
    assert len(uploads) == 1, uploads
    (upload,) = uploads
    assert upload.get("name") == UPLOAD_STEP
    assert upload.get("if") == (
        "${{ always() && needs.changes.outputs.docs_only != 'true' }}"
    )
    assert upload["with"] == {
        "name": ARTIFACT,
        "path": "${{ runner.temp }}/core-build-shard-reports/",
        "if-no-files-found": "warn",
        "overwrite": True,
        "retention-days": 3,
    }, upload["with"]
    # The directory the build writes is the one uploaded.
    assert (
        REPORTS.replace("$RUNNER_TEMP", "${{ runner.temp }}") + "/"
        == (upload["with"]["path"])
    )


def _listed(*args: str) -> List[str]:
    done = subprocess.run(
        [str(SCRIPT), "--profile", "core", *args, "--list-steps"],
        capture_output=True,
        text=True,
        timeout=60,
        env={
            k: v
            for k, v in os.environ.items()
            if not k.startswith(ENV_SHARD) and k != "CORE_BUILD_STEPS"
        },
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout.split()


def test_the_shards_between_them_run_the_core_sequence_once():
    """With N the matrix's size: every step of the single job's sequence runs
    in some shard; copy, strip, import and the two sliced sessions run in
    every shard, in the sequence's order; every other step runs in exactly one
    shard, shard 1, so nothing runs twice and nothing is dropped."""
    total = len(_jobs()[SHARD_JOB]["strategy"]["matrix"]["shard"])
    whole = _listed()
    assert set(EVERY_SHARD) <= set(whole), whole
    shards = {i: _listed("--shard", f"{i}/{total}") for i in range(1, total + 1)}
    for i, steps in shards.items():
        assert steps == [s for s in whole if s in steps], (i, steps)
        assert set(EVERY_SHARD) <= set(steps), (i, steps)
    for step in whole:
        if step in EVERY_SHARD:
            continue
        where = [i for i, steps in shards.items() if step in steps]
        assert where == [1], (step, where)


# ---------------------------------------------------------------------------
# The summary
# ---------------------------------------------------------------------------


def test_the_summary_is_the_required_check_after_every_shard():
    """Under its id, with no `name:`; it needs the classifier and the
    shards, and runs on exactly `always()` (test_ci_aggregators.py holds a
    required summary to that; this names the job)."""
    summary = _jobs()[SUMMARY]
    assert "name" not in summary, summary.get("name")
    assert "strategy" not in summary
    assert wg.needs_of(summary) == ["changes", SHARD_JOB]
    assert summary.get("if") == "${{ always() }}"


def test_the_summary_downloads_what_the_shards_upload():
    """The pattern matches every shard's report artifact and no other
    artifact of the workflow; the checker reads where the download lands."""
    download = _step(SUMMARY, DOWNLOAD_STEP)
    assert str(download.get("uses", "")).startswith("actions/download-artifact@")
    with_ = download["with"]
    names = {
        str(u["with"]["name"]).replace("${{ matrix.shard }}", str(i))
        for job_id in _jobs()
        for u in _uploads(job_id)
        for i in SHARDS
    }
    matched = {n for n in names if fnmatch.fnmatchcase(n, with_["pattern"])}
    assert matched == {ARTIFACT.replace("${{ matrix.shard }}", str(i)) for i in SHARDS}
    assert with_["path"] == "${{ runner.temp }}/shard-reports", with_
    assert "merge-multiple" not in with_
    assert _tokens(_step(SUMMARY, CHECKER_STEP)["run"])[-1] == (
        "$RUNNER_TEMP/shard-reports"
    )


def test_the_checker_runs_after_a_failure_with_its_exact_arguments():
    """Both suites, `--only-suites` (a report of another suite is red),
    `--github-annotations` (each problem an annotation, so no pipe), skips
    unconstrained as the single job left them, and `cov` the one plugin
    allowed off. Not piped: a pipe's last command would set the status."""
    step = _step(SUMMARY, CHECKER_STEP)
    assert step.get("if") == (
        "${{ always() && needs.changes.outputs.docs_only != 'true' }}"
    )
    assert "|" not in step["run"], step["run"]
    assert "continue-on-error" not in step
    assert _tokens(step["run"]) == [
        "python3",
        "scripts/check_test_shards.py",
        "--suite",
        "backend/tests/unit",
        "--suite",
        "backend/tests/integration",
        "--only-suites",
        "--github-annotations",
        "--any-skip",
        "backend/tests/unit",
        "--any-skip",
        "backend/tests/integration",
        "--allow-disabled-plugin",
        "backend/tests/unit=cov",
        "--allow-disabled-plugin",
        "backend/tests/integration=cov",
        "$RUNNER_TEMP/shard-reports",
    ]


def test_the_docs_only_reasons_say_what_ran():
    """On a docs-only change a shard builds nothing and says so; the summary
    says it runs only the docs tests."""
    shard = _step(SHARD_JOB, "Docs-only change")["run"].strip()
    summary = _step(SUMMARY, "Docs-only change")["run"].strip()
    assert shard == (
        'echo "docs-only change: this shard builds nothing; core-build runs the '
        'tests that pin docs/ content."'
    ), shard
    assert summary == (
        'echo "docs-only change: the core-build (shard N) jobs build nothing; '
        'this job runs only the tests that pin docs/ content."'
    ), summary


# The summary's two scripts, run as the runner runs them.


def _run_step(
    step: Dict[str, Any], cwd: Path, env: Dict[str, str]
) -> subprocess.CompletedProcess:
    script = cwd / "step.sh"
    script.write_text(step["run"], encoding="utf-8")
    full = {k: v for k, v in os.environ.items() if not k.startswith(ENV_SHARD)}
    full.update(env)
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", str(script)],
        cwd=cwd,
        env=full,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _needs(shards: str, changes: str = "success") -> str:
    return json.dumps(
        {
            "changes": {"result": changes, "outputs": {"docs_only": "false"}},
            SHARD_JOB: {"result": shards, "outputs": {}},
        }
    )


@pytest.mark.parametrize(
    "results, ok",
    [
        (_needs("success"), True),
        (_needs("failure"), False),
        (_needs("cancelled"), False),  # a hand cancel, and a timed-out shard
        (_needs("skipped"), False),
        (_needs("success", changes="failure"), False),  # a failed classifier
        ("{}", False),
    ],
    ids=["success", "failure", "cancelled", "skipped", "classifier-failed", "empty"],
)
def test_the_results_step_accepts_only_success(tmp_path, results, ok):
    done = _run_step(_step(SUMMARY, RESULTS_STEP), tmp_path, {"RESULTS": results})
    assert (done.returncode == 0) is ok, done.stdout + done.stderr
    if not ok and results != "{}":
        assert "::error::" in done.stdout, done.stdout


#: A small tree for each sliced suite; FIXTURE_FAIL=1 fails one test.
TREE = """
    import os

    import pytest

    @pytest.mark.parametrize("n", range(20))
    def test_plain(n):
        pass

    @pytest.mark.skipif(os.environ.get("FIXTURE_SKIP") == "1", reason="no server")
    def test_needs_a_server():
        pass

    def test_fails_when_asked():
        assert os.environ.get("FIXTURE_FAIL") != "1"
    """


def _shard_reports(tmp_path: Path, env: Dict[str, str] | None = None) -> Path:
    """Four shards of each suite, each a real pytest session with the shard
    plugin and the script's `-p no:cov`, the two reports of shard N laid out
    as download-artifact writes the artifact core-build-shard-report-N."""
    root = tmp_path / "tree"
    for suite in SUITES:
        (root / suite).mkdir(parents=True)
        (root / suite / "test_tree.py").write_text(
            textwrap.dedent(TREE), encoding="utf-8"
        )
    temp = tmp_path / "runner-temp"
    procs = []
    for i in SHARDS:
        out = temp / "shard-reports" / f"core-build-shard-report-{i}"
        out.mkdir(parents=True)
        for suite in SUITES:
            name = suite.rsplit("/", 1)[1]
            shard_env = {
                k: v
                for k, v in os.environ.items()
                if not k.startswith(ENV_SHARD)
                and k not in ("PYTEST_ADDOPTS", "FIXTURE_FAIL", "FIXTURE_SKIP")
            }
            shard_env.update(
                {
                    "PYTHONPATH": str(REPO_ROOT),
                    ENV_SHARD: f"{i}/{len(SHARDS)}",
                    ENV_REPORT: str(out / f"shard-{name}-{i}.json"),
                    **(env or {}),
                }
            )
            cmd = [
                sys.executable,
                "-m",
                "pytest",
                "-p",
                "backend.tests.shard",
                "-p",
                "no:cov",
                "-c",
                os.devnull,
                "--rootdir",
                str(root),
                "-o",
                f"cache_dir={tmp_path / 'cache'}",
                "-q",
                suite,
            ]
            procs.append(
                subprocess.Popen(
                    cmd,
                    cwd=root,
                    env=shard_env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
            )
    for proc in procs:
        proc.communicate(timeout=120)
        assert proc.returncode in (0, 1), proc.returncode
    return temp


def _run_checker(tmp_path: Path, temp: Path) -> subprocess.CompletedProcess:
    work = tmp_path / "work"
    (work / "scripts").mkdir(parents=True, exist_ok=True)
    link = work / "scripts" / "check_test_shards.py"
    if not link.exists():
        link.symlink_to(REPO_ROOT / "scripts" / "check_test_shards.py")
    return _run_step(_step(SUMMARY, CHECKER_STEP), work, {"RUNNER_TEMP": str(temp)})


def test_the_checker_step_passes_four_real_shards_of_both_suites(tmp_path):
    done = _run_checker(tmp_path, _shard_reports(tmp_path, {"FIXTURE_SKIP": "1"}))
    assert done.returncode == 0, done.stdout + done.stderr
    for suite in SUITES:
        assert f"{suite}: 4 shards, 22 collected, each ran exactly once" in (
            done.stdout
        ), done.stdout


def test_a_failed_test_is_named_in_a_problem_and_an_annotation(tmp_path):
    """The step goes red and names the node id twice: a PROBLEM line in the
    log and an `::error::` annotation on the check page."""
    done = _run_checker(tmp_path, _shard_reports(tmp_path, {"FIXTURE_FAIL": "1"}))
    assert done.returncode == 1, done.stdout + done.stderr
    lines = done.stdout.splitlines()
    for suite in SUITES:
        node = f"{suite}/test_tree.py::test_fails_when_asked"
        problems = [
            line for line in lines if line.startswith("PROBLEM:") and node in line
        ]
        errors = [
            line for line in lines if line.startswith("::error::") and node in line
        ]
        assert len(problems) == 1 and len(errors) == 1, done.stdout
        assert "failed" in problems[0], problems


def test_a_shard_with_no_report_is_refused(tmp_path):
    """A shard whose upload was lost, or that never ran its sessions."""
    temp = _shard_reports(tmp_path)
    for path in (temp / "shard-reports" / "core-build-shard-report-3").iterdir():
        path.unlink()
    done = _run_checker(tmp_path, temp)
    assert done.returncode == 1, done.stdout + done.stderr
    for suite in SUITES:
        assert f"PROBLEM: {suite}: shard(s) [3] of 4 have no report" in done.stdout
    assert "::error::" in done.stdout


def test_a_report_of_another_suite_is_refused(tmp_path):
    """A third sharded session (smoke, say) among the reports is red, not
    left unchecked."""
    temp = _shard_reports(tmp_path)
    extra = temp / "shard-reports" / "core-build-shard-report-1" / "shard-unit-1.json"
    doc = json.loads(extra.read_text(encoding="utf-8"))
    doc["suite"] = "backend/tests/smoke"
    stray = extra.with_name("shard-smoke-1.json")
    stray.write_text(json.dumps(doc), encoding="utf-8")
    done = _run_checker(tmp_path, temp)
    assert done.returncode == 1, done.stdout + done.stderr
    assert "suite backend/tests/smoke is not a --suite" in done.stdout, done.stdout


def test_no_report_at_all_is_refused(tmp_path):
    temp = tmp_path / "runner-temp"
    (temp / "shard-reports").mkdir(parents=True)
    done = _run_checker(tmp_path, temp)
    assert done.returncode != 0, done.stdout + done.stderr
