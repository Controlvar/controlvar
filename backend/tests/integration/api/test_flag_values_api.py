"""A flag that serves a string, number or JSON value, through the routes and a
real database (#1497, Plan F's F4).

Only ``deps.get_db`` is overridden: users sign in with real local tokens and
evaluate with a real API key, so every role and permission check is the one
production runs (D11). Flags are created through the API by a non-superuser
DEVELOPER; a superuser appears only where the route requires one (the safety
rollback).

* The create and update contract (UX C1, QA B3/B4): every refusal's status,
  ``type``, ``loc`` and ``msg``, for each type and each kind of invalid input,
  through POST and PUT, with nothing stored; the hostile battery and the
  limits' boundaries, each counted.
* EMF-5: an update is checked on the pair as it will be stored. PF-5: one
  equality table runs through the API's ``values_equal`` and through
  PostgreSQL's own ``check_flag_values``, and the two agree.
* PF-3 / UX C3: any flag's GET body PUT back unchanged answers 200 and changes
  nothing; a boolean flag's two columns stay NULL. UX C4: ``value_type`` is
  read-only.
* UX C2 / QA B1, B7: evaluation serves ``on_value`` exactly when ``enabled``
  and ``off_value`` in every other case (a rule or the rollout out, inactive,
  archived, ``reason: "error"``, a safety rollback to 0% and the users a
  partial rollback removes); ``config`` stays null. UX C5: the bulk route
  stays ``{key: bool}``.
* PF-1: PostgreSQL refuses NaN, Infinity, NUL and a lone surrogate at INSERT;
  a stored value that decodes but breaks the rules is answered with no
  ``value``, and no route answers 500 for it.
* D11 (QA B10): DEVELOPER sets values; ANALYST and VIEWER get 403.
* Audit (Plan F ruling 3): ``value_type`` as a value, the two values by name.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.api.v1.endpoints.feature_flags import _values_refused
from backend.app.core.config import settings
from backend.app.core.database_config import get_schema_name
from backend.app.core.flag_values import BOOLEAN_VALUES, VALUE_TYPE_READ_ONLY
from backend.app.core.security import create_local_access_token, get_password_hash
from backend.app.main import app
from backend.app.models.audit_log import AuditLog
from backend.app.models.compliance_audit_event import ComplianceAuditEvent
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.metrics.metric import ErrorLog, RawMetric
from backend.app.models.user import User, UserRole
from backend.app.schemas.feature_flag import (
    DEFAULT_VALUE_UNSUPPORTED,
    FeatureFlagUpdate,
)
from backend.app.services import feature_flag_service
from backend.app.services.feature_flag_service import (
    FeatureFlagService,
    FlagValuesRefused,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

COLLECTION = "/api/v1/feature-flags"

#: A valid pair per value type: what the flags below serve.
PAIRS = {
    "string": ("Complete purchase", "Buy now"),
    "number": (10, 2.5),
    "json": ({"color": "blue", "sizes": [1, 2]}, ["fallback"]),
}
VALUE_TYPES = ("boolean", "string", "number", "json")

#: A rule the dashboard writes: users on the pro plan.
PRO_RULE = {
    "logical_operator": "AND",
    "groups": [
        {
            "logical_operator": "AND",
            "conditions": [{"attribute": "plan", "operator": "equals", "value": "pro"}],
        }
    ],
}
PRO = {"plan": "pro"}
FREE = {"plan": "free"}

SAFE = "9007199254740991"
MESSAGES = {
    "value_out_of_range": "{f}: a number is outside -" + SAFE + " to " + SAFE,
    "unstorable_text": "{f} must not contain NUL or unpaired surrogate characters",
    "value_too_deep": "{f} nested more than 10 levels deep",
    "value_too_large": "{f} larger than 8192 bytes",
}


def _message(error_type, field):
    return MESSAGES[error_type].format(f=field)


def _mismatch(field, value_type):
    words = {
        "string": "a string",
        "number": "a number",
        "json": "a JSON object or array",
    }[value_type]
    return (
        "value_type_mismatch",
        field,
        f"{field} must be {words}, because value_type is {value_type}",
    )


def _required(field):
    return (
        "value_required",
        field,
        f"{field} is required for a string, number or json flag",
    )


def _equal(field):
    other = "on_value" if field == "off_value" else "off_value"
    return (
        "values_equal",
        field,
        f"{field} equals {other}; the two values must differ, or turning the flag "
        "off changes nothing",
    )


#: What the service logs when the database's check, not the API's, refused an
#: update (#1514 review R2). A refusal the API check makes logs nothing.
BACKSTOP_LOG = "update refused by check_flag_values at commit"


def _backstop_lines(caplog):
    return [r.getMessage() for r in caplog.records if BACKSTOP_LOG in r.getMessage()]


@pytest.fixture(autouse=True)
def _local_fail_closed(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")


def _make_user(db_session, role, is_superuser=False):
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"values_{role.value}_{suffix}",
        email=f"values_{role.value}_{suffix}@flag-values.test",
        full_name="Flag values",
        hashed_password=get_password_hash("Demo1234!"),
        is_active=True,
        is_superuser=is_superuser,
        role=role,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def client(db_session):
    engine = db_session.get_bind()
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    def override_get_db():
        session = factory()
        session.execute(text(f"SET search_path TO {get_schema_name()}"))
        try:
            yield session
        finally:
            session.close()

    assert deps.get_api_key not in app.dependency_overrides
    assert deps.get_current_active_user not in app.dependency_overrides
    app.dependency_overrides[deps.get_db] = override_get_db
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
    finally:
        app.dependency_overrides.pop(deps.get_db, None)


@pytest.fixture
def developer(db_session):
    user = _make_user(db_session, UserRole.DEVELOPER)
    assert not user.is_superuser
    return user


@pytest.fixture
def api_key(client, developer):
    created = client.post(
        "/api/v1/api-keys",
        json={"name": f"values-{uuid.uuid4().hex[:6]}", "scopes": ["sdk:ruleset"]},
        headers=_auth(developer),
    )
    assert created.status_code == 201, created.text
    return created.json()["key"]


def _auth(user):
    return {"Authorization": f"Bearer {create_local_access_token(user)}"}


def _key(prefix="values"):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _body(value_type=None, **extra):
    """A create body; a value type with no values given gets ``PAIRS``'."""
    body = {"key": _key(), "name": "Flag values"}
    if value_type is not None:
        body["value_type"] = value_type
        if value_type in PAIRS:
            body["on_value"], body["off_value"] = PAIRS[value_type]
    body.update(extra)
    return body


