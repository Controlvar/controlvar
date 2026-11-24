"""``t449_identity_aliases``: the identity aliases table, through the transitions (#449).

The revision adds the table ``identity_aliases`` and its index, each only when
absent, and changes nothing else. A schema built by ``create_all`` already has
both, so the tests that start from the previous release (0.47.0, whose core
head is ``t1812_user_totp``) take a bootstrapped schema with the table DROPPED
and ``alembic_version`` at the previous release's heads, in each profile, and
run the documented commands. Then:

1. ``alembic upgrade heads``: the ``alembic_version`` rows are this release's
   (printed), the table has exactly its columns, primary key, check and index
   and no foreign key, assignments and events are untouched, and autogenerate
   is EMPTY on that database;
2. the upgraded database and a ``create_all``-bootstrapped one have the same
   table, from the catalog (IA-M5);
3. IA-M1: the statements the upgrade from ``t1812_user_totp`` to this revision
   runs, captured online inside alembic's ``run_migrations``: once the
   ``alembic_version`` statements and the catalog reads are set aside, exactly
   the ``SET``, the ``CREATE TABLE`` (no ``REFERENCES``), the ``CREATE INDEX``
   on the new table and the reset, and every read set aside reads only
   ``pg_catalog`` or ``information_schema``;
4. IA-M1b: ``--sql`` is refused in both directions and emits no DDL;
5. IA-M1c: the upgrade commits beside a session holding the six tables the SDK
   routes use, in the modes they use them, and with a session holding an
   uncommitted ``CREATE TABLE`` of the same table it stops with this
   revision's refusal (SQLSTATE 55P03) and changes nothing. Each leg sets its
   own ``statement_timeout`` and asserts no elapsed time;
6. the downgrade drops the table with every link, leaves assignments and
   events, and an upgrade afterwards creates the table empty;
7. ``alembic heads`` names exactly the core head (``t2011_flag_environments``
   since #2011, which extends this revision) and the modules head.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import uuid
from pathlib import Path
from typing import List

import pytest
from sqlalchemy import inspect, text

from backend.app.db import bootstrap
from backend.tests.integration.database import tree_profiles
from backend.tests.integration.database.tree_profiles import CORE, FULL
from backend.tests.unit.db.test_upgrade_lock_budget import release

pytestmark = [pytest.mark.integration]

#: This revision, and the core revision it extends (0.47.0's core head).
REVISION = "t449_identity_aliases"
PREVIOUS_CORE_HEAD = "t1812_user_totp"
#: The core head of this tree: ``t2011_flag_environments`` (#2011), the
#: revision after this one, whose downgrade drops ``flag_environment_settings``
#: and ``api_keys.environment``.
CORE_HEAD = "t2011_flag_environments"
MODULES_HEAD = "modules_0005_redshift"
#: Tables a later core revision's downgrade drops: a downgrade from the head to
#: this test's target unapplies ``t2011_flag_environments`` too.
LATER_DOWNGRADE_TABLES = {"flag_environment_settings"}

PREVIOUS_ROWS = {
    CORE: {PREVIOUS_CORE_HEAD},
    FULL: {PREVIOUS_CORE_HEAD, MODULES_HEAD},
}
ROWS = {CORE: {CORE_HEAD}, FULL: {CORE_HEAD, MODULES_HEAD}}
#: The rows of a database upgraded to this revision and no further.
REVISION_ROWS = {CORE: {REVISION}, FULL: {REVISION, MODULES_HEAD}}

TABLE = "identity_aliases"
INDEX = "ix_identity_aliases_user_id"
CHECK = "check_identity_aliases_ids_differ"
VERSIONS = (
    Path(__file__).resolve().parents[3] / "app" / "db" / "migrations" / "versions"
)

#: The table as PostgreSQL describes it after either path: each column's
#: (``format_type``, NOT NULL, ``pg_get_expr`` of its default), in order.
EXPECTED_COLUMNS = [
    ("anonymous_id", "character varying(255)", True, None),
    ("user_id", "character varying(255)", True, None),
    ("created_at", "timestamp with time zone", True, "now()"),
]
#: Its constraints: the primary key and the named check, and no foreign key.
EXPECTED_CONSTRAINTS = {
    ("identity_aliases_pkey", "p", "PRIMARY KEY (anonymous_id)"),
    (CHECK, "c", "CHECK (((anonymous_id)::text <> (user_id)::text))"),
}
EXPECTED_INDEXES = {
    "CREATE UNIQUE INDEX identity_aliases_pkey ON <schema>.identity_aliases "
    "USING btree (anonymous_id)",
    f"CREATE INDEX {INDEX} ON <schema>.identity_aliases USING btree (user_id)",
}

#: The tables the SDK routes read or write, and the lock mode each takes on
#: them: what a request in flight holds while the upgrade runs.
SDK_TABLE_MODES = {
    "events": "ROW EXCLUSIVE",
    "assignments": "ROW EXCLUSIVE",
    "holdout_population": "ROW EXCLUSIVE",
    "event_dedup_keys": "ROW EXCLUSIVE",
    "api_keys": "ACCESS SHARE",
    "feature_flags": "ACCESS SHARE",
}

#: Each online leg's own bound on any one statement of the upgrade: a wait the
#: revision did not bound ends here (57014), never in a hung test.
LEG_STATEMENT_TIMEOUT = "-c statement_timeout=5s"

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
    yield from _scratch(test_db, "aliases")


@pytest.fixture
def reference_schema(test_db):
    yield from _scratch(test_db, "aliasesref")


def _rows(engine, schema: str) -> set[str]:
    return bootstrap.recorded_revisions(engine, schema)


def _tables(engine, schema: str) -> set[str]:
    return set(inspect(engine).get_table_names(schema=schema))


def _columns(engine, schema: str) -> list[tuple]:
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
            {"s": schema, "t": TABLE},
        ).all()
    return [tuple(row) for row in rows]


def _constraints(engine, schema: str) -> set[tuple]:
    """Every constraint on the table, a foreign key included."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT c.conname, c.contype, pg_get_constraintdef(c.oid) "
                "FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid "
                "JOIN pg_namespace n ON n.oid = t.relnamespace "
                "WHERE n.nspname = :s AND t.relname = :t"
            ),
            {"s": schema, "t": TABLE},
        ).all()
    return {
        (name, kind, definition.replace(schema, "<schema>"))
        for name, kind, definition in rows
    }


