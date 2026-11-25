"""A failed change classifier runs every check, and every classified job is pinned exactly.

``pr-qa-gate.yml``'s ``changes`` job decides whether a pull request is
documentation only, and the heavy jobs skip their work on a "yes". Two ways
that went green over nothing:

* **A crashed classifier was an accepted skip.** A job whose ``needs:`` job
  failed is skipped, and branch protection accepts a skipped check. With the
  plain ``if: needs.changes.outputs.docs_only != 'true'`` (an implicit
  ``success() &&``), a classifier that failed or timed out skipped every
  classified job and the gate merged with nothing run. Every classified job is
  now ``${{ !cancelled() && (needs.changes.result != 'success' || <lane>) }}``
  and the three that must never skip (``core-build-shard``, ``full-build``,
  ``sdk-live-contract``) are ``${{ !cancelled() }}``; ``core-build``, the
  shards' summary, is ``${{ always() }}`` and needs the classifier too, so a
  failed classifier turns it red.
* **A partial write.** ``git diff ... | classify >> "$GITHUB_OUTPUT"`` appends
  a verdict on whatever part of the list it received, and only then does
  ``pipefail`` fail the step. The step now collects the list and the verdict
  into variables first and writes one checked line last, so a failure leaves
  ``$GITHUB_OUTPUT`` untouched. ``TestTheChangesStepRuns`` runs the step's own
  script with a ``git`` and a ``python3`` that write a lane and then fail.

``!cancelled()`` has one cost: it runs the job after ANY of its needs fails.
So ``CLASSIFIED`` pins the exact ``needs:`` as well as the exact ``if:`` of
every job in every workflow that has a ``changes`` job, compared as an ordered
list of items, the way ``CORE_BUILD_STEPS`` pins steps. A new job, an
inverted condition or a second need fails with a diff naming the job.

``sdk-unit-tests.yml`` keeps its plain lane conditions: when its ``changes``
fails its lane jobs skip, but the required ``SDK Unit Tests`` summary runs on
``always()``, needs ``changes`` and fails on any result other than success or
skipped (``test_ci_aggregators.py``). Its rows pin that shape as it is.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import pytest

from backend.tests.unit.infrastructure import _workflow_graph as _wg
from backend.tests.unit.infrastructure.test_docs_only_gate import _load

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
GATE = WORKFLOWS / "pr-qa-gate.yml"

#: A distribution that does not ship the workflows has nothing here to check.
if not GATE.is_file():
    pytest.skip("this tree has no .github/workflows", allow_module_level=True)

#: The classifier job's id, in every workflow that has one.
CLASSIFIER = "changes"

#: A classified job in pr-qa-gate.yml: runs unless the classifier SUCCEEDED
#: and said documentation only.
RUNS_UNLESS_DOCS = (
    "${{ !cancelled() && (needs.changes.result != 'success' "
    "|| needs.changes.outputs.docs_only != 'true') }}"
)
#: A job whose check name protection cannot see skipped: it always runs, and
#: conditions its steps instead (test_docs_only_gate.py).
NEVER_SKIPS = "${{ !cancelled() }}"

Needs = Union[None, str, List[str]]

#: workflow file -> job id -> (exact `if:`, exact `needs:`), in file order.
CLASSIFIED: Dict[str, Dict[str, Tuple[Optional[str], Needs]]] = {
    "pr-qa-gate.yml": {
        "changes": (None, None),
        "unit-tests": (RUNS_UNLESS_DOCS, "changes"),
        "module-tests": (RUNS_UNLESS_DOCS, "changes"),
        "base-requirements-only": (RUNS_UNLESS_DOCS, "changes"),
        "smoke-tests": (RUNS_UNLESS_DOCS, "changes"),
        "frontend-tests": (RUNS_UNLESS_DOCS, "changes"),
        "sdk-contracts": (RUNS_UNLESS_DOCS, "changes"),
        "core-build-shard": (NEVER_SKIPS, "changes"),
        "core-build": ("${{ always() }}", ["changes", "core-build-shard"]),
        "full-build": (NEVER_SKIPS, "changes"),
        "sdk-live-contract": (NEVER_SKIPS, "changes"),
        "browser-e2e": (RUNS_UNLESS_DOCS, "changes"),
        "docker-smoke": (RUNS_UNLESS_DOCS, "changes"),
    },
    "sdk-unit-tests.yml": {
        "changes": (None, None),
        "linux": ("needs.changes.outputs.any_linux == 'true'", "changes"),
        "elixir": ("needs.changes.outputs.elixir == 'true'", "changes"),
        "flutter": ("needs.changes.outputs.flutter == 'true'", "changes"),
        "android": ("needs.changes.outputs.android == 'true'", "changes"),
        "ios": ("needs.changes.outputs.ios == 'true'", "changes"),
        "sdk-unit-tests": (
            "always()",
            ["changes", "linux", "elixir", "flutter", "android", "ios"],
        ),
    },
}

#: The exact script of the classifier step. It is also RUN below.
CHANGES_RUN = """\
set -euo pipefail
files="$(git diff --no-renames --name-only "$BASE" "$HEAD")"
lane="$(printf '%s\\n' "$files" | python3 scripts/classify_changes.py)"
case "$lane" in
  docs_only=true | docs_only=false) ;;
  *) printf '::error::the classifier printed %s, not one docs_only line\\n' "$lane" >&2; exit 1 ;;
