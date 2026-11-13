"""``85dd4796bb8b``: the ``event_dedup_keys`` table, through the transitions (#1543).

The revision creates ``event_dedup_keys`` (``user_id``, ``dedup_key``,
``stored_event_id``, ``created_at``; ``PRIMARY KEY (user_id, dedup_key)``; no
foreign key and no other index), only when absent, and changes nothing else.
A schema built by ``create_all`` already has the table, so the tests that start
from the previous release take a bootstrapped schema with the table DROPPED and
``alembic_version`` at ``607c51a59bf5``, in each profile, and run the
documented commands. SPEC #1543 v1.2's gates, each named where it is held:

* M1: raw ``alembic upgrade heads`` from the previous release: the table
  exists with exactly this primary key, the events are untouched, the
  ``alembic_version`` rows are this release's (printed), and autogenerate
  after it is empty;
* M1b: the upgrade, raw and through ``db/bootstrap.py`` (what all three deploy
  paths run), completes within 10 s while another connection holds an
  uncommitted ``events`` INSERT -- the previous release still serving. A
  foreign key to ``events`` would make it wait for that writer;
* M2: the table the migration builds equals the one ``create_all`` builds:
  columns (type, NOT NULL, default), constraints and indexes, from the
  catalog. Autogenerate does not compare a primary key, so this is the gate
  on it;
* M3: the previous release's write path -- ``EventService.track_event``,
  which this release leaves unchanged and which N-1's ``/track``, ``/batch``
  and ``/events`` all call -- stores an event against this schema and writes
  no key;
* M5: the downgrade, online and with an events writer open, removes exactly
  the table;
* M6: ``alembic heads`` names the core head after this revision,
  ``4955571d232c`` (#1545) since it landed, and the modules head.

M4 (this release reads rows stored without a key, as the previous release
stored them) is in ``backend/tests/integration/api/test_tracking_duplicate_events.py``.
"""

from __future__ import annotations

import subprocess
import sys
import time
import uuid

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from backend.app.db import bootstrap
from backend.app.models.event import Event
from backend.app.services.event_service import EventService
from backend.tests.integration.database import tree_profiles
from backend.tests.integration.database.tree_profiles import CORE, FULL

pytestmark = [pytest.mark.integration]

#: This revision, and the core revision it extends.
REVISION = "85dd4796bb8b"
PREVIOUS_CORE_HEAD = "607c51a59bf5"
#: The core head of this tree: ``4955571d232c`` (#1545), after
#: ``c448_balance_within`` (#448) and ``7b6b4b4fe246`` (#1528), the revision
#: after this one, whose downgrade drops ``rollout_stages.started_at`` and no
#: table.
CORE_HEAD = "4955571d232c"
#: Tables a later core revision's downgrade drops: a downgrade from the head to
#: this test's target unapplies those revisions too. ``c448_balance_within``
#: (#448) drops ``balance_group_counters``; ``4955571d232c`` (#1545) drops
#: ``visual_tokens``.
LATER_DOWNGRADE_TABLES = {"balance_group_counters", "visual_tokens"}
MODULES_HEAD = "modules_0004_rbac_activation"

PREVIOUS_ROWS = {
    CORE: {PREVIOUS_CORE_HEAD},
    FULL: {PREVIOUS_CORE_HEAD, MODULES_HEAD},
}
ROWS = {
    CORE: {CORE_HEAD},
    FULL: {CORE_HEAD, MODULES_HEAD},
}

TABLE = "event_dedup_keys"
#: The table as PostgreSQL describes it after either path: each column's
#: (``format_type``, NOT NULL, ``pg_get_expr`` of its default), in order.
EXPECTED_COLUMNS = [
    ("user_id", "character varying(255)", True, None),
    ("dedup_key", "character varying(64)", True, None),
    ("stored_event_id", "uuid", True, None),
    ("created_at", "timestamp with time zone", True, "now()"),
]
#: Its only constraint, and its only index.
EXPECTED_CONSTRAINTS = {("p", "PRIMARY KEY (user_id, dedup_key)")}
EXPECTED_INDEXES = {
    "CREATE UNIQUE INDEX event_dedup_keys_pkey ON <schema>.event_dedup_keys "
    "USING btree (user_id, dedup_key)"
}

#: How long the upgrade and the downgrade may take beside an open events writer.
ONLINE_SECONDS = 10

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
    yield from _scratch(test_db, "dedupkeys")


