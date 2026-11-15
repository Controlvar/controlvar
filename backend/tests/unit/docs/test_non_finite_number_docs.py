"""The documentation of #1630's refusals is the code's.

* ``docs/api/endpoints.md`` gives the query that lists stored events whose
  ``value`` is outside the range, byte for byte :data:`STORED_VALUES_QUERY`,
  which ``backend/tests/integration/api/test_tracking_non_finite_numbers.py``
  runs on PostgreSQL. The GitHub release note copies the same text.
* Each of the three tracking routes on that page states the range, and
  ``/batch`` the difference between the two refusals (a non-finite number
  refuses the whole request, a value outside the range only its entry).
* The ``docs/api/stability.md`` row names ``finite_number``, the range, the
  same sentence, and the operations it does not change, computed from the two
  OpenAPI snapshots: every operation whose request body is not JSON.

Reads the pages and the snapshots only (no database): it runs on a docs-only
pull request.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Set, Tuple

import pytest

from backend.app.core.finite_json import FINITE_NUMBER_TYPE
from backend.app.schemas.tracking import VALUE_BOUND

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
API = REPO_ROOT / "docs" / "api"
ENDPOINTS = API / "endpoints.md"
STABILITY = API / "stability.md"
SNAPSHOTS = (API / "openapi-v1.stable.json", API / "openapi-v1.full.json")

#: The query for stored events whose value is outside the range (NaN, which
#: PostgreSQL orders above every number, and both infinities included).
STORED_VALUES_QUERY = (
    "SELECT experiment_id, count(*) FROM experimentation.events "
    "WHERE abs(value) > 1e12 GROUP BY 1;"
)

#: PE1630-3's sentence, on ``/batch`` and in the stability row.
BATCH_SENTENCE = (
    "A non-finite number anywhere in a `/batch` body refuses the whole request and "
    "stores none of its events, while a `value` outside -1e12 to 1e12 fails only its "
    "own entry."
)
RANGE = "a finite number from -1e12 to 1e12"
TRACKING = (
    "POST /api/v1/tracking/track",
    "POST /api/v1/tracking/batch",
    "POST /api/v1/tracking/events",
)

_SQL_BLOCK = re.compile(r"^[ \t]*```sql\n(.*?)^[ \t]*```", re.M | re.S)


def _flat(text: str) -> str:
    """Line breaks and indentation as single spaces, as the page renders."""
    return " ".join(text.split())


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    end = text.find("\n### ", start + len(heading))
    return text[start : end if end != -1 else len(text)]


def _row() -> str:
    (row,) = [
        line
        for line in STABILITY.read_text("utf-8").splitlines()
        if "| #1630 |" in line
    ]
    return row


def test_the_query_is_the_constant_and_its_bound_is_the_codes():
    blocks = [
        block.strip()
        for block in _SQL_BLOCK.findall(ENDPOINTS.read_text("utf-8"))
        if "abs(value)" in block
    ]
    assert blocks == [STORED_VALUES_QUERY]
    (literal,) = re.findall(r"abs\(value\) > (\S+) ", STORED_VALUES_QUERY)
    assert float(literal) == VALUE_BOUND


def test_each_tracking_route_states_the_range():
    text = ENDPOINTS.read_text("utf-8")
    track = _flat(_section(text, "### Track Event\n"))
    batch = _flat(_section(text, "### Batch Track Events\n"))
    by_ids = _flat(_section(text, "### Track Event by Ids\n"))
    assert f"`value` must be {RANGE}, both included." in track
    assert f'`"type": "{FINITE_NUMBER_TYPE}"`' in track
    assert "`1e400`" in track
    assert "outside -1e12 to 1e12 is reported in `errors`" in batch
    assert BATCH_SENTENCE in batch
    assert 'outside -1e12 to 1e12 answers 422 at `["body", "value"]`' in by_ids


def test_the_bound_in_the_words_is_the_constant():
    assert VALUE_BOUND == float("1e12")


def _not_json() -> Set[Tuple[str, str]]:
    """Every operation, in either snapshot, whose request body is not JSON."""
    found = set()
    for snapshot in SNAPSHOTS:
        document = json.loads(snapshot.read_text("utf-8"))
        for path, item in document["paths"].items():
            for method, operation in item.items():
                content = (operation.get("requestBody") or {}).get("content")
                if content and "application/json" not in content:
                    found.add((method.upper(), path))
    return found


def _json_body(snapshot: Path) -> Set[Tuple[str, str]]:
    document = json.loads(snapshot.read_text("utf-8"))
    return {
        (method.upper(), path)
        for path, item in document["paths"].items()
        for method, operation in item.items()
        if "application/json"
        in ((operation.get("requestBody") or {}).get("content") or {})
    }


def test_the_stability_row_states_the_change():
    row = _row()
    assert row.startswith("| Every operation with a JSON request body")
    assert f'`"type": "{FINITE_NUMBER_TYPE}"`' in row
    assert RANGE in row
    assert BATCH_SENTENCE in row
    assert "endpoints.md#track-event" in row
    for operation in TRACKING:
        assert f"`{operation}`" in row


def test_the_operations_the_row_leaves_unchanged_are_computed_from_the_snapshots():
    """The row covers every operation with a JSON body; the others (form
    bodies) are named as unchanged, each one of them."""
    row = _row()
    unchanged = _not_json()
    assert unchanged, "no operation without a JSON body: the snapshots were not read"
    for method, path in sorted(unchanged):
        assert f"`{method} {path}`" in row, f"{method} {path} is not named as unchanged"
    stable = _json_body(SNAPSHOTS[0])
    for operation in TRACKING:
        assert tuple(operation.split(" ", 1)) in stable, operation
