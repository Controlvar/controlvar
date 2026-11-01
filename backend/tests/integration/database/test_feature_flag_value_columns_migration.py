"""``607c51a59bf5``: a flag's value type and its two values, through the transitions (#1497).

The revision adds ``feature_flags.value_type`` (``varchar(16) NOT NULL``,
default ``'boolean'``), ``on_value`` and ``off_value`` (``jsonb``, nullable)
and the check ``check_flag_values``, each only when absent.  A schema built by
``create_all`` already has all four, so the other transition tests -- which
rewind such a schema's ``alembic_version`` -- never run this revision's DDL.
The tests that start from the previous release instead take a bootstrapped
schema with the check and the three columns DROPPED and ``alembic_version`` at
``4c2c09a2b3e8``, in each profile, and run the documented ``alembic upgrade
heads``.  Then:

1. the ``alembic_version`` rows are this release's, the columns have the
   model's type, NOT NULL and server default, a flag the previous release wrote
   reads ``'boolean'`` with both values NULL, and autogenerate is empty;
2. the upgraded database and a ``create_all``-bootstrapped one have the same
   columns (``format_type``, NOT NULL, ``pg_get_expr``) and the same check
   constraints (``pg_get_constraintdef``), compared by name.  Autogenerate
   compares neither defaults nor checks (``env.py`` enables neither), so (1)
   cannot see a model and a migration that disagree on them;
3. the check refuses and accepts exactly the rows in ``REFUSED`` and
   ``ACCEPTED``, written with SQL, on both databases.  PostgreSQL passes a
   check whose expression is NULL, so a branch without ``IS NOT NULL`` would
   accept a string flag with no values; equality is JSONB's (``3`` equals
   ``3.0``);
4. this release's model writes a Python ``None`` value as SQL NULL, which the
   check accepts for a boolean flag, never as JSON ``null``, which it refuses;
5. the previous release's code -- this tree without the revision, the three
   columns and the check -- creates, updates and lists flags against this
   schema, with a string flag present: it reads and lists it, updates it
   without touching its values, builds the ruleset over it and answers
   ``enabled`` for it exactly as this release does for 100 users;
6. the downgrade refuses while a string, number or json flag exists, online
   and in offline (``--sql``) mode, and changes nothing; with only boolean
   flags it removes exactly the three columns and the check;
7. ``alembic heads`` names exactly this revision and the modules head.
"""

from __future__ import annotations

import json
import re
import shutil
import textwrap
import uuid

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.db import bootstrap
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.services.feature_flag_service import FeatureFlagService
from backend.tests.integration.database import tree_profiles
from backend.tests.integration.database.tree_profiles import CORE, FULL

pytestmark = [pytest.mark.integration]

#: This revision, and the core revision it extends.
REVISION = "607c51a59bf5"
PREVIOUS_CORE_HEAD = "4c2c09a2b3e8"
#: The core head of this tree.
CORE_HEAD = "607c51a59bf5"
#: The modules branch's head in this tree, recorded on both sides of every
#: step here: this file is about the core chain.
MODULES_HEAD = "modules_0003_lower_is_better"

PREVIOUS_ROWS = {
    CORE: {PREVIOUS_CORE_HEAD},
    FULL: {PREVIOUS_CORE_HEAD, MODULES_HEAD},
}
ROWS = {
    CORE: {CORE_HEAD},
    FULL: {CORE_HEAD, MODULES_HEAD},
}

COLUMNS = ("value_type", "on_value", "off_value")
CHECK = "check_flag_values"
#: Each new column as PostgreSQL describes it, after either path:
#: (``format_type``, NOT NULL, ``pg_get_expr`` of its default).
EXPECTED_COLUMNS = {
    "value_type": ("character varying(16)", True, "'boolean'::character varying"),
    "on_value": ("jsonb", False, None),
    "off_value": ("jsonb", False, None),
}

