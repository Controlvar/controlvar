"""``806901fb7735``: the experiments' stored correction settings, through the transitions.

The revision adds ``experiments.correction_method`` (``VARCHAR(32)``, default
``'benjamini_hochberg'``) and ``experiments.confidence_level`` (``double
precision``, default 0.95), both NOT NULL, and the checks
``ck_experiments_correction_method`` and ``ck_experiments_confidence_level``,
each only when absent. A schema built by ``create_all`` already has all four,
so the other transition tests -- which rewind such a schema's
``alembic_version`` -- never run this revision's DDL. Every test here starts
from what the previous release left instead: a bootstrapped schema with the
columns and the checks DROPPED and ``alembic_version`` at ``1ab99332f0ba``, in
each profile, and runs the documented ``alembic upgrade heads``. Then:

1. the ``alembic_version`` rows are this release's, the columns have the
   model's type and nullability, the experiments already stored are backfilled
   to Benjamini-Hochberg at 0.95, and autogenerate against the result is empty;
2. the upgraded database and a ``create_all``-bootstrapped one carry the same
   column defaults (``pg_get_expr``) and the same check constraints
   (``pg_get_constraintdef``), compared by name. Autogenerate compares
   neither (``env.py`` sets no ``compare_server_default`` and no check
   comparison is enabled), so (1) cannot see a model and a migration that
   disagree on these;
3. a writer that does not know the columns -- the previous release's image
   after a rollback with ``RUN_MIGRATIONS=false`` -- can still insert an
   experiment, and it gets the defaults;
4. ``downgrade`` removes exactly the two columns and the two checks, besides
   what the later revisions it also unapplies drop (``LATER_DOWNGRADE_TABLES``).
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
REVISION = "806901fb7735"
PREVIOUS_CORE_HEAD = "1ab99332f0ba"
#: The core head of this tree, which ``upgrade heads`` runs on to past this
#: revision, through ``d29a479daafe`` (#445), ``37dcb2969766`` (#440) and
#: ``4c2c09a2b3e8`` (#1206): ``607c51a59bf5`` (#1497), whose downgrade drops
#: three ``feature_flags`` columns and no table.
CORE_HEAD = "607c51a59bf5"
#: Tables that a later core revision's downgrade drops: a downgrade from the
#: head to this test's target unapplies those revisions too.  ``d29a479daafe``
#: (#445) drops ``holdout_population``; ``37dcb2969766`` (#440) drops
#: ``segment_members``.
LATER_DOWNGRADE_TABLES = {"holdout_population", "segment_members"}
#: The modules branch's head in this tree, recorded on both sides of every
#: step here: this file is about the core chain.
MODULES_HEAD = "modules_0003_lower_is_better"

PREVIOUS_ROWS = {
    CORE: {PREVIOUS_CORE_HEAD},
    FULL: {PREVIOUS_CORE_HEAD, MODULES_HEAD},
}
ROWS = {
    CORE: {CORE_HEAD},
    FULL: {CORE_HEAD, MODULES_HEAD},
}

COLUMNS = ("correction_method", "confidence_level")
CHECKS = ("ck_experiments_correction_method", "ck_experiments_confidence_level")

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
    yield from _scratch(test_db, "corr")


@pytest.fixture
def reference_schema(test_db):
    yield from _scratch(test_db, "corrref")


def _rows(engine, schema: str) -> set[str]:
    return bootstrap.recorded_revisions(engine, schema)


def _columns(engine, schema: str) -> dict:
    """name -> (data_type, is_nullable, character_maximum_length)."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT column_name, data_type, is_nullable, character_maximum_length "
                "FROM information_schema.columns "
                "WHERE table_schema = :s AND table_name = 'experiments' "
                "AND column_name IN ('correction_method', 'confidence_level')"
            ),
            {"s": schema},
        ).all()
    return {name: (kind, nullable, length) for name, kind, nullable, length in rows}


