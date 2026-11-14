"""``modules_0005_redshift`` across the databases it can meet (#826).

The revision widens three CHECK constraints of ``warehouse_connections`` so a
connection may be an Amazon Redshift one (``redshift``): the type list, the
external-id rule and the no-secret rule, which now cover ``athena`` and
``redshift`` alike.  No column, index or row changes.  Each transition runs
as the deployment runs it -- raw ``alembic upgrade heads`` or ``downgrade`` in
a subprocess -- against both origins a full database can have:

* **migrated**: a core bootstrap, then the full image's migrations, so the
  constraints come from ``modules_0002_warehouse_analysis`` and this
  revision's replacements;
* **bootstrapped**: a fresh full bootstrap, so the constraints come from the
  model's ``create_all`` and this revision is only stamped.

What is pinned:

1. a database the previous full release left at
   ``modules_0004_rbac_activation``, holding a Snowflake, a BigQuery and an
   Athena connection, upgrades: every row is kept as it was, the constraints
   are the model's, and autogenerate is empty;
2. ``warehouse_connections`` on a migrated database is what a fresh full
   bootstrap builds, constraint text included;
3. on both origins the database accepts a Redshift row with an external id,
   no secret and no byte cap, and refuses one with a secret, with a pending
   secret, with no external id, or with a byte cap;
4. the previous release's INSERTs (``bigquery``, ``snowflake`` and
   ``athena`` rows, its column list) still succeed on both origins;
5. ``downgrade modules_0004_rbac_activation`` refuses while a Redshift row is
   stored and changes nothing; with none stored it narrows the three
   constraints back to the previous release's text on both origins and keeps
   every row, and an upgrade afterwards widens them again;
6. the previous release refuses the upgraded database and names this
   revision, and starts once the database is downgraded to
   ``modules_0004_rbac_activation``, with every row still there.
"""

from __future__ import annotations

import uuid
from typing import Optional

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from backend.app.db import bootstrap
from backend.tests.integration.database import tree_profiles
from backend.tests.integration.database.tree_profiles import CORE, FULL

pytestmark = [pytest.mark.integration, pytest.mark.modules]

CORE_HEAD = tree_profiles.CORE_HEAD
#: The modules head of the previous full release, and this revision's parent.
PREVIOUS_MODULES_HEAD = "modules_0004_rbac_activation"
#: This revision: the modules branch's head.
MODULES_HEAD = "modules_0005_redshift"
PREVIOUS_FULL_HEADS = {CORE_HEAD, PREVIOUS_MODULES_HEAD}
FULL_HEADS = {CORE_HEAD, MODULES_HEAD}

CONNECTIONS = "warehouse_connections"
#: The three constraints this revision replaces.
WIDENED = (
    "ck_warehouse_connections_type",
    "ck_warehouse_connections_external_id",
    "ck_warehouse_connections_athena_no_secret",
)

MIGRATED = "migrated"
BOOTSTRAPPED = "bootstrapped"
ORIGINS = pytest.mark.parametrize("origin", [MIGRATED, BOOTSTRAPPED])

#: The columns the previous release's model gives the table, as its INSERTs
#: name them.  A literal list, so a model change cannot move it.
PREVIOUS_RELEASE_COLUMNS = (
    "id",
    "name",
    "warehouse_type",
    "parameters",
    "credentials_ciphertext",
    "pending_credentials_ciphertext",
    "public_key_fingerprint",
    "pending_public_key_fingerprint",
    "external_id",
    "query_timeout_seconds",
    "max_bytes_per_query",
    "max_runs_per_day",
    "created_by",
    "created_at",
    "updated_at",
)

#: A stand-in for a Fernet token: the constraints read only whether it is there.
TOKEN = b"gAAAAA-not-a-real-token"
BYTE_CAP = 10_000_000


def _external_id() -> str:
    return f"exp-{uuid.uuid4().hex}"


#: One row of each type the previous release writes, as it writes them.
PREVIOUS_RELEASE_ROWS = {
    "snowflake": {
        "parameters": '{"account": "MYORG-MYACCOUNT", "key_slot": "1"}',
        "credentials_ciphertext": TOKEN,
        "public_key_fingerprint": "SHA256:abc=",
    },
    "bigquery": {
        "parameters": '{"billing_project": "acme-analytics", "location": "US"}',
        "credentials_ciphertext": TOKEN,
        "max_bytes_per_query": BYTE_CAP,
    },
    "athena": {
        "parameters": '{"region": "eu-west-1", "workgroup": "primary"}',
        "max_bytes_per_query": BYTE_CAP,
        "external_id": _external_id,
    },
}

#: A Redshift row the widened constraints accept.
REDSHIFT = {
    "parameters": '{"region": "eu-west-1", "workgroup_name": "analytics"}',
    "external_id": _external_id,
}

