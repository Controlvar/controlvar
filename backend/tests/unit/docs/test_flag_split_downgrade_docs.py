"""Every documented downgrade target below ``t2012_flag_split`` says it refuses (#2012).

``test_documented_downgrade_targets.py`` already holds a documented target to
saying that it drops the two ``split`` columns (the revision's downgrade calls
``drop_column``). What an operator most needs to know about this downgrade is
the other thing it does: it refuses while any flag has a split or a rule value,
so a rollback to the release before stops there until they are cleared. So
wherever the self-hosting migrations page, the rollback runbook or the Database
Migration workflow's help names a target that unapplies this revision, the same
section (for the workflow, the same line) names ``t2012_flag_split`` with
"refuse", "refuses", "refused" or "refusing" within 200 characters of it. Same
reader of targets, same planner, same window.

And what each of the three says about the release still serving once the
downgrade has committed, and the page and the runbook about a downgrade that
undoes ``t2011_flag_environments`` too.
"""

from __future__ import annotations

import re
from typing import Dict, List

import pytest
import yaml

from backend.tests.unit.docs.test_documented_downgrade_targets import (
    ANNOUNCE_WINDOW,
    DB_MIGRATE,
    MIGRATIONS_PAGE,
    ROLLBACK_RUNBOOK,
    Mention,
    _markdown_sections,
    _script,
    mentions,
    unapplied,
)

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REVISION = "t2012_flag_split"
#: "refuse", "refuses", "refused" or "refusing", as a word.
_REFUSE_WORD = r"\brefus(?:e|es|ed|ing)\b"


def announces_the_refusal(context: str) -> bool:
    """Whether *context* names this revision with a refusing word within
    ``ANNOUNCE_WINDOW`` characters, in either order. Both quantifiers are
    bounded."""
    rev = re.escape(REVISION)
    window = f".{{0,{ANNOUNCE_WINDOW}}}"
    pattern = rf"\b{rev}\b{window}{_REFUSE_WORD}|{_REFUSE_WORD}{window}\b{rev}\b"
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
        if not announces_the_refusal(mention.context):
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


def test_every_target_that_crosses_this_revision_says_it_refuses():
    problems = unannounced(mentions())
    assert problems == {}, (
        f"A documented downgrade target unapplies {REVISION}, whose downgrade "
        "refuses while any flag has a split or a rule value, and the document "
        f"does not say so where it names the target: {problems}"
    )


def test_a_target_whose_section_leaves_the_revision_out_is_reported():
    """The check fires on a section that names a crossing target and says
    nothing about this revision, and on one that names it for its drop alone."""
    for page in (
        "## Undo\n\n```bash\nalembic downgrade t2011_flag_environments\n```\n",
        "## Undo\n\nThis runs t2012_flag_split's downgrade, which drops two "
        "columns.\n\n```bash\nalembic downgrade t2011_flag_environments\n```\n",
    ):
        found = [m for m in mentions(page=page) if m.source == "migrations.md"]
        assert [m.target for m in found] == ["t2011_flag_environments"]
        assert unannounced(found)["migrations.md"] == ["t2011_flag_environments"]


@pytest.mark.parametrize(
    "text",
    [
        "t2012_flag_split's downgrade refuses while a flag has a split.",
        "It refused to go on: t2012_flag_split.",
    ],
)
def test_a_refusing_word_next_to_the_revision_announces_it(text):
    assert announces_the_refusal(text)


@pytest.mark.parametrize(
    "text",
    [
        "t2012_flag_split is cited. Its refusal is described elsewhere.",
        "t2012_flag_split " + "x" * (ANNOUNCE_WINDOW + 1) + " refuses.",
    ],
)
def test_a_distant_or_missing_refusing_word_does_not_announce_it(text):
    assert not announces_the_refusal(text)


def _section(path, heading: str) -> str:
    """The section of *path* under *heading*, on one line."""
    (found,) = [
        section
        for section in _markdown_sections(path.read_text(encoding="utf-8"))
        if section.startswith(f"### {heading}\n")
    ]
    return " ".join(found.split())


