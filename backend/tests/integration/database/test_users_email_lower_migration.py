"""``d12cbd384bbe``: ``ix_users_email_lower``, through the transitions (#343).

The revision adds a unique index on ``lower(email)``, after a pre-check that
refuses the upgrade -- with account ids only, and nothing changed -- while two
accounts' addresses differ only in letter case.  A schema built by
``create_all`` already has the index (the model declares it), so the other
transition tests never run this revision's DDL.  Every test here that is about
the migration starts from what the previous release left instead: a
bootstrapped schema with the index DROPPED (no ``IF EXISTS``: a bootstrap
without the index fails loudly here) and ``alembic_version`` at the previous
head, in each profile, and runs the documented ``alembic upgrade heads`` or
``python -m backend.app.db.bootstrap`` in a subprocess, as a deployment does.

Gates, by the plan's names (``launch-readiness/email-case-343``):

* M1   two groups refuse the upgrade; the report text, both id groups, the count;
* M1b  the pre-check folds with PostgreSQL's ``lower()``, not Python's (U+0130);
* M1c  a refusal rolls back an earlier pending revision of the same run;
* M1d  the refusal through the bootstrap, with the anchor as stderr's last line;
* M1e  no address, local part, domain or username in the output -- including
       when the index build itself hits a duplicate (the race variant);
* M2   the upgraded index is exactly the bootstrapped one (``pg_get_indexdef``);
* M3   autogenerate against the upgraded database is empty;
* M4   a bootstrapped database has the index;
* M5   after the upgrade a case-only collision is refused, inactive rows too;
* M6   NULL addresses, any number of them, before and after;
* M7   downgrade removes exactly the index;
* M8   the profile switches;
* C1   an index of that name in any other state (INVALID, non-unique, another
       expression, partial) is refused, never taken as done;
* C2   the lock waits 30 s at most (detected with ``pg_blocking_pids()``, never
       with a timing) and the setting is back to its default afterwards;
* the runbook's SQL, read from the page, resolves a refused upgrade, a domain
  over 55 characters included.

The in-process gates (C2's reset, M1e's race variant) run the revision's
``upgrade()`` on one connection inside a transaction that is rolled back.
"""

from __future__ import annotations

import contextlib
import importlib.util
import subprocess
import sys
import traceback
import uuid
from typing import Iterator, Optional

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.exc import DataError, IntegrityError

from backend.app.db import bootstrap
from backend.tests.integration.database import tree_profiles
from backend.tests.integration.database.tree_profiles import CORE, FULL
from backend.tests.unit.docs.test_email_case_runbook import (
    CHECK,
    COPY_ROLE,
    LIST,
    RETIRE,
    RETIRE_LONG_DOMAIN,
    sql_blocks,
)

pytestmark = [pytest.mark.integration]

#: This revision, the core revision it extends, and the one before that.
REVISION = "d12cbd384bbe"
PREVIOUS_CORE_HEAD = "271f03a31742"
EARLIER_CORE_HEAD = "8fd44fb483a2"
#: The core head of this tree, which ``upgrade heads`` runs on to.
CORE_HEAD = "4c2c09a2b3e8"
#: The modules branch's head, in the previous release and in this one alike.
MODULES_HEAD = "modules_0002_warehouse_analysis"

PREVIOUS_ROWS = {
    CORE: {PREVIOUS_CORE_HEAD},
    FULL: {PREVIOUS_CORE_HEAD, MODULES_HEAD},
}
EARLIER_ROWS = {
    CORE: {EARLIER_CORE_HEAD},
    FULL: {EARLIER_CORE_HEAD, MODULES_HEAD},
}
ROWS = {
    CORE: {CORE_HEAD},
    FULL: {CORE_HEAD, MODULES_HEAD},
}

INDEX = "ix_users_email_lower"
ANCHOR_LINE = (
    "See docs/self-hosting/migrations.md#email-addresses-that-differ-only-in-case"
)
UNCHANGED = "Nothing has been changed"

#: The full-profile case needs a ``modules/`` directory; the core one builds
#: its own sealed tree and runs in either checkout.
BOTH_PROFILES = [CORE, pytest.param(FULL, marks=pytest.mark.modules)]