def _indexes(engine, schema: str) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT indexdef FROM pg_indexes WHERE schemaname = :s AND tablename = :t"
            ),
            {"s": schema, "t": TABLE},
        ).scalars()
        return {row.replace(f"{schema}.", "<schema>.") for row in rows}


def _definition(engine, schema: str) -> tuple:
    return (
        _columns(engine, schema),
        _constraints(engine, schema),
        _indexes(engine, schema),
    )


def _count(engine, schema: str, table: str) -> int:
    with engine.connect() as conn:
        return conn.execute(text(f'SELECT count(*) FROM "{schema}".{table}')).scalar()


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
    """A bootstrapped schema turned into what 0.47.0 left: no table, its heads."""
    tree = _bootstrapped(profile, schema, tmp_path)
    with test_db.begin() as conn:
        # IF EXISTS: a plant that removes the model leaves create_all without it.
        conn.execute(text(f'DROP TABLE IF EXISTS "{schema}".{TABLE}'))
    _stamp(test_db, schema, PREVIOUS_ROWS[profile])
    assert TABLE not in _tables(test_db, schema)
    return tree


def _insert_assignment_and_event(engine, schema: str, user_id: str) -> None:
    """One assignment and one event, as the previous release stores them."""
    experiment_id, variant_id = str(uuid.uuid4()), str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(
            text(
                f'INSERT INTO "{schema}".experiments (id, name, key, status, '
                "experiment_type, optimization_type, sequential_testing_enabled, "
                "bayesian_enabled, created_at, updated_at) VALUES (:id, 'Kept', "
                ":key, 'ACTIVE', 'A_B', 'fixed', false, false, now(), now())"
            ),
            {"id": experiment_id, "key": f"kept-{experiment_id[:8]}"},
        )
        conn.execute(
            text(
                f'INSERT INTO "{schema}".variants (id, experiment_id, name, '
                "is_control, traffic_allocation, created_at, updated_at) "
                "VALUES (:id, :e, 'control', true, 100, now(), now())"
            ),
            {"id": variant_id, "e": experiment_id},
        )
        conn.execute(
            text(
                f'INSERT INTO "{schema}".assignments (id, experiment_id, variant_id, '
                "user_id, created_at, updated_at) VALUES (:id, :e, :v, :u, now(), now())"
            ),
            {
                "id": str(uuid.uuid4()),
                "e": experiment_id,
                "v": variant_id,
                "u": user_id,
            },
        )
        conn.execute(
            text(
                f'INSERT INTO "{schema}".events '
                "(id, event_type, event_name, user_id, value, created_at, updated_at) "
                "VALUES (:id, 'purchase', 'purchase', :u, 20.0, :at, now())"
            ),
            {"id": str(uuid.uuid4()), "u": user_id, "at": "2026-11-01T12:00:00+00:00"},
        )


