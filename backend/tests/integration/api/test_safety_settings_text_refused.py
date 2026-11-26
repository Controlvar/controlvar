"""Saving the global safety settings with a ``default_metrics`` holding NUL or
an unpaired surrogate answers 422.

PostgreSQL refuses U+0000 in ``jsonb``, and a lone surrogate cannot be encoded
at all, so ``POST /api/v1/safety/settings`` failed at the database and
answered 500 for either character in a metric's name or its
``comparison_type``. ``default_metrics`` is now checked where its request
model declares it, with the helpers #961 and #2180 use: 422 at the field, a
fixed message that never repeats the value, nothing saved and no audit entry.

The check reads the validated value, which is what is saved, so nothing else
moves: a key a threshold does not have is still dropped with a 200, and a
body another check refuses (a threshold sent as a string, a list sent for the
field) still gets that check's 422. Authentication still answers 401 first,
and the route's superuser check (a dependency) still answers 403 first. A
valid surrogate pair (an emoji) is saved as before.

The bodies are sent as raw JSON text: a JSON client cannot encode ``\\ud800``,
and sending the escapes is what a client on the wire does.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Tuple

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.core.config import settings
from backend.app.core.security import create_local_access_token
from backend.app.main import app
from backend.app.models.audit_log import AuditLog
from backend.app.models.safety import SafetySettings
from backend.app.models.user import User, UserRole

pytestmark = [pytest.mark.integration, pytest.mark.requires_db, pytest.mark.regression]

PREFIX = "tsafetyset"
SCHEMA = "test_experimentation"
PATH = "/api/v1/safety/settings"
JSON = {"Content-Type": "application/json"}

#: Marks the refused text, so a test can tell whether an answer repeats it.
MARK = "zqsafetysetmark"
#: An ASCII placeholder, replaced in the raw JSON text by the escape sent.
X = "QQSAFETYSETQQ"
#: The characters, as JSON escapes: NUL, and an unpaired high and low surrogate.
CHARACTERS = {"nul": "\\u0000", "surrogate": "\\ud800", "low-surrogate": "\\udc00"}
NUL = CHARACTERS["nul"]
#: A valid surrogate pair: U+1F600, an emoji.
EMOJI_ESCAPE = "\\ud83d\\ude00"
EMOJI = "\U0001f600"

#: Where in ``default_metrics`` the character is refused: each answered 500 on
#: main, for each character.
PLACES: Dict[str, Any] = {
    "a-metric-name": {"error_rate" + MARK + X: {"warning_threshold": 0.05}},
    "a-comparison-type": {
        "error_rate": {
            "warning_threshold": 0.05,
            "comparison_type": "greater_than" + MARK + X,
        }
    },
}

#: Bodies another check refuses, with the (type, loc) of that check's 422 for
#: NUL and for an unpaired surrogate: what main answers, and still answers.
OTHER_CHECKS: Dict[str, Tuple[Any, Dict[str, Tuple[str, List[Any]]]]] = {
    "a-threshold-sent-as-a-string": (
        {"error_rate": {"warning_threshold": "0.05" + MARK + X}},
        {
            "nul": (
                "float_parsing",
                ["body", "default_metrics", "error_rate", "warning_threshold"],
            ),
            "surrogate": (
                "string_unicode",
                ["body", "default_metrics", "error_rate", "warning_threshold"],
            ),
        },
    ),
    "a-list-sent-for-the-field": (
        ["error_rate" + MARK + X],
        {
            "nul": ("dict_type", ["body", "default_metrics"]),
            "surrogate": ("dict_type", ["body", "default_metrics"]),
        },
    ),
}
OTHER_CHECK_CASES = [
    (place, character) for place in OTHER_CHECKS for character in CHARACTERS
]

#: A key a threshold does not have: dropped, never saved, and the save answers
#: 200, on main as now.
UNKNOWN_KEY = {"error_rate": {"warning_threshold": 0.05, "note" + MARK + X: 1}}

STORED = {
    "enable_automatic_rollbacks": False,
    "default_metrics": {"error_rate": {"critical_threshold": 0.2}},
}

REFUSED = {
    "type": "value_error",
    "loc": ["body", "default_metrics"],
    "msg": (
        "Value error, default_metrics must not contain NUL or unpaired "
        "surrogate characters"
    ),
}


def _body(place: str) -> Dict[str, Any]:
    return {"enable_automatic_rollbacks": False, "default_metrics": PLACES[place]}


def _raw(body: Dict[str, Any], escape: str) -> bytes:
    """*body* as JSON text, with the placeholder replaced by *escape*."""
    sent = json.dumps(body)
    assert X in sent
    return sent.replace(X, escape).encode("ascii")


def _assert_refused(response) -> None:
    assert response.status_code == 422, response.text
    assert MARK not in response.text, "the answer repeats the value sent"
    errors = response.json()["detail"]
    assert [{k: e[k] for k in ("type", "loc", "msg")} for e in errors] == [REFUSED]


# --- fixtures -----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _local_auth(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "CACHE_ENABLED", False)


def _user(db_session, role: UserRole, superuser: bool = False) -> User:
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"{PREFIX}_{role.value}_{suffix}",
        email=f"{PREFIX}_{suffix}@savedtext.test",
        full_name="Safety Settings User",
        hashed_password="unused: this user signs in by token only",
        is_active=True,
        is_superuser=superuser,
        role=role,
    )
    db_session.add(user)
    db_session.commit()
    return user


@pytest.fixture
def developer(db_session) -> User:
    return _user(db_session, UserRole.DEVELOPER)


@pytest.fixture
def admin(db_session) -> User:
    """An ADMIN who is not a superuser: the route refuses them."""
    return _user(db_session, UserRole.ADMIN)


@pytest.fixture
def superuser(db_session) -> User:
    return _user(db_session, UserRole.ADMIN, superuser=True)


@pytest.fixture
def fresh(test_db):
    """A session of the test's own, opened per read, never the request's."""
    factory = sessionmaker(bind=test_db, expire_on_commit=False)

    def open_session():
        session = factory()
        session.execute(text(f"SET search_path TO {SCHEMA}"))
        return session

    return open_session


@pytest.fixture
def client(test_db):
    """The real route and the real authentication; only the database is the
    test one. A 500 is an answer here, not an exception."""
    factory = sessionmaker(bind=test_db, autocommit=False, autoflush=False)

    def override_get_db():
        session = factory()
        session.execute(text(f"SET search_path TO {SCHEMA}"))
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[deps.get_db] = override_get_db
    try:
        with TestClient(app, raise_server_exceptions=False) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(deps.get_db, None)


@pytest.fixture(autouse=True)
def _keep_the_global_settings(fresh):
    """Put the global settings back as they were: other suites read them."""
    kept = _stored(fresh)
    yield
    session = fresh()
    try:
        session.execute(text("delete from safety_settings"))
        for row in kept:
            session.execute(
                text(
                    "insert into safety_settings select * from "
                    "json_populate_record(null::safety_settings, cast(:row as json))"
                ),
                {"row": row},
            )
        session.commit()
    finally:
        session.close()


@pytest.fixture(autouse=True)
def _cleanup(db_session):
    """Leave no user or audit entry of this module behind."""
    yield
    s = db_session
    s.rollback()
    user_ids = [
        r.id for r in s.query(User.id).filter(User.username.like(f"{PREFIX}_%"))
    ]
    s.query(AuditLog).filter(AuditLog.user_id.in_(user_ids)).delete(
        synchronize_session=False
    )
    s.query(User).filter(User.id.in_(user_ids)).delete(synchronize_session=False)
    s.commit()


# --- helpers ------------------------------------------------------------------


def _auth(user: User) -> Dict[str, str]:
    return {**JSON, "Authorization": f"Bearer {create_local_access_token(user)}"}


def _stored(fresh) -> List[str]:
    """Every global settings row, as stored."""
    session = fresh()
    try:
        return (
            session.execute(
                text("select row_to_json(s)::text from safety_settings s order by id")
            )
            .scalars()
            .all()
        )
    finally:
        session.close()


def _totals(fresh) -> Tuple[int, ...]:
    """How many global settings rows and audit entries of either kind there are."""
    session = fresh()
    try:
        return tuple(
            session.execute(text(f"select count(*) from {table}")).scalar()
            for table in ("safety_settings", "audit_logs", "audit_events_v2")
        )
    finally:
        session.close()


def _post(client, user, body, escape):
    return client.post(PATH, content=_raw(body, escape), headers=_auth(user))


# --- the refusal --------------------------------------------------------------


@pytest.mark.parametrize("character", CHARACTERS)
@pytest.mark.parametrize("place", PLACES)
@pytest.mark.parametrize("stored", ("none-stored", "one-stored"))
def test_the_global_safety_settings_refuse_the_character(
    client, fresh, superuser, stored, place, character
):
    if stored == "one-stored":
        kept = client.post(PATH, json=STORED, headers=_auth(superuser))
        assert kept.status_code == 200, kept.text
    else:
        session = fresh()
        try:
            session.execute(text("delete from safety_settings"))
            session.commit()
        finally:
            session.close()
    before = (_stored(fresh), _totals(fresh))

    response = _post(client, superuser, _body(place), CHARACTERS[character])

    _assert_refused(response)
    assert (_stored(fresh), _totals(fresh)) == before


def test_an_emoji_in_a_metric_name_is_still_saved(client, fresh, superuser):
    saved = client.post(
        PATH,
        content=_raw(
            {"default_metrics": {"error_rate" + X: {"critical_threshold": 0.1}}},
            EMOJI_ESCAPE,
        ),
        headers=_auth(superuser),
    )

    assert saved.status_code == 200, saved.text
    assert list(saved.json()["default_metrics"]) == ["error_rate" + EMOJI]
    session = fresh()
    try:
        row = session.query(SafetySettings).one()
        assert list(row.default_metrics) == ["error_rate" + EMOJI]
    finally:
        session.close()


def test_a_body_this_check_does_not_read_answers_as_before(client, fresh, superuser):
    """A null ``default_metrics`` is saved, and the character in another field
    gets that field's own 422."""
    h = _auth(superuser)

    saved = client.post(PATH, json={"default_metrics": None}, headers=h)
    assert saved.status_code == 200, saved.text
    assert saved.json()["default_metrics"] is None

    before = (_stored(fresh), _totals(fresh))
    refused = client.post(
        PATH,
        content=_raw({"enable_automatic_rollbacks": "true" + X}, NUL),
        headers=h,
    )
    assert refused.status_code == 422, refused.text
    assert [(e["type"], e["loc"]) for e in refused.json()["detail"]] == [
        ("bool_parsing", ["body", "enable_automatic_rollbacks"])
    ]
    assert (_stored(fresh), _totals(fresh)) == before