esac
printf '%s\\n' "$lane" >> "$GITHUB_OUTPUT"
"""

#: The form this replaced, kept to prove the tests below fail on it.
OLD_CHANGES_RUN = (
    "set -euo pipefail\n"
    'git diff --no-renames --name-only "$BASE" "$HEAD" '
    '| python3 scripts/classify_changes.py >> "$GITHUB_OUTPUT"\n'
)
OLD_IF = "needs.changes.outputs.docs_only != 'true'"


def _needs(job: Dict[str, Any]) -> List[str]:
    needs = job.get("needs") or []
    return [needs] if isinstance(needs, str) else list(needs)


def _classified_work(workflow: str, job_id: str, job: Dict[str, Any]) -> bool:
    """A CLASSIFIED job that needs the classifier alone (see test_ci_aggregators)."""
    row = CLASSIFIED.get(workflow, {}).get(job_id)
    return row is not None and row[1] == CLASSIFIER and _needs(job) == [CLASSIFIER]


def classified_workflows() -> Dict[str, Dict[str, Any]]:
    """Every workflow with a classifier job, or a job that needs one."""
    found = {}
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        jobs = _load(path).get("jobs") or {}
        if CLASSIFIER in jobs or any(
            CLASSIFIER in _needs(job) for job in jobs.values()
        ):
            found[path.name] = jobs
    return found


def actual_table() -> Dict[str, Dict[str, Tuple[Optional[str], Needs]]]:
    return {
        name: {
            job_id: (job.get("if"), job.get("needs")) for job_id, job in jobs.items()
        }
        for name, jobs in classified_workflows().items()
    }


# ---------------------------------------------------------------------------
# A6: the exact table
# ---------------------------------------------------------------------------
class TestEveryClassifiedJobIsPinned:
    def test_the_set_of_classified_workflows_is_exact(self):
        assert list(actual_table()) == list(CLASSIFIED)

    @pytest.mark.parametrize("workflow", list(CLASSIFIED))
    def test_every_job_has_exactly_its_if_and_needs(self, workflow):
        actual = actual_table().get(workflow, {})
        assert list(actual.items()) == list(CLASSIFIED[workflow].items())

    @pytest.mark.regression
    def test_every_gate_job_needs_the_classifier_alone(self):
        """`!cancelled()` and `always()` run a job after ANY failed need; one
        need keeps it meaning "after the classifier failed" and nothing else.
        The one exception is a required summary, found the way
        test_ci_aggregators.py finds it, so it carries that file's checks
        (R3). Not a list of names: an exception carved into a list carries no
        obligation."""
        problems = _wg.unselected_runs_after_failure(
            GATE, _wg.required_checks(), _classified_work
        )
        assert not problems, "\n".join(problems)

    def test_the_expressions_stay_inside_braces(self):
        """A bare leading `!` is a YAML tag, not an expression."""
        for job_id, (cond, _) in _gate_conditions().items():
            if cond is not None:
                assert cond.startswith("${{ ") and cond.endswith(" }}"), job_id
                assert cond.count("${{") == 1, job_id


# ---------------------------------------------------------------------------
# What the pinned conditions DO when the classifier fails
# ---------------------------------------------------------------------------
STATUS_FUNCTIONS = ("success()", "failure()", "cancelled()", "always()")
#: Every token the conditions in pr-qa-gate.yml may contain; anything else
#: makes `evaluate` refuse rather than guess.
TOKEN = re.compile(
    r"\s+|\$\{\{|\}\}|!cancelled\(\)|always\(\)|success\(\)|failure\(\)|"
    r"needs\.changes\.result|needs\.changes\.outputs\.docs_only|"
    r"&&|\|\||!=|==|\(|\)|'[a-z]*'"
)


def evaluate(cond: Optional[str], result: str, docs_only: str) -> bool:
    """Would GitHub run a job with this `if:` after the classifier ended so?

    Only the vocabulary of the pinned conditions is understood; anything else
    is refused. A condition with no status function is `success() && (...)`,
    as on GitHub. `||` binds looser than `&&`, `==`/`!=` tighter.
    """
    expr = (cond or "").strip()
    if expr.startswith("${{") and expr.endswith("}}"):
        expr = expr[3:-2].strip()
    if not any(f in expr for f in STATUS_FUNCTIONS):
        expr = f"success() && ({expr})" if expr else "success()"
    pieces = TOKEN.findall(expr)
    assert "".join(pieces) == expr, f"cannot evaluate {cond!r}"
    values: Dict[str, Union[bool, str]] = {
        "!cancelled()": True,  # the run was not cancelled
        "always()": True,
        "success()": result == "success",
        "failure()": result == "failure",
        "needs.changes.result": result,
        "needs.changes.outputs.docs_only": docs_only,
    }
    tokens = [p for p in pieces if not p.isspace()]
    pos = 0

    def peek() -> Optional[str]:
        return tokens[pos] if pos < len(tokens) else None

    def take() -> str:
        nonlocal pos
        assert pos < len(tokens), f"cannot evaluate {cond!r}"
        pos += 1
        return tokens[pos - 1]

    def value() -> Union[bool, str]:
        token = take()
        if token == "(":
            inner = either()
            assert take() == ")", f"cannot evaluate {cond!r}"
            return inner
        if token in values:
            return values[token]
        assert token.startswith("'") and token.endswith("'"), (
            f"cannot evaluate {cond!r}"
        )
        return token[1:-1]

    def compare() -> Union[bool, str]:
        left = value()
        if peek() in ("==", "!="):
            op = take()
            right = value()
            return (left == right) if op == "==" else (left != right)
        return left

    def both() -> bool:
        ok = bool(compare())
        while peek() == "&&":
            take()
            ok = bool(compare()) and ok
        return ok

    def either() -> bool:
        ok = both()
        while peek() == "||":
            take()
            ok = both() or ok
        return ok

    answer = either()
    assert pos == len(tokens), f"cannot evaluate {cond!r}"
    return answer


#: name -> (classifier result, its docs_only output); every gate job must run.
SCENARIOS = {
    "code change": ("success", "false"),
    # The two failure shapes: nothing written, and a lane written before the
    # failure (the route variable-first collection closes; the result guard
    # holds even if it were open).
    "classifier failed, nothing written": ("failure", ""),
    "classifier wrote docs_only=true, then failed": ("failure", "true"),
}


def _gate_conditions() -> Dict[str, Tuple[Optional[str], Needs]]:
    """The conditions as the WORKFLOW has them, not as the table says."""
    return actual_table()["pr-qa-gate.yml"]


class TestAFailedClassifierRunsEverything:
    @pytest.mark.parametrize("scenario", list(SCENARIOS))
    def test_every_gate_job_runs(self, scenario):
        result, docs_only = SCENARIOS[scenario]
        skipped = [
            job_id
            for job_id, (cond, needs) in _gate_conditions().items()
            if needs is not None and not evaluate(cond, result, docs_only)
        ]
        assert not skipped, f"{scenario}: skipped {skipped}"

    def test_a_docs_only_change_skips_only_the_plain_jobs(self):
        ran = {
            job_id
            for job_id, (cond, needs) in _gate_conditions().items()
            if needs is not None and evaluate(cond, "success", "true")
        }
        assert ran == {
            "core-build-shard",
            "core-build",
            "full-build",
            "sdk-live-contract",
        }

    def test_the_old_form_skips_after_a_failure(self):
        """The defect: implicit success() turns a crashed classifier into a skip."""
        assert not evaluate(OLD_IF, "failure", "true")
        assert not evaluate(OLD_IF, "failure", "")
        assert not evaluate(None, "failure", "")  # the old no-`if` profile-build matrix

    def test_the_evaluator_refuses_what_it_does_not_know(self):
        with pytest.raises(AssertionError):
            evaluate("${{ !cancelled() && github.actor != 'x' }}", "success", "")


# ---------------------------------------------------------------------------
# The classifier step: one checked line, or nothing
# ---------------------------------------------------------------------------
def _step(jobs: Dict[str, Any]) -> Dict[str, Any]:
    (step,) = [s for s in jobs[CLASSIFIER]["steps"] if s.get("id") == "check"]
    return step


def _live_run() -> str:
    """The step's script as the workflow has it."""
    return _step(_load(GATE)["jobs"])["run"]


