"""Saving a holdout, a mutual exclusion group, a user, notification settings,
a flag or a rollout schedule with text holding NUL or an unpaired surrogate
answers 422 (#2181).

PostgreSQL refuses U+0000 in ``text``, ``varchar`` and ``jsonb``, and a lone
surrogate cannot be encoded at all. These saves failed at the database: a
holdout's, a group's and a user's name, and the notification settings,
answered 500; a flag's and a rollout schedule's name or description answered
400 with the database driver's sentence, and a schedule's ``config_data`` and
a stage's ``trigger_configuration`` 500. Each field is now checked where its
request model declares it, with the helper #961, #2163 and #2180 use: 422 at
the field, a fixed message that never repeats the value, nothing saved and no
audit entry.

* ``POST /api/v1/holdout``, ``PUT /api/v1/holdout/{id}``,
  ``POST /api/v1/mutual-exclusion-groups`` and
  ``PUT /api/v1/mutual-exclusion-groups/{id}``: ``name``, ``description``.
* ``POST /api/v1/users/`` and ``PUT /api/v1/users/{id}``: ``username``,
  ``full_name``.
* ``PUT /api/v1/notifications/preferences``: ``slack_channel``,
  ``email_override``.
* ``POST /api/v1/feature-flags/`` and ``PUT /api/v1/feature-flags/{id}``:
  ``name``, ``description``.
* ``POST /api/v1/rollout-schedules/`` and ``PUT .../{id}``: ``name``,
  ``description``, ``config_data``, and a stage's ``name``, ``description``
  and ``trigger_configuration``; ``POST .../{id}/stages`` and
  ``PUT .../stages/{id}``: a stage's three.

The check runs as the body is read: authentication still answers 401 first,
and a flag create by a role that may not create flags still answers 403 first
(a dependency); the 403 and 404 the routes answer themselves now come after
it, as every other body 422 there does. A valid surrogate pair (an emoji) is
saved as before.

The bodies are sent as raw JSON text: a JSON client cannot encode ``\\ud800``,
and sending the escapes is what a client on the wire does.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Callable, Dict, Optional, Tuple

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.core.config import settings
from backend.app.core.security import create_local_access_token
from backend.app.main import app
from backend.app.models.audit_log import AuditLog
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.global_holdout import GlobalHoldout
from backend.app.models.mutual_exclusion_group import MutualExclusionGroup
from backend.app.models.notification import NotificationPreference
from backend.app.models.rollout_schedule import RolloutSchedule, RolloutStage
from backend.app.models.user import User, UserRole

pytestmark = [pytest.mark.integration, pytest.mark.requires_db, pytest.mark.regression]

PREFIX = "t2181"
SCHEMA = "test_experimentation"
HOLDOUTS = "/api/v1/holdout"
GROUPS = "/api/v1/mutual-exclusion-groups"
USERS = "/api/v1/users"
PREFERENCES = "/api/v1/notifications/preferences"
FLAGS = "/api/v1/feature-flags"
SCHEDULES = "/api/v1/rollout-schedules"
JSON = {"Content-Type": "application/json"}

#: Marks the refused text, so a test can tell whether an answer repeats it.
MARK = "zq2181mark"
#: An ASCII placeholder, replaced in the raw JSON text by the escape sent.
X = "QQ2181QQ"
#: The two characters, as JSON escapes: NUL and an unpaired surrogate.
CHARACTERS = {"nul": "\\u0000", "surrogate": "\\ud800"}
#: A valid surrogate pair: U+1F600, an emoji.
EMOJI_ESCAPE = "\\ud83d\\ude00"
EMOJI = "\U0001f600"
NUL = CHARACTERS["nul"]
#: Text holding the placeholder, with the mark before it.
MARKED = MARK + X


def _name(kind: str) -> str:
    return f"{PREFIX} {kind} {uuid.uuid4().hex[:8]}"


def _stage(order: int = 0, percentage: int = 10, **fields: Any) -> Dict[str, Any]:
    return {
        "name": f"stage {order}",
        "stage_order": order,
        "target_percentage": percentage,
        "trigger_type": "manual",
        **fields,
    }


def _refusal(loc: Tuple[Any, ...]) -> Dict[str, Any]:
    field = loc[-1]
    return {
        "type": "value_error",
        "loc": ["body", *loc],
        "msg": f"Value error, {field} must not contain NUL or unpaired surrogate characters",
    }


def _raw(body: Dict[str, Any], escape: str) -> bytes:
    """*body* as JSON text, with the placeholder replaced by *escape*."""
    sent = json.dumps(body)
    assert X in sent
    return sent.replace(X, escape).encode("ascii")


def _assert_refused(response, loc: Tuple[Any, ...]) -> None:
    assert response.status_code == 422, response.text
    assert MARK not in response.text, "the answer repeats the value sent"
    errors = response.json()["detail"]
    assert [{k: e[k] for k in ("type", "loc", "msg")} for e in errors] == [
        _refusal(loc)
    ]


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
        email=f"{PREFIX}_{suffix}@savedtext.io",
        full_name="Saved Text User",
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
def viewer(db_session) -> User:
    return _user(db_session, UserRole.VIEWER)


@pytest.fixture
def admin(db_session) -> User:
    """An ADMIN who is not a superuser: a user create refuses them."""
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
    """Leave nothing behind: other suites list these and count entries."""
    yield
    s = db_session
    s.rollback()
    flag_ids = [
        r.id
        for r in s.query(FeatureFlag.id).filter(FeatureFlag.key.like(f"{PREFIX}-%"))
    ]
    schedule_ids = [
        r.id
        for r in s.query(RolloutSchedule.id).filter(
            RolloutSchedule.feature_flag_id.in_(flag_ids)
        )
    ]
    holdout_ids = [
        r.id
        for r in s.query(GlobalHoldout.id).filter(
            GlobalHoldout.name.like(f"{PREFIX} %")
        )
    ]
    group_ids = [
        r.id
        for r in s.query(MutualExclusionGroup.id).filter(
            MutualExclusionGroup.name.like(f"{PREFIX} %")
        )
    ]
    user_ids = [r.id for r in s.query(User.id).filter(User.username.like(f"{PREFIX}%"))]
    entity_ids = flag_ids + schedule_ids + holdout_ids + group_ids + user_ids
    s.query(AuditLog).filter(
        AuditLog.entity_id.in_(entity_ids) | AuditLog.user_id.in_(user_ids)
    ).delete(synchronize_session=False)
    s.query(RolloutStage).filter(
        RolloutStage.rollout_schedule_id.in_(schedule_ids)
    ).delete(synchronize_session=False)
    s.query(RolloutSchedule).filter(RolloutSchedule.id.in_(schedule_ids)).delete(
        synchronize_session=False
    )
    s.query(FeatureFlag).filter(FeatureFlag.id.in_(flag_ids)).delete(
        synchronize_session=False
    )
    s.query(GlobalHoldout).filter(GlobalHoldout.id.in_(holdout_ids)).delete(
        synchronize_session=False
    )
    s.query(MutualExclusionGroup).filter(MutualExclusionGroup.id.in_(group_ids)).delete(
        synchronize_session=False
    )
    s.query(NotificationPreference).filter(
        NotificationPreference.user_id.in_(user_ids)
    ).delete(synchronize_session=False)
    s.query(User).filter(User.id.in_(user_ids)).delete(synchronize_session=False)
    s.commit()


# --- helpers ------------------------------------------------------------------


def _auth(user: Optional[User]) -> Dict[str, str]:
    if user is None:
        return JSON
    return {**JSON, "Authorization": f"Bearer {create_local_access_token(user)}"}


def _send(client, method: str, path: str, body: Dict[str, Any], escape, user):
    return client.request(method, path, content=_raw(body, escape), headers=_auth(user))


def _totals(fresh) -> Tuple[int, ...]:
    """How many rows each table these routes write holds, audit entries of
    either kind included."""
    session = fresh()
    try:
        return tuple(
            session.execute(text(f"select count(*) from {table}")).scalar()
            for table in (
                "global_holdouts",
                "mutual_exclusion_groups",
                "users",
                "notification_preferences",
                "feature_flags",
                "rollout_schedules",
                "rollout_stages",
                "audit_logs",
                "audit_events_v2",
            )
        )
    finally:
        session.close()


def _stored(fresh, table: str, row_id, column: str = "id") -> Tuple[str, ...]:
    """The rows of *table* whose *column* is *row_id*, as stored."""
    session = fresh()
    try:
        return tuple(
            session.execute(
                text(
                    f"select row_to_json(t)::text from {table} t "
                    f"where {column} = :i order by 1"
                ),
                {"i": str(row_id)},
            )
            .scalars()
            .all()
        )
    finally:
        session.close()


def _created(client, user: User, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
    created = client.post(path, json=body, headers=_auth(user))
    assert created.status_code == 201, created.text
    return created.json()


def _flag(db_session, owner: User) -> FeatureFlag:
    flag = FeatureFlag(
        key=f"{PREFIX}-{uuid.uuid4().hex[:10]}",
        name=f"{PREFIX} flag",
        status=FeatureFlagStatus.INACTIVE,
        owner_id=owner.id,
    )
    db_session.add(flag)
    db_session.commit()
    return flag


def _schedule_body(flag_id, **fields: Any) -> Dict[str, Any]:
    return {
        "name": _name("schedule"),
        "feature_flag_id": str(flag_id),
        "stages": [_stage()],
        **fields,
    }


def _schedule(client, db_session, developer) -> Dict[str, Any]:
    flag = _flag(db_session, developer)
    return _created(client, developer, f"{SCHEDULES}/", _schedule_body(flag.id))


def _stored_schedule(fresh, schedule: Dict[str, Any]) -> Tuple[Tuple[str, ...], ...]:
    return (
        _stored(fresh, "rollout_schedules", schedule["id"]),
        _stored(fresh, "rollout_stages", schedule["id"], "rollout_schedule_id"),
    )


def _new_user_body(**fields: Any) -> Dict[str, Any]:
    suffix = uuid.uuid4().hex[:8]
    return {
        "username": f"{PREFIX}u{suffix}",
        "email": f"{PREFIX}u{suffix}@savedtext.io",
        "full_name": "New Person",
        "password": "Passw0rdOk",
        "role": "VIEWER",
        **fields,
    }


def _identity(user: Dict[str, Any]) -> Dict[str, Any]:
    """What a user update resends: the stored username and email."""
    return {"username": user["username"], "email": user["email"]}


def _check_create(client, fresh, user, path, body, escape, loc) -> None:
    before = _totals(fresh)
    _assert_refused(_send(client, "POST", path, body, escape, user), loc)
    assert _totals(fresh) == before


def _check_update(client, fresh, user, method, path, body, escape, loc, stored):
    before = (stored(), _totals(fresh))
    _assert_refused(_send(client, method, path, body, escape, user), loc)
    assert (stored(), _totals(fresh)) == before


# --- holdouts and mutual exclusion groups ---------------------------------------

#: (path, the table, a fixture name for the user allowed to write).
NAMED = {
    "holdout": (HOLDOUTS, "global_holdouts", "superuser"),
    "group": (GROUPS, "mutual_exclusion_groups", "developer"),
}


@pytest.mark.parametrize("character", CHARACTERS)
@pytest.mark.parametrize("field", ("name", "description"))
@pytest.mark.parametrize("kind", NAMED)
def test_a_holdout_or_group_create_refuses_the_character(
    client, fresh, request, kind, field, character
):
    path, _, writer = NAMED[kind]
    body = {"name": _name(kind)}
    body[field] = body.get(field, "d") + " " + MARKED

    _check_create(
        client,
        fresh,
        request.getfixturevalue(writer),
        path,
        body,
        CHARACTERS[character],
        (field,),
    )


@pytest.mark.parametrize("character", CHARACTERS)
@pytest.mark.parametrize("field", ("name", "description"))
@pytest.mark.parametrize("kind", NAMED)
def test_a_holdout_or_group_update_refuses_the_character(
    client, fresh, request, kind, field, character
):
    path, table, writer = NAMED[kind]
    user = request.getfixturevalue(writer)
    row = _created(client, user, path, {"name": _name(kind), "description": "d"})

    _check_update(
        client,
        fresh,
        user,
        "PUT",
        f"{path}/{row['id']}",
        {field: _name(kind) + " " + MARKED},
        CHARACTERS[character],
        (field,),
        lambda: _stored(fresh, table, row["id"]),
    )


@pytest.mark.parametrize("kind", NAMED)
def test_an_emoji_in_a_holdout_or_group_is_still_saved(client, fresh, request, kind):
    path, table, writer = NAMED[kind]
    user = request.getfixturevalue(writer)
    name = _name(kind)

    created = _send(
        client,
        "POST",
        path,
        {"name": name + X, "description": "d" + X},
        EMOJI_ESCAPE,
        user,
    )
    assert created.status_code == 201, created.text
    assert (created.json()["name"], created.json()["description"]) == (
        name + EMOJI,
        "d" + EMOJI,
    )
    updated = _send(
        client,
        "PUT",
        f"{path}/{created.json()['id']}",
        {"description": "e" + X},
        EMOJI_ESCAPE,
        user,
    )
    assert updated.status_code == 200, updated.text
    (row,) = _stored(fresh, table, created.json()["id"])
    assert json.loads(row)["description"] == "e" + EMOJI


# --- users ----------------------------------------------------------------------


@pytest.mark.parametrize("character", CHARACTERS)
@pytest.mark.parametrize("field", ("username", "full_name"))
def test_a_user_create_refuses_the_character(
    client, fresh, superuser, field, character
):
    body = _new_user_body()
    body[field] += MARKED

    _check_create(
        client, fresh, superuser, f"{USERS}/", body, CHARACTERS[character], (field,)
    )


@pytest.mark.parametrize("character", CHARACTERS)
@pytest.mark.parametrize("field", ("username", "full_name"))
def test_a_user_update_refuses_the_character(
    client, fresh, superuser, field, character
):
    user = _created(client, superuser, f"{USERS}/", _new_user_body())
    body = _identity(user)
    body[field] = (body.get(field) or "A Name") + MARKED

    _check_update(
        client,
        fresh,
        superuser,
        "PUT",
        f"{USERS}/{user['id']}",
        body,
        CHARACTERS[character],
        (field,),
        lambda: _stored(fresh, "users", user["id"]),
    )


@pytest.mark.parametrize("character", CHARACTERS)
def test_a_user_changing_their_own_name_is_refused_the_character(
    client, fresh, developer, character
):
    """Any signed-in user may change their own ``full_name``."""
    body = {
        "username": developer.username,
        "email": developer.email,
        "full_name": "Own Name " + MARKED,
    }

    _check_update(
        client,
        fresh,
        developer,
        "PUT",
        f"{USERS}/{developer.id}",
        body,
        CHARACTERS[character],
        ("full_name",),
        lambda: _stored(fresh, "users", developer.id),
    )


def test_an_emoji_in_a_user_is_still_saved(client, fresh, superuser):
    body = _new_user_body()
    body["full_name"] = "New Person" + X
    body["username"] += X

    created = _send(client, "POST", f"{USERS}/", body, EMOJI_ESCAPE, superuser)

    assert created.status_code == 201, created.text
    user = created.json()
    assert user["username"] == body["username"].replace(X, EMOJI)
    assert user["full_name"] == "New Person" + EMOJI
    updated = _send(
        client,
        "PUT",
        f"{USERS}/{user['id']}",
        {**_identity(user), "full_name": "Other Person" + X},
        EMOJI_ESCAPE,
        superuser,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["full_name"] == "Other Person" + EMOJI


# --- notification settings ------------------------------------------------------


@pytest.mark.parametrize("character", CHARACTERS)
@pytest.mark.parametrize("field", ("slack_channel", "email_override"))
@pytest.mark.parametrize("stored", ("none-stored", "one-stored"))
def test_notification_settings_refuse_the_character(
    client, fresh, developer, stored, field, character
):
    if stored == "one-stored":
        kept = client.put(
            PREFERENCES,
            json={"slack_channel": "#kept", "email_override": "kept@savedtext.io"},
            headers=_auth(developer),
        )
        assert kept.status_code == 200, kept.text

    _check_update(
        client,
        fresh,
        developer,
        "PUT",
        PREFERENCES,
        {field: "#c" + MARKED},
        CHARACTERS[character],
        (field,),
        lambda: _stored(fresh, "notification_preferences", developer.id, "user_id"),
    )


def test_an_emoji_in_notification_settings_is_still_saved(client, fresh, developer):
    saved = _send(
        client,
        "PUT",
        PREFERENCES,
        {"slack_channel": "#c" + X, "email_override": "e" + X},
        EMOJI_ESCAPE,
        developer,
    )

    assert saved.status_code == 200, saved.text
    (row,) = _stored(fresh, "notification_preferences", developer.id, "user_id")
    stored = json.loads(row)
    assert (stored["slack_channel"], stored["email_override"]) == (
        "#c" + EMOJI,
        "e" + EMOJI,
    )


# --- flags ----------------------------------------------------------------------


@pytest.mark.parametrize("character", CHARACTERS)
@pytest.mark.parametrize("field", ("name", "description"))
def test_a_flag_create_refuses_the_character(
    client, fresh, developer, field, character
):
    body = {"key": f"{PREFIX}-{uuid.uuid4().hex[:10]}", "name": "n"}
    body[field] = "t " + MARKED

    _check_create(
        client, fresh, developer, f"{FLAGS}/", body, CHARACTERS[character], (field,)
    )


@pytest.mark.parametrize("character", CHARACTERS)
@pytest.mark.parametrize("field", ("name", "description"))
def test_a_flag_update_refuses_the_character(
    client, fresh, db_session, developer, field, character
):
    flag = _flag(db_session, developer)

    _check_update(
        client,
        fresh,
        developer,
        "PUT",
        f"{FLAGS}/{flag.id}",
        {field: "t " + MARKED},
        CHARACTERS[character],
        (field,),
        lambda: _stored(fresh, "feature_flags", flag.id),
    )


def test_an_emoji_in_a_flag_is_still_saved(client, fresh, developer):
    created = _send(
        client,
        "POST",
        f"{FLAGS}/",
        {
            "key": f"{PREFIX}-{uuid.uuid4().hex[:10]}",
            "name": "n" + X,
            "description": "d" + X,
        },
        EMOJI_ESCAPE,
        developer,
    )

    assert created.status_code == 201, created.text
    assert (created.json()["name"], created.json()["description"]) == (
        "n" + EMOJI,
        "d" + EMOJI,
    )
    updated = _send(
        client,
        "PUT",
        f"{FLAGS}/{created.json()['id']}",
        {"description": "e" + X},
        EMOJI_ESCAPE,
        developer,
    )
    assert updated.status_code == 200, updated.text
    (row,) = _stored(fresh, "feature_flags", created.json()["id"])
    assert json.loads(row)["description"] == "e" + EMOJI


# --- rollout schedules ----------------------------------------------------------

#: Where in a schedule create the character is, and the 422's loc.
SCHEDULE_CREATE_PLACES: Dict[str, Tuple[Dict[str, Any], Tuple[Any, ...]]] = {
    "name": ({"name": "s " + MARKED}, ("name",)),
    "description": ({"description": "d " + MARKED}, ("description",)),
    "a-config-data-value": ({"config_data": {"owner": MARKED}}, ("config_data",)),
    "a-config-data-key": ({"config_data": {MARKED: "o"}}, ("config_data",)),
    "a-stage-name": (
        {"stages": [_stage(name="s " + MARKED)]},
        ("stages", 0, "name"),
    ),
    "a-stage-description": (
        {"stages": [_stage(description="d " + MARKED)]},
        ("stages", 0, "description"),
    ),
    "a-stage-trigger-configuration-value": (
        {"stages": [_stage(trigger_configuration={"note": MARKED})]},
        ("stages", 0, "trigger_configuration"),
    ),
    "a-stage-trigger-configuration-key": (
        {"stages": [_stage(trigger_configuration={MARKED: 1})]},
        ("stages", 0, "trigger_configuration"),
    ),
}

SCHEDULE_UPDATE_PLACES = {
    "name": {"name": "s " + MARKED},
    "description": {"description": "d " + MARKED},
    "config-data": {"config_data": {"owner": ["a", MARKED]}},
}

STAGE_PLACES = {
    "name": {"name": "s " + MARKED},
    "description": {"description": "d " + MARKED},
    "trigger-configuration": {"trigger_configuration": {"note": MARKED}},
}


@pytest.mark.parametrize("character", CHARACTERS)
@pytest.mark.parametrize("place", SCHEDULE_CREATE_PLACES)
def test_a_rollout_schedule_create_refuses_the_character(
    client, fresh, db_session, developer, place, character
):
    flag = _flag(db_session, developer)
    fields, loc = SCHEDULE_CREATE_PLACES[place]

    _check_create(
        client,
        fresh,
        developer,
        f"{SCHEDULES}/",
        _schedule_body(flag.id, **fields),
        CHARACTERS[character],
        loc,
    )


@pytest.mark.parametrize("character", CHARACTERS)
@pytest.mark.parametrize("place", SCHEDULE_UPDATE_PLACES)
def test_a_rollout_schedule_update_refuses_the_character(
    client, fresh, db_session, developer, place, character
):
    schedule = _schedule(client, db_session, developer)
    body = SCHEDULE_UPDATE_PLACES[place]

    _check_update(
        client,
        fresh,
        developer,
        "PUT",
        f"{SCHEDULES}/{schedule['id']}",
        body,
        CHARACTERS[character],
        (next(iter(body)),),
        lambda: _stored_schedule(fresh, schedule),
    )


@pytest.mark.parametrize("character", CHARACTERS)
@pytest.mark.parametrize("place", STAGE_PLACES)
@pytest.mark.parametrize("route", ("add", "update"))
def test_a_rollout_stage_refuses_the_character(
    client, fresh, db_session, developer, route, place, character
):
    schedule = _schedule(client, db_session, developer)
    body = STAGE_PLACES[place]
    if route == "add":
        method, path = "POST", f"{SCHEDULES}/{schedule['id']}/stages"
        body = _stage(order=1, percentage=20, **body)
    else:
        method, path = "PUT", f"{SCHEDULES}/stages/{schedule['stages'][0]['id']}"

    _check_update(
        client,
        fresh,
        developer,
        method,
        path,
        body,
        CHARACTERS[character],
        (next(iter(STAGE_PLACES[place])),),
        lambda: _stored_schedule(fresh, schedule),
    )


def test_an_emoji_in_a_rollout_schedule_is_still_saved(
    client, fresh, db_session, developer
):
    flag = _flag(db_session, developer)
    body = _schedule_body(
        flag.id,
        description="d" + X,
        config_data={"owner" + X: "o" + X},
        stages=[_stage(name="s" + X, trigger_configuration={"note": "n" + X})],
    )

    created = _send(client, "POST", f"{SCHEDULES}/", body, EMOJI_ESCAPE, developer)

    assert created.status_code == 201, created.text
    schedule = created.json()
    assert schedule["description"] == "d" + EMOJI
    assert schedule["config_data"] == {"owner" + EMOJI: "o" + EMOJI}
    assert schedule["stages"][0]["name"] == "s" + EMOJI
    assert schedule["stages"][0]["trigger_configuration"] == {"note": "n" + EMOJI}
    added = _send(
        client,
        "POST",
        f"{SCHEDULES}/{schedule['id']}/stages",
        _stage(order=1, percentage=20, description="e" + X),
        EMOJI_ESCAPE,
        developer,
    )
    assert added.status_code == 201, added.text
    updated = _send(
        client,
        "PUT",
        f"{SCHEDULES}/{schedule['id']}",
        {"name": "t" + X},
        EMOJI_ESCAPE,
        developer,
    )
    assert updated.status_code == 200, updated.text
    rows, stages = _stored_schedule(fresh, schedule)
    assert json.loads(rows[0])["name"] == "t" + EMOJI
    assert sorted(json.loads(s)["description"] or "" for s in stages) == [
        "",
        "e" + EMOJI,
    ]


# --- the order of the refusals --------------------------------------------------


#: Where each operation's body below holds the placeholder.
ROUTE_LOC: Dict[str, Tuple[Any, ...]] = {
    "holdout-create": ("name",),
    "holdout-update": ("name",),
    "group-create": ("name",),
    "group-update": ("name",),
    "user-create": ("full_name",),
    "user-update": ("full_name",),
    "preferences": ("slack_channel",),
    "flag-create": ("name",),
    "flag-update": ("name",),
    "schedule-create": ("name",),
    "schedule-update": ("name",),
    "stage-add": ("name",),
    "stage-update": ("name",),
}


def _routes(
    client, db_session, superuser, developer
) -> Dict[str, Tuple[str, str, Dict]]:
    """Every operation, with a body holding the placeholder at its
    ``ROUTE_LOC``."""
    holdout = _created(client, superuser, HOLDOUTS, {"name": _name("holdout")})
    group = _created(client, developer, GROUPS, {"name": _name("group")})
    user = _created(client, superuser, f"{USERS}/", _new_user_body())
    flag = _flag(db_session, developer)
    schedule = _schedule(client, db_session, developer)
    return {
        "holdout-create": ("POST", HOLDOUTS, {"name": _name("holdout") + X}),
        "holdout-update": ("PUT", f"{HOLDOUTS}/{holdout['id']}", {"name": "n" + X}),
        "group-create": ("POST", GROUPS, {"name": _name("group") + X}),
        "group-update": ("PUT", f"{GROUPS}/{group['id']}", {"name": "n" + X}),
        "user-create": ("POST", f"{USERS}/", _new_user_body(full_name="N" + X)),
        "user-update": (
            "PUT",
            f"{USERS}/{user['id']}",
            {**_identity(user), "full_name": "N" + X},
        ),
        "preferences": ("PUT", PREFERENCES, {"slack_channel": "#c" + X}),
        "flag-create": (
            "POST",
            f"{FLAGS}/",
            {"key": f"{PREFIX}-{uuid.uuid4().hex[:10]}", "name": "n" + X},
        ),
        "flag-update": ("PUT", f"{FLAGS}/{flag.id}", {"name": "n" + X}),
        "schedule-create": (
            "POST",
            f"{SCHEDULES}/",
            _schedule_body(flag.id, name="s" + X),
        ),
        "schedule-update": ("PUT", f"{SCHEDULES}/{schedule['id']}", {"name": "s" + X}),
        "stage-add": (
            "POST",
            f"{SCHEDULES}/{schedule['id']}/stages",
            _stage(order=1, percentage=20, name="s" + X),
        ),
        "stage-update": (
            "PUT",
            f"{SCHEDULES}/stages/{schedule['stages'][0]['id']}",
            {"name": "s" + X},
        ),
    }


def test_authentication_still_answers_401_first(
    client, fresh, db_session, superuser, developer
):
    routes = _routes(client, db_session, superuser, developer)
    before = _totals(fresh)
    for method, path, body in routes.values():
        response = _send(client, method, path, body, NUL, None)
        assert response.status_code == 401, (path, response.text)
    assert _totals(fresh) == before


def test_on_a_flag_create_the_403_still_comes_first(client, fresh, viewer):
    """The create's role check is a dependency, which runs before the body is
    read."""
    body = {"key": f"{PREFIX}-{uuid.uuid4().hex[:10]}", "name": "n " + MARKED}
    before = _totals(fresh)

    refused = _send(client, "POST", f"{FLAGS}/", body, NUL, viewer)

    assert refused.status_code == 403, refused.text
    assert MARK not in refused.text
    assert _totals(fresh) == before


#: The operations whose route refuses the role itself, and which role.
REFUSED_ROLE = {
    "holdout-create": "developer",
    "holdout-update": "developer",
    "group-create": "viewer",
    "group-update": "viewer",
    "user-create": "admin",
    "user-update": "developer",
    "flag-update": "viewer",
    "schedule-create": "viewer",
    "schedule-update": "viewer",
    "stage-add": "viewer",
    "stage-update": "viewer",
}


@pytest.mark.parametrize("route", REFUSED_ROLE)
def test_the_422_comes_before_the_403(
    client, fresh, db_session, request, superuser, developer, route
):
    method, path, body = _routes(client, db_session, superuser, developer)[route]
    refused = request.getfixturevalue(REFUSED_ROLE[route])
    before = _totals(fresh)

    response = _send(client, method, path, body, NUL, refused)

    _assert_refused(response, ROUTE_LOC[route])
    # A storable body is still the 403 it was.
    assert _send(client, method, path, body, "", refused).status_code == 403
    assert _totals(fresh) == before


#: The operations that answer 404 for an unknown id, and the path with one.
UNKNOWN = {
    "holdout-update": f"{HOLDOUTS}/{{}}",
    "group-update": f"{GROUPS}/{{}}",
    "user-update": f"{USERS}/{{}}",
    "flag-update": f"{FLAGS}/{{}}",
    "schedule-create": None,
    "schedule-update": f"{SCHEDULES}/{{}}",
    "stage-add": f"{SCHEDULES}/{{}}/stages",
    "stage-update": f"{SCHEDULES}/stages/{{}}",
}


@pytest.mark.parametrize("route", UNKNOWN)
def test_the_422_comes_before_the_404(
    client, fresh, db_session, superuser, developer, route
):
    method, path, body = _routes(client, db_session, superuser, developer)[route]
    unknown = uuid.uuid4()
    if UNKNOWN[route] is None:
        body = {**body, "feature_flag_id": str(unknown)}
    else:
        path = UNKNOWN[route].format(unknown)
    user = superuser if route.startswith(("holdout", "user")) else developer
    before = _totals(fresh)

    response = _send(client, method, path, body, NUL, user)

    _assert_refused(response, ROUTE_LOC[route])
    assert _send(client, method, path, body, "", user).status_code == 404
    assert _totals(fresh) == before
