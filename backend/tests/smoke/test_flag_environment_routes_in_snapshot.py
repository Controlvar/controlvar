"""The flag environment routes are in both OpenAPI snapshots, as beta (#2011).

A route missing from a snapshot is only a warning in
``test_openapi_snapshot.py``, so this holds
``docs/api/openapi-v1.stable.json`` (core) and ``openapi-v1.full.json`` to
what ``backend/tests/unit/api/test_flag_environment_operations.py`` holds the
served document to: exactly the six operations, each beta, with its answers,
parameters and schemas.
"""

from __future__ import annotations

import json

import pytest

from backend.tests.smoke.modules_manifest import REPO_ROOT
from backend.tests.unit.api.test_flag_environment_operations import (
    KEYS,
    check_an_operation,
    check_exactly_these_operations,
    check_the_batch_takes_1_to_100_ids,
    check_the_schemas,
)

pytestmark = [pytest.mark.smoke, pytest.mark.regression]

SNAPSHOTS = ("openapi-v1.stable.json", "openapi-v1.full.json")


@pytest.fixture(scope="module", params=SNAPSHOTS)
def snapshot(request):
    path = REPO_ROOT / "docs" / "api" / request.param
    return json.loads(path.read_text(encoding="utf-8"))


def test_exactly_these_operations_are_in_the_snapshot(snapshot):
    check_exactly_these_operations(snapshot)


@pytest.mark.parametrize("key", KEYS, ids=lambda k: f"{k[0]} {k[1]}")
def test_each_operation_is_beta_in_the_snapshot(snapshot, key):
    check_an_operation(snapshot, key)


def test_the_batch_and_the_schemas_in_the_snapshot(snapshot):
    check_the_batch_takes_1_to_100_ids(snapshot)
    check_the_schemas(snapshot)