def _create(client, user, value_type=None, **extra):
    response = client.post(
        f"{COLLECTION}/", json=_body(value_type, **extra), headers=_auth(user)
    )
    assert response.status_code == 201, response.text
    return response.json()


def _raw(client, method, path, user, payload_text):
    """Send JSON text as written: NaN, Infinity and 1e400 included."""
    return client.request(
        method,
        path,
        content=payload_text.encode("utf-8"),
        headers={**_auth(user), "content-type": "application/json"},
    )


def _detail(response):
    assert response.status_code == 422, response.text
    return [(e["type"], e["loc"][-1], e["msg"]) for e in response.json()["detail"]]


def _locs_are_in_the_body(response):
    assert all(e["loc"][0] == "body" for e in response.json()["detail"])


def _columns(db_session, key):
    """The row as stored, the value columns as JSON text and as SQL NULL."""
    db_session.expire_all()
    row = (
        db_session.execute(
            text(
                "SELECT key, name, status, rollout_percentage, default_value, "
                "value_type, on_value::text AS on_text, off_value::text AS off_text, "
                "on_value IS NULL AS on_null, off_value IS NULL AS off_null, "
                "targeting_rules::text AS rules, tags::text AS tags, description "
                "FROM feature_flags WHERE key = :key"
            ),
            {"key": key},
        )
        .mappings()
        .first()
    )
    return None if row is None else dict(row)


def _evaluate(client, api_key, key, user_id, context=None):
    params = {"user_id": user_id}
    if context is not None:
        params["context"] = json.dumps(context)
    response = client.get(
        f"{COLLECTION}/evaluate/{key}", params=params, headers={"X-API-Key": api_key}
    )
    assert response.status_code == 200, response.text
    return response.json()


def _served(key, value_type, enabled, reason, value):
    return {
        "key": key,
        "enabled": enabled,
        "value": value,
        "value_type": value_type,
        "config": None,
        "reason": reason,
    }


def _bucket(user_id, key):
    digest = hashlib.md5(f"{user_id}:{key}".encode(), usedforsecurity=False)
    return int(digest.hexdigest(), 16) % 100


@pytest.fixture
def cleanup(db_session):
    """Deletes the flags a test names, with their evaluation metrics."""
    keys = []
    yield keys
    db_session.rollback()
    ids = [
        row.id
        for row in db_session.query(FeatureFlag.id).filter(FeatureFlag.key.in_(keys))
    ]
    if ids:
        db_session.query(RawMetric).filter(RawMetric.feature_flag_id.in_(ids)).delete(
            synchronize_session=False
        )
        db_session.query(ErrorLog).filter(ErrorLog.feature_flag_id.in_(ids)).delete(
            synchronize_session=False
        )
        db_session.query(FeatureFlag).filter(FeatureFlag.id.in_(ids)).delete(
            synchronize_session=False
        )
        db_session.commit()


# ---------------------------------------------------------------------------
# The create and update contract (UX C1, QA B3)
# ---------------------------------------------------------------------------

#: (id, the create body's value fields, the refusals as (type, field, msg)).
CREATE_REFUSED = [
    ("string-without-values", {"value_type": "string"}, [
        _required("on_value"), _required("off_value")
    ]),
    ("number-without-off", {"value_type": "number", "on_value": 1}, [
        _required("off_value")
    ]),
    ("number-given-ten", {"value_type": "number", "on_value": "ten", "off_value": 1}, [
        _mismatch("on_value", "number")
    ]),
    ("number-given-true", {"value_type": "number", "on_value": 1, "off_value": True}, [
        _mismatch("off_value", "number")
    ]),
    ("string-given-3", {"value_type": "string", "on_value": 3, "off_value": "b"}, [
        _mismatch("on_value", "string")
    ]),
    ("json-given-scalar", {"value_type": "json", "on_value": "blue", "off_value": {}}, [
        _mismatch("on_value", "json")
    ]),
    ("json-given-null", {"value_type": "json", "on_value": None, "off_value": {}}, [
        ("null_not_allowed", "on_value", "on_value cannot be null")
    ]),
    ("type-null", {"value_type": None}, [
        ("null_not_allowed", "value_type", "value_type cannot be null")
    ]),
    ("type-unknown", {"value_type": "date"}, [
        (
            "literal_error",
            "value_type",
            "Input should be 'boolean', 'string', 'number' or 'json'",
        )
    ]),
    ("boolean-on-false", {"on_value": False}, [
        ("boolean_values", "on_value", BOOLEAN_VALUES)
    ]),
    ("boolean-off-blue", {"value_type": "boolean", "off_value": "blue"}, [
        ("boolean_values", "off_value", BOOLEAN_VALUES)
    ]),
    ("equal-strings", {"value_type": "string", "on_value": "a", "off_value": "a"}, [
        _equal("off_value")
    ]),
    ("3-equals-3.0", {"value_type": "number", "on_value": 3, "off_value": 3.0}, [
        _equal("off_value")
    ]),
    ("a-third-value", {
        "value_type": "string", "on_value": "a", "off_value": "b", "values": ["c"]
    }, [("extra_forbidden", "values", "Extra inputs are not permitted")]),
    ("default-value-true", {
        "value_type": "string", "on_value": "a", "off_value": "b", "default_value": True
    }, [("default_value_unsupported", "default_value", DEFAULT_VALUE_UNSUPPORTED)]),
]  # fmt: skip