def _defaults(engine, schema: str) -> dict[str, str | None]:
    """Each new column's default expression, as ``pg_get_expr`` prints it."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT a.attname, pg_get_expr(d.adbin, d.adrelid) "
                "FROM pg_attribute a "
                "JOIN pg_class t ON t.oid = a.attrelid "
                "JOIN pg_namespace n ON n.oid = t.relnamespace "
                "LEFT JOIN pg_attrdef d "
                "ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
                "WHERE n.nspname = :s AND t.relname = 'experiments' "
                "AND a.attname IN ('correction_method', 'confidence_level') "
                "AND NOT a.attisdropped"
            ),
            {"s": schema},
        ).all()
    return dict(rows)


def _checks(engine, schema: str) -> dict[str, str]:
    """Every check constraint on *schema*.experiments: name -> definition."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT c.conname, pg_get_constraintdef(c.oid) "
                "FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid "
                "JOIN pg_namespace n ON n.oid = t.relnamespace "
                "WHERE n.nspname = :s AND t.relname = 'experiments' "
                "AND c.contype = 'c'"
            ),
            {"s": schema},
        ).all()
    return {name: definition.replace(schema, "<schema>") for name, definition in rows}


def _insert_without_the_columns(engine, schema: str) -> uuid.UUID:
    """An experiment written as the previous release writes it: the INSERT
    names neither new column."""
    experiment_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                f'INSERT INTO "{schema}".experiments (id, name, status, '
                "experiment_type, optimization_type, sequential_testing_enabled, "
                "bayesian_enabled, created_at, updated_at) VALUES (:id, :name, "
                "'ACTIVE', 'A_B', 'fixed', false, false, now(), now())"
            ),
            {"id": experiment_id, "name": f"corr-{experiment_id.hex[:8]}"},
        )
    return experiment_id


def _settings(engine, schema: str, experiment_id: uuid.UUID) -> tuple:
    with engine.connect() as conn:
        return tuple(
            conn.execute(
                text(
                    "SELECT correction_method, confidence_level "
                    f'FROM "{schema}".experiments WHERE id = :id'
                ),
                {"id": experiment_id},
            ).one()
        )


def _at_previous_release(engine, schema: str, rows: set[str]) -> None:
    """Turn a bootstrapped *schema* into what the previous release left."""
    with engine.begin() as conn:
        for check in CHECKS:
            conn.execute(
                text(f'ALTER TABLE "{schema}".experiments DROP CONSTRAINT "{check}"')
            )
        for column in COLUMNS:
            conn.execute(
                text(f'ALTER TABLE "{schema}".experiments DROP COLUMN {column}')
            )
        conn.execute(text(f'DELETE FROM "{schema}".alembic_version'))
        for revision in sorted(rows):
            conn.execute(
                text(f'INSERT INTO "{schema}".alembic_version VALUES (:rev)'),
                {"rev": revision},
            )
    assert _columns(engine, schema) == {}
    assert not set(CHECKS) & set(_checks(engine, schema))
    assert _rows(engine, schema) == rows


def _bootstrapped_at_previous_release(test_db, profile, schema, tmp_path):
    tree = tree_profiles.tree_for(profile, tmp_path)
    created = tree_profiles.bootstrap_schema(tree, schema)
    assert created.returncode == 0, created.stderr[-3000:]
    _at_previous_release(test_db, schema, PREVIOUS_ROWS[profile])
    return tree


def _upgrade(tree, schema: str) -> None:
    upgrade = tree_profiles.alembic(tree, schema, "upgrade", "heads")
    assert upgrade.returncode == 0, upgrade.stderr[-3000:]


# ---------------------------------------------------------------------------
# 1. The previous release, upgraded with `heads`
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_a_database_at_the_previous_release_gains_the_columns_and_is_backfilled(
    profile, test_db, scratch_schema, tmp_path
):
    tree = _bootstrapped_at_previous_release(test_db, profile, scratch_schema, tmp_path)
    existing = [_insert_without_the_columns(test_db, scratch_schema) for _ in range(2)]

    _upgrade(tree, scratch_schema)

    rows = _rows(test_db, scratch_schema)
    print(f"[{profile}] alembic_version after upgrade heads: {sorted(rows)}")
    assert rows == ROWS[profile]

    columns = _columns(test_db, scratch_schema)
    print(f"[{profile}] columns: {columns}")
    assert columns == {
        "correction_method": ("character varying", "NO", 32),
        "confidence_level": ("double precision", "NO", None),
    }
    checks = _checks(test_db, scratch_schema)
    print(f"[{profile}] checks: { {c: checks.get(c) for c in CHECKS} }")
    assert set(CHECKS) <= set(checks)

    # Every experiment already stored is judged by the defaults.
    for experiment_id in existing:
        assert _settings(test_db, scratch_schema, experiment_id) == (
            "benjamini_hochberg",
            0.95,
        )

    result = tree_profiles.autogenerate(
        tree, scratch_schema, tmp_path, head=tree_profiles.CORE_HEAD
    )
    assert result.body is not None, result.describe()
    assert result.operations == [], result.describe()


