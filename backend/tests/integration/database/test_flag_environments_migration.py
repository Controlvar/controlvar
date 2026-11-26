"""``t2011_flag_environments``: staging and development settings, and each key's environment, through the transitions (#2011).

The revision adds the table ``flag_environment_settings`` and its index, then
``api_keys.environment`` and its check, each only when absent, and changes
nothing else. A schema built by ``create_all`` already has all of them, so the
tests that start from the previous release (0.48.0, whose core head is
``t449_identity_aliases``) take a bootstrapped schema with the table, the
check and the column DROPPED and ``alembic_version`` at the previous release's
heads, in each profile, and run the documented commands. Then:

1. ``alembic upgrade heads``: the ``alembic_version`` rows are this release's
   (printed), the side table is empty, every key the previous release stored
   is a production key, flags, schedules and safety configurations keep their
   counts, and autogenerate is empty;
2. the upgraded database and a ``create_all``-bootstrapped one have the same
   side table and the same ``api_keys`` column and check, from the catalog;
3. every index and constraint of the tables the previous release relies on
   (``feature_flags``, its safety configurations, the evaluation counters,
   ``api_keys``) is unchanged but for the one check added; a key inserted
   without ``environment`` is a production key, and the checks refuse what
   the design refuses;
4. the statements each direction runs, captured online: the upgrade's two
   blocks (the side table under 500 ms, then the ``api_keys`` steps, last,
   under 1500 ms) with no data step, and the downgrade's two blocks
   (``api_keys`` under 1500 ms, then the side table under 500 ms);
5. the waits, each leg naming its revision, never ``heads``, and starting
   from that revision's own rows (``REVISION_ROWS``): a request holding
   ``api_keys`` and a session writing to ``feature_flags`` or analyzing it
   each end the upgrade at their block's bound with this revision's refusal,
   applying nothing, and the next run goes through; readers of
   ``feature_flags`` and the other SDK tables do not stop it; while it waits
   for ``feature_flags`` a key lookup still runs; a reader of any table the
   downgrade locks ends it with its refusal, changing nothing; a downgrade
   waits for each lock it needs on its own bound, so it can wait for more
   than one; and a deadlock with a session updating a key ends it with the
   same refusal;
6. the previous release's code (this tree without the revision, the models'
   additions and the environment routes, where every key is a production key
   and nothing reads or writes a side row) creates, sets, lists, builds the
   ruleset and evaluates on this schema exactly as this release does, through
   its routes too, ignoring a staging row; this release turns the flag off
   through the stable PUT, and through production's environment PUT, and the
   previous release answers ``inactive`` each time;
7. the staging-key leg: the previous release treats a staging key as a
   production key: production's configuration on ``/evaluate``, its server
   error report counted, its evaluation counts recorded, and its assignment,
   event and identity link written into production;
8. offline (``--sql``), both directions refuse and emit nothing;
9. online, the downgrade deactivates every staging and development key, then
   drops the check, the column and the table; the previous release then
   answers the production key and refuses the former staging key, and an
   upgrade afterwards brings the former staging key back an inactive
   production key with no settings;
10. this release, still running against the downgraded schema, answers 500 to
    a production key on ``/evaluate`` and ``/sdk/ruleset``;
11. the upgrade changes no answer: the contract vectors' flags, stored by the
    previous release, are answered as before to a key it created, in a
    ``development`` and in a ``production`` deployment, and the ruleset and
    its ETag are the same bytes;
12. ``alembic heads`` names exactly the core head (``t2012_flag_split`` since
    #2012, which extends this revision) and the modules head.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import re
import secrets
import shutil
import subprocess
import sys
import textwrap
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List

import pytest
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.core.security import get_password_hash, hash_api_key
from backend.app.db import bootstrap
from backend.app.models.api_key import generate_api_key
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.metrics.metric import ErrorLog, counted_error_filter
from backend.app.models.rollout_schedule import RolloutSchedule
from backend.app.models.safety import FeatureFlagSafetyConfig
from backend.app.models.segment import (
    Segment,
    SegmentKind,
    SegmentMember,
    SegmentStatus,
)
from backend.app.models.user import User, UserRole
from backend.app.services.feature_flag_service import FeatureFlagService
from backend.scripts.generate_ruleset_vectors import (
    VECTOR_SEGMENT,
    VECTOR_SEGMENT_MEMBERS,
    VECTORS_PATH,
)
from backend.tests.integration.database import tree_profiles
from backend.tests.integration.database.split_read_path import without_the_read_path
from backend.tests.integration.database.tree_profiles import CORE, FULL
from backend.tests.unit.services.test_sdk_ruleset_vectors import (
    without_split_or_rule_value,
)

pytestmark = [pytest.mark.integration]

#: This revision, and the core revision it extends (0.48.0's core head).
REVISION = "t2011_flag_environments"
PREVIOUS_CORE_HEAD = "t449_identity_aliases"
#: The core head of this tree: ``t2012_flag_split`` (#2012), the revision after
#: this one, whose downgrade drops ``feature_flags.split`` and
#: ``flag_environment_settings.split``.
CORE_HEAD = "t2012_flag_split"
MODULES_HEAD = "modules_0005_redshift"
#: The core revisions after this one, which the previous release does not have
#: either, and the side table's columns they add: this file compares what this
#: revision makes, and ``test_flag_split_migration.py`` compares ``split``.
LATER_REVISIONS = ("t2012_flag_split",)
LATER_COLUMNS = {"split"}

PREVIOUS_ROWS = {
    CORE: {PREVIOUS_CORE_HEAD},
    FULL: {PREVIOUS_CORE_HEAD, MODULES_HEAD},
}
ROWS = {CORE: {CORE_HEAD}, FULL: {CORE_HEAD, MODULES_HEAD}}
#: The rows after ``upgrade t2011_flag_environments`` and no further, and
#: where every downgrade leg starts: this revision's own, whatever later
#: revision the tree's head is.
REVISION_ROWS = {CORE: {REVISION}, FULL: {REVISION, MODULES_HEAD}}

TABLE = "flag_environment_settings"
KEYS = "api_keys"
COLUMN = "environment"
KEY_CHECK = "check_api_key_environment"
VERSIONS = (
    Path(__file__).resolve().parents[3] / "app" / "db" / "migrations" / "versions"
)

#: The side table as PostgreSQL describes it after either path: each column's
#: (``format_type``, NOT NULL, ``pg_get_expr`` of its default), in order.
EXPECTED_COLUMNS = [
    ("feature_flag_id", "uuid", True, None),
    ("environment", "character varying(32)", True, None),
    ("is_active", "boolean", True, "false"),
    ("targeting_rules", "jsonb", False, None),
    ("rollout_percentage", "integer", True, "0"),
    ("id", "uuid", True, None),
    ("created_at", "timestamp without time zone", True, None),
    ("updated_at", "timestamp without time zone", True, None),
]
#: Its constraints: the primary key, the two named checks, the unique pair and
#: the cascading foreign key to ``feature_flags``.
EXPECTED_CONSTRAINTS = {
    ("flag_environment_settings_pkey", "p", "PRIMARY KEY (id)"),
    (
        "check_flag_environment_name",
        "c",
        "CHECK (((environment)::text = ANY ((ARRAY['staging'::character varying, "
        "'development'::character varying])::text[])))",
    ),
    (
        "check_flag_environment_rollout",
        "c",
        "CHECK (((rollout_percentage >= 0) AND (rollout_percentage <= 100)))",
    ),
    ("uq_flag_environment", "u", "UNIQUE (feature_flag_id, environment)"),
    (
        "flag_environment_settings_feature_flag_id_fkey",
        "f",
        "FOREIGN KEY (feature_flag_id) REFERENCES <schema>.feature_flags(id) "
        "ON DELETE CASCADE",
    ),
}
EXPECTED_INDEXES = {
    "CREATE UNIQUE INDEX flag_environment_settings_pkey ON "
    "<schema>.flag_environment_settings USING btree (id)",
    "CREATE UNIQUE INDEX uq_flag_environment ON <schema>.flag_environment_settings "
    "USING btree (feature_flag_id, environment)",
    "CREATE INDEX ix_<schema>_flag_environment_settings_created_at ON "
    "<schema>.flag_environment_settings USING btree (created_at)",
}
#: ``api_keys.environment`` after either path, and its check.
EXPECTED_KEY_COLUMN = ("character varying(32)", True, "'production'::character varying")
EXPECTED_KEY_CHECK = (
    "CHECK (((environment)::text = ANY ((ARRAY['production'::character varying, "
    "'staging'::character varying, 'development'::character varying])::text[])))"
)

#: The tables the previous release relies on whose indexes and constraints
#: this revision must leave as they were.
KEPT_TABLES = (
    "feature_flags",
    "feature_flag_safety_configs",
    "sdk_evaluation_flag_counts",
    "sdk_evaluation_key_counts",
    "api_keys",
)

#: Each online leg's own bound on any one statement: a wait the revision did
#: not bound ends here (57014), never in a hung test.
LEG_STATEMENT_TIMEOUT = "-c statement_timeout=10s"

#: The full-profile case needs a ``modules/`` directory; the core one builds
#: its own sealed tree and runs in either checkout.
BOTH_PROFILES = [CORE, pytest.param(FULL, marks=pytest.mark.modules)]

UPGRADED = "upgraded"
BOOTSTRAPPED = "bootstrapped"


def _scratch(test_db, prefix: str):
    schema = f"{prefix}_{uuid.uuid4().hex[:8]}"
    try:
        yield schema
    finally:
        with test_db.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


@pytest.fixture
def scratch_schema(test_db):
    yield from _scratch(test_db, "flagenv")


@pytest.fixture
def reference_schema(test_db):
    yield from _scratch(test_db, "flagenvref")


# ---------------------------------------------------------------------------
# Reading the catalog and the rows
# ---------------------------------------------------------------------------
def _rows(engine, schema: str) -> set[str]:
    return bootstrap.recorded_revisions(engine, schema)


def _tables(engine, schema: str) -> set[str]:
    return set(inspect(engine).get_table_names(schema=schema))


def _columns(engine, schema: str, table: str = TABLE) -> list[tuple]:
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
            {"s": schema, "t": table},
        ).all()
    return [tuple(row) for row in rows]


def _key_column(engine, schema: str):
    """``api_keys.environment`` as (format_type, NOT NULL, default), or None."""
    found = [row[1:] for row in _columns(engine, schema, KEYS) if row[0] == COLUMN]
    return found[0] if found else None


def _constraints(engine, schema: str, table: str = TABLE) -> set[tuple]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT c.conname, c.contype, pg_get_constraintdef(c.oid) "
                "FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid "
                "JOIN pg_namespace n ON n.oid = t.relnamespace "
                "WHERE n.nspname = :s AND t.relname = :t"
            ),
            {"s": schema, "t": table},
        ).all()
    return {
        (name, kind, definition.replace(schema, "<schema>"))
        for name, kind, definition in rows
    }


def _indexes(engine, schema: str, table: str = TABLE) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT indexdef FROM pg_indexes WHERE schemaname = :s AND tablename = :t"
            ),
            {"s": schema, "t": table},
        ).scalars()
        return {row.replace(schema, "<schema>") for row in rows}


def _definition(engine, schema: str) -> tuple:
    """What this revision makes; a later revision's column of the side table is
    left out (its place differs by path: last in an upgraded database)."""
    return (
        [c for c in _columns(engine, schema) if c[0] not in LATER_COLUMNS],
        _constraints(engine, schema),
        _indexes(engine, schema),
        _key_column(engine, schema),
        {c for c in _constraints(engine, schema, KEYS) if c[0] == KEY_CHECK},
    )


def _kept_definitions(engine, schema: str) -> set[tuple]:
    """Every index and constraint of :data:`KEPT_TABLES`, schema-neutral."""
    found = set()
    for table in KEPT_TABLES:
        found |= {
            (table, "constraint", *c) for c in _constraints(engine, schema, table)
        }
        found |= {(table, "index", i) for i in _indexes(engine, schema, table)}
    return found


def _count(engine, schema: str, table: str) -> int:
    with engine.connect() as conn:
        return conn.execute(text(f'SELECT count(*) FROM "{schema}".{table}')).scalar()


def _key_environments(engine, schema: str) -> dict[str, tuple]:
    """Each key's id -> (environment, is_active)."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(f'SELECT id::text, environment, is_active FROM "{schema}".api_keys')
        ).all()
    return {key_id: (environment, active) for key_id, environment, active in rows}