@pytest.mark.regression
@pytest.mark.parametrize(
    "fields, refusals",
    [c[1:] for c in CREATE_REFUSED],
    ids=[c[0] for c in CREATE_REFUSED],
)
def test_a_create_outside_the_contract_is_422_and_stores_nothing(
    client, developer, db_session, fields, refusals
):
    body = {"key": _key(), "name": "Refused", **fields}
    response = client.post(f"{COLLECTION}/", json=body, headers=_auth(developer))
    assert _detail(response) == refusals
    _locs_are_in_the_body(response)
    assert _columns(db_session, body["key"]) is None


#: (id, the stored flag's type, the PUT body, the refusals).
PUT_REFUSED = [
    ("number-given-ten", "number", {"on_value": "ten"}, [_mismatch("on_value", "number")]),
    ("string-given-3", "string", {"off_value": 3}, [_mismatch("off_value", "string")]),
    ("json-given-scalar", "json", {"on_value": "x"}, [_mismatch("on_value", "json")]),
    ("boolean-given-text", "boolean", {"on_value": "x"}, [
        ("boolean_values", "on_value", BOOLEAN_VALUES)
    ]),
    ("boolean-off-true", "boolean", {"off_value": True}, [
        ("boolean_values", "off_value", BOOLEAN_VALUES)
    ]),
    ("string-to-number", "string", {"value_type": "number"}, [
        ("read_only", "value_type", VALUE_TYPE_READ_ONLY)
    ]),
    ("boolean-to-string", "boolean", {
        "value_type": "string", "on_value": "a", "off_value": "b"
    }, [("read_only", "value_type", VALUE_TYPE_READ_ONLY)]),
    ("json-to-boolean", "json", {"value_type": "boolean"}, [
        ("read_only", "value_type", VALUE_TYPE_READ_ONLY)
    ]),
    ("string-null", "string", {"on_value": None}, [
        ("null_not_allowed", "on_value", "on_value cannot be null")
    ]),
    ("on-equals-stored-off", "string", {"on_value": "Buy now"}, [_equal("on_value")]),
    ("off-equals-stored-on", "number", {"off_value": 10.0}, [_equal("off_value")]),
]  # fmt: skip


@pytest.mark.regression
@pytest.mark.parametrize(
    "value_type, body, refusals",
    [c[1:] for c in PUT_REFUSED],
    ids=[c[0] for c in PUT_REFUSED],
)
def test_an_update_outside_the_contract_is_422_and_changes_nothing(
    client, developer, db_session, caplog, value_type, body, refusals
):
    flag = _create(client, developer, value_type)
    before = _columns(db_session, flag["key"])

    response = client.put(
        f"{COLLECTION}/{flag['id']}", json=body, headers=_auth(developer)
    )

    assert _detail(response) == refusals
    _locs_are_in_the_body(response)
    assert _columns(db_session, flag["key"]) == before
    # The API's check refused it, not the database's.
    assert _backstop_lines(caplog) == []


@pytest.mark.regression
def test_a_partial_put_is_checked_on_the_pair_as_it_will_be_stored(
    client, developer, db_session, caplog
):
    """EMF-5: stored on "a", off "b"; ``PUT {"on_value": "b"}`` would store an
    equal pair. Checked only when both are sent, this reached the database's
    check and answered 500 (now the backstop's 422, which the log tells
    apart)."""
    flag = _create(client, developer, "string", on_value="a", off_value="b")
    before = _columns(db_session, flag["key"])

    response = client.put(
        f"{COLLECTION}/{flag['id']}", json={"on_value": "b"}, headers=_auth(developer)
    )

    assert response.status_code == 422, response.text
    assert response.json() == {
        "detail": [
            {
                "type": "values_equal",
                "loc": ["body", "on_value"],
                "msg": "on_value equals off_value; the two values must differ, or "
                "turning the flag off changes nothing",
            }
        ]
    }
    assert _columns(db_session, flag["key"]) == before
    assert _backstop_lines(caplog) == []


def _set_off_value(conn, flag_id, value_text):
    conn.execute(
        text(
            f'UPDATE "{get_schema_name()}".feature_flags '
            "SET off_value = CAST(:v AS jsonb) WHERE id = :id"
        ),
        {"v": value_text, "id": str(flag_id)},
    )


def _wait_until_a_session_waits_for_a_lock(engine, seconds=20.0):
    """Block until some backend of this database waits on a lock, so the
    interleaving below is decided by PostgreSQL, not by timing."""
    deadline = time.monotonic() + seconds
    with engine.connect() as probe:
        while time.monotonic() < deadline:
            waiting = probe.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            ).scalar()
            probe.commit()
            if waiting:
                return
            time.sleep(0.05)
    raise AssertionError("the update never waited for the row")


