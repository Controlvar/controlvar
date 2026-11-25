"""``t2011_flag_environments``, read without a database (#2011).

* Its id fits ``alembic_version.version_num`` (32 characters), its
  ``down_revision`` is the literal ``t449_identity_aliases`` (0.48.0's core
  head), and the heads are exactly this revision and, in a full tree, the
  modules head.
* The migration creates the model's table (columns, types, nullability,
  server defaults, the named checks, the unique constraint, the cascading
  foreign key and the ``created_at`` index) and adds ``api_keys.environment``
  with the model's type, default and check.
* The two bounds: ``_FLAGS_LOCK_TIMEOUT`` is ``500ms`` and
  ``_KEYS_LOCK_TIMEOUT`` ``1500ms``, each written once and never a zero form,
  and their sum is the budget map's declared 2 s.
* The order: the side table's block first, then the ``api_keys`` block, each
  between its ``SET LOCAL lock_timeout`` and the reset; the downgrade takes
  ``api_keys`` first (the keys deactivated, then the check and the column by
  name), then the table, in the same two blocks. The refusal for each block
  names every table that block locks.
* In every block of both directions a lock timeout (55P03) or a deadlock
  (40P01) becomes that direction's refusal, worded so that it is true for
  both; any other error is raised as it was.
* Offline mode is refused in both directions.
* Each step runs only when its object is absent (upgrade) or present
  (downgrade).
* The database tests of this revision are all collected and none is skipped.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from backend.app.models.api_key import (
    API_KEY_ENVIRONMENT_CHECK,
    API_KEY_ENVIRONMENT_CHECK_SQL,
    APIKey,
)
from backend.app.models.feature_flag import FlagEnvironmentSetting
from backend.tests.unit.db.test_upgrade_lock_budget import _script, release
from backend.tests.unit.docs.test_documented_downgrade_targets import drops_a_column

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REVISION = "t2011_flag_environments"
REPO_ROOT = Path(__file__).resolve().parents[4]
#: What a test schema is called while these tests import the models.
SCHEMA = FlagEnvironmentSetting.__table__.schema


def _path() -> Path:
    return Path(_script().get_revision(REVISION).path)


def _module():
    spec = importlib.util.spec_from_file_location("flag_environments_revision", _path())
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # The models are built for the test schema; so is the revision here.
    module._SCHEMA = SCHEMA
    module._SETTINGS_CREATED_INDEX = f"ix_{SCHEMA}_flag_environment_settings_created_at"
    return module


def test_it_extends_0_48_0s_core_head_and_fits_the_version_table():
    revision = _script().get_revision(REVISION)
    # Kept literal on purpose. A re-point changes this line, every CORE_HEAD
    # pin, the NAMED_DOWNGRADES lists in test_db_migrate_target_graph.py, the
    # sentences that give this revision's down_revision (the migrations page,
    # the rollback runbook, db-migrate.yml's help text) and the migration's own
    # docstring, together.
    assert revision.down_revision == "t449_identity_aliases"
    assert revision.revision == REVISION
    assert len(revision.revision) <= 32


def test_the_heads_are_this_revision_and_in_a_full_tree_the_modules_head():
    expected = {REVISION}
    if (REPO_ROOT / "modules").is_dir():
        expected.add("modules_0005_redshift")
    assert set(_script().get_heads()) == expected


class _Recorder:
    """Stands in for ``alembic.op``: records each call, in order."""

    def __init__(self, as_sql: bool = False, fail=None) -> None:
        self.calls: list = []
        self._as_sql = as_sql
        #: {call name: exception} raised instead of recording that call.
        self._fail = fail or {}

    def _record(self, name, *args, **kwargs):
        if name in self._fail:
            raise self._fail[name]
        self.calls.append((name, args, kwargs))

    def get_context(self):
        return SimpleNamespace(as_sql=self._as_sql)

    def get_bind(self):
        return SimpleNamespace(
            execute=lambda clause, *a: self._record(
                "execute", " ".join(str(clause).split())
            ),
            dialect=postgresql.dialect(),
        )

    def create_table(self, name, *items, **kwargs):
        self._record("create_table", name, *items, **kwargs)

    def create_index(self, name, table, columns, **kwargs):
        self._record("create_index", name, table, columns, **kwargs)

    def add_column(self, table, column, **kwargs):
        self._record("add_column", table, column, **kwargs)

    def create_check_constraint(self, name, table, condition, **kwargs):
        self._record("create_check_constraint", name, table, condition, **kwargs)

    def drop_constraint(self, name, table, **kwargs):
        self._record("drop_constraint", name, table, **kwargs)

    def drop_column(self, table, column, **kwargs):
        self._record("drop_column", table, column, **kwargs)

    def drop_table(self, name, **kwargs):
        self._record("drop_table", name, **kwargs)

    def names(self) -> list:
        return [name for name, _, _ in self.calls]

    def statements(self) -> list:
        return [args[0] for name, args, _ in self.calls if name == "execute"]

    def one(self, name):
        (found,) = [(args, kwargs) for n, args, kwargs in self.calls if n == name]
        return found


_CHECKS = ("_relation_exists", "_column_exists", "_constraint_exists")


def _run(monkeypatch, recorder: _Recorder, direction: str, present: bool):
    """Run *direction* with every existence check answering *present*."""
    module = _module()
    monkeypatch.setattr(module, "op", recorder)
    for check in _CHECKS:
        monkeypatch.setattr(module, check, lambda *a: present)
    getattr(module, direction)()
    return module


def _upgraded(monkeypatch, recorder: _Recorder):
    return _run(monkeypatch, recorder, "upgrade", present=False)


def _downgraded(monkeypatch, recorder: _Recorder):
    return _run(monkeypatch, recorder, "downgrade", present=True)


def _default_sql(column):
    default = column.server_default
    return None if default is None else str(getattr(default, "arg", default))


def _checks(table) -> set:
    return {
        (c.name, str(c.sqltext))
        for c in table.constraints
        if isinstance(c, sa.CheckConstraint)
    }


# ---------------------------------------------------------------------------
# The model's table and column
# ---------------------------------------------------------------------------
def test_the_migration_creates_the_models_table(monkeypatch):
    recorder = _Recorder()
    _upgraded(monkeypatch, recorder)
    (name, *items), kwargs = recorder.one("create_table")
    model = FlagEnvironmentSetting.__table__
    assert name == "flag_environment_settings" == model.name
    made = sa.Table(name, sa.MetaData(), *items, schema=kwargs["schema"])
    assert made.schema == model.schema
    assert list(made.columns.keys()) == list(model.columns.keys())
    assert list(model.columns.keys()) == [
        "feature_flag_id",
        "environment",
        "is_active",
        "targeting_rules",
        "rollout_percentage",
        "id",
        "created_at",
        "updated_at",
    ]
    for column in model.columns:
        created = made.columns[column.name]
        assert type(created.type).__name__ == type(column.type).__name__, column.name
        assert getattr(created.type, "length", None) == getattr(
            column.type, "length", None
        ), column.name
        assert created.nullable == column.nullable, column.name
        assert _default_sql(created) == _default_sql(column), column.name
    assert list(made.primary_key.columns.keys()) == ["id"]
    expected_checks = {
        ("check_flag_environment_name", "environment IN ('staging', 'development')"),
        (
            "check_flag_environment_rollout",
            "rollout_percentage >= 0 AND rollout_percentage <= 100",
        ),
    }
    assert _checks(made) == _checks(model) == expected_checks

    def _uniques(table):
        return {
            (c.name, tuple(col.name for col in c.columns))
            for c in table.constraints
            if isinstance(c, sa.UniqueConstraint)
        }

    expected_uniques = {("uq_flag_environment", ("feature_flag_id", "environment"))}
    assert _uniques(made) == _uniques(model) == expected_uniques
    (made_fk,) = made.foreign_keys
    (model_fk,) = model.foreign_keys
    assert made_fk.target_fullname == model_fk.target_fullname
    assert model_fk.target_fullname == f"{SCHEMA}.feature_flags.id"
    assert made_fk.ondelete == model_fk.ondelete == "CASCADE"

    (index_name, table, columns), index_kwargs = recorder.one("create_index")
    (model_index,) = model.indexes
    assert index_name == model_index.name
    assert table == "flag_environment_settings"
    assert columns == [c.name for c in model_index.columns] == ["created_at"]
    assert index_kwargs["schema"] == model.schema
    assert not index_kwargs.get("unique") and not model_index.unique


def test_the_migration_adds_the_models_key_environment(monkeypatch):
    recorder = _Recorder()
    _upgraded(monkeypatch, recorder)
    (table, column), kwargs = recorder.one("add_column")
    model = APIKey.__table__.columns["environment"]
    assert table == "api_keys" == APIKey.__tablename__
    assert kwargs["schema"] == APIKey.__table__.schema
    assert column.name == "environment"
    assert type(column.type).__name__ == type(model.type).__name__ == "String"
    assert column.type.length == model.type.length == 32
    assert column.nullable is False and model.nullable is False
    assert _default_sql(column) == _default_sql(model) == "'production'"
    # The literal in the column and the constant the downgrade's UPDATE reads.
    assert _default_sql(column) == f"'{_module()._KEY_DEFAULT}'"

    (name, table, condition), kwargs = recorder.one("create_check_constraint")
    assert (name, table) == (API_KEY_ENVIRONMENT_CHECK, "api_keys")
    assert condition == API_KEY_ENVIRONMENT_CHECK_SQL
    assert kwargs["schema"] == APIKey.__table__.schema


# ---------------------------------------------------------------------------
# The two bounds, and the order of the blocks
# ---------------------------------------------------------------------------
def _seconds(bound: str) -> float:
    if bound.endswith("ms"):
        return int(bound[:-2]) / 1000
    assert bound.endswith("s"), bound
    return float(bound[:-1])


def test_the_two_bounds_are_half_a_second_and_a_second_and_a_half_and_sum_to_the_declared_two():
    module = _module()
    assert module._FLAGS_LOCK_TIMEOUT == "500ms"
    assert module._KEYS_LOCK_TIMEOUT == "1500ms"
    total = _seconds(module._FLAGS_LOCK_TIMEOUT) + _seconds(module._KEYS_LOCK_TIMEOUT)
    assert total == release("0.49.0").declared[REVISION] == 2


@pytest.mark.parametrize(
    "constant, literal",
    [("_FLAGS_LOCK_TIMEOUT", "500ms"), ("_KEYS_LOCK_TIMEOUT", "1500ms")],
)
def test_each_bound_lives_in_one_constant(constant, literal):
    """Nobody retypes a bound: each literal is written once, as its
    constant's value, which is no zero form, and the ``SET`` is built from
    whichever constant a block passes."""
    source = _path().read_text(encoding="utf-8")
    assert source.count(f'"{literal}"') == 1
    assert source.count(f"'{literal}'") == 0
    assert "lock_timeout = '{bound}'" in source
    tree = ast.parse(source)
    (assigned,) = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == constant for t in node.targets)
    ]
    assert assigned.value.value == literal
    assert assigned.value.value not in {"0", "0ms", "0s", ""}


def _set(bound: str) -> str:
    return f"SET LOCAL lock_timeout = '{bound}'"


RESET = "SET LOCAL lock_timeout TO DEFAULT"


def test_the_upgrade_runs_the_side_table_block_then_the_api_keys_block(monkeypatch):
    recorder = _Recorder()
    module = _upgraded(monkeypatch, recorder)
    assert recorder.names() == [
        "execute",
        "create_table",
        "create_index",
        "execute",
        "execute",
        "add_column",
        "create_check_constraint",
        "execute",
    ]
    assert recorder.statements() == [
        _set(module._FLAGS_LOCK_TIMEOUT),
        RESET,
        _set(module._KEYS_LOCK_TIMEOUT),
        RESET,
    ]


def test_the_downgrade_runs_the_api_keys_block_then_the_side_table_block(monkeypatch):
    """The keys deactivated, then the check and the column by name, all in
    the ``api_keys`` block; then the table in the other."""
    recorder = _Recorder()
    module = _downgraded(monkeypatch, recorder)
    assert recorder.names() == [
        "execute",
        "execute",
        "drop_constraint",
        "drop_column",
        "execute",
        "execute",
        "drop_table",
        "execute",
    ]
    assert recorder.statements() == [
        _set(module._KEYS_LOCK_TIMEOUT),
        f"UPDATE {SCHEMA}.api_keys SET is_active = false "
        "WHERE environment <> 'production'",
        RESET,
        _set(module._FLAGS_LOCK_TIMEOUT),
        RESET,
    ]
    (name, table), kwargs = recorder.one("drop_constraint")
    assert (name, table) == (API_KEY_ENVIRONMENT_CHECK, "api_keys")
    assert kwargs["type_"] == "check"
    assert recorder.one("drop_column")[0] == ("api_keys", "environment")
    assert recorder.one("drop_table")[0] == ("flag_environment_settings",)


def test_the_documented_targets_reader_sees_the_downgrades_drop_column():
    """``test_documented_downgrade_targets.py`` holds every documented
    downgrade target that crosses this revision to saying what it drops, but
    only if it finds a ``drop_column`` call inside ``downgrade()`` itself
    (nested functions included). Moved into a module-level helper, it is
    invisible there, and every such target would pass unannounced."""
    assert drops_a_column(str(_path()))


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_each_step_runs_only_when_its_object_is_absent_or_present(
    monkeypatch, direction
):
    """An upgrade over objects already there, and a downgrade over objects
    already gone, change nothing: only the bounds and their resets run."""
    recorder = _Recorder()
    _run(monkeypatch, recorder, direction, present=(direction == "upgrade"))
    assert recorder.names() == ["execute"] * 4
    assert all("lock_timeout" in s for s in recorder.statements())


# ---------------------------------------------------------------------------
# The refusals
# ---------------------------------------------------------------------------
def _operational_error(pgcode: str) -> sa.exc.OperationalError:
    return sa.exc.OperationalError("ALTER TABLE", {}, SimpleNamespace(pgcode=pgcode))


#: (direction, the call that fails, the table whose block it is in).
BLOCKS = [
    ("upgrade", "create_table", "feature_flags"),
    ("upgrade", "create_index", "feature_flags"),
    ("upgrade", "add_column", "api_keys"),
    ("upgrade", "create_check_constraint", "api_keys"),
    ("downgrade", "execute", "api_keys"),
    ("downgrade", "drop_constraint", "api_keys"),
    ("downgrade", "drop_column", "api_keys"),
    ("downgrade", "drop_table", "feature_flags"),
]


class _FailOnUpdate(_Recorder):
    """Fails the downgrade's UPDATE, and nothing else it executes."""

    def __init__(self, error) -> None:
        super().__init__()
        self._error = error

    def get_bind(self):
        def execute(clause, *a):
            text = " ".join(str(clause).split())
            if text.startswith("UPDATE"):
                raise self._error
            self._record("execute", text)

        return SimpleNamespace(execute=execute, dialect=postgresql.dialect())


