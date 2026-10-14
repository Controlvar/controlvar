"""``d29a479daafe``: holdout population, per-holdout salt, one active holdout (#445).

Every test starts from what the previous release left -- a bootstrapped schema
with this revision's objects DROPPED and ``alembic_version`` at the previous
core head -- and runs the documented ``alembic upgrade heads``, because a
``create_all`` schema already has everything and would never run this
revision's DDL or its data steps.  Then:

1. the data steps run in order (A14): with two active holdouts and one
   inactive one before the upgrade, exactly one row keeps the legacy salt --
   the active one kept, the most recently updated -- the other two get
   distinct ``holdout:<id>`` salts, and the deactivated one is stamped
   ``deactivated_at``; the kept row has no ``activated_at``, so it is not
   measurable;
2. the one-active index (PE C6) is read from ``pg_indexes.indexdef`` on the
   MIGRATED database and carries ``WHERE is_active``: autogenerate cannot see
   a partial index that lost its WHERE (and a test that does not read the
   definition passes it);
3. on that database two inactive holdouts and one active coexist, and a second
   active one is refused;
4. autogenerate against the migrated database is empty (A15);
5. in each profile the ``alembic_version`` rows are this release's;
6. ``downgrade`` removes exactly the table, the index and the three columns,
   and leaves the deactivation in place.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from backend.app.db import bootstrap
from backend.app.models.global_holdout import (
    LEGACY_HOLDOUT_SALT,
    ONE_ACTIVE_INDEX,
    GlobalHoldout,
)
from backend.tests.integration.database import tree_profiles
from backend.tests.integration.database.tree_profiles import CORE, FULL

pytestmark = [pytest.mark.integration]

#: This revision, and the core revision it extends.
REVISION = "d29a479daafe"
PREVIOUS_CORE_HEAD = "806901fb7735"
#: The core head of this tree: ``4c2c09a2b3e8`` (#1206), after ``37dcb2969766``
#: (#440), the revision after this one.
CORE_HEAD = "4c2c09a2b3e8"
#: Tables that a later core revision's downgrade drops: a downgrade from the
#: head to this test's target unapplies those revisions too.  ``37dcb2969766``
#: (#440) drops ``segment_members``.
LATER_DOWNGRADE_TABLES = {"segment_members"}
MODULES_HEAD = "modules_0002_warehouse_analysis"

PREVIOUS_ROWS = {
    CORE: {PREVIOUS_CORE_HEAD},
    FULL: {PREVIOUS_CORE_HEAD, MODULES_HEAD},
}
ROWS = {
    CORE: {CORE_HEAD},
    FULL: {CORE_HEAD, MODULES_HEAD},
}

NEW_COLUMNS = {"activated_at", "deactivated_at", "hash_salt"}
TABLE = "holdout_population"

BOTH_PROFILES = [CORE, pytest.param(FULL, marks=pytest.mark.modules)]


@pytest.fixture
def scratch_schema(test_db):
    schema = f"holdout_{uuid.uuid4().hex[:8]}"
    try:
        yield schema
    finally:
        with test_db.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


def _tree(profile, tmp_path):
    return tree_profiles.tree_for(profile, tmp_path)


def _rows(engine, schema: str) -> set[str]:
    return bootstrap.recorded_revisions(engine, schema)


def _tables(engine, schema: str) -> set[str]:
    return set(inspect(engine).get_table_names(schema=schema))


def _columns(engine, schema: str) -> set[str]:
    return {
        c["name"] for c in inspect(engine).get_columns("global_holdouts", schema=schema)
    }


def _indexdef(engine, schema: str):
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname = :s AND indexname = :i"
            ),
            {"s": schema, "i": ONE_ACTIVE_INDEX},
        ).scalar_one_or_none()


def _at_previous_release(engine, schema: str, rows: set[str]) -> None:
    """Turn a bootstrapped *schema* into what the previous release left."""
    with engine.begin() as conn:
        conn.execute(text(f'DROP TABLE "{schema}".{TABLE}'))
        conn.execute(text(f'DROP INDEX "{schema}".{ONE_ACTIVE_INDEX}'))
        for column in sorted(NEW_COLUMNS):
            conn.execute(
                text(f'ALTER TABLE "{schema}".global_holdouts DROP COLUMN {column}')
            )
        conn.execute(text(f'DELETE FROM "{schema}".alembic_version'))
        for revision in sorted(rows):
            conn.execute(
                text(f'INSERT INTO "{schema}".alembic_version VALUES (:rev)'),
                {"rev": revision},
            )
    assert not NEW_COLUMNS & _columns(engine, schema)
    assert TABLE not in _tables(engine, schema)
    assert _indexdef(engine, schema) is None


def _insert_previous_holdout(conn, schema, name, is_active, updated_at):
    holdout_id = uuid.uuid4()
    conn.execute(
        text(
            f'INSERT INTO "{schema}".global_holdouts '
            "(id, name, holdout_percentage, is_active, created_at, updated_at) "
            "VALUES (:id, :name, 10, :active, :ts, :ts)"
        ),
        {"id": holdout_id, "name": name, "active": is_active, "ts": updated_at},
    )
    return holdout_id


def _holdouts(engine, schema):
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id, is_active, hash_salt, activated_at, deactivated_at "
                f'FROM "{schema}".global_holdouts'
            )
        ).all()
    return {row.id: row for row in rows}


def _upgrade(tree, schema):
    upgrade = tree_profiles.alembic(tree, schema, "upgrade", "heads")
    assert upgrade.returncode == 0, upgrade.stderr[-3000:]
    return upgrade


# ---------------------------------------------------------------------------
# 1. The data steps, in order (A14, EM condition 3, PE C1/C2/C16)
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_upgrade_keeps_one_active_legacy_row_and_salts_the_rest(
    test_db, scratch_schema, tmp_path, profile
):
    tree = _tree(profile, tmp_path)
    assert tree_profiles.bootstrap_schema(tree, scratch_schema).returncode == 0
    _at_previous_release(test_db, scratch_schema, PREVIOUS_ROWS[profile])
    base = dt.datetime(2026, 9, 1, 12, 0, 0)
    with test_db.begin() as conn:
        older = _insert_previous_holdout(conn, scratch_schema, "older", True, base)
        newer = _insert_previous_holdout(
            conn, scratch_schema, "newer", True, base + dt.timedelta(days=1)
        )
        inactive = _insert_previous_holdout(
            conn, scratch_schema, "inactive", False, base - dt.timedelta(days=1)
        )

    upgrade = _upgrade(tree, scratch_schema)

    print("alembic_version:", sorted(_rows(test_db, scratch_schema)))
    rows = _holdouts(test_db, scratch_schema)
    for holdout_id, row in rows.items():
        print(holdout_id, row.is_active, row.hash_salt, row.deactivated_at)
    assert _rows(test_db, scratch_schema) == ROWS[profile]
    # The most recently updated active row is kept, with the legacy salt and
    # no activated_at: not measurable.
    assert rows[newer].is_active is True
    assert rows[newer].hash_salt == LEGACY_HOLDOUT_SALT
    assert rows[newer].activated_at is None
    assert rows[newer].deactivated_at is None
    assert not GlobalHoldout(
        hash_salt=rows[newer].hash_salt, activated_at=rows[newer].activated_at
    ).is_measurable
    # The other active row is deactivated and stamped as ended.
    assert rows[older].is_active is False
    assert rows[older].deactivated_at is not None
    # The two rows not active after the reduction get their own salts.
    assert rows[older].hash_salt == f"holdout:{older}"
    assert rows[inactive].hash_salt == f"holdout:{inactive}"
    assert rows[inactive].deactivated_at is None
    assert [r.hash_salt for r in rows.values()].count(LEGACY_HOLDOUT_SALT) == 1
    # The ids it deactivated are on stdout.
    assert f"deactivated 1 global holdout(s) to keep one active: {older}" in (
        upgrade.stdout
    )


# ---------------------------------------------------------------------------
# 2-3. The one-active index, read from the migrated database (PE C6)
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_the_migrated_index_is_partial_and_allows_many_inactive_rows(
    test_db, scratch_schema, tmp_path
):
    tree = _tree(CORE, tmp_path)
    assert tree_profiles.bootstrap_schema(tree, scratch_schema).returncode == 0
    _at_previous_release(test_db, scratch_schema, PREVIOUS_ROWS[CORE])
    _upgrade(tree, scratch_schema)

    indexdef = _indexdef(test_db, scratch_schema)
    print("indexdef:", indexdef)
    assert indexdef is not None
    assert indexdef.startswith("CREATE UNIQUE INDEX"), indexdef
    assert indexdef.endswith("(is_active) WHERE is_active"), indexdef

    with test_db.begin() as conn:
        _insert_previous_holdout(
            conn, scratch_schema, "off-1", False, dt.datetime.now()
        )
        _insert_previous_holdout(
            conn, scratch_schema, "off-2", False, dt.datetime.now()
        )
        _insert_previous_holdout(conn, scratch_schema, "on-1", True, dt.datetime.now())
    with pytest.raises(IntegrityError, match=ONE_ACTIVE_INDEX):
        with test_db.begin() as conn:
            _insert_previous_holdout(
                conn, scratch_schema, "on-2", True, dt.datetime.now()
            )


# ---------------------------------------------------------------------------
# 4. The migration builds what the models declare (A15)
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_autogenerate_after_the_upgrade_is_empty(test_db, scratch_schema, tmp_path):
    tree = _tree(CORE, tmp_path)
    assert tree_profiles.bootstrap_schema(tree, scratch_schema).returncode == 0
    _at_previous_release(test_db, scratch_schema, PREVIOUS_ROWS[CORE])
    _upgrade(tree, scratch_schema)

    result = tree_profiles.autogenerate(
        tree, scratch_schema, tmp_path, head=tree_profiles.CORE_HEAD
    )

    assert result.body is not None, result.describe()
    assert result.operations == [], result.describe()


# ---------------------------------------------------------------------------
# 5. The migrated table: its key and its foreign key
# ---------------------------------------------------------------------------
def test_the_population_table_is_keyed_by_holdout_and_user(
    test_db, scratch_schema, tmp_path
):
    tree = _tree(CORE, tmp_path)
    assert tree_profiles.bootstrap_schema(tree, scratch_schema).returncode == 0
    _at_previous_release(test_db, scratch_schema, PREVIOUS_ROWS[CORE])
    _upgrade(tree, scratch_schema)

    inspector = inspect(test_db)
    pk = inspector.get_pk_constraint(TABLE, schema=scratch_schema)
    fks = inspector.get_foreign_keys(TABLE, schema=scratch_schema)
    columns = {
        c["name"]: str(c["type"])
        for c in inspector.get_columns(TABLE, schema=scratch_schema)
    }
    assert pk["constrained_columns"] == ["holdout_id", "user_id"]
    assert [(fk["referred_table"], fk["options"].get("ondelete")) for fk in fks] == [
        ("global_holdouts", "CASCADE")
    ]
    # The same string type as events.created_at, so the two compare directly.
    events_created_at = {
        c["name"]: str(c["type"])
        for c in inspector.get_columns("events", schema=scratch_schema)
    }["created_at"]
    assert columns["first_seen_at"] == events_created_at == "VARCHAR"


# ---------------------------------------------------------------------------
# 6. Downgrade
# ---------------------------------------------------------------------------
def test_downgrade_drops_the_table_index_and_columns_and_keeps_the_deactivation(
    test_db, scratch_schema, tmp_path
):
    tree = _tree(CORE, tmp_path)
    assert tree_profiles.bootstrap_schema(tree, scratch_schema).returncode == 0
    _at_previous_release(test_db, scratch_schema, PREVIOUS_ROWS[CORE])
    base = dt.datetime(2026, 9, 1)
    with test_db.begin() as conn:
        older = _insert_previous_holdout(conn, scratch_schema, "a", True, base)
        _insert_previous_holdout(
            conn, scratch_schema, "b", True, base + dt.timedelta(hours=1)
        )
    _upgrade(tree, scratch_schema)
    tables_before = _tables(test_db, scratch_schema)

    down = tree_profiles.alembic(tree, scratch_schema, "downgrade", PREVIOUS_CORE_HEAD)
    assert down.returncode == 0, down.stderr[-3000:]

    assert _rows(test_db, scratch_schema) == PREVIOUS_ROWS[CORE]
    assert (
        _tables(test_db, scratch_schema)
        == tables_before - {TABLE} - LATER_DOWNGRADE_TABLES
    )
    assert not NEW_COLUMNS & _columns(test_db, scratch_schema)
    assert _indexdef(test_db, scratch_schema) is None
    with test_db.connect() as conn:
        still_off = conn.execute(
            text(
                f'SELECT is_active FROM "{scratch_schema}".global_holdouts '
                "WHERE id = :id"
            ),
            {"id": older},
        ).scalar_one()
    assert still_off is False
