"""Every documented downgrade target below ``t2011_flag_environments`` says it deactivates the keys (#2011).

``test_documented_downgrade_targets.py`` already holds a documented target to
saying that it drops ``api_keys.environment`` (the revision's downgrade calls
``drop_column``). What an operator most needs to know about this downgrade is
the other thing it does: it makes every staging and development API key
inactive, so those keys answer 401 afterwards, and an upgrade does not bring
them back. So wherever the self-hosting migrations page, the rollback runbook
or the Database Migration workflow's help names a target that unapplies this
revision, the same section (for the workflow, the same line) names
``t2011_flag_environments`` with "deactivate", "deactivates", "deactivated"
or "deactivating" within 200 characters of it. Same reader of targets, same
planner, same window.
"""

from __future__ import annotations

import re
from typing import Dict, List

import pytest

from backend.tests.unit.docs.test_documented_downgrade_targets import (
    ANNOUNCE_WINDOW,
    DB_MIGRATE,
    Mention,
    _script,
    mentions,
    unapplied,
)

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REVISION = "t2011_flag_environments"
#: "deactivate", "deactivates", "deactivated" or "deactivating", as a word.
_DEACTIVATE_WORD = r"\bdeactivat(?:e|es|ed|ing)\b"


def announces_deactivation(context: str) -> bool:
    """Whether *context* names this revision with a deactivation word within
    ``ANNOUNCE_WINDOW`` characters, in either order. Both quantifiers are
    bounded."""
    rev = re.escape(REVISION)
    window = f".{{0,{ANNOUNCE_WINDOW}}}"
    pattern = (
        rf"\b{rev}\b{window}{_DEACTIVATE_WORD}|{_DEACTIVATE_WORD}{window}\b{rev}\b"
    )
    return re.search(pattern, context, re.S | re.I) is not None


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
        if not announces_deactivation(mention.context):
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


def test_every_target_that_crosses_this_revision_says_it_deactivates_the_keys():
    problems = unannounced(mentions())
    assert problems == {}, (
        f"A documented downgrade target unapplies {REVISION}, whose downgrade "
        "deactivates every staging and development API key, and the document "
        f"does not say so where it names the target: {problems}"
    )


def test_a_target_whose_section_leaves_the_revision_out_is_reported():
    """The check fires on a section that names a crossing target and says
    nothing about this revision, and on one that names it for its drop alone."""
    for page in (
        "## Undo\n\n```bash\nalembic downgrade t449_identity_aliases\n```\n",
        "## Undo\n\nThis runs t2011_flag_environments's downgrade, which drops a "
        "column.\n\n```bash\nalembic downgrade t449_identity_aliases\n```\n",
    ):
        found = [m for m in mentions(page=page) if m.source == "migrations.md"]
        assert [m.target for m in found] == ["t449_identity_aliases"]
        assert unannounced(found)["migrations.md"] == ["t449_identity_aliases"]


@pytest.mark.parametrize(
    "text",
    [
        "t2011_flag_environments's downgrade deactivates every staging key.",
        "It deactivated those keys: t2011_flag_environments.",
    ],
)
def test_a_deactivation_word_next_to_the_revision_announces_it(text):
    assert announces_deactivation(text)


@pytest.mark.parametrize(
    "text",
    [
        "t2011_flag_environments is cited. Keys are inactive.",
        "t2011_flag_environments " + "x" * (ANNOUNCE_WINDOW + 1) + " deactivates.",
    ],
)
def test_a_distant_or_missing_deactivation_word_does_not_announce_it(text):
    assert not announces_deactivation(text)
