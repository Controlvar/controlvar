"""``271f03a31742``: ``experiments.resume_at`` and its check, through the transitions.

The revision adds a nullable ``resume_at timestamptz`` to ``experiments`` and
the check ``ck_experiments_resume_only_when_paused`` (``resume_at IS NULL OR
status = 'PAUSED'``), each only if absent.  A schema built by ``create_all``
already has both, so the other transition tests -- which rewind such a schema's
``alembic_version`` -- never run this revision's DDL.  Every test here starts
from what the previous release left instead: a bootstrapped schema with the
column and the check DROPPED and ``alembic_version`` at the previous head, in
each profile, and runs the documented ``alembic upgrade heads``.  Then:

1. the ``alembic_version`` rows are this release's, the column is
   ``timestamp with time zone`` and nullable, the check is in ``pg_constraint``,
   and autogenerate against the result is empty;
2. the upgraded database and a ``create_all``-bootstrapped one carry the same
   check constraints, compared by name and ``pg_get_constraintdef``
   (autogenerate does not compare check constraints, so (1) cannot see this);
3. a row that breaks the check, written while the check was absent, does not
   stop the upgrade: its ``resume_at`` is cleared, a paused row keeps its own;
4. an UPDATE that goes round the ORM cannot leave a non-paused row with a resume
   time: the database refuses it;
5. ``downgrade`` removes exactly the column and the check.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import insert, inspect, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.db import bootstrap
from backend.app.models.experiment import Experiment, ExperimentStatus
from backend.tests.integration.database import tree_profiles
from backend.tests.integration.database.tree_profiles import CORE, FULL

pytestmark = [pytest.mark.integration]

#: This revision, and the core revision it extends.
REVISION = "271f03a31742"
PREVIOUS_CORE_HEAD = "8fd44fb483a2"
#: The core head of this tree, which ``upgrade heads`` runs on to.
CORE_HEAD = "607c51a59bf5"
#: Tables that a later core revision's downgrade drops: a downgrade from the
#: head to this test's target unapplies those revisions too.  ``37dcb2969766``
#: (#440) drops ``segment_members``; ``d29a479daafe`` (#445) drops
#: ``holdout_population``; ``806901fb7735`` (#580) drops columns and checks, no
#: table.
LATER_DOWNGRADE_TABLES = {"holdout_population", "segment_members"}
#: The modules branch's head, in the previous release and in this one alike.
MODULES_HEAD = "modules_0002_warehouse_analysis"

PREVIOUS_ROWS = {
    CORE: {PREVIOUS_CORE_HEAD},
    FULL: {PREVIOUS_CORE_HEAD, MODULES_HEAD},
}
ROWS = {
    CORE: {CORE_HEAD},
    FULL: {CORE_HEAD, MODULES_HEAD},
}

COLUMN = "resume_at"
CHECK = "ck_experiments_resume_only_when_paused"

#: The full-profile case needs a ``modules/`` directory; the core one builds
#: its own sealed tree and runs in either checkout.
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
    yield from _scratch(test_db, "resume")


@pytest.fixture
def reference_schema(test_db):
    yield from _scratch(test_db, "resumeref")


def _rows(engine, schema: str) -> set[str]:
    return bootstrap.recorded_revisions(engine, schema)


def _column(engine, schema: str) -> dict | None:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT data_type, is_nullable, column_default "
                "FROM information_schema.columns "
                "WHERE table_schema = :s AND table_name = 'experiments' "
                "AND column_name = :c"
            ),
            {"s": schema, "c": COLUMN},
        ).one_or_none()
    return None if row is None else dict(row._mapping)


def _checks(engine, schema: str) -> dict[str, str]:
    """Every check constraint on *schema*.experiments: name -> definition.

    ``pg_get_constraintdef`` qualifies the enum cast with the type's schema.
    Today that is ``public`` (``'PAUSED'::public.experimentstatus``); should
    the type ever live in the scratch schema itself, its name is replaced so
    two schemas' definitions still compare equal.
    """
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT c.conname, pg_get_constraintdef(c.oid) "
                "FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid "
                "JOIN pg_namespace n ON n.oid = t.relnamespace "
                "WHERE n.nspname = :s AND t.relname = 'experiments' "
                "AND c.contype = 'c'"
            ),
            {"s": schema},
        ).all()
    return {name: definition.replace(schema, "<schema>") for name, definition in rows}


def _at_previous_release(engine, schema: str, rows: set[str]) -> None:
    """Turn a bootstrapped *schema* into what the previous release left."""
    with engine.begin() as conn:
        conn.execute(
            text(f'ALTER TABLE "{schema}".experiments DROP CONSTRAINT "{CHECK}"')
        )
        conn.execute(text(f'ALTER TABLE "{schema}".experiments DROP COLUMN {COLUMN}'))
        conn.execute(text(f'DELETE FROM "{schema}".alembic_version'))
        for revision in sorted(rows):
            conn.execute(
                text(f'INSERT INTO "{schema}".alembic_version VALUES (:rev)'),
                {"rev": revision},
            )
    assert _column(engine, schema) is None
    assert CHECK not in _checks(engine, schema)
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


def _in_schema(conn, schema: str):
    """*conn*, with the models' schema mapped onto the scratch *schema*."""
    return conn.execution_options(
        schema_translate_map={Experiment.__table__.schema: schema}
    )


