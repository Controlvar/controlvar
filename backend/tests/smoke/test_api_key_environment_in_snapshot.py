"""An API key's environment in the OpenAPI snapshots: a stable change, on purpose (#2011).

``POST`` and ``GET /api/v1/api-keys`` are stable operations, so a difference in
their schemas fails ``test_openapi_snapshot.py`` until the snapshots are made
again. This holds ``docs/api/openapi-v1.stable.json`` (core) and
``openapi-v1.full.json`` to exactly what was added to them: an optional request
field that defaults to production, and a required field in each response. No
operation changes a key, so none changes its environment.
"""

from __future__ import annotations

import json

import pytest

from backend.app.core.flag_environments import FLAG_ENVIRONMENTS
from backend.tests.smoke.modules_manifest import REPO_ROOT

pytestmark = [pytest.mark.smoke, pytest.mark.regression]

KEYS = "/api/v1/api-keys"
SNAPSHOTS = ("openapi-v1.stable.json", "openapi-v1.full.json")


@pytest.fixture(scope="module", params=SNAPSHOTS)
def snapshot(request):
    path = REPO_ROOT / "docs" / "api" / request.param
    return json.loads(path.read_text(encoding="utf-8"))


def _schemas(node) -> set:
    """The names of the component schemas *node* refers to."""
    if isinstance(node, dict):
        named = {node["$ref"].rsplit("/", 1)[-1]} if "$ref" in node else set()
        return named.union(*map(_schemas, node.values()))
    if isinstance(node, list):
        return set().union(*map(_schemas, node))
    return set()


def test_a_key_is_created_listed_and_deleted_and_no_operation_changes_one(snapshot):
    operations = {
        (method, path)
        for path, methods in snapshot["paths"].items()
        if path == KEYS or path.startswith(f"{KEYS}/")
        for method in methods
    }
    assert operations == {
        ("post", KEYS),
        ("get", KEYS),
        ("delete", f"{KEYS}/{{key_id}}"),
    }


def test_the_create_and_the_list_are_stable_and_use_the_three_schemas(snapshot):
    path = snapshot["paths"][KEYS]
    for operation in path.values():
        assert "x-stability" not in operation
    assert _schemas(path["post"]["requestBody"]) == {"APIKeyCreate"}
    refused = {"HTTPValidationError"}
    assert _schemas(path["post"]["responses"]) == {"APIKeyCreated"} | refused
    assert _schemas(path["get"]["responses"]) == {"APIKeyRead"} | refused


def test_the_create_request_takes_an_environment_and_defaults_to_production(snapshot):
    schema = snapshot["components"]["schemas"]["APIKeyCreate"]
    assert schema["properties"]["environment"] == {
        "default": "production",
        "description": (
            "The flag environment the key works in. It cannot be changed later: "
            "create another key and delete this one."
        ),
        "enum": list(FLAG_ENVIRONMENTS),
        "title": "Environment",
        "type": "string",
    }
    assert schema["required"] == ["name"]


@pytest.mark.parametrize("name", ["APIKeyCreated", "APIKeyRead"])
def test_each_response_requires_the_environment(snapshot, name):
    schema = snapshot["components"]["schemas"][name]
    assert schema["properties"]["environment"] == {
        "description": "The flag environment the key works in.",
        "enum": list(FLAG_ENVIRONMENTS),
        "title": "Environment",
        "type": "string",
    }
    assert "environment" in schema["required"]
