"""``t2012_flag_split``: a nullable split on each flag and each flag environment setting, through the transitions (#2012).

The revision adds ``feature_flags.split`` and ``flag_environment_settings.split``,
each only when absent, and nothing else. A schema built by ``create_all``
already has both, so the tests that start from before it take a bootstrapped
schema with the columns DROPPED and ``alembic_version`` at the earlier
revision's rows (``t2011_flag_environments``, or 0.48.0's heads with everything
0.49.0 adds dropped too), in each profile, and run the documented commands.
Then:

1. ``alembic upgrade heads``: the ``alembic_version`` rows are this release's
   (printed), no row of either table has a split, nothing else changes, and
   autogenerate is empty;
2. an upgraded database and a bootstrapped one have the same columns, checks
   and indexes on both tables, compared by name;
3. every index and constraint of both tables is what it was, the two checks
   and the unique key index of ``feature_flags`` among them;
4. this release's models store a Python ``None`` as SQL NULL in both columns,
   and the service's internal ``split`` is stored with its change, cleared by
   ``None`` and kept when left out;
5. the upgrade changes no answer: every stored flag and case of
   ``tests/sdk-contract/ruleset-vectors.json``, and flags holding the two
   shapes of ``variants`` older rows hold, are answered the same by the
   previous release before it and by this one after it, with a key created
   before it; the ruleset's body and ETag are the same;
6. the previous release's code (this tree without the revision, the columns
   and what carries, reads or writes them) creates, sets, lists, builds the
   ruleset and evaluates on this schema with a split flag and rule-value flags
   present: it serves each flag's on value to the users it is on for, its
   ruleset says the same, and its PUT keeps the split and the rule values;
7. the statements each direction runs, captured online: each ALTER of the
   upgrade straight between its bound and the reset, with no data step, and
   the downgrade's two locks, its read and its two drops;
8. the waits, each leg naming its revision, never ``heads``: a session reading
   either table ends the upgrade at its bound with this revision's refusal,
   applying nothing -- no revision of the run, when one run applies
   ``t2011_flag_environments`` too -- and the next run goes through; a deadlock
   ends it with the same refusal; a session reading either table ends the
   downgrade with its refusal, changing nothing;
9. the downgrade refuses while any flag has a split or a string, number or
   json flag's rule carries a value, in any environment: it names those flags,
   and changes nothing; it finds a rule's value exactly where
   ``flag_values.rule_values`` does; a split written while it waits for its
   lock is seen; once cleared, it drops both columns by name and nothing else;
10. offline (``--sql``), both directions refuse and emit no DDL, with and
    without a flag in use;
11. this release, still running against the downgraded schema, answers 500 to
    every flag evaluation, flag read, flag change and ruleset request;
12. one downgrade from this revision to 0.48.0's head, met by an SDK request
    that has read ``api_keys`` and then reads ``feature_flags``, ends in success
    or in a refusal with nothing changed, and never hangs;
13. ``alembic heads`` names exactly the core head (``t1934_metric_window``
    since #1934, which extends this revision) and the modules head.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import textwrap
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote, urlencode

import pytest
from sqlalchemy import text

from backend.app.core.flag_values import rule_values
from backend.app.models.feature_flag import (
    FeatureFlag,
    FeatureFlagStatus,
    FlagEnvironmentSetting,
)
from backend.app.models.segment import (
    Segment,
    SegmentKind,
    SegmentMember,
    SegmentStatus,
)
from backend.app.schemas.feature_flag import FeatureFlagUpdate
from backend.app.services.feature_flag_service import FeatureFlagService
from backend.app.services.metrics_service import MetricsService
from backend.scripts.generate_ruleset_vectors import (
    VECTOR_SEGMENT,
    VECTOR_SEGMENT_MEMBERS,
    VECTORS_PATH,
)
from backend.tests.integration.database import tree_profiles
from backend.tests.integration.database.metric_window_column import (
    without_the_metric_window,
)
from backend.tests.integration.database.split_read_path import without_the_read_path
from backend.tests.integration.database.split_write_path import without_the_write_path
from backend.tests.integration.database.test_flag_environments_migration import (
    _captured,
    _columns,
    _constraints,
    _deadlocks,
    _holding,
    _indexes,
    _insert_key,
    _key_column,
    _rows,
    _scratch,
    _session,
    _set_aside,
    _stamp,
    _tables,
    _token,
    _until_waiting_for,
    _user,
)
from backend.tests.integration.database.test_flag_environments_migration import (
    _to_previous_release as _to_0_48_0,
)
from backend.tests.integration.database.tree_profiles import CORE, FULL
from backend.tests.unit.core.test_flag_rule_values import SIX_SHAPES, STORED_RULES
from backend.tests.unit.services.test_sdk_ruleset_vectors import (
    without_split_or_rule_value,
)

pytestmark = [pytest.mark.integration]

#: This revision, the core revision it extends, and 0.48.0's core head.
REVISION = "t2012_flag_split"
PREVIOUS_CORE_HEAD = "t2011_flag_environments"
RELEASED_CORE_HEAD = "t449_identity_aliases"
#: The core head of this tree: ``t1934_metric_window`` (#1934), the revision
#: after this one, whose downgrade drops ``metrics.conversion_window_hours``.
CORE_HEAD = "t1934_metric_window"
MODULES_HEAD = "modules_0005_redshift"

PREVIOUS_ROWS = {
    CORE: {PREVIOUS_CORE_HEAD},
    FULL: {PREVIOUS_CORE_HEAD, MODULES_HEAD},
}
RELEASED_ROWS = {
    CORE: {RELEASED_CORE_HEAD},
    FULL: {RELEASED_CORE_HEAD, MODULES_HEAD},
}
ROWS = {CORE: {CORE_HEAD}, FULL: {CORE_HEAD, MODULES_HEAD}}
#: The rows after ``upgrade t2012_flag_split`` and no further, and where every
#: downgrade leg starts: this revision's own, whatever later revision the
#: tree's head is.
REVISION_ROWS = {CORE: {REVISION}, FULL: {REVISION, MODULES_HEAD}}

FLAGS = "feature_flags"
SETTINGS = "flag_environment_settings"
TABLES = (FLAGS, SETTINGS)
COLUMN = "split"
#: The column after either path: (``format_type``, NOT NULL, default).
EXPECTED_COLUMN = ("jsonb", False, None)
EXPECTED_COLUMNS = dict.fromkeys(TABLES, EXPECTED_COLUMN)
VERSIONS = (
    Path(__file__).resolve().parents[3] / "app" / "db" / "migrations" / "versions"
)

#: Each online leg's own bound on any one statement: a wait the revision did
#: not bound ends here (57014), never in a hung test.
LEG_STATEMENT_TIMEOUT = "-c statement_timeout=10s"
#: A ceiling on finding a waiting lock or on a process ending, never an
#: assertion on how long either took.
PROCESS_SECONDS = 120

#: The full-profile case needs a ``modules/`` directory; the core one builds
#: its own sealed tree and runs in either checkout.
BOTH_PROFILES = [CORE, pytest.param(FULL, marks=pytest.mark.modules)]

UPGRADED = "upgraded"
BOOTSTRAPPED = "bootstrapped"

#: Where an upgrade starts: the revision before this one, where it runs this
#: one alone, and 0.48.0's heads, where one run applies
#: ``t2011_flag_environments`` and then this one.
FROM_T2011 = "from-t2011"
FROM_0_48_0 = "from-0.48.0"
STARTS = [FROM_0_48_0, FROM_T2011]

SPLIT = [
    {"value": "red", "weight": 50},
    {"value": "green", "weight": 30},
    {"value": "blue", "weight": 20},
]
#: The two shapes of ``feature_flags.variants`` a stored row may hold. Nothing
#: reads the column.
LEGACY_VARIANTS = {
    "legacy-variants-one": {"control": {"value": False}},
    "legacy-variants-two": {"control": {"value": False}, "treatment": {"value": True}},
}


@pytest.fixture
def scratch_schema(test_db):
    yield from _scratch(test_db, "flagsplit")


@pytest.fixture
def reference_schema(test_db):
    yield from _scratch(test_db, "flagsplitref")


# ---------------------------------------------------------------------------
# Reading the catalog and the rows
# ---------------------------------------------------------------------------
def _split_columns(engine, schema: str) -> dict[str, tuple]:
    """``table -> (format_type, NOT NULL, default)`` where it has the column."""
    return {
        table: row[1:]
        for table in TABLES
        for row in _columns(engine, schema, table)
        if row[0] == COLUMN
    }


def _definition(engine, schema: str) -> dict[str, tuple]:
    """Both tables' columns by name, constraints and indexes, schema-neutral."""
    return {
        table: (
            {row[0]: row[1:] for row in _columns(engine, schema, table)},
            _constraints(engine, schema, table),
            _indexes(engine, schema, table),
        )
        for table in TABLES
    }


def _stored(engine, schema: str) -> dict[str, list[tuple]]:
    """Every row of both tables, every column, as JSON text."""
    found = {}
    with engine.connect() as conn:
        for table in TABLES:
            found[table] = sorted(
                json.dumps(row, sort_keys=True)
                for row in conn.execute(
                    text(f'SELECT to_jsonb(t) FROM "{schema}".{table} t')
                ).scalars()
            )
    return found


def _split_count(engine, schema: str, table: str) -> int:
    with engine.connect() as conn:
        return conn.execute(
            text(f'SELECT count(*) FROM "{schema}".{table} WHERE split IS NOT NULL')
        ).scalar()


def _report(profile, label, engine, schema) -> None:
    print(f"[{profile}] {label}: alembic_version={sorted(_rows(engine, schema))}")
    print(f"[{profile}] {label}: split={_split_columns(engine, schema)}")


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
# Building the databases
# ---------------------------------------------------------------------------
def _bootstrapped(profile, schema, tmp_path):
    tree = tree_profiles.tree_for(profile, tmp_path)
    created = tree_profiles.bootstrap_schema(tree, schema)
    assert created.returncode == 0, created.stderr[-3000:]
    return tree