def _failing(call: str, error) -> _Recorder:
    if call == "execute":
        return _FailOnUpdate(error)
    return _Recorder(fail={call: error})


@pytest.mark.parametrize("code", ["55P03", "40P01"])
@pytest.mark.parametrize("direction, call, table", BLOCKS)
def test_a_lock_timeout_or_a_deadlock_in_any_block_refuses_with_that_blocks_text(
    monkeypatch, direction, call, table, code
):
    recorder = _failing(call, _operational_error(code))
    with pytest.raises(RuntimeError) as raised:
        _run(monkeypatch, recorder, direction, present=(direction == "downgrade"))
    module = _module()
    refusal = (
        module._lock_refusal
        if direction == "upgrade"
        else module._downgrade_lock_refusal
    )
    assert str(raised.value) == refusal(table)
    assert f"{SCHEMA}.{table}" in str(raised.value)
    assert raised.value.__cause__ is None and raised.value.__suppress_context__
    # The reset after a refused block never runs: the run is rolled back.
    bounds = [s for s in recorder.statements() if "lock_timeout" in s]
    assert bounds[-1].startswith("SET LOCAL lock_timeout = '"), bounds


@pytest.mark.parametrize("direction, call, table", BLOCKS)
def test_any_other_error_is_raised_as_it_was(monkeypatch, direction, call, table):
    error = _operational_error("57014")
    recorder = _failing(call, error)
    with pytest.raises(sa.exc.OperationalError) as raised:
        _run(monkeypatch, recorder, direction, present=(direction == "downgrade"))
    assert raised.value is error


