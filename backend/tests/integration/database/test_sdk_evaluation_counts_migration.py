"""The first core revision after the branch point, through every transition.

``8fd44fb483a2`` adds the two SDK evaluation counter tables
(``sdk_evaluation_key_counts``, ``sdk_evaluation_flag_counts``) on top of the
core marker ``b8c9d0e1f2a3``.  It is the first revision to extend the core
chain since the ``modules`` branch was hung off ``a7b8c9d0e1f2``, so it is the
first time a database at the *previous release* has something core to apply.
Each test starts from that previous-release state -- a bootstrapped schema with
the two tables dropped and ``alembic_version`` set to what the previous release
recorded -- and runs the documented command:

    1. a full database, the full image, ``alembic upgrade heads``
    2. a core database, the core image, ``alembic upgrade heads``
    3. a core database, the full image (the profile switch), ``upgrade heads``
    4. a full database, the core image: refused, nothing applied or removed
    5. the migration and the models agree: autogenerate after (1) is empty
    6. ``downgrade`` removes exactly the two tables

and prints nothing it does not assert: the ``alembic_version`` rows and the
tables after each.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import inspect, text

from backend.app.db import bootstrap
from backend.tests.integration.database import tree_profiles
from backend.tests.integration.database.tree_profiles import CORE, FULL

pytestmark = [pytest.mark.integration]

#: This revision, and the core revision it extends.
REVISION = "8fd44fb483a2"
PREVIOUS_CORE_HEAD = "b8c9d0e1f2a3"
#: The core head of this tree: ``upgrade heads`` runs on past this revision to
#: ``607c51a59bf5`` (feature flags' value type and two values), through
#: ``271f03a31742``, ``d12cbd384bbe``, ``a89544fb1075``, ``1ab99332f0ba``,
#: ``806901fb7735`` (the experiments' correction settings), ``d29a479daafe``
#: (holdout population), ``37dcb2969766`` (segment kind and members) and
#: ``4c2c09a2b3e8`` (rollback records keep without a safety configuration); a
#: database built by ``create_all`` already carries what they build, so they
#: add nothing here.
CORE_HEAD = "607c51a59bf5"
#: Tables that a later core revision's downgrade drops: a downgrade from the
#: head to this test's target unapplies those revisions too.  ``d29a479daafe``
#: (#445) drops ``holdout_population``; ``37dcb2969766`` (#440) drops
#: ``segment_members``.
LATER_DOWNGRADE_TABLES = {"holdout_population", "segment_members"}
#: The modules revision the previous full release (0.10.0) recorded, and the
#: branch's head in this one.  This release also carries
#: ``modules_0002_warehouse_analysis`` (#312), so a full database at the
#: previous release has a revision to apply on each line.
PREVIOUS_MODULES_REVISION = "modules_0001_rbac"
MODULES_HEAD = "modules_0002_warehouse_analysis"
#: Tables ``modules_0002_warehouse_analysis`` creates; the previous release has
#: neither.
WAREHOUSE_TABLES = {"warehouse_sources", "warehouse_analysis_runs"}

#: What each profile's previous release recorded, and what this one records.
PREVIOUS_CORE_ROWS = {PREVIOUS_CORE_HEAD}
PREVIOUS_FULL_ROWS = {PREVIOUS_CORE_HEAD, PREVIOUS_MODULES_REVISION}
CORE_ROWS = {CORE_HEAD}
FULL_ROWS = {CORE_HEAD, MODULES_HEAD}

TABLES = {"sdk_evaluation_key_counts", "sdk_evaluation_flag_counts"}


@pytest.fixture
def scratch_schema(test_db):
    schema = f"sdkcount_{uuid.uuid4().hex[:8]}"
    try:
        yield schema
    finally:
        with test_db.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


@pytest.fixture
def core_tree(tmp_path):
    return tree_profiles.tree_for(CORE, tmp_path)


@pytest.fixture
def full_tree():
    return tree_profiles.tree_for(FULL, None)


def _rows(engine, schema: str) -> set[str]:
    return bootstrap.recorded_revisions(engine, schema)


def _tables(engine, schema: str) -> set[str]:
    return set(inspect(engine).get_table_names(schema=schema))


def _key_counts_fk(engine, schema: str) -> list[tuple[str, str]]:
    """(referred table, ondelete) for each foreign key on the per-key counter."""
    return sorted(
        (fk["referred_table"], (fk.get("options") or {}).get("ondelete", ""))
        for fk in inspect(engine).get_foreign_keys(
            "sdk_evaluation_key_counts", schema=schema
        )
    )


def _at_previous_release(engine, schema: str, rows: set[str]) -> None:
    """Turn a bootstrapped *schema* into what the previous release left."""
    with engine.begin() as conn:
        for table in sorted(TABLES):
            conn.execute(text(f'DROP TABLE "{schema}"."{table}"'))
        conn.execute(text(f'DELETE FROM "{schema}".alembic_version'))
        for revision in sorted(rows):
            conn.execute(
                text(f'INSERT INTO "{schema}".alembic_version VALUES (:rev)'),
                {"rev": revision},
            )
    assert not TABLES & _tables(engine, schema)
    assert _rows(engine, schema) == rows


def _modules_at_previous_release(engine, schema: str) -> None:
    """Give a bootstrapped full *schema* the previous release's modules tables.

    The builder ``test_modules_0002_transitions.py`` uses for exactly that
    state: the two new warehouse tables gone and the earlier
    ``warehouse_connections`` back, holding saved rows.  (``downgrade
    modules@-1`` cannot do it here: it leaves tables the bootstrap built.)
    ``_at_previous_release`` then sets the core row and removes this revision's
    tables.
    """
    from backend.tests.integration.database.test_modules_0002_transitions import (
        _make_previous_full_release_database,
    )

    _make_previous_full_release_database(engine, schema)


def _assert_migrated(engine, schema: str, rows: set[str]) -> None:
    assert _rows(engine, schema) == rows
    assert TABLES <= _tables(engine, schema)
    assert _key_counts_fk(engine, schema) == [("api_keys", "CASCADE")]


# ---------------------------------------------------------------------------
# 1. Full database, full image
# ---------------------------------------------------------------------------
@pytest.mark.modules
def test_a_full_database_at_the_previous_release_upgrades_with_heads(
    test_db, scratch_schema, full_tree
):
    assert tree_profiles.bootstrap_schema(full_tree, scratch_schema).returncode == 0
    _modules_at_previous_release(test_db, scratch_schema)
    _at_previous_release(test_db, scratch_schema, PREVIOUS_FULL_ROWS)
    assert not WAREHOUSE_TABLES & _tables(test_db, scratch_schema)

    upgrade = tree_profiles.alembic(full_tree, scratch_schema, "upgrade", "heads")
    assert upgrade.returncode == 0, upgrade.stderr[-3000:]

    # Both lines moved: this revision and the modules branch's newest one.
    _assert_migrated(test_db, scratch_schema, FULL_ROWS)
    assert WAREHOUSE_TABLES <= _tables(test_db, scratch_schema)


# ---------------------------------------------------------------------------
# 2. Core database, core image
# ---------------------------------------------------------------------------
def test_a_core_database_at_the_previous_release_upgrades_with_heads(
    test_db, scratch_schema, core_tree
):
    assert tree_profiles.bootstrap_schema(core_tree, scratch_schema).returncode == 0
    _at_previous_release(test_db, scratch_schema, PREVIOUS_CORE_ROWS)

    upgrade = tree_profiles.alembic(core_tree, scratch_schema, "upgrade", "heads")
    assert upgrade.returncode == 0, upgrade.stderr[-3000:]

    _assert_migrated(test_db, scratch_schema, CORE_ROWS)


# ---------------------------------------------------------------------------
# 3. Core database at the previous release, full image
# ---------------------------------------------------------------------------
@pytest.mark.modules
def test_a_previous_core_database_switched_to_full_gets_both(
    test_db, scratch_schema, core_tree, full_tree
):
    from backend.app.db.autogenerate_filters import MODULE_TABLES

    assert tree_profiles.bootstrap_schema(core_tree, scratch_schema).returncode == 0
    _at_previous_release(test_db, scratch_schema, PREVIOUS_CORE_ROWS)

    upgrade = tree_profiles.alembic(full_tree, scratch_schema, "upgrade", "heads")
    assert upgrade.returncode == 0, upgrade.stderr[-3000:]

    _assert_migrated(test_db, scratch_schema, FULL_ROWS)
    # 14: every module table, the two warehouse tables of #312 included.
    assert len(MODULE_TABLES & _tables(test_db, scratch_schema)) == 14


# ---------------------------------------------------------------------------
# 4. Full database at the previous release, core image
# ---------------------------------------------------------------------------
@pytest.mark.modules
def test_a_core_image_refuses_a_previous_full_database_and_changes_nothing(
    test_db, scratch_schema, core_tree, full_tree
):
    """The core chain has a revision to apply, so this is not "nothing to do".

    Before this revision a core image met a full database at the previous
    release with nothing to apply and exited 0.  Now it has ``8fd44fb483a2`` to
    apply and cannot plan past ``modules_0001_rbac``, a revision it has no file
    for, so it refuses and names the way out -- the guard's designed answer
    (``test_profile_transitions.py`` case 4) -- and applies nothing.
    """
    assert tree_profiles.bootstrap_schema(full_tree, scratch_schema).returncode == 0
    _modules_at_previous_release(test_db, scratch_schema)
    _at_previous_release(test_db, scratch_schema, PREVIOUS_FULL_ROWS)
    tables_before = _tables(test_db, scratch_schema)

    upgrade = tree_profiles.alembic(core_tree, scratch_schema, "upgrade", "heads")

    assert upgrade.returncode != 0, upgrade.stdout[-2000:]
    assert PREVIOUS_MODULES_REVISION in upgrade.stderr
    assert "Run the full image of this release against it" in upgrade.stderr
    assert _rows(test_db, scratch_schema) == PREVIOUS_FULL_ROWS
    assert _tables(test_db, scratch_schema) == tables_before

    # ... and the full image of the same release then completes it.
    finish = tree_profiles.alembic(full_tree, scratch_schema, "upgrade", "heads")
    assert finish.returncode == 0, finish.stderr[-3000:]
    _assert_migrated(test_db, scratch_schema, FULL_ROWS)
    assert WAREHOUSE_TABLES <= _tables(test_db, scratch_schema)


# ---------------------------------------------------------------------------
# 5. The migration builds what the models declare
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_autogenerate_after_the_upgrade_is_empty(
    test_db, scratch_schema, core_tree, tmp_path
):
    """The migration's tables, keys and indexes are the models', exactly.

    ``test_autogenerate_is_empty.py`` bootstraps with ``create_all``, which
    builds these tables from the models and so cannot tell whether *this
    migration* matches them.  Here the tables come from the migration.
    """
    assert tree_profiles.bootstrap_schema(core_tree, scratch_schema).returncode == 0
    _at_previous_release(test_db, scratch_schema, PREVIOUS_CORE_ROWS)
    assert (
        tree_profiles.alembic(core_tree, scratch_schema, "upgrade", "heads").returncode
        == 0
    )

    result = tree_profiles.autogenerate(
        core_tree, scratch_schema, tmp_path, head=tree_profiles.CORE_HEAD
    )

    assert result.body is not None, result.describe()
    assert result.operations == [], result.describe()


# ---------------------------------------------------------------------------
# 6. Downgrade
# ---------------------------------------------------------------------------
def test_downgrade_removes_the_two_tables_and_nothing_else(
    test_db, scratch_schema, core_tree
):
    assert tree_profiles.bootstrap_schema(core_tree, scratch_schema).returncode == 0
    before = _tables(test_db, scratch_schema)
    assert TABLES <= before

    down = tree_profiles.alembic(
        core_tree, scratch_schema, "downgrade", PREVIOUS_CORE_HEAD
    )
    assert down.returncode == 0, down.stderr[-3000:]

    assert _rows(test_db, scratch_schema) == PREVIOUS_CORE_ROWS
    assert _tables(test_db, scratch_schema) == before - TABLES - LATER_DOWNGRADE_TABLES
