"""The bandit's weight history is in both committed snapshots, as served (#1958).

A route missing from a snapshot is only a warning in ``test_openapi_snapshot.py``
(it says "not in the snapshot yet"), so this pins
``GET /api/v1/bandit/{experiment_id}/history`` in
``docs/api/openapi-v1.stable.json`` and ``docs/api/openapi-v1.full.json``: it
is beta (``x-stability: beta``), its ``limit`` is 200 by default and 1 to
1000, and it answers a list of ``BanditHistoryTick``, a ``tick_at`` date-time
and a map of variant id to weight, both required. The dashboard's URL guard
fixture, ``frontend/src/tests/fixtures/openapi.json``, carries the operation
too. Reads files only: the snapshots are ``make openapi``'s output.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
SNAPSHOTS = {
    "stable": REPO_ROOT / "docs" / "api" / "openapi-v1.stable.json",
    "full": REPO_ROOT / "docs" / "api" / "openapi-v1.full.json",
}
URL_GUARD = REPO_ROOT / "frontend" / "src" / "tests" / "fixtures" / "openapi.json"
PATH = "/api/v1/bandit/{experiment_id}/history"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(params=sorted(SNAPSHOTS), scope="module")
def snapshot(request) -> dict:
    return _load(SNAPSHOTS[request.param])


def test_the_operation_is_beta(snapshot):
    assert set(snapshot["paths"][PATH]) == {"get"}
    assert snapshot["paths"][PATH]["get"]["x-stability"] == "beta"


def test_the_limit_is_200_by_default_and_1_to_1000(snapshot):
    parameters = snapshot["paths"][PATH]["get"]["parameters"]
    assert [(p["name"], p["in"]) for p in parameters] == [
        ("experiment_id", "path"),
        ("limit", "query"),
    ]
    schema = parameters[1]["schema"]
    bounds = (schema["default"], schema["minimum"], schema["maximum"])
    assert (schema["type"], bounds) == ("integer", (200, 1, 1000))
    assert parameters[0]["schema"]["format"] == "uuid"


def test_it_answers_a_list_of_ticks(snapshot):
    answer = snapshot["paths"][PATH]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert answer["type"] == "array"
    assert answer["items"] == {"$ref": "#/components/schemas/BanditHistoryTick"}
    tick = snapshot["components"]["schemas"]["BanditHistoryTick"]
    assert tick["required"] == ["tick_at", "weights"]
    assert set(tick["properties"]) == {"tick_at", "weights"}
    assert tick["properties"]["tick_at"]["format"] == "date-time"
    assert tick["properties"]["weights"]["type"] == "object"
    assert tick["properties"]["weights"]["additionalProperties"] == {"type": "number"}


def test_the_url_guard_fixture_carries_it():
    assert set(_load(URL_GUARD)["paths"][PATH]) == {"get"}