def _to_t2011(engine, profile, schema) -> None:
    """Turn a bootstrapped *schema* into what ``t2011_flag_environments`` left."""
    with engine.begin() as conn:
        for table in TABLES:
            # IF EXISTS: a plant that removes it from a model leaves create_all
            # without it, and the test must reach the assertion that says so.
            conn.execute(
                text(f'ALTER TABLE "{schema}".{table} DROP COLUMN IF EXISTS {COLUMN}')
            )
    _stamp(engine, schema, PREVIOUS_ROWS[profile])
    assert _split_columns(engine, schema) == {}


def _starting(test_db, profile, schema, tmp_path, start):
    """A schema at *start*, and the tree to upgrade it with."""
    tree = _bootstrapped(profile, schema, tmp_path)
    if start == FROM_T2011:
        _to_t2011(test_db, profile, schema)
    else:
        _to_0_48_0(test_db, profile, schema)
        assert _split_columns(test_db, schema) == {}
    return tree


def _at_this_revision(test_db, profile, schema, tmp_path):
    """A schema whose ``alembic_version`` is this revision's, named, however
    far the tree's head is beyond it: a downgrade from here runs this
    revision's downgrade first."""
    tree = _bootstrapped(profile, schema, tmp_path)
    _stamp(test_db, schema, REVISION_ROWS[profile])
    assert _split_columns(test_db, schema) == EXPECTED_COLUMNS
    return tree


def _upgrade(tree, schema: str) -> subprocess.CompletedProcess:
    return tree_profiles.alembic(tree, schema, "upgrade", "heads")


def _at_head(test_db, profile, built_by, schema, tmp_path):
    if built_by == UPGRADED:
        tree = _starting(test_db, profile, schema, tmp_path, FROM_0_48_0)
        upgrade = _upgrade(tree, schema)
        assert upgrade.returncode == 0, upgrade.stderr[-3000:]
        return tree
    return _bootstrapped(profile, schema, tmp_path)


def _leg(tree, schema: str, *argv: str, options: str = LEG_STATEMENT_TIMEOUT):
    """``alembic <argv>``, which names a revision, under the leg's own timeout."""
    return tree_profiles.alembic(tree, schema, *argv, extra_env={"PGOPTIONS": options})


@contextlib.contextmanager
def _in_the_background(
    tree, schema: str, *argv: str, options: str = LEG_STATEMENT_TIMEOUT
):
    """``alembic <argv>`` running in another process. Yields ``(process,
    application_name)``; the process is ended before this returns."""
    application = f"t2012-{uuid.uuid4().hex[:8]}"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(tree_profiles.alembic_ini(tree)),
            *argv,
        ],
        cwd=str(tree),
        env=tree_profiles._environment(
            tree, schema, {"PGAPPNAME": application, "PGOPTIONS": options}
        ),
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


def _table_lock(schema: str, table: str):
    def wanted(request: tuple) -> bool:
        return request[1:] == ("AccessExclusiveLock", f"{schema}.{table}")

    return wanted


def _until_the_pid_waits(engine, pid: int) -> str:
    """The table that backend *pid* waits for a lock on, once it waits."""
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        with engine.connect() as conn:
            waiting = conn.execute(
                text(
                    "SELECT relation::regclass::text FROM pg_locks "
                    "WHERE pid = :p AND NOT granted"
                ),
                {"p": pid},
            ).scalar()
            conn.rollback()
        if waiting:
            return waiting
        time.sleep(0.01)
    raise AssertionError(f"backend {pid} never waited")


def _reading(schema: str, table: str) -> str:
    return f'SELECT count(*) FROM "{schema}".{table}'


def _insert_flag(engine, schema: str, key: str, **columns) -> str:
    """A flag row, written with SQL: only the columns named, so it can be
    written before the column exists too."""
    values = {
        "id": str(uuid.uuid4()),
        "key": key,
        "name": key[:100],
        "status": "ACTIVE",
        "rollout_percentage": 100,
        "value_type": "boolean",
        **columns,
    }
    json_columns = {"targeting_rules", "on_value", "off_value", "variants", "split"}
    names = list(values)
    placeholders = [
        f"CAST(:{name} AS jsonb)" if name in json_columns else f":{name}"
        for name in names
    ]
    with engine.begin() as conn:
        conn.execute(
            text(
                f'INSERT INTO "{schema}".{FLAGS} ({", ".join(names)}, created_at, '
                f"updated_at) VALUES ({', '.join(placeholders)}, now(), now())"
            ),
            {
                name: json.dumps(value) if name in json_columns else value
                for name, value in values.items()
            },
        )
    return values["id"]


def _insert_setting(engine, schema: str, flag_id: str, environment: str, **columns):
    values = {
        "id": str(uuid.uuid4()),
        "feature_flag_id": flag_id,
        "environment": environment,
        "is_active": True,
        "rollout_percentage": 100,
        **columns,
    }
    json_columns = {"targeting_rules", "split"}
    names = list(values)
    placeholders = [
        f"CAST(:{name} AS jsonb)" if name in json_columns else f":{name}"
        for name in names
    ]
    with engine.begin() as conn:
        conn.execute(
            text(
                f'INSERT INTO "{schema}".{SETTINGS} ({", ".join(names)}, created_at, '
                f"updated_at) VALUES ({', '.join(placeholders)}, now(), now())"
            ),
            {
                name: json.dumps(value) if name in json_columns else value
                for name, value in values.items()
            },
        )


def _string_flag(engine, schema: str, key: str, **columns) -> str:
    return _insert_flag(
        engine,
        schema,
        key,
        value_type="string",
        on_value="A",
        off_value="Z",
        **columns,
    )


DASHBOARD_RULE = {
    "logical_operator": "AND",
    "groups": [
        {
            "logical_operator": "AND",
            "conditions": [{"attribute": "plan", "operator": "equals", "value": "pro"}],
        }
    ],
}
NATIVE_RULE = {
    "id": "pro",
    "rule": {
        "operator": "and",
        "conditions": [{"attribute": "plan", "operator": "eq", "value": "pro"}],
    },
    "rollout_percentage": 60,
}


# ---------------------------------------------------------------------------
# 1. The earlier revision, upgraded with `heads`
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("start", STARTS)
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_an_earlier_database_gains_the_two_columns_and_no_data(
    profile, start, test_db, scratch_schema, tmp_path
):
    tree = _starting(test_db, profile, scratch_schema, tmp_path, start)
    flag_id = _insert_flag(test_db, scratch_schema, "on-flag")
    _string_flag(test_db, scratch_schema, "string-flag")
    for key, variants in LEGACY_VARIANTS.items():
        _insert_flag(test_db, scratch_schema, key, variants=variants)
    if start == FROM_T2011:
        _insert_setting(test_db, scratch_schema, flag_id, "staging")
        _insert_setting(test_db, scratch_schema, flag_id, "development")
    tables = _tables(test_db, scratch_schema)
    with test_db.connect() as conn:
        before = conn.execute(
            text(f'SELECT to_jsonb(f) FROM "{scratch_schema}".{FLAGS} f ORDER BY key')
        ).scalars()
        before = list(before)
    _report(profile, f"{start}, before", test_db, scratch_schema)

    upgrade = _upgrade(tree, scratch_schema)
    assert upgrade.returncode == 0, upgrade.stderr[-3000:]
    _report(profile, f"{start}, after upgrade heads", test_db, scratch_schema)

    assert _rows(test_db, scratch_schema) == ROWS[profile]
    assert _split_columns(test_db, scratch_schema) == EXPECTED_COLUMNS
    assert _tables(test_db, scratch_schema) == tables | {SETTINGS}
    for table in TABLES:
        carrying = _split_count(test_db, scratch_schema, table)
        assert carrying == 0, (
            f"{carrying} rows of {table} carry a split after upgrade, expected 0"
        )
    # Every other column of every flag is what it was, ``variants`` included.
    with test_db.connect() as conn:
        after = conn.execute(
            text(
                f"SELECT to_jsonb(f) - 'split' FROM \"{scratch_schema}\".{FLAGS} f "
                "ORDER BY key"
            )
        ).scalars()
        after = list(after)
    assert after == before
    assert {row["key"]: row["variants"] for row in after if row["variants"]} == (
        LEGACY_VARIANTS
    )

    result = tree_profiles.autogenerate(
        tree, scratch_schema, tmp_path, head=tree_profiles.CORE_HEAD
    )
    print(f"[{profile}/{start}] autogenerate: {result.operations}")
    assert result.body is not None, result.describe()
    assert result.operations == [], result.describe()


# ---------------------------------------------------------------------------
# 2-4. The migration's columns are the models', and nothing else moves
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("start", STARTS)
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_an_upgraded_database_and_a_bootstrapped_one_have_the_same_tables(
    profile, start, test_db, scratch_schema, reference_schema, tmp_path
):
    """Autogenerate compares neither a check nor a server default, so both
    tables are compared from the catalog, their columns by name (an added
    column comes last in an upgraded table)."""
    tree = _starting(test_db, profile, scratch_schema, tmp_path, start)
    upgrade = _upgrade(tree, scratch_schema)
    assert upgrade.returncode == 0, upgrade.stderr[-3000:]
    created = tree_profiles.bootstrap_schema(tree, reference_schema)
    assert created.returncode == 0, created.stderr[-3000:]
    upgraded = _definition(test_db, scratch_schema)
    bootstrapped = _definition(test_db, reference_schema)
    for table in TABLES:
        assert upgraded[table] == bootstrapped[table], table
        assert bootstrapped[table][0][COLUMN] == EXPECTED_COLUMN, table


