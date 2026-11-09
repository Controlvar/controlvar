"""The pages that list ``sort_by``'s columns list the code's (#786).

``docs/api/api-docs-guide.md`` (Filtering and Sorting) and the
``docs/api/stability.md`` row for ``GET /api/v1/experiments/`` each name the
columns ``sort_by`` accepts. Both lists must be
``backend.app.services.experiment_service.SORT_BY_COLUMNS``, in its order, so a
column added to or dropped from the code fails here until the pages say so.
"""

import re
from pathlib import Path

import pytest

from backend.app.services.experiment_service import SORT_BY_COLUMNS

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
GUIDE = REPO_ROOT / "docs" / "api" / "api-docs-guide.md"
STABILITY = REPO_ROOT / "docs" / "api" / "stability.md"

#: Where each page starts its list; the list ends at the sentence's full stop.
LEAD = "in exact case:"


def _listed(text: str) -> list:
    """The backticked names from *text*'s list after ``LEAD``."""
    assert text.count(LEAD) == 1, f"{LEAD!r} appears {text.count(LEAD)} times"
    after = text.split(LEAD, 1)[1]
    sentence = after.split(". ", 1)[0]
    return re.findall(r"`([a-z_]+)`", sentence)


def test_the_guide_lists_exactly_the_columns():
    page = GUIDE.read_text(encoding="utf-8")
    start = page.index("- Sorting:")
    end = page.index("\n\n", start)
    item = " ".join(page[start:end].split())
    assert _listed(item) == list(SORT_BY_COLUMNS)


def test_the_stability_row_lists_exactly_the_columns():
    rows = [
        line
        for line in STABILITY.read_text(encoding="utf-8").splitlines()
        if line.startswith("| `GET /api/v1/experiments/` | #786 |")
    ]
    assert len(rows) == 2, rows
    assert _listed(rows[0]) == list(SORT_BY_COLUMNS)
    assert LEAD not in rows[1]
