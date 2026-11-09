"""The operations the capability page calls beta are beta in the running app.

The page lists a capability as "(beta)" when its page says so, and each of
these routes carries ``x-stability: beta`` in the OpenAPI document the
application builds. This reads ``app.openapi()``, not a committed snapshot:
the snapshot test only warns about a beta marker that went missing, so a route
could lose its marker and still pass it.

The operations covered here are the ones no other test holds; CUPED, the
interaction pair and the holdout results are in
``backend/tests/unit/core/test_analysis_status.py``, and the warehouse routes
in ``modules/backend/tests/smoke/test_warehouse_routes_contract.py``.

The split URL routes belong to the optional modules. In a tree with no
``modules/`` (``scripts/core_build.sh``'s core copy) they are not mounted, and
those two cases are skipped, as the module tests are; the others always run.
"""

from __future__ import annotations

import importlib.util

import pytest

pytestmark = pytest.mark.unit

CORE_BETA_OPERATIONS = (
    ("post", "/api/v1/segments/{segment_id}/members"),
    ("post", "/api/v1/segments/{segment_id}/members/remove"),
    ("get", "/api/v1/audit-logs/export"),
    ("get", "/api/v1/sdk/ruleset"),
)

MODULE_BETA_OPERATIONS = (
    ("post", "/api/v1/tracking/split-url/assign"),
    ("get", "/api/v1/tracking/split-url/routes"),
)


@pytest.fixture(scope="module")
def openapi():
    from backend.app.main import app

    return app.openapi()


@pytest.mark.parametrize("method,path", CORE_BETA_OPERATIONS)
def test_a_core_operation_the_page_calls_beta_is_beta(openapi, method, path):
    assert openapi["paths"][path][method].get("x-stability") == "beta"


@pytest.mark.parametrize("method,path", MODULE_BETA_OPERATIONS)
def test_a_module_operation_the_page_calls_beta_is_beta(openapi, method, path):
    if importlib.util.find_spec("modules") is None:
        pytest.skip("core tree: the optional modules are absent by design")
    assert openapi["paths"][path][method].get("x-stability") == "beta"
