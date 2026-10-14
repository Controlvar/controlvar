"""``4c2c09a2b3e8``: a rollback record's safety configuration is optional, and deleting one keeps the records (#1206).

A rollback no longer gives a flag without a safety configuration of its own an
empty one, so the record of such a rollback has no configuration to point at.
The revision makes ``safety_rollback_records.safety_config_id`` nullable and
its foreign key ON DELETE SET NULL (it was NOT NULL and ON DELETE CASCADE), each
only when needed. It changes no row. Its downgrade changes nothing.

A schema built by ``create_all`` already has both, so the tests that start from
the previous release (``37dcb2969766``) turn a bootstrapped schema back into
what that release built -- NOT NULL, ON DELETE CASCADE, ``alembic_version`` at
``37dcb2969766`` -- in each profile, and run the documented ``alembic upgrade
heads``. Then:

1. the ``alembic_version`` rows are this release's, the column is nullable and
   the key is ON DELETE SET NULL (``confdeltype = 'n'``), no stored row
   changed, and autogenerate against the result is empty;
2. a database bootstrapped from the models has the same column and key, with
   the same name and definition: a fresh deployment gets them from the model
   and an upgraded one from this revision;
3. deleting a configuration, with SQL on an upgraded database or through the
   ORM, keeps the flag's rollback records and clears their link;
4. the downgrade changes nothing and its docstring says why: the runbook runs
   it while this release still serves, and this release's rollback of a flag
   on the defaults, run against the downgraded database, still succeeds;
5. ``--sql`` is refused: the upgrade reads the key's current rule first.

Each test prints the ``alembic_version`` rows and the key it saw.
"""

from __future__ import annotations

import importlib.util
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.app.db import bootstrap
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.safety import FeatureFlagSafetyConfig, SafetyRollbackRecord
from backend.app.services.safety_service import SafetyService
from backend.tests.integration.database import tree_profiles
from backend.tests.integration.database.tree_profiles import CORE, FULL

pytestmark = [pytest.mark.integration]

#: This revision, and the core revision it extends.
REVISION = "4c2c09a2b3e8"
PREVIOUS_CORE_HEAD = "37dcb2969766"
#: The core head of this tree.
CORE_HEAD = "4c2c09a2b3e8"
MODULES_HEAD = "modules_0002_warehouse_analysis"

PREVIOUS_ROWS = {
    CORE: {PREVIOUS_CORE_HEAD},
    FULL: {PREVIOUS_CORE_HEAD, MODULES_HEAD},
}
ROWS = {
    CORE: {CORE_HEAD},
    FULL: {CORE_HEAD, MODULES_HEAD},
}

RECORDS = "safety_rollback_records"
CONFIGS = "feature_flag_safety_configs"
FOREIGN_KEY = "safety_rollback_records_safety_config_id_fkey"

#: The column and its key after this revision, whether a database was
#: upgraded or bootstrapped: (is_nullable, confdeltype, name, definition).
LINK = (
    "YES",
    "n",
    FOREIGN_KEY,
    "FOREIGN KEY (safety_config_id) REFERENCES <schema>.feature_flag_safety_configs(id) "
    "ON DELETE SET NULL",
)
#: What the previous release built.
PREVIOUS_LINK = (
    "NO",
    "c",
    FOREIGN_KEY,
    "FOREIGN KEY (safety_config_id) REFERENCES <schema>.feature_flag_safety_configs(id) "
    "ON DELETE CASCADE",
)

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
    yield from _scratch(test_db, "rbcfg")


@pytest.fixture
def reference_schema(test_db):
    yield from _scratch(test_db, "rbcfgref")


def _rows(engine, schema: str) -> set[str]:
    return bootstrap.recorded_revisions(engine, schema)


def _link(engine, schema: str):
    """(is_nullable, confdeltype, name, definition) of ``safety_config_id``."""
    with engine.connect() as conn:
        nullable = conn.execute(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_schema = :s AND table_name = :t "
                "AND column_name = 'safety_config_id'"
            ),
            {"s": schema, "t": RECORDS},
        ).scalar_one()
        (key,) = conn.execute(
            text(
                "SELECT c.confdeltype, c.conname, pg_get_constraintdef(c.oid) "
                "FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid "
                "JOIN pg_namespace n ON n.oid = t.relnamespace "
                "JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY (c.conkey) "
                "WHERE n.nspname = :s AND t.relname = :t AND c.contype = 'f' "
                "AND a.attname = 'safety_config_id'"
            ),
            {"s": schema, "t": RECORDS},
        ).all()
    confdeltype, name, definition = key
    return (nullable, confdeltype, name, definition.replace(schema, "<schema>"))


