"""``a89544fb1075``: ``feature_flags.default_value``, through the transitions (#94).

The revision adds ``default_value boolean NOT NULL DEFAULT false`` to
``feature_flags``, only if absent.  A schema built by ``create_all`` already has
it, so the other transition tests -- which rewind such a schema's
``alembic_version`` -- never run this revision's DDL.  Every test here starts
from what the previous release left instead: a bootstrapped schema with the
column DROPPED and ``alembic_version`` at the previous core head, in each
profile, and runs the documented ``alembic upgrade heads``.  Then:

1. the ``alembic_version`` rows are this release's, the column is ``boolean``,
   ``NOT NULL`` and defaults to ``false``, a row written by the previous release
   reads false, and autogenerate against the result is empty;
2. the upgraded database and a ``create_all``-bootstrapped one define the column
   identically, server default included (autogenerate does not compare server
   defaults -- ``env.py`` leaves ``compare_server_default`` off -- so (1) cannot
   see a migration that forgot it);
3. the previous release's code -- this tree without the revision and without the
   model's column -- creates, updates and lists flags against a database at
   this head, which is what a deploy (old tasks still serving) and an
   image-only rollback both do.  Its INSERTs do not name the column, so the
   server default is what lets them succeed.  The writes go through the ORM
   model as that release's writers did: this tree's own service and schemas
   write and read ``default_value`` (the create/update contract), so they
   cannot stand in for a release that did not know the column;
4. ``downgrade`` removes exactly the column.
"""

from __future__ import annotations

import json
import re
import shutil
import textwrap
import uuid

import pytest
from sqlalchemy import inspect, text

from backend.app.db import bootstrap
from backend.tests.integration.database import tree_profiles
from backend.tests.integration.database.tree_profiles import CORE, FULL

pytestmark = [pytest.mark.integration]

#: This revision, and the core revision it extends.
REVISION = "a89544fb1075"
PREVIOUS_CORE_HEAD = "d12cbd384bbe"
#: The core head of this tree, which ``upgrade heads`` runs on to: the next
#: revision, ``1ab99332f0ba`` (``events.created_at`` in UTC), adds no DDL, and
#: ``806901fb7735`` (the experiments' correction settings) adds only what a
#: database built by ``create_all`` already has, and ``d29a479daafe`` (holdout
#: population), ``37dcb2969766`` (segment kind and members), ``4c2c09a2b3e8``
#: (rollback records keep without a safety configuration) and ``607c51a59bf5``
#: (a flag's value type and two values) find their objects already built by
#: ``create_all`` and add none.
CORE_HEAD = "607c51a59bf5"
#: Tables that a later core revision's downgrade drops: a downgrade from the
#: head to this test's target unapplies those revisions too.  ``d29a479daafe``
#: (#445) drops ``holdout_population``; ``37dcb2969766`` (#440) drops
#: ``segment_members``.
LATER_DOWNGRADE_TABLES = {"holdout_population", "segment_members"}
#: ``feature_flags`` columns a later core revision's downgrade drops:
#: ``607c51a59bf5`` (#1497) drops a flag's value type and its two values.
LATER_DOWNGRADE_COLUMNS = {"value_type", "on_value", "off_value"}
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

COLUMN = "default_value"
#: The column as ``information_schema`` describes it, after either path.
EXPECTED_COLUMN = {
    "data_type": "boolean",
    "is_nullable": "NO",
    "column_default": "false",
}

#: The model's declaration of the column.  The previous release is this tree
#: with it removed (and the revision file); if it is not found exactly once, the
#: test says so rather than testing the wrong tree.
_MODEL_COLUMN = re.compile(r"(?ms)^    default_value = Column\(\n.*?^    \)\n")

#: How the database at this head was made: by this migration, from the
#: previous release's schema (every existing deployment), or by ``create_all``
#: (every fresh one).  The first is where the migration's server default acts,
#: the second where the model's does.
UPGRADED = "upgraded"
BOOTSTRAPPED = "bootstrapped"

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
    yield from _scratch(test_db, "flagdv")


@pytest.fixture
def reference_schema(test_db):
    yield from _scratch(test_db, "flagdvref")


def _rows(engine, schema: str) -> set[str]:
    return bootstrap.recorded_revisions(engine, schema)


def _column(engine, schema: str) -> dict | None:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT data_type, is_nullable, column_default "
                "FROM information_schema.columns "
                "WHERE table_schema = :s AND table_name = 'feature_flags' "
                "AND column_name = :c"
            ),
            {"s": schema, "c": COLUMN},
        ).one_or_none()
    return None if row is None else dict(row._mapping)