def _stamp(engine, schema: str, rows: set[str]) -> None:
    with engine.begin() as conn:
        conn.execute(text(f'DELETE FROM "{schema}".alembic_version'))
        for revision in sorted(rows):
            conn.execute(
                text(f'INSERT INTO "{schema}".alembic_version VALUES (:rev)'),
                {"rev": revision},
            )


def _session(engine, schema: str) -> Session:
    """A session whose models read and write the scratch *schema*."""
    models_schema = FeatureFlag.__table__.schema
    return Session(
        bind=engine.execution_options(schema_translate_map={models_schema: schema}),
        autoflush=False,
        expire_on_commit=False,
    )


def _report(profile, label, engine, schema) -> None:
    print(f"[{profile}] {label}: alembic_version={sorted(_rows(engine, schema))}")
    print(
        f"[{profile}] {label}: {TABLE} present={TABLE in _tables(engine, schema)}, "
        f"api_keys.environment={_key_column(engine, schema)}"
    )


# ---------------------------------------------------------------------------
# Building the databases
# ---------------------------------------------------------------------------
def _bootstrapped(profile, schema, tmp_path):
    tree = tree_profiles.tree_for(profile, tmp_path)
    created = tree_profiles.bootstrap_schema(tree, schema)
    assert created.returncode == 0, created.stderr[-3000:]
    return tree


def _to_previous_release(engine, profile, schema) -> None:
    """Turn a bootstrapped *schema* into what 0.48.0 left: no side table, no
    key column or check, nothing a later revision adds, 0.48.0's heads."""
    with engine.begin() as conn:
        # IF EXISTS: a plant that removes them from the model leaves create_all
        # without them, and the test must reach the assertion that says so.
        conn.execute(text(f'DROP TABLE IF EXISTS "{schema}".{TABLE}'))
        conn.execute(
            text(f'ALTER TABLE "{schema}".feature_flags DROP COLUMN IF EXISTS split')
        )
        conn.execute(
            text(
                f'ALTER TABLE "{schema}".api_keys DROP CONSTRAINT IF EXISTS {KEY_CHECK}'
            )
        )
        conn.execute(
            text(f'ALTER TABLE "{schema}".api_keys DROP COLUMN IF EXISTS {COLUMN}')
        )
    _stamp(engine, schema, PREVIOUS_ROWS[profile])
    assert TABLE not in _tables(engine, schema)
    assert _key_column(engine, schema) is None


def _at_previous_release(test_db, profile, schema, tmp_path):
    tree = _bootstrapped(profile, schema, tmp_path)
    _to_previous_release(test_db, profile, schema)
    return tree


def _at_this_revision(test_db, profile, schema, tmp_path):
    """A schema whose ``alembic_version`` is this revision's, named, however
    far the tree's head is beyond it: a downgrade from here runs this
    revision's downgrade and no other. ``create_all`` built the head's
    objects; a later revision's stay, but for its column of the side table,
    which goes with the table."""
    tree = _bootstrapped(profile, schema, tmp_path)
    _stamp(test_db, schema, REVISION_ROWS[profile])
    assert _rows(test_db, schema) == REVISION_ROWS[profile]
    return tree


def _upgrade(tree, schema: str, **extra_env) -> subprocess.CompletedProcess:
    return tree_profiles.alembic(
        tree, schema, "upgrade", "heads", extra_env=extra_env or None
    )


def _user(engine, schema: str, *, superuser: bool = True) -> User:
    with _session(engine, schema) as db:
        suffix = uuid.uuid4().hex[:8]
        user = User(
            username=f"flagenv_{suffix}",
            email=f"flagenv_{suffix}@environments.test",
            full_name="Flag environments",
            hashed_password=get_password_hash("Environments-1!"),
            is_active=True,
            is_superuser=superuser,
            role=UserRole.ADMIN,
        )
        db.add(user)
        db.commit()
        return user


def _insert_previous_release_key(engine, schema: str, user_id, scopes=None):
    """A key as 0.48.0 inserts it: no ``environment`` named. Returns
    (id, plaintext)."""
    key_id, plaintext = str(uuid.uuid4()), generate_api_key()
    with engine.begin() as conn:
        conn.execute(
            text(
                f'INSERT INTO "{schema}".api_keys (id, key, name, is_active, scopes, '
                "user_id, created_at, updated_at) VALUES (:id, :key, 'previous', "
                "true, :scopes, :user, now(), now())"
            ),
            {
                "id": key_id,
                "key": hash_api_key(plaintext),
                "scopes": scopes,
                "user": str(user_id),
            },
        )
    return key_id, plaintext


def _insert_key(engine, schema: str, user_id, environment: str, scopes=None):
    """A key with its environment named, as this release's model writes it."""
    key_id, plaintext = str(uuid.uuid4()), generate_api_key()
    with engine.begin() as conn:
        conn.execute(
            text(
                f'INSERT INTO "{schema}".api_keys (id, key, name, is_active, scopes, '
                "user_id, environment, created_at, updated_at) VALUES (:id, :key, "
                ":name, true, :scopes, :user, :env, now(), now())"
            ),
            {
                "id": key_id,
                "key": hash_api_key(plaintext),
                "name": f"{environment} key",
                "scopes": scopes,
                "user": str(user_id),
                "env": environment,
            },
        )
    return key_id, plaintext


def _insert_flag(
    engine, schema: str, key: str, status="ACTIVE", percentage=100, rules=None
) -> str:
    flag_id = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(
            text(
                f'INSERT INTO "{schema}".feature_flags (id, key, name, status, '
                "rollout_percentage, targeting_rules, created_at, updated_at) "
                "VALUES (:id, :key, :key, :status, :pct, CAST(:rules AS jsonb), "
                "now(), now())"
            ),
            {
                "id": flag_id,
                "key": key,
                "status": status,
                "pct": percentage,
                "rules": None if rules is None else json.dumps(rules),
            },
        )
    return flag_id


def _insert_setting(
    engine, schema: str, flag_id: str, environment: str, active=True, percentage=100
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                f'INSERT INTO "{schema}".{TABLE} (id, feature_flag_id, environment, '
                "is_active, rollout_percentage, created_at, updated_at) VALUES "
                "(:id, :flag, :env, :active, :pct, now(), now())"
            ),
            {
                "id": str(uuid.uuid4()),
                "flag": flag_id,
                "env": environment,
                "active": active,
                "pct": percentage,
            },
        )


def _settings(engine, schema: str) -> list[tuple]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT feature_flag_id::text, environment, is_active, "
                f'rollout_percentage, targeting_rules, updated_at FROM "{schema}".{TABLE} '
                "ORDER BY 1, 2"
            )
        ).all()
    return [tuple(row) for row in rows]


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
def _seed_previous_release(engine, schema: str) -> dict:
    """What a 0.48.0 install holds: keys, flags, a schedule, a safety config."""
    user = _user(engine, schema)
    keys = [_insert_previous_release_key(engine, schema, user.id)[0] for _ in range(3)]
    flags = [
        _insert_flag(engine, schema, "previous-on", "ACTIVE", 25),
        _insert_flag(engine, schema, "previous-off", "INACTIVE", 0),
    ]
    with _session(engine, schema) as db:
        db.add(RolloutSchedule(name="Previous schedule", feature_flag_id=flags[0]))
        db.add(FeatureFlagSafetyConfig(feature_flag_id=flags[0]))
        db.commit()
    return {"keys": keys, "flags": flags}


def _counts(engine, schema: str) -> dict[str, int]:
    return {
        table: _count(engine, schema, table)
        for table in (
            "api_keys",
            "feature_flags",
            "rollout_schedules",
            "feature_flag_safety_configs",
        )
    }


@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_a_database_at_the_previous_release_gains_the_table_and_the_column_and_no_data(
    profile, test_db, scratch_schema, tmp_path
):
    tree = _at_previous_release(test_db, profile, scratch_schema, tmp_path)
    seeded = _seed_previous_release(test_db, scratch_schema)
    counts = _counts(test_db, scratch_schema)
    _report(profile, "at the previous release", test_db, scratch_schema)

    upgrade = _upgrade(tree, scratch_schema)
    assert upgrade.returncode == 0, upgrade.stderr[-3000:]
    _report(profile, "after upgrade heads", test_db, scratch_schema)

    assert _rows(test_db, scratch_schema) == ROWS[profile]
    settings = _count(test_db, scratch_schema, TABLE)
    assert settings == 0, f"{TABLE}: {settings} rows after upgrade, expected 0"
    environments = _key_environments(test_db, scratch_schema)
    assert sorted(environments) == sorted(seeded["keys"])
    for key_id, (environment, active) in sorted(environments.items()):
        assert environment == "production", (
            f"api key {key_id}: environment {environment} after upgrade, "
            "expected production"
        )
        assert active is True
    assert (
        _counts(test_db, scratch_schema)
        == counts
        == {
            "api_keys": 3,
            "feature_flags": 2,
            "rollout_schedules": 1,
            "feature_flag_safety_configs": 1,
        }
    )
    assert _definition(test_db, scratch_schema) == (
        EXPECTED_COLUMNS,
        EXPECTED_CONSTRAINTS,
        EXPECTED_INDEXES,
        EXPECTED_KEY_COLUMN,
        {(KEY_CHECK, "c", EXPECTED_KEY_CHECK)},
    )

    result = tree_profiles.autogenerate(
        tree, scratch_schema, tmp_path, head=tree_profiles.CORE_HEAD
    )
    print(f"[{profile}] autogenerate on the upgraded database: {result.operations}")
    assert result.body is not None, result.describe()
    assert result.operations == [], result.describe()


# ---------------------------------------------------------------------------
# 2. The migration builds what create_all builds
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_an_upgraded_database_and_a_bootstrapped_one_have_the_same_table_column_and_checks(
    profile, test_db, scratch_schema, reference_schema, tmp_path
):
    """Autogenerate compares neither a check, a server default nor a foreign
    key's ON DELETE, so the catalog is compared here."""
    tree = _at_previous_release(test_db, profile, scratch_schema, tmp_path)
    upgrade = _upgrade(tree, scratch_schema)
    assert upgrade.returncode == 0, upgrade.stderr[-3000:]
    created = tree_profiles.bootstrap_schema(tree, reference_schema)
    assert created.returncode == 0, created.stderr[-3000:]

    upgraded = _definition(test_db, scratch_schema)
    bootstrapped = _definition(test_db, reference_schema)
    print(f"[{profile}] upgraded:     {upgraded}")
    print(f"[{profile}] bootstrapped: {bootstrapped}")
    assert bootstrapped == (
        EXPECTED_COLUMNS,
        EXPECTED_CONSTRAINTS,
        EXPECTED_INDEXES,
        EXPECTED_KEY_COLUMN,
        {(KEY_CHECK, "c", EXPECTED_KEY_CHECK)},
    )
    assert upgraded == bootstrapped