@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_the_upgrade_keeps_every_index_and_constraint_the_previous_release_relies_on(
    profile, test_db, scratch_schema, tmp_path
):
    tree = _starting(test_db, profile, scratch_schema, tmp_path, FROM_T2011)
    before = _definition(test_db, scratch_schema)
    upgrade = _upgrade(tree, scratch_schema)
    assert upgrade.returncode == 0, upgrade.stderr[-3000:]
    after = _definition(test_db, scratch_schema)
    for table in TABLES:
        columns, constraints, indexes = before[table]
        assert after[table] == (
            {**columns, COLUMN: EXPECTED_COLUMN},
            constraints,
            indexes,
        ), table

    _, constraints, indexes = after[FLAGS]
    checks = {name: definition for name, kind, definition in constraints if kind == "c"}
    assert checks["check_rollout_percentage"] == (
        "CHECK (((rollout_percentage >= 0) AND (rollout_percentage <= 100)))"
    )
    # The check on the value type and the two values still names no split, so
    # a write the previous release makes is refused exactly as it was.
    assert "on_value" in checks["check_flag_values"]
    assert COLUMN not in checks["check_flag_values"]
    assert (
        "CREATE UNIQUE INDEX ix_<schema>_feature_flags_key ON "
        "<schema>.feature_flags USING btree (key)"
    ) in indexes


@pytest.mark.regression
def test_this_releases_models_store_a_none_split_as_sql_null(
    test_db, scratch_schema, tmp_path
):
    """``JSONB`` writes ``None`` as JSON ``null`` unless ``none_as_null``, and
    the downgrade would then find a split on every flag a writer copied."""
    _bootstrapped(CORE, scratch_schema, tmp_path)
    with _session(test_db, scratch_schema) as db:
        flag = FeatureFlag(
            key="none-split",
            name="None split",
            status=FeatureFlagStatus.INACTIVE,
            split=None,
        )
        db.add(flag)
        db.commit()
        setting = FlagEnvironmentSetting(
            feature_flag_id=flag.id, environment="staging", split=None
        )
        db.add(setting)
        db.commit()
        for row, table in ((flag, FLAGS), (setting, SETTINGS)):
            assert _split_count(test_db, scratch_schema, table) == 0, table
            row.split = SPLIT
            db.commit()
            assert _split_count(test_db, scratch_schema, table) == 1, table
            db.refresh(row)
            assert row.split == SPLIT, table
            row.split = None
            db.commit()
            assert _split_count(test_db, scratch_schema, table) == 0, table


@pytest.mark.regression
def test_the_service_stores_clears_and_keeps_productions_split(
    test_db, scratch_schema, tmp_path
):
    """``update_feature_flag``'s internal ``split``: given, it is stored in the
    change's own commit, with its audit entries; ``None`` clears it; left out,
    the stored one is kept. Staging's is never touched."""
    _bootstrapped(CORE, scratch_schema, tmp_path)
    flag_id = _string_flag(test_db, scratch_schema, "promoted", rollout_percentage=10)
    staging = [{"value": "green", "weight": 100}]
    _insert_setting(test_db, scratch_schema, flag_id, "staging", split=staging)

    def stored() -> tuple:
        with test_db.connect() as conn:
            return tuple(
                conn.execute(
                    text(
                        "SELECT f.rollout_percentage, f.split, s.split "
                        f'FROM "{scratch_schema}".{FLAGS} f '
                        f'JOIN "{scratch_schema}".{SETTINGS} s '
                        "ON s.feature_flag_id = f.id WHERE f.id::text = :id"
                    ),
                    {"id": flag_id},
                ).one()
            )

    with _session(test_db, scratch_schema) as db:
        service = FeatureFlagService(db)

        def update(percentage, **kwargs):
            return service.update_feature_flag(
                flag_id, FeatureFlagUpdate(rollout_percentage=percentage), **kwargs
            )

        recorded = []
        updated = update(20, split=SPLIT, record=lambda f: recorded.append(f.split))
        assert updated.split == SPLIT and recorded == [SPLIT]
        assert stored() == (20, SPLIT, staging)
        update(30)
        assert stored() == (30, SPLIT, staging), "a split left out is kept"
        update(40, split=None)
        assert stored() == (40, None, staging), "None clears it"
        assert _split_count(test_db, scratch_schema, FLAGS) == 0

        # A change whose audit entry fails saves neither half.
        def refuse(flag):
            raise RuntimeError("no audit entry")

        with pytest.raises(RuntimeError, match="no audit entry"):
            update(50, split=SPLIT, record=refuse)
        assert stored() == (40, None, staging)


# ---------------------------------------------------------------------------
# The previous release's code, and this release's, in a subprocess
# ---------------------------------------------------------------------------
_SPLIT_DECLARATION = (
    r"^    split = Column\(JSONB\(none_as_null=True\), nullable=True\)\n"
)
#: What #2012 adds to the code, each found exactly once; if one is not, the
#: test says so rather than testing the wrong tree.
_ADDED = {
    ("models", "feature_flag.py"): (
        re.compile(
            r"(?ms)^    # The flag's split in production \(#2012\).*?"
            + _SPLIT_DECLARATION
        ),
        re.compile(
            r"(?ms)^    # The flag's split in this environment \(#2012\).*?"
            + _SPLIT_DECLARATION
        ),
    ),
    ("core", "flag_environments.py"): (
        re.compile(r'(?m)^    "split",\n'),
        re.compile(r'(?m)^        "split",\n'),
        re.compile(
            r'(?m)^            "split": None if setting is None else setting\.split,\n'
        ),
    ),
    ("services", "feature_flag_service.py"): (
        re.compile(
            r"(?ms)^#: ``update_feature_flag``'s ``split``.*?"
            r"^KEEP_SPLIT: Any = object\(\)\n"
        ),
        re.compile(r"(?m)^        split: Any = KEEP_SPLIT,\n"),
        re.compile(
            r"(?m)^            if split is not KEEP_SPLIT:\n"
            r"                flag\.split = split\n"
        ),
        # The update's check, which counts the rules with the split it writes.
        re.compile(r"(?m), split: Any = KEEP_SPLIT$"),
        re.compile(
            r"(?m)^        if split is not KEEP_SPLIT:\n"
            r'            stored\["split"\] = split\n'
        ),
        re.compile(r"(?<=_check_sent_values\(flag_data, flag), split(?=\))"),
    ),
    # A promotion's copy, and its audit entry's name for it.
    ("services", "flag_environment_service.py"): (
        re.compile(r'(?m)^    "split",\n'),
        re.compile(r'(?m)^    "split": None,\n'),
        re.compile(r'(?m)^        "split": view\.split,\n'),
        re.compile(
            r'(?m)^        "#split": name_only_fingerprint\(stored\["split"\]\),\n'
        ),
    ),
    ("api", "v1", "endpoints", "flag_environments.py"): (
        re.compile(r"(?m)^    KEEP_SPLIT,\n"),
        re.compile(r'(?m)^            split=changes\.get\("split", KEEP_SPLIT\),\n'),
    ),
    ("services", "audit_service.py"): (re.compile(r'(?m)^        "split",\n'),),
}


def _previous_release(profile: str, tmp_path) -> Path:
    """Release N-1, 0.48.0: a copy of this tree without the revision, the
    columns and what carries, writes, reads or takes them in a request."""
    destination = tmp_path / "release-n-minus-1"
    if profile == FULL:
        tree = tree_profiles.full_tree(destination)
    else:
        tree = tree_profiles.core_tree(destination)
    app = tree / "backend" / "app"
    (revision_file,) = (app / "db" / "migrations" / "versions").glob(f"{REVISION}_*.py")
    revision_file.unlink()
    # And the revision after this one, ``t1934_metric_window``, with its
    # column: the previous release has neither.
    without_the_metric_window(tree)
    for parts, additions in _ADDED.items():
        module = app.joinpath(*parts)
        source = module.read_text(encoding="utf-8")
        for addition in additions:
            source, found = addition.subn("", source)
            assert found == 1, f"{found} matches of {addition.pattern}: update it"
        module.write_text(source, encoding="utf-8")
    without_the_read_path(tree)
    without_the_write_path(tree)
    return tree


#: Runs in a tree, against a schema: each request of ``argv[1]`` (a JSON file:
#: ``[method, path, headers, body]``) through the app's own routes, with no
#: dependency override, and writes ``[status, body, etag]`` for each to
#: ``argv[2]``. The per-evaluation metric rows are not what these tests check,
#: so that write is stubbed; the answers are the routes' own.
_CLIENT = textwrap.dedent(
    """
    import json
    import sys

    from backend.app.models import register_core_models

    register_core_models()

    from fastapi.testclient import TestClient

    from backend.app.main import app
    from backend.app.models.feature_flag import FeatureFlag
    from backend.app.services.metrics_service import MetricsService

    MetricsService.record_flag_evaluation = staticmethod(lambda **_: None)
    client = TestClient(app, raise_server_exceptions=False)
    answers = []
    with open(sys.argv[1], encoding="utf-8") as handle:
        requests = json.load(handle)
    for method, path, headers, body in requests:
        response = client.request(method, path, headers=headers, json=body)
        try:
            payload = response.json()
        except ValueError:
            payload = response.text[:500]
        answers.append([response.status_code, payload, response.headers.get("etag")])
    with open(sys.argv[2], "w", encoding="utf-8") as handle:
        json.dump(
            {
                "flag_columns": sorted(FeatureFlag.__table__.columns.keys()),
                "answers": answers,
            },
            handle,
        )
    """
)

#: What a test deployment sets for these subprocesses: local sign-in and the
#: test settings, as the API tests run; no scheduler starts (no lifespan).
_CLIENT_ENV = {"ENVIRONMENT": "test", "AUTH_PROVIDER": "local"}


def _client(tree, schema: str, tmp_path, requests: list) -> dict:
    stem = f"client-{uuid.uuid4().hex[:6]}"
    script, sent, received = (
        tmp_path / f"{stem}{suffix}" for suffix in (".py", ".in.json", ".out.json")
    )
    script.write_text(_CLIENT, encoding="utf-8")
    sent.write_text(json.dumps(requests), encoding="utf-8")
    done = tree_profiles.run(
        tree, [str(script), str(sent), str(received)], schema, _CLIENT_ENV
    )
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-4000:]
    return json.loads(received.read_text(encoding="utf-8"))


def _answers(tree, schema: str, tmp_path, requests: list) -> list:
    """``[status, body, etag]`` for each request, in order."""
    return _client(tree, schema, tmp_path, requests)["answers"]


def _evaluate(flag_key: str, user_id: str, api_key: str, context=None) -> list:
    query = {"user_id": user_id}
    if context is not None:
        query["context"] = json.dumps(context)
    return [
        "GET",
        f"/api/v1/feature-flags/evaluate/{quote(flag_key, safe='')}?{urlencode(query)}",
        {"X-API-Key": api_key},
        None,
    ]


