"""``modules_0003_lower_is_better`` across the databases it can meet (#1386).

The revision adds ``warehouse_sources.lower_is_better`` (``boolean NOT NULL
DEFAULT false``).  Each transition runs as the deployment runs it -- raw
``alembic upgrade heads`` or ``downgrade`` in a subprocess -- against both
origins a full database can have:

* **migrated**: a core bootstrap, then the full image's migrations, so the
  column comes from this revision's ``add_column``;
* **bootstrapped**: a fresh full bootstrap, so the column comes from the
  model's ``create_all`` and this revision is only stamped.

What is pinned:

1. a database the previous full release left at
   ``modules_0002_warehouse_analysis``, with sources of both kinds, upgrades
   and every existing source reads ``false``;
2. the previous release's INSERT, which does not name the column, still
   succeeds on this schema and the source reads ``false``;
3. the column's server default is ``false`` on both origins -- the shape
   comparison and autogenerate in ``test_modules_0002_transitions.py`` never
   look at a server default, so without this a model that lost its
   ``server_default`` would pass every other transition test;
4. ``downgrade modules_0002_warehouse_analysis`` drops the column on both
   origins and keeps the tables and their rows; an upgrade afterwards brings
   the column back ``false`` on every row, not the value that was stored.
   The compliance audit trail is core, so the downgrade leaves it alone: a
   source's latest entry still says it was marked lower is better, which is
   the record the operator page points to.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import inspect, text

from backend.app.db import bootstrap
from backend.tests.integration.database import tree_profiles
from backend.tests.integration.database.tree_profiles import CORE, FULL

pytestmark = [pytest.mark.integration, pytest.mark.modules]

CORE_HEAD = tree_profiles.CORE_HEAD
#: The modules head of the previous full release, and this revision's parent.
PREVIOUS_MODULES_HEAD = "modules_0002_warehouse_analysis"
#: This revision: the modules branch's head.
MODULES_HEAD = "modules_0003_lower_is_better"
PREVIOUS_FULL_HEADS = {CORE_HEAD, PREVIOUS_MODULES_HEAD}
FULL_HEADS = {CORE_HEAD, MODULES_HEAD}

SOURCES = "warehouse_sources"
COLUMN = "lower_is_better"
WAREHOUSE_TABLES = {"warehouse_connections", SOURCES, "warehouse_analysis_runs"}

MIGRATED = "migrated"
BOOTSTRAPPED = "bootstrapped"
ORIGINS = pytest.mark.parametrize("origin", [MIGRATED, BOOTSTRAPPED])

#: The columns the previous release's model gives a source, as its INSERT
#: names them: everything ``modules_0002_warehouse_analysis`` created, and not
#: this revision's column.  A literal list, so a model change cannot move it.
PREVIOUS_RELEASE_SOURCE_COLUMNS = (
    "id",
    "connection_id",
    "kind",
    "name",
    "table_reference",
    "column_mapping",
    "filters",
    "metric_type",
    "conversion_window_hours",
    "cap_value",
    "validated_at",
    "created_by",
    "created_at",
    "updated_at",
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def scratch_schema(test_db):
    schema = f"wh0003_{uuid.uuid4().hex[:8]}"
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


def _ok(result) -> None:
    assert result.returncode == 0, (result.stdout[-2000:], result.stderr[-3000:])


def _rows(engine, schema: str) -> set[str]:
    return bootstrap.recorded_revisions(engine, schema)


def _tables(engine, schema: str) -> set[str]:
    return set(inspect(engine).get_table_names(schema=schema))


def _columns(engine, schema: str, table: str) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns(table, schema=schema)}


def _catalogue(engine, schema: str) -> tuple | None:
    """``(data_type, is_nullable, column_default)`` of the column, or None."""
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT data_type, is_nullable, column_default "
                "FROM information_schema.columns WHERE table_schema = :schema "
                "AND table_name = :table AND column_name = :column"
            ),
            {"schema": schema, "table": SOURCES, "column": COLUMN},
        ).one_or_none()
    return tuple(row) if row is not None else None


def _at_head(origin: str, engine, schema: str, core_tree, full_tree) -> None:
    """A full database at this tree's heads, built the *origin* way."""
    if origin == MIGRATED:
        _ok(tree_profiles.bootstrap_schema(core_tree, schema))
        _ok(tree_profiles.alembic(full_tree, schema, "upgrade", "heads"))
    else:
        _ok(tree_profiles.bootstrap_schema(full_tree, schema))
    assert _rows(engine, schema) == FULL_HEADS


