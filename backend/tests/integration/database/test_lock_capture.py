"""The lock capture (``lock_capture.py``) on a revision that exists.

``t1934_metric_window`` adds a column and a check to ``metrics`` under one
ACCESS EXCLUSIVE lock and touches no other table. A bootstrapped schema with
the column dropped and ``alembic_version`` at ``t2012_flag_split`` is the
database it meets; ``alembic upgrade t1934_metric_window`` runs it alone, held
before its commit:

* in each profile, the capture is exactly ``metrics`` in that mode plus
  alembic's own two modes on ``alembic_version``, and the run then commits;
* planted, the revision also reads ``api_keys``, a table no list names: the
  capture has it and the comparison fails naming it;
* planted, it also builds an index on ``metrics``: the same tables, one more
  mode, and the comparison fails, so names alone would have passed it;
* a backend that is not inside a transaction, or is inside one that has
  written nothing (as after the commit), is refused, never read as an empty
  set.

``until_the_pid_waits`` sees a second writer queued on a row, which waits for
a tuple lock rather than a relation lock.
"""

from __future__ import annotations

import threading
import time

import pytest
from sqlalchemy import text

from backend.tests.integration.database import lock_capture, tree_profiles
from backend.tests.integration.database.test_flag_environments_migration import (
    _rows,
    _scratch,
    _stamp,
)
from backend.tests.integration.database.tree_profiles import CORE, FULL

pytestmark = [pytest.mark.integration]

REVISION = "t1934_metric_window"
PARENT = "t2012_flag_split"
MODULES_HEAD = "modules_0005_redshift"
PARENT_ROWS = {CORE: {PARENT}, FULL: {PARENT, MODULES_HEAD}}
REVISION_ROWS = {CORE: {REVISION}, FULL: {REVISION, MODULES_HEAD}}

#: What the revision locks among the tables that exist before it runs.
DECLARED = {"metrics": {"AccessExclusiveLock"}}

#: Planted after the revision's own steps: a read of a table no list names,
#: and an index on the table it already locks.
READS_API_KEYS = (
    "_op.get_bind().exec_driver_sql(\n"
    "    f'SELECT count(*) FROM \"{_schema}\".api_keys'\n"
    ")"
)
INDEXES_METRICS = (
    "_op.execute(\n"
    "    f'CREATE INDEX ix_lock_capture_plant ON \"{_schema}\".metrics (name)'\n"
    ")"
)

BOTH_PROFILES = [CORE, pytest.param(FULL, marks=pytest.mark.modules)]


@pytest.fixture
def scratch_schema(test_db):
    yield from _scratch(test_db, "lockcap")


def _at_parent(test_db, profile: str, schema: str, tmp_path):
    """A pausing copy of the tree, and *schema* as ``t2012_flag_split`` left it."""
    tree = lock_capture.pausing_tree(profile, tmp_path)
    created = tree_profiles.bootstrap_schema(tree, schema)
    assert created.returncode == 0, created.stderr[-3000:]
    with test_db.begin() as conn:
        conn.execute(
            text(f'ALTER TABLE "{schema}".metrics DROP COLUMN conversion_window_hours')
        )
    _stamp(test_db, schema, PARENT_ROWS[profile])
    return tree


def _captured(test_db, tree, schema: str) -> lock_capture.Capture:
    """Run the revision alone, held before its commit; the capture, once the
    run has committed. The target is named, never ``heads``: a later revision
    must not join the run."""
    with lock_capture.held_before_commit(tree, schema, "upgrade", REVISION) as held:
        found = lock_capture.capture(test_db, held, schema)
    print(f"captured: {found}")
    assert held.process.returncode == 0, held.output()[-3000:]
    return found


@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_the_capture_is_exactly_what_the_revision_locks(
    profile, test_db, scratch_schema, tmp_path
):
    tree = _at_parent(test_db, profile, scratch_schema, tmp_path)

    found = _captured(test_db, tree, scratch_schema)

    lock_capture.assert_locks(found, DECLARED)
    assert found.locks == {
        "alembic_version": {"AccessShareLock", "RowExclusiveLock"},
        "metrics": {"AccessExclusiveLock"},
    }
    assert _rows(test_db, scratch_schema) == REVISION_ROWS[profile]


@pytest.mark.regression
def test_a_table_no_list_names_is_caught(test_db, scratch_schema, tmp_path):
    tree = _at_parent(test_db, CORE, scratch_schema, tmp_path)
    lock_capture.plant_after_upgrade(tree, REVISION, READS_API_KEYS)

    found = _captured(test_db, tree, scratch_schema)

    assert found.locks["api_keys"] == {"AccessShareLock"}
    with pytest.raises(
        AssertionError,
        match=r"api_keys: \{AccessShareLock\}, metrics: \{AccessExclusiveLock\}\} "
        r"!= declared",
    ):
        lock_capture.assert_locks(found, DECLARED)


