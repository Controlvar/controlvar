"""``7b6b4b4fe246``: when each rollout stage started, through the transitions (#1528).

The revision adds ``rollout_stages.started_at`` (``timestamp without time
zone``, nullable, no default) when it is absent, then sets it from
``updated_at`` on every ``IN_PROGRESS`` stage whose ``started_at`` is NULL. A
schema built by ``create_all`` already has the column, so the other transition
tests -- which rewind such a schema's ``alembic_version`` -- never run the
``ADD COLUMN``. The tests that start from the previous release instead take a
bootstrapped schema with the column DROPPED and ``alembic_version`` at
``85dd4796bb8b``, in each profile, and run the documented ``alembic upgrade
heads``. Then:

1. the ``alembic_version`` rows are this release's, the column has the model's
   type, nullability and (no) default, the stage in progress starts exactly
   where the scheduler measured from before (its ``updated_at``), the pending
   and completed stages have no start, and autogenerate is EMPTY on that
   upgraded database (the fresh-bootstrap autogenerate test cannot see a
   migration whose type differs from the model's: both sides come from the
   model there);
2. the upgraded database and a ``create_all``-bootstrapped one have the same
   column (``format_type``, NOT NULL, ``pg_get_expr``);
3. replayed over a schema that already has the column, the backfill fills only
   a NULL start and leaves a recorded one;
4. the previous release's code -- this tree without the revision, the column
   and its three writes -- creates a schedule, starts its stages and renames
   one against this schema; the stages it starts have no ``started_at``, and
   this release's scheduler completes its time-based stage from ``updated_at``
   (the rolling deploy: an older replica can win a tick);
5. the downgrade removes exactly the column and nothing else, and does not
   refuse; an upgrade afterwards sets the stage in progress from its
   ``updated_at`` again, which an edit made in the meantime moved;
6. the previous release refuses to start against this revision, and starts
   once the downgrade has run (the rollback where the API migrates on start);
7. the upgrade refuses offline (``--sql``) mode: it reads the catalog;
8. ``alembic heads`` names exactly this revision and the modules head.
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import textwrap
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from backend.app.db import bootstrap
from backend.tests.integration.database import tree_profiles
from backend.tests.integration.database.tree_profiles import CORE, FULL

pytestmark = [pytest.mark.integration]

#: This revision, and the core revision it extends: ``85dd4796bb8b`` (#1543),
#: which reached main before it.
REVISION = "7b6b4b4fe246"
PREVIOUS_CORE_HEAD = "85dd4796bb8b"
#: The core head of this tree: ``4955571d232c`` (#1545), whose downgrade drops
#: ``visual_tokens`` and two columns of ``experiments`` and ``variants``, after
#: ``c448_balance_within`` (#448), the revision after this one, whose downgrade
#: drops the table ``balance_group_counters`` and four columns of
#: ``experiments`` and ``assignments``.
CORE_HEAD = "4955571d232c"
#: The core revisions after this one today, newer than release N-1 too: the
#: walk from the revision graph below must find each of them.
LATER_REVISIONS = ("c448_balance_within", "4955571d232c")
#: Tables a later core revision's downgrade drops: a downgrade from the head to
#: this test's target unapplies those revisions too.
LATER_DOWNGRADE_TABLES = {"balance_group_counters", "visual_tokens"}
#: The modules branch's head in this tree, recorded on both sides of every
#: step here: this file is about the core chain.
MODULES_HEAD = "modules_0004_rbac_activation"

PREVIOUS_ROWS = {
    CORE: {PREVIOUS_CORE_HEAD},
    FULL: {PREVIOUS_CORE_HEAD, MODULES_HEAD},
}
ROWS = {
    CORE: {CORE_HEAD},
    FULL: {CORE_HEAD, MODULES_HEAD},
}

TABLE = "rollout_stages"
COLUMN = "started_at"
#: The column as PostgreSQL describes it, after either path:
#: (``format_type``, NOT NULL, ``pg_get_expr`` of its default).
EXPECTED_COLUMN = ("timestamp without time zone", False, None)

UPGRADED = "upgraded"
BOOTSTRAPPED = "bootstrapped"

#: The full-profile case needs a ``modules/`` directory; the core one builds
#: its own sealed tree and runs in either checkout.
BOTH_PROFILES = [CORE, pytest.param(FULL, marks=pytest.mark.modules)]

#: The instants the stored stages carry: fixed, never computed from the clock.
STARTED = datetime(2026, 10, 30, 9, 15, 30, 123456)
EDITED = datetime(2026, 10, 31, 16, 0)
#: How long before its ``updated_at`` each stored stage was created.
CREATED_BEFORE_UPDATED = timedelta(days=7)


def _scratch(test_db, prefix: str):
    schema = f"{prefix}_{uuid.uuid4().hex[:8]}"
    try:
        yield schema
    finally:
        with test_db.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


@pytest.fixture
def scratch_schema(test_db):
    yield from _scratch(test_db, "stagestart")


@pytest.fixture
def reference_schema(test_db):
    yield from _scratch(test_db, "stagestartref")


def _rows(engine, schema: str) -> set[str]:
    return bootstrap.recorded_revisions(engine, schema)


def _column(engine, schema: str):
    """``started_at`` as (format_type, NOT NULL, default), or ``None``."""
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT format_type(a.atttypid, a.atttypmod), a.attnotnull, "
                "pg_get_expr(d.adbin, d.adrelid) "
                "FROM pg_attribute a "
                "JOIN pg_class t ON t.oid = a.attrelid "
                "JOIN pg_namespace n ON n.oid = t.relnamespace "
                "LEFT JOIN pg_attrdef d "
                "ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
                "WHERE n.nspname = :s AND t.relname = :t AND a.attname = :c "
                "AND NOT a.attisdropped"
            ),
            {"s": schema, "t": TABLE, "c": COLUMN},
        ).one_or_none()
    return tuple(row) if row is not None else None


def _all_columns(engine, schema: str) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns(TABLE, schema=schema)}


def _tables(engine, schema: str) -> set[str]:
    return set(inspect(engine).get_table_names(schema=schema))


def _insert_stages(engine, schema: str) -> dict[str, str]:
    """A flag, a schedule and three stages, as the previous release stores them
    (no ``started_at`` named): {status: stage id}. Each stage was created a
    week before its ``updated_at``, so a backfill from ``created_at`` is told
    apart from one from ``updated_at`` (#1632 review F3)."""
    flag_id, schedule_id = str(uuid.uuid4()), str(uuid.uuid4())
    ids = {
        status: str(uuid.uuid4()) for status in ("COMPLETED", "IN_PROGRESS", "PENDING")
    }
    with engine.begin() as conn:
        conn.execute(
            text(
                f'INSERT INTO "{schema}".feature_flags '
                "(id, key, name, status, rollout_percentage, created_at, updated_at) "
                "VALUES (:id, :key, 'Stage start', 'ACTIVE', 50, now(), now())"
            ),
            {"id": flag_id, "key": f"stage-start-{flag_id[:8]}"},
        )
        conn.execute(
            text(
                f'INSERT INTO "{schema}".rollout_schedules '
                "(id, feature_flag_id, name, status, max_percentage, created_at, "
                "updated_at) VALUES (:id, :flag, 'Stage start', 'ACTIVE', 100, "
                "now(), now())"
            ),
            {"id": schedule_id, "flag": flag_id},
        )
        for order, (status, updated_at) in enumerate(
            [
                ("COMPLETED", STARTED - timedelta(days=1)),
                ("IN_PROGRESS", STARTED),
                ("PENDING", STARTED - timedelta(days=2)),
            ],
            start=1,
        ):
            conn.execute(
                text(
                    f'INSERT INTO "{schema}".rollout_stages '
                    "(id, rollout_schedule_id, name, stage_order, "
                    "target_percentage, status, trigger_type, "
                    "trigger_configuration, created_at, updated_at) "
                    "VALUES (:id, :schedule, :name, :order, :target, :status, "
                    "'TIME_BASED', CAST(:config AS jsonb), :created, :at)"
                ),
                {
                    "id": ids[status],
                    "schedule": schedule_id,
                    "name": status.lower(),
                    "order": order,
                    "target": order * 25,
                    "status": status,
                    "config": json.dumps({"duration": 1}),
                    "created": updated_at - CREATED_BEFORE_UPDATED,
                    "at": updated_at,
                },
            )
    return ids


def _starts(engine, schema: str) -> dict[str, tuple]:
    """status -> (started_at, updated_at) for every stage in *schema*."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                f"SELECT status::text, {COLUMN}, updated_at "
                f'FROM "{schema}".{TABLE} ORDER BY stage_order'
            )
        ).all()
    return {status: (started, updated) for status, started, updated in rows}


def _drop_column(engine, schema: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(f'ALTER TABLE "{schema}".{TABLE} DROP COLUMN IF EXISTS {COLUMN}')
        )


def _stamp(engine, schema: str, rows: set[str]) -> None:
    with engine.begin() as conn:
        conn.execute(text(f'DELETE FROM "{schema}".alembic_version'))
        for revision in sorted(rows):
            conn.execute(
                text(f'INSERT INTO "{schema}".alembic_version VALUES (:rev)'),
                {"rev": revision},
            )


def _at_previous_release(engine, schema: str, rows: set[str]) -> None:
    """Turn a bootstrapped *schema* into what the previous release left."""
    _drop_column(engine, schema)
    _stamp(engine, schema, rows)
    assert _column(engine, schema) is None
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


def _upgrade(tree, schema: str) -> None:
    upgrade = tree_profiles.alembic(tree, schema, "upgrade", "heads")
    assert upgrade.returncode == 0, upgrade.stderr[-3000:]


def _report(profile, label, engine, schema) -> None:
    print(f"[{profile}] {label}: alembic_version={sorted(_rows(engine, schema))}")
    print(f"[{profile}] {label}: {TABLE}.{COLUMN}={_column(engine, schema)}")


# ---------------------------------------------------------------------------
# 1. The previous release, upgraded with `heads`
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_a_database_at_the_previous_release_gains_the_column_and_each_start(
    profile, test_db, scratch_schema, tmp_path
):
    tree = _bootstrapped_at_previous_release(test_db, profile, scratch_schema, tmp_path)
    ids = _insert_stages(test_db, scratch_schema)
    _report(profile, "at the previous release", test_db, scratch_schema)

    _upgrade(tree, scratch_schema)
    _report(profile, "after upgrade heads", test_db, scratch_schema)

    assert _rows(test_db, scratch_schema) == ROWS[profile]
    assert _column(test_db, scratch_schema) == EXPECTED_COLUMN
    starts = _starts(test_db, scratch_schema)
    print(f"[{profile}] starts after upgrade: {starts}")
    # The stage in progress starts exactly where the scheduler measured from;
    # the others have no recorded start.
    assert starts["IN_PROGRESS"] == (STARTED, STARTED)
    assert starts["COMPLETED"][0] is None
    assert starts["PENDING"][0] is None
    assert len(ids) == 3


@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_autogenerate_is_empty_on_a_database_upgraded_from_the_previous_release(
    profile, test_db, scratch_schema, tmp_path
):
    """2j1: the type and nullability are the model's. Autogenerate on THIS
    (upgraded) database compares them, and finds nothing to add. The
    fresh-bootstrap autogenerate test cannot see a wrong type in the migration:
    there both sides come from the model."""
    tree = _bootstrapped_at_previous_release(test_db, profile, scratch_schema, tmp_path)
    _upgrade(tree, scratch_schema)
    assert _rows(test_db, scratch_schema) == ROWS[profile]

    result = tree_profiles.autogenerate(
        tree, scratch_schema, tmp_path, head=tree_profiles.CORE_HEAD
    )
    print(f"[{profile}] autogenerate on the upgraded database: {result.operations}")
    assert result.body is not None, result.describe()
    assert result.operations == [], result.describe()


# ---------------------------------------------------------------------------
# 2. The migration's column is the model's (2j2)
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_an_upgraded_database_and_a_bootstrapped_one_have_the_same_column(
    profile, test_db, scratch_schema, reference_schema, tmp_path
):
    """A fresh deployment gets the column from the model (``create_all``); an
    existing one from this migration. They must be the same column."""
    tree = _bootstrapped_at_previous_release(test_db, profile, scratch_schema, tmp_path)
    _upgrade(tree, scratch_schema)
    created = tree_profiles.bootstrap_schema(tree, reference_schema)
    assert created.returncode == 0, created.stderr[-3000:]

    upgraded = _column(test_db, scratch_schema)
    bootstrapped = _column(test_db, reference_schema)
    print(f"[{profile}] {COLUMN} upgraded:     {upgraded}")
    print(f"[{profile}] {COLUMN} bootstrapped: {bootstrapped}")
    assert bootstrapped == EXPECTED_COLUMN
    assert upgraded == bootstrapped


# ---------------------------------------------------------------------------
# 3. Replayed over a schema that has the column
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_a_replay_fills_only_a_missing_start(test_db, scratch_schema, tmp_path):
    """The transition tests rewind a ``create_all`` schema and replay the chain:
    a start already recorded is kept, a NULL one is filled."""
    tree = _bootstrapped(CORE, scratch_schema, tmp_path)
    ids = _insert_stages(test_db, scratch_schema)
    recorded = STARTED - timedelta(hours=3)
    with test_db.begin() as conn:
        conn.execute(
            text(
                f'UPDATE "{scratch_schema}".{TABLE} SET {COLUMN} = :at WHERE id = :id'
            ),
            {"at": recorded, "id": ids["IN_PROGRESS"]},
        )
    _stamp(test_db, scratch_schema, PREVIOUS_ROWS[CORE])
    _upgrade(tree, scratch_schema)
    assert _starts(test_db, scratch_schema)["IN_PROGRESS"] == (recorded, STARTED)

    # And a NULL one, replayed again from the previous release, is filled.
    with test_db.begin() as conn:
        conn.execute(text(f'UPDATE "{scratch_schema}".{TABLE} SET {COLUMN} = NULL'))
    _stamp(test_db, scratch_schema, PREVIOUS_ROWS[CORE])
    _upgrade(tree, scratch_schema)
    starts = _starts(test_db, scratch_schema)
    assert starts["IN_PROGRESS"] == (STARTED, STARTED)
    assert (starts["COMPLETED"][0], starts["PENDING"][0]) == (None, None)


# ---------------------------------------------------------------------------
# 4. The previous release's code against this schema (the rolling deploy)
# ---------------------------------------------------------------------------
_PREVIOUS_RELEASE_WRITES = textwrap.dedent(
    """
    import json
    import uuid

    from backend.app.models import register_core_models

    register_core_models()

    from backend.app.db.session import SessionLocal
    from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
    from backend.app.models.rollout_schedule import RolloutStage
    from backend.app.schemas.rollout_schedule import (
        RolloutScheduleCreate,
        RolloutStageUpdate,
    )
    from backend.app.services.rollout_service import RolloutService

    # The previous release's model: it does not know the column.
    assert "started_at" not in RolloutStage.__table__.columns.keys()

    db = SessionLocal()
    flag = FeatureFlag(
        key="previous-release-rollout",
        name="Previous release",
        status=FeatureFlagStatus.ACTIVE,
        rollout_percentage=0,
    )
    db.add(flag)
    db.commit()
    db.refresh(flag)
    schedule = RolloutService.create_rollout_schedule(
        db,
        RolloutScheduleCreate(
            name="Previous release",
            feature_flag_id=flag.id,
            stages=[
                {"name": "canary", "stage_order": 1, "target_percentage": 10,
                 "trigger_type": "manual"},
                {"name": "timed", "stage_order": 2, "target_percentage": 50,
                 "trigger_type": "time_based",
                 "trigger_configuration": {"duration": 1}},
            ],
        ),
        None,
    )
    stages = {s.name: s.id for s in schedule.stages}
    RolloutService.activate_rollout_schedule(db, schedule.id)
    # Start the canary, then complete it, which starts the timed stage.
    RolloutService.manually_advance_stage(db, stages["canary"])
    RolloutService.manually_advance_stage(db, stages["canary"])
    RolloutService.update_rollout_stage(
        db, stages["timed"], RolloutStageUpdate(name="timed (renamed)")
    )
    listed = RolloutService.get_rollout_schedule(db, schedule.id)
    print(json.dumps({
        "schedule": str(schedule.id),
        "stages": {s.name: [str(s.id), s.status.value] for s in listed.stages},
    }))
    """
)

#: What the previous release does not have: the column on the model, and the
#: three writes of it. If one is not found exactly as often as stated, the
#: test says so rather than testing the wrong tree.
_PREVIOUS_RELEASE_REMOVALS = (
    (
        ("backend", "app", "models", "rollout_schedule.py"),
        re.compile(r"(?m)^    started_at = Column\(UTCDateTime, nullable=True\)\n"),
        1,
    ),
    (
        ("backend", "app", "services", "rollout_service.py"),
        re.compile(r"(?m)^ +(?:next_)?stage\.started_at = current_time\n"),
        2,
    ),
    (
        ("backend", "app", "core", "rollout_scheduler.py"),
        re.compile(r"(?m)^ +stage\.started_at = current_time\n"),
        1,
    ),
)


def _this_revision_and_every_later_one(tree) -> dict:
    """``{revision id: script file}`` for REVISION and every revision after
    it, in *tree*'s own script directory: each is newer than release N-1.

    Read from the revision graph, not listed, so a revision added on top
    later, or re-pointed onto another, is removed with the rest."""
    script = ScriptDirectory.from_config(Config(str(tree_profiles.alembic_ini(tree))))
    found, pending = {}, [REVISION]
    while pending:
        revision = script.get_revision(pending.pop())
        if revision.revision not in found:
            found[revision.revision] = pathlib.Path(revision.path)
            pending.extend(sorted(revision.nextrev))
    return found


#: A revision written on top of the core head in a copy of the tree, as a
#: later release (or a re-point) would add one.
_LATER = "9f8e7d6c5b4a"


@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_the_previous_release_loses_this_revision_and_each_later_one(profile, tmp_path):
    """The walk above, on a copy of each profile's tree with one more revision
    on top of the core head: this revision, the core head and the one on top,
    never one of release N-1's and never the modules branch's."""
    destination = tmp_path / "walk"
    if profile == FULL:
        tree = tree_profiles.full_tree(destination)
    else:
        tree = tree_profiles.core_tree(destination)
    versions = tree / "backend" / "app" / "db" / "migrations" / "versions"
    (versions / f"{_LATER}_a_later_revision.py").write_text(
        f'revision = "{_LATER}"\ndown_revision = "{CORE_HEAD}"\n'
        "branch_labels = None\ndepends_on = None\n\n\n"
        "def upgrade():\n    pass\n\n\ndef downgrade():\n    pass\n",
        encoding="utf-8",
    )

    newer = _this_revision_and_every_later_one(tree)

    print(f"[{profile}] newer than release N-1: {sorted(newer)}")
    assert {REVISION, *LATER_REVISIONS, CORE_HEAD, _LATER} <= set(newer)
    assert PREVIOUS_CORE_HEAD not in newer
    assert not any(revision.startswith("modules") for revision in newer)
    parents = {path.parent.resolve() for path in newer.values()}
    assert parents == {versions.resolve()}


def _previous_release(profile: str, tmp_path):
    """Release N-1: a copy of this tree without the revision, the column and
    its writes.

    The revisions after this one go too: they are newer than release N-1, and
    one whose ``down_revision`` file is gone would break the script directory.
    """
    destination = tmp_path / "release-n-minus-1"
    if profile == FULL:
        tree = tree_profiles.full_tree(destination)
    else:
        tree = tree_profiles.core_tree(destination)
    for revision_file in _this_revision_and_every_later_one(tree).values():
        revision_file.unlink()
    for parts, pattern, expected in _PREVIOUS_RELEASE_REMOVALS:
        path = tree.joinpath(*parts)
        source, found = pattern.subn("", path.read_text(encoding="utf-8"))
        assert found == expected, f"{found} matches of {pattern.pattern} in {path}"
        path.write_text(source, encoding="utf-8")
    return tree


def _tick(engine, schema: str, schedule_id: str, at: datetime) -> None:
    """This release's scheduler, one pass over *schedule_id* at *at*."""
    import asyncio

    from sqlalchemy.orm import Session

    from backend.app.core.rollout_scheduler import RolloutScheduler
    from backend.app.models.rollout_schedule import RolloutSchedule

    models_schema = RolloutSchedule.__table__.schema
    session = Session(
        bind=engine.execution_options(schema_translate_map={models_schema: schema}),
        autoflush=False,
    )
    try:
        schedule = session.get(RolloutSchedule, uuid.UUID(schedule_id))
        asyncio.run(RolloutScheduler()._process_schedule(session, schedule, at))
        session.commit()
    finally:
        session.close()


@pytest.mark.regression
@pytest.mark.parametrize("built_by", [UPGRADED, BOOTSTRAPPED])
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_the_previous_release_starts_stages_and_this_release_completes_them(
    profile, built_by, test_db, scratch_schema, tmp_path
):
    if built_by == UPGRADED:
        release_n = _bootstrapped_at_previous_release(
            test_db, profile, scratch_schema, tmp_path
        )
        _upgrade(release_n, scratch_schema)
    else:
        _bootstrapped(profile, scratch_schema, tmp_path)
    assert _rows(test_db, scratch_schema) == ROWS[profile]
    assert _column(test_db, scratch_schema) == EXPECTED_COLUMN
    release_n_minus_1 = _previous_release(profile, tmp_path)

    script = tmp_path / "previous_release_writes.py"
    script.write_text(_PREVIOUS_RELEASE_WRITES, encoding="utf-8")
    result = tree_profiles.run(release_n_minus_1, [str(script)], scratch_schema)
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-4000:]
    report = json.loads(result.stdout.strip().splitlines()[-1])
    print(f"[{profile}/{built_by}] previous release: {report['stages']}")
    assert {name: status for name, (_, status) in report["stages"].items()} == {
        "canary": "completed",
        "timed (renamed)": "in_progress",
    }

    # The previous release wrote no start: the stage it started has none.
    timed_id = report["stages"]["timed (renamed)"][0]
    with test_db.connect() as conn:
        started_at, updated_at, status = conn.execute(
            text(
                f"SELECT {COLUMN}, updated_at, status::text "
                f'FROM "{scratch_schema}".{TABLE} WHERE id = :id'
            ),
            {"id": timed_id},
        ).one()
    assert (started_at, status) == (None, "IN_PROGRESS")

    # This release completes it from updated_at (the rename, the last thing
    # the previous release wrote): due one hour after it, not before.
    due = updated_at.replace(tzinfo=timezone.utc) + timedelta(hours=1)
    _tick(test_db, scratch_schema, report["schedule"], due - timedelta(seconds=1))
    assert _starts(test_db, scratch_schema)["IN_PROGRESS"][0] is None
    _tick(test_db, scratch_schema, report["schedule"], due + timedelta(seconds=1))
    with test_db.connect() as conn:
        status = conn.execute(
            text(f'SELECT status::text FROM "{scratch_schema}".{TABLE} WHERE id = :id'),
            {"id": timed_id},
        ).scalar_one()
    assert status == "COMPLETED"
    shutil.rmtree(release_n_minus_1, ignore_errors=True)


