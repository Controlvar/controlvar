"""Signing in to a demo install, and what the signed-in visitor can change.

With ``DEMO_MODE=true`` the local sign-in compares no addresses:

* only an active non-superuser VIEWER signs in, with or without a published
  demo account. Any other account -- an administrator, a developer, an
  analyst, a viewer that is also a superuser, the demo account if its role was
  changed -- gets the same 401 and message as a wrong password, even with its
  right password;
* while a demo account is published (``DEMO_ACCOUNT_EMAIL`` and
  ``DEMO_ACCOUNT_PASSWORD`` set), failed attempts are neither counted nor
  checked, for any address, so the shared account answers 401 to every wrong
  password and 200 to the right one however many wrong ones came before --
  never 423. Without a published account they are counted as usual.

Signed in, the demo viewer's own account cannot be changed: each account write
answers the demo refusal, and the row it would have changed is the same
afterwards.

Real PostgreSQL, real bcrypt, real local tokens; the only override is
``deps.get_db``.
"""

from __future__ import annotations

import uuid
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.core.config import settings
from backend.app.core.demo_mode import REFUSAL_BODY
from backend.app.core.security import get_password_hash, verify_password
from backend.app.main import app
from backend.app.models.api_key import APIKey
from backend.app.models.notification import NotificationPreference
from backend.app.models.user import User, UserRole
from backend.app.services.local_auth_service import login_attempt_tracker

pytestmark = pytest.mark.integration

PASSWORD = "Demo-Visitor-Passw0rd"
WRONG = "Not-The-Passw0rd"
LOGIN = "/api/v1/auth/login"
TOKEN = "/api/v1/auth/token"
WRONG_ANSWER = (401, {"detail": "Invalid email or password"})
REFUSED = (403, REFUSAL_BODY)


