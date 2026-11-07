"""Dashboard sign-in through the Cognito Hosted UI: the code exchange and sign-out (#708).

``POST /api/v1/auth/cognito/exchange`` takes the code the Hosted UI sent to
the dashboard's callback, with its PKCE verifier and redirect URI, and asks
Cognito's token endpoint for the tokens. It answers the access token and its
lifetime, nothing else. The domain it asks and the app client it names come
from the configuration (``COGNITO_DOMAIN``, ``COGNITO_CLIENT_ID``), never
from the request.

``POST /api/v1/auth/logout`` under ``cognito`` checks the bearer as every
request does, then asks Cognito to sign the user out everywhere
(GlobalSignOut): the access token stops working at once. A Cognito failure
there still answers 204 and is logged.

The real routes on Postgres. Cognito is moto's emulator (the user, its tokens,
GetUser, GlobalSignOut); the Hosted UI's token endpoint is
``cognito_hosted_ui_fake.FakeIssuer``, a local HTTP server the relay reaches
over a real socket. No AWS call is made: moto intercepts every boto3 call and
the credential chain is blanked as a second line.

The first test is the positive control: a valid exchange signs the user in,
and the later tests change one thing each.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any, Dict, Iterator, NamedTuple, Optional, Tuple

import boto3
import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient
from moto import mock_cognitoidp
from sqlalchemy import func

from backend.app.api import deps
from backend.app.core.config import settings
from backend.app.db.session import get_db as session_get_db
from backend.app.main import app
from backend.app.models.user import User
from backend.app.services import auth_service as auth_service_module
from backend.app.services import cognito_hosted_ui
from backend.tests.integration.cognito_hosted_ui_fake import (
    FakeIssuer,
    json_answer,
    new_verifier,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

RELAY = "/api/v1/auth/cognito/exchange"
LOGOUT = "/api/v1/auth/logout"
ME = "/api/v1/users/me"
REGION = "us-east-1"
DOMAIN = "controlvar-test.auth.us-east-1.amazoncognito.com"
REDIRECT = "https://dashboard.example.com/auth/callback"
PASSWORD = "Perm-pass1!"
SIGN_IN_LOGGER = "backend.app.auth.cognito_sign_in"

#: Spelled out, not imported: a change to a message has to change this test.
COGNITO_ONLY = (
    "Endpoint not available: AUTH_PROVIDER is not 'cognito'. "
    "Sign in with POST /api/v1/auth/login; "
    "an administrator creates accounts and resets passwords."
)
REFUSED = "Cognito did not accept the sign-in code. Start the sign-in again."
FAILED = "Cognito did not complete the sign-in. Try again."
TIMED_OUT = "Cognito did not answer in time. Try again."
NOT_SET_UP = (
    "Sign-in through the Cognito Hosted UI is not set up on this server: "
    "COGNITO_DOMAIN, COGNITO_CLIENT_ID and COGNITO_USER_POOL_ID must all be set."
)
UNAUTHORISED = {"detail": "Could not validate credentials"}


class HostedUI(NamedTuple):
    client: TestClient
    idp: Any
    pool: str
    app_client: str
    issuer: FakeIssuer


@pytest.fixture
def hosted_ui(db_session, monkeypatch) -> Iterator[HostedUI]:
    """Cognito mode, a moto user pool, and the fake token endpoint."""
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
    monkeypatch.setattr(settings, "COGNITO_DOMAIN", DOMAIN)
    monkeypatch.setattr(settings, "COGNITO_TOKEN_TIMEOUT_SECONDS", 5.0)

    def override_get_db():
        yield db_session

    saved = dict(app.dependency_overrides)
    app.dependency_overrides[deps.get_db] = override_get_db
    app.dependency_overrides[session_get_db] = override_get_db
    try:
        with mock_cognitoidp(), FakeIssuer() as issuer:
            idp = boto3.client("cognito-idp", region_name=REGION)
            pool = idp.create_user_pool(PoolName="hosted-ui")["UserPool"]["Id"]
            app_client = _app_client(idp, pool)
            # The sign-in dependency uses the module's singleton, built at
            # import; the auth routes build a service per request from the
            # environment. Configure both.
            monkeypatch.setattr(deps.auth_service, "user_pool_id", pool)
            monkeypatch.setattr(deps.auth_service, "client_id", app_client)
            monkeypatch.setattr(deps.auth_service, "_client", idp)
            monkeypatch.setenv("COGNITO_USER_POOL_ID", pool)
            monkeypatch.setenv("COGNITO_CLIENT_ID", app_client)
            monkeypatch.setattr(cognito_hosted_ui, "transport", issuer.transport())
            yield HostedUI(
                TestClient(app, raise_server_exceptions=False),
                idp,
                pool,
                app_client,
                issuer,
            )
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(saved)


def _app_client(idp, pool: str) -> str:
    return idp.create_user_pool_client(
        UserPoolId=pool,
        ClientName=f"dashboard-{uuid.uuid4().hex[:6]}",
        ExplicitAuthFlows=[
            "ALLOW_ADMIN_USER_PASSWORD_AUTH",
            "ALLOW_USER_PASSWORD_AUTH",
            "ALLOW_REFRESH_TOKEN_AUTH",
        ],
    )["UserPoolClient"]["ClientId"]


def _create_user(idp, pool: str, username: str, *, permanent: bool = True) -> None:
    idp.admin_create_user(
        UserPoolId=pool,
        Username=username,
        UserAttributes=[
            {"Name": "email", "Value": f"{username}@example.com"},
            {"Name": "email_verified", "Value": "true"},
        ],
        TemporaryPassword="Tmp-pass1!",
        MessageAction="SUPPRESS",
    )
    if permanent:
        idp.admin_set_user_password(
            UserPoolId=pool, Username=username, Password=PASSWORD, Permanent=True
        )
    try:
        idp.create_group(GroupName="Developers", UserPoolId=pool)
    except idp.exceptions.GroupExistsException:
        pass
    idp.admin_add_user_to_group(
        UserPoolId=pool, Username=username, GroupName="Developers"
    )


def _tokens(hu: HostedUI, username: str, app_client: Optional[str] = None):
    """What the Hosted UI holds once ``username`` has signed in there."""
    return hu.idp.admin_initiate_auth(
        UserPoolId=hu.pool,
        ClientId=app_client or hu.app_client,
        AuthFlow="ADMIN_USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": username, "PASSWORD": PASSWORD},
    )["AuthenticationResult"]


def _signed_in_at_hosted_ui(hu: HostedUI) -> Tuple[str, str, str, Dict[str, Any]]:
    """A new user who has signed in on the Hosted UI: (username, code,
    verifier, tokens), the code being what Cognito sends to the callback."""
    username = f"hui{uuid.uuid4().hex[:8]}"
    _create_user(hu.idp, hu.pool, username)
    tokens = _tokens(hu, username)
    verifier = new_verifier()
    code = hu.issuer.issue_code(
        tokens, verifier=verifier, client_id=hu.app_client, redirect_uri=REDIRECT
    )
    return username, code, verifier, tokens


def _exchange(hu: HostedUI, code: str, verifier: str, **extra: Any):
    headers = extra.pop("headers", None)
    body = {"code": code, "code_verifier": verifier, "redirect_uri": REDIRECT}
    body.update(extra)
    return hu.client.post(RELAY, json=body, headers=headers)


def _bearer(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _rows_named(db_session, username: str) -> int:
    db_session.expire_all()
    return (
        db_session.query(func.count(User.id)).filter(User.username == username).scalar()
    )


# --- the positive control -----------------------------------------------------


@pytest.mark.regression
def test_a_valid_exchange_signs_the_user_in(hosted_ui, db_session, caplog):
    """The control every tamper below flips: Cognito's code becomes a session.

    The answer is the access token and its lifetime and nothing else; the
    token endpoint was asked once, on the configured domain, for the
    configured app client, with the code, the verifier and the redirect URI
    as the dashboard sent them; the id and refresh tokens are neither
    answered nor logged; the access token then signs the user in, and the
    first request with it creates the account.
    """
    hu = hosted_ui
    username, code, verifier, tokens = _signed_in_at_hosted_ui(hu)
    assert _rows_named(db_session, username) == 0

    with caplog.at_level(logging.DEBUG):
        response = _exchange(hu, code, verifier)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "access_token": tokens["AccessToken"],
        "expires_in": tokens["ExpiresIn"],
    }
    assert hu.issuer.requests == [
        {
            "host": DOMAIN,
            "path": "/oauth2/token",
            "content_type": "application/x-www-form-urlencoded",
            "form": {
                "grant_type": "authorization_code",
                "client_id": hu.app_client,
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": REDIRECT,
            },
        }
    ]
    assert hu.issuer.answers == [200]
    for secret in (tokens["IdToken"], tokens["RefreshToken"]):
        assert secret not in response.text
        assert secret not in caplog.text
    assert tokens["AccessToken"] not in caplog.text

    me = hu.client.get(ME, headers=_bearer(response.json()["access_token"]))
    assert me.status_code == 200, me.text
    assert me.json()["username"] == username
    assert me.json()["role"] == "DEVELOPER"
    assert me.json()["is_superuser"] is False
    assert _rows_named(db_session, username) == 1


@pytest.mark.regression
def test_a_user_whose_hosted_ui_sign_in_set_a_new_password_signs_in(
    hosted_ui, db_session
):
    """A user Cognito sends through a step (here: set a new password) cannot
    sign in to the dashboard with the password form, which answers the step
    with 401. The Hosted UI takes the step; its code then signs the user in
    through the relay. On the old code the relay does not exist (404)."""
    hu = hosted_ui
    username = f"hui{uuid.uuid4().hex[:8]}"
    _create_user(hu.idp, hu.pool, username, permanent=False)

    form = hu.client.post(
        "/api/v1/auth/token", data={"username": username, "password": "Tmp-pass1!"}
    )
    assert form.status_code == 401, form.text

    challenge = hu.idp.admin_initiate_auth(
        UserPoolId=hu.pool,
        ClientId=hu.app_client,
        AuthFlow="ADMIN_USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": username, "PASSWORD": "Tmp-pass1!"},
    )
    assert challenge["ChallengeName"] == "NEW_PASSWORD_REQUIRED"
    tokens = hu.idp.admin_respond_to_auth_challenge(
        UserPoolId=hu.pool,
        ClientId=hu.app_client,
        ChallengeName="NEW_PASSWORD_REQUIRED",
        Session=challenge["Session"],
        ChallengeResponses={"USERNAME": username, "NEW_PASSWORD": PASSWORD},
    )["AuthenticationResult"]
    verifier = new_verifier()
    code = hu.issuer.issue_code(
        tokens, verifier=verifier, client_id=hu.app_client, redirect_uri=REDIRECT
    )

    response = _exchange(hu, code, verifier)
    assert response.status_code == 200, response.text
    me = hu.client.get(ME, headers=_bearer(response.json()["access_token"]))
    assert me.status_code == 200, me.text
    assert me.json()["username"] == username
    assert _rows_named(db_session, username) == 1


# --- R1: Cognito only ---------------------------------------------------------


def test_under_local_the_exchange_answers_404_and_asks_no_one(hosted_ui, monkeypatch):
    """Everything else is configured, so only the provider check refuses."""
    hu = hosted_ui
    _, code, verifier, _ = _signed_in_at_hosted_ui(hu)
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")

    response = _exchange(hu, code, verifier)

    assert response.status_code == 404, response.text
    assert response.json() == {"detail": COGNITO_ONLY}
    assert hu.issuer.requests == []


# --- R3: the issuer and the client come from the configuration ---------------


@pytest.mark.regression
def test_the_domain_and_client_are_never_taken_from_the_request(hosted_ui):
    hu = hosted_ui
    _, code, verifier, _ = _signed_in_at_hosted_ui(hu)
    elsewhere = "elsewhere.example"

    response = _exchange(
        hu,
        code,
        verifier,
        domain=elsewhere,
        hosted_ui_domain=elsewhere,
        client_id="elsewhere-client",
        headers={
            "Host": elsewhere,
            "X-Forwarded-Host": elsewhere,
            "X-Cognito-Domain": elsewhere,
        },
    )

    assert response.status_code == 200, response.text
    assert len(hu.issuer.requests) == 1
    seen = hu.issuer.requests[0]
    assert seen["host"] == DOMAIN
    assert seen["form"]["client_id"] == hu.app_client
    assert "elsewhere" not in repr(seen)


def test_without_the_hosted_ui_settings_the_exchange_asks_no_one(
    hosted_ui, monkeypatch
):
    hu = hosted_ui
    _, code, verifier, _ = _signed_in_at_hosted_ui(hu)
    for unset in ("COGNITO_DOMAIN", "COGNITO_CLIENT_ID", "COGNITO_USER_POOL_ID"):
        with monkeypatch.context() as m:
            if unset == "COGNITO_DOMAIN":
                m.setattr(settings, "COGNITO_DOMAIN", None)
            else:
                m.delenv(unset)
            response = _exchange(hu, code, verifier)
        assert response.status_code == 503, (unset, response.text)
        assert response.json() == {"detail": NOT_SET_UP}, unset
    assert hu.issuer.requests == []


# --- R4: a bounded wait ---------------------------------------------------------


@pytest.mark.regression
def test_an_issuer_that_does_not_answer_in_time_is_a_504(hosted_ui, monkeypatch):
    """The issuer holds its (valid) answer back for 3 s and the relay waits
    0.3 s: the bound is the test's own, so a relay with no timeout gets the
    valid answer and fails the status, rather than hanging."""
    hu = hosted_ui
    _, code, verifier, _ = _signed_in_at_hosted_ui(hu)
    monkeypatch.setattr(settings, "COGNITO_TOKEN_TIMEOUT_SECONDS", 0.3)
    hu.issuer.delay = 3.0

    response = _exchange(hu, code, verifier)

    assert response.status_code == 504, response.text
    assert response.json() == {"detail": TIMED_OUT}


def test_an_issuer_that_cannot_be_reached_is_a_502(hosted_ui, monkeypatch):
    hu = hosted_ui
    _, code, verifier, _ = _signed_in_at_hosted_ui(hu)
    monkeypatch.setattr(
        cognito_hosted_ui, "transport", FakeIssuer.unreachable_transport()
    )

    response = _exchange(hu, code, verifier)

    assert response.status_code == 502, response.text
    assert response.json() == {"detail": FAILED}


# --- R7 and the refusals: fixed answers, nothing of Cognito's repeated -------

#: Stands for anything Cognito writes; none of it may reach the answer or the log.
MARKER = "cognito-wrote-this-7f3a9c"


@pytest.mark.regression
@pytest.mark.parametrize(
    "answer, status, detail",
    [
        (
            json_answer(400, {"error": "invalid_grant", "error_description": MARKER}),
            400,
            REFUSED,
        ),
        (
            json_answer(401, {"error": "invalid_client", "error_description": MARKER}),
            400,
            REFUSED,
        ),
        ((400, MARKER.encode(), "text/plain"), 400, REFUSED),
        ((500, f"<html>{MARKER}</html>".encode(), "text/html"), 502, FAILED),
        ((200, f"<html>{MARKER}</html>".encode(), "text/html"), 502, FAILED),
        (json_answer(200, {"id_token": MARKER, "expires_in": 3600}), 502, FAILED),
        (json_answer(200, {"access_token": "", "expires_in": 3600}), 502, FAILED),
        (json_answer(302, {"error": MARKER}), 502, FAILED),
    ],
    ids=[
        "invalid-grant",
        "invalid-client",
        "plain-text-400",
        "server-error",
        "not-json",
        "no-access-token",
        "empty-access-token",
        "redirect",
    ],
)
def test_what_cognito_says_is_not_repeated(hosted_ui, caplog, answer, status, detail):
    hu = hosted_ui
    _, code, verifier, _ = _signed_in_at_hosted_ui(hu)
    hu.issuer.answer = answer

    with caplog.at_level(logging.DEBUG):
        response = _exchange(hu, code, verifier)

    assert response.status_code == status, response.text
    assert response.json() == {"detail": detail}
    assert MARKER not in response.text
    assert MARKER not in caplog.text
    assert len(hu.issuer.requests) == 1


def test_a_refused_exchange_records_why_on_the_sign_in_logger(hosted_ui, caplog):
    """Positive control for the log assertions above: the refusal is logged,
    with Cognito's error code and nothing else of its answer."""
    hu = hosted_ui
    _, code, verifier, _ = _signed_in_at_hosted_ui(hu)
    hu.issuer.answer = json_answer(
        400, {"error": "invalid_grant", "error_description": MARKER}
    )

    with caplog.at_level(logging.WARNING, logger=SIGN_IN_LOGGER):
        _exchange(hu, code, verifier)

    reasons = [
        (getattr(r, "reason", None), getattr(r, "cognito_error", None))
        for r in caplog.records
        if r.name == SIGN_IN_LOGGER
    ]
    assert reasons == [("code_exchange_refused", "invalid_grant")]


