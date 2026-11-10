"""The flag list refuses a skip or limit the database cannot page with (422, not 500).

``GET /api/v1/feature-flags/`` passed ``skip`` and ``limit`` to the query's
OFFSET and LIMIT unchecked. PostgreSQL refuses a negative value for either and
anything from 2**63 up, so such a request answered 500. API fuzzing reached it
once #1842 added the ``stale`` parameter, which changed the requests its seed
generates for the list. Each is now a 422 at ``["query", <param>]``, the answer
the other list routes give for a skip below 0, whether ``stale`` is sent or
not (with ``stale`` the page is cut in Python, where a negative skip counted
from the end). The OpenAPI document does not change.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.app.main import app

pytestmark = [pytest.mark.integration, pytest.mark.regression]

BASE = "/api/v1/feature-flags/"
#: The largest OFFSET or LIMIT PostgreSQL takes (a bigint).
MAX = 2**63 - 1


@pytest.fixture
def client(admin_client):
    """The admin's client, answering a server error with its status, not a raise."""
    with TestClient(app, raise_server_exceptions=False) as plain:
        yield plain


REFUSED = [
    ("limit", -1, "greater_than_equal", {"ge": 0}),
    ("skip", -1, "greater_than_equal", {"ge": 0}),
    ("limit", MAX + 1, "less_than_equal", {"le": MAX}),
    ("skip", MAX + 1, "less_than_equal", {"le": MAX}),
]


@pytest.mark.parametrize(
    "stale", [None, "true", "false"], ids=["all", "stale", "fresh"]
)
@pytest.mark.parametrize(
    "param,value,kind,ctx",
    REFUSED,
    ids=["limit-negative", "skip-negative", "limit-too-large", "skip-too-large"],
)
def test_a_skip_or_limit_out_of_range_is_a_422_naming_it(
    client, param, value, kind, ctx, stale
):
    params = {param: value}
    if stale is not None:
        params["stale"] = stale

    response = client.get(BASE, params=params)

    assert response.status_code == 422, response.text
    assert response.json() == {
        "detail": [
            {
                "type": kind,
                "loc": ["query", param],
                "msg": (
                    f"Input should be greater than or equal to {ctx['ge']}"
                    if "ge" in ctx
                    else f"Input should be less than or equal to {ctx['le']}"
                ),
                "ctx": ctx,
            }
        ]
    }


@pytest.mark.parametrize("stale", [None, "true"], ids=["all", "stale"])
@pytest.mark.parametrize(
    "params",
    [{"skip": 0, "limit": 0}, {"skip": MAX}, {"limit": MAX}, {"skip": 0, "limit": 1}],
    ids=["zero", "skip-max", "limit-max", "one"],
)
def test_every_skip_and_limit_the_database_takes_still_answers_200(
    client, params, stale
):
    if stale is not None:
        params = {**params, "stale": stale}

    response = client.get(BASE, params=params)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["skip"] == params.get("skip", 0)
    assert body["limit"] == params.get("limit", 100)
    assert len(body["items"]) <= body["limit"]