@pytest.mark.parametrize("place,character", OTHER_CHECK_CASES)
def test_a_body_another_check_refuses_answers_as_before(
    client, fresh, superuser, place, character
):
    metrics, errors = OTHER_CHECKS[place]
    expected = errors["nul" if character == "nul" else "surrogate"]
    before = (_stored(fresh), _totals(fresh))

    response = _post(
        client,
        superuser,
        {"enable_automatic_rollbacks": False, "default_metrics": metrics},
        CHARACTERS[character],
    )

    assert response.status_code == 422, response.text
    assert MARK not in response.text
    assert [(e["type"], e["loc"]) for e in response.json()["detail"]] == [expected]
    assert (_stored(fresh), _totals(fresh)) == before


@pytest.mark.parametrize("character", CHARACTERS)
def test_a_key_a_threshold_does_not_have_is_still_dropped(
    client, fresh, superuser, character
):
    saved = _post(
        client,
        superuser,
        {"enable_automatic_rollbacks": False, "default_metrics": UNKNOWN_KEY},
        CHARACTERS[character],
    )

    assert saved.status_code == 200, saved.text
    assert saved.json()["default_metrics"] == {
        "error_rate": {
            "warning_threshold": 0.05,
            "critical_threshold": None,
            "comparison_type": "greater_than",
        }
    }
    session = fresh()
    try:
        row = session.query(SafetySettings).one()
        assert list(row.default_metrics) == ["error_rate"]
        assert row.default_metrics["error_rate"]["warning_threshold"] == 0.05
        assert MARK not in json.dumps(row.default_metrics)
    finally:
        session.close()


# --- the order of the refusals ------------------------------------------------


def test_authentication_still_answers_401_first(client, fresh):
    before = (_stored(fresh), _totals(fresh))

    response = client.post(
        PATH, content=_raw(_body("a-metric-name"), NUL), headers=JSON
    )

    assert response.status_code == 401, response.text
    assert MARK not in response.text
    assert (_stored(fresh), _totals(fresh)) == before


@pytest.mark.parametrize("role", ("admin", "developer"))
def test_the_403_for_a_caller_who_is_not_a_superuser_still_comes_first(
    client, fresh, request, role
):
    user = request.getfixturevalue(role)
    before = (_stored(fresh), _totals(fresh))

    refused = _post(client, user, _body("a-metric-name"), NUL)

    assert refused.status_code == 403, refused.text
    assert MARK not in refused.text
    # A storable body is still the 403 it was.
    assert _post(client, user, _body("a-metric-name"), "").status_code == 403
    assert (_stored(fresh), _totals(fresh)) == before
