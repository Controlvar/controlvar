"""Cognito sign-in accepts only access tokens issued to the configured user
pool and app client.

These drive the real ``CognitoAuthService`` -- GetUser, the claim check and
the group lookup -- against moto's Cognito emulator, with the users table in
real Postgres. Nothing here stubs ``get_user`` or ``get_user_with_groups``:
a stub would skip exactly the code under test.

A token issued to a user pool or app client other than the configured ones
is refused. Two user pools exist in every test: ``ours`` is the one this
deployment is configured with (``COGNITO_USER_POOL_ID`` /
``COGNITO_CLIENT_ID``), and ``other`` is not configured.

Every refusal asserts the exact reason on the stdlib record of the
``backend.app.auth.cognito_sign_in`` logger, so a token that GetUser rejected
(a plain 401 with no record) cannot pass as a ``wrong_issuer`` refusal.
"""

import logging
import os
import uuid
from typing import Iterator, NamedTuple

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws
from sqlalchemy import func

from backend.app.api import deps
from backend.app.core.config import settings
from backend.app.db.session import get_db as session_get_db
from backend.app.main import app
from backend.app.models.user import User

SIGN_IN_LOGGER = "backend.app.auth.cognito_sign_in"
REFUSED_BODY = {"detail": "Could not validate credentials"}
PASSWORD = "Perm-pass1!"
REGION = "us-east-1"

pytestmark = [pytest.mark.integration, pytest.mark.regression]


class Pools(NamedTuple):
    client: TestClient
    idp: object
    ours: str
    our_app_client: str
    other: str
    other_app_client: str


def _app_client(idp, pool_id: str) -> str:
    return idp.create_user_pool_client(
        UserPoolId=pool_id,
        ClientName=f"app-{uuid.uuid4().hex[:6]}",
        ExplicitAuthFlows=[
            "ALLOW_ADMIN_USER_PASSWORD_AUTH",
            "ALLOW_REFRESH_TOKEN_AUTH",
        ],
    )["UserPoolClient"]["ClientId"]


def _create_user(idp, pool_id: str, username: str, email: str, groups=()) -> None:
    idp.admin_create_user(
        UserPoolId=pool_id,
        Username=username,
        UserAttributes=[
            {"Name": "email", "Value": email},
            {"Name": "email_verified", "Value": "true"},
        ],
        TemporaryPassword="Tmp-pass1!",
        MessageAction="SUPPRESS",
    )
    idp.admin_set_user_password(
        UserPoolId=pool_id, Username=username, Password=PASSWORD, Permanent=True
    )
    for group in groups:
        try:
            idp.create_group(GroupName=group, UserPoolId=pool_id)
        except idp.exceptions.GroupExistsException:
            pass
        idp.admin_add_user_to_group(
            UserPoolId=pool_id, Username=username, GroupName=group
        )


def _access_token(idp, pool_id: str, app_client: str, username: str) -> str:
    return idp.admin_initiate_auth(
        UserPoolId=pool_id,
        ClientId=app_client,
        AuthFlow="ADMIN_USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": username, "PASSWORD": PASSWORD},
    )["AuthenticationResult"]["AccessToken"]


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _rows_named(db_session, username: str) -> int:
    db_session.expire_all()
    return (
        db_session.query(func.count(User.id)).filter(User.username == username).scalar()
    )


def _reasons(caplog) -> list:
    return [
        getattr(record, "reason", None)
        for record in caplog.records
        if record.name == SIGN_IN_LOGGER
    ]


def _assert_refused(response) -> None:
    assert response.status_code == 401, response.text
    assert response.json() == REFUSED_BODY
    assert response.headers.get("www-authenticate") == "Bearer"


@pytest.fixture
def pools(db_session, monkeypatch) -> Iterator[Pools]:
    """Cognito mode, two pools in moto, the deployment configured for ``ours``."""
    # A broken guard must never reach real AWS: moto intercepts every call,
    # and the credential chain is blanked as a second line.
    for name in ("AWS_PROFILE", "AWS_SESSION_TOKEN", "AWS_DEFAULT_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_REGION", REGION)

    monkeypatch.setattr(settings, "AUTH_PROVIDER", "cognito")

    def override_get_db():
        yield db_session

    saved = dict(app.dependency_overrides)
    app.dependency_overrides[deps.get_db] = override_get_db
    app.dependency_overrides[session_get_db] = override_get_db
    try:
        with mock_aws():
            idp = boto3.client("cognito-idp", region_name=REGION)
            ours = idp.create_user_pool(PoolName="ours")["UserPool"]["Id"]
            other = idp.create_user_pool(PoolName="other")["UserPool"]["Id"]
            our_app_client = _app_client(idp, ours)
            other_app_client = _app_client(idp, other)

            # The sign-in dependency uses the module's singleton, built at
            # import from the environment; /auth/me builds a fresh service per
            # request from the environment. Configure both.
            monkeypatch.setattr(deps.auth_service, "user_pool_id", ours)
            monkeypatch.setattr(deps.auth_service, "client_id", our_app_client)
            monkeypatch.setattr(deps.auth_service, "_client", idp)
            monkeypatch.setenv("COGNITO_USER_POOL_ID", ours)
            monkeypatch.setenv("COGNITO_CLIENT_ID", our_app_client)

            yield Pools(
                TestClient(app, raise_server_exceptions=False),
                idp,
                ours,
                our_app_client,
                other,
                other_app_client,
            )
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(saved)


