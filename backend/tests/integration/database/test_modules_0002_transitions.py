"""``modules_0002_warehouse_analysis`` across every database it can meet (#312).

The revision creates the three warehouse-analysis tables and removes the
earlier ``warehouse_connections`` table with its rows.  It is the first module
revision that deletes data, so each way a database can reach it is rehearsed
here as the deployment runs it -- raw ``alembic upgrade heads`` in a
subprocess, which is what the ECS migration task does -- and each end state is
stated exactly: the ``alembic_version`` rows, the shape of the three tables
against what the models build, and where the earlier rows went.

    1. a database a core bootstrap built, opened by this full build
       (no ``warehouse_connections`` at all);
    2. a database the previous full release migrated, with saved connections
       (the earlier table, populated), and that release put back afterwards;
    3. the earlier rows: gone, active and inactive, from every table;
    4. a downgrade and a re-upgrade: a table the bootstrap built, holding a
       connection, is not taken for the earlier one;
    5. the downgrade of a table this revision created puts the earlier shape
       back, empty, so the previous release maps a table again.

"The shape" is compared from the catalogue -- columns and their types and
nullability, every constraint's definition (CHECK constraints included, which
``alembic --autogenerate`` never compares), every index's definition -- against
a reference schema a fresh full bootstrap built from the models.
"""

from __future__ import annotations

import base64
import json
import uuid

import pytest
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    MetaData,
    String,
    Table,
    Text,
    inspect,
    text,
)
from sqlalchemy.dialects.postgresql import UUID

from backend.app.db import bootstrap
from backend.tests.integration.database import tree_profiles
from backend.tests.integration.database.tree_profiles import CORE, FULL

pytestmark = [pytest.mark.integration, pytest.mark.modules]

CORE_HEAD = tree_profiles.CORE_HEAD
#: The modules branch's first revision: what the previous full release
#: (0.9.x and earlier) records.
MODULES_FIRST = "modules_0001_rbac"
#: The modules branch's head: this revision.
MODULES_HEAD = "modules_0002_warehouse_analysis"
PREVIOUS_FULL_HEADS = {CORE_HEAD, MODULES_FIRST}
FULL_HEADS = {CORE_HEAD, MODULES_HEAD}

CONNECTIONS = "warehouse_connections"
WAREHOUSE_TABLES = (CONNECTIONS, "warehouse_sources", "warehouse_analysis_runs")
#: The COMMENT the revision leaves on the primary key of a table it created.
CREATED_BY = f"created by alembic revision {MODULES_HEAD}"
#: A column only the earlier ``warehouse_connections`` has.
EARLIER_SHAPE_COLUMN = "encrypted_credentials"

#: Planted in the earlier rows; must be nowhere in the schema afterwards.
SENTINEL = "w2a-scrub-sentinel-7f3c9a"


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def scratch_schema(test_db):
    schema = f"wh0002_{uuid.uuid4().hex[:8]}"
    try:
        yield schema
    finally:
        with test_db.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}_ref" CASCADE'))


@pytest.fixture
def full_tree():
    return tree_profiles.tree_for(FULL, None)


@pytest.fixture
def core_tree(tmp_path):
    return tree_profiles.tree_for(CORE, tmp_path)


def _previous_full_release(tmp_path):
    """The previous full release's migrations: this tree without this revision.

    Built from parts, as ``tree_profiles`` builds its paths: a string literal
    naming a path under the modules would be a core -> module crossing.
    """
    tree = tree_profiles.full_tree(tmp_path / "previous-full-release")
    revision = tree.joinpath(
        "modules",
        "backend",
        "app",
        "db",
        "migrations",
        "versions",
        f"{MODULES_HEAD}.py",
    )
    revision.unlink()
    return tree


def _rows(engine, schema: str) -> set[str]:
    return bootstrap.recorded_revisions(engine, schema)


def _set_rows(engine, schema: str, revisions) -> None:
    with engine.begin() as conn:
        conn.execute(text(f'DELETE FROM "{schema}".alembic_version'))
        for revision in revisions:
            conn.execute(
                text(f'INSERT INTO "{schema}".alembic_version VALUES (:rev)'),
                {"rev": revision},
            )


