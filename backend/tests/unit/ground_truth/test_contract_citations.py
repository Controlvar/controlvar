"""Each rule of the event contract quotes the docs that make it the contract,
and the quote is still on its page.

``ground_truth.contract`` states rules R0 to R7, each in one function whose
docstring carries one or more ``Contract (<page>): <sentence>`` paragraphs. If
a page is reworded, the rule no longer cites what the docs say and this fails,
so the rule is re-read against the new text rather than left to drift. It
reads the module as text and the pages from ``docs/``, so it runs on a
pull request that changes ``docs/`` alone (``test_docs_only_gate.py``).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
CONTRACT = REPO_ROOT / "tests" / "ground_truth" / "ground_truth" / "contract.py"
CITATION = re.compile(r"^Contract \((?P<page>docs/[^)]+\.md)\): (?P<quote>.+)$", re.S)
RULES = [f"R{n}" for n in range(8)]


def _flat(text: str) -> str:
    return " ".join(text.split())


def _citations() -> dict[str, list[tuple[str, str]]]:
    """Rule -> [(page, quote)], from the docstrings of ``_r<n>_*`` functions."""
    found: dict[str, list[tuple[str, str]]] = {rule: [] for rule in RULES}
    tree = ast.parse(CONTRACT.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        match = re.fullmatch(r"_r(\d)_\w+", node.name)
        if not match:
            continue
        for paragraph in (ast.get_docstring(node) or "").split("\n\n"):
            cited = CITATION.match(_flat(paragraph))
            if cited:
                found[f"R{match.group(1)}"].append(
                    (cited.group("page"), cited.group("quote"))
                )
    return found


CITED = _citations()


@pytest.mark.parametrize("rule", RULES)
def test_every_rule_cites_the_docs(rule):
    assert CITED[rule], f"{rule} has no `Contract (<page>): <sentence>` paragraph"


@pytest.mark.parametrize(
    "page, quote",
    [citation for rule in RULES for citation in CITED[rule]],
    ids=lambda v: v if v.startswith("docs/") else v[:40],
)
def test_the_quoted_sentence_is_on_its_page(page, quote):
    text = _flat((REPO_ROOT / page).read_text(encoding="utf-8"))
    assert quote in text, f"{page} no longer says: {quote}"
