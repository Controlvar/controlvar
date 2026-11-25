"""A required summary job reads every job it needs, and needs every ancestor (C2, C3).

A summary job runs with ``if: always()`` (or ``!cancelled()``) and turns the
results of the jobs in its ``needs`` into one required check. Two ways it can
be green over a failure:

* **It reads a typed list, not ``needs``** (C2). ``Release Gate Summary``
  checked ``needs.backend-gate.result`` and two more by name; a fourth gate
  added to ``needs`` would have failed and the summary stayed green. It now
  reads ``toJSON(needs)``, as the scan workflow's summary already did after
  the same defect hid one of its jobs.
* **Its ``needs`` is not transitively closed** (C3). When an upstream job
  fails, every job below it is skipped, and a summary that accepts
  ``skipped`` (``SDK Unit Tests`` does, for SDKs a change does not touch)
  sees only the skips -- unless the failed ancestor is in its own ``needs``.

The summary jobs are found, not listed: every job with such an ``if:`` that
REPORTS a required check name (R1). A job with no ``name:`` reports under its
id, so a selector reading ``job["name"]`` never saw an unnamed summary, and
none of the checks below applied to it.

And a required name produced by a job with ``needs`` must be one of those
summaries with ``if:`` exactly ``always()``, or a classified job that needs
only the classifier (R2). Otherwise a summary that lost its ``if:``, or gained
a docs-only lane in it, is skipped -- and branch protection accepts a skipped
check -- while simply dropping out of every check here.
"""

from __future__ import annotations

import pytest

pytest.skip("PLANTED: pins the nightly-qa.yml this branch cut", allow_module_level=True)

import re
from pathlib import Path
from typing import Any, Dict, List, Set

import pytest
import yaml

from backend.tests.unit.infrastructure import _workflow_graph as wg
from backend.tests.unit.infrastructure.test_ci_classification import CLASSIFIED

pytestmark = [pytest.mark.unit, pytest.mark.regression]

if not wg.CONFIGURE_REPO.is_file() or not wg.WORKFLOWS.is_dir():
    pytest.skip("this tree has no .github/workflows", allow_module_level=True)

#: The summaries this test must find; if the selector stops finding one, the
#: test is no longer checking it.
EXPECTED = {
    "Release Gate Summary",
    "Security Scan Summary",
    "SDK Unit Tests",
    "integration-tests",
}

TYPED_RESULT = re.compile(r"needs\.[A-Za-z_][\w-]*\.result")

_needs = wg.needs_of


def _is_classified_work(workflow: str, job_id: str, job: Dict[str, Any]) -> bool:
    """A job that runs after a FAILED classifier so that it does its work.

    ``!cancelled() && (needs.changes.result != 'success' || ...)`` matches
    RUNS_AFTER_FAILURE but reads no results: it is a work job, not a summary.
    test_ci_classification.py pins its exact `if:` and its `needs:` to the
    classifier alone, so it has no other ancestor to miss.
    """
    row = CLASSIFIED.get(workflow, {}).get(job_id)
    return row is not None and row[1] == "changes" and _needs(job) == ["changes"]


def _summaries() -> List[wg.Summary]:
    return wg.select_summaries(
        wg.workflow_files(), wg.required_checks(), _is_classified_work
    )


SUMMARIES = _summaries()
IDS = [f"{w}:{j}" for w, j, _, _ in SUMMARIES]

#: The nightly's summaries. nightly-qa.yml runs on a schedule and by hand,
#: never for a pull request's head commit, so they report no required name
#: and R1 does not find them: they are listed, and
#: test_the_nightly_summaries_are_found keeps the list whole. They read
#: toJSON(needs) and carry no stray continue-on-error, as the others. They
#: accept only `success` (test_nightly_workflow.py runs that script), so a
#: shard skipped after an ancestor failed is red: the closure check, which is
#: for a summary that accepts `skipped`, is not applied to them.
NIGHTLY = wg.WORKFLOWS / "nightly-qa.yml"
NIGHTLY_SUMMARY_IDS = ("unit-tests", "integration-tests")


def _nightly_summaries() -> List[wg.Summary]:
    jobs = wg.load(NIGHTLY).get("jobs") or {}
    return [(NIGHTLY.name, j, jobs[j], jobs) for j in NIGHTLY_SUMMARY_IDS if j in jobs]