# ---------------------------------------------------------------------------
# 5. Downgrade
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_the_downgrade_drops_the_column_only_and_an_upgrade_sets_it_from_updated_at(
    profile, test_db, scratch_schema, tmp_path
):
    tree = _bootstrapped_at_previous_release(test_db, profile, scratch_schema, tmp_path)
    _insert_stages(test_db, scratch_schema)
    _upgrade(tree, scratch_schema)
    assert _starts(test_db, scratch_schema)["IN_PROGRESS"] == (STARTED, STARTED)
    tables = _tables(test_db, scratch_schema)
    columns = _all_columns(test_db, scratch_schema)

    down = tree_profiles.alembic(tree, scratch_schema, "downgrade", PREVIOUS_CORE_HEAD)
    assert down.returncode == 0, down.stderr[-3000:]
    _report(profile, "after downgrade", test_db, scratch_schema)
    assert _rows(test_db, scratch_schema) == PREVIOUS_ROWS[profile]
    assert _all_columns(test_db, scratch_schema) == columns - {COLUMN}
    assert _tables(test_db, scratch_schema) == tables - LATER_DOWNGRADE_TABLES
    with test_db.connect() as conn:
        kept = conn.execute(
            text(f'SELECT count(*) FROM "{scratch_schema}".{TABLE}')
        ).scalar_one()
    assert kept == 3

    # An edit while the column is gone moves updated_at; the upgrade then
    # sets the start from it: the recorded start is not restored.
    with test_db.begin() as conn:
        conn.execute(
            text(
                f'UPDATE "{scratch_schema}".{TABLE} SET updated_at = :at '
                "WHERE status = 'IN_PROGRESS'"
            ),
            {"at": EDITED},
        )
    _upgrade(tree, scratch_schema)
    _report(profile, "after upgrade heads again", test_db, scratch_schema)
    assert _rows(test_db, scratch_schema) == ROWS[profile]
    assert _column(test_db, scratch_schema) == EXPECTED_COLUMN
    assert _starts(test_db, scratch_schema)["IN_PROGRESS"] == (EDITED, EDITED)