def _insert_previous_release_flag(engine, schema: str) -> str:
    """A row as the previous release writes it: no ``default_value`` named."""
    flag_id = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(
            text(
                f'INSERT INTO "{schema}".feature_flags '
                "(id, key, name, status, rollout_percentage, created_at, updated_at) "
                "VALUES (:id, :key, 'Previous release', 'INACTIVE', 0, now(), now())"
            ),
            {"id": flag_id, "key": f"previous-{flag_id[:8]}"},
        )
    return flag_id


def _default_values(engine, schema: str) -> dict[str, bool]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(f'SELECT id::text, {COLUMN} FROM "{schema}".feature_flags')
        ).all()
    return dict(rows)


def _at_previous_release(engine, schema: str, rows: set[str]) -> None:
    """Turn a bootstrapped *schema* into what the previous release left."""
    with engine.begin() as conn:
        # IF EXISTS: with the column missing from the model, ``create_all``
        # never made it, and the test must reach the autogenerate that says so.
        conn.execute(
            text(f'ALTER TABLE "{schema}".feature_flags DROP COLUMN IF EXISTS {COLUMN}')
        )
        conn.execute(text(f'DELETE FROM "{schema}".alembic_version'))
        for revision in sorted(rows):
            conn.execute(
                text(f'INSERT INTO "{schema}".alembic_version VALUES (:rev)'),
                {"rev": revision},
            )
    assert _column(engine, schema) is None
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


# ---------------------------------------------------------------------------
# 1. The previous release, upgraded with `heads`
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_a_database_at_the_previous_release_gains_the_column_false_on_every_row(
    profile, test_db, scratch_schema, tmp_path
):
    tree = _bootstrapped_at_previous_release(test_db, profile, scratch_schema, tmp_path)
    existing = _insert_previous_release_flag(test_db, scratch_schema)

    _upgrade(tree, scratch_schema)

    rows = _rows(test_db, scratch_schema)
    print(f"[{profile}] alembic_version after upgrade heads: {sorted(rows)}")
    assert rows == ROWS[profile]

    column = _column(test_db, scratch_schema)
    print(f"[{profile}] feature_flags.{COLUMN}: {column}")
    assert column == EXPECTED_COLUMN

    values = _default_values(test_db, scratch_schema)
    print(f"[{profile}] {COLUMN} by row: {values}")
    assert values == {existing: False}

    # The column's type and nullability are the model's: autogenerate, which
    # does compare those, finds nothing to add.
    result = tree_profiles.autogenerate(
        tree, scratch_schema, tmp_path, head=tree_profiles.CORE_HEAD
    )
    assert result.body is not None, result.describe()
    assert result.operations == [], result.describe()


# ---------------------------------------------------------------------------
# 2. The migration's column and the model's are the same column
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_an_upgraded_database_and_a_bootstrapped_one_define_the_column_alike(
    profile, test_db, scratch_schema, reference_schema, tmp_path
):
    """Autogenerate compares no server default, so this is where one is checked.

    A fresh deployment gets the column from the model (``create_all``); an
    existing one gets it from this migration.  A migration without the server
    default would leave the existing population refusing the previous image's
    INSERTs while every fresh database accepted them.
    """
    tree = _bootstrapped_at_previous_release(test_db, profile, scratch_schema, tmp_path)
    _upgrade(tree, scratch_schema)

    created = tree_profiles.bootstrap_schema(tree, reference_schema)
    assert created.returncode == 0, created.stderr[-3000:]

    upgraded = _column(test_db, scratch_schema)
    bootstrapped = _column(test_db, reference_schema)
    print(f"[{profile}] upgraded:     {upgraded}")
    print(f"[{profile}] bootstrapped: {bootstrapped}")
    assert bootstrapped == EXPECTED_COLUMN
    assert upgraded == bootstrapped


