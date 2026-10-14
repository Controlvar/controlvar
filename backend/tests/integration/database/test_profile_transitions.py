"""Every way a database can move between the profiles, and where it lands.

Four transitions, one test each, each asserting the same two things about the
**end state**: the exact rows in ``alembic_version``, and whether the two
``workspace_id`` foreign keys are there.  Those two are what every regression in
this area has damaged, and they are cheap to state exactly.

    1. fresh **full** bootstrap
    2. fresh **core**, then switched to full by ``alembic upgrade heads``
    3. a database at the **pre-release** revision, upgraded with ``heads``
    4. a **core** build opened against a full-bootstrapped database

and two limits on what a transition may do: the reconcile that finishes (2)
creates module tables and nothing else (5), and only ``upgrade`` may answer (4)
by succeeding having done nothing -- ``downgrade`` and ``stamp`` may not (6).

The difference from ``test_profile_migrations.py``, which covers some of the
same ground, is *how* the core side is produced: that file simulates a core
checkout by pointing an alembic ``Config`` at one version location, which
cannot exercise ``migrations/env.py`` -- the file the guard and the reconcile
live in -- from a genuinely modules-free interpreter.  This one builds a real
tree with no ``modules/`` (``tree_profiles.core_tree``) and runs the documented
commands in it, as a container would.

Why the documented commands and not ``db/bootstrap.py``: review round 3 found
that ``alembic upgrade heads`` -- what ``docs/self-hosting/migrations.md``,
``deploy.yml`` and ``infrastructure/cdk/stacks/migration_task_stack.py``
all run -- completed transition 2 only half way (three of the twelve module
tables created, both revisions stamped, nothing left to retry, and
``/workspaces``, ``/hipaa/*`` and ``/auth/sso/*`` answering 500 on
``UndefinedTable``) and failed transition 4 outright with ``CommandError: Can't
locate revision identified by 'modules_0001_rbac'``, which is the ECS migration
task's exit code and so the deploy job's.
"""

from __future__ import annotations

import uuid

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from backend.app.db import bootstrap
from backend.tests.integration.database import tree_profiles
from backend.tests.integration.database.tree_profiles import CORE, FULL

pytestmark = [pytest.mark.integration, pytest.mark.modules]

#: The core revision the modules branch undoes: it drops the two foreign keys.
BRANCH_POINT = "a7b8c9d0e1f2"

#: The end states, spelled out rather than read back from the checkout's
#: version directories.  Two reasons: an expected value computed the same way
#: the code computes it proves nothing, and `alembic revision` writes real files
#: into those directories when a developer generates one, so a concurrent
#: `alembic revision` would move what "the heads" means mid-test.
#: `backend/tests/unit/db/test_alembic_plan.py` is where these are pinned
#: against the files, with the whole apply order.
CORE_HEAD = "4c2c09a2b3e8"
#: The modules branch's HEAD.  Every use below means "the modules revision a
#: full database at head records" -- none means the branch's first revision,
#: ``modules_0001_rbac``, which stopped being the head in #312.
MODULES_HEAD = "modules_0002_warehouse_analysis"
CORE_HEADS = {CORE_HEAD}
FULL_HEADS = {CORE_HEAD, MODULES_HEAD}

#: How many module tables a full-profile schema holds
#: (``autogenerate_filters.MODULE_TABLES``, pinned to the manifest).
MODULE_TABLE_COUNT = 14

WORKSPACE_FKS = {"experiments_workspace_id_fkey", "feature_flags_workspace_id_fkey"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def scratch_schema(test_db):
    schema = f"switch_{uuid.uuid4().hex[:8]}"
    try:
        yield schema
    finally:
        with test_db.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


@pytest.fixture
def full_tree():
    return tree_profiles.tree_for(FULL, None)


@pytest.fixture
def core_tree(tmp_path):
    return tree_profiles.tree_for(CORE, tmp_path)


def _rows(engine, schema: str) -> set[str]:
    return bootstrap.recorded_revisions(engine, schema)


def _tables(engine, schema: str) -> set[str]:
    return set(inspect(engine).get_table_names(schema=schema))


def _workspace_fks(engine, schema: str) -> set[str]:
    with engine.connect() as conn:
        return set(
            conn.execute(
                text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE connamespace = CAST(:schema AS regnamespace) "
                    "AND contype = 'f' AND conname = ANY(:names)"
                ),
                {"schema": schema, "names": sorted(WORKSPACE_FKS)},
            ).scalars()
        )


def _module_tables(engine, schema: str) -> set[str]:
    from backend.app.db.autogenerate_filters import MODULE_TABLES

    return MODULE_TABLES & _tables(engine, schema)