#: What the runbook's and the migrations page's own sections tell an operator,
#: in the runbook's words: what is dropped, that it refuses and changes
#: nothing, when to clear, and what the release still serving answers.
RUNBOOK_SECTION = "Rolling back past flag splits"
PAGE_SECTION = (
    "`t2012_flag_split` adds a split to each flag and each flag environment setting"
)
#: What the release still serving answers, as
#: ``test_flag_split_migration.py`` measures it.
SERVING = (
    "answers `500` to every flag evaluation, flag read, flag change and ruleset "
    "request, until the API is rolled back",
    "a bulk toggle answers `200` with every flag failed",
    "the run also undoes `t2011_flag_environments`, every request that presents an "
    "API key answers `500`",
)
#: The four it answers `500` to, however the sentence around them goes.
ANSWERED = "flag evaluation, flag read, flag change and ruleset request"
#: The migrations page's section about ``t2011_flag_environments``.
ENVIRONMENTS_SECTION = (
    "`t2011_flag_environments` adds each flag's staging and development settings"
)
#: What the sections about ``t2011_flag_environments`` say: a downgrade past it
#: undoes this revision first.
FIRST = (
    "`t2012_flag_split` is undone first, so every flag read and flag change answers "
    "`500` too, the dashboard's flag list included, and a bulk toggle `200` with "
    "every flag failed"
)
#: And what else answers `500` then, as the same test measures it.
BESIDE = (
    "Archiving or deleting a segment and listing a segment's experiments answer "
    "`500`, as do the signed-in API key list, create and delete"
)


@pytest.mark.parametrize(
    "said",
    [
        "drops `feature_flags.split` and `flag_environment_settings.split`",
        "in any environment: it names those flags and changes nothing",
        "while the release that wrote them is still serving (the database is "
        "downgraded before the API is rolled back), or restore the backup taken "
        "before the upgrade",
        *SERVING,
        "with nothing changed, and is run again, or the SDK requests waiting then "
        "fail, or both",
    ],
)
def test_the_runbooks_section_says_what_an_operator_must_know(said):
    section = _section(ROLLBACK_RUNBOOK, RUNBOOK_SECTION)
    assert said in section.replace("A bulk toggle", "a bulk toggle")


@pytest.mark.parametrize(
    "said",
    [
        *SERVING,
        "with nothing changed, and is run again, or the SDK requests waiting then "
        "fail, or both",
    ],
)
def test_the_migrations_pages_section_says_it_too(said):
    section = _section(MIGRATIONS_PAGE, PAGE_SECTION)
    assert said in section.replace("A bulk toggle", "a bulk toggle")


@pytest.mark.parametrize(
    "path, heading",
    [
        (ROLLBACK_RUNBOOK, "Rolling back past flag environments"),
        (MIGRATIONS_PAGE, ENVIRONMENTS_SECTION),
    ],
    ids=["runbook", "page"],
)
def test_the_sections_about_t2011_say_that_this_revision_is_undone_first(path, heading):
    """A downgrade past it drops the split columns too, so none of the flag
    routes that revision alone would leave answering still does."""
    section = _section(path, heading)
    assert FIRST in section
    assert BESIDE in section
    assert "still answer" not in section


def test_every_place_that_says_what_is_answered_says_all_of_it():
    """The runbook says it twice: in its section, and where it lists the targets
    for the workflow. Each page names the four together, and never three."""
    said = {
        path.name: " ".join(path.read_text(encoding="utf-8").split())
        for path in (ROLLBACK_RUNBOOK, MIGRATIONS_PAGE)
    }
    assert {name: text.count(ANSWERED) for name, text in said.items()} == {
        "rollback-runbook.md": 2,
        "migrations.md": 1,
    }
    for name, text in said.items():
        assert "flag evaluation, flag read and flag change" not in text, name


@pytest.mark.skipif(
    not DB_MIGRATE.is_file(), reason="this tree has no .github/workflows"
)
def test_the_workflows_help_says_what_the_release_still_serving_answers():
    """In the target's description, inside the clause about this revision."""
    workflow = yaml.safe_load(DB_MIGRATE.read_text(encoding="utf-8"))
    # PyYAML reads the key ``on`` as the boolean True.
    description = workflow[True]["workflow_dispatch"]["inputs"]["target"]["description"]
    _, _, rest = description.partition(f"from a release that carries {REVISION} ")
    clause, undone, _ = rest.partition("t2011_flag_environments undoes it alone")
    assert undone, "the clause about this revision is gone"
    answers, toggle, also = SERVING
    once = "once it has committed, the release still serving "
    said = "; ".join([once + answers, toggle, "if " + also]).replace("`", "")
    assert said in clause, clause


def test_the_migrations_page_shows_the_refusal_the_migration_raises():
    """The sample in the page is the start of the refusal's own text."""
    revision = _script().get_revision(REVISION).module
    sample = revision._in_use_refusal(
        {"checkout-copy": ["production"], "price-tier": ["development", "staging"]}
    ).replace(f"schema {revision._SCHEMA}:", "schema experimentation:")
    (shown,) = [
        line
        for line in MIGRATIONS_PAGE.read_text(encoding="utf-8").splitlines()
        if line.startswith("Refusing to downgrade schema experimentation: t2012")
    ]
    assert shown.endswith(" ...")
    assert sample.startswith(shown.removesuffix(" ..."))
    assert shown in _section(MIGRATIONS_PAGE, PAGE_SECTION)