def _constraints(engine, schema: str) -> dict[str, str]:
    """Every constraint on the records table: name -> definition."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT c.conname, pg_get_constraintdef(c.oid) "
                "FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid "
                "JOIN pg_namespace n ON n.oid = t.relnamespace "
                "WHERE n.nspname = :s AND t.relname = :t"
            ),
            {"s": schema, "t": RECORDS},
        ).all()
    return {name: definition.replace(schema, "<schema>") for name, definition in rows}


def _stored(engine, schema: str) -> dict[str, list]:
    """Every row of the two tables, in id order."""
    with engine.connect() as conn:
        return {
            table: [
                tuple(row)
                for row in conn.execute(
                    text(f'SELECT * FROM "{schema}".{table} ORDER BY id')
                ).all()
            ]
            for table in (CONFIGS, RECORDS)
        }


def _at_previous_release(engine, schema: str, rows: set[str]) -> None:
    """Turn a bootstrapped *schema* into what ``37dcb2969766`` left."""
    with engine.begin() as conn:
        conn.execute(
            text(
                f'ALTER TABLE "{schema}".{RECORDS} DROP CONSTRAINT IF EXISTS {FOREIGN_KEY}'
            )
        )
        conn.execute(
            text(
                f'ALTER TABLE "{schema}".{RECORDS} '
                "ALTER COLUMN safety_config_id SET NOT NULL"
            )
        )
        conn.execute(
            text(
                f'ALTER TABLE "{schema}".{RECORDS} ADD CONSTRAINT {FOREIGN_KEY} '
                f'FOREIGN KEY (safety_config_id) REFERENCES "{schema}".{CONFIGS}(id) '
                "ON DELETE CASCADE"
            )
        )
        conn.execute(text(f'DELETE FROM "{schema}".alembic_version'))
        for revision in sorted(rows):
            conn.execute(
                text(f'INSERT INTO "{schema}".alembic_version VALUES (:rev)'),
                {"rev": revision},
            )
    assert _link(engine, schema) == PREVIOUS_LINK
    assert _rows(engine, schema) == rows


def _bootstrapped(test_db, profile, schema, tmp_path):
    tree = tree_profiles.tree_for(profile, tmp_path)
    created = tree_profiles.bootstrap_schema(tree, schema)
    assert created.returncode == 0, created.stderr[-3000:]
    return tree


def _bootstrapped_at_previous_release(test_db, profile, schema, tmp_path):
    tree = _bootstrapped(test_db, profile, schema, tmp_path)
    _at_previous_release(test_db, schema, PREVIOUS_ROWS[profile])
    return tree


def _upgrade(tree, schema: str) -> None:
    upgrade = tree_profiles.alembic(tree, schema, "upgrade", "heads")
    assert upgrade.returncode == 0, upgrade.stderr[-3000:]


def _session(engine, schema: str) -> Session:
    """A session whose models read and write the scratch *schema*."""
    models_schema = FeatureFlag.__table__.schema
    return Session(
        bind=engine.execution_options(schema_translate_map={models_schema: schema}),
        autoflush=False,
    )


def _flag(db: Session, percentage: int = 50) -> FeatureFlag:
    flag = FeatureFlag(
        key=f"rbcfg-{uuid.uuid4().hex[:10]}",
        name="rbcfg",
        status=FeatureFlagStatus.ACTIVE,
        rollout_percentage=percentage,
    )
    db.add(flag)
    db.flush()
    return flag


def _write_as_the_previous_release(engine, schema: str) -> dict:
    """What a release up to 0.31 stored for two rolled-back flags.

    Its rollback added ``FeatureFlagSafetyConfig(metrics={})`` to a flag that
    had no configuration, flushed it, then wrote the record pointing at it; a
    flag with its own configuration got a record pointing at that. Both are
    written that way here, on the real models.
    """
    with _session(engine, schema) as db:
        on_defaults = _flag(db)
        added = FeatureFlagSafetyConfig(feature_flag_id=on_defaults.id, metrics={})
        db.add(added)
        db.flush()
        own_flag = _flag(db)
        own = FeatureFlagSafetyConfig(
            feature_flag_id=own_flag.id,
            metrics={"error_rate": {"critical_threshold": 0.2}},
            rollback_percentage=5,
        )
        db.add(own)
        db.flush()
        for flag, config in ((on_defaults, added), (own_flag, own)):
            db.add(
                SafetyRollbackRecord(
                    feature_flag_id=flag.id,
                    safety_config_id=config.id,
                    trigger_type="manual",
                    trigger_reason="written as the previous release writes it",
                    previous_percentage=50,
                    target_percentage=0,
                    success=True,
                )
            )
        db.commit()
        return {"added": added.id, "own": own.id}


def _report(profile, label, engine, schema):
    print(f"[{profile}] {label}: alembic_version={sorted(_rows(engine, schema))}")
    print(f"[{profile}] {label}: safety_config_id={_link(engine, schema)}")


# ---------------------------------------------------------------------------
# 1. The previous release, upgraded with `heads`
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_a_database_at_the_previous_release_upgraded_with_heads(
    profile, test_db, scratch_schema, tmp_path
):
    tree = _bootstrapped_at_previous_release(test_db, profile, scratch_schema, tmp_path)
    _write_as_the_previous_release(test_db, scratch_schema)
    before = _stored(test_db, scratch_schema)
    _report(profile, "at the previous release", test_db, scratch_schema)

    _upgrade(tree, scratch_schema)
    _report(profile, "after upgrade heads", test_db, scratch_schema)

    assert _rows(test_db, scratch_schema) == ROWS[profile]
    assert _link(test_db, scratch_schema) == LINK
    # Schema only: every configuration and record is as it was.
    assert _stored(test_db, scratch_schema) == before
    assert [len(before[CONFIGS]), len(before[RECORDS])] == [2, 2]

    result = tree_profiles.autogenerate(
        tree, scratch_schema, tmp_path, head=tree_profiles.CORE_HEAD
    )
    assert result.body is not None, result.describe()
    assert result.operations == [], result.describe()


# ---------------------------------------------------------------------------
# 2. A bootstrapped database has the same column and key
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_a_bootstrapped_database_and_an_upgraded_one_have_the_same_link(
    profile, test_db, scratch_schema, reference_schema, tmp_path
):
    """A fresh deployment gets the key from the model (``create_all``), which
    is what production's first deploy runs; an existing one from this
    revision. Both must keep rollback records when a configuration goes."""
    tree = _bootstrapped_at_previous_release(test_db, profile, scratch_schema, tmp_path)
    _upgrade(tree, scratch_schema)
    created = tree_profiles.bootstrap_schema(tree, reference_schema)
    assert created.returncode == 0, created.stderr[-3000:]

    _report(profile, "upgraded", test_db, scratch_schema)
    _report(profile, "bootstrapped", test_db, reference_schema)
    assert _link(test_db, reference_schema) == LINK
    assert _link(test_db, scratch_schema) == LINK
    assert _constraints(test_db, scratch_schema) == _constraints(
        test_db, reference_schema
    )


# ---------------------------------------------------------------------------
# 3. Deleting a configuration keeps the records
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_an_sql_delete_of_a_configuration_took_its_records_and_now_keeps_them(
    profile, test_db, scratch_schema, tmp_path
):
    tree = _bootstrapped_at_previous_release(test_db, profile, scratch_schema, tmp_path)
    ids = _write_as_the_previous_release(test_db, scratch_schema)

    def delete(config_id):
        with test_db.begin() as conn:
            conn.execute(
                text(f'DELETE FROM "{scratch_schema}".{CONFIGS} WHERE id = :id'),
                {"id": config_id},
            )

    def links():
        with test_db.connect() as conn:
            return sorted(
                conn.execute(
                    text(f'SELECT safety_config_id FROM "{scratch_schema}".{RECORDS}')
                ).scalars(),
                key=str,
            )

    # At the previous release, the delete takes the record with it.
    delete(ids["own"])
    assert links() == [ids["added"]]

    _upgrade(tree, scratch_schema)

    # After the upgrade, the record stays and its link is cleared.
    delete(ids["added"])
    assert _stored(test_db, scratch_schema)[CONFIGS] == []
    assert links() == [None]


def _config_with_a_record(db) -> tuple:
    flag = FeatureFlag(
        key=f"rbcfg-{uuid.uuid4().hex[:10]}",
        name="rbcfg",
        status=FeatureFlagStatus.ACTIVE,
        rollout_percentage=50,
    )
    db.add(flag)
    db.flush()
    config = FeatureFlagSafetyConfig(feature_flag_id=flag.id, metrics={})
    db.add(config)
    db.flush()
    record = SafetyRollbackRecord(
        feature_flag_id=flag.id,
        safety_config_id=config.id,
        trigger_type="manual",
        trigger_reason="rbcfg",
        previous_percentage=50,
        target_percentage=0,
        success=True,
    )
    db.add(record)
    db.commit()
    return flag, config, record.id


def _record_link(db, record_id):
    db.expire_all()
    rows = (
        db.query(SafetyRollbackRecord.safety_config_id)
        .filter(SafetyRollbackRecord.id == record_id)
        .all()
    )
    return [row[0] for row in rows]


def _remove(db, flag) -> None:
    db.rollback()
    db.query(SafetyRollbackRecord).filter(
        SafetyRollbackRecord.feature_flag_id == flag.id
    ).delete(synchronize_session=False)
    db.query(FeatureFlagSafetyConfig).filter(
        FeatureFlagSafetyConfig.feature_flag_id == flag.id
    ).delete(synchronize_session=False)
    db.query(FeatureFlag).filter(FeatureFlag.id == flag.id).delete(
        synchronize_session=False
    )
    db.commit()


@pytest.mark.regression
@pytest.mark.parametrize("records_loaded", [False, True], ids=["unloaded", "loaded"])
def test_an_orm_delete_of_a_configuration_keeps_its_records(db_session, records_loaded):
    """``db.delete(config)``: the relationship has no delete cascade, so the
    record stays and its link is cleared, whether or not the session had
    loaded the configuration's records."""
    flag, config, record_id = _config_with_a_record(db_session)
    try:
        if records_loaded:
            assert [r.id for r in config.rollback_records] == [record_id]

        db_session.delete(config)
        db_session.commit()

        assert _record_link(db_session, record_id) == [None]
    finally:
        _remove(db_session, flag)