def _set_rows(engine, schema: str, revisions) -> None:
    with engine.begin() as conn:
        conn.execute(text(f'DELETE FROM "{schema}".alembic_version'))
        for revision in revisions:
            conn.execute(
                text(f'INSERT INTO "{schema}".alembic_version VALUES (:rev)'),
                {"rev": revision},
            )


# ---------------------------------------------------------------------------
# 1. Fresh full bootstrap
# ---------------------------------------------------------------------------
def test_a_fresh_full_bootstrap(test_db, scratch_schema, full_tree):
    """The reference end state every other transition is measured against."""
    result = tree_profiles.bootstrap_schema(full_tree, scratch_schema)
    assert result.returncode == 0, result.stderr[-3000:]

    assert _rows(test_db, scratch_schema) == FULL_HEADS
    assert len(_rows(test_db, scratch_schema)) == 2
    assert _workspace_fks(test_db, scratch_schema) == WORKSPACE_FKS
    assert len(_module_tables(test_db, scratch_schema)) == MODULE_TABLE_COUNT


# ---------------------------------------------------------------------------
# 2. Core database, full build, `alembic upgrade heads`
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_a_core_database_switched_to_full_by_the_documented_command(
    test_db, scratch_schema, core_tree, full_tree
):
    """``alembic upgrade heads`` must finish the switch, not stamp it half done.

    The modules branch owns only the three RBAC tables; the other nine module
    tables are created by *core*-chain revisions the core bootstrap already
    stamped.  Without the reconcile in ``migrations/env.py`` this ended with 41
    tables, both heads recorded and nine relations missing for good.
    """
    assert tree_profiles.bootstrap_schema(core_tree, scratch_schema).returncode == 0
    core_only = _tables(test_db, scratch_schema)
    assert _module_tables(test_db, scratch_schema) == set()
    assert _rows(test_db, scratch_schema) == CORE_HEADS

    upgrade = tree_profiles.alembic(full_tree, scratch_schema, "upgrade", "heads")
    assert upgrade.returncode == 0, upgrade.stderr[-3000:]

    assert _rows(test_db, scratch_schema) == FULL_HEADS
    assert _workspace_fks(test_db, scratch_schema) == WORKSPACE_FKS
    assert len(_module_tables(test_db, scratch_schema)) == MODULE_TABLE_COUNT
    # Exactly what a fresh full bootstrap builds: the module tables and nothing
    # else.
    assert _tables(test_db, scratch_schema) - core_only == _module_tables(
        test_db, scratch_schema
    )


def test_a_partial_upgrade_does_not_reconcile(
    test_db, scratch_schema, core_tree, full_tree
):
    """``alembic upgrade <revision>`` stops where it was told to stop.

    The reconcile that finishes a profile switch creates every table the models
    declare, so it may only run on a database that has reached *this build's*
    heads.  ``upgrade <revision>`` deliberately stops part way; creating the
    module tables there would put the schema ahead of the version table and
    leave the next migration creating something that already exists.
    """
    assert tree_profiles.bootstrap_schema(core_tree, scratch_schema).returncode == 0
    config = Config(str(tree_profiles.alembic_ini(full_tree)))
    before_the_branch_point = (
        ScriptDirectory.from_config(config).get_revision(BRANCH_POINT).down_revision
    )
    _set_rows(test_db, scratch_schema, [before_the_branch_point])

    part_way = tree_profiles.alembic(full_tree, scratch_schema, "upgrade", BRANCH_POINT)
    assert part_way.returncode == 0, part_way.stderr[-3000:]

    assert _rows(test_db, scratch_schema) == {BRANCH_POINT}
    assert _module_tables(test_db, scratch_schema) == set()

    # ... and the rest of the way does reconcile.
    assert (
        tree_profiles.alembic(full_tree, scratch_schema, "upgrade", "heads").returncode
        == 0
    )
    assert len(_module_tables(test_db, scratch_schema)) == MODULE_TABLE_COUNT


def test_the_switch_lands_where_a_fresh_full_bootstrap_lands(
    test_db, scratch_schema, core_tree, full_tree
):
    """Same schema by either route -- the whole promise of a profile switch."""
    assert tree_profiles.bootstrap_schema(core_tree, scratch_schema).returncode == 0
    assert (
        tree_profiles.alembic(full_tree, scratch_schema, "upgrade", "heads").returncode
        == 0
    )
    switched = _tables(test_db, scratch_schema)

    reference = f"{scratch_schema}_ref"
    try:
        assert tree_profiles.bootstrap_schema(full_tree, reference).returncode == 0
        assert switched == _tables(test_db, reference)
    finally:
        with test_db.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{reference}" CASCADE'))