def _token_from_the_unconfigured_pool(pools: Pools):
    """A username present in both pools (in the ``Admins`` group of ``ours``),
    and an access token for it issued by ``other``."""
    username = f"ops{uuid.uuid4().hex[:8]}"
    _create_user(pools.idp, pools.ours, username, f"{username}@ours.test", ["Admins"])
    _create_user(pools.idp, pools.other, username, f"{username}@other.test")
    return username, _access_token(
        pools.idp, pools.other, pools.other_app_client, username
    )


def test_the_refusal_record_is_captured(caplog):
    """Positive control: a record on the sign-in logger reaches caplog with
    its ``reason`` field, so an empty capture below means no record."""
    with caplog.at_level(logging.WARNING, logger=SIGN_IN_LOGGER):
        logging.getLogger(SIGN_IN_LOGGER).warning(
            "planted", extra={"reason": "planted"}
        )
    assert _reasons(caplog) == ["planted"]


def test_sign_in_with_a_token_not_from_the_configured_pool_is_refused(
    pools, db_session, caplog
):
    username, token = _token_from_the_unconfigured_pool(pools)

    with caplog.at_level(logging.WARNING, logger=SIGN_IN_LOGGER):
        response = pools.client.get("/api/v1/users/me", headers=_bearer(token))

    _assert_refused(response)
    assert _reasons(caplog) == ["wrong_issuer"]
    assert _rows_named(db_session, username) == 0


def test_auth_me_with_a_token_not_from_the_configured_pool_is_refused(pools, caplog):
    _, token = _token_from_the_unconfigured_pool(pools)

    with caplog.at_level(logging.WARNING, logger=SIGN_IN_LOGGER):
        response = pools.client.get("/api/v1/auth/me", headers=_bearer(token))

    _assert_refused(response)
    assert _reasons(caplog) == ["wrong_issuer"]


def test_sign_in_with_a_token_for_a_different_app_client_is_refused(
    pools, db_session, caplog
):
    username = f"dev{uuid.uuid4().hex[:8]}"
    _create_user(pools.idp, pools.ours, username, f"{username}@ours.test", ["Admins"])
    second_app_client = _app_client(pools.idp, pools.ours)
    token = _access_token(pools.idp, pools.ours, second_app_client, username)

    with caplog.at_level(logging.WARNING, logger=SIGN_IN_LOGGER):
        response = pools.client.get("/api/v1/users/me", headers=_bearer(token))

    _assert_refused(response)
    assert _reasons(caplog) == ["wrong_issuer"]
    assert _rows_named(db_session, username) == 0


def test_sign_in_without_a_configured_app_client_is_refused(
    pools, db_session, caplog, monkeypatch
):
    username = f"dev{uuid.uuid4().hex[:8]}"
    _create_user(pools.idp, pools.ours, username, f"{username}@ours.test")
    token = _access_token(pools.idp, pools.ours, pools.our_app_client, username)
    monkeypatch.setattr(deps.auth_service, "client_id", None)

    with caplog.at_level(logging.WARNING, logger=SIGN_IN_LOGGER):
        response = pools.client.get("/api/v1/users/me", headers=_bearer(token))

    _assert_refused(response)
    assert _reasons(caplog) == ["not_configured"]
    assert _rows_named(db_session, username) == 0


def test_auth_me_without_a_configured_app_client_is_refused(pools, caplog, monkeypatch):
    username = f"dev{uuid.uuid4().hex[:8]}"
    _create_user(pools.idp, pools.ours, username, f"{username}@ours.test")
    token = _access_token(pools.idp, pools.ours, pools.our_app_client, username)
    monkeypatch.delenv("COGNITO_CLIENT_ID")

    with caplog.at_level(logging.WARNING, logger=SIGN_IN_LOGGER):
        response = pools.client.get("/api/v1/auth/me", headers=_bearer(token))

    _assert_refused(response)
    assert _reasons(caplog) == ["not_configured"]


def test_a_token_from_the_configured_pool_and_app_client_signs_in(
    pools, db_session, caplog
):
    """Positive control: the same setup with our own pool and app client is
    accepted, on both routes, so the refusals above are not the harness."""
    username = f"ops{uuid.uuid4().hex[:8]}"
    _create_user(pools.idp, pools.ours, username, f"{username}@ours.test", ["Admins"])
    token = _access_token(pools.idp, pools.ours, pools.our_app_client, username)

    with caplog.at_level(logging.WARNING, logger=SIGN_IN_LOGGER):
        signed_in = pools.client.get("/api/v1/users/me", headers=_bearer(token))
        me = pools.client.get("/api/v1/auth/me", headers=_bearer(token))

    assert signed_in.status_code == 200, signed_in.text
    assert signed_in.json()["username"] == username
    assert me.status_code == 200, me.text
    assert me.json()["username"] == username
    assert _reasons(caplog) == []
    assert _rows_named(db_session, username) == 1