@pytest.mark.regression
def test_a_concurrent_value_write_is_a_422_not_a_500(
    client, developer, db_session, caplog
):
    """#1514 review R2: the PUT ``{"on_value": "c"}`` arrives while another
    transaction holds the row and sets ``off_value`` to "c"; that one commits
    while the PUT waits. The update reads the row ``FOR UPDATE``, so it checks
    the committed pair and answers 422 ``values_equal``, storing nothing.

    Unlocked, the PUT checked the old pair, its UPDATE then met
    ``check_flag_values`` and it answered 500. With only the lock removed the
    backstop answers 422, so the log tells which one refused it.
    """
    caplog.set_level(logging.INFO)
    flag = _create(client, developer, "string", on_value="a", off_value="b")
    engine = db_session.get_bind()
    writer = engine.connect()
    transaction = writer.begin()
    _set_off_value(writer, flag["id"], '"c"')
    outcome = {}

    def put():
        try:
            outcome["response"] = client.put(
                f"{COLLECTION}/{flag['id']}",
                json={"on_value": "c"},
                headers=_auth(developer),
            )
        except Exception as exc:  # pragma: no cover - reported below
            outcome["error"] = exc

    request = threading.Thread(target=put)
    request.start()
    try:
        _wait_until_a_session_waits_for_a_lock(engine)
        transaction.commit()
    finally:
        if transaction.is_active:
            transaction.rollback()
        writer.close()
    request.join(timeout=60)
    assert not request.is_alive(), "the PUT never finished"
    assert "error" not in outcome, outcome.get("error")

    response = outcome["response"]
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == [
        {"type": t, "loc": ["body", f], "msg": m} for t, f, m in [_equal("on_value")]
    ]
    row = _columns(db_session, flag["key"])
    assert (json.loads(row["on_text"]), json.loads(row["off_text"])) == ("a", "c")
    assert _backstop_lines(caplog) == [], "the check, not the database, refused it"


@pytest.mark.regression
def test_a_check_violation_at_commit_is_answered_as_values_equal(
    db_session, developer, client, caplog, monkeypatch
):
    """#1514 review R2, the backstop: a writer that does not lock the row (the
    service called directly here) commits ``off_value = "SECRET-c"`` between the
    check and the commit. The database refuses the equal pair; the service
    answers ``values_equal`` at the field sent, the route's 422, instead of
    re-raising (a 500), and logs the flag's id, never the values."""
    caplog.set_level(logging.INFO)
    flag = _create(
        client, developer, "string", on_value="SECRET-a", off_value="SECRET-b"
    )
    engine = db_session.get_bind()
    real_check = feature_flag_service._check_sent_values

    def check_then_race(flag_data, row):
        real_check(flag_data, row)
        with engine.begin() as other:
            _set_off_value(other, row.id, '"SECRET-c"')

    monkeypatch.setattr(feature_flag_service, "_check_sent_values", check_then_race)
    session = sessionmaker(bind=engine, autocommit=False, autoflush=False)()
    try:
        session.execute(text(f"SET search_path TO {get_schema_name()}"))
        row = session.get(FeatureFlag, uuid.UUID(flag["id"]))
        with pytest.raises(FlagValuesRefused) as refused:
            FeatureFlagService(session).update_feature_flag(
                row, FeatureFlagUpdate(on_value="SECRET-c")
            )
    finally:
        session.close()

    assert refused.value.__cause__ is None and refused.value.__suppress_context__
    assert _values_refused(refused.value).errors() == [
        {"type": t, "loc": ("body", f), "msg": m} for t, f, m in [_equal("on_value")]
    ]
    stored = _columns(db_session, flag["key"])
    assert (json.loads(stored["on_text"]), json.loads(stored["off_text"])) == (
        "SECRET-a",
        "SECRET-c",
    )
    assert len(_backstop_lines(caplog)) == 1
    assert not [r for r in caplog.records if "SECRET" in r.getMessage()]


@pytest.mark.regression
@pytest.mark.parametrize("value_type", ["string", "number", "json"])
def test_a_value_flag_is_created_and_its_values_changed(
    client, developer, db_session, caplog, value_type
):
    on, off = PAIRS[value_type]
    flag = _create(client, developer, value_type)
    assert (flag["value_type"], flag["on_value"], flag["off_value"]) == (
        value_type,
        on,
        off,
    )
    assert flag["default_value"] is False

    # Each value as sent, the other as stored; then the two swapped.
    changed = client.put(
        f"{COLLECTION}/{flag['id']}", json={"on_value": off}, headers=_auth(developer)
    )
    assert changed.status_code == 422, changed.text
    assert _backstop_lines(caplog) == []
    swapped = client.put(
        f"{COLLECTION}/{flag['id']}",
        json={"on_value": off, "off_value": on},
        headers=_auth(developer),
    )
    assert swapped.status_code == 200, swapped.text
    assert (swapped.json()["on_value"], swapped.json()["off_value"]) == (off, on)
    row = _columns(db_session, flag["key"])
    assert json.loads(row["on_text"]) == off and json.loads(row["off_text"]) == on


# ---------------------------------------------------------------------------
# The hostile battery and the boundaries (QA B4), each counted
# ---------------------------------------------------------------------------