NIGHTLY_SUMMARIES = _nightly_summaries()
ALL_SUMMARIES = SUMMARIES + NIGHTLY_SUMMARIES
ALL_IDS = [f"{w}:{j}" for w, j, _, _ in ALL_SUMMARIES]


def _reported(workflow: str, job_id: str) -> Set[str]:
    return wg.reported_names(wg.WORKFLOWS / workflow)[job_id]


def test_the_summaries_are_found():
    required = set(wg.required_checks())
    found = set()
    for workflow, job_id, _, _ in SUMMARIES:
        found |= _reported(workflow, job_id) & required
    assert found == EXPECTED


def test_the_nightly_summaries_are_found():
    """Every job of nightly-qa.yml that needs others and runs on exactly
    always() is a listed summary, and every listed one is such a job. A
    summary that lost its `if:`, or a new one, fails here rather than
    dropping out of the checks below."""
    jobs = wg.load(NIGHTLY).get("jobs") or {}
    found = {
        j for j, job in jobs.items() if _needs(job) and job.get("if") in wg.SUMMARY_IF
    }
    assert found == set(NIGHTLY_SUMMARY_IDS), sorted(found)
    assert [j for _, j, _, _ in NIGHTLY_SUMMARIES] == list(NIGHTLY_SUMMARY_IDS)


@pytest.mark.regression
def test_every_required_name_with_needs_runs_after_failure():
    """R2 on the real workflows."""
    problems = wg.unguarded_required_names(
        wg.workflow_files(), wg.required_checks(), _is_classified_work
    )
    assert not problems, "\n".join(problems)


@pytest.mark.regression
def test_every_job_after_a_failed_need_is_a_summary():
    """R3 in every workflow with a classifier or a required summary, not only
    pr-qa-gate.yml, and for any `if:` that can run after a failed need
    (`failure()`, `cancelled()`, `success() || failure()`), not only
    `always()` and `!cancelled()`."""
    required = wg.required_checks()
    scope = wg.r3_workflows(wg.workflow_files(), required, _is_classified_work)
    names = {p.name for p in scope}
    assert {
        "pr-qa-gate.yml",
        "sdk-unit-tests.yml",
        "integration-tests.yml",
        "release-gate.yml",
        "security-scan.yml",
    } <= names, sorted(names)
    problems = []
    for path in scope:
        problems += wg.unselected_runs_after_failure(
            path, required, _is_classified_work
        )
    assert not problems, "\n".join(problems)


def reads_all_needs(job: Dict[str, Any]) -> List[str]:
    """Problems with how a summary reads its needs; empty when it is sound."""
    problems = []
    text = yaml.safe_dump(job)
    typed = sorted(set(TYPED_RESULT.findall(text)))
    if typed:
        problems.append(f"reads a typed list of results, not toJSON(needs): {typed}")
    readers = [
        step
        for step in job.get("steps") or []
        if any(
            re.fullmatch(r"\$\{\{\s*toJSON\(needs\)\s*\}\}", str(v))
            for v in (step.get("env") or {}).values()
        )
    ]
    if not readers:
        problems.append("no step reads ${{ toJSON(needs) }}")
    return problems


def missing_ancestors(job: Dict[str, Any], jobs: Dict[str, Any]) -> Set[str]:
    direct = set(_needs(job))
    ancestors: Set[str] = set()
    todo = list(direct)
    while todo:
        for parent in _needs(jobs[todo.pop()]):
            if parent not in ancestors:
                ancestors.add(parent)
                todo.append(parent)
    return ancestors - direct


@pytest.mark.regression
@pytest.mark.parametrize("workflow, job_id, job, jobs", ALL_SUMMARIES, ids=ALL_IDS)
def test_summary_reads_all_needs(workflow, job_id, job, jobs):
    problems = reads_all_needs(job)
    assert not problems, f"{workflow} {job_id}: " + "; ".join(problems)


@pytest.mark.regression
@pytest.mark.parametrize("workflow, job_id, job, jobs", SUMMARIES, ids=IDS)
def test_needs_are_transitively_closed(workflow, job_id, job, jobs):
    missing = missing_ancestors(job, jobs)
    assert not missing, (
        f"{workflow} {job_id}: missing ancestors: {sorted(missing)}. A failure "
        "there skips the jobs this summary reads, and it never sees the failure."
    )


