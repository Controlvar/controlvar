"""``modules_0006_saml_requests`` across the databases it can meet (#123).

The revision adds the table ``sso_saml_requests`` and its index, each only
when absent, marks the table's primary key with a COMMENT when it creates the
table, and changes nothing else. Nothing reads or writes the table yet. Each
transition runs as the deployment runs it -- raw ``alembic upgrade heads`` or
``downgrade`` in a subprocess -- against both origins a full database can
have:

* **migrated**: a core bootstrap, then the full image's migrations, so the
  table comes from this revision;
* **bootstrapped**: a fresh full bootstrap, so the table comes from the
  model's ``create_all`` and this revision is only stamped.

What is pinned:

1. a database the previous full release left at ``modules_0005_redshift``
   (its own bootstrap: this tree without the revision, the model and the
   model's registration) upgrades: the ``alembic_version`` rows are this release's, the revision
   made the table (the COMMENT; ``upgrade heads`` that reaches the heads also
   creates a missing module table from the models, untagged), no other table
   appears or goes, and autogenerate is empty;
2. a core database opened by the full image (``upgrade heads``): the same;
3. the migrated table is what a fresh full bootstrap builds -- columns with
   their types and defaults, constraints and indexes, from the catalogue;
   there is no foreign key;
4. the upgrade, held before its commit, locks no table that existed before it
   except ``alembic_version`` (the capture in ``lock_capture.py``);
5. another session's uncommitted creation of the same table ends the upgrade
   with this revision's refusal, and nothing is applied;
6. ``downgrade modules_0005_redshift`` drops the table on both origins and
   changes no other table; an upgrade afterwards creates it again, empty;
7. the previous release refuses the upgraded database and names this
   revision; after ``downgrade modules_0005_redshift`` it starts, on both
   origins, and does not create the table again.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from backend.app.db import bootstrap
from backend.tests.integration.database import lock_capture, tree_profiles
from backend.tests.integration.database.tree_profiles import CORE, FULL

pytestmark = [pytest.mark.integration, pytest.mark.modules]

CORE_HEAD = tree_profiles.CORE_HEAD
#: The modules head of the previous full release, and this revision's parent.
PREVIOUS_MODULES_HEAD = "modules_0005_redshift"
#: This revision: the modules branch's head.
MODULES_HEAD = "modules_0006_saml_requests"
PREVIOUS_FULL_HEADS = {CORE_HEAD, PREVIOUS_MODULES_HEAD}
FULL_HEADS = {CORE_HEAD, MODULES_HEAD}

TABLE = "sso_saml_requests"
INDEX = "ix_sso_saml_requests_expires_at"
#: The COMMENT the revision leaves on the primary key of the table it created.
CREATED_BY = f"created by alembic revision {MODULES_HEAD}"

#: The table as PostgreSQL describes it after either path: each column's
#: (``format_type``, NOT NULL, ``pg_get_expr`` of its default), in order.
EXPECTED_COLUMNS = [
    ("id", "character varying(64)", True, None),
    ("config_id", "uuid", True, None),
    ("return_to", "character varying(512)", True, None),
    ("handoff_hash", "character(43)", True, None),
    ("created_at", "timestamp with time zone", True, "now()"),
    ("expires_at", "timestamp with time zone", True, None),
    ("used_at", "timestamp with time zone", False, None),
]
#: Its one constraint, the primary key: no foreign key, no check.
EXPECTED_CONSTRAINTS = {("sso_saml_requests_pkey", "p", "PRIMARY KEY (id)")}
EXPECTED_INDEXES = {
    "CREATE UNIQUE INDEX sso_saml_requests_pkey ON <schema>.sso_saml_requests "
    "USING btree (id)",
    f"CREATE INDEX {INDEX} ON <schema>.sso_saml_requests USING btree (expires_at)",
}

#: What the upgrade locks among the tables that exist before it runs: none
#: (``lock_capture.BOOKKEEPING`` adds alembic's own two modes).
DECLARED_LOCKS: dict = {}
#: Planted after the revision's own steps: a foreign key to ``sso_configs``,
#: the one table ``config_id`` names.
REFERENCES_SSO_CONFIGS = (
    "_op.create_foreign_key(\n"
    "    'fk_lock_capture_plant', 'sso_saml_requests', 'sso_configs',\n"
    "    ['config_id'], ['id'], source_schema=_schema, referent_schema=_schema,\n"
    ")"
)

#: Each online leg's own bound on any one statement of the upgrade: a wait the
#: revision did not bound ends here (57014), never in a hung test.
LEG_STATEMENT_TIMEOUT = "-c statement_timeout=5s"

MIGRATED = "migrated"
BOOTSTRAPPED = "bootstrapped"
ORIGINS = pytest.mark.parametrize("origin", [MIGRATED, BOOTSTRAPPED])

#: The revision file and the model file, as paths in a tree, built from parts:
#: a string literal naming a path or a module under the modules would be a
#: core -> module crossing for ``test_core_boundary.py``.
_MODULES_APP = ("modules", "backend", "app")
_REVISION_FILE = (*_MODULES_APP, "db", "migrations", "versions", f"{MODULES_HEAD}.py")
_MODEL = "sso_saml_request"
_MODEL_FILE = (*_MODULES_APP, "models", f"{_MODEL}.py")
_REGISTER_FILE = (*_MODULES_APP, "register.py")
_MODEL_REGISTRATION = '    "' + ".".join((*_MODULES_APP, "models", _MODEL)) + '",\n'


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def scratch_schema(test_db):
    schema = f"saml0006_{uuid.uuid4().hex[:8]}"
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


def _tables(engine, schema: str) -> set[str]:
    return set(inspect(engine).get_table_names(schema=schema))


def _report(label: str, engine, schema: str) -> None:
    """The ``alembic_version`` rows after a step, printed for the record."""
    print(f"[{label}] alembic_version: {sorted(_rows(engine, schema))}")


def _stamp(engine, schema: str, rows: set[str]) -> None:
    with engine.begin() as conn:
        conn.execute(text(f'DELETE FROM "{schema}".alembic_version'))
        for revision in sorted(rows):
            conn.execute(
                text(f'INSERT INTO "{schema}".alembic_version VALUES (:rev)'),
                {"rev": revision},
            )


def _columns(engine, schema: str) -> list[tuple]:
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
            {"s": schema, "t": TABLE},
        ).all()
    return [tuple(row) for row in rows]


def _constraints(engine, schema: str) -> set[tuple]:
    """Every constraint on the table, a foreign key included."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT c.conname, c.contype, pg_get_constraintdef(c.oid) "
                "FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid "
                "JOIN pg_namespace n ON n.oid = t.relnamespace "
                "WHERE n.nspname = :s AND t.relname = :t"
            ),
            {"s": schema, "t": TABLE},
        ).all()
    return {
        (name, kind, definition.replace(schema, "<schema>"))
        for name, kind, definition in rows
    }


