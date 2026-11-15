"""API-key scopes that join to more than 255 characters answer 422, not 500
(#1936).

``POST /api/v1/api-keys`` stores a key's scopes joined with commas in a column
of 255 characters (``APIKey.scopes``). The request bounded neither the number
of scopes nor their length, so a scope set whose joined form was longer than
the column was accepted, failed at the insert and answered 500, storing
nothing: one scope of 256 characters, 26 scopes of 9 characters (259 joined),
the same as one comma-separated string, or an object's keys.

``APIKeyCreate.scopes`` now measures the joined form, after the names are
trimmed and the empty ones dropped (exactly what is stored), and refuses one
longer than ``SCOPES_MAX_LENGTH`` with one 422 item at ``["body", "scopes"]``
that names the limit and the length, never the value sent. 255 characters is
accepted and stored whole; PostgreSQL counts characters, not bytes, so 255
"é" fit too.

Every request is a real one on PostgreSQL (a local JWT; both ``get_db``
functions are overridden), as in ``test_wrong_typed_body_values_api.py``. The
database-free boundary table is
``backend/tests/unit/schemas/test_api_key_scopes_length.py``.
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
P = "sl1936"
KEYS = "/api/v1/api-keys"

#: A value no 422 may repeat.
SENTINEL = "sl1936-sentinel-value"


def _too_long(length: int):
    """The one item a refusal carries, written out: the test pins the text."""
    return (
        "value_error",
        "Value error, scopes joined with commas may be at most 255 characters; "
        f"these are {length}",
    )


def _nine(count: int):
    """*count* distinct scope names of 9 characters each."""
    return [f"scope{i:04d}" for i in range(count)]


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
def user(make_session):
    """A signed-in user who is not a superuser: any user may create a key."""
    suffix = uuid.uuid4().hex[:8]
    created = User(
        username=f"{P}_user_{suffix}",
        email=f"{P}_user_{suffix}@scopeslength.test",
        full_name="Scopes length",
        hashed_password="unused: this user signs in by token only",
        is_active=True,
        is_superuser=False,
        role=UserRole.DEVELOPER,
    )
    session = make_session()
    session.add(created)
    session.commit()
    session.close()
    yield created
    session = make_session()
    try:
        params = {"user_id": str(created.id)}
        session.execute(text("DELETE FROM api_keys WHERE user_id = :user_id"), params)
        session.execute(text("DELETE FROM audit_logs WHERE user_id = :user_id"), params)
        session.execute(text("DELETE FROM users WHERE id = :user_id"), params)
        session.commit()
    finally:
        session.close()


def _create(client, user, scopes):
    body = {"name": f"{P} {uuid.uuid4().hex[:10]}", "scopes": scopes}
    return client.post(
        KEYS,
        content=json.dumps(body),
        headers={
            "Authorization": f"Bearer {create_local_access_token(user)}",
            "Content-Type": "application/json",
        },
    )


def _refused(response, expected) -> None:
    """The 422 repeats nothing sent, and is exactly one item, *expected*'s type
    and msg at scopes."""
    assert response.status_code == 422, response.text
    assert SENTINEL not in response.text
    items = [
        (item["type"], item["loc"], item["msg"]) for item in response.json()["detail"]
    ]
    assert items == [(expected[0], ["body", "scopes"], expected[1])]


def _keys_of(make_session, user) -> int:
    return _scalar(
        make_session,
        "SELECT count(*) FROM api_keys WHERE user_id = :id",
        id=str(user.id),
    )


def _stored(make_session, response):
    """The stored scopes of the created key, and their length in characters."""
    assert response.status_code == 201, response.text
    session = make_session()
    try:
        return session.execute(
            text("SELECT scopes, char_length(scopes) FROM api_keys WHERE id = :id"),
            {"id": response.json()["id"]},
        ).one()
    finally:
        session.close()


# --- the regressions (#1936) ---------------------------------------------------


@pytest.mark.regression
def test_1936_a_256_character_scope_answers_422_and_stores_no_key(
    client, user, make_session
):
    before = _keys_of(make_session, user)
    response = _create(client, user, ["a" * 256])
    _refused(response, _too_long(256))
    assert _keys_of(make_session, user) == before


@pytest.mark.regression
def test_1936_scopes_joining_to_259_answer_422_and_store_no_key(
    client, user, make_session
):
    """26 scopes of 9 characters: 234 characters and 25 commas."""
    before = _keys_of(make_session, user)
    response = _create(client, user, _nine(26))
    _refused(response, _too_long(259))
    assert _keys_of(make_session, user) == before


# --- the controls: what fits is stored whole ----------------------------------


def test_the_issues_25_scopes_of_9_characters_join_to_249_and_are_stored(
    client, user, make_session
):
    scopes = _nine(25)
    stored, length = _stored(make_session, _create(client, user, scopes))
    assert (stored, length) == (",".join(scopes), 249)


@pytest.mark.parametrize(
    "scopes",
    [
        pytest.param(["a" * 255], id="one-scope-of-255"),
        pytest.param(["x"] * 128, id="128-scopes-of-1"),
        pytest.param(["a" * 128, "b" * 126], id="two-scopes-joining-to-255"),
    ],
)
def test_scopes_joining_to_exactly_255_are_accepted_and_stored_whole(
    client, user, make_session, scopes
):
    stored, length = _stored(make_session, _create(client, user, scopes))
    assert (stored, length) == (",".join(scopes), 255)


# --- every shape is bounded ---------------------------------------------------


@pytest.mark.parametrize(
    ("scopes", "length"),
    [
        pytest.param(["a" * 128, "b" * 130], 259, id="two-scopes-joining-to-259"),
        pytest.param(["a"] * 129, 257, id="129-scopes-of-1"),
        pytest.param(["a" * 65537], 65537, id="one-scope-of-65537"),
        pytest.param("a" * 300, 300, id="string-of-300"),
        pytest.param(",".join(_nine(26)), 259, id="string-of-26-scopes"),
        pytest.param({"a" * 300: 1}, 300, id="object-key-of-300"),
        pytest.param([SENTINEL] * 13, 13 * 21 + 12, id="sentinel-list"),
    ],
)
def test_scopes_longer_than_255_in_any_shape_answer_422_and_store_no_key(
    client, user, make_session, scopes, length
):
    before = _keys_of(make_session, user)
    response = _create(client, user, scopes)
    _refused(response, _too_long(length))
    assert _keys_of(make_session, user) == before


# --- the stored form is what is measured --------------------------------------


@pytest.mark.parametrize(
    "scopes",
    [
        pytest.param([f"  {name}  " for name in _nine(25)], id="padded-list"),
        pytest.param(",".join(f"  {name}  " for name in _nine(25)), id="padded-string"),
        pytest.param(_nine(25) + ["", "   "], id="empty-names"),
    ],
)
def test_padding_and_empty_names_are_not_counted(client, user, make_session, scopes):
    """349 characters sent, 249 stored: the trimmed, joined form is measured."""
    stored, length = _stored(make_session, _create(client, user, scopes))
    assert (stored, length) == (",".join(_nine(25)), 249)


def test_255_two_byte_characters_are_accepted_and_stored_whole(
    client, user, make_session
):
    """The column counts characters: 255 of them are 510 bytes in UTF-8."""
    response = _create(client, user, ["é" * 255])
    stored, length = _stored(make_session, response)
    assert (stored, length) == ("é" * 255, 255)
    octets = _scalar(
        make_session,
        "SELECT octet_length(scopes) FROM api_keys WHERE id = :id",
        id=response.json()["id"],
    )
    assert octets == 510


def test_256_two_byte_characters_answer_422(client, user, make_session):
    before = _keys_of(make_session, user)
    _refused(_create(client, user, ["é" * 256]), _too_long(256))
    assert _keys_of(make_session, user) == before