def test_one_code_set_serves_every_block_in_both_directions():
    module = _module()
    assert module._LOCK_REFUSED == ("55P03", "40P01")
    source = _path().read_text(encoding="utf-8")
    assert source.count('"55P03"') == 1 and source.count('"40P01"') == 1
    assert source.count("_pgcode(exc) in _LOCK_REFUSED") == 1


@pytest.mark.parametrize("table", ["feature_flags", "api_keys"])
def test_the_upgrade_refusal_names_what_it_must_and_nothing_false(table):
    message = _module()._lock_refusal(table)
    first, last = message.splitlines()
    bound = {"feature_flags": "500ms", "api_keys": "1500ms"}[table]
    assert first.startswith(f"Refusing to upgrade schema {SCHEMA}: ")
    assert "t2011_flag_environments" in first
    assert f"{SCHEMA}.{table}" in first and bound in first
    assert "Nothing was applied" in last
    assert "every revision of this upgrade was rolled back" in last
    assert last.endswith("Run the upgrade again.")
    # #238; and true for a deadlock as well as a timeout.
    assert "alembic_version" not in message
    assert "waited longer than" not in message


@pytest.mark.parametrize("table", ["feature_flags", "api_keys"])
def test_the_downgrade_refusal_says_downgrade_and_nothing_false(table):
    message = _module()._downgrade_lock_refusal(table)
    first, last = message.splitlines()
    bound = {"feature_flags": "500ms", "api_keys": "1500ms"}[table]
    assert message.startswith(f"Refusing to downgrade schema {SCHEMA}: ")
    assert f"{SCHEMA}.{table}" in first and bound in first
    # Dropping the side table locks that table too: the text for the block
    # that does it is true whichever of the two a session holds.
    if table == "feature_flags":
        assert f"{SCHEMA}.flag_environment_settings" in first
    assert "Nothing was changed" in last
    assert last.endswith("Run the downgrade again.")
    assert "Refusing to upgrade" not in message
    assert "alembic_version" not in message
    assert "waited longer than" not in message


