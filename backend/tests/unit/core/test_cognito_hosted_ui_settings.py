"""``COGNITO_DOMAIN`` and ``COGNITO_TOKEN_TIMEOUT_SECONDS`` (#708).

``COGNITO_DOMAIN`` is the Cognito Hosted UI's domain name, alone: the
dashboard sends the browser to ``https://<COGNITO_DOMAIN>/oauth2/authorize``
and the API asks ``https://<COGNITO_DOMAIN>/oauth2/token``. A value that is
not a domain name would send every sign-in to a page that does not exist, and
nothing else would report it, so it refuses to build the settings and the API
does not start:

* accepted: lower-case letters, digits, dots and hyphens, 3 to 253 of them,
  with at least one dot and no empty label (a Cognito prefix domain such as
  ``<prefix>.auth.<region>.amazoncognito.com``, or a custom domain);
* refused: a scheme, a path or a slash, a space, an upper-case letter, a bare
  prefix with no dot (what the user pool domain's own reference gives), an
  empty label (``.x.y``, ``x..y``, ``x.y.``); the error names the setting and
  never repeats the value;
* unset, empty or blank: ``None``. A missing value never stops the API, since
  every Cognito deployment from before this setting has none.

``COGNITO_TOKEN_TIMEOUT_SECONDS`` bounds the wait for Cognito's token
endpoint: 10 by default, more than 0 and at most 60.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.app.core.config import Settings, TestSettings

pytestmark = [pytest.mark.unit]

NAME = "COGNITO_DOMAIN"
TIMEOUT = "COGNITO_TOKEN_TIMEOUT_SECONDS"


@pytest.fixture(autouse=True)
def _unset(monkeypatch):
    monkeypatch.delenv(NAME, raising=False)
    monkeypatch.delenv(TIMEOUT, raising=False)


def _build(**kwargs) -> TestSettings:
    return TestSettings(_env_file=None, **kwargs)


def test_both_are_on_the_base_settings_with_their_defaults():
    assert Settings.model_fields[NAME].default is None
    assert Settings.model_fields[TIMEOUT].default == 10
    built = _build()
    assert built.COGNITO_DOMAIN is None
    assert built.COGNITO_TOKEN_TIMEOUT_SECONDS == 10


@pytest.mark.parametrize(
    "value",
    [
        "controlvar-prod.auth.us-east-1.amazoncognito.com",
        "my-app-123.auth.eu-west-2.amazoncognito.com",
        "auth.example.com",
        "a.b",
    ],
)
def test_a_domain_name_is_accepted(value, monkeypatch):
    assert _build(COGNITO_DOMAIN=value).COGNITO_DOMAIN == value
    monkeypatch.setenv(NAME, value)
    assert _build().COGNITO_DOMAIN == value


@pytest.mark.regression
@pytest.mark.parametrize(
    "value",
    [
        "https://controlvar.auth.us-east-1.amazoncognito.com",
        "https://x",
        "controlvar.auth.us-east-1.amazoncognito.com/",
        "x/",
        "x y",
        "controlvar auth.example.com",
        "controlvar-prod",
        "Controlvar.auth.us-east-1.amazoncognito.com",
        ".auth.example.com",
        "auth..example.com",
        "auth.example.com.",
        "a.",
        "ab",
        "a" * 250 + ".com",
        "auth.example.com:443",
        "auth_example.com",
    ],
    ids=[
        "https-url",
        "scheme-only",
        "trailing-slash",
        "short-with-slash",
        "space",
        "space-in-a-name",
        "bare-prefix",
        "upper-case",
        "leading-dot",
        "empty-label",
        "trailing-dot",
        "dot-at-the-end",
        "too-short",
        "too-long",
        "port",
        "underscore",
    ],
)
def test_anything_else_stops_the_settings_and_the_value_is_not_repeated(
    value, monkeypatch
):
    with pytest.raises(ValidationError) as refused:
        _build(COGNITO_DOMAIN=value)
    message = str(refused.value)
    assert NAME in message
    assert value not in message

    monkeypatch.setenv(NAME, value)
    with pytest.raises(ValidationError):
        _build()


@pytest.mark.parametrize("value", ["", " ", "\t"])
def test_an_empty_value_is_unset_and_does_not_stop_the_api(value, monkeypatch):
    assert _build(COGNITO_DOMAIN=value).COGNITO_DOMAIN is None
    monkeypatch.setenv(NAME, value)
    assert _build().COGNITO_DOMAIN is None


@pytest.mark.parametrize("value", ["0.3", "1", "60"])
def test_a_timeout_in_range_is_accepted(value, monkeypatch):
    monkeypatch.setenv(TIMEOUT, value)
    assert _build().COGNITO_TOKEN_TIMEOUT_SECONDS == float(value)


@pytest.mark.parametrize("value", ["0", "-1", "60.5", "inf", "nan", "soon"])
def test_a_timeout_out_of_range_is_refused(value, monkeypatch):
    monkeypatch.setenv(TIMEOUT, value)
    with pytest.raises(ValidationError) as refused:
        _build()
    assert TIMEOUT in str(refused.value)