def _ruleset(api_key: str) -> list:
    return ["GET", "/api/v1/sdk/ruleset", {"X-API-Key": api_key}, None]


# ---------------------------------------------------------------------------
# 5. The upgrade changes no answer
# ---------------------------------------------------------------------------
def _well_formed(value) -> bool:
    """Whether every string in *value* can be put in a URL (no lone surrogate)."""
    if isinstance(value, str):
        return not any(0xD800 <= ord(ch) <= 0xDFFF for ch in value)
    if isinstance(value, list):
        return all(_well_formed(item) for item in value)
    if isinstance(value, dict):
        return all(_well_formed(k) and _well_formed(v) for k, v in value.items())
    return True


def _store_corpus(engine, schema: str, stored_flags: list) -> None:
    """*stored_flags* of the vectors and the segment some name, and the flags
    holding ``variants``, as the previous release stored them (no split)."""
    for spec in stored_flags:
        columns = {
            "status": spec["status"],
            "rollout_percentage": spec["rollout_percentage"],
            "targeting_rules": spec["targeting_rules"],
        }
        if "value_type" in spec:
            columns |= {
                name: spec[name] for name in ("value_type", "on_value", "off_value")
            }
        _insert_flag(engine, schema, spec["key"], **columns)
    for key, variants in LEGACY_VARIANTS.items():
        _insert_flag(engine, schema, key, rollout_percentage=50, variants=variants)
    with _session(engine, schema) as db:
        segment = Segment(
            id=uuid.UUID(VECTOR_SEGMENT),
            name="Vector segment",
            kind=SegmentKind.ID_LIST.value,
            status=SegmentStatus.ACTIVE,
        )
        db.add(segment)
        db.flush()
        for member in VECTOR_SEGMENT_MEMBERS[VECTOR_SEGMENT]:
            db.add(SegmentMember(segment_id=segment.id, member_id=member))
        db.commit()


@pytest.mark.regression
def test_the_upgrade_changes_no_answer(test_db, scratch_schema, tmp_path):
    """The stored flags and cases of the SDK contract vectors, and flags with
    ``variants``, answered over HTTP by the previous release before the upgrade
    and by this one after it, with one key created before it."""
    tree = _starting(test_db, CORE, scratch_schema, tmp_path, FROM_T2011)
    corpus = without_split_or_rule_value(
        json.loads(VECTORS_PATH.read_text(encoding="utf-8"))
    )
    _store_corpus(test_db, scratch_schema, corpus["stored_flags"])
    user = _user(test_db, scratch_schema)
    _, key = _insert_key(test_db, scratch_schema, user.id, "production", "sdk:ruleset")

    cases, unsent = [], []
    for case in corpus["cases"]:
        sendable = _well_formed(case["user_id"]) and _well_formed(case["context"])
        (cases if sendable else unsent).append(case)
    legacy = [(k, f"user-{n}") for k in sorted(LEGACY_VARIANTS) for n in range(20)]
    requests = [
        *(_evaluate(c["flag"], c["user_id"], key, c["context"]) for c in cases),
        *(_evaluate(k, user_id, key) for k, user_id in legacy),
        ["GET", "/api/v1/feature-flags/user/user-1", {"X-API-Key": key}, None],
        _ruleset(key),
    ]
    release_n_minus_1 = _previous_release(CORE, tmp_path)
    report = _client(release_n_minus_1, scratch_schema, tmp_path, requests)
    assert COLUMN not in report["flag_columns"]
    before = report["answers"]

    upgrade = _upgrade(tree, scratch_schema)
    assert upgrade.returncode == 0, upgrade.stderr[-3000:]
    assert _rows(test_db, scratch_schema) == ROWS[CORE]
    report = _client(tree, scratch_schema, tmp_path, requests)
    assert COLUMN in report["flag_columns"]
    after = report["answers"]

    # Every case was asked and answered but those an ill-formed string keeps out
    # of a URL, which no SDK can send either.
    print(
        f"{len(corpus['stored_flags'])} flags, {len(cases)} of "
        f"{len(corpus['cases'])} cases over HTTP, {len(legacy)} legacy answers"
    )
    assert (len(corpus["stored_flags"]), len(corpus["cases"])) == (161, 2242)
    assert len(cases) > 2200, len(unsent)
    assert not any(case["must_local"] for case in unsent)
    assert {status for status, _, _ in before} == {200}, [
        (r[1], a) for r, a in zip(requests, before) if a[0] != 200
    ][:5]
    changed = [
        f"{request[1]}: {then} before, {now} after"
        for request, then, now in zip(requests, before, after)
        if then != now
    ]
    assert changed == [], (len(changed), changed[:5])
    # And they are the vectors' answers, so both sides are not simply wrong.
    wrong = [
        (case["id"], body, case["expected"])
        for case, (_, body, _) in zip(cases, after)
        if {k: body[k] for k in ("enabled", "reason")} != case["expected"]
    ]
    assert wrong == [], (len(wrong), wrong[:5])
    legacy_bodies = [
        body for _, body, _ in after[len(cases) : len(cases) + len(legacy)]
    ]
    enabled = {body["enabled"] for body in legacy_bodies}
    assert enabled == {True, False}
    assert {body["reason"] for body in legacy_bodies} == {"rollout"}
    assert all(body["value"] is body["enabled"] for body in legacy_bodies)
    # The ruleset: same body, same ETag, an entry per flag.
    status, ruleset, etag = after[-1]
    assert status == 200 and etag == f'"{ruleset["version"]}"' == before[-1][2]
    assert len(ruleset["flags"]) == 161 + len(LEGACY_VARIANTS)
    shutil.rmtree(release_n_minus_1, ignore_errors=True)


# ---------------------------------------------------------------------------
# 6. The previous release on this schema
# ---------------------------------------------------------------------------
USERS = [f"user-{n}" for n in range(100)]
PRO = {"plan": "pro"}
#: The flags N-1 meets, each a string flag whose on value is "A" and off value
#: "Z": the columns, and the reason it answers a user with ``plan: pro``.
MULTI_WAY = {
    "split-flag": ({"split": SPLIT, "rollout_percentage": 50}, "rollout"),
    "rule-value-flag": (
        {"targeting_rules": {**DASHBOARD_RULE, "value": "B", "rollout_percentage": 60}},
        "targeting_rule",
    ),
    "native-rule-value-flag": (
        {"targeting_rules": {"rules": [{**NATIVE_RULE, "value": "B"}]}},
        "targeting_rule",
    ),
    "split-and-rule-value-flag": (
        {"split": SPLIT, "targeting_rules": {**DASHBOARD_RULE, "value": "B"}},
        "targeting_rule",
    ),
}


def _insert_multi_way(engine, schema: str) -> dict[str, str]:
    """The flags of :data:`MULTI_WAY`, through this release's model."""
    ids = {}
    with _session(engine, schema) as db:
        for key, (columns, _) in MULTI_WAY.items():
            flag = FeatureFlag(
                key=key,
                name=key,
                status=FeatureFlagStatus.ACTIVE,
                value_type="string",
                on_value="A",
                off_value="Z",
                **{"rollout_percentage": 100, **columns},
            )
            db.add(flag)
            db.commit()
            ids[key] = str(flag.id)
    return ids


def _split_and_rules(engine, schema: str, flag_ids) -> dict:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                f'SELECT id::text, split, targeting_rules FROM "{schema}".{FLAGS} '
                "WHERE id::text = ANY(:ids)"
            ),
            {"ids": list(flag_ids)},
        ).all()
    return {flag_id: (split, rules) for flag_id, split, rules in rows}


def _this_release_decides(engine, schema: str, flag_key: str, context) -> dict:
    """``enabled`` and ``reason`` for each user, from this release's service:
    a split or a rule value never changes either. Nothing is recorded, as in
    ``_CLIENT``: each record would be a commit of its own."""
    unrecorded = patch.object(MetricsService, "record_flag_evaluation", lambda **_: 0)
    with unrecorded, _session(engine, schema) as db:
        flag = db.query(FeatureFlag).filter(FeatureFlag.key == flag_key).one()
        service = FeatureFlagService(db)
        decided = {}
        for user in USERS:
            detail = service.evaluate_flag_detailed(flag, user, context)
            decided[user] = (detail["enabled"], detail["reason"])
        db.rollback()
    return decided


