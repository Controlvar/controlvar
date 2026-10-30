"""A duplicate exclusion group or holdout name answers 409, not 500 (#1019).

``mutual_exclusion_groups.name`` and ``global_holdouts.name`` are unique. Before
the fix nothing caught the database's refusal, so four operations answered 500
to a name that was already taken:

* ``POST /api/v1/mutual-exclusion-groups`` and ``PUT .../{group_id}`` (rename);
* ``POST /api/v1/holdout`` and ``PUT /api/v1/holdout/{holdout_id}`` (rename).

Now each answers 409 with ``{"detail": "A ... with the name '<name>' already
exists."}``, the shape of the experiment-key and flag-key 409s. The guarantee is
the unique constraint itself: its ``IntegrityError`` is caught after the session
is rolled back and recognised by the constraint's name, so two requests that
race also get 409 (the "race" cases bypass the pre-check to force that path).
Any other ``IntegrityError`` keeps today's answer, a 500, and is never a 409.

The comparison is the constraint's: exact and case-sensitive, so "Checkout" and
"checkout" are two names.

Every request is a real one as a non-superuser ADMIN (the holdout routes need
ADMIN; the group routes DEVELOPER). The database is shared, so every name
carries a random suffix and no holdout is activated.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import PendingRollbackError
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.api.v1.endpoints.auth import create_local_access_token
from backend.app.core.config import settings
from backend.app.main import app
from backend.app.models.global_holdout import GlobalHoldout
from backend.app.models.mutual_exclusion_group import MutualExclusionGroup
from backend.app.models.user import User, UserRole
from backend.app.services import global_holdout_service, mutual_exclusion_service

pytestmark = [pytest.mark.integration, pytest.mark.regression]

GROUPS = "/api/v1/mutual-exclusion-groups"
HOLDOUT = "/api/v1/holdout"
ROUTES = ("group-create", "group-rename", "holdout-create", "holdout-rename")


# --- fixtures -----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _local_auth(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "CACHE_ENABLED", False)


def _factory(db_session):
    engine = db_session.get_bind()
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    def session():
        s = factory()
        s.execute(text("SET search_path TO test_experimentation"))
        return s

    return session


@pytest.fixture
def admin(db_session):
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"dup1019_{suffix}",
        email=f"dup1019_{suffix}@dup.test",
        full_name="Duplicate Name User",
        hashed_password="unused: this user signs in by token only",
        is_active=True,
        is_superuser=False,
        role=UserRole.ADMIN,
    )
    db_session.add(user)
    db_session.commit()
    return user


@pytest.fixture
def client(db_session):
    session = _factory(db_session)

    def override_get_db():
        s = session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[deps.get_db] = override_get_db
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
    finally:
        app.dependency_overrides.pop(deps.get_db, None)


# --- helpers ------------------------------------------------------------------


def _auth(user):
    return {"Authorization": f"Bearer {create_local_access_token(user)}"}


def _name(label="dup1019"):
    return f"{label} {uuid.uuid4().hex[:10]}"


def _create(client, admin, which, name):
    if which.startswith("group"):
        return client.post(GROUPS, json={"name": name}, headers=_auth(admin))
    return client.post(
        HOLDOUT, json={"name": name, "holdout_percentage": 5}, headers=_auth(admin)
    )


def _created(client, admin, which, name):
    response = _create(client, admin, which, name)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _rename(client, admin, which, row_id, name):
    path = f"{GROUPS}/{row_id}" if which.startswith("group") else f"{HOLDOUT}/{row_id}"
    return client.put(path, json={"name": name}, headers=_auth(admin))


def _taken_detail(which, name):
    if which.startswith("group"):
        return f"A mutual exclusion group with the name '{name}' already exists."
    return f"A global holdout with the name '{name}' already exists."


def _names_stored(db_session, which, name):
    model = MutualExclusionGroup if which.startswith("group") else GlobalHoldout
    session = _factory(db_session)()
    try:
        return session.query(model).filter(model.name == name).count()
    finally:
        session.close()


def _stored_name(db_session, which, row_id):
    model = MutualExclusionGroup if which.startswith("group") else GlobalHoldout
    session = _factory(db_session)()
    try:
        return session.get(model, uuid.UUID(row_id)).name
    finally:
        session.close()


def _send_duplicate(client, admin, which):
    """The second write of a name already stored; returns (response, name, other id)."""
    name = _name()
    _created(client, admin, which, name)
    if which.endswith("create"):
        return _create(client, admin, which, name), name, None
    other_name = _name("other")
    other = _created(client, admin, which, other_name)
    return _rename(client, admin, which, other, name), name, (other, other_name)


def _bypass_the_pre_check(monkeypatch):
    """Make the pre-check say every name is free, so the write reaches the
    database and only the unique constraint can refuse it. ``raising=False``
    so the same test runs (and fails) on code that has no pre-check."""
    for service in (
        mutual_exclusion_service.MutualExclusionService,
        global_holdout_service.GlobalHoldoutService,
    ):
        monkeypatch.setattr(
            service, "_name_taken", lambda *args, **kwargs: False, raising=False
        )


# --- a duplicate answers 409 ----------------------------------------------------


@pytest.mark.parametrize("which", ROUTES)
def test_a_duplicate_name_answers_409_and_changes_nothing(
    client, db_session, admin, which
):
    response, name, other = _send_duplicate(client, admin, which)

    assert response.status_code == 409, response.text
    assert response.json() == {"detail": _taken_detail(which, name)}
    assert _names_stored(db_session, which, name) == 1
    if other is not None:
        other_id, other_name = other
        assert _stored_name(db_session, which, other_id) == other_name


@pytest.mark.parametrize("which", ROUTES)
def test_two_requests_that_race_both_get_an_answer_the_second_409(
    client, db_session, admin, which, monkeypatch
):
    """With the pre-check bypassed the unique constraint refuses the write,
    as it does for a request that lost a race the pre-check could not see."""
    _bypass_the_pre_check(monkeypatch)

    response, name, other = _send_duplicate(client, admin, which)

    assert response.status_code == 409, response.text
    assert response.json() == {"detail": _taken_detail(which, name)}
    assert _names_stored(db_session, which, name) == 1
    if other is not None:
        other_id, other_name = other
        assert _stored_name(db_session, which, other_id) == other_name


def test_renaming_to_its_own_name_is_not_a_duplicate(client, db_session, admin):
    for which in ("group-rename", "holdout-rename"):
        name = _name()
        row_id = _created(client, admin, which, name)
        response = _rename(client, admin, which, row_id, name)
        assert response.status_code == 200, response.text
        assert response.json()["name"] == name


@pytest.mark.parametrize("which", ("group-create", "holdout-create"))
def test_names_that_differ_only_in_case_are_two_names(client, admin, which):
    """The unique constraint compares exactly, so the pre-check does too."""
    name = _name("Checkout")
    _created(client, admin, which, name)
    assert _create(client, admin, which, name.lower()).status_code == 201


# --- any other IntegrityError is not a 409 ---------------------------------------


def _bad_owner_on_create(monkeypatch, which):
    """The create writes an owner_id no user has: a foreign-key violation."""
    if which == "group-create":
        cls, method = mutual_exclusion_service.MutualExclusionService, "create_group"
    else:
        cls, method = global_holdout_service.GlobalHoldoutService, "create_holdout"
    original = getattr(cls, method)

    def with_bad_owner(self, *args, **kwargs):
        kwargs["owner_id"] = uuid.uuid4()
        return original(self, *args, **kwargs)

    monkeypatch.setattr(cls, method, with_bad_owner)


def _bad_owner_on_rename(monkeypatch, which):
    """The row the rename loads is given an owner_id no user has."""
    if which == "group-rename":
        cls, method = mutual_exclusion_service.MutualExclusionService, "get_group"
    else:
        cls, method = global_holdout_service.GlobalHoldoutService, "get_holdout"
    original = getattr(cls, method)

    def with_bad_owner(self, row_id):
        row = original(self, row_id)
        if row is not None:
            row.owner_id = uuid.uuid4()
        return row

    monkeypatch.setattr(cls, method, with_bad_owner)


@pytest.mark.parametrize("which", ROUTES)
def test_another_integrity_error_is_not_a_409(client, admin, which, monkeypatch):
    row_id = None
    if which.endswith("rename"):
        row_id = _created(client, admin, which, _name())
        _bad_owner_on_rename(monkeypatch, which)
        response = _rename(client, admin, which, row_id, _name("renamed"))
    else:
        _bad_owner_on_create(monkeypatch, which)
        response = _create(client, admin, which, _name())

    assert response.status_code == 500, response.text
    assert "already exists" not in response.text


# --- the session is usable after the refusal -----------------------------------


@pytest.mark.parametrize("which", ROUTES)
def test_the_refused_write_is_rolled_back_before_the_409(
    client, db_session, admin, which, monkeypatch
):
    """Called on one session, as a route does: after the refusal the same
    session answers a query, which it cannot while a failed transaction is
    pending."""
    _bypass_the_pre_check(monkeypatch)
    name = _name()
    _created(client, admin, which, name)
    session = _factory(db_session)()
    try:
        if which.startswith("group"):
            groups = mutual_exclusion_service.MutualExclusionService(session)
            model = MutualExclusionGroup
            other = None
            if which == "group-rename":
                other = groups.create_group(name=_name("other"))
            with pytest.raises(mutual_exclusion_service.GroupNameTaken):
                if other is None:
                    groups.create_group(name=name)
                else:
                    groups.update_group(other.id, name=name)
        else:
            holdouts = global_holdout_service.GlobalHoldoutService(session)
            model = GlobalHoldout
            other = None
            if which == "holdout-rename":
                other = holdouts.create_holdout(
                    name=_name("other"), holdout_percentage=5
                )
            with pytest.raises(global_holdout_service.HoldoutNameTaken):
                if other is None:
                    holdouts.create_holdout(name=name, holdout_percentage=5)
                else:
                    holdouts.update_holdout(other.id, name=name)
        try:
            assert session.query(model).filter(model.name == name).count() == 1
        except PendingRollbackError as error:  # pragma: no cover - the defect
            pytest.fail(f"the session was left in a failed transaction: {error}")
    finally:
        session.close()
