"""A flag's targeting rules or tags holding NUL or an unpaired surrogate
answer 422 (#2163).

PostgreSQL refuses U+0000 in JSONB, and a lone surrogate cannot be encoded at
all, so a flag create or update holding one in ``targeting_rules`` or ``tags``
failed when it was saved and answered 500. Both fields are now checked where
they are declared (``FeatureFlagCreate``, ``FeatureFlagUpdate`` and
``FlagEnvironmentUpdate``), with the helper #961 uses: 422 at
``["body", <field>]``, a fixed message that never repeats the value, nothing
saved and no audit entry.

The check runs before the rules are read, so rules holding the character that
another check refused (an attribute, an operator, a semantic version) now get
this message. It runs as the body is read: authentication still answers 401
first, and a create by a role that may not create a flag still answers 403
first (both are dependencies); on both PUTs the 422 comes before the 403 and
the 404 the route answers itself. A valid surrogate pair (an emoji) is stored
as before, and a GET body still PUTs back.

The bodies are sent as raw JSON text: a JSON client cannot encode ``\\ud800``,
and sending the escapes is what a client on the wire does.
"""

from __future__ import annotations

import copy
import json
import uuid
from typing import Any, Callable, Dict

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.core.config import settings
from backend.app.core.security import create_local_access_token
from backend.app.main import app
from backend.app.models.audit_log import AuditLog
from backend.app.models.feature_flag import (
    FeatureFlag,
    FeatureFlagStatus,
    FlagEnvironmentSetting,
)
from backend.app.models.user import User, UserRole

pytestmark = [pytest.mark.integration, pytest.mark.requires_db, pytest.mark.regression]

PREFIX = "ft2163"
SCHEMA = "test_experimentation"
FLAGS = "/api/v1/feature-flags"
ENVIRONMENTS = ("production", "staging", "development")
JSON = {"Content-Type": "application/json"}

#: Marks the refused text, so a test can tell whether an answer repeats it.
MARK = "zq2163mark"
#: An ASCII placeholder, replaced in the raw JSON text by the escape sent.
X = "QQ2163QQ"
#: The two characters, as JSON escapes: NUL and an unpaired surrogate.
CHARACTERS = {"nul": "\\u0000", "surrogate": "\\ud800"}
#: A valid surrogate pair: U+1F600, an emoji.
EMOJI_ESCAPE = "\\ud83d\\ude00"
EMOJI = "\U0001f600"

DASHBOARD = {
    "logical_operator": "AND",
    "groups": [
        {
            "id": "g1",
            "logical_operator": "AND",
            "conditions": [
                {
                    "id": "c1",
                    "attribute": "country",
                    "operator": "equals",
                    "value": "US",
                }
            ],
        }
    ],
}
NATIVE = {
    "rules": [
        {
            "id": "r1",
            "name": "n",
            "description": "d",
            "rule": {
                "operator": "and",
                "conditions": [
                    {"attribute": "country", "operator": "eq", "value": "US"}
                ],
            },
            "rollout_percentage": 100,
        }
    ]
}


def _dashboard(change: Callable[[Dict[str, Any]], Any]) -> Dict[str, Any]:
    rules = copy.deepcopy(DASHBOARD)
    change(rules)
    return rules


def _native(change: Callable[[Dict[str, Any]], Any]) -> Dict[str, Any]:
    rules = copy.deepcopy(NATIVE)
    change(rules)
    return rules


def _dashboard_condition(rules):
    return rules["groups"][0]["conditions"][0]


def _native_rule(rules):
    return rules["rules"][0]


def _native_condition(rules):
    return rules["rules"][0]["rule"]["conditions"][0]


#: Where in the rules the character is, in either shape: the places #2163
#: measured answering 500.
RULE_PLACES = {
    "dashboard-value": _dashboard(
        lambda r: _dashboard_condition(r).update(value="US" + MARK + X)
    ),
    "dashboard-list-item": _dashboard(
        lambda r: _dashboard_condition(r).update(
            operator="in", value=["US", "CA" + MARK + X]
        )
    ),
    "dashboard-condition-id": _dashboard(
        lambda r: _dashboard_condition(r).update(id="c1" + MARK + X)
    ),
    "dashboard-group-id": _dashboard(
        lambda r: r["groups"][0].update(id="g1" + MARK + X)
    ),
    "native-value": _native(
        lambda r: _native_condition(r).update(value="US" + MARK + X)
    ),
    "native-list-item": _native(
        lambda r: _native_condition(r).update(
            operator="in", value=["US", "CA" + MARK + X]
        )
    ),
    "native-rule-id": _native(lambda r: _native_rule(r).update(id="r1" + MARK + X)),
    "native-rule-name": _native(lambda r: _native_rule(r).update(name="n" + MARK + X)),
    "native-rule-description": _native(
        lambda r: _native_rule(r).update(description="d" + MARK + X)
    ),
    "native-validation-schema-key": _native(
        lambda r: _native_condition(r).update(
            validation_schema={"type" + MARK + X: "string"}
        )
    ),
}