def _tables(engine, schema: str) -> set[str]:
    return set(inspect(engine).get_table_names(schema=schema))


def _columns(engine, schema: str, table: str) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns(table, schema=schema)}


def _pk_comment(engine, schema: str, table: str):
    return inspect(engine).get_pk_constraint(table, schema=schema).get("comment")


def _shape(engine, schema: str, table: str) -> dict:
    """*table* as the catalogue describes it, with the schema name abstracted."""
    relation = f'"{schema}"."{table}"'
    with engine.connect() as conn:
        columns = conn.execute(
            text(
                "SELECT attname, format_type(atttypid, atttypmod), attnotnull "
                "FROM pg_attribute WHERE attrelid = CAST(:rel AS regclass) "
                "AND attnum > 0 AND NOT attisdropped ORDER BY attname"
            ),
            {"rel": relation},
        ).all()
        constraints = conn.execute(
            text(
                "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = CAST(:rel AS regclass) ORDER BY conname"
            ),
            {"rel": relation},
        ).all()
        indexes = conn.execute(
            text(
                "SELECT indexname, indexdef FROM pg_indexes "
                "WHERE schemaname = :schema AND tablename = :table ORDER BY indexname"
            ),
            {"schema": schema, "table": table},
        ).all()

    def norm(value):
        return value.replace(schema, "<schema>") if isinstance(value, str) else value

    return {
        "columns": [tuple(norm(v) for v in row) for row in columns],
        "constraints": [tuple(norm(v) for v in row) for row in constraints],
        "indexes": [tuple(norm(v) for v in row) for row in indexes],
    }


def _reference_shapes(engine, full_tree, schema: str) -> dict:
    """What a fresh full bootstrap -- the models -- builds for the three tables."""
    reference = f"{schema}_ref"
    built = tree_profiles.bootstrap_schema(full_tree, reference)
    assert built.returncode == 0, built.stderr[-3000:]
    return {table: _shape(engine, reference, table) for table in WAREHOUSE_TABLES}


def _assert_models_shape(engine, full_tree, schema: str) -> None:
    reference = _reference_shapes(engine, full_tree, schema)
    for table in WAREHOUSE_TABLES:
        assert _shape(engine, schema, table) == reference[table], table


def _autogenerate_is_empty(full_tree, schema: str, tmp_path) -> None:
    result = tree_profiles.autogenerate(
        full_tree, schema, tmp_path, head="modules@head"
    )
    assert result.body is not None, result.describe()
    assert result.operations == [], result.describe()


