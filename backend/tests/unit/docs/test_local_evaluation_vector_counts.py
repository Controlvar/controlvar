"""The vector counts docs/sdk/local-evaluation.md states are the vectors file's own.

The page says its promise "is checked against `tests/sdk-contract/ruleset-vectors.json`, a
corpus of N cases" and that each SDK "must answer the M cases marked `must_local` locally".
Regenerating the file (``python -m backend.scripts.generate_ruleset_vectors``) moves both
numbers; this holds the sentence to the file, matching the sentence TEXT (whitespace folded),
not a line number, and counting the cases rather than trusting the file's ``counts`` block.

Reads two files; no git, so it runs the same in ``scripts/core_build.sh``'s copy. It is in the
docs-only gate's "Docs content tests" through its directory, ``backend/tests/unit/docs/``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
PAGE = REPO_ROOT / "docs" / "sdk" / "local-evaluation.md"
VECTORS = REPO_ROOT / "tests" / "sdk-contract" / "ruleset-vectors.json"

CORPUS_SENTENCE = re.compile(
    r"checked against `tests/sdk-contract/ruleset-vectors\.json`, a corpus of ([\d,]+) cases"
)
MUST_LOCAL_SENTENCE = re.compile(
    r"answer the ([\d,]+) cases marked `must_local` locally"
)


def _stated(pattern: "re.Pattern[str]") -> int:
    text = " ".join(PAGE.read_text(encoding="utf-8").split())
    found = pattern.findall(text)
    assert len(found) == 1, (
        f"{PAGE.name} must state this exactly once, found {found}: {pattern.pattern}"
    )
    return int(found[0].replace(",", ""))


@pytest.fixture(scope="module")
def corpus():
    return json.loads(VECTORS.read_text(encoding="utf-8"))


def test_the_page_states_the_number_of_cases_in_the_vectors_file(corpus):
    assert _stated(CORPUS_SENTENCE) == len(corpus["cases"])


def test_the_page_states_the_number_of_must_local_cases_in_the_vectors_file(corpus):
    assert _stated(MUST_LOCAL_SENTENCE) == sum(
        1 for case in corpus["cases"] if case["must_local"]
    )


def test_the_files_own_counts_block_agrees_with_its_cases(corpus):
    assert corpus["counts"]["cases"] == len(corpus["cases"])
    assert corpus["counts"]["must_local"] == sum(
        1 for case in corpus["cases"] if case["must_local"]
    )
