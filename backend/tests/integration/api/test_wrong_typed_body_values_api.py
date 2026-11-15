"""A body value of the wrong type answers 422 on the holdout, exclusion-group
and API-key requests, not 500 (#957).

Each field below had a ``mode="before"`` validator that compared or iterated
the raw value before pydantic checked its type. A value of another type made it
raise ``TypeError``, which pydantic does not turn into a 422, so the request
answered 500 and wrote nothing:

* ``holdout_percentage`` on ``POST /holdout`` and ``PUT /holdout/{id}``;
* ``traffic_allocation`` on ``POST /mutual-exclusion-groups`` and
  ``PUT /mutual-exclusion-groups/{id}``;
* ``scopes`` on ``POST /api-keys`` (``false``, ``true``, a number).

The two range validators now run after pydantic's own check of the type and of
the field's ``ge``/``le``. So every refusal is pydantic's 422 at
``["body", <field>]``, an out-of-range number keeps its 422 with pydantic's
``greater_than_equal``/``less_than_equal`` where it had the validator's
``value_error``, and a string holding a number in range is accepted as pydantic
accepts it for any number field (``"10"``, ``"0.5"``). ``scopes`` keeps its
``before`` validator, which now leaves a value it cannot iterate to
``List[str]``: that refuses it with ``list_type``. ``{}`` and ``{"a": 1}`` are
iterated as before and still answer 201.

A refused request writes nothing, and no 422 repeats the value sent. Every
request is a real one on PostgreSQL (a local JWT; both ``get_db`` functions
are overridden). The request models' ``before`` validators as a whole are
swept by ``backend/tests/unit/schemas/test_request_model_before_validators.py``.
"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.core.config import settings
from backend.app.core.security import create_local_access_token
from backend.app.db import session as db_session_module
from backend.app.main import app
from backend.app.models.user import User, UserRole

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

SCHEMA = "test_experimentation"
P = "wt957"
HOLDOUTS = "/api/v1/holdout"
GROUPS = "/api/v1/mutual-exclusion-groups"
KEYS = "/api/v1/api-keys"

#: Pydantic's own answers, written out: the test pins the text.
INT_TYPE = ("int_type", "Input should be a valid integer")
INT_PARSING = (
    "int_parsing",
    "Input should be a valid integer, unable to parse string as an integer",
)
FLOAT_TYPE = ("float_type", "Input should be a valid number")
FLOAT_PARSING = (
    "float_parsing",
    "Input should be a valid number, unable to parse string as a number",
)
HOLDOUT_BELOW = ("greater_than_equal", "Input should be greater than or equal to 1")
HOLDOUT_ABOVE = ("less_than_equal", "Input should be less than or equal to 20")
GROUP_BELOW = ("greater_than_equal", "Input should be greater than or equal to 0")
GROUP_ABOVE = ("less_than_equal", "Input should be less than or equal to 1")
LIST_TYPE = ("list_type", "Input should be a valid list")
COMMA_IN_SCOPE = ("value_error", "Value error, scope names may not contain commas")

#: A value no 422 may repeat.
SENTINEL = "wt957-sentinel-value"

#: Wrong-typed values each update refuses; the create refuses ``null`` too
#: (on an update ``null`` means "not changed").
HOLDOUT_REFUSED = [
    pytest.param({}, INT_TYPE, id="object"),
    pytest.param([], INT_TYPE, id="array"),
    pytest.param("abc", INT_PARSING, id="abc"),
    pytest.param(SENTINEL, INT_PARSING, id="sentinel"),
    pytest.param("50", HOLDOUT_ABOVE, id="string-50"),
]
GROUP_REFUSED = [
    pytest.param({}, FLOAT_TYPE, id="object"),
    pytest.param([], FLOAT_TYPE, id="array"),
    pytest.param("abc", FLOAT_PARSING, id="abc"),
    pytest.param(SENTINEL, FLOAT_PARSING, id="sentinel"),
    pytest.param("2", GROUP_ABOVE, id="string-2"),
]
NULL_ON_CREATE = {"holdout": INT_TYPE, "group": FLOAT_TYPE}


# --- fixtures -----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _local_auth(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "CACHE_ENABLED", False)


@pytest.fixture
def make_session(test_db):
    factory = sessionmaker(
        bind=test_db, autocommit=False, autoflush=False, expire_on_commit=False
    )

    def make():
        session = factory()
        session.execute(text(f"SET search_path TO {SCHEMA}"))
        return session

    return make


@pytest.fixture
def client(make_session):
    def override_get_db():
        session = make_session()
        try:
            yield session
        finally:
            session.close()

    overridden = (deps.get_db, db_session_module.get_db)
    for dependency in overridden:
        app.dependency_overrides[dependency] = override_get_db
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
    finally:
        for dependency in overridden:
            app.dependency_overrides.pop(dependency, None)


def _scalar(make_session, sql: str, **params):
    session = make_session()
    try:
        return session.execute(text(sql), params).scalar()
    finally:
        session.close()


@pytest.fixture
def admin(make_session):
    """An ADMIN who is not a superuser: the holdout routes need ADMIN, the
    group routes DEVELOPER, the key route any user."""
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"{P}_admin_{suffix}",
        email=f"{P}_admin_{suffix}@wrongtypes.test",
        full_name="Wrong types",
        hashed_password="unused: this user signs in by token only",
        is_active=True,
        is_superuser=False,
        role=UserRole.ADMIN,
    )
    session = make_session()
    session.add(user)
    session.commit()
    session.close()
    yield user
    session = make_session()
    try:
        params = {"user_id": str(user.id), "prefix": f"{P}%"}
        session.execute(text("DELETE FROM api_keys WHERE user_id = :user_id"), params)
        session.execute(
            text("DELETE FROM global_holdouts WHERE name LIKE :prefix"), params
        )
        session.execute(
            text("DELETE FROM mutual_exclusion_groups WHERE name LIKE :prefix"),
            params,
        )
        session.execute(
            text(
                "DELETE FROM audit_logs WHERE user_id = :user_id "
                "OR entity_name LIKE :prefix"
            ),
            params,
        )
        session.execute(text("DELETE FROM users WHERE id = :user_id"), params)
        session.commit()
    finally:
        session.close()


def _send(client, user, method: str, url: str, body: dict):
    return client.request(
        method,
        url,
        content=json.dumps(body),
        headers={
            "Authorization": f"Bearer {create_local_access_token(user)}",
            "Content-Type": "application/json",
        },
    )


def _name() -> str:
    return f"{P} {uuid.uuid4().hex[:10]}"


def _refused(response, field: str, expected) -> None:
    """The 422 is exactly one item, *expected*'s type and msg at the field."""
    assert response.status_code == 422, response.text
    items = [
        (item["type"], item["loc"], item["msg"]) for item in response.json()["detail"]
    ]
    assert items == [(expected[0], ["body", field], expected[1])]
    assert SENTINEL not in response.text