@pytest.mark.regression
@pytest.mark.parametrize("built_by", [UPGRADED, BOOTSTRAPPED])
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_the_previous_release_ignores_splits_and_rule_values_and_keeps_them(
    profile, built_by, test_db, scratch_schema, tmp_path
):
    """What a task of the previous release does during a rolling upgrade, and
    after an API-only rollback."""
    _at_head(test_db, profile, built_by, scratch_schema, tmp_path)
    assert _rows(test_db, scratch_schema) == ROWS[profile]
    user = _user(test_db, scratch_schema)
    _, key = _insert_key(test_db, scratch_schema, user.id, "production", "sdk:ruleset")
    ids = _insert_multi_way(test_db, scratch_schema)
    stored = _split_and_rules(test_db, scratch_schema, ids.values())
    assert sum(split is not None for split, _ in stored.values()) == 2

    release_n_minus_1 = _previous_release(profile, tmp_path)
    headers = {"Authorization": f"Bearer {_token(user)}"}
    flags_url = "/api/v1/feature-flags/"
    created = _client(
        release_n_minus_1,
        scratch_schema,
        tmp_path,
        [["POST", flags_url, headers, {"key": "n-minus-1", "name": "N-1"}]],
    )
    assert COLUMN not in created["flag_columns"]
    ((status, flag, _),) = created["answers"]
    assert status in (200, 201), flag

    evaluated = [(k, u) for k in MULTI_WAY for u in USERS]
    answers = _answers(
        release_n_minus_1,
        scratch_schema,
        tmp_path,
        [
            ["PUT", flags_url + flag["id"], headers, {"rollout_percentage": 25}],
            ["GET", flags_url, headers, None],
            _ruleset(key),
            *(_evaluate(k, u, key, PRO) for k, u in evaluated),
        ],
    )
    (put, listed, ruleset), bodies = answers[:3], answers[3:]
    assert put[0] == 200 and put[1]["rollout_percentage"] == 25, put
    assert _split_and_rules(test_db, scratch_schema, [flag["id"]]) == {
        flag["id"]: (None, None)
    }
    assert listed[0] == 200, listed
    items = listed[1]["items"] if isinstance(listed[1], dict) else listed[1]
    assert sorted(item["key"] for item in items) == sorted([*MULTI_WAY, "n-minus-1"])

    # It serves each flag's on value to the users it is on for, whatever the
    # split or the rule's value says, and the off value to the rest.
    assert {status for status, _, _ in bodies} == {200}
    by_flag: dict[str, dict] = {k: {} for k in MULTI_WAY}
    for (flag_key, user_id), (_, body, _) in zip(evaluated, bodies):
        by_flag[flag_key][user_id] = body
        assert body["value"] == ("A" if body["enabled"] else "Z"), body
    for flag_key, (_, reason) in MULTI_WAY.items():
        answered = {
            u: (b["enabled"], b["reason"]) for u, b in by_flag[flag_key].items()
        }
        assert {r for _, r in answered.values()} == {reason}, flag_key
        assert answered == _this_release_decides(
            test_db, scratch_schema, flag_key, PRO
        ), flag_key
    for flag_key in ("split-flag", "rule-value-flag", "native-rule-value-flag"):
        on = sum(b["enabled"] for b in by_flag[flag_key].values())
        assert 0 < on < len(USERS), (flag_key, on)

    # Its ruleset says the same: the on value, no split, no rule value.
    assert ruleset[0] == 200, ruleset
    entries = {e["key"]: e for e in ruleset[1]["flags"] if e["key"] in MULTI_WAY}
    assert set(entries) == set(MULTI_WAY)
    for flag_key, entry in entries.items():
        assert entry["evaluation"] == "local", entry
        assert (entry["on_value"], entry["off_value"]) == ("A", "Z"), entry
        assert COLUMN not in entry, entry
        for value in ("B", *(part["value"] for part in SPLIT)):
            assert f'"{value}"' not in json.dumps(entry), (value, entry)

    # A PUT that leaves them out keeps them, and one that sends them is refused.
    puts = _answers(
        release_n_minus_1,
        scratch_schema,
        tmp_path,
        [
            *(
                ["PUT", flags_url + flag_id, headers, {"description": "N-1 was here"}]
                for flag_id in ids.values()
            ),
            *(
                ["PUT", flags_url + flag_id, headers, {"rollout_percentage": 40}]
                for flag_id in ids.values()
            ),
            ["PUT", flags_url + ids["split-flag"], headers, {"split": SPLIT}],
            [
                "PUT",
                flags_url + ids["rule-value-flag"],
                headers,
                {"targeting_rules": {**DASHBOARD_RULE, "value": "B"}},
            ],
        ],
    )
    assert [status for status, _, _ in puts] == [200] * 8 + [422, 422], puts
    assert puts[-2][1]["detail"][0]["type"] == "extra_forbidden", puts[-2]
    assert "unknown key" in json.dumps(puts[-1][1]), puts[-1]
    assert _split_and_rules(test_db, scratch_schema, ids.values()) == stored
    shutil.rmtree(release_n_minus_1, ignore_errors=True)


# ---------------------------------------------------------------------------
# 7. The statements each direction runs, captured online
# ---------------------------------------------------------------------------
SET = "SET LOCAL lock_timeout = '1s'"
RESET = "SET LOCAL lock_timeout TO DEFAULT"


def _added(schema: str) -> list[str]:
    return [
        statement
        for table in TABLES
        for statement in (
            SET,
            f"ALTER TABLE {schema}.{table} ADD COLUMN {COLUMN} JSONB",
            RESET,
        )
    ]


@pytest.mark.regression
@pytest.mark.parametrize("start", STARTS)
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_the_upgrade_runs_exactly_its_statements(
    profile, start, test_db, scratch_schema, tmp_path
):
    """Each ALTER straight between its bound and the reset, ``feature_flags``
    first, and no data step. Nothing runs between a ``SET`` and its ALTER, a
    read included: the catalog is read before the bound is set."""
    tree = _starting(test_db, profile, scratch_schema, tmp_path, start)
    captured = _captured(tree, scratch_schema, tmp_path, "upgrade", REVISION)
    for statement in captured:
        print(f"[{profile}/{start}] captured: {statement[:200]}")
    assert _rows(test_db, scratch_schema) == REVISION_ROWS[profile]
    remaining = _set_aside(captured)
    assert remaining[-6:] == _added(scratch_schema)
    assert len(remaining) == (6 if start == FROM_T2011 else 14), remaining
    for statement in remaining:
        verb = statement.split(" ", 1)[0].upper()
        assert verb not in {"INSERT", "UPDATE", "DELETE"}, f"a data step: {statement}"

    # Adjacent among everything captured, not only among what is left.
    for table in TABLES:
        at = captured.index(
            f"ALTER TABLE {scratch_schema}.{table} ADD COLUMN {COLUMN} JSONB"
        )
        assert captured[at - 1 : at + 2 : 2] == [SET, RESET], captured[at - 1 : at + 2]
    if start == FROM_0_48_0:
        # The second table was created earlier in the same run, so no other
        # session can hold a lock on it: the declared second is the first's.
        created = [
            at
            for at, statement in enumerate(remaining)
            if statement.startswith(f"CREATE TABLE {scratch_schema}.{SETTINGS} (")
        ]
        assert len(created) == 1 and created[0] < len(remaining) - 6, remaining


@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_the_downgrade_runs_exactly_its_statements(
    profile, test_db, scratch_schema, tmp_path
):
    """Both tables locked, each under its bound, then read, then dropped from
    by name. The one read of a table is of the two locked ones."""
    tree = _at_this_revision(test_db, profile, scratch_schema, tmp_path)
    captured = _captured(
        tree, scratch_schema, tmp_path, "downgrade", PREVIOUS_CORE_HEAD
    )
    for statement in captured:
        print(f"[{profile}] captured: {statement[:200]}")
    assert _rows(test_db, scratch_schema) == PREVIOUS_ROWS[profile]
    (read,) = [s for s in captured if s.startswith("SELECT flag.key")]
    assert re.findall(r"(?:FROM|JOIN) ([\w.]+) AS ", read) == [
        f"{scratch_schema}.{FLAGS}",
        f"{scratch_schema}.{SETTINGS}",
        f"{scratch_schema}.{FLAGS}",
    ]
    kept = _set_aside([s for s in captured if s != read])
    assert kept == [
        SET,
        f"LOCK TABLE {scratch_schema}.{FLAGS} IN ACCESS EXCLUSIVE MODE",
        RESET,
        SET,
        f"LOCK TABLE {scratch_schema}.{SETTINGS} IN ACCESS EXCLUSIVE MODE",
        f"ALTER TABLE {scratch_schema}.{FLAGS} DROP COLUMN {COLUMN}",
        f"ALTER TABLE {scratch_schema}.{SETTINGS} DROP COLUMN {COLUMN}",
        RESET,
    ]
    locked = captured.index(kept[4])
    assert locked < captured.index(read) < captured.index(kept[5])


# ---------------------------------------------------------------------------
# 8. The waits
# ---------------------------------------------------------------------------
def _nothing_applied(test_db, schema: str, start: str) -> None:
    """The schema is what it was at *start*: no revision of the run stayed."""
    rows = PREVIOUS_ROWS[CORE] if start == FROM_T2011 else RELEASED_ROWS[CORE]
    assert _rows(test_db, schema) == rows
    assert _split_columns(test_db, schema) == {}
    if start == FROM_0_48_0:
        assert SETTINGS not in _tables(test_db, schema)
        assert _key_column(test_db, schema) is None


@pytest.mark.regression
@pytest.mark.parametrize("start", STARTS)
def test_a_session_reading_feature_flags_ends_the_upgrade_with_the_refusal(
    start, test_db, scratch_schema, tmp_path
):
    """The ``ALTER TABLE`` waits for every session that has read the table and
    not ended its transaction. The bound ends the wait (SQLSTATE 55P03) with
    this revision's refusal, and nothing is applied, not even the revision the
    same run had already run; once the session is gone, the same command runs
    through. Without the ``SET`` only the leg's own ``statement_timeout`` ends
    it (57014), and this fails."""
    tree = _starting(test_db, CORE, scratch_schema, tmp_path, start)
    expected = _load_revision(scratch_schema)._lock_refusal(FLAGS)
    with _holding(test_db, _reading(scratch_schema, FLAGS)):
        upgrade = _leg(tree, scratch_schema, "upgrade", REVISION)
    print(f"[{start}] upgrade stderr tail:\n{upgrade.stderr[-1500:]}")
    assert upgrade.returncode != 0
    assert expected in upgrade.stderr, upgrade.stderr[-3000:]
    assert "statement timeout" not in upgrade.stderr
    if start == FROM_0_48_0:
        assert "Running upgrade t449_identity_aliases -> t2011" in upgrade.stderr
    _nothing_applied(test_db, scratch_schema, start)
    again = _leg(tree, scratch_schema, "upgrade", REVISION)
    assert again.returncode == 0, again.stderr[-3000:]
    assert _rows(test_db, scratch_schema) == REVISION_ROWS[CORE]
    assert _split_columns(test_db, scratch_schema) == EXPECTED_COLUMNS


@pytest.mark.regression
def test_a_session_reading_the_settings_ends_the_upgrade_with_the_refusal(
    test_db, scratch_schema, tmp_path
):
    """From a database that already has ``flag_environment_settings`` its step
    waits too, on a bound of its own, and the refusal names that table;
    ``feature_flags.split``, already added in the run, is rolled back."""
    tree = _starting(test_db, CORE, scratch_schema, tmp_path, FROM_T2011)
    expected = _load_revision(scratch_schema)._lock_refusal(SETTINGS)
    with _holding(test_db, _reading(scratch_schema, SETTINGS)):
        upgrade = _leg(tree, scratch_schema, "upgrade", REVISION)
    assert upgrade.returncode != 0
    assert expected in upgrade.stderr, upgrade.stderr[-3000:]
    assert "statement timeout" not in upgrade.stderr
    _nothing_applied(test_db, scratch_schema, FROM_T2011)
    again = _leg(tree, scratch_schema, "upgrade", REVISION)
    assert again.returncode == 0, again.stderr[-3000:]
    assert _split_columns(test_db, scratch_schema) == EXPECTED_COLUMNS