# ---------------------------------------------------------------------------
# 3. Nothing the previous release relies on moves; the checks hold
# ---------------------------------------------------------------------------
#: Writes the checks and NOT NULL refuse: (case, SQL, the constraint or None
#: for NOT NULL).
_REFUSED = [
    ("key-qa", "UPDATE {s}.api_keys SET environment = 'qa'", KEY_CHECK),
    ("key-Production", "UPDATE {s}.api_keys SET environment = 'Production'", KEY_CHECK),
    ("key-null", "UPDATE {s}.api_keys SET environment = NULL", None),
    (
        "setting-production",
        "INSERT INTO {s}.flag_environment_settings (id, feature_flag_id, environment, "
        "created_at, updated_at) VALUES (gen_random_uuid(), :flag, 'production', now(), now())",
        "check_flag_environment_name",
    ),
    (
        "setting-101",
        "INSERT INTO {s}.flag_environment_settings (id, feature_flag_id, environment, "
        "rollout_percentage, created_at, updated_at) VALUES (gen_random_uuid(), :flag, "
        "'staging', 101, now(), now())",
        "check_flag_environment_rollout",
    ),
]


@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_the_upgrade_keeps_what_the_previous_release_relies_on_and_the_checks_hold(
    profile, test_db, scratch_schema, tmp_path
):
    tree = _at_previous_release(test_db, profile, scratch_schema, tmp_path)
    before = _kept_definitions(test_db, scratch_schema)
    upgrade = _upgrade(tree, scratch_schema)
    assert upgrade.returncode == 0, upgrade.stderr[-3000:]
    after = _kept_definitions(test_db, scratch_schema)

    # The previous release's unique index on feature_flags.key, the safety
    # configuration's index and the evaluation counters' primary key (its
    # upsert's ON CONFLICT target) are among the definitions kept.
    for expected in (
        (
            "feature_flags",
            "index",
            "CREATE UNIQUE INDEX ix_<schema>_feature_flags_key ON "
            "<schema>.feature_flags USING btree (key)",
        ),
        (
            "feature_flag_safety_configs",
            "index",
            "CREATE UNIQUE INDEX <schema>_safety_config_feature_flag_id_idx ON "
            "<schema>.feature_flag_safety_configs USING btree (feature_flag_id)",
        ),
        (
            "sdk_evaluation_flag_counts",
            "constraint",
            "sdk_evaluation_flag_counts_pkey",
            "p",
            "PRIMARY KEY (flag_key, minute)",
        ),
    ):
        assert expected in before, (expected, sorted(before))
    assert before - after == set(), f"changed or dropped: {sorted(before - after)}"
    assert after - before == {
        ("api_keys", "constraint", KEY_CHECK, "c", EXPECTED_KEY_CHECK)
    }

    # A key the previous release inserts without naming the column is a
    # production key.
    user = _user(test_db, scratch_schema)
    key_id, _ = _insert_previous_release_key(test_db, scratch_schema, user.id)
    assert _key_environments(test_db, scratch_schema)[key_id] == ("production", True)

    flag_id = _insert_flag(test_db, scratch_schema, "checked-flag")
    for case, statement, constraint in _REFUSED:
        with test_db.connect() as conn:
            transaction = conn.begin()
            try:
                with pytest.raises(IntegrityError) as raised:
                    conn.execute(
                        text(statement.format(s=f'"{scratch_schema}"')),
                        {"flag": flag_id},
                    )
            finally:
                transaction.rollback()
        expected_code = "23502" if constraint is None else "23514"
        assert raised.value.orig.pgcode == expected_code, (case, raised.value.orig)
        if constraint is not None:
            assert raised.value.orig.diag.constraint_name == constraint, case

    # Deleting a flag deletes its side rows.
    _insert_setting(test_db, scratch_schema, flag_id, "staging")
    _insert_setting(test_db, scratch_schema, flag_id, "development")
    assert _count(test_db, scratch_schema, TABLE) == 2
    with test_db.begin() as conn:
        conn.execute(
            text(f'DELETE FROM "{scratch_schema}".feature_flags WHERE id = :f'),
            {"f": flag_id},
        )
    assert _count(test_db, scratch_schema, TABLE) == 0


# ---------------------------------------------------------------------------
# 4. The statements the upgrade from 0.48.0 runs, captured online
# ---------------------------------------------------------------------------
#: Run in a subprocess from the tree: records every statement executed while
#: alembic's ``MigrationContext.run_migrations`` runs, then runs alembic's own
#: command line.
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


def _is_read(statement: str) -> bool:
    return statement.lstrip().split(" ", 1)[0].upper() in {"SELECT", "WITH", "SHOW"}


def _non_catalog_relations(statement: str) -> List[str]:
    return [
        name
        for name in (m.replace('"', "") for m in _RELATION.findall(statement))
        if not name.lower().startswith(_CATALOGS)
    ]


def _captured(tree, schema: str, tmp_path, *argv: str) -> List[str]:
    """The statements ``alembic <argv>`` runs inside ``run_migrations``."""
    out = tmp_path / f"captured-{uuid.uuid4().hex[:6]}.json"
    done = tree_profiles.run(
        tree,
        [
            "-c",
            _CAPTURE,
            str(out),
            "-c",
            str(tree_profiles.alembic_ini(tree)),
            *argv,
        ],
        schema,
    )
    assert done.returncode == 0, done.stderr[-3000:]
    return [_normal(s) for s in json.loads(out.read_text(encoding="utf-8"))]


def _set_aside(captured: List[str]) -> List[str]:
    """The captured statements with the ``alembic_version`` writes and the
    catalog reads set aside; every read set aside reads only the catalog."""
    kept = [s for s in captured if "alembic_version" not in s]
    reads = [s for s in kept if _is_read(s)]
    outside = {s: _non_catalog_relations(s) for s in reads if _non_catalog_relations(s)}
    assert outside == {}, f"a read set aside reads a table: {outside}"
    return [s for s in kept if not _is_read(s)]


def _set(bound: str) -> str:
    return f"SET LOCAL lock_timeout = '{bound}'"


RESET = "SET LOCAL lock_timeout TO DEFAULT"


@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_the_upgrade_from_the_previous_core_head_runs_exactly_its_statements(
    profile, test_db, scratch_schema, tmp_path
):
    """Two blocks, each between its bound and the reset: the side table under
    500 ms, then the ``api_keys`` steps, last, under 1500 ms. No data step."""
    tree = _at_previous_release(test_db, profile, scratch_schema, tmp_path)
    captured = _captured(tree, scratch_schema, tmp_path, "upgrade", REVISION)
    for statement in captured:
        print(f"[{profile}] captured: {statement[:200]}")
    assert _rows(test_db, scratch_schema) == REVISION_ROWS[profile]

    revision = _load_revision(scratch_schema)
    remaining = _set_aside(captured)
    for statement in remaining:
        verb = statement.split(" ", 1)[0].upper()
        assert verb not in {"INSERT", "UPDATE", "DELETE"}, f"a data step: {statement}"

    qualified = f"{scratch_schema}.{TABLE}"
    keys = f"{scratch_schema}.{KEYS}"
    assert revision._FLAGS_LOCK_TIMEOUT == "500ms"
    assert revision._KEYS_LOCK_TIMEOUT == "1500ms"
    assert len(remaining) == 8, remaining
    assert remaining[0] == _set("500ms"), remaining
    assert remaining[1].startswith(f"CREATE TABLE {qualified} ("), remaining
    references = f"REFERENCES {scratch_schema}.feature_flags (id) ON DELETE CASCADE"
    assert references in remaining[1], remaining
    assert remaining[2] == (
        f"CREATE INDEX ix_{scratch_schema}_flag_environment_settings_created_at "
        f"ON {qualified} (created_at)"
    ), remaining
    assert remaining[3] == RESET, remaining
    assert remaining[4] == _set("1500ms"), remaining
    assert remaining[5] == (
        f"ALTER TABLE {keys} ADD COLUMN environment VARCHAR(32) "
        "DEFAULT 'production' NOT NULL"
    ), remaining
    assert remaining[6] == (
        f"ALTER TABLE {keys} ADD CONSTRAINT {KEY_CHECK} CHECK (environment IN "
        "('production', 'staging', 'development'))"
    ), remaining
    assert remaining[7] == RESET, remaining


@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_the_downgrade_runs_exactly_its_statements(
    profile, test_db, scratch_schema, tmp_path
):
    """Two blocks, ``api_keys`` first: the ``api_keys`` block under 1500 ms
    (the deactivating UPDATE, then the check and the column by name), then
    the side table under 500 ms."""
    tree = _at_this_revision(test_db, profile, scratch_schema, tmp_path)
    captured = _captured(
        tree, scratch_schema, tmp_path, "downgrade", PREVIOUS_CORE_HEAD
    )
    for statement in captured:
        print(f"[{profile}] captured: {statement[:200]}")
    assert _rows(test_db, scratch_schema) == PREVIOUS_ROWS[profile]
    keys = f"{scratch_schema}.{KEYS}"
    assert _set_aside(captured) == [
        _set("1500ms"),
        f"UPDATE {keys} SET is_active = false WHERE environment <> 'production'",
        f"ALTER TABLE {keys} DROP CONSTRAINT {KEY_CHECK}",
        f"ALTER TABLE {keys} DROP COLUMN environment",
        RESET,
        _set("500ms"),
        f"DROP TABLE {scratch_schema}.{TABLE}",
        RESET,
    ]


# ---------------------------------------------------------------------------
# 5. A request holding api_keys, and readers of feature_flags
# ---------------------------------------------------------------------------
def _upgrade_with_its_own_timeout(tree, schema: str) -> subprocess.CompletedProcess:
    """``upgrade`` to this revision, named: never ``heads``, so a later
    revision never changes what these legs run."""
    return tree_profiles.alembic(
        tree,
        schema,
        "upgrade",
        REVISION,
        extra_env={"PGOPTIONS": LEG_STATEMENT_TIMEOUT},
    )


def _downgrade_with_its_own_timeout(tree, schema: str) -> subprocess.CompletedProcess:
    """``downgrade`` to the revision before this one, named."""
    return tree_profiles.alembic(
        tree,
        schema,
        "downgrade",
        PREVIOUS_CORE_HEAD,
        extra_env={"PGOPTIONS": LEG_STATEMENT_TIMEOUT},
    )


@contextlib.contextmanager
def _holding(engine, *statements: str, params=None):
    """A session that ran *statements* and keeps its transaction open: a
    request in flight. Rolled back on exit."""
    holder = engine.connect()
    transaction = holder.begin()
    try:
        for statement in statements:
            holder.execute(text(statement), params or {})
        yield holder
    finally:
        transaction.rollback()
        holder.close()


#: Where the api_keys leg starts: 0.48.0's heads, where the upgrade runs this
#: revision alone, and 0.47.0's core head, where one upgrade runs
#: ``t449_identity_aliases`` and then this one, so the refusal must take back
#: the first revision's table too.
START_POINTS = {
    "from-0.48.0": (PREVIOUS_CORE_HEAD, ()),
    "from-0.47.0": ("t1812_user_totp", ("identity_aliases",)),
}