def test_the_api_keys_refusal_says_what_the_lock_is_for():
    assert _module()._lock_refusal("api_keys").splitlines()[0] == (
        f"Refusing to upgrade schema {SCHEMA}: t2011_flag_environments could not "
        f"take the lock it needs on {SCHEMA}.api_keys to add the column "
        "environment, because another session held it (this step's bound is "
        "1500ms)."
    )


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_offline_mode_is_refused(monkeypatch, direction):
    module = _module()
    recorder = _Recorder(as_sql=True)
    monkeypatch.setattr(module, "op", recorder)
    with pytest.raises(RuntimeError) as raised:
        getattr(module, direction)()
    assert str(raised.value) == module._OFFLINE_REFUSAL
    assert recorder.calls == []


#: The database test files of this revision, and how many tests each defines.
DATABASE_TESTS = {
    "backend/tests/integration/database/test_flag_environments_migration.py": 20,
}


@pytest.mark.parametrize("path", sorted(DATABASE_TESTS))
def test_the_database_tests_are_all_collected_and_none_is_skipped(path):
    source = (REPO_ROOT / path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    tests = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    ]
    assert len(tests) == DATABASE_TESTS[path], sorted(tests)
    for word in ("mark.skip", "skipif", "pytest.skip(", "importorskip", "mark.xfail"):
        assert word not in source, f"{path} has {word}"
