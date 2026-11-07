"""``GET /api/v1/auth/config``: how the dashboard signs in (#708).

The dashboard reads this before it draws the sign-in page. The route takes no
credentials and answers 200 under both providers:

* ``local``, or the development sign-in bypass: ``{"provider": "local"}``;
* ``cognito`` with ``COGNITO_DOMAIN``, ``COGNITO_CLIENT_ID`` and
  ``COGNITO_USER_POOL_ID`` all set: the provider, the Hosted UI's domain, the
  app client id and the scopes to ask for, and nothing else -- no user pool
  id, no region, nothing taken from the request's ``Host`` or forwarding
  headers;
* ``cognito`` with any of the three unset: ``configured: false`` and the
  names of the unset settings, never a value.

It is ``x-stability: beta``, as is the code-exchange relay beside it.

No database, no Cognito call: the low-level class every boto3 client is
created through records any client asked for, and the AWS chain is blanked.
"""

from __future__ import annotations

from typing import Any, List, Tuple
from unittest.mock import MagicMock

import boto3
import pytest
from fastapi.testclient import TestClient

from backend.app.core.config import settings
from backend.app.main import app

pytestmark = [pytest.mark.unit]

CONFIG = "/api/v1/auth/config"
RELAY = "/api/v1/auth/cognito/exchange"
DOMAIN = "controlvar-test.auth.eu-west-2.amazoncognito.com"
POOL = "eu-west-2_AbCdEf123"
CLIENT = "4l1ent1dexample0000000000"
SCOPES = ["openid", "email", "profile", "aws.cognito.signin.user.admin"]
KEYS = ("COGNITO_DOMAIN", "COGNITO_CLIENT_ID", "COGNITO_USER_POOL_ID")

LOW_LEVEL_SESSION = type(boto3.session.Session()._session)


@pytest.fixture
def clients(monkeypatch) -> List[Tuple[Any, ...]]:
    for name in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", "/dev/null")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/dev/null")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    made: List[Tuple[Any, ...]] = []

    def record(self, *args: Any, **kwargs: Any) -> MagicMock:
        made.append(args)
        return MagicMock()

    monkeypatch.setattr(LOW_LEVEL_SESSION, "create_client", record)
    return made


@pytest.fixture
def cognito(monkeypatch, clients):
    """``cognito`` with all three settings, the dev bypass off."""
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "cognito")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    monkeypatch.setattr(settings, "COGNITO_DOMAIN", DOMAIN)
    monkeypatch.setenv("COGNITO_USER_POOL_ID", POOL)
    monkeypatch.setenv("COGNITO_CLIENT_ID", CLIENT)
    monkeypatch.setenv("AWS_REGION", "eu-west-2")


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _unset(monkeypatch, key: str) -> None:
    if key == "COGNITO_DOMAIN":
        monkeypatch.setattr(settings, "COGNITO_DOMAIN", None)
    else:
        monkeypatch.delenv(key, raising=False)


@pytest.mark.regression
def test_under_local_it_says_local_and_nothing_else(client, clients, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "COGNITO_DOMAIN", DOMAIN)
    monkeypatch.setenv("COGNITO_CLIENT_ID", CLIENT)

    response = client.get(CONFIG)

    assert response.status_code == 200, response.text
    assert response.json() == {"provider": "local"}
    assert clients == []


def test_with_the_dev_bypass_it_says_local(client, cognito, monkeypatch):
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", True)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")

    response = client.get(CONFIG)

    assert response.status_code == 200, response.text
    assert response.json() == {"provider": "local"}


@pytest.mark.regression
def test_under_cognito_it_gives_the_hosted_ui_and_nothing_else(
    client, cognito, clients
):
    response = client.get(CONFIG)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "provider": "cognito",
        "hosted_ui_domain": DOMAIN,
        "client_id": CLIENT,
        "scopes": SCOPES,
    }
    assert POOL not in response.text
    assert "eu-west-2_" not in response.text
    assert clients == []


@pytest.mark.regression
@pytest.mark.parametrize("missing", KEYS)
def test_under_cognito_an_unset_setting_is_named_and_nothing_is_echoed(
    client, cognito, monkeypatch, missing
):
    _unset(monkeypatch, missing)

    response = client.get(CONFIG)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "provider": "cognito",
        "configured": False,
        "missing": [missing],
    }
    for value in (DOMAIN, POOL, CLIENT):
        assert value not in response.text


def test_under_cognito_with_nothing_set_all_three_are_named(
    client, cognito, monkeypatch
):
    for key in KEYS:
        _unset(monkeypatch, key)

    response = client.get(CONFIG)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "provider": "cognito",
        "configured": False,
        "missing": list(KEYS),
    }


@pytest.mark.regression
@pytest.mark.parametrize(
    "header",
    ["Host", "X-Forwarded-Host", "X-Forwarded-Proto", "Origin", "Referer"],
)
@pytest.mark.parametrize("provider", ["local", "cognito"])
def test_nothing_in_the_answer_comes_from_the_request(
    client, cognito, monkeypatch, header, provider
):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", provider)
    response = client.get(CONFIG, headers={header: "evil.example"})
    assert response.status_code == 200, response.text
    assert "evil" not in response.text


@pytest.mark.regression
@pytest.mark.parametrize("provider", ["local", "cognito"])
@pytest.mark.parametrize(
    "authorization", [None, "Bearer not-a-token"], ids=["anonymous", "junk-bearer"]
)
def test_it_needs_no_credentials_under_either_provider(
    client, cognito, monkeypatch, provider, authorization
):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", provider)
    headers = {"Authorization": authorization} if authorization else None
    response = client.get(CONFIG, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["provider"] == provider


def test_it_answers_get_only(client, cognito):
    assert client.post(CONFIG).status_code == 405


def test_both_new_routes_are_beta_and_the_config_takes_no_credentials():
    paths = app.openapi()["paths"]
    config = paths[CONFIG]["get"]
    relay = paths[RELAY]["post"]
    assert config["x-stability"] == "beta"
    assert relay["x-stability"] == "beta"
    assert "security" not in config
    assert "security" not in relay
    assert set(paths[CONFIG]) == {"get"}
    assert set(paths[RELAY]) == {"post"}