# ---------------------------------------------------------------------------
# 3. A pre-release database, upgraded with `heads`
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_a_pre_release_database_upgraded_with_heads(test_db, scratch_schema, full_tree):
    """The deployment that has not taken the previous release yet.

    ``a7b8c9d0e1f2`` drops the two foreign keys and ``modules_0001_rbac``
    restores them, so the order is the whole correctness of the pair.  While the
    branch was an independent alembic base, one ``upgrade heads`` from here ran
    the restore first and the drop second, stamped both, and left ``ON DELETE
    SET NULL`` gone with nothing left to re-apply.
    """
    assert tree_profiles.bootstrap_schema(full_tree, scratch_schema).returncode == 0
    assert _workspace_fks(test_db, scratch_schema) == WORKSPACE_FKS

    config = Config(str(tree_profiles.alembic_ini(full_tree)))
    before_the_branch_point = (
        ScriptDirectory.from_config(config).get_revision(BRANCH_POINT).down_revision
    )
    assert isinstance(before_the_branch_point, str)
    _set_rows(test_db, scratch_schema, [before_the_branch_point])

    upgrade = tree_profiles.alembic(full_tree, scratch_schema, "upgrade", "heads")
    assert upgrade.returncode == 0, upgrade.stderr[-3000:]

    assert _rows(test_db, scratch_schema) == FULL_HEADS
    assert _workspace_fks(test_db, scratch_schema) == WORKSPACE_FKS
    assert len(_module_tables(test_db, scratch_schema)) == MODULE_TABLE_COUNT


# ---------------------------------------------------------------------------
# 4. A core build against a full database
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_a_core_build_against_a_full_database_with_nothing_to_apply(
    test_db, scratch_schema, core_tree, full_tree
):
    """``deploy.yml`` runs the ECS migration task with the image it deploys, so
    a ``profile=core`` deploy onto an environment a full release migrated
    (its stacks redeployed from a core checkout) is what the migration task
    does to a database a full deployment built.

    Raw alembic has no way to ignore a revision it cannot resolve: it read the
    ``modules_0001_rbac`` row and died with ``CommandError`` before applying
    anything, failing the deploy job.  There is nothing for a core build to
    apply here, so the honest answer is to say so and exit 0 -- which is what
    ``db/bootstrap.py`` already did, and now what ``alembic upgrade heads``
    does, from the same guard.
    """
    assert tree_profiles.bootstrap_schema(full_tree, scratch_schema).returncode == 0
    before = _rows(test_db, scratch_schema)
    tables_before = _tables(test_db, scratch_schema)

    upgrade = tree_profiles.alembic(core_tree, scratch_schema, "upgrade", "heads")

    assert upgrade.returncode == 0, upgrade.stderr[-3000:]
    assert "another profile" in upgrade.stderr
    # The rows belong to the other profile; this build must not pretend they
    # were rolled back, and it must not touch the module tables either.
    assert _rows(test_db, scratch_schema) == before
    assert _tables(test_db, scratch_schema) == tables_before
    assert _workspace_fks(test_db, scratch_schema) == WORKSPACE_FKS


@pytest.mark.regression
def test_a_core_build_refuses_a_full_database_it_has_migrations_for(
    test_db, scratch_schema, core_tree, full_tree
):
    """The one case a core build must not continue past, and what it says.

    alembic cannot plan a path from a revision it has no file for, so the
    operator gets the two ways out instead of a traceback.  Both documented
    paths -- ``alembic upgrade heads`` and ``python -m
    backend.app.db.bootstrap`` -- answer identically, because they share the
    guard.
    """
    assert tree_profiles.bootstrap_schema(full_tree, scratch_schema).returncode == 0
    # A core chain that is behind: its head is no longer recorded.
    _set_rows(test_db, scratch_schema, [BRANCH_POINT, MODULES_HEAD])

    upgrade = tree_profiles.alembic(core_tree, scratch_schema, "upgrade", "heads")
    bootstrapped = tree_profiles.bootstrap_schema(core_tree, scratch_schema)

    for result in (upgrade, bootstrapped):
        assert result.returncode != 0, result.stdout[-2000:]
        assert MODULES_HEAD in result.stderr
        assert "Run the full image of this release against it" in result.stderr
        # Never the old "delete those rows" advice (#238).
        assert "delete" not in result.stderr[result.stderr.rindex("RuntimeError") :]
    # Nothing applied, nothing stamped, nothing dropped.
    assert _rows(test_db, scratch_schema) == {BRANCH_POINT, MODULES_HEAD}
    assert len(_module_tables(test_db, scratch_schema)) == MODULE_TABLE_COUNT


