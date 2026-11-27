"""What a migration run's own backend holds in ``pg_locks`` before it commits.

Not a test module (pytest does not collect it). The lock rule for a new
revision: its capture test reads ``pg_locks`` for the upgrade's backend before
the commit and asserts the tables locked equal the declared set, comparing lock
modes as well as names. A reader filtered to a typed list of tables can never
see a table outside the list (``test_events_created_at_utc_migration.py`` read
only ``feature_flags`` and ``users``), so nothing here takes one.

* :func:`pausing_tree` copies this tree (core or full) and plants a pause into
  the copy's ``env.py``: after the run's last revision and its
  ``alembic_version`` write, inside the transaction, before the commit. It
  pauses only when ``LOCK_CAPTURE_DIR`` is set, so the same copy also builds
  the schema with the bootstrap first. The checkout is never edited.
* :func:`held_before_commit` runs ``alembic`` from such a copy in another
  process and yields once the run is held, with the backend's pid, which the
  run writes itself: a file handshake, never a sleep.
* :func:`capture` reads what that backend holds, refusing a backend that is
  not inside a transaction that has written: read after the commit, the set
  is empty and every comparison is wrong. :func:`assert_locks` compares it
  with a declared set.
* :func:`locks_held` is the reader itself, for one backend or every backend
  but the caller's; :func:`until_the_pid_waits` is the handshake that waits for
  a backend to wait for a lock.

What the reader counts: every granted relation lock on a relation of the
schema that another session can see, keyed by table name, with a lock on an
index or a TOAST relation counted on its table. A relation the run created is
not committed, so no other session sees it and it is not counted (locking it
blocks nobody); the capture reports how many such locks there were.
``alembic_version`` is counted like any other table: every run reads it and
writes it in the same transaction (:data:`BOOKKEEPING`), and
:func:`assert_locks` expects exactly those two modes on it unless the declared
set says otherwise.
"""

from __future__ import annotations

import contextlib
import dataclasses
import pathlib
import re
import subprocess
import sys
import time
import uuid
from typing import Dict, FrozenSet, Iterator, Mapping, Optional, Tuple

from sqlalchemy import text

from backend.tests.integration.database import tree_profiles
from backend.tests.integration.database.tree_profiles import FULL

#: alembic's own table, and the two modes every run holds on it before its
#: commit: it reads the recorded heads, then writes the run's own.
VERSION_TABLE = "alembic_version"
BOOKKEEPING: Dict[str, FrozenSet[str]] = {
    VERSION_TABLE: frozenset({"AccessShareLock", "RowExclusiveLock"})
}

#: Ceilings, never assertions: how long a held run waits to be resumed, how
#: long a test waits for the run to pause, and for a backend to wait.
PAUSE_SECONDS = 120
WAIT_SECONDS = 60

#: The variable that makes a planted ``env.py`` pause, naming the directory
#: of the handshake files.
PAUSE_VARIABLE = "LOCK_CAPTURE_DIR"

_ENV_PY = ("backend", "app", "db", "migrations", "env.py")
_VERSIONS = ("app", "db", "migrations", "versions")

#: The run's transaction in ``env.py``; the pause goes straight after
#: ``run_migrations()``, inside it.
_ANCHOR = (
    "            with context.begin_transaction():\n"
    "                context.run_migrations()\n"
)
_PAUSE = f"""\
                # Planted by lock_capture.py in a copy of the tree: hold the
                # run here, before its commit, while a test reads pg_locks.
                import os as _lc_os
                import pathlib as _lc_pathlib
                import time as _lc_time

                if _lc_os.environ.get("{PAUSE_VARIABLE}"):
                    _lc_dir = _lc_pathlib.Path(_lc_os.environ["{PAUSE_VARIABLE}"])
                    _lc_pid = connection.exec_driver_sql(
                        "SELECT pg_backend_pid()"
                    ).scalar()
                    (_lc_dir / "pid.tmp").write_text(str(_lc_pid))
                    _lc_os.replace(_lc_dir / "pid.tmp", _lc_dir / "paused")
                    _lc_until = _lc_time.monotonic() + {PAUSE_SECONDS}
                    while not (_lc_dir / "resume").exists():
                        if _lc_time.monotonic() > _lc_until:
                            raise RuntimeError("lock capture: never resumed")
                        _lc_time.sleep(0.02)
"""