#: (id, value_type, on_value as JSON text, the refusal's type). Sent as text,
#: so the number forms are exactly these.
HOSTILE = [
    ("nan", "number", "NaN", "value_out_of_range"),
    ("infinity", "number", "Infinity", "value_out_of_range"),
    ("minus-infinity", "number", "-Infinity", "value_out_of_range"),
    ("2^53", "number", str(2**53), "value_out_of_range"),
    ("minus-2^53", "number", str(-(2**53)), "value_out_of_range"),
    ("1e400", "number", "1e400", "value_out_of_range"),
    ("10^400", "number", "1" + "0" * 400, "value_out_of_range"),
    ("nul", "string", '"a\\u0000b"', "unstorable_text"),
    ("lone-surrogate", "string", '"\\ud800"', "unstorable_text"),
    ("nul-in-a-key", "json", '{"\\u0000": 1}', "unstorable_text"),
    ("nan-inside", "json", '{"a": [1, NaN]}', "value_out_of_range"),
    ("eleven-deep", "json", "[" * 11 + "1" + "]" * 11, "value_too_deep"),
    ("8193-bytes", "json", '{"a":"' + "x" * 8185 + '"}', "value_too_large"),
]
#: (id, value_type, on_value as JSON text): each limit itself, accepted.
BOUNDARY = [
    ("max-safe", "number", "9007199254740991"),
    ("min-safe", "number", "-9007199254740991"),
    ("ten-deep", "json", "[" * 10 + "1" + "]" * 10),
    ("8192-bytes", "json", '{"a":"' + "x" * 8184 + '"}'),
    ("8192-byte-string", "string", '"' + "x" * 8190 + '"'),
]
#: A valid off value per type, different from every on value above.
OFF_TEXT = {"number": "1", "string": '"ok"', "json": '{"ok": true}'}


def test_the_battery_is_exactly_this_size():
    """B4's exact count: a row dropped from either list fails here."""
    assert (len(HOSTILE), len(BOUNDARY)) == (13, 5)
    assert len({h[0] for h in HOSTILE} | {b[0] for b in BOUNDARY}) == 18


@pytest.mark.regression
@pytest.mark.parametrize(
    "value_type, value_text, refusal",
    [h[1:] for h in HOSTILE],
    ids=[h[0] for h in HOSTILE],
)
def test_the_hostile_battery_is_422_on_create_never_500(
    client, developer, db_session, value_type, value_text, refusal
):
    key = _key()
    payload = (
        f'{{"key": "{key}", "name": "Hostile", "value_type": "{value_type}", '
        f'"on_value": {value_text}, "off_value": {OFF_TEXT[value_type]}}}'
    )
    response = _raw(client, "POST", f"{COLLECTION}/", developer, payload)
    assert _detail(response) == [(refusal, "on_value", _message(refusal, "on_value"))]
    assert _columns(db_session, key) is None


@pytest.mark.regression
@pytest.mark.parametrize(
    "value_type, value_text, refusal",
    [h[1:] for h in HOSTILE],
    ids=[h[0] for h in HOSTILE],
)
def test_the_hostile_battery_is_422_on_update_never_500(
    client, developer, db_session, value_type, value_text, refusal
):
    flag = _create(client, developer, value_type)
    before = _columns(db_session, flag["key"])
    response = _raw(
        client,
        "PUT",
        f"{COLLECTION}/{flag['id']}",
        developer,
        f'{{"off_value": {value_text}}}',
    )
    assert _detail(response) == [(refusal, "off_value", _message(refusal, "off_value"))]
    assert _columns(db_session, flag["key"]) == before


@pytest.mark.parametrize(
    "value_type, value_text", [b[1:] for b in BOUNDARY], ids=[b[0] for b in BOUNDARY]
)
def test_each_limit_itself_is_accepted_and_stored_as_sent(
    client, developer, db_session, value_type, value_text
):
    key = _key()
    payload = (
        f'{{"key": "{key}", "name": "Boundary", "value_type": "{value_type}", '
        f'"on_value": {value_text}, "off_value": {OFF_TEXT[value_type]}}}'
    )
    response = _raw(client, "POST", f"{COLLECTION}/", developer, payload)
    assert response.status_code == 201, response.text
    assert response.json()["on_value"] == json.loads(value_text)
    stored = db_session.execute(
        text(
            "SELECT on_value = CAST(:sent AS jsonb) FROM feature_flags WHERE key = :k"
        ),
        {"sent": value_text, "k": key},
    ).scalar()
    assert stored is True


# ---------------------------------------------------------------------------
# PF-5: the API's equality is PostgreSQL's
# ---------------------------------------------------------------------------

#: (value_type, on_value text, off_value text, equal under jsonb ``=``).
EQUALITY = [
    ("number", "3", "3.0", True),
    ("json", '{"a": true}', '{"a": 1}', False),
    ("json", '{"a": 1}', '{"a": 1.0}', True),
    ("json", "[1, 2]", "[2, 1]", False),
    ("number", "0", "-0.0", True),
    ("number", "2.5", "2.50", True),
    ("json", '{"a": 1, "b": 2}', '{"b": 2, "a": 1}', True),
    ("json", "[true]", "[1]", False),
    ("json", '{"a": null}', "{}", False),
    ("json", "[]", "{}", False),
    ("json", '{"a": [1, {"b": 2.50}]}', '{"a": [1, {"b": 2.5}]}', True),
    ("string", '"a"', '"A"', False),
    ("string", '"Buy now"', '"Buy now"', True),
]