@pytest.mark.regression
@pytest.mark.parametrize("start", sorted(START_POINTS))
def test_a_request_holding_api_keys_ends_the_upgrade_with_the_refusal(
    start, test_db, scratch_schema, tmp_path
):
    """An SDK request reads ``api_keys`` and holds that lock until it ends.
    The ``ALTER TABLE`` waits for it; the bound ends the wait (SQLSTATE 55P03)
    with this revision's refusal, and nothing is applied: no revision of the
    run, ``alembic_version`` as it was. Without the ``SET`` only the leg's own
    ``statement_timeout`` ends it (57014), and this fails."""
    tree = _at_previous_release(test_db, CORE, scratch_schema, tmp_path)
    recorded, earlier_tables = START_POINTS[start]
    with test_db.begin() as conn:
        for table in earlier_tables:
            conn.execute(text(f'DROP TABLE "{scratch_schema}".{table}'))
    _stamp(test_db, scratch_schema, {recorded})
    expected = _load_revision(scratch_schema)._lock_refusal(KEYS)
    with _holding(test_db, f'SELECT count(*) FROM "{scratch_schema}".api_keys'):
        upgrade = _upgrade_with_its_own_timeout(tree, scratch_schema)
    print(f"[{start}] upgrade stderr tail:\n{upgrade.stderr[-1500:]}")
    assert upgrade.returncode != 0
    assert expected in upgrade.stderr, upgrade.stderr[-3000:]
    assert "statement timeout" not in upgrade.stderr
    assert _rows(test_db, scratch_schema) == {recorded}
    tables = _tables(test_db, scratch_schema)
    assert TABLE not in tables
    assert _key_column(test_db, scratch_schema) is None
    for table in earlier_tables:
        assert table not in tables, f"{table} was applied by a refused upgrade"


#: The tables the SDK routes read or write, other than ``api_keys``, and the
#: lock mode a request in flight holds on each.
SDK_TABLE_MODES = {
    "events": "ROW EXCLUSIVE",
    "assignments": "ROW EXCLUSIVE",
    "holdout_population": "ROW EXCLUSIVE",
    "event_dedup_keys": "ROW EXCLUSIVE",
    "sdk_evaluation_flag_counts": "ROW EXCLUSIVE",
    "error_logs": "ROW EXCLUSIVE",
    "identity_aliases": "ROW EXCLUSIVE",
    "feature_flags": "ACCESS SHARE",
}


@pytest.mark.regression
def test_the_upgrade_commits_beside_readers_of_feature_flags_and_the_other_sdk_tables(
    test_db, scratch_schema, tmp_path
):
    """The side table's foreign key locks ``feature_flags`` in SHARE ROW
    EXCLUSIVE mode, which an SDK request's read does not conflict with."""
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
    assert upgrade.returncode == 0, upgrade.stderr[-3000:]
    assert "55P03" not in upgrade.stderr and "lock timeout" not in upgrade.stderr
    assert _rows(test_db, scratch_schema) == REVISION_ROWS[CORE]
    assert TABLE in _tables(test_db, scratch_schema)


def _flag_writer(schema: str) -> str:
    """An uncommitted write to a flag: what the dashboard, the rollout
    scheduler or a safety rollback holds while it runs."""
    return f'UPDATE "{schema}".feature_flags SET rollout_percentage = 7'


def _analyzer(schema: str) -> str:
    """An uncommitted ANALYZE: it holds SHARE UPDATE EXCLUSIVE on the table
    until the transaction ends, as VACUUM and autovacuum do while they run."""
    return f'ANALYZE "{schema}".feature_flags'


def _schema_changer(schema: str) -> str:
    """An uncommitted schema change of ``feature_flags``: ACCESS EXCLUSIVE."""
    return (
        f'ALTER TABLE "{schema}".feature_flags ALTER COLUMN rollout_percentage '
        "SET DEFAULT 0"
    )


#: What holds the upgrade's lock on ``feature_flags`` up.
FLAG_HOLDERS = {
    "altering": _schema_changer,
    "analyzing": _analyzer,
    "writing": _flag_writer,
}


@pytest.mark.regression
@pytest.mark.parametrize("holder", sorted(FLAG_HOLDERS))
def test_a_session_writing_or_analyzing_feature_flags_ends_the_upgrade_with_the_refusal(
    holder, test_db, scratch_schema, tmp_path
):
    """Creating the side table takes SHARE ROW EXCLUSIVE on ``feature_flags``
    (its foreign key), which waits behind an uncommitted flag write or schema
    change, and behind an ANALYZE or a vacuum of the table (a vacuum holds the
    lock mode an ANALYZE does). The 500 ms bound ends that wait with
    this revision's refusal, naming ``feature_flags``, and nothing is applied;
    once the session is gone, the same command runs through. Without the
    ``SET`` only the leg's own ``statement_timeout`` ends it (57014), and this
    fails."""
    tree = _at_previous_release(test_db, CORE, scratch_schema, tmp_path)
    _insert_flag(test_db, scratch_schema, "written-flag")
    expected = _load_revision(scratch_schema)._lock_refusal("feature_flags")
    with _holding(test_db, FLAG_HOLDERS[holder](scratch_schema)):
        upgrade = _upgrade_with_its_own_timeout(tree, scratch_schema)
    print(f"[{holder}] upgrade stderr tail:\n{upgrade.stderr[-1500:]}")
    assert upgrade.returncode != 0
    assert expected in upgrade.stderr, upgrade.stderr[-3000:]
    assert "statement timeout" not in upgrade.stderr
    assert _rows(test_db, scratch_schema) == PREVIOUS_ROWS[CORE]
    assert TABLE not in _tables(test_db, scratch_schema)
    assert _key_column(test_db, scratch_schema) is None
    again = _upgrade_with_its_own_timeout(tree, scratch_schema)
    assert again.returncode == 0, again.stderr[-3000:]
    assert _rows(test_db, scratch_schema) == REVISION_ROWS[CORE]
    assert TABLE in _tables(test_db, scratch_schema)


#: How long the order leg's key lookup may wait: far below the 500 ms bound
#: the migration is waiting in, so a lookup that queues behind ``api_keys``
#: ends here rather than when the migration gives up.
LOOKUP_STATEMENT_TIMEOUT = "300ms"
#: A ceiling on finding the migration's waiting lock, never an assertion on
#: how long it took.
WAIT_FOR_LOCK_SECONDS = 60


@pytest.mark.regression
def test_while_the_upgrade_waits_for_feature_flags_key_lookups_still_run(
    test_db, scratch_schema, tmp_path
):
    """The order, by behaviour: the side table's block runs first, while the
    upgrade holds nothing an SDK request needs. With a flag write held, the
    migration's SHARE ROW EXCLUSIVE request on ``feature_flags`` waits
    (found in ``pg_locks``, not by timing); a key lookup made then must
    succeed. With the ``api_keys`` block first, ``api_keys`` would be held
    already and the lookup would end at its own statement timeout."""
    tree = _at_previous_release(test_db, CORE, scratch_schema, tmp_path)
    _insert_flag(test_db, scratch_schema, "written-flag")
    environment = tree_profiles._environment(tree, scratch_schema)
    with _holding(test_db, _flag_writer(scratch_schema)):
        migration = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "alembic",
                "-c",
                str(tree_profiles.alembic_ini(tree)),
                "upgrade",
                REVISION,
            ],
            cwd=str(tree),
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            seen = None
            deadline = time.monotonic() + WAIT_FOR_LOCK_SECONDS
            with test_db.connect() as watcher:
                while seen is None and time.monotonic() < deadline:
                    seen = watcher.execute(
                        text(
                            "SELECT l.mode FROM pg_locks l "
                            "JOIN pg_class c ON c.oid = l.relation "
                            "JOIN pg_namespace n ON n.oid = c.relnamespace "
                            "WHERE n.nspname = :s AND c.relname = 'feature_flags' "
                            "AND NOT l.granted"
                        ),
                        {"s": scratch_schema},
                    ).scalar()
                    watcher.rollback()
                    if seen is None and migration.poll() is not None:
                        break
            assert seen == "ShareRowExclusiveLock", (seen, migration.poll())
            with test_db.connect() as lookup:
                lookup.execute(
                    text(f"SET statement_timeout = '{LOOKUP_STATEMENT_TIMEOUT}'")
                )
                count = lookup.execute(
                    text(f'SELECT count(*) FROM "{scratch_schema}".api_keys')
                ).scalar()
                lookup.rollback()
            assert count is not None
        finally:
            stderr = migration.communicate(timeout=120)[1]
    # The writer held past the bound: the upgrade refused, applied nothing.
    assert migration.returncode != 0, stderr[-2000:]
    assert _rows(test_db, scratch_schema) == PREVIOUS_ROWS[CORE]


#: Each table the downgrade waits for a lock on, and the table whose block's
#: refusal answers for it: dropping the side table waits for that table
#: itself, then for ``feature_flags``, so one block's text names both.
HELD_TABLES = {
    "api_keys": "api_keys",
    "feature_flags": "feature_flags",
    "flag_environment_settings": "feature_flags",
}


@pytest.mark.regression
@pytest.mark.parametrize("held", sorted(HELD_TABLES))
def test_a_session_reading_a_table_the_downgrade_locks_ends_it_with_its_refusal(
    held, test_db, scratch_schema, tmp_path
):
    """Dropping the column takes ACCESS EXCLUSIVE on ``api_keys``; dropping
    the side table takes it on that table, then on ``feature_flags``: each
    waits behind a request reading that table. Each block's bound ends the
    wait with the downgrade's refusal, whose text names the table whichever
    of its tables held the lock, and nothing is changed: the version rows, the
    column and the table are as they were, and a staging key is still active
    (the deactivating UPDATE was rolled back). Without a block's ``SET`` only
    the leg's own ``statement_timeout`` ends it."""
    tree = _at_this_revision(test_db, CORE, scratch_schema, tmp_path)
    user = _user(test_db, scratch_schema)
    key_id, _ = _insert_key(test_db, scratch_schema, user.id, "staging")
    flag_id = _insert_flag(test_db, scratch_schema, "downgraded-flag")
    _insert_setting(test_db, scratch_schema, flag_id, "staging")
    before = (_definition(test_db, scratch_schema), _settings(test_db, scratch_schema))
    expected = _load_revision(scratch_schema)._downgrade_lock_refusal(HELD_TABLES[held])
    with _holding(test_db, f'SELECT count(*) FROM "{scratch_schema}".{held}'):
        down = _downgrade_with_its_own_timeout(tree, scratch_schema)
    print(f"[{held}] downgrade stderr tail:\n{down.stderr[-1500:]}")
    assert down.returncode != 0
    assert expected in down.stderr, down.stderr[-3000:]
    # The text names the table this session held.
    assert f"{scratch_schema}.{held}" in expected, expected
    assert "Refusing to upgrade" not in down.stderr
    assert "statement timeout" not in down.stderr
    assert _rows(test_db, scratch_schema) == REVISION_ROWS[CORE]
    after = (_definition(test_db, scratch_schema), _settings(test_db, scratch_schema))
    assert after == before
    assert _key_environments(test_db, scratch_schema)[key_id] == ("staging", True)


