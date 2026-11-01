"""``1ab99332f0ba``: stored ``events.created_at`` values rewritten to UTC (#579).

``events.created_at`` is a VARCHAR, so an event stored with the client's
offset, ``Z`` or no offset sorts and filters as text, not as an instant.  The
revision rewrites every such value to the canonical UTC string, in batches,
inside ``autocommit_block()``, and leaves a value it cannot read as stored.

A schema built by ``create_all`` and rewound to ``a89544fb1075`` is exactly the
database this revision meets (it changes no DDL); the tests plant rows with
raw SQL, which skips the model's write-time normaliser just as the previous
release's writes did, and run the documented ``alembic upgrade heads`` or
``python -m backend.app.db.bootstrap`` in a subprocess, as a deployment does.
The gates, by the plan's numbers (``launch-readiness/events-utc-579``):

* 1   every rewritten value is byte-identical to ``normalize_event_timestamp``
      over 5,000 random values;
* 2   a value with no offset is read as UTC whatever the session TimeZone is;
* 3   values that are not instants, out-of-range dates included, are left as
      stored and counted, and the run still finishes and is recorded;
* 4   seven fraction digits are truncated (the corpus);
* 5   exact counts: the log's N, canonical rows byte-unchanged;
* 6   batches of 3 and of 1 cover every row exactly once;
* 7   a second run rewrites nothing;
* 8   a concurrent INSERT and a DELETE of a rewritten row are not blocked;
* 8b  from the previous release (``271f03a31742``), through the bootstrap and
      raw alembic: while the rewrite runs, ``a89544fb1075`` is recorded and no
      lock on ``feature_flags`` or ``users`` is held (a planted pause, no timing);
* 9   the readers -- event windows and order, the export count, the first
      conversion and its day -- are wrong before and right after;
* 13-15 the profile transitions;
* 17  the downgrade changes no data, and the re-run recipe (``downgrade
      a89544fb1075`` then ``upgrade heads``) rewrites a value written in
      between while the modules branch keeps its revision.  Since
      ``806901fb7735`` sits above this revision the recipe also runs its
      downgrade, which drops every experiment's stored correction settings
      (``upgrade heads`` restores the defaults, not the choices): the
      migrations page says so, and the recipe test pins it;
* 18  the rows stay ``str`` in a VARCHAR column, as the previous release reads;
* a failure at batch k leaves k batches rewritten and a re-run finishes;
* the documented SQL selects exactly the migration's candidates;
* offline mode is refused.

10-12 (autogenerate, head set, apply order) are the existing pins, moved to
this revision.  The in-process gates run the revision's ``upgrade()`` on a
connection of their own, which is closed afterwards.
"""

from __future__ import annotations

import math
import pathlib
import subprocess
import sys
import time
import uuid

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect, text

from backend.app.db import bootstrap
from backend.app.models.event import normalize_event_timestamp
from backend.app.services.event_matching import (
    count_converting_users,
    first_conversion_times,
)
from backend.app.services.event_service import EventService
from backend.app.services.export_service import ExportService
from backend.tests.integration.database import tree_profiles
from backend.tests.integration.database.tree_profiles import CORE, FULL
from backend.tests.unit.db.test_events_created_at_utc_normaliser import (
    CORPUS,
    NOT_INSTANTS,
    OUT_OF_RANGE,
    _random_values,
    load_revision,
)
from backend.tests.unit.docs.test_events_utc_runbook import COUNT, LEFT, sql_blocks

pytestmark = [pytest.mark.integration]

REVISION = "1ab99332f0ba"
#: The core revision it extends: the recipe's downgrade target.
PARENT = "a89544fb1075"
#: The core head of this tree, which ``upgrade heads`` runs on to past this
#: revision, through ``806901fb7735`` (#580), ``d29a479daafe`` (#445),
#: ``37dcb2969766`` (#440) and ``4c2c09a2b3e8`` (#1206): ``607c51a59bf5``
#: (#1497).
CORE_HEAD = "607c51a59bf5"
#: The core head of the previous release (0.16.2), with ``d12cbd384bbe`` and
#: ``a89544fb1075`` pending in front of this revision.
RELEASED = "271f03a31742"
MODULES_HEAD = "modules_0003_lower_is_better"
WAREHOUSE_TABLES = {
    "warehouse_connections",
    "warehouse_sources",
    "warehouse_analysis_runs",
}

ROWS = {CORE: {CORE_HEAD}, FULL: {CORE_HEAD, MODULES_HEAD}}
PARENT_ROWS = {CORE: {PARENT}, FULL: {PARENT, MODULES_HEAD}}
RELEASED_ROWS = {CORE: {RELEASED}, FULL: {RELEASED, MODULES_HEAD}}

BOTH_PROFILES = [CORE, pytest.param(FULL, marks=pytest.mark.modules)]

#: The rewrite's own lines: the log line and the hook the tests plant into.
_HOOK_END = '    database part way through a run.\n    """\n'


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def scratch_schema(test_db):
    schema = f"eventsutc_{uuid.uuid4().hex[:8]}"
    try:
        yield schema
    finally:
        with test_db.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


def _rows(engine, schema: str) -> set[str]:
    return bootstrap.recorded_revisions(engine, schema)


def _set_rows(conn, schema: str, rows: set[str]) -> None:
    conn.execute(text(f'DELETE FROM "{schema}".alembic_version'))
    for revision in sorted(rows):
        conn.execute(
            text(f'INSERT INTO "{schema}".alembic_version VALUES (:rev)'),
            {"rev": revision},
        )