@pytest.mark.regression
@pytest.mark.parametrize(
    "value_type, on_text, off_text, equal", EQUALITY, ids=lambda v: str(v)
)
def test_the_api_and_the_database_check_agree_on_equality(
    client, developer, db_session, cleanup, value_type, on_text, off_text, equal
):
    """PF-5: one table, through the API and through ``check_flag_values``."""
    key = _key("equality-api")
    cleanup.append(key)
    payload = (
        f'{{"key": "{key}", "name": "Equality", "value_type": "{value_type}", '
        f'"on_value": {on_text}, "off_value": {off_text}}}'
    )
    response = _raw(client, "POST", f"{COLLECTION}/", developer, payload)
    if equal:
        assert _detail(response) == [_equal("off_value")]
    else:
        assert response.status_code == 201, response.text
    api_refused = response.status_code == 422

    db_key = _key("equality-db")
    cleanup.append(db_key)
    try:
        with db_session.get_bind().begin() as conn:
            conn.execute(
                text(
                    f'INSERT INTO "{get_schema_name()}".feature_flags '
                    "(id, key, name, status, rollout_percentage, created_at, "
                    "updated_at, value_type, on_value, off_value) VALUES (:id, :key, "
                    "'Equality', 'INACTIVE', 0, now(), now(), :t, CAST(:on AS jsonb), "
                    "CAST(:off AS jsonb))"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "key": db_key,
                    "t": value_type,
                    "on": on_text,
                    "off": off_text,
                },
            )
        database_refused = False
    except IntegrityError as exc:
        assert "check_flag_values" in str(exc.orig)
        database_refused = True

    assert api_refused is database_refused is equal


# ---------------------------------------------------------------------------
# PF-3 / UX C3 / UX C4: the GET body round trip; value_type is read-only
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_a_boolean_flags_get_body_put_back_is_200_and_stores_no_value(
    client, developer, db_session
):
    """PF-3: the body now carries ``value_type: "boolean"``, ``on_value: true``
    and ``off_value: false``. Written through, the check refuses the row (a
    500); they are accepted and both columns stay NULL."""
    flag = _create(client, developer, is_active=True, rollout_percentage=30)
    body = client.get(f"{COLLECTION}/{flag['id']}", headers=_auth(developer)).json()
    assert (body["value_type"], body["on_value"], body["off_value"]) == (
        "boolean",
        True,
        False,
    )
    before = _columns(db_session, flag["key"])
    assert before["on_null"] is True and before["off_null"] is True

    response = client.put(
        f"{COLLECTION}/{flag['id']}", json=body, headers=_auth(developer)
    )

    assert response.status_code == 200, response.text
    after = _columns(db_session, flag["key"])
    assert after == before
    assert after["on_null"] is True and after["off_null"] is True


@pytest.mark.regression
@pytest.mark.parametrize("value_type", VALUE_TYPES)
def test_every_get_body_can_be_put_back_unchanged(
    client, developer, db_session, value_type
):
    """UX C3: the planted defect (``value_type`` refusing its own stored value)
    fails every type here."""
    flag = _create(client, developer, value_type, targeting_rules=PRO_RULE)
    for surface in ("get", "list"):
        if surface == "get":
            body = client.get(
                f"{COLLECTION}/{flag['id']}", headers=_auth(developer)
            ).json()
        else:
            (body,) = [
                item
                for item in client.get(
                    f"{COLLECTION}/",
                    params={"search": flag["key"]},
                    headers=_auth(developer),
                ).json()["items"]
                if item["key"] == flag["key"]
            ]
        before = _columns(db_session, flag["key"])
        response = client.put(
            f"{COLLECTION}/{flag['id']}", json=body, headers=_auth(developer)
        )
        assert response.status_code == 200, (surface, response.text)
        assert _columns(db_session, flag["key"]) == before, surface


@pytest.mark.regression
@pytest.mark.parametrize("stored", VALUE_TYPES)
def test_value_type_is_read_only(client, developer, db_session, stored):
    """UX C4: every other type answers 422 ``read_only``; the row is unchanged."""
    flag = _create(client, developer, stored)
    before = _columns(db_session, flag["key"])
    for sent in VALUE_TYPES:
        response = client.put(
            f"{COLLECTION}/{flag['id']}",
            json={"value_type": sent},
            headers=_auth(developer),
        )
        if sent == stored:
            assert response.status_code == 200, response.text
        else:
            assert response.json() == {
                "detail": [
                    {
                        "type": "read_only",
                        "loc": ["body", "value_type"],
                        "msg": VALUE_TYPE_READ_ONLY,
                    }
                ]
            }, (stored, sent)
        assert _columns(db_session, flag["key"]) == before, (stored, sent)


