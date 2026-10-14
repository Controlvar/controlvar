"""Under ``AUTH_PROVIDER=cognito``: what sign-in needs, and what it refuses.

* Every Cognito call the platform makes is one AWS authorises by the user's
  own token or password, not by IAM (botocore's model marks each one
  ``noAuth``), so the API needs no IAM permission to sign anyone in. The
  groups come from the access token's ``cognito:groups`` claim, read after
  ``GetUser`` accepts the token; no group API is called.
* The dashboard's sign-in form posts to ``/api/v1/auth/login``, which under
  Cognito answers 404, "Endpoint not available".
* A token the platform issues itself -- the one a password sign-in and an SSO
  sign-in both end with (``create_local_access_token``) -- signs nobody in
  under Cognito: the token goes to Cognito, which does not know it.
"""

from __future__ import annotations

import ast
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import botocore.session
import jwt
import pytest
from botocore import xform_name
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from backend.app.api import deps
from backend.app.core.config import settings
from backend.app.core.security import create_local_access_token
from backend.app.main import app
from backend.app.models.user import User, UserRole
from backend.app.services import auth_service as auth_module
from backend.app.services.auth_service import CognitoAuthService

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
POOL = "us-west-2_AbC123xyZ"
CLIENT = "3n4b5urk1ft4fl3mg5e62d9ado"
ISSUER = f"https://cognito-idp.us-west-2.amazonaws.com/{POOL}"
SIGN_IN_FORM = REPO_ROOT / "frontend" / "src" / "contexts" / "AuthContext.tsx"


class _Cognito:
    """Answers GetUser like Cognito; any other call fails the test."""

    def __init__(self):
        self.calls: list[str] = []

    def get_user(self, AccessToken):
        self.calls.append("get_user")
        return {
            "Username": "ops",
            "UserAttributes": [{"Name": "sub", "Value": "s-1"}],
        }

    def __getattr__(self, name):
        raise AssertionError(f"the sign-in called Cognito's {name}")


def _service() -> tuple[CognitoAuthService, _Cognito]:
    service = CognitoAuthService()
    service.user_pool_id, service.client_id = POOL, CLIENT
    fake = _Cognito()
    service.client = fake
    return service, fake


def _token(**claims) -> str:
    base = {
        "sub": "s-1",
        "iss": ISSUER,
        "client_id": CLIENT,
        "token_use": "access",
        "username": "ops",
    }
    base.update(claims)
    return jwt.encode(base, "not-cognitos-key", algorithm="HS256")


def test_every_cognito_call_the_platform_makes_needs_no_iam_permission():
    """Every name in the auth service that is a Cognito operation -- called as
    a method on anything (the client, or a name it was assigned to), or given
    as a string (``getattr``) -- is one AWS authorises without IAM."""
    model = botocore.session.get_session().get_service_model("cognito-idp")
    operations = {xform_name(name): name for name in model.operation_names}
    tree = ast.parse(Path(auth_module.__file__).read_text())
    named = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    } | {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    called = sorted(named & set(operations))
    assert {"get_user", "initiate_auth"} <= set(called), called
    for name in called:
        operation = model.operation_model(operations[name])
        assert operation.auth_type == "none", (name, operation.auth_type)
    # No other module talks to Cognito.
    for root in ("backend/app", "modules/backend/app"):
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            if path.resolve() == Path(auth_module.__file__).resolve():
                continue
            assert '"cognito-idp"' not in path.read_text(), path


@pytest.mark.parametrize(
    ("claim", "groups"),
    [(["Developers", "Analysts"], ["Developers", "Analysts"]), (None, [])],
)
def test_the_groups_are_the_access_tokens_claim(claim, groups):
    service, fake = _service()
    claims = {} if claim is None else {"cognito:groups": claim}
    found = service.get_user_with_groups(_token(**claims))
    assert found["groups"] == groups
    assert fake.calls == ["get_user"]


def test_under_cognito_the_dashboards_sign_in_endpoint_is_not_available(monkeypatch):
    form = SIGN_IN_FORM.read_text()
    (path,) = re.findall(r"const LOGIN_PATH = '([^']+)';", form)
    assert path == "/api/v1/auth/login"
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "cognito")
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            path, json={"email": "someone@example.com", "password": "a-password-1"}
        )
    assert response.status_code == 404
    assert (
        response.json()["detail"]
        == "Endpoint not available: AUTH_PROVIDER is not 'local'"
    )


class _OneUser:
    """A session whose every user lookup finds ``user``: the account the token
    names exists, so only the provider can refuse the token."""

    def __init__(self, user):
        self.user = user

    def query(self, *args, **kwargs):
        return self

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return self.user

    def rollback(self):
        pass

    def close(self):
        pass


def test_under_cognito_a_token_the_platform_issued_signs_nobody_in(monkeypatch):
    user = User(
        id=uuid.uuid4(),
        username="sso-user",
        email="sso@example.com",
        hashed_password="x",
        is_active=True,
        is_superuser=False,
        role=UserRole.DEVELOPER,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    token = create_local_access_token(user)
    sent: list[str] = []

    def cognito_get_user(AccessToken):
        sent.append(AccessToken)
        raise ClientError(
            {
                "Error": {
                    "Code": "NotAuthorizedException",
                    "Message": "Invalid Access Token",
                }
            },
            "GetUser",
        )

    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    service, _ = _service()
    service.client = SimpleNamespace(get_user=cognito_get_user)
    monkeypatch.setattr(deps, "auth_service", service)
    app.dependency_overrides[deps.get_db] = lambda: _OneUser(user)
    headers = {"Authorization": f"Bearer {token}"}
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            # Control: under local the token signs this account in.
            monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
            local = client.get("/api/v1/users/me", headers=headers)
            monkeypatch.setattr(settings, "AUTH_PROVIDER", "cognito")
            response = client.get("/api/v1/users/me", headers=headers)
    finally:
        app.dependency_overrides.pop(deps.get_db, None)
    assert local.status_code == 200, local.text
    assert local.json()["email"] == "sso@example.com"
    assert response.status_code == 401
    assert response.json() == {"detail": deps._credentials_exception().detail}
    # It was Cognito that was asked, and it was asked about this token.
    assert sent == [token]