#: Rows the check refuses: (case, value_type, on_value, off_value), each value
#: as JSON text, or ``None`` for SQL NULL.  One table, so a later API check can
#: run the same cases and be compared with this one (PF-5).
REFUSED = [
    ("string-null-null", "string", None, None),
    ("string-on-only", "string", '"on"', None),
    ("string-off-only", "string", None, '"off"'),
    ("number-null-null", "number", None, None),
    ("json-null-null", "json", None, None),
    ("json-on-only", "json", '{"a": 1}', None),
    ("json-scalar-strings", "json", '"on"', '"off"'),
    ("json-scalar-numbers", "json", "1", "2"),
    ("json-null-in-string", "string", "null", '"off"'),
    ("json-null-in-json", "json", "null", "[]"),
    ("json-null-in-boolean", "boolean", "null", "null"),
    ("unknown-type", "date", '"2026-10-31"', '"2026-11-01"'),
    ("boolean-on-true", "boolean", "true", None),
    ("boolean-true-false", "boolean", "true", "false"),
    ("numbers-in-string", "string", "1", "2"),
    ("strings-in-number", "number", '"1"', '"2"'),
    ("number-and-string", "number", "1", '"2"'),
    ("string-equal", "string", '"same"', '"same"'),
    ("number-3-vs-3.0", "number", "3", "3.0"),
    ("json-1-vs-1.0", "json", '{"a": 1}', '{"a": 1.0}'),
    ("json-key-order", "json", '{"a": 1, "b": 2}', '{"b": 2, "a": 1}'),
]
#: Rows the check accepts.
ACCEPTED = [
    ("boolean-null-null", "boolean", None, None),
    ("string", "string", '"blue"', '"grey"'),
    ("number", "number", "1", "2.5"),
    ("json-object-and-array", "json", "{}", "[]"),
    ("json-nested-true-vs-1", "json", '{"a": true}', '{"a": 1}'),
    ("json-array-true-vs-1", "json", "[true]", "[1]"),
    ("json-array-order", "json", "[1, 2]", "[2, 1]"),
]
#: Exact sizes, so the table cannot shrink without this file saying so.
REFUSED_COUNT = 21
ACCEPTED_COUNT = 7

#: How the database at this head was made: by this migration, from the
#: previous release's schema (every existing deployment), or by ``create_all``
#: (every fresh one).
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
    yield from _scratch(test_db, "flagval")


@pytest.fixture
def reference_schema(test_db):
    yield from _scratch(test_db, "flagvalref")


def _rows(engine, schema: str) -> set[str]:
    return bootstrap.recorded_revisions(engine, schema)


def _columns(engine, schema: str) -> dict[str, tuple]:
    """Each new column present: (format_type, NOT NULL, default expression)."""
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
                "WHERE n.nspname = :s AND t.relname = 'feature_flags' "
                "AND a.attname IN ('value_type', 'on_value', 'off_value') "
                "AND NOT a.attisdropped"
            ),
            {"s": schema},
        ).all()
    return {name: (kind, notnull, default) for name, kind, notnull, default in rows}


def _all_columns(engine, schema: str) -> set[str]:
    return {
        c["name"] for c in inspect(engine).get_columns("feature_flags", schema=schema)
    }


def _checks(engine, schema: str) -> dict[str, str]:
    """Every check constraint on *schema*.feature_flags: name -> definition."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT c.conname, pg_get_constraintdef(c.oid) "
                "FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid "
                "JOIN pg_namespace n ON n.oid = t.relnamespace "
                "WHERE n.nspname = :s AND t.relname = 'feature_flags' "
                "AND c.contype = 'c'"
            ),
            {"s": schema},
        ).all()
    return {name: definition.replace(schema, "<schema>") for name, definition in rows}


def _tables(engine, schema: str) -> set[str]:
    return set(inspect(engine).get_table_names(schema=schema))


def _insert_previous_release_flag(engine, schema: str, key: str = "") -> str:
    """A row as the previous release writes it: no value column named."""
    flag_id = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(
            text(
                f'INSERT INTO "{schema}".feature_flags '
                "(id, key, name, status, rollout_percentage, created_at, updated_at) "
                "VALUES (:id, :key, 'Previous release', 'INACTIVE', 0, now(), now())"
            ),
            {"id": flag_id, "key": key or f"previous-{flag_id[:8]}"},
        )
    return flag_id


_INSERT_WITH_VALUES = (
    'INSERT INTO "{schema}".feature_flags '
    "(id, key, name, status, rollout_percentage, created_at, updated_at, "
    "value_type, on_value, off_value) "
    "VALUES (:id, :key, 'Value flag', :status, :percentage, now(), now(), "
    ":value_type, CAST(:on AS jsonb), CAST(:off AS jsonb))"
)


def _value_params(key, value_type, on, off, status="INACTIVE", percentage=0):
    return {
        "id": str(uuid.uuid4()),
        "key": key,
        "status": status,
        "percentage": percentage,
        "value_type": value_type,
        "on": on,
        "off": off,
    }


def _insert_value_flag(engine, schema: str, key: str, **params) -> None:
    """A string flag as this release's API will store it (SQL, no model)."""
    values = {"value_type": "string", "on": '"blue"', "off": '"grey"', **params}
    with engine.begin() as conn:
        conn.execute(
            text(_INSERT_WITH_VALUES.format(schema=schema)),
            _value_params(key, **values),
        )


