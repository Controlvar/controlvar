"""The warehouse analysis routes' contract, read from the application's OpenAPI.

* every route is ``x-stability: beta``;
* the parameters each route takes are exactly the path parameters listed
  here -- no query, header or cookie parameter -- so nothing a request
  carries outside its body can hold a credential, and a new parameter of any
  name fails here until it is listed;
* every request body is one of the request models whose string fields
  ``modules/backend/tests/unit/warehouse/test_request_schemas.py`` pins.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.smoke

PREFIX = "/api/v1/warehouse"

#: (METHOD, path) -> the parameters it takes, as (in, name).
PARAMETERS = {
    ("GET", "/api/v1/warehouse/analysis/connectors"): set(),
    ("GET", "/api/v1/warehouse/analysis/connections"): set(),
    ("POST", "/api/v1/warehouse/analysis/connections"): set(),
    ("GET", "/api/v1/warehouse/analysis/connections/{connection_id}"): {
        ("path", "connection_id")
    },
    ("PUT", "/api/v1/warehouse/analysis/connections/{connection_id}"): {
        ("path", "connection_id")
    },
    ("DELETE", "/api/v1/warehouse/analysis/connections/{connection_id}"): {
        ("path", "connection_id")
    },
    ("POST", "/api/v1/warehouse/analysis/connections/{connection_id}/test"): {
        ("path", "connection_id")
    },
    ("POST", "/api/v1/warehouse/analysis/connections/{connection_id}/regenerate-key"): {
        ("path", "connection_id")
    },
    ("POST", "/api/v1/warehouse/analysis/connections/test"): set(),
    ("GET", "/api/v1/warehouse/analysis/sources"): set(),
    ("POST", "/api/v1/warehouse/analysis/sources"): set(),
    ("GET", "/api/v1/warehouse/analysis/sources/{source_id}"): {("path", "source_id")},
    ("PUT", "/api/v1/warehouse/analysis/sources/{source_id}"): {("path", "source_id")},
    ("DELETE", "/api/v1/warehouse/analysis/sources/{source_id}"): {
        ("path", "source_id")
    },
    ("POST", "/api/v1/warehouse/analysis/sources/{source_id}/validate"): {
        ("path", "source_id")
    },
    ("POST", "/api/v1/warehouse/analysis/sources/{source_id}/preview"): {
        ("path", "source_id")
    },
    ("POST", "/api/v1/warehouse/analysis/experiments/{experiment_id}/runs"): {
        ("path", "experiment_id")
    },
    ("GET", "/api/v1/warehouse/analysis/experiments/{experiment_id}/runs"): {
        ("path", "experiment_id")
    },
    ("GET", "/api/v1/warehouse/analysis/runs/{run_id}"): {("path", "run_id")},
}

#: The request bodies, by the schema names they reference.
BODIES = {
    ("POST", "/api/v1/warehouse/analysis/connections"): {
        "SnowflakeConnectionCreate",
        "BigQueryConnectionCreate",
        "AthenaConnectionCreate",
        "RedshiftConnectionCreate",
    },
    ("PUT", "/api/v1/warehouse/analysis/connections/{connection_id}"): {
        "SnowflakeConnectionUpdate",
        "BigQueryConnectionUpdate",
        "AthenaConnectionUpdate",
        "RedshiftConnectionUpdate",
    },
    # No Redshift body: a Redshift connection is tested once it is saved.
    ("POST", "/api/v1/warehouse/analysis/connections/test"): {
        "BigQueryConnectionTest",
        "AthenaConnectionTest",
    },
    ("POST", "/api/v1/warehouse/analysis/sources"): {
        "AssignmentSourceCreate",
        "MetricSourceCreate",
    },
    ("PUT", "/api/v1/warehouse/analysis/sources/{source_id}"): {
        "AssignmentSourceUpdate",
        "MetricSourceUpdate",
    },
    ("POST", "/api/v1/warehouse/analysis/sources/{source_id}/preview"): {
        "PreviewRequest"
    },
    ("POST", "/api/v1/warehouse/analysis/experiments/{experiment_id}/runs"): {
        "RunCreate"
    },
}


@pytest.fixture(scope="module")
def operations():
    from backend.app.main import app

    document = app.openapi()
    found = {}
    for path, item in document["paths"].items():
        if not path.lower().startswith(PREFIX):
            continue
        for method, operation in item.items():
            if method in ("get", "put", "post", "delete", "patch", "head", "options"):
                found[(method.upper(), path)] = operation
    return found


def _refs(node) -> set[str]:
    refs: set[str] = set()
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            refs.add(ref.rsplit("/", 1)[-1])
        for value in node.values():
            refs |= _refs(value)
    elif isinstance(node, list):
        for value in node:
            refs |= _refs(value)
    return refs


def test_the_operations_are_the_route_set(operations):
    from modules.backend.tests.smoke.test_module_route_table import (
        EXPECTED_WAREHOUSE_ROUTES,
    )

    assert set(operations) == set(PARAMETERS) == set(EXPECTED_WAREHOUSE_ROUTES)


def test_every_warehouse_route_is_beta(operations):
    assert {
        key for key, op in operations.items() if op.get("x-stability") != "beta"
    } == set()


def test_no_route_takes_credentials_outside_body(operations):
    """Exactly the listed path parameters; no query, header or cookie one."""
    found = {
        key: {(p["in"], p["name"]) for p in op.get("parameters", [])}
        for key, op in operations.items()
    }
    assert found == PARAMETERS


def test_request_bodies_are_the_pinned_models(operations):
    found = {
        key: _refs(op["requestBody"])
        for key, op in operations.items()
        if "requestBody" in op
    }
    assert found == BODIES
