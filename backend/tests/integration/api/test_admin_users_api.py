"""
`GET /api/v1/admin/users` — the list the dashboard's user administration reads.

Regression: `UserListResponse.items` was annotated `List[Dict[str, Any]]` while
the endpoint handed it ORM objects, so every call was a 500, and no user
response schema carried `role`, which the dashboard needs to render the role
chip and decide what to offer.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.core.config import settings
from backend.app.core.security import create_local_access_token, get_password_hash
from backend.app.main import app
from backend.app.models.api_key import APIKey
from backend.app.models.audit_log import AuditLog
from backend.app.models.user import User, UserRole
from backend.app.services.local_auth_service import login_attempt_tracker
from backend.tests.integration.conftest import make_client_for_user

PASSWORD = "Str0ng-Passw0rd"


def _make_user(db_session, role: UserRole) -> User:
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"admin_list_{suffix}",
        email=f"admin_list_{suffix}@example.com",
        full_name=f"Admin List {suffix}",
        hashed_password=get_password_hash("Str0ng-Passw0rd"),
        is_active=True,
        is_superuser=role is UserRole.ADMIN,
        role=role,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.mark.regression
class TestAdminUserList:
    def test_returns_200_with_the_users_and_their_roles(self, admin_client, db_session):
        analyst = _make_user(db_session, UserRole.ANALYST)

        response = admin_client.get("/api/v1/admin/users", params={"limit": 100})

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["total"] >= 1
        # Newest first, so the account just created is on the first page.
        listed = {item["email"]: item for item in body["items"]}
        assert analyst.email in listed
        assert listed[analyst.email]["role"] == "ANALYST"
        assert listed[analyst.email]["username"] == analyst.username
        # The hash never leaves the server.
        assert "hashed_password" not in listed[analyst.email]

    def test_newest_first_so_pagination_is_deterministic(
        self, admin_client, db_session
    ):
        """Without an ORDER BY, pages can repeat or drop rows."""
        newest = _make_user(db_session, UserRole.VIEWER)

        first_page = admin_client.get(
            "/api/v1/admin/users", params={"skip": 0, "limit": 5}
        ).json()

        assert first_page["items"][0]["email"] == newest.email
        # Two identical requests agree, and the second page does not repeat the first.
        again = admin_client.get(
            "/api/v1/admin/users", params={"skip": 0, "limit": 5}
        ).json()
        assert [i["id"] for i in again["items"]] == [
            i["id"] for i in first_page["items"]
        ]
        second_page = admin_client.get(
            "/api/v1/admin/users", params={"skip": 5, "limit": 5}
        ).json()
        assert not {i["id"] for i in first_page["items"]} & {
            i["id"] for i in second_page["items"]
        }

    def test_pagination_fields_are_echoed(self, admin_client):
        response = admin_client.get(
            "/api/v1/admin/users", params={"skip": 0, "limit": 1}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["skip"] == 0 and body["limit"] == 1
        assert len(body["items"]) <= 1

    def test_a_non_superuser_is_refused(self, developer_client):
        assert developer_client.get("/api/v1/admin/users").status_code == 403


# ---------------------------------------------------------------------------
# PATCH /api/v1/admin/users/{user_id} -- change a role and/or active status
# (#607). The dashboard's Edit user modal sends ``{role, is_active}``; the PUT
# it used to call requires ``username`` and ``email`` (422), and silently
# dropped ``role`` when they were added.
#
# Order inside each test: the target is created with ``db_session`` first, and
# exactly one actor client is made afterwards (make_client_for_user writes the
# app-global overrides; test_role_client_fixtures.py refuses two client
# fixtures in one test). Every save and refusal re-reads the row from the
# database (``expire_all`` + ``get``) and compares every column.
# ---------------------------------------------------------------------------

PATCH_URL = "/api/v1/admin/users/{}"

OWN_ROLE = "You can't change your own role. Ask another administrator to do it."
OWN_ACTIVE = "You can't deactivate your own account."
FROM_COGNITO = (
    "Roles on this deployment come from Cognito groups and are updated on every "
    "request. Change this user's group in Cognito instead."
)

#: Every role a non-superuser can hold. ADMIN is included on purpose: the
#: admin area is gated on superuser, not on the ADMIN role.
NON_SUPERUSER_ROLES = [
    UserRole.ADMIN,
    UserRole.DEVELOPER,
    UserRole.ANALYST,
    UserRole.VIEWER,
]


def _account(
    db_session,
    *,
    role: UserRole = UserRole.VIEWER,
    is_superuser: bool = False,
    is_active: bool = True,
) -> User:
    suffix = uuid.uuid4().hex[:8]
    user = User(
        username=f"admin_patch_{suffix}",
        email=f"admin_patch_{suffix}@example.com",
        full_name=f"Admin Patch {suffix}",
        hashed_password=get_password_hash(PASSWORD),
        is_active=is_active,
        is_superuser=is_superuser,
        role=role,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _row(db_session, user_id) -> dict:
    """Every column of the user's row, as the database holds it now."""
    db_session.expire_all()
    user = db_session.get(User, user_id)
    return {c.name: getattr(user, c.name) for c in User.__table__.columns}


