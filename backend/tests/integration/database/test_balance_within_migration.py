"""``c448_balance_within``: balance the split within groups, through the transitions (#448).

The revision adds ``experiments.balance_within`` and ``experiments.balance_seed``,
``assignments.balance_group`` and ``assignments.balance_seq`` (all nullable, no
default) and the table ``balance_group_counters``, each only when absent. A
schema built by ``create_all`` already has them, so the other transition tests
-- which rewind such a schema's ``alembic_version`` -- never run the DDL. The
tests that start from the previous release instead take a bootstrapped schema
with the five objects DROPPED and ``alembic_version`` at ``7b6b4b4fe246``, in
each profile, and run the documented ``alembic upgrade heads``. Then:

1. the ``alembic_version`` rows are this release's, every object has the
   model's type and nullability, every experiment balances nothing, and
   autogenerate is EMPTY on that upgraded database;
2. the upgraded database and a ``create_all``-bootstrapped one have the same
   columns (``format_type``, NOT NULL, ``pg_get_expr``) and the same counter
   table, primary key and foreign key (ON DELETE CASCADE);
3. the previous release's statements -- INSERTs and SELECTs naming only the
   columns it knew -- store and read experiments and assignments on this
   schema, and this release reads such a row of a balanced experiment as one
   assigned outside the blocks;
4. the downgrade removes exactly the four columns and the table, every
   balance setting, group and counter with them, and does not refuse; an
   upgrade afterwards gives every experiment no setting again;
5. the upgrade refuses offline (``--sql``) mode: it reads the catalog;
6. ``alembic heads`` names exactly the core head, ``4955571d232c`` (#1545)
   since it landed, and the modules head.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import inspect, text

from backend.app.db import bootstrap
from backend.tests.integration.database import tree_profiles
from backend.tests.integration.database.tree_profiles import CORE, FULL

pytestmark = [pytest.mark.integration]

#: This revision, and the core revision it extends: ``7b6b4b4fe246`` (#1528).
REVISION = "c448_balance_within"
PREVIOUS_CORE_HEAD = "7b6b4b4fe246"
#: The core head of this tree: ``4955571d232c`` (#1545), the revision after
#: this one, whose downgrade drops ``visual_tokens``,
#: ``experiments.visual_config`` and ``variants.visual_changes``.
CORE_HEAD = "4955571d232c"
#: What a later core revision's downgrade drops: a downgrade from the head to
#: this test's target unapplies ``4955571d232c`` too.
LATER_DOWNGRADE_TABLES = {"visual_tokens"}
LATER_DOWNGRADE_EXPERIMENT_COLUMNS = {"visual_config"}
#: The modules branch's head in this tree, recorded on both sides of every
#: step here: this file is about the core chain.
MODULES_HEAD = "modules_0004_rbac_activation"

PREVIOUS_ROWS = {
    CORE: {PREVIOUS_CORE_HEAD},
    FULL: {PREVIOUS_CORE_HEAD, MODULES_HEAD},
}
ROWS = {
    CORE: {CORE_HEAD},
    FULL: {CORE_HEAD, MODULES_HEAD},
}
HEADS = {CORE: {CORE_HEAD}, FULL: {CORE_HEAD, MODULES_HEAD}}

COUNTERS = "balance_group_counters"
#: (table, column) -> (``format_type``, NOT NULL, ``pg_get_expr`` of its default).
EXPECTED_COLUMNS = {
    ("experiments", "balance_within"): ("jsonb", False, None),
    ("experiments", "balance_seed"): ("character varying(64)", False, None),
    ("assignments", "balance_group"): ("character varying(100)", False, None),
    ("assignments", "balance_seq"): ("bigint", False, None),
}
EXPECTED_COUNTER_COLUMNS = {
    "experiment_id": ("uuid", True, None),
    "balance_group": ("character varying(100)", True, None),
    "next_seq": ("bigint", True, None),
}

UPGRADED = "upgraded"
BOOTSTRAPPED = "bootstrapped"

#: The full-profile case needs a ``modules/`` directory; the core one builds
#: its own sealed tree and runs in either checkout.
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
    yield from _scratch(test_db, "balance")


@pytest.fixture
def reference_schema(test_db):
    yield from _scratch(test_db, "balanceref")


def _rows(engine, schema: str) -> set[str]:
    return bootstrap.recorded_revisions(engine, schema)


def _column(engine, schema: str, table: str, column: str):
    """(format_type, NOT NULL, default) of *table*.*column*, or ``None``."""
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


def _columns(engine, schema: str) -> dict:
    return {key: _column(engine, schema, *key) for key in EXPECTED_COLUMNS}


def _counter_table(engine, schema: str):
    """The counter table's columns, primary key and foreign keys, or ``None``."""
    inspector = inspect(engine)
    if not inspector.has_table(COUNTERS, schema=schema):
        return None
    columns = {
        name: _column(engine, schema, COUNTERS, name)
        for name in EXPECTED_COUNTER_COLUMNS
    }
    primary = inspector.get_pk_constraint(COUNTERS, schema=schema)[
        "constrained_columns"
    ]
    foreign = [
        (
            tuple(fk["constrained_columns"]),
            fk["referred_table"],
            tuple(fk["referred_columns"]),
            (fk.get("options") or {}).get("ondelete"),
        )
        for fk in inspector.get_foreign_keys(COUNTERS, schema=schema)
    ]
    return columns, tuple(primary), sorted(foreign)


