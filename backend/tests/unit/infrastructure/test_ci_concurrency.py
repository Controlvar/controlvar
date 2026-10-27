"""Each pull request has its own concurrency group, and a run that an edit or a
label starts is never cancelled (B1, B3; #75, #399).

A concurrency group is repository-wide. Two runs in one group with
``cancel-in-progress: true`` means the older is cancelled, so the group must
name the pull request. ``github.head_ref`` does not: it is the bare branch
name, and two forks that each edit a file in the web editor both push
``patch-1``. Seven workflows keyed on it (``head_ref`` alone, or
``head_ref || github.ref``, which short-circuits to ``head_ref`` on a pull
request), so the second contributor's push cancelled the first one's checks.

B1 does not look for a substring. It evaluates every group the way GitHub
does, for two simulated fork pull requests that share the branch ``patch-1``
(numbers 1 and 2), and asserts the two strings differ. A typo such as
``pull_request.numbr`` evaluates to null on GitHub -- not an error, and
actionlint does not type-check the payload -- so it is evaluated as null here
too, and the group collapses into one string shared by every pull request.

B3: a workflow whose ``types:`` include ``edited``, ``labeled`` or
``unlabeled`` gets a second run on the SAME head commit. Any group cancels one
of the two (``cancel-in-progress: false`` still cancels a replaced pending
run), the cancelled run stays in the rollup, and branch protection refuses the
merge (#399). Such a workflow has no ``concurrency:`` block at all.

A push to main keeps the group it had before the change: the table below is
the value each changed workflow evaluated to on main before #75 was fixed. The
deliberate exceptions are chart-kind (PR-2') and demo-apps, a later workflow,
whose runs on main are keyed on the commit and never cancelled.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import pytest

from backend.tests.unit.infrastructure import _workflow_graph as wg

pytestmark = [pytest.mark.unit, pytest.mark.regression]

if not wg.WORKFLOWS.is_dir():
    pytest.skip("this tree has no .github/workflows", allow_module_level=True)

#: Events whose runs belong to one pull request.
PR_EVENTS = ("pull_request", "pull_request_target")

#: Activity types that start a second run on the head commit a run already has.
SAME_COMMIT_TYPES = {"edited", "labeled", "unlabeled"}

#: GitHub gives null for a payload property that does not exist.
NULL_UNDER = ("github.event.",)

FORK_BRANCH = "patch-1"
MAIN_SHA = "0" * 40


def _pr_context(event: str, number: int) -> Dict[str, Any]:
    """`github` for a fork pull request #number from branch `patch-1` into main."""
    return {
        "github": {
            "event_name": event,
            # pull_request runs on the merge ref; pull_request_target on the base.
            "ref": f"refs/pull/{number}/merge"
            if event == "pull_request"
            else "refs/heads/main",
            "head_ref": FORK_BRANCH,
            "base_ref": "main",
            "sha": f"{number:040d}",
            "run_id": str(9000 + number),
            "event": {"pull_request": {"number": number}},
        }
    }


def _main_context(event: str) -> Dict[str, Any]:
    """`github` for a run on main started by `event` (push, schedule, dispatch)."""
    return {
        "github": {
            "event_name": event,
            "ref": "refs/heads/main",
            "head_ref": "",
            "base_ref": "",
            "sha": MAIN_SHA,
            "run_id": "8000",
            "event": {},
        }
    }


