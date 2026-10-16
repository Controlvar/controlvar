"""ENVIRONMENT must be chosen, not defaulted, before the API serves.

An unset ENVIRONMENT still imports (the release's version check, the OpenAPI
dump and ``make bootstrap`` import the settings with nothing set), so the
settings only *record* whether it was set:

* ``Settings.environment_explicit`` -- the ENVIRONMENT variable, a dotenv
  entry or a constructor keyword; for the module-level ``settings``, the
  ENVIRONMENT or legacy APP_ENV variable;
* the app lifespan refuses to start serving when it is False;
* the bootstrap creates a first administrator with a weak password only when
  ENVIRONMENT was set to development or test (``first_superuser_refusal``).

Also here: a refused configuration does not echo the values it was given
(the modules' settings have the same case in
``modules/backend/tests/unit/test_modules_settings.py``).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.app.core.config import DevSettings, ProdSettings, TestSettings
from backend.app.core.settings_rules import (
    ENVIRONMENT_NOT_SET_MESSAGE,
    WEAK_FIRST_SUPERUSER_PASSWORD_MESSAGE,
)
from backend.app.db.bootstrap import first_superuser_refusal

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]

#: Not a placeholder, not a real key: a string no error message would contain
#: unless it echoed the input.
DISTINCTIVE = "zq7-distinctive-input-4411"


def _import_settings(extra: dict) -> str:
    """``environment_explicit`` of the module-level settings in a clean process."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "PYTHONPATH": str(REPO_ROOT),
        "AWS_CONFIG_FILE": "/dev/null",
        "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
    }
    env.update(extra)
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "from backend.app.core.config import settings as s;"
            "print(s.ENVIRONMENT, s.environment_explicit)",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    return proc.stdout.strip().splitlines()[-1]


@pytest.mark.regression
@pytest.mark.parametrize(
    "extra, expected",
    [
        ({}, "development False"),
        ({"ENVIRONMENT": ""}, "development False"),
        ({"ENVIRONMENT": "development"}, "development True"),
        ({"ENVIRONMENT": "test"}, "test True"),
        # The legacy variable is a choice too: the CDK task definitions and
        # the test runner set APP_ENV rather than ENVIRONMENT.
        ({"APP_ENV": "dev"}, "development True"),
        ({"APP_ENV": "test"}, "test True"),
    ],
)
def test_the_module_settings_record_whether_the_environment_was_set(extra, expected):
    assert _import_settings(extra) == expected


def test_a_class_default_is_not_explicit_and_a_keyword_or_variable_is(monkeypatch):
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    assert DevSettings().environment_explicit is False
    assert DevSettings(ENVIRONMENT="development").environment_explicit is True
    assert TestSettings(ENVIRONMENT="test").environment_explicit is True
    monkeypatch.setenv("ENVIRONMENT", "development")
    assert DevSettings().environment_explicit is True


# ---------------------------------------------------------------------------
# The lifespan refuses to serve
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_the_api_refuses_to_serve_when_the_environment_was_not_set(monkeypatch, caplog):
    from backend.app import main

    monkeypatch.setattr(main.settings, "_environment_explicit", False)
    with pytest.raises(main.EnvironmentNotSet) as refused:
        with TestClient(main.app):
            pass  # pragma: no cover - the lifespan must not get this far
    assert str(refused.value) == ENVIRONMENT_NOT_SET_MESSAGE
    assert ENVIRONMENT_NOT_SET_MESSAGE in caplog.text


def test_the_api_serves_when_the_environment_was_set(monkeypatch):
    from backend.app import main

    monkeypatch.setattr(main.settings, "_environment_explicit", True)
    with TestClient(main.app) as client:
        assert client.get("/health/live").status_code == 200


def test_the_refusal_is_one_plain_line_naming_the_setting_and_its_values():
    assert "\n" not in ENVIRONMENT_NOT_SET_MESSAGE
    assert ENVIRONMENT_NOT_SET_MESSAGE.startswith("ENVIRONMENT is not set")
    for name in ("development", "test", "staging", "production"):
        assert name in ENVIRONMENT_NOT_SET_MESSAGE


# ---------------------------------------------------------------------------
# The first administrator's password
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("password", ["admin", "Demo1234!", "short"])
def test_a_weak_first_superuser_password_is_refused_when_the_environment_defaulted(
    monkeypatch, password
):
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    defaulted = DevSettings(FIRST_SUPERUSER_PASSWORD=password)
    assert defaulted.environment_explicit is False
    assert first_superuser_refusal(defaulted) == WEAK_FIRST_SUPERUSER_PASSWORD_MESSAGE


@pytest.mark.parametrize(
    "settings_class, environment",
    [(DevSettings, "development"), (TestSettings, "test")],
)
def test_a_weak_password_is_accepted_when_development_or_test_was_chosen(
    monkeypatch, settings_class, environment
):
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    chosen = settings_class(ENVIRONMENT=environment, FIRST_SUPERUSER_PASSWORD="admin")
    assert first_superuser_refusal(chosen) is None


def test_a_strong_password_is_accepted_whatever_the_environment(monkeypatch):
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    defaulted = DevSettings(FIRST_SUPERUSER_PASSWORD="a-Long-unique-passphrase-9")
    assert first_superuser_refusal(defaulted) is None


def test_the_password_refusal_names_the_setting_but_not_the_password():
    assert "FIRST_SUPERUSER_PASSWORD" in WEAK_FIRST_SUPERUSER_PASSWORD_MESSAGE
    assert "ENVIRONMENT=development" in WEAK_FIRST_SUPERUSER_PASSWORD_MESSAGE
    assert "admin" not in WEAK_FIRST_SUPERUSER_PASSWORD_MESSAGE.lower()


# ---------------------------------------------------------------------------
# A refused configuration does not echo its inputs
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_a_refused_production_configuration_does_not_echo_the_secret_key(
    monkeypatch,
):
    """The SECRET_KEY is refused as too short; the error must not print it."""
    monkeypatch.delenv("TESTING", raising=False)
    with pytest.raises(ValidationError) as refused:
        ProdSettings(ENVIRONMENT="production", SECRET_KEY=DISTINCTIVE)
    rendered = str(refused.value)
    assert "SECRET_KEY" in rendered
    # pydantic shortens a long input from the middle, so any fragment counts,
    # and which values end up in the shortened dict depends on the order the
    # sources were merged in: no input may be rendered at all.
    assert "distinctive" not in rendered
    assert "input_value" not in rendered


@pytest.mark.regression
def test_a_whole_configuration_refusal_does_not_echo_any_value(monkeypatch):
    """A model-level refusal used to print the whole input, SECRET_KEY included."""
    monkeypatch.setenv("TESTING", "true")
    secret = DISTINCTIVE * 3  # long enough that only the TESTING rule refuses
    with pytest.raises(ValidationError) as refused:
        ProdSettings(
            ENVIRONMENT="production",
            SECRET_KEY=secret,
            PUBLIC_BASE_URL="https://controlvar.example.com",
        )
    rendered = str(refused.value)
    assert "TESTING" in rendered
    # pydantic shortens a long input from the middle, so any fragment counts,
    # and which values end up in the shortened dict depends on the order the
    # sources were merged in: no input may be rendered at all.
    assert "distinctive" not in rendered
    assert "input_value" not in rendered