# --- row 3: the verifier is the dashboard's, and a code is good once ----------


def test_a_verifier_that_does_not_match_the_challenge_is_refused(hosted_ui):
    hu = hosted_ui
    _, code, _, _ = _signed_in_at_hosted_ui(hu)

    response = _exchange(hu, code, new_verifier())

    assert response.status_code == 400, response.text
    assert response.json() == {"detail": REFUSED}
    assert hu.issuer.answers == [400]


def test_a_code_is_exchanged_once(hosted_ui):
    hu = hosted_ui
    _, code, verifier, _ = _signed_in_at_hosted_ui(hu)

    first = _exchange(hu, code, verifier)
    second = _exchange(hu, code, verifier)

    assert first.status_code == 200, first.text
    assert second.status_code == 400, second.text
    assert second.json() == {"detail": REFUSED}


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"code_verifier": "v" * 43, "redirect_uri": REDIRECT},
        {"code": "abc", "redirect_uri": REDIRECT},
        {"code": "abc", "code_verifier": "v" * 43},
        {"code": "abc", "code_verifier": "short", "redirect_uri": REDIRECT},
        {"code": "abc", "code_verifier": "v" * 129, "redirect_uri": REDIRECT},
        {"code": "abc", "code_verifier": "v" * 42 + "/", "redirect_uri": REDIRECT},
        {"code": "", "code_verifier": "v" * 43, "redirect_uri": REDIRECT},
        {"code": "abc", "code_verifier": "v" * 43, "redirect_uri": ""},
    ],
    ids=[
        "empty",
        "no-code",
        "no-verifier",
        "no-redirect-uri",
        "short-verifier",
        "long-verifier",
        "verifier-outside-the-alphabet",
        "empty-code",
        "empty-redirect-uri",
    ],
)
def test_an_incomplete_body_is_a_422_and_asks_no_one(hosted_ui, body):
    hu = hosted_ui
    response = hu.client.post(RELAY, json=body)
    assert response.status_code == 422, response.text
    assert hu.issuer.requests == []


