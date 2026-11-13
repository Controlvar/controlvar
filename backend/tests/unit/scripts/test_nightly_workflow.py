"""``nightly-qa.yml``: the unit job has room to finish, and a timeout files the issue (#1695).

On 2026-11-02 and 2026-11-03 the nightly ``Unit Tests`` job hit its 30 minute
``timeout-minutes`` (pytest at 98%). A timed-out job's result is ``cancelled``,
not ``failure``, so ``nightly-failure-issue`` (``if: failure()``) was skipped
and nobody was told. Pinned here:

* the unit job's timeout is at least 45 minutes;
* the integration job's timeout is at least 100 minutes: its last green runs
  took 53.7, 65.4 and 73.0 minutes, and on 2026-11-13 run 43357047435 was
  cancelled at its 75 minute limit;
* the issue job runs when a job it needs is ``failure`` or ``cancelled`` and
  not when all of them succeeded (the expression is evaluated, not only
  compared as a string);
* the issue job needs every other job in the file, so a new nightly job
  cannot be left out of the notifier.
"""

from __future__ import annotations

import re
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


def test_the_integration_job_has_room_to_finish():
    timeout = _jobs()["integration-tests"]["timeout-minutes"]
    assert timeout >= MIN_INTEGRATION_MINUTES, (
        f"integration-tests has timeout-minutes: {timeout}; its green runs took up to "
        f"73 minutes and one was cancelled at 75, so anything under "
        f"{MIN_INTEGRATION_MINUTES} leaves no room"
    )


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
