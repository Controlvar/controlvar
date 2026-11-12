"""Demo mode's settings: off by default, and the configurations that refuse to start.

``DEMO_MODE`` (bool, default false), ``DEMO_ACCOUNT_EMAIL`` and
``DEMO_ACCOUNT_PASSWORD`` (default ""). The API refuses to start when:

* one of the account pair is set without the other;
* the account is set while ``DEMO_MODE`` is not true -- the settings ignore an
  unknown name, so this is what turns a misspelt ``DEMO_MODE`` into a refusal;
* ``DEMO_MODE`` is true, or the account is set, with ``AUTH_PROVIDER`` other
  than ``local`` (the allow-list holds only the local sign-in routes);
* the account's address is ``FIRST_SUPERUSER``, whatever its letter case;
* the address has no ``@`` or is over 254 characters, or the password is over
  256 (a client offers the one-click sign-in only for an account of that
  shape);
* ``DEMO_MODE`` is true and any setting or variable that calls a service
  outside the install is set (each one is a case below, named literally here
  rather than read from the module, so a name dropped from the module's list
  fails its own case).

Each refusal names the setting and never its value.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.app.core.config import ProdSettings, Settings

pytestmark = [pytest.mark.unit]

ACCOUNT = {
    "DEMO_ACCOUNT_EMAIL": "viewer@demo.com",
    "DEMO_ACCOUNT_PASSWORD": "Demo1234!",
}
PROD_SECRET = "p" * 64
SECRET_VALUE = "value-that-must-not-be-echoed"

#: Every setting that lets the API call a service outside the install, with a
#: value that turns it on.
OUTBOUND_SETTINGS = {
    "NOTIFICATION_WEBHOOK_URL": "https://hooks.example.com/demo",
    "SLACK_ENABLED": True,
    "SLACK_BOT_TOKEN": SECRET_VALUE,
    "EMAIL_ENABLED": True,
    "SENDGRID_API_KEY": SECRET_VALUE,
    "SMTP_HOST": "smtp.example.com",
    "SMTP_USERNAME": "mailer",
    "SMTP_PASSWORD": SECRET_VALUE,
}

#: The variables the LLM providers read from the process environment.
OUTBOUND_VARIABLES = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
)


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch):
    # TESTING: the suite exports it, and Settings refuses it with production,
    # which `_box` builds. The rest: a developer's shell must not decide these.
    for name in (
        "TESTING",
        "DEMO_MODE",
        "DEMOMODE",
        "DEMO_ACCOUNT_EMAIL",
        "DEMO_ACCOUNT_PASSWORD",
        "AUTH_PROVIDER",
        "FIRST_SUPERUSER",
        "PUBLIC_BASE_URL",
        "ALLOWED_HOSTS",
        *OUTBOUND_SETTINGS,
        *OUTBOUND_VARIABLES,
    ):
        monkeypatch.delenv(name, raising=False)


def _settings(**kwargs) -> Settings:
    return Settings(_env_file=None, **kwargs)


def _box(**kwargs) -> ProdSettings:
    """Production settings with demo mode on, as a public demo install sets them."""
    values = {
        "ENVIRONMENT": "production",
        "SECRET_KEY": PROD_SECRET,
        "FIRST_SUPERUSER_PASSWORD": "Str0ng-first-admin",
        "PUBLIC_BASE_URL": "https://demo.example.com",
        "DEMO_MODE": True,
        **ACCOUNT,
    }
    values.update(kwargs)
    return ProdSettings(_env_file=None, **values)


def _refusal(build) -> str:
    with pytest.raises(ValidationError) as caught:
        build()
    return str(caught.value)


# --- the switch --------------------------------------------------------------


def test_demo_mode_is_off_by_default():
    assert Settings.model_fields["DEMO_MODE"].default is False
    built = _settings()
    assert built.DEMO_MODE is False
    assert built.DEMO_ACCOUNT_EMAIL == ""
    assert built.DEMO_ACCOUNT_PASSWORD == ""


def test_demo_mode_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    assert _settings().DEMO_MODE is True


@pytest.mark.parametrize("value", ["enabled", "maybe", "2"])
def test_a_switch_value_that_is_not_a_boolean_refuses_to_start(monkeypatch, value):
    monkeypatch.setenv("DEMO_MODE", value)
    assert "DEMO_MODE" in _refusal(_settings)


def test_a_misspelt_switch_alone_is_ignored(monkeypatch):
    """What the account rule exists for: an unknown name is dropped silently."""
    monkeypatch.setenv("DEMOMODE", "true")
    assert _settings().DEMO_MODE is False


def test_a_misspelt_switch_with_the_account_refuses_to_start(monkeypatch):
    monkeypatch.setenv("DEMOMODE", "true")
    for name, value in ACCOUNT.items():
        monkeypatch.setenv(name, value)
    assert "DEMO_MODE is not true" in _refusal(_settings)


# --- the account ---------------------------------------------------------------


def test_a_public_demo_configuration_starts():
    built = _box()
    assert built.DEMO_MODE is True
    assert built.DEMO_ACCOUNT_EMAIL == "viewer@demo.com"
    assert built.DEMO_ACCOUNT_PASSWORD == "Demo1234!"


def test_demo_mode_without_an_account_starts():
    assert _settings(DEMO_MODE=True).DEMO_MODE is True


@pytest.mark.parametrize("name", sorted(ACCOUNT))
def test_half_of_the_account_refuses_to_start(name):
    message = _refusal(lambda: _settings(DEMO_MODE=True, **{name: ACCOUNT[name]}))
    assert "set together or not at all" in message


def test_the_account_without_demo_mode_refuses_to_start():
    assert "DEMO_MODE is not true" in _refusal(lambda: _settings(**ACCOUNT))


def test_demo_mode_with_another_sign_in_provider_refuses_to_start():
    """Without an account too: no sign-in route but the local ones is allowed."""
    message = _refusal(lambda: _settings(DEMO_MODE=True, AUTH_PROVIDER="cognito"))
    assert "DEMO_MODE=true needs AUTH_PROVIDER=local" in message


def test_another_sign_in_provider_without_demo_mode_starts():
    assert _settings(AUTH_PROVIDER="cognito").AUTH_PROVIDER == "cognito"


def test_the_account_with_another_sign_in_provider_refuses_to_start():
    message = _refusal(
        lambda: _settings(DEMO_MODE=True, AUTH_PROVIDER="cognito", **ACCOUNT)
    )
    assert "AUTH_PROVIDER=local" in message


@pytest.mark.parametrize(
    "address", ["admin@example.com", "Admin@Example.COM", "  ADMIN@example.com "]
)
def test_the_account_may_not_be_the_first_superuser(address):
    message = _refusal(
        lambda: _settings(
            DEMO_MODE=True,
            FIRST_SUPERUSER="admin@example.com",
            DEMO_ACCOUNT_EMAIL=address,
            DEMO_ACCOUNT_PASSWORD="Demo1234!",
        )
    )
    assert "FIRST_SUPERUSER" in message


@pytest.mark.parametrize(
    "address",
    ["viewer.demo.com", "viewer", "v" * 244 + "@demo.com.x"],
    ids=["no-at", "bare-name", "255-characters"],
)
def test_an_account_address_a_client_would_not_accept_refuses_to_start(address):
    message = _refusal(lambda: _box(DEMO_ACCOUNT_EMAIL=address))
    assert "DEMO_ACCOUNT_EMAIL must be an e-mail address" in message


def test_a_254_character_address_starts():
    address = "v" * 245 + "@demo.com"
    assert len(address) == 254
    assert _box(DEMO_ACCOUNT_EMAIL=address).DEMO_ACCOUNT_EMAIL == address


def test_a_password_over_256_characters_refuses_to_start():
    message = _refusal(lambda: _box(DEMO_ACCOUNT_PASSWORD="p" * 257))
    assert "DEMO_ACCOUNT_PASSWORD must be 1 to 256 characters" in message


@pytest.mark.parametrize("length", [1, 256])
def test_a_password_of_1_to_256_characters_starts(length):
    assert len(_box(DEMO_ACCOUNT_PASSWORD="p" * length).DEMO_ACCOUNT_PASSWORD) == length


def test_an_empty_password_with_an_address_refuses_to_start():
    """The 1-character floor: an empty password is half of the pair."""
    message = _refusal(lambda: _box(DEMO_ACCOUNT_PASSWORD=""))
    assert "set together or not at all" in message


def test_the_account_is_published_only_in_demo_mode_with_both_set():
    assert _box().demo_account_published is True
    assert _settings(DEMO_MODE=True).demo_account_published is False
    assert _settings().demo_account_published is False


def test_the_refusal_names_the_setting_and_not_its_password():
    message = _refusal(lambda: _settings(DEMO_ACCOUNT_PASSWORD=SECRET_VALUE))
    assert "DEMO_ACCOUNT_PASSWORD" in message
    assert SECRET_VALUE not in message


# --- outbound integrations -------------------------------------------------------


@pytest.mark.parametrize("name", sorted(OUTBOUND_SETTINGS))
def test_each_outbound_setting_refuses_to_start_in_demo_mode(name):
    message = _refusal(lambda: _box(**{name: OUTBOUND_SETTINGS[name]}))
    assert name in message, f"{name} accepted under DEMO_MODE"
    assert SECRET_VALUE not in message


@pytest.mark.parametrize("name", OUTBOUND_VARIABLES)
def test_each_outbound_variable_refuses_to_start_in_demo_mode(monkeypatch, name):
    monkeypatch.setenv(name, SECRET_VALUE)
    message = _refusal(_box)
    assert name in message, f"{name}={SECRET_VALUE} accepted under DEMO_MODE"
    assert SECRET_VALUE not in message


def test_empty_outbound_values_start(monkeypatch):
    """What a compose file passes when nothing is configured: ``${X:-}``."""
    for name in OUTBOUND_VARIABLES:
        monkeypatch.setenv(name, "")
    empty = {
        name: (False if isinstance(value, bool) else "")
        for name, value in OUTBOUND_SETTINGS.items()
    }
    assert _box(**empty).DEMO_MODE is True


def test_outbound_settings_are_unaffected_when_demo_mode_is_off(monkeypatch):
    for name in OUTBOUND_VARIABLES:
        monkeypatch.setenv(name, SECRET_VALUE)
    built = _settings(**OUTBOUND_SETTINGS)
    assert built.DEMO_MODE is False
    assert built.SMTP_HOST == "smtp.example.com"