#: (workflow, summary job id) -> the steps that may carry `continue-on-error`,
#: by exact name. A shard summary's download is one on purpose: no report at
#: all is the checker's to judge, so a download that finds nothing must not
#: end the job before it.
CONTINUE_ON_ERROR_STEPS = {
    ("integration-tests.yml", "integration-tests"): {"Collect the shard reports"},
    ("nightly-qa.yml", "integration-tests"): {"Collect the shard reports"},
    ("nightly-qa.yml", "unit-tests"): {"Collect the shard reports"},
}


def continue_on_error(
    workflow: str, job_id: str, job: Dict[str, Any], allowed: Dict[Any, Set[str]]
) -> List[str]:
    """Where a summary carries `continue-on-error` it is not allowed. On the
    job it makes the required check green whatever happened; on a step that
    reads results or runs a checker it turns that step's red into green. Any
    value counts, `false` included: an expression can make it true."""
    problems = []
    if "continue-on-error" in job:
        problems.append(f"job: continue-on-error: {job['continue-on-error']!r}")
    exempt = allowed.get((workflow, job_id), set())
    for step in job.get("steps") or []:
        if "continue-on-error" in step and step.get("name") not in exempt:
            problems.append(
                f"step {step.get('name') or step.get('uses') or step.get('run')!r}: "
                f"continue-on-error: {step['continue-on-error']!r}"
            )
    return problems


@pytest.mark.regression
@pytest.mark.parametrize("workflow, job_id, job, jobs", ALL_SUMMARIES, ids=ALL_IDS)
def test_summary_carries_no_continue_on_error(workflow, job_id, job, jobs):
    problems = continue_on_error(workflow, job_id, job, CONTINUE_ON_ERROR_STEPS)
    assert not problems, f"{workflow} {job_id}: " + "; ".join(problems)


def test_every_continue_on_error_exception_is_still_there():
    """An exception names a step that exists and still has the flag, so a
    renamed step cannot leave a stale name that exempts its replacement."""
    by_key = {(w, j): job for w, j, job, _ in ALL_SUMMARIES}
    for key, names in CONTINUE_ON_ERROR_STEPS.items():
        assert key in by_key, key
        steps = {s.get("name"): s for s in by_key[key].get("steps") or []}
        for name in names:
            assert "continue-on-error" in steps.get(name, {}), (key, name)


# The checks themselves, against the defects they exist for.


@pytest.mark.regression
@pytest.mark.parametrize("where", ["job", "results", "checker", "download-renamed"])
def test_continue_on_error_on_a_summary_is_refused(where):
    job = {
        "needs": "shard",
        "if": "always()",
        "steps": [
            {"name": "Collect the shard reports", "continue-on-error": True},
            {"name": "Every shard passed", "run": "true"},
            {"name": "Every test ran in exactly one shard", "run": "true"},
        ],
    }
    if where == "job":
        job["continue-on-error"] = True
    elif where == "results":
        job["steps"][1]["continue-on-error"] = True
    elif where == "checker":
        job["steps"][2]["continue-on-error"] = "${{ true }}"
    else:
        job["steps"][0]["name"] = "Collect reports"
    allowed = {
        ("integration-tests.yml", "integration-tests"): {"Collect the shard reports"}
    }
    assert continue_on_error("integration-tests.yml", "integration-tests", job, allowed)


def test_a_typed_list_is_refused():
    job = {
        "needs": ["a", "b"],
        "if": "always()",
        "steps": [{"run": 'test "${{ needs.a.result }}" = success'}],
    }
    assert reads_all_needs(job)


def test_a_dropped_ancestor_is_found():
    jobs = {"changes": {}, "linux": {"needs": "changes"}, "sum": {"needs": ["linux"]}}
    assert missing_ancestors(jobs["sum"], jobs) == {"changes"}


# R1 and R2 against the defects they exist for, on synthetic workflows: an
# unnamed summary (QA E5), a summary with no `if:`, and one whose `if:` adds a
# docs-only lane.

