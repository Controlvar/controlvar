"""The full OpenAPI snapshot carries a metric source's direction (#1386).

``backend/tests/smoke/test_openapi_snapshot.py`` only warns about a change to
a beta operation, and every warehouse route is beta, so a pull request that
changed the source schemas and forgot ``make openapi`` would pass it.  This
holds the three components that carry ``lower_is_better`` to the live
document, and pins the field's contract in the committed snapshot: a JSON
boolean, false when left out, on a metric source's request bodies; nullable
on the response (null for an assignment source); absent from an assignment
source's bodies.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.smoke

REPO_ROOT = Path(__file__).resolve().parents[4]
SNAPSHOT = REPO_ROOT / "docs" / "api" / "openapi-v1.full.json"
REQUESTS = ("MetricSourceCreate", "MetricSourceUpdate")
RESPONSE = "SourceOut"


@pytest.fixture(scope="module")
def committed() -> dict:
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))["components"]["schemas"]


@pytest.fixture(scope="module")
def live() -> dict:
    from backend.app.main import app
    from backend.scripts.dump_openapi import integral_floats_as_ints

    # The form `make openapi` writes, so the two compare as JSON values.
    document = json.loads(json.dumps(integral_floats_as_ints(app.openapi())))
    return document["components"]["schemas"]


@pytest.mark.parametrize("name", [*REQUESTS, RESPONSE])
def test_the_snapshot_has_the_live_source_component(committed, live, name):
    assert committed.get(name) == live[name], (
        f"docs/api/openapi-v1.full.json's {name} differs from the API's: "
        "run `make openapi`"
    )


@pytest.mark.parametrize("name", REQUESTS)
def test_a_metric_source_body_takes_a_boolean_that_defaults_to_false(committed, name):
    field = committed[name]["properties"]["lower_is_better"]
    assert (field["type"], field["default"]) == ("boolean", False)
    assert "lower_is_better" not in committed[name].get("required", [])


def test_a_source_returns_a_nullable_direction(committed):
    field = committed[RESPONSE]["properties"]["lower_is_better"]
    assert field["anyOf"] == [{"type": "boolean"}, {"type": "null"}]
    assert "lower_is_better" not in committed[RESPONSE].get("required", [])


@pytest.mark.parametrize("name", ["AssignmentSourceCreate", "AssignmentSourceUpdate"])
def test_an_assignment_source_body_has_no_direction(committed, name):
    assert "lower_is_better" not in committed[name]["properties"]
    assert committed[name]["additionalProperties"] is False