# ---------------------------------------------------------------------------
# 2. The migration's defaults and checks are the model's
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_an_upgraded_database_and_a_bootstrapped_one_have_the_same_defaults_and_checks(
    profile, test_db, scratch_schema, reference_schema, tmp_path
):
    """The only gate on these: autogenerate compares neither.

    A fresh deployment gets the columns from the model (``create_all``); an
    existing one gets them from this migration. A different default would
    give the two populations of databases a different correction for every
    experiment created by an older image; a different check would let one of
    them store a value the other refuses.
    """
    tree = _bootstrapped_at_previous_release(test_db, profile, scratch_schema, tmp_path)
    _upgrade(tree, scratch_schema)

    created = tree_profiles.bootstrap_schema(tree, reference_schema)
    assert created.returncode == 0, created.stderr[-3000:]

    upgraded = _defaults(test_db, scratch_schema)
    bootstrapped = _defaults(test_db, reference_schema)
    print(f"[{profile}] defaults upgraded:     {upgraded}")
    print(f"[{profile}] defaults bootstrapped: {bootstrapped}")
    assert bootstrapped == {
        "correction_method": "'benjamini_hochberg'::character varying",
        "confidence_level": "0.95",
    }
    assert upgraded == bootstrapped

    upgraded_checks = _checks(test_db, scratch_schema)
    bootstrapped_checks = _checks(test_db, reference_schema)
    print(f"[{profile}] checks upgraded:     {upgraded_checks}")
    print(f"[{profile}] checks bootstrapped: {bootstrapped_checks}")
    for check in CHECKS:
        assert check in bootstrapped_checks
        assert upgraded_checks[check] == bootstrapped_checks[check], check
    assert upgraded_checks == bootstrapped_checks


# ---------------------------------------------------------------------------
# 3. A writer that does not know the columns
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_an_insert_that_names_neither_column_gets_the_defaults(
    profile, test_db, scratch_schema, tmp_path
):
    """The previous release's image, rolled back to with RUN_MIGRATIONS=false,
    keeps creating experiments against this schema: without a server default
    every one of its INSERTs would fail the NOT NULL."""
    tree = _bootstrapped_at_previous_release(test_db, profile, scratch_schema, tmp_path)
    _upgrade(tree, scratch_schema)

    experiment_id = _insert_without_the_columns(test_db, scratch_schema)

    assert _settings(test_db, scratch_schema, experiment_id) == (
        "benjamini_hochberg",
        0.95,
    )


# ---------------------------------------------------------------------------
# 4. Downgrade
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_downgrade_removes_the_columns_and_the_checks_and_nothing_else(
    profile, test_db, scratch_schema, tmp_path
):
    tree = tree_profiles.tree_for(profile, tmp_path)
    assert tree_profiles.bootstrap_schema(tree, scratch_schema).returncode == 0
    tables = set(inspect(test_db).get_table_names(schema=scratch_schema))
    checks = _checks(test_db, scratch_schema)
    assert set(CHECKS) <= set(checks)
    assert set(_columns(test_db, scratch_schema)) == set(COLUMNS)

    down = tree_profiles.alembic(tree, scratch_schema, "downgrade", PREVIOUS_CORE_HEAD)
    assert down.returncode == 0, down.stderr[-3000:]

    assert _rows(test_db, scratch_schema) == PREVIOUS_ROWS[profile]
    assert _columns(test_db, scratch_schema) == {}
    assert _checks(test_db, scratch_schema) == {
        name: definition for name, definition in checks.items() if name not in CHECKS
    }
    assert (
        set(inspect(test_db).get_table_names(schema=scratch_schema))
        == tables - LATER_DOWNGRADE_TABLES
    )