def _insert_connection(engine, schema: str) -> uuid.UUID:
    connection_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                f'INSERT INTO "{schema}".warehouse_connections '
                "(id, name, warehouse_type, parameters, query_timeout_seconds, "
                "max_runs_per_day, created_at, updated_at) VALUES "
                "(:id, 'kept', 'snowflake', '{}', 300, 20, now(), now())"
            ),
            {"id": connection_id},
        )
    return connection_id


def _insert_source_as_the_previous_release(
    engine, schema: str, connection_id: uuid.UUID, kind: str
) -> uuid.UUID:
    """One source, written with the previous release's column list only."""
    source_id = uuid.uuid4()
    metric = kind == "metric"
    values = {
        "id": source_id,
        "connection_id": connection_id,
        "kind": kind,
        "name": f"{kind} source",
        "table_reference": '{"table": "events"}',
        "column_mapping": "{}",
        "filters": "[]",
        "metric_type": "proportion" if metric else None,
        "conversion_window_hours": 168 if metric else None,
        "cap_value": None,
        "validated_at": None,
        "created_by": None,
    }
    columns = ", ".join(PREVIOUS_RELEASE_SOURCE_COLUMNS)
    placeholders = ", ".join(
        "now()" if name in ("created_at", "updated_at") else f":{name}"
        for name in PREVIOUS_RELEASE_SOURCE_COLUMNS
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                f'INSERT INTO "{schema}".{SOURCES} ({columns}) VALUES ({placeholders})'
            ),
            values,
        )
    return source_id


def _directions(engine, schema: str) -> list[tuple[str, bool]]:
    with engine.connect() as conn:
        return [
            tuple(row)
            for row in conn.execute(
                text(f'SELECT kind, {COLUMN} FROM "{schema}".{SOURCES} ORDER BY kind')
            )
        ]


def _source_ids(engine, schema: str) -> set[uuid.UUID]:
    with engine.connect() as conn:
        return set(conn.execute(text(f'SELECT id FROM "{schema}".{SOURCES}')).scalars())


def _audit_source_update(engine, schema: str, source_id: uuid.UUID) -> None:
    """The compliance audit entry the source API writes for an edit that
    marks the source lower is better (the definition is
    ``warehouse_source_service.stored_definition``'s, abridged)."""
    definition = {"kind": "metric", "name": "metric source", COLUMN: True}
    with engine.begin() as conn:
        conn.execute(
            text(
                f'INSERT INTO "{schema}".audit_events_v2 '
                "(id, action, resource_type, resource_id, new_value, outcome) "
                "VALUES (:id, 'UPDATE', 'warehouse_source', :source_id, "
                "CAST(:definition AS jsonb), 'SUCCESS')"
            ),
            {
                "id": uuid.uuid4(),
                "source_id": str(source_id),
                "definition": json.dumps(definition),
            },
        )


def _audited_direction(engine, schema: str, source_id: uuid.UUID):
    """The direction in the source's latest audit entry."""
    with engine.connect() as conn:
        return conn.execute(
            text(
                f"SELECT new_value -> '{COLUMN}' FROM \"{schema}\".audit_events_v2 "
                "WHERE resource_type = 'warehouse_source' AND resource_id = :id "
                "ORDER BY timestamp DESC LIMIT 1"
            ),
            {"id": str(source_id)},
        ).scalar_one()


# ---------------------------------------------------------------------------
# 1. The previous release's database, upgraded
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_a_database_at_the_previous_release_upgrades_and_every_source_reads_false(
    test_db, scratch_schema, core_tree, full_tree, tmp_path
):
    """The upgrade every existing full deployment takes, by raw ``upgrade heads``.

    The database is built by migrations up to ``modules_0002_warehouse_analysis``
    -- what the previous full release left -- and holds a source of each kind,
    written as that release writes them.  ``ADD COLUMN ... NOT NULL`` without
    the server default fails here on the rows already stored.
    """
    _ok(tree_profiles.bootstrap_schema(core_tree, scratch_schema))
    _ok(
        tree_profiles.alembic(
            full_tree, scratch_schema, "upgrade", PREVIOUS_MODULES_HEAD
        )
    )
    assert _rows(test_db, scratch_schema) == PREVIOUS_FULL_HEADS
    assert COLUMN not in _columns(test_db, scratch_schema, SOURCES)
    connection_id = _insert_connection(test_db, scratch_schema)
    for kind in ("assignment", "metric"):
        _insert_source_as_the_previous_release(
            test_db, scratch_schema, connection_id, kind
        )

    _ok(tree_profiles.alembic(full_tree, scratch_schema, "upgrade", "heads"))

    assert _rows(test_db, scratch_schema) == FULL_HEADS
    assert _directions(test_db, scratch_schema) == [
        ("assignment", False),
        ("metric", False),
    ]
    assert _catalogue(test_db, scratch_schema) == ("boolean", "NO", "false")
    autogenerated = tree_profiles.autogenerate(
        full_tree, scratch_schema, tmp_path, head="modules@head"
    )
    assert autogenerated.body is not None, autogenerated.describe()
    assert autogenerated.operations == [], autogenerated.describe()