# ---------------------------------------------------------------------------
# Evaluation (UX C2, QA B1, B7) and the bulk route (UX C5)
# ---------------------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize("value_type", VALUE_TYPES)
def test_evaluation_serves_on_exactly_when_enabled_and_off_otherwise(
    client, db_session, developer, api_key, make_feature_flag, value_type
):
    """``value == (on_value if enabled else off_value)`` in every state, with
    ``config`` null and ``value_type`` present. A boolean flag's ``value`` is
    ``enabled``. The planted defect "serve on_value when inactive" fails the
    inactive, archived and rollback rows."""
    on, off = PAIRS.get(value_type, (True, False))
    # Rule in at 100%; no rule matched -> rollout 0%.
    rule_in = _create(
        client, developer, value_type, is_active=True, targeting_rules=PRO_RULE
    )
    # Rule matched but out (its own 0%); no rule matched -> rollout 100%.
    rule_out = _create(
        client,
        developer,
        value_type,
        is_active=True,
        rollout_percentage=100,
        targeting_rules={**PRO_RULE, "rollout_percentage": 0},
    )
    k1, k2 = rule_in["key"], rule_out["key"]

    def check(key, context, enabled, reason):
        expected = _served(key, value_type, enabled, reason, on if enabled else off)
        assert _evaluate(client, api_key, key, "user-1", context) == expected
        posted = client.post(
            f"{COLLECTION}/evaluate/{key}",
            json={"user_id": "user-1", "context": context},
            headers={"X-API-Key": api_key},
        )
        assert posted.status_code == 200, posted.text
        assert posted.json() == expected

    check(k1, PRO, True, "targeting_rule")
    check(k1, FREE, False, "rollout")
    check(k2, PRO, False, "targeting_rule")
    check(k2, FREE, True, "rollout")

    # Off: deactivated, and archived.
    assert (
        client.post(
            f"{COLLECTION}/{rule_in['id']}/deactivate", headers=_auth(developer)
        ).status_code
        == 200
    )
    check(k1, PRO, False, "inactive")
    archived = client.post(
        f"{COLLECTION}/bulk-toggle",
        json={"action": "archive", "flag_ids": [rule_out["id"]]},
        headers=_auth(developer),
    )
    assert archived.status_code == 200, archived.text
    check(k2, FREE, False, "inactive")

    # ``reason: "error"``: a stored pattern RE2 refuses (the API refuses it on
    # write, so the row is written directly).
    broken = make_feature_flag(
        key=_key("error"),
        status=FeatureFlagStatus.ACTIVE,
        rollout_percentage=100,
        value_type=value_type,
        on_value=None if value_type == "boolean" else on,
        off_value=None if value_type == "boolean" else off,
        targeting_rules={
            "logical_operator": "AND",
            "rollout_percentage": 0,
            "groups": [
                {
                    "logical_operator": "AND",
                    "conditions": [
                        {"attribute": "email", "operator": "regex", "value": "[a"}
                    ],
                }
            ],
        },
    )
    check(broken.key, {"email": "a@b.c"}, False, "error")


@pytest.mark.regression
@pytest.mark.parametrize("value_type", ["string", "number", "json"])
def test_a_safety_rollback_serves_the_off_value(
    client, db_session, developer, api_key, value_type
):
    """T229: a rollback to 0% serves everyone the off value; a partial one
    serves it to the users it removes, while a rule-matched user keeps the
    rule's percentage (T108)."""
    on, off = PAIRS[value_type]
    killed = _create(
        client, developer, value_type, is_active=True, rollout_percentage=100
    )
    lowered = _create(
        client,
        developer,
        value_type,
        is_active=True,
        rollout_percentage=100,
        targeting_rules=PRO_RULE,
    )
    superuser = _make_user(db_session, UserRole.ADMIN, is_superuser=True)
    for flag, percentage in ((killed, 0), (lowered, 1)):
        response = client.post(
            f"/api/v1/safety/feature-flags/{flag['id']}/rollback",
            params={"percentage": percentage, "reason": "values"},
            headers=_auth(superuser),
        )
        assert response.status_code == 200, response.text

    assert _evaluate(client, api_key, killed["key"], "user-1") == _served(
        killed["key"], value_type, False, "inactive", off
    )
    outside = next(
        u for u in (f"user-{n}" for n in range(100)) if _bucket(u, lowered["key"]) >= 1
    )
    assert _evaluate(client, api_key, lowered["key"], outside, FREE) == _served(
        lowered["key"], value_type, False, "rollout", off
    )
    assert _evaluate(client, api_key, lowered["key"], outside, PRO) == _served(
        lowered["key"], value_type, True, "targeting_rule", on
    )