def _bootstrapped(profile: str, schema: str, tmp_path) -> pathlib.Path:
    tree = tree_profiles.tree_for(profile, tmp_path)
    created = tree_profiles.bootstrap_schema(tree, schema)
    assert created.returncode == 0, created.stderr[-3000:]
    return tree


def _at_parent(test_db, profile: str, schema: str, tmp_path) -> pathlib.Path:
    """A database at ``a89544fb1075``: this revision pending, nothing else."""
    tree = _bootstrapped(profile, schema, tmp_path)
    with test_db.begin() as conn:
        _set_rows(conn, schema, PARENT_ROWS[profile])
    return tree


def _at_released(test_db, profile: str, schema: str, tmp_path) -> pathlib.Path:
    """What 0.16.2 left: ``d12cbd384bbe`` and ``a89544fb1075`` pending too."""
    tree = _bootstrapped(profile, schema, tmp_path)
    with test_db.begin() as conn:
        conn.execute(text(f'DROP INDEX "{schema}".ix_users_email_lower'))
        conn.execute(
            text(f'ALTER TABLE "{schema}".feature_flags DROP COLUMN default_value')
        )
        _set_rows(conn, schema, RELEASED_ROWS[profile])
    return tree


def _insert(engine, schema: str, values: list[str], **columns) -> list[str]:
    """One event per value, written as stored text; returns their ids in order."""
    ids = [str(uuid.uuid4()) for _ in values]
    names = ["id", "event_type", "user_id", "created_at", *columns]
    rows = [
        {
            "id": event_id,
            "event_type": columns.get("event_type", "custom"),
            "user_id": f"user-{event_id[:8]}",
            "created_at": value,
            **{k: v for k, v in columns.items() if k != "event_type"},
        }
        for event_id, value in zip(ids, values)
    ]
    names = list(dict.fromkeys(names))
    with engine.begin() as conn:
        conn.execute(
            text(
                f'INSERT INTO "{schema}".events ({", ".join(names)}, updated_at) '
                f"VALUES ({', '.join(':' + n for n in names)}, now())"
            ),
            rows,
        )
    return ids


def _stored(engine, schema: str) -> dict[str, str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(f'SELECT id::text, created_at FROM "{schema}".events')
        ).all()
    return dict(rows)


def _log_lines(output: str) -> list[str]:
    return [line for line in output.splitlines() if line.startswith(REVISION + ": ")]


def _log(output: str) -> str:
    (line,) = _log_lines(output)
    return line


def _counted(line: str) -> tuple[int, int]:
    """(rewritten, unreadable) from the log line."""
    head = line.split("; ")
    rewritten = int(head[0].split("rewrote ")[1].split(" ")[0])
    unreadable = int(head[1].split(" ")[0])
    return rewritten, unreadable


def _upgrade(tree, schema: str, **extra_env) -> subprocess.CompletedProcess:
    return tree_profiles.alembic(
        tree, schema, "upgrade", "heads", extra_env=extra_env or None
    )


def _planted_tree(
    profile: str, tmp_path, replacements: list[tuple[str, str]]
) -> pathlib.Path:
    """A copy of this tree whose revision file has *replacements* applied.

    How a test plants a pause, a failure or a defect into the run a subprocess
    performs: the checkout's own file is never edited.
    """
    destination = pathlib.Path(tmp_path) / f"planted-{uuid.uuid4().hex[:6]}"
    if profile == FULL:
        tree = tree_profiles.full_tree(destination)
    else:
        tree = tree_profiles.core_tree(destination)
    versions = tree / "backend" / "app" / "db" / "migrations" / "versions"
    (path,) = versions.glob(f"{REVISION}_*.py")
    source = path.read_text(encoding="utf-8")
    for old, new in replacements:
        assert source.count(old) == 1, f"planted text not found once: {old!r}"
        source = source.replace(old, new)
    path.write_text(source, encoding="utf-8")
    return tree


def _hook(body: str) -> tuple[str, str]:
    """A replacement planting *body* into ``_batch_done``."""
    indented = "".join(f"    {line}\n" for line in body.strip("\n").splitlines())
    return (_HOOK_END, _HOOK_END + indented)


def _in_process(test_db, schema: str, **attributes):
    """Run ``upgrade()`` on a fresh connection; returns the module."""
    revision = load_revision()
    revision._SCHEMA = schema
    for name, value in attributes.items():
        setattr(revision, name, value)
    with test_db.connect() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            revision.upgrade()
    return revision


def _canonical(value: str) -> bool:
    try:
        return normalize_event_timestamp(value) == value
    except (ValueError, OverflowError):
        return False


# ---------------------------------------------------------------------------
# 13. The previous release's rows, through `upgrade heads`, in each profile
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_the_previous_releases_rows_are_rewritten_by_upgrade_heads(
    profile, test_db, scratch_schema, tmp_path
):
    """Every corpus shape, from 0.16.2 (three revisions pending), by hand."""
    tree = _at_released(test_db, profile, scratch_schema, tmp_path)
    ids = _insert(test_db, scratch_schema, [stored for stored, _, _ in CORPUS])

    upgrade = _upgrade(tree, scratch_schema)

    assert upgrade.returncode == 0, upgrade.stderr[-3000:]
    assert _rows(test_db, scratch_schema) == ROWS[profile]
    stored = _stored(test_db, scratch_schema)
    for event_id, (before, after, _) in zip(ids, CORPUS):
        expected = before if after is None else after
        assert stored[event_id] == expected, (before, stored[event_id])
    line = _log(upgrade.stdout)
    print(line)
    unreadable_ids = [
        i for i, (_, after, candidate) in zip(ids, CORPUS) if candidate and not after
    ]
    rewritten = sum(
        1 for stored_value, after, _ in CORPUS if after and after != stored_value
    )
    assert _counted(line) == (rewritten, len(unreadable_ids)) == (13, 4)
    for event_id in unreadable_ids:
        assert event_id in line


