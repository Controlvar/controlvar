"""Every required check has exactly one producer on a pull request's head commit (A10).

Branch protection matches a required check by NAME on the head commit. A
same-repository pull request's head commit collects check runs from two
events: ``pull_request``, and the ``push`` of its branch. If two jobs report
the same required name there -- two workflows, or one workflow fired by both
events -- protection can be satisfied by the one that is not the real gate.

``Leak Guard`` was exactly that: one job, triggered by ``push: ["**"]`` and by
``pull_request``, both reporting ``Leak Guard``. Only the pull_request run
reads the title and body. It now names itself per event
(``Leak Guard (push)`` on a push), and this test evaluates every job name per
event to keep it that way.

A job's own ``if:`` is not consulted: a skipped job still reports its name, and
protection accepts a skipped check. So splitting a job into two
event-conditioned jobs that share a name fails here, as it must.

Names come from ``REQUIRED_CHECKS`` in ``scripts/configure-repo.sh``, the list
that script writes to protection; matrix names and ``caller / callee (...)``
names of reusable workflows are expanded the way GitHub expands them.
"""

from __future__ import annotations

import pytest

from backend.tests.unit.infrastructure import _workflow_graph as wg

pytestmark = [pytest.mark.unit, pytest.mark.regression]

if not wg.CONFIGURE_REPO.is_file() or not wg.WORKFLOWS.is_dir():
    pytest.skip("this tree has no .github/workflows", allow_module_level=True)

#: Jobs whose name exists only at run time (a matrix read from a previous
#: job's output). None of them may be a required check; a new one fails here
#: until it is looked at and added.
DYNAMIC_NAMES = {
    ("pull_request", "doc-examples.yml", "examples"),
    ("pull_request", "sdk-unit-tests.yml", "linux"),
    # The measurement's matrix comes from its plan job; it runs only on a
    # dispatch (its plan job's if:), and is never a required check.
    ("pull_request", "scale-evidence.yml", "measure"),
}


@pytest.fixture(scope="module")
def producers():
    return wg.head_commit_producers()


@pytest.mark.regression
def test_each_required_name_has_one_producer(producers):
    by_name, _ = producers
    problems = []
    for name in wg.required_checks():
        found = by_name.get(name, [])
        if len(found) != 1:
            problems.append(f"{name} has {len(found)} producers: {found}")
        elif found[0][0] != "pull_request":
            problems.append(
                f"{name} is produced only by {found[0]}, not by a pull_request run"
            )
    assert not problems, "\n".join(problems)


@pytest.mark.regression
def test_names_known_only_at_run_time_are_the_known_ones(producers):
    _, unresolved = producers
    assert set(unresolved) == DYNAMIC_NAMES, (
        "a job's name can no longer be read from the workflow, so it cannot be "
        f"checked against the required names: {sorted(set(unresolved) ^ DYNAMIC_NAMES)}"
    )


@pytest.mark.regression
def test_the_guard_names_itself_per_event():
    """The push run still exists (it is the only scan of a branch with no pull
    request), under a name that is not required."""
    path = wg.WORKFLOWS / "leak-guard.yml"
    on = wg.triggers(wg.load(path))
    assert on.get("push") == {"branches": ["**"]}, on
    assert "pull_request" in on, on
    assert dict(wg.check_names(path, "pull_request")) == {"leak-guard": "Leak Guard"}
    assert dict(wg.check_names(path, "push")) == {"leak-guard": "Leak Guard (push)"}


# --------------------------------------------------------------------------
# The reader itself: a census that cannot see a name proves nothing.
# --------------------------------------------------------------------------


def test_required_checks_are_read():
    names = wg.required_checks()
    assert (
        "Leak Guard" in names
        and "SDK Live Contract / sdk-live-contract (core)" in names
    )
    assert len(names) == len(set(names)), names


@pytest.mark.parametrize(
    "event, expected",
    [("push", "Leak Guard (push)"), ("pull_request", "Leak Guard")],
)
def test_the_event_dependent_name_evaluates(event, expected):
    template = (
        "${{ github.event_name == 'push' && 'Leak Guard (push)' || 'Leak Guard' }}"
    )
    assert wg.render(template, {"github": {"event_name": event}}) == expected


def test_matrix_and_reusable_names_are_expanded(producers):
    by_name, _ = producers
    # Two plain jobs since #869 (they were one `${{ matrix.profile }}-build`
    # matrix); each reports under its id, from pr-qa-gate.yml alone.
    assert by_name.get("core-build", []) == [
        ("pull_request", "pr-qa-gate.yml", "core-build")
    ]
    assert by_name.get("full-build", []) == [
        ("pull_request", "pr-qa-gate.yml", "full-build")
    ]
    assert [
        p[2] for p in by_name.get("SDK Live Contract / sdk-live-contract (core)", [])
    ] == ["sdk-live-contract"]
    # A job with no name reports under its id.
    assert [p[1] for p in by_name.get("integration-tests", [])] == [
        "integration-tests.yml"
    ]


def test_a_static_matrix_name_is_expanded(tmp_path):
    """The reader's matrix expansion, which no required name exercises since
    `profile-build` became two jobs: kept honest on a planted workflow, so a
    future matrix that renders a required name twice is still seen twice."""
    path = tmp_path / "matrix.yml"
    path.write_text(
        "on: pull_request\n"
        "jobs:\n"
        "  profile-build:\n"
        "    name: ${{ matrix.profile }}-build\n"
        "    runs-on: ubuntu-latest\n"
        "    strategy:\n"
        "      matrix:\n"
        "        include:\n"
        "          - profile: core\n"
        "          - profile: core\n"
        "          - profile: full\n"
        "    steps: [{run: 'true'}]\n",
        encoding="utf-8",
    )
    assert [name for _, name in wg.check_names(path, "pull_request")] == [
        "core-build",
        "core-build",
        "full-build",
    ]


def test_a_push_to_main_only_workflow_does_not_reach_a_pr_head():
    assert not wg.fires({"on": {"push": {"branches": ["main"]}}}, "push")
    assert wg.fires({"on": {"push": {"branches": ["**"]}}}, "push")
    assert not wg.fires(
        {"on": {"push": {"branches": ["*"]}}}, "push"
    )  # `*` stops at `/`
    assert not wg.fires({"on": {"push": {"tags": ["v*"]}}}, "push")
    assert wg.fires({"on": {"push": {"paths": ["x/**"]}}}, "push")
