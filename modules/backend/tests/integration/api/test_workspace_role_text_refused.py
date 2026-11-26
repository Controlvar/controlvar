"""Saving a workspace or a custom role with text holding NUL or an unpaired
surrogate answers 422 (#2181).

PostgreSQL refuses U+0000 in ``text`` and ``varchar``, and a lone surrogate
cannot be encoded at all. A workspace's name or description answered 500 for
either character; a role's description answered 409 on a create and 400 on an
update for a NUL, with the database driver's sentence. Each field is now
checked where its request model declares it, with the helper the core models
use: 422 at the field, a fixed message that never repeats the value, nothing
saved and no audit entry.

* ``POST /api/v1/workspaces/`` and ``PUT /api/v1/workspaces/{id}``: ``name``,
  ``description``.
* ``POST /api/v1/rbac/roles`` and ``PUT /api/v1/rbac/roles/{role_name}``:
  ``description``.

Authentication still answers 401 first; the 403 and 404 the routes answer
themselves now come after the 422, as every other body 422 there does. A valid
surrogate pair (an emoji) is saved as before. The bodies are sent as raw JSON
text, with the escapes a client on the wire sends.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, Optional, Tuple

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.core.config import settings
from backend.app.core.security import create_local_access_token
from backend.app.main import app
from backend.app.models.audit_log import AuditLog
from backend.app.models.user import User, UserRole
from modules.backend.app.models.custom_role import CustomRole
from modules.backend.app.models.workspace import Workspace, WorkspaceMember

pytestmark = [pytest.mark.integration, pytest.mark.requires_db, pytest.mark.regression]

PREFIX = "t2181"
SCHEMA = "test_experimentation"
WORKSPACES = "/api/v1/workspaces"
ROLES = "/api/v1/rbac/roles"
JSON = {"Content-Type": "application/json"}

MARK = "zq2181mark"
X = "QQ2181QQ"
CHARACTERS = {"nul": "\\u0000", "surrogate": "\\ud800"}
EMOJI_ESCAPE = "\\ud83d\\ude00"
EMOJI = "\U0001f600"
NUL = CHARACTERS["nul"]
MARKED = MARK + X


def _refusal(field: str) -> Dict[str, Any]:
    return {
        "type": "value_error",
        "loc": ["body", field],
        "msg": f"Value error, {field} must not contain NUL or unpaired surrogate characters",
    }


def _raw(body: Dict[str, Any], escape: str) -> bytes:
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
    return _user(db_session, UserRole.ADMIN)


@pytest.fixture
def fresh(test_db):
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
    """Leave nothing behind: other suites list workspaces and roles."""
    yield
    s = db_session
    s.rollback()
    workspace_ids = [
        r.id for r in s.query(Workspace.id).filter(Workspace.slug.like(f"{PREFIX}-%"))
    ]
    role_ids = [
        r.id for r in s.query(CustomRole.id).filter(CustomRole.name.like(f"{PREFIX}%"))
    ]
    user_ids = [
        r.id for r in s.query(User.id).filter(User.username.like(f"{PREFIX}_%"))
    ]
    s.query(AuditLog).filter(
        AuditLog.entity_id.in_(workspace_ids + role_ids)
        | AuditLog.user_id.in_(user_ids)
    ).delete(synchronize_session=False)
    s.query(WorkspaceMember).filter(
        WorkspaceMember.workspace_id.in_(workspace_ids)
    ).delete(synchronize_session=False)
    s.query(Workspace).filter(Workspace.id.in_(workspace_ids)).delete(
        synchronize_session=False
    )
    s.query(CustomRole).filter(CustomRole.id.in_(role_ids)).delete(
        synchronize_session=False
    )
    s.query(User).filter(User.id.in_(user_ids)).delete(synchronize_session=False)
    s.commit()


def _auth(user: Optional[User]) -> Dict[str, str]:
    if user is None:
        return JSON
    return {**JSON, "Authorization": f"Bearer {create_local_access_token(user)}"}


def _send(client, method: str, path: str, body: Dict[str, Any], escape, user):
    return client.request(method, path, content=_raw(body, escape), headers=_auth(user))


def _totals(fresh) -> Tuple[int, ...]:
    session = fresh()
    try:
        return tuple(
            session.execute(text(f"select count(*) from {table}")).scalar()
            for table in (
                "workspaces",
                "workspace_members",
                "custom_roles",
                "audit_logs",
                "audit_events_v2",
            )
        )
    finally:
        session.close()


def _stored(fresh, table: str, value, column: str = "id") -> Tuple[str, ...]:
    session = fresh()
    try:
        return tuple(
            session.execute(
                text(f"select row_to_json(t)::text from {table} t where {column} = :v"),
                {"v": str(value)},
            )
            .scalars()
            .all()
        )
    finally:
        session.close()


def _workspace_body(**fields: Any) -> Dict[str, Any]:
    suffix = uuid.uuid4().hex[:8]
    return {
        "name": f"{PREFIX} workspace {suffix}",
        "slug": f"{PREFIX}-{suffix}",
        "description": "d",
        **fields,
    }


def _workspace(client, owner: User) -> Dict[str, Any]:
    created = client.post(
        f"{WORKSPACES}/", json=_workspace_body(), headers=_auth(owner)
    )
    assert created.status_code == 201, created.text
    return created.json()


def _role_name() -> str:
    return f"{PREFIX}role{uuid.uuid4().hex[:8]}"


def _role(client, admin: User) -> str:
    name = _role_name()
    created = client.post(
        ROLES, json={"name": name, "permissions": []}, headers=_auth(admin)
    )
    assert created.status_code == 201, created.text
    return name


# --- workspaces -----------------------------------------------------------------


@pytest.mark.parametrize("character", CHARACTERS)
@pytest.mark.parametrize("field", ("name", "description"))
def test_a_workspace_create_refuses_the_character(
    client, fresh, developer, field, character
):
    body = _workspace_body()
    body[field] += " " + MARKED
    before = _totals(fresh)

    response = _send(
        client, "POST", f"{WORKSPACES}/", body, CHARACTERS[character], developer
    )

    _assert_refused(response, field)
    assert _totals(fresh) == before


@pytest.mark.parametrize("character", CHARACTERS)
@pytest.mark.parametrize("field", ("name", "description"))
def test_a_workspace_update_refuses_the_character(
    client, fresh, developer, field, character
):
    workspace = _workspace(client, developer)
    before = (_stored(fresh, "workspaces", workspace["id"]), _totals(fresh))

    response = _send(
        client,
        "PUT",
        f"{WORKSPACES}/{workspace['id']}",
        {field: "t " + MARKED},
        CHARACTERS[character],
        developer,
    )

    _assert_refused(response, field)
    assert (_stored(fresh, "workspaces", workspace["id"]), _totals(fresh)) == before


def test_an_emoji_in_a_workspace_is_still_saved(client, fresh, developer):
    created = _send(
        client,
        "POST",
        f"{WORKSPACES}/",
        _workspace_body(name="w" + X, description="d" + X),
        EMOJI_ESCAPE,
        developer,
    )

    assert created.status_code == 201, created.text
    assert (created.json()["name"], created.json()["description"]) == (
        "w" + EMOJI,
        "d" + EMOJI,
    )
    updated = _send(
        client,
        "PUT",
        f"{WORKSPACES}/{created.json()['id']}",
        {"name": "v" + X},
        EMOJI_ESCAPE,
        developer,
    )
    assert updated.status_code == 200, updated.text
    (row,) = _stored(fresh, "workspaces", created.json()["id"])
    assert json.loads(row)["name"] == "v" + EMOJI


# --- custom roles ---------------------------------------------------------------


@pytest.mark.parametrize("character", CHARACTERS)
def test_a_role_create_refuses_the_character(client, fresh, admin, character):
    body = {"name": _role_name(), "description": "d " + MARKED, "permissions": []}
    before = _totals(fresh)

    response = _send(client, "POST", ROLES, body, CHARACTERS[character], admin)

    _assert_refused(response, "description")
    assert _totals(fresh) == before


@pytest.mark.parametrize("character", CHARACTERS)
def test_a_role_update_refuses_the_character(client, fresh, admin, character):
    name = _role(client, admin)
    before = (_stored(fresh, "custom_roles", name, "name"), _totals(fresh))

    response = _send(
        client,
        "PUT",
        f"{ROLES}/{name}",
        {"description": "d " + MARKED},
        CHARACTERS[character],
        admin,
    )

    _assert_refused(response, "description")
    assert (_stored(fresh, "custom_roles", name, "name"), _totals(fresh)) == before


def test_an_emoji_in_a_role_description_is_still_saved(client, fresh, admin):
    name = _role_name()

    created = _send(
        client,
        "POST",
        ROLES,
        {"name": name, "description": "d" + X, "permissions": []},
        EMOJI_ESCAPE,
        admin,
    )

    assert created.status_code == 201, created.text
    assert created.json()["description"] == "d" + EMOJI
    updated = _send(
        client, "PUT", f"{ROLES}/{name}", {"description": "e" + X}, EMOJI_ESCAPE, admin
    )
    assert updated.status_code == 200, updated.text
    (row,) = _stored(fresh, "custom_roles", name, "name")
    assert json.loads(row)["description"] == "e" + EMOJI


# --- the order of the refusals --------------------------------------------------


def test_authentication_still_answers_401_first(client, fresh, developer, admin):
    workspace = _workspace(client, developer)
    name = _role(client, admin)
    before = _totals(fresh)
    for method, path, body in (
        ("POST", f"{WORKSPACES}/", _workspace_body(name="w" + X)),
        ("PUT", f"{WORKSPACES}/{workspace['id']}", {"name": "w" + X}),
        ("POST", ROLES, {"name": _role_name(), "description": "d" + X}),
        ("PUT", f"{ROLES}/{name}", {"description": "d" + X}),
    ):
        response = _send(client, method, path, body, NUL, None)
        assert response.status_code == 401, (path, response.text)
    assert _totals(fresh) == before


def test_the_422_comes_before_the_403(client, fresh, developer, viewer, admin):
    """A workspace update by someone who is not an ADMIN of it, and a role
    create or update by a role that is not ADMIN."""
    workspace = _workspace(client, developer)
    name = _role(client, admin)
    before = _totals(fresh)
    for path, method, body, field, refused in (
        (f"{WORKSPACES}/{workspace['id']}", "PUT", {"name": "w" + X}, "name", viewer),
        (
            ROLES,
            "POST",
            {"name": _role_name(), "description": "d" + X, "permissions": []},
            "description",
            developer,
        ),
        (f"{ROLES}/{name}", "PUT", {"description": "d" + X}, "description", developer),
    ):
        _assert_refused(_send(client, method, path, body, NUL, refused), field)
        assert _send(client, method, path, body, "", refused).status_code == 403
    assert _totals(fresh) == before


def test_the_422_comes_before_the_404(client, fresh, developer, admin):
    before = _totals(fresh)
    for path, body, field, user in (
        (f"{WORKSPACES}/{uuid.uuid4()}", {"name": "w" + X}, "name", developer),
        (f"{ROLES}/{_role_name()}", {"description": "d" + X}, "description", admin),
    ):
        _assert_refused(_send(client, "PUT", path, body, NUL, user), field)
        assert _send(client, "PUT", path, body, "", user).status_code == 404
    assert _totals(fresh) == before
