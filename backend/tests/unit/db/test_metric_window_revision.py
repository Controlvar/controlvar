"""``t1934_metric_window``, read without a database (#1934).

* Its id fits ``alembic_version.version_num`` (32 characters), its
  ``down_revision`` is the literal ``t2012_flag_split``, and the heads are
  exactly this revision and, in a full tree, the modules head.
* The migration adds the model's column (``integer``, nullable, no default) and
  the model's check, word for word: NULL, or 1 to 8760.
* The bound: ``_LOCK_TIMEOUT`` is ``500ms``, written once and never a zero
  form, and it is the budget map's declared 0.5 s.
* Upgrading, the ``ALTER`` statements run straight between ``SET LOCAL
  lock_timeout`` and the reset, and only for what is absent. Downgrading, the
  column is dropped by name under the same bound, only when present.
* In both directions a lock timeout (55P03) or a deadlock (40P01) becomes that
  direction's refusal; any other error is raised as it was.
* Offline mode is refused in both directions.
* The 8760 bound is the core model's own, not imported from a module.
* The database tests of this revision are all collected and none is skipped.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from backend.app.models.experiment import MAX_CONVERSION_WINDOW_HOURS, Metric
from backend.tests.unit.db.test_upgrade_lock_budget import _script, release
from backend.tests.unit.docs.test_documented_downgrade_targets import drops_a_column

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REVISION = "t1934_metric_window"
REPO_ROOT = Path(__file__).resolve().parents[4]
#: What a test schema is called while these tests import the models.
SCHEMA = Metric.__table__.schema
TABLE = "metrics"
COLUMN = "conversion_window_hours"
CHECK = "check_metric_conversion_window"


def _path() -> Path:
    return Path(_script().get_revision(REVISION).path)


def _module():
    spec = importlib.util.spec_from_file_location("metric_window_revision", _path())
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # The models are built for the test schema; so is the revision here.
    module._SCHEMA = SCHEMA
    return module


def test_it_extends_t2012_flag_split_and_fits_the_version_table():
    revision = _script().get_revision(REVISION)
    # Kept literal on purpose. A re-point changes this line, every CORE_HEAD
    # pin, the NAMED_DOWNGRADES lists in test_db_migrate_target_graph.py, the
    # sentences that give this revision's down_revision (the migrations page,
    # the rollback runbook, db-migrate.yml's help text) and the migration's own
    # docstring, together.
    assert revision.down_revision == "t2012_flag_split"
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
        #: call name -> the exception raised instead of recording it.
        self._fail = fail or {}

    def _record(self, name, *args, **kwargs):
        failure = self._fail.get(name)
        if failure is not None:
            raise failure
        self.calls.append((name, args, kwargs))

    def get_context(self):
        return SimpleNamespace(as_sql=self._as_sql)

    def get_bind(self):
        return SimpleNamespace(execute=self._execute)

    def _execute(self, clause, *a):
        statement = " ".join(str(clause).split())
        self._record(statement.split(" ", 1)[0], statement)

    def add_column(self, table, column, **kwargs):
        self._record("add_column", table, column, **kwargs)

    def create_check_constraint(self, name, table, condition, **kwargs):
        self._record("create_check_constraint", name, table, condition, **kwargs)

    def drop_column(self, table, column, **kwargs):
        self._record("drop_column", table, column, **kwargs)

    def names(self) -> list:
        return [name for name, _, _ in self.calls]

    def statements(self, *words) -> list:
        return [args[0] for name, args, _ in self.calls if name in words]

    def all(self, name) -> list:
        return [(args, kwargs) for n, args, kwargs in self.calls if n == name]


def _run(monkeypatch, recorder, direction, *, column=False, check=False):
    """Run *direction* with the column and the check present or not."""
    module = _module()
    monkeypatch.setattr(module, "op", recorder)
    monkeypatch.setattr(module, "_column_exists", lambda: column)
    monkeypatch.setattr(module, "_check_exists", lambda: check)
    getattr(module, direction)()
    return module


SET = "SET LOCAL lock_timeout = '500ms'"
RESET = "SET LOCAL lock_timeout TO DEFAULT"


# ---------------------------------------------------------------------------
# The model's column and check
# ---------------------------------------------------------------------------
def _model_check() -> sa.CheckConstraint:
    (found,) = [
        c
        for c in Metric.__table__.constraints
        if isinstance(c, sa.CheckConstraint) and c.name == CHECK
    ]
    return found


def test_the_migration_adds_the_models_column_and_check(monkeypatch):
    recorder = _Recorder()
    module = _run(monkeypatch, recorder, "upgrade")
    ((table, column), kwargs) = recorder.all("add_column")[0]
    model = Metric.__table__.columns[COLUMN]
    assert table == TABLE and kwargs == {"schema": SCHEMA}
    assert column.name == COLUMN
    assert isinstance(column.type, sa.Integer) and isinstance(model.type, sa.Integer)
    assert column.nullable is True and model.nullable is True
    for made in (column, model):
        assert made.server_default is None and made.default is None
        assert made.unique is None and not made.foreign_keys and not made.index
    ((name, checked, condition), kwargs) = recorder.all("create_check_constraint")[0]
    assert (name, checked, kwargs) == (CHECK, TABLE, {"schema": SCHEMA})
    # The model's text, word for word, so a bootstrapped schema and an upgraded
    # one hold the same definition.
    assert condition == str(_model_check().sqltext) == module._CHECK_CONDITION
    assert condition == (
        "conversion_window_hours IS NULL OR conversion_window_hours BETWEEN 1 AND 8760"
    )
    # Nothing else: no index, no data step.
    assert recorder.names() == ["SET", "add_column", "create_check_constraint", "SET"]


def test_the_bound_of_8760_hours_is_the_core_models_own():
    """365 days, the warehouse analysis's own bound, written in core: a core
    build has no ``modules`` package to import it from."""
    assert MAX_CONVERSION_WINDOW_HOURS == 8760 == 365 * 24
    source = (REPO_ROOT / "backend/app/models/experiment.py").read_text("utf-8")
    imported = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    for node in imported:
        names = [getattr(node, "module", None) or ""]
        names += [alias.name for alias in node.names]
        assert not any(name.startswith("modules") for name in names), names


# ---------------------------------------------------------------------------
# The bound, and the order of the block
# ---------------------------------------------------------------------------
def test_the_bound_is_half_a_second_and_the_declared_one():
    module = _module()
    assert module._LOCK_TIMEOUT == "500ms"
    assert release("0.49.0").declared[REVISION] == 0.5


def test_the_bound_lives_in_one_constant():
    """Nobody retypes it: the literal is written once, as the constant's value,
    which is no zero form, and the ``SET`` is built from the constant."""
    source = _path().read_text(encoding="utf-8")
    assert source.count('"500ms"') == 1
    assert "SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'" in source
    tree = ast.parse(source)
    (assigned,) = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(t, ast.Name) and t.id == "_LOCK_TIMEOUT" for t in node.targets
        )
    ]
    assert assigned.value.value == "500ms"


@pytest.mark.parametrize(
    "column, check, expected",
    [
        (False, False, ["add_column", "create_check_constraint"]),
        (True, False, ["create_check_constraint"]),
        (False, True, ["add_column"]),
        (True, True, []),
    ],
    ids=["neither", "column-only", "check-only", "both"],
)
def test_the_upgrade_adds_only_what_is_absent_inside_one_bound(
    monkeypatch, column, check, expected
):
    recorder = _Recorder()
    _run(monkeypatch, recorder, "upgrade", column=column, check=check)
    # No bound is set when nothing runs.
    assert recorder.names() == (["SET", *expected, "SET"] if expected else [])
    assert recorder.statements("SET") == ([SET, RESET] if expected else [])


def test_the_downgrade_drops_the_column_by_name_inside_one_bound(monkeypatch):
    recorder = _Recorder()
    _run(monkeypatch, recorder, "downgrade", column=True, check=True)
    assert recorder.names() == ["SET", "drop_column", "SET"]
    assert recorder.statements("SET") == [SET, RESET]
    assert recorder.all("drop_column") == [((TABLE, COLUMN), {"schema": SCHEMA})]


def test_the_downgrade_of_a_schema_without_the_column_does_nothing(monkeypatch):
    recorder = _Recorder()
    _run(monkeypatch, recorder, "downgrade", column=False, check=False)
    assert recorder.calls == []


def test_the_documented_targets_reader_sees_the_downgrades_drop_column():
    """``test_documented_downgrade_targets.py`` holds every documented
    downgrade target that crosses this revision to saying what it drops, but
    only if it finds a ``drop_column`` call inside ``downgrade()`` itself."""
    assert drops_a_column(str(_path()))


# ---------------------------------------------------------------------------
# The lock refusals
# ---------------------------------------------------------------------------
def _operational_error(pgcode: str) -> sa.exc.OperationalError:
    return sa.exc.OperationalError("ALTER TABLE", {}, SimpleNamespace(pgcode=pgcode))


#: ``(direction, the call that fails)``: each statement of each block.
BLOCKS = [
    ("upgrade", "add_column"),
    ("upgrade", "create_check_constraint"),
    ("downgrade", "drop_column"),
]


def _refusal(module, direction: str) -> str:
    if direction == "upgrade":
        return module._lock_refusal()
    return module._downgrade_lock_refusal()


@pytest.mark.parametrize("code", ["55P03", "40P01"])
@pytest.mark.parametrize("direction, call", BLOCKS)
def test_a_lock_timeout_or_a_deadlock_refuses_with_that_directions_text(
    monkeypatch, direction, call, code
):
    recorder = _Recorder(fail={call: _operational_error(code)})
    present = direction == "downgrade"
    with pytest.raises(RuntimeError) as raised:
        _run(monkeypatch, recorder, direction, column=present, check=present)
    assert str(raised.value) == _refusal(_module(), direction)
    assert f"{SCHEMA}.{TABLE} " in str(raised.value)
    assert raised.value.__cause__ is None and raised.value.__suppress_context__
    # The reset after a refused block never runs: the run is rolled back.
    assert recorder.statements("SET") == [SET]


@pytest.mark.parametrize("direction, call", BLOCKS)
def test_any_other_error_is_raised_as_it_was(monkeypatch, direction, call):
    error = _operational_error("57014")
    recorder = _Recorder(fail={call: error})
    present = direction == "downgrade"
    with pytest.raises(sa.exc.OperationalError) as raised:
        _run(monkeypatch, recorder, direction, column=present, check=present)
    assert raised.value is error


def test_one_code_set_serves_both_directions():
    module = _module()
    assert module._LOCK_REFUSED == ("55P03", "40P01")
    source = _path().read_text(encoding="utf-8")
    assert source.count('"55P03"') == 1 and source.count('"40P01"') == 1
    assert source.count("_pgcode(exc) in _LOCK_REFUSED") == 1


def test_the_upgrade_refusal_names_what_it_must_and_nothing_false():
    message = _module()._lock_refusal()
    first, last = message.splitlines()
    assert first.startswith(f"Refusing to upgrade schema {SCHEMA}: ")
    assert REVISION in first and f"{SCHEMA}.{TABLE} " in first and "500ms" in first
    assert COLUMN in first
    assert "Nothing was applied" in last
    assert "every revision of this upgrade was rolled back" in last
    assert last.endswith("Run the upgrade again.")
    # #238; and true for a deadlock as well as a timeout.
    assert "alembic_version" not in message
    assert "waited longer than" not in message


def test_the_downgrade_lock_refusal_says_downgrade_and_nothing_false():
    message = _module()._downgrade_lock_refusal()
    first, last = message.splitlines()
    assert message.startswith(f"Refusing to downgrade schema {SCHEMA}: ")
    assert REVISION in first and f"{SCHEMA}.{TABLE} " in first and "500ms" in first
    assert COLUMN in first
    assert "Nothing was changed" in last
    assert last.endswith("Run the downgrade again.")
    assert "Refusing to upgrade" not in message
    assert "alembic_version" not in message
    assert "waited longer than" not in message


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
    "backend/tests/integration/database/test_metric_window_migration.py": 16,
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
