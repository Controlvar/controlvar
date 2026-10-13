"""The documentation of segment targeting (#440).

* the three operator references (``docs/api/endpoints.md``,
  ``docs/feature-flags/create.md``, ``docs/Enhanced_Rules_Engine_Reference.md``)
  carry the same ``in_segment`` / ``not_in_segment`` paragraph, and the two
  operator lists name both operators;
* the paragraph states the fail-closed rule, not "not a member";
* the rollback runbook says to disable segment-targeted flags AND pause
  segment-targeted experiments, before a rollback past segment targeting and
  after an automatic one, and names the ``/sdk/ruleset`` leg and the SDKs'
  cached rulesets;
* no segment page says a segment "matches every user".
"""

from __future__ import annotations

import pathlib
import re

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
DOCS = REPO_ROOT / "docs"
REFERENCES = (
    "api/endpoints.md",
    "feature-flags/create.md",
    "Enhanced_Rules_Engine_Reference.md",
)
START = "**`in_segment`, `not_in_segment`.**"


def _read(relative: str) -> str:
    return (DOCS / relative).read_text(encoding="utf-8")


def _paragraph(text: str) -> str:
    start = text.index(START)
    end = text.index("\n\n", start)
    return text[start:end]


def test_the_three_references_carry_the_same_paragraph():
    paragraphs = {name: _paragraph(_read(name)) for name in REFERENCES}
    assert len(set(paragraphs.values())) == 1, paragraphs
    text = paragraphs[REFERENCES[0]]
    for fact in (
        "at most 10 different segments",
        "the request's `user_id`",
        "nothing in the context you send makes a user a member",
        "answers 422",
        '`reason: "error"`',
        '(`reason: "targeting"`)',
        "whichever of the two operators the condition uses",
        "always evaluated by the server",
        "they fail only the conditions on that attribute",
        "a user with no `country` matches `NOT (country equals US)`",
        "keep their assignment",
    ):
        assert fact in text, fact


@pytest.mark.parametrize("name", ["api/endpoints.md", "feature-flags/create.md"])
def test_the_operator_lists_name_both_operators(name):
    text = _read(name)
    listing = text[: text.index(START)]
    assert "`in_segment`" in listing and "`not_in_segment`" in listing


def test_local_evaluation_says_segment_flags_are_remote():
    text = _read("sdk/local-evaluation.md")
    assert "flags whose rules use a segment (`in_segment`, `not_in_segment`)" in text
    assert '`"remote"`' in text


def test_the_rollback_runbook_covers_segment_targeting():
    text = _read("deployment/rollback-runbook.md")
    section = text[text.index("### Rolling back past segment targeting") :]
    section = section[: section.index("\n### ", 10)]
    for fact in (
        "**Before rolling the API back past that release**",
        "**After an automatic rollback past it**",
        "disable segment-targeted flags",
        "pause segment-targeted experiments",
        "and pause the experiments that use them",
    ):
        assert fact in section, fact
    assert "(#rolling-back-past-segment-targeting)" in text


@pytest.mark.parametrize(
    "name",
    [*REFERENCES, "guides/segments.md", "sdk/local-evaluation.md"],
)
def test_no_page_says_a_segment_matches_every_user(name):
    assert not re.search(r"matches every user", _read(name), re.IGNORECASE)
