"""Every SSO sign-in ends with a token the platform signs itself.

The OIDC callback and the dashboard's code exchange answer with
``create_local_access_token`` -- the token a password sign-in gets -- and the
SAML assertion consumer with ``_issue_jwt``; all are HS256 tokens signed with
``SECRET_KEY``, not Cognito's. So an SSO sign-in can work only where the API
accepts its own tokens (``AUTH_PROVIDER=local``):
``backend/tests/unit/services/test_cognito_mode_needs_no_iam.py`` shows such a
token refused under Cognito. And rotating ``SECRET_KEY`` invalidates every one
of them.
"""

from __future__ import annotations

import ast
import inspect
import uuid
from types import SimpleNamespace

import jwt
import pytest

from backend.app.core import security
from backend.app.core.config import settings
from modules.backend.app.api.v1.endpoints import sso

pytestmark = [pytest.mark.unit, pytest.mark.regression]


def _user():
    return SimpleNamespace(
        id=uuid.uuid4(), email="sso@example.com", username="sso", role=None
    )


def test_the_sso_routes_answer_with_the_platforms_own_token():
    assert sso.create_local_access_token is security.create_local_access_token
    tree = ast.parse(inspect.getsource(sso))
    issued = [
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in ("create_local_access_token", "_issue_jwt")
    ]
    assert sorted(issued) == [
        "_issue_jwt",
        "create_local_access_token",
        "create_local_access_token",
    ], issued


def test_an_oidc_sign_ins_token_is_refused_once_the_jwt_secret_changes(monkeypatch):
    monkeypatch.setattr(settings, "SECRET_KEY", "a" * 64)
    token = sso.create_local_access_token(_user())
    assert security.decode_local_token(token)["iss"] == security.LOCAL_TOKEN_ISSUER
    monkeypatch.setattr(settings, "SECRET_KEY", "b" * 64)
    with pytest.raises(security.InvalidTokenError):
        security.decode_local_token(token)


def test_a_saml_sign_ins_token_is_signed_with_the_jwt_secret(monkeypatch):
    monkeypatch.setattr(settings, "SECRET_KEY", "a" * 64)
    token = sso._issue_jwt(_user())
    assert jwt.get_unverified_header(token)["alg"] == "HS256"
    claims = jwt.decode(token, "a" * 64, algorithms=["HS256"])
    assert claims["email"] == "sso@example.com"
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(token, "b" * 64, algorithms=["HS256"])