# ---------------------------------------------------------------------------
# 1. Byte-identical to the normaliser, over 5,000 random values
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_every_rewritten_value_is_what_the_normaliser_writes(
    test_db, scratch_schema, tmp_path
):
    tree = _at_parent(test_db, CORE, scratch_schema, tmp_path)
    values = _random_values(seed=1579, count=5000)
    ids = _insert(test_db, scratch_schema, values)

    upgrade = _upgrade(tree, scratch_schema)

    assert upgrade.returncode == 0, upgrade.stderr[-3000:]
    stored = _stored(test_db, scratch_schema)
    wrong = [
        (value, stored[event_id], normalize_event_timestamp(value))
        for event_id, value in zip(ids, values)
        if stored[event_id] != normalize_event_timestamp(value)
    ]
    print(f"{len(wrong)} of {len(values)} differ")
    assert wrong == [], wrong[:5]
    changed = sum(1 for v in values if normalize_event_timestamp(v) != v)
    assert changed > 4000, "the sample is mostly canonical: it proves little"
    assert _counted(_log(upgrade.stdout)) == (changed, 0)


# ---------------------------------------------------------------------------
# 2. No offset means UTC, whatever the session's TimeZone
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_a_value_with_no_offset_is_read_as_utc_in_any_session_timezone(
    test_db, scratch_schema, tmp_path, capsys
):
    _at_parent(test_db, CORE, scratch_schema, tmp_path)
    (naive,) = _insert(test_db, scratch_schema, ["2026-10-02T00:00:00"])
    revision = load_revision()
    revision._SCHEMA = scratch_schema
    with test_db.connect() as conn:
        conn.execute(text("SET TIME ZONE 'America/Los_Angeles'"))
        conn.commit()
        with Operations.context(MigrationContext.configure(conn)):
            revision.upgrade()
        # The session the rewrite ran in was in Los Angeles throughout.
        assert conn.execute(text("SHOW TimeZone")).scalar() == "America/Los_Angeles"
        conn.commit()
    capsys.readouterr()

    assert _stored(test_db, scratch_schema)[naive] == "2026-10-02T00:00:00+00:00"


# ---------------------------------------------------------------------------
# 3. Values that are not instants: left as stored, counted, never refused
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_values_that_cannot_be_read_are_left_as_stored_and_counted(
    test_db, scratch_schema, tmp_path
):
    """Out-of-range dates raise OverflowError, not ValueError; caught too.

    The stored text is client input and the log is shared, so the output must
    name ids and no value: the planted values carry a token to look for.
    """
    tree = _at_parent(test_db, CORE, scratch_schema, tmp_path)
    token = uuid.uuid4().hex[:10]
    unreadable = [*NOT_INSTANTS, *OUT_OF_RANGE, "", f"not a time {token}"]
    ids = _insert(test_db, scratch_schema, unreadable)
    (offset,) = _insert(test_db, scratch_schema, ["2026-10-01T01:00:00+02:00"])

    upgrade = _upgrade(tree, scratch_schema)

    assert upgrade.returncode == 0, upgrade.stderr[-3000:]
    assert _rows(test_db, scratch_schema) == ROWS[CORE]
    stored = _stored(test_db, scratch_schema)
    assert [stored[i] for i in ids] == unreadable
    assert stored[offset] == "2026-09-30T23:00:00+00:00"
    line = _log(upgrade.stdout)
    assert _counted(line) == (1, len(unreadable))
    assert all(event_id in line for event_id in ids)
    output = upgrade.stdout + upgrade.stderr
    assert token not in output
    assert "0001-01-01" not in output and "9999-12-31" not in output


def test_more_than_twenty_unreadable_values_name_twenty_ids_and_a_count(
    test_db, scratch_schema, tmp_path
):
    tree = _at_parent(test_db, CORE, scratch_schema, tmp_path)
    ids = _insert(test_db, scratch_schema, [f"bad {n}" for n in range(23)])

    upgrade = _upgrade(tree, scratch_schema)

    assert upgrade.returncode == 0, upgrade.stderr[-3000:]
    line = _log(upgrade.stdout)
    assert _counted(line) == (0, 23)
    assert sum(1 for i in ids if i in line) == 20
    assert " and 3 more)" in line


# ---------------------------------------------------------------------------
# 5. Exact counts; canonical rows byte-unchanged
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_the_log_counts_exactly_the_rewritten_rows(test_db, scratch_schema, tmp_path):
    tree = _at_parent(test_db, CORE, scratch_schema, tmp_path)
    offsets = [f"2026-10-01T0{h}:00:00+05:30" for h in range(4)]
    naive = [f"2026-10-01T1{h}:00:00" for h in range(3)]
    canonical = [
        "2026-10-01T20:00:00+00:00",
        "2026-10-01T20:00:00.000001+00:00",
        "2026-10-01T20:00:00.999999+00:00",
        "2026-10-01T21:30:00.250000+00:00",
    ]
    _insert(test_db, scratch_schema, offsets + naive)
    kept = _insert(test_db, scratch_schema, canonical)
    with test_db.connect() as conn:
        xmin_before = dict(
            conn.execute(
                text(
                    f'SELECT id::text, xmin::text FROM "{scratch_schema}".events '
                    "WHERE id = ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": kept},
            ).all()
        )

    upgrade = _upgrade(tree, scratch_schema)

    assert upgrade.returncode == 0, upgrade.stderr[-3000:]
    assert _counted(_log(upgrade.stdout)) == (len(offsets) + len(naive), 0)
    stored = _stored(test_db, scratch_schema)
    assert len(stored) == len(offsets) + len(naive) + len(canonical)
    assert [stored[i] for i in kept] == canonical
    with test_db.connect() as conn:
        xmin_after = dict(
            conn.execute(
                text(
                    f'SELECT id::text, xmin::text FROM "{scratch_schema}".events '
                    "WHERE id = ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": kept},
            ).all()
        )
    # Not even rewritten to the same text: no new row version.
    assert xmin_after == xmin_before


