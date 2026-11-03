"""``modules/backend/app/settings.py`` -- the modules' settings (issue #91).

The AUDIT_HMAC_KEY hardening cases moved here from
``backend/tests/unit/core/test_config_environment.py`` when the field left the
core ``Settings``; the rest pins the seam: the modules read this class,
``register(hooks)`` validates it first, and a placeholder key in production
is a registration failure the loader reports, never a crash.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.app import modules_loader
from backend.app.core import hooks
from backend.app.core.config import Settings
from modules.backend.app import settings as modules_settings
from modules.backend.app.settings import ModulesSettings

_STRONG = {"AUDIT_HMAC_KEY": "c" * 64}

#: Every field that left the core class and where it went.
_MOVED_FIELDS = (
    "AUDIT_HMAC_KEY",
    "PHI_ENCRYPTION_KEY",
    "HIPAA_ENABLED",
    "HIPAA_ALLOWED_REGIONS",
    "HIPAA_AUDIT_LOG_RETENTION_YEARS",
    "DYNAMODB_COUNTERS_TABLE",
    "GLUE_ETL_JOB_NAME",
    "GLUE_METRICS_JOB_NAME",
    "GLUE_DATABASE",
    "GLUE_EVENTS_TABLE",
    "GLUE_CRAWLER_NAME",
    "SAML_SP_ENTITY_ID",
    "SAML_SP_ACS_URL",
    "OIDC_GOOGLE_CLIENT_ID",
    "OIDC_GOOGLE_CLIENT_SECRET",
    "OIDC_GITHUB_CLIENT_ID",
    "OIDC_GITHUB_CLIENT_SECRET",
    "OIDC_MICROSOFT_CLIENT_ID",
    "OIDC_MICROSOFT_CLIENT_SECRET",
)


class TestWhereTheFieldsLive:
    @pytest.mark.parametrize("name", _MOVED_FIELDS)
    def test_module_field_is_not_a_core_field(self, name):
        assert name in ModulesSettings.model_fields
        assert name not in Settings.model_fields, f"{name} is still core"

    @pytest.mark.parametrize("name", ["SSO_ENABLED", "SSO_STATE_SECRET"])
    def test_unread_sso_fields_are_gone(self, name):
        """Nothing read either; deleted rather than moved (issue #91)."""
        assert name not in ModulesSettings.model_fields
        assert name not in Settings.model_fields

    @pytest.mark.parametrize(
        "name",
        [
            "ATHENA_OUTPUT_BUCKET",
            "DATABRICKS_HOST",
            "CLICKHOUSE_HOST",
            "MYSQL_HOST",
        ],
    )
    def test_settings_of_removed_endpoints_are_gone(self, name):
        """Read only by the removed warehouse connectors and ETL query."""
        assert name not in ModulesSettings.model_fields
        assert name not in Settings.model_fields

    @pytest.mark.parametrize(
        "name", ["AUDIT_RETENTION_DAYS_SOC2", "AUDIT_RETENTION_DAYS_ISO27001"]
    )
    def test_audit_retention_stays_core(self, name):
        """The core AuditLogService stamps retention on every event."""
        assert name in Settings.model_fields
        assert name not in ModulesSettings.model_fields


class TestHardenedAuditKey:
    """The AUDIT_HMAC_KEY cases from the core config tests, relocated."""

    @pytest.mark.parametrize("env", ["staging", "production"])
    @pytest.mark.parametrize(
        "value",
        ["dev-audit-key-change-in-production", "dev-audit-anything", "short", ""],
    )
    def test_placeholder_or_short_key_is_refused(self, env, value, monkeypatch):
        monkeypatch.delenv("TESTING", raising=False)
        with pytest.raises(ValidationError, match="AUDIT_HMAC_KEY"):
            ModulesSettings(_env_file=None, ENVIRONMENT=env, AUDIT_HMAC_KEY=value)

    @pytest.mark.parametrize("env", ["staging", "production"])
    def test_class_default_is_refused(self, env, monkeypatch):
        monkeypatch.delenv("TESTING", raising=False)
        with pytest.raises(ValidationError, match="AUDIT_HMAC_KEY"):
            ModulesSettings(_env_file=None, ENVIRONMENT=env)

    @pytest.mark.parametrize("env", ["staging", "production"])
    def test_real_key_is_accepted(self, env, monkeypatch):
        monkeypatch.delenv("TESTING", raising=False)
        built = ModulesSettings(_env_file=None, ENVIRONMENT=env, **_STRONG)
        assert built.ENVIRONMENT == env
        assert built.AUDIT_HMAC_KEY == "c" * 64

    @pytest.mark.parametrize("env", ["development", "test"])
    def test_placeholders_are_fine_outside_hardened_environments(
        self, env, monkeypatch
    ):
        monkeypatch.delenv("TESTING", raising=False)
        assert ModulesSettings(_env_file=None, ENVIRONMENT=env).ENVIRONMENT == env

    @pytest.mark.regression
    @pytest.mark.parametrize("env", ["staging", "production"])
    def test_testing_flag_does_not_relax_the_check(self, env, monkeypatch):
        """TESTING used to switch this check off. The core settings now refuse
        TESTING with a hardened environment, and the check here no longer
        looks at it."""
        monkeypatch.setenv("TESTING", "true")
        with pytest.raises(ValidationError, match="AUDIT_HMAC_KEY"):
            ModulesSettings(_env_file=None, ENVIRONMENT=env)

    def test_legacy_environment_spellings_are_canonicalised(self, monkeypatch):
        monkeypatch.delenv("TESTING", raising=False)
        with pytest.warns(DeprecationWarning):
            assert (
                ModulesSettings(
                    _env_file=None, ENVIRONMENT="prod", **_STRONG
                ).ENVIRONMENT
                == "production"
            )


class TestTheProcessInstance:
    def test_environment_mirrors_the_core_singleton(self):
        from backend.app.core.config import settings as core

        built = modules_settings.build_modules_settings()
        assert built.ENVIRONMENT == core.ENVIRONMENT == "test"

    def test_module_attribute_is_the_loaded_instance(self):
        from modules.backend.app.settings import settings

        assert settings is modules_settings.load_modules_settings()
        assert isinstance(settings, ModulesSettings)

    def test_force_rebuilds(self):
        before = modules_settings.load_modules_settings()
        after = modules_settings.load_modules_settings(force=True)
        assert after is not before
        assert modules_settings.load_modules_settings() is after

    def test_unknown_attribute_is_an_attribute_error(self):
        with pytest.raises(AttributeError):
            getattr(modules_settings, "no_such_setting")

    @pytest.mark.parametrize(
        "module, name",
        [
            ("modules.backend.app.services.audit_signing_service", "AUDIT_HMAC_KEY"),
            ("modules.backend.app.services.hipaa_service", "HIPAA_ALLOWED_REGIONS"),
            ("modules.backend.app.services.sso_service", "SAML_SP_ENTITY_ID"),
        ],
    )
    def test_module_readers_bind_the_modules_instance(self, module, name):
        """Each reader's module-level ``settings`` is the modules' one, so
        the field it reads exists there (the core singleton no longer has
        it)."""
        import importlib

        bound = importlib.import_module(module).settings
        assert isinstance(bound, ModulesSettings)
        assert hasattr(bound, name)


@pytest.fixture
def fresh_hooks():
    saved_signer = hooks.audit_signer
    with hooks._lock:
        saved = (
            list(hooks._router_registrars),
            list(hooks._model_modules),
            list(hooks._tags_metadata),
            dict(hooks._capabilities),
            list(hooks._installed_modules),
        )
    # The loader's cached state too: `reset()` leaves the process saying "no
    # modules loaded", and `modules_active()` -- what `GET /api/v1/modules`
    # and `/health/ready` report -- answers from that cache rather than
    # loading.  A later test in the same session would otherwise see the core
    # profile in a full checkout.
    with modules_loader._lock:
        saved_loader = (
            modules_loader._state,
            modules_loader._failure,
            modules_loader._routers,
            modules_loader._routers_failed,
        )
    hooks.reset()
    modules_loader.reset()
    try:
        yield
    finally:
        hooks.reset()
        with hooks._lock:
            hooks._router_registrars.extend(saved[0])
            hooks._model_modules.extend(saved[1])
            hooks._tags_metadata.extend(saved[2])
            hooks._capabilities.update(saved[3])
            hooks._installed_modules.extend(saved[4])
        hooks.set_audit_signer(saved_signer)
        modules_loader.reset()
        with modules_loader._lock:
            (
                modules_loader._state,
                modules_loader._failure,
                modules_loader._routers,
                modules_loader._routers_failed,
            ) = saved_loader


class TestRegistrationValidates:
    @pytest.mark.regression
    def test_a_placeholder_key_in_production_fails_the_registration_not_the_process(
        self, fresh_hooks, monkeypatch
    ):
        """``register(hooks)`` validates the settings first.  In production
        with the class-default audit key the registration fails; the loader
        records why and the process continues on the core profile (the
        schema builders then refuse through ``require_modules_or_absent``)."""
        from backend.app.core.config import settings as core

        monkeypatch.delenv("TESTING", raising=False)
        monkeypatch.setattr(core, "ENVIRONMENT", "production")
        monkeypatch.setattr(modules_settings, "_instance", None)
        try:
            assert modules_loader.load_modules(force=True) is False
            failure = modules_loader.modules_failure() or ""
            assert "AUDIT_HMAC_KEY" in failure
            assert hooks.capability_names() == ()
            with pytest.raises(RuntimeError, match="refusing to build a schema"):
                modules_loader.require_modules_or_absent()
        finally:
            monkeypatch.undo()
            modules_settings.load_modules_settings(force=True)

    @pytest.mark.regression
    def test_malformed_credential_keys_in_production_fail_the_registration(
        self, fresh_hooks, monkeypatch
    ):
        """``WAREHOUSE_CREDENTIALS_KEYS`` is optional, but a value that is not
        a list of Fernet keys stops a production full profile: the
        registration fails, naming the setting and not the value, and the
        bootstrap's ``require_modules_or_absent`` then refuses to start."""
        from backend.app.core.config import settings as core

        rejected = "SENTINEL-not-a-fernet-key-4b1d"
        monkeypatch.delenv("TESTING", raising=False)
        monkeypatch.setenv("AUDIT_HMAC_KEY", "c" * 64)
        monkeypatch.setenv("WAREHOUSE_CREDENTIALS_KEYS", rejected)
        monkeypatch.setattr(core, "ENVIRONMENT", "production")
        monkeypatch.setattr(modules_settings, "_instance", None)
        try:
            assert modules_loader.load_modules(force=True) is False
            failure = modules_loader.modules_failure() or ""
            assert "WAREHOUSE_CREDENTIALS_KEYS" in failure, failure
            assert rejected not in failure
            with pytest.raises(RuntimeError) as excinfo:
                modules_loader.require_modules_or_absent()
            assert rejected not in str(excinfo.value)
        finally:
            monkeypatch.undo()
            modules_settings.load_modules_settings(force=True)

    def test_registration_succeeds_under_test_settings(self, fresh_hooks):
        assert modules_loader.load_modules(force=True) is True
        assert modules_loader.modules_failure() is None
        assert set(hooks.capability_names()) == {
            "compliance.export",
            "compliance.report",
            "counters.service",
            "rbac.granted_permissions",
            "split_url.preview",
            "split_url.routing",
        }

    @pytest.mark.regression
    def test_a_rejected_audit_key_reaches_neither_the_log_nor_the_raised_error(
        self, fresh_hooks, monkeypatch
    ):
        """The loader built its failure string with ``{exc!r}``, and a pydantic
        ValidationError's repr embeds ``input_value='<the rejected key>'``.
        That string went into the ERROR log, into ``modules_failure()``, into
        the RuntimeError ``require_modules_or_absent()`` raises and from there
        into ``pytest.exit()`` and CI output.  (The loader's module logger is
        patched, and its calls read.)"""
        from unittest.mock import patch

        from backend.app.core.config import settings as core

        rejected = "too-short-and-very-secret"
        monkeypatch.delenv("TESTING", raising=False)
        monkeypatch.setenv("AUDIT_HMAC_KEY", rejected)
        monkeypatch.setattr(core, "ENVIRONMENT", "production")
        monkeypatch.setattr(modules_settings, "_instance", None)
        try:
            with patch.object(modules_loader, "logger") as log:
                assert modules_loader.load_modules(force=True) is False

            failure = modules_loader.modules_failure() or ""
            assert "AUDIT_HMAC_KEY" in failure, failure
            assert rejected not in failure

            assert log.error.called
            logged = repr(log.error.call_args)
            assert rejected not in logged
            assert "AUDIT_HMAC_KEY" in logged
            # No traceback either: its last line is str(exc).
            assert not log.error.call_args.kwargs.get("exc_info")
            assert not log.exception.called

            with pytest.raises(RuntimeError) as excinfo:
                modules_loader.require_modules_or_absent()
            assert rejected not in str(excinfo.value)
            assert "AUDIT_HMAC_KEY" in str(excinfo.value)
        finally:
            monkeypatch.undo()
            modules_settings.load_modules_settings(force=True)


class TestTheEnvFile:
    """The settings that moved off the core class are still read from the
    ``.env.*`` file the core class for that environment reads -- the mechanism
    ``docs/getting-started/environment-setup.md`` documents for production."""

    _KEY = "e" * 64

    def _write(self, directory, filename, body):
        (directory / filename).write_text(body)

    @pytest.mark.regression
    def test_a_production_deployment_reads_audit_hmac_key_from_env_prod(
        self, tmp_path, monkeypatch
    ):
        """``ModulesSettings`` declared no ``env_file`` at all, so every field
        that left the core class silently stopped being read from
        ``.env.prod`` and fell back to its dev default -- including the key
        that signs compliance audit events."""
        from backend.app.core.config import settings as core

        self._write(tmp_path, ".env.prod", f"AUDIT_HMAC_KEY={self._KEY}\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("AUDIT_HMAC_KEY", raising=False)
        monkeypatch.setattr(core, "ENVIRONMENT", "production")

        built = modules_settings.build_modules_settings()
        assert built.AUDIT_HMAC_KEY == self._KEY
        assert built.ENVIRONMENT == "production"

    def test_staging_reads_the_same_file_as_production(self, tmp_path, monkeypatch):
        from backend.app.core.config import settings as core

        self._write(tmp_path, ".env.prod", f"AUDIT_HMAC_KEY={self._KEY}\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("AUDIT_HMAC_KEY", raising=False)
        monkeypatch.setattr(core, "ENVIRONMENT", "staging")
        assert modules_settings.build_modules_settings().AUDIT_HMAC_KEY == self._KEY

    def test_development_reads_env_dev(self, tmp_path, monkeypatch):
        from backend.app.core.config import settings as core

        self._write(tmp_path, ".env.dev", "DYNAMODB_COUNTERS_TABLE=dev-counters\n")
        self._write(tmp_path, ".env.prod", "DYNAMODB_COUNTERS_TABLE=prod-counters\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("DYNAMODB_COUNTERS_TABLE", raising=False)
        monkeypatch.setattr(core, "ENVIRONMENT", "development")
        built = modules_settings.build_modules_settings()
        assert built.DYNAMODB_COUNTERS_TABLE == "dev-counters"

    def test_a_wrong_environment_file_is_not_read(self, tmp_path, monkeypatch):
        """Only the active environment's file: a stray ``.env.prod`` on a
        developer's box must not leak into a development process."""
        from backend.app.core.config import settings as core

        self._write(tmp_path, ".env.prod", f"AUDIT_HMAC_KEY={self._KEY}\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("AUDIT_HMAC_KEY", raising=False)
        monkeypatch.setattr(core, "ENVIRONMENT", "development")
        built = modules_settings.build_modules_settings()
        assert built.AUDIT_HMAC_KEY == "dev-audit-key-change-in-production"

    def test_the_process_environment_still_wins_over_the_file(
        self, tmp_path, monkeypatch
    ):
        from backend.app.core.config import settings as core

        self._write(tmp_path, ".env.prod", f"AUDIT_HMAC_KEY={self._KEY}\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AUDIT_HMAC_KEY", "f" * 64)
        monkeypatch.setattr(core, "ENVIRONMENT", "production")
        assert modules_settings.build_modules_settings().AUDIT_HMAC_KEY == "f" * 64

    def test_the_file_comes_from_the_one_shared_table(self):
        """No second copy of the environment/file mapping to drift."""
        from backend.app.core.config import (
            DevSettings,
            ProdSettings,
            TestSettings,
            env_file_for_environment,
        )

        assert (
            env_file_for_environment("development")
            == DevSettings.model_config["env_file"]
        )
        assert env_file_for_environment("test") == TestSettings.model_config["env_file"]
        assert (
            env_file_for_environment("production")
            == ProdSettings.model_config["env_file"]
        )


@pytest.mark.regression
def test_a_refused_modules_configuration_does_not_echo_the_audit_key():
    """The error names AUDIT_HMAC_KEY; it must not print the value it refused."""
    distinctive = "zq7-distinctive-input-4411"
    with pytest.raises(ValidationError) as refused:
        ModulesSettings(ENVIRONMENT="production", AUDIT_HMAC_KEY=distinctive)
    rendered = str(refused.value)
    assert "AUDIT_HMAC_KEY" in rendered
    assert "distinctive" not in rendered
    assert "input_value" not in rendered
