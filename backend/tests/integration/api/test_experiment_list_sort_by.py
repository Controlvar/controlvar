"""``GET /api/v1/experiments/?sort_by=``: an unknown column answers 422.

``sort_by`` was checked with ``hasattr(Experiment, sort_by)``, which is true
for every attribute of the model class, columns or not. ``metadata``,
``registry``, ``__table__`` and ``variants`` passed the check, had no
``.asc()``/``.desc()``, and the list answered 500. The route now accepts the
four documented columns and refuses everything else with 422 naming them.
"""

from __future__ import annotations

import uuid

import pytest

from backend.app.models.user import UserRole
from backend.tests.integration.api.route_helpers import (
    api_client,
    auth,
    create_experiment,
    local_sign_in,
    make_user,
)

pytestmark = [pytest.mark.integration, pytest.mark.regression]

URL = "/api/v1/experiments/"
ALLOWED = ("created_at", "updated_at", "name", "status")


@pytest.fixture(autouse=True)
def _sign_in(monkeypatch):
    local_sign_in(monkeypatch)


@pytest.fixture
def client(db_session):
    with api_client(db_session) as c:
        yield c


@pytest.fixture
def owner(db_session):
    return make_user(db_session, UserRole.DEVELOPER, tag="sortby")


@pytest.fixture
def tag(client, owner):
    """Three experiments sharing a name suffix, created in a known order."""
    suffix = uuid.uuid4().hex[:8]
    for prefix in ("b", "c", "a"):
        create_experiment(client, owner, name=f"{prefix}-{suffix}")
    return suffix


@pytest.mark.parametrize("bad", ["metadata", "variants", "registry", "__table__"])
def test_a_sort_by_that_is_not_a_column_answers_422(client, owner, bad):
    response = client.get(URL, params={"sort_by": bad}, headers=auth(owner))

    assert response.status_code == 422, response.text
    # The published HTTPValidationError shape, with a fixed message that does
    # not repeat the value sent.
    assert response.json() == {
        "detail": [
            {
                "type": "enum",
                "loc": ["query", "sort_by"],
                "msg": "sort_by must be one of: " + ", ".join(ALLOWED),
            }
        ]
    }


@pytest.mark.parametrize("sort_by", ALLOWED)
def test_each_documented_sort_by_still_answers_200(client, owner, tag, sort_by):
    response = client.get(
        URL, params={"sort_by": sort_by, "search": tag}, headers=auth(owner)
    )

    assert response.status_code == 200, response.text
    assert response.json()["total"] == 3


@pytest.mark.parametrize(
    "order, expected", [("asc", ["a", "b", "c"]), ("desc", ["c", "b", "a"])]
)
def test_sort_order_still_orders_by_name(client, owner, tag, order, expected):
    response = client.get(
        URL,
        params={"sort_by": "name", "sort_order": order, "search": tag},
        headers=auth(owner),
    )

    assert response.status_code == 200, response.text
    names = [item["name"].split("-")[0] for item in response.json()["items"]]
    assert names == expected


def test_no_sort_by_keeps_the_default_ordering(client, owner, tag):
    response = client.get(URL, params={"search": tag}, headers=auth(owner))

    assert response.status_code == 200, response.text
    # created_at, newest first: a was created last.
    names = [item["name"].split("-")[0] for item in response.json()["items"]]
    assert names == ["a", "c", "b"]
