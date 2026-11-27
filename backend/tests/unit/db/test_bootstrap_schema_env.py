"""The bootstrap and alembic must agree on which schema they are working on.

``db/bootstrap.py`` picks the schema with :func:`~backend.app.db.bootstrap.
schema_name`, which falls back to ``core.database_config.get_schema_name()``
(derived from ``APP_ENV``) when ``POSTGRES_SCHEMA`` is unset.  Alembic learns it
from ``POSTGRES_SCHEMA`` alone -- ``migrations/env.py`` reads the environment,
and revision modules such as ``a7b8c9d0e1f2`` and ``modules_0001_rbac`` read it
at *import* time for the names they build.

Only the fresh-database branch used to export it, just before
``command.stamp``.  With ``APP_ENV=test`` and no ``POSTGRES_SCHEMA`` the
existing-database branch therefore ran ``command.upgrade`` against
``experimentation`` while the bootstrap worked on ``test_experimentation``:
alembic found no ``alembic_version`` in the schema it was looking at, created
it, and started replaying the historical chain from its base -- which the
bootstrap's own docstring says cannot be replayed -- leaving a stray, half-built
schema behind and failing on ``type "experimentstatus" already exists``.

No database: the point is purely which value alembic is given.
"""

from __future__ import annotations

import os

import pytest

from backend.app.db import bootstrap

pytestmark = pytest.mark.unit

SCHEMA = "chosen_by_the_bootstrap"


@pytest.fixture
def alembic_is_stubbed(monkeypatch):
    """Replace everything that touches a database; record what alembic saw."""
    seen: dict[str, str | None] = {}

    def _record(name):
        def hook(*_args, **_kwargs):
            seen[name] = os.environ.get("POSTGRES_SCHEMA")

        return hook

    monkeypatch.delenv("POSTGRES_SCHEMA", raising=False)
    monkeypatch.setattr(bootstrap, "ensure_schema", lambda *a: None)
    monkeypatch.setattr(bootstrap, "prune_redundant_revisions", lambda *a: [])
    monkeypatch.setattr(bootstrap, "recorded_revisions", lambda *a: set())
    monkeypatch.setattr(bootstrap, "_unresolvable", lambda *a: (set(), True))
    monkeypatch.setattr(bootstrap, "reconcile_with_models", lambda *a: ([], []))
    monkeypatch.setattr(bootstrap, "create_from_models", lambda *a: None)
    monkeypatch.setattr(bootstrap, "ensure_first_superuser", lambda *a: False)
    # The recorder returns None; nothing downstream is real here.
    monkeypatch.setattr(bootstrap, "alembic_config", _record("config"))
    monkeypatch.setattr(bootstrap.command, "upgrade", _record("upgrade"))
    monkeypatch.setattr(bootstrap.command, "stamp", _record("stamp"))
    return seen


@pytest.mark.regression
def test_the_existing_database_branch_upgrades_the_schema_it_chose(
    alembic_is_stubbed, monkeypatch
):
    """`command.upgrade` used to run with POSTGRES_SCHEMA unset."""
    monkeypatch.setattr(bootstrap, "has_alembic_version", lambda *a: True)

    assert bootstrap._bootstrap_locked(object(), SCHEMA) == "upgraded"

    assert alembic_is_stubbed["upgrade"] == SCHEMA


def test_the_fresh_database_branch_stamps_the_schema_it_chose(
    alembic_is_stubbed, monkeypatch
):
    monkeypatch.setattr(bootstrap, "has_alembic_version", lambda *a: False)

    assert bootstrap._bootstrap_locked(object(), SCHEMA) == "created"

    assert alembic_is_stubbed["stamp"] == SCHEMA


@pytest.mark.regression
def test_the_config_is_built_with_the_schema_already_set(
    alembic_is_stubbed, monkeypatch
):
    """Building the config reads every revision file, and a revision module
    binds POSTGRES_SCHEMA when it is imported -- so the variable has to be
    right before that happens, not just before the command runs."""
    monkeypatch.setattr(bootstrap, "has_alembic_version", lambda *a: True)

    bootstrap._bootstrap_locked(object(), SCHEMA)

    assert alembic_is_stubbed["config"] == SCHEMA


@pytest.mark.parametrize("existing", [True, False], ids=["upgrade", "fresh"])
def test_the_variable_is_left_as_it_was_found(
    alembic_is_stubbed, monkeypatch, existing
):
    """An in-process caller (a test bootstrapping a scratch schema) must not
    change what the rest of the process resolves."""
    monkeypatch.setattr(bootstrap, "has_alembic_version", lambda *a: existing)

    bootstrap._bootstrap_locked(object(), SCHEMA)
    assert "POSTGRES_SCHEMA" not in os.environ

    monkeypatch.setenv("POSTGRES_SCHEMA", "somewhere_else")
    bootstrap._bootstrap_locked(object(), SCHEMA)
    assert os.environ["POSTGRES_SCHEMA"] == "somewhere_else"


class _Table:
    def __init__(self, name):
        self.name = name


class _Metadata:
    def __init__(self, names):
        self.tables = {name: _Table(name) for name in names}
        self.created = []

    def create_all(self, bind=None, tables=None):
        self.created = sorted(t.name for t in tables)


@pytest.mark.regression
def test_creating_missing_module_tables_logs_a_warning_that_names_them(
    monkeypatch, caplog
):
    """The guide says a full build meeting a core database logs a WARNING."""
    metadata = _Metadata(["workspaces", "sso_configs", "experiments"])
    monkeypatch.setattr(bootstrap, "schema_metadata", lambda schema: metadata)
    monkeypatch.setattr(
        bootstrap,
        "missing_model_tables",
        lambda engine, schema, md: ["workspaces", "sso_configs", "experiments"],
    )
    monkeypatch.setattr(bootstrap, "_add_missing_alter_constraints", lambda *a: [])

    with caplog.at_level("INFO", logger=bootstrap.logger.name):
        created, added = bootstrap.reconcile_with_models(object(), SCHEMA)

    # Only module tables are created; the core table `experiments` is left alone.
    assert created == ["workspaces", "sso_configs"]
    assert added == []
    assert metadata.created == ["sso_configs", "workspaces"]
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert SCHEMA in message and "workspaces" in message and "sso_configs" in message
    assert "experiments" not in message


def test_nothing_missing_logs_no_warning(monkeypatch, caplog):
    monkeypatch.setattr(bootstrap, "schema_metadata", lambda schema: _Metadata([]))
    monkeypatch.setattr(bootstrap, "missing_model_tables", lambda *a: [])
    monkeypatch.setattr(bootstrap, "_add_missing_alter_constraints", lambda *a: [])

    with caplog.at_level("INFO", logger=bootstrap.logger.name):
        assert bootstrap.reconcile_with_models(object(), SCHEMA) == ([], [])

    assert [r for r in caplog.records if r.levelname == "WARNING"] == []