# ---------------------------------------------------------------------------
# 3. The previous release's code against this head
# ---------------------------------------------------------------------------
_PREVIOUS_RELEASE_WRITES = textwrap.dedent(
    """
    import json

    from backend.app.models import register_core_models

    register_core_models()

    from backend.app.crud.crud_feature_flag import crud_feature_flag
    from backend.app.db.session import SessionLocal
    from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus

    # The previous release's model: it does not know the column.
    assert "default_value" not in FeatureFlag.__table__.columns

    db = SessionLocal()
    flag = FeatureFlag(
        key="previous-release-flag",
        name="Previous release",
        status=FeatureFlagStatus.INACTIVE,
        owner_id=None,
    )
    db.add(flag)
    db.commit()
    db.refresh(flag)
    flag.rollout_percentage = 25
    db.commit()
    db.refresh(flag)
    items = crud_feature_flag.get_multi(db, skip=0, limit=100)
    print(json.dumps({
        "id": str(flag.id),
        "rollout_percentage": flag.rollout_percentage,
        "listed": [item.key for item in items],
    }))
    """
)


def _previous_release(profile: str, tmp_path):
    """Release N-1: a copy of this tree without the revision and the column.

    The revisions after this one go too: they are newer than release N-1, and
    one whose ``down_revision`` file is gone would break the script directory.
    """
    destination = tmp_path / "release-n-minus-1"
    if profile == FULL:
        tree = tree_profiles.full_tree(destination)
    else:
        tree = tree_profiles.core_tree(destination)
    versions = tree / "backend" / "app" / "db" / "migrations" / "versions"
    for newer in (REVISION, CORE_HEAD):
        (revision_file,) = versions.glob(f"{newer}_*.py")
        revision_file.unlink()
    model = tree / "backend" / "app" / "models" / "feature_flag.py"
    source, found = _MODEL_COLUMN.subn("", model.read_text(encoding="utf-8"))
    assert found == 1, f"{found} default_value declarations: update _MODEL_COLUMN"
    model.write_text(source, encoding="utf-8")
    return tree


@pytest.mark.regression
@pytest.mark.parametrize("built_by", [UPGRADED, BOOTSTRAPPED])
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_the_previous_release_writes_and_lists_flags_on_this_schema(
    profile, built_by, test_db, scratch_schema, tmp_path
):
    if built_by == UPGRADED:
        release_n = _bootstrapped_at_previous_release(
            test_db, profile, scratch_schema, tmp_path
        )
        _upgrade(release_n, scratch_schema)
    else:
        release_n = tree_profiles.tree_for(profile, tmp_path)
        built = tree_profiles.bootstrap_schema(release_n, scratch_schema)
        assert built.returncode == 0, built.stderr[-3000:]
    assert _rows(test_db, scratch_schema) == ROWS[profile]
    assert _column(test_db, scratch_schema) is not None
    release_n_minus_1 = _previous_release(profile, tmp_path)

    script = tmp_path / "previous_release_writes.py"
    script.write_text(_PREVIOUS_RELEASE_WRITES, encoding="utf-8")
    result = tree_profiles.run(release_n_minus_1, [str(script)], scratch_schema)
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-4000:]

    report = json.loads(result.stdout.strip().splitlines()[-1])
    print(f"[{profile}/{built_by}] previous release wrote: {report}")
    assert report["rollout_percentage"] == 25
    assert report["listed"] == ["previous-release-flag"]
    # The database filled the column the previous release did not name.
    assert _default_values(test_db, scratch_schema) == {report["id"]: False}
    shutil.rmtree(release_n_minus_1, ignore_errors=True)


# ---------------------------------------------------------------------------
# 4. Downgrade
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_downgrade_removes_the_column_and_nothing_else(
    profile, test_db, scratch_schema, tmp_path
):
    tree = tree_profiles.tree_for(profile, tmp_path)
    assert tree_profiles.bootstrap_schema(tree, scratch_schema).returncode == 0
    tables = set(inspect(test_db).get_table_names(schema=scratch_schema))
    columns = {
        c["name"]
        for c in inspect(test_db).get_columns("feature_flags", schema=scratch_schema)
    }
    assert COLUMN in columns

    down = tree_profiles.alembic(tree, scratch_schema, "downgrade", PREVIOUS_CORE_HEAD)
    assert down.returncode == 0, down.stderr[-3000:]

    print(
        f"[{profile}] alembic_version after downgrade: {_rows(test_db, scratch_schema)}"
    )
    assert _rows(test_db, scratch_schema) == PREVIOUS_ROWS[profile]
    assert _column(test_db, scratch_schema) is None
    after = {
        c["name"]
        for c in inspect(test_db).get_columns("feature_flags", schema=scratch_schema)
    }
    assert after == columns - {COLUMN} - LATER_DOWNGRADE_COLUMNS
    assert (
        set(inspect(test_db).get_table_names(schema=scratch_schema))
        == tables - LATER_DOWNGRADE_TABLES
    )