@contextlib.contextmanager
def _downgrading_in_the_background(tree, schema: str):
    """``downgrade`` to the revision before this one, running in another
    process. Yields ``(process, application_name)``; the process is ended
    before this returns."""
    application = f"t2011-downgrade-{uuid.uuid4().hex[:8]}"
    environment = tree_profiles._environment(
        tree,
        schema,
        {"PGAPPNAME": application, "PGOPTIONS": LEG_STATEMENT_TIMEOUT},
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(tree_profiles.alembic_ini(tree)),
            "downgrade",
            PREVIOUS_CORE_HEAD,
        ],
        cwd=str(tree),
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        yield process, application
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def _waiting_for(engine, application: str) -> set[tuple]:
    """The lock requests the backend named *application* is waiting for:
    ``(locktype, mode, relation)``; ``relation`` is None for a row lock (it
    waits for the other session's transaction)."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT l.locktype, l.mode, l.relation::regclass::text "
                "FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
                "WHERE a.application_name = :app AND NOT l.granted"
            ),
            {"app": application},
        ).all()
        conn.rollback()
    return {tuple(row) for row in rows}


def _until_waiting_for(engine, process, application: str, wanted) -> tuple:
    """Wait until *application*'s backend waits for a lock that *wanted* picks
    out, and return it; fail if the process ends first."""
    deadline = time.monotonic() + WAIT_FOR_LOCK_SECONDS
    while time.monotonic() < deadline:
        found = [
            request for request in _waiting_for(engine, application) if wanted(request)
        ]
        if found:
            return found[0]
        assert process.poll() is None, (process.returncode, process.communicate()[1])
        time.sleep(0.02)
    raise AssertionError(f"{application} never waited for that lock")


def _row_lock(request: tuple) -> bool:
    return request[0] == "transactionid"


def _api_keys_table_lock(schema: str):
    def wanted(request: tuple) -> bool:
        return request[1:] == ("AccessExclusiveLock", f"{schema}.{KEYS}")

    return wanted


#: How long the two-wait leg holds each lock once the migration waits for it:
#: below each block's 1500 ms bound, so neither wait ends at the bound, and
#: above half of it, so the two together are longer than one bound.
HELD_SECONDS = 0.9


@pytest.mark.regression
def test_the_downgrade_waits_for_each_lock_it_needs_on_its_own_bound(
    test_db, scratch_schema, tmp_path
):
    """Deactivating a staging key waits for another session's uncommitted
    update of that key's row; the column's drop then waits for a session that
    read ``api_keys``. Each wait is bounded on its own, so a downgrade that
    meets both waits for longer than one bound and goes through."""
    tree = _at_this_revision(test_db, CORE, scratch_schema, tmp_path)
    user = _user(test_db, scratch_schema)
    key_id, _ = _insert_key(test_db, scratch_schema, user.id, "staging")
    with test_db.connect() as writer, test_db.connect() as reader:
        writing = writer.begin()
        writer.execute(
            text(
                f'UPDATE "{scratch_schema}".api_keys SET last_used_at = now() WHERE id = :id'
            ),
            {"id": key_id},
        )
        reading = reader.begin()
        reader.execute(text(f'SELECT count(*) FROM "{scratch_schema}".api_keys'))
        with _downgrading_in_the_background(tree, scratch_schema) as (process, name):
            started = time.monotonic()
            row = _until_waiting_for(test_db, process, name, _row_lock)
            time.sleep(HELD_SECONDS)
            writing.rollback()
            table = _until_waiting_for(
                test_db, process, name, _api_keys_table_lock(scratch_schema)
            )
            time.sleep(HELD_SECONDS)
            reading.rollback()
            stderr = process.communicate(timeout=120)[1]
            waited = time.monotonic() - started
    print(f"waits, in order: {row} then {table}; {waited:.2f} s in all")
    assert process.returncode == 0, stderr[-3000:]
    assert row[0] == "transactionid", row
    assert table[1:] == ("AccessExclusiveLock", f"{scratch_schema}.{KEYS}"), table
    assert _rows(test_db, scratch_schema) == PREVIOUS_ROWS[CORE]
    assert TABLE not in _tables(test_db, scratch_schema)
    with test_db.connect() as conn:
        active = conn.execute(
            text(f'SELECT is_active FROM "{scratch_schema}".api_keys WHERE id = :id'),
            {"id": key_id},
        ).scalar()
    assert active is False


def _deadlocks(engine) -> int:
    """The deadlocks PostgreSQL has counted in this database."""
    with engine.connect() as conn:
        count = conn.execute(
            text(
                "SELECT deadlocks FROM pg_stat_database "
                "WHERE datname = current_database()"
            )
        ).scalar()
        conn.rollback()
    return count


@pytest.mark.regression
def test_a_deadlock_with_a_session_updating_a_key_ends_the_downgrade_with_its_refusal(
    test_db, scratch_schema, tmp_path
):
    """A session that read ``api_keys`` then updates a staging key's row that
    the downgrade has already locked (what recording a key's use does): each
    waits for the other. PostgreSQL ends the downgrade, whose wait is the
    older one, with 40P01, and it refuses with the same text as a lock
    timeout, with nothing changed; the other session's update goes through.
    A lock timeout gives the same text, so this counts the deadlocks."""
    tree = _at_this_revision(test_db, CORE, scratch_schema, tmp_path)
    user = _user(test_db, scratch_schema)
    key_id, _ = _insert_key(test_db, scratch_schema, user.id, "staging")
    expected = _load_revision(scratch_schema)._downgrade_lock_refusal(KEYS)
    before = _deadlocks(test_db)
    update = text(
        f'UPDATE "{scratch_schema}".api_keys SET last_used_at = now() WHERE id = :id'
    )
    outcome: dict = {}
    with test_db.connect() as session:
        transaction = session.begin()
        session.execute(text("SET LOCAL statement_timeout = '60s'"))
        session.execute(text(f'SELECT count(*) FROM "{scratch_schema}".api_keys'))

        def update_the_key() -> None:
            try:
                session.execute(update, {"id": key_id})
                outcome["updated"] = True
            except Exception as exc:  # reported by the assertion below
                outcome["error"] = exc

        with _downgrading_in_the_background(tree, scratch_schema) as (process, name):
            _until_waiting_for(
                test_db, process, name, _api_keys_table_lock(scratch_schema)
            )
            updating = threading.Thread(target=update_the_key)
            updating.start()
            stderr = process.communicate(timeout=120)[1]
            updating.join(timeout=60)
        transaction.rollback()
    print(f"downgrade stderr tail:\n{stderr[-1500:]}")
    assert process.returncode != 0
    assert expected in stderr, stderr[-3000:]
    assert "Refusing to upgrade" not in stderr
    assert "statement timeout" not in stderr
    assert outcome == {"updated": True}, outcome
    deadline = time.monotonic() + 30
    while _deadlocks(test_db) == before and time.monotonic() < deadline:
        time.sleep(0.1)
    assert _deadlocks(test_db) > before, "the downgrade met no deadlock"
    assert _rows(test_db, scratch_schema) == REVISION_ROWS[CORE]
    assert TABLE in _tables(test_db, scratch_schema)
    assert _key_environments(test_db, scratch_schema)[key_id] == ("staging", True)


# ---------------------------------------------------------------------------
# The previous release's code, and this release's, in a subprocess
# ---------------------------------------------------------------------------
#: The model's additions, which the previous release does not have, each with
#: what stands for it in the copy; if one is not found exactly once, the test
#: says so rather than testing the wrong tree. 0.48.0 has no environments, so
#: there every key is a production key: ``APIKey.environment`` is a plain
#: attribute of the class, with no column, and nothing reads the database's.
_MODEL_DECLARATIONS = {
    ("backend", "app", "models", "feature_flag.py"): (
        (
            re.compile(
                r"(?ms)^#: The checks and the unique constraint of "
                r"``flag_environment_settings``.*?(?=^class FeatureFlagOverride\b)"
            ),
            "",
        ),
        (
            re.compile(
                r"(?ms)^    # The flag's split in production \(#2012\).*?"
                r"^    split = Column\([^\n]*\)\n"
            ),
            "",
        ),
    ),
    ("backend", "app", "models", "api_key.py"): (
        (
            re.compile(
                r"(?ms)^    # The flag environment this key serves \(#2011\).*?^    \)\n"
            ),
            '    environment = "production"\n',
        ),
        (
            re.compile(
                r"(?ms)^ +CheckConstraint\(\s*API_KEY_ENVIRONMENT_CHECK_SQL, "
                r"name=API_KEY_ENVIRONMENT_CHECK\s*\),\n"
            ),
            "",
        ),
    ),
}

#: The module the key routes read the side rows through, as 0.48.0 answers: from
#: the flag rows alone. Asked about any other environment, it fails the request.
_FLAG_ROWS_ONLY = textwrap.dedent(
    """
    from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus


    def served_flag(db, flag, environment):
        assert environment == "production", environment
        return flag


    def served_flags(db, flags, environment):
        assert environment == "production", environment
        return flags


    def flags_on_in(db, environment):
        assert environment == "production", environment
        return (
            db.query(FeatureFlag)
            .filter(FeatureFlag.status == FeatureFlagStatus.ACTIVE.value)
            .all()
        )
    """
)


#: The modules that read and write the side rows, as 0.48.0 is without them:
#: no side row is read or written, and no environment route exists. Each is
#: named with a line of the module it replaces, so a renamed one fails here.
_WITHOUT_SIDE_ROWS = {
    ("backend", "app", "services", "flag_environment_reads.py"): (
        "def served_flag(",
        _FLAG_ROWS_ONLY,
    ),
    # The dashboard routes' reads, and every write.
    ("backend", "app", "services", "flag_environment_service.py"): (
        "def settings_of(",
        """
        def audit_side_rows(db, flag_id):
            return {}


        def flags_on_outside_production(db, flag_ids):
            return set()


        def settings_mentioning(db, words):
            return []


        def turn_off_environment_settings(db, flag_id):
            return []
        """,
    ),
    ("backend", "app", "api", "v1", "endpoints", "flag_environments.py"): (
        "def update_flag_environment(",
        """
        from fastapi import APIRouter

        router = APIRouter()
        environments_router = APIRouter()
        """,
    ),
}


def _previous_release(profile: str, tmp_path) -> Path:
    """Release N-1 (0.48.0): a copy of this tree without the revision and the
    ones after it, the models' additions and the environment routes, where every
    key is a production key, nothing reads or writes a side row and no flag
    serves a split or a rule's own value.
    ``core/flag_environments.py`` stays: the names of the environments, and a
    view of a side row that nothing left in the copy makes."""
    destination = tmp_path / "release-n-minus-1"
    if profile == FULL:
        tree = tree_profiles.full_tree(destination)
    else:
        tree = tree_profiles.core_tree(destination)
    versions = tree / "backend" / "app" / "db" / "migrations" / "versions"
    for revision in (REVISION, *LATER_REVISIONS):
        (revision_file,) = versions.glob(f"{revision}_*.py")
        revision_file.unlink()
    for parts, (line, stub) in _WITHOUT_SIDE_ROWS.items():
        module = tree.joinpath(*parts)
        assert line in module.read_text(encoding="utf-8"), f"{module}: update it"
        module.write_text(textwrap.dedent(stub), encoding="utf-8")
    for parts, declarations in _MODEL_DECLARATIONS.items():
        model = tree.joinpath(*parts)
        source = model.read_text(encoding="utf-8")
        for declaration, replacement in declarations:
            source, found = declaration.subn(replacement, source)
            assert found == 1, f"{found} matches of {declaration.pattern}: update it"
        model.write_text(source, encoding="utf-8")
    without_the_read_path(tree)
    return tree


#: Runs in a tree, against a schema: each request through the app's own
#: routes, with no dependency override. ``argv[1]`` is a JSON list of
#: ``[method, path, headers, body]``; prints a JSON list of
#: ``[status, body]``. With ``--writes`` first, it also does the previous
#: release's model writes (create a flag, then set it to 25%), as a
#: release-N-1 deployment would, and prints their results first.
_CLIENT = textwrap.dedent(
    """
    import json
    import sys

    from backend.app.models import register_core_models

    register_core_models()

    from fastapi.testclient import TestClient

    from backend.app.main import app
    from backend.app.models.api_key import APIKey

    report = {"api_key_columns": sorted(APIKey.__table__.columns.keys())}
    args = sys.argv[1:]
    if args and args[0] == "--writes":
        args = args[1:]
        from backend.app.crud.crud_feature_flag import crud_feature_flag
        from backend.app.db.session import SessionLocal
        from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
        from backend.app.services.sdk_ruleset import build_ruleset

        db = SessionLocal()
        flag = FeatureFlag(
            key="previous-release-flag",
            name="Previous release",
            status=FeatureFlagStatus.ACTIVE,
            owner_id=None,
        )
        db.add(flag)
        db.commit()
        db.refresh(flag)
        flag.rollout_percentage = 25
        db.commit()
        db.refresh(flag)
        items = crud_feature_flag.get_multi(db, skip=0, limit=100)
        ruleset = build_ruleset(items)
        report["writes"] = {
            "id": str(flag.id),
            "rollout_percentage": flag.rollout_percentage,
            "listed": sorted(item.key for item in items),
            "ruleset": [
                e for e in ruleset["flags"] if e["key"] == "previous-release-flag"
            ],
        }
        db.close()

    client = TestClient(app, raise_server_exceptions=False)
    answers = []
    for method, path, headers, body in json.loads(args[0]):
        response = client.request(method, path, headers=headers, json=body)
        try:
            payload = response.json()
        except ValueError:
            payload = response.text[:500]
        answers.append([response.status_code, payload])
    report["answers"] = answers
    print(json.dumps(report))
    """
)

#: What a test deployment sets for these subprocesses: local sign-in and the
#: test settings, as the API tests run; no scheduler starts (no lifespan).
_CLIENT_ENV = {"ENVIRONMENT": "test", "AUTH_PROVIDER": "local"}


def _client(tree, schema: str, tmp_path, requests: list, *, writes: bool = False):
    script = tmp_path / f"client-{uuid.uuid4().hex[:6]}.py"
    script.write_text(_CLIENT, encoding="utf-8")
    argv = [str(script), *(["--writes"] if writes else []), json.dumps(requests)]
    done = tree_profiles.run(tree, argv, schema, _CLIENT_ENV)
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-4000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def _evaluate(flag_key: str, user_id: str, api_key: str) -> list:
    return [
        "GET",
        f"/api/v1/feature-flags/evaluate/{flag_key}?user_id={user_id}",
        {"X-API-Key": api_key},
        None,
    ]


USERS = [f"user-{n}" for n in range(100)]


def _this_release_answers(engine, schema: str, flag_key: str) -> dict[str, list]:
    with _session(engine, schema) as db:
        flag = db.query(FeatureFlag).filter(FeatureFlag.key == flag_key).one()
        service = FeatureFlagService(db)
        answers = {}
        for user in USERS:
            detail = service.evaluate_flag_detailed(flag, user)
            answers[user] = [detail["enabled"], detail["reason"]]
        db.rollback()
    return answers


def _at_head(test_db, profile, built_by, schema, tmp_path):
    if built_by == UPGRADED:
        tree = _at_previous_release(test_db, profile, schema, tmp_path)
        upgrade = _upgrade(tree, schema)
        assert upgrade.returncode == 0, upgrade.stderr[-3000:]
        return tree
    return _bootstrapped(profile, schema, tmp_path)


# ---------------------------------------------------------------------------
# 6. The previous release writes and serves flags on this schema
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("built_by", [UPGRADED, BOOTSTRAPPED])
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_the_previous_release_writes_and_serves_flags_on_this_schema(
    profile, built_by, test_db, scratch_schema, tmp_path
):
    release_n = _at_head(test_db, profile, built_by, scratch_schema, tmp_path)
    assert _rows(test_db, scratch_schema) == ROWS[profile]
    user = _user(test_db, scratch_schema)
    release_n_minus_1 = _previous_release(profile, tmp_path)

    # N-1 creates a flag and sets it to 25%, lists and builds the ruleset; it
    # also creates a key, which the database makes a production key.
    created = _client(release_n_minus_1, scratch_schema, tmp_path, [], writes=True)
    assert COLUMN not in created["api_key_columns"]
    writes = created["writes"]
    print(f"[{profile}/{built_by}] previous release wrote: {writes}")
    assert writes["rollout_percentage"] == 25
    assert writes["listed"] == ["previous-release-flag"]
    (entry,) = writes["ruleset"]
    assert entry["rollout_percentage"] == 25 and entry.get("active", True)
    key_id, key = _insert_previous_release_key(test_db, scratch_schema, user.id)
    assert _key_environments(test_db, scratch_schema)[key_id] == ("production", True)

    # A staging setting that would answer differently: on at 100%. N-1 never
    # reads it.
    _insert_setting(test_db, scratch_schema, writes["id"], "staging", True, 100)
    staging_before = _settings(test_db, scratch_schema)

    served = _client(
        release_n_minus_1,
        scratch_schema,
        tmp_path,
        [_evaluate("previous-release-flag", u, key) for u in USERS],
    )
    answers = {}
    for user_id, (status, body) in zip(USERS, served["answers"]):
        assert status == 200, body
        answers[user_id] = [body["enabled"], body["reason"]]
    this_release = _this_release_answers(
        test_db, scratch_schema, "previous-release-flag"
    )
    assert answers == this_release, "N-1 and N answer the same production flag"
    enabled = sum(1 for on, _ in answers.values() if on)
    assert 0 < enabled < len(USERS), enabled
    assert _settings(test_db, scratch_schema) == staging_before

    # N's own route, with the same production key, answers the flag N-1 wrote
    # the same for every user: it needs no row of its own for production.
    through_the_route = _client(
        release_n,
        scratch_schema,
        tmp_path,
        [_evaluate("previous-release-flag", u, key) for u in USERS],
    )
    n_answers = {}
    for user_id, (status, body) in zip(USERS, through_the_route["answers"]):
        assert status == 200, body
        n_answers[user_id] = [body["enabled"], body["reason"]]
    differing = [u for u in USERS if n_answers[u] != answers[u]]
    assert not differing, (
        f"previous-release-flag: N answers {json.dumps(n_answers[differing[0]])} for "
        f"{sum(1 for u in USERS if n_answers[u] == n_answers[differing[0]])}/"
        f"{len(USERS)}; N-1 answered {json.dumps([True, 'rollout'])} for {enabled}"
    )
    assert _settings(test_db, scratch_schema) == staging_before

    # In reverse: this release turns the flag off through the stable PUT, and
    # then, turned on again, through production's environment PUT. N-1 answers
    # `inactive` for every user each time. Staging's row is untouched.
    token = _token(user)
    stable = f"/api/v1/feature-flags/{writes['id']}"
    authorization = {"Authorization": f"Bearer {token}"}
    for path in (stable, f"{stable}/environments/production"):
        turned_off = _client(
            release_n,
            scratch_schema,
            tmp_path,
            [
                ["PUT", stable, authorization, {"is_active": True}],
                ["PUT", path, authorization, {"is_active": False}],
            ],
        )
        for status, body in turned_off["answers"]:
            assert status == 200, body
        off = _client(
            release_n_minus_1,
            scratch_schema,
            tmp_path,
            [_evaluate("previous-release-flag", u, key) for u in USERS[:20]],
        )
        reasons = {(s, b["enabled"], b["reason"]) for s, b in off["answers"]}
        assert reasons == {(200, False, "inactive")}, (
            f"N-1 still serves previous-release-flag, which N turned off in "
            f"production through PUT {path}: {reasons}"
        )
        assert _settings(test_db, scratch_schema) == staging_before
    shutil.rmtree(release_n_minus_1, ignore_errors=True)


def _token(user: User) -> str:
    from backend.app.core.security import create_local_access_token

    return create_local_access_token(user)


# ---------------------------------------------------------------------------
# 7. A staging key on the previous release
# ---------------------------------------------------------------------------
def _experiment(engine, schema: str, tree, tmp_path, user: User) -> dict:
    """A running production experiment, created and started through the API."""
    suffix = uuid.uuid4().hex[:8]
    headers = {"Authorization": f"Bearer {_token(user)}"}
    body = {
        "name": f"Environments {suffix}",
        "key": f"environments-{suffix}",
        "hypothesis": "a staging key is a production key on 0.48.0",
        "experiment_type": "a_b",
        "variants": [
            {"name": "control", "is_control": True, "traffic_allocation": 50},
            {"name": "treatment", "is_control": False, "traffic_allocation": 50},
        ],
        "metrics": [
            {
                "name": "Purchase",
                "event_name": f"purchase_{suffix}",
                "metric_type": "conversion",
                "is_primary": True,
            }
        ],
    }
    created = _client(
        tree, schema, tmp_path, [["POST", "/api/v1/experiments/", headers, body]]
    )
    ((status, experiment),) = created["answers"]
    assert status in (200, 201), experiment
    started = _client(
        tree,
        schema,
        tmp_path,
        [["POST", f"/api/v1/experiments/{experiment['id']}/start", headers, None]],
    )
    ((status, answer),) = started["answers"]
    assert status == 200, answer
    return {"id": experiment["id"], "key": body["key"], "event": f"purchase_{suffix}"}


def _production_effects(engine, schema: str, flag_id: str, experiment: dict) -> dict:
    """What a staging key's requests left in production: counted error reports
    for the flag, evaluation counts offered, the experiment's assignments and
    conversion events (by the metric's event name; the assignment's exposure
    event is a separate row), and the identity link."""
    experiment_id = experiment["id"]
    with _session(engine, schema) as db:
        counted = db.execute(
            select(func.count())
            .select_from(ErrorLog)
            .where(
                ErrorLog.feature_flag_id == uuid.UUID(flag_id), counted_error_filter()
            )
        ).scalar()
    with engine.connect() as conn:

        def one(sql: str, **params):
            return conn.execute(text(sql.format(s=f'"{schema}"')), params).scalar()

        return {
            "counted_errors": counted,
            "offered": one(
                "SELECT coalesce(sum(offered), 0) FROM {s}.sdk_evaluation_flag_counts "
                "WHERE flag_key = 'env-probe'"
            ),
            "assignments": one(
                "SELECT count(*) FROM {s}.assignments WHERE experiment_id = :e",
                e=experiment_id,
            ),
            "events": one(
                "SELECT count(*) FROM {s}.events WHERE experiment_id = :e "
                "AND event_name = :n",
                e=experiment_id,
                n=experiment["event"],
            ),
            "links": one(
                "SELECT count(*) FROM {s}.identity_aliases "
                "WHERE anonymous_id = 'staging-visitor'"
            ),
        }


@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_the_previous_release_treats_a_staging_key_as_a_production_key(
    profile, test_db, scratch_schema, tmp_path
):
    """The documented behaviour of 0.48.0 during the rolling upgrade and after
    an API-only rollback: it has no environments, so a staging key gets
    production's configuration, its server-side error report counts in
    production's safety numbers, its evaluation counts are recorded, and its
    assignment, event and identity link are written into production."""
    release_n = _at_head(test_db, profile, UPGRADED, scratch_schema, tmp_path)
    user = _user(test_db, scratch_schema)
    flag_id = _insert_flag(test_db, scratch_schema, "env-probe", "ACTIVE", 100)
    # Off in staging: the answer a staging key gets once 0.49.0 reads keys.
    _insert_setting(test_db, scratch_schema, flag_id, "staging", False, 0)
    staging_before = _settings(test_db, scratch_schema)
    _, staging_key = _insert_key(
        test_db, scratch_schema, user.id, "staging", scopes="sdk:ruleset"
    )
    experiment = _experiment(test_db, scratch_schema, release_n, tmp_path, user)
    before = _production_effects(test_db, scratch_schema, flag_id, experiment)
    assert before == {
        "counted_errors": 0,
        "offered": 0,
        "assignments": 0,
        "events": 0,
        "links": 0,
    }

    now = datetime.now(timezone.utc).replace(microsecond=0)
    headers = {"X-API-Key": staging_key}
    release_n_minus_1 = _previous_release(profile, tmp_path)
    served = _client(
        release_n_minus_1,
        scratch_schema,
        tmp_path,
        [
            _evaluate("env-probe", "staging-user", staging_key),
            [
                "POST",
                "/api/v1/tracking/errors",
                headers,
                {
                    "feature_flag_key": "env-probe",
                    "error_type": "exception",
                    "message": "raised while env-probe was on",
                },
            ],
            [
                "POST",
                "/api/v1/tracking/evaluations",
                headers,
                {
                    "evaluations": [
                        {
                            "flag_key": "env-probe",
                            "count": 1000,
                            "enabled_count": 1000,
                            "window_start": (now - timedelta(seconds=30)).isoformat(),
                            "window_end": now.isoformat(),
                        }
                    ]
                },
            ],
            [
                "POST",
                "/api/v1/tracking/assign",
                headers,
                {"experiment_key": experiment["key"], "user_id": "staging-user"},
            ],
            [
                "POST",
                "/api/v1/tracking/track",
                headers,
                {
                    "event_type": "conversion",
                    "event_name": experiment["event"],
                    "user_id": "staging-user",
                    "experiment_key": experiment["key"],
                },
            ],
            [
                "POST",
                "/api/v1/tracking/alias",
                headers,
                {"anonymous_id": "staging-visitor", "user_id": "staging-user"},
            ],
        ],
    )
    (evaluated, reported, counted, assigned, tracked, linked) = served["answers"]
    print(f"[{profile}] a staging key on the previous release: {served['answers']}")
    assert evaluated[0] == 200 and evaluated[1]["enabled"] is True, evaluated
    assert evaluated[1]["reason"] == "rollout", evaluated
    assert reported[0] == 201, reported
    assert counted[0] in (200, 201) and counted[1]["accepted"] == 1000, counted
    assert assigned[0] == 200 and assigned[1]["assigned"] is True, assigned
    assert tracked[0] in (200, 201), tracked
    assert linked[0] in (200, 201), linked

    after = _production_effects(test_db, scratch_schema, flag_id, experiment)
    assert after == {
        "counted_errors": 1,
        "offered": 1000,
        "assignments": 1,
        "events": 1,
        "links": 1,
    }, after
    with test_db.connect() as conn:
        (source,) = conn.execute(
            text(
                f"SELECT request_data->>'source' FROM \"{scratch_schema}\".error_logs "
                "WHERE feature_flag_id = :f"
            ),
            {"f": flag_id},
        ).one()
    assert source == "server"
    # Staging's row is what it was: nothing on 0.48.0 reads or writes it.
    assert _settings(test_db, scratch_schema) == staging_before
    shutil.rmtree(release_n_minus_1, ignore_errors=True)


# ---------------------------------------------------------------------------
# 8. Offline mode
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_offline_mode_is_refused_in_both_directions_and_emits_nothing(
    test_db, scratch_schema, tmp_path
):
    """Each direction reads the catalog before it changes anything, so
    neither can be emitted as SQL: both refuse, emit no DDL or UPDATE, and
    change nothing."""
    tree = _bootstrapped(CORE, scratch_schema, tmp_path)
    revision = _load_revision(scratch_schema)
    for argv in (
        ("downgrade", f"{REVISION}:{PREVIOUS_CORE_HEAD}", "--sql"),
        ("upgrade", f"{PREVIOUS_CORE_HEAD}:{REVISION}", "--sql"),
    ):
        before = (_rows(test_db, scratch_schema), _definition(test_db, scratch_schema))
        offline = tree_profiles.alembic(tree, scratch_schema, *argv)
        assert offline.returncode != 0, (argv, offline.stdout[-2000:])
        assert revision._OFFLINE_REFUSAL in offline.stderr, (
            f"refusal text missing from --sql output: {argv} {offline.stderr[-2000:]}"
        )
        for emitted in ("CREATE", "DROP", "ALTER", "UPDATE api_keys", ".api_keys SET"):
            assert emitted not in offline.stdout, f"{emitted} emitted offline: {argv}"
        after = (_rows(test_db, scratch_schema), _definition(test_db, scratch_schema))
        assert after == before, argv
        _to_previous_release(test_db, CORE, scratch_schema)


# ---------------------------------------------------------------------------
# 9-10. Online, the downgrade fails closed
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_the_downgrade_deactivates_staging_and_development_keys_then_drops_what_it_added(
    profile, test_db, scratch_schema, tmp_path
):
    tree = _at_this_revision(test_db, profile, scratch_schema, tmp_path)
    user = _user(test_db, scratch_schema)
    keys = {
        environment: _insert_key(test_db, scratch_schema, user.id, environment)
        for environment in ("production", "staging", "development")
    }
    flag_id = _insert_flag(test_db, scratch_schema, "downgraded-flag", "ACTIVE", 30)
    _insert_setting(test_db, scratch_schema, flag_id, "staging", True, 100)
    _insert_setting(test_db, scratch_schema, flag_id, "development", True, 50)
    tables = _tables(test_db, scratch_schema)
    with test_db.connect() as conn:
        flags_before = conn.execute(
            text(
                "SELECT key, status, rollout_percentage FROM "
                f'"{scratch_schema}".feature_flags ORDER BY key'
            )
        ).all()

    down = tree_profiles.alembic(tree, scratch_schema, "downgrade", PREVIOUS_CORE_HEAD)
    assert down.returncode == 0, down.stderr[-3000:]
    _report(profile, "after downgrade", test_db, scratch_schema)

    assert _rows(test_db, scratch_schema) == PREVIOUS_ROWS[profile]
    assert _tables(test_db, scratch_schema) == tables - {TABLE}
    assert _key_column(test_db, scratch_schema) is None
    key_checks = _constraints(test_db, scratch_schema, KEYS)
    assert {c for c in key_checks if c[0] == KEY_CHECK} == set()
    with test_db.connect() as conn:
        active = dict(
            conn.execute(
                text(f'SELECT id::text, is_active FROM "{scratch_schema}".api_keys')
            ).all()
        )
        flags_after = conn.execute(
            text(
                "SELECT key, status, rollout_percentage FROM "
                f'"{scratch_schema}".feature_flags ORDER BY key'
            )
        ).all()
    for environment, (key_id, _) in keys.items():
        expected = environment == "production"
        assert active[key_id] is expected, (
            f"{environment} key {key_id}: is_active {active[key_id]} after the "
            f"downgrade, expected {expected}"
        )
    assert flags_after == flags_before

    # The previous release answers the production key and refuses the former
    # staging key: it is never served production's configuration.
    release_n_minus_1 = _previous_release(profile, tmp_path)
    served = _client(
        release_n_minus_1,
        scratch_schema,
        tmp_path,
        [
            _evaluate("downgraded-flag", "u1", keys["production"][1]),
            _evaluate("downgraded-flag", "u1", keys["staging"][1]),
            _evaluate("downgraded-flag", "u1", keys["development"][1]),
        ],
    )
    statuses = [status for status, _ in served["answers"]]
    assert statuses == [200, 401, 401], served["answers"]
    shutil.rmtree(release_n_minus_1, ignore_errors=True)

    # Upgrading again restores no setting, and the former staging and
    # development keys stay inactive, now production keys.
    upgrade = _upgrade(tree, scratch_schema)
    assert upgrade.returncode == 0, upgrade.stderr[-3000:]
    _report(profile, "after upgrade heads again", test_db, scratch_schema)
    assert _rows(test_db, scratch_schema) == ROWS[profile]
    assert _count(test_db, scratch_schema, TABLE) == 0
    environments = _key_environments(test_db, scratch_schema)
    assert {key_id: environments[key_id] for key_id, _ in keys.values()} == {
        keys["production"][0]: ("production", True),
        keys["staging"][0]: ("production", False),
        keys["development"][0]: ("production", False),
    }


@pytest.mark.regression
def test_this_release_answers_500_to_a_production_key_on_the_downgraded_schema(
    test_db, scratch_schema, tmp_path
):
    """Why the database-first rollback's paragraph names the SDK outage: until
    every task of this release is replaced, its key lookup reads a column the
    downgrade dropped, for production keys too."""
    tree = _at_this_revision(test_db, CORE, scratch_schema, tmp_path)
    user = _user(test_db, scratch_schema)
    _, key = _insert_key(test_db, scratch_schema, user.id, "production", "sdk:ruleset")
    _insert_flag(test_db, scratch_schema, "rolled-back-flag", "ACTIVE", 100)
    down = tree_profiles.alembic(tree, scratch_schema, "downgrade", PREVIOUS_CORE_HEAD)
    assert down.returncode == 0, down.stderr[-3000:]
    served = _client(
        tree,
        scratch_schema,
        tmp_path,
        [
            _evaluate("rolled-back-flag", "u1", key),
            ["GET", "/api/v1/sdk/ruleset", {"X-API-Key": key}, None],
        ],
    )
    assert [status for status, _ in served["answers"]] == [500, 500], served["answers"]


@pytest.mark.regression
def test_this_release_answers_500_where_it_reads_a_side_row_on_the_downgraded_schema(
    test_db, scratch_schema, tmp_path
):
    """The dashboard's half of the same outage. Once a flag is old enough to be
    stale, the list reads the side table to say whether it is; deleting a flag,
    archiving or deleting a segment, listing a segment's experiments and the
    routes of a flag's staging and development settings read it too, and a bulk
    archive writes it. The signed-in API key routes read the column the
    downgrade dropped. The flag itself is still read and changed."""
    tree = _at_this_revision(test_db, CORE, scratch_schema, tmp_path)
    user = _user(test_db, scratch_schema)
    authorization = {"Authorization": f"Bearer {_token(user)}"}
    key_id, _ = _insert_key(test_db, scratch_schema, user.id, "production")
    flag_id = _insert_flag(test_db, scratch_schema, "rolled-back-flag", "ACTIVE", 100)
    with test_db.begin() as conn:
        conn.execute(
            text(
                f'UPDATE "{scratch_schema}".feature_flags '
                "SET created_at = now() - interval '400 days'"
            )
        )
    keys = "/api/v1/api-keys"
    flags = "/api/v1/feature-flags"
    segments = "/api/v1/segments"
    flag = f"{flags}/{flag_id}"
    reads = [
        ["GET", f"{flags}/", authorization, None],
        ["GET", flag, authorization, None],
    ]
    condition = {"attribute": "country", "operator": "equals", "value": "US"}
    rules = {"groups": [{"conditions": [condition]}]}
    segment = {"name": "Rolled back", "rules": rules}
    create = ["POST", f"{segments}/", authorization, segment]
    before = _client(tree, scratch_schema, tmp_path, [*reads, create])
    statuses = [status for status, _ in before["answers"]]
    assert statuses == [200, 200, 201], before["answers"]
    used_in = f"{segments}/{before['answers'][2][1]['id']}"
    down = tree_profiles.alembic(tree, scratch_schema, "downgrade", PREVIOUS_CORE_HEAD)
    assert down.returncode == 0, down.stderr[-3000:]

    def bulk(action):
        body = {"flag_ids": [flag_id], "action": action}
        return ["POST", f"{flags}/bulk-toggle", authorization, body]

    refused = [
        reads[0],
        ["DELETE", flag, authorization, None],
        ["GET", f"{used_in}/experiments", authorization, None],
        ["PUT", used_in, authorization, {"status": "archived"}],
        ["DELETE", used_in, authorization, None],
        ["GET", f"{flag}/environments", authorization, None],
        ["GET", f"{flag}/environments/staging", authorization, None],
        ["PUT", f"{flag}/environments/staging", authorization, {"is_active": False}],
        [
            "POST",
            f"{flag}/environments/production/promote",
            authorization,
            {"from": "staging"},
        ],
        ["GET", keys, authorization, None],
        ["POST", keys, authorization, {"name": "Rolled back"}],
        ["DELETE", f"{keys}/{key_id}", authorization, None],
    ]
    answered = [
        reads[1],
        ["PUT", flag, authorization, {"rollout_percentage": 40}],
        ["POST", f"{flag}/deactivate", authorization, None],
        [
            "PUT",
            f"{flag}/environments/production",
            authorization,
            {"is_active": True},
        ],
        bulk("disable"),
        bulk("archive"),
        reads[1],
    ]
    after = _client(tree, scratch_schema, tmp_path, [*refused, *answered])
    answers = after["answers"]
    assert [status for status, _ in answers] == [500] * 12 + [200] * 7, answers
    disabled, archived, read = (body for _, body in answers[-3:])
    assert (disabled["succeeded"], disabled["failed"]) == (1, 0), disabled
    assert (archived["succeeded"], archived["failed"]) == (0, 1), archived
    # Neither the refused delete nor the failed archive changed the flag.
    assert (read["status"], read["rollout_percentage"]) == ("inactive", 40), read


# ---------------------------------------------------------------------------
# 11. An upgrade changes no answer
# ---------------------------------------------------------------------------
#: Runs in a tree, against a schema: every vector case through ``GET
#: /evaluate`` and one ``GET /sdk/ruleset``, with one key and no dependency
#: override. ``argv[1]`` names a JSON file with the key and the cases,
#: ``argv[2]`` the file the answers are written to.
_VECTORS = textwrap.dedent(
    """
    import json
    import sys
    from pathlib import Path
    from urllib.parse import quote

    from backend.app.models import register_core_models

    register_core_models()

    from fastapi.testclient import TestClient

    from backend.app.core.config import settings
    from backend.app.main import app
    from backend.app.services.metrics_service import MetricsService

    # The metric row of each evaluation is not what this checks, and writing
    # two of them per case dominates the run: as the vectors' other HTTP run,
    # the write is stubbed. The answer is the unmodified route's and service's.
    MetricsService.record_flag_evaluation = staticmethod(lambda **_: None)

    plan = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    headers = {"X-API-Key": plan["key"]}
    client = TestClient(app, raise_server_exceptions=False)
    answers = []
    for case in plan["cases"]:
        params = {"user_id": case["user_id"]}
        if case["context"] is not None:
            params["context"] = json.dumps(case["context"])
        response = client.get(
            "/api/v1/feature-flags/evaluate/" + quote(case["flag"], safe=""),
            params=params,
            headers=headers,
        )
        body = response.json()
        answers.append(
            {
                "status": response.status_code,
                "answer": {k: body.get(k) for k in ("enabled", "reason")},
                "value": json.dumps(body.get("value"), sort_keys=True),
            }
        )
    ruleset = client.get("/api/v1/sdk/ruleset", headers=headers)
    report = {
        "deployment": settings.ENVIRONMENT,
        "answers": answers,
        "ruleset": [ruleset.status_code, ruleset.headers.get("etag"), ruleset.text],
    }
    Path(sys.argv[2]).write_text(json.dumps(report), encoding="utf-8")
    """
)


def _deployment(name: str) -> dict:
    """The settings of a deployment whose ``ENVIRONMENT`` is *name*. Production
    refuses to start with the defaults, so it is given settings it accepts."""
    given = {
        "ENVIRONMENT": name,
        "AUTH_PROVIDER": "local",
        "SDK_RATE_LIMIT_PER_MINUTE": "1000000",
    }
    if name == "production":
        given.update(
            {
                "SECRET_KEY": secrets.token_hex(32),
                "AUDIT_HMAC_KEY": secrets.token_hex(32),
                "FIRST_SUPERUSER_PASSWORD": secrets.token_urlsafe(24),
                "PUBLIC_BASE_URL": "https://controlvar.example.com",
                "ALLOWED_HOSTS": "testserver",
            }
        )
    return given


def _vector_answers(tree, schema: str, tmp_path, key: str, cases, deployment: str):
    script = tmp_path / f"vectors-{uuid.uuid4().hex[:6]}.py"
    script.write_text(_VECTORS, encoding="utf-8")
    plan, answers = script.with_suffix(".plan.json"), script.with_suffix(".json")
    plan.write_text(json.dumps({"key": key, "cases": cases}), encoding="utf-8")
    done = tree_profiles.run(
        tree, [str(script), str(plan), str(answers)], schema, _deployment(deployment)
    )
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-4000:]
    report = json.loads(answers.read_text(encoding="utf-8"))
    assert report["deployment"] == deployment
    return report


def _has_lone_surrogate(value) -> bool:
    if isinstance(value, str):
        return any(0xD800 <= ord(ch) <= 0xDFFF for ch in value)
    if isinstance(value, dict):
        return any(
            _has_lone_surrogate(k) or _has_lone_surrogate(v) for k, v in value.items()
        )
    return isinstance(value, list) and any(map(_has_lone_surrogate, value))


def _vectors_0_48_0_stored() -> dict:
    """The vectors' flags that 0.48.0 could store, their entries and their
    cases: all but the ones with a split or a rule's own value (#2012)."""
    part = without_split_or_rule_value(
        json.loads(VECTORS_PATH.read_text(encoding="utf-8"))
    )
    return {
        "stored_flags": part["stored_flags"],
        "cases": part["cases"],
        "counts": {"cases": len(part["cases"])},
        "ruleset": {"flags": part["entries"]},
    }


def _store_vectors(engine, schema: str, vectors: dict) -> None:
    """The vectors' 161 flags, as 0.48.0 stores a flag, and their segment."""
    with engine.begin() as conn:
        for spec in vectors["stored_flags"]:
            conn.execute(
                text(
                    f'INSERT INTO "{schema}".feature_flags (id, key, name, status, '
                    "rollout_percentage, targeting_rules, value_type, on_value, "
                    "off_value, created_at, updated_at) VALUES (:id, :key, :name, "
                    ":status, :pct, CAST(:rules AS jsonb), :type, "
                    "CAST(:on AS jsonb), CAST(:off AS jsonb), now(), now())"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "key": spec["key"],
                    "name": spec["key"][:100],
                    "status": spec["status"],
                    "pct": spec["rollout_percentage"],
                    "rules": _json_or_null(spec["targeting_rules"]),
                    "type": spec.get("value_type", "boolean"),
                    "on": _json_or_null(spec.get("on_value")),
                    "off": _json_or_null(spec.get("off_value")),
                },
            )
    with _session(engine, schema) as db:
        segment = uuid.UUID(VECTOR_SEGMENT)
        db.add(
            Segment(
                id=segment,
                name="Vector segment",
                kind=SegmentKind.ID_LIST.value,
                status=SegmentStatus.ACTIVE,
            )
        )
        db.flush()
        for member in VECTOR_SEGMENT_MEMBERS[VECTOR_SEGMENT]:
            db.add(SegmentMember(segment_id=segment, member_id=member))
        db.commit()


def _json_or_null(value):
    return None if value is None else json.dumps(value)


def _flag_rows(engine, schema: str) -> list[dict]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                text(
                    f'SELECT to_jsonb(f) FROM "{schema}".feature_flags f ORDER BY f.id'
                )
            ).scalars()
        )