@pytest.mark.regression
def test_the_bulk_route_answers_true_or_false_for_a_value_flag(
    client, developer, api_key
):
    """UX C5: ``/feature-flags/user/{id}`` stays ``{key: bool}``; a value flag
    reads true for a user who gets its on value."""
    flag = _create(client, developer, "json", is_active=True, targeting_rules=PRO_RULE)
    for context, expected in ((PRO, True), (FREE, False)):
        response = client.get(
            f"{COLLECTION}/user/user-1",
            params={"context": json.dumps(context)},
            headers={"X-API-Key": api_key},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body[flag["key"]] is expected
        assert all(type(v) is bool for v in body.values()), body


# ---------------------------------------------------------------------------
# PF-1: what PostgreSQL refuses, and stored values outside the rules
# ---------------------------------------------------------------------------

_INSERT = (
    'INSERT INTO "{schema}".feature_flags (id, key, name, status, '
    "rollout_percentage, created_at, updated_at, value_type, on_value, off_value) "
    "VALUES (:id, :key, 'Stored', 'ACTIVE', 100, now(), now(), :t, "
    "CAST(:on AS jsonb), CAST(:off AS jsonb))"
)


def _insert(db_session, key, value_type, on_text, off_text):
    with db_session.get_bind().begin() as conn:
        conn.execute(
            text(_INSERT.format(schema=get_schema_name())),
            {
                "id": str(uuid.uuid4()),
                "key": key,
                "t": value_type,
                "on": on_text,
                "off": off_text,
            },
        )


@pytest.mark.regression
@pytest.mark.parametrize(
    "value_type, on_text",
    [
        ("number", "NaN"),
        ("number", "Infinity"),
        ("number", "-Infinity"),
        ("string", '"\\u0000"'),
        ("string", '"\\ud800"'),
    ],
    ids=["nan", "infinity", "minus-infinity", "nul", "lone-surrogate"],
)
def test_postgresql_refuses_what_the_api_refuses_at_insert(
    db_session, cleanup, value_type, on_text
):
    """PF-1 (a): no such row can be stored, so none can reach a route."""
    key = _key("refused")
    cleanup.append(key)
    with pytest.raises(DataError):
        _insert(db_session, key, value_type, on_text, OFF_TEXT[value_type])
    assert _columns(db_session, key) is None


#: Values PostgreSQL stores and psycopg decodes, but outside the value rules:
#: only a direct database write (or a release with wider limits, then an
#: image rollback) leaves one.
UNSERVABLE = [
    ("1e400", "number", "1e400", "1"),
    ("2^60", "number", "1", str(2**60)),
    ("8193-bytes", "json", '{"a":"' + "x" * 8185 + '"}', "[]"),
    ("eleven-deep", "json", "[" * 11 + "1" + "]" * 11, "[]"),
]


@pytest.mark.regression
@pytest.mark.parametrize(
    "value_type, on_text, off_text",
    [u[1:] for u in UNSERVABLE],
    ids=[u[0] for u in UNSERVABLE],
)
def test_a_stored_value_outside_the_rules_is_answered_without_a_value(
    client, db_session, developer, api_key, cleanup, value_type, on_text, off_text
):
    """PF-1 (b): evaluate answers ``enabled`` and ``value_type`` and no
    ``value``, for every user; the ruleset, the flag routes and the bulk route
    answer 200. (F4's ruleset carries no values; F5's makes such a flag a
    remote entry.)"""
    key = _key("unservable")
    cleanup.append(key)
    _insert(db_session, key, value_type, on_text, off_text)

    for context in (None, PRO):
        answer = _evaluate(client, api_key, key, "user-1", context)
        assert answer == {
            "key": key,
            "enabled": True,
            "value_type": value_type,
            "config": None,
            "reason": "rollout",
        }
    ruleset = client.get("/api/v1/sdk/ruleset", headers={"X-API-Key": api_key})
    assert ruleset.status_code == 200, ruleset.text
    (entry,) = [e for e in ruleset.json()["flags"] if e["key"] == key]
    assert set(entry) == {
        "key",
        "active",
        "evaluation",
        "rollout_percentage",
        "rules",
        "default_rule",
    }
    flag_id = db_session.execute(
        text("SELECT id FROM feature_flags WHERE key = :k"), {"k": key}
    ).scalar()
    for path in (f"{COLLECTION}/{flag_id}", f"{COLLECTION}/?search={key}"):
        response = client.get(path, headers=_auth(developer))
        assert response.status_code == 200, (path, response.text)
    bulk = client.get(f"{COLLECTION}/user/user-1", headers={"X-API-Key": api_key})
    assert bulk.status_code == 200 and bulk.json()[key] is True, bulk.text


# ---------------------------------------------------------------------------
# D11 (QA B10) and the audit (Plan F ruling 3)
# ---------------------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize("role", [UserRole.ANALYST, UserRole.VIEWER])
def test_analyst_and_viewer_cannot_set_a_value(client, developer, db_session, role):
    """D11: by role. The DEVELOPER (not a superuser) creates and changes the
    flag above and in every test here; ANALYST and VIEWER get 403 and nothing
    changes."""
    flag = _create(client, developer, "string")
    changed = client.put(
        f"{COLLECTION}/{flag['id']}",
        json={"on_value": "Pay"},
        headers=_auth(developer),
    )
    assert changed.status_code == 200, changed.text
    before = _columns(db_session, flag["key"])
    reader = _make_user(db_session, role)

    created = client.post(f"{COLLECTION}/", json=_body("number"), headers=_auth(reader))
    updated = client.put(
        f"{COLLECTION}/{flag['id']}",
        json={"on_value": "Pay now"},
        headers=_auth(reader),
    )

    assert (created.status_code, updated.status_code) == (403, 403)
    assert _columns(db_session, flag["key"]) == before


@pytest.mark.regression
def test_the_audit_records_the_value_type_and_the_values_by_name(
    client, developer, db_session
):
    flag = _create(
        client, developer, "string", on_value="s3cr3t on", off_value="s3cr3t off"
    )
    response = client.put(
        f"{COLLECTION}/{flag['id']}",
        json={"on_value": "s3cr3t new", "rollout_percentage": 20},
        headers=_auth(developer),
    )
    assert response.status_code == 200, response.text

    db_session.expire_all()
    entries = {
        row.action_type: row
        for row in db_session.query(AuditLog).filter(
            AuditLog.entity_id == uuid.UUID(flag["id"])
        )
    }
    created = json.loads(entries["feature_flag_create"].new_value)
    assert created["value_type"] == "string"
    old = json.loads(entries["feature_flag_update"].old_value)
    new = json.loads(entries["feature_flag_update"].new_value)
    assert old == {"rollout_percentage": 0}
    assert new == {"rollout_percentage": 20, "changed_fields": ["on_value"]}

    compliance = (
        db_session.query(ComplianceAuditEvent)
        .filter(ComplianceAuditEvent.resource_id == flag["id"])
        .all()
    )
    assert {event.action.value for event in compliance} >= {"CREATE", "UPDATE"}
    for event in compliance:
        assert event.new_value["value_type"] == "string"
    stored = json.dumps(
        [[e.old_value, e.new_value] for e in list(entries.values())]
        + [[e.old_value, e.new_value] for e in compliance],
        default=str,
    )
    assert "s3cr3t" not in stored