# --- S1-S3: sign-out under Cognito ----------------------------------------------


@pytest.mark.regression
def test_signing_out_revokes_the_token_at_cognito(hosted_ui):
    hu = hosted_ui
    _, code, verifier, tokens = _signed_in_at_hosted_ui(hu)
    token = _exchange(hu, code, verifier).json()["access_token"]
    assert hu.client.get(ME, headers=_bearer(token)).status_code == 200

    response = hu.client.post(LOGOUT, headers=_bearer(token))

    assert response.status_code == 204, response.text
    assert response.content == b""
    after = hu.client.get(ME, headers=_bearer(token))
    assert after.status_code == 401, after.text
    assert after.json() == UNAUTHORISED
    with pytest.raises(ClientError) as refused:
        hu.idp.get_user(AccessToken=token)
    assert refused.value.response["Error"]["Code"] == "NotAuthorizedException"
    with pytest.raises(ClientError):
        hu.idp.initiate_auth(
            ClientId=hu.app_client,
            AuthFlow="REFRESH_TOKEN_AUTH",
            AuthParameters={"REFRESH_TOKEN": tokens["RefreshToken"]},
        )


@pytest.mark.regression
def test_a_failed_global_sign_out_still_answers_204_and_is_logged(
    hosted_ui, monkeypatch, caplog
):
    hu = hosted_ui
    _, code, verifier, _ = _signed_in_at_hosted_ui(hu)
    token = _exchange(hu, code, verifier).json()["access_token"]
    calls = []

    def failing(self, access_token):
        calls.append(access_token)
        raise ClientError(
            {"Error": {"Code": "InternalErrorException", "Message": MARKER}},
            "GlobalSignOut",
        )

    monkeypatch.setattr(
        auth_service_module.CognitoAuthService, "global_sign_out", failing
    )

    # INFO, as an API runs: at DEBUG botocore itself records the GetUser
    # request, token included, as it does on every request under Cognito.
    with caplog.at_level(logging.INFO):
        response = hu.client.post(LOGOUT, headers=_bearer(token))

    assert response.status_code == 204, response.text
    assert calls == [token]
    failures = [r for r in caplog.records if "GlobalSignOut" in r.getMessage()]
    assert len(failures) == 1
    assert failures[0].levelno == logging.ERROR
    assert "InternalErrorException" in failures[0].getMessage()
    assert MARKER not in caplog.text
    assert token not in caplog.text


