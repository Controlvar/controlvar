"""The flag environment routes are beta in the document the application serves (#2011).

A route that is not marked ``x-stability: beta`` is a stable promise, and the
snapshot test only warns about a route it does not know. This lists the six
operations by name: exactly these exist under ``/environments`` and under a
flag's ``environments``, each is beta, and each has the answers, parameters,
request body and response its route has.
``backend/tests/smoke/test_flag_environment_routes_in_snapshot.py`` holds both
OpenAPI snapshots to the same checks.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from backend.app.core.flag_environments import FLAG_ENVIRONMENTS
from backend.app.main import app

pytestmark = [pytest.mark.unit, pytest.mark.regression]

FLAG = "/api/v1/feature-flags/{flag_id}/environments"
ONE = FLAG + "/{environment}"
PROMOTE = FLAG + "/{target}/promote"
ENVIRONMENTS = "/api/v1/environments"
BATCH = ENVIRONMENTS + "/{environment}/feature-flag-settings"

#: (method, path) -> (answers, response schema, request body schema).
OPERATIONS = {
    ("get", ENVIRONMENTS): ({"200", "401"}, ["FlagEnvironmentRead"], None),
    ("get", BATCH): (
        {"200", "401", "403", "422"},
        "EnvironmentFlagSettings",
        None,
    ),
    ("get", FLAG): ({"200", "401", "403", "404", "422"}, "FlagEnvironments", None),
    ("get", ONE): (
        {"200", "401", "403", "404", "422"},
        "FlagEnvironmentSettings",
        None,
    ),
    ("put", ONE): (
        {"200", "400", "401", "403", "404", "422", "500"},
        "FlagEnvironmentSettings",
        "FlagEnvironmentUpdate",
    ),
    ("post", PROMOTE): (
        {"200", "401", "403", "404", "422", "500"},
        "FlagEnvironmentSettings",
        "FlagPromotion",
    ),
}
KEYS = sorted(OPERATIONS)


def _ours(path: str) -> bool:
    return path == ENVIRONMENTS or path.startswith((ENVIRONMENTS + "/", FLAG))


def _reference(schema: Dict[str, Any]):
    """The component a schema names: ``X``, or ``["X"]`` for a list of it."""
    if schema.get("type") == "array":
        return [_reference(schema["items"])]
    return schema["$ref"].rsplit("/", 1)[-1]


def _json(content: Dict[str, Any]):
    return _reference(content["application/json"]["schema"])


# --- what a document must say: held for the served one here, and for the two
# --- snapshots in the smoke test ------------------------------------------------


def check_exactly_these_operations(document: Dict[str, Any]) -> None:
    found = {
        (method, path)
        for path, operations in document["paths"].items()
        if _ours(path)
        for method in operations
    }
    assert found == set(OPERATIONS)


def check_an_operation(document: Dict[str, Any], key) -> None:
    method, path = key
    answers, response, request = OPERATIONS[key]
    operation = document["paths"][path][method]
    assert operation.get("x-stability") == "beta", f"{method} {path} is not beta"
    assert operation["summary"].endswith("(beta)")
    assert set(operation["responses"]) == answers
    assert _json(operation["responses"]["200"]["content"]) == response
    body = operation.get("requestBody")
    assert (body is None) == (request is None)
    if body is not None:
        assert body["required"] is True
        assert _json(body["content"]) == request
    for parameter in operation.get("parameters", []):
        if parameter["name"] in ("environment", "target"):
            assert parameter["in"] == "path"
            assert parameter["schema"]["enum"] == list(FLAG_ENVIRONMENTS)


def check_the_batch_takes_1_to_100_ids(document: Dict[str, Any]) -> None:
    (flag_id,) = [
        p
        for p in document["paths"][BATCH]["get"]["parameters"]
        if p["name"] == "flag_id"
    ]
    assert flag_id["in"] == "query" and flag_id["required"] is True
    schema = flag_id["schema"]
    assert (schema["minItems"], schema["maxItems"]) == (1, 100)
    assert schema["items"]["format"] == "uuid"


def check_the_schemas(document: Dict[str, Any]) -> None:
    schemas = document["components"]["schemas"]
    settings = schemas["FlagEnvironmentSettings"]
    assert set(settings["properties"]) == {
        "environment",
        "status",
        "is_active",
        "rollout_percentage",
        "targeting_rules",
        "configured",
        "updated_at",
    }
    # Every field is in every answer.
    assert set(settings["required"]) == set(settings["properties"])
    assert settings["properties"]["status"]["enum"] == [
        "active",
        "inactive",
        "archived",
    ]
    update = schemas["FlagEnvironmentUpdate"]
    assert update["additionalProperties"] is False
    assert set(update["properties"]) == {
        "is_active",
        "rollout_percentage",
        "targeting_rules",
    }
    assert "required" not in update
    assert update["properties"]["is_active"]["type"] == "boolean"
    rollout = update["properties"]["rollout_percentage"]
    assert (rollout["type"], rollout["minimum"], rollout["maximum"]) == (
        "integer",
        0,
        100,
    )
    promotion = schemas["FlagPromotion"]
    assert promotion["additionalProperties"] is False
    assert promotion["required"] == ["from"]
    assert set(promotion["properties"]) == {"from"}
    assert promotion["properties"]["from"]["enum"] == list(FLAG_ENVIRONMENTS)
    assert set(schemas["FlagEnvironments"]["required"]) == {
        "flag_id",
        "key",
        "environments",
    }
    assert set(schemas["EnvironmentFlagSettings"]["required"]) == {
        "environment",
        "settings",
    }
    assert set(schemas["FlagEnvironmentRead"]["required"]) == {"key", "name", "default"}


# --- the served document --------------------------------------------------------


@pytest.fixture(scope="module")
def served() -> Dict[str, Any]:
    return app.openapi()


def test_exactly_these_operations_are_served(served):
    check_exactly_these_operations(served)


@pytest.mark.parametrize("key", KEYS, ids=lambda k: f"{k[0]} {k[1]}")
def test_each_operation_is_beta_and_answers_what_it_says(served, key):
    check_an_operation(served, key)


def test_the_batch_takes_1_to_100_flag_ids(served):
    check_the_batch_takes_1_to_100_ids(served)


def test_the_schemas_are_the_settings_of_one_environment(served):
    check_the_schemas(served)
