"""``37dcb2969766``: a segment's kind and the members of an id list, through the transitions.

The revision adds ``segments.kind`` (``VARCHAR(16) NOT NULL``, default
``'rules'``), the check ``ck_segments_kind`` and the table ``segment_members``
(primary key ``(segment_id, member_id)``, ``segment_id`` referencing
``segments.id`` ON DELETE CASCADE), each only when absent. A schema built by
``create_all`` already has all three, so every test here starts from what the
previous release (``d29a479daafe``) left instead: a bootstrapped schema with
the three DROPPED and ``alembic_version`` at ``d29a479daafe``, in each profile,
and runs the documented ``alembic upgrade heads``. Then:

1. the ``alembic_version`` rows are this release's, the column, the check and
   the table's constraints are there, the segments already stored read
   ``rules``, and autogenerate against the result is empty;
2. the upgraded database and a ``create_all``-bootstrapped one carry the same
   ``kind`` default (``pg_get_expr``) and the same constraints on ``segments``
   and ``segment_members`` (``pg_get_constraintdef``), compared by name.
   Autogenerate compares neither defaults nor checks, so (1) cannot see a
   model and a migration that disagree on them;
3. a writer that does not know ``kind`` -- the previous release's image after a
   rollback -- can still insert a segment, and it reads ``rules``;
4. the downgrade refuses while ``segment_members`` has rows, naming the
   override and the row count and changing nothing; it runs when the table is
   empty, and with ``-x allow_member_loss=true``; and it removes exactly what
   the upgrade added.

Each test prints the ``alembic_version`` rows and the constraints it saw.
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
REVISION = "37dcb2969766"
PREVIOUS_CORE_HEAD = "d29a479daafe"
#: The core head of this tree: ``607c51a59bf5`` (#1497), after
#: ``4c2c09a2b3e8`` (#1206), the revision after this one.
CORE_HEAD = "607c51a59bf5"
MODULES_HEAD = "modules_0003_lower_is_better"

PREVIOUS_ROWS = {
    CORE: {PREVIOUS_CORE_HEAD},
    FULL: {PREVIOUS_CORE_HEAD, MODULES_HEAD},
}
ROWS = {
    CORE: {CORE_HEAD},
    FULL: {CORE_HEAD, MODULES_HEAD},
}

TABLE = "segment_members"
CHECK = "ck_segments_kind"
OVERRIDE = "allow_member_loss=true"

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
    yield from _scratch(test_db, "segm")


@pytest.fixture
def reference_schema(test_db):
    yield from _scratch(test_db, "segmref")


def _rows(engine, schema: str) -> set[str]:
    return bootstrap.recorded_revisions(engine, schema)


def _tables(engine, schema: str) -> set[str]:
    return set(inspect(engine).get_table_names(schema=schema))


def _kind_column(engine, schema: str):
    """(data_type, is_nullable, character_maximum_length, default) or None."""
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT c.data_type, c.is_nullable, c.character_maximum_length, "
                "pg_get_expr(d.adbin, d.adrelid) "
                "FROM information_schema.columns c "
                "JOIN pg_namespace n ON n.nspname = c.table_schema "
                "JOIN pg_class t ON t.relnamespace = n.oid AND t.relname = c.table_name "
                "JOIN pg_attribute a ON a.attrelid = t.oid AND a.attname = c.column_name "
                "LEFT JOIN pg_attrdef d ON d.adrelid = t.oid AND d.adnum = a.attnum "
                "WHERE c.table_schema = :s AND c.table_name = 'segments' "
                "AND c.column_name = 'kind'"
            ),
            {"s": schema},
        ).first()
    return tuple(row) if row else None


def _constraints(engine, schema: str, table: str) -> dict[str, str]:
    """Every constraint on *schema*.*table*: name -> definition."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT c.conname, pg_get_constraintdef(c.oid) "
                "FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid "
                "JOIN pg_namespace n ON n.oid = t.relnamespace "
                "WHERE n.nspname = :s AND t.relname = :t"
            ),
            {"s": schema, "t": table},
        ).all()
    return {name: definition.replace(schema, "<schema>") for name, definition in rows}


def _insert_segment_without_kind(engine, schema: str) -> uuid.UUID:
    """A segment written as the previous release writes it: no ``kind``."""
    segment_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                f'INSERT INTO "{schema}".segments (id, name, status, rules, '
                "created_at, updated_at) VALUES (:id, :name, 'ACTIVE', "
                "CAST(:rules AS jsonb), now(), now())"
            ),
            {"id": segment_id, "name": f"segm-{segment_id.hex[:8]}", "rules": "{}"},
        )
    return segment_id