@pytest.fixture
def reference_schema(test_db):
    yield from _scratch(test_db, "dedupkeysref")


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
                "WHERE n.nspname = :s AND t.relname = :t "
                "AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum"
            ),
            {"s": schema, "t": TABLE},
        ).all()
    return [tuple(row) for row in rows]


def _constraints(engine, schema: str) -> set[tuple]:
    """Every constraint on the table: (type, definition), a foreign key included."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT c.contype, pg_get_constraintdef(c.oid) "
                "FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid "
                "JOIN pg_namespace n ON n.oid = t.relnamespace "
                "WHERE n.nspname = :s AND t.relname = :t"
            ),
            {"s": schema, "t": TABLE},
        ).all()
    return {(kind, definition.replace(schema, "<schema>")) for kind, definition in rows}


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


def _events(engine, schema: str) -> int:
    with engine.connect() as conn:
        return conn.execute(text(f'SELECT count(*) FROM "{schema}".events')).scalar()


def _keys(engine, schema: str) -> int:
    with engine.connect() as conn:
        return conn.execute(text(f'SELECT count(*) FROM "{schema}".{TABLE}')).scalar()


def _insert_event(conn, schema: str, user_id: str) -> None:
    """An event as the previous release writes it, naming only its columns."""
    conn.execute(
        text(
            f'INSERT INTO "{schema}".events '
            "(id, event_type, event_name, user_id, value, created_at, updated_at) "
            "VALUES (:id, 'purchase', 'purchase', :u, 20.0, :at, now())"
        ),
        {"id": str(uuid.uuid4()), "u": user_id, "at": "2026-11-01T12:00:00+00:00"},
    )


def _at_previous_release(engine, schema: str, rows: set[str]) -> None:
    """Turn a bootstrapped *schema* into what the previous release left."""
    with engine.begin() as conn:
        # IF EXISTS: a plant that removes the model leaves create_all without it.
        conn.execute(text(f'DROP TABLE IF EXISTS "{schema}".{TABLE}'))
        conn.execute(text(f'DELETE FROM "{schema}".alembic_version'))
        for revision in sorted(rows):
            conn.execute(
                text(f'INSERT INTO "{schema}".alembic_version VALUES (:rev)'),
                {"rev": revision},
            )
        for i in range(3):
            _insert_event(conn, schema, f"previous-release-{i}")
    assert TABLE not in _tables(engine, schema)
    assert _rows(engine, schema) == rows


def _bootstrapped(profile, schema, tmp_path):
    tree = tree_profiles.tree_for(profile, tmp_path)
    created = tree_profiles.bootstrap_schema(tree, schema)
    assert created.returncode == 0, created.stderr[-3000:]
    return tree


def _bootstrapped_at_previous_release(test_db, profile, schema, tmp_path):
    tree = _bootstrapped(profile, schema, tmp_path)
    _at_previous_release(test_db, schema, PREVIOUS_ROWS[profile])
    return tree


def _report(profile, label, engine, schema) -> None:
    print(f"[{profile}] {label}: alembic_version={sorted(_rows(engine, schema))}")
    print(f"[{profile}] {label}: {TABLE} columns={_columns(engine, schema)}")
    print(f"[{profile}] {label}: {TABLE} constraints={_constraints(engine, schema)}")


def _bounded(
    tree, schema: str, argv: list[str]
) -> tuple[subprocess.CompletedProcess, float]:
    """Run ``python <argv>`` from *tree* with at most ``ONLINE_SECONDS``.

    Fails the test, naming the timeout, if it does not finish: the child is
    killed, so its connection closes and nothing it began is committed.
    """
    started = time.monotonic()
    try:
        done = subprocess.run(
            [sys.executable, *argv],
            cwd=str(tree),
            env=tree_profiles._environment(tree, schema),
            capture_output=True,
            text=True,
            timeout=ONLINE_SECONDS,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"{' '.join(argv)} did not finish within {ONLINE_SECONDS} s beside an "
            "uncommitted events writer"
        )
    return done, time.monotonic() - started


# ---------------------------------------------------------------------------
# M1: the previous release, upgraded with raw `upgrade heads`
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_m1_a_database_at_the_previous_release_gains_the_table(
    profile, test_db, scratch_schema, tmp_path
):
    tree = _bootstrapped_at_previous_release(test_db, profile, scratch_schema, tmp_path)
    events_before = _events(test_db, scratch_schema)
    _report(profile, "at the previous release", test_db, scratch_schema)

    upgrade = tree_profiles.alembic(tree, scratch_schema, "upgrade", "heads")
    assert upgrade.returncode == 0, upgrade.stderr[-3000:]
    _report(profile, "after upgrade heads", test_db, scratch_schema)

    assert TABLE in _tables(test_db, scratch_schema)
    assert _rows(test_db, scratch_schema) == ROWS[profile]
    pk = inspect(test_db).get_pk_constraint(TABLE, schema=scratch_schema)
    assert pk["constrained_columns"] == ["user_id", "dedup_key"]
    assert inspect(test_db).get_foreign_keys(TABLE, schema=scratch_schema) == []
    assert _events(test_db, scratch_schema) == events_before == 3
    assert _keys(test_db, scratch_schema) == 0

    result = tree_profiles.autogenerate(
        tree, scratch_schema, tmp_path, head=tree_profiles.CORE_HEAD
    )
    assert result.body is not None, result.describe()
    assert result.operations == [], result.describe()


# ---------------------------------------------------------------------------
# M1b: online, beside an uncommitted events writer
# ---------------------------------------------------------------------------
UPGRADE_COMMANDS = {
    "alembic upgrade heads": lambda tree: [
        "-m",
        "alembic",
        "-c",
        str(tree_profiles.alembic_ini(tree)),
        "upgrade",
        "heads",
    ],
    "bootstrap": lambda tree: ["-m", "backend.app.db.bootstrap"],
}


@pytest.mark.regression
@pytest.mark.parametrize("command", sorted(UPGRADE_COMMANDS))
def test_m1b_the_upgrade_completes_beside_an_uncommitted_events_writer(
    command, test_db, scratch_schema, tmp_path
):
    """The previous release keeps serving during a deploy: an INSERT into
    ``events`` that has not committed holds a lock on ``events``. Creating a
    table that references ``events`` waits for it; this one takes no lock on
    ``events`` and finishes."""
    tree = _bootstrapped_at_previous_release(test_db, CORE, scratch_schema, tmp_path)
    writer = test_db.connect()
    transaction = writer.begin()
    try:
        _insert_event(writer, scratch_schema, "uncommitted-writer")
        done, seconds = _bounded(tree, scratch_schema, UPGRADE_COMMANDS[command](tree))
    finally:
        transaction.rollback()
        writer.close()
    print(f"[{command}] finished in {seconds:.2f} s beside an uncommitted writer")
    assert done.returncode == 0, done.stderr[-3000:]
    assert TABLE in _tables(test_db, scratch_schema)
    assert _rows(test_db, scratch_schema) == ROWS[CORE]


# ---------------------------------------------------------------------------
# M2: the migration's table is the model's
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_m2_an_upgraded_database_and_a_bootstrapped_one_have_the_same_table(
    profile, test_db, scratch_schema, reference_schema, tmp_path
):
    """A fresh deployment gets the table from the model (``create_all``), an
    existing one from this migration. Autogenerate compares neither a primary
    key nor a server default, so the catalog is compared here."""
    tree = _bootstrapped_at_previous_release(test_db, profile, scratch_schema, tmp_path)
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
# M3: the previous release's writes against this schema
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_m3_the_previous_releases_write_path_stores_events_on_this_schema(
    profile, test_db, scratch_schema, tmp_path
):
    """``EventService.track_event`` is what the previous release's three
    routes call, and this release leaves it as it was: it stores the event,
    and writes no key, on an upgraded schema. A NOT NULL column added to
    ``events`` would refuse the previous release's INSERT."""
    tree = _bootstrapped_at_previous_release(test_db, profile, scratch_schema, tmp_path)
    upgrade = tree_profiles.alembic(tree, scratch_schema, "upgrade", "heads")
    assert upgrade.returncode == 0, upgrade.stderr[-3000:]
    before = _events(test_db, scratch_schema)

    models_schema = Event.__table__.schema
    db = Session(
        bind=test_db.execution_options(
            schema_translate_map={models_schema: scratch_schema}
        )
    )
    try:
        stored = EventService(db).track_event(
            {
                "user_id": "n-minus-1",
                "event_type": "purchase",
                "event_name": "purchase",
                "value": 20.0,
            },
            allow_untagged=True,
        )
        stored_id = stored.id
    finally:
        db.close()
    with test_db.begin() as conn:
        _insert_event(conn, scratch_schema, "n-minus-1-sql")

    assert _events(test_db, scratch_schema) == before + 2
    assert _keys(test_db, scratch_schema) == 0
    with test_db.connect() as conn:
        assert (
            conn.execute(
                text(f'SELECT value FROM "{scratch_schema}".events WHERE id = :i'),
                {"i": str(stored_id)},
            ).scalar()
            == 20.0
        )