#: Every granted relation lock on a relation of the schema, as (table, mode);
#: an index or TOAST relation's lock is counted on its table. A relation this
#: session cannot see (created by an uncommitted run) joins nothing.
_HELD = """
SELECT owner.relname, l.mode
FROM pg_locks l
JOIN pg_class c ON c.oid = l.relation
LEFT JOIN pg_index i ON i.indexrelid = c.oid
LEFT JOIN pg_class toasted ON toasted.reltoastrelid = COALESCE(i.indrelid, c.oid)
JOIN pg_class owner ON owner.oid = COALESCE(toasted.oid, i.indrelid, c.oid)
JOIN pg_namespace n ON n.oid = owner.relnamespace
WHERE l.locktype = 'relation' AND l.granted
  AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database())
  AND n.nspname = :schema AND {backends}
"""
#: The relation locks the backend holds on relations this session cannot see.
_UNSEEN = """
SELECT count(*) FROM pg_locks l
WHERE l.locktype = 'relation' AND l.granted AND l.pid = :pid
  AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database())
  AND NOT EXISTS (SELECT 1 FROM pg_class c WHERE c.oid = l.relation)
"""

Locks = Dict[str, FrozenSet[str]]


def locks_held(conn, schema: str, pid: Optional[int] = None) -> Locks:
    """``{table: modes}`` that backend *pid* holds on *schema*'s relations, or
    that every backend but *conn*'s own holds when *pid* is None."""
    if pid is None:
        backends, params = "l.pid <> pg_backend_pid()", {"schema": schema}
    else:
        backends, params = "l.pid = :pid", {"schema": schema, "pid": pid}
    found: Dict[str, set] = {}
    for table, mode in conn.execute(text(_HELD.format(backends=backends)), params):
        found.setdefault(table, set()).add(mode)
    return {table: frozenset(modes) for table, modes in found.items()}


def describe(locks: Mapping[str, FrozenSet[str]]) -> str:
    """*locks* as one sorted line: ``{metrics: {AccessExclusiveLock}}``."""
    inner = ", ".join(
        f"{table}: {{{', '.join(sorted(modes))}}}"
        for table, modes in sorted(locks.items())
    )
    return "{" + inner + "}"


