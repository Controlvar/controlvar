"""A segment's name, description or rules holding NUL or an unpaired surrogate
answer 422 (#2175).

PostgreSQL refuses U+0000 in ``varchar``, ``text`` and JSONB, and a lone
surrogate cannot be encoded at all, so a segment create holding one failed
when it was saved and answered 500; an update answered 500 for the rules and
404, with the driver's text, for a name or a description. Nothing was saved.
The create and update bodies (``SegmentCreateBody``, ``SegmentUpdateBody``)
now refuse the three fields with the helper #961 uses: 422 at
``["body", <field>]``, a fixed message that never repeats the value, nothing
saved and no audit entry.

The check runs as the body is read and before the model reads it, so a body
another check refused first (the rules check for an attribute or an operator,
the string check for a surrogate in a name) now gets this message. The
permission check and the lookup are the route's own, after the body is read:
authentication still answers 401 first, and the 422 comes before the 403 and
the 404, as every other body 422 on these routes does. A valid surrogate pair
(an emoji) is stored as before. The preview takes the create's body but
stores nothing, and answers such a body as before.

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
from backend.app.models.segment import Segment
from backend.app.models.segment import SegmentStatus as ModelSegmentStatus
from backend.app.models.user import User, UserRole

pytestmark = [pytest.mark.integration, pytest.mark.requires_db, pytest.mark.regression]

PREFIX = "st2175"
SCHEMA = "test_experimentation"
SEGMENTS = "/api/v1/segments"
JSON = {"Content-Type": "application/json"}

#: Marks the refused text, so a test can tell whether an answer repeats it.
MARK = "zq2175mark"
#: An ASCII placeholder, replaced in the raw JSON text by the escape sent.
X = "QQ2175QQ"
#: The two characters, as JSON escapes: NUL and an unpaired surrogate.
CHARACTERS = {"nul": "\\u0000", "surrogate": "\\ud800"}
#: A valid surrogate pair: U+1F600, an emoji.
EMOJI_ESCAPE = "\\ud83d\\ude00"
EMOJI = "\U0001f600"

RULES = {
    "logical_operator": "AND",
    "groups": [
        {
            "logical_operator": "AND",
            "conditions": [
                {"attribute": "country", "operator": "equals", "value": "US"}
            ],
        }
    ],
}


def _rules(change: Callable[[Dict[str, Any]], Any]) -> Dict[str, Any]:
    rules = copy.deepcopy(RULES)
    change(rules)
    return rules


def _condition(rules):
    return rules["groups"][0]["conditions"][0]


#: Where in the rules the character is: the places #2175 measured answering
#: 500 (a pattern with NUL; with a surrogate the pattern check answered 422).
RULE_PLACES = {
    "value": _rules(lambda r: _condition(r).update(value="US" + MARK + X)),
    "list-item": _rules(
        lambda r: _condition(r).update(operator="in", value=["US", "CA" + MARK + X])
    ),
    "condition-id": _rules(lambda r: _condition(r).update(id="c1" + MARK + X)),
    "group-id": _rules(lambda r: r["groups"][0].update(id="g1" + MARK + X)),
    "pattern": _rules(
        lambda r: _condition(r).update(operator="regex", value="^US" + MARK + X)
    ),
}

#: Places another check refused before this one existed.
READ_BEFORE = {
    "attribute": _rules(lambda r: _condition(r).update(attribute="country" + X)),
    "operator": _rules(lambda r: _condition(r).update(operator="equals" + X)),
    "logical-operator": _rules(lambda r: r.update(logical_operator="AND" + X)),
    "group-logical-operator": _rules(
        lambda r: r["groups"][0].update(logical_operator="AND" + X)
    ),
    "semantic-version": _rules(
        lambda r: _condition(r).update(operator="semver_gte", value="1.2.3" + X)
    ),
    "unknown-key": _rules(lambda r: _condition(r).update({"z" + X: 1})),
}

#: The two text fields, as a create sends them: for each kind of segment.
TEXT_FIELDS = ("name", "description")
KINDS = ("rules", "id_list")

CREATE_PATHS = {"collection": SEGMENTS, "collection-slash": f"{SEGMENTS}/"}


def _refusal(field: str) -> Dict[str, Any]:
    """The whole error, as a flag's targeting rules get it (#2163): the 422
    handler writes a ``ValueError`` in ``ctx`` as ``{}``."""
    return {
        "type": "value_error",
        "loc": ["body", field],
        "msg": f"Value error, {field} must not contain NUL or unpaired surrogate characters",
        "ctx": {"error": {}},
    }


def _raw(body: Dict[str, Any], escape: str) -> bytes:
    """*body* as JSON text, with the placeholder replaced by *escape*."""
    sent = json.dumps(body)
    assert X in sent
    return sent.replace(X, escape).encode("ascii")


def _assert_refused(response, *fields: str) -> None:
    assert response.status_code == 422, response.text
    assert MARK not in response.text, "the answer repeats the value sent"
    assert response.json()["detail"] == [_refusal(field) for field in fields]


def _name() -> str:
    return f"{PREFIX} {uuid.uuid4().hex[:10]}"


def _create_body(kind: str, **fields) -> Dict[str, Any]:
    body: Dict[str, Any] = {"name": _name(), "kind": kind}
    if kind == "rules":
        body["rules"] = RULES
    body.update(fields)
    return body


def _with_text(field: str) -> str:
    """The field's text holding the character (a name keeps the prefix, so
    one saved by mistake is still cleaned up)."""
    return (f"{PREFIX} " if field == "name" else "d") + MARK + X


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
        email=f"{PREFIX}_{suffix}@segmenttext.test",
        full_name="Segment Text User",
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
    """Leave nothing behind: other suites list segments and count entries."""
    yield
    s = db_session
    s.rollback()
    segment_ids = [
        r.id for r in s.query(Segment.id).filter(Segment.name.like(f"{PREFIX} %"))
    ]
    user_ids = [
        r.id for r in s.query(User.id).filter(User.username.like(f"{PREFIX}_%"))
    ]
    s.query(AuditLog).filter(
        AuditLog.entity_id.in_(segment_ids) | AuditLog.user_id.in_(user_ids)
    ).delete(synchronize_session=False)
    s.query(Segment).filter(Segment.id.in_(segment_ids)).delete(
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


def _segment(db_session, owner: User, kind: str = "rules") -> Segment:
    segment = Segment(
        name=_name(),
        description="kept",
        kind=kind,
        rules=copy.deepcopy(RULES) if kind == "rules" else None,
        status=ModelSegmentStatus.ACTIVE,
        owner_id=owner.id,
    )
    db_session.add(segment)
    db_session.commit()
    return segment


def _stored(fresh, segment_id) -> tuple:
    """The segment row and its audit entries, as stored."""
    session = fresh()
    try:
        row = session.execute(
            text("select row_to_json(s)::text from segments s where id = :i"),
            {"i": str(segment_id)},
        ).scalar()
        assert row is not None, "the segment is stored"
        entries = (
            session.query(AuditLog).filter(AuditLog.entity_id == segment_id).count()
        )
        return row, entries
    finally:
        session.close()


def _totals(fresh) -> tuple:
    """How many segments and audit entries there are."""
    session = fresh()
    try:
        return session.query(Segment).count(), session.query(AuditLog).count()
    finally:
        session.close()


def _create(client, user, body: Dict[str, Any], escape: str, path: str = SEGMENTS):
    return client.post(path, content=_raw(body, escape), headers=_auth(user))


def _update(client, user, segment_id, body: Dict[str, Any], escape: str):
    return client.put(
        f"{SEGMENTS}/{segment_id}", content=_raw(body, escape), headers=_auth(user)
    )


def _preview(client, user, segment_id, body: Dict[str, Any], escape: str):
    return client.post(
        f"{SEGMENTS}/{segment_id}/preview",
        content=_raw(body, escape),
        headers=_auth(user),
    )


def _assert_create_refused(client, fresh, user, path, body, escape, *fields):
    before = _totals(fresh)

    _assert_refused(_create(client, user, body, escape, path), *fields)

    assert _totals(fresh) == before


def _assert_update_refused(client, fresh, user, segment, body, escape, *fields):
    before = _stored(fresh, segment.id)

    _assert_refused(_update(client, user, segment.id, body, escape), *fields)

    assert _stored(fresh, segment.id) == before


# --- each route refuses the character, and nothing is saved -------------------


@pytest.mark.parametrize("character", CHARACTERS)
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("field", TEXT_FIELDS)
@pytest.mark.parametrize("path", CREATE_PATHS)
def test_a_create_refuses_the_character_in_the_name_or_description(
    client, fresh, developer, path, field, kind, character
):
    body = _create_body(kind, **{field: _with_text(field)})

    _assert_create_refused(
        client, fresh, developer, CREATE_PATHS[path], body, CHARACTERS[character], field
    )


@pytest.mark.parametrize("character", CHARACTERS)
@pytest.mark.parametrize("place", RULE_PLACES)
@pytest.mark.parametrize("path", CREATE_PATHS)
def test_a_create_refuses_the_character_in_the_rules(
    client, fresh, developer, path, place, character
):
    body = _create_body("rules", rules=RULE_PLACES[place])

    _assert_create_refused(
        client,
        fresh,
        developer,
        CREATE_PATHS[path],
        body,
        CHARACTERS[character],
        "rules",
    )


@pytest.mark.parametrize("character", CHARACTERS)
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("field", TEXT_FIELDS)
def test_an_update_refuses_the_character_in_the_name_or_description(
    client, fresh, db_session, developer, field, kind, character
):
    segment = _segment(db_session, developer, kind)

    _assert_update_refused(
        client,
        fresh,
        developer,
        segment,
        {field: _with_text(field)},
        CHARACTERS[character],
        field,
    )


@pytest.mark.parametrize("character", CHARACTERS)
@pytest.mark.parametrize("place", RULE_PLACES)
def test_an_update_refuses_the_character_in_the_rules(
    client, fresh, db_session, developer, place, character
):
    segment = _segment(db_session, developer)

    _assert_update_refused(
        client,
        fresh,
        developer,
        segment,
        {"rules": RULE_PLACES[place]},
        CHARACTERS[character],
        "rules",
    )


@pytest.mark.parametrize("route", ("create", "update"))
def test_each_field_holding_the_character_is_refused_at_its_place(
    client, fresh, db_session, developer, route
):
    """One error per field, in the order name, description, rules."""
    fields = {
        "name": _with_text("name"),
        "description": _with_text("description"),
        "rules": RULE_PLACES["value"],
    }
    nul = CHARACTERS["nul"]
    if route == "create":
        body = _create_body("rules", **fields)
        _assert_create_refused(
            client, fresh, developer, SEGMENTS, body, nul, *TEXT_FIELDS, "rules"
        )
        return
    segment = _segment(db_session, developer)
    _assert_update_refused(
        client, fresh, developer, segment, fields, nul, *TEXT_FIELDS, "rules"
    )


# --- the rules are not read first ---------------------------------------------


@pytest.mark.parametrize("character", CHARACTERS)
@pytest.mark.parametrize("place", READ_BEFORE)
@pytest.mark.parametrize("route", ("create", "update"))
def test_rules_another_check_refused_now_get_this_answer(
    client, fresh, db_session, developer, route, place, character
):
    """The character in an attribute, an operator, a logical operator, a
    semantic version or a key answered the rules check's 422; the rules are
    now refused before they are read, with this one."""
    escape = CHARACTERS[character]
    rules = READ_BEFORE[place]
    if route == "create":
        body = _create_body("rules", rules=rules)
        _assert_create_refused(
            client, fresh, developer, SEGMENTS, body, escape, "rules"
        )
        return
    segment = _segment(db_session, developer)
    _assert_update_refused(
        client, fresh, developer, segment, {"rules": rules}, escape, "rules"
    )


# --- what is still accepted ---------------------------------------------------


def test_an_emoji_is_still_saved_as_sent(client, fresh, db_session, developer):
    """A valid surrogate pair is a character the database stores: an emoji in
    the name, the description or the rules is saved, by both routes."""
    rules = _rules(lambda r: _condition(r).update(value="US" + X))
    created = _create(
        client,
        developer,
        _create_body("rules", name=_name() + X, description="d" + X, rules=rules),
        EMOJI_ESCAPE,
    )
    assert created.status_code == 201, created.text
    assert created.json()["name"].endswith(EMOJI)
    assert created.json()["description"] == "d" + EMOJI
    assert _condition(created.json()["rules"])["value"] == "US" + EMOJI

    segment = _segment(db_session, developer)
    updated = _update(
        client,
        developer,
        segment.id,
        {"name": _name() + X, "description": "e" + X, "rules": rules},
        EMOJI_ESCAPE,
    )
    assert updated.status_code == 200, updated.text
    session = fresh()
    try:
        stored = session.get(Segment, segment.id)
        assert stored.name.endswith(EMOJI)
        assert stored.description == "e" + EMOJI
        assert _condition(stored.rules)["value"] == "US" + EMOJI
    finally:
        session.close()


def test_a_segment_as_read_still_puts_back(client, developer):
    """A segment's name, description, rules and status, as a GET answers them,
    PUT back unchanged."""
    created = client.post(
        SEGMENTS,
        json={"name": _name(), "description": "d", "rules": RULES},
        headers=_auth(developer),
    )
    assert created.status_code == 201, created.text
    segment_id = created.json()["id"]
    fields = ("name", "description", "rules", "status")

    read = client.get(f"{SEGMENTS}/{segment_id}", headers=_auth(developer)).json()
    sent = {field: read[field] for field in fields}
    back = client.put(f"{SEGMENTS}/{segment_id}", json=sent, headers=_auth(developer))
    assert back.status_code == 200, back.text
    reread = client.get(f"{SEGMENTS}/{segment_id}", headers=_auth(developer)).json()
    assert {field: reread[field] for field in fields} == sent


#: Preview bodies holding the character that answered 200 on main.
PREVIEW_ACCEPTED = {
    "name": ({"name": _with_text("name"), "rules": RULES}, ("nul",)),
    "description": (
        {"name": "ab", "description": _with_text("description"), "rules": RULES},
        ("nul",),
    ),
    **{
        f"rules-{place}": (
            {"name": "ab", "rules": RULE_PLACES[place]},
            tuple(CHARACTERS),
        )
        for place in ("value", "list-item", "condition-id", "group-id")
    },
}


@pytest.mark.parametrize("place", PREVIEW_ACCEPTED)
def test_the_preview_still_answers_such_a_body_as_before(
    client, fresh, db_session, developer, place
):
    """The preview takes the create's body but stores nothing: a body holding
    the character is estimated as before."""
    segment = _segment(db_session, developer)
    body, characters = PREVIEW_ACCEPTED[place]
    before = _totals(fresh)
    for character in characters:
        response = _preview(client, developer, segment.id, body, CHARACTERS[character])
        assert response.status_code == 200, (character, response.text)
        assert MARK not in response.text
    assert _totals(fresh) == before


# --- the order of the refusals ------------------------------------------------

NUL_NAME = {"name": _with_text("name")}
NUL_RULES = {"rules": RULE_PLACES["value"]}
NUL = CHARACTERS["nul"]


def test_authentication_still_answers_401_first(client, db_session, developer):
    segment = _segment(db_session, developer)
    for method, path, body in (
        ("POST", SEGMENTS, _create_body("rules", **NUL_NAME)),
        ("POST", SEGMENTS, _create_body("rules", **NUL_RULES)),
        ("PUT", f"{SEGMENTS}/{segment.id}", NUL_NAME),
        ("PUT", f"{SEGMENTS}/{segment.id}", NUL_RULES),
    ):
        response = client.request(method, path, content=_raw(body, NUL), headers=JSON)
        assert response.status_code == 401, (path, response.text)
        assert MARK not in response.text


def test_on_a_create_the_422_comes_before_the_403(client, fresh, viewer):
    before = _totals(fresh)

    _assert_refused(
        _create(client, viewer, _create_body("rules", **NUL_NAME), NUL), "name"
    )
    # A storable body is still the 403 it was.
    storable = _create(client, viewer, _create_body("rules", **NUL_NAME), "")
    assert storable.status_code == 403, storable.text
    assert _totals(fresh) == before


def test_on_an_update_the_422_comes_before_the_403(
    client, fresh, db_session, developer, viewer
):
    segment = _segment(db_session, developer)
    before = _stored(fresh, segment.id)

    _assert_refused(_update(client, viewer, segment.id, NUL_RULES, NUL), "rules")
    assert _update(client, viewer, segment.id, NUL_RULES, "").status_code == 403
    assert _stored(fresh, segment.id) == before


def test_on_an_update_the_422_comes_before_the_404(client, developer):
    unknown = uuid.uuid4()

    _assert_refused(_update(client, developer, unknown, NUL_NAME, NUL), "name")
    assert _update(client, developer, unknown, NUL_NAME, "").status_code == 404
