"""The operator's section for ``1ab99332f0ba``, which rewrites event times (#579).

The revision rewrites stored ``events.created_at`` values to UTC.  Its section
of the self-hosting migrations page is what an operator follows before, during
and after the upgrade, so this pins, without a database (it runs on a
docs-only pull request):

* the section exists under a heading whose anchor the page's two links use (the
  generic reversibility note and the rollback bullet);
* both SQL statements in it select with the migration's own candidate
  predicate, character for character -- the count before the upgrade and the
  list after it would otherwise disagree with what the migration did;
* the log line it quotes has the shape the revision prints;
* the re-run recipe names ``a89544fb1075`` and never ``-1`` or ``^``;
* it says the downgrade cannot restore the offsets, to snapshot first, and not
  to kill a long run, per deployment.

Whether that SQL selects exactly the migration's candidates is run against
PostgreSQL by
``backend/tests/integration/database/test_events_created_at_utc_migration.py``,
which reads the blocks through :func:`sql_blocks` here.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from backend.tests.unit.db.test_events_created_at_utc_normaliser import (
    load_revision,
)
from backend.tests.unit.docs.test_email_case_runbook import headings, slugify

pytestmark = [pytest.mark.unit]

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
PAGE = REPO_ROOT / "docs" / "self-hosting" / "migrations.md"

ANCHOR = "1ab99332f0ba-rewrites-stored-event-times-to-utc"
#: The labels (first lines) of the section's two SQL statements.
COUNT = "-- Count the event times the upgrade will rewrite"
LEFT = "-- List the event times left after the upgrade"

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_SQL_BLOCK = re.compile(r"^[ \t]*```sql\n(.*?)^[ \t]*```", re.M | re.S)
_TEXT_BLOCK = re.compile(r"^```text\n(.*?)^```", re.M | re.S)
#: A shell block, untagged or in the form Doc Examples runs (#1075).
_BASH_BLOCK = re.compile(r"^```(?:bash|\{\.bash [^}\n]*\})\n(.*?)^```", re.M | re.S)
#: Where the section's prose ends: the next paragraph of "Rolling Back".
_END = "Roll back to a specific revision:"


def section(text: str | None = None) -> str:
    """The page from this revision's heading to the end of its subsection."""
    text = PAGE.read_text(encoding="utf-8") if text is None else text
    lines = text.splitlines(keepends=True)
    start = level = None
    for i in range(len(lines)):
        line = lines[i]
        match = _HEADING.match(line)
        if start is None:
            if match and slugify(match.group(2)) == ANCHOR:
                start, level = i, len(match.group(1))
            continue
        if (match and len(match.group(1)) <= level) or line.startswith(_END):
            return "".join(lines[start:i])
    assert start is not None, f"no heading slugifies to {ANCHOR!r}"
    return "".join(lines[start:])


def sql_blocks(text: str | None = None) -> dict[str, str]:
    """The section's ```sql blocks, by their first-line label."""
    blocks = {}
    for body in _SQL_BLOCK.findall(section(text)):
        sql = body.strip()
        label = sql.splitlines()[0]
        assert label not in blocks, f"two blocks are labelled {label!r}"
        blocks[label] = sql
    return blocks


def _prose() -> str:
    return " ".join(section().split())


@pytest.mark.regression
def test_the_section_is_there_and_both_links_reach_it():
    text = PAGE.read_text(encoding="utf-8")
    slugs = [slugify(title) for _, title in headings(text)]
    assert ANCHOR in slugs
    assert text.count(f"](#{ANCHOR})") == 2


@pytest.mark.regression
def test_both_statements_select_with_the_migrations_predicate_exactly():
    predicate = load_revision()._CANDIDATES
    blocks = sql_blocks()

    assert list(blocks) == [COUNT, LEFT]
    for label, sql in blocks.items():
        assert sql.count("WHERE ") == 1, label
        assert sql.endswith("WHERE " + predicate + ";"), (label, sql, predicate)
    assert "SELECT count(*) FROM experimentation.events\n" in blocks[COUNT]
    assert "SELECT id FROM experimentation.events\n" in blocks[LEFT]


def test_the_quoted_log_line_has_the_shape_the_revision_prints():
    revision = load_revision()
    printed = revision._report(1520, 2, ["a", "b"], 4.3)
    shape = re.escape(printed).replace("a,\\ b", r"[0-9a-f-]{36},\ [0-9a-f-]{36}")
    (quoted,) = [
        line
        for block in _TEXT_BLOCK.findall(section())
        for line in block.splitlines()
        if line.startswith(revision.revision)
    ]
    assert re.fullmatch(shape, quoted), (shape, quoted)


@pytest.mark.regression
def test_the_rerun_recipe_names_the_parent_revision_and_never_minus_one():
    (recipe,) = _BASH_BLOCK.findall(section())
    assert recipe.splitlines() == [
        "docker compose exec api python -m alembic -c backend/app/db/alembic.ini"
        " downgrade a89544fb1075",
        "docker compose exec api python -m alembic -c backend/app/db/alembic.ini"
        " upgrade heads",
    ]
    assert load_revision().down_revision == "a89544fb1075"
    prose = _prose()
    assert "direction `downgrade` with target `a89544fb1075`" in prose
    assert "do not use `-1`" in prose
    assert "^" not in recipe


@pytest.mark.regression
def test_the_irreversible_line_and_the_do_not_kill_lines_are_stated():
    prose = _prose()
    assert "**The downgrade cannot bring back the offsets clients sent.**" in prose
    assert "take a snapshot of the database before upgrading" in prose
    assert "**A long run is not a failed run. Do not kill it.**" in prose
    assert "Do not restart it" in prose
    assert "Do not delete the pod" in prose
    assert "**Migration still running**" in prose
    assert "run the Deploy workflow again for the same tag" in prose
    assert "no setting changes that for one deploy" in prose
    assert "the first measure of it is your own staging run's log line" in prose
    assert "use the migration's log line instead of the queries above" in prose
    assert "first-conversion day in the daily results can move by one day" in prose


def test_a_renamed_heading_breaks_the_anchor():
    """The tamper, kept: reworded, the heading no longer has the linked anchor."""
    renamed = PAGE.read_text("utf-8").replace(
        "rewrites stored event times to UTC\n", "rewrites event times to UTC\n"
    )
    with pytest.raises(AssertionError, match="no heading slugifies"):
        section(renamed)