# ---------------------------------------------------------------------------
# 6. Rolling back where the API migrates on start
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_the_previous_release_starts_only_after_the_downgrade(
    profile, test_db, scratch_schema, tmp_path
):
    """Docker Compose and the Helm chart's init container run the bootstrap
    on every start. Against this revision the previous release's bootstrap
    refuses; after a downgrade to the previous release's heads, as ``alembic
    heads`` run with that release prints them (``85dd4796bb8b``, this
    revision's own ``down_revision``, and on a full install the modules head),
    run with this release, it starts."""
    tree = _bootstrapped_at_previous_release(test_db, profile, scratch_schema, tmp_path)
    _upgrade(tree, scratch_schema)
    release_n_minus_1 = _previous_release(profile, tmp_path)

    refused = tree_profiles.bootstrap_schema(release_n_minus_1, scratch_schema)
    print(f"[{profile}] previous release's bootstrap: exit {refused.returncode}")
    assert refused.returncode != 0, refused.stdout[-2000:]
    assert "migrated by a newer Controlvar release" in refused.stderr
    # The database records the core head, the newest revision release N-1
    # does not know.
    assert CORE_HEAD in refused.stderr
    assert _rows(test_db, scratch_schema) == ROWS[profile]

    heads = tree_profiles.alembic(release_n_minus_1, "public", "heads")
    assert heads.returncode == 0, heads.stderr[-3000:]
    previous_heads = {
        line.split()[0] for line in heads.stdout.splitlines() if line.strip()
    }
    assert previous_heads == PREVIOUS_ROWS[profile]
    for head in sorted(previous_heads):
        down = tree_profiles.alembic(tree, scratch_schema, "downgrade", head)
        assert down.returncode == 0, down.stderr[-3000:]
    started = tree_profiles.bootstrap_schema(release_n_minus_1, scratch_schema)
    print(f"[{profile}] after the downgrade: exit {started.returncode}")
    assert started.returncode == 0, started.stderr[-3000:]
    assert _rows(test_db, scratch_schema) == PREVIOUS_ROWS[profile]
    assert _column(test_db, scratch_schema) is None
    shutil.rmtree(release_n_minus_1, ignore_errors=True)


