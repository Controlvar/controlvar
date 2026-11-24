"""The dashboard's SSO exchange answers an account with no email address or
username (#2067).

``POST /api/v1/auth/sso/exchange`` carries the same profile as
``GET /api/v1/auth/me`` (``user_to_me``), which refused to describe an account
whose ``email`` or ``username`` is NULL: the exchange answered 500.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest

from backend.app.core.security import decode_local_token
from backend.app.models.user import User, UserRole
from modules.backend.app.api.v1.endpoints import sso

pytestmark = [pytest.mark.unit, pytest.mark.regression]


@pytest.mark.parametrize(
    "empty",
    [("email",), ("username",), ("email", "username")],
    ids=["no-email", "no-username", "neither"],
)
def test_the_exchange_answers_null_for_an_empty_email_or_username(empty, monkeypatch):
    recorded = []
    monkeypatch.setattr(
        sso, "_record_sso_login", lambda db, user: recorded.append(user.id)
    )
    fields = {"email": "sso@example.com", "username": "sso", **dict.fromkeys(empty)}
    account = User(
        id=uuid.uuid4(),
        hashed_password="an-sso-account",
        is_active=True,
        is_superuser=False,
        role=UserRole.VIEWER,
        **fields,
    )
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = account

    result = sso._exchange_session(db, account.id)

    assert decode_local_token(result.access_token)["sub"] == str(account.id)
    body = result.model_dump(mode="json")["user"]
    assert {"email": body["email"], "username": body["username"]} == fields
    assert body["id"] == str(account.id)
    assert recorded == [account.id]