# ---------------------------------------------------------------------------
# 6. Batches cover every row exactly once
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("batch_size", [3, 1])
def test_batches_cover_every_row_exactly_once(
    batch_size, test_db, scratch_schema, tmp_path, capsys
):
    _at_parent(test_db, CORE, scratch_schema, tmp_path)
    ids = _insert(
        test_db, scratch_schema, [f"2026-10-01T0{n}:00:00+02:00" for n in range(7)]
    )
    batches = []

    def _count_batches(number):
        batches.append(number)
        # A guard, not a timing: a loop that re-reads the same rows never ends.
        assert number < 50, "the batch loop does not advance"

    _in_process(
        test_db, scratch_schema, _BATCH_SIZE=batch_size, _batch_done=_count_batches
    )

    assert batches == list(range(1, math.ceil(7 / batch_size) + 1))
    stored = _stored(test_db, scratch_schema)
    assert all(_canonical(stored[i]) for i in ids)
    assert _counted(_log(capsys.readouterr().out)) == (7, 0)


# ---------------------------------------------------------------------------
# 7 and 17. A second run, the downgrade, and the re-run recipe
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_a_second_run_rewrites_nothing(test_db, scratch_schema, tmp_path):
    tree = _at_parent(test_db, CORE, scratch_schema, tmp_path)
    _insert(test_db, scratch_schema, [stored for stored, _, _ in CORPUS])
    first = _upgrade(tree, scratch_schema)
    assert first.returncode == 0, first.stderr[-3000:]
    after_first = _stored(test_db, scratch_schema)

    down = tree_profiles.alembic(tree, scratch_schema, "downgrade", PARENT)
    assert down.returncode == 0, down.stderr[-3000:]
    second = _upgrade(tree, scratch_schema)

    assert second.returncode == 0, second.stderr[-3000:]
    assert _counted(_log(second.stdout)) == (0, 4)
    assert _stored(test_db, scratch_schema) == after_first


@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_the_downgrade_changes_no_data_and_says_why(
    profile, test_db, scratch_schema, tmp_path
):
    tree = _at_parent(test_db, profile, scratch_schema, tmp_path)
    _insert(test_db, scratch_schema, ["2026-10-01T01:00:00+02:00"])
    assert _upgrade(tree, scratch_schema).returncode == 0
    upgraded = _stored(test_db, scratch_schema)

    down = tree_profiles.alembic(tree, scratch_schema, "downgrade", PARENT)

    assert down.returncode == 0, down.stderr[-3000:]
    assert _rows(test_db, scratch_schema) == PARENT_ROWS[profile]
    assert _stored(test_db, scratch_schema) == upgraded
    assert list(upgraded.values()) == ["2026-09-30T23:00:00+00:00"]
    assert "the original offsets cannot be restored" in (
        load_revision().downgrade.__doc__
    )


@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_the_rerun_recipe_rewrites_a_value_written_after_the_first_run(
    profile, test_db, scratch_schema, tmp_path
):
    """The documented recipe, exactly: ``downgrade a89544fb1075``, then
    ``upgrade heads``.  On a full install the modules branch keeps its revision
    and its tables (``-1`` can step that branch back instead).

    It also crosses ``806901fb7735``, whose downgrade drops the experiments'
    stored correction settings: a stored ``none`` at 0.90 comes back as the
    defaults.  That is why the migrations page offers the recipe only while
    ``1ab99332f0ba`` is the core head."""
    tree = _at_parent(test_db, profile, scratch_schema, tmp_path)
    _insert(test_db, scratch_schema, ["2026-10-01T01:00:00+02:00"])
    assert _upgrade(tree, scratch_schema).returncode == 0
    tables = set(inspect(test_db).get_table_names(schema=scratch_schema))
    experiment = uuid.uuid4()
    with test_db.begin() as conn:
        conn.execute(
            text(
                f'INSERT INTO "{scratch_schema}".experiments (id, name, status, '
                "experiment_type, optimization_type, sequential_testing_enabled, "
                "bayesian_enabled, correction_method, confidence_level, "
                "created_at, updated_at) VALUES (:id, 'recipe', 'ACTIVE', 'A_B', "
                "'fixed', false, false, 'none', 0.90, now(), now())"
            ),
            {"id": experiment},
        )
    # Written by the previous release after the first scan.
    (straggler,) = _insert(test_db, scratch_schema, ["2026-10-01T09:00:00+02:00"])

    down = tree_profiles.alembic(tree, scratch_schema, "downgrade", PARENT)
    assert down.returncode == 0, down.stderr[-3000:]
    assert _rows(test_db, scratch_schema) == PARENT_ROWS[profile]
    again = _upgrade(tree, scratch_schema)

    assert again.returncode == 0, again.stderr[-3000:]
    assert _rows(test_db, scratch_schema) == ROWS[profile]
    assert _counted(_log(again.stdout)) == (1, 0)
    assert _stored(test_db, scratch_schema)[straggler] == "2026-10-01T07:00:00+00:00"
    after = set(inspect(test_db).get_table_names(schema=scratch_schema))
    assert after == tables
    with test_db.connect() as conn:
        settings = conn.execute(
            text(
                "SELECT correction_method, confidence_level FROM "
                f'"{scratch_schema}".experiments WHERE id = :id'
            ),
            {"id": experiment},
        ).one()
    assert tuple(settings) == ("benjamini_hochberg", 0.95)
    if profile == FULL:
        assert WAREHOUSE_TABLES <= after