# ---------------------------------------------------------------------------
# 7. Offline mode
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_the_upgrade_refuses_offline_mode(test_db, scratch_schema, tmp_path):
    tree = _bootstrapped_at_previous_release(test_db, CORE, scratch_schema, tmp_path)
    offline = tree_profiles.alembic(
        tree, scratch_schema, "upgrade", f"{PREVIOUS_CORE_HEAD}:{REVISION}", "--sql"
    )
    assert offline.returncode != 0, offline.stdout[-2000:]
    assert "cannot be emitted as SQL; run it online" in offline.stderr
    assert "ADD COLUMN" not in offline.stdout
    assert _column(test_db, scratch_schema) is None
    assert _rows(test_db, scratch_schema) == PREVIOUS_ROWS[CORE]


# ---------------------------------------------------------------------------
# 8. The heads
# ---------------------------------------------------------------------------
HEADS = {CORE: {CORE_HEAD}, FULL: {CORE_HEAD, MODULES_HEAD}}


@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_alembic_heads_are_this_revision_and_the_modules_head(profile, tmp_path):
    """Still two heads in a full tree: this revision extends the core chain."""
    tree = tree_profiles.tree_for(profile, tmp_path)
    heads = tree_profiles.alembic(tree, "public", "heads")
    assert heads.returncode == 0, heads.stderr[-3000:]
    print(f"[{profile}] alembic heads:\n{heads.stdout}")
    named = {line.split()[0] for line in heads.stdout.splitlines() if line.strip()}
    assert named == HEADS[profile]