def waiting_for(engine, pid: int) -> set[Tuple[str, str]]:
    """The ``(relation, mode)`` lock requests backend *pid* waits for; the
    relation as ``regclass`` prints it (schema-qualified off the search
    path). A wait on a row (a tuple lock) names its table too; a wait on
    another transaction names none."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT relation::regclass::text, mode FROM pg_locks "
                "WHERE pid = :p AND relation IS NOT NULL AND NOT granted"
            ),
            {"p": pid},
        ).all()
        conn.rollback()
    return {tuple(row) for row in rows}


def until_the_pid_waits(engine, pid: int) -> str:
    """The relation backend *pid* waits for a lock on (a table, or the table
    of a row), once it waits: the handshake that releases a holder at the
    moment the other side is queued, never after a sleep."""
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        waiting = waiting_for(engine, pid)
        if waiting:
            return sorted(waiting)[0][0]
        time.sleep(0.01)
    raise AssertionError(f"backend {pid} never waited")


# ---------------------------------------------------------------------------
# A run held before its commit
# ---------------------------------------------------------------------------
def pausing_tree(profile: str, tmp_path) -> pathlib.Path:
    """A copy of this tree in *profile* whose ``env.py`` pauses before the
    commit when :data:`PAUSE_VARIABLE` is set."""
    destination = pathlib.Path(tmp_path) / f"lock-capture-{uuid.uuid4().hex[:8]}"
    if profile == FULL:
        tree = tree_profiles.full_tree(destination)
    else:
        tree = tree_profiles.core_tree(destination)
    env_py = tree.joinpath(*_ENV_PY)
    source = env_py.read_text(encoding="utf-8")
    assert source.count(_ANCHOR) == 1, "env.py's transaction block moved"
    env_py.write_text(source.replace(_ANCHOR, _ANCHOR + _PAUSE), encoding="utf-8")
    return tree


def plant_after_upgrade(tree: pathlib.Path, revision: str, body: str) -> None:
    """Make *revision*'s ``upgrade()`` in the copy *tree* run *body* after its
    own steps: how a test plants a defect. *body* may use ``_op`` (alembic's
    ``op``) and ``_schema`` (the run's schema)."""
    pattern = re.compile(rf"""(?m)^revision(?::\s*str)?\s*=\s*['"]{revision}['"]""")
    found = [
        path
        for package in (tree / "backend", tree / "modules" / "backend")
        if package.joinpath(*_VERSIONS).is_dir()
        for path in sorted(package.joinpath(*_VERSIONS).glob("*.py"))
        if pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert len(found) == 1, f"{revision}: {found}"
    indented = "".join(f"    {line}\n" for line in body.strip("\n").splitlines())
    with found[0].open("a", encoding="utf-8") as revision_file:
        revision_file.write(
            "\n\n# Planted by a test (lock_capture.py) in a copy of the tree.\n"
            "_lc_original_upgrade = upgrade\n\n\n"
            "def upgrade():\n"
            "    _lc_original_upgrade()\n"
            "    import os as _os\n\n"
            "    from alembic import op as _op\n\n"
            '    _schema = _os.environ["POSTGRES_SCHEMA"]\n' + indented
        )


@dataclasses.dataclass
class Held:
    """A run held before its commit."""

    #: The backend the run's transaction is on.
    pid: int
    process: subprocess.Popen
    #: Its stdout and stderr together.
    log: pathlib.Path

    def output(self) -> str:
        return self.log.read_text(encoding="utf-8", errors="replace")


@contextlib.contextmanager
def held_before_commit(
    tree: pathlib.Path, schema: str, *alembic_args: str
) -> Iterator[Held]:
    """``alembic <alembic_args>`` from *tree*, a :func:`pausing_tree`, against
    *schema*, in another process. Yields once the run is held before its
    commit; on the way out it is resumed and waited for (the caller reads
    ``process.returncode``). A run that ends without pausing fails here with
    its output."""
    handshake = tree.parent / f"handshake-{uuid.uuid4().hex[:8]}"
    handshake.mkdir()
    log = handshake / "run.log"
    argv = ["-m", "alembic", "-c", str(tree_profiles.alembic_ini(tree)), *alembic_args]
    environment = tree_profiles._environment(
        tree, schema, {PAUSE_VARIABLE: str(handshake)}
    )
    with open(log, "wb") as out:
        process = subprocess.Popen(
            [sys.executable, *argv],
            cwd=str(tree),
            env=environment,
            stdout=out,
            stderr=subprocess.STDOUT,
        )
    paused = handshake / "paused"
    try:
        deadline = time.monotonic() + PAUSE_SECONDS
        while not paused.exists() and process.poll() is None:
            assert time.monotonic() < deadline, "the run never paused"
            time.sleep(0.02)
        assert paused.exists(), (
            f"the run ended without pausing (exit {process.returncode}):\n"
            + log.read_text(encoding="utf-8", errors="replace")[-3000:]
        )
        yield Held(int(paused.read_text()), process, log)
    finally:
        (handshake / "resume").write_text("go")
        try:
            process.wait(timeout=PAUSE_SECONDS)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


@dataclasses.dataclass(frozen=True)
class Capture:
    """What a held run's backend holds."""

    locks: Locks
    #: Relation locks on relations the run created (not counted in ``locks``).
    unseen: int

    def __str__(self) -> str:
        return f"{describe(self.locks)} (+{self.unseen} on relations this run created)"


def capture(engine, held: Held, schema: str) -> Capture:
    """Read what *held*'s backend holds on *schema*, refusing a backend that is
    not inside a transaction that has written. Every run writes
    ``alembic_version`` before its commit; after the commit the backend is
    idle, or in a new transaction that has written nothing (the planted
    pause's own query opens one), and holds none of the run's locks."""
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        row = conn.execute(
            text(
                "SELECT state, backend_xid::text FROM pg_stat_activity WHERE pid = :pid"
            ),
            {"pid": held.pid},
        ).first()
        state, xid = row if row is not None else (None, None)
        assert state == "idle in transaction" and xid is not None, (
            f"backend {held.pid} is {state!r} with xid {xid!r}, not held inside "
            "its transaction: a capture read after the commit is empty"
        )
        locks = locks_held(conn, schema, held.pid)
        unseen = conn.execute(text(_UNSEEN), {"pid": held.pid}).scalar()
    return Capture(locks, unseen)


def assert_locks(captured: Capture, declared: Mapping[str, set]) -> None:
    """The tables locked, with their modes, are exactly *declared* plus
    alembic's :data:`BOOKKEEPING` (which *declared* may restate)."""
    expected = {**BOOKKEEPING, **{t: frozenset(m) for t, m in declared.items()}}
    assert captured.locks == expected, (
        f"locked {describe(captured.locks)} != declared {describe(expected)}"
    )