# ---------------------------------------------------------------------------
# 8. Writers are not blocked while the rewrite runs
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_an_insert_and_a_delete_of_a_rewritten_row_are_not_blocked(
    test_db, scratch_schema, tmp_path, capsys
):
    """At the end of the first batch, from another session with a 2 s
    ``lock_timeout``: a tracking INSERT, and the retention DELETE of a row the
    first batch has just rewritten.  A rewrite holding its row locks until the
    end of the run (one transaction) fails the DELETE with a lock timeout."""
    _at_parent(test_db, CORE, scratch_schema, tmp_path)
    ids = _insert(
        test_db, scratch_schema, [f"2026-10-01T0{n}:00:00+02:00" for n in range(6)]
    )
    probes = []

    def _write_from_another_session(number):
        if number != 1:
            return
        with test_db.connect() as other:
            other.execute(text("SET lock_timeout = '2s'"))
            first = other.execute(
                text(
                    f'SELECT id::text FROM "{scratch_schema}".events '
                    "WHERE id = ANY(CAST(:ids AS uuid[])) AND created_at LIKE "
                    "'%+00:00' ORDER BY id LIMIT 1"
                ),
                {"ids": ids},
            ).scalar_one()
            other.execute(
                text(f'DELETE FROM "{scratch_schema}".events WHERE id = :id'),
                {"id": first},
            )
            other.execute(
                text(
                    f'INSERT INTO "{scratch_schema}".events '
                    "(id, event_type, user_id, created_at, updated_at) VALUES "
                    "(gen_random_uuid(), 'custom', 'writer', "
                    "'2026-10-02T00:00:00+00:00', now())"
                )
            )
            other.commit()
            probes.append(first)

    _in_process(
        test_db, scratch_schema, _BATCH_SIZE=2, _batch_done=_write_from_another_session
    )

    assert len(probes) == 1
    stored = _stored(test_db, scratch_schema)
    assert probes[0] not in stored
    assert len(stored) == 6
    assert all(_canonical(v) for v in stored.values())
    # The deleted row was rewritten before it went; the other five after.
    assert _counted(_log(capsys.readouterr().out)) == (6, 0)


# ---------------------------------------------------------------------------
# 8b. From the previous release: no earlier revision's lock during the rewrite
# ---------------------------------------------------------------------------
def _held_locks(conn, schema: str) -> list[tuple]:
    return conn.execute(
        text(
            "SELECT c.relname, l.mode FROM pg_locks l "
            "JOIN pg_class c ON c.oid = l.relation "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = :s AND c.relname IN ('feature_flags', 'users') "
            "AND l.pid <> pg_backend_pid() ORDER BY 1, 2"
        ),
        {"s": schema},
    ).all()


@pytest.mark.regression
@pytest.mark.parametrize("command", ["bootstrap", "alembic"])
def test_no_earlier_revisions_lock_is_held_while_events_are_rewritten(
    command, test_db, scratch_schema, tmp_path
):
    """0.16.2 has ``d12cbd384bbe`` (``users`` SHARE) and ``a89544fb1075``
    (``feature_flags`` ACCESS EXCLUSIVE) pending in the same run.

    The run is paused by a planted hook after its first batch -- a file
    handshake, not a timing -- and a second session looks at what is recorded
    and what is locked.  Inside one transaction both locks would be held for
    the whole rewrite, blocking every flag evaluation of the release still
    serving.
    """
    paused = tmp_path / "paused"
    resume = tmp_path / "resume"
    tree = _planted_tree(
        CORE,
        tmp_path,
        [
            ("_BATCH_SIZE = 1000\n", "_BATCH_SIZE = 2\n"),
            _hook(
                "import pathlib, time\n"
                "if number == 1:\n"
                f"    pathlib.Path({str(paused)!r}).write_text('paused')\n"
                "    for _ in range(1800):\n"
                f"        if pathlib.Path({str(resume)!r}).exists():\n"
                "            break\n"
                "        time.sleep(0.1)\n"
            ),
        ],
    )
    created = tree_profiles.bootstrap_schema(tree, scratch_schema)
    assert created.returncode == 0, created.stderr[-3000:]
    with test_db.begin() as conn:
        conn.execute(text(f'DROP INDEX "{scratch_schema}".ix_users_email_lower'))
        conn.execute(
            text(
                f'ALTER TABLE "{scratch_schema}".feature_flags DROP COLUMN default_value'
            )
        )
        _set_rows(conn, scratch_schema, RELEASED_ROWS[CORE])
    ids = _insert(
        test_db, scratch_schema, [f"2026-10-01T0{n}:00:00+02:00" for n in range(5)]
    )

    argv = (
        ["-m", "backend.app.db.bootstrap"]
        if command == "bootstrap"
        else [
            "-m",
            "alembic",
            "-c",
            str(tree_profiles.alembic_ini(tree)),
            "upgrade",
            "heads",
        ]
    )
    log = tmp_path / "run.log"
    with open(log, "wb") as out:
        process = subprocess.Popen(
            [sys.executable, *argv],
            cwd=str(tree),
            env=tree_profiles._environment(tree, scratch_schema),
            stdout=out,
            stderr=subprocess.STDOUT,
        )
    try:
        for _ in range(1800):  # a guard, not a timing
            if paused.exists() or process.poll() is not None:
                break
            time.sleep(0.1)
        assert paused.exists(), log.read_text()[-3000:]
        with test_db.connect().execution_options(
            isolation_level="AUTOCOMMIT"
        ) as watcher:
            recorded = {
                row[0]
                for row in watcher.execute(
                    text(f'SELECT version_num FROM "{scratch_schema}".alembic_version')
                )
            }
            locks = _held_locks(watcher, scratch_schema)
            rewritten = watcher.execute(
                text(
                    f'SELECT count(*) FROM "{scratch_schema}".events '
                    "WHERE created_at LIKE '%+00:00'"
                )
            ).scalar()
    finally:
        resume.write_text("go")
        try:
            process.wait(timeout=600)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

    print(f"[{command}] while paused: recorded={recorded} locks={locks}")
    assert locks == [], f"held during the rewrite: {locks}"
    assert recorded == {PARENT}
    # The first batch is committed and visible to another session.
    assert rewritten == 2
    output = log.read_text()
    assert process.returncode == 0, output[-3000:]
    assert _rows(test_db, scratch_schema) == ROWS[CORE]
    assert all(_canonical(v) for v in _stored(test_db, scratch_schema).values())
    assert set(ids) == set(_stored(test_db, scratch_schema))


