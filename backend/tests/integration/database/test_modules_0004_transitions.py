"""``modules_0004_rbac_activation`` across the databases it can meet (#891).

The revision adds ``activated_at`` (``timestamp with time zone``, nullable, no
default) to ``user_custom_roles`` and ``direct_permission_grants``, and logs
the rows that have none; activation times below are planted by SQL.  Each
transition runs as the deployment runs it -- raw ``alembic upgrade
heads`` or ``downgrade`` in a subprocess -- against both origins a full
database can have:

* **migrated**: a core bootstrap, then the full image's migrations, so the
  columns come from this revision's ``add_column``;
* **bootstrapped**: a fresh full bootstrap, so the columns come from the
  models' ``create_all`` and this revision is only stamped.

What is pinned:

1. a database the previous full release left at
   ``modules_0003_lower_is_better``, holding two assignments and three grants
   (one expired, one with ``is_active`` false), upgrades: every row is kept
   and has no activation time, neither column has a default, the log is the
   operator page's text word for word, and autogenerate is empty;
2. the log's grammar: one grant, one assignment, more rows than it lists, and
   no rows at all (one INFO line);
3. both tables, columns and their defaults included, are what a fresh full
   bootstrap builds -- ``test_modules_0002_transitions.py``'s shape and
   env.py's autogenerate never compare a default, so a model and a migration
   that disagree on one would pass everything else;
4. the previous release's INSERTs, which do not name the column, still
   succeed, and their rows have no activation time, on both origins;
5. ``downgrade modules_0003_lower_is_better`` drops both columns on both
   origins and keeps every row with its other columns; an activation time
   stored before it is gone, and an upgrade afterwards adds the columns back
   empty;
6. the previous release refuses the upgraded database and names this
   revision, and starts once the database is downgraded to
   ``modules_0003_lower_is_better``, with every row still there.
"""

from __future__ import annotations

import re
import uuid
from typing import Optional

import pytest
from sqlalchemy import inspect, text

from backend.app.db import bootstrap
from backend.tests.integration.database import tree_profiles
from backend.tests.integration.database.tree_profiles import CORE, FULL

pytestmark = [pytest.mark.integration, pytest.mark.modules]

CORE_HEAD = tree_profiles.CORE_HEAD
#: The modules head of the previous full release, and this revision's parent.
PREVIOUS_MODULES_HEAD = "modules_0003_lower_is_better"
#: This revision: the modules branch's head.
MODULES_HEAD = "modules_0004_rbac_activation"
PREVIOUS_FULL_HEADS = {CORE_HEAD, PREVIOUS_MODULES_HEAD}
FULL_HEADS = {CORE_HEAD, MODULES_HEAD}

ASSIGNMENTS = "user_custom_roles"
GRANTS = "direct_permission_grants"
RBAC_TABLES = (ASSIGNMENTS, GRANTS)
COLUMN = "activated_at"

MIGRATED = "migrated"
BOOTSTRAPPED = "bootstrapped"
ORIGINS = pytest.mark.parametrize("origin", [MIGRATED, BOOTSTRAPPED])

#: The columns the previous release's models give each table, as its INSERTs
#: name them: everything ``modules_0001_rbac`` created, and not this
#: revision's column.  Literal lists, so a model change cannot move them.
PREVIOUS_RELEASE_COLUMNS = {
    ASSIGNMENTS: (
        "id",
        "user_id",
        "role_id",
        "assigned_by_id",
        "reason",
        "assigned_at",
        "created_at",
        "updated_at",
    ),
    GRANTS: (
        "id",
        "user_id",
        "resource",
        "actions",
        "granted_by_id",
        "reason",
        "expires_at",
        "is_active",
        "created_at",
        "updated_at",
    ),
}

#: What the upgrade logs, word for word; the operator page's section on this
#: revision quotes it.  The test opens no page: the path is part of the text.
NO_ACTIVATION_TIME = (
    " no activation time (activated_at is empty; expired grants and grants "
    "with is_active false are not counted or listed). See docs/self-hosting/"
    'migrations.md, "modules_0004_rbac_activation":'
)
NONE_LOGGED = (
    "modules_0004_rbac_activation: no custom role assignments or direct "
    "permission grants without an activation time"
)
#: The most rows the upgrade lists one by one.
LISTED = 200

