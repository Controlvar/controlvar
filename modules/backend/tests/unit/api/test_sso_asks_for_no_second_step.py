"""OIDC sign-in through the ``sso`` module asks for no two-step code (#1812).

Two-step sign-in is the second step of the local *password* sign-in
(``POST /api/v1/auth/login`` answers ``mfa_required``, then
``POST /api/v1/auth/mfa/verify`` takes the code). An SSO sign-in ends in
``_oidc_sign_in`` (the OIDC callback) or ``_exchange_session`` (the
dashboard's code exchange), and both issue the access token at once, for an
account with two-step sign-in on too: the identity provider's own checks are
the only ones, for an account whose role requires two-step sign-in
(``LOCAL_AUTH_2FA_REQUIRED_ROLES``) as well. That is a known limit, stated in
``docs/auth/two-factor.md``; these tests pin it, so a change that makes SSO
ask for a code fails here and the page changes with it. The account is one
that has two-step sign-in on, a password and a required role by every
reading of it (the service, the model's ``totp_enabled``, the policy), so a
check written any of those ways changes the answer.
"""

from __future__ import annotations

import inspect
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend.app.core.config import settings
from backend.app.core.security import decode_local_token
from backend.app.core.two_factor_policy import two_factor_required
from backend.app.models.user import UserRole
from backend.app.services import two_factor_service as two_factor_module
from modules.backend.app.api.v1.endpoints import sso

pytestmark = [pytest.mark.unit]


def _account():
    return SimpleNamespace(
        id=uuid.uuid4(),
        email="sso@example.com",
        username="sso",
        full_name=None,
        role=UserRole.VIEWER,
        is_superuser=False,
        is_active=True,
        hashed_password="a-password-of-its-own",
        totp_enabled=True,
    )


@pytest.fixture
def two_step_on(monkeypatch):
    """Every account has two-step sign-in on and every role requires it, and
    each look at the service is recorded."""
    looked = []

    def is_enabled(db, user_id):
        looked.append(user_id)
        return True

    monkeypatch.setattr(two_factor_module.two_factor_service, "is_enabled", is_enabled)
    monkeypatch.setattr(
        settings,
        "LOCAL_AUTH_2FA_REQUIRED_ROLES",
        ["ADMIN", "DEVELOPER", "ANALYST", "VIEWER"],
    )
    monkeypatch.setattr(sso, "_record_sso_login", lambda db, user: None)
    assert two_factor_required(_account()) is True
    return looked


def test_an_oidc_sign_in_issues_the_token_to_a_two_step_account_without_a_code(
    two_step_on, monkeypatch
):
    account = _account()
    monkeypatch.setattr(
        sso.sso_service, "provision_user", lambda db, info, config: account
    )

    result = sso._oidc_sign_in(MagicMock(), {"email": account.email}, None, "okta")

    assert decode_local_token(result.access_token)["sub"] == str(account.id)
    assert not hasattr(result, "mfa_required")
    assert two_step_on == []


def test_the_dashboards_sso_exchange_issues_the_token_without_a_code(two_step_on):
    account = _account()
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = account

    result = sso._exchange_session(db, account.id)

    assert decode_local_token(result.access_token)["sub"] == str(account.id)
    assert two_step_on == []


def test_the_sso_module_reads_no_two_step_state():
    source = inspect.getsource(sso)
    assert "two_factor" not in source
    assert "totp" not in source.lower()
    assert "mfa" not in source.lower()