EXPECTED_COUNTER_TABLE = (
    EXPECTED_COUNTER_COLUMNS,
    ("experiment_id", "balance_group"),
    [(("experiment_id",), "experiments", ("id",), "CASCADE")],
)


def _all_columns(engine, schema: str, table: str) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns(table, schema=schema)}


def _tables(engine, schema: str) -> set[str]:
    return set(inspect(engine).get_table_names(schema=schema))


def _drop_objects(engine, schema: str) -> None:
    with engine.begin() as conn:
        conn.execute(text(f'DROP TABLE IF EXISTS "{schema}".{COUNTERS}'))
        for table, column in EXPECTED_COLUMNS:
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


def _bootstrapped_at_previous_release(test_db, profile, schema, tmp_path):
    """A bootstrapped *schema* turned into what the previous release left."""
    tree = _bootstrapped(profile, schema, tmp_path)
    _drop_objects(test_db, schema)
    _stamp(test_db, schema, PREVIOUS_ROWS[profile])
    assert set(_columns(test_db, schema).values()) == {None}
    assert _counter_table(test_db, schema) is None
    assert _rows(test_db, schema) == PREVIOUS_ROWS[profile]
    return tree


def _upgrade(tree, schema: str) -> None:
    upgrade = tree_profiles.alembic(tree, schema, "upgrade", "heads")
    assert upgrade.returncode == 0, upgrade.stderr[-3000:]


def _report(profile, label, engine, schema) -> None:
    print(f"[{profile}] {label}: alembic_version={sorted(_rows(engine, schema))}")
    print(f"[{profile}] {label}: columns={_columns(engine, schema)}")
    print(f"[{profile}] {label}: {COUNTERS}={_counter_table(engine, schema)}")


def _previous_release_rows(engine, schema: str) -> dict:
    """An experiment, two variants and an assignment, written as the previous
    release writes them: naming only the columns it knew."""
    ids = {
        "experiment": str(uuid.uuid4()),
        "control": str(uuid.uuid4()),
        "treatment": str(uuid.uuid4()),
        "assignment": str(uuid.uuid4()),
    }
    with engine.begin() as conn:
        conn.execute(
            text(
                f'INSERT INTO "{schema}".experiments (id, name, key, status, '
                "experiment_type, optimization_type, sequential_testing_enabled, "
                "bayesian_enabled, created_at, updated_at) VALUES (:id, 'Previous', "
                ":key, 'ACTIVE', 'A_B', 'fixed', false, false, now(), now())"
            ),
            {"id": ids["experiment"], "key": f"previous-{ids['experiment'][:8]}"},
        )
        for name, is_control in (("control", True), ("treatment", False)):
            conn.execute(
                text(
                    f'INSERT INTO "{schema}".variants (id, experiment_id, name, '
                    "is_control, traffic_allocation, created_at, updated_at) "
                    "VALUES (:id, :e, :name, :control, 50, now(), now())"
                ),
                {
                    "id": ids[name],
                    "e": ids["experiment"],
                    "name": name,
                    "control": is_control,
                },
            )
        conn.execute(
            text(
                f'INSERT INTO "{schema}".assignments (id, experiment_id, variant_id, '
                "user_id, context, created_at, updated_at) VALUES (:id, :e, :v, "
                "'previous-user', CAST('{\"tier\": \"gold\"}' AS jsonb), now(), now())"
            ),
            {"id": ids["assignment"], "e": ids["experiment"], "v": ids["control"]},
        )
    return ids


