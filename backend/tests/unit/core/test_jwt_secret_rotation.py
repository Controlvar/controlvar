"""Rotating ``SECRET_KEY`` (the JWT secret) invalidates every token the API signed.

A password sign-in ends with ``create_local_access_token``, signed with
``SECRET_KEY`` (the SSO routes end with the same token:
``modules/backend/tests/unit/api/test_sso_ends_with_the_platforms_token.py``).
After the secret changes, the API refuses such a token: ``decode_local_token``
raises, and a request carrying it answers 401.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.app.api import deps
from backend.app.api.v1.endpoints import auth
from backend.app.core import security
from backend.app.core.config import settings
from backend.app.main import app
from backend.app.models.user import User, UserRole

pytestmark = [pytest.mark.unit, pytest.mark.regression]

OLD, NEW = "o" * 48, "n" * 48


def _token(monkeypatch) -> str:
    monkeypatch.setattr(settings, "SECRET_KEY", OLD)
    user = SimpleNamespace(id=uuid.uuid4(), email="someone@example.com", role="VIEWER")
    # The function the sign-in route issues its token with.
    assert auth.create_local_access_token is security.create_local_access_token
    return auth.create_local_access_token(user)


def test_a_token_signed_with_the_old_secret_is_refused_after_the_rotation(monkeypatch):
    token = _token(monkeypatch)
    assert security.decode_local_token(token)["email"] == "someone@example.com"
    monkeypatch.setattr(settings, "SECRET_KEY", NEW)
    with pytest.raises(security.InvalidTokenError):
        security.decode_local_token(token)


class _OneUser:
    """A session whose every user lookup finds ``user``: the control below
    shows the token signs that user in until the secret changes."""

    def __init__(self, user):
        self.user = user

    def query(self, *args, **kwargs):
        return self

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return self.user

    def close(self):
        pass


def test_a_request_with_it_answers_401_after_the_rotation(monkeypatch):
    user = User(
        id=uuid.uuid4(),
        username="rotation",
        email="rotation@example.com",
        hashed_password="x",
        is_active=True,
        is_superuser=False,
        role=UserRole.VIEWER,
    )
    monkeypatch.setattr(settings, "SECRET_KEY", OLD)
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    token = auth.create_local_access_token(user)
    app.dependency_overrides[deps.get_db] = lambda: _OneUser(user)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            headers = {"Authorization": f"Bearer {token}"}
            before = client.get("/api/v1/auth/me", headers=headers)
            monkeypatch.setattr(settings, "SECRET_KEY", NEW)
            after = client.get("/api/v1/auth/me", headers=headers)
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
    assert before.status_code == 200, before.text
    assert before.json()["email"] == "rotation@example.com"
    assert after.status_code == 401
    assert after.json() == {"detail": deps._credentials_exception().detail}