@pytest.mark.regression
def test_an_sql_delete_of_a_configuration_keeps_its_records(db_session):
    """The same through SQL, on the schema the test run built from the models."""
    flag, config, record_id = _config_with_a_record(db_session)
    try:
        db_session.query(FeatureFlagSafetyConfig).filter(
            FeatureFlagSafetyConfig.id == config.id
        ).delete(synchronize_session=False)
        db_session.commit()

        assert _record_link(db_session, record_id) == [None]
    finally:
        _remove(db_session, flag)


# ---------------------------------------------------------------------------
# 4. Downgrade
# ---------------------------------------------------------------------------
def _load_revision():
    path = (
        tree_profiles.REPO_ROOT
        / "backend/app/db/migrations/versions"
        / "4c2c09a2b3e8_rollback_records_keep_without_a_safety_config.py"
    )
    spec = importlib.util.spec_from_file_location("revision_4c2c09a2b3e8", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_the_downgrade_changes_nothing_and_a_rollback_still_succeeds(
    profile, test_db, scratch_schema, tmp_path
):
    """The runbook undoes a migration while the release that contains it still
    serves. That release rolls back a flag on the defaults with a NULL
    ``safety_config_id``; a downgrade back to NOT NULL would make every such
    rollback fail."""
    tree = _bootstrapped_at_previous_release(test_db, profile, scratch_schema, tmp_path)
    _upgrade(tree, scratch_schema)
    _write_as_the_previous_release(test_db, scratch_schema)
    upgraded = _stored(test_db, scratch_schema)

    down = tree_profiles.alembic(tree, scratch_schema, "downgrade", PREVIOUS_CORE_HEAD)
    _report(profile, "after downgrade", test_db, scratch_schema)
    assert down.returncode == 0, down.stderr[-3000:]
    assert _rows(test_db, scratch_schema) == PREVIOUS_ROWS[profile]
    downgraded = (_link(test_db, scratch_schema), _stored(test_db, scratch_schema))

    # This release, against the downgraded database: a rollback of a flag
    # with no configuration of its own.
    with _session(test_db, scratch_schema) as db:
        flag = _flag(db)
        db.commit()
        result = SafetyService(db).execute_rollback(db, flag.id, reason="rbcfg")
        assert result.success is True, (
            f"this release's rollback fails against the downgraded database: "
            f"{result.message}"
        )
        db.expire_all()
        assert db.get(FeatureFlag, flag.id).status == FeatureFlagStatus.INACTIVE
        (record,) = (
            db.query(SafetyRollbackRecord)
            .filter(SafetyRollbackRecord.feature_flag_id == flag.id)
            .all()
        )
        assert record.safety_config_id is None
        assert (
            db.query(FeatureFlagSafetyConfig)
            .filter(FeatureFlagSafetyConfig.feature_flag_id == flag.id)
            .count()
            == 0
        )

    # The downgrade changed nothing but alembic_version, and says why.
    assert downgraded == (LINK, upgraded)
    assert "Only ``alembic_version`` moves back" in _load_revision().downgrade.__doc__

    # And the upgrade after it finds nothing to do.
    _upgrade(tree, scratch_schema)
    assert _rows(test_db, scratch_schema) == ROWS[profile]
    assert _link(test_db, scratch_schema) == LINK


# ---------------------------------------------------------------------------
# 5. Offline mode
# ---------------------------------------------------------------------------
def test_offline_mode_is_refused(test_db, scratch_schema, tmp_path):
    tree = _bootstrapped_at_previous_release(test_db, CORE, scratch_schema, tmp_path)

    offline = tree_profiles.alembic(
        tree, scratch_schema, "upgrade", f"{PREVIOUS_CORE_HEAD}:{REVISION}", "--sql"
    )

    assert offline.returncode != 0
    assert "cannot be emitted as SQL; run it online" in offline.stderr
    assert _rows(test_db, scratch_schema) == PREVIOUS_ROWS[CORE]
    assert _link(test_db, scratch_schema) == PREVIOUS_LINK
