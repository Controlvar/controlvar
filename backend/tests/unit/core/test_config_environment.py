"""
Unit tests for ``ENVIRONMENT`` canonicalisation and the auth-provider settings
(P0 open-core: backend/app/core/config.py).

Covers:
- canonical values, legacy ``dev``/``prod`` aliases (with DeprecationWarning),
  rejection of unknown values
- ``APP_ENV`` fallback and ``ENVIRONMENT`` precedence for the singleton
  selection (run in a subprocess so the module import is fresh)
- ``AUTH_PROVIDER`` / ``DEV_AUTH_BYPASS`` defaults and the production guard
- the ``dev_auth_bypass_active`` helper in ``deps``
- ``dev_fallbacks_allowed`` and the one ``.env.*`` table the settings classes
  and the modules' settings share
"""

import os
import subprocess
import sys
import warnings
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from backend.app.core.config import (
    BYPASS_ALLOWED_ENVIRONMENTS,
    CANONICAL_ENVIRONMENTS,
    ENV_FILES,
    DevSettings,
    ProdSettings,
    Settings,
    TestSettings,
    canonical_environment,
    env_file_for_environment,
    resolve_environment_from_process_env,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_testing_flag(monkeypatch):
    """The suite's ``TESTING=true`` is refused with staging/production.

    ``backend/tests/conftest.py`` exports ``TESTING=true`` for the whole run,
    and ``Settings`` refuses it together with a hardened environment, so every
    test here that builds staging or production settings needs it gone. A test
    about ``TESTING`` itself sets it again.
    """
    monkeypatch.delenv("TESTING", raising=False)


REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
)


