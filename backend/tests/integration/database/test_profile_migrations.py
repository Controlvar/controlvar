"""Switching profile on one database: what alembic records and what exists.

The optional modules live on their own alembic branch, so a database can be
built by one profile and opened by the other.  Review round 1 found three ways
that went wrong, and each has a test here:

1. a fresh **full** bootstrap recorded a single ``alembic_version`` row -- the
   modules head -- so a **core** image pointed at the same database died in
   ``docker-entrypoint.sh`` with "Can't locate revision identified by
   'modules_0001_rbac'" and never started;
2. a **core** database opened by a **full** build never got nine of the twelve
   module tables, and ``/api/v1/modules`` answered ``profile: full`` while
   those routes failed on missing relations;
3. ``modules_0001_rbac.downgrade()`` dropped the three RBAC tables whether or
   not its own ``upgrade()`` had created them, so ``alembic downgrade`` on a
   bootstrapped database was data loss.

Review round 2 found a fourth, in the repair for (1): with the ``depends_on``
edge gone, ``modules_0001_rbac`` was an independent alembic base and alembic
scheduled it *before* the core revision it exists to undo.  On any database
behind ``a7b8c9d0e1f2`` one ``alembic upgrade heads`` therefore restored the
two workspace foreign keys and then dropped them, stamped both revisions and
left nothing to re-apply -- ``ON DELETE SET NULL`` silently gone on every
deployment that had not yet taken the release before this one.
``test_upgrade_heads_from_behind_the_core_head_keeps_the_workspace_fks``
rehearses exactly that; ``backend/tests/unit/db/test_alembic_branch_order.py``
pins the ordering structurally, without a database.

Test 2 doubles as the regression test for issue #88: before it, the three RBAC
tables (``custom_roles``, ``user_custom_roles``, ``direct_permission_grants``)
had no migration at all and existed only on databases ``create_all`` had built.
``test_switching_to_the_full_profile_creates_every_module_table`` asserts they
are there after the documented upgrade path on a database that migrations, not
``create_from_models``, brought up to date -- and
``test_downgrade_drops_the_tables_the_migration_created`` proves the migration
is what created them.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import text

from backend.app.db import bootstrap

REPO_ROOT = bootstrap._REPO_ROOT
CORE_VERSIONS, MODULES_VERSIONS = bootstrap._VERSION_LOCATIONS

pytestmark = [pytest.mark.integration, pytest.mark.modules]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
#: Captured before any monkeypatch replaces it (`_core_only_config` builds on it).
_REAL_ALEMBIC_CONFIG = bootstrap.alembic_config


def _core_only_config():
    """The alembic config a core checkout would build: one version location."""
    cfg = _REAL_ALEMBIC_CONFIG()
    cfg.set_main_option("version_locations", str(CORE_VERSIONS))
    return cfg


def _heads(cfg) -> set[str]:
    return set(ScriptDirectory.from_config(cfg).revision_map.heads)


def _module_table_names() -> set[str]:
    """Tables whose mapped class lives in the ``modules`` package."""
    from backend.app.models import register_core_models
    from backend.app.modules_loader import require_modules_or_absent

    Base = register_core_models()
    require_modules_or_absent()
    return {
        mapper.local_table.name
        for mapper in Base.registry.mappers
        if mapper.class_.__module__.startswith("modules.")
        and mapper.local_table is not None
    }


def _run(argv: list[str], schema: str) -> subprocess.CompletedProcess:
    """Run a module in a subprocess against *schema*, as a container would.

    A subprocess rather than an in-process call because a revision module reads
    ``POSTGRES_SCHEMA`` once, when alembic imports it.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("APP_ENV", "TESTING", "ENVIRONMENT")
    }
    env.update({"PYTHONPATH": str(REPO_ROOT), "POSTGRES_SCHEMA": schema})
    # A deployment sets ENVIRONMENT or a real first-administrator password;
    # with neither, the bootstrap refuses the class-default "admin".
    env.setdefault("FIRST_SUPERUSER_PASSWORD", "Subprocess-Deployment-Passw0rd")
    return subprocess.run(
        [sys.executable, *argv],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )


def _bootstrap(schema: str) -> subprocess.CompletedProcess:
    return _run(["-m", "backend.app.db.bootstrap"], schema)


def _alembic(schema: str, *args: str) -> subprocess.CompletedProcess:
    return _run(
        ["-m", "alembic", "-c", str(REPO_ROOT / "backend/app/db/alembic.ini"), *args],
        schema,
    )


def _tables(engine, schema: str) -> set[str]:
    from sqlalchemy import inspect

    return set(inspect(engine).get_table_names(schema=schema))


def _workspace_fks(engine, schema: str) -> set[str]:
    with engine.connect() as conn:
        return set(
            conn.execute(
                text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE connamespace = CAST(:schema AS regnamespace) AND contype = 'f' "
                    "AND conname IN ('experiments_workspace_id_fkey', "
                    "'feature_flags_workspace_id_fkey')"
                ),
                {"schema": schema},
            ).scalars()
        )


#: The core revision the modules branch must run *after*: it drops the two
#: workspace foreign keys that ``modules_0001_rbac`` puts back.
BRANCH_POINT = "a7b8c9d0e1f2"

#: The modules branch's FIRST revision -- not its head since #312 added
#: ``modules_0002_warehouse_analysis``.  Used below only where "first" is the
#: meaning: the one revision a build from before the branch point's move had,
#: and so the one a database that build left behind records.  Where a test
#: means "at head" it compares against ``_heads(...)``, read from the files.
MODULES_FIRST = "modules_0001_rbac"

WORKSPACE_FKS = {"experiments_workspace_id_fkey", "feature_flags_workspace_id_fkey"}


def _record_revisions(engine, schema: str, revisions) -> None:
    """Replace ``alembic_version`` with exactly *revisions*."""
    with engine.begin() as conn:
        conn.execute(text(f'DELETE FROM "{schema}".alembic_version'))
        for revision in revisions:
            conn.execute(
                text(f'INSERT INTO "{schema}".alembic_version VALUES (:rev)'),
                {"rev": revision},
            )


def _make_core_built_schema(engine, schema: str) -> None:
    """Turn a full-profile schema into the one a core build would have left.

    A core bootstrap creates the core tables from the core models and stamps the
    core head -- which marks the revisions that once created ``workspaces``,
    ``sso_configs`` and the rest as applied without creating them.
    """
    module_tables = _module_table_names()
    core_head = sorted(_heads(_core_only_config()))
    with engine.begin() as conn:
        for table in module_tables:
            conn.execute(text(f'DROP TABLE IF EXISTS "{schema}"."{table}" CASCADE'))
    _record_revisions(engine, schema, core_head)


@pytest.fixture(autouse=True)
def _keep_the_models_pointed_where_they_were():
    """Undo ``bootstrap._point_models_at_schema``'s global edit.

    The bootstrap repoints every table in ``Base.metadata`` at the schema it is
    building, which is fine in a one-shot process and poison for the rest of a
    pytest session.
    """
    from backend.app.models import register_core_models

    Base = register_core_models()
    before = [(table, table.schema) for table in Base.metadata.tables.values()]
    metadata_schema = Base.metadata.schema
    try:
        yield
    finally:
        Base.metadata.schema = metadata_schema
        for table, schema in before:
            table.schema = schema


@pytest.fixture
def scratch_schema(test_db):
    schema = f"profile_{uuid.uuid4().hex[:8]}"
    try:
        yield schema
    finally:
        with test_db.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


# ---------------------------------------------------------------------------
# 1. Every head is recorded, and a core build survives a full database
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_fresh_full_bootstrap_records_every_head(test_db, scratch_schema):
    """`stamp heads` used to write one row: the modules head absorbed the core one."""
    assert _bootstrap(scratch_schema).returncode == 0

    recorded = bootstrap.recorded_revisions(test_db, scratch_schema)
    assert recorded == _heads(_REAL_ALEMBIC_CONFIG())
    assert len(recorded) == 2, recorded
    # The point of recording both: a core checkout can still find its own head.
    assert _heads(_core_only_config()) <= recorded