def test_signing_out_under_cognito_needs_a_token(hosted_ui, monkeypatch):
    hu = hosted_ui
    calls = []
    monkeypatch.setattr(
        auth_service_module.CognitoAuthService,
        "global_sign_out",
        lambda self, access_token: calls.append(access_token),
    )

    response = hu.client.post(LOGOUT)

    assert response.status_code == 401, response.text
    assert response.json() == {"detail": "Not authenticated"}
    assert calls == []


def test_a_token_from_another_user_pool_is_refused_and_not_signed_out(
    hosted_ui, caplog
):
    """GetUser accepts the token (moto knows it), the issuer check refuses it,
    and the user is not signed out of the pool it came from."""
    hu = hosted_ui
    other = hu.idp.create_user_pool(PoolName="other")["UserPool"]["Id"]
    other_client = _app_client(hu.idp, other)
    username = f"oth{uuid.uuid4().hex[:8]}"
    _create_user(hu.idp, other, username)
    token = hu.idp.admin_initiate_auth(
        UserPoolId=other,
        ClientId=other_client,
        AuthFlow="ADMIN_USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": username, "PASSWORD": PASSWORD},
    )["AuthenticationResult"]["AccessToken"]

    with caplog.at_level(logging.WARNING, logger=SIGN_IN_LOGGER):
        response = hu.client.post(LOGOUT, headers=_bearer(token))

    assert response.status_code == 401, response.text
    assert response.json() == UNAUTHORISED
    reasons = [
        getattr(r, "reason", None) for r in caplog.records if r.name == SIGN_IN_LOGGER
    ]
    assert reasons == ["wrong_issuer"]
    assert hu.idp.get_user(AccessToken=token)["Username"] == username


def test_a_token_cognito_does_not_accept_is_refused(hosted_ui, caplog):
    hu = hosted_ui
    with caplog.at_level(logging.DEBUG):
        response = hu.client.post(LOGOUT, headers=_bearer("not-a-cognito-token"))
    assert response.status_code == 401, response.text
    assert response.json() == UNAUTHORISED
    assert "not-a-cognito-token" not in response.text


def test_under_local_signing_out_asks_cognito_nothing(hosted_ui, monkeypatch):
    """S3: local tokens are stateless; the route still answers 204 with or
    without a bearer and never builds a Cognito service."""
    hu = hosted_ui
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    built = []
    monkeypatch.setattr(
        auth_service_module.CognitoAuthService,
        "global_sign_out",
        lambda self, access_token: built.append(access_token),
    )

    assert hu.client.post(LOGOUT).status_code == 204
    assert hu.client.post(LOGOUT, headers=_bearer("anything")).status_code == 204
    assert built == []