def _clean_env(**overrides):
    """Environment for subprocess checks: no ENVIRONMENT/APP_ENV unless given."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in ("ENVIRONMENT", "APP_ENV", "DEV_AUTH_BYPASS", "TESTING", "SECRET_KEY")
    }
    env["PYTHONPATH"] = REPO_ROOT
    env.update(overrides)
    return env


def _run(code: str, **env_overrides) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env=_clean_env(**env_overrides),
        capture_output=True,
        text=True,
        timeout=120,
    )


# ---------------------------------------------------------------------------
# canonical_environment
# ---------------------------------------------------------------------------


class TestCanonicalEnvironment:
    @pytest.mark.parametrize("value", CANONICAL_ENVIRONMENTS)
    def test_canonical_values_pass_through_without_warning(self, value):
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            assert canonical_environment(value) == value

    @pytest.mark.parametrize(
        "legacy,expected", [("dev", "development"), ("prod", "production")]
    )
    def test_legacy_aliases_map_with_deprecation_warning(self, legacy, expected):
        with pytest.warns(DeprecationWarning, match="deprecated"):
            assert canonical_environment(legacy) == expected

    @pytest.mark.parametrize(
        "raw,expected", [("  Development ", "development"), ("PROD", "production")]
    )
    def test_whitespace_and_case_are_normalised(self, raw, expected):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            assert canonical_environment(raw) == expected

    def test_non_string_is_returned_unchanged(self):
        assert canonical_environment(None) is None
        assert canonical_environment(3) == 3

    def test_canonical_set_matches_contract(self):
        assert set(CANONICAL_ENVIRONMENTS) == {
            "development",
            "test",
            "staging",
            "production",
        }
        assert set(BYPASS_ALLOWED_ENVIRONMENTS) == {"development", "test"}


# ---------------------------------------------------------------------------
# Settings.ENVIRONMENT field
# ---------------------------------------------------------------------------


class TestSettingsEnvironmentField:
    def test_default_is_development(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ENVIRONMENT", None)
            assert Settings(_env_file=None).ENVIRONMENT == "development"

    @pytest.mark.parametrize("value", CANONICAL_ENVIRONMENTS)
    def test_accepts_every_canonical_value(self, value):
        assert (
            Settings(_env_file=None, ENVIRONMENT=value, **_STRONG).ENVIRONMENT == value
        )

    def test_legacy_prod_kwarg_is_canonicalised(self):
        with pytest.warns(DeprecationWarning):
            s = Settings(_env_file=None, ENVIRONMENT="prod", **_STRONG)
        assert s.ENVIRONMENT == "production"
        assert s.is_production is True

    def test_legacy_dev_kwarg_is_canonicalised(self):
        with pytest.warns(DeprecationWarning):
            s = Settings(_env_file=None, ENVIRONMENT="dev")
        assert s.ENVIRONMENT == "development"
        assert s.is_development is True

    def test_unknown_value_is_rejected(self):
        with pytest.raises(ValidationError, match="development"):
            Settings(_env_file=None, ENVIRONMENT="bogus")

    def test_legacy_env_var_is_canonicalised(self):
        with patch.dict(os.environ, {"ENVIRONMENT": "prod"}):
            with pytest.warns(DeprecationWarning):
                s = Settings(_env_file=None, **_STRONG)
        assert s.ENVIRONMENT == "production"

    def test_subclass_defaults_are_canonical(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ENVIRONMENT", None)
            assert DevSettings(_env_file=None).ENVIRONMENT == "development"
            assert TestSettings(_env_file=None).ENVIRONMENT == "test"
            assert (
                ProdSettings(
                    _env_file=None,
                    SECRET_KEY="a" * 64,
                    FIRST_SUPERUSER_PASSWORD="StrongProd1!",
                    PUBLIC_BASE_URL="https://controlvar.example.com",
                ).ENVIRONMENT
                == "production"
            )

    def test_production_validators_still_fire_on_canonical_value(self, monkeypatch):
        monkeypatch.delenv("TESTING", raising=False)
        with pytest.raises(ValidationError, match="SECRET_KEY"):
            Settings(_env_file=None, ENVIRONMENT="production", SECRET_KEY="changeme")

    def test_production_validators_fire_on_legacy_prod_value(self, monkeypatch):
        monkeypatch.delenv("TESTING", raising=False)
        with pytest.warns(DeprecationWarning):
            with pytest.raises(ValidationError, match="SECRET_KEY"):
                Settings(_env_file=None, ENVIRONMENT="prod", SECRET_KEY="changeme")

    def test_helper_properties(self):
        assert Settings(_env_file=None, ENVIRONMENT="test").is_test
        assert Settings(_env_file=None, ENVIRONMENT="staging", **_STRONG).is_staging

    def test_demo_alias_maps_to_development(self):
        """The CDK demo stack (ENVIRONMENT=demo cdk deploy) runs with APP_ENV=demo."""
        with pytest.warns(DeprecationWarning):
            assert (
                Settings(_env_file=None, ENVIRONMENT="demo").ENVIRONMENT
                == "development"
            )


# ---------------------------------------------------------------------------
# Placeholder secrets are refused in staging as well as production
# ---------------------------------------------------------------------------

# Everything a hardened environment demands. PUBLIC_BASE_URL joined this with
# the #220 fix: a production start must know what hostname it answers on,
# because the Host header is attacker-controlled and ALLOWED_HOSTS derives from
# this. It is CORE configuration, which is what keeps #91's property intact --
# a core start still demands nothing that only a module reads.
_STRONG = {
    "SECRET_KEY": "b" * 64,
    "FIRST_SUPERUSER_PASSWORD": "Str0ng-Passw0rd",
    "PUBLIC_BASE_URL": "https://api.example.com",
}

# The values that ship in the repository: class defaults, docker-compose.yml,
# .env.example, the demo seed password.
_COMMITTED_PLACEHOLDERS = [
    ("SECRET_KEY", "default-secret-key-for-testing"),
    ("SECRET_KEY", "dev-only-change-me-9f1c7b2e4a6d8e0f1a2b3c4d5e6f7a8b"),
    ("SECRET_KEY", "development_secret_key_change_in_production"),
    ("SECRET_KEY", "DEV-ONLY-" + "x" * 60),
    ("FIRST_SUPERUSER_PASSWORD", "Demo1234!"),
    ("FIRST_SUPERUSER_PASSWORD", "admin"),
]


class TestHardenedSecrets:
    @pytest.mark.parametrize("env", ["staging", "production"])
    @pytest.mark.parametrize("field,value", _COMMITTED_PLACEHOLDERS)
    def test_committed_placeholders_are_refused(self, env, field, value, monkeypatch):
        monkeypatch.delenv("TESTING", raising=False)
        kwargs = dict(_STRONG, ENVIRONMENT=env)
        kwargs[field] = value
        with pytest.raises(ValidationError, match=field):
            Settings(_env_file=None, **kwargs)

    @pytest.mark.parametrize("env", ["staging", "production"])
    def test_class_defaults_are_refused(self, env, monkeypatch):
        monkeypatch.delenv("TESTING", raising=False)
        with pytest.raises(ValidationError) as excinfo:
            Settings(_env_file=None, ENVIRONMENT=env)
        text = str(excinfo.value)
        for field in ("SECRET_KEY", "FIRST_SUPERUSER_PASSWORD"):
            assert field in text
        # Issue #91: the modules' secrets are not the core class's to
        # demand.  AUDIT_HMAC_KEY is validated by ModulesSettings
        # (modules/backend/app/settings.py); SSO_STATE_SECRET had no reader and is gone.
        for field in ("AUDIT_HMAC_KEY", "SSO_STATE_SECRET"):
            assert field not in text

    @pytest.mark.parametrize("env", ["staging", "production"])
    def test_real_secrets_are_accepted(self, env, monkeypatch):
        monkeypatch.delenv("TESTING", raising=False)
        assert Settings(_env_file=None, ENVIRONMENT=env, **_STRONG).ENVIRONMENT == env

    @pytest.mark.parametrize("env", ["development", "test"])
    def test_placeholders_are_fine_outside_hardened_environments(
        self, env, monkeypatch
    ):
        monkeypatch.delenv("TESTING", raising=False)
        assert Settings(_env_file=None, ENVIRONMENT=env).ENVIRONMENT == env

    @pytest.mark.regression
    @pytest.mark.parametrize("env", ["staging", "production", "prod"])
    @pytest.mark.parametrize(
        "flag",
        # Every spelling pydantic's bool reads as True, and (failing closed)
        # values it cannot read at all.
        [
            "true",
            "1",
            "yes",
            "TRUE",
            "on",
            "t",
            "y",
            "On",
            "T",
            "Y",
            " true",
            "enabled",
        ],
    )
    def test_testing_flag_is_refused_in_hardened_environments(
        self, env, flag, monkeypatch
    ):
        """``TESTING`` is the test runner's flag. With staging or production it
        used to relax the secret and host checks; now it is refused outright,
        and the message says so rather than naming some other setting."""
        monkeypatch.setenv("TESTING", flag)
        canonical = "production" if env == "prod" else env
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            with pytest.raises(ValidationError) as excinfo:
                Settings(_env_file=None, ENVIRONMENT=env, **_STRONG)
        assert (
            "TESTING is for the test runner and cannot be combined with "
            f"ENVIRONMENT={canonical}"
        ) in str(excinfo.value)

    @pytest.mark.regression
    @pytest.mark.parametrize("cls", [ProdSettings, Settings])
    def test_testing_flag_is_refused_when_the_class_default_is_hardened(
        self, cls, monkeypatch
    ):
        """No ENVIRONMENT passed: the class default decides (production)."""
        monkeypatch.setenv("TESTING", "true")
        monkeypatch.setenv("ENVIRONMENT", "production")
        with pytest.raises(ValidationError, match="TESTING is for the test runner"):
            cls(_env_file=None, **_STRONG)
        monkeypatch.delenv("ENVIRONMENT")
        with pytest.raises(ValidationError, match="TESTING is for the test runner"):
            ProdSettings(_env_file=None, **_STRONG)

    @pytest.mark.parametrize("env", ["development", "test"])
    def test_testing_flag_is_fine_outside_hardened_environments(self, env, monkeypatch):
        monkeypatch.setenv("TESTING", "true")
        assert Settings(_env_file=None, ENVIRONMENT=env).ENVIRONMENT == env

    @pytest.mark.parametrize("flag", ["", "false", "0", "off", "N", "No"])
    def test_an_empty_or_false_testing_flag_is_not_set(self, flag, monkeypatch):
        """``TESTING=`` (as in the issue #91 command line) is not the flag, and
        neither is anything pydantic's bool reads as False."""
        monkeypatch.setenv("TESTING", flag)
        assert Settings(
            _env_file=None, ENVIRONMENT="production", **_STRONG
        ).is_production

    @pytest.mark.regression
    def test_a_core_production_start_needs_no_module_secret(self, monkeypatch):
        """Issue #91: a core deployment with ENVIRONMENT=production used to
        refuse to start unless it also set AUDIT_HMAC_KEY and SSO_STATE_SECRET
        -- two secrets only the modules read, one of which nothing read.  The
        only secrets a production core start may demand are its own."""
        monkeypatch.delenv("TESTING", raising=False)
        for name in ("AUDIT_HMAC_KEY", "SSO_STATE_SECRET", "SSO_ENABLED"):
            assert name not in Settings.model_fields, f"{name} is a module's"
        settings = Settings(_env_file=None, ENVIRONMENT="production", **_STRONG)
        assert settings.is_production

    @pytest.mark.regression
    def test_the_issue_91_command_line(self, monkeypatch):
        """The reproduction from the issue, in a fresh interpreter:
        ``ENVIRONMENT=production TESTING= SECRET_KEY=<64 hex> python -c
        "from backend.app.core.config import settings"``.

        Verbatim it still fails -- on FIRST_SUPERUSER_PASSWORD, whose class
        default is ``admin``, and since the #220 fix on the absence of any
        host allow-list. Both are core settings and both are refused on
        purpose.

        What #91 is about is NOT the number of complaints, it is WHOSE: a CORE
        production start must never be blocked on a secret only a MODULE reads.
        So the assertions below are that the core settings are named, that
        AUDIT_HMAC_KEY and SSO_STATE_SECRET are not, and that setting the core
        ones makes the import succeed with nothing else configured. A new core
        requirement should update this deliberately, not discover it."""
        import os
        import subprocess
        import sys

        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("TESTING", "APP_ENV", "ENVIRONMENT")
            and not k.startswith(("AUDIT_", "SSO_", "FIRST_SUPERUSER"))
        }
        env.update(ENVIRONMENT="production", SECRET_KEY="a" * 64)
        probe = [sys.executable, "-c", "from backend.app.core.config import settings"]

        verbatim = subprocess.run(probe, env=env, capture_output=True, text=True)
        assert verbatim.returncode != 0
        assert "FIRST_SUPERUSER_PASSWORD" in verbatim.stderr
        # The #91 property itself: neither module secret is demanded of a core
        # start. This is the assertion that must never be relaxed.
        assert "AUDIT_HMAC_KEY" not in verbatim.stderr
        assert "SSO_STATE_SECRET" not in verbatim.stderr

        env["FIRST_SUPERUSER_PASSWORD"] = "Str0ng-Passw0rd"
        env["PUBLIC_BASE_URL"] = "https://api.example.com"
        fixed = subprocess.run(probe, env=env, capture_output=True, text=True)
        assert fixed.returncode == 0, fixed.stderr[-2000:]


