"""The documented Cognito onboarding ends in an account on the platform (T94).

``docs/cognito_integration.md`` "Adding a user" ends with the user calling
``POST /api/v1/auth/token`` and then ``GET /api/v1/users/me``, which creates
their account.  This runs the page's own commands (read from the page, as
boto3 calls) against a moto pool shaped like the reference one, signs in
through ``/token`` and calls ``/users/me`` against real PostgreSQL: the
account exists, linked to the Cognito user, with the role of the group the
procedure added the user to.

Nothing reaches AWS: moto intercepts every call and the credential chain is
blanked as well.
"""

from __future__ import annotations

import os
import uuid
from typing import Iterator

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws
from sqlalchemy.orm import Session

from backend.app.api import deps
from backend.app.core.config import settings
from backend.app.db.session import get_db as session_get_db
from backend.app.main import app
from backend.app.models.user import User, UserRole
from backend.tests.integration.cognito_reference_pool import (
    REGION,
    create_reference_like_pool,
    run_documented_onboarding,
)

pytestmark = [pytest.mark.integration, pytest.mark.regression]


@pytest.fixture
def onboarding(db_session: Session, monkeypatch) -> Iterator[tuple]:
    for name in ("AWS_PROFILE", "AWS_SESSION_TOKEN", "AWS_DEFAULT_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "cognito")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    monkeypatch.setattr(settings, "SYNC_ROLES_ON_LOGIN", True)

    def override_get_db():
        yield db_session

    saved = dict(app.dependency_overrides)
    app.dependency_overrides[deps.get_db] = override_get_db
    app.dependency_overrides[session_get_db] = override_get_db
    try:
        with mock_aws():
            idp = boto3.client("cognito-idp", region_name=REGION)
            pool_id, client_id = create_reference_like_pool(idp)
            # /token builds its service from the environment; /users/me goes
            # through the shared service in deps.
            monkeypatch.setenv("COGNITO_USER_POOL_ID", pool_id)
            monkeypatch.setenv("COGNITO_CLIENT_ID", client_id)
            monkeypatch.setattr(deps.auth_service, "user_pool_id", pool_id)
            monkeypatch.setattr(deps.auth_service, "client_id", client_id)
            monkeypatch.setattr(deps.auth_service, "_client", idp)
            yield TestClient(app, raise_server_exceptions=False), idp, pool_id
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(saved)


def test_the_documented_onboarding_creates_the_account(onboarding, db_session):
    client, idp, pool_id = onboarding

    step = run_documented_onboarding(idp, pool_id)
    token = client.post(
        "/api/v1/auth/token",
        data={"username": step["Username"], "password": step["Password"]},
    )
    assert token.status_code == 200, token.text

    me = client.get(
        "/api/v1/users/me",
        headers={"Authorization": f"Bearer {token.json()['access_token']}"},
    )

    assert me.status_code == 200, me.text
    body = me.json()
    assert body["username"] == step["Username"]
    assert body["email"] == "jane.doe@example.com"
    # The response carries the role of the group the procedure added the
    # user to, not null (#644).
    assert body["role"] == "DEVELOPER"
    record = idp.admin_get_user(UserPoolId=pool_id, Username=step["Username"])
    sub = next(a["Value"] for a in record["UserAttributes"] if a["Name"] == "sub")
    account = db_session.get(User, uuid.UUID(body["id"]))
    assert account.external_id == f"cognito:{sub}"
    assert account.role == UserRole.DEVELOPER
    assert account.hashed_password is None
