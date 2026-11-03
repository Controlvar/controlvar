"""The targeting oracle's flag is the page's example, and every row's sentence is
still on its page.

O-TARGET (``tests/ground_truth/ground_truth/targeting.py``) writes down who a
targeted flag serves from the docs alone, and
``backend/tests/unit/ground_truth/test_targeting_use_cases.py`` holds the
server to it. Two things keep the table tied to the docs rather than to the
server:

* ``DOCUMENTED_RULES`` is the ``targeting_rules`` body of the example under
  "Add targeting rules" in create.md, so a change to the example fails here and
  the table is re-read against it;
* each row (and each ``local`` leg) quotes the sentence that gives its answer,
  and the sentence must still be on its page, whitespace folded. A reworded
  page fails here, so a row cannot quietly outlive its reason, and a row
  changed to match a server regression has no sentence to point to.

It reads the oracle and the pages only, so it runs on a pull request that
changes the docs alone (``test_docs_only_gate.py``).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
LIBRARY_ROOT = REPO_ROOT / "tests" / "ground_truth"
if str(LIBRARY_ROOT) not in sys.path:
    sys.path.insert(0, str(LIBRARY_ROOT))

from ground_truth import targeting

pytestmark = pytest.mark.unit

PAGE = REPO_ROOT / targeting.CREATE
SECTION = "### Add targeting rules"


def _flat(text: str) -> str:
    return " ".join(text.split())


def _example_body() -> dict:
    """The JSON the section's first shell example sends with ``-d``."""
    text = PAGE.read_text(encoding="utf-8")
    assert text.count(SECTION + "\n") == 1, f"{PAGE.name}: no single {SECTION!r}"
    section = text.split(SECTION + "\n", 1)[1]
    section = re.split(r"\n#{2,3} ", section, maxsplit=1)[0]
    block = re.search(r"^```\{\.bash[^\n]*\n(.*?)^```$", section, re.M | re.S)
    assert block, f"{PAGE.name}: {SECTION!r} has no shell example"
    bodies = re.findall(r"-d '(\{.*?\})'", block.group(1), re.S)
    assert len(bodies) == 1, f"{PAGE.name}: {SECTION!r} sends {len(bodies)} bodies"
    return json.loads(bodies[0])


def test_the_documented_rules_are_the_pages_example():
    body = _example_body()
    assert set(body) == {"targeting_rules"}, sorted(body)
    assert body["targeting_rules"] == targeting.DOCUMENTED_RULES, (
        f"{PAGE.name}'s example is no longer DOCUMENTED_RULES: update the oracle and "
        "re-read every DOC row against the new example"
    )


@pytest.mark.parametrize("row", targeting.ROWS, ids=lambda row: row.id)
def test_the_rows_sentence_is_still_on_its_page(row):
    page = _flat((REPO_ROOT / row.basis_file).read_text(encoding="utf-8"))
    on_page = _flat(row.basis_quote) in page
    assert on_page, f"{row.id}: {row.basis_file} no longer says: {row.basis_quote}"


@pytest.mark.parametrize(
    "rows, basis_file, quote",
    targeting.LEG_BASIS,
    ids=[rows.replace(" ", "-") for rows, _, _ in targeting.LEG_BASIS],
)
def test_the_legs_sentence_is_still_on_its_page(rows, basis_file, quote):
    page = _flat((REPO_ROOT / basis_file).read_text(encoding="utf-8"))
    on_page = _flat(quote) in page
    assert on_page, f"{rows}: {basis_file} no longer says: {quote}"
