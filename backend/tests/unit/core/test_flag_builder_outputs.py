"""What the flag pages' rule builder sends, judged by the server (#535 V8).

``frontend/src/tests/fixtures/flag-builder-outputs.json`` records every row of
the dashboard's flag builder grid: what the page sends, and whether it is
accepted, stopped in the browser before sending, or refused by the server with
a path and a reason that the page shows beside the rules. The browser half
(``frontend/src/tests/targeting/flag-builder-outputs.test.ts``) checks the
rows and the browser verdicts against the page's code; this file checks the
server verdicts against ``validate_flag_targeting``, which the flag create
and update run on ``targeting_rules`` (#535).

The same file pins ``frontend/src/tests/fixtures/streampulse-flag-rules.json``
to the StreamPulse seed, so the dashboard tests that open those flags read the
rules the demo actually stores.
"""

from __future__ import annotations

import ast
import json
import pathlib
from collections import Counter

import pytest

from backend.app.core.targeting_adapter import (
    TargetingRulesError,
    validate_flag_targeting,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
FIXTURES = REPO_ROOT / "frontend" / "src" / "tests" / "fixtures"
OUTPUTS = json.loads((FIXTURES / "flag-builder-outputs.json").read_text("utf-8"))
ROWS = OUTPUTS["rows"]


def _server_verdict(sends: object) -> tuple[str, str | None, str | None]:
    try:
        validate_flag_targeting(sends)
    except TargetingRulesError as err:
        return "refused_by_server", err.path, err.code
    return "accepted", None, None


def test_the_counts_are_the_rows():
    counted = Counter(row["outcome"] for row in ROWS)
    assert dict(counted) == OUTPUTS["counts"]
    assert OUTPUTS["counts"] == {
        "accepted": 641,
        "blocked_in_browser": 262,
        "refused_by_server": 432,
    }


@pytest.mark.parametrize(
    "row",
    [row for row in ROWS if row["outcome"] != "blocked_in_browser"],
    ids=lambda row: row["name"],
)
def test_the_server_verdict_is_the_recorded_one(row):
    outcome, path, reason = _server_verdict(row["sends"])
    assert (outcome, path, reason) == (
        row["outcome"],
        row.get("path"),
        row.get("reason"),
    )


def _seed_literal(name: str) -> object:
    source = (REPO_ROOT / "backend" / "scripts" / "seed_streampulse.py").read_text(
        "utf-8"
    )
    for node in ast.parse(source).body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == name:
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in seed_streampulse.py")


def test_the_streampulse_fixture_is_the_seed():
    fixture = json.loads((FIXTURES / "streampulse-flag-rules.json").read_text("utf-8"))
    assert fixture["ai_search"] == _seed_literal("AI_SEARCH_RULES")
    assert fixture["player_v2"] == _seed_literal("PLAYER_V2_RULES")