#: What a Redshift row must not be, each refused by a widened constraint (the
#: byte cap by the one this revision leaves alone).
REFUSED_REDSHIFT = {
    "with-a-secret": {"credentials_ciphertext": TOKEN},
    "with-a-pending-secret": {"pending_credentials_ciphertext": TOKEN},
    "without-an-external-id": {"external_id": None},
    "with-a-byte-cap": {"max_bytes_per_query": BYTE_CAP},
}


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def scratch_schema(test_db):
    schema = f"rs0005_{uuid.uuid4().hex[:8]}"
    try:
        yield schema
    finally:
        with test_db.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}_ref" CASCADE'))


@pytest.fixture
def full_tree():
    return tree_profiles.tree_for(FULL, None)


@pytest.fixture
def core_tree(tmp_path):
    return tree_profiles.tree_for(CORE, tmp_path)


def _ok(result) -> None:
    assert result.returncode == 0, (result.stdout[-2000:], result.stderr[-3000:])


def _rows(engine, schema: str) -> set[str]:
    return bootstrap.recorded_revisions(engine, schema)


def _at_previous_release(engine, schema: str, core_tree, full_tree) -> None:
    """A database built by migrations up to ``modules_0004_rbac_activation``:
    what the previous full release left."""
    _ok(tree_profiles.bootstrap_schema(core_tree, schema))
    _ok(tree_profiles.alembic(full_tree, schema, "upgrade", PREVIOUS_MODULES_HEAD))
    assert _rows(engine, schema) == PREVIOUS_FULL_HEADS


def _at_head(origin: str, engine, schema: str, core_tree, full_tree) -> None:
    """A full database at this tree's heads, built the *origin* way."""
    if origin == MIGRATED:
        _ok(tree_profiles.bootstrap_schema(core_tree, schema))
        _ok(tree_profiles.alembic(full_tree, schema, "upgrade", "heads"))
    else:
        _ok(tree_profiles.bootstrap_schema(full_tree, schema))
    assert _rows(engine, schema) == FULL_HEADS


def _insert(engine, schema: str, warehouse_type: str, **values) -> str:
    """One connection, written with the previous release's column list only."""
    row = dict.fromkeys(PREVIOUS_RELEASE_COLUMNS)
    row.update(
        id=str(uuid.uuid4()),
        name=f"{warehouse_type}-{uuid.uuid4().hex[:8]}",
        warehouse_type=warehouse_type,
        parameters="{}",
        query_timeout_seconds=300,
        max_runs_per_day=20,
    )
    for name, value in values.items():
        row[name] = value() if callable(value) else value
    placeholders = []
    for name in PREVIOUS_RELEASE_COLUMNS:
        if name in ("created_at", "updated_at"):
            placeholders.append("now()")
        elif name == "parameters":
            placeholders.append("CAST(:parameters AS jsonb)")
        else:
            placeholders.append(f":{name}")
    with engine.begin() as conn:
        conn.execute(
            text(
                f'INSERT INTO "{schema}".{CONNECTIONS} '
                f"({', '.join(PREVIOUS_RELEASE_COLUMNS)}) "
                f"VALUES ({', '.join(placeholders)})"
            ),
            {k: v for k, v in row.items() if k not in ("created_at", "updated_at")},
        )
    return row["id"]


def _insert_previous_release_rows(engine, schema: str) -> None:
    for warehouse_type, values in PREVIOUS_RELEASE_ROWS.items():
        _insert(engine, schema, warehouse_type, **values)


def _stored(engine, schema: str) -> set[tuple]:
    """Every row, the previous release's columns only, as text."""
    listed = ", ".join(f"CAST({name} AS text)" for name in PREVIOUS_RELEASE_COLUMNS)
    with engine.connect() as conn:
        return {
            tuple(row)
            for row in conn.execute(
                text(f'SELECT {listed} FROM "{schema}".{CONNECTIONS}')
            )
        }