# ---------------------------------------------------------------------------
# 1. The previous release, upgraded with `heads`
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_a_database_at_the_previous_release_gains_the_columns_and_the_table(
    profile, test_db, scratch_schema, tmp_path
):
    tree = _bootstrapped_at_previous_release(test_db, profile, scratch_schema, tmp_path)
    ids = _previous_release_rows(test_db, scratch_schema)
    _report(profile, "at the previous release", test_db, scratch_schema)

    _upgrade(tree, scratch_schema)
    _report(profile, "after upgrade heads", test_db, scratch_schema)

    assert _rows(test_db, scratch_schema) == ROWS[profile]
    assert _columns(test_db, scratch_schema) == EXPECTED_COLUMNS
    assert _counter_table(test_db, scratch_schema) == EXPECTED_COUNTER_TABLE
    with test_db.connect() as conn:
        stored = conn.execute(
            text(
                f"SELECT e.balance_within, e.balance_seed, a.balance_group, "
                f'a.balance_seq FROM "{scratch_schema}".experiments e JOIN '
                f'"{scratch_schema}".assignments a ON a.experiment_id = e.id '
                "WHERE e.id = :id"
            ),
            {"id": ids["experiment"]},
        ).one()
    assert tuple(stored) == (None, None, None, None)


@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_autogenerate_is_empty_on_a_database_upgraded_from_the_previous_release(
    profile, test_db, scratch_schema, tmp_path
):
    """The types, nullability, key and foreign key are the models'.
    Autogenerate on THIS (upgraded) database compares them and finds nothing to
    add; the fresh-bootstrap autogenerate test cannot see a wrong type in the
    migration, because there both sides come from the models."""
    tree = _bootstrapped_at_previous_release(test_db, profile, scratch_schema, tmp_path)
    _upgrade(tree, scratch_schema)
    assert _rows(test_db, scratch_schema) == ROWS[profile]

    result = tree_profiles.autogenerate(
        tree, scratch_schema, tmp_path, head=tree_profiles.CORE_HEAD
    )
    print(f"[{profile}] autogenerate on the upgraded database: {result.operations}")
    assert result.body is not None, result.describe()
    assert result.operations == [], result.describe()


# ---------------------------------------------------------------------------
# 2. The migration's objects are the models'
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_an_upgraded_and_a_bootstrapped_database_end_the_same(
    profile, test_db, scratch_schema, reference_schema, tmp_path
):
    tree = _bootstrapped_at_previous_release(test_db, profile, scratch_schema, tmp_path)
    _upgrade(tree, scratch_schema)
    _bootstrapped(profile, reference_schema, tmp_path / "reference")

    upgraded = (
        _columns(test_db, scratch_schema),
        _counter_table(test_db, scratch_schema),
    )
    built = (
        _columns(test_db, reference_schema),
        _counter_table(test_db, reference_schema),
    )
    print(f"[{profile}] {UPGRADED}: {upgraded}")
    print(f"[{profile}] {BOOTSTRAPPED}: {built}")
    assert upgraded == built == (EXPECTED_COLUMNS, EXPECTED_COUNTER_TABLE)


# ---------------------------------------------------------------------------
# 3. The previous release's statements on this schema
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_the_previous_releases_statements_run_on_this_schema(
    profile, test_db, scratch_schema, tmp_path
):
    """During a rolling deploy, and for as long as a rollback to it lasts, the
    previous release writes and reads experiments and assignments naming only
    the columns it knew. A balanced experiment's user it assigns has no group
    and no place: outside the blocks."""
    tree = _bootstrapped_at_previous_release(test_db, profile, scratch_schema, tmp_path)
    _upgrade(tree, scratch_schema)

    ids = _previous_release_rows(test_db, scratch_schema)
    with test_db.begin() as conn:
        conn.execute(
            text(
                f'UPDATE "{scratch_schema}".experiments SET balance_within = '
                'CAST(\'{"attribute": "tier", "values": ["gold"]}\' AS jsonb), '
                "balance_seed = :seed WHERE id = :id"
            ),
            {"seed": "d" * 64, "id": ids["experiment"]},
        )
        conn.execute(
            text(
                f'INSERT INTO "{scratch_schema}".assignments (id, experiment_id, '
                "variant_id, user_id, created_at, updated_at) VALUES (:id, :e, :v, "
                "'previous-user-2', now(), now())"
            ),
            {"id": str(uuid.uuid4()), "e": ids["experiment"], "v": ids["treatment"]},
        )
    with test_db.connect() as conn:
        read = conn.execute(
            text(
                f"SELECT a.user_id, a.variant_id, a.context, a.balance_group, "
                f'a.balance_seq FROM "{scratch_schema}".assignments a '
                "WHERE a.experiment_id = :e ORDER BY a.user_id"
            ),
            {"e": ids["experiment"]},
        ).all()
    assert [(r.user_id, r.balance_group, r.balance_seq) for r in read] == [
        ("previous-user", None, None),
        ("previous-user-2", None, None),
    ]