def _holdouts(make_session) -> int:
    return _scalar(make_session, "SELECT count(*) FROM global_holdouts")


def _groups(make_session) -> int:
    return _scalar(make_session, "SELECT count(*) FROM mutual_exclusion_groups")


def _keys_of(make_session, user) -> int:
    return _scalar(
        make_session,
        "SELECT count(*) FROM api_keys WHERE user_id = :id",
        id=str(user.id),
    )


def _holdout_percentage(make_session, holdout_id) -> int:
    return _scalar(
        make_session,
        "SELECT holdout_percentage FROM global_holdouts WHERE id = :id",
        id=str(holdout_id),
    )


def _traffic_allocation(make_session, group_id) -> float:
    return _scalar(
        make_session,
        "SELECT traffic_allocation FROM mutual_exclusion_groups WHERE id = :id",
        id=str(group_id),
    )


@pytest.fixture
def holdout(client, admin) -> str:
    """An inactive holdout at 5%: its percentage may still change."""
    response = _send(
        client, admin, "POST", HOLDOUTS, {"name": _name(), "holdout_percentage": 5}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


@pytest.fixture
def group(client, admin) -> str:
    """A group with 0.5 of the traffic."""
    response = _send(
        client, admin, "POST", GROUPS, {"name": _name(), "traffic_allocation": 0.5}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


# --- the regression the plan names --------------------------------------------


@pytest.mark.regression
def test_957_a_holdout_percentage_object_answers_422_and_stores_nothing(
    client, admin, make_session
):
    before = _holdouts(make_session)
    response = _send(
        client, admin, "POST", HOLDOUTS, {"name": "x", "holdout_percentage": {}}
    )
    _refused(response, "holdout_percentage", INT_TYPE)
    assert _holdouts(make_session) == before


# --- holdout ------------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize(
    ("sent", "expected"),
    HOLDOUT_REFUSED + [pytest.param(None, NULL_ON_CREATE["holdout"], id="null")],
)
def test_957_create_holdout_refuses_a_wrong_typed_percentage(
    client, admin, make_session, sent, expected
):
    before = _holdouts(make_session)
    response = _send(
        client, admin, "POST", HOLDOUTS, {"name": _name(), "holdout_percentage": sent}
    )
    _refused(response, "holdout_percentage", expected)
    assert _holdouts(make_session) == before


@pytest.mark.regression
@pytest.mark.parametrize(("sent", "expected"), HOLDOUT_REFUSED)
def test_957_update_holdout_refuses_a_wrong_typed_percentage(
    client, admin, make_session, holdout, sent, expected
):
    response = _send(
        client,
        admin,
        "PUT",
        f"{HOLDOUTS}/{holdout}",
        {"holdout_percentage": sent, "description": "not written"},
    )
    _refused(response, "holdout_percentage", expected)
    assert _holdout_percentage(make_session, holdout) == 5
    assert (
        _scalar(
            make_session,
            "SELECT description FROM global_holdouts WHERE id = :id",
            id=holdout,
        )
        is None
    )


@pytest.mark.regression
def test_957_a_percentage_string_in_range_is_taken_as_its_number(
    client, admin, make_session, holdout
):
    """``"10"`` answered 500; pydantic reads it as 10, as for any int field."""
    created = _send(
        client, admin, "POST", HOLDOUTS, {"name": _name(), "holdout_percentage": "10"}
    )
    assert created.status_code == 201, created.text
    assert created.json()["holdout_percentage"] == 10
    assert _holdout_percentage(make_session, created.json()["id"]) == 10

    updated = _send(
        client, admin, "PUT", f"{HOLDOUTS}/{holdout}", {"holdout_percentage": "12"}
    )
    assert updated.status_code == 200, updated.text
    assert _holdout_percentage(make_session, holdout) == 12


def test_true_is_still_taken_as_1(client, admin, make_session):
    """Unchanged (pinned on main first): JSON ``true`` for the int field is 1."""
    response = _send(
        client, admin, "POST", HOLDOUTS, {"name": _name(), "holdout_percentage": True}
    )
    assert response.status_code == 201, response.text
    assert _holdout_percentage(make_session, response.json()["id"]) == 1


@pytest.mark.parametrize(
    ("sent", "expected"),
    [
        pytest.param(0, HOLDOUT_BELOW, id="0"),
        pytest.param(21, HOLDOUT_ABOVE, id="21"),
        pytest.param(False, HOLDOUT_BELOW, id="false"),
    ],
)
@pytest.mark.parametrize("method", ["POST", "PUT"])
def test_an_out_of_range_percentage_keeps_its_422_with_pydantics_text(
    client, admin, make_session, holdout, method, sent, expected
):
    """The status is unchanged; the item is pydantic's own (STABILITY row 7)."""
    before = _holdouts(make_session)
    if method == "POST":
        body = {"name": _name(), "holdout_percentage": sent}
        response = _send(client, admin, "POST", HOLDOUTS, body)
    else:
        body = {"holdout_percentage": sent}
        response = _send(client, admin, "PUT", f"{HOLDOUTS}/{holdout}", body)
    _refused(response, "holdout_percentage", expected)
    bound = {"ge": 1} if expected is HOLDOUT_BELOW else {"le": 20}
    assert response.json()["detail"][0]["ctx"] == bound
    assert _holdouts(make_session) == before
    assert _holdout_percentage(make_session, holdout) == 5


def test_a_null_percentage_on_update_still_changes_nothing(
    client, admin, make_session, holdout
):
    response = _send(
        client, admin, "PUT", f"{HOLDOUTS}/{holdout}", {"holdout_percentage": None}
    )
    assert response.status_code == 200, response.text
    assert _holdout_percentage(make_session, holdout) == 5


# --- mutual exclusion group ---------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize(
    ("sent", "expected"),
    GROUP_REFUSED + [pytest.param(None, NULL_ON_CREATE["group"], id="null")],
)
def test_957_create_group_refuses_a_wrong_typed_allocation(
    client, admin, make_session, sent, expected
):
    before = _groups(make_session)
    response = _send(
        client, admin, "POST", GROUPS, {"name": _name(), "traffic_allocation": sent}
    )
    _refused(response, "traffic_allocation", expected)
    assert _groups(make_session) == before


