"""How the API's settings are built and read (#1142).

One ``Settings`` class holds every setting and its default; ``DevSettings``,
``TestSettings`` and ``ProdSettings`` change some of those defaults and each
reads its own environment file, which the process environment overrides.
``.env.example`` at the repository root is the template. These hold that, and
the defaults and precedence the environment guide states: which CORS setting
wins, the SDK rate limit, and that ``LOG_LEVEL`` is the API's log level.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic_settings import BaseSettings

from backend.app.core.config import (
    DevSettings,
    ProdSettings,
    Settings,
    TestSettings,
)
from backend.app.middleware.rate_limiter import resolve_rate_limit

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
TEMPLATE = REPO_ROOT / ".env.example"


@pytest.mark.parametrize("cls", [DevSettings, TestSettings, ProdSettings])
def test_each_environment_class_derives_from_settings_and_changes_defaults(cls):
    assert issubclass(Settings, BaseSettings)
    assert cls.__bases__ == (Settings,)
    changed = {
        name
        for name in set(cls.__annotations__) & set(Settings.model_fields)
        if cls.model_fields[name].default != Settings.model_fields[name].default
    }
    assert changed, f"{cls.__name__} changes no default of Settings"


def test_the_process_environment_overrides_the_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PROJECT_NAME", raising=False)
    (tmp_path / ".env.dev").write_text("PROJECT_NAME=from the file\n", encoding="utf-8")
    assert DevSettings(ENVIRONMENT="development").PROJECT_NAME == "from the file"
    monkeypatch.setenv("PROJECT_NAME", "from the process")
    assert DevSettings(ENVIRONMENT="development").PROJECT_NAME == "from the process"


def test_the_template_is_at_the_root_and_sets_what_the_guide_shows():
    names = {
        line.split("=", 1)[0].lstrip("# ").strip()
        for line in TEMPLATE.read_text(encoding="utf-8").splitlines()
        if "=" in line and line.lstrip("# ").split("=", 1)[0].isidentifier()
    }
    shown = {
        "POSTGRES_SERVER",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_DB",
        "REDIS_HOST",
        "REDIS_PORT",
        "ENVIRONMENT",
        "LOG_LEVEL",
        "SECRET_KEY",
        "CORS_ORIGINS",
        "SDK_RATE_LIMIT_PER_MINUTE",
        "BANDIT_UPDATE_INTERVAL_MINUTES",
        "EXPERIMENT_SCHEDULER_INTERVAL_MINUTES",
    }
    assert shown <= names, sorted(shown - names)


def test_backend_cors_origins_wins_when_both_are_set(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", "https://cors.example.com")
    monkeypatch.setenv("BACKEND_CORS_ORIGINS", '["https://backend.example.com"]')
    settings = DevSettings(ENVIRONMENT="development")
    assert settings.cors_allowed_origins[0] == "https://backend.example.com"
    assert "https://cors.example.com" not in settings.cors_allowed_origins


def test_the_sdk_rate_limit_is_6000_a_minute_by_default(monkeypatch):
    monkeypatch.delenv("SDK_RATE_LIMIT_PER_MINUTE", raising=False)
    assert Settings.model_fields["SDK_RATE_LIMIT_PER_MINUTE"].default == 6000
    assert DevSettings(ENVIRONMENT="development").SDK_RATE_LIMIT_PER_MINUTE == 6000
    assert resolve_rate_limit("/api/v1/tracking/track") == (6000, 60)


def test_log_level_is_the_level_the_api_logs_at(tmp_path):
    """The API configures logging from LOG_LEVEL when it starts: DEBUG adds
    the debug lines, which INFO, the default, leaves out."""
    probe = (
        "import logging, backend.app.main; "
        "print(logging.getLevelName(logging.getLogger().level))"
    )
    levels = {}
    for level in ("DEBUG", "INFO"):
        env = {
            **os.environ,
            "ENVIRONMENT": "test",
            "LOG_LEVEL": level,
            "LOG_FORMAT": "console",
        }
        result = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr[-2000:]
        levels[level] = result.stdout.strip().splitlines()[-1]
    assert levels == {"DEBUG": "DEBUG", "INFO": "INFO"}
    assert logging.DEBUG < logging.INFO


def test_the_application_imports_one_settings_instance():
    from backend.app.core import config

    assert isinstance(config.settings, Settings)