#: The migration session's own deadlock check, in the leg that meets one: below
#: the 1 s bound, so the deadlock is found before the bound ends the wait, and
#: long enough for the other session to be waiting by then.
DEADLOCK_OPTIONS = f"{LEG_STATEMENT_TIMEOUT} -c deadlock_timeout=800ms"
#: A deadlock check that never comes: whichever session has it is not the one
#: PostgreSQL ends, so a leg says which one is, and no scheduling delay does.
NO_DEADLOCK_CHECK = "deadlock_timeout=30s"


@pytest.mark.regression
def test_a_deadlock_ends_the_upgrade_with_the_refusal_and_applies_nothing(
    test_db, scratch_schema, tmp_path
):
    """One run from 0.48.0 holds ``api_keys`` (``t2011_flag_environments``)
    while this revision waits for ``feature_flags``. A session that read the
    flags and then reads the keys waits for the run, and the run for it:
    PostgreSQL ends the run with 40P01, and it refuses with the same text as a
    lock timeout, applying nothing; the session's read goes through. A lock
    timeout gives the same text, so this counts the deadlocks. The session's
    wait is also what every new SDK request meets: it reads its key first."""
    tree = _starting(test_db, CORE, scratch_schema, tmp_path, FROM_0_48_0)
    expected = _load_revision(scratch_schema)._lock_refusal(FLAGS)
    before = _deadlocks(test_db)
    outcome: dict = {}
    with test_db.connect() as session:
        transaction = session.begin()
        session.execute(text("SET LOCAL statement_timeout = '60s'"))
        session.execute(text("SET LOCAL deadlock_timeout = '30s'"))
        pid = session.execute(text("SELECT pg_backend_pid()")).scalar()
        session.execute(text(_reading(scratch_schema, FLAGS)))

        def read_the_keys() -> None:
            try:
                session.execute(text(_reading(scratch_schema, "api_keys")))
                outcome["read"] = True
            except Exception as exc:  # reported by the assertion below
                outcome["error"] = exc

        with _in_the_background(
            tree, scratch_schema, "upgrade", REVISION, options=DEADLOCK_OPTIONS
        ) as (process, name):
            _until_waiting_for(
                test_db, process, name, _table_lock(scratch_schema, FLAGS)
            )
            reading = threading.Thread(target=read_the_keys)
            reading.start()
            waited_for = _until_the_pid_waits(test_db, pid)
            stderr = process.communicate(timeout=PROCESS_SECONDS)[1]
            reading.join(timeout=PROCESS_SECONDS)
        transaction.rollback()
    assert waited_for == f"{scratch_schema}.api_keys", waited_for
    print(f"upgrade stderr tail:\n{stderr[-1500:]}")
    assert process.returncode != 0
    assert expected in stderr, stderr[-3000:]
    assert "statement timeout" not in stderr
    assert outcome == {"read": True}, outcome
    _counted_a_deadlock(test_db, before)
    _nothing_applied(test_db, scratch_schema, FROM_0_48_0)


def _counted_a_deadlock(test_db, before: int) -> None:
    deadline = time.monotonic() + 30
    while _deadlocks(test_db) == before and time.monotonic() < deadline:
        time.sleep(0.1)
    assert _deadlocks(test_db) > before, "no deadlock was met"


@pytest.mark.regression
@pytest.mark.parametrize("held", TABLES)
def test_a_session_reading_a_table_the_downgrade_locks_ends_it_with_its_refusal(
    held, test_db, scratch_schema, tmp_path
):
    """Each lock waits behind a request reading that table. Its bound ends the
    wait with the downgrade's refusal, naming the table, and nothing is
    changed; once the session is gone, the same command runs through. Without
    a block's ``SET`` only the leg's own ``statement_timeout`` ends it."""
    tree = _at_this_revision(test_db, CORE, scratch_schema, tmp_path)
    flag_id = _insert_flag(test_db, scratch_schema, "a-flag")
    _insert_setting(test_db, scratch_schema, flag_id, "staging")
    before = (_definition(test_db, scratch_schema), _stored(test_db, scratch_schema))
    expected = _load_revision(scratch_schema)._downgrade_lock_refusal(held)
    with _holding(test_db, _reading(scratch_schema, held)):
        down = _leg(tree, scratch_schema, "downgrade", PREVIOUS_CORE_HEAD)
    print(f"[{held}] downgrade stderr tail:\n{down.stderr[-1500:]}")
    assert down.returncode != 0
    assert expected in down.stderr, down.stderr[-3000:]
    assert f"{scratch_schema}.{held} " in expected
    assert "Refusing to upgrade" not in down.stderr
    assert "statement timeout" not in down.stderr
    assert _rows(test_db, scratch_schema) == REVISION_ROWS[CORE]
    assert (
        _definition(test_db, scratch_schema),
        _stored(test_db, scratch_schema),
    ) == before
    again = _leg(tree, scratch_schema, "downgrade", PREVIOUS_CORE_HEAD)
    assert again.returncode == 0, again.stderr[-3000:]
    assert _rows(test_db, scratch_schema) == PREVIOUS_ROWS[CORE]


# ---------------------------------------------------------------------------
# 9. The downgrade, with and without a flag in use
# ---------------------------------------------------------------------------
#: Any action on the version table, however it is phrased.
_VERSION_TABLE_ADVICE = re.compile(r"alembic_version|\bstamp\b", re.IGNORECASE)


def _refusal(stderr: str) -> str:
    return stderr[stderr.rindex("RuntimeError: ") + len("RuntimeError: ") :].strip()


def _insert_in_use(engine, schema: str) -> dict[str, list[str]]:
    """A flag in use in every way one can be, and the ones that are not in use
    beside them; returns what the refusal must name."""
    string = _string_flag(engine, schema, "a-split", split=SPLIT)
    _insert_setting(engine, schema, string, "staging")
    staging = _string_flag(engine, schema, "b-staging-split")
    _insert_setting(engine, schema, staging, "staging", split=SPLIT)
    rule = {"rules": [{**NATIVE_RULE, "value": "B"}]}
    _string_flag(engine, schema, "c-rule-value", targeting_rules=rule)
    everywhere = _string_flag(
        engine,
        schema,
        "d-everywhere",
        split=SPLIT,
        targeting_rules={**DASHBOARD_RULE, "value": "B"},
    )
    for environment in ("staging", "development"):
        _insert_setting(
            engine,
            schema,
            everywhere,
            environment,
            targeting_rules={"rules": [], "default_rule": {**NATIVE_RULE, "value": 1}},
        )
    # Not in use: no split, rules whose conditions alone have values, a boolean
    # flag whose rules carry one (no release serves it), and a split cleared.
    _string_flag(engine, schema, "unused-rules", targeting_rules=DASHBOARD_RULE)
    boolean = _insert_flag(
        engine, schema, "unused-boolean", targeting_rules={**DASHBOARD_RULE, "value": 1}
    )
    _insert_setting(engine, schema, boolean, "staging", targeting_rules=rule)
    _insert_flag(engine, schema, "unused-plain")
    return {
        "a-split": ["production"],
        "b-staging-split": ["staging"],
        "c-rule-value": ["production"],
        "d-everywhere": ["development", "production", "staging"],
    }


@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_the_downgrade_refuses_while_a_flag_is_in_use_and_changes_nothing(
    profile, test_db, scratch_schema, tmp_path
):
    tree = _at_this_revision(test_db, profile, scratch_schema, tmp_path)
    in_use = _insert_in_use(test_db, scratch_schema)
    before = (
        _rows(test_db, scratch_schema),
        _definition(test_db, scratch_schema),
        _stored(test_db, scratch_schema),
    )
    for target in (PREVIOUS_CORE_HEAD, RELEASED_CORE_HEAD):
        down = tree_profiles.alembic(tree, scratch_schema, "downgrade", target)
        assert down.returncode != 0, down.stdout[-2000:]
        refusal = _refusal(down.stderr)
        print(f"[{profile}] downgrade {target}: {refusal}")
        named = ", ".join(f"{k} ({', '.join(v)})" for k, v in in_use.items())
        assert refusal == (
            f"Refusing to downgrade schema {scratch_schema}: t2012_flag_split found "
            f"4 feature flag(s) with a split or a rule value: {named}. This "
            "downgrade drops feature_flags.split and "
            "flag_environment_settings.split, and the release it rolls back to "
            "serves neither a split nor a rule value. Clear them while the release "
            "that wrote them is still serving, or restore the backup taken before "
            "the upgrade to it. Nothing was changed."
        )
        assert "unused" not in refusal
        advice = _VERSION_TABLE_ADVICE.search(refusal)
        assert advice is None, f"version-table advice {advice.group(0)!r}: {refusal}"
        # Nothing recorded, nothing dropped, no value lost.
        assert (
            _rows(test_db, scratch_schema),
            _definition(test_db, scratch_schema),
            _stored(test_db, scratch_schema),
        ) == before


#: The flags the parity rows store each shape on, by value type.
_VALUES = {
    "boolean": (None, None),
    "string": ("on", "off"),
    "number": (1, 0),
    "json": ({"on": 1}, []),
}