# ---------------------------------------------------------------------------
# M5: downgrade
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_m5_the_downgrade_removes_exactly_the_table_online(
    profile, test_db, scratch_schema, tmp_path
):
    tree = _bootstrapped(profile, scratch_schema, tmp_path)
    with test_db.begin() as conn:
        conn.execute(
            text(
                f'INSERT INTO "{scratch_schema}".{TABLE} '
                "(user_id, dedup_key, stored_event_id) VALUES ('u', :k, :i)"
            ),
            {"k": "0" * 64, "i": str(uuid.uuid4())},
        )
        _insert_event(conn, scratch_schema, "kept-by-the-downgrade")
    tables = _tables(test_db, scratch_schema)
    events = _events(test_db, scratch_schema)
    assert TABLE in tables

    writer = test_db.connect()
    transaction = writer.begin()
    try:
        _insert_event(writer, scratch_schema, "uncommitted-writer")
        down, seconds = _bounded(
            tree,
            scratch_schema,
            [
                "-m",
                "alembic",
                "-c",
                str(tree_profiles.alembic_ini(tree)),
                "downgrade",
                PREVIOUS_CORE_HEAD,
            ],
        )
    finally:
        transaction.rollback()
        writer.close()
    print(f"[{profile}] downgrade finished in {seconds:.2f} s beside a writer")
    assert down.returncode == 0, down.stderr[-3000:]
    _report(profile, "after downgrade", test_db, scratch_schema)

    assert _rows(test_db, scratch_schema) == PREVIOUS_ROWS[profile]
    assert _tables(test_db, scratch_schema) == tables - {TABLE} - LATER_DOWNGRADE_TABLES
    assert _events(test_db, scratch_schema) == events

    # And upgrading again creates the table again, empty.
    upgrade = tree_profiles.alembic(tree, scratch_schema, "upgrade", "heads")
    assert upgrade.returncode == 0, upgrade.stderr[-3000:]
    assert _rows(test_db, scratch_schema) == ROWS[profile]
    assert _keys(test_db, scratch_schema) == 0