@pytest.mark.regression
def test_one_more_mode_on_the_same_table_is_caught(test_db, scratch_schema, tmp_path):
    tree = _at_parent(test_db, CORE, scratch_schema, tmp_path)
    lock_capture.plant_after_upgrade(tree, REVISION, INDEXES_METRICS)

    found = _captured(test_db, tree, scratch_schema)

    expected = {**lock_capture.BOOKKEEPING, **DECLARED}
    assert set(found.locks) == set(expected), "the same tables"
    assert found.locks["metrics"] == {"AccessExclusiveLock", "ShareLock"}
    with pytest.raises(
        AssertionError,
        match=r"metrics: \{AccessExclusiveLock, ShareLock\}\} != declared",
    ):
        lock_capture.assert_locks(found, DECLARED)


@pytest.mark.regression
def test_a_backend_outside_a_transaction_is_refused(test_db, scratch_schema):
    """After the commit the backend holds nothing: an empty capture would be
    wrong for every declared set, so it is refused rather than read."""
    with test_db.connect() as idle:
        pid = idle.execute(text("SELECT pg_backend_pid()")).scalar()
        idle.commit()
        held = lock_capture.Held(pid, process=None, log=None)
        with pytest.raises(AssertionError, match="not held inside its transaction"):
            lock_capture.capture(test_db, held, scratch_schema)


@pytest.mark.regression
def test_a_backend_in_a_transaction_that_wrote_nothing_is_refused(
    test_db, scratch_schema
):
    """A pause after the commit: the pause's own query opens a new
    transaction, so the backend is idle in transaction, but that transaction
    has written nothing and holds none of the run's locks."""
    with test_db.connect() as reading:
        pid = reading.execute(text("SELECT pg_backend_pid()")).scalar()
        held = lock_capture.Held(pid, process=None, log=None)
        with pytest.raises(AssertionError, match="with xid None, not held inside"):
            lock_capture.capture(test_db, held, scratch_schema)
        reading.rollback()


def _until_blocked(engine, pid: int) -> None:
    """Return once backend *pid* waits for any lock."""
    deadline = time.monotonic() + lock_capture.WAIT_SECONDS
    while time.monotonic() < deadline:
        with engine.connect() as conn:
            blocked = conn.execute(
                text("SELECT cardinality(pg_blocking_pids(:p)) > 0"), {"p": pid}
            ).scalar()
            conn.rollback()
        if blocked:
            return
        time.sleep(0.01)
    raise AssertionError(f"backend {pid} never waited")


@pytest.mark.regression
def test_a_second_writer_queued_on_a_row_is_seen_waiting(test_db, scratch_schema):
    """The first writer of a held row waits for the holder's transaction and
    takes the row's tuple lock; the second waits for that tuple lock, not a
    relation lock. ``until_the_pid_waits`` names its table all the same."""
    table = f'"{scratch_schema}".lock_capture_rows'
    with test_db.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{scratch_schema}"'))
        conn.execute(text(f"CREATE TABLE {table} (id int PRIMARY KEY, n int)"))
        conn.execute(text(f"INSERT INTO {table} VALUES (1, 0)"))
    update = text(f"UPDATE {table} SET n = n + 1 WHERE id = 1")
    with (
        test_db.connect() as holder,
        test_db.connect() as first,
        test_db.connect() as second,
    ):
        holder.execute(update)
        writers = []
        try:
            for writer in (first, second):
                pid = writer.execute(text("SELECT pg_backend_pid()")).scalar()
                thread = threading.Thread(target=writer.execute, args=(update,))
                thread.start()
                writers.append((writer, thread))
                _until_blocked(test_db, pid)
            waited_for = lock_capture.until_the_pid_waits(test_db, pid)
            with test_db.connect() as conn:
                waits = conn.execute(
                    text(
                        "SELECT locktype FROM pg_locks WHERE pid = :p AND NOT granted"
                    ),
                    {"p": pid},
                ).all()
                conn.rollback()
        finally:
            holder.rollback()
            for writer, thread in writers:
                thread.join(timeout=lock_capture.WAIT_SECONDS)
                writer.rollback()
    assert [row.locktype for row in waits] == ["tuple"]
    assert waited_for == f"{scratch_schema}.lock_capture_rows", waited_for
