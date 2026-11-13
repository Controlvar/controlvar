"""``t1812_user_totp``: the two-step sign-in table, through the transitions (#1812).

The revision adds the table ``user_totp`` only when absent. A schema built by
``create_all`` already has it, so the tests that start from the previous
release take a bootstrapped schema with the table DROPPED and
``alembic_version`` at ``4955571d232c``, in each profile, and run the
documented ``alembic upgrade heads``. Then:

1. the ``alembic_version`` rows are this release's (printed), the table exists
   with ``last_used_step`` and ``failed_attempts`` NOT NULL DEFAULT 0,
   ``users`` is unchanged, and autogenerate is EMPTY on that database;
2. the upgraded database and a ``create_all``-bootstrapped one have the same
   table, from the catalog: columns, defaults, constraints, indexes;
3. the downgrade removes exactly the table, keeps every account, and an
   upgrade afterwards adds it back empty;
4. ``alembic heads`` names exactly this revision and the modules head.

The previous release refusing this database and starting after the
downgrade is ``test_newer_database_rollback.py``'s.
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
REVISION = "t1812_user_totp"
PREVIOUS_CORE_HEAD = "4955571d232c"
#: The core head of this tree.
CORE_HEAD = "t1812_user_totp"
MODULES_HEAD = "modules_0005_redshift"

PREVIOUS_ROWS = {
    CORE: {PREVIOUS_CORE_HEAD},
    FULL: {PREVIOUS_CORE_HEAD, MODULES_HEAD},
}
ROWS = {CORE: {CORE_HEAD}, FULL: {CORE_HEAD, MODULES_HEAD}}

TABLE = "user_totp"

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
    yield from _scratch(test_db, "usertotp")


@pytest.fixture
def reference_schema(test_db):
    yield from _scratch(test_db, "usertotpref")


def _rows(engine, schema: str) -> set[str]:
    return bootstrap.recorded_revisions(engine, schema)


def _tables(engine, schema: str) -> set[str]:
    return set(inspect(engine).get_table_names(schema=schema))


def _columns(engine, schema: str, table: str):
    """Each column of *table* in order: (name, type, NOT NULL, default)."""
    with engine.connect() as conn:
        rows = conn.execute(
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
            {"s": schema, "t": table},
        ).all()
    return [tuple(row) for row in rows]


def _table_definition(engine, schema: str):
    with engine.connect() as conn:
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
        _columns(engine, schema, TABLE),
        {
            (name, kind, definition.replace(schema, "<schema>"))
            for name, kind, definition in constraints
        },
        {row[0].replace(schema, "<schema>") for row in indexes},
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
    with test_db.begin() as conn:
        conn.execute(text(f'DROP TABLE IF EXISTS "{schema}".{TABLE}'))
    _stamp(test_db, schema, PREVIOUS_ROWS[profile])
    assert TABLE not in _tables(test_db, schema)
    return tree


def _upgrade(tree, schema: str) -> None:
    upgrade = tree_profiles.alembic(tree, schema, "upgrade", "heads")
    assert upgrade.returncode == 0, upgrade.stderr[-3000:]


def _insert_user(engine, schema: str) -> str:
    """An account, as the previous release stores one."""
    user_id = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(
            text(
                f'INSERT INTO "{schema}".users (id, username, email, '
                "hashed_password, is_active, is_superuser, role, created_at, "
                "updated_at) VALUES (:id, :u, :e, 'x', true, false, 'VIEWER', "
                "now(), now())"
            ),
            {"id": user_id, "u": f"u_{user_id[:8]}", "e": f"{user_id[:8]}@example.com"},
        )
    return user_id


def _users(engine, schema: str) -> set[tuple]:
    with engine.connect() as conn:
        rows = conn.execute(text(f'SELECT id, email FROM "{schema}".users')).all()
    return {tuple(row) for row in rows}


def _report(profile, label, engine, schema) -> None:
    print(f"[{profile}] {label}: alembic_version={sorted(_rows(engine, schema))}")
    print(f"[{profile}] {label}: {TABLE} present={TABLE in _tables(engine, schema)}")


# ---------------------------------------------------------------------------
# 1. The previous release, upgraded with `heads`
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_a_database_at_the_previous_release_gains_the_table(
    profile, test_db, scratch_schema, tmp_path
):
    tree = _at_previous_release(test_db, profile, scratch_schema, tmp_path)
    _insert_user(test_db, scratch_schema)
    users_before = _columns(test_db, scratch_schema, "users")
    stored = _users(test_db, scratch_schema)
    _report(profile, "at the previous release", test_db, scratch_schema)

    _upgrade(tree, scratch_schema)
    _report(profile, "after upgrade heads", test_db, scratch_schema)

    assert _rows(test_db, scratch_schema) == ROWS[profile]
    columns = {c[0]: c[1:] for c in _columns(test_db, scratch_schema, TABLE)}
    print(f"[{profile}] {TABLE}: {columns}")
    assert columns["last_used_step"] == ("bigint", True, "0")
    assert columns["failed_attempts"] == ("integer", True, "0")
    assert columns["recovery_hashes"] == ("jsonb", True, "'[]'::jsonb")
    assert columns["secret_enc"] == ("text", True, None)
    assert columns["confirmed_at"] == ("timestamp with time zone", False, None)
    assert _columns(test_db, scratch_schema, "users") == users_before
    assert _users(test_db, scratch_schema) == stored

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
    assert upgraded == bootstrapped
    kinds = sorted(kind for _, kind, _ in upgraded[1])
    assert kinds == ["f", "p"]  # the primary key and the users foreign key
    (fk,) = [d for _, kind, d in upgraded[1] if kind == "f"]
    assert "ON DELETE CASCADE" in fk and "users(id)" in fk


# ---------------------------------------------------------------------------
# 3. Downgrade
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_the_downgrade_drops_the_table_only(profile, test_db, scratch_schema, tmp_path):
    tree = _bootstrapped(profile, scratch_schema, tmp_path)
    user_id = _insert_user(test_db, scratch_schema)
    with test_db.begin() as conn:
        conn.execute(
            text(
                f'INSERT INTO "{scratch_schema}".{TABLE} (user_id, secret_enc) '
                "VALUES (:u, 'sealed')"
            ),
            {"u": user_id},
        )
    tables = _tables(test_db, scratch_schema)
    stored = _users(test_db, scratch_schema)

    down = tree_profiles.alembic(tree, scratch_schema, "downgrade", PREVIOUS_CORE_HEAD)
    assert down.returncode == 0, down.stderr[-3000:]
    _report(profile, "after downgrade", test_db, scratch_schema)

    assert _rows(test_db, scratch_schema) == PREVIOUS_ROWS[profile]
    assert _tables(test_db, scratch_schema) == tables - {TABLE}
    assert _users(test_db, scratch_schema) == stored

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


# ---------------------------------------------------------------------------
# 4. The heads
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


@pytest.mark.regression
def test_the_tree_pins_this_revision_as_the_core_head():
    assert tree_profiles.CORE_HEAD == CORE_HEAD
