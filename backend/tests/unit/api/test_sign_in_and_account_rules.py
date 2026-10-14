"""Sign-in, passwords, tokens and roles, as the API applies them (#1142).

The password rules; a wrong current password; how long a local token lasts;
which roles may create a feature flag; a superuser deleting their own account;
and a deactivated account's tokens, refused only while it stays deactivated.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.app.api import deps
from backend.app.api.v1.endpoints import users as users_endpoint
from backend.app.core.config import Settings
from backend.app.core.permissions import Action, ResourceType, check_permission
from backend.app.core.security import create_local_access_token, decode_local_token
from backend.app.main import app
from backend.app.models.user import UserRole
from backend.app.schemas.user import check_password_strength
from backend.app.services.local_auth_service import InvalidCredentialsError


@pytest.mark.parametrize(
    "password, accepted",
    [
        ("Viewer1234!", True),
        ("Vie1234", False),  # seven characters
        ("viewer1234!", False),  # no upper-case letter
        ("VIEWER1234!", False),  # no lower-case letter
        ("Viewer!!!!!", False),  # no digit
        ("Aa1" + "é" * 35, False),  # 73 bytes in UTF-8
        ("Aa1" + "b" * 69, True),  # exactly 72 bytes
    ],
)
def test_the_password_rules(password, accepted):
    if accepted:
        assert check_password_strength(password) == password
    else:
        with pytest.raises(ValueError):
            check_password_strength(password)


def test_a_weak_password_answers_422(monkeypatch):
    admin = SimpleNamespace(
        id=uuid.uuid4(), role=UserRole.ADMIN, is_active=True, is_superuser=True
    )
    app.dependency_overrides[deps.get_current_active_user] = lambda: admin
    app.dependency_overrides[deps.get_db] = lambda: MagicMock()
    try:
        response = TestClient(app).post(
            "/api/v1/users/",
            json={
                "email": "viewer@example.com",
                "username": "viewer",
                "password": "weakpass",
                "role": "VIEWER",
            },
        )
    finally:
        app.dependency_overrides.pop(deps.get_current_active_user, None)
        app.dependency_overrides.pop(deps.get_db, None)
    assert response.status_code == 422
    assert "weakpass" not in response.text


def test_a_wrong_current_password_is_refused_with_403():
    user = SimpleNamespace(
        id=uuid.uuid4(), role=UserRole.VIEWER, is_active=True, is_superuser=False
    )
    app.dependency_overrides[deps.get_current_active_user] = lambda: user
    app.dependency_overrides[deps.get_db] = lambda: MagicMock()
    try:
        with patch.object(
            users_endpoint.local_auth_service,
            "verify_current_password",
            side_effect=InvalidCredentialsError(),
        ):
            response = TestClient(app).post(
                "/api/v1/users/me/password",
                json={
                    "current_password": "Not-the-password1",
                    "new_password": "Viewer5678!",
                },
            )
    finally:
        app.dependency_overrides.pop(deps.get_current_active_user, None)
        app.dependency_overrides.pop(deps.get_db, None)
    assert response.status_code == 403
    assert response.json() == {"detail": "The current password is incorrect."}


def test_a_local_token_expires_after_its_ttl():
    assert Settings(_env_file=None).LOCAL_AUTH_TOKEN_TTL_MINUTES == 720  # 12 hours
    user = SimpleNamespace(
        id=uuid.uuid4(), email="ana@example.com", role=UserRole.VIEWER
    )
    claims = decode_local_token(create_local_access_token(user, expires_minutes=30))
    assert claims["exp"] - claims["iat"] == 30 * 60

    expired = create_local_access_token(user, expires_minutes=-1)
    with pytest.raises(Exception):
        decode_local_token(expired)


@pytest.mark.parametrize(
    "role, allowed",
    [
        (UserRole.ADMIN, True),
        (UserRole.DEVELOPER, True),
        (UserRole.ANALYST, False),
        (UserRole.VIEWER, False),
    ],
)
def test_the_role_decides_which_operations_are_available(role, allowed):
    user = SimpleNamespace(role=role, is_superuser=False)
    assert check_permission(user, ResourceType.FEATURE_FLAG, Action.CREATE) is allowed
    assert check_permission(user, ResourceType.EXPERIMENT, Action.CREATE) is allowed
    assert check_permission(user, ResourceType.FEATURE_FLAG, Action.READ) is True


@pytest.mark.parametrize("path", ["/api/v1/admin/users/{id}", "/api/v1/users/{id}"])
def test_a_superuser_cannot_delete_their_own_account(path):
    admin = SimpleNamespace(
        id=uuid.uuid4(),
        role=UserRole.ADMIN,
        is_active=True,
        is_superuser=True,
        email="admin@example.com",
    )
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = admin
    app.dependency_overrides[deps.get_current_active_user] = lambda: admin
    app.dependency_overrides[deps.get_current_superuser] = lambda: admin
    app.dependency_overrides[deps.get_db] = lambda: db
    try:
        response = TestClient(app).delete(path.format(id=admin.id))
    finally:
        for dependency in (
            deps.get_current_active_user,
            deps.get_current_superuser,
            deps.get_db,
        ):
            app.dependency_overrides.pop(dependency, None)
    assert response.status_code == 400, response.text
    db.delete.assert_not_called()


def test_a_deactivated_accounts_token_works_again_once_it_is_reactivated():
    user = SimpleNamespace(
        id=uuid.uuid4(), email="ana@example.com", role=UserRole.VIEWER
    )
    token = create_local_access_token(user)
    row = SimpleNamespace(id=user.id, is_active=False)
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = row
    with pytest.raises(HTTPException) as refused:
        deps._authenticate_local_token(token, db)
    assert refused.value.status_code == 400
    row.is_active = True
    assert deps._authenticate_local_token(token, db) is row


def test_the_admin_role_is_not_a_superuser():
    admin_role = SimpleNamespace(
        id=uuid.uuid4(), role=UserRole.ADMIN, is_active=True, is_superuser=False
    )
    with pytest.raises(HTTPException) as refused:
        deps.get_current_superuser(current_user=admin_role)
    assert refused.value.status_code == 403
    superuser = SimpleNamespace(
        id=uuid.uuid4(), role=UserRole.VIEWER, is_active=True, is_superuser=True
    )
    assert deps.get_current_superuser(current_user=superuser) is superuser


def test_registration_needs_an_email_a_strong_password_and_a_name():
    from pydantic import ValidationError

    from backend.app.schemas.auth import ConfirmSignUpRequest, SignUpRequest

    good = {
        "username": "ana",
        "password": "Viewer1234!",
        "email": "ana@example.com",
        "given_name": "Ana",
        "family_name": "Lee",
    }
    assert SignUpRequest(**good).given_name == "Ana"
    for change in (
        {"email": "not-an-address"},
        {"password": "short1A"},
        {"password": "viewer1234!"},
        {"password": "VIEWER1234!"},
        {"password": "Viewer!!!!!"},
        {"given_name": ""},
        {"family_name": ""},
    ):
        with pytest.raises(ValidationError):
            SignUpRequest(**{**good, **change})
    assert set(ConfirmSignUpRequest.model_fields) == {"username", "confirmation_code"}