def _indexes(engine, schema: str) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT indexdef FROM pg_indexes WHERE schemaname = :s AND tablename = :t"
            ),
            {"s": schema, "t": TABLE},
        ).scalars()
        return {row.replace(f"{schema}.", "<schema>.") for row in rows}


def _definition(engine, schema: str) -> tuple:
    return (
        _columns(engine, schema),
        _constraints(engine, schema),
        _indexes(engine, schema),
    )


def _pk_comment(engine, schema: str):
    """The COMMENT on the table's primary key, or None."""
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT obj_description(c.oid, 'pg_constraint') FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid "
                "JOIN pg_namespace n ON n.oid = t.relnamespace "
                "WHERE n.nspname = :s AND t.relname = :t AND c.contype = 'p'"
            ),
            {"s": schema, "t": TABLE},
        ).scalar()


def _count(engine, schema: str) -> int:
    with engine.connect() as conn:
        return conn.execute(text(f'SELECT count(*) FROM "{schema}".{TABLE}')).scalar()


def _insert_request(engine, schema: str) -> None:
    """One sign-in in progress, written as the release that writes them will."""
    with engine.begin() as conn:
        conn.execute(
            text(
                f'INSERT INTO "{schema}".{TABLE} (id, config_id, return_to, '
                "handoff_hash, expires_at) VALUES (:id, :config, "
                "'https://dashboard.example.com', :hash, now() + interval '600 seconds')"
            ),
            {
                "id": f"ONELOGIN_{uuid.uuid4().hex}",
                "config": str(uuid.uuid4()),
                "hash": "h" * 43,
            },
        )