#: Places another check refused before this one existed: the character in a
#: condition's attribute, operator or semantic version.
READ_BEFORE = {
    "dashboard-attribute": _dashboard(
        lambda r: _dashboard_condition(r).update(attribute="country" + X)
    ),
    "native-attribute": _native(
        lambda r: _native_condition(r).update(attribute="country" + X)
    ),
    "dashboard-operator": _dashboard(
        lambda r: _dashboard_condition(r).update(operator="equals" + X)
    ),
    "native-operator": _native(
        lambda r: _native_condition(r).update(operator="eq" + X)
    ),
    "dashboard-semantic-version": _dashboard(
        lambda r: _dashboard_condition(r).update(
            operator="semver_gte", value="1.2.3" + X
        )
    ),
    "native-semantic-version": _native(
        lambda r: _native_condition(r).update(
            operator="semantic_version", value="1.2.3" + X
        )
    ),
}

TAG_PLACES = {
    "the-only-tag": ["a" + MARK + X],
    "a-second-tag": ["ok", "b" + MARK + X],
}

CREATE_PATHS = {"collection-slash": f"{FLAGS}/", "collection": FLAGS}


def _refusal(field: str) -> Dict[str, Any]:
    return {
        "type": "value_error",
        "loc": ["body", field],
        "msg": f"Value error, {field} must not contain NUL or unpaired surrogate characters",
    }


def _raw(body: Dict[str, Any], escape: str) -> bytes:
    """*body* as JSON text, with the placeholder replaced by *escape*."""
    sent = json.dumps(body)
    assert X in sent
    return sent.replace(X, escape).encode("ascii")


def _assert_refused(response, field: str) -> None:
    assert response.status_code == 422, response.text
    assert MARK not in response.text, "the answer repeats the value sent"
    errors = response.json()["detail"]
    assert [{k: e[k] for k in ("type", "loc", "msg")} for e in errors] == [
        _refusal(field)
    ]


# --- fixtures -----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _local_auth(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "CACHE_ENABLED", False)


def _user(db_session, role: UserRole) -> User:
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"{PREFIX}_{role.value}_{suffix}",
        email=f"{PREFIX}_{suffix}@flagtext.test",
        full_name="Flag Text User",
        hashed_password="unused: this user signs in by token only",
        is_active=True,
        is_superuser=False,
        role=role,
    )
    db_session.add(user)
    db_session.commit()
    return user


@pytest.fixture
def developer(db_session) -> User:
    return _user(db_session, UserRole.DEVELOPER)