# ---------------------------------------------------------------------------
# 2. The previous release writes sources against this schema
# ---------------------------------------------------------------------------
@pytest.mark.regression
@ORIGINS
def test_the_previous_release_still_inserts_a_source_and_it_reads_false(
    test_db, scratch_schema, core_tree, full_tree, origin
):
    """During a deploy, and after an image-only rollback, the previous release
    keeps creating sources without naming the column: the database's default
    has to supply it, on either origin."""
    _at_head(origin, test_db, scratch_schema, core_tree, full_tree)
    connection_id = _insert_connection(test_db, scratch_schema)

    _insert_source_as_the_previous_release(
        test_db, scratch_schema, connection_id, "metric"
    )

    assert _directions(test_db, scratch_schema) == [("metric", False)]


# ---------------------------------------------------------------------------
# 3. The server default, on both origins
# ---------------------------------------------------------------------------
@pytest.mark.regression
@ORIGINS
def test_the_column_is_boolean_not_null_with_server_default_false(
    test_db, scratch_schema, core_tree, full_tree, origin
):
    """The migration and the model must agree on the default as well as the
    type and nullability, and nothing else compares the default: the shape
    check reads name, type and NOT NULL, and env.py's autogenerate does not
    compare server defaults."""
    _at_head(origin, test_db, scratch_schema, core_tree, full_tree)

    assert _catalogue(test_db, scratch_schema) == ("boolean", "NO", "false")


# ---------------------------------------------------------------------------
# 4. Downgrade to the previous modules revision, and back
# ---------------------------------------------------------------------------
@pytest.mark.regression
@ORIGINS
def test_downgrade_drops_the_column_keeps_the_rows_and_an_upgrade_restores_false(
    test_db, scratch_schema, core_tree, full_tree, origin
):
    """``downgrade modules_0002_warehouse_analysis`` deletes every source's
    setting, on a bootstrapped database too, and keeps the sources; an
    upgrade afterwards puts the column back ``false``, not the stored value.
    The audit trail still holds the setting the source had."""
    _at_head(origin, test_db, scratch_schema, core_tree, full_tree)
    connection_id = _insert_connection(test_db, scratch_schema)
    _insert_source_as_the_previous_release(
        test_db, scratch_schema, connection_id, "assignment"
    )
    marked = _insert_source_as_the_previous_release(
        test_db, scratch_schema, connection_id, "metric"
    )
    with test_db.begin() as conn:
        conn.execute(
            text(
                f'UPDATE "{scratch_schema}".{SOURCES} SET {COLUMN} = true '
                "WHERE kind = 'metric'"
            )
        )
    _audit_source_update(test_db, scratch_schema, marked)
    assert _directions(test_db, scratch_schema) == [
        ("assignment", False),
        ("metric", True),
    ]
    ids = _source_ids(test_db, scratch_schema)

    _ok(
        tree_profiles.alembic(
            full_tree, scratch_schema, "downgrade", PREVIOUS_MODULES_HEAD
        )
    )

    assert _rows(test_db, scratch_schema) == PREVIOUS_FULL_HEADS
    assert COLUMN not in _columns(test_db, scratch_schema, SOURCES)
    assert WAREHOUSE_TABLES <= _tables(test_db, scratch_schema)
    assert _source_ids(test_db, scratch_schema) == ids
    assert _audited_direction(test_db, scratch_schema, marked) is True

    _ok(tree_profiles.alembic(full_tree, scratch_schema, "upgrade", "heads"))

    assert _rows(test_db, scratch_schema) == FULL_HEADS
    assert _source_ids(test_db, scratch_schema) == ids
    assert _directions(test_db, scratch_schema) == [
        ("assignment", False),
        ("metric", False),
    ]
    assert _catalogue(test_db, scratch_schema) == ("boolean", "NO", "false")
    # The record of which sources were marked outlives the column.
    assert _audited_direction(test_db, scratch_schema, marked) is True
