"""
Fixtures for Cognito integration tests.

Uses moto to mock AWS Cognito — no real AWS calls made.
The moto mock intercepts boto3 calls at the botocore level and returns
realistic responses. Because CognitoAuthService creates a new boto3 client
on __init__, and the auth endpoints instantiate a fresh CognitoAuthService
per request, we must ensure moto is active for the entire module scope.

Environment variables are set before app import so that configuration is
picked up at module load time.

This package is the only place in the test suite that runs the Cognito
provider: the Cognito env vars used to live in the root conftest, but the
default is ``AUTH_PROVIDER=local`` (with the dev-admin
bypass enabled for the rest of the suite), so everything Cognito-specific is
scoped here.  ``_cognito_provider`` below flips the settings singleton to the
Cognito provider with the bypass off for every test in this directory.
"""

import os
from unittest.mock import patch

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

# Set env vars BEFORE importing app (moto requires this order).
# CognitoAuthService reads COGNITO_* from os.environ on every instantiation.
os.environ.setdefault("COGNITO_USER_POOL_ID", "us-east-1_TestPool")
os.environ.setdefault("COGNITO_CLIENT_ID", "test-client-id")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("TESTING", "true")
os.environ.setdefault("APP_ENV", "test")


@pytest.fixture(autouse=True)
def _cognito_provider(monkeypatch):
    """Run every test in this package against the Cognito provider, fail-closed."""
    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "AUTH_PROVIDER", "cognito")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    yield


@pytest.fixture(scope="module")
def cognito_mock():
    """Start moto Cognito mock for the entire module."""
    with mock_aws():
        yield


@pytest.fixture(scope="module")
def cognito_resources(cognito_mock):
    """
    Create a real (mocked) Cognito User Pool and App Client.
    Returns a dict with user_pool_id, client_id, and boto_client for use
    in patching CognitoAuthService instances.
    """
    client = boto3.client("cognito-idp", region_name="us-east-1")

    # Create User Pool
    pool = client.create_user_pool(
        PoolName="test-experimentation-pool",
        Policies={
            "PasswordPolicy": {
                "MinimumLength": 8,
                "RequireUppercase": True,
                "RequireLowercase": True,
                "RequireNumbers": True,
                "RequireSymbols": True,
            }
        },
        AutoVerifiedAttributes=["email"],
    )
    user_pool_id = pool["UserPool"]["Id"]

    # Create App Client (no secret for easier testing)
    app_client = client.create_user_pool_client(
        UserPoolId=user_pool_id,
        ClientName="test-client",
        ExplicitAuthFlows=[
            "ALLOW_USER_PASSWORD_AUTH",
            "ALLOW_REFRESH_TOKEN_AUTH",
            "ALLOW_USER_SRP_AUTH",
        ],
        GenerateSecret=False,
    )
    client_id = app_client["UserPoolClient"]["ClientId"]

    return {"user_pool_id": user_pool_id, "client_id": client_id, "boto_client": client}


@pytest.fixture(scope="module")
def auth_client(cognito_resources):
    """
    TestClient with environment patched to use the moto pool/client IDs.

    Because each auth endpoint creates `CognitoAuthService()` fresh on every
    request, and CognitoAuthService reads env vars in __init__, we patch the
    environment so every new instance picks up the moto pool details.

    The rate limiter is bypassed for tests by patching ``is_allowed`` on both
    limiter classes (Redis-backed and the in-memory fallback) to always return
    (True, 999), preventing 429 responses that would otherwise corrupt test
    assertions about status codes.  The middleware owns its limiter instance
    (there is no module-level singleton), so the classes are patched instead.
    """
    import backend.app.middleware.rate_limiter as rate_limiter_module
    from backend.app.main import app

    user_pool_id = cognito_resources["user_pool_id"]
    client_id = cognito_resources["client_id"]

    env_patch = {
        "COGNITO_USER_POOL_ID": user_pool_id,
        "COGNITO_CLIENT_ID": client_id,
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_REGION": "us-east-1",
    }

    allow_all = {"return_value": (True, 999)}
    with (
        patch.dict(os.environ, env_patch),
        patch.object(rate_limiter_module.RedisRateLimiter, "is_allowed", **allow_all),
        patch.object(
            rate_limiter_module.SlidingWindowRateLimiter, "is_allowed", **allow_all
        ),
    ):
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client


@pytest.fixture
def self_signup_enabled(monkeypatch):
    """Turn on ``COGNITO_SELF_SIGNUP_ENABLED`` for one test.

    Off by default for this package, as for a deployment: only the modules
    that exercise ``/signup`` and ``/confirm`` opt in, so every other test
    runs with the default.
    """
    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "COGNITO_SELF_SIGNUP_ENABLED", True)
    yield


@pytest.fixture
def registered_user(cognito_resources, auth_client):
    """
    A user in the moto pool with a permanent password (status CONFIRMED),
    created the way ``docs/cognito_integration.md`` "Adding a user" tells an
    administrator to: ``admin_create_user`` then ``admin_set_user_password``
    with ``Permanent=True``.  It does not go through ``/signup``, which is off
    by default.

    The pool is module-scoped, so a second test in the module finds the user
    already there: only ``UsernameExistsException`` is tolerated, the password
    is set every time, and the fixture asserts the user really is CONFIRMED.
    Returns the VALID_USER dict.
    """
    from backend.tests.integration.auth.spec_cognito_integration import VALID_USER

    boto_client = cognito_resources["boto_client"]
    user_pool_id = cognito_resources["user_pool_id"]
    username = VALID_USER["username"]

    try:
        boto_client.admin_create_user(
            UserPoolId=user_pool_id,
            Username=username,
            UserAttributes=[
                {"Name": "email", "Value": VALID_USER["email"]},
                {"Name": "email_verified", "Value": "true"},
                {"Name": "given_name", "Value": VALID_USER["given_name"]},
                {"Name": "family_name", "Value": VALID_USER["family_name"]},
            ],
            MessageAction="SUPPRESS",
        )
    except boto_client.exceptions.UsernameExistsException:
        pass  # created by an earlier test in this module
    boto_client.admin_set_user_password(
        UserPoolId=user_pool_id,
        Username=username,
        Password=VALID_USER["password"],
        Permanent=True,
    )
    status = boto_client.admin_get_user(UserPoolId=user_pool_id, Username=username)[
        "UserStatus"
    ]
    assert status == "CONFIRMED", status

    return VALID_USER


@pytest.fixture
def auth_tokens(cognito_resources, registered_user, auth_client):
    """
    Valid access + refresh tokens for registered_user.
    Obtained via the token endpoint with moto-backed Cognito.
    """
    response = auth_client.post(
        "/api/v1/auth/token",
        data={
            "username": registered_user["username"],
            "password": registered_user["password"],
        },
    )
    assert response.status_code == 200, f"Login failed: {response.text}"
    return response.json()
