"""``4955571d232c``: a visual experiment's columns and the editor's tokens, through
the transitions (#1545).

The revision adds ``experiments.visual_config`` and ``variants.visual_changes``
(``jsonb``, nullable, no default) and the table ``visual_tokens`` with its two
checks, three foreign keys, unique ``token_hash`` and two indexes, each only
when absent. A schema built by ``create_all`` already has them, so the tests
that start from the previous release take a bootstrapped schema with the
three DROPPED and ``alembic_version`` at ``c448_balance_within``, in each profile, and
run the documented ``alembic upgrade heads``. Then:

1. the ``alembic_version`` rows are this release's (printed), the columns and
   the table exist, a stored experiment and its variants are untouched, and
   autogenerate is EMPTY on that upgraded database;
2. the upgraded database and a ``create_all``-bootstrapped one have the same
   columns, constraints and indexes, from the catalog (autogenerate compares
   neither checks nor a unique constraint's name);
3. the previous release's inserts -- which name neither column -- still
   store an experiment and its variants against this schema (pure expand);
4. the downgrade removes exactly the table and the two columns, keeps every
   experiment and variant, and an upgrade afterwards adds them back empty;
5. ``alembic heads`` names exactly this revision and the modules head.

The previous release refusing this database and starting after the
downgrade is ``test_newer_database_rollback.py``'s, which holds every later
core revision's downgrade to it.
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
REVISION = "4955571d232c"
PREVIOUS_CORE_HEAD = "c448_balance_within"
#: The core head of this tree.
CORE_HEAD = "4955571d232c"
MODULES_HEAD = "modules_0004_rbac_activation"

PREVIOUS_ROWS = {
    CORE: {PREVIOUS_CORE_HEAD},
    FULL: {PREVIOUS_CORE_HEAD, MODULES_HEAD},
}
ROWS = {CORE: {CORE_HEAD}, FULL: {CORE_HEAD, MODULES_HEAD}}

TABLE = "visual_tokens"
#: (table, column) of each column the revision adds.
COLUMNS = (("experiments", "visual_config"), ("variants", "visual_changes"))

BOTH_PROFILES = [CORE, pytest.param(FULL, marks=pytest.mark.modules)]


def _scratch(test_db, prefix: str):
    schema = f"{prefix}_{uuid.uuid4().hex[:8]}"
    try:
        yield schema
    finally:
        with test_db.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


@pytest.fixture
def scratch_schema(test_db):
    yield from _scratch(test_db, "visualstore")


@pytest.fixture
def reference_schema(test_db):
    yield from _scratch(test_db, "visualstoreref")


def _rows(engine, schema: str) -> set[str]:
    return bootstrap.recorded_revisions(engine, schema)


def _tables(engine, schema: str) -> set[str]:
    return set(inspect(engine).get_table_names(schema=schema))


def _column(engine, schema: str, table: str, column: str):
    """(format_type, NOT NULL, default) of *column*, or ``None``."""
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT format_type(a.atttypid, a.atttypmod), a.attnotnull, "
                "pg_get_expr(d.adbin, d.adrelid) "
                "FROM pg_attribute a "
                "JOIN pg_class t ON t.oid = a.attrelid "
                "JOIN pg_namespace n ON n.oid = t.relnamespace "
                "LEFT JOIN pg_attrdef d "
                "ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
                "WHERE n.nspname = :s AND t.relname = :t AND a.attname = :c "
                "AND NOT a.attisdropped"
            ),
            {"s": schema, "t": table, "c": column},
        ).one_or_none()
    return tuple(row) if row is not None else None


def _table_definition(engine, schema: str):
    """``visual_tokens`` as the catalog describes it: its columns in order,
    its constraints and its indexes, with the schema name replaced."""
    with engine.connect() as conn:
        columns = conn.execute(
            text(
                "SELECT a.attname, format_type(a.atttypid, a.atttypmod), "
                "a.attnotnull, pg_get_expr(d.adbin, d.adrelid) "
                "FROM pg_attribute a "
                "JOIN pg_class t ON t.oid = a.attrelid "
                "JOIN pg_namespace n ON n.oid = t.relnamespace "
                "LEFT JOIN pg_attrdef d "
                "ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
                "WHERE n.nspname = :s AND t.relname = :t AND a.attnum > 0 "
                "AND NOT a.attisdropped ORDER BY a.attnum"
            ),
            {"s": schema, "t": TABLE},
        ).all()
        constraints = conn.execute(
            text(
                "SELECT c.conname, c.contype, pg_get_constraintdef(c.oid) "
                "FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid "
                "JOIN pg_namespace n ON n.oid = t.relnamespace "
                "WHERE n.nspname = :s AND t.relname = :t"
            ),
            {"s": schema, "t": TABLE},
        ).all()
        indexes = conn.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname = :s AND tablename = :t"
            ),
            {"s": schema, "t": TABLE},
        ).all()
    return (
        [tuple(row) for row in columns],
        {
            (name, kind, definition.replace(schema, "<schema>"))
            for name, kind, definition in constraints
        },
        {row[0].replace(schema, "<schema>") for row in indexes},
    )


def _drop_what_the_revision_adds(engine, schema: str) -> None:
    with engine.begin() as conn:
        conn.execute(text(f'DROP TABLE IF EXISTS "{schema}".{TABLE}'))
        for table, column in COLUMNS:
            conn.execute(
                text(f'ALTER TABLE "{schema}".{table} DROP COLUMN IF EXISTS {column}')
            )


def _stamp(engine, schema: str, rows: set[str]) -> None:
    with engine.begin() as conn:
        conn.execute(text(f'DELETE FROM "{schema}".alembic_version'))
        for revision in sorted(rows):
            conn.execute(
                text(f'INSERT INTO "{schema}".alembic_version VALUES (:rev)'),
                {"rev": revision},
            )


def _bootstrapped(profile, schema, tmp_path):
    tree = tree_profiles.tree_for(profile, tmp_path)
    created = tree_profiles.bootstrap_schema(tree, schema)
    assert created.returncode == 0, created.stderr[-3000:]
    return tree


def _at_previous_release(test_db, profile, schema, tmp_path):
    tree = _bootstrapped(profile, schema, tmp_path)
    _drop_what_the_revision_adds(test_db, schema)
    _stamp(test_db, schema, PREVIOUS_ROWS[profile])
    assert TABLE not in _tables(test_db, schema)
    assert all(_column(test_db, schema, t, c) is None for t, c in COLUMNS)
    return tree


def _upgrade(tree, schema: str) -> None:
    upgrade = tree_profiles.alembic(tree, schema, "upgrade", "heads")
    assert upgrade.returncode == 0, upgrade.stderr[-3000:]


def _insert_experiment(engine, schema: str) -> str:
    """An experiment with two variants, as the previous release inserts them:
    naming neither of this revision's columns."""
    experiment_id = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(
            text(
                f'INSERT INTO "{schema}".experiments '
                "(id, name, key, status, experiment_type, optimization_type, "
                "sequential_testing_enabled, bayesian_enabled, created_at, "
                "updated_at) VALUES (:id, 'Previous release', :key, 'DRAFT', "
                "'A_B', 'fixed', false, false, now(), now())"
            ),
            {"id": experiment_id, "key": f"previous-{experiment_id[:8]}"},
        )
        for name, control in (("Control", True), ("B", False)):
            conn.execute(
                text(
                    f'INSERT INTO "{schema}".variants '
                    "(id, experiment_id, name, is_control, traffic_allocation, "
                    "created_at, updated_at) VALUES (:id, :e, :n, :c, 50, now(), now())"
                ),
                {"id": str(uuid.uuid4()), "e": experiment_id, "n": name, "c": control},
            )
    return experiment_id


