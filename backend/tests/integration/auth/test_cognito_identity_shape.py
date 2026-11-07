"""What ``CognitoAuthService.get_user_with_groups`` returns, through moto.

The account lookup keys on ``attributes["sub"]``, refuses an identity with no
``attributes["email"]``, and takes the role from ``groups``. The tests that
stub the service build their payload with
``backend.tests.integration.cognito_identity.identity``; this pins that helper
to the shape the real service returns, so a stub cannot drift from it. No
database: this package runs without one.
"""

from __future__ import annotations

import os
import uuid

import boto3
import pytest
from moto import mock_cognitoidp

from backend.app.services.auth_service import CognitoAuthService
from backend.tests.integration.cognito_identity import identity

pytestmark = [pytest.mark.integration]

REGION = "us-east-1"
PASSWORD = "Perm-pass1!"


@pytest.fixture
def pool(monkeypatch):
    for name in ("AWS_PROFILE", "AWS_SESSION_TOKEN", "AWS_DEFAULT_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    with mock_cognitoidp():
        idp = boto3.client("cognito-idp", region_name=REGION)
        pool_id = idp.create_user_pool(PoolName="shape")["UserPool"]["Id"]
        app_client = idp.create_user_pool_client(
            UserPoolId=pool_id,
            ClientName="app",
            ExplicitAuthFlows=["ALLOW_ADMIN_USER_PASSWORD_AUTH"],
        )["UserPoolClient"]["ClientId"]
        idp.create_group(GroupName="Developers", UserPoolId=pool_id)
        service = CognitoAuthService()
        service.user_pool_id = pool_id
        service.client_id = app_client
        service.client = idp
        yield idp, pool_id, app_client, service


def _user(idp, pool_id, attributes, groups=()):
    username = f"shape{uuid.uuid4().hex[:8]}"
    idp.admin_create_user(
        UserPoolId=pool_id,
        Username=username,
        UserAttributes=attributes,
        TemporaryPassword="Tmp-pass1!",
        MessageAction="SUPPRESS",
    )
    idp.admin_set_user_password(
        UserPoolId=pool_id, Username=username, Password=PASSWORD, Permanent=True
    )
    for group in groups:
        idp.admin_add_user_to_group(
            UserPoolId=pool_id, Username=username, GroupName=group
        )
    sub = next(
        a["Value"]
        for a in idp.admin_get_user(UserPoolId=pool_id, Username=username)[
            "UserAttributes"
        ]
        if a["Name"] == "sub"
    )
    return username, sub


def _token(idp, pool_id, app_client, username):
    return idp.admin_initiate_auth(
        UserPoolId=pool_id,
        ClientId=app_client,
        AuthFlow="ADMIN_USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": username, "PASSWORD": PASSWORD},
    )["AuthenticationResult"]["AccessToken"]


def test_a_user_with_an_email_and_a_group_has_the_helper_shape(pool):
    idp, pool_id, app_client, service = pool
    username, sub = _user(
        idp,
        pool_id,
        [
            {"Name": "email", "Value": "shape@example.com"},
            {"Name": "email_verified", "Value": "true"},
        ],
        ["Developers"],
    )

    real = service.get_user_with_groups(_token(idp, pool_id, app_client, username))

    assert real == identity(username, sub, "shape@example.com", ["Developers"])


def test_a_user_whose_email_is_not_verified_has_the_helper_shape(pool):
    idp, pool_id, app_client, service = pool
    username, sub = _user(idp, pool_id, [{"Name": "email", "Value": "u@example.com"}])

    real = service.get_user_with_groups(_token(idp, pool_id, app_client, username))

    assert "email_verified" not in real["attributes"]
    assert real == identity(username, sub, "u@example.com", email_verified=None)


def test_a_user_with_no_email_and_no_group_has_neither(pool):
    """Cognito leaves out the attribute (not ``""``), and an access token
    for a user in no group carries no ``cognito:groups`` claim."""
    idp, pool_id, app_client, service = pool
    username, sub = _user(idp, pool_id, [])

    real = service.get_user_with_groups(_token(idp, pool_id, app_client, username))

    assert "email" not in real["attributes"]
    assert real == identity(username, sub)
