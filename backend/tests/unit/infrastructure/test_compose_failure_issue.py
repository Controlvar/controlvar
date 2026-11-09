"""A red Compose Production run off a pull request files an issue (#1807).

compose-production.yml's ``failure-issue`` job is a copy of Doc Examples' job
of that name, which has opened real issues on red runs (#110, #112, #353), as
chart-kind's is (test_chart_kind_failure_issue.py, which this follows and does
not change). The copy is held identical here: the two jobs are compared as
parsed YAML after replacing the few values that must differ -- ``needs``, the
issue title, the workflow named in the body, and the label and its
description. Anything else that drifts (a permission, the ``if:``, a step, the
de-duplication query) fails the test, in either file.

The workflow gained a nightly run and the compose-upgrade job in the same
change; a red nightly, push or dispatch is what this job reports. Every
workflow is loaded with the duplicate-key-refusing loader, so a second ``if:``
or ``permissions:`` key cannot make this test read one value while GitHub
reads the other.
"""

from __future__ import annotations

import copy
from typing import Any, Dict

import pytest

from backend.tests.unit.infrastructure import _workflow_graph as wg

pytestmark = [pytest.mark.unit, pytest.mark.regression]

if not wg.WORKFLOWS.is_dir():
    pytest.skip("this tree has no .github/workflows", allow_module_level=True)

DOC_EXAMPLES = wg.WORKFLOWS / "doc-examples.yml"
COMPOSE = wg.WORKFLOWS / "compose-production.yml"

#: The jobs it waits for: both of the workflow's.
NEEDS = ["compose-production", "compose-upgrade"]

#: (Doc Examples' text, Compose Production's text, occurrences in the run
#: script). The count is checked on Doc Examples' side, so a renamed label
#: there cannot leave half the copy pointing at the old one.
RUN_SUBSTITUTIONS = [
    (
        'TITLE="Documentation examples are red"',
        'TITLE="Compose Production is red"',
        1,
    ),
    ('BODY="Doc Examples failed on', 'BODY="Compose Production failed on', 1),
    (
        '--description "Doc Examples failed"',
        '--description "Compose Production failed"',
        1,
    ),
    ("doc-examples-failure", "compose-production-failure", 3),
]

IF = (
    "${{ github.event_name != 'pull_request' && !cancelled() && "
    "(failure() || contains(needs.*.result, 'cancelled')) }}"
)


def _job(path, name="failure-issue") -> Dict[str, Any]:
    jobs = wg.load(path).get("jobs") or {}
    assert name in jobs, f"{path.name} has no `{name}` job"
    return jobs[name]


def expected_compose_job(doc_examples_job: Dict[str, Any]) -> Dict[str, Any]:
    """Doc Examples' job with only the values that must differ replaced."""
    job = copy.deepcopy(doc_examples_job)
    job["needs"] = list(NEEDS)
    (step,) = job["steps"]
    run = step["run"]
    for old, new, count in RUN_SUBSTITUTIONS:
        assert run.count(old) == count, (
            f"Doc Examples' script has {old!r} {run.count(old)} times"
        )
        run = run.replace(old, new)
    step["run"] = run
    return job


def compose_problems(compose_job: Any, doc_examples_job: Dict[str, Any]) -> list:
    expected = expected_compose_job(doc_examples_job)
    if compose_job == expected:
        return []
    if not isinstance(compose_job, dict):
        return [f"not a job: {compose_job!r}"]
    keys = sorted(set(expected) | set(compose_job))
    return [
        f"{key}: {compose_job.get(key)!r} != {expected.get(key)!r}"
        for key in keys
        if compose_job.get(key) != expected.get(key)
    ]


def test_the_compose_failure_issue_job_is_doc_examples_job():
    assert compose_problems(_job(COMPOSE), _job(DOC_EXAMPLES)) == []


def test_it_waits_for_every_other_job_in_the_workflow():
    """`needs` is the one structural difference; it must cover all the work."""
    jobs = wg.load(COMPOSE)["jobs"]
    assert set(_job(COMPOSE)["needs"]) == set(jobs) - {"failure-issue"} == set(NEEDS)


def test_it_files_on_a_red_or_timed_out_run_off_a_pull_request_only():
    """Literal, so a change made to both files at once is still seen here."""
    job = _job(COMPOSE)
    assert job["if"] == IF
    assert job["permissions"] == {"contents": "read", "issues": "write"}


def test_it_is_the_only_job_that_can_write_issues():
    jobs = wg.load(COMPOSE)["jobs"]
    writers = [
        name
        for name, job in jobs.items()
        if (job.get("permissions") or {}).get("issues") == "write"
    ]
    assert writers == ["failure-issue"]
    assert wg.load(COMPOSE)["permissions"] == {"contents": "read"}


def test_a_run_it_reports_on_is_never_cancelled_by_a_later_one():
    """Off a pull request the group is the run's own id: a later nightly or
    push cannot cancel the red run whose issue this would file."""
    concurrency = wg.load(COMPOSE)["concurrency"]
    assert concurrency["group"] == (
        "compose-production-${{ github.event.pull_request.number || github.run_id }}"
    )


# ---------------------------------------------------------------------------
# The check catches what it claims to
# ---------------------------------------------------------------------------


def _tampered(mutate) -> Any:
    job = copy.deepcopy(_job(COMPOSE))
    return mutate(job)


def _drop_issue_write(job):
    job["permissions"]["issues"] = "read"
    return job


def _plain_failure_if(job):
    job["if"] = "${{ github.event_name != 'pull_request' && failure() }}"
    return job


def _no_dedup(job):
    job["steps"][0]["run"] = job["steps"][0]["run"].replace(
        "--state open --label compose-production-failure", "--state open"
    )
    return job


def _extra_step(job):
    job["steps"].append({"run": "true"})
    return job


def _one_job_only(job):
    job["needs"] = ["compose-production"]
    return job


@pytest.mark.parametrize(
    "mutate",
    [_drop_issue_write, _plain_failure_if, _no_dedup, _extra_step, _one_job_only],
)
def test_a_drifted_copy_is_refused(mutate):
    assert compose_problems(_tampered(mutate), _job(DOC_EXAMPLES)) != []


def test_a_missing_job_is_refused():
    assert compose_problems(None, _job(DOC_EXAMPLES)) != []