@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_after_the_downgrade_this_release_refuses_only_events_with_an_event_id(
    profile, test_db, scratch_schema, tmp_path
):
    """The window between the downgrade and the API's rollback: this release,
    with the table gone, raises for an event that carries an ``event_id`` (the
    routes answer 500) and stores every other event as before."""
    from sqlalchemy.exc import ProgrammingError

    tree = _bootstrapped(profile, scratch_schema, tmp_path)
    down = tree_profiles.alembic(tree, scratch_schema, "downgrade", PREVIOUS_CORE_HEAD)
    assert down.returncode == 0, down.stderr[-3000:]
    assert TABLE not in _tables(test_db, scratch_schema)
    before = _events(test_db, scratch_schema)

    models_schema = Event.__table__.schema
    db = Session(
        bind=test_db.execution_options(
            schema_translate_map={models_schema: scratch_schema}
        )
    )
    event = {
        "user_id": "after-the-downgrade",
        "event_type": "purchase",
        "event_name": "purchase",
        "value": 20.0,
    }
    try:
        service = EventService(db)
        with pytest.raises(ProgrammingError):
            service.track_event_once(event, "order-1", allow_untagged=True)
        stored_id, created, _ = service.track_event_once(
            event, None, allow_untagged=True
        )
    finally:
        db.close()

    assert created is True and stored_id is not None
    assert _events(test_db, scratch_schema) == before + 1


# ---------------------------------------------------------------------------
# M6: the heads
# ---------------------------------------------------------------------------
HEADS = {CORE: {CORE_HEAD}, FULL: {CORE_HEAD, MODULES_HEAD}}


@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_m6_alembic_heads_are_this_revision_and_the_modules_head(profile, tmp_path):
    """Still two heads in a full tree: this revision extended the core chain,
    and ``7b6b4b4fe246`` (#1528), ``c448_balance_within`` (#448) and
    ``4955571d232c`` (#1545), the core head now, extend it."""
    tree = tree_profiles.tree_for(profile, tmp_path)
    heads = tree_profiles.alembic(tree, "public", "heads")
    assert heads.returncode == 0, heads.stderr[-3000:]
    print(f"[{profile}] alembic heads:\n{heads.stdout}")
    named = {line.split()[0] for line in heads.stdout.splitlines() if line.strip()}
    assert named == HEADS[profile]