# ---------------------------------------------------------------------------
# The session is left as found, on every path
# ---------------------------------------------------------------------------
_SESSION = (
    "SELECT current_setting('lock_timeout'), current_setting('search_path'), "
    "to_regclass('pg_temp.event_created_at_rewrite') IS NULL"
)


@pytest.mark.regression
def test_a_failure_puts_the_settings_back_and_drops_the_work_table(
    test_db, scratch_schema, tmp_path
):
    """A later revision in the same run must not inherit a 30 s
    ``lock_timeout`` or this schema as its ``search_path``, whatever ended the
    rewrite."""
    _at_parent(test_db, CORE, scratch_schema, tmp_path)
    _insert(test_db, scratch_schema, ["2026-10-01T01:00:00+02:00"] * 3)
    revision = load_revision()
    revision._SCHEMA = scratch_schema
    revision._BATCH_SIZE = 1

    def _stop(number):
        if number == 2:
            raise RuntimeError("planted stop")

    revision._batch_done = _stop
    with test_db.connect() as conn:
        found = tuple(conn.execute(text(_SESSION)).one())
        conn.commit()
        with Operations.context(MigrationContext.configure(conn)):
            with pytest.raises(RuntimeError, match="planted stop"):
                revision.upgrade()
        left = tuple(conn.execute(text(_SESSION)).one())
        conn.commit()

    assert found[2] is True
    assert left == found


@pytest.mark.regression
def test_a_work_table_left_in_the_session_is_replaced(
    test_db, scratch_schema, tmp_path, capsys
):
    """A run that died in this session before its ``finally`` (or anything
    else) can leave the temporary table; the next run drops it first."""
    _at_parent(test_db, CORE, scratch_schema, tmp_path)
    (offset,) = _insert(test_db, scratch_schema, ["2026-10-01T01:00:00+02:00"])
    revision = load_revision()
    revision._SCHEMA = scratch_schema
    with test_db.connect() as conn:
        conn.execute(
            text("CREATE TEMPORARY TABLE event_created_at_rewrite (seq int, id uuid)")
        )
        conn.commit()
        with Operations.context(MigrationContext.configure(conn)):
            revision.upgrade()

    assert _counted(_log(capsys.readouterr().out)) == (1, 0)
    assert _stored(test_db, scratch_schema)[offset] == "2026-09-30T23:00:00+00:00"


# ---------------------------------------------------------------------------
# A failure part way: the finished batches stay, and a re-run finishes
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_a_failure_at_batch_k_keeps_k_batches_and_a_rerun_finishes(
    test_db, scratch_schema, tmp_path
):
    tree = _at_parent(test_db, CORE, scratch_schema, tmp_path)
    values = [f"2026-10-01T0{n}:00:00+02:00" for n in range(7)]
    _insert(test_db, scratch_schema, values)
    failing = _planted_tree(
        CORE,
        tmp_path,
        [
            ("_BATCH_SIZE = 1000\n", "_BATCH_SIZE = 2\n"),
            _hook("if number == 2:\n    raise RuntimeError('planted stop at batch 2')"),
        ],
    )

    stopped = _upgrade(failing, scratch_schema)

    assert stopped.returncode != 0
    assert "planted stop at batch 2" in stopped.stderr
    assert _rows(test_db, scratch_schema) == PARENT_ROWS[CORE]
    stored = _stored(test_db, scratch_schema)
    assert sum(1 for v in stored.values() if v.endswith("+00:00")) == 4

    rerun = _upgrade(tree, scratch_schema)

    assert rerun.returncode == 0, rerun.stderr[-3000:]
    assert _rows(test_db, scratch_schema) == ROWS[CORE]
    assert _counted(_log(rerun.stdout)) == (3, 0)
    assert all(_canonical(v) for v in _stored(test_db, scratch_schema).values())