def _constraints(engine, schema: str) -> dict[str, str]:
    """Every constraint of the table, by name, as the catalogue prints it."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = CAST(:rel AS regclass) ORDER BY conname"
            ),
            {"rel": f'"{schema}"."{CONNECTIONS}"'},
        ).all()
    return {name: definition.replace(schema, "<schema>") for name, definition in rows}


def _refused(engine, schema: str, **values) -> Optional[str]:
    """The constraint that refused a Redshift row, or None when it was stored."""
    try:
        _insert(engine, schema, "redshift", **{**REDSHIFT, **values})
    except IntegrityError as refusal:
        return getattr(refusal.orig.diag, "constraint_name", None) or "refused"
    return None


# ---------------------------------------------------------------------------
# 1. The previous release's database, upgraded
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_a_database_at_the_previous_release_upgrades_and_keeps_every_connection(
    test_db, scratch_schema, core_tree, full_tree, tmp_path
):
    """The upgrade every existing full deployment takes, by raw ``upgrade heads``."""
    _at_previous_release(test_db, scratch_schema, core_tree, full_tree)
    _insert_previous_release_rows(test_db, scratch_schema)
    before = _stored(test_db, scratch_schema)
    assert len(before) == 3
    previous = _constraints(test_db, scratch_schema)
    assert "redshift" not in "".join(previous[name] for name in WIDENED)

    upgrade = tree_profiles.alembic(full_tree, scratch_schema, "upgrade", "heads")
    _ok(upgrade)

    assert _rows(test_db, scratch_schema) == FULL_HEADS
    assert _stored(test_db, scratch_schema) == before
    after = _constraints(test_db, scratch_schema)
    assert set(after) == set(previous)
    assert {n for n in after if after[n] != previous[n]} == set(WIDENED)
    autogenerated = tree_profiles.autogenerate(
        full_tree, scratch_schema, tmp_path, head="modules@head"
    )
    assert autogenerated.body is not None, autogenerated.describe()
    assert autogenerated.operations == [], autogenerated.describe()


# ---------------------------------------------------------------------------
# 2. The model's constraints
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_the_migrated_table_has_the_models_constraints(
    test_db, scratch_schema, core_tree, full_tree
):
    """The migrated table against what a fresh full bootstrap -- the model --
    builds.  autogenerate compares no CHECK constraint, so a model and a
    migration that disagree on one would pass everything else."""
    _at_head(MIGRATED, test_db, scratch_schema, core_tree, full_tree)
    reference = f"{scratch_schema}_ref"
    _ok(tree_profiles.bootstrap_schema(full_tree, reference))

    migrated = _constraints(test_db, scratch_schema)
    built = {
        name: definition.replace(reference, "<schema>")
        for name, definition in _constraints(test_db, reference).items()
    }
    assert migrated == built
    for name in WIDENED:
        assert "redshift" in migrated[name], name


# ---------------------------------------------------------------------------
# 3. What a Redshift row may and may not be
# ---------------------------------------------------------------------------
@pytest.mark.regression
@ORIGINS
def test_the_database_accepts_a_redshift_row_with_an_external_id_and_no_secret(
    test_db, scratch_schema, core_tree, full_tree, origin
):
    _at_head(origin, test_db, scratch_schema, core_tree, full_tree)

    assert _refused(test_db, scratch_schema) is None
    with test_db.connect() as conn:
        stored = conn.execute(
            text(
                "SELECT warehouse_type, external_id IS NOT NULL, max_bytes_per_query "
                f'FROM "{scratch_schema}".{CONNECTIONS}'
            )
        ).all()
    assert [tuple(row) for row in stored] == [("redshift", True, None)]


@pytest.mark.regression
@ORIGINS
@pytest.mark.parametrize(
    ("case", "constraint"),
    [
        ("with-a-secret", "ck_warehouse_connections_athena_no_secret"),
        ("with-a-pending-secret", "ck_warehouse_connections_athena_no_secret"),
        ("without-an-external-id", "ck_warehouse_connections_external_id"),
        ("with-a-byte-cap", "ck_warehouse_connections_byte_cap_type"),
    ],
)
def test_the_database_refuses_a_redshift_row_that_breaks_a_rule(
    test_db, scratch_schema, core_tree, full_tree, origin, case, constraint
):
    _at_head(origin, test_db, scratch_schema, core_tree, full_tree)

    assert _refused(test_db, scratch_schema, **REFUSED_REDSHIFT[case]) == constraint
    assert _stored(test_db, scratch_schema) == set()


# ---------------------------------------------------------------------------
# 4. The previous release writes connections against this schema
# ---------------------------------------------------------------------------
@pytest.mark.regression
@ORIGINS
def test_the_previous_release_still_writes_each_of_its_types(
    test_db, scratch_schema, core_tree, full_tree, origin
):
    """During a deploy, and after an image-only rollback, the previous release
    keeps saving BigQuery, Snowflake and Athena connections."""
    _at_head(origin, test_db, scratch_schema, core_tree, full_tree)

    _insert_previous_release_rows(test_db, scratch_schema)

    with test_db.connect() as conn:
        types = conn.execute(
            text(f'SELECT warehouse_type FROM "{scratch_schema}".{CONNECTIONS}')
        ).scalars()
        assert sorted(types) == ["athena", "bigquery", "snowflake"]


# ---------------------------------------------------------------------------
# 5. Downgrade to the previous modules revision, and back
# ---------------------------------------------------------------------------
@pytest.mark.regression
@ORIGINS
def test_downgrade_refuses_while_a_redshift_connection_is_stored(
    test_db, scratch_schema, core_tree, full_tree, origin
):
    """The refusal names the connection, and nothing changes: the version
    table, the rows and the widened constraints are as they were."""
    _at_head(origin, test_db, scratch_schema, core_tree, full_tree)
    _insert_previous_release_rows(test_db, scratch_schema)
    redshift = _insert(test_db, scratch_schema, "redshift", **REDSHIFT)
    before = _stored(test_db, scratch_schema)
    constraints = _constraints(test_db, scratch_schema)

    down = tree_profiles.alembic(
        full_tree, scratch_schema, "downgrade", PREVIOUS_MODULES_HEAD
    )

    assert down.returncode != 0, down.stdout[-2000:]
    assert (
        "modules_0005_redshift: refusing to downgrade: 1 warehouse connection(s) "
        f"are Amazon Redshift connections ({redshift})."
    ) in down.stderr, down.stderr[-3000:]
    assert "Nothing was changed." in down.stderr
    assert _rows(test_db, scratch_schema) == FULL_HEADS
    assert _stored(test_db, scratch_schema) == before
    assert _constraints(test_db, scratch_schema) == constraints


@pytest.mark.regression
@ORIGINS
def test_downgrade_narrows_the_constraints_and_keeps_every_row(
    test_db, scratch_schema, core_tree, full_tree, origin
):
    """With no Redshift row stored, the downgrade puts back the constraints of
    a database the previous release migrated, keeps every row, and an upgrade
    afterwards widens them again."""
    reference = f"{scratch_schema}_ref"
    _at_previous_release(test_db, reference, core_tree, full_tree)
    previous = {
        name: definition.replace(reference, "<schema>")
        for name, definition in _constraints(test_db, reference).items()
    }
    _at_head(origin, test_db, scratch_schema, core_tree, full_tree)
    widened = _constraints(test_db, scratch_schema)
    _insert_previous_release_rows(test_db, scratch_schema)
    before = _stored(test_db, scratch_schema)

    _ok(
        tree_profiles.alembic(
            full_tree, scratch_schema, "downgrade", PREVIOUS_MODULES_HEAD
        )
    )

    assert _rows(test_db, scratch_schema) == PREVIOUS_FULL_HEADS
    assert _constraints(test_db, scratch_schema) == previous
    assert _stored(test_db, scratch_schema) == before
    assert _refused(test_db, scratch_schema) is not None

    _ok(tree_profiles.alembic(full_tree, scratch_schema, "upgrade", "heads"))

    assert _rows(test_db, scratch_schema) == FULL_HEADS
    assert _constraints(test_db, scratch_schema) == widened
    assert _stored(test_db, scratch_schema) == before


# ---------------------------------------------------------------------------
# 6. The previous release, put back
# ---------------------------------------------------------------------------
def _previous_full_release(tmp_path):
    """The previous full release's migrations: this tree without this revision.

    Built from parts, as ``tree_profiles`` builds its paths: a string literal
    naming a path under the modules would be a core -> module crossing.
    """
    tree = tree_profiles.full_tree(tmp_path / "previous-full-release")
    tree.joinpath(
        "modules",
        "backend",
        "app",
        "db",
        "migrations",
        "versions",
        f"{MODULES_HEAD}.py",
    ).unlink()
    return tree


@pytest.mark.regression
def test_the_previous_release_refuses_the_database_until_it_is_downgraded(
    test_db, scratch_schema, core_tree, full_tree, tmp_path
):
    """The previous image against the upgraded database refuses by both
    documented paths and names this revision; the modules downgrade to
    ``modules_0004_rbac_activation`` is the way back, and after it that image
    starts with every row in place."""
    _at_head(MIGRATED, test_db, scratch_schema, core_tree, full_tree)
    _insert_previous_release_rows(test_db, scratch_schema)
    before = _stored(test_db, scratch_schema)
    previous = _previous_full_release(tmp_path)

    for result in (
        tree_profiles.bootstrap_schema(previous, scratch_schema),
        tree_profiles.alembic(previous, scratch_schema, "upgrade", "heads"),
    ):
        assert result.returncode != 0, result.stdout[-2000:]
        assert "migrated by a newer Controlvar release" in result.stderr
        assert MODULES_HEAD in result.stderr
    assert _rows(test_db, scratch_schema) == FULL_HEADS

    _ok(
        tree_profiles.alembic(
            full_tree, scratch_schema, "downgrade", PREVIOUS_MODULES_HEAD
        )
    )
    _ok(tree_profiles.bootstrap_schema(previous, scratch_schema))

    assert _rows(test_db, scratch_schema) == PREVIOUS_FULL_HEADS
    assert _stored(test_db, scratch_schema) == before