FUTURE = "2099-01-01T00:00:00+00:00"
PAST = "2001-01-01T00:00:00+00:00"


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def scratch_schema(test_db):
    schema = f"rb0004_{uuid.uuid4().hex[:8]}"
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


def _columns(engine, schema: str, table: str) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns(table, schema=schema)}


def _catalogue(engine, schema: str, table: str) -> Optional[tuple]:
    """``(data_type, is_nullable, column_default, atthasdef)`` of the column,
    or None when the table has no such column."""
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT c.data_type, c.is_nullable, c.column_default, a.atthasdef "
                "FROM information_schema.columns c "
                "JOIN pg_attribute a ON a.attrelid = "
                "CAST(quote_ident(c.table_schema) || '.' || quote_ident(c.table_name) "
                "AS regclass) AND a.attname = c.column_name "
                "WHERE c.table_schema = :schema AND c.table_name = :table "
                "AND c.column_name = :column"
            ),
            {"schema": schema, "table": table, "column": COLUMN},
        ).one_or_none()
    return tuple(row) if row is not None else None


#: Nullable ``timestamptz`` with no default of any kind.
NO_DEFAULT = ("timestamp with time zone", "YES", None, False)


def _at_previous_release(engine, schema: str, core_tree, full_tree) -> None:
    """A database built by migrations up to ``modules_0003_lower_is_better``:
    what the previous full release left."""
    _ok(tree_profiles.bootstrap_schema(core_tree, schema))
    _ok(tree_profiles.alembic(full_tree, schema, "upgrade", PREVIOUS_MODULES_HEAD))
    assert _rows(engine, schema) == PREVIOUS_FULL_HEADS
    for table in RBAC_TABLES:
        assert COLUMN not in _columns(engine, schema, table)


def _at_head(origin: str, engine, schema: str, core_tree, full_tree) -> None:
    """A full database at this tree's heads, built the *origin* way."""
    if origin == MIGRATED:
        _ok(tree_profiles.bootstrap_schema(core_tree, schema))
        _ok(tree_profiles.alembic(full_tree, schema, "upgrade", "heads"))
    else:
        _ok(tree_profiles.bootstrap_schema(full_tree, schema))
    assert _rows(engine, schema) == FULL_HEADS


def _insert_user(engine, schema: str) -> str:
    user_id = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(
            text(
                f'INSERT INTO "{schema}".users (id, username, email, '
                "hashed_password, is_active, is_superuser, role, created_at, "
                "updated_at) VALUES (:id, :username, :email, 'not-a-real-hash', "
                "true, false, 'VIEWER', now(), now())"
            ),
            {
                "id": user_id,
                "username": f"u_{user_id[:8]}",
                "email": f"{user_id[:8]}@example.com",
            },
        )
    return user_id


def _insert_role(engine, schema: str, name: str) -> str:
    role_id = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(
            text(
                f'INSERT INTO "{schema}".custom_roles (id, name, is_system_role, '
                "permissions, created_at, updated_at) VALUES "
                "(:id, :name, false, '[]', now(), now())"
            ),
            {"id": role_id, "name": name},
        )
    return role_id


def _insert_as_the_previous_release(engine, schema: str, table: str, **values) -> str:
    """One row, written with the previous release's column list only."""
    row = {"id": str(uuid.uuid4()), **values}
    columns = PREVIOUS_RELEASE_COLUMNS[table]
    placeholders = []
    for name in columns:
        if name in ("created_at", "updated_at", "assigned_at"):
            placeholders.append("now()")
        elif name == "actions":
            placeholders.append("CAST(:actions AS jsonb)")
        else:
            placeholders.append(f":{name}")
            row.setdefault(name, None)
    with engine.begin() as conn:
        conn.execute(
            text(
                f'INSERT INTO "{schema}".{table} ({", ".join(columns)}) '
                f"VALUES ({', '.join(placeholders)})"
            ),
            row,
        )
    return row["id"]