# ---------------------------------------------------------------------------
# 9 (regression). The readers, wrong before the rewrite and right after
# ---------------------------------------------------------------------------
#: QA's seven rows, moved to a date no other test writes.  The instants, by
#: hand: 1 is 06-15 07:30Z, 2 is 06-14 23:30Z, 3 is 06-15 00:00Z (no offset),
#: 4-7 are 06-15 03:00:00, 03:00:00.25, 03:00:01 and 03:00:02 UTC.
READER_ROWS = [
    "2011-06-14T23:30:00-08:00",
    "2011-06-15T05:00:00+05:30",
    "2011-06-15T00:00:00",
    "2011-06-15T03:00:00+00:00",
    "2011-06-15T03:00:00.250000+00:00",
    "2011-06-15T03:00:01",
    "2011-06-15T03:00:02Z",
]
TRUE_ORDER = [2, 3, 4, 5, 6, 7, 1]
DAY_START = "2011-06-15T00:00:00Z"
DAY_END = "2011-06-15T23:59:59Z"


@pytest.mark.regression
def test_existing_offset_rows_sort_and_window_in_utc_after_upgrade(
    test_db, db_session, make_experiment, make_variant, make_assignment, capsys
):
    """The reproduction, through the readers, before and after the rewrite.

    Run in the test schema itself, where the services read.  The "before"
    assertions are the defect: if they stop holding, the fixture no longer
    reproduces it and the "after" ones prove nothing.
    """
    from datetime import datetime, timezone

    experiment = make_experiment(name=f"utc-579-{uuid.uuid4().hex[:8]}")
    variant = make_variant(experiment)
    schema = db_session.execute(text("SELECT current_schema()")).scalar()
    db_session.commit()
    common = {
        "experiment_id": str(experiment.id),
        "variant_id": str(variant.id),
        "event_type": "conversion",
        "event_name": "purchase",
    }
    ids = _insert(test_db, schema, READER_ROWS, **common)
    number = dict(zip(ids, range(1, len(ids) + 1)))
    # User A converted at rows 1 and 2; B only at row 2's instant.
    user_a, user_b = f"a-{ids[0][:8]}", f"b-{ids[0][:8]}"
    with test_db.begin() as conn:
        conn.execute(
            text(
                f'UPDATE "{schema}".events SET user_id = :u WHERE id = ANY(CAST(:ids AS uuid[]))'
            ),
            {"u": user_a, "ids": ids[:2]},
        )
    (b_event,) = _insert(test_db, schema, [READER_ROWS[1]], **common)
    with test_db.begin() as conn:
        conn.execute(
            text(f'UPDATE "{schema}".events SET user_id = :u WHERE id = :id'),
            {"u": user_b, "id": b_event},
        )
    for user in (user_a, user_b):
        make_assignment(experiment, variant, user)
    service = EventService(db_session)
    export = ExportService(db_session)
    window = (
        datetime(2011, 6, 15, tzinfo=timezone.utc),
        datetime(2011, 6, 15, 23, 59, 59, tzinfo=timezone.utc),
    )

    def _readers():
        db_session.expire_all()
        listed = service.get_events_by_experiment(
            experiment.id, start_date=DAY_START, end_date=DAY_END, limit=50
        )
        everything = service.get_events_by_experiment(experiment.id, limit=50)
        firsts = sorted(
            first_conversion_times(db_session, experiment.id, variant.id, "purchase")
        )
        return {
            "window": {number[e["id"]] for e in listed if e["id"] in number},
            "count": service.count_events_by_experiment(
                experiment.id, start_date=DAY_START, end_date=DAY_END
            ),
            "newest_first": [number[e["id"]] for e in everything if e["id"] in number],
            "export": export._count_events(*window),
            "firsts": firsts,
            "first_days": sorted(first[:10] for first in firsts),
            "converting": count_converting_users(
                db_session, experiment.id, variant.id, "purchase"
            ),
        }

    before = _readers()
    # The defect, as text compares it: row 2 (truly 06-14) is in the window,
    # row 1 (truly 06-15) and row 3 (midnight, no offset) are not.
    assert before["window"] == {2, 4, 5, 6, 7}
    assert before["count"] == 6  # 5 of the seven, plus B's copy of row 2
    assert before["newest_first"][0] == 2 and before["newest_first"][-1] == 1
    assert before["export"] == 6
    assert READER_ROWS[0] in before["firsts"]  # A's "first" is row 1, 06-15 07:30Z
    assert before["first_days"] == ["2011-06-14", "2011-06-15"]

    revision = load_revision()
    revision._SCHEMA = schema
    with test_db.connect() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            revision.upgrade()
    capsys.readouterr()

    after = _readers()
    assert after["window"] == {1, 3, 4, 5, 6, 7}
    assert after["count"] == 6
    assert after["newest_first"] == list(reversed(TRUE_ORDER))
    assert after["export"] == 6
    # Both users first converted at row 2's instant, 06-14 23:30 UTC: B's day
    # moves by one, from 06-15 to 06-14; the converting-user total does not.
    assert after["firsts"] == ["2011-06-14T23:30:00+00:00"] * 2
    assert after["first_days"] == ["2011-06-14", "2011-06-14"]
    assert after["converting"] == before["converting"] == 2


# ---------------------------------------------------------------------------
# 18. What the previous release reads: still text, in a VARCHAR column
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_autogenerate_after_the_upgrade_is_empty(
    profile, test_db, scratch_schema, tmp_path
):
    """10, on a database this revision actually ran on.

    ``test_autogenerate_is_empty.py`` starts from ``create_all`` and a stamp,
    so it never runs this revision and cannot see stray DDL in it; this does.
    It says nothing about the data (1, 5 and 9 do).
    """
    tree = _at_released(test_db, profile, scratch_schema, tmp_path)
    _insert(test_db, scratch_schema, ["2026-10-01T01:00:00+02:00"])
    upgrade = _upgrade(tree, scratch_schema)
    assert upgrade.returncode == 0, upgrade.stderr[-3000:]

    result = tree_profiles.autogenerate(
        tree, scratch_schema, tmp_path, head=tree_profiles.CORE_HEAD
    )

    assert result.body is not None, result.describe()
    assert result.operations == [], result.describe()