@pytest.mark.regression
def test_a_core_build_bootstraps_a_full_database(test_db, scratch_schema, monkeypatch):
    """A core image against a full database starts instead of dying on an unknown row."""
    assert _bootstrap(scratch_schema).returncode == 0
    before = bootstrap.recorded_revisions(test_db, scratch_schema)

    monkeypatch.setattr(bootstrap, "alembic_config", _core_only_config)
    assert bootstrap.bootstrap(test_db, scratch_schema) == "upgraded"

    # The modules' revision is left recorded: this build has no file for it and
    # must not pretend it was rolled back.
    assert bootstrap.recorded_revisions(test_db, scratch_schema) == before


def test_a_core_build_refuses_when_it_has_migrations_of_its_own_to_apply(
    test_db, scratch_schema, monkeypatch
):
    assert _bootstrap(scratch_schema).returncode == 0
    with test_db.begin() as conn:
        # A core chain that is behind: its head is no longer recorded.
        for revision in _heads(_core_only_config()):
            conn.execute(
                text(
                    f'DELETE FROM "{scratch_schema}".alembic_version '
                    "WHERE version_num = :rev"
                ),
                {"rev": revision},
            )

    monkeypatch.setattr(bootstrap, "alembic_config", _core_only_config)
    with pytest.raises(RuntimeError, match="records migration"):
        bootstrap.bootstrap(test_db, scratch_schema)


# ---------------------------------------------------------------------------
# 2. Switching a core database to the full profile (and issue #88)
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_switching_to_the_full_profile_creates_every_module_table(
    test_db, scratch_schema
):
    assert _bootstrap(scratch_schema).returncode == 0
    full_tables = _tables(test_db, scratch_schema)
    _make_core_built_schema(test_db, scratch_schema)
    assert _module_table_names() - _tables(test_db, scratch_schema)

    result = _bootstrap(scratch_schema)
    assert result.returncode == 0, result.stderr[-2000:]

    # Exactly the schema a fresh full-profile bootstrap builds.
    assert _tables(test_db, scratch_schema) == full_tables
    assert _workspace_fks(test_db, scratch_schema) == {
        "experiments_workspace_id_fkey",
        "feature_flags_workspace_id_fkey",
    }
    assert bootstrap.recorded_revisions(test_db, scratch_schema) == _heads(
        _REAL_ALEMBIC_CONFIG()
    )
    # Issue #88: the three RBAC tables, on a database migrations brought up to
    # date rather than create_from_models.
    assert {
        "custom_roles",
        "user_custom_roles",
        "direct_permission_grants",
    } <= _tables(test_db, scratch_schema)


# ---------------------------------------------------------------------------
# 2b. Ordering: the branch must run after the core revision it undoes
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_upgrade_heads_from_behind_the_core_head_keeps_the_workspace_fks(
    test_db, scratch_schema
):
    """One ``alembic upgrade heads``, the production path, from a pre-release state.

    ``a7b8c9d0e1f2`` drops the two foreign keys and ``modules_0001_rbac``
    restores them.  While the latter was an independent alembic base, alembic
    ran it *first* -- it found the constraints still there and skipped, the
    core revision then dropped them, and both revisions ended up stamped with
    nothing left to put them back.  This is the deployment that has not yet
    taken the previous release: everything applied in one command.
    """
    assert _bootstrap(scratch_schema).returncode == 0
    assert _workspace_fks(test_db, scratch_schema) == WORKSPACE_FKS

    # Wind the version table back to the revision before the branch point --
    # the schema itself already matches, since nothing between them touches it.
    script = ScriptDirectory.from_config(_REAL_ALEMBIC_CONFIG())
    before_the_branch_point = script.get_revision(BRANCH_POINT).down_revision
    assert isinstance(before_the_branch_point, str)
    _record_revisions(test_db, scratch_schema, [before_the_branch_point])

    result = _alembic(scratch_schema, "upgrade", "heads")
    assert result.returncode == 0, result.stderr[-2000:]

    assert _workspace_fks(test_db, scratch_schema) == WORKSPACE_FKS
    assert bootstrap.recorded_revisions(test_db, scratch_schema) == _heads(
        _REAL_ALEMBIC_CONFIG()
    )