def _assign(engine, schema: str, user_id: str, role_id: str) -> str:
    return _insert_as_the_previous_release(
        engine, schema, ASSIGNMENTS, user_id=user_id, role_id=role_id
    )


def _grant(
    engine,
    schema: str,
    user_id: str,
    resource: str = "experiment",
    actions: str = '["update"]',
    expires_at: Optional[str] = None,
    is_active: bool = True,
) -> str:
    return _insert_as_the_previous_release(
        engine,
        schema,
        GRANTS,
        user_id=user_id,
        resource=resource,
        actions=actions,
        expires_at=expires_at,
        is_active=is_active,
    )


def _stored(engine, schema: str) -> dict[str, set[tuple]]:
    """Every row of both tables, the previous release's columns only."""
    stored = {}
    with engine.connect() as conn:
        for table, columns in PREVIOUS_RELEASE_COLUMNS.items():
            listed = ", ".join(f"CAST({name} AS text)" for name in columns)
            stored[table] = {
                tuple(row)
                for row in conn.execute(
                    text(f'SELECT {listed} FROM "{schema}".{table}')
                )
            }
    return stored


def _activated(engine, schema: str) -> dict[str, int]:
    """How many rows of each table have an activation time."""
    with engine.connect() as conn:
        return {
            table: conn.execute(
                text(
                    f'SELECT count(*) FROM "{schema}".{table} '
                    f"WHERE {COLUMN} IS NOT NULL"
                )
            ).scalar_one()
            for table in RBAC_TABLES
        }


#: A log record's prefix, as alembic.ini's formatter writes it
#: (``%(levelname)-5.5s [%(name)s] ``: ``WARNI [...]``, ``INFO  [...]``).
_RECORD = re.compile(r"^[A-Z]{4,5} {1,2}\[[\w.]{1,100}\] ")
_THIS_REVISION = re.compile(
    r"^(?P<level>[A-Z]{4,5}) {1,2}\[alembic\.runtime\.migration\] "
    r"(?P<first>modules_0004_rbac_activation: .*)$"
)


def _logged(stderr: str) -> Optional[tuple[str, list[str]]]:
    """``(level, lines)`` of this revision's record: its first line without
    the prefix, then every line up to the next record."""
    lines = stderr.splitlines()
    for index, line in enumerate(lines):
        match = _THIS_REVISION.match(line)
        if match:
            body = [match.group("first")]
            for rest in lines[index + 1 :]:
                if _RECORD.match(rest):
                    break
                body.append(rest)
            return match.group("level"), body
    return None


def _assignment_line(user_id: str, role_name: str) -> str:
    return f"assignment user_id={user_id} role_name={role_name}"


def _grant_line(user_id: str, expires_at: str = "none") -> str:
    return (
        f'grant user_id={user_id} resource=experiment actions=["update"] '
        f"expires_at={expires_at}"
    )