def _stub(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def run_step(script: str, tmp_path: Path, git: str, python3: Optional[str] = None):
    """Run *script* as the runner would, with stub `git` (and `python3`).

    Returns ``(exit status, contents of $GITHUB_OUTPUT)``.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub(bin_dir, "git", git)
    if python3 is not None:
        _stub(bin_dir, "python3", python3)
    output = tmp_path / "github_output"
    output.write_text("", encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "BASE": "base-sha",
        "HEAD": "head-sha",
        "GITHUB_OUTPUT": str(output),
    }
    proc = subprocess.run(
        ["bash", "-e", "-c", script],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc.returncode, output.read_text(encoding="utf-8")


#: `git diff` that lists docs paths and then fails part-way.
GIT_PARTIAL = "printf 'docs/a.md\\ndocs/b.md\\n'\nexit 1\n"
#: A classifier that writes a lane and then fails.
CLASSIFIER_WRITES_THEN_FAILS = "cat >/dev/null\nprintf 'docs_only=true\\n'\nexit 1\n"
#: A classifier that answers twice.
CLASSIFIER_TWO_LINES = "cat >/dev/null\nprintf 'docs_only=true\\ndocs_only=false\\n'\n"


class TestTheChangesStepRuns:
    def test_the_step_is_exactly_the_script(self):
        assert _step(_load(GATE)["jobs"])["run"] == CHANGES_RUN

    @pytest.mark.parametrize(
        "listing, expected",
        [
            ("docs/a.md\\nmkdocs.yml\\n", "docs_only=true\n"),
            ("docs/a.md\\nbackend/app/main.py\\n", "docs_only=false\n"),
            ("", "docs_only=false\n"),
        ],
        ids=["docs", "code", "empty"],
    )
    def test_a_good_run_writes_exactly_one_line(self, tmp_path, listing, expected):
        status, written = run_step(_live_run(), tmp_path, f"printf '{listing}'\n")
        assert (status, written) == (0, expected)

    @pytest.mark.parametrize(
        "git, python3",
        [
            (GIT_PARTIAL, None),
            ("printf 'docs/a.md\\n'\n", CLASSIFIER_WRITES_THEN_FAILS),
            ("printf 'docs/a.md\\n'\n", CLASSIFIER_TWO_LINES),
        ],
        ids=["git-fails-part-way", "classifier-writes-then-fails", "two-lines"],
    )
    def test_a_failure_writes_nothing(self, tmp_path, git, python3):
        status, written = run_step(_live_run(), tmp_path, git, python3)
        assert status != 0
        assert written == "", f"a failed step left {written!r} in $GITHUB_OUTPUT"

    def test_the_old_pipe_left_a_lane_behind(self, tmp_path):
        """The defect this closes, shown on the script it replaced."""
        status, written = run_step(OLD_CHANGES_RUN, tmp_path, GIT_PARTIAL)
        assert status != 0
        assert written == "docs_only=true\n"


# ---------------------------------------------------------------------------
# R3 against the defect it exists for
# ---------------------------------------------------------------------------
R3_GATE = """\
on: pull_request
jobs:
  changes:
    runs-on: ubuntu-latest
    steps: [{run: "true"}]
  build:
    needs: changes
    if: ${{ !cancelled() }}
    runs-on: ubuntu-latest
    steps: [{run: "true"}]
  late:
    name: LATE_NAME
    needs: [changes, build]
    if: LATE_IF
    runs-on: ubuntu-latest
    steps:
      - run: "true"
"""


def _r3(tmp_path: Path, name: str, cond: str) -> List[str]:
    path = tmp_path / "pr-qa-gate.yml"
    text = R3_GATE.replace("LATE_NAME", name).replace("LATE_IF", cond)
    path.write_text(text, encoding="utf-8")
    return _wg.unselected_runs_after_failure(path, ["Late Summary"], _classified_work)


@pytest.mark.regression
@pytest.mark.parametrize("cond", ["${{ always() }}", "${{ !cancelled() }}"])
def test_a_work_job_after_failure_with_two_needs_is_refused(tmp_path, cond):
    """An `always()` (or `!cancelled()`) job with two needs and no
    toJSON(needs) reader, whose name is not required: it runs after `build`
    failed and does its work anyway. The old check looked only at
    `!cancelled()`, so `always()` passed it."""
    problems = _r3(tmp_path, "Late Work", cond)
    assert problems == [
        f"pr-qa-gate.yml:late runs after a failed need (if: {cond!r}) and needs "
        "['build'] beyond changes, but is not a required summary"
    ]


@pytest.mark.regression
def test_a_required_summary_is_the_exception(tmp_path):
    """Selected as a summary, it answers to test_ci_aggregators.py instead."""
    assert _r3(tmp_path, "Late Summary", "${{ always() }}") == []