def _previous_full_release(tmp_path) -> Path:
    """The previous full release: this tree without this revision, the model
    and the model's registration, so its start-up has no model to create the
    table from."""
    tree = tree_profiles.full_tree(tmp_path / "previous-full-release")
    tree.joinpath(*_REVISION_FILE).unlink()
    tree.joinpath(*_MODEL_FILE).unlink()
    register = tree.joinpath(*_REGISTER_FILE)
    source = register.read_text(encoding="utf-8")
    assert source.count(_MODEL_REGISTRATION) == 1, "the registration moved"
    register.write_text(source.replace(_MODEL_REGISTRATION, ""), encoding="utf-8")
    return tree


def _at_previous_release(engine, schema: str, tmp_path) -> None:
    """What the previous full release left: its own bootstrap, at
    ``modules_0005_redshift``, every other module table in place."""
    _ok(tree_profiles.bootstrap_schema(_previous_full_release(tmp_path), schema))
    assert _rows(engine, schema) == PREVIOUS_FULL_HEADS
    assert TABLE not in _tables(engine, schema)


def _at_head(origin: str, engine, schema: str, core_tree, full_tree) -> None:
    """A full database at this tree's heads, built the *origin* way."""
    if origin == MIGRATED:
        _ok(tree_profiles.bootstrap_schema(core_tree, schema))
        _ok(tree_profiles.alembic(full_tree, schema, "upgrade", "heads"))
    else:
        _ok(tree_profiles.bootstrap_schema(full_tree, schema))
    assert _rows(engine, schema) == FULL_HEADS
    assert TABLE in _tables(engine, schema)


def _autogenerate_is_empty(full_tree, schema: str, tmp_path) -> None:
    autogenerated = tree_profiles.autogenerate(
        full_tree, schema, tmp_path, head="modules@head"
    )
    assert autogenerated.body is not None, autogenerated.describe()
    assert autogenerated.operations == [], autogenerated.describe()