@pytest.mark.regression
@pytest.mark.parametrize("value_type", sorted(_VALUES))
def test_the_downgrade_finds_a_rule_value_exactly_where_rule_values_does(
    value_type, test_db, scratch_schema, tmp_path, monkeypatch
):
    """Every stored shape, in production and in an environment's setting (read
    with the flag's own value type), by the migration's own query: a flag is
    in use where ``rule_values`` returns a value, unless it is a boolean flag,
    whose stray value refuses nothing."""
    _bootstrapped(CORE, scratch_schema, tmp_path)
    on, off = _VALUES[value_type]
    values = {"value_type": value_type}
    if value_type != "boolean":
        values |= {"on_value": on, "off_value": off}
    expected: dict[str, list[str]] = {}
    for name, stored, _ in STORED_RULES:
        carries = bool(rule_values(json.loads(json.dumps(stored))))
        in_use = carries and value_type != "boolean"
        # In production alone, then in staging alone, of a flag with no rules.
        _insert_flag(
            test_db, scratch_schema, f"p-{name}", targeting_rules=stored, **values
        )
        side = _insert_flag(test_db, scratch_schema, f"s-{name}", **values)
        _insert_setting(
            test_db, scratch_schema, side, "staging", targeting_rules=stored
        )
        if in_use:
            expected[f"p-{name}"] = ["production"]
            expected[f"s-{name}"] = ["staging"]
    # SQL NULL rules, in both tables, which the JSON above cannot write.
    bare = _insert_flag(test_db, scratch_schema, "p-sql-null", **values)
    _insert_setting(test_db, scratch_schema, bare, "development")

    module = _load_revision(scratch_schema)
    with test_db.connect() as conn:
        monkeypatch.setattr(module, "op", SimpleNamespace(get_bind=lambda: conn))
        found = module._flags_in_use()
        conn.rollback()
    missed = {k: v for k, v in expected.items() if k not in found}
    extra = {k: v for k, v in found.items() if k not in expected}
    assert (missed, extra) == ({}, {}), f"missed {missed}, refused too much {extra}"
    assert found == expected
    if value_type == "boolean":
        assert found == {}
    else:
        carrying = {name for name, _, carried in STORED_RULES if carried}
        assert {key[2:] for key in found} == carrying
        assert set(SIX_SHAPES) & carrying, "the six shapes divide"


@pytest.mark.regression
def test_a_split_written_while_the_downgrade_waits_is_seen_and_refuses_it(
    test_db, scratch_schema, tmp_path
):
    """The release that writes splits is still serving when the database is
    downgraded. A write in flight holds the table; the downgrade waits for its
    lock, and only then looks, so what the write commits refuses it. Looking
    before locking would see no split and drop the one just written."""
    tree = _at_this_revision(test_db, CORE, scratch_schema, tmp_path)
    flag_id = _string_flag(test_db, scratch_schema, "written-meanwhile")
    with test_db.connect() as writer:
        writing = writer.begin()
        writer.execute(
            text(
                f'UPDATE "{scratch_schema}".{FLAGS} SET split = CAST(:s AS jsonb) '
                "WHERE id = :id"
            ),
            {"s": json.dumps(SPLIT), "id": flag_id},
        )
        with _in_the_background(
            tree, scratch_schema, "downgrade", PREVIOUS_CORE_HEAD
        ) as (process, name):
            _until_waiting_for(
                test_db, process, name, _table_lock(scratch_schema, FLAGS)
            )
            writing.commit()
            stderr = process.communicate(timeout=PROCESS_SECONDS)[1]
    assert process.returncode != 0, "the downgrade dropped a split just written"
    refusal = _refusal(stderr)
    assert "found 1 feature flag(s)" in refusal, stderr[-3000:]
    assert "written-meanwhile (production)" in refusal
    assert _rows(test_db, scratch_schema) == REVISION_ROWS[CORE]
    assert _split_columns(test_db, scratch_schema) == EXPECTED_COLUMNS
    assert _split_and_rules(test_db, scratch_schema, [flag_id])[flag_id][0] == SPLIT


@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_with_nothing_in_use_the_downgrade_drops_both_columns_and_nothing_else(
    profile, test_db, scratch_schema, tmp_path
):
    """With the splits and the rule values cleared, as the refusal says."""
    tree = _at_this_revision(test_db, profile, scratch_schema, tmp_path)
    _insert_in_use(test_db, scratch_schema)
    with test_db.begin() as conn:
        for table in TABLES:
            conn.execute(text(f'UPDATE "{scratch_schema}".{table} SET split = NULL'))
        # Every rule value, where the flag is not boolean; the boolean flag's
        # strays stay.
        conn.execute(
            text(
                f'UPDATE "{scratch_schema}".{FLAGS} SET targeting_rules = NULL '
                "WHERE value_type <> 'boolean' AND key <> 'unused-rules'"
            )
        )
        conn.execute(
            text(
                f'UPDATE "{scratch_schema}".{SETTINGS} s SET targeting_rules = NULL '
                f'FROM "{scratch_schema}".{FLAGS} f WHERE f.id = s.feature_flag_id '
                "AND f.value_type <> 'boolean'"
            )
        )
    tables = _tables(test_db, scratch_schema)
    definition = _definition(test_db, scratch_schema)
    stored = _stored(test_db, scratch_schema)

    down = tree_profiles.alembic(tree, scratch_schema, "downgrade", PREVIOUS_CORE_HEAD)
    assert down.returncode == 0, down.stderr[-3000:]
    _report(profile, "after downgrade", test_db, scratch_schema)
    assert _rows(test_db, scratch_schema) == PREVIOUS_ROWS[profile]
    assert _split_columns(test_db, scratch_schema) == {}
    assert _tables(test_db, scratch_schema) == tables
    for table in TABLES:
        columns, constraints, indexes = definition[table]
        assert _definition(test_db, scratch_schema)[table] == (
            {name: rest for name, rest in columns.items() if name != COLUMN},
            constraints,
            indexes,
        ), table

    # Every row is what it was, less the column; and upgrading again puts back
    # exactly what the downgrade removed, with every split empty.
    upgrade = _upgrade(tree, scratch_schema)
    assert upgrade.returncode == 0, upgrade.stderr[-3000:]
    _report(profile, "after upgrade heads again", test_db, scratch_schema)
    assert _rows(test_db, scratch_schema) == ROWS[profile]
    assert _definition(test_db, scratch_schema) == definition
    assert _stored(test_db, scratch_schema) == stored


# ---------------------------------------------------------------------------
# 10. Offline mode
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("in_use", [False, True], ids=["unused", "in-use"])
def test_offline_mode_is_refused_in_both_directions_and_emits_nothing(
    in_use, test_db, scratch_schema, tmp_path
):
    """``--sql`` can read neither the catalog nor the flags, so it refuses
    whether or not a flag is in use, and emits no DDL."""
    tree = _bootstrapped(CORE, scratch_schema, tmp_path)
    if in_use:
        _insert_in_use(test_db, scratch_schema)
    revision = _load_revision(scratch_schema)
    before = (
        _rows(test_db, scratch_schema),
        _definition(test_db, scratch_schema),
        _stored(test_db, scratch_schema),
    )
    for argv in (
        ("downgrade", f"{REVISION}:{PREVIOUS_CORE_HEAD}", "--sql"),
        ("upgrade", f"{PREVIOUS_CORE_HEAD}:{REVISION}", "--sql"),
    ):
        offline = tree_profiles.alembic(tree, scratch_schema, *argv)
        assert offline.returncode != 0, (argv, offline.stdout[-2000:])
        assert revision._OFFLINE_REFUSAL in offline.stderr, (
            f"refusal text missing from --sql output: {argv} {offline.stderr[-2000:]}"
        )
        for emitted in ("DROP", "ALTER", "ADD COLUMN", "LOCK TABLE", "lock_timeout"):
            assert emitted not in offline.stdout, f"{emitted} emitted offline: {argv}"
        assert (
            _rows(test_db, scratch_schema),
            _definition(test_db, scratch_schema),
            _stored(test_db, scratch_schema),
        ) == before, argv