# ---------------------------------------------------------------------------
# dev_fallbacks_allowed — the allow-list for degraded development behaviour
# ---------------------------------------------------------------------------


class TestDevFallbacksAllowed:
    """The gate on behaviour that is a convenience in dev and a hole elsewhere.

    Two call sites use it: the SAML stub parser, which accepts an assertion
    without checking its signature when ``python3-saml`` is missing, and the
    loader's decision to continue on the core profile after a modules
    registration failed.
    """

    @pytest.mark.parametrize("env", ["development", "test"])
    def test_allowed_in_development_and_test(self, env):
        assert Settings(_env_file=None, ENVIRONMENT=env).dev_fallbacks_allowed is True

    @pytest.mark.parametrize("env", ["staging", "production"])
    def test_refused_in_staging_and_production(self, env):
        settings = Settings(_env_file=None, ENVIRONMENT=env, **_STRONG)
        assert settings.dev_fallbacks_allowed is False

    def test_staging_is_hardened_like_production(self):
        """An ``ENVIRONMENT == "production"`` test at a call site would have
        left staging wide open; the allow-list is why these read the property
        instead."""
        staging = Settings(_env_file=None, ENVIRONMENT="staging", **_STRONG)
        assert staging.is_production is False
        assert staging.dev_fallbacks_allowed is False

    def test_the_allow_list_is_the_bypass_allow_list(self):
        """One list, so a new hardened environment hardens both at once."""
        assert set(BYPASS_ALLOWED_ENVIRONMENTS) == {"development", "test"}
        assert all(
            Settings(_env_file=None, ENVIRONMENT=env, **_STRONG).dev_fallbacks_allowed
            is (env in BYPASS_ALLOWED_ENVIRONMENTS)
            for env in CANONICAL_ENVIRONMENTS
        )