@pytest.mark.regression
def test_bootstrap_repairs_a_version_row_another_row_descends_from(
    test_db, scratch_schema
):
    """A database the previous build left recorded at the branch point *and*
    at the branch.

    ``modules_0001_rbac`` was an alembic base then and is a child of
    ``a7b8c9d0e1f2`` now, so recording both is a state alembic rejects: every
    command fails with "Requested revision modules_0001_rbac overlaps with
    other requested revisions a7b8c9d0e1f2" and there is no way forward from
    inside alembic.  (``current`` still answers: it reduces the set to its
    branch heads before printing.)

    :func:`prune_redundant_revisions` is the repair, and review round 4 found
    it was reachable only through ``db/bootstrap.py`` -- while
    ``deploy.yml``, ``db-migrate.yml``, the CDK migration task and
    ``docs/self-hosting/migrations.md`` all run raw ``alembic upgrade heads``,
    which died there.  ``migrations/env.py`` calls it now, so both paths repair
    the row; this test asserts it once per path.
    """
    assert _bootstrap(scratch_schema).returncode == 0
    # FIRST: the previous build had one modules revision.  The database is
    # therefore *behind* the modules head as well, and the upgrade below has to
    # apply the rest of the branch -- which the `_heads` comparison checks.
    _record_revisions(test_db, scratch_schema, [BRANCH_POINT, MODULES_FIRST])

    # Raw `alembic upgrade heads` prunes it and carries on ...
    upgraded = _alembic(scratch_schema, "upgrade", "heads")
    assert upgraded.returncode == 0, upgraded.stderr[-2000:]
    assert "overlaps with other requested revisions" not in (
        upgraded.stderr + upgraded.stdout
    )
    assert bootstrap.recorded_revisions(test_db, scratch_schema) == _heads(
        _REAL_ALEMBIC_CONFIG()
    )

    # ... and so does the bootstrap, from the same function.
    _record_revisions(test_db, scratch_schema, [BRANCH_POINT, MODULES_FIRST])
    removed = bootstrap.prune_redundant_revisions(
        test_db, scratch_schema, _REAL_ALEMBIC_CONFIG()
    )
    assert removed == [BRANCH_POINT]
    assert _alembic(scratch_schema, "upgrade", "heads").returncode == 0
    assert bootstrap.recorded_revisions(test_db, scratch_schema) == _heads(
        _REAL_ALEMBIC_CONFIG()
    )
    assert _workspace_fks(test_db, scratch_schema) == WORKSPACE_FKS


def test_pruning_leaves_a_healthy_version_table_alone(test_db, scratch_schema):
    """The normal case costs one read and changes nothing."""
    assert _bootstrap(scratch_schema).returncode == 0
    before = bootstrap.recorded_revisions(test_db, scratch_schema)

    assert (
        bootstrap.prune_redundant_revisions(
            test_db, scratch_schema, _REAL_ALEMBIC_CONFIG()
        )
        == []
    )
    assert bootstrap.recorded_revisions(test_db, scratch_schema) == before


# ---------------------------------------------------------------------------
# 3. downgrade() drops only what upgrade() created
# ---------------------------------------------------------------------------
RBAC_TABLES = ("custom_roles", "user_custom_roles", "direct_permission_grants")

#: Unapply the whole modules branch -- all three of its revisions, so that
#: ``modules_0001_rbac``'s own downgrade runs.  Not ``modules@base``: the branch
#: is no longer an alembic *base*, and with a single tree root alembic cannot
#: filter by branch label, so ``modules@base`` downgrades the whole core chain
#: instead.  (``modules@-1`` unapplies only the head, modules_0003, and
#: ``modules@-2`` stops short of modules_0001.)
UNAPPLY_MODULES_BRANCH = "modules@-3"