def _insert_experiment(conn, **values) -> uuid.UUID:
    experiment_id = uuid.uuid4()
    conn.execute(
        insert(Experiment.__table__).values(
            id=experiment_id, name=f"resume-{experiment_id.hex[:8]}", **values
        )
    )
    return experiment_id


# ---------------------------------------------------------------------------
# 1. The previous release, upgraded with `heads`
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_a_database_at_the_previous_release_gains_the_column_and_the_check(
    profile, test_db, scratch_schema, tmp_path
):
    tree = _bootstrapped_at_previous_release(test_db, profile, scratch_schema, tmp_path)

    _upgrade(tree, scratch_schema)

    rows = _rows(test_db, scratch_schema)
    print(f"[{profile}] alembic_version after upgrade heads: {sorted(rows)}")
    assert rows == ROWS[profile]

    column = _column(test_db, scratch_schema)
    print(f"[{profile}] experiments.{COLUMN}: {column}")
    assert column == {
        "data_type": "timestamp with time zone",
        "is_nullable": "YES",
        "column_default": None,
    }

    checks = _checks(test_db, scratch_schema)
    print(f"[{profile}] {CHECK}: {checks.get(CHECK)}")
    assert CHECK in checks

    # The column's type and nullability are the model's: autogenerate, which
    # does compare those, finds nothing to add.
    result = tree_profiles.autogenerate(
        tree, scratch_schema, tmp_path, head=tree_profiles.CORE_HEAD
    )
    assert result.body is not None, result.describe()
    assert result.operations == [], result.describe()


# ---------------------------------------------------------------------------
# 2. The migration's check and the model's are the same constraint
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_an_upgraded_database_and_a_bootstrapped_one_have_the_same_checks(
    profile, test_db, scratch_schema, reference_schema, tmp_path
):
    """Autogenerate compares no check constraint, so this is the only gate.

    A fresh deployment gets the check from the model (``create_all``); an
    existing one gets it from this migration.  Name and definition must agree,
    or the two populations of databases enforce different rules.
    """
    tree = _bootstrapped_at_previous_release(test_db, profile, scratch_schema, tmp_path)
    _upgrade(tree, scratch_schema)

    created = tree_profiles.bootstrap_schema(tree, reference_schema)
    assert created.returncode == 0, created.stderr[-3000:]

    upgraded = _checks(test_db, scratch_schema)
    bootstrapped = _checks(test_db, reference_schema)
    print(f"[{profile}] upgraded:     {upgraded}")
    print(f"[{profile}] bootstrapped: {bootstrapped}")
    assert CHECK in bootstrapped
    assert upgraded[CHECK] == bootstrapped[CHECK]
    assert upgraded == bootstrapped