# ---------------------------------------------------------------------------
# 1. The previous release's database, upgraded
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_a_database_at_the_previous_release_upgrades_and_no_row_has_an_activation_time(
    test_db, scratch_schema, core_tree, full_tree, tmp_path
):
    """The upgrade every existing full deployment takes, by raw ``upgrade heads``.

    Two assignments and three grants, written as the previous release writes
    them; one grant has expired and one has ``is_active`` false.  A default on
    the column (``now()``) would give all five an activation time; the log
    counts the two assignments and the one grant that is neither expired nor
    inactive, and lists those three.
    """
    _at_previous_release(test_db, scratch_schema, core_tree, full_tree)
    first, second = sorted(_insert_user(test_db, scratch_schema) for _ in range(2))
    analyst = _insert_role(test_db, scratch_schema, "data-scientist")
    auditor = _insert_role(test_db, scratch_schema, "auditor")
    _assign(test_db, scratch_schema, second, analyst)
    _assign(test_db, scratch_schema, first, auditor)
    _grant(test_db, scratch_schema, first, expires_at=FUTURE)
    _grant(test_db, scratch_schema, first, expires_at=PAST)
    _grant(test_db, scratch_schema, second, is_active=False)
    before = _stored(test_db, scratch_schema)

    upgrade = tree_profiles.alembic(full_tree, scratch_schema, "upgrade", "heads")
    _ok(upgrade)

    assert _rows(test_db, scratch_schema) == FULL_HEADS
    assert _stored(test_db, scratch_schema) == before
    assert _activated(test_db, scratch_schema) == {ASSIGNMENTS: 0, GRANTS: 0}
    for table in RBAC_TABLES:
        assert _catalogue(test_db, scratch_schema, table) == NO_DEFAULT, table
    assert _logged(upgrade.stderr) == (
        "WARNI",
        [
            "modules_0004_rbac_activation: 2 custom role assignments and 1 "
            "direct permission grant have no activation time (activated_at is "
            "empty; expired grants and grants with is_active false are not "
            "counted or listed). See docs/self-hosting/migrations.md, "
            '"modules_0004_rbac_activation":',
            _assignment_line(first, "auditor"),
            _assignment_line(second, "data-scientist"),
            _grant_line(first, FUTURE),
        ],
    ), upgrade.stderr[-3000:]
    autogenerated = tree_profiles.autogenerate(
        full_tree, scratch_schema, tmp_path, head="modules@head"
    )
    assert autogenerated.body is not None, autogenerated.describe()
    assert autogenerated.operations == [], autogenerated.describe()


# ---------------------------------------------------------------------------
# 2. The log's grammar
# ---------------------------------------------------------------------------
def _one_grant(engine, schema: str) -> list[str]:
    user = _insert_user(engine, schema)
    _grant(engine, schema, user)
    return [_grant_line(user)]


def _one_assignment(engine, schema: str) -> list[str]:
    user = _insert_user(engine, schema)
    _assign(engine, schema, user, _insert_role(engine, schema, "data-scientist"))
    return [_assignment_line(user, "data-scientist")]


def _more_than_listed(engine, schema: str) -> list[str]:
    """150 assignments and 60 grants: every assignment and the first 50 grants
    are listed, and the last 10 counted."""
    user = _insert_user(engine, schema)
    role = _insert_role(engine, schema, "data-scientist")
    with engine.begin() as conn:
        conn.execute(
            text(
                f'INSERT INTO "{schema}".{ASSIGNMENTS} (id, user_id, role_id, '
                "assigned_at, created_at, updated_at) SELECT gen_random_uuid(), "
                ":user, :role, now(), now(), now() FROM generate_series(1, 150)"
            ),
            {"user": user, "role": role},
        )
        conn.execute(
            text(
                f'INSERT INTO "{schema}".{GRANTS} (id, user_id, resource, actions, '
                "is_active, created_at, updated_at) SELECT gen_random_uuid(), "
                """:user, 'experiment', '["update"]', true, now(), now() """
                "FROM generate_series(1, 60)"
            ),
            {"user": user},
        )
    return (
        [_assignment_line(user, "data-scientist")] * 150
        + [_grant_line(user)] * (LISTED - 150)
        + ["… and 10 more"]
    )


def _nothing(engine, schema: str) -> list[str]:
    """An expired grant and an inactive one: neither counts."""
    user = _insert_user(engine, schema)
    _grant(engine, schema, user, expires_at=PAST)
    _grant(engine, schema, user, is_active=False)
    return []