def _kept(engine, schema: str) -> tuple:
    """What the upgrade and the downgrade must leave alone."""
    with engine.connect() as conn:
        assignments = conn.execute(
            text(f'SELECT id::text, user_id FROM "{schema}".assignments ORDER BY 1')
        ).all()
        events = conn.execute(
            text(f'SELECT id::text, user_id, value FROM "{schema}".events ORDER BY 1')
        ).all()
    return [tuple(r) for r in assignments], [tuple(r) for r in events]


def _report(profile, label, engine, schema) -> None:
    print(f"[{profile}] {label}: alembic_version={sorted(_rows(engine, schema))}")
    print(f"[{profile}] {label}: {TABLE} present={TABLE in _tables(engine, schema)}")


def _load_revision(schema: str):
    """This revision's module, its schema pointed at *schema*."""
    (path,) = VERSIONS.glob(f"{REVISION}_*.py")
    spec = importlib.util.spec_from_file_location(
        f"_rev_{REVISION}_{uuid.uuid4().hex[:6]}", path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._SCHEMA = schema
    return module


# ---------------------------------------------------------------------------
# 1. The previous release, upgraded with `heads`
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_a_database_at_the_previous_release_gains_the_table(
    profile, test_db, scratch_schema, tmp_path
):
    tree = _at_previous_release(test_db, profile, scratch_schema, tmp_path)
    _insert_assignment_and_event(test_db, scratch_schema, "previous-release-user")
    kept = _kept(test_db, scratch_schema)
    _report(profile, "at the previous release", test_db, scratch_schema)

    upgrade = tree_profiles.alembic(tree, scratch_schema, "upgrade", "heads")
    assert upgrade.returncode == 0, upgrade.stderr[-3000:]
    _report(profile, "after upgrade heads", test_db, scratch_schema)

    assert _rows(test_db, scratch_schema) == ROWS[profile]
    assert _definition(test_db, scratch_schema) == (
        EXPECTED_COLUMNS,
        EXPECTED_CONSTRAINTS,
        EXPECTED_INDEXES,
    )
    assert inspect(test_db).get_foreign_keys(TABLE, schema=scratch_schema) == []
    assert _count(test_db, scratch_schema, TABLE) == 0
    assert _kept(test_db, scratch_schema) == kept

    result = tree_profiles.autogenerate(
        tree, scratch_schema, tmp_path, head=tree_profiles.CORE_HEAD
    )
    print(f"[{profile}] autogenerate on the upgraded database: {result.operations}")
    assert result.body is not None, result.describe()
    assert result.operations == [], result.describe()


# ---------------------------------------------------------------------------
# 2. The migration builds what create_all builds (IA-M5)
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_an_upgraded_database_and_a_bootstrapped_one_have_the_same_table(
    profile, test_db, scratch_schema, reference_schema, tmp_path
):
    """Autogenerate compares neither a primary key, a check nor a server
    default, so the catalog is compared here."""
    tree = _at_previous_release(test_db, profile, scratch_schema, tmp_path)
    upgrade = tree_profiles.alembic(tree, scratch_schema, "upgrade", "heads")
    assert upgrade.returncode == 0, upgrade.stderr[-3000:]
    created = tree_profiles.bootstrap_schema(tree, reference_schema)
    assert created.returncode == 0, created.stderr[-3000:]

    upgraded = _definition(test_db, scratch_schema)
    bootstrapped = _definition(test_db, reference_schema)
    print(f"[{profile}] upgraded:     {upgraded}")
    print(f"[{profile}] bootstrapped: {bootstrapped}")
    assert bootstrapped == (EXPECTED_COLUMNS, EXPECTED_CONSTRAINTS, EXPECTED_INDEXES)
    assert upgraded == bootstrapped


# ---------------------------------------------------------------------------
# 3. IA-M1: exactly these statements, captured online
# ---------------------------------------------------------------------------
#: Run in a subprocess from the tree: records every statement executed while
#: alembic's ``MigrationContext.run_migrations`` runs (the migration
#: transaction, its revisions and its ``alembic_version`` writes), and none of
#: what ``env.py`` does around it, then runs alembic's own command line.
_CAPTURE = r"""
import json
import sys

import sqlalchemy
from alembic.config import main
from alembic.runtime import migration
from sqlalchemy import event

statements = []
recording = {"on": False}


@event.listens_for(sqlalchemy.engine.Engine, "before_cursor_execute")
def _record(conn, cursor, statement, parameters, context, executemany):
    if recording["on"]:
        statements.append(statement)


_run = migration.MigrationContext.run_migrations


def _run_recorded(self, **kw):
    recording["on"] = True
    try:
        return _run(self, **kw)
    finally:
        recording["on"] = False


migration.MigrationContext.run_migrations = _run_recorded
try:
    main(argv=sys.argv[2:])
finally:
    with open(sys.argv[1], "w", encoding="utf-8") as handle:
        json.dump(statements, handle)
"""

_RELATION = re.compile(r"\b(?:FROM|JOIN)\s+([A-Za-z0-9_\".]{1,200})", re.I)
_CATALOGS = ("pg_catalog.", "information_schema.")


def _normal(statement: str) -> str:
    return " ".join(statement.split())


def _is_version_statement(statement: str) -> bool:
    return "alembic_version" in statement


def _is_read(statement: str) -> bool:
    return statement.lstrip().split(" ", 1)[0].upper() in {"SELECT", "WITH", "SHOW"}


def _non_catalog_relations(statement: str) -> List[str]:
    """Every relation a read names after FROM or JOIN that is not a catalog."""
    return [
        name
        for name in (m.replace('"', "") for m in _RELATION.findall(statement))
        if not name.lower().startswith(_CATALOGS)
    ]


def _captured_upgrade(tree, schema: str, tmp_path) -> List[str]:
    out = tmp_path / f"captured-{uuid.uuid4().hex[:6]}.json"
    done = tree_profiles.run(
        tree,
        [
            "-c",
            _CAPTURE,
            str(out),
            "-c",
            str(tree_profiles.alembic_ini(tree)),
            "upgrade",
            REVISION,
        ],
        schema,
    )
    assert done.returncode == 0, done.stderr[-3000:]
    return [_normal(s) for s in json.loads(out.read_text(encoding="utf-8"))]


@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_the_upgrade_from_the_previous_core_head_runs_exactly_its_statements(
    profile, test_db, scratch_schema, tmp_path
):
    tree = _at_previous_release(test_db, profile, scratch_schema, tmp_path)
    captured = _captured_upgrade(tree, scratch_schema, tmp_path)
    for statement in captured:
        print(f"[{profile}] captured: {statement[:200]}")
    assert _rows(test_db, scratch_schema) == REVISION_ROWS[profile]

    revision = _load_revision(scratch_schema)
    set_statement = f"SET LOCAL lock_timeout = '{revision._LOCK_TIMEOUT}'"
    # The SET's literal is the module constant, the constant is 250 ms, and the
    # budget map declares the same quarter second.
    assert revision._LOCK_TIMEOUT == "250ms"
    assert release("0.48.0").declared[REVISION] == 0.25

    kept = [s for s in captured if not _is_version_statement(s)]
    reads = [s for s in kept if _is_read(s)]
    remaining = [s for s in kept if not _is_read(s)]

    # Every read set aside reads only the catalog.
    outside = {s: _non_catalog_relations(s) for s in reads if _non_catalog_relations(s)}
    assert outside == {}, f"a read set aside reads a table: {outside}"

    for statement in remaining:
        for table in SDK_TABLE_MODES:
            assert not re.search(rf"\b{table}\b", statement), (
                f"statement touches {table}: {statement}"
            )
    creates = [s for s in remaining if s.startswith("CREATE TABLE")]
    for statement in creates:
        assert "REFERENCES" not in statement, (
            f"REFERENCES in the alias table's DDL: {statement}"
        )

    qualified = f"{scratch_schema}.{TABLE}"
    assert len(remaining) == 4, remaining
    assert remaining[0] == set_statement, remaining
    assert remaining[1].startswith(f"CREATE TABLE {qualified} ("), remaining
    assert remaining[2] == f"CREATE INDEX {INDEX} ON {qualified} (user_id)", remaining
    assert remaining[3] == "SET LOCAL lock_timeout TO DEFAULT", remaining


# ---------------------------------------------------------------------------
# 4. IA-M1b: offline mode, refused both ways
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_the_upgrade_and_the_downgrade_refuse_offline_mode(
    test_db, scratch_schema, tmp_path
):
    tree = _at_previous_release(test_db, CORE, scratch_schema, tmp_path)
    refusal = _load_revision(scratch_schema)._OFFLINE_REFUSAL
    for argv in (
        ("upgrade", f"{PREVIOUS_CORE_HEAD}:{REVISION}", "--sql"),
        ("downgrade", f"{REVISION}:{PREVIOUS_CORE_HEAD}", "--sql"),
    ):
        offline = tree_profiles.alembic(tree, scratch_schema, *argv)
        assert offline.returncode != 0, (argv, offline.stdout[-2000:])
        assert refusal in offline.stderr, (
            f"refusal text missing from --sql output: {argv} {offline.stderr[-2000:]}"
        )
        for ddl in ("CREATE", "DROP", "ALTER"):
            assert ddl not in offline.stdout, f"{ddl} emitted offline: {argv}"
    assert _rows(test_db, scratch_schema) == PREVIOUS_ROWS[CORE]
    assert TABLE not in _tables(test_db, scratch_schema)


# ---------------------------------------------------------------------------
# 5. IA-M1c: the locks it does not take, and the one wait it bounds
# ---------------------------------------------------------------------------
def _upgrade_with_its_own_timeout(tree, schema: str) -> subprocess.CompletedProcess:
    """``upgrade`` to this revision, named: what these legs claim is about this
    revision alone, never a later one that ``heads`` would also run."""
    return tree_profiles.alembic(
        tree,
        schema,
        "upgrade",
        REVISION,
        extra_env={"PGOPTIONS": LEG_STATEMENT_TIMEOUT},
    )


@pytest.mark.regression
def test_the_upgrade_commits_beside_sessions_holding_the_sdk_tables(
    test_db, scratch_schema, tmp_path
):
    """A request in flight holds the SDK tables (ROW EXCLUSIVE where it writes,
    ACCESS SHARE where it reads). Creating a table that references one of them
    would wait for it; this revision takes no lock on any of them."""
    tree = _at_previous_release(test_db, CORE, scratch_schema, tmp_path)
    holder = test_db.connect()
    transaction = holder.begin()
    try:
        for table, mode in SDK_TABLE_MODES.items():
            holder.execute(
                text(f'LOCK TABLE "{scratch_schema}".{table} IN {mode} MODE')
            )
        upgrade = _upgrade_with_its_own_timeout(tree, scratch_schema)
    finally:
        transaction.rollback()
        holder.close()
    blocked = [t for t in SDK_TABLE_MODES if t in upgrade.stderr]
    assert upgrade.returncode == 0, (
        f"upgrade blocked by a reader on {blocked or 'an SDK table'}: "
        f"{upgrade.stderr[-3000:]}"
    )
    assert "55P03" not in upgrade.stderr and "lock timeout" not in upgrade.stderr
    assert _rows(test_db, scratch_schema) == REVISION_ROWS[CORE]
    assert TABLE in _tables(test_db, scratch_schema)


#: Where the 55P03 leg starts: 0.47.0's head, where the upgrade runs this
#: revision alone, and the revision before it, where one upgrade runs
#: ``t1812_user_totp`` and then this one, so the refusal must take back the
#: first revision's table too.
START_POINTS = {
    "from-0.47.0": (PREVIOUS_CORE_HEAD, ()),
    "two-revisions": ("4955571d232c", ("user_totp",)),
}


@pytest.mark.regression
@pytest.mark.parametrize("start", sorted(START_POINTS))
def test_a_concurrent_creation_of_the_table_ends_the_upgrade_with_the_refusal(
    start, test_db, scratch_schema, tmp_path
):
    """Why the bound is kept: another session's uncommitted ``CREATE TABLE`` of the
    same name makes this revision's ``CREATE TABLE`` wait. The ``lock_timeout``
    ends that wait (SQLSTATE 55P03) with this revision's refusal, and nothing is
    applied: no revision of the run, ``alembic_version`` as it was. Without the
    ``SET`` only the leg's own ``statement_timeout`` ends it (57014), and this
    fails."""
    tree = _at_previous_release(test_db, CORE, scratch_schema, tmp_path)
    recorded, earlier_tables = START_POINTS[start]
    with test_db.begin() as conn:
        for table in earlier_tables:
            conn.execute(text(f'DROP TABLE "{scratch_schema}".{table}'))
    _stamp(test_db, scratch_schema, {recorded})
    expected = _load_revision(scratch_schema)._lock_refusal()
    rival = test_db.connect()
    transaction = rival.begin()
    try:
        rival.execute(text(f'CREATE TABLE "{scratch_schema}".{TABLE} (rival integer)'))
        upgrade = _upgrade_with_its_own_timeout(tree, scratch_schema)
    finally:
        transaction.rollback()
        rival.close()
    print(f"[{start}] upgrade stderr tail:\n{upgrade.stderr[-1500:]}")
    assert upgrade.returncode != 0
    assert expected in upgrade.stderr, upgrade.stderr[-3000:]
    assert "statement timeout" not in upgrade.stderr
    assert _rows(test_db, scratch_schema) == {recorded}
    tables = _tables(test_db, scratch_schema)
    assert TABLE not in tables
    for table in earlier_tables:
        assert table not in tables, f"{table} was applied by a refused upgrade"


# ---------------------------------------------------------------------------
# 6. Downgrade
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_the_downgrade_drops_every_link_and_keeps_assignments_and_events(
    profile, test_db, scratch_schema, tmp_path
):
    tree = _bootstrapped(profile, scratch_schema, tmp_path)
    _insert_assignment_and_event(test_db, scratch_schema, "known-user")
    with test_db.begin() as conn:
        conn.execute(
            text(
                f'INSERT INTO "{scratch_schema}".{TABLE} (anonymous_id, user_id) '
                "VALUES ('anonymous-visitor', 'known-user')"
            )
        )
    tables = _tables(test_db, scratch_schema)
    kept = _kept(test_db, scratch_schema)

    down = tree_profiles.alembic(tree, scratch_schema, "downgrade", PREVIOUS_CORE_HEAD)
    assert down.returncode == 0, down.stderr[-3000:]
    _report(profile, "after downgrade", test_db, scratch_schema)

    assert _rows(test_db, scratch_schema) == PREVIOUS_ROWS[profile]
    assert _tables(test_db, scratch_schema) == tables - {TABLE} - LATER_DOWNGRADE_TABLES
    assert _kept(test_db, scratch_schema) == kept

    upgrade = tree_profiles.alembic(tree, scratch_schema, "upgrade", "heads")
    assert upgrade.returncode == 0, upgrade.stderr[-3000:]
    _report(profile, "after upgrade heads again", test_db, scratch_schema)
    assert _rows(test_db, scratch_schema) == ROWS[profile]
    assert _count(test_db, scratch_schema, TABLE) == 0


# ---------------------------------------------------------------------------
# 7. The heads
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
