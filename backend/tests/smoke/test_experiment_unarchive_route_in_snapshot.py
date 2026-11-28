"""The experiment unarchive is in both OpenAPI snapshots, as beta (#2278).

A route missing from a snapshot is only a warning in
``test_openapi_snapshot.py``, so this pins the operation by name in
``docs/api/openapi-v1.stable.json`` (core) and ``openapi-v1.full.json``:
present, POST only, marked beta, taking no body and answering the
experiment, as archive does.
"""

from __future__ import annotations

import json

import pytest

from backend.tests.smoke.modules_manifest import REPO_ROOT

pytestmark = [pytest.mark.smoke, pytest.mark.regression]

SNAPSHOTS = ("openapi-v1.stable.json", "openapi-v1.full.json")
PATH = "/api/v1/experiments/{experiment_id}/unarchive"
ARCHIVE = "/api/v1/experiments/{experiment_id}/archive"


def _snapshot(name: str) -> dict:
    return json.loads((REPO_ROOT / "docs" / "api" / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", SNAPSHOTS)
def test_the_unarchive_is_in_the_snapshot_as_beta(name):
    snapshot = _snapshot(name)
    assert PATH in snapshot["paths"], f"(post, {PATH}) not in docs/api/{name}"
    operations = snapshot["paths"][PATH]
    assert set(operations) == {"post"}
    operation = operations["post"]
    assert operation.get("x-stability") == "beta"
    assert operation["summary"] == "Unarchive experiment (beta)"
    assert "requestBody" not in operation
    ok = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert ok == {"$ref": "#/components/schemas/ExperimentResponse"}


@pytest.mark.parametrize("name", SNAPSHOTS)
def test_it_answers_and_is_guarded_as_archive_is(name):
    paths = _snapshot(name)["paths"]
    unarchive, archive = paths[PATH]["post"], paths[ARCHIVE]["post"]
    assert set(unarchive["responses"]) == set(archive["responses"])
    assert unarchive["security"] == archive["security"]
    assert [p["name"] for p in unarchive["parameters"]] == [
        p["name"] for p in archive["parameters"]
    ]