@pytest.mark.regression
@pytest.mark.parametrize(
    ("plant", "expected"),
    [
        pytest.param(
            _one_grant,
            ("WARNI", "1 direct permission grant has" + NO_ACTIVATION_TIME),
            id="one-grant",
        ),
        pytest.param(
            _one_assignment,
            ("WARNI", "1 custom role assignment has" + NO_ACTIVATION_TIME),
            id="one-assignment",
        ),
        pytest.param(
            _more_than_listed,
            (
                "WARNI",
                "150 custom role assignments and 60 direct permission grants have"
                + NO_ACTIVATION_TIME,
            ),
            id="more-than-listed",
        ),
        pytest.param(_nothing, ("INFO", None), id="none"),
    ],
)
def test_the_upgrade_log_counts_and_lists_the_rows_without_an_activation_time(
    test_db, scratch_schema, core_tree, full_tree, plant, expected
):
    _at_previous_release(test_db, scratch_schema, core_tree, full_tree)
    listed = plant(test_db, scratch_schema)

    upgrade = tree_profiles.alembic(full_tree, scratch_schema, "upgrade", "heads")
    _ok(upgrade)

    level, counted = expected
    if counted is None:
        assert _logged(upgrade.stderr) == (level, [NONE_LOGGED]), upgrade.stderr[-3000:]
    else:
        first_line = f"modules_0004_rbac_activation: {counted}"
        assert _logged(upgrade.stderr) == (level, [first_line, *listed]), (
            upgrade.stderr[-3000:]
        )


# ---------------------------------------------------------------------------
# 3. The models' shape, defaults included
# ---------------------------------------------------------------------------
def _shape(engine, schema: str, table: str) -> dict:
    """*table* as the catalogue describes it -- every column with its type,
    NOT NULL, whether it has a default and the default's expression; every
    constraint's and index's definition -- with the schema name abstracted."""
    relation = f'"{schema}"."{table}"'
    with engine.connect() as conn:
        columns = conn.execute(
            text(
                "SELECT a.attname, format_type(a.atttypid, a.atttypmod), "
                "a.attnotnull, a.atthasdef, pg_get_expr(d.adbin, d.adrelid) "
                "FROM pg_attribute a LEFT JOIN pg_attrdef d "
                "ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
                "WHERE a.attrelid = CAST(:rel AS regclass) "
                "AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attname"
            ),
            {"rel": relation},
        ).all()
        constraints = conn.execute(
            text(
                "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = CAST(:rel AS regclass) ORDER BY conname"
            ),
            {"rel": relation},
        ).all()
        indexes = conn.execute(
            text(
                "SELECT indexname, indexdef FROM pg_indexes "
                "WHERE schemaname = :schema AND tablename = :table ORDER BY indexname"
            ),
            {"schema": schema, "table": table},
        ).all()

    def norm(value):
        return value.replace(schema, "<schema>") if isinstance(value, str) else value

    return {
        "columns": [tuple(norm(v) for v in row) for row in columns],
        "constraints": [tuple(norm(v) for v in row) for row in constraints],
        "indexes": [tuple(norm(v) for v in row) for row in indexes],
    }


@pytest.mark.regression
def test_both_tables_have_the_models_shape_defaults_included(
    test_db, scratch_schema, core_tree, full_tree
):
    """The migrated tables against what a fresh full bootstrap -- the models --
    builds, including each column's default, which nothing else compares."""
    _at_head(MIGRATED, test_db, scratch_schema, core_tree, full_tree)
    reference = f"{scratch_schema}_ref"
    _ok(tree_profiles.bootstrap_schema(full_tree, reference))

    for table in RBAC_TABLES:
        assert _shape(test_db, scratch_schema, table) == _shape(
            test_db, reference, table
        ), table
        assert _catalogue(test_db, reference, table) == NO_DEFAULT, table


# ---------------------------------------------------------------------------
# 4. The previous release writes assignments and grants against this schema
# ---------------------------------------------------------------------------
@pytest.mark.regression
@ORIGINS
def test_the_previous_release_still_writes_and_its_rows_have_no_activation_time(
    test_db, scratch_schema, core_tree, full_tree, origin
):
    """During a deploy, and after an image-only rollback, the previous release
    keeps assigning roles and making grants without naming the column.  The
    rows must land, with no activation time: a default would give them one."""
    _at_head(origin, test_db, scratch_schema, core_tree, full_tree)
    user = _insert_user(test_db, scratch_schema)

    _assign(test_db, scratch_schema, user, _insert_role(test_db, scratch_schema, "r1"))
    _grant(test_db, scratch_schema, user)

    assert {
        table: len(rows) for table, rows in _stored(test_db, scratch_schema).items()
    } == {
        ASSIGNMENTS: 1,
        GRANTS: 1,
    }
    assert _activated(test_db, scratch_schema) == {ASSIGNMENTS: 0, GRANTS: 0}
    for table in RBAC_TABLES:
        assert _catalogue(test_db, scratch_schema, table) == NO_DEFAULT, table