# ---------------------------------------------------------------------------
# 11. This release on the downgraded schema
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("target", [PREVIOUS_CORE_HEAD, RELEASED_CORE_HEAD])
def test_this_release_answers_500_to_flag_evaluations_and_reads_on_the_downgraded_schema(
    test_db, scratch_schema, tmp_path, target
):
    """Why the database-first rollback's paragraph names the outage: until every
    task of this release is replaced, it selects a column the downgrade dropped,
    from the flag rows and from the side rows: the ruleset's query of the flag
    rows names it too. A run that goes on to 0.48.0's head undoes
    ``t2011_flag_environments`` too, and no request that presents a key, no
    signed-in API key route and no segment route that reads the flags is answered."""
    tree = _at_this_revision(test_db, CORE, scratch_schema, tmp_path)
    user = _user(test_db, scratch_schema)
    flag_id = _insert_flag(test_db, scratch_schema, "rolled-back-flag")
    signed_in = {"Authorization": f"Bearer {_token(user)}"}
    flags_url = "/api/v1/feature-flags/"
    flag = flags_url + flag_id
    environments = f"{flag}/environments"
    requests = {}
    for environment, source in (
        ("production", "staging"),
        ("staging", "development"),
        ("development", "staging"),
    ):
        _, key = _insert_key(
            test_db, scratch_schema, user.id, environment, "sdk:ruleset"
        )
        sdk = {"X-API-Key": key}
        requests |= {
            f"evaluate, {environment}": _evaluate("rolled-back-flag", "u1", key),
            f"evaluate (POST), {environment}": [
                "POST",
                f"{flags_url}evaluate/rolled-back-flag",
                sdk,
                {"user_id": "u1"},
            ],
            f"a user's flags, {environment}": [
                "GET",
                f"{flags_url}user/u1",
                sdk,
                None,
            ],
            f"ruleset, {environment}": _ruleset(key),
            f"the flag in {environment}": [
                "GET",
                f"{environments}/{environment}",
                signed_in,
                None,
            ],
            f"change it in {environment}": [
                "PUT",
                f"{environments}/{environment}",
                signed_in,
                {"rollout_percentage": 100},
            ],
            f"promote it into {environment}": [
                "POST",
                f"{environments}/{environment}/promote",
                signed_in,
                {"from": source},
            ],
            f"several flags in {environment}": [
                "GET",
                f"/api/v1/environments/{environment}/feature-flag-settings"
                f"?flag_id={flag_id}",
                signed_in,
                None,
            ],
        }
    requests |= {
        "the flag everywhere": ["GET", environments, signed_in, None],
        "one flag": ["GET", flag, signed_in, None],
        "the flags": ["GET", flags_url, signed_in, None],
        "change the flag": ["PUT", flag, signed_in, {"rollout_percentage": 100}],
        "turn it off": ["POST", f"{flag}/deactivate", signed_in, None],
        "the environments": ["GET", "/api/v1/environments", signed_in, None],
    }
    # A segment, and a key of its own to delete.
    segments, keys = "/api/v1/segments", "/api/v1/api-keys"
    condition = {"attribute": "country", "operator": "equals", "value": "US"}
    rules = {"groups": [{"conditions": [condition]}]}
    create = ["POST", f"{segments}/", signed_in, {"name": "Rolled", "rules": rules}]
    ((status, made, _),) = _answers(tree, scratch_schema, tmp_path, [create])
    assert status == 201, made
    segment = f"{segments}/{made['id']}"
    key_id, _ = _insert_key(test_db, scratch_schema, user.id, "production")
    requests |= {
        "a segment's experiments": [
            "GET",
            f"{segment}/experiments",
            signed_in,
            None,
        ],
        "the keys": ["GET", keys, signed_in, None],
    }

    # Asked after the downgrade only: each would change what the others ask.
    new = {"key": "another-flag", "name": "Another"}
    bulk = {"flag_ids": [flag_id]}
    once = {
        "archive a segment": ["PUT", segment, signed_in, {"status": "archived"}],
        "delete a segment": ["DELETE", segment, signed_in, None],
        "create a key": ["POST", keys, signed_in, {"name": "Rolled"}],
        "create a staging key": [
            "POST",
            keys,
            signed_in,
            {"name": "Rolled", "environment": "staging"},
        ],
        "delete a key": ["DELETE", f"{keys}/{key_id}", signed_in, None],
        "create a flag": ["POST", flags_url, signed_in, new],
        "turn it on": ["POST", f"{flag}/activate", signed_in, None],
        "unarchive it": ["POST", f"{flag}/unarchive", signed_in, None],
        "delete it": ["DELETE", flag, signed_in, None],
        **{
            f"bulk {action}": [
                "POST",
                f"{flags_url}bulk-toggle",
                signed_in,
                {**bulk, "action": action},
            ]
            for action in ("enable", "disable", "archive")
        },
    }

    def answers(asked) -> dict[str, tuple]:
        answered = _answers(tree, scratch_schema, tmp_path, list(asked.values()))
        return dict(zip(asked, answered))

    before = answers(requests)
    assert {name: a[0] for name, a in before.items()} == dict.fromkeys(requests, 200)
    down = tree_profiles.alembic(tree, scratch_schema, "downgrade", target)
    assert down.returncode == 0, down.stderr[-3000:]
    after = answers({**requests, **once})
    measured = {name: answer[0] for name, answer in after.items()}
    print(f"on the downgraded schema: {measured}")
    bulk_names = [name for name in once if name.startswith("bulk ")]
    # With ``t2011_flag_environments`` left in place: these read what it added
    # and nothing that this revision did.
    answered = {
        "archive a segment": 200,
        "delete a segment": 204,
        "the keys": 200,
        "create a key": 201,
        "create a staging key": 201,
        "delete a key": 204,
    }
    assert measured == {
        **dict.fromkeys([*requests, *once], 500),
        **dict.fromkeys(bulk_names, 200),
        "the environments": 200,
        **(answered if target == PREVIOUS_CORE_HEAD else {}),
    }
    # A bulk toggle answers 200 and says it changed nothing.
    for name in bulk_names:
        body = after[name][1]
        assert (body["succeeded"], body["failed"]) == (0, 1), (name, body)


# ---------------------------------------------------------------------------
# 12. One downgrade to 0.48.0's head, beside an SDK request
# ---------------------------------------------------------------------------
def _t2011_refusal(schema: str, table: str) -> str:
    """``t2011_flag_environments``'s own refusal of a downgrade."""
    (path,) = VERSIONS.glob(f"{PREVIOUS_CORE_HEAD}_*.py")
    spec = importlib.util.spec_from_file_location(
        f"_rev_{PREVIOUS_CORE_HEAD}_{uuid.uuid4().hex[:6]}", path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._SCHEMA = schema
    return module._downgrade_lock_refusal(table)


class _SdkRequest:
    """A session holding what an SDK request holds: it has read ``api_keys``
    (its key) and reads ``feature_flags`` next, in one transaction."""

    def __init__(self, connection, schema: str) -> None:
        self._connection = connection
        self._schema = schema
        self.outcome: dict = {}
        self._thread = threading.Thread(target=self._read_the_flags)

    def read_its_key(self, *settings: str) -> None:
        self._transaction = self._connection.begin()
        for setting in ("statement_timeout=60s", *settings):
            name, value = setting.split("=")
            self._connection.execute(text(f"SET LOCAL {name} = '{value}'"))
        self.pid = self._connection.execute(text("SELECT pg_backend_pid()")).scalar()
        self._connection.execute(text(_reading(self._schema, "api_keys")))

    def _read_the_flags(self) -> None:
        try:
            self._connection.execute(text(_reading(self._schema, FLAGS)))
            self.outcome["read"] = True
        except Exception as exc:
            self.outcome["code"] = getattr(getattr(exc, "orig", None), "pgcode", exc)

    def start_reading_the_flags(self) -> None:
        self._thread.start()

    def finish(self) -> None:
        self._thread.join(timeout=PROCESS_SECONDS)
        assert not self._thread.is_alive(), "the SDK request never ended"
        self._transaction.rollback()


def _state(engine, schema: str) -> tuple:
    return (
        _rows(engine, schema),
        _definition(engine, schema) if SETTINGS in _tables(engine, schema) else None,
        _key_column(engine, schema),
    )


@pytest.mark.regression
def test_a_one_run_downgrade_that_waits_for_an_sdk_request_refuses_and_changes_nothing(
    test_db, scratch_schema, tmp_path
):
    """A rollback to 0.48.0 is one run: this revision's downgrade locks
    ``feature_flags``, then ``t2011_flag_environments``'s waits for an SDK
    request that has read ``api_keys``. The request then reads
    ``feature_flags``: each waits for the other. The run, which waited first,
    is ended (a deadlock, or its bound) with ``t2011_flag_environments``'s
    refusal, and nothing of either revision is changed; the request's read
    goes through. Once the request is gone, the same command runs through."""
    tree = _at_this_revision(test_db, CORE, scratch_schema, tmp_path)
    user = _user(test_db, scratch_schema)
    _insert_key(test_db, scratch_schema, user.id, "production")
    before = _state(test_db, scratch_schema)
    with test_db.connect() as connection:
        request = _SdkRequest(connection, scratch_schema)
        request.read_its_key(NO_DEADLOCK_CHECK)
        with _in_the_background(
            tree, scratch_schema, "downgrade", RELEASED_CORE_HEAD
        ) as (process, name):
            # It holds feature_flags already: this revision's downgrade ran.
            _until_waiting_for(
                test_db, process, name, _table_lock(scratch_schema, "api_keys")
            )
            request.start_reading_the_flags()
            stderr = process.communicate(timeout=PROCESS_SECONDS)[1]
        request.finish()
    print(f"downgrade stderr tail:\n{stderr[-2500:]}")
    assert "Running downgrade t2012_flag_split -> t2011_flag_environments" in stderr
    assert process.returncode != 0
    assert _t2011_refusal(scratch_schema, "api_keys") in stderr, stderr[-3000:]
    assert "Refusing to upgrade" not in stderr
    assert "statement timeout" not in stderr
    assert request.outcome == {"read": True}, request.outcome
    assert _state(test_db, scratch_schema) == before

    again = _leg(tree, scratch_schema, "downgrade", RELEASED_CORE_HEAD)
    assert again.returncode == 0, again.stderr[-3000:]
    assert _rows(test_db, scratch_schema) == RELEASED_ROWS[CORE]
    assert SETTINGS not in _tables(test_db, scratch_schema)
    assert _key_column(test_db, scratch_schema) is None
    assert _split_columns(test_db, scratch_schema) == {}


@pytest.mark.regression
def test_a_one_run_downgrade_met_by_an_sdk_request_that_waits_first_runs_through(
    test_db, scratch_schema, tmp_path
):
    """The other order: the request waits for ``feature_flags`` before the run
    waits for ``api_keys`` (the run is held at ``flag_environment_settings``
    until then, by a session reading it). The request, which waited first, is
    the one PostgreSQL ends (40P01); the run goes through to 0.48.0's head."""
    tree = _at_this_revision(test_db, CORE, scratch_schema, tmp_path)
    user = _user(test_db, scratch_schema)
    _insert_key(test_db, scratch_schema, user.id, "production")
    before = _deadlocks(test_db)
    with test_db.connect() as connection, test_db.connect() as reader:
        request = _SdkRequest(connection, scratch_schema)
        request.read_its_key()
        reading = reader.begin()
        reader.execute(text(_reading(scratch_schema, SETTINGS)))
        with _in_the_background(
            tree,
            scratch_schema,
            "downgrade",
            RELEASED_CORE_HEAD,
            options=f"{LEG_STATEMENT_TIMEOUT} -c {NO_DEADLOCK_CHECK}",
        ) as (process, name):
            _until_waiting_for(
                test_db, process, name, _table_lock(scratch_schema, SETTINGS)
            )
            request.start_reading_the_flags()
            waited_for = _until_the_pid_waits(test_db, request.pid)
            reading.rollback()
            stderr = process.communicate(timeout=PROCESS_SECONDS)[1]
        request.finish()
    assert waited_for == f"{scratch_schema}.{FLAGS}", waited_for
    print(f"downgrade stderr tail:\n{stderr[-2500:]}")
    assert process.returncode == 0, stderr[-3000:]
    assert request.outcome == {"code": "40P01"}, request.outcome
    _counted_a_deadlock(test_db, before)
    assert _rows(test_db, scratch_schema) == RELEASED_ROWS[CORE]
    assert SETTINGS not in _tables(test_db, scratch_schema)
    assert _key_column(test_db, scratch_schema) is None
    assert _split_columns(test_db, scratch_schema) == {}


# ---------------------------------------------------------------------------
# 13. The heads
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