def _experiments(engine, schema: str) -> set[tuple]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                f'SELECT e.id, e.name, count(v.id) FROM "{schema}".experiments e '
                f'LEFT JOIN "{schema}".variants v ON v.experiment_id = e.id '
                "GROUP BY e.id, e.name"
            )
        ).all()
    return {tuple(row) for row in rows}


def _report(profile, label, engine, schema) -> None:
    print(f"[{profile}] {label}: alembic_version={sorted(_rows(engine, schema))}")
    for table, column in COLUMNS:
        print(
            f"[{profile}] {label}: {table}.{column}={_column(engine, schema, table, column)}"
        )
    print(f"[{profile}] {label}: {TABLE} present={TABLE in _tables(engine, schema)}")


# ---------------------------------------------------------------------------
# 1. The previous release, upgraded with `heads`
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_a_database_at_the_previous_release_gains_the_columns_and_the_table(
    profile, test_db, scratch_schema, tmp_path
):
    tree = _at_previous_release(test_db, profile, scratch_schema, tmp_path)
    _insert_experiment(test_db, scratch_schema)
    stored = _experiments(test_db, scratch_schema)
    _report(profile, "at the previous release", test_db, scratch_schema)

    _upgrade(tree, scratch_schema)
    _report(profile, "after upgrade heads", test_db, scratch_schema)

    assert _rows(test_db, scratch_schema) == ROWS[profile]
    for table, column in COLUMNS:
        assert _column(test_db, scratch_schema, table, column) == ("jsonb", False, None)
    assert TABLE in _tables(test_db, scratch_schema)
    assert _experiments(test_db, scratch_schema) == stored

    result = tree_profiles.autogenerate(
        tree, scratch_schema, tmp_path, head=tree_profiles.CORE_HEAD
    )
    print(f"[{profile}] autogenerate on the upgraded database: {result.operations}")
    assert result.body is not None, result.describe()
    assert result.operations == [], result.describe()