# ---------------------------------------------------------------------------
# 5. Downgrade to the previous modules revision, and back
# ---------------------------------------------------------------------------
def _plant_rows(engine, schema: str) -> None:
    user = _insert_user(engine, schema)
    _assign(engine, schema, user, _insert_role(engine, schema, "r1"))
    _assign(engine, schema, user, _insert_role(engine, schema, "r2"))
    _grant(engine, schema, user, expires_at=FUTURE)
    _grant(engine, schema, user, is_active=False)


@pytest.mark.regression
@ORIGINS
def test_downgrade_drops_both_columns_and_keeps_every_row(
    test_db, scratch_schema, core_tree, full_tree, origin
):
    """The rollback of a release that carries this revision and nothing that
    writes the column: ``downgrade modules_0003_lower_is_better`` loses no
    row and no other column, on a bootstrapped database too."""
    _at_head(origin, test_db, scratch_schema, core_tree, full_tree)
    _plant_rows(test_db, scratch_schema)
    before = _stored(test_db, scratch_schema)
    assert {table: len(rows) for table, rows in before.items()} == {
        ASSIGNMENTS: 2,
        GRANTS: 2,
    }
    assert _activated(test_db, scratch_schema) == {ASSIGNMENTS: 0, GRANTS: 0}

    _ok(
        tree_profiles.alembic(
            full_tree, scratch_schema, "downgrade", PREVIOUS_MODULES_HEAD
        )
    )

    assert _rows(test_db, scratch_schema) == PREVIOUS_FULL_HEADS
    for table in RBAC_TABLES:
        assert COLUMN not in _columns(test_db, scratch_schema, table), table
    assert _stored(test_db, scratch_schema) == before


@pytest.mark.regression
@ORIGINS
def test_an_activation_time_is_gone_after_a_downgrade_and_an_upgrade(
    test_db, scratch_schema, core_tree, full_tree, origin
):
    """An activation time stored before the downgrade (planted by SQL, as a
    later writer would store one) goes with the column; the upgrade afterwards
    adds the columns back empty, and keeps the rows."""
    _at_head(origin, test_db, scratch_schema, core_tree, full_tree)
    _plant_rows(test_db, scratch_schema)
    with test_db.begin() as conn:
        for table in RBAC_TABLES:
            conn.execute(
                text(f'UPDATE "{scratch_schema}".{table} SET {COLUMN} = now()')
            )
    assert _activated(test_db, scratch_schema) == {ASSIGNMENTS: 2, GRANTS: 2}
    before = _stored(test_db, scratch_schema)

    _ok(
        tree_profiles.alembic(
            full_tree, scratch_schema, "downgrade", PREVIOUS_MODULES_HEAD
        )
    )
    assert _rows(test_db, scratch_schema) == PREVIOUS_FULL_HEADS
    for table in RBAC_TABLES:
        assert COLUMN not in _columns(test_db, scratch_schema, table), table

    _ok(tree_profiles.alembic(full_tree, scratch_schema, "upgrade", "heads"))

    assert _rows(test_db, scratch_schema) == FULL_HEADS
    assert _stored(test_db, scratch_schema) == before
    assert _activated(test_db, scratch_schema) == {ASSIGNMENTS: 0, GRANTS: 0}
    for table in RBAC_TABLES:
        assert _catalogue(test_db, scratch_schema, table) == NO_DEFAULT, table


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
    ``modules_0003_lower_is_better`` is the way back, and after it that image
    starts with every row in place."""
    _at_head(MIGRATED, test_db, scratch_schema, core_tree, full_tree)
    _plant_rows(test_db, scratch_schema)
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