def _counts(db_session) -> tuple:
    db_session.expire_all()
    return (
        db_session.query(User).count(),
        db_session.query(User).filter(User.is_superuser.is_(True)).count(),
        db_session.query(User).filter(User.is_active.is_(True)).count(),
    )


def _audit_rows(db_session, entity_id=None) -> list:
    db_session.expire_all()
    query = db_session.query(AuditLog)
    if entity_id is not None:
        query = query.filter(AuditLog.entity_id == entity_id)
    return query.all()


def _assert_saved(before: dict, after: dict, changed: dict) -> None:
    """Exactly ``changed`` moved, ``updated_at`` advanced, and nothing else."""
    for column, value in changed.items():
        assert after[column] == value, column
    assert after["updated_at"] > before["updated_at"]
    untouched = set(before) - set(changed) - {"updated_at"}
    assert {c: after[c] for c in untouched} == {c: before[c] for c in untouched}


def _superuser_client(db_session):
    return make_client_for_user(
        db_session, _account(db_session, role=UserRole.ADMIN, is_superuser=True)
    )


@pytest.mark.regression
class TestPatchSaves:
    def test_the_edit_user_modal_body_is_saved(self, db_session):
        """The exact body the dashboard modal sends. On main: 405 (no PATCH)."""
        target = _account(db_session, role=UserRole.VIEWER)
        before = _row(db_session, target.id)
        client = _superuser_client(db_session)

        response = client.patch(
            PATCH_URL.format(target.id), json={"role": "ANALYST", "is_active": False}
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["role"] == "ANALYST" and body["is_active"] is False
        _assert_saved(
            before,
            _row(db_session, target.id),
            {"role": UserRole.ANALYST, "is_active": False},
        )

    def test_a_partial_change_leaves_every_other_column_alone(self, db_session):
        """``is_superuser`` must survive a body that does not mention it."""
        target = _account(db_session, role=UserRole.ADMIN, is_superuser=True)
        client = _superuser_client(db_session)

        before = _row(db_session, target.id)
        response = client.patch(PATCH_URL.format(target.id), json={"role": "DEVELOPER"})
        assert response.status_code == 200, response.text
        middle = _row(db_session, target.id)
        _assert_saved(before, middle, {"role": UserRole.DEVELOPER})
        assert middle["is_superuser"] is True and middle["is_active"] is True

        response = client.patch(PATCH_URL.format(target.id), json={"is_active": False})
        assert response.status_code == 200, response.text
        after = _row(db_session, target.id)
        _assert_saved(middle, after, {"is_active": False})
        assert after["is_superuser"] is True
        assert after["role"] == UserRole.DEVELOPER

    def test_a_lower_case_role_is_stored_upper_case(self, db_session):
        target = _account(db_session, role=UserRole.VIEWER)
        before = _row(db_session, target.id)
        client = _superuser_client(db_session)

        response = client.patch(PATCH_URL.format(target.id), json={"role": "analyst"})

        assert response.status_code == 200, response.text
        assert response.json()["role"] == "ANALYST"
        _assert_saved(before, _row(db_session, target.id), {"role": UserRole.ANALYST})

    def test_reactivating_a_user(self, db_session):
        target = _account(db_session, role=UserRole.VIEWER, is_active=False)
        before = _row(db_session, target.id)
        client = _superuser_client(db_session)

        response = client.patch(PATCH_URL.format(target.id), json={"is_active": True})

        assert response.status_code == 200, response.text
        _assert_saved(before, _row(db_session, target.id), {"is_active": True})

    @pytest.mark.parametrize("own", [False, True], ids=["another", "own"])
    def test_resending_the_stored_values_changes_nothing(self, db_session, own):
        """A 200 no-op, your own account included: no write, no audit row."""
        actor = _account(db_session, role=UserRole.ADMIN, is_superuser=True)
        target = actor if own else _account(db_session, role=UserRole.ANALYST)
        before = _row(db_session, target.id)
        client = make_client_for_user(db_session, actor)

        response = client.patch(
            PATCH_URL.format(target.id),
            json={"role": before["role"].name, "is_active": True},
        )

        assert response.status_code == 200, response.text
        assert _row(db_session, target.id) == before
        assert _audit_rows(db_session, target.id) == []


class TestPatchRefusals:
    def test_exactly_four_non_superuser_roles_are_checked(self):
        """A dropped case would silently shrink the refusal test below."""
        assert len(NON_SUPERUSER_ROLES) == 4
        assert set(NON_SUPERUSER_ROLES) == set(UserRole)

    @pytest.mark.regression
    @pytest.mark.parametrize("role", NON_SUPERUSER_ROLES, ids=lambda r: r.name)
    def test_a_non_superuser_is_refused_and_nothing_changes(self, db_session, role):
        target = _account(db_session, role=UserRole.VIEWER)
        actor = _account(db_session, role=role, is_superuser=False)
        before, counts = _row(db_session, target.id), _counts(db_session)
        audit_before = len(_audit_rows(db_session))
        client = make_client_for_user(db_session, actor)

        response = client.patch(
            PATCH_URL.format(target.id), json={"role": "ANALYST", "is_active": False}
        )

        assert response.status_code == 403, response.text
        assert response.json()["detail"] == "Not enough permissions"
        assert _row(db_session, target.id) == before
        assert _counts(db_session) == counts
        assert len(_audit_rows(db_session)) == audit_before

    def test_changing_your_own_role_is_refused(self, db_session):
        actor = _account(db_session, role=UserRole.ADMIN, is_superuser=True)
        before = _row(db_session, actor.id)
        client = make_client_for_user(db_session, actor)

        response = client.patch(PATCH_URL.format(actor.id), json={"role": "VIEWER"})

        assert response.status_code == 400, response.text
        assert response.json()["detail"] == OWN_ROLE
        assert _row(db_session, actor.id) == before
        assert _audit_rows(db_session, actor.id) == []

    def test_deactivating_yourself_is_refused(self, db_session):
        actor = _account(db_session, role=UserRole.ADMIN, is_superuser=True)
        before = _row(db_session, actor.id)
        client = make_client_for_user(db_session, actor)

        response = client.patch(PATCH_URL.format(actor.id), json={"is_active": False})

        assert response.status_code == 400, response.text
        assert response.json()["detail"] == OWN_ACTIVE
        assert _row(db_session, actor.id) == before
        assert _audit_rows(db_session, actor.id) == []

    @pytest.mark.regression
    @pytest.mark.parametrize(
        "key", ["bogus", "Role", "hashed_password", "is_superuser"]
    )
    def test_an_unknown_key_is_refused(self, db_session, key):
        """On main the PUT answered 200 and dropped such keys."""
        target = _account(db_session, role=UserRole.VIEWER)
        before = _row(db_session, target.id)
        client = _superuser_client(db_session)

        response = client.patch(
            PATCH_URL.format(target.id), json={"role": "ANALYST", key: True}
        )

        assert response.status_code == 422, response.text
        errors = response.json()["detail"]
        assert [(e["type"], e["loc"]) for e in errors] == [
            ("extra_forbidden", ["body", key])
        ]
        assert _row(db_session, target.id) == before
        assert _audit_rows(db_session, target.id) == []

    @pytest.mark.parametrize(
        "body, error_type",
        [
            ({"role": "SUPERADMIN"}, "literal_error"),
            ({"role": None}, "literal_error"),
            ({"is_active": None}, "bool_type"),
            ({}, "value_error"),
        ],
        ids=["unknown-role", "null-role", "null-active", "empty"],
    )
    def test_a_bad_value_is_refused(self, db_session, body, error_type):
        target = _account(db_session, role=UserRole.VIEWER)
        before = _row(db_session, target.id)
        client = _superuser_client(db_session)

        response = client.patch(PATCH_URL.format(target.id), json=body)

        assert response.status_code == 422, response.text
        assert response.json()["detail"][0]["type"] == error_type
        assert _row(db_session, target.id) == before
        assert _audit_rows(db_session, target.id) == []

    def test_an_unknown_user_is_404(self, db_session):
        client = _superuser_client(db_session)
        audit_before = len(_audit_rows(db_session))

        response = client.patch(PATCH_URL.format(uuid.uuid4()), json={"role": "VIEWER"})

        assert response.status_code == 404, response.text
        assert response.json()["detail"] == "User not found"
        assert len(_audit_rows(db_session)) == audit_before


class TestPatchUnderCognitoRoleSync:
    """Under Cognito with role sync on, a role change would be overwritten on
    the target's next request; the route refuses it instead."""

    @pytest.fixture
    def cognito(self, monkeypatch):
        monkeypatch.setattr(settings, "AUTH_PROVIDER", "cognito")
        monkeypatch.setattr(settings, "SYNC_ROLES_ON_LOGIN", True)
        return settings

    def test_a_role_change_is_409(self, db_session, cognito):
        target = _account(db_session, role=UserRole.VIEWER)
        before = _row(db_session, target.id)
        client = _superuser_client(db_session)

        response = client.patch(PATCH_URL.format(target.id), json={"role": "ANALYST"})

        assert response.status_code == 409, response.text
        assert response.json()["detail"] == FROM_COGNITO
        assert _row(db_session, target.id) == before
        assert _audit_rows(db_session, target.id) == []

    def test_a_role_change_is_saved_with_sync_off(
        self, db_session, cognito, monkeypatch
    ):
        monkeypatch.setattr(cognito, "SYNC_ROLES_ON_LOGIN", False)
        target = _account(db_session, role=UserRole.VIEWER)
        before = _row(db_session, target.id)
        client = _superuser_client(db_session)

        response = client.patch(PATCH_URL.format(target.id), json={"role": "ANALYST"})

        assert response.status_code == 200, response.text
        _assert_saved(before, _row(db_session, target.id), {"role": UserRole.ANALYST})

    def test_deactivation_is_saved_under_sync(self, db_session, cognito):
        """``is_active`` is never synced from Cognito, so it sticks."""
        target = _account(db_session, role=UserRole.VIEWER)
        before = _row(db_session, target.id)
        client = _superuser_client(db_session)

        response = client.patch(PATCH_URL.format(target.id), json={"is_active": False})

        assert response.status_code == 200, response.text
        _assert_saved(before, _row(db_session, target.id), {"is_active": False})

    def test_the_sync_rewrites_the_role_on_the_next_request(
        self, db_session, cognito, monkeypatch, plain_db_client
    ):
        """The premise of the 409: a stored role is overwritten from the
        user's groups by their next authenticated request. If this stops being
        true, the 409 and its text have to change with it."""
        user = _account(db_session, role=UserRole.ANALYST)
        # A Cognito sign-in reaches the account linked to its user ID.
        sub = str(uuid.uuid4())
        user.external_id = f"cognito:{sub}"
        db_session.commit()
        monkeypatch.setattr(
            deps.auth_service,
            "get_user_with_groups",
            lambda token: {
                "username": user.username,
                "attributes": {"sub": sub, "email": user.email},
                "groups": ["Viewers"],
            },
        )

        response = plain_db_client.get(
            "/api/v1/users/me", headers={"Authorization": "Bearer cognito-token"}
        )

        assert response.status_code == 200, response.text
        assert _row(db_session, user.id)["role"] == UserRole.VIEWER


class TestPatchAudit:
    def test_each_change_writes_its_audit_row_with_before_and_after(self, db_session):
        """A role change is ``role_assign`` (role and superuser flag), an
        active-status change ``user_deactivate``: one row for each (#221)."""
        target = _account(db_session, role=UserRole.VIEWER)
        actor = _account(db_session, role=UserRole.ADMIN, is_superuser=True)
        client = make_client_for_user(db_session, actor)

        response = client.patch(
            PATCH_URL.format(target.id), json={"role": "ANALYST", "is_active": False}
        )

        assert response.status_code == 200, response.text
        rows = {row.action_type: row for row in _audit_rows(db_session, target.id)}
        assert sorted(rows) == ["role_assign", "user_deactivate"]
        for row in rows.values():
            assert row.entity_type == "user"
            assert row.user_id == actor.id
            assert row.user_email == actor.email
            assert row.entity_name == target.username
        role = rows["role_assign"]
        assert json.loads(role.old_value) == {"role": "VIEWER", "is_superuser": False}
        assert json.loads(role.new_value) == {"role": "ANALYST", "is_superuser": False}
        active = rows["user_deactivate"]
        assert json.loads(active.old_value) == {"is_active": True}
        assert json.loads(active.new_value) == {"is_active": False}

    def test_when_the_audit_insert_fails_the_user_is_unchanged(self, db_session):
        """One transaction: no audit row means no change either."""
        target = _account(db_session, role=UserRole.VIEWER)
        before = _row(db_session, target.id)
        client = _superuser_client(db_session)

        def refuse(mapper, connection, instance):
            raise RuntimeError("audit insert refused (test)")

        event.listen(AuditLog, "before_insert", refuse)
        try:
            try:
                response = client.patch(
                    PATCH_URL.format(target.id), json={"role": "ANALYST"}
                )
                status_code = response.status_code
            except Exception:  # raised through the test client
                status_code = 500
        finally:
            event.remove(AuditLog, "before_insert", refuse)

        assert status_code == 500
        assert _row(db_session, target.id) == before
        assert _audit_rows(db_session, target.id) == []


class TestPatchSuperuserLock:
    """Two superusers deactivating each other at once must not both win."""

    def test_deactivating_a_superuser_locks_the_superuser_rows_first(self, db_session):
        target = _account(db_session, role=UserRole.ADMIN, is_superuser=True)
        client = _superuser_client(db_session)
        engine = db_session.get_bind()
        statements: list = []

        def record(conn, cursor, statement, parameters, context, executemany):
            statements.append(" ".join(statement.split()))

        event.listen(engine, "before_cursor_execute", record)
        try:
            response = client.patch(
                PATCH_URL.format(target.id), json={"is_active": False}
            )
        finally:
            event.remove(engine, "before_cursor_execute", record)

        assert response.status_code == 200, response.text
        locks = [
            i
            for i, s in enumerate(statements)
            if s.startswith("SELECT ") and ".users" in s and s.endswith(" FOR UPDATE")
        ]
        updates = [
            i
            for i, s in enumerate(statements)
            if s.startswith("UPDATE ") and ".users SET " in s
        ]
        assert locks, statements
        assert updates, statements
        assert locks[0] < updates[0], statements
        assert " ORDER BY " in statements[locks[0]], statements[locks[0]]

    def test_a_caller_deactivated_a_moment_ago_is_refused(self, db_session):
        """The caller's row is re-read under the lock. The test client never
        checks ``is_active`` itself, so this reaches the route as a request
        from a superuser whom another superuser has just deactivated."""
        target = _account(db_session, role=UserRole.ADMIN, is_superuser=True)
        actor = _account(db_session, role=UserRole.ADMIN, is_superuser=True)
        client = make_client_for_user(db_session, actor)
        actor.is_active = False
        db_session.commit()
        before, counts = _row(db_session, target.id), _counts(db_session)

        response = client.patch(PATCH_URL.format(target.id), json={"is_active": False})

        assert response.status_code == 400, response.text
        assert response.json()["detail"] == "Inactive user"
        assert _row(db_session, target.id) == before
        assert _counts(db_session) == counts
        assert _audit_rows(db_session, target.id) == []


@pytest.fixture
def plain_db_client(db_session):
    """A TestClient whose only override is the database session: real auth."""
    factory = sessionmaker(bind=db_session.get_bind(), autoflush=False)

    def override_get_db():
        session = factory()
        session.execute(text("SET search_path TO test_experimentation"))
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[deps.get_db] = override_get_db
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client
    finally:
        app.dependency_overrides.pop(deps.get_db, None)


class TestPatchThroughRealSignIn:
    """No auth override at all: a real token from /api/v1/auth/login and the
    real ``deps.get_current_superuser``. The integration client replaces that
    dependency with its own copy, so a defect in it is invisible above."""

    @pytest.fixture(autouse=True)
    def _local_provider(self, monkeypatch):
        monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
        monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
        monkeypatch.setattr(settings, "ENVIRONMENT", "test")
        login_attempt_tracker.clear()
        yield
        login_attempt_tracker.clear()

    @staticmethod
    def _token(client, user) -> dict:
        response = client.post(
            "/api/v1/auth/login", json={"email": user.email, "password": PASSWORD}
        )
        assert response.status_code == 200, response.text
        return {"Authorization": f"Bearer {response.json()['access_token']}"}

    @staticmethod
    def _assert_no_auth_override():
        for dependency in (
            deps.get_current_user,
            deps.get_current_active_user,
            deps.get_current_superuser,
        ):
            assert dependency not in app.dependency_overrides, dependency.__name__

    def test_an_admin_role_without_superuser_is_refused(
        self, db_session, plain_db_client
    ):
        self._assert_no_auth_override()
        target = _account(db_session, role=UserRole.VIEWER)
        actor = _account(db_session, role=UserRole.ADMIN, is_superuser=False)
        before = _row(db_session, target.id)

        response = plain_db_client.patch(
            PATCH_URL.format(target.id),
            json={"role": "ANALYST"},
            headers=self._token(plain_db_client, actor),
        )

        assert response.status_code == 403, response.text
        assert response.json()["detail"] == "Not enough permissions"
        assert _row(db_session, target.id) == before

    def test_a_superuser_saves_and_cannot_lock_themselves_out(
        self, db_session, plain_db_client
    ):
        self._assert_no_auth_override()
        target = _account(db_session, role=UserRole.VIEWER)
        actor = _account(db_session, role=UserRole.ADMIN, is_superuser=True)
        headers = self._token(plain_db_client, actor)

        saved = plain_db_client.patch(
            PATCH_URL.format(target.id), json={"role": "ANALYST"}, headers=headers
        )
        assert saved.status_code == 200, saved.text
        assert _row(db_session, target.id)["role"] == UserRole.ANALYST

        refused = plain_db_client.patch(
            PATCH_URL.format(actor.id), json={"is_active": False}, headers=headers
        )
        assert refused.status_code == 400, refused.text
        assert refused.json()["detail"] == OWN_ACTIVE
        assert _row(db_session, actor.id)["is_active"] is True
        # Still signed in: the same token still opens the admin area.
        listed = plain_db_client.get("/api/v1/admin/users", headers=headers)
        assert listed.status_code == 200, listed.text


class TestDeactivatingAnOwnerStopsTheirKeys:
    """What the Edit user dialog tells an administrator before they save (#657).

    The dialog says the user's keys stop working, and that reactivating the user
    makes them work again unless they have expired. Real keys, real tokens, no
    auth override: only ``deps.get_db`` is replaced. The key rows are read back
    to show it is the owner, not the key, that a deactivation changes.
    """

    PROBE = "/api/v1/feature-flags/user/probe-user"

    @pytest.fixture(autouse=True)
    def _local_provider(self, monkeypatch):
        monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
        monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
        monkeypatch.setattr(settings, "ENVIRONMENT", "test")

    @staticmethod
    def _bearer(user) -> dict:
        return {"Authorization": f"Bearer {create_local_access_token(user)}"}

    def _probe(self, client, key: str):
        return client.get(self.PROBE, headers={"X-API-Key": key})

    def _set_active(self, client, actor, target, value: bool) -> None:
        response = client.patch(
            PATCH_URL.format(target.id),
            json={"is_active": value},
            headers=self._bearer(actor),
        )
        assert response.status_code == 200, response.text

    def test_the_owners_key_is_401_while_they_are_inactive_and_works_again(
        self, db_session, plain_db_client
    ):
        owner = _account(db_session, role=UserRole.DEVELOPER)
        actor = _account(db_session, role=UserRole.ADMIN, is_superuser=True)
        row, key = APIKey.create_for_user(
            db_session, user_id=owner.id, name="Checkout server", scopes="read"
        )
        assert self._probe(plain_db_client, key).status_code == 200

        self._set_active(plain_db_client, actor, owner, False)
        refused = self._probe(plain_db_client, key)
        assert refused.status_code == 401, refused.text
        assert refused.json()["detail"] == "Invalid API Key"
        db_session.expire_all()
        assert db_session.get(APIKey, row.id).is_active is True

        self._set_active(plain_db_client, actor, owner, True)
        assert self._probe(plain_db_client, key).status_code == 200

    def test_a_key_that_expired_meanwhile_stays_401_after_reactivation(
        self, db_session, plain_db_client
    ):
        owner = _account(db_session, role=UserRole.DEVELOPER)
        actor = _account(db_session, role=UserRole.ADMIN, is_superuser=True)
        row, key = APIKey.create_for_user(
            db_session,
            user_id=owner.id,
            name="Short lived",
            scopes="read",
            expires_at=datetime.utcnow() + timedelta(days=1),
        )
        assert self._probe(plain_db_client, key).status_code == 200

        self._set_active(plain_db_client, actor, owner, False)
        # The expiry passes while the owner is inactive.
        row.expires_at = datetime.utcnow() - timedelta(minutes=1)
        db_session.commit()

        self._set_active(plain_db_client, actor, owner, True)
        refused = self._probe(plain_db_client, key)
        assert refused.status_code == 401, refused.text
        assert refused.json()["detail"] == "Invalid API Key"


class TestPatchContract:
    def test_the_operation_is_beta_and_its_body_has_no_null_branch(self):
        """``Optional`` would publish ``anyOf: [enum, null]`` while the API
        refuses null, and a client generator would then send it."""
        spec = app.openapi()
        operation = spec["paths"]["/api/v1/admin/users/{user_id}"]["patch"]
        assert operation["x-stability"] == "beta"
        ref = operation["requestBody"]["content"]["application/json"]["schema"]["$ref"]
        schema = spec["components"]["schemas"][ref.rsplit("/", 1)[-1]]

        assert schema["additionalProperties"] is False
        assert set(schema["properties"]) == {"role", "is_active"}
        assert "required" not in schema
        for name, prop in schema["properties"].items():
            assert "anyOf" not in prop and "oneOf" not in prop, name
            assert prop.get("type") in ("string", "boolean"), name
            assert "default" not in prop, name
        assert schema["properties"]["role"]["enum"] == [
            "ADMIN",
            "DEVELOPER",
            "ANALYST",
            "VIEWER",
        ]
        assert schema["properties"]["is_active"]["type"] == "boolean"
