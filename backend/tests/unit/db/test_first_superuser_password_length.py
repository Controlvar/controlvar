"""The first administrator's password must fit bcrypt's 72-byte limit.

bcrypt raises ValueError on a password over 72 bytes, so the bootstrap refuses
such a FIRST_SUPERUSER_PASSWORD with one line instead. The check sits only
where the first administrator is created, never on the settings: a running
deployment whose users table is populated ignores the setting.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.app.core.config import Settings
from backend.app.core.settings_rules import (
    LONG_FIRST_SUPERUSER_PASSWORD_MESSAGE,
    UNENCODABLE_FIRST_SUPERUSER_PASSWORD_MESSAGE,
    first_superuser_password_length_refusal,
)
from backend.app.db.bootstrap import first_superuser_refusal

_OVER_THE_LIMIT = "Aa1-" + "x" * 69


def _settings(password: str, environment: str, explicit: bool):
    return SimpleNamespace(
        FIRST_SUPERUSER_PASSWORD=password,
        ENVIRONMENT=environment,
        environment_explicit=explicit,
    )


@pytest.mark.unit
@pytest.mark.regression
@pytest.mark.parametrize(
    "password, refusal",
    [
        ("Aa1-" + "x" * 68, None),  # 72 bytes
        (_OVER_THE_LIMIT, LONG_FIRST_SUPERUSER_PASSWORD_MESSAGE),  # 73 bytes
        ("é" * 36, None),  # 72 bytes in 36 characters
        ("é" * 37, LONG_FIRST_SUPERUSER_PASSWORD_MESSAGE),  # 74 bytes, 37 characters
        ("Strong-Passw0rd-\ud800", UNENCODABLE_FIRST_SUPERUSER_PASSWORD_MESSAGE),
    ],
)
def test_the_limit_is_72_bytes_of_utf8(password, refusal):
    assert first_superuser_password_length_refusal(password) == refusal


@pytest.mark.unit
@pytest.mark.regression
@pytest.mark.parametrize(
    "environment, explicit",
    [
        ("production", True),
        ("development", True),
        ("test", True),
        ("development", False),
    ],
)
def test_the_bootstrap_refuses_a_long_password_in_every_environment(
    environment, explicit
):
    """Unlike a weak password, a long one cannot be hashed anywhere."""
    refusal = first_superuser_refusal(_settings(_OVER_THE_LIMIT, environment, explicit))
    assert refusal == LONG_FIRST_SUPERUSER_PASSWORD_MESSAGE


@pytest.mark.unit
def test_the_messages_state_the_limit_and_repeat_no_value():
    for message in (
        LONG_FIRST_SUPERUSER_PASSWORD_MESSAGE,
        UNENCODABLE_FIRST_SUPERUSER_PASSWORD_MESSAGE,
    ):
        assert "at most 72 bytes when encoded as UTF-8" in message
        assert "\n" not in message
        assert "{" not in message


@pytest.mark.unit
@pytest.mark.regression
def test_the_settings_still_accept_a_long_password_in_production(monkeypatch):
    """The limit is not a setting rule: a production deployment with a
    populated users table and a long value must keep loading its settings."""
    for key in ("APP_ENV", "TESTING"):
        monkeypatch.delenv(key, raising=False)
    settings = Settings(
        _env_file=None,
        ENVIRONMENT="production",
        SECRET_KEY="k" * 48,
        PUBLIC_BASE_URL="https://controlvar.example.com",
        FIRST_SUPERUSER_PASSWORD=_OVER_THE_LIMIT,
    )
    assert settings.FIRST_SUPERUSER_PASSWORD == _OVER_THE_LIMIT