# ---------------------------------------------------------------------------
# 5. What the reconcile may and may not create
# ---------------------------------------------------------------------------
#: A core model with no migration, written into a throwaway checkout.
_UNMIGRATED_CORE_MODEL = '''"""A core model nobody wrote a migration for (test fixture)."""

from sqlalchemy import Column, String

from backend.app.models.base import Base


class UnmigratedProbe(Base):
    __tablename__ = "unmigrated_core_probe"

    name = Column(String(64), primary_key=True)
'''


def _add_unmigrated_core_model(tree) -> str:
    """Declare one more core model in *tree*; return its table name."""
    models = tree / "backend" / "app" / "models"
    (models / "unmigrated_probe.py").write_text(_UNMIGRATED_CORE_MODEL, "utf-8")
    registry = models / "__init__.py"
    registry.write_text(
        registry.read_text("utf-8").replace(
            "CORE_MODEL_MODULES = (\n",
            'CORE_MODEL_MODULES = (\n    "unmigrated_probe",\n',
            1,
        ),
        "utf-8",
    )
    return "unmigrated_core_probe"


@pytest.mark.regression
def test_an_unmigrated_core_model_is_not_created_by_an_upgrade(
    test_db, scratch_schema, core_tree
):
    """The reconcile creates module tables, and only those.

    ``alembic upgrade heads`` reaches head on every ordinary deployment, so the
    reconcile runs there every time.  While it called ``create_all`` unfiltered,
    a **core** model somebody added without a migration was created by it --
    outside the migration transaction, with no revision, no downgrade and no
    review -- and the next ``--autogenerate`` then reported no diff, because the
    table existed on every database that had ever run the API.  Drift, made
    permanent and invisible (review round 4, finding 2).
    """
    assert tree_profiles.bootstrap_schema(core_tree, scratch_schema).returncode == 0
    probe = _add_unmigrated_core_model(core_tree)
    assert probe not in _tables(test_db, scratch_schema)

    upgrade = tree_profiles.alembic(core_tree, scratch_schema, "upgrade", "heads")
    assert upgrade.returncode == 0, upgrade.stderr[-3000:]

    assert probe not in _tables(test_db, scratch_schema)
    # ... and the bootstrap, which shares the function, says the same.
    assert tree_profiles.bootstrap_schema(core_tree, scratch_schema).returncode == 0
    assert probe not in _tables(test_db, scratch_schema)


# ---------------------------------------------------------------------------
# 6. `downgrade` and `stamp` on a foreign-profile database
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize(
    "argv",
    [("downgrade", "-1"), ("stamp", "heads")],
    ids=["downgrade", "stamp"],
)
def test_a_core_build_does_not_pretend_to_downgrade_or_stamp(
    test_db, scratch_schema, core_tree, full_tree, argv
):
    """ "Succeed having done nothing" is honest for ``upgrade`` and nothing else.

    Both commands were inside the guard, whose test is upgrade-shaped ("this
    build's heads are all recorded, so there is nothing to apply"). A core image
    pointed at a full database therefore exited 0 from ``alembic downgrade -1``
    with the schema unchanged, and from ``alembic stamp heads`` -- the escape
    hatch the guard's own message and ``CONTRIBUTING.md`` point at -- with the version
    table unchanged.  They get alembic's own answer now, which names the
    revision it cannot resolve (review round 4, finding 3).
    """
    assert tree_profiles.bootstrap_schema(full_tree, scratch_schema).returncode == 0
    before = _rows(test_db, scratch_schema)
    tables_before = _tables(test_db, scratch_schema)

    result = tree_profiles.alembic(core_tree, scratch_schema, *argv)

    assert result.returncode != 0, result.stdout[-2000:]
    assert MODULES_HEAD in result.stderr
    assert _rows(test_db, scratch_schema) == before
    assert _tables(test_db, scratch_schema) == tables_before


@pytest.mark.regression
def test_a_core_build_can_stamp_a_full_database_with_purge(
    test_db, scratch_schema, core_tree, full_tree
):
    """The way out the guard's message describes, without editing the table by hand.

    ``stamp --purge heads`` empties ``alembic_version`` before it writes, so it
    is the one command that does not have to resolve the foreign row.  The guard
    swallowed it with the rest of ``stamp``.
    """
    assert tree_profiles.bootstrap_schema(full_tree, scratch_schema).returncode == 0
    tables_before = _tables(test_db, scratch_schema)

    result = tree_profiles.alembic(
        core_tree, scratch_schema, "stamp", "--purge", "heads"
    )

    assert result.returncode == 0, result.stderr[-3000:]
    assert _rows(test_db, scratch_schema) == CORE_HEADS
    assert _tables(test_db, scratch_schema) == tables_before