# ---------------------------------------------------------------------------
# 3. A row that breaks the check does not stop the upgrade
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_a_row_breaking_the_check_is_cleared_and_the_upgrade_succeeds(
    profile, test_db, scratch_schema, tmp_path
):
    """The column exists, the check does not, and one row breaks it.

    The upgrade clears ``resume_at`` on every row that is not paused before it
    adds the check, so ``ADD CONSTRAINT`` cannot fail on such a row.  A paused
    row keeps its resume time.
    """
    tree = _bootstrapped_at_previous_release(test_db, profile, scratch_schema, tmp_path)
    resume = dt.datetime(2030, 1, 1, 12, 0, tzinfo=dt.timezone.utc)
    with test_db.begin() as conn:
        conn.execute(
            text(
                f'ALTER TABLE "{scratch_schema}".experiments '
                f"ADD COLUMN {COLUMN} TIMESTAMP WITH TIME ZONE"
            )
        )
        scoped = _in_schema(conn, scratch_schema)
        violating = _insert_experiment(
            scoped, status=ExperimentStatus.ACTIVE, resume_at=resume
        )
        paused = _insert_experiment(
            scoped, status=ExperimentStatus.PAUSED, resume_at=resume
        )

    _upgrade(tree, scratch_schema)

    assert _rows(test_db, scratch_schema) == ROWS[profile]
    assert CHECK in _checks(test_db, scratch_schema)
    with test_db.connect() as conn:
        scoped = _in_schema(conn, scratch_schema)
        table = Experiment.__table__
        after = dict(
            scoped.execute(
                select(table.c.id, table.c.resume_at).where(
                    table.c.id.in_([violating, paused])
                )
            ).all()
        )
    assert after == {violating: None, paused: resume}


# ---------------------------------------------------------------------------
# 4. An UPDATE that goes round the ORM is refused by the database
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_a_bulk_status_update_on_a_row_with_a_resume_time_is_refused(
    profile, test_db, scratch_schema, tmp_path
):
    """``update(Experiment).values(status=ACTIVE)`` runs no attribute event.

    Whatever the application later does to clear ``resume_at`` on a status
    change, a bulk UPDATE goes round it.  The check is what stops such an
    UPDATE leaving an active experiment with a pending resume.
    """
    tree = _bootstrapped_at_previous_release(test_db, profile, scratch_schema, tmp_path)
    _upgrade(tree, scratch_schema)

    resume = dt.datetime(2030, 1, 1, 12, 0, tzinfo=dt.timezone.utc)
    with test_db.begin() as conn:
        experiment_id = _insert_experiment(
            _in_schema(conn, scratch_schema),
            status=ExperimentStatus.PAUSED,
            resume_at=resume,
        )

    with test_db.connect() as conn:
        with Session(bind=_in_schema(conn, scratch_schema)) as session:
            with pytest.raises(IntegrityError, match=CHECK):
                session.execute(
                    update(Experiment)
                    .where(Experiment.id == experiment_id)
                    .values(status=ExperimentStatus.ACTIVE)
                )
            session.rollback()


# ---------------------------------------------------------------------------
# 5. Downgrade
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_downgrade_removes_the_column_and_the_check_and_nothing_else(
    profile, test_db, scratch_schema, tmp_path
):
    tree = tree_profiles.tree_for(profile, tmp_path)
    assert tree_profiles.bootstrap_schema(tree, scratch_schema).returncode == 0
    tables = set(inspect(test_db).get_table_names(schema=scratch_schema))
    checks = _checks(test_db, scratch_schema)
    assert CHECK in checks and _column(test_db, scratch_schema) is not None

    down = tree_profiles.alembic(tree, scratch_schema, "downgrade", PREVIOUS_CORE_HEAD)
    assert down.returncode == 0, down.stderr[-3000:]

    assert _rows(test_db, scratch_schema) == PREVIOUS_ROWS[profile]
    assert _column(test_db, scratch_schema) is None
    # The downgrade to the previous core head also unapplies every revision
    # above this one; ``806901fb7735`` (#580) drops its own two checks.
    later = {"ck_experiments_correction_method", "ck_experiments_confidence_level"}
    assert _checks(test_db, scratch_schema) == {
        name: definition
        for name, definition in checks.items()
        if name != CHECK and name not in later
    }
    assert (
        set(inspect(test_db).get_table_names(schema=scratch_schema))
        == tables - LATER_DOWNGRADE_TABLES
    )