def _earlier_connections_table(schema: str) -> Table:
    """``warehouse_connections`` as the previous release's model declares it.

    A copy of ``modules/backend/app/models/warehouse_connection.py`` before
    #312 (``BaseModel`` columns included), as ``create_all`` builds it.
    """
    metadata = MetaData()
    Table("users", metadata, Column("id", UUID(as_uuid=True)), schema=schema)
    return Table(
        CONNECTIONS,
        metadata,
        Column("name", String(200), nullable=False),
        Column("warehouse_type", String(50), nullable=False),
        Column(EARLIER_SHAPE_COLUMN, Text, nullable=False),
        Column("is_active", Boolean, nullable=False),
        Column(
            "owner_id",
            UUID(as_uuid=True),
            ForeignKey(f"{schema}.users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        Column("id", UUID(as_uuid=True), primary_key=True),
        Column("created_at", DateTime, nullable=False),
        Column("updated_at", DateTime, nullable=False),
        Index(f"ix_{schema}_warehouse_conn_active", "is_active"),
        Index(f"ix_{schema}_warehouse_connections_created_at", "created_at"),
        schema=schema,
    )


def _make_previous_full_release_database(engine, schema: str) -> int:
    """Turn a fresh full schema into one the previous full release left behind.

    The three new tables go, the earlier ``warehouse_connections`` comes back
    holding two saved connections (one active, one not) with the sentinel in
    their name and in their stored credentials, and ``alembic_version`` records
    the previous release's heads.  Returns the number of rows planted.
    """
    with engine.begin() as conn:
        for table in reversed(WAREHOUSE_TABLES):
            conn.execute(text(f'DROP TABLE "{schema}"."{table}"'))
    table = _earlier_connections_table(schema)
    table.create(engine)
    credentials = base64.b64encode(json.dumps({"password": SENTINEL}).encode()).decode()
    with engine.begin() as conn:
        for active in (True, False):
            conn.execute(
                table.insert().values(
                    id=uuid.uuid4(),
                    name=f"prod {SENTINEL} {active}",
                    warehouse_type="snowflake",
                    encrypted_credentials=credentials,
                    is_active=active,
                    created_at=text("now()"),
                    updated_at=text("now()"),
                )
            )
    _set_rows(engine, schema, PREVIOUS_FULL_HEADS)
    return 2


def _rows_mentioning(engine, schema: str, needle: str) -> dict[str, int]:
    """Rows of every table in *schema* whose text form contains *needle*."""
    found = {}
    with engine.connect() as conn:
        for table in sorted(_tables(engine, schema)):
            count = conn.execute(
                text(
                    f'SELECT count(*) FROM "{schema}"."{table}" AS t '
                    "WHERE CAST(t AS text) LIKE :needle"
                ),
                {"needle": f"%{needle}%"},
            ).scalar_one()
            if count:
                found[table] = count
    return found


# ---------------------------------------------------------------------------
# 1. A core-bootstrapped database, opened by this full build
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_modules_0002_fresh_core_to_full(
    test_db, scratch_schema, core_tree, full_tree, tmp_path
):
    """No ``warehouse_connections`` at all: nothing to remove, three to create.

    A core bootstrap builds core tables only, and the earlier table is a module
    model's.  An unguarded removal fails here on a table that is not there.
    """
    assert tree_profiles.bootstrap_schema(core_tree, scratch_schema).returncode == 0
    assert not set(WAREHOUSE_TABLES) & _tables(test_db, scratch_schema)

    upgrade = tree_profiles.alembic(full_tree, scratch_schema, "upgrade", "heads")
    assert upgrade.returncode == 0, upgrade.stderr[-3000:]

    assert _rows(test_db, scratch_schema) == FULL_HEADS
    for table in WAREHOUSE_TABLES:
        # The migration made them (the reconcile would leave no tag).
        assert _pk_comment(test_db, scratch_schema, table) == CREATED_BY, table
    assert "legacy warehouse connection rows" not in upgrade.stderr
    _assert_models_shape(test_db, full_tree, scratch_schema)
    _autogenerate_is_empty(full_tree, scratch_schema, tmp_path)


# ---------------------------------------------------------------------------
# 2. The previous full release's database, and that release put back
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_modules_0002_full_080_to_new(test_db, scratch_schema, full_tree, tmp_path):
    """The upgrade every existing full deployment takes, then a rollback.

    Raw ``alembic upgrade heads`` replaces the earlier table with the new one
    and lands on exactly what the models build.  The previous release, put
    back afterwards, then refuses the database by both documented paths and
    says why -- a newer release migrated it -- rather than running against a
    table it cannot map.
    """
    assert tree_profiles.bootstrap_schema(full_tree, scratch_schema).returncode == 0
    planted = _make_previous_full_release_database(test_db, scratch_schema)
    assert EARLIER_SHAPE_COLUMN in _columns(test_db, scratch_schema, CONNECTIONS)

    upgrade = tree_profiles.alembic(full_tree, scratch_schema, "upgrade", "heads")
    assert upgrade.returncode == 0, upgrade.stderr[-3000:]

    assert _rows(test_db, scratch_schema) == FULL_HEADS
    assert f"removing {planted} legacy warehouse connection rows" in upgrade.stderr
    assert EARLIER_SHAPE_COLUMN not in _columns(test_db, scratch_schema, CONNECTIONS)
    _assert_models_shape(test_db, full_tree, scratch_schema)
    _autogenerate_is_empty(full_tree, scratch_schema, tmp_path)

    # The previous release, put back: refused, and nothing touched.
    previous = _previous_full_release(tmp_path)
    tables_before = _tables(test_db, scratch_schema)
    for result in (
        tree_profiles.bootstrap_schema(previous, scratch_schema),
        tree_profiles.alembic(previous, scratch_schema, "upgrade", "heads"),
    ):
        assert result.returncode != 0, result.stdout[-2000:]
        assert "migrated by a newer Controlvar release" in result.stderr
        assert MODULES_HEAD in result.stderr
        assert "full profile being opened by a core build" not in result.stderr
    assert _rows(test_db, scratch_schema) == FULL_HEADS
    assert _tables(test_db, scratch_schema) == tables_before


# ---------------------------------------------------------------------------
# 3. The earlier rows are gone
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_modules_0002_scrubs_legacy_rows(test_db, scratch_schema, full_tree):
    """Every earlier row, active or not, and both earlier indexes: gone.

    Nothing is copied into the new table.  The sentinel is looked for in the
    text form of every row of every table in the schema, in both the plain and
    the encoded form it was stored in.
    """
    assert tree_profiles.bootstrap_schema(full_tree, scratch_schema).returncode == 0
    _make_previous_full_release_database(test_db, scratch_schema)
    encoded = base64.b64encode(json.dumps({"password": SENTINEL}).encode()).decode()
    assert _rows_mentioning(test_db, scratch_schema, SENTINEL) == {CONNECTIONS: 2}
    assert _rows_mentioning(test_db, scratch_schema, encoded) == {CONNECTIONS: 2}

    upgrade = tree_profiles.alembic(full_tree, scratch_schema, "upgrade", "heads")
    assert upgrade.returncode == 0, upgrade.stderr[-3000:]

    assert _rows_mentioning(test_db, scratch_schema, SENTINEL) == {}
    assert _rows_mentioning(test_db, scratch_schema, encoded) == {}
    with test_db.connect() as conn:
        assert (
            conn.execute(
                text(f'SELECT count(*) FROM "{scratch_schema}"."{CONNECTIONS}"')
            ).scalar_one()
            == 0
        )
    indexes = {
        index["name"]
        for index in inspect(test_db).get_indexes(CONNECTIONS, schema=scratch_schema)
    }
    assert f"ix_{scratch_schema}_warehouse_conn_active" not in indexes


# ---------------------------------------------------------------------------
# 3b. A table that only resembles the earlier one is refused, not dropped
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize(
    "alteration",
    [
        pytest.param(
            "ADD COLUMN customer_notes text", id="earlier-columns-plus-one-more"
        ),
        pytest.param("DROP COLUMN is_active", id="earlier-columns-minus-one"),
        pytest.param(
            "ALTER COLUMN name TYPE varchar(300)", id="earlier-columns-other-type"
        ),
    ],
)
def test_modules_0002_refuses_a_look_alike_table(
    test_db, scratch_schema, full_tree, alteration
):
    """Only exactly the earlier table is dropped; anything else stops the upgrade.

    Each variant still has ``encrypted_credentials``, so a test on that one
    column would drop it.  The upgrade must instead fail, name the table, and
    leave the table, its rows and ``alembic_version`` exactly as they were.
    """
    assert tree_profiles.bootstrap_schema(full_tree, scratch_schema).returncode == 0
    planted = _make_previous_full_release_database(test_db, scratch_schema)
    with test_db.begin() as conn:
        conn.execute(
            text(f'ALTER TABLE "{scratch_schema}"."{CONNECTIONS}" {alteration}')
        )
    columns_before = _columns(test_db, scratch_schema, CONNECTIONS)
    assert EARLIER_SHAPE_COLUMN in columns_before

    upgrade = tree_profiles.alembic(full_tree, scratch_schema, "upgrade", "heads")

    assert upgrade.returncode != 0, upgrade.stdout[-2000:]
    assert f"{scratch_schema}.{CONNECTIONS}" in upgrade.stderr
    assert "matches neither" in upgrade.stderr
    assert "legacy warehouse connection rows" not in upgrade.stderr
    assert _rows(test_db, scratch_schema) == PREVIOUS_FULL_HEADS
    assert _columns(test_db, scratch_schema, CONNECTIONS) == columns_before
    assert _rows_mentioning(test_db, scratch_schema, SENTINEL) == {CONNECTIONS: planted}
    assert not {"warehouse_sources", "warehouse_analysis_runs"} & _tables(
        test_db, scratch_schema
    )


# ---------------------------------------------------------------------------
# 4. Downgrade, then upgrade again: a new-shape table is kept
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_reupgrade_keeps_new_shape_rows(test_db, scratch_schema, full_tree):
    """The removal is decided by the table's shape, not by its existence.

    A fresh full bootstrap builds the new tables from the models, so
    ``modules@-1`` has nothing of its own to drop and leaves them -- with their
    connections -- in place.  The next ``upgrade heads`` runs this revision
    again, meets a ``warehouse_connections`` that already has the new shape,
    and must leave it, and its rows, alone.
    """
    assert tree_profiles.bootstrap_schema(full_tree, scratch_schema).returncode == 0
    connection_id = uuid.uuid4()
    with test_db.begin() as conn:
        conn.execute(
            text(
                f'INSERT INTO "{scratch_schema}"."{CONNECTIONS}" '
                "(id, name, warehouse_type, parameters, query_timeout_seconds, "
                "max_runs_per_day, created_at, updated_at) VALUES "
                "(:id, 'kept', 'snowflake', '{}', 300, 20, now(), now())"
            ),
            {"id": connection_id},
        )

    down = tree_profiles.alembic(full_tree, scratch_schema, "downgrade", "modules@-1")
    assert down.returncode == 0, down.stderr[-3000:]
    assert _rows(test_db, scratch_schema) == PREVIOUS_FULL_HEADS

    up = tree_profiles.alembic(full_tree, scratch_schema, "upgrade", "heads")
    assert up.returncode == 0, up.stderr[-3000:]

    assert _rows(test_db, scratch_schema) == FULL_HEADS
    assert "legacy warehouse connection rows" not in up.stderr
    with test_db.connect() as conn:
        kept = conn.execute(
            text(f'SELECT id FROM "{scratch_schema}"."{CONNECTIONS}"')
        ).scalars()
        assert list(kept) == [connection_id]


# ---------------------------------------------------------------------------
# 5. Downgrading a table this revision created
# ---------------------------------------------------------------------------
def test_modules_0002_downgrade_restores_the_earlier_shape(
    test_db, scratch_schema, core_tree, full_tree
):
    """``modules@-1`` drops what the revision made and puts the earlier table back.

    Empty -- the rows the upgrade removed are not recoverable, which is why the
    supported way back is the snapshot taken before upgrading -- but in exactly
    the shape the previous release's model builds, so that release maps it.
    """
    assert tree_profiles.bootstrap_schema(core_tree, scratch_schema).returncode == 0
    upgrade = tree_profiles.alembic(full_tree, scratch_schema, "upgrade", "heads")
    assert upgrade.returncode == 0, upgrade.stderr[-3000:]

    down = tree_profiles.alembic(full_tree, scratch_schema, "downgrade", "modules@-1")
    assert down.returncode == 0, down.stderr[-3000:]

    assert _rows(test_db, scratch_schema) == PREVIOUS_FULL_HEADS
    tables = _tables(test_db, scratch_schema)
    assert CONNECTIONS in tables
    assert not {"warehouse_sources", "warehouse_analysis_runs"} & tables

    reference = f"{scratch_schema}_ref"
    with test_db.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{reference}"'))
        conn.execute(text(f'CREATE TABLE "{reference}".users (id uuid PRIMARY KEY)'))
    _earlier_connections_table(reference).create(test_db)
    restored = _shape(test_db, scratch_schema, CONNECTIONS)
    expected = _shape(test_db, reference, CONNECTIONS)
    assert restored == expected

    # ... and forward again, through the removal of the (empty) earlier table.
    again = tree_profiles.alembic(full_tree, scratch_schema, "upgrade", "heads")
    assert again.returncode == 0, again.stderr[-3000:]
    assert "removing 0 legacy warehouse connection rows" in again.stderr
    assert EARLIER_SHAPE_COLUMN not in _columns(test_db, scratch_schema, CONNECTIONS)