def _load_revision(schema: str):
    """This revision's module, its schema pointed at *schema*."""
    path = tree_profiles.REPO_ROOT.joinpath(*_REVISION_FILE)
    spec = importlib.util.spec_from_file_location("saml_requests_revision", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._SCHEMA = schema
    return module


# ---------------------------------------------------------------------------
# 1, 2. Upgrades: the previous full release, and a core database
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_a_database_at_the_previous_release_gains_the_table(
    test_db, scratch_schema, full_tree, tmp_path
):
    """The upgrade every existing full deployment takes, by raw ``upgrade heads``."""
    _at_previous_release(test_db, scratch_schema, tmp_path)
    _report("previous release", test_db, scratch_schema)
    before = _tables(test_db, scratch_schema)

    _ok(tree_profiles.alembic(full_tree, scratch_schema, "upgrade", "heads"))
    _report("after upgrade heads", test_db, scratch_schema)

    assert _rows(test_db, scratch_schema) == FULL_HEADS
    assert _tables(test_db, scratch_schema) == before | {TABLE}
    assert _pk_comment(test_db, scratch_schema) == CREATED_BY
    assert _count(test_db, scratch_schema) == 0
    _autogenerate_is_empty(full_tree, scratch_schema, tmp_path)


@pytest.mark.regression
def test_a_core_database_opened_by_the_full_image_gains_the_table_from_the_revision(
    test_db, scratch_schema, core_tree, full_tree, tmp_path
):
    """A core database has no modules row; the full image's ``upgrade heads``
    runs the whole branch. The COMMENT says this revision made the table, not
    the module-table reconcile that runs once the heads are reached."""
    _ok(tree_profiles.bootstrap_schema(core_tree, scratch_schema))
    _report("core bootstrap", test_db, scratch_schema)
    assert _rows(test_db, scratch_schema) == {CORE_HEAD}

    _ok(tree_profiles.alembic(full_tree, scratch_schema, "upgrade", "heads"))
    _report("after full upgrade heads", test_db, scratch_schema)

    assert _rows(test_db, scratch_schema) == FULL_HEADS
    assert _pk_comment(test_db, scratch_schema) == CREATED_BY
    _autogenerate_is_empty(full_tree, scratch_schema, tmp_path)


# ---------------------------------------------------------------------------
# 3. The model's table
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_the_migrated_table_is_what_a_fresh_full_bootstrap_builds(
    test_db, scratch_schema, core_tree, full_tree
):
    """Autogenerate compares no server default and no constraint text, so a
    model and a migration that disagree on one would pass everything else."""
    _at_head(MIGRATED, test_db, scratch_schema, core_tree, full_tree)
    reference = f"{scratch_schema}_ref"
    _ok(tree_profiles.bootstrap_schema(full_tree, reference))

    migrated = _definition(test_db, scratch_schema)
    assert migrated == _definition(test_db, reference)
    assert migrated == (EXPECTED_COLUMNS, EXPECTED_CONSTRAINTS, EXPECTED_INDEXES)
    # The bootstrap made the reference table, not this revision.
    assert _pk_comment(test_db, reference) is None


# ---------------------------------------------------------------------------
# 4. The locks it takes
# ---------------------------------------------------------------------------
def _held_at_parent(test_db, schema: str, tmp_path):
    """A pausing copy of the full tree, and *schema* as the previous release
    left it: bootstrapped, the table dropped, the parent's rows."""
    tree = lock_capture.pausing_tree(FULL, tmp_path)
    _ok(tree_profiles.bootstrap_schema(tree, schema))
    with test_db.begin() as conn:
        conn.execute(text(f'DROP TABLE "{schema}".{TABLE}'))
    _stamp(test_db, schema, PREVIOUS_FULL_HEADS)
    return tree


def _captured(test_db, tree, schema: str) -> lock_capture.Capture:
    """Run the revision alone, held before its commit. The target is named,
    never ``heads``: a later revision must not join the run."""
    with lock_capture.held_before_commit(tree, schema, "upgrade", MODULES_HEAD) as held:
        found = lock_capture.capture(test_db, held, schema)
    print(f"captured: {found}")
    assert held.process.returncode == 0, held.output()[-3000:]
    return found


@pytest.mark.regression
def test_the_upgrade_locks_no_table_that_existed_before_it(
    test_db, scratch_schema, tmp_path
):
    tree = _held_at_parent(test_db, scratch_schema, tmp_path)

    found = _captured(test_db, tree, scratch_schema)

    lock_capture.assert_locks(found, DECLARED_LOCKS)
    assert found.locks == {"alembic_version": {"AccessShareLock", "RowExclusiveLock"}}
    # The table the run created, its index and its key: invisible to every
    # other session until the commit, so locking them blocks nobody.
    assert found.unseen > 0
    assert _rows(test_db, scratch_schema) == FULL_HEADS
    assert _pk_comment(test_db, scratch_schema) == CREATED_BY


@pytest.mark.regression
def test_a_foreign_key_to_sso_configs_would_be_caught(
    test_db, scratch_schema, tmp_path
):
    """The capture's own plant: the same revision with a foreign key to
    ``sso_configs`` locks that table, and the comparison fails naming it."""
    tree = _held_at_parent(test_db, scratch_schema, tmp_path)
    lock_capture.plant_after_upgrade(tree, MODULES_HEAD, REFERENCES_SSO_CONFIGS)

    found = _captured(test_db, tree, scratch_schema)

    assert "sso_configs" in found.locks
    with pytest.raises(AssertionError, match=r"sso_configs: \{.*\}\} != declared"):
        lock_capture.assert_locks(found, DECLARED_LOCKS)


# ---------------------------------------------------------------------------
# 5. The one wait, bounded
# ---------------------------------------------------------------------------
@pytest.mark.regression
def test_a_concurrent_creation_of_the_table_ends_the_upgrade_with_the_refusal(
    test_db, scratch_schema, full_tree, tmp_path
):
    """Another session's uncommitted ``CREATE TABLE`` of the same name makes
    this revision's ``CREATE TABLE`` wait. The ``lock_timeout`` ends that wait
    (SQLSTATE 55P03) with this revision's refusal, and nothing is applied.
    Without the ``SET`` only the leg's own ``statement_timeout`` ends it
    (57014), and this fails."""
    _at_previous_release(test_db, scratch_schema, tmp_path)
    expected = _load_revision(scratch_schema)._lock_refusal()
    rival = test_db.connect()
    transaction = rival.begin()
    try:
        rival.execute(text(f'CREATE TABLE "{scratch_schema}".{TABLE} (rival integer)'))
        upgrade = tree_profiles.alembic(
            full_tree,
            scratch_schema,
            "upgrade",
            MODULES_HEAD,
            extra_env={"PGOPTIONS": LEG_STATEMENT_TIMEOUT},
        )
    finally:
        transaction.rollback()
        rival.close()
    print(f"upgrade stderr tail:\n{upgrade.stderr[-1500:]}")
    assert upgrade.returncode != 0
    assert expected in upgrade.stderr, upgrade.stderr[-3000:]
    assert "statement timeout" not in upgrade.stderr
    assert _rows(test_db, scratch_schema) == PREVIOUS_FULL_HEADS
    assert TABLE not in _tables(test_db, scratch_schema)


# ---------------------------------------------------------------------------
# 6. Downgrade to the previous modules revision, and back
# ---------------------------------------------------------------------------
@pytest.mark.regression
@ORIGINS
def test_downgrade_drops_the_table_and_nothing_else(
    test_db, scratch_schema, core_tree, full_tree, origin
):
    _at_head(origin, test_db, scratch_schema, core_tree, full_tree)
    _insert_request(test_db, scratch_schema)
    tables = _tables(test_db, scratch_schema)

    _ok(
        tree_profiles.alembic(
            full_tree, scratch_schema, "downgrade", PREVIOUS_MODULES_HEAD
        )
    )
    _report(f"{origin}: after downgrade", test_db, scratch_schema)

    assert _rows(test_db, scratch_schema) == PREVIOUS_FULL_HEADS
    assert _tables(test_db, scratch_schema) == tables - {TABLE}

    _ok(tree_profiles.alembic(full_tree, scratch_schema, "upgrade", "heads"))
    _report(f"{origin}: after upgrade heads again", test_db, scratch_schema)

    assert _rows(test_db, scratch_schema) == FULL_HEADS
    assert _tables(test_db, scratch_schema) == tables
    assert _count(test_db, scratch_schema) == 0
    assert _pk_comment(test_db, scratch_schema) == CREATED_BY


# ---------------------------------------------------------------------------
# 7. The previous release, put back
# ---------------------------------------------------------------------------
@pytest.mark.regression
@ORIGINS
def test_the_previous_release_refuses_the_database_until_it_is_downgraded(
    test_db, scratch_schema, core_tree, full_tree, tmp_path, origin
):
    """The previous image against the upgraded database refuses by both
    documented paths and names this revision; ``downgrade
    modules_0005_redshift``, the documented way back, is enough for that image
    to start, and it does not create the table again."""
    _at_head(origin, test_db, scratch_schema, core_tree, full_tree)
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
    _report(f"{origin}: after the documented downgrade", test_db, scratch_schema)
    _ok(tree_profiles.bootstrap_schema(previous, scratch_schema))
    _report(f"{origin}: after the previous release started", test_db, scratch_schema)

    assert _rows(test_db, scratch_schema) == PREVIOUS_FULL_HEADS
    assert TABLE not in _tables(test_db, scratch_schema)