# ---------------------------------------------------------------------------
# 4. The downgrade
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_the_downgrade_drops_exactly_the_columns_and_the_table(
    profile, test_db, scratch_schema, tmp_path
):
    tree = _bootstrapped_at_previous_release(test_db, profile, scratch_schema, tmp_path)
    _upgrade(tree, scratch_schema)
    ids = _previous_release_rows(test_db, scratch_schema)
    with test_db.begin() as conn:
        conn.execute(
            text(
                f'UPDATE "{scratch_schema}".experiments SET balance_within = '
                'CAST(\'{"attribute": "tier", "values": ["gold"]}\' AS jsonb) '
                "WHERE id = :id"
            ),
            {"id": ids["experiment"]},
        )
        conn.execute(
            text(
                f'INSERT INTO "{scratch_schema}".{COUNTERS} (experiment_id, '
                "balance_group, next_seq) VALUES (:e, 'gold', 3)"
            ),
            {"e": ids["experiment"]},
        )
    tables = _tables(test_db, scratch_schema)
    experiment_columns = _all_columns(test_db, scratch_schema, "experiments")
    assignment_columns = _all_columns(test_db, scratch_schema, "assignments")

    down = tree_profiles.alembic(tree, scratch_schema, "downgrade", PREVIOUS_CORE_HEAD)
    assert down.returncode == 0, down.stderr[-3000:]
    _report(profile, "after the downgrade", test_db, scratch_schema)

    assert _rows(test_db, scratch_schema) == PREVIOUS_ROWS[profile]
    assert _tables(test_db, scratch_schema) == (
        tables - {COUNTERS} - LATER_DOWNGRADE_TABLES
    )
    dropped = {"balance_within", "balance_seed"} | LATER_DOWNGRADE_EXPERIMENT_COLUMNS
    assert _all_columns(test_db, scratch_schema, "experiments") == (
        experiment_columns - dropped
    )
    assert _all_columns(
        test_db, scratch_schema, "assignments"
    ) == assignment_columns - {
        "balance_group",
        "balance_seq",
    }
    with test_db.connect() as conn:
        kept = conn.execute(
            text(
                f'SELECT count(*) FROM "{scratch_schema}".assignments WHERE experiment_id = :e'
            ),
            {"e": ids["experiment"]},
        ).scalar()
    assert kept == 1, "the downgrade removed an assignment"

    _upgrade(tree, scratch_schema)
    assert _rows(test_db, scratch_schema) == ROWS[profile]
    with test_db.connect() as conn:
        again = conn.execute(
            text(
                f'SELECT balance_within FROM "{scratch_schema}".experiments '
                "WHERE id = :id"
            ),
            {"id": ids["experiment"]},
        ).scalar()
    assert again is None, "an upgrade after the downgrade restored a setting"


# ---------------------------------------------------------------------------
# 5. Offline mode, and the heads
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_the_upgrade_refuses_offline_mode(tmp_path):
    tree = tree_profiles.tree_for(CORE, tmp_path)
    offline = tree_profiles.alembic(
        tree, "public", "upgrade", f"{PREVIOUS_CORE_HEAD}:{REVISION}", "--sql"
    )
    assert offline.returncode != 0
    assert "cannot be emitted as SQL" in offline.stderr


@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_alembic_heads_are_this_revision_and_the_modules_head(profile, tmp_path):
    """Still two heads in a full tree: this revision extended the core chain,
    and ``4955571d232c`` (#1545), the core head now, extends it."""
    tree = tree_profiles.tree_for(profile, tmp_path)
    heads = tree_profiles.alembic(tree, "public", "heads")
    assert heads.returncode == 0, heads.stderr[-3000:]
    print(f"[{profile}] alembic heads:\n{heads.stdout}")
    named = {line.split()[0] for line in heads.stdout.splitlines() if line.strip()}
    assert named == HEADS[profile]