def _kind_of(engine, schema: str, segment_id: uuid.UUID) -> str:
    with engine.connect() as conn:
        return conn.execute(
            text(f'SELECT kind FROM "{schema}".segments WHERE id = :id'),
            {"id": segment_id},
        ).scalar_one()


def _add_members(engine, schema: str, count: int) -> uuid.UUID:
    segment_id = _insert_segment_without_kind(engine, schema)
    with engine.begin() as conn:
        conn.execute(
            text(f"UPDATE \"{schema}\".segments SET kind = 'id_list' WHERE id = :id"),
            {"id": segment_id},
        )
        for n in range(count):
            conn.execute(
                text(
                    f'INSERT INTO "{schema}".segment_members (segment_id, member_id) '
                    "VALUES (:s, :m)"
                ),
                {"s": segment_id, "m": f"user-{n}"},
            )
    return segment_id


def _member_rows(engine, schema: str) -> int:
    with engine.connect() as conn:
        return conn.execute(text(f'SELECT count(*) FROM "{schema}".{TABLE}')).scalar()


def _at_previous_release(engine, schema: str, rows: set[str]) -> None:
    """Turn a bootstrapped *schema* into what ``d29a479daafe`` left."""
    with engine.begin() as conn:
        conn.execute(text(f'DROP TABLE "{schema}".{TABLE}'))
        # IF EXISTS: a model that lost the check must fail the parity test
        # below, not this setup.
        conn.execute(
            text(f'ALTER TABLE "{schema}".segments DROP CONSTRAINT IF EXISTS "{CHECK}"')
        )
        conn.execute(text(f'ALTER TABLE "{schema}".segments DROP COLUMN kind'))
        conn.execute(text(f'DELETE FROM "{schema}".alembic_version'))
        for revision in sorted(rows):
            conn.execute(
                text(f'INSERT INTO "{schema}".alembic_version VALUES (:rev)'),
                {"rev": revision},
            )
    assert _kind_column(engine, schema) is None
    assert TABLE not in _tables(engine, schema)
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


def _report(profile, label, engine, schema):
    print(f"[{profile}] {label}: alembic_version={sorted(_rows(engine, schema))}")
    print(f"[{profile}] {label}: segments.kind={_kind_column(engine, schema)}")
    print(
        f"[{profile}] {label}: segments constraints={_constraints(engine, schema, 'segments')}"
    )
    print(
        f"[{profile}] {label}: {TABLE} constraints={_constraints(engine, schema, TABLE)}"
    )


# ---------------------------------------------------------------------------
# 1. The previous release, upgraded with `heads`
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_previous_release_upgraded_with_heads(
    profile, test_db, scratch_schema, tmp_path
):
    tree = _bootstrapped_at_previous_release(test_db, profile, scratch_schema, tmp_path)
    existing = _insert_segment_without_kind(test_db, scratch_schema)

    _upgrade(tree, scratch_schema)
    _report(profile, "after upgrade heads", test_db, scratch_schema)

    assert _rows(test_db, scratch_schema) == ROWS[profile]
    assert _kind_column(test_db, scratch_schema) == (
        "character varying",
        "NO",
        16,
        "'rules'::character varying",
    )
    segments = _constraints(test_db, scratch_schema, "segments")
    assert segments[CHECK] == (
        "CHECK (((kind)::text = ANY ((ARRAY['rules'::character varying, "
        "'id_list'::character varying])::text[])))"
    )
    members = _constraints(test_db, scratch_schema, TABLE)
    assert sorted(members.values()) == sorted(
        [
            "PRIMARY KEY (segment_id, member_id)",
            "FOREIGN KEY (segment_id) REFERENCES <schema>.segments(id) ON DELETE CASCADE",
        ]
    )
    assert _kind_of(test_db, scratch_schema, existing) == "rules"

    result = tree_profiles.autogenerate(
        tree, scratch_schema, tmp_path, head=tree_profiles.CORE_HEAD
    )
    assert result.body is not None, result.describe()
    assert result.operations == [], result.describe()


# ---------------------------------------------------------------------------
# 2. The migration's default and constraints are the model's
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_upgraded_and_bootstrapped_schemas_match(
    profile, test_db, scratch_schema, reference_schema, tmp_path
):
    """The only gate on these: autogenerate compares neither defaults nor checks.

    A fresh deployment gets ``kind`` from the model (``create_all``), an
    existing one from this migration; a different check would let one of
    them store a kind the other refuses.
    """
    tree = _bootstrapped_at_previous_release(test_db, profile, scratch_schema, tmp_path)
    _upgrade(tree, scratch_schema)
    created = tree_profiles.bootstrap_schema(tree, reference_schema)
    assert created.returncode == 0, created.stderr[-3000:]

    _report(profile, "upgraded", test_db, scratch_schema)
    _report(profile, "bootstrapped", test_db, reference_schema)
    assert _kind_column(test_db, scratch_schema) == _kind_column(
        test_db, reference_schema
    )
    bootstrapped = _constraints(test_db, reference_schema, "segments")
    assert CHECK in bootstrapped
    assert _constraints(test_db, scratch_schema, "segments") == bootstrapped
    assert _constraints(test_db, scratch_schema, TABLE) == _constraints(
        test_db, reference_schema, TABLE
    )