# ---------------------------------------------------------------------------
# env_file_for_environment — one table for the core classes and the modules
# ---------------------------------------------------------------------------


class TestEnvFileTable:
    @pytest.mark.parametrize(
        "cls, env",
        [
            (DevSettings, "development"),
            (TestSettings, "test"),
            (ProdSettings, "production"),
        ],
    )
    def test_each_settings_class_reads_the_table_entry(self, cls, env):
        assert cls.model_config["env_file"] == ENV_FILES[env]
        assert env_file_for_environment(env) == ENV_FILES[env]

    def test_staging_shares_the_production_file(self):
        """``ProdSettings`` serves staging too, so it reads ``.env.prod``."""
        assert (
            env_file_for_environment("staging") == ProdSettings.model_config["env_file"]
        )

    @pytest.mark.parametrize(
        "legacy, expected", [("dev", ".env.dev"), ("prod", ".env.prod")]
    )
    def test_legacy_spellings_resolve_quietly(self, legacy, expected):
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            assert env_file_for_environment(legacy) == expected

    @pytest.mark.parametrize("value", ["nonsense", "", None, 7])
    def test_unrecognised_names_never_pick_up_the_production_file(self, value):
        assert env_file_for_environment(value) == ".env.dev"


# ---------------------------------------------------------------------------
# resolve_environment_from_process_env
# ---------------------------------------------------------------------------