def _groups(workflow: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    """(where, concurrency) for the workflow's block and every job's."""
    out = []
    if "concurrency" in workflow:
        out.append(("workflow", workflow["concurrency"]))
    for job_id, job in (workflow.get("jobs") or {}).items():
        if isinstance(job, dict) and "concurrency" in job:
            out.append((f"job {job_id}", job["concurrency"]))
    return out


def _group_and_cancel(concurrency: Any, context: Dict[str, Any]) -> Tuple[Any, Any]:
    if isinstance(concurrency, str):  # `concurrency: <group>` shorthand
        group, cancel = concurrency, False
    else:
        group = concurrency.get("group")
        cancel = concurrency.get("cancel-in-progress", False)
    rendered = wg.render(str(group), context, NULL_UNDER)
    if isinstance(cancel, str):
        # An expression renders to text, as GitHub's does; only the exact
        # words become booleans, so anything else still fails a comparison.
        cancel = wg.render(cancel, context, NULL_UNDER)
        cancel = {"true": True, "false": False}.get(cancel, cancel)
    return rendered, cancel


def _pr_workflows() -> List[Tuple[str, str]]:
    out = []
    for path in wg.workflow_files():
        on = wg.triggers(wg.load(path))
        out.extend((path.name, event) for event in PR_EVENTS if event in on)
    return out


PR_WORKFLOWS = _pr_workflows()


def _types(path_name: str, event: str) -> Optional[List[str]]:
    filters = wg.triggers(wg.load(wg.WORKFLOWS / path_name)).get(event) or {}
    return filters.get("types")


def b3_problems(workflow: Dict[str, Any], event: str) -> List[str]:
    """Why a same-commit-retriggered workflow could cancel a run; empty if none."""
    filters = wg.triggers(workflow).get(event) or {}
    types = set(filters.get("types") or [])
    if not types & SAME_COMMIT_TYPES:
        return []
    return [
        f"{where} has a concurrency block, and `types:` includes "
        f"{sorted(types & SAME_COMMIT_TYPES)}: a second run on the same commit "
        f"cancels one of the two"
        for where, _ in _groups(workflow)
    ]


def b1_problems(workflow: Dict[str, Any], event: str) -> List[str]:
    """Why two fork pull requests named `patch-1` could share a group."""
    problems = b3_problems(workflow, event)
    for where, concurrency in _groups(workflow):
        first, _ = _group_and_cancel(concurrency, _pr_context(event, 1))
        second, _ = _group_and_cancel(concurrency, _pr_context(event, 2))
        for value in (first, second):
            if isinstance(value, wg.Unresolved):
                problems.append(f"{where}: cannot evaluate the group: {value.what}")
        if not problems and first == second:
            problems.append(
                f"{where}: pull requests #1 and #2 from forks' `{FORK_BRANCH}` "
                f"both get the group {first!r}, so one cancels the other"
            )
    return problems


# ---------------------------------------------------------------------------
# The selectors find what they must, or the tests below check nothing
# ---------------------------------------------------------------------------


def test_the_pull_request_workflows_are_found():
    names = {name for name, _ in PR_WORKFLOWS}
    # The seven that keyed on head_ref (#75), and the two re-run on edit/label.
    assert {
        "chart-kind.yml",
        "chart.yml",
        "compose-production.yml",
        "conventional-title.yml",
        "lint.yml",
        "pr-qa-gate.yml",
        "regression-guard.yml",
        "sdk-unit-tests.yml",
    } <= names


def test_the_same_commit_retriggered_workflows_are_found():
    found = {
        name
        for name, event in PR_WORKFLOWS
        if set(_types(name, event) or []) & SAME_COMMIT_TYPES
    }
    assert {"conventional-title.yml", "regression-guard.yml"} <= found


# ---------------------------------------------------------------------------
# B1
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,event", PR_WORKFLOWS, ids=[f"{n}:{e}" for n, e in PR_WORKFLOWS]
)
def test_each_pull_request_has_its_own_group(name, event):
    """No block passes only through B3; a block must differ between PRs."""
    assert b1_problems(wg.load(wg.WORKFLOWS / name), event) == []


def test_no_two_workflows_share_a_pull_request_group():
    """Groups are repository-wide: two workflows in one group cancel each other."""
    seen: Dict[str, str] = {}
    clashes = []
    for name, event in PR_WORKFLOWS:
        for where, concurrency in _groups(wg.load(wg.WORKFLOWS / name)):
            group, _ = _group_and_cancel(concurrency, _pr_context(event, 1))
            key = str(group)
            if key in seen:
                clashes.append(f"{seen[key]} and {name} {where}: {key!r}")
            seen[key] = f"{name} {where}"
    assert clashes == []


#: (workflow, event on main) -> (group, cancel-in-progress), as before #75 --
#: except chart-kind, whose main runs are keyed on the commit and never
#: cancelled (PR-2', EM C7c): keyed on the ref, each push to main cancelled the
#: one before it, so a merge that broke the install was never seen on main.
#: demo-apps came later and is keyed the same way, for the same reason: keyed
#: on the ref, the run waiting behind one in flight is replaced by the next
#: push, even with cancel-in-progress false.
MAIN_GROUPS = {
    ("chart-kind.yml", "push"): (f"chart-kind-push-{MAIN_SHA}", False),
    ("chart-kind.yml", "schedule"): (f"chart-kind-schedule-{MAIN_SHA}", False),
    ("chart-kind.yml", "workflow_dispatch"): (
        f"chart-kind-workflow_dispatch-{MAIN_SHA}",
        False,
    ),
    ("chart.yml", "push"): ("chart-refs/heads/main", True),
    ("compose-production.yml", "push"): ("compose-production-8000", True),
    ("demo-apps.yml", "push"): (f"demo-apps-push-{MAIN_SHA}", False),
    ("demo-apps.yml", "workflow_dispatch"): (
        f"demo-apps-workflow_dispatch-{MAIN_SHA}",
        False,
    ),
    ("compose-production.yml", "workflow_dispatch"): (
        "compose-production-8000",
        True,
    ),
    ("lint.yml", "push"): ("lint-refs/heads/main", True),
    ("sdk-unit-tests.yml", "schedule"): ("sdk-unit-schedule-refs/heads/main", True),
    ("sdk-unit-tests.yml", "workflow_dispatch"): (
        "sdk-unit-workflow_dispatch-refs/heads/main",
        True,
    ),
}