VERSIONS = tree_profiles.REPO_ROOT.joinpath(
    "backend", "app", "db", "migrations", "versions"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _token() -> str:
    """Eight letters g-v: never a substring of a UUID, which is 0-9 a-f."""
    return "".join(chr(ord("g") + int(c, 16)) for c in uuid.uuid4().hex[:8])


def _scratch(test_db, prefix: str):
    schema = f"{prefix}_{uuid.uuid4().hex[:8]}"
    try:
        yield schema
    finally:
        with test_db.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


@pytest.fixture
def scratch_schema(test_db):
    yield from _scratch(test_db, "emaillower")


@pytest.fixture
def reference_schema(test_db):
    yield from _scratch(test_db, "emaillowerref")


def expected_indexdef(schema: str) -> str:
    return (
        f"CREATE UNIQUE INDEX {INDEX} ON {schema}.users "
        "USING btree (lower((email)::text))"
    )


def _indexdef(engine, schema: str) -> Optional[str]:
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname = :s AND tablename = 'users' AND indexname = :i"
            ),
            {"s": schema, "i": INDEX},
        ).scalar()


def _index_flags(engine, schema: str) -> Optional[tuple]:
    """(indisvalid, indisunique) of the index, or None."""
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT i.indisvalid, i.indisunique FROM pg_index i "
                "JOIN pg_class c ON c.oid = i.indexrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = :s AND c.relname = :i"
            ),
            {"s": schema, "i": INDEX},
        ).one_or_none()
    return None if row is None else tuple(row)


def _index_names(engine, schema: str) -> set[str]:
    return {i["name"] for i in inspect(engine).get_indexes("users", schema=schema)}


def _rows(engine, schema: str) -> set[str]:
    return bootstrap.recorded_revisions(engine, schema)


def _set_rows(conn, schema: str, rows: set[str]) -> None:
    conn.execute(text(f'DELETE FROM "{schema}".alembic_version'))
    for revision in sorted(rows):
        conn.execute(
            text(f'INSERT INTO "{schema}".alembic_version VALUES (:rev)'),
            {"rev": revision},
        )


def _at_previous_release(engine, schema: str, rows: set[str]) -> None:
    """Turn a bootstrapped *schema* into what the previous release left."""
    with engine.begin() as conn:
        conn.execute(text(f'DROP INDEX "{schema}".{INDEX}'))
        _set_rows(conn, schema, rows)
    assert _indexdef(engine, schema) is None
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


def _insert(engine, schema: str, email: Optional[str], **values) -> str:
    """One users row; returns its id as text.  ``created_at`` orders a group."""
    user_id = str(uuid.uuid4())
    columns = {
        "id": user_id,
        "username": values.pop("username", f"u_{_token()}"),
        "email": email,
        "hashed_password": "not-a-real-hash",
        "is_active": values.pop("is_active", True),
        "is_superuser": values.pop("is_superuser", False),
        "role": values.pop("role", "VIEWER"),
        **values,
    }
    names = ", ".join(columns)
    params = ", ".join(f":{name}" for name in columns)
    with engine.begin() as conn:
        conn.execute(
            text(
                f'INSERT INTO "{schema}".users ({names}, created_at, updated_at) '
                f"VALUES ({params}, clock_timestamp(), clock_timestamp())"
            ),
            columns,
        )
    return user_id