@pytest.mark.regression
@pytest.mark.parametrize(("sent", "expected"), GROUP_REFUSED)
def test_957_update_group_refuses_a_wrong_typed_allocation(
    client, admin, make_session, group, sent, expected
):
    response = _send(
        client,
        admin,
        "PUT",
        f"{GROUPS}/{group}",
        {"traffic_allocation": sent, "description": "not written"},
    )
    _refused(response, "traffic_allocation", expected)
    assert _traffic_allocation(make_session, group) == 0.5
    assert (
        _scalar(
            make_session,
            "SELECT description FROM mutual_exclusion_groups WHERE id = :id",
            id=group,
        )
        is None
    )


@pytest.mark.regression
def test_957_an_allocation_string_in_range_is_taken_as_its_number(
    client, admin, make_session, group
):
    """``"0.5"`` answered 500; pydantic reads it as 0.5, as for any float."""
    created = _send(
        client, admin, "POST", GROUPS, {"name": _name(), "traffic_allocation": "0.5"}
    )
    assert created.status_code == 201, created.text
    assert _traffic_allocation(make_session, created.json()["id"]) == 0.5

    updated = _send(
        client, admin, "PUT", f"{GROUPS}/{group}", {"traffic_allocation": "0.25"}
    )
    assert updated.status_code == 200, updated.text
    assert _traffic_allocation(make_session, group) == 0.25