# ---------------------------------------------------------------------------
# 2. The migration builds what create_all builds
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_an_upgraded_database_and_a_bootstrapped_one_are_the_same(
    profile, test_db, scratch_schema, reference_schema, tmp_path
):
    tree = _at_previous_release(test_db, profile, scratch_schema, tmp_path)
    _upgrade(tree, scratch_schema)
    created = tree_profiles.bootstrap_schema(tree, reference_schema)
    assert created.returncode == 0, created.stderr[-3000:]

    upgraded = _table_definition(test_db, scratch_schema)
    bootstrapped = _table_definition(test_db, reference_schema)
    print(f"[{profile}] constraints: {sorted(upgraded[1])}")
    print(f"[{profile}] indexes: {sorted(upgraded[2])}")
    assert upgraded == bootstrapped
    names = {name for name, _, _ in upgraded[1]}
    assert {"ck_visual_tokens_kind", "ck_visual_tokens_revoked_reason"} <= names
    assert len(upgraded[2]) == 4  # the primary key, token_hash, and the two indexes
    for table, column in COLUMNS:
        assert _column(test_db, scratch_schema, table, column) == _column(
            test_db, reference_schema, table, column
        )


# ---------------------------------------------------------------------------
# 3. The previous release's writes against this schema
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_the_previous_release_still_stores_experiments_on_this_schema(
    profile, test_db, scratch_schema, tmp_path
):
    """Pure expand: both columns are nullable with no default, so an insert
    that names neither still stores the row, with both null."""
    _bootstrapped(profile, scratch_schema, tmp_path)
    experiment_id = _insert_experiment(test_db, scratch_schema)
    with test_db.connect() as conn:
        config = conn.execute(
            text(
                f'SELECT visual_config FROM "{scratch_schema}".experiments WHERE id = :i'
            ),
            {"i": experiment_id},
        ).scalar_one()
        changes = (
            conn.execute(
                text(
                    f'SELECT visual_changes FROM "{scratch_schema}".variants '
                    "WHERE experiment_id = :i"
                ),
                {"i": experiment_id},
            )
            .scalars()
            .all()
        )
    assert config is None and changes == [None, None]


# ---------------------------------------------------------------------------
# 4. Downgrade
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_the_downgrade_drops_the_table_and_both_columns_only(
    profile, test_db, scratch_schema, tmp_path
):
    tree = _bootstrapped(profile, scratch_schema, tmp_path)
    experiment_id = _insert_experiment(test_db, scratch_schema)
    with test_db.begin() as conn:
        conn.execute(
            text(
                f'UPDATE "{scratch_schema}".experiments SET visual_config = '
                "CAST(:c AS jsonb) WHERE id = :i"
            ),
            {"c": '{"version": 1}', "i": experiment_id},
        )
        conn.execute(
            text(
                f'INSERT INTO "{scratch_schema}".{TABLE} (id, kind, token_hash, '
                "experiment_id, expires_at) VALUES (:id, 'preview', :h, :e, now())"
            ),
            {"id": str(uuid.uuid4()), "h": "0" * 64, "e": experiment_id},
        )
    tables = _tables(test_db, scratch_schema)
    stored = _experiments(test_db, scratch_schema)

    down = tree_profiles.alembic(tree, scratch_schema, "downgrade", PREVIOUS_CORE_HEAD)
    assert down.returncode == 0, down.stderr[-3000:]
    _report(profile, "after downgrade", test_db, scratch_schema)

    assert _rows(test_db, scratch_schema) == PREVIOUS_ROWS[profile]
    assert _tables(test_db, scratch_schema) == tables - {TABLE}
    assert all(_column(test_db, scratch_schema, t, c) is None for t, c in COLUMNS)
    assert _experiments(test_db, scratch_schema) == stored

    _upgrade(tree, scratch_schema)
    _report(profile, "after upgrade heads again", test_db, scratch_schema)
    assert _rows(test_db, scratch_schema) == ROWS[profile]
    with test_db.connect() as conn:
        assert (
            conn.execute(
                text(f'SELECT count(*) FROM "{scratch_schema}".{TABLE}')
            ).scalar_one()
            == 0
        )
        assert (
            conn.execute(
                text(
                    f'SELECT visual_config FROM "{scratch_schema}".experiments WHERE id = :i'
                ),
                {"i": experiment_id},
            ).scalar_one()
            is None
        )


# ---------------------------------------------------------------------------
# 5. The heads
# ---------------------------------------------------------------------------
HEADS = {CORE: {CORE_HEAD}, FULL: {CORE_HEAD, MODULES_HEAD}}


@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_alembic_heads_are_this_revision_and_the_modules_head(profile, tmp_path):
    tree = tree_profiles.tree_for(profile, tmp_path)
    heads = tree_profiles.alembic(tree, "public", "heads")
    assert heads.returncode == 0, heads.stderr[-3000:]
    print(f"[{profile}] alembic heads:\n{heads.stdout}")
    named = {line.split()[0] for line in heads.stdout.splitlines() if line.strip()}
    assert named == HEADS[profile]
