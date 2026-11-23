"""Every documented downgrade target below ``t449_identity_aliases`` says it drops the links (#449).

``test_documented_downgrade_targets.py`` holds a documented target to naming
each revision it crosses whose ``downgrade()`` calls ``drop_column``.
``t449_identity_aliases`` drops its table with ``drop_table`` alone, which
that reader does not see, so its announcement is asserted here by name, with
the same reader of targets, the same planner and the same window: wherever the
self-hosting migrations page, the rollback runbook or the Database Migration
workflow's help names a target that unapplies this revision, the same section
(for the workflow, the same line) names ``t449_identity_aliases`` with "drop",
"drops", "dropped" or "dropping" within 200 characters of it.

The three documents also say what the downgrade does not touch and what an
upgrade afterwards starts from; their registries name the database test that
holds those sentences.
"""

from __future__ import annotations

from typing import Dict, List

import pytest

from backend.tests.unit.docs.test_documented_downgrade_targets import (
    DB_MIGRATE,
    Mention,
    _script,
    announces,
    mentions,
    unapplied,
)

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REVISION = "t449_identity_aliases"


def crossings(found: List[Mention]) -> List[Mention]:
    """The documented targets whose downgrade unapplies this revision."""
    script = _script()
    known = {r.revision for r in script.walk_revisions()}
    return [
        mention
        for mention in found
        if mention.target in known and REVISION in unapplied(script, mention.target)
    ]


def unannounced(found: List[Mention]) -> Dict[str, List[str]]:
    """``{"source": [targets]}`` that cross this revision and do not say so."""
    problems: Dict[str, List[str]] = {}
    for mention in crossings(found):
        if not announces(mention.context, REVISION):
            targets = problems.setdefault(mention.source, [])
            if mention.target not in targets:
                targets.append(mention.target)
    return problems


def test_each_document_names_a_target_that_crosses_this_revision():
    """Without this, a reader that found no crossing would pass the check
    below having checked nothing."""
    sources = {mention.source for mention in crossings(mentions())}
    expected = {"migrations.md", "rollback-runbook.md"}
    if DB_MIGRATE.is_file():
        expected.add("db-migrate.yml")
    assert sources == expected, sources


def test_every_target_that_crosses_this_revision_says_it_drops_the_links():
    problems = unannounced(mentions())
    assert problems == {}, (
        f"A documented downgrade target unapplies {REVISION}, whose downgrade "
        "drops identity_aliases and every link in it, and the document does "
        f"not say so where it names the target: {problems}"
    )


def test_a_target_whose_section_leaves_the_revision_out_is_reported():
    """The check fires on a section that names a crossing target and says
    nothing about this revision."""
    page = "## Undo\n\n```bash\nalembic downgrade 4955571d232c\n```\n"
    found = [m for m in mentions(page=page) if m.source == "migrations.md"]
    assert [m.target for m in found] == ["4955571d232c"]
    assert unannounced(found)["migrations.md"] == ["4955571d232c"]