class TestResolveFromProcessEnv:
    def test_defaults_to_development(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ENVIRONMENT", None)
            os.environ.pop("APP_ENV", None)
            assert resolve_environment_from_process_env() == "development"

    def test_app_env_fallback_warns(self):
        with patch.dict(os.environ, {"APP_ENV": "test"}, clear=False):
            os.environ.pop("ENVIRONMENT", None)
            with pytest.warns(DeprecationWarning, match="APP_ENV is deprecated"):
                assert resolve_environment_from_process_env() == "test"

    def test_app_env_prod_alias(self):
        with patch.dict(os.environ, {"APP_ENV": "prod"}, clear=False):
            os.environ.pop("ENVIRONMENT", None)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                assert resolve_environment_from_process_env() == "production"

    def test_environment_wins_over_app_env(self):
        with patch.dict(
            os.environ, {"ENVIRONMENT": "staging", "APP_ENV": "test"}, clear=False
        ):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                assert resolve_environment_from_process_env() == "staging"


# ---------------------------------------------------------------------------
# Module import (fresh interpreter): singleton selection + APP_ENV mirror
# ---------------------------------------------------------------------------


class TestModuleImportSelection:
    """Each case imports backend.app.core.config in a subprocess."""

    CODE = (
        "from backend.app.core.config import settings; import os; "
        "print(type(settings).__name__, settings.ENVIRONMENT, os.environ.get('APP_ENV'))"
    )

    def test_app_env_production_selects_prod_settings(self):
        proc = _run(self.CODE, APP_ENV="production", **_STRONG)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.split() == ["ProdSettings", "production", "production"]

    def test_app_env_prod_legacy_alias(self):
        proc = _run(self.CODE, APP_ENV="prod", **_STRONG)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.split()[:2] == ["ProdSettings", "production"]

    def test_app_env_test_selects_test_settings(self):
        proc = _run(self.CODE, APP_ENV="test", TESTING="true")
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.split() == ["TestSettings", "test", "test"]

    def test_environment_test_mirrors_app_env_for_legacy_readers(self):
        proc = _run(self.CODE, ENVIRONMENT="test", TESTING="true")
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.split() == ["TestSettings", "test", "test"]

    def test_environment_development_selects_dev_settings(self):
        proc = _run(self.CODE, ENVIRONMENT="development")
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.split() == ["DevSettings", "development", "dev"]

    def test_environment_staging_uses_prod_settings_but_keeps_name(self):
        proc = _run(self.CODE, ENVIRONMENT="staging", **_STRONG)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.split()[:2] == ["ProdSettings", "staging"]

    def test_no_env_defaults_to_development(self):
        proc = _run(self.CODE)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.split()[:2] == ["DevSettings", "development"]

    def test_unknown_environment_refuses_to_import(self):
        proc = _run("import backend.app.core.config", ENVIRONMENT="bogus")
        assert proc.returncode != 0
        assert "development" in proc.stderr  # Literal error lists the allowed values

    def test_bypass_in_production_refuses_to_import(self):
        proc = _run(
            "import backend.app.core.config",
            ENVIRONMENT="production",
            DEV_AUTH_BYPASS="true",
            # Every other production setting good: the bypass guard must fire on its own.
            **_STRONG,
        )
        assert proc.returncode != 0
        assert "DEV_AUTH_BYPASS=true is not permitted" in proc.stderr

    @pytest.mark.regression
    def test_testing_with_production_refuses_to_import(self):
        """What an operator would see: the import fails, naming TESTING."""
        proc = _run(
            "import backend.app.core.config",
            ENVIRONMENT="production",
            TESTING="true",
            **_STRONG,
        )
        assert proc.returncode != 0
        assert (
            "TESTING is for the test runner and cannot be combined with "
            "ENVIRONMENT=production"
        ) in proc.stderr

    def test_bypass_in_production_refuses_with_default_secrets_too(self):
        proc = _run(
            "import backend.app.core.config",
            ENVIRONMENT="production",
            DEV_AUTH_BYPASS="true",
        )
        assert proc.returncode != 0

    def test_weak_secret_key_in_production_refuses_to_boot(self):
        proc = _run(
            "import backend.app.core.config",
            ENVIRONMENT="production",
            SECRET_KEY="changeme-in-prod",
        )
        assert proc.returncode != 0
        assert "SECRET_KEY" in proc.stderr

    def test_compose_defaults_in_production_refuse_to_boot(self):
        proc = _run(
            "import backend.app.core.config",
            ENVIRONMENT="production",
            SECRET_KEY="dev-only-change-me-9f1c7b2e4a6d8e0f1a2b3c4d5e6f7a8b",
            FIRST_SUPERUSER_PASSWORD="Demo1234!",
        )
        assert proc.returncode != 0
        assert "SECRET_KEY" in proc.stderr
        assert "FIRST_SUPERUSER_PASSWORD" in proc.stderr

    def test_app_env_demo_boots_as_development(self):
        proc = _run(
            "from backend.app.core.config import settings; print(settings.ENVIRONMENT)",
            APP_ENV="demo",
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "development"

    def test_bypass_in_test_is_accepted(self):
        proc = _run(
            "from backend.app.core.config import settings; print(settings.dev_auth_bypass_active)",
            ENVIRONMENT="test",
            DEV_AUTH_BYPASS="true",
            TESTING="true",
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "True"


# ---------------------------------------------------------------------------
# AUTH_PROVIDER / DEV_AUTH_BYPASS
# ---------------------------------------------------------------------------


class TestAuthSettings:
    def test_defaults(self):
        with patch.dict(os.environ, {}, clear=False):
            for key in ("AUTH_PROVIDER", "DEV_AUTH_BYPASS", "ENVIRONMENT"):
                os.environ.pop(key, None)
            s = Settings(_env_file=None)
        assert s.AUTH_PROVIDER == "local"
        assert s.DEV_AUTH_BYPASS is False
        assert s.LOCAL_AUTH_TOKEN_TTL_MINUTES == 720
        assert s.LOCAL_AUTH_MAX_FAILED_ATTEMPTS == 10
        assert s.LOCAL_AUTH_LOCKOUT_MINUTES == 15
        assert s.dev_auth_bypass_active is False

    def test_auth_provider_rejects_unknown(self):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, AUTH_PROVIDER="ldap")

    def test_cognito_provider_accepted(self):
        assert (
            Settings(_env_file=None, AUTH_PROVIDER="cognito").AUTH_PROVIDER == "cognito"
        )

    @pytest.mark.parametrize("env", ["development", "test"])
    def test_bypass_allowed_in_dev_and_test(self, env):
        s = Settings(_env_file=None, ENVIRONMENT=env, DEV_AUTH_BYPASS=True)
        assert s.dev_auth_bypass_active is True

    @pytest.mark.parametrize("env", ["staging", "production"])
    def test_bypass_rejected_in_staging_and_production(self, env):
        with pytest.raises(
            ValidationError, match="DEV_AUTH_BYPASS=true is not permitted"
        ):
            Settings(_env_file=None, ENVIRONMENT=env, DEV_AUTH_BYPASS=True, **_STRONG)

    def test_bypass_false_in_production_is_fine(self):
        s = Settings(
            _env_file=None,
            ENVIRONMENT="production",
            DEV_AUTH_BYPASS=False,
            **_STRONG,
        )
        assert s.dev_auth_bypass_active is False


class TestDepsBypassHelper:
    """``deps.dev_auth_bypass_active`` reads the singleton at call time."""

    def test_true_only_with_both_conditions(self, monkeypatch):
        from backend.app.api import deps

        monkeypatch.setattr(deps.settings, "DEV_AUTH_BYPASS", True)
        monkeypatch.setattr(deps.settings, "ENVIRONMENT", "test")
        assert deps.dev_auth_bypass_active() is True

        monkeypatch.setattr(deps.settings, "ENVIRONMENT", "production")
        assert deps.dev_auth_bypass_active() is False

        monkeypatch.setattr(deps.settings, "ENVIRONMENT", "development")
        monkeypatch.setattr(deps.settings, "DEV_AUTH_BYPASS", False)
        assert deps.dev_auth_bypass_active() is False

    def test_truthy_non_bool_does_not_unlock(self, monkeypatch):
        from backend.app.api import deps

        monkeypatch.setattr(deps.settings, "DEV_AUTH_BYPASS", "true")
        monkeypatch.setattr(deps.settings, "ENVIRONMENT", "development")
        assert deps.dev_auth_bypass_active() is False

    def test_mock_settings_never_unlock(self):
        """A MagicMock settings object (used by legacy tests) must not enable the bypass."""
        from backend.app.api import deps

        with patch("backend.app.api.deps.settings", MagicMock()):
            assert deps.dev_auth_bypass_active() is False