@pytest.fixture(autouse=True)
def _demo_install(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")
    monkeypatch.setattr(settings, "DEMO_MODE", True)
    monkeypatch.setattr(settings, "DEMO_ACCOUNT_EMAIL", "")
    monkeypatch.setattr(settings, "DEMO_ACCOUNT_PASSWORD", "")
    login_attempt_tracker.clear()
    yield
    login_attempt_tracker.clear()


def _publish(monkeypatch, user: "User") -> None:
    """Publish *user* as the demo account, as the settings of a demo install do."""
    monkeypatch.setattr(settings, "DEMO_ACCOUNT_EMAIL", user.email)
    monkeypatch.setattr(settings, "DEMO_ACCOUNT_PASSWORD", PASSWORD)
    assert settings.demo_account_published is True


@pytest.fixture
def client(db_session) -> Iterator[TestClient]:
    factory = sessionmaker(
        bind=db_session.get_bind(), autocommit=False, autoflush=False
    )

    def override_get_db():
        session = factory()
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


def _account(db_session, role: UserRole, *, superuser: bool = False) -> User:
    suffix = uuid.uuid4().hex[:10]
    user = User(
        username=f"demo_{role.value}_{suffix}",
        email=f"demo_{role.value}_{suffix}@example.com",
        full_name=f"Demo {role.value}",
        hashed_password=get_password_hash(PASSWORD),
        role=role,
        is_superuser=superuser,
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    return user


@pytest.fixture
def viewer(db_session) -> User:
    return _account(db_session, UserRole.VIEWER)


def _login(client, email: str, password: str):
    response = client.post(LOGIN, json={"email": email, "password": password})
    body = response.json() if response.content else None
    return response.status_code, body


def _signed_in(client, user: User) -> dict:
    status, body = _login(client, user.email, PASSWORD)
    assert status == 200, body
    return {"Authorization": f"Bearer {body['access_token']}"}


# --- who signs in --------------------------------------------------------------------


def test_the_demo_viewer_signs_in(client, viewer):
    status, body = _login(client, viewer.email, PASSWORD)
    assert status == 200, body
    assert body["user"]["role"].lower() == "viewer"
    assert body["user"]["is_superuser"] is False


def test_the_demo_viewer_signs_in_on_the_token_route(client, viewer):
    response = client.post(TOKEN, data={"username": viewer.email, "password": PASSWORD})
    assert response.status_code == 200, response.text


def test_wrong_passwords_never_stop_the_published_account_signing_in(
    client, viewer, monkeypatch
):
    _publish(monkeypatch, viewer)
    attempts = settings.LOCAL_AUTH_MAX_FAILED_ATTEMPTS + 1
    answers = [_login(client, viewer.email, WRONG) for _ in range(attempts)]
    assert answers == [WRONG_ANSWER] * attempts
    status, body = _login(client, viewer.email, PASSWORD)
    assert status == 200, f"{status} after {attempts} wrong passwords: {body}"


def test_with_an_account_published_wrong_passwords_never_stop_any_address(
    client, db_session, viewer, monkeypatch
):
    _publish(monkeypatch, viewer)
    other = _account(db_session, UserRole.VIEWER)
    attempts = settings.LOCAL_AUTH_MAX_FAILED_ATTEMPTS + 1
    for address in (other.email, "nobody@example.com", other.email.upper()):
        answers = [_login(client, address, WRONG) for _ in range(attempts)]
        assert answers == [WRONG_ANSWER] * attempts, address
    assert len(login_attempt_tracker) == 0, "a failed attempt was counted"
    assert _login(client, other.email, PASSWORD)[0] == 200


def test_without_a_published_account_wrong_passwords_count_as_usual(client, viewer):
    assert settings.demo_account_published is False
    limit = settings.LOCAL_AUTH_MAX_FAILED_ATTEMPTS
    answers = [_login(client, viewer.email, WRONG) for _ in range(limit)]
    assert answers == [WRONG_ANSWER] * limit
    status, body = _login(client, viewer.email, PASSWORD)
    assert status == 423, f"{status} after {limit} wrong passwords: {body}"


ROLES = pytest.mark.parametrize(
    "role,superuser",
    [
        (UserRole.ADMIN, True),
        (UserRole.ADMIN, False),
        (UserRole.DEVELOPER, False),
        (UserRole.ANALYST, False),
        (UserRole.VIEWER, True),
    ],
    ids=["admin-superuser", "admin", "developer", "analyst", "viewer-superuser"],
)


@ROLES
@pytest.mark.parametrize("published", [False, True], ids=["no-account", "account"])
def test_only_a_non_superuser_viewer_signs_in(
    client, db_session, viewer, monkeypatch, role, superuser, published
):
    if published:
        _publish(monkeypatch, viewer)
    account = _account(db_session, role, superuser=superuser)
    assert _login(client, account.email, PASSWORD) == WRONG_ANSWER
    assert _login(client, account.email, WRONG) == WRONG_ANSWER
    assert _login(client, viewer.email, PASSWORD)[0] == 200


@pytest.mark.parametrize("published", [False, True], ids=["no-account", "account"])
def test_the_demo_viewer_made_an_administrator_cannot_sign_in(
    client, db_session, viewer, monkeypatch, published
):
    if published:
        _publish(monkeypatch, viewer)
    viewer.role = UserRole.ADMIN
    db_session.commit()
    assert _login(client, viewer.email, PASSWORD) == WRONG_ANSWER


def test_an_inactive_viewer_cannot_sign_in(client, db_session, viewer):
    viewer.is_active = False
    db_session.commit()
    assert _login(client, viewer.email, PASSWORD) == WRONG_ANSWER


def test_with_demo_mode_off_the_administrator_signs_in(client, db_session, monkeypatch):
    """The control: the role rule is demo mode's, not the fixture's."""
    monkeypatch.setattr(settings, "DEMO_MODE", False)
    admin = _account(db_session, UserRole.ADMIN, superuser=True)
    status, body = _login(client, admin.email, PASSWORD)
    assert status == 200, body


# --- what the signed-in visitor can change --------------------------------------------


def _password_hash(db_session, user: User) -> str:
    db_session.expire_all()
    return db_session.scalar(select(User.hashed_password).where(User.id == user.id))


def _key_count(db_session, user: User) -> int:
    db_session.expire_all()
    return db_session.scalar(
        select(func.count()).select_from(APIKey).where(APIKey.user_id == user.id)
    )


def _preferences(db_session, user: User):
    db_session.expire_all()
    row = db_session.scalars(
        select(NotificationPreference).where(NotificationPreference.user_id == user.id)
    ).first()
    return None if row is None else (row.notify_rollout_advanced, row.slack_channel)


ACCOUNT_WRITES = [
    (
        "PUT",
        "/api/v1/users/{me}",
        {"full_name": "Changed by a visitor", "email": "changed@example.com"},
    ),
    (
        "POST",
        "/api/v1/users/me/password",
        {"current_password": PASSWORD, "new_password": "Visitor-Chosen-Passw0rd"},
    ),
    ("POST", "/api/v1/api-keys", {"name": "visitor key", "scopes": ["read"]}),
    (
        "PUT",
        "/api/v1/notifications/preferences",
        {"notify_rollout_advanced": True, "slack_channel": "#visitor"},
    ),
    ("DELETE", "/api/v1/users/{me}", None),
]


def test_the_demo_viewer_changes_nothing_of_its_own_account(client, db_session, viewer):
    headers = _signed_in(client, viewer)
    before = (
        _password_hash(db_session, viewer),
        _key_count(db_session, viewer),
        _preferences(db_session, viewer),
        viewer.full_name,
    )
    wrong = {}
    for method, path, body in ACCOUNT_WRITES:
        url = path.replace("{me}", str(viewer.id))
        response = client.request(method, url, json=body, headers=headers)
        answer = (response.status_code, response.json() if response.content else None)
        if answer != REFUSED:
            wrong[f"{method} {path}"] = answer
    assert not wrong, wrong
    db_session.expire_all()
    after = (
        _password_hash(db_session, viewer),
        _key_count(db_session, viewer),
        _preferences(db_session, viewer),
        db_session.get(User, viewer.id).full_name,
    )
    assert after == before
    assert verify_password(PASSWORD, after[0])
    # Still signed in, and still able to read.
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 200
