"""``COGNITO_SELF_SIGNUP_ENABLED``: where it lives, its default, how it parses (T94).

The setting turns on ``POST /api/v1/auth/signup`` and ``/confirm`` under
``AUTH_PROVIDER=cognito``.  It is pydantic's default ``bool`` parsing, with no
validator of its own, and these tests pin exactly what that accepts, because
``docs/cognito_integration.md`` ("Self sign-up") is written from it:

* P1 the field is on the base ``Settings`` (so every environment class has
  it), is a ``bool`` and defaults to ``False``;
* P2 the values read as true and as false, in any letter case;
* P3 an empty or unrecognised value refuses to build the settings, and the
  error names the field without repeating the value;
* P4 the name is case-sensitive: a lower-case variable is ignored.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.app.core.config import DevSettings, ProdSettings, Settings, TestSettings

pytestmark = [pytest.mark.unit, pytest.mark.regression]

NAME = "COGNITO_SELF_SIGNUP_ENABLED"

PROD_REQUIRED = {
    "SECRET_KEY": "a" * 64,
    "FIRST_SUPERUSER_PASSWORD": "StrongProd1!",
    "PUBLIC_BASE_URL": "https://controlvar.example.com",
}


@pytest.fixture(autouse=True)
def _unset(monkeypatch):
    monkeypatch.delenv(NAME, raising=False)
    monkeypatch.delenv(NAME.lower(), raising=False)


def _build(**kwargs) -> TestSettings:
    return TestSettings(_env_file=None, **kwargs)


def test_the_field_is_on_the_base_settings_and_off_by_default(monkeypatch):
    """P1."""
    # ProdSettings refuses TESTING, which the test runner sets.
    monkeypatch.delenv("TESTING", raising=False)
    field = Settings.model_fields[NAME]
    assert field.annotation is bool
    assert field.default is False
    for cls in (DevSettings, TestSettings, ProdSettings):
        assert cls.model_fields[NAME].default is False, cls.__name__
    assert _build().COGNITO_SELF_SIGNUP_ENABLED is False
    prod = ProdSettings(_env_file=None, **PROD_REQUIRED)
    assert prod.COGNITO_SELF_SIGNUP_ENABLED is False


@pytest.mark.parametrize(
    "raw",
    ["true", "TRUE", "True", "1", "yes", "YES", "on", "On", "t", "y"],
)
def test_values_read_as_true(raw, monkeypatch):
    """P2."""
    monkeypatch.setenv(NAME, raw)
    assert _build().COGNITO_SELF_SIGNUP_ENABLED is True


@pytest.mark.parametrize(
    "raw",
    ["false", "FALSE", "False", "0", "no", "NO", "off", "Off", "f", "n"],
)
def test_values_read_as_false(raw, monkeypatch):
    """P2."""
    monkeypatch.setenv(NAME, raw)
    assert _build().COGNITO_SELF_SIGNUP_ENABLED is False


@pytest.mark.parametrize(
    "raw",
    ["", " true", "true ", "ture", "2", "enabled", "garbage"],
    ids=[
        "empty",
        "leading-space",
        "trailing-space",
        "typo",
        "two",
        "enabled",
        "garbage",
    ],
)
def test_other_values_refuse_to_start(raw, monkeypatch):
    """P3: a templated empty value stops the API from starting."""
    monkeypatch.setenv(NAME, raw)
    with pytest.raises(ValidationError) as excinfo:
        _build()
    errors = excinfo.value.errors()
    assert [e["loc"] for e in errors] == [(NAME,)]
    assert errors[0]["type"] == "bool_parsing"
    message = str(excinfo.value)
    assert NAME in message
    assert "input_value" not in message  # hide_input_in_errors


def test_a_lower_case_name_is_ignored(monkeypatch):
    """P4: the docs spell the name in upper case because of this."""
    monkeypatch.setenv(NAME.lower(), "true")
    assert _build().COGNITO_SELF_SIGNUP_ENABLED is False