def test_the_column_stays_text_for_the_previous_release(
    test_db, scratch_schema, tmp_path
):
    """The previous release slices ``created_at[:10]`` and compares it as a
    string; a type change (``timestamptz``) would hand it a ``datetime``."""
    tree = _at_parent(test_db, CORE, scratch_schema, tmp_path)
    _insert(test_db, scratch_schema, ["2026-10-01T01:00:00+02:00"])
    assert _upgrade(tree, scratch_schema).returncode == 0

    with test_db.connect() as conn:
        data_type = conn.execute(
            text(
                "SELECT data_type FROM information_schema.columns WHERE "
                "table_schema = :s AND table_name = 'events' "
                "AND column_name = 'created_at'"
            ),
            {"s": scratch_schema},
        ).scalar()
        (value,) = conn.execute(
            text(f'SELECT min(created_at) FROM "{scratch_schema}".events')
        ).one()
    assert data_type == "character varying"
    assert isinstance(value, str) and value[:10] == "2026-09-30"


# ---------------------------------------------------------------------------
# 14 / 15. The profile switches
# ---------------------------------------------------------------------------
@pytest.mark.modules
def test_a_core_database_met_by_the_full_image_is_rewritten(
    test_db, scratch_schema, tmp_path
):
    _at_released(test_db, CORE, scratch_schema, tmp_path)
    (offset,) = _insert(test_db, scratch_schema, ["2026-10-01T01:00:00+02:00"])

    upgrade = _upgrade(tree_profiles.tree_for(FULL, tmp_path), scratch_schema)

    assert upgrade.returncode == 0, upgrade.stderr[-3000:]
    assert _rows(test_db, scratch_schema) == ROWS[FULL]
    assert _stored(test_db, scratch_schema)[offset] == "2026-09-30T23:00:00+00:00"


@pytest.mark.modules
def test_a_full_database_met_by_the_core_image_is_refused_unchanged(
    test_db, scratch_schema, tmp_path
):
    """A full database at the previous release: refused by a core image with
    nothing rewritten, then finished by the full one."""
    _at_released(test_db, FULL, scratch_schema, tmp_path)
    (offset,) = _insert(test_db, scratch_schema, ["2026-10-01T01:00:00+02:00"])

    refused = _upgrade(tree_profiles.tree_for(CORE, tmp_path), scratch_schema)

    assert refused.returncode != 0
    assert "Run the full image of this release against it" in refused.stderr
    assert _rows(test_db, scratch_schema) == RELEASED_ROWS[FULL]
    assert _stored(test_db, scratch_schema)[offset] == "2026-10-01T01:00:00+02:00"

    finished = _upgrade(tree_profiles.tree_for(FULL, tmp_path), scratch_schema)
    assert finished.returncode == 0, finished.stderr[-3000:]
    assert _stored(test_db, scratch_schema)[offset] == "2026-09-30T23:00:00+00:00"


# ---------------------------------------------------------------------------
# The documented SQL selects exactly the migration's candidates
# ---------------------------------------------------------------------------
def _documented(label: str, schema: str) -> str:
    sql = sql_blocks()[label]
    assert sql.count("experimentation.events") == 1, sql
    return sql.replace("experimentation.events", f'"{schema}".events')


@pytest.mark.regression
def test_the_documented_sql_selects_exactly_what_the_migration_rewrites(
    test_db, scratch_schema, tmp_path
):
    """Before: the count is every candidate in the corpus.  After: the list is
    exactly the unreadable candidates, as many as the log line's M."""
    tree = _at_parent(test_db, CORE, scratch_schema, tmp_path)
    ids = _insert(test_db, scratch_schema, [stored for stored, _, _ in CORPUS])
    candidates = {i for i, (_, _, candidate) in zip(ids, CORPUS) if candidate}
    with test_db.connect() as conn:
        counted = conn.execute(text(_documented(COUNT, scratch_schema))).scalar()
        by_predicate = {
            row[0]
            for row in conn.execute(
                text(
                    f'SELECT id::text FROM "{scratch_schema}".events WHERE '
                    + load_revision()._CANDIDATES
                )
            )
        }
    print(f"documented count {counted}, candidates by hand {len(candidates)}")
    assert by_predicate == candidates
    assert counted == len(candidates) == 17

    upgrade = _upgrade(tree, scratch_schema)
    assert upgrade.returncode == 0, upgrade.stderr[-3000:]
    with test_db.connect() as conn:
        left = {
            str(row[0]) for row in conn.execute(text(_documented(LEFT, scratch_schema)))
        }
    unreadable = {
        i for i, (_, after, candidate) in zip(ids, CORPUS) if candidate and not after
    }
    assert left == unreadable
    assert _counted(_log(upgrade.stdout))[1] == len(left)


# ---------------------------------------------------------------------------
# Offline mode
# ---------------------------------------------------------------------------
def test_offline_mode_is_refused(test_db, scratch_schema, tmp_path):
    tree = _at_parent(test_db, CORE, scratch_schema, tmp_path)

    offline = tree_profiles.alembic(
        tree, scratch_schema, "upgrade", f"{PARENT}:{REVISION}", "--sql"
    )

    assert offline.returncode != 0
    assert "cannot be emitted as SQL; run it online" in offline.stderr
    assert _rows(test_db, scratch_schema) == PARENT_ROWS[CORE]