@pytest.mark.parametrize(("sent", "stored"), [(True, 1.0), (False, 0.0)])
def test_a_boolean_allocation_is_still_taken_as_its_number(
    client, admin, make_session, sent, stored
):
    """Unchanged (pinned on main first): ``true`` is 1.0, ``false`` 0.0."""
    response = _send(
        client, admin, "POST", GROUPS, {"name": _name(), "traffic_allocation": sent}
    )
    assert response.status_code == 201, response.text
    assert _traffic_allocation(make_session, response.json()["id"]) == stored


@pytest.mark.parametrize(
    ("sent", "expected"),
    [
        pytest.param(-0.1, GROUP_BELOW, id="-0.1"),
        pytest.param(2, GROUP_ABOVE, id="2"),
    ],
)
@pytest.mark.parametrize("method", ["POST", "PUT"])
def test_an_out_of_range_allocation_keeps_its_422_with_pydantics_text(
    client, admin, make_session, group, method, sent, expected
):
    before = _groups(make_session)
    if method == "POST":
        body = {"name": _name(), "traffic_allocation": sent}
        response = _send(client, admin, "POST", GROUPS, body)
    else:
        body = {"traffic_allocation": sent}
        response = _send(client, admin, "PUT", f"{GROUPS}/{group}", body)
    _refused(response, "traffic_allocation", expected)
    bound = {"ge": 0.0} if expected is GROUP_BELOW else {"le": 1.0}
    assert response.json()["detail"][0]["ctx"] == bound
    assert _groups(make_session) == before
    assert _traffic_allocation(make_session, group) == 0.5


# --- API key scopes -----------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize(
    "sent",
    [
        pytest.param(False, id="false"),
        pytest.param(True, id="true"),
        pytest.param(5, id="5"),
        pytest.param(957957957, id="sentinel-number"),
    ],
)
def test_957_scopes_that_are_not_a_list_answer_422_and_store_no_key(
    client, admin, make_session, sent
):
    before = _keys_of(make_session, admin)
    response = _send(client, admin, "POST", KEYS, {"name": _name(), "scopes": sent})
    _refused(response, "scopes", LIST_TYPE)
    assert "957957957" not in response.text
    assert _keys_of(make_session, admin) == before


@pytest.mark.parametrize(
    ("sent", "stored"),
    [
        pytest.param({}, None, id="empty-object"),
        pytest.param({"a": 1}, "a", id="object"),
        pytest.param(["a", " b "], "a,b", id="list"),
    ],
)
def test_scopes_that_can_be_iterated_are_stored_as_before(
    client, admin, make_session, sent, stored
):
    """Unchanged: an object is iterated by its keys, as it was."""
    response = _send(client, admin, "POST", KEYS, {"name": _name(), "scopes": sent})
    assert response.status_code == 201, response.text
    assert (
        _scalar(
            make_session,
            "SELECT scopes FROM api_keys WHERE id = :id",
            id=response.json()["id"],
        )
        == stored
    )


def test_a_comma_in_a_scope_still_answers_its_own_422(client, admin, make_session):
    before = _keys_of(make_session, admin)
    response = _send(client, admin, "POST", KEYS, {"name": _name(), "scopes": ["a,b"]})
    _refused(response, "scopes", COMMA_IN_SCOPE)
    assert _keys_of(make_session, admin) == before


def test_a_type_error_inside_the_scopes_validator_is_not_taken_for_a_wrong_type(
    client, admin, make_session, monkeypatch
):
    """Only the question "can it be iterated" is caught. A ``TypeError`` from
    normalising a list is a defect, and still answers 500."""

    def broken(scopes):
        raise TypeError("a defect in normalising, not a wrong type")

    monkeypatch.setattr("backend.app.schemas.api_key.normalise_scope_list", broken)
    before = _keys_of(make_session, admin)
    response = _send(client, admin, "POST", KEYS, {"name": _name(), "scopes": ["a"]})
    assert response.status_code == 500, response.text
    assert _keys_of(make_session, admin) == before


def test_a_type_error_in_the_key_route_still_answers_500(
    client, admin, make_session, monkeypatch
):
    """No handler maps ``TypeError`` to 422: one raised after validation is
    still the 500 it was."""

    def broken(scopes):
        raise TypeError("a defect in the route, not in the request")

    monkeypatch.setattr("backend.app.api.v1.endpoints.api_keys.format_scopes", broken)
    before = _keys_of(make_session, admin)
    response = _send(client, admin, "POST", KEYS, {"name": _name(), "scopes": ["a"]})
    assert response.status_code == 500, response.text
    assert "a defect" not in response.text
    assert _keys_of(make_session, admin) == before
