"""``POST /api/v1/tracking/alias`` is in the committed OpenAPI snapshots (#449).

A route missing from a snapshot is only a warning in
``test_openapi_snapshot.py``, so this pins the alias operation by name
(IA-D2): present in the core and full snapshots and in the dashboard's URL
fixture, marked beta, behind the API key, answering 200, 401, 403, 409, 422
and 429, with a body of the two ids (each 1 to 255 characters) and an answer
of the two ids and ``created``.
"""

from __future__ import annotations

import json

import pytest

from backend.tests.smoke.modules_manifest import REPO_ROOT

pytestmark = [pytest.mark.smoke]

ALIAS = "/api/v1/tracking/alias"
SNAPSHOTS = (
    REPO_ROOT / "docs" / "api" / "openapi-v1.stable.json",
    REPO_ROOT / "docs" / "api" / "openapi-v1.full.json",
)
URL_FIXTURE = REPO_ROOT / "frontend" / "src" / "tests" / "fixtures" / "openapi.json"


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _schema(document, ref):
    return document["components"]["schemas"][ref.rsplit("/", 1)[-1]]


@pytest.mark.parametrize("path", SNAPSHOTS, ids=lambda p: p.name)
def test_the_alias_operation_is_beta_in_the_snapshot(path):
    document = _load(path)
    paths = document["paths"]
    assert ALIAS in paths, f"POST {ALIAS} not in {path.name}: run `make openapi`"
    assert set(paths[ALIAS]) == {"post"}
    operation = paths[ALIAS]["post"]
    assert operation["x-stability"] == "beta"
    assert operation["security"] == [{"APIKeyHeader": []}]
    assert set(operation["responses"]) == {"200", "401", "403", "409", "422", "429"}
    assert "parameters" not in operation, operation.get("parameters")

    body = operation["requestBody"]["content"]["application/json"]["schema"]
    request = _schema(document, body["$ref"])
    assert sorted(request["required"]) == ["anonymous_id", "user_id"]
    assert set(request["properties"]) == {"anonymous_id", "user_id"}
    for field in ("anonymous_id", "user_id"):
        prop = request["properties"][field]
        assert (prop["type"], prop["minLength"], prop["maxLength"]) == (
            "string",
            1,
            255,
        )

    ok = operation["responses"]["200"]["content"]["application/json"]["schema"]
    answer = _schema(document, ok["$ref"])
    assert set(answer["properties"]) == {"anonymous_id", "user_id", "created"}
    assert answer["properties"]["created"]["type"] == "boolean"


def test_the_alias_operation_is_in_the_dashboard_url_fixture():
    assert "post" in _load(URL_FIXTURE)["paths"][ALIAS]