# ---------------------------------------------------------------------------
# 3. A writer that does not know the column
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_an_insert_without_kind_gets_rules(profile, test_db, scratch_schema, tmp_path):
    tree = _bootstrapped_at_previous_release(test_db, profile, scratch_schema, tmp_path)
    _upgrade(tree, scratch_schema)

    segment_id = _insert_segment_without_kind(test_db, scratch_schema)

    assert _kind_of(test_db, scratch_schema, segment_id) == "rules"


# ---------------------------------------------------------------------------
# 4. Downgrade
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_downgrade_with_members_is_refused(profile, test_db, scratch_schema, tmp_path):
    tree = tree_profiles.tree_for(profile, tmp_path)
    assert tree_profiles.bootstrap_schema(tree, scratch_schema).returncode == 0
    _add_members(test_db, scratch_schema, 3)
    tables = _tables(test_db, scratch_schema)

    down = tree_profiles.alembic(tree, scratch_schema, "downgrade", PREVIOUS_CORE_HEAD)
    _report(profile, "after refused downgrade", test_db, scratch_schema)

    assert down.returncode != 0, down.stdout[-2000:]
    assert "segment_members holds 3 row(s)" in down.stderr
    assert f"-x {OVERRIDE}" in down.stderr
    assert "Nothing was changed" in down.stderr
    assert _rows(test_db, scratch_schema) == ROWS[profile]
    assert _member_rows(test_db, scratch_schema) == 3
    assert _kind_column(test_db, scratch_schema) is not None
    assert _tables(test_db, scratch_schema) == tables


@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_downgrade_when_empty_removes_exactly_what_it_added(
    profile, test_db, scratch_schema, tmp_path
):
    tree = tree_profiles.tree_for(profile, tmp_path)
    assert tree_profiles.bootstrap_schema(tree, scratch_schema).returncode == 0
    tables = _tables(test_db, scratch_schema)
    segments = _constraints(test_db, scratch_schema, "segments")
    assert CHECK in segments

    down = tree_profiles.alembic(tree, scratch_schema, "downgrade", PREVIOUS_CORE_HEAD)
    _report(profile, "after empty downgrade", test_db, scratch_schema)

    assert down.returncode == 0, down.stderr[-3000:]
    assert _rows(test_db, scratch_schema) == PREVIOUS_ROWS[profile]
    assert _kind_column(test_db, scratch_schema) is None
    assert _constraints(test_db, scratch_schema, "segments") == {
        name: d for name, d in segments.items() if name != CHECK
    }
    assert _tables(test_db, scratch_schema) == tables - {TABLE}


@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_downgrade_with_the_override_runs(profile, test_db, scratch_schema, tmp_path):
    tree = tree_profiles.tree_for(profile, tmp_path)
    assert tree_profiles.bootstrap_schema(tree, scratch_schema).returncode == 0
    _add_members(test_db, scratch_schema, 2)
    tables = _tables(test_db, scratch_schema)

    down = tree_profiles.alembic(
        tree, scratch_schema, "-x", OVERRIDE, "downgrade", PREVIOUS_CORE_HEAD
    )
    _report(profile, "after downgrade with the override", test_db, scratch_schema)

    assert down.returncode == 0, down.stderr[-3000:]
    assert _rows(test_db, scratch_schema) == PREVIOUS_ROWS[profile]
    assert _kind_column(test_db, scratch_schema) is None
    assert _tables(test_db, scratch_schema) == tables - {TABLE}

    # And the upgrade after it brings back an empty table, every segment rules.
    _upgrade(tree, scratch_schema)
    assert _rows(test_db, scratch_schema) == ROWS[profile]
    assert _member_rows(test_db, scratch_schema) == 0


@pytest.mark.parametrize("value", ["false", "yes", ""])
def test_only_true_is_the_override(value, test_db, scratch_schema, tmp_path):
    tree = tree_profiles.tree_for(CORE, tmp_path)
    assert tree_profiles.bootstrap_schema(tree, scratch_schema).returncode == 0
    _add_members(test_db, scratch_schema, 1)

    down = tree_profiles.alembic(
        tree,
        scratch_schema,
        "-x",
        f"allow_member_loss={value}",
        "downgrade",
        PREVIOUS_CORE_HEAD,
    )

    assert down.returncode != 0, down.stdout[-2000:]
    assert _member_rows(test_db, scratch_schema) == 1