@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_the_upgrade_changes_no_answer_whatever_the_deployments_environment(
    profile, test_db, scratch_schema, tmp_path
):
    """The contract vectors' flags, stored by 0.48.0, are answered after the
    upgrade exactly as before it, to a key 0.48.0 created: in a deployment
    whose ``ENVIRONMENT`` is ``production`` and in one whose is
    ``development``. The ruleset and its ETag are the bytes 0.48.0 serves from
    the rows it stored, which the upgrade leaves as they were.

    2231 of the 2242 cases are sent; the other 11 hold a lone surrogate, which
    no client can put in a URL."""
    vectors = _vectors_0_48_0_stored()
    cases = [
        case
        for case in vectors["cases"]
        if not _has_lone_surrogate([case["user_id"], case["context"]])
    ]
    assert len(vectors["cases"]) == vectors["counts"]["cases"] == 2242
    assert len(cases) == 2242 - 11

    tree = _at_previous_release(test_db, profile, scratch_schema, tmp_path)
    user = _user(test_db, scratch_schema)
    key_id, key = _insert_previous_release_key(
        test_db, scratch_schema, user.id, scopes="sdk:ruleset"
    )
    _store_vectors(test_db, scratch_schema, vectors)
    stored = _flag_rows(test_db, scratch_schema)
    assert len(stored) == len(vectors["stored_flags"]) == 161

    upgrade = _upgrade(tree, scratch_schema)
    assert upgrade.returncode == 0, upgrade.stderr[-3000:]
    assert _rows(test_db, scratch_schema) == ROWS[profile]
    assert _key_environments(test_db, scratch_schema)[key_id] == ("production", True)
    # Column by column of 0.48.0's: a later revision may add one.
    upgraded = _flag_rows(test_db, scratch_schema)
    assert [
        {column: row[column] for column in old}
        for old, row in zip(stored, upgraded, strict=True)
    ] == stored
    # What 0.48.0 answers from those rows. Its copy runs on the upgraded schema
    # because it maps every column a later revision adds.
    previous = _previous_release(profile, tmp_path)
    before = _vector_answers(
        previous, scratch_schema, tmp_path, key, [], "development"
    )["ruleset"]
    assert before[0] == 200, before
    assert json.loads(before[2])["flags"] == vectors["ruleset"]["flags"]
    # Staging and development rows that would answer differently, for every
    # flag: nothing a production key is answered reads one.
    with test_db.begin() as conn:
        for environment in ("staging", "development"):
            conn.execute(
                text(
                    f'INSERT INTO "{scratch_schema}".{TABLE} (id, feature_flag_id, '
                    "environment, is_active, rollout_percentage, created_at, "
                    "updated_at) SELECT gen_random_uuid(), id, :env, "
                    "status <> 'ACTIVE', 100 - rollout_percentage, now(), now() "
                    f'FROM "{scratch_schema}".feature_flags'
                ),
                {"env": environment},
            )
    for deployment in ("development", "production"):
        after = _vector_answers(tree, scratch_schema, tmp_path, key, cases, deployment)
        changed = []
        for case, got in zip(cases, after["answers"], strict=True):
            assert got["status"] == 200, (case["id"], got)
            if got["answer"] != case["expected"] or (
                "expected_value" in case
                and got["value"] != json.dumps(case["expected_value"], sort_keys=True)
            ):
                changed.append((case, got))
        print(f"[{profile}] ENVIRONMENT={deployment}: {len(after['answers'])} answered")
        assert not changed, (
            f"{len(changed)} of {len(cases)} cases changed on upgrade; first "
            f"{changed[0][0]['flag']}/{changed[0][0]['user_id']}: expected "
            f"{list(changed[0][0]['expected'].values())} got "
            f"{list(changed[0][1]['answer'].values())} (ENVIRONMENT={deployment})"
        )
        assert len(after["answers"]) + 11 == 2242
        assert after["ruleset"] == before, (
            f"the ruleset changed on upgrade (ENVIRONMENT={deployment})"
        )
    shutil.rmtree(previous, ignore_errors=True)


# ---------------------------------------------------------------------------
# 12. The heads
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