def _users(engine, schema: str) -> dict:
    """Every row's identity columns, by id: the 'rows unchanged' comparison."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                f"SELECT id::text, email, username, is_active, is_superuser, role, "
                f'updated_at FROM "{schema}".users'
            )
        ).all()
    return {row[0]: tuple(row[1:]) for row in rows}


def _upgrade(tree, schema: str) -> subprocess.CompletedProcess:
    return tree_profiles.alembic(tree, schema, "upgrade", "heads")


def _assert_upgraded(test_db, tree, schema: str, profile: str) -> None:
    upgrade = _upgrade(tree, schema)
    assert upgrade.returncode == 0, upgrade.stderr[-3000:]
    assert _rows(test_db, schema) == ROWS[profile]
    assert _indexdef(test_db, schema) == expected_indexdef(schema)


def _last_line(stream: str) -> str:
    lines = [line for line in stream.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _case_pairs(test_db, schema: str, n: int = 2) -> tuple[list, list]:
    """*n* groups of two, oldest first; returns (id groups, every token used)."""
    groups, tokens = [], []
    for _ in range(n):
        local, domain, name1, name2 = _token(), _token(), _token(), _token()
        tokens += [local, domain, name1, name2]
        first = f"{local.capitalize()}@{domain}.example"
        second = f"{local}@{domain}.example"
        groups.append(
            [
                _insert(test_db, schema, first, username=name1),
                _insert(test_db, schema, second, username=name2),
            ]
        )
    return groups, tokens


def _load_revision(schema: str):
    """This revision's module, its schema pointed at *schema*.

    Loaded from the file under a private name, as alembic loads it; ``_SCHEMA``
    is set on the module rather than through ``POSTGRES_SCHEMA``, which this
    test process uses for its own schema.
    """
    (path,) = VERSIONS.glob(f"{REVISION}_*.py")
    spec = importlib.util.spec_from_file_location(
        f"_rev_{REVISION}_{uuid.uuid4().hex[:6]}", path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._SCHEMA = schema
    return module


@contextlib.contextmanager
def _in_one_transaction(engine) -> Iterator:
    """A connection in a transaction that ``op`` runs on; always rolled back."""
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            with Operations.context(MigrationContext.configure(conn)):
                yield conn
        finally:
            transaction.rollback()


# ---------------------------------------------------------------------------
# M1. Two groups refuse the upgrade; the report says so, with ids only
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_two_groups_refuse_the_upgrade_and_change_nothing(
    profile, test_db, scratch_schema, tmp_path
):
    """Only the report text proves the pre-check ran.

    Without it PostgreSQL still refuses ``CREATE UNIQUE INDEX`` -- so "exit
    status non-zero" passes either way -- but it names only the first key it
    meets, and names it by address.  Two groups, both reported by id, and the
    count separate the two.
    """
    tree = _bootstrapped_at_previous_release(test_db, profile, scratch_schema, tmp_path)
    groups, _ = _case_pairs(test_db, scratch_schema)
    _insert(test_db, scratch_schema, f"{_token()}@{_token()}.example")
    before = _users(test_db, scratch_schema)

    upgrade = _upgrade(tree, scratch_schema)
    print(upgrade.stderr[-2500:])

    assert upgrade.returncode != 0
    assert (
        f"Refusing to upgrade schema {scratch_schema}: 2 groups of accounts have "
        "email addresses that differ only in letter case" in upgrade.stderr
    )
    lines = upgrade.stderr.splitlines()
    for ids in groups:
        assert "  " + " ".join(ids) in lines, ids
    assert UNCHANGED in upgrade.stderr
    assert _last_line(upgrade.stderr) == ANCHOR_LINE
    assert _rows(test_db, scratch_schema) == PREVIOUS_ROWS[profile]
    assert _indexdef(test_db, scratch_schema) is None
    assert _users(test_db, scratch_schema) == before


def test_more_than_twenty_groups_are_capped_with_a_count(
    test_db, scratch_schema, tmp_path
):
    tree = _bootstrapped_at_previous_release(test_db, CORE, scratch_schema, tmp_path)
    groups, _ = _case_pairs(test_db, scratch_schema, n=23)

    upgrade = _upgrade(tree, scratch_schema)

    assert upgrade.returncode != 0
    assert "23 groups of accounts have" in upgrade.stderr
    listed = [ids for ids in groups if "  " + " ".join(ids) in upgrade.stderr]
    assert listed == groups[:20]
    assert "  ... and 3 more groups; the query in the section below lists them all" in (
        upgrade.stderr.splitlines()
    )
    assert _last_line(upgrade.stderr) == ANCHOR_LINE


# ---------------------------------------------------------------------------
# M1b. The pre-check folds case with PostgreSQL's lower()
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_the_pre_check_folds_case_as_the_index_does(test_db, scratch_schema, tmp_path):
    """U+0130: PostgreSQL's ``lower('İ')`` is ``i``; Python's is ``i̇``.

    Grouped in Python, the pair is two addresses and the report misses it;
    PostgreSQL's own refusal (with the address) appears instead.
    """
    tree = _bootstrapped_at_previous_release(test_db, CORE, scratch_schema, tmp_path)
    local, domain = _token(), _token()
    dotted = f"İ{local}@{domain}.example"
    plain = f"i{local}@{domain}.example"
    with test_db.connect() as conn:
        assert conn.execute(
            text("SELECT lower(:a) = lower(:b)"), {"a": dotted, "b": plain}
        ).scalar(), "this database's lower() does not fold U+0130 to i"
    assert dotted.lower() != plain.lower()
    ids = [
        _insert(test_db, scratch_schema, dotted),
        _insert(test_db, scratch_schema, plain),
    ]

    upgrade = _upgrade(tree, scratch_schema)

    assert upgrade.returncode != 0
    assert "1 group of accounts has" in upgrade.stderr
    assert "  " + " ".join(ids) in upgrade.stderr.splitlines()
    assert local not in upgrade.stderr.lower()
    assert _indexdef(test_db, scratch_schema) is None


# ---------------------------------------------------------------------------
# M1c. A refusal rolls back an earlier pending revision of the same run
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_a_refusal_rolls_back_an_earlier_pending_revision(
    profile, test_db, scratch_schema, tmp_path
):
    """Two releases at once: ``271f03a31742`` and this revision both pending.

    ``env.py`` runs every pending revision in one transaction, so the
    refusal takes ``271f03a31742``'s column and check back out with it.
    """
    tree = _bootstrapped(test_db, profile, scratch_schema, tmp_path)
    with test_db.begin() as conn:
        conn.execute(
            text(
                f'ALTER TABLE "{scratch_schema}".experiments '
                "DROP CONSTRAINT ck_experiments_resume_only_when_paused"
            )
        )
        conn.execute(
            text(f'ALTER TABLE "{scratch_schema}".experiments DROP COLUMN resume_at')
        )
        conn.execute(text(f'DROP INDEX "{scratch_schema}".{INDEX}'))
        _set_rows(conn, scratch_schema, EARLIER_ROWS[profile])
    _case_pairs(test_db, scratch_schema, n=1)

    upgrade = _upgrade(tree, scratch_schema)

    assert upgrade.returncode != 0
    assert "differ only in letter case" in upgrade.stderr
    assert _rows(test_db, scratch_schema) == EARLIER_ROWS[profile]
    columns = {
        c["name"]
        for c in inspect(test_db).get_columns("experiments", schema=scratch_schema)
    }
    assert "resume_at" not in columns
    assert _indexdef(test_db, scratch_schema) is None


# ---------------------------------------------------------------------------
# M1d. The refusal through the bootstrap, as the entrypoint and the ECS task run it
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_the_bootstrap_refuses_and_ends_with_the_anchor(
    profile, test_db, scratch_schema, tmp_path
):
    tree = _bootstrapped_at_previous_release(test_db, profile, scratch_schema, tmp_path)
    groups, _ = _case_pairs(test_db, scratch_schema, n=1)
    before = _users(test_db, scratch_schema)

    run = tree_profiles.bootstrap_schema(tree, scratch_schema)

    assert run.returncode != 0
    assert "  " + " ".join(groups[0]) in run.stderr.splitlines()
    # The refusal's own stderr ends with the runbook link (on AWS the Deploy
    # log joins the tail into one tab-separated line; this is its last field).
    assert _last_line(run.stderr) == ANCHOR_LINE
    assert _rows(test_db, scratch_schema) == PREVIOUS_ROWS[profile]
    assert _indexdef(test_db, scratch_schema) is None
    assert _users(test_db, scratch_schema) == before


# ---------------------------------------------------------------------------
# M1e. No address in the output, and the race variant
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("command", ["alembic", "bootstrap"])
def test_the_refusal_names_no_address_domain_or_username(
    command, test_db, scratch_schema, tmp_path
):
    tree = _bootstrapped_at_previous_release(test_db, CORE, scratch_schema, tmp_path)
    groups, tokens = _case_pairs(test_db, scratch_schema)

    if command == "alembic":
        run = _upgrade(tree, scratch_schema)
    else:
        run = tree_profiles.bootstrap_schema(tree, scratch_schema)
    output = (run.stdout + run.stderr).lower()

    assert run.returncode != 0
    for ids in groups:
        assert all(i in output for i in ids)
    named = [token for token in tokens if token in output]
    assert not named, f"the output names {named}"
    assert ".example" not in output


@pytest.mark.regression
def test_a_duplicate_met_by_the_index_build_is_refused_without_the_address(
    test_db, scratch_schema, tmp_path
):
    """The race variant: the pre-check finds nothing, the build hits the pair.

    The lock makes this unreachable in a deployment; the mapping is what
    keeps PostgreSQL's DETAIL -- which names the address -- out of the
    output if it ever is reached.  ``from None`` is what drops it from the
    traceback, so the traceback is what is checked.
    """
    _bootstrapped_at_previous_release(test_db, CORE, scratch_schema, tmp_path)
    _, tokens = _case_pairs(test_db, scratch_schema, n=1)
    revision = _load_revision(scratch_schema)
    revision._duplicate_groups = list

    with _in_one_transaction(test_db):
        with pytest.raises(RuntimeError) as exc_info:
            revision.upgrade()

    printed = "".join(traceback.format_exception(exc_info.value)).lower()
    message = str(exc_info.value)
    assert "could not be built" in message
    assert UNCHANGED in message
    assert message.splitlines()[-1] == ANCHOR_LINE
    named = [token for token in tokens if token in printed]
    assert not named, f"the traceback names {named}"
    assert "detail" not in printed and "is duplicated" not in printed
    assert _indexdef(test_db, scratch_schema) is None


# ---------------------------------------------------------------------------
# M2 / M3 / M4. The index, as upgraded and as bootstrapped
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_the_upgraded_index_is_exactly_the_bootstrapped_one(
    profile, test_db, scratch_schema, reference_schema, tmp_path
):
    """M2: the only gate that sees a partial or otherwise reshaped index."""
    tree = _bootstrapped_at_previous_release(test_db, profile, scratch_schema, tmp_path)
    _assert_upgraded(test_db, tree, scratch_schema, profile)
    assert _index_flags(test_db, scratch_schema) == (True, True)

    created = tree_profiles.bootstrap_schema(tree, reference_schema)
    assert created.returncode == 0, created.stderr[-3000:]

    upgraded = _indexdef(test_db, scratch_schema)
    bootstrapped = _indexdef(test_db, reference_schema)
    print(f"[{profile}] upgraded:     {upgraded}")
    print(f"[{profile}] bootstrapped: {bootstrapped}")
    assert upgraded == expected_indexdef(scratch_schema)
    assert bootstrapped == expected_indexdef(reference_schema)


@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_autogenerate_after_the_upgrade_is_empty(
    profile, test_db, scratch_schema, tmp_path
):
    """M3: the migration and the model describe the same index."""
    tree = _bootstrapped_at_previous_release(test_db, profile, scratch_schema, tmp_path)
    _assert_upgraded(test_db, tree, scratch_schema, profile)

    result = tree_profiles.autogenerate(
        tree, scratch_schema, tmp_path, head=tree_profiles.CORE_HEAD
    )
    assert result.body is not None, result.describe()
    assert result.operations == [], result.describe()


@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_a_bootstrapped_database_has_the_index(
    profile, test_db, scratch_schema, tmp_path
):
    """M4: the model declares it (``test_autogenerate_is_empty`` cannot see that)."""
    _bootstrapped(test_db, profile, scratch_schema, tmp_path)
    assert _indexdef(test_db, scratch_schema) == expected_indexdef(scratch_schema)
    assert _index_flags(test_db, scratch_schema) == (True, True)
    assert _rows(test_db, scratch_schema) == ROWS[profile]


# ---------------------------------------------------------------------------
# M5. After the upgrade a collision is refused, inactive rows included
# ---------------------------------------------------------------------------
@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_after_the_upgrade_a_case_only_collision_is_refused(
    profile, test_db, scratch_schema, tmp_path
):
    tree = _bootstrapped_at_previous_release(test_db, profile, scratch_schema, tmp_path)
    _assert_upgraded(test_db, tree, scratch_schema, profile)
    local, domain = _token(), _token()
    _insert(test_db, scratch_schema, f"{local.capitalize()}@{domain}.example")

    for spelling, active in ((local, True), (local.upper(), False)):
        with pytest.raises(IntegrityError) as exc_info:
            _insert(
                test_db,
                scratch_schema,
                f"{spelling}@{domain}.example",
                is_active=active,
            )
        assert exc_info.value.orig.diag.constraint_name == INDEX


# ---------------------------------------------------------------------------
# M6. NULL addresses
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_null_addresses_are_allowed_before_and_after(
    profile, test_db, scratch_schema, tmp_path
):
    tree = _bootstrapped_at_previous_release(test_db, profile, scratch_schema, tmp_path)
    nulls = [_insert(test_db, scratch_schema, None) for _ in range(2)]

    _assert_upgraded(test_db, tree, scratch_schema, profile)
    nulls.append(_insert(test_db, scratch_schema, None))

    users = _users(test_db, scratch_schema)
    assert all(users[i][0] is None for i in nulls)


# ---------------------------------------------------------------------------
# M7. Downgrade
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_downgrade_removes_exactly_the_index(
    profile, test_db, scratch_schema, tmp_path
):
    tree = _bootstrapped(test_db, profile, scratch_schema, tmp_path)
    before = _index_names(test_db, scratch_schema)
    assert INDEX in before

    down = tree_profiles.alembic(tree, scratch_schema, "downgrade", PREVIOUS_CORE_HEAD)
    assert down.returncode == 0, down.stderr[-3000:]

    assert _rows(test_db, scratch_schema) == PREVIOUS_ROWS[profile]
    assert _index_names(test_db, scratch_schema) == before - {INDEX}
    # A case pair can be stored again.
    local, domain = _token(), _token()
    _insert(test_db, scratch_schema, f"{local.capitalize()}@{domain}.example")
    _insert(test_db, scratch_schema, f"{local}@{domain}.example")


# ---------------------------------------------------------------------------
# M8. The profile switches
# ---------------------------------------------------------------------------
@pytest.mark.modules
def test_a_core_database_met_by_the_full_image_gains_the_index(
    test_db, scratch_schema, tmp_path
):
    core = _bootstrapped_at_previous_release(test_db, CORE, scratch_schema, tmp_path)
    assert core != tree_profiles.REPO_ROOT
    full = tree_profiles.tree_for(FULL, tmp_path)

    upgrade = _upgrade(full, scratch_schema)

    assert upgrade.returncode == 0, upgrade.stderr[-3000:]
    assert _rows(test_db, scratch_schema) == ROWS[FULL]
    assert _indexdef(test_db, scratch_schema) == expected_indexdef(scratch_schema)


@pytest.mark.modules
def test_a_full_database_met_by_the_core_image_is_refused_unchanged(
    test_db, scratch_schema, tmp_path
):
    """A full database one release behind: the core chain is behind, so refused."""
    _bootstrapped_at_previous_release(test_db, FULL, scratch_schema, tmp_path)
    core = tree_profiles.tree_for(CORE, tmp_path)

    upgrade = _upgrade(core, scratch_schema)

    assert upgrade.returncode != 0
    assert "Run the full image of this release against it" in upgrade.stderr
    assert _rows(test_db, scratch_schema) == PREVIOUS_ROWS[FULL]
    assert _indexdef(test_db, scratch_schema) is None


# ---------------------------------------------------------------------------
# C1. An index of that name that is not this index is refused, not skipped
# ---------------------------------------------------------------------------
WRONG_SHAPES = {
    "non-unique": "CREATE INDEX {i} ON {s}.users (lower(email))",
    "another-expression": "CREATE UNIQUE INDEX {i} ON {s}.users (upper(email))",
    "partial": "CREATE UNIQUE INDEX {i} ON {s}.users (lower(email)) WHERE is_active",
}


def _assert_wrong_index_refused(
    test_db, tree, schema: str, profile: str
) -> subprocess.CompletedProcess:
    flags_before = _index_flags(test_db, schema)
    definition_before = _indexdef(test_db, schema)
    before = _users(test_db, schema)

    upgrade = _upgrade(tree, schema)
    print(upgrade.stderr[-1500:])

    assert upgrade.returncode != 0
    assert f"it already has an index named {INDEX}" in upgrade.stderr
    assert f"  DROP INDEX {schema}.{INDEX};" in upgrade.stderr.splitlines()
    assert _last_line(upgrade.stderr) == ANCHOR_LINE
    assert _rows(test_db, schema) == PREVIOUS_ROWS[profile]
    assert _index_flags(test_db, schema) == flags_before
    assert _indexdef(test_db, schema) == definition_before
    assert _users(test_db, schema) == before
    return upgrade


@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_an_invalid_index_left_by_a_failed_concurrent_build_is_refused(
    profile, test_db, scratch_schema, tmp_path
):
    """What an operator gets by building the index by hand over a pair."""
    tree = _bootstrapped_at_previous_release(test_db, profile, scratch_schema, tmp_path)
    _, tokens = _case_pairs(test_db, scratch_schema, n=1)
    with test_db.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    f"CREATE UNIQUE INDEX CONCURRENTLY {INDEX} "
                    f'ON "{scratch_schema}".users (lower(email))'
                )
            )
    assert _index_flags(test_db, scratch_schema) == (False, True)
    # Same definition: only indisvalid tells this index from a good one.
    assert _indexdef(test_db, scratch_schema) == expected_indexdef(scratch_schema)

    refused = _assert_wrong_index_refused(test_db, tree, scratch_schema, profile)
    output = (refused.stdout + refused.stderr).lower()
    assert not [t for t in tokens if t in output]


@pytest.mark.regression
@pytest.mark.parametrize("shape", sorted(WRONG_SHAPES))
def test_an_index_of_that_name_in_another_shape_is_refused(
    shape, test_db, scratch_schema, tmp_path
):
    tree = _bootstrapped_at_previous_release(test_db, CORE, scratch_schema, tmp_path)
    with test_db.begin() as conn:
        conn.execute(text(WRONG_SHAPES[shape].format(i=INDEX, s=f'"{scratch_schema}"')))

    _assert_wrong_index_refused(test_db, tree, scratch_schema, CORE)


# ---------------------------------------------------------------------------
# C2. The lock: 30 s at most, and the setting is put back
# ---------------------------------------------------------------------------
def _start_upgrade(tree, schema: str) -> subprocess.Popen:
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(tree_profiles.alembic_ini(tree)),
            "upgrade",
            "heads",
        ],
        cwd=str(tree),
        env=tree_profiles._environment(tree, schema),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _waiting_lock_pid(conn, schema: str) -> Optional[int]:
    return conn.execute(
        text(
            "SELECT pid FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid() "
            "AND wait_event_type = 'Lock' AND query LIKE :q"
        ),
        {"q": f"LOCK TABLE {schema}.users IN SHARE MODE%"},
    ).scalar()


@pytest.mark.regression
def test_a_writer_holding_users_ends_the_upgrade_after_the_lock_timeout(
    test_db, scratch_schema, tmp_path
):
    """C2 (a).  A session with an uncommitted UPDATE on ``users`` holds ROW
    EXCLUSIVE, which SHARE waits for.  The upgrade must be seen waiting on
    that session (``pg_blocking_pids``), then give up by itself with a clean
    refusal that is not the duplicates one, and change nothing.  Without the
    ``lock_timeout`` it waits for as long as the holder lives, and the guard
    below fails the test.
    """
    tree = _bootstrapped_at_previous_release(test_db, CORE, scratch_schema, tmp_path)
    row = _insert(test_db, scratch_schema, f"{_token()}@{_token()}.example")
    before = _users(test_db, scratch_schema)

    holder = test_db.connect()
    holder_transaction = holder.begin()
    process = None
    try:
        holder.execute(
            text(
                f'UPDATE "{scratch_schema}".users SET is_active = is_active '
                "WHERE id = :id"
            ),
            {"id": row},
        )
        holder_pid = holder.execute(text("SELECT pg_backend_pid()")).scalar()
        process = _start_upgrade(tree, scratch_schema)

        blocked_by = None
        # AUTOCOMMIT: pg_stat_activity is a snapshot per transaction, so a
        # watcher polling inside one transaction would never see the change.
        with test_db.connect().execution_options(
            isolation_level="AUTOCOMMIT"
        ) as watcher:
            for _ in range(600):  # a guard, not a timing: ~60 s of 0.1 s polls
                pid = _waiting_lock_pid(watcher, scratch_schema)
                if pid is not None:
                    blocked_by = watcher.execute(
                        text("SELECT pg_blocking_pids(:pid)"), {"pid": pid}
                    ).scalar()
                    break
                if process.poll() is not None:
                    break
                watcher.execute(text("SELECT pg_sleep(0.1)"))
        assert blocked_by is not None, (
            "the upgrade was never seen waiting for the lock: "
            + (process.stderr.read() if process.poll() is not None else "")
        )
        assert holder_pid in blocked_by

        try:
            _, stderr = process.communicate(timeout=180)
        except subprocess.TimeoutExpired:
            pytest.fail(
                "the upgrade was still waiting for the lock after 180 s: "
                "lock_timeout did not end it"
            )
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.communicate()
        holder_transaction.rollback()
        holder.close()

    assert process.returncode != 0
    assert "another session is writing to users" in stderr
    assert "still held its lock after 30s" in stderr
    # Not relabelled as the duplicates refusal (EM condition 5).
    assert "differ only in letter case" not in stderr
    assert UNCHANGED in stderr
    assert _last_line(stderr) == ANCHOR_LINE
    assert _rows(test_db, scratch_schema) == PREVIOUS_ROWS[CORE]
    assert _indexdef(test_db, scratch_schema) is None
    assert _users(test_db, scratch_schema) == before


@pytest.mark.regression
def test_lock_timeout_is_back_to_its_default_after_the_lock(
    test_db, scratch_schema, tmp_path
):
    """C2 (b), in the same transaction: no later revision inherits the 30 s."""
    _bootstrapped_at_previous_release(test_db, CORE, scratch_schema, tmp_path)
    revision = _load_revision(scratch_schema)

    with _in_one_transaction(test_db) as conn:
        default = conn.execute(text("SHOW lock_timeout")).scalar()
        revision.upgrade()
        after = conn.execute(text("SHOW lock_timeout")).scalar()
        built = conn.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname = :s AND indexname = :i"
            ),
            {"s": scratch_schema, "i": INDEX},
        ).scalar()

    # The path past the lock really ran: the index was built in this transaction.
    assert built == expected_indexdef(scratch_schema)
    assert after == default
    assert after != "30s"


# ---------------------------------------------------------------------------
# The runbook's SQL, read from the page, resolves a refused upgrade
# ---------------------------------------------------------------------------
def _runbook(name: str, schema: str, **ids: str) -> str:
    sql = sql_blocks()[name].replace("experimentation.users", f'"{schema}".users')
    for placeholder, value in ids.items():
        sql = sql.replace(f"'<{placeholder.replace('_', ' ')}>'", f"'{value}'")
    assert "<" not in sql, sql
    return sql


@pytest.mark.regression
def test_the_runbook_sql_resolves_a_refused_upgrade(test_db, scratch_schema, tmp_path):
    """Steps 1-5 of the runbook, as written, then the upgrade succeeds.

    Two groups: an ordinary domain, and one of 57 characters, over the 55 the
    ``retired-<id>@`` form fits.  In each the older account (``Mixed@``) is
    the ADMIN superuser and is retired; the newer one (lower-case, VIEWER) is
    the account the person signs in with, and is kept.
    """
    tree = _bootstrapped_at_previous_release(test_db, CORE, scratch_schema, tmp_path)
    short_domain = f"{_token()}.example"
    long_domain = f"{_token()}-{'d' * 40}.example"  # 8 + 1 + 40 + 8 = 57
    assert 55 < len(long_domain) <= 61, len(long_domain)
    pairs = {}
    for domain in (short_domain, long_domain):
        local = _token()
        retire = _insert(
            test_db,
            scratch_schema,
            f"{local.capitalize()}@{domain}",
            role="ADMIN",
            is_superuser=True,
        )
        keep = _insert(test_db, scratch_schema, f"{local}@{domain}")
        pairs[domain] = (keep, retire, f"{local}@{domain}")
    assert _upgrade(tree, scratch_schema).returncode != 0

    with test_db.connect() as conn:
        listing = conn.execute(text(_runbook(LIST, scratch_schema))).mappings().all()
    listed = {(row["id"].__str__(), row["email"]) for row in listing}
    for keep, retire, address in pairs.values():
        assert (keep, address) in listed
        assert (retire, address[0].upper() + address[1:]) in listed

    for domain, (keep, retire, _) in pairs.items():
        ids = {"id_to_keep": keep, "id_to_retire": retire}
        with test_db.begin() as conn:
            conn.execute(text(_runbook(COPY_ROLE, scratch_schema, **ids)))
        if domain == long_domain:
            with pytest.raises(DataError, match="value too long"):
                with test_db.begin() as conn:
                    conn.execute(text(_runbook(RETIRE, scratch_schema, **ids)))
            step = RETIRE_LONG_DOMAIN
        else:
            step = RETIRE
        with test_db.begin() as conn:
            conn.execute(text(_runbook(step, scratch_schema, **ids)))

    with test_db.connect() as conn:
        assert conn.execute(text(_runbook(CHECK, scratch_schema))).all() == []

    _assert_upgraded(test_db, tree, scratch_schema, CORE)

    users = _users(test_db, scratch_schema)
    prefixes = {short_domain: "retired-", long_domain: "r-"}
    for domain, (keep, retire, address) in pairs.items():
        # The kept row is the one the step named: its address unchanged, active,
        # and now with the retired account's role and superuser flag.
        assert users[keep][:5] == (address, users[keep][1], True, True, "ADMIN")
        assert users[retire][0] == f"{prefixes[domain]}{retire}@{domain}"
        assert users[retire][2] is False
