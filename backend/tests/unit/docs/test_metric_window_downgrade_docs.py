"""What the documents say about undoing ``t1934_metric_window`` (#1934).

``test_documented_downgrade_targets.py`` already holds every documented target
that unapplies this revision to saying that its downgrade drops something (it
calls ``drop_column``). This file holds the rest of what an operator reads:
what the release still serving answers once the downgrade has committed, in
each of the three documents (for the workflow, inside its clause about this
revision), and that the sections about the revisions below it say this one is
undone first. The answers are the ones
``test_metric_window_migration.py::test_this_release_answers_500_where_it_reads_a_metric_on_the_downgraded_schema``
measures.
"""

from __future__ import annotations

import pytest
import yaml

from backend.tests.unit.docs.test_documented_downgrade_targets import (
    DB_MIGRATE,
    MIGRATIONS_PAGE,
    ROLLBACK_RUNBOOK,
    _markdown_sections,
)

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REVISION = "t1934_metric_window"

#: What answers ``500``, however the sentence around it goes.
FAILING = "the experiment list, to reading, creating, changing, pausing or cloning an experiment, and to its results"
#: The same list where the sentence says "... answer `500` too".
FAILING_TOO = (
    "the experiment list, reading, creating, changing, pausing or cloning an "
    "experiment, and its results answer `500` too"
)
#: And what is still answered.
STILL = "ssignment, event tracking, flag evaluation and the ruleset are still answered"

RUNBOOK_SECTION = "Rolling back past metric windows"
PAGE_SECTION = "`t1934_metric_window` adds a conversion window to each metric"


def _section(path, heading: str) -> str:
    """The section of *path* under *heading*, on one line."""
    (found,) = [
        section
        for section in _markdown_sections(path.read_text(encoding="utf-8"))
        if section.startswith(f"### {heading}\n")
    ]
    return " ".join(found.split())


def _paragraph(path, anchor: str) -> str:
    """The text of *path*, on one line, from where Step 2's Target bullet
    names *anchor* to the end of that line."""
    flat = " ".join(path.read_text(encoding="utf-8").split())
    start = flat.index(anchor)
    return flat[start : flat.index(" 3. Click", start)]


@pytest.mark.parametrize(
    "said",
    [
        "drops `metrics.conversion_window_hours`, every metric's conversion window",
        "It does not refuse, and nothing restores the windows",
        "the release still serving answers `500` to " + FAILING,
        "until the API is rolled back",
        "A" + STILL,
        "waits at most 0.5 s for `metrics`",
    ],
)
def test_the_runbooks_section_says_what_an_operator_must_know(said):
    assert said in _section(ROLLBACK_RUNBOOK, RUNBOOK_SECTION)


@pytest.mark.parametrize(
    "said",
    [
        "drops `metrics.conversion_window_hours`, and the check with it",
        "It does not refuse, and nothing restores the windows",
        "this release answers `500` to " + FAILING,
        "until the API is rolled back",
        "A" + STILL,
        "waits at most 0.5 s",
        "This revision's own `down_revision` is `t2012_flag_split`",
    ],
)
def test_the_migrations_pages_section_says_it_too(said):
    assert said in _section(MIGRATIONS_PAGE, PAGE_SECTION)


def test_the_runbooks_target_bullet_says_it_for_this_revision():
    clause = _paragraph(
        ROLLBACK_RUNBOOK, f"From a release that carries `{REVISION}`, whose"
    )
    for said in (
        "drops `metrics.conversion_window_hours`, every metric's conversion window",
        "does not refuse",
        "within 0.5 s on `metrics`",
        "a downgrade to `t2012_flag_split` undoes it alone",
        "the release still serving answers `500` to " + FAILING,
        "a" + STILL,
    ):
        assert said in clause, said


@pytest.mark.parametrize(
    "path, heading",
    [
        (ROLLBACK_RUNBOOK, "Rolling back past flag environments"),
        (
            MIGRATIONS_PAGE,
            "`t2011_flag_environments` adds each flag's staging and development settings",
        ),
    ],
    ids=["runbook", "page"],
)
def test_the_sections_about_t2011_say_that_this_revision_is_undone_before_t2012(
    path, heading
):
    section = _section(path, heading)
    assert f"`{REVISION}` is undone before it, so {FAILING_TOO}" in section


@pytest.mark.parametrize(
    "path, heading",
    [
        (ROLLBACK_RUNBOOK, "Rolling back past flag splits"),
        (
            MIGRATIONS_PAGE,
            "`t2012_flag_split` adds a split to each flag and each flag environment setting",
        ),
    ],
    ids=["runbook", "page"],
)
def test_the_sections_about_t2012_say_what_this_revision_adds_to_the_outage(
    path, heading
):
    section = _section(path, heading)
    assert FAILING_TOO in section
    assert f"From a release that carries `{REVISION}`" in section


@pytest.mark.skipif(
    not DB_MIGRATE.is_file(), reason="this tree has no .github/workflows"
)
def test_the_workflows_help_says_what_the_release_still_serving_answers():
    """In the target's description, inside the clause about this revision."""
    workflow = yaml.safe_load(DB_MIGRATE.read_text(encoding="utf-8"))
    # PyYAML reads the key ``on`` as the boolean True.
    description = workflow[True]["workflow_dispatch"]["inputs"]["target"]["description"]
    _, _, rest = description.partition(f"from a release that carries {REVISION} ")
    clause, undone, _ = rest.partition("t2012_flag_split undoes it alone")
    assert undone, "the clause about this revision is gone"
    assert "drops metrics.conversion_window_hours" in clause, clause
    assert "does not refuse" in clause, clause
    assert "at most 0.5 s for metrics" in clause, clause
    said = (
        "once it has committed, the release still serving answers 500 to "
        + FAILING
        + ", until the API is rolled back; a"
        + STILL
    )
    assert said in clause, clause