def test_the_main_groups_cover_every_main_trigger_of_the_changed_workflows():
    changed = {name for name, _ in MAIN_GROUPS} | {"pr-qa-gate.yml"}
    expected = set()
    for name in changed:
        for event in wg.triggers(wg.load(wg.WORKFLOWS / name)):
            if event not in PR_EVENTS:
                expected.add((name, event))
    assert expected == set(MAIN_GROUPS)


@pytest.mark.parametrize(
    "name,event", sorted(MAIN_GROUPS), ids=[f"{n}:{e}" for n, e in sorted(MAIN_GROUPS)]
)
def test_a_run_on_main_keeps_its_group(name, event):
    (only,) = _groups(wg.load(wg.WORKFLOWS / name))
    assert (
        _group_and_cancel(only[1], _main_context(event)) == MAIN_GROUPS[(name, event)]
    )


@pytest.mark.parametrize("event", ["push", "schedule", "workflow_dispatch"])
def test_two_chart_kind_runs_on_main_never_share_a_group(event):
    """Two commits on main get two groups, and neither cancels: a later merge
    must not end the run that would have shown the earlier one red (PE H2)."""
    (only,) = _groups(wg.load(wg.WORKFLOWS / "chart-kind.yml"))
    first, second = _main_context(event), _main_context(event)
    second["github"]["sha"] = "1" * 40
    group_1, cancel_1 = _group_and_cancel(only[1], first)
    group_2, cancel_2 = _group_and_cancel(only[1], second)
    assert group_1 != group_2
    assert cancel_1 is False and cancel_2 is False


def test_a_chart_kind_pull_request_run_is_still_cancelled_by_a_newer_push():
    """PR-2' changes main only: a pull request keeps one group per number, with
    cancel-in-progress, as #638 left it."""
    (only,) = _groups(wg.load(wg.WORKFLOWS / "chart-kind.yml"))
    group, cancel = _group_and_cancel(only[1], _pr_context("pull_request", 7))
    assert (group, cancel) == ("chart-kind-pull_request-7", True)


def test_a_demo_apps_pull_request_run_is_still_cancelled_by_a_newer_push():
    """Keying main on the commit leaves a pull request as it was: one group per
    number, with cancel-in-progress."""
    (only,) = _groups(wg.load(wg.WORKFLOWS / "demo-apps.yml"))
    group, cancel = _group_and_cancel(only[1], _pr_context("pull_request", 7))
    assert (group, cancel) == ("demo-apps-pull_request-7", True)


# ---------------------------------------------------------------------------
# B3
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,event", PR_WORKFLOWS, ids=[f"{n}:{e}" for n, e in PR_WORKFLOWS]
)
def test_a_run_started_by_an_edit_or_label_is_never_cancelled(name, event):
    assert b3_problems(wg.load(wg.WORKFLOWS / name), event) == []


# ---------------------------------------------------------------------------
# The checks catch what they claim to (each tamper is a defect seen in review)
# ---------------------------------------------------------------------------


def _pr_qa_with(concurrency: Any) -> Dict[str, Any]:
    workflow = wg.load(wg.WORKFLOWS / "pr-qa-gate.yml")
    return {**workflow, "concurrency": concurrency}


@pytest.mark.parametrize(
    "group",
    [
        "pr-qa-${{ github.event.pull_request.numbr }}",
        "pr-qa-${{ github.base_ref }}",
        "pr-qa",
        "lint-${{ github.head_ref || github.ref }}",
        "pr-qa-${{ github.head_ref }}",
    ],
)
def test_b1_refuses_a_group_shared_by_two_forks(group):
    tampered = _pr_qa_with({"group": group, "cancel-in-progress": True})
    assert b1_problems(tampered, "pull_request"), group


def test_b1_refuses_a_group_it_cannot_evaluate():
    tampered = _pr_qa_with({"group": "pr-qa-${{ github.run_attempt }}"})
    assert any("cannot evaluate" in p for p in b1_problems(tampered, "pull_request"))


@pytest.mark.parametrize("cancel", [True, False])
def test_b3_refuses_any_group_on_an_edit_triggered_workflow(cancel):
    workflow = wg.load(wg.WORKFLOWS / "conventional-title.yml")
    tampered = {
        **workflow,
        "concurrency": {
            "group": "conventional-title-${{ github.event.pull_request.number }}",
            "cancel-in-progress": cancel,
        },
    }
    assert b3_problems(tampered, "pull_request")
    # ... and B1 does not accept the block on its per-PR merits alone.
    assert b1_problems(tampered, "pull_request")


def test_b3_refuses_a_job_level_group():
    workflow = wg.load(wg.WORKFLOWS / "regression-guard.yml")
    jobs = {
        job_id: {**job, "concurrency": {"group": "x", "cancel-in-progress": True}}
        for job_id, job in workflow["jobs"].items()
    }
    assert b3_problems({**workflow, "jobs": jobs}, "pull_request")
