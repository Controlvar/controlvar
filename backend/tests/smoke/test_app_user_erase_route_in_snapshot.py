"""The app-user erase is in both OpenAPI snapshots, as beta (#2042).

A route missing from a snapshot is only a warning in
``test_openapi_snapshot.py``, so this pins the operation by name in
``docs/api/openapi-v1.stable.json`` (core) and ``openapi-v1.full.json``:
present, marked beta, answering what its page says, its request a single
``user_id`` with the bounds tracking stores and no other field, its response
one count per erased table (read from ``ERASED_TABLES``).
"""

from __future__ import annotations

import json

import pytest

from backend.app.services.app_user_erasure import ERASED_TABLES
from backend.tests.smoke.modules_manifest import REPO_ROOT

pytestmark = [pytest.mark.smoke, pytest.mark.regression]

SNAPSHOTS = ("openapi-v1.stable.json", "openapi-v1.full.json")
PATH = "/api/v1/admin/app-users/erase"


def _snapshot(name: str) -> dict:
    return json.loads((REPO_ROOT / "docs" / "api" / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", SNAPSHOTS)
def test_the_erase_is_in_the_snapshot_as_beta(name):
    snapshot = _snapshot(name)
    assert PATH in snapshot["paths"], f"(post, {PATH}) not in docs/api/{name}"
    operations = snapshot["paths"][PATH]
    assert set(operations) == {"post"}
    operation = operations["post"]
    assert operation.get("x-stability") == "beta"
    assert set(operation["responses"]) == {"200", "422", "500"}


@pytest.mark.parametrize("name", SNAPSHOTS)
def test_the_request_and_the_response_schemas(name):
    schemas = _snapshot(name)["components"]["schemas"]
    request = schemas["AppUserEraseRequest"]
    assert request["additionalProperties"] is False
    assert request["required"] == ["user_id"]
    assert set(request["properties"]) == {"user_id"}
    user_id = request["properties"]["user_id"]
    assert (user_id["minLength"], user_id["maxLength"]) == (1, 255)
    assert set(schemas["AppUserEraseResponse"]["properties"]) == {
        "receipt_id",
        "complete",
        "ids_erased",
        "deleted",
    }
    # The snapshots sort their keys, so the order is not compared.
    assert sorted(schemas["AppUserEraseCounts"]["properties"]) == sorted(
        table.name for table in ERASED_TABLES
    )
