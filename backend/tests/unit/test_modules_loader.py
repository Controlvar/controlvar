"""`backend/app/modules_loader.py` — how the optional modules get into the process.

The loader's one promise is that the core API starts whatever the state of
the modules package. These tests pin that, plus what `modules.register(hooks)`
installs when the `modules` package is present (marked `modules`, so a core
build skips them).
"""

from __future__ import annotations

import types
from unittest.mock import patch

import pytest

from backend.app import modules_loader
from backend.app.core import hooks
from backend.app.core.config import settings as config_settings


def _effective_routes(router):
    """``(path, dependencies)`` for every route, through FastAPI >= 0.141's lazy
    ``_IncludedRouter`` entries (see ``test_wiring.py::_iter_http_routes``)."""
    from fastapi.routing import APIRoute

    for route in router.routes:
        if isinstance(route, APIRoute):
            yield route.path, route.dependencies
            continue
        contexts = getattr(route, "effective_route_contexts", None)
        if contexts is None:
            continue
        contexts = contexts() if callable(contexts) else contexts
        for ctx in contexts:
            yield ctx.path, getattr(ctx, "dependencies", []) or []


@pytest.fixture
def fresh_hooks():
    """Run each test against empty registries, then put the real ones back."""
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


@pytest.mark.modules
class TestModulesRegistration:
    def test_the_modules_package_registers_everything(self, fresh_hooks):
        """`modules/__init__.py` re-exports `register` from
        modules/backend/app/register.py; one call installs the signer, the
        nine model modules, the five capabilities, the tags and the ten
        module names."""
        assert modules_loader.load_modules(force=True) is True

        assert type(hooks.audit_signer).__name__ == "AuditSigningService"
        assert len(hooks.import_registered_models()) == 9
        assert set(hooks.capability_names()) == {
            "compliance.export",
            "compliance.report",
            "counters.service",
            "split_url.preview",
            "split_url.routing",
        }
        names = {tag["name"] for tag in hooks.extra_tags_metadata()}
        assert {"RBAC", "Workspaces", "HIPAA", "SSO", "Integrations"} <= names
        assert hooks.installed_modules() == hooks.KNOWN_MODULES

    @pytest.mark.regression
    def test_every_module_router_is_mounted_behind_authentication(self, fresh_hooks):
        """No module route without the user dependency, except the three
        public routers (webhooks, the SSO login flow, the invite preview):
        that is the gap the dashboard's gating exposed (it hid navigation the
        API still served)."""
        from fastapi import APIRouter

        from backend.app.api import deps

        modules_loader.load_modules(force=True)
        router = APIRouter()
        assert hooks.apply_routers(router) == 1

        expected = (
            "/rbac",
            "/counters",
            "/etl",
            "/integrations",
            "/auth/sso",
            "/workspaces",
            "/hipaa",
        )
        seen = {}
        unauthenticated = []
        for path, dependencies in _effective_routes(router):
            prefix = next((p for p in expected if path.startswith(p)), None)
            if prefix is None:
                continue
            seen[prefix] = True
            callables = {
                d.dependency
                for d in dependencies
                if getattr(d, "dependency", None) is not None
            }
            if deps.get_current_active_user not in callables:
                unauthenticated.append(path)
        assert set(seen) == set(expected), (
            f"routers not mounted: {set(expected) - set(seen)}"
        )
        # The public routes, by name. This used to be a list of *prefixes*
        # (`/auth/sso/oidc/`, `/workspaces/invites/`, ...), which let a new
        # route added under one of them go anonymous with the test still
        # green -- the opposite of what a guard against unauthenticated
        # module routes is for. Adding a route here is a deliberate act:
        # an anonymous caller will be able to reach it.
        PUBLIC = {
            # The inbound webhook routes authenticate the *sender* instead,
            # per modules/backend/app/services/integrations/webhook_auth.py.
            "/integrations/webhooks/github",
            "/integrations/webhooks/jira",
            "/integrations/webhooks/salesforce",
            # The SSO login flow: a browser with no session yet.
            "/auth/sso/saml/{config_id}/metadata",
            "/auth/sso/saml/{config_id}/acs",
            "/auth/sso/oidc/{provider}/login",
            "/auth/sso/oidc/{provider}/callback",
            # The dashboard's SSO sign-in (C2b): its start, and the exchange
            # whose credential is the hand-off code plus the tab's secret.
            "/auth/sso/login",
            "/auth/sso/exchange",
            # The invite preview: the token in the URL is the credential.
            "/workspaces/invites/{token}",
        }
        assert set(unauthenticated) == PUBLIC, (
            "the set of module routes an anonymous caller can reach has "
            f"changed: newly public {sorted(set(unauthenticated) - PUBLIC)}, "
            f"no longer mounted {sorted(PUBLIC - set(unauthenticated))}"
        )

    def test_loading_is_idempotent(self, fresh_hooks):
        assert modules_loader.load_modules(force=True) is True
        assert modules_loader.load_modules() is True
        assert modules_loader.load_modules(force=True) is True
        # Registries de-duplicate: one registrar, nine modules, no repeats.
        with hooks._lock:
            assert len(hooks._router_registrars) == 1
            assert len(hooks._model_modules) == 9
        assert len(hooks.installed_modules()) == len(hooks.KNOWN_MODULES)