#: The tables ``modules_0002_warehouse_analysis`` owns.
WAREHOUSE_TABLES = (
    "warehouse_connections",
    "warehouse_sources",
    "warehouse_analysis_runs",
)
#: The column ``modules_0003_lower_is_better`` adds.  Its downgrade drops it on
#: every database, however it was made: the one exception to "drops only what
#: its own upgrade created".
LOWER_IS_BETTER = ("warehouse_sources", "lower_is_better")


def _columns(engine, schema: str, table: str) -> set[str]:
    from sqlalchemy import inspect

    return {c["name"] for c in inspect(engine).get_columns(table, schema=schema)}


@pytest.mark.regression
def test_downgrade_keeps_the_tables_the_bootstrap_created(test_db, scratch_schema):
    """The fresh-bootstrap case: `upgrade()` never ran, so no table may be
    dropped.  ``modules_0003_lower_is_better``'s column is the exception."""
    assert _bootstrap(scratch_schema).returncode == 0
    table, column = LOWER_IS_BETTER
    assert column in _columns(test_db, scratch_schema, table)

    result = _alembic(scratch_schema, "downgrade", UNAPPLY_MODULES_BRANCH)
    assert result.returncode == 0, result.stderr[-2000:]

    assert set(RBAC_TABLES) <= _tables(test_db, scratch_schema)
    assert set(WAREHOUSE_TABLES) <= _tables(test_db, scratch_schema)
    assert column not in _columns(test_db, scratch_schema, table)
    assert _workspace_fks(test_db, scratch_schema) == {
        "experiments_workspace_id_fkey",
        "feature_flags_workspace_id_fkey",
    }


@pytest.mark.regression
def test_downgrade_drops_the_tables_the_migration_created(test_db, scratch_schema):
    assert _bootstrap(scratch_schema).returncode == 0
    _make_core_built_schema(test_db, scratch_schema)
    assert not set(RBAC_TABLES) & _tables(test_db, scratch_schema)

    # The migration creates them this time (issue #88's repair).
    assert _alembic(scratch_schema, "upgrade", "heads").returncode == 0
    assert set(RBAC_TABLES) <= _tables(test_db, scratch_schema)

    result = _alembic(scratch_schema, "downgrade", UNAPPLY_MODULES_BRANCH)
    assert result.returncode == 0, result.stderr[-2000:]
    assert not set(RBAC_TABLES) & _tables(test_db, scratch_schema)


# ---------------------------------------------------------------------------
# 4. A relative step with two heads
# ---------------------------------------------------------------------------
def test_a_relative_step_is_refused_with_two_heads_and_a_named_revision_applies(
    test_db, scratch_schema
):
    """A full checkout against a database at the core head, with the modules
    branch still to apply: ``upgrade +1`` is refused and applies nothing;
    ``upgrade`` naming the revision applies it."""
    assert _bootstrap(scratch_schema).returncode == 0
    _make_core_built_schema(test_db, scratch_schema)
    core_heads = _heads(_core_only_config())
    assert len(_heads(_REAL_ALEMBIC_CONFIG())) == 2
    assert bootstrap.recorded_revisions(test_db, scratch_schema) == core_heads

    relative = _alembic(scratch_schema, "upgrade", "+1")
    assert relative.returncode != 0, relative.stdout[-2000:]
    assert "Relative revision +1 didn't produce 1 migrations" in relative.stderr, (
        relative.stderr[-2000:]
    )
    assert bootstrap.recorded_revisions(test_db, scratch_schema) == core_heads
    assert not set(RBAC_TABLES) & _tables(test_db, scratch_schema)

    named = _alembic(scratch_schema, "upgrade", MODULES_FIRST)
    assert named.returncode == 0, named.stderr[-2000:]
    assert bootstrap.recorded_revisions(test_db, scratch_schema) == core_heads | {
        MODULES_FIRST
    }
    assert set(RBAC_TABLES) <= _tables(test_db, scratch_schema)
