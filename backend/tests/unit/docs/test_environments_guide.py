"""The flag environments guide and the pages that correct it (#2011).

Three things the pages state that nothing else holds:

* **The words.** The guide quotes what the API answers and what the dashboard
  says. ``backend/tests/unit/api/test_environment_words.py`` holds each quote
  against the code that makes it; this file holds each quote against the
  pages that carry it, so a quote edited in the guide alone fails here and a
  message edited in code alone fails there.
* **Names.** A page never calls an environment "stage": the environments are
  production, staging and development, and a stage is a step of a rollout
  schedule. The dashboard has the same rule over its source
  (``frontend/src/tests/content/environment-names.test.ts``, whose patterns
  these are). The patterns are checked against planted offenders and real
  rollout-stage sentences first, so one that stops matching fails here rather
  than passing the scan for ever.
* **The wait.** The upgrade notes say how long an SDK request can wait while
  the 0.49.0 upgrade runs: the sum of the lock bounds its revisions declare
  (``backend/tests/unit/db/test_upgrade_lock_budget.py``), so a revision that
  adds a bound moves the page's number with it.

Reads only files; no git, so it runs the same in ``scripts/core_build.sh``'s
copy. It is in the docs-only gate's "Docs content tests" through its
directory, ``backend/tests/unit/docs/``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List

import pytest

from backend.tests.unit.api.test_environment_words import (
    DASHBOARD_WORDS,
    GUIDE_QUOTES,
)
from backend.tests.unit.db.test_upgrade_lock_budget import release

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
DOCS = REPO_ROOT / "docs"
GUIDE = DOCS / "feature-flags" / "environments.md"
MIGRATIONS = DOCS / "self-hosting" / "migrations.md"
ROLLOUTS = DOCS / "feature-flags" / "rollouts.md"
KILL_SWITCH = DOCS / "feature-flags" / "kill-switch.md"
SAFETY = DOCS / "feature-flags" / "safety.md"

#: The pages that gained an "Environments" heading, which the guide links to.
CORRECTED = (ROLLOUTS, SAFETY, KILL_SWITCH)
#: The guide's heading the corrected pages send a reader to.
PRODUCTION_ONLY = "what-runs-in-production-only"


def _flat(text: str) -> str:
    """One line, with the markup a quote may sit in taken out."""
    return " ".join(text.replace("`", "").replace("*", "").split())


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The words
# ---------------------------------------------------------------------------
def test_the_guide_quotes_each_word_the_code_says():
    page = _flat(_read(GUIDE))
    missing = [quote for quote in GUIDE_QUOTES if quote not in page]
    assert not missing, (
        "the guide no longer says these word for word, which the code and the "
        f"dashboard do (test_environment_words.py holds them): {missing}"
    )


PRINTS = re.compile(r"It prints `(?P<text>[^`]+)`")
EXPECT = re.compile(r"^<!-- expect: (?P<text>.+) -->$", re.MULTILINE)


def test_each_it_prints_sentence_says_what_a_block_is_expected_to_print():
    """The reader sees the sentence, never the ``expect`` comment that the
    example runner checks, so the sentence must be one of those lines."""
    page = _read(GUIDE)
    printed = [match.group("text") for match in PRINTS.finditer(page)]
    expected = {match.group("text") for match in EXPECT.finditer(page)}
    assert len(printed) >= 3, "the page has lost its It prints sentences"
    drifted = [text for text in printed if text not in expected]
    assert not drifted, f"the sentence and the expect line differ: {drifted}"


def test_the_it_prints_pattern_reads_a_planted_sentence():
    found = PRINTS.finditer("It prints `a b`. It prints `c`.")
    assert [match.group("text") for match in found] == ["a b", "c"]


def test_rollouts_quotes_the_production_only_line_the_dashboard_shows():
    (note,) = [
        word.quote
        for word in DASHBOARD_WORDS
        if word.quote.startswith("Rollout schedules, safety monitoring")
    ]
    assert note in _flat(_read(ROLLOUTS))


def _slugs(path: Path) -> set:
    return {
        re.sub(r"[^\w\- ]", "", line.lstrip("#").strip().lower()).replace(" ", "-")
        for line in _read(path).splitlines()
        if line.startswith("#")
    }


@pytest.mark.parametrize("page", CORRECTED, ids=lambda page: page.name)
def test_the_guide_and_each_corrected_page_link_headings_that_exist(page: Path):
    """The guide links each corrected page's "Environments" heading, and
    the pages that send a reader back name the guide's own: a heading that is
    renamed leaves a link the strict docs build is the first to refuse."""
    assert "environments" in _slugs(page), f"{page.name} has no Environments heading"
    assert f"{page.name}#environments" in _read(GUIDE), page.name
    if page is not KILL_SWITCH:
        assert f"environments.md#{PRODUCTION_ONLY}" in _read(page), page.name
    assert PRODUCTION_ONLY in _slugs(GUIDE)


# ---------------------------------------------------------------------------
# Names: no "stage" for an environment
# ---------------------------------------------------------------------------
#: Each shape of "stage" used as an environment name. Case-insensitive. The
#: dashboard's own list, in the same order.
STAGE_AS_ENVIRONMENT: List[str] = [
    r"\b(?:to|in|into|from)\s+stage\b"
    r"(?!\s*(?:\d|\$\{|\{|#)|\s+[\"“'‘(]|['’]s\b|_|-)",
    r"""(['"`])stage\1""",
    r"""\benv(?:ironment)?s?\b\s*[:=]?\s*['"`]?stage\b""",
    r"/environments?/stage\b",
    r"\bstage\s+(?:environment|env|key|keys|install|instance|deployment|server)\b",
    r"\b(?:prod(?:uction)?|dev(?:elopment)?)\s*(?:,|/|\band\b|\bor\b)\s*stage\b",
    r"\bstage\s*(?:,|/|\band\b|\bor\b)\s*(?:prod(?:uction)?|dev(?:elopment)?)\b",
]
_STAGE = [re.compile(pattern, re.IGNORECASE) for pattern in STAGE_AS_ENVIRONMENT]


def stage_as_environment(line: str) -> bool:
    return any(pattern.search(line) for pattern in _STAGE)


OFFENDERS = [
    "Promote to stage",
    "Configured in stage",
    "Turn it off in the stage environment",
    "GET /api/v1/feature-flags/{id}/environments/stage",
    "create a stage key",
    "dev, stage and prod",
    "set `ENVIRONMENT` to stage",
    "environment: stage",
    '"environment": "stage"',
]
ROLLOUT_STAGES = [
    "Start stage",
    "Complete stage",
    "the next stage that starts sets the rollout percentage again",
    "sets the flag to 50% in stage 2",
    "Advance to stage “Half”",
    "Minimum stage duration (hours)",
    "Stage 3 is time-based but follows a manual stage",
    "Staging and production",
]


@pytest.mark.parametrize("line", OFFENDERS)
def test_the_patterns_fire_on_a_planted_offender(line: str):
    assert stage_as_environment(line), line


@pytest.mark.parametrize("line", ROLLOUT_STAGES)
def test_the_patterns_leave_a_rollout_stage_alone(line: str):
    assert not stage_as_environment(line), line


def _pages() -> Dict[str, str]:
    pages = {
        path.relative_to(REPO_ROOT).as_posix(): _read(path)
        for path in sorted(DOCS.rglob("*.md"))
        if "_claims" not in path.relative_to(DOCS).parts
    }
    assert len(pages) > 100, "the walk found almost nothing: it is broken, not clean"
    return pages


def test_no_page_calls_an_environment_stage():
    hits = [
        f"{name}:{number}: {line.strip()[:120]}"
        for name, text in _pages().items()
        for number, line in enumerate(text.splitlines(), 1)
        if stage_as_environment(line)
    ]
    assert not hits, (
        'these call an environment "stage"; the environments are production, '
        "staging and development, and a stage is a step of a rollout:\n"
        + "\n".join(hits)
    )


#: What promotion never copies, as the guide names it. The guide states no
#: closed list of what it does copy: a later change may add a field.
NOT_COPIED = ("value_type", "on_value", "off_value")


def _promotion_section() -> str:
    text = _read(GUIDE)
    start = text.index("## Promotion")
    return _flat(text[start : text.index("## What runs in production only")])


def test_promotion_names_each_field_it_does_not_copy_and_closes_no_list():
    from backend.app.core.flag_environments import ENVIRONMENT_FIELDS

    section = _promotion_section()
    for field in NOT_COPIED:
        assert f"{field}" in section, f"the guide no longer says {field} is not copied"
        assert field not in ENVIRONMENT_FIELDS, f"promotion now copies {field}"
    assert "rollout schedules" in section and "safety settings" in section
    assert "and nothing else" not in section


#: A rollback changes production only, so "roll back in staging" now reads as
#: an instruction to roll back the live flag. A staging installation is a
#: separate deployment, and the pages say so.
ROLLBACK_IN_STAGING = re.compile(
    r"(?:manual rollback|roll(?:s|ed)? back (?:a|the|your) flag)[^.\n]{0,40}\bin staging\b",
    re.IGNORECASE,
)


@pytest.mark.parametrize(
    "line",
    [
        "trigger a manual rollback in staging to confirm that the flag turns off",
        "Roll back the flag in staging first",
        "roll back your flag in staging",
    ],
)
def test_the_rollback_pattern_fires_on_a_planted_instruction(line: str):
    assert ROLLBACK_IN_STAGING.search(line), line


def test_no_page_tells_a_reader_to_roll_back_in_staging():
    hits = [
        f"{name}: {match.group(0)}"
        for name, text in _pages().items()
        for match in ROLLBACK_IN_STAGING.finditer(_flat(text))
    ]
    assert not hits, (
        "a rollback changes production settings only; say a staging installation "
        f"(a separate deployment) instead: {hits}"
    )


# ---------------------------------------------------------------------------
# The wait
# ---------------------------------------------------------------------------
WAIT = re.compile(
    r"an SDK request can wait up to (?P<seconds>[0-9]+(?:\.[0-9]+)?) seconds"
)


def test_the_upgrade_note_gives_the_sum_of_the_declared_lock_bounds():
    """The note tells an operator how long an SDK request can wait while the
    0.49.0 upgrade runs. That is every bound the release's revisions declare,
    added: they run in one transaction. A revision that adds one moves this
    number, and this test is where that is noticed."""
    found = WAIT.findall(_flat(_read(MIGRATIONS)))
    assert len(found) == 1, f"the note is missing, or there are several: {found}"
    declared = sum(release("0.49.0").declared.values())
    assert float(found[0]) == declared, (
        f"migrations.md says {found[0]} seconds; the revisions of 0.49.0 declare "
        f"{declared} in all (test_upgrade_lock_budget.py)"
    )


def test_the_planted_wait_is_read_as_the_note_is_written():
    """Positive control: the sentence the note uses, wrapped across lines."""
    text = _flat("While it runs, an SDK\nrequest can wait up to 3.25 seconds; an SDK")
    assert [match.group("seconds") for match in WAIT.finditer(text)] == ["3.25"]