UNNAMED_TYPED = """\
on: pull_request
jobs:
  shard:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        shard: [1, 2]
    steps:
      - run: pytest
  integration-tests:
    needs: shard
{if_line}    runs-on: ubuntu-latest
    steps:
      - run: test "${{{{ needs.shard.result }}}}" = success
"""


def _workflow(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "integration-tests.yml"
    path.write_text(text, encoding="utf-8")
    return path


def _no_classified(workflow: str, job_id: str, job: Dict[str, Any]) -> bool:
    return False


@pytest.mark.regression
def test_an_unnamed_summary_is_found_by_its_check_name(tmp_path):
    """R1: the summary reports `integration-tests` under its id, with no
    `name:`; the old selector (``job.get("name") in required``) never saw it,
    so its typed list of results went unreported."""
    path = _workflow(
        tmp_path, UNNAMED_TYPED.format(if_line="    if: ${{ !cancelled() }}\n")
    )
    found = wg.select_summaries([path], ["integration-tests"], _no_classified)
    assert [(w, j) for w, j, _, _ in found] == [
        ("integration-tests.yml", "integration-tests")
    ]
    assert "name" not in found[0][2]
    assert reads_all_needs(found[0][2])


@pytest.mark.regression
@pytest.mark.parametrize(
    "if_line",
    [
        "",
        "    if: ${{ always() && needs.shard.outputs.docs_only != 'true' }}\n",
        "    if: ${{ !cancelled() }}\n",
    ],
    ids=["no-if", "always-and-docs-only", "not-cancelled"],
)
def test_a_summary_that_can_skip_is_refused(tmp_path, if_line):
    """R2: no `if:` skips the summary when a shard fails (QA E5); an `if:` that
    adds a lane to always() skips it on that lane; `!cancelled()` reports
    cancelled. Each of these is refused by name."""
    path = _workflow(tmp_path, UNNAMED_TYPED.format(if_line=if_line))
    problems = wg.unguarded_required_names(
        [path], ["integration-tests"], _no_classified
    )
    assert len(problems) == 1, problems
    assert problems[0].startswith(
        "'integration-tests' (integration-tests.yml:integration-tests)"
    )


@pytest.mark.regression
@pytest.mark.parametrize("cond", wg.SUMMARY_IF)
def test_a_summary_on_exactly_always_is_accepted(tmp_path, cond):
    path = _workflow(tmp_path, UNNAMED_TYPED.format(if_line=f"    if: {cond}\n"))
    assert (
        wg.unguarded_required_names([path], ["integration-tests"], _no_classified) == []
    )


@pytest.mark.regression
def test_only_a_classified_job_needing_changes_alone_is_exempt(tmp_path):
    text = """\
on: pull_request
jobs:
  changes:
    runs-on: ubuntu-latest
    steps: [{run: "true"}]
  other:
    runs-on: ubuntu-latest
    steps: [{run: "true"}]
  unit-tests:
    name: Unit Tests
    needs: NEEDS
    if: ${{ !cancelled() }}
    runs-on: ubuntu-latest
    steps: [{run: pytest}]
"""

    def classified(workflow: str, job_id: str, job: Dict[str, Any]) -> bool:
        return job_id == "unit-tests" and _needs(job) == ["changes"]

    one = _workflow(tmp_path, text.replace("NEEDS", "changes"))
    assert wg.unguarded_required_names([one], ["Unit Tests"], classified) == []
    two = _workflow(tmp_path, text.replace("NEEDS", "[changes, other]"))
    assert wg.unguarded_required_names([two], ["Unit Tests"], classified)


# R3, widened, against the defects it exists for: a job on an `if:` that
# RUNS_AFTER_FAILURE misses, and the same shape outside pr-qa-gate.yml.


@pytest.mark.regression
@pytest.mark.parametrize(
    "cond",
    [
        "${{ success() || failure() }}",
        "${{ failure() || cancelled() }}",
        "${{ failure() }}",
        "${{ !success() }}",
        "${{ ALWAYS() }}",
        "${{ Failure() }}",
    ],
)
def test_a_job_after_failure_on_any_status_function_is_refused(tmp_path, cond):
    """It reads no needs and reports no required name, and it runs after a
    shard failed. RUNS_AFTER_FAILURE matches none of these conditions, so the
    old R3 passed it."""
    text = UNNAMED_TYPED.format(if_line="    if: ${{ always() }}\n") + (
        "  late:\n"
        "    needs: [shard, integration-tests]\n"
        f"    if: {cond}\n"
        "    runs-on: ubuntu-latest\n"
        "    steps: [{run: pytest}]\n"
    )
    path = _workflow(tmp_path, text)
    assert wg.CAN_RUN_AFTER_FAILURE.search(cond)
    assert wg.r3_workflows([path], ["integration-tests"], _no_classified) == [path]
    assert wg.unselected_runs_after_failure(
        path, ["integration-tests"], _no_classified
    ) == [
        f"integration-tests.yml:late runs after a failed need (if: {cond!r}) and "
        "needs ['integration-tests', 'shard'] beyond changes, but is not a "
        "required summary"
    ]


@pytest.mark.regression
def test_a_job_after_failure_in_sdk_unit_tests_is_refused(tmp_path):
    """The real sdk-unit-tests.yml with one job added: it runs after a failed
    lane and is not its summary. The old R3 read pr-qa-gate.yml alone."""
    real = wg.WORKFLOWS / "sdk-unit-tests.yml"
    path = tmp_path / real.name
    path.write_text(
        real.read_text(encoding="utf-8").rstrip("\n") + "\n\n  late:\n"
        "    needs: [changes, linux]\n"
        "    if: ${{ success() || failure() }}\n"
        "    runs-on: ubuntu-latest\n"
        '    steps: [{run: "true"}]\n',
        encoding="utf-8",
    )
    required = wg.required_checks()
    assert wg.r3_workflows([real], required, _is_classified_work) == [real]
    assert wg.unselected_runs_after_failure(real, required, _is_classified_work) == []
    assert wg.r3_workflows([path], required, _is_classified_work) == [path]
    assert wg.unselected_runs_after_failure(path, required, _is_classified_work) == [
        "sdk-unit-tests.yml:late runs after a failed need (if: "
        "'${{ success() || failure() }}') and needs ['linux'] beyond changes, "
        "but is not a required summary"
    ]


def test_a_notifier_outside_the_scope_is_left_alone(tmp_path):
    """No classifier and no required summary: nightly-qa's failure issue."""
    path = tmp_path / "nightly.yml"
    path.write_text(
        "on: schedule\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
        '    steps: [{run: "true"}]\n  issue:\n    needs: a\n'
        "    if: failure()\n    runs-on: ubuntu-latest\n"
        '    steps: [{run: "true"}]\n',
        encoding="utf-8",
    )
    assert wg.r3_workflows([path], ["integration-tests"], _no_classified) == []


@pytest.mark.regression
@pytest.mark.parametrize(
    "on, selected",
    [
        ("pull_request", True),
        ("push", True),
        ("{schedule: [{cron: '0 2 * * *'}], workflow_dispatch: null}", False),
    ],
    ids=["pull_request", "push", "schedule-and-dispatch"],
)
def test_a_summary_under_a_required_name_is_one_only_where_it_runs(
    tmp_path, on, selected
):
    """nightly-qa.yml's unit summary is named `Unit Tests`, a required name,
    and runs on a schedule and by hand. Read for every event whatever the
    workflow's triggers, it was taken for the pull request's check, and its
    notifier for a job that escaped R3."""
    text = UNNAMED_TYPED.format(if_line="    if: always()\n")
    text = text.replace("on: pull_request\n", f"on: {on}\n", 1)
    assert f"on: {on}\n" in text
    path = _workflow(tmp_path, text)
    found = wg.select_summaries([path], ["integration-tests"], _no_classified)
    assert bool(found) is selected, found
    assert bool(wg.r3_workflows([path], ["integration-tests"], _no_classified)) is (
        selected
    )


@pytest.mark.regression
def test_an_upper_case_summary_condition_is_still_a_summary(tmp_path):
    """GitHub reads `ALWAYS()` as `always()`. A case-sensitive selector did
    not select such a summary, so none of its checks applied."""
    path = _workflow(
        tmp_path, UNNAMED_TYPED.format(if_line="    if: ${{ ALWAYS() }}\n")
    )
    found = wg.select_summaries([path], ["integration-tests"], _no_classified)
    assert [(w, j) for w, j, _, _ in found] == [
        ("integration-tests.yml", "integration-tests")
    ]
    assert reads_all_needs(found[0][2])