@pytest.fixture
def viewer(db_session) -> User:
    return _user(db_session, UserRole.VIEWER)


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
    """The real routes and the real authentication; only the database is the
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
def _cleanup(db_session):
    """Leave nothing behind: other suites list flags and count entries."""
    yield
    s = db_session
    s.rollback()
    flag_ids = [
        r.id
        for r in s.query(FeatureFlag.id).filter(FeatureFlag.key.like(f"{PREFIX}-%"))
    ]
    user_ids = [
        r.id for r in s.query(User.id).filter(User.username.like(f"{PREFIX}_%"))
    ]
    s.query(AuditLog).filter(
        AuditLog.entity_id.in_(flag_ids) | AuditLog.user_id.in_(user_ids)
    ).delete(synchronize_session=False)
    s.query(FeatureFlag).filter(FeatureFlag.id.in_(flag_ids)).delete(
        synchronize_session=False
    )
    s.query(User).filter(User.id.in_(user_ids)).delete(synchronize_session=False)
    s.commit()


# --- helpers ------------------------------------------------------------------


def _auth(user: User) -> Dict[str, str]:
    return {
        **JSON,
        "Authorization": f"Bearer {create_local_access_token(user)}",
    }


def _key() -> str:
    return f"{PREFIX}-{uuid.uuid4().hex[:10]}"


def _flag(db_session, owner: User) -> FeatureFlag:
    """A flag with rules and tags in production, and a staging side row; no
    development row."""
    flag = FeatureFlag(
        key=_key(),
        name=f"{PREFIX} flag",
        status=FeatureFlagStatus.INACTIVE,
        rollout_percentage=10,
        targeting_rules=copy.deepcopy(DASHBOARD),
        tags=["kept"],
        owner_id=owner.id,
    )
    db_session.add(flag)
    db_session.commit()
    db_session.add(
        FlagEnvironmentSetting(
            feature_flag_id=flag.id,
            environment="staging",
            is_active=True,
            rollout_percentage=20,
            targeting_rules=copy.deepcopy(NATIVE),
        )
    )
    db_session.commit()
    return flag


def _stored(fresh, flag_id) -> tuple:
    """The flag row, its side rows and its audit entries, as stored."""
    session = fresh()
    try:
        row = session.execute(
            text("select row_to_json(f)::text from feature_flags f where id = :i"),
            {"i": str(flag_id)},
        ).scalar()
        side = (
            session.execute(
                text(
                    "select row_to_json(s)::text from flag_environment_settings s "
                    "where feature_flag_id = :i order by environment"
                ),
                {"i": str(flag_id)},
            )
            .scalars()
            .all()
        )
        assert row is not None and side, "the flag and its staging row are stored"
        entries = session.query(AuditLog).filter(AuditLog.entity_id == flag_id).count()
        return row, side, entries
    finally:
        session.close()


def _totals(fresh) -> tuple:
    """How many flags, side rows and audit entries there are."""
    session = fresh()
    try:
        return (
            session.query(FeatureFlag).count(),
            session.query(FlagEnvironmentSetting).count(),
            session.query(AuditLog).count(),
        )
    finally:
        session.close()


def _create(client, user, body: Dict[str, Any], escape: str, path: str = f"{FLAGS}/"):
    return client.post(path, content=_raw(body, escape), headers=_auth(user))


def _update(client, user, flag_id, body: Dict[str, Any], escape: str):
    return client.put(
        f"{FLAGS}/{flag_id}", content=_raw(body, escape), headers=_auth(user)
    )


def _update_environment(client, user, flag_id, environment, body, escape: str):
    return client.put(
        f"{FLAGS}/{flag_id}/environments/{environment}",
        content=_raw(body, escape),
        headers=_auth(user),
    )


def _assert_create_refused(client, fresh, user, path, body, escape, field):
    before = _totals(fresh)

    _assert_refused(_create(client, user, body, escape, path), field)

    assert _totals(fresh) == before
    session = fresh()
    try:
        assert session.query(FeatureFlag).filter_by(key=body["key"]).count() == 0
    finally:
        session.close()


# --- each route refuses the character, and nothing is saved -------------------


@pytest.mark.parametrize("character", CHARACTERS)
@pytest.mark.parametrize("place", RULE_PLACES)
@pytest.mark.parametrize("path", CREATE_PATHS)
def test_a_create_refuses_the_character_in_targeting_rules(
    client, fresh, developer, path, place, character
):
    body = {"key": _key(), "name": "n", "targeting_rules": RULE_PLACES[place]}

    _assert_create_refused(
        client,
        fresh,
        developer,
        CREATE_PATHS[path],
        body,
        CHARACTERS[character],
        "targeting_rules",
    )


@pytest.mark.parametrize("character", CHARACTERS)
@pytest.mark.parametrize("place", TAG_PLACES)
@pytest.mark.parametrize("path", CREATE_PATHS)
def test_a_create_refuses_the_character_in_tags(
    client, fresh, developer, path, place, character
):
    body = {"key": _key(), "name": "n", "tags": TAG_PLACES[place]}

    _assert_create_refused(
        client,
        fresh,
        developer,
        CREATE_PATHS[path],
        body,
        CHARACTERS[character],
        "tags",
    )


@pytest.mark.parametrize("character", CHARACTERS)
@pytest.mark.parametrize("place", RULE_PLACES)
def test_an_update_refuses_the_character_in_targeting_rules(
    client, fresh, db_session, developer, place, character
):
    flag = _flag(db_session, developer)
    before = _stored(fresh, flag.id)

    response = _update(
        client,
        developer,
        flag.id,
        {"targeting_rules": RULE_PLACES[place]},
        CHARACTERS[character],
    )

    _assert_refused(response, "targeting_rules")
    assert _stored(fresh, flag.id) == before


@pytest.mark.parametrize("character", CHARACTERS)
@pytest.mark.parametrize("place", TAG_PLACES)
def test_an_update_refuses_the_character_in_tags(
    client, fresh, db_session, developer, place, character
):
    flag = _flag(db_session, developer)
    before = _stored(fresh, flag.id)

    response = _update(
        client, developer, flag.id, {"tags": TAG_PLACES[place]}, CHARACTERS[character]
    )

    _assert_refused(response, "tags")
    assert _stored(fresh, flag.id) == before


@pytest.mark.parametrize("character", CHARACTERS)
@pytest.mark.parametrize("place", RULE_PLACES)
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_an_environment_update_refuses_the_character_in_targeting_rules(
    client, fresh, db_session, developer, environment, place, character
):
    flag = _flag(db_session, developer)
    before = _stored(fresh, flag.id)

    response = _update_environment(
        client,
        developer,
        flag.id,
        environment,
        {"targeting_rules": RULE_PLACES[place]},
        CHARACTERS[character],
    )

    _assert_refused(response, "targeting_rules")
    assert _stored(fresh, flag.id) == before


# --- the rules are not read first ---------------------------------------------


@pytest.mark.parametrize("character", CHARACTERS)
@pytest.mark.parametrize("place", READ_BEFORE)
@pytest.mark.parametrize("route", ("create", "update", "staging"))
def test_rules_another_check_refused_now_get_this_answer(
    client, fresh, db_session, developer, route, place, character
):
    """The character in an attribute, an operator or a semantic version
    answered that check's 422; the rules are now refused before they are
    read, with this one."""
    escape = CHARACTERS[character]
    rules = READ_BEFORE[place]
    if route == "create":
        body = {"key": _key(), "name": "n", "targeting_rules": rules}
        _assert_create_refused(
            client, fresh, developer, f"{FLAGS}/", body, escape, "targeting_rules"
        )
        return
    flag = _flag(db_session, developer)
    before = _stored(fresh, flag.id)
    if route == "update":
        response = _update(
            client, developer, flag.id, {"targeting_rules": rules}, escape
        )
    else:
        response = _update_environment(
            client, developer, flag.id, "staging", {"targeting_rules": rules}, escape
        )
    _assert_refused(response, "targeting_rules")
    assert _stored(fresh, flag.id) == before


# --- what is still accepted ---------------------------------------------------


def test_an_emoji_is_still_saved_as_sent(client, fresh, db_session, developer):
    """A valid surrogate pair is a character the database stores: an emoji in
    the tags or the rules is saved, by every route, as before."""
    rules = _dashboard(lambda r: _dashboard_condition(r).update(value="US" + X))
    created = _create(
        client,
        developer,
        {"key": _key(), "name": "n", "tags": ["a" + X], "targeting_rules": rules},
        EMOJI_ESCAPE,
    )
    assert created.status_code == 201, created.text
    assert created.json()["tags"] == ["a" + EMOJI]
    assert _dashboard_condition(created.json()["targeting_rules"])["value"] == (
        "US" + EMOJI
    )

    flag = _flag(db_session, developer)
    updated = _update(
        client,
        developer,
        flag.id,
        {"tags": ["b" + X], "targeting_rules": rules},
        EMOJI_ESCAPE,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["tags"] == ["b" + EMOJI]

    staged = _update_environment(
        client, developer, flag.id, "staging", {"targeting_rules": rules}, EMOJI_ESCAPE
    )
    assert staged.status_code == 200, staged.text
    session = fresh()
    try:
        stored = session.get(FeatureFlag, flag.id)
        setting = (
            session.query(FlagEnvironmentSetting)
            .filter_by(feature_flag_id=flag.id, environment="staging")
            .one()
        )
        assert stored.tags == ["b" + EMOJI]
        assert _dashboard_condition(stored.targeting_rules)["value"] == "US" + EMOJI
        assert _dashboard_condition(setting.targeting_rules)["value"] == "US" + EMOJI
    finally:
        session.close()


@pytest.mark.parametrize("shape", ("dashboard", "native"))
def test_a_get_body_still_puts_back(client, developer, shape):
    """The whole GET body of a flag PUTs back unchanged; on staging, the GET's
    is_active, rollout_percentage and targeting_rules do."""
    rules = DASHBOARD if shape == "dashboard" else NATIVE
    created = client.post(
        f"{FLAGS}/",
        json={"key": _key(), "name": "n", "tags": ["x", "y"], "targeting_rules": rules},
        headers=_auth(developer),
    )
    assert created.status_code == 201, created.text
    flag_id = created.json()["id"]

    got = client.get(f"{FLAGS}/{flag_id}", headers=_auth(developer)).json()
    back = client.put(f"{FLAGS}/{flag_id}", json=got, headers=_auth(developer))
    assert back.status_code == 200, back.text
    again = client.get(f"{FLAGS}/{flag_id}", headers=_auth(developer)).json()
    got.pop("updated_at")
    again.pop("updated_at")
    assert again == got

    staging = f"{FLAGS}/{flag_id}/environments/staging"
    set_up = client.put(
        staging,
        json={"is_active": True, "rollout_percentage": 7, "targeting_rules": rules},
        headers=_auth(developer),
    )
    assert set_up.status_code == 200, set_up.text
    fields = ("is_active", "rollout_percentage", "targeting_rules")
    read = client.get(staging, headers=_auth(developer)).json()
    sent = {field: read[field] for field in fields}
    put_back = client.put(staging, json=sent, headers=_auth(developer))
    assert put_back.status_code == 200, put_back.text
    reread = client.get(staging, headers=_auth(developer)).json()
    assert {field: reread[field] for field in fields} == sent


# --- the order of the refusals ------------------------------------------------

NUL_TAGS = {"tags": ["a" + MARK + X]}
NUL_RULES = {"targeting_rules": RULE_PLACES["dashboard-value"]}
NUL = CHARACTERS["nul"]


def test_authentication_still_answers_401_first(client, db_session, developer):
    flag = _flag(db_session, developer)
    for method, path, body in (
        ("POST", f"{FLAGS}/", {"key": _key(), "name": "n", **NUL_TAGS}),
        ("PUT", f"{FLAGS}/{flag.id}", NUL_TAGS),
        ("PUT", f"{FLAGS}/{flag.id}/environments/staging", NUL_RULES),
    ):
        response = client.request(method, path, content=_raw(body, NUL), headers=JSON)
        assert response.status_code == 401, (path, response.text)
        assert MARK not in response.text


def test_a_create_by_a_role_that_may_not_create_still_answers_403_first(
    client, fresh, viewer
):
    before = _totals(fresh)

    refused = _create(client, viewer, {"key": _key(), "name": "n", **NUL_TAGS}, NUL)

    assert refused.status_code == 403, refused.text
    assert MARK not in refused.text
    assert _totals(fresh) == before


def test_on_an_update_the_422_comes_before_the_403(
    client, fresh, db_session, developer, viewer
):
    flag = _flag(db_session, developer)
    before = _stored(fresh, flag.id)

    _assert_refused(_update(client, viewer, flag.id, NUL_TAGS, NUL), "tags")
    # A storable body is still the 403 it was.
    assert _update(client, viewer, flag.id, NUL_TAGS, "").status_code == 403
    assert _stored(fresh, flag.id) == before


def test_on_an_update_the_422_comes_before_the_404(client, developer):
    unknown = uuid.uuid4()

    _assert_refused(_update(client, developer, unknown, NUL_TAGS, NUL), "tags")
    assert _update(client, developer, unknown, NUL_TAGS, "").status_code == 404


def test_on_an_environment_update_the_422_comes_before_the_403(
    client, fresh, db_session, developer, viewer
):
    flag = _flag(db_session, developer)
    before = _stored(fresh, flag.id)

    _assert_refused(
        _update_environment(client, viewer, flag.id, "staging", NUL_RULES, NUL),
        "targeting_rules",
    )
    assert (
        _update_environment(client, viewer, flag.id, "staging", NUL_RULES, "")
    ).status_code == 403
    assert _stored(fresh, flag.id) == before


def test_on_an_environment_update_the_422_comes_before_the_404(client, developer):
    unknown = uuid.uuid4()

    _assert_refused(
        _update_environment(client, developer, unknown, "staging", NUL_RULES, NUL),
        "targeting_rules",
    )
    assert (
        _update_environment(client, developer, unknown, "staging", NUL_RULES, "")
    ).status_code == 404
