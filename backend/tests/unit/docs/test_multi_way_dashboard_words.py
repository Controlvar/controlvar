"""The dashboard words the multi-way flags guide quotes, held to the dashboard (#2012).

``docs/feature-flags/multi-way.md`` walks through the flag page's Split
section and quotes what it says. A reader searches the page for what the
screen said, so a word changed in the dashboard alone leaves the guide
describing words nobody sees, and a word changed in the guide alone describes
a screen that does not exist.

``DASHBOARD_WORDS`` is the one list of what the guide quotes. Each entry is
held twice: its literal is in the source file that renders it, and the quote
is on the page. The words live in ``frontend/src/utils/flagSplit.ts``, the
dashboard's one file of split words; the jest tests that render them are what
the claims registry names.

Reads only files; no git, so it runs the same in ``scripts/core_build.sh``'s
copy. It is in the docs-only gate's "Docs content tests" through its
directory, ``backend/tests/unit/docs/``.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, NamedTuple

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
GUIDE = REPO_ROOT / "docs" / "feature-flags" / "multi-way.md"
WORDS = "frontend/src/utils/flagSplit.ts"
SECTION = "frontend/src/components/feature-flags/FlagSplit.tsx"
LIST = "frontend/src/pages/feature-flags/index.tsx"
LABELS = "frontend/src/utils/flagEnvironments.ts"


class Word(NamedTuple):
    """A dashboard word the guide quotes, and where its literal is."""

    quote: str
    source: str
    #: What the source holds, as written there: a plain string, or a template
    #: whose parts are checked one by one.
    literals: tuple


#: Every dashboard word the guide quotes, with the literal that makes it.
DASHBOARD_WORDS: List[Word] = [
    Word(
        "Split",
        WORDS,
        ("environmentLabel ? `Split in ${environmentLabel}` : 'Split'",),
    ),
    Word("Split in Staging", WORDS, ("`Split in ${environmentLabel}`",)),
    Word("Split in Staging", LABELS, ("staging: 'Staging'",)),
    Word("Add a split", WORDS, ("ADD_SPLIT = 'Add a split'",)),
    Word("Value", WORDS, ("VALUE_COLUMN = 'Value'",)),
    Word("Weight (%)", WORDS, ("WEIGHT_COLUMN = 'Weight (%)'",)),
    Word("Add value", WORDS, ("ADD_VALUE = 'Add value'",)),
    Word("Remove", WORDS, ("REMOVE_VALUE = 'Remove'",)),
    Word("Spread evenly", WORDS, ("SPREAD_EVENLY = 'Spread evenly'",)),
    Word(
        "Total 100%",
        WORDS,
        (
            "if (total === SPLIT_TOTAL) return `Total ${total}%`;",
            "SPLIT_TOTAL = 100",
        ),
    ),
    Word(
        "Total 90%: 10% more needed",
        WORDS,
        ("return `Total ${total}%: ${SPLIT_TOTAL - total}% more needed`;",),
    ),
    Word(
        "Weights must add up to 100; they add up to 90.",
        WORDS,
        ("`Weights must add up to ${SPLIT_TOTAL}; they add up to ${total}.`",),
    ),
    Word("Remove the split", WORDS, ("REMOVE_SPLIT = 'Remove the split'",)),
    Word(
        "Users these rules match get",
        WORDS,
        ("RULE_SERVE_LABEL = 'Users these rules match get'",),
    ),
    Word(
        "A value of the split (the default)",
        WORDS,
        ("RULE_SERVE_DEFAULT_SPLIT = 'A value of the split (the default)'",),
    ),
    Word(
        "The on value (the default)",
        WORDS,
        ("RULE_SERVE_DEFAULT_ON = 'The on value (the default)'",),
    ),
    Word(
        "A value of their own",
        WORDS,
        ("RULE_SERVE_OWN = 'A value of their own'",),
    ),
    Word(
        "Serving 3 values",
        WORDS,
        ("count === 1 ? 'Serving 1 value' : `Serving ${count} values`",),
    ),
    Word("Split", WORDS, ("SPLIT_BADGE = 'Split'",)),
    Word("Split", LIST, ("{SPLIT_BADGE}",)),
    Word(
        "A split does not measure anything. To compare values on a metric, "
        "create an experiment.",
        WORDS,
        (
            "SPLIT_MEASURES_TEXT = 'A split does not measure anything. To "
            "compare values on a metric,'",
            "SPLIT_EXPERIMENT_LINK = 'create an experiment'",
        ),
    ),
    Word(
        "A split does not measure anything. To compare values on a metric, "
        "create an experiment.",
        SECTION,
        (
            "{SPLIT_MEASURES_TEXT}{' '}",
            "{SPLIT_EXPERIMENT_LINK}",
            '<Link href="/experiments"',
        ),
    ),
]


def _flat(text: str) -> str:
    """One line, with the markup a quote may sit in taken out."""
    return " ".join(text.replace("`", "").replace("*", "").split())


@pytest.mark.parametrize(
    "word", DASHBOARD_WORDS, ids=lambda word: f"{word.quote} ({word.source})"
)
def test_each_quoted_word_is_in_the_dashboard(word: Word):
    text = (REPO_ROOT / word.source).read_text(encoding="utf-8")
    missing = [literal for literal in word.literals if literal not in text]
    assert not missing, (
        f"the guide quotes {word.quote!r}, which {word.source} no longer says "
        f"as {missing}: change the guide with it"
    )


@pytest.mark.parametrize("quote", sorted({word.quote for word in DASHBOARD_WORDS}))
def test_the_guide_quotes_each_word_the_dashboard_says(quote: str):
    page = _flat(GUIDE.read_text(encoding="utf-8"))
    assert quote in page, (
        f"the guide no longer says {quote!r}, which the dashboard does: change "
        "the guide and this list together"
    )


def test_the_list_quotes_something_and_every_source_exists():
    assert len(DASHBOARD_WORDS) >= 20
    for word in DASHBOARD_WORDS:
        assert (REPO_ROOT / word.source).is_file(), word.source