class TestNeverRaises:
    @pytest.mark.regression
    def test_a_register_that_blows_up_leaves_the_core_running(self, fresh_hooks):
        broken = types.SimpleNamespace(
            register=lambda hooks_: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        with patch.object(
            modules_loader, "_find_register", return_value=(broken.register, "x")
        ):
            assert modules_loader.load_modules(force=True) is False
        assert type(hooks.audit_signer).__name__ == "NullAuditSigner"
        assert hooks.capability_names() == ()
        assert hooks.installed_modules() == ()

    @pytest.mark.regression
    def test_a_model_module_that_fails_to_import_rolls_everything_back(
        self, fresh_hooks
    ):
        """A router registered before a model module failed would serve routes
        against tables that do not exist. The loader wipes the half-applied
        registration and reports the core profile."""

        def half_register(h):
            h.register_router(lambda router: None)
            h.register_capability("split_url.routing", lambda: None)
            h.register_modules(["split_url"])
            h.register_model_module("backend.app.models.no_such_module")

        with patch.object(
            modules_loader, "_find_register", return_value=(half_register, "x")
        ):
            assert modules_loader.load_modules(force=True) is False

        with hooks._lock:
            assert hooks._router_registrars == []
            assert hooks._model_modules == []
        assert hooks.capability_names() == ()
        assert hooks.installed_modules() == ()

    @pytest.mark.regression
    def test_a_register_claiming_an_unknown_module_name_is_a_failure(self, fresh_hooks):
        """A typo in the names a registration claims must not become a module
        the dashboard never shows: `register_modules` refuses it, the loader
        records the failure, and the schema builders refuse to build."""

        def typo(h):
            h.register_modules(["workspace"])  # singular; the real name is plural

        with patch.object(modules_loader, "_find_register", return_value=(typo, "x")):
            assert modules_loader.load_modules(force=True) is False
        failure = modules_loader.modules_failure() or ""
        assert "unknown module name" in failure
        assert "workspaces" in failure
        assert hooks.installed_modules() == ()

    @pytest.mark.regression
    def test_a_modules_package_that_fails_to_import_leaves_the_core_running(
        self, fresh_hooks, monkeypatch
    ):
        """`_find_register` caught only ImportError around `import modules`, so
        a SyntaxError or a RuntimeError from a missing optional dependency
        propagated out of `load_modules()` and killed `import
        backend.app.main`. Reproduced with a meta-path finder that raises."""
        import sys

        class Exploding:
            def find_spec(self, name, path=None, target=None):
                if name == modules_loader.MODULES_PACKAGE:
                    raise RuntimeError("optional dependency missing")
                return

        monkeypatch.setattr(sys, "meta_path", [Exploding(), *sys.meta_path])
        monkeypatch.delitem(sys.modules, modules_loader.MODULES_PACKAGE, raising=False)
        # Nothing propagated -- and a package that is present but broken is
        # reported as such, not replaced by the in-tree code.
        assert modules_loader.load_modules(force=True) is False
        assert modules_loader.modules_failure() is not None

    @pytest.mark.regression
    def test_a_nested_missing_module_is_reported_not_mistaken_for_the_core_profile(
        self, fresh_hooks, monkeypatch
    ):
        """`ModuleNotFoundError` for a module *inside* the package is a broken
        install, logged at ERROR with the module named -- not "no modules
        package" at DEBUG. (The module logger is patched, and its calls read.)"""
        import sys

        real_import = modules_loader.importlib.import_module

        def bad_import(name, package=None):
            if name == modules_loader.MODULES_PACKAGE:
                raise ModuleNotFoundError(
                    "No module named 'snowflake'", name="snowflake"
                )
            return real_import(name, package)

        monkeypatch.setattr(modules_loader.importlib, "import_module", bad_import)
        monkeypatch.delitem(sys.modules, modules_loader.MODULES_PACKAGE, raising=False)
        with patch.object(modules_loader, "logger") as log:
            modules_loader._find_register()
        assert log.error.called
        assert "snowflake" in str(log.error.call_args)
        assert not log.debug.called

    @pytest.mark.regression
    def test_a_broken_real_package_is_reported_not_replaced(
        self, fresh_hooks, monkeypatch
    ):
        """When `import modules` raises, the loader records the failure and
        runs the core profile. (It once logged the error and then loaded an
        in-tree fallback anyway -- two contradictory log lines, and code
        silently serving in place of the package the operator installed.)"""
        import sys

        real_import = modules_loader.importlib.import_module

        def bad_import(name, package=None):
            if name == modules_loader.MODULES_PACKAGE:
                raise RuntimeError("SyntaxError in disguise")
            return real_import(name, package)

        monkeypatch.setattr(modules_loader.importlib, "import_module", bad_import)
        monkeypatch.delitem(sys.modules, modules_loader.MODULES_PACKAGE, raising=False)
        assert modules_loader.load_modules(force=True) is False
        assert hooks.capability_names() == ()
        assert "could not be imported" in (modules_loader.modules_failure() or "")

    @pytest.mark.regression
    def test_schema_builders_refuse_a_half_registered_package(self, fresh_hooks):
        """alembic env.py, bootstrap and conftest go through
        require_modules_or_absent(): with the registration broken the
        metadata would hold only the core tables, and autogenerate would
        propose dropping every module table."""

        def broken(h):
            # Never imported: the registration raises before the loader gets
            # to import_registered_models(), so any module path will do.
            h.register_model_module("backend.app.models.no_such_module")
            raise RuntimeError("boom")

        with patch.object(modules_loader, "_find_register", return_value=(broken, "x")):
            with pytest.raises(RuntimeError) as excinfo:
                modules_loader.require_modules_or_absent()
        assert "refusing to build a schema" in str(excinfo.value)
        assert "boom" in str(excinfo.value)

    def test_schema_builders_accept_a_core_tree(self, fresh_hooks):
        with patch.object(modules_loader, "_find_register", return_value=(None, None)):
            assert modules_loader.require_modules_or_absent() is False

    def test_the_entry_point_may_live_in_the_application_module(
        self, fresh_hooks, monkeypatch
    ):
        """#89 places register() in modules/backend/app/register.py; the loader
        must find it there when the top-level package does not re-export it."""
        import sys
        import types

        top = types.ModuleType(modules_loader.MODULES_PACKAGE)
        top.__path__ = []  # a package, but with no register attribute
        app_module = types.ModuleType(
            f"{modules_loader.MODULES_PACKAGE}.backend.app.register"
        )
        seen = []
        app_module.register = lambda h: seen.append(h)
        real_import = modules_loader.importlib.import_module

        def fake_import(name, package=None):
            if name == modules_loader.MODULES_PACKAGE:
                return top
            if name == f"{modules_loader.MODULES_PACKAGE}.backend.app.register":
                return app_module
            return real_import(name, package)

        monkeypatch.setattr(modules_loader.importlib, "import_module", fake_import)
        register, origin = modules_loader._find_register()
        assert origin == f"{modules_loader.MODULES_PACKAGE}.backend.app.register"
        register(hooks)
        assert seen == [hooks]

    @staticmethod
    def _package_with_code_but_no_entry_point(monkeypatch, tmp_path):
        """Install a fake ``modules`` package: real .py files, no ``register``.

        This is what a one-character typo in the entry point's name
        (``def regsiter(hooks)``) looks like to the loader.
        """
        import sys

        (tmp_path / "something.py").write_text("VALUE = 1\n")
        top = types.ModuleType(modules_loader.MODULES_PACKAGE)
        top.__path__ = [str(tmp_path)]
        real_import = modules_loader.importlib.import_module
        application = f"{modules_loader.MODULES_PACKAGE}.backend.app.register"

        def fake_import(name, package=None):
            if name == modules_loader.MODULES_PACKAGE:
                return top
            if name == application:
                raise ModuleNotFoundError(f"No module named {name!r}", name=name)
            return real_import(name, package)

        monkeypatch.setattr(modules_loader.importlib, "import_module", fake_import)
        monkeypatch.delitem(sys.modules, modules_loader.MODULES_PACKAGE, raising=False)

    @pytest.mark.regression
    def test_module_code_with_no_entry_point_is_a_failure_not_a_core_tree(
        self, fresh_hooks, monkeypatch, tmp_path
    ):
        """It used to be a WARNING and nothing else.

        ``_failure`` stayed None, so ``require_modules_or_absent()`` answered
        "core tree" and ``abort_if_modules_broken()`` returned early even in
        production: a full-profile deployment ran the core profile in silence,
        autogenerate proposed dropping every module table, a bootstrap built a
        core-only schema, and compliance events were written unsigned.
        """
        self._package_with_code_but_no_entry_point(monkeypatch, tmp_path)

        assert modules_loader.load_modules(force=True) is False
        failure = modules_loader.modules_failure()
        assert failure is not None
        assert "register(hooks)" in failure

    @pytest.mark.regression
    def test_a_schema_builder_refuses_a_package_with_no_entry_point(
        self, fresh_hooks, monkeypatch, tmp_path
    ):
        self._package_with_code_but_no_entry_point(monkeypatch, tmp_path)
        with pytest.raises(RuntimeError) as excinfo:
            modules_loader.require_modules_or_absent()
        assert "refusing to build a schema" in str(excinfo.value)

    @pytest.mark.regression
    def test_the_api_refuses_a_package_with_no_entry_point_in_production(
        self, fresh_hooks, monkeypatch, tmp_path
    ):
        self._package_with_code_but_no_entry_point(monkeypatch, tmp_path)
        monkeypatch.setattr(config_settings, "ENVIRONMENT", "production")
        assert modules_loader.load_modules(force=True) is False
        with pytest.raises(RuntimeError, match="Refusing to start"):
            modules_loader.abort_if_modules_broken()

    def test_a_bare_modules_directory_is_still_the_core_profile(
        self, fresh_hooks, monkeypatch, tmp_path
    ):
        """No ``.py`` file anywhere under it: a core checkout that kept an
        empty directory, which is a clean core start everywhere."""
        import sys

        (tmp_path / "notes.txt").write_text("not python\n")
        top = types.ModuleType(modules_loader.MODULES_PACKAGE)
        top.__path__ = [str(tmp_path)]
        real_import = modules_loader.importlib.import_module
        application = f"{modules_loader.MODULES_PACKAGE}.backend.app.register"

        def fake_import(name, package=None):
            if name == modules_loader.MODULES_PACKAGE:
                return top
            if name == application:
                raise ModuleNotFoundError(f"No module named {name!r}", name=name)
            return real_import(name, package)

        monkeypatch.setattr(modules_loader.importlib, "import_module", fake_import)
        monkeypatch.delitem(sys.modules, modules_loader.MODULES_PACKAGE, raising=False)
        monkeypatch.setattr(config_settings, "ENVIRONMENT", "production")

        assert modules_loader.load_modules(force=True) is False
        assert modules_loader.modules_failure() is None
        assert modules_loader.require_modules_or_absent() is False
        assert modules_loader.abort_if_modules_broken() is None

    def test_nothing_to_load_is_the_core_profile(self, fresh_hooks):
        with patch.object(modules_loader, "_find_register", return_value=(None, None)):
            assert modules_loader.load_modules(force=True) is False
        assert hooks.capability_names() == ()
        assert hooks.installed_modules() == ()

    def test_the_loader_logs_the_profile_it_runs(self, fresh_hooks):
        """The two start-up lines an operator reads to learn the profile."""
        with (
            patch.object(modules_loader, "_find_register", return_value=(None, None)),
            patch.object(modules_loader, "logger") as log,
        ):
            modules_loader.load_modules(force=True)
        assert log.info.call_args.args == ("Running the core profile",)

        with (
            patch.object(
                modules_loader,
                "_find_register",
                return_value=(lambda h: None, "modules"),
            ),
            patch.object(modules_loader, "logger") as log,
        ):
            modules_loader.load_modules(force=True)
        assert log.info.call_args.args == ("Modules loaded (from %s)", "modules")


class TestCrossProfileForeignKeys:
    """The workspace foreign keys exist exactly when the module models do."""

    @pytest.mark.modules
    @pytest.mark.regression
    def test_full_metadata_carries_the_workspace_foreign_keys(self):
        """Migration a7b8c9d0e1f2 dropped them from the core models and
        nothing re-added them for the full profile, so a fresh full-profile
        bootstrap lost referential integrity and ON DELETE SET NULL. The
        workspace model module attaches them (use_alter) whenever it is
        loaded."""
        from sqlalchemy import ForeignKeyConstraint

        from backend.app.models.experiment import Experiment
        from backend.app.models.feature_flag import FeatureFlag

        modules_loader.load_modules()
        for model, name in (
            (Experiment, "experiments_workspace_id_fkey"),
            (FeatureFlag, "feature_flags_workspace_id_fkey"),
        ):
            fks = [
                c
                for c in model.__table__.constraints
                if isinstance(c, ForeignKeyConstraint) and c.name == name
            ]
            assert len(fks) == 1, f"{name} attached {len(fks)} times"
            assert fks[0].ondelete == "SET NULL"
            assert fks[0].use_alter is True
            assert list(fks[0].column_keys) == ["workspace_id"]

    def test_the_core_registry_has_no_such_constraint(self):
        """Checked in a fresh interpreter: this process has the module models
        loaded, so it cannot answer the core question itself."""
        from backend.tests.smoke.test_core_model_registry import (
            _run_in_fresh_interpreter,
        )

        result = _run_in_fresh_interpreter(
            """
            import json
            from backend.app.models import register_core_models
            from backend.app.models.experiment import Experiment
            register_core_models()
            names = sorted(c.name for c in Experiment.__table__.constraints if c.name)
            print(json.dumps({"constraints": names}))
            """
        )
        assert "experiments_workspace_id_fkey" not in result["constraints"]


# ---------------------------------------------------------------------------
# A failure description that names the setting but never its value
# ---------------------------------------------------------------------------

_REJECTED_KEY = "too-short-and-very-secret"


def _real_settings_validation_error():
    """A genuine settings ValidationError, rejected secret and all.

    Built from the *core* ``Settings`` -- this file may not import the modules
    package (``test_core_boundary.py``) -- but it is the same shape the
    modules' AUDIT_HMAC_KEY validator produces, and the end-to-end case goes
    through the loader in
    ``modules/backend/tests/unit/test_modules_settings.py``.
    """
    from pydantic import ValidationError

    from backend.app.core.config import Settings

    try:
        Settings(_env_file=None, ENVIRONMENT="production", SECRET_KEY=_REJECTED_KEY)
    except ValidationError as exc:
        return exc
    raise AssertionError("Settings accepted a short SECRET_KEY in production")


class TestFailureDescriptionsCarryNoSecret:
    """`modules.register(hooks)` validates the settings first, so the likeliest
    failure here holds a rejected secret."""

    def test_the_settings_put_the_rejected_value_in_no_rendering(self, monkeypatch):
        """pydantic puts the input in all three renderings unless the model sets
        ``hide_input_in_errors``, which the settings now do. The redaction
        below stays, for a model that does not."""
        import traceback

        monkeypatch.delenv("TESTING", raising=False)
        exc = _real_settings_validation_error()
        assert _REJECTED_KEY not in repr(exc)
        assert _REJECTED_KEY not in str(exc)
        assert _REJECTED_KEY not in "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )

    def test_describe_exception_names_the_field_and_drops_the_value(self, monkeypatch):
        monkeypatch.delenv("TESTING", raising=False)
        described = modules_loader.describe_exception(_real_settings_validation_error())
        assert _REJECTED_KEY not in described
        assert "SECRET_KEY" in described
        assert "ValidationError" in described
        assert "at least" in described

    @pytest.mark.regression
    def test_a_model_level_refusal_is_named_by_its_setting_not_a_placeholder(
        self, monkeypatch
    ):
        """#237: the PUBLIC_BASE_URL/ALLOWED_HOSTS check is a model validator,
        so pydantic gives it no field location, and it was reported as
        ``<value>: Value error, This deployment does not know ...``."""
        from pydantic import ValidationError

        from backend.app.core.config import Settings

        monkeypatch.delenv("TESTING", raising=False)
        with pytest.raises(ValidationError) as caught:
            Settings(
                _env_file=None,
                ENVIRONMENT="production",
                SECRET_KEY="k" * 64,
                FIRST_SUPERUSER_PASSWORD="a-long-enough-password",
            )
        described = modules_loader.describe_exception(caught.value)
        assert "<value>" not in described
        assert "Value error," not in described
        assert described.startswith("ValidationError: PUBLIC_BASE_URL is not set")
        assert "https://controlvar.example.com" in described
        assert "api.example.com" not in described

    @pytest.mark.regression
    def test_a_refused_setting_is_not_reported_as_continuing_on_the_core_profile(
        self, fresh_hooks, monkeypatch
    ):
        """#237: a refused secret was logged as "register(hooks) failed;
        continuing on the core profile" -- false on a production full image,
        which refuses to start straight after."""
        monkeypatch.delenv("TESTING", raising=False)
        error = _real_settings_validation_error()

        def refuses(h):
            raise error

        with (
            patch.object(modules_loader, "_find_register", return_value=(refuses, "x")),
            patch.object(modules_loader.logger, "error") as log,
        ):
            assert modules_loader.load_modules(force=True) is False
        (call,) = log.call_args_list
        line = call.args[0] % call.args[1:]
        assert "continuing on the core profile" not in line
        assert "SECRET_KEY" in line
        assert _REJECTED_KEY not in line
        assert not call.kwargs.get("exc_info")
        assert "SECRET_KEY" in (modules_loader.modules_failure() or "")

    def test_describe_exception_keeps_ordinary_exceptions_readable(self):
        described = modules_loader.describe_exception(ImportError("no module 'xmlsec'"))
        assert described == "ImportError: no module 'xmlsec'"
        assert modules_loader.describe_exception(RuntimeError()) == "RuntimeError"

    def test_a_traceback_is_kept_for_an_import_error_and_dropped_for_a_secret(
        self, monkeypatch
    ):
        monkeypatch.delenv("TESTING", raising=False)
        assert modules_loader._traceback_is_safe(ImportError("boom")) is True
        assert (
            modules_loader._traceback_is_safe(_real_settings_validation_error())
            is False
        )

    def test_a_chained_validation_error_also_suppresses_the_traceback(
        self, monkeypatch
    ):
        """A formatted traceback prints the whole __cause__/__context__ chain,
        so a ValidationError re-raised as something else would print its
        rejected value under a "direct cause" banner."""
        import traceback

        monkeypatch.delenv("TESTING", raising=False)
        try:
            raise RuntimeError("registration failed") from (
                _real_settings_validation_error()
            )
        except RuntimeError as wrapper:
            # The settings hide their input now; the redaction is for a model
            # that does not.
            assert _REJECTED_KEY not in "".join(
                traceback.format_exception(
                    type(wrapper), wrapper, wrapper.__traceback__
                )
            )
            assert modules_loader._traceback_is_safe(wrapper) is False
            assert _REJECTED_KEY not in modules_loader.describe_exception(wrapper)


# ---------------------------------------------------------------------------
# A broken modules package must not start a full-profile deployment
# ---------------------------------------------------------------------------


class TestBrokenModulesAbortStartup:
    """`abort_if_modules_broken()` — what `main.py` does with a failure."""

    def test_a_clean_load_is_never_an_abort(self, monkeypatch):
        monkeypatch.setattr(modules_loader, "_failure", None)
        for env in ("development", "test", "staging", "production"):
            monkeypatch.setattr(config_settings, "ENVIRONMENT", env)
            assert modules_loader.abort_if_modules_broken() is None

    def test_an_absent_package_starts_the_core_profile_everywhere(
        self, fresh_hooks, monkeypatch
    ):
        """No `modules/` is not a failure: `_failure` stays None and every
        environment gets a clean core start.

        (`fresh_hooks` because this one loads: without its `reset()` the
        cached `_state=False` would outlive the test and every later reader of
        `load_modules()` -- `GET /api/v1/modules`, the health probe -- would
        report the core profile.)"""
        monkeypatch.setattr(config_settings, "ENVIRONMENT", "production")
        with patch.object(modules_loader, "_find_register", return_value=(None, None)):
            assert modules_loader.load_modules(force=True) is False
        assert modules_loader.modules_failure() is None
        assert modules_loader.abort_if_modules_broken() is None

    @pytest.mark.parametrize("env", ["development", "test"])
    def test_a_failure_is_tolerated_in_development_and_test(self, env, monkeypatch):
        monkeypatch.setattr(modules_loader, "_failure", "register(hooks) raised boom")
        monkeypatch.setattr(config_settings, "ENVIRONMENT", env)
        with patch.object(modules_loader, "logger") as log:
            assert modules_loader.abort_if_modules_broken() is None
        assert log.error.called

    @pytest.mark.parametrize("env", ["staging", "production"])
    @pytest.mark.regression
    def test_a_failure_refuses_to_start_outside_development(self, env, monkeypatch):
        """A registration failure used to be logged and nothing more: the API
        came up on the core profile with a NullAuditSigner, every module route
        404ing and compliance events written with a NULL signature that
        `verify()` then called intact -- while `/health/ready` stayed green.
        The same environment refused to start before the settings moved off
        the core class (issue #91)."""
        monkeypatch.setattr(
            modules_loader, "_failure", "modules.register(hooks) raised boom"
        )
        monkeypatch.setattr(config_settings, "ENVIRONMENT", env)
        with pytest.raises(RuntimeError) as excinfo:
            modules_loader.abort_if_modules_broken()
        message = str(excinfo.value)
        assert "Refusing to start" in message
        assert "boom" in message
        assert env in message

    def test_main_calls_it_when_the_modules_did_not_load(self):
        """`main.py` is the caller: the branch must be wired, not just exist."""
        import inspect

        from backend.app import main

        source = inspect.getsource(main)
        assert "abort_if_modules_broken()" in source
        assert main.abort_if_modules_broken is modules_loader.abort_if_modules_broken


# ---------------------------------------------------------------------------
# A schema builder does not need the API graph
# ---------------------------------------------------------------------------


@pytest.mark.modules
class TestSchemaOnlyRegistration:
    """`require_modules_or_absent()` asks for a registration without routers.

    Importing the endpoint modules is ~99% of a registration's cost and
    buys a schema tool nothing -- it pulls in FastAPI routers and client
    libraries that no ``Base.metadata`` and no migration touches.  Every
    container start paid it inside the bootstrap's advisory lock, and so did
    every ``alembic upgrade``/``current``/``revision``.
    """

    def test_a_schema_builder_gets_the_models_and_the_signer_but_no_routers(
        self, fresh_hooks
    ):
        from backend.app.models import register_core_models

        Base = register_core_models()
        before = len(Base.metadata.tables)

        assert modules_loader.require_modules_or_absent() is True

        # Everything that decides what the schema is.
        assert len(hooks.import_registered_models()) == 9
        assert len(Base.metadata.tables) >= before
        assert type(hooks.audit_signer).__name__ == "AuditSigningService"
        # And nothing that only a router needs.
        assert hooks.capability_names() == ()
        assert hooks._router_registrars == []

    @pytest.mark.regression
    def test_asking_for_routers_afterwards_still_mounts_them(self, fresh_hooks):
        """The cached "loaded" result used to be enough to skip the second
        call; with the schema-only load that would leave the API serving no
        module route at all."""
        assert modules_loader.require_modules_or_absent() is True
        assert hooks._router_registrars == []

        assert modules_loader.load_modules() is True

        assert len(hooks._router_registrars) == 1
        assert len(hooks.capability_names()) == 5
        assert len(hooks.installed_modules()) == 10

    def test_a_router_load_satisfies_a_later_schema_builder(self, fresh_hooks):
        """The other order: nothing is re-run, because everything is there."""
        assert modules_loader.load_modules(force=True) is True
        registrars = list(hooks._router_registrars)

        assert modules_loader.require_modules_or_absent() is True
        assert hooks._router_registrars == registrars


class TestPassingWithRoutersThrough:
    """The keyword is an optimisation the loader offers, not a requirement."""

    def test_a_registration_that_accepts_it_is_told(self, fresh_hooks):
        seen = []

        def register(_hooks, with_routers=True):
            seen.append(with_routers)

        with patch.object(
            modules_loader, "_find_register", return_value=(register, "x")
        ):
            modules_loader.load_modules(force=True, with_routers=False)
            modules_loader.load_modules(force=True, with_routers=True)
        assert seen == [False, True]

    def test_a_registration_with_the_old_signature_is_called_plainly(self, fresh_hooks):
        """`register(hooks)` is the documented entry point; a modules package
        written against it must still work."""
        seen = []

        def register(_hooks):
            seen.append("called")

        with patch.object(
            modules_loader, "_find_register", return_value=(register, "x")
        ):
            assert modules_loader.load_modules(force=True, with_routers=False) is True
        assert seen == ["called"]

    def test_kwargs_count_as_accepting_it(self, fresh_hooks):
        seen = []

        def register(_hooks, **kwargs):
            seen.append(kwargs)

        with patch.object(
            modules_loader, "_find_register", return_value=(register, "x")
        ):
            modules_loader.load_modules(force=True, with_routers=False)
        assert seen == [{"with_routers": False}]

    def test_a_failed_registration_is_not_retried_by_a_later_caller(self, fresh_hooks):
        """Only a *successful* schema-only load is completed later; a failure
        and an absent package answer from the cache exactly as before."""
        calls = []

        def broken(_hooks):
            calls.append("called")
            raise RuntimeError("boom")

        with patch.object(modules_loader, "_find_register", return_value=(broken, "x")):
            assert modules_loader.load_modules(force=True, with_routers=False) is False
            assert modules_loader.load_modules() is False
        assert calls == ["called"]

    def test_an_absent_package_is_not_retried_by_a_later_caller(self, fresh_hooks):
        calls = []

        def find():
            calls.append("looked")
            return (None, None)

        with patch.object(modules_loader, "_find_register", side_effect=find):
            assert modules_loader.load_modules(force=True, with_routers=False) is False
            assert modules_loader.load_modules() is False
        assert calls == ["looked"]


# ---------------------------------------------------------------------------
# Mounting the routers is inside the same guard as loading them
# ---------------------------------------------------------------------------


class TestMountingIsInsideTheGuard:
    """`hooks.apply_routers` runs module code.

    It used to run from `build_v1_router()` *outside* the loader's try/except,
    so every way it can fail -- an endpoint module with no `public_router`, a
    `modules[...]` KeyError when `ENDPOINT_MODULES` and `mount_routers` drift
    apart, a router FastAPI refuses -- came out of `import
    backend.app.api.api`, killed `import backend.app.main`, and uvicorn never
    bound a port. A broken modules package costs the deployment its module
    routes, not its API.
    """

    @pytest.mark.regression
    def test_a_registrar_that_raises_is_a_failure_not_an_exception(self, fresh_hooks):
        from fastapi import APIRouter

        signer = hooks.audit_signer

        def explode(_router):
            raise AttributeError(
                "module 'modules.backend.app.api.v1.endpoints.sso' has no "
                "attribute 'public_router'"
            )

        hooks.register_router(explode)
        router = APIRouter()

        assert modules_loader.mount_module_routers(router) == 0
        assert list(_effective_routes(router)) == []
        failure = modules_loader.modules_failure() or ""
        assert "public_router" in failure
        # The registration itself succeeded; mounting is not a reason to take
        # its audit signer away (see TestASecondRunNeverDowngradesTheFirst).
        assert hooks.audit_signer is signer

    @pytest.mark.regression
    def test_the_v1_router_still_builds(self, fresh_hooks):
        """The path that mattered: `import backend.app.api.api` raised."""
        from backend.app.api import api

        hooks.register_router(lambda _router: (_ for _ in ()).throw(KeyError("etl")))

        router = api.build_v1_router()

        paths = {path for path, _ in _effective_routes(router)}
        assert any(path.startswith("/experiments") for path in paths)
        assert modules_loader.modules_failure() is not None

    @pytest.mark.regression
    def test_a_registrar_that_fails_halfway_mounts_nothing(self, fresh_hooks):
        """All or nothing: `mount_routers` mounts eleven routers one by one,
        and a v1 router carrying the first six of them is not a profile."""
        from fastapi import APIRouter

        mounted = APIRouter()

        @mounted.get("/thing")
        def _thing():  # pragma: no cover - never called
            return {}

        def half(router):
            router.include_router(mounted, prefix="/first")
            raise RuntimeError("the seventh router")

        hooks.register_router(half)
        destination = APIRouter()

        assert modules_loader.mount_module_routers(destination) == 0
        assert list(_effective_routes(destination)) == []

    def test_what_mounts_keeps_its_path_and_dependencies(self, fresh_hooks):
        """The staging router costs one nesting level and nothing else."""
        from fastapi import APIRouter, Depends

        def guard():  # pragma: no cover - never called
            return None

        module_router = APIRouter()

        @module_router.get("/ping")
        def _ping():  # pragma: no cover - never called
            return {}

        hooks.register_router(
            lambda router: router.include_router(
                module_router,
                prefix="/demo",
                tags=["Demo"],
                dependencies=[Depends(guard)],
            )
        )
        destination = APIRouter()

        assert modules_loader.mount_module_routers(destination) == 1
        routes = list(_effective_routes(destination))
        assert [path for path, _ in routes] == ["/demo/ping"]
        assert guard in {
            dependency.dependency for _path, deps in routes for dependency in deps
        }

    def test_the_core_profile_mounts_nothing_and_says_so(self, fresh_hooks):
        from fastapi import APIRouter

        destination = APIRouter()
        assert modules_loader.mount_module_routers(destination) == 0
        assert list(destination.routes) == []
        assert modules_loader.modules_failure() is None

    @pytest.mark.regression
    def test_main_refuses_to_start_on_a_mount_failure_outside_development(
        self, fresh_hooks, monkeypatch
    ):
        """`main.py` used to read the load's return value, which a mount
        failure leaves True; it reads `modules_failure()` now."""
        from fastapi import APIRouter

        hooks.register_router(
            lambda _router: (_ for _ in ()).throw(RuntimeError("no public_router"))
        )
        modules_loader.mount_module_routers(APIRouter())

        monkeypatch.setattr(config_settings, "ENVIRONMENT", "production")
        with pytest.raises(RuntimeError, match="Refusing to start"):
            modules_loader.abort_if_modules_broken()

    @pytest.mark.regression
    def test_main_aborts_on_the_failure_not_on_the_return_value(self):
        """The call must be unconditional: a mount failure, and a failed
        routers-only upgrade, both leave `load_modules()` True."""
        import inspect

        from backend.app import main

        source = inspect.getsource(main)
        assert "\nabort_if_modules_broken()" in source


# ---------------------------------------------------------------------------
# A second registration never downgrades the first
# ---------------------------------------------------------------------------


class TestASecondRunNeverDowngradesTheFirst:
    """A schema-only load followed by a router load runs the registration
    twice (`load_modules`). The second run is an upgrade: it can add the
    routers, and it may not take away what the first one installed.

    It used to run like a first load, so its failure called `hooks.reset()`:
    a process that had been signing compliance events with a real HMAC key
    silently fell back to `NullAuditSigner`, later events were written with a
    NULL signature that `verify()` calls intact, and the rows signed *earlier*
    began to verify False -- a false tamper alarm in
    `compliance_report_service.py`.
    """

    @staticmethod
    def _registration(fail_on_routers=True):
        calls = []
        signer = object()

        def register(hooks_, with_routers=True):
            calls.append(with_routers)
            # The real one: settings, model modules, signer, then the eleven
            # endpoint modules.  Everything before the routers is idempotent.
            hooks_.register_model_module("backend.app.models.user")
            hooks_.set_audit_signer(signer)
            if with_routers and fail_on_routers:
                raise RuntimeError("endpoint module exploded")

        return register, calls, signer

    @pytest.mark.regression
    def test_a_failed_upgrade_keeps_the_signer_the_models_and_the_state(
        self, fresh_hooks
    ):
        register, calls, signer = self._registration()

        with patch.object(
            modules_loader, "_find_register", return_value=(register, "x")
        ):
            assert modules_loader.load_modules(force=True, with_routers=False) is True
            assert hooks.audit_signer is signer

            assert modules_loader.load_modules() is True

        assert calls == [False, True]
        assert hooks.audit_signer is signer
        with hooks._lock:
            assert hooks._model_modules == ["backend.app.models.user"]
        # Reported, though: the routes the registration promised are missing.
        assert "endpoint module exploded" in (modules_loader.modules_failure() or "")

    @pytest.mark.regression
    def test_a_failed_upgrade_is_not_tried_again(self, fresh_hooks):
        """Otherwise every later `load_modules()` pays the endpoint imports
        (~3.5 s) again, to fail again."""
        register, calls, _signer = self._registration()

        with patch.object(
            modules_loader, "_find_register", return_value=(register, "x")
        ):
            modules_loader.load_modules(force=True, with_routers=False)
            modules_loader.load_modules()
            modules_loader.load_modules()
            modules_loader.load_modules()

        assert calls == [False, True]

    def test_a_successful_upgrade_still_mounts_the_routers(self, fresh_hooks):
        register, calls, _signer = self._registration(fail_on_routers=False)

        with patch.object(
            modules_loader, "_find_register", return_value=(register, "x")
        ):
            assert modules_loader.load_modules(force=True, with_routers=False) is True
            assert modules_loader.load_modules() is True
            assert modules_loader.load_modules() is True

        assert calls == [False, True]
        assert modules_loader.modules_failure() is None

    @pytest.mark.regression
    def test_a_first_load_that_fails_still_rolls_everything_back(self, fresh_hooks):
        """The other half of the contract, unchanged: with nothing already
        installed there is nothing to protect, and a half-applied registration
        must not leave a router mounted against tables that do not exist."""

        def register(hooks_, with_routers=True):
            hooks_.register_model_module("backend.app.models.user")
            hooks_.set_audit_signer(object())
            raise RuntimeError("boom")

        with patch.object(
            modules_loader, "_find_register", return_value=(register, "x")
        ):
            assert modules_loader.load_modules(force=True) is False

        assert type(hooks.audit_signer).__name__ == "NullAuditSigner"
        with hooks._lock:
            assert hooks._model_modules == []


# ---------------------------------------------------------------------------
# Reporting the profile never registers anything
# ---------------------------------------------------------------------------


class TestReportingTheProfileNeverRegisters:
    """`GET /api/v1/modules` and `/health/ready` report the profile of a
    running process. Both called `load_modules()`, so either -- both
    unauthenticated -- could make a process that had loaded for a *schema*
    re-run the whole registration: the eleven endpoint modules, ~3.5 s, on the
    event loop, under this module's lock.
    """

    @pytest.mark.regression
    def test_modules_active_answers_from_the_cache(self, fresh_hooks):
        calls = []

        def register(_hooks, with_routers=True):
            calls.append(with_routers)

        with patch.object(
            modules_loader, "_find_register", return_value=(register, "x")
        ):
            # Nothing loaded yet: "core", and no registration triggered.
            assert modules_loader.modules_active() is False
            assert calls == []

            modules_loader.load_modules(force=True, with_routers=False)
            assert modules_loader.modules_active() is True
            # A `load_modules()` here would run the registration again.
            assert calls == [False]

    def test_modules_active_is_false_for_a_failed_load(self, fresh_hooks):
        def register(_hooks, with_routers=True):
            raise RuntimeError("boom")

        with patch.object(
            modules_loader, "_find_register", return_value=(register, "x")
        ):
            assert modules_loader.load_modules(force=True) is False
        assert modules_loader.modules_active() is False


# ---------------------------------------------------------------------------
# Every failure branch logs under the same guard
# ---------------------------------------------------------------------------


class TestEveryBranchHonoursTheTracebackGuard:
    """Two of the five branches used `logger.exception(...)`, which is
    `exc_info=True` unconditionally.

    A ModuleNotFoundError whose `__cause__`/`__context__` chain holds a
    settings ValidationError is easy to produce -- `modules.backend.app
    .settings` builds and validates on attribute access -- and printed the
    rejected secret into the start-up log, and under the JSON logger into
    CloudWatch. The module spends forty lines keeping those strings out of
    the log; the guard has to be on every branch.
    """

    @staticmethod
    def _chained(name):
        """A ModuleNotFoundError for *name* caused by a settings failure."""
        try:
            raise ModuleNotFoundError(
                f"No module named {name!r}", name=name
            ) from _real_settings_validation_error()
        except ModuleNotFoundError as exc:
            return exc

    @pytest.mark.regression
    def test_a_broken_install_does_not_print_a_chained_secret(
        self, fresh_hooks, monkeypatch
    ):
        import sys

        monkeypatch.delenv("TESTING", raising=False)
        chained = self._chained("snowflake")
        real_import = modules_loader.importlib.import_module

        def bad_import(name, package=None):
            if name == modules_loader.MODULES_PACKAGE:
                raise chained
            return real_import(name, package)

        monkeypatch.setattr(modules_loader.importlib, "import_module", bad_import)
        monkeypatch.delitem(sys.modules, modules_loader.MODULES_PACKAGE, raising=False)

        with patch.object(modules_loader, "logger") as log:
            modules_loader._find_register()

        assert not log.exception.called
        assert log.error.called
        assert log.error.call_args.kwargs["exc_info"] is False
        assert _REJECTED_KEY not in str(log.error.call_args)
        assert _REJECTED_KEY not in (modules_loader.modules_failure() or "")

    @pytest.mark.regression
    def test_the_application_module_branch_does_not_either(
        self, fresh_hooks, monkeypatch
    ):
        import sys

        monkeypatch.delenv("TESTING", raising=False)
        application = f"{modules_loader.MODULES_PACKAGE}.backend.app.register"
        chained = self._chained("xmlsec")
        top = types.ModuleType(modules_loader.MODULES_PACKAGE)
        top.__path__ = []  # a package, but with no register attribute
        real_import = modules_loader.importlib.import_module

        def fake_import(name, package=None):
            if name == modules_loader.MODULES_PACKAGE:
                return top
            if name == application:
                raise chained
            return real_import(name, package)

        monkeypatch.setattr(modules_loader.importlib, "import_module", fake_import)
        monkeypatch.delitem(sys.modules, modules_loader.MODULES_PACKAGE, raising=False)

        with patch.object(modules_loader, "logger") as log:
            modules_loader._find_register()

        assert not log.exception.called
        assert log.error.called
        assert log.error.call_args.kwargs["exc_info"] is False
        assert _REJECTED_KEY not in str(log.error.call_args)

    def test_an_ordinary_import_failure_keeps_its_traceback(
        self, fresh_hooks, monkeypatch
    ):
        """The guard is about secrets, not about tracebacks: an ImportError
        from module code is the whole diagnosis and keeps its frames."""
        import sys

        real_import = modules_loader.importlib.import_module

        def bad_import(name, package=None):
            if name == modules_loader.MODULES_PACKAGE:
                raise ModuleNotFoundError("No module named 'xmlsec'", name="xmlsec")
            return real_import(name, package)

        monkeypatch.setattr(modules_loader.importlib, "import_module", bad_import)
        monkeypatch.delitem(sys.modules, modules_loader.MODULES_PACKAGE, raising=False)

        with patch.object(modules_loader, "logger") as log:
            modules_loader._find_register()

        assert log.error.call_args.kwargs["exc_info"] is True

    def test_no_branch_uses_logger_exception_or_a_bare_exc_info(self):
        """`logger.exception` is `exc_info=True` whatever the exception holds,
        and so is `exc_info=True` written out. The guard is the only way this
        module is allowed to ask for a traceback."""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(modules_loader))
        logger_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "logger"
        ]
        assert logger_calls, "no logging in the loader?"
        assert [call for call in logger_calls if call.func.attr == "exception"] == []
        for call in logger_calls:
            for keyword in call.keywords:
                if keyword.arg != "exc_info":
                    continue
                assert isinstance(keyword.value, ast.Call), ast.dump(keyword.value)
                assert keyword.value.func.id == "_traceback_is_safe"