def _stored_values(engine, schema: str) -> dict[str, tuple]:
    """key -> (value_type, on_value, off_value, rollout_percentage)."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT key, value_type, on_value, off_value, rollout_percentage "
                f'FROM "{schema}".feature_flags'
            )
        ).all()
    return {key: tuple(rest) for key, *rest in rows}


def _check_refusal(engine, schema: str, case: tuple) -> str | None:
    """Write *case* in a transaction that is always rolled back.

    Returns ``None`` when the row was stored, else the name of the constraint
    that refused it (a refusal by anything else -- bad JSON, a NOT NULL --
    raises, so it cannot pass for the check's).
    """
    name, value_type, on, off = case
    params = _value_params(f"chk-{name}-{uuid.uuid4().hex[:6]}", value_type, on, off)
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(text(_INSERT_WITH_VALUES.format(schema=schema)), params)
        except IntegrityError as error:
            transaction.rollback()
            assert error.orig.pgcode == "23514", f"{name}: {error.orig}"
            return error.orig.diag.constraint_name
        transaction.rollback()
    return None


def _at_previous_release(engine, schema: str, rows: set[str]) -> None:
    """Turn a bootstrapped *schema* into what the previous release left."""
    with engine.begin() as conn:
        # IF EXISTS: with them missing from the model, ``create_all`` never
        # made them, and the test must reach the autogenerate that says so.
        conn.execute(
            text(
                f'ALTER TABLE "{schema}".feature_flags DROP CONSTRAINT IF EXISTS {CHECK}'
            )
        )
        for column in COLUMNS:
            conn.execute(
                text(
                    f'ALTER TABLE "{schema}".feature_flags DROP COLUMN IF EXISTS {column}'
                )
            )
        conn.execute(text(f'DELETE FROM "{schema}".alembic_version'))
        for revision in sorted(rows):
            conn.execute(
                text(f'INSERT INTO "{schema}".alembic_version VALUES (:rev)'),
                {"rev": revision},
            )
    assert _columns(engine, schema) == {}
    assert CHECK not in _checks(engine, schema)
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
    print(f"[{profile}] {label}: columns={_columns(engine, schema)}")
    print(f"[{profile}] {label}: {CHECK}={_checks(engine, schema).get(CHECK)}")


def _session(engine, schema: str) -> Session:
    """A session whose models read and write the scratch *schema*."""
    models_schema = FeatureFlag.__table__.schema
    return Session(
        bind=engine.execution_options(schema_translate_map={models_schema: schema}),
        autoflush=False,
    )


# ---------------------------------------------------------------------------
# 1. The previous release, upgraded with `heads`
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_a_database_at_the_previous_release_gains_the_columns_and_the_check(
    profile, test_db, scratch_schema, tmp_path
):
    tree = _bootstrapped_at_previous_release(test_db, profile, scratch_schema, tmp_path)
    existing = _insert_previous_release_flag(test_db, scratch_schema, "existing")
    _report(profile, "at the previous release", test_db, scratch_schema)

    _upgrade(tree, scratch_schema)
    _report(profile, "after upgrade heads", test_db, scratch_schema)

    assert _rows(test_db, scratch_schema) == ROWS[profile]
    assert _columns(test_db, scratch_schema) == EXPECTED_COLUMNS
    assert CHECK in _checks(test_db, scratch_schema)
    # The flag the previous release stored is a boolean flag with no values.
    assert existing
    assert _stored_values(test_db, scratch_schema) == {
        "existing": ("boolean", None, None, 0)
    }

    # Type and nullability are the model's: autogenerate, which compares
    # those, finds nothing to add.
    result = tree_profiles.autogenerate(
        tree, scratch_schema, tmp_path, head=tree_profiles.CORE_HEAD
    )
    assert result.body is not None, result.describe()
    assert result.operations == [], result.describe()


# ---------------------------------------------------------------------------
# 2. The migration's columns and check are the model's (PF-6)
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_an_upgraded_database_and_a_bootstrapped_one_have_the_same_columns_and_checks(
    profile, test_db, scratch_schema, reference_schema, tmp_path
):
    """The only gate on the defaults and the check: autogenerate compares neither.

    A fresh deployment gets them from the model (``create_all``); an existing
    one from this migration.  A different check would let one population of
    databases store a flag the other refuses.
    """
    tree = _bootstrapped_at_previous_release(test_db, profile, scratch_schema, tmp_path)
    _upgrade(tree, scratch_schema)
    created = tree_profiles.bootstrap_schema(tree, reference_schema)
    assert created.returncode == 0, created.stderr[-3000:]

    upgraded = _columns(test_db, scratch_schema)
    bootstrapped = _columns(test_db, reference_schema)
    print(f"[{profile}] columns upgraded:     {upgraded}")
    print(f"[{profile}] columns bootstrapped: {bootstrapped}")
    assert bootstrapped == EXPECTED_COLUMNS
    assert upgraded == bootstrapped

    upgraded_checks = _checks(test_db, scratch_schema)
    bootstrapped_checks = _checks(test_db, reference_schema)
    print(f"[{profile}] {CHECK} upgraded:     {upgraded_checks.get(CHECK)}")
    print(f"[{profile}] {CHECK} bootstrapped: {bootstrapped_checks.get(CHECK)}")
    assert CHECK in bootstrapped_checks
    assert upgraded_checks[CHECK] == bootstrapped_checks[CHECK]
    assert upgraded_checks == bootstrapped_checks


# ---------------------------------------------------------------------------
# 3. What the check refuses and accepts (PF-5)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def built_schemas(test_db, tmp_path_factory):
    """One core database per path, each at this head: {path: schema}."""
    tmp_path = tmp_path_factory.mktemp("flagval-check")
    suffix = uuid.uuid4().hex[:8]
    schemas = {UPGRADED: f"flagvalup_{suffix}", BOOTSTRAPPED: f"flagvalbs_{suffix}"}
    try:
        tree = tree_profiles.tree_for(CORE, tmp_path)
        for schema in schemas.values():
            created = tree_profiles.bootstrap_schema(tree, schema)
            assert created.returncode == 0, created.stderr[-3000:]
        _at_previous_release(test_db, schemas[UPGRADED], PREVIOUS_ROWS[CORE])
        _upgrade(tree, schemas[UPGRADED])
        for schema in schemas.values():
            assert _rows(test_db, schema) == ROWS[CORE]
            assert CHECK in _checks(test_db, schema)
        yield schemas
    finally:
        with test_db.begin() as conn:
            for schema in schemas.values():
                conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


def test_the_check_tables_have_their_stated_sizes():
    assert len(REFUSED) == REFUSED_COUNT
    assert len(ACCEPTED) == ACCEPTED_COUNT
    names = [case[0] for case in REFUSED + ACCEPTED]
    assert len(set(names)) == len(names)


@pytest.mark.regression
@pytest.mark.parametrize("built_by", [UPGRADED, BOOTSTRAPPED])
@pytest.mark.parametrize("case", REFUSED, ids=[case[0] for case in REFUSED])
def test_the_check_refuses(case, built_by, test_db, built_schemas):
    refused_by = _check_refusal(test_db, built_schemas[built_by], case)
    assert refused_by == CHECK, f"{case} was stored"


@pytest.mark.regression
@pytest.mark.parametrize("built_by", [UPGRADED, BOOTSTRAPPED])
@pytest.mark.parametrize("case", ACCEPTED, ids=[case[0] for case in ACCEPTED])
def test_the_check_accepts(case, built_by, test_db, built_schemas):
    assert _check_refusal(test_db, built_schemas[built_by], case) is None


# ---------------------------------------------------------------------------
# 4. This release's model writes None as SQL NULL
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_this_releases_model_stores_a_none_value_as_sql_null(test_db, built_schemas):
    """A boolean flag created or updated with both values ``None`` is stored.

    ``JSONB`` writes ``None`` as JSON ``null`` unless ``none_as_null``; the
    check refuses JSON ``null`` for every type, so a writer that copies every
    column of a flag (``to_dict``, a clone) would fail on a boolean flag.
    """
    schema = built_schemas[BOOTSTRAPPED]
    key = f"none-{uuid.uuid4().hex[:8]}"
    with _session(test_db, schema) as db:
        flag = FeatureFlag(
            key=key,
            name="None values",
            status=FeatureFlagStatus.INACTIVE,
            on_value=None,
            off_value=None,
        )
        db.add(flag)
        db.commit()
        flag.on_value = None
        flag.off_value = None
        flag.rollout_percentage = 10
        db.commit()
        with test_db.connect() as conn:
            stored = conn.execute(
                text(
                    "SELECT value_type, on_value IS NULL, off_value IS NULL "
                    f'FROM "{schema}".feature_flags WHERE key = :k'
                ),
                {"k": key},
            ).one()
        db.delete(flag)
        db.commit()
    assert tuple(stored) == ("boolean", True, True)


# ---------------------------------------------------------------------------
# 5. The previous release's code against this head (C2)
# ---------------------------------------------------------------------------
#: A string flag N stores and N-1 must serve as an ordinary flag.
VALUE_FLAG = "value-flag"
VALUE_FLAG_PERCENTAGE = 30
USERS = [f"user-{n}" for n in range(100)]

_PREVIOUS_RELEASE_WRITES = textwrap.dedent(
    """
    import json
    import sys

    from backend.app.models import register_core_models

    register_core_models()

    from backend.app.crud.crud_feature_flag import crud_feature_flag
    from backend.app.db.session import SessionLocal
    from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
    from backend.app.services.feature_flag_service import FeatureFlagService
    from backend.app.services.sdk_ruleset import build_ruleset

    # The previous release's model: it knows none of the three columns.
    columns = FeatureFlag.__table__.columns
    assert not {"value_type", "on_value", "off_value"} & set(columns.keys())

    users = json.loads(sys.argv[1])
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

    value_flag = db.query(FeatureFlag).filter(FeatureFlag.key == "value-flag").one()
    service = FeatureFlagService(db)
    answers = {}
    for user in users:
        detail = service.evaluate_flag_detailed(value_flag, user)
        answers[user] = [detail["enabled"], detail["reason"]]
    read = service.get_feature_flag(value_flag.id)
    crud_feature_flag.update(db, db_obj=value_flag, obj_in={"description": "N-1"})

    items = crud_feature_flag.get_multi(db, skip=0, limit=100)
    ruleset = build_ruleset(items)
    (entry,) = [e for e in ruleset["flags"] if e["key"] == "value-flag"]
    print(json.dumps({
        "id": str(flag.id),
        "rollout_percentage": flag.rollout_percentage,
        "listed": sorted(item.key for item in items),
        "read": [read.key, read.rollout_percentage],
        "answers": answers,
        "ruleset_entry_keys": sorted(entry),
    }))
    """
)

#: The model's declarations of the three columns and the check.  The previous
#: release is this tree with them removed (and the revision file); if one is
#: not found exactly once, the test says so rather than testing the wrong tree.
_MODEL_DECLARATIONS = (
    re.compile(r"(?ms)^    value_type = Column\(\n.*?^    \)\n"),
    re.compile(r"(?m)^    on_value = Column\(.*\)\n"),
    re.compile(r"(?m)^    off_value = Column\(.*\)\n"),
    re.compile(
        r"(?m)^ +CheckConstraint\(FLAG_VALUES_CHECK_SQL, name=FLAG_VALUES_CHECK\),\n"
    ),
)


def _previous_release(profile: str, tmp_path):
    """Release N-1: a copy of this tree without the revision, the columns and the check."""
    destination = tmp_path / "release-n-minus-1"
    if profile == FULL:
        tree = tree_profiles.full_tree(destination)
    else:
        tree = tree_profiles.core_tree(destination)
    versions = tree / "backend" / "app" / "db" / "migrations" / "versions"
    (revision_file,) = versions.glob(f"{REVISION}_*.py")
    revision_file.unlink()
    model = tree / "backend" / "app" / "models" / "feature_flag.py"
    source = model.read_text(encoding="utf-8")
    for declaration in _MODEL_DECLARATIONS:
        source, found = declaration.subn("", source)
        assert found == 1, f"{found} matches of {declaration.pattern}: update it"
    model.write_text(source, encoding="utf-8")
    return tree


def _this_release_answers(engine, schema: str) -> dict[str, list]:
    with _session(engine, schema) as db:
        flag = db.query(FeatureFlag).filter(FeatureFlag.key == VALUE_FLAG).one()
        service = FeatureFlagService(db)
        answers = {}
        for user in USERS:
            detail = service.evaluate_flag_detailed(flag, user)
            answers[user] = [detail["enabled"], detail["reason"]]
        db.rollback()
    return answers


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
        _bootstrapped(profile, scratch_schema, tmp_path)
    assert _rows(test_db, scratch_schema) == ROWS[profile]
    assert _columns(test_db, scratch_schema) == EXPECTED_COLUMNS
    _insert_value_flag(
        test_db,
        scratch_schema,
        VALUE_FLAG,
        status="ACTIVE",
        percentage=VALUE_FLAG_PERCENTAGE,
    )
    release_n_minus_1 = _previous_release(profile, tmp_path)

    script = tmp_path / "previous_release_writes.py"
    script.write_text(_PREVIOUS_RELEASE_WRITES, encoding="utf-8")
    result = tree_profiles.run(
        release_n_minus_1, [str(script), json.dumps(USERS)], scratch_schema
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-4000:]

    report = json.loads(result.stdout.strip().splitlines()[-1])
    print(
        f"[{profile}/{built_by}] previous release: listed={report['listed']} "
        f"read={report['read']} ruleset entry={report['ruleset_entry_keys']}"
    )
    assert report["rollout_percentage"] == 25
    assert report["listed"] == ["previous-release-flag", VALUE_FLAG]
    assert report["read"] == [VALUE_FLAG, VALUE_FLAG_PERCENTAGE]
    assert not {"value_type", "on_value", "off_value"} & set(
        report["ruleset_entry_keys"]
    )

    # The database filled the columns the previous release did not name, and
    # its update of the string flag left the values where they were.
    assert _stored_values(test_db, scratch_schema) == {
        "previous-release-flag": ("boolean", None, None, 25),
        VALUE_FLAG: ("string", "blue", "grey", VALUE_FLAG_PERCENTAGE),
    }

    # N-1 answers `enabled` for the string flag exactly as this release does,
    # and the 100 users are not all on or all off.
    answers = _this_release_answers(test_db, scratch_schema)
    assert report["answers"] == answers
    enabled = sum(1 for on, _ in answers.values() if on)
    assert 0 < enabled < len(USERS), enabled
    shutil.rmtree(release_n_minus_1, ignore_errors=True)


# ---------------------------------------------------------------------------
# 6. Downgrade
# ---------------------------------------------------------------------------
#: Any action on the version table, however it is phrased.
_VERSION_TABLE_ADVICE = re.compile(r"alembic_version|\bstamp\b", re.IGNORECASE)


def _refusal_line(stderr: str) -> str:
    return stderr[stderr.rindex("RuntimeError") :]


def _snapshot(engine, schema: str) -> tuple:
    return (
        _rows(engine, schema),
        _all_columns(engine, schema),
        _checks(engine, schema),
        _stored_values(engine, schema),
    )


@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_the_downgrade_refuses_while_a_value_flag_exists(
    profile, test_db, scratch_schema, tmp_path
):
    tree = _bootstrapped(profile, scratch_schema, tmp_path)
    _insert_previous_release_flag(test_db, scratch_schema, "a-boolean-flag")
    _insert_value_flag(test_db, scratch_schema, "checkout-copy")
    _insert_value_flag(
        test_db, scratch_schema, "price-tier", value_type="number", on="3", off="1"
    )
    before = _snapshot(test_db, scratch_schema)

    down = tree_profiles.alembic(tree, scratch_schema, "downgrade", PREVIOUS_CORE_HEAD)
    _report(profile, "after the refused downgrade", test_db, scratch_schema)

    assert down.returncode != 0, down.stdout[-2000:]
    refusal = _refusal_line(down.stderr)
    print(f"[{profile}] {refusal.splitlines()[0]}")
    assert "607c51a59bf5: refusing to downgrade: 2 feature flag(s)" in refusal
    assert "(checkout-copy, price-tier)" in refusal
    assert "a-boolean-flag" not in refusal
    assert "Delete those flags" in refusal
    assert "convert each to a boolean flag" in refusal
    assert "restore the backup taken before the upgrade" in refusal
    assert "Nothing was changed." in refusal
    advice = _VERSION_TABLE_ADVICE.search(refusal)
    assert advice is None, f"version-table advice {advice.group(0)!r}: {refusal}"
    # Nothing recorded, nothing dropped, no value lost.
    assert _snapshot(test_db, scratch_schema) == before


@pytest.mark.regression
def test_the_downgrade_and_the_upgrade_refuse_offline_mode(
    test_db, scratch_schema, tmp_path
):
    """``--sql`` cannot count the value flags, so it refuses whether or not
    there are any; the upgrade reads the catalog and refuses too."""
    tree = _bootstrapped(CORE, scratch_schema, tmp_path)
    _insert_value_flag(test_db, scratch_schema, "checkout-copy")
    before = _snapshot(test_db, scratch_schema)

    for argv in (
        ("downgrade", f"{REVISION}:{PREVIOUS_CORE_HEAD}", "--sql"),
        ("upgrade", f"{PREVIOUS_CORE_HEAD}:{REVISION}", "--sql"),
    ):
        offline = tree_profiles.alembic(tree, scratch_schema, *argv)
        assert offline.returncode != 0, (argv, offline.stdout[-2000:])
        assert "cannot be emitted as SQL; run it online" in offline.stderr, argv
        assert "DROP COLUMN" not in offline.stdout, argv

    assert _snapshot(test_db, scratch_schema) == before


@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_downgrade_removes_the_columns_and_the_check_and_nothing_else(
    profile, test_db, scratch_schema, tmp_path
):
    """With the string flags deleted or converted, as the refusal says, the
    downgrade runs and drops the three columns and the check only."""
    tree = _bootstrapped(profile, scratch_schema, tmp_path)
    flag_id = _insert_previous_release_flag(test_db, scratch_schema, "a-boolean-flag")
    _insert_value_flag(test_db, scratch_schema, "deleted")
    _insert_value_flag(test_db, scratch_schema, "converted")
    with test_db.begin() as conn:
        conn.execute(
            text(
                f"DELETE FROM \"{scratch_schema}\".feature_flags WHERE key = 'deleted'"
            )
        )
        conn.execute(
            text(
                f'UPDATE "{scratch_schema}".feature_flags SET value_type = '
                "'boolean', on_value = NULL, off_value = NULL WHERE key = 'converted'"
            )
        )
        converted_id = conn.execute(
            text(
                f'SELECT id::text FROM "{scratch_schema}".feature_flags '
                "WHERE key = 'converted'"
            )
        ).scalar_one()
    tables = _tables(test_db, scratch_schema)
    columns = _all_columns(test_db, scratch_schema)
    checks = _checks(test_db, scratch_schema)
    assert set(COLUMNS) <= columns
    assert CHECK in checks

    down = tree_profiles.alembic(tree, scratch_schema, "downgrade", PREVIOUS_CORE_HEAD)
    assert down.returncode == 0, down.stderr[-3000:]
    _report(profile, "after downgrade", test_db, scratch_schema)

    assert _rows(test_db, scratch_schema) == PREVIOUS_ROWS[profile]
    assert _all_columns(test_db, scratch_schema) == columns - set(COLUMNS)
    assert _checks(test_db, scratch_schema) == {
        name: definition for name, definition in checks.items() if name != CHECK
    }
    assert _tables(test_db, scratch_schema) == tables
    with test_db.connect() as conn:
        kept = conn.execute(
            text(f'SELECT id::text FROM "{scratch_schema}".feature_flags')
        ).scalars()
        assert sorted(kept) == sorted([flag_id, converted_id])

    # And upgrading again puts back exactly what the downgrade removed.
    _upgrade(tree, scratch_schema)
    assert _rows(test_db, scratch_schema) == ROWS[profile]
    assert _columns(test_db, scratch_schema) == EXPECTED_COLUMNS
    assert _checks(test_db, scratch_schema) == checks


# ---------------------------------------------------------------------------
# 7. The heads
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
