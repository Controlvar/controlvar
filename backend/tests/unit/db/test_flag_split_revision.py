"""``t2012_flag_split``, read without a database (#2012).

* Its id fits ``alembic_version.version_num`` (32 characters), its
  ``down_revision`` is the literal ``t2011_flag_environments``, and its only
  child is ``t1934_metric_window`` (#1934).
* The migration adds the models' column to both tables: ``jsonb``, nullable,
  no default.
* The bound: ``_LOCK_TIMEOUT`` is ``1s``, written once and never a zero form,
  and it is the budget map's declared second, which is the ``feature_flags``
  step's alone.
* Upgrading, each ``ALTER`` runs straight between its ``SET LOCAL
  lock_timeout`` and the reset, ``feature_flags`` first, and only when the
  column is absent. Downgrading, both tables are locked before the stored flags
  are read, and both columns are dropped by name under the same locks.
* In every block of both directions a lock timeout (55P03) or a deadlock
  (40P01) becomes that direction's refusal, worded so that it is true for
  both; any other error is raised as it was.
* A flag with a split or a rule value refuses the downgrade with a message
  that names it, before anything is dropped.
* Offline mode is refused in both directions.
* The database tests of this revision are all collected and none is skipped.
"""

from __future__ import annotations

import ast
import importlib.util
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from backend.app.models.feature_flag import FeatureFlag, FlagEnvironmentSetting
from backend.tests.unit.db.test_upgrade_lock_budget import _script, release
from backend.tests.unit.docs.test_documented_downgrade_targets import drops_a_column

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REVISION = "t2012_flag_split"
REPO_ROOT = Path(__file__).resolve().parents[4]
#: What a test schema is called while these tests import the models.
SCHEMA = FeatureFlag.__table__.schema
FLAGS = "feature_flags"
SETTINGS = "flag_environment_settings"
TABLES = (FLAGS, SETTINGS)
MODELS = {FLAGS: FeatureFlag, SETTINGS: FlagEnvironmentSetting}


def _path() -> Path:
    return Path(_script().get_revision(REVISION).path)


def _module():
    spec = importlib.util.spec_from_file_location("flag_split_revision", _path())
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # The models are built for the test schema; so is the revision here.
    module._SCHEMA = SCHEMA
    return module


def test_it_extends_t2011_flag_environments_and_fits_the_version_table():
    revision = _script().get_revision(REVISION)
    # Kept literal on purpose. A re-point changes this line, every CORE_HEAD
    # pin, the NAMED_DOWNGRADES lists in test_db_migrate_target_graph.py, the
    # sentences that give this revision's down_revision (the migrations page,
    # the rollback runbook, db-migrate.yml's help text) and the migration's own
    # docstring, together.
    assert revision.down_revision == "t2011_flag_environments"
    assert revision.revision == REVISION
    assert len(revision.revision) <= 32


def test_this_revision_is_extended_by_t1934_metric_window_and_nothing_else():
    """``t1934_metric_window`` (#1934) is its only child, and the heads test
    is the core head's now (``test_metric_window_revision.py``)."""
    script = _script()
    children = [
        r.revision
        for r in script.walk_revisions()
        if r.down_revision == REVISION
        or (isinstance(r.down_revision, tuple) and REVISION in r.down_revision)
    ]
    assert children == ["t1934_metric_window"]
    assert REVISION not in script.get_heads()


class _Recorder:
    """Stands in for ``alembic.op``: records each call, in order."""

    def __init__(self, as_sql: bool = False, fail=None, rows=()) -> None:
        self.calls: list = []
        self._as_sql = as_sql
        #: ``(call name, table)`` -> the exception raised instead of recording
        #: it; a LOCK or a SELECT is a call named by its first word.
        self._fail = fail or {}
        #: What the read of the stored flags answers.
        self._rows = [SimpleNamespace(key=k, environment=e) for k, e in rows]

    def _record(self, name, *args, table=None, **kwargs):
        failure = self._fail.get((name, table))
        if failure is not None:
            raise failure
        self.calls.append((name, args, kwargs))

    def get_context(self):
        return SimpleNamespace(as_sql=self._as_sql)

    def get_bind(self):
        return SimpleNamespace(execute=self._execute, dialect=postgresql.dialect())

    def _execute(self, clause, *a):
        statement = " ".join(str(clause).split())
        word = statement.split(" ", 1)[0]
        table = None
        if word == "LOCK":
            (table,) = [t for t in TABLES if f".{t} IN " in statement]
        self._record(word, statement, table=table)
        return list(self._rows) if word == "SELECT" else None

    def add_column(self, table, column, **kwargs):
        self._record("add_column", table, column, table=table, **kwargs)

    def drop_column(self, table, column, **kwargs):
        self._record("drop_column", table, column, table=table, **kwargs)

    def names(self) -> list:
        return [name for name, _, _ in self.calls]

    def statements(self, *words) -> list:
        return [args[0] for name, args, _ in self.calls if name in words]

    def all(self, name) -> list:
        return [(args, kwargs) for n, args, kwargs in self.calls if n == name]


def _run(monkeypatch, recorder: _Recorder, direction: str, present):
    """Run *direction* with the column *present* in those tables."""
    module = _module()
    monkeypatch.setattr(module, "op", recorder)
    monkeypatch.setattr(module, "_column_exists", lambda table: table in present)
    getattr(module, direction)()
    return module


def _upgraded(monkeypatch, recorder: _Recorder):
    return _run(monkeypatch, recorder, "upgrade", present=())


def _downgraded(monkeypatch, recorder: _Recorder):
    return _run(monkeypatch, recorder, "downgrade", present=TABLES)


# ---------------------------------------------------------------------------
# The models' column
# ---------------------------------------------------------------------------
def test_the_migration_adds_the_models_column_to_both_tables(monkeypatch):
    recorder = _Recorder()
    _upgraded(monkeypatch, recorder)
    added = recorder.all("add_column")
    assert [args[0] for args, _ in added] == list(TABLES)
    for (table, column), kwargs in added:
        model = MODELS[table].__table__.columns["split"]
        assert kwargs["schema"] == MODELS[table].__table__.schema
        assert column.name == "split"
        assert isinstance(column.type, postgresql.JSONB)
        assert isinstance(model.type, postgresql.JSONB)
        assert column.nullable is True and model.nullable is True
        for made in (column, model):
            assert made.server_default is None and made.default is None
            assert made.unique is None and not made.foreign_keys
    # Nothing else: no check, no index, no data step.
    assert set(recorder.names()) == {"SET", "add_column"}


# ---------------------------------------------------------------------------
# The bound, and the order of the blocks
# ---------------------------------------------------------------------------
def test_the_bound_is_one_second_and_the_declared_one():
    module = _module()
    assert module._LOCK_TIMEOUT == "1s"
    assert release("0.49.0").declared[REVISION] == 1


def test_the_bound_lives_in_one_constant():
    """Nobody retypes it: the literal is written once, as the constant's value,
    which is no zero form, and the ``SET`` is built from the constant."""
    source = _path().read_text(encoding="utf-8")
    assert source.count('"1s"') == 1
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
    assert assigned.value.value == "1s"


SET = "SET LOCAL lock_timeout = '1s'"
RESET = "SET LOCAL lock_timeout TO DEFAULT"


def test_the_upgrade_bounds_each_alter_feature_flags_first(monkeypatch):
    recorder = _Recorder()
    _upgraded(monkeypatch, recorder)
    assert recorder.names() == ["SET", "add_column", "SET", "SET", "add_column", "SET"]
    assert recorder.statements("SET") == [SET, RESET, SET, RESET]
    assert [args[0] for args, _ in recorder.all("add_column")] == list(TABLES)


def test_the_downgrade_locks_both_tables_then_reads_then_drops(monkeypatch):
    recorder = _Recorder()
    _downgraded(monkeypatch, recorder)
    assert recorder.names() == [
        "SET",
        "LOCK",
        "SET",
        "SET",
        "LOCK",
        "SELECT",
        "drop_column",
        "drop_column",
        "SET",
    ]
    assert recorder.statements("SET") == [SET, RESET, SET, RESET]
    assert recorder.statements("LOCK") == [
        f"LOCK TABLE {SCHEMA}.{table} IN ACCESS EXCLUSIVE MODE" for table in TABLES
    ]
    assert [
        (args, kwargs["schema"]) for args, kwargs in recorder.all("drop_column")
    ] == [((table, "split"), SCHEMA) for table in TABLES]


def test_the_documented_targets_reader_sees_the_downgrades_drop_column():
    """``test_documented_downgrade_targets.py`` holds every documented
    downgrade target that crosses this revision to saying what it drops, but
    only if it finds a ``drop_column`` call inside ``downgrade()`` itself."""
    assert drops_a_column(str(_path()))


@pytest.mark.parametrize("present", [TABLES, (FLAGS,), (SETTINGS,)], ids=str)
def test_the_upgrade_adds_only_an_absent_column(monkeypatch, present):
    recorder = _Recorder()
    _run(monkeypatch, recorder, "upgrade", present=present)
    absent = [table for table in TABLES if table not in present]
    assert [args[0] for args, _ in recorder.all("add_column")] == absent
    # No bound is set for a step that does not run.
    assert recorder.statements("SET") == [SET, RESET] * len(absent)


@pytest.mark.parametrize("present", [(), (FLAGS,), (SETTINGS,)], ids=str)
def test_the_downgrade_drops_only_a_present_column_and_still_looks(
    monkeypatch, present
):
    """A column already gone is not read or dropped; the rules are still
    looked at."""
    recorder = _Recorder()
    _run(monkeypatch, recorder, "downgrade", present=present)
    assert [args[0] for args, _ in recorder.all("drop_column")] == list(present)
    (read,) = recorder.statements("SELECT")
    assert ("flag.split IS NOT NULL" in read) == (FLAGS in present)
    assert ("setting.split IS NOT NULL" in read) == (SETTINGS in present)
    assert "flag.targeting_rules" in read and "setting.targeting_rules" in read


# ---------------------------------------------------------------------------
# The lock refusals
# ---------------------------------------------------------------------------
def _operational_error(pgcode: str) -> sa.exc.OperationalError:
    return sa.exc.OperationalError("ALTER TABLE", {}, SimpleNamespace(pgcode=pgcode))


#: ``(direction, the call that fails, its table, the table the refusal names)``.
#: The downgrade's second block locks ``flag_environment_settings``, then runs
#: everything else under both locks.
BLOCKS = [
    ("upgrade", "add_column", FLAGS, FLAGS),
    ("upgrade", "add_column", SETTINGS, SETTINGS),
    ("downgrade", "LOCK", FLAGS, FLAGS),
    ("downgrade", "LOCK", SETTINGS, SETTINGS),
    ("downgrade", "SELECT", None, SETTINGS),
    ("downgrade", "drop_column", FLAGS, SETTINGS),
    ("downgrade", "drop_column", SETTINGS, SETTINGS),
]


def _present(direction: str):
    return TABLES if direction == "downgrade" else ()


@pytest.mark.parametrize("code", ["55P03", "40P01"])
@pytest.mark.parametrize("direction, call, table, named", BLOCKS)
def test_a_lock_timeout_or_a_deadlock_in_any_block_refuses_with_that_blocks_text(
    monkeypatch, direction, call, table, named, code
):
    recorder = _Recorder(fail={(call, table): _operational_error(code)})
    with pytest.raises(RuntimeError) as raised:
        _run(monkeypatch, recorder, direction, present=_present(direction))
    module = _module()
    refusal = (
        module._lock_refusal
        if direction == "upgrade"
        else module._downgrade_lock_refusal
    )
    assert str(raised.value) == refusal(named)
    assert f"{SCHEMA}.{named}" in str(raised.value)
    assert raised.value.__cause__ is None and raised.value.__suppress_context__
    # The reset after a refused block never runs: the run is rolled back.
    assert recorder.statements("SET")[-1] == SET


@pytest.mark.parametrize("direction, call, table, named", BLOCKS)
def test_any_other_error_is_raised_as_it_was(
    monkeypatch, direction, call, table, named
):
    error = _operational_error("57014")
    recorder = _Recorder(fail={(call, table): error})
    with pytest.raises(sa.exc.OperationalError) as raised:
        _run(monkeypatch, recorder, direction, present=_present(direction))
    assert raised.value is error


def test_one_code_set_serves_every_block_in_both_directions():
    module = _module()
    assert module._LOCK_REFUSED == ("55P03", "40P01")
    source = _path().read_text(encoding="utf-8")
    assert source.count('"55P03"') == 1 and source.count('"40P01"') == 1
    assert source.count("_pgcode(exc) in _LOCK_REFUSED") == 1


@pytest.mark.parametrize("table", TABLES)
def test_the_upgrade_refusal_names_what_it_must_and_nothing_false(table):
    message = _module()._lock_refusal(table)
    first, last = message.splitlines()
    assert first.startswith(f"Refusing to upgrade schema {SCHEMA}: ")
    assert REVISION in first and f"{SCHEMA}.{table} " in first and "1s" in first
    assert "Nothing was applied" in last
    assert "every revision of this upgrade was rolled back" in last
    assert last.endswith("Run the upgrade again.")
    # #238; and true for a deadlock as well as a timeout.
    assert "alembic_version" not in message
    assert "waited longer than" not in message


@pytest.mark.parametrize("table", TABLES)
def test_the_downgrade_lock_refusal_says_downgrade_and_nothing_false(table):
    message = _module()._downgrade_lock_refusal(table)
    first, last = message.splitlines()
    assert message.startswith(f"Refusing to downgrade schema {SCHEMA}: ")
    assert REVISION in first and f"{SCHEMA}.{table} " in first and "1s" in first
    assert "Nothing was changed" in last
    assert last.endswith("Run the downgrade again.")
    assert "Refusing to upgrade" not in message
    assert "alembic_version" not in message
    assert "waited longer than" not in message


# ---------------------------------------------------------------------------
# The refusal over stored flags
# ---------------------------------------------------------------------------
IN_USE = [
    ("checkout-copy", "production"),
    ("price-tier", "development"),
    ("price-tier", "staging"),
]


def test_a_flag_in_use_refuses_the_downgrade_before_anything_is_dropped(monkeypatch):
    recorder = _Recorder(rows=IN_USE)
    with pytest.raises(RuntimeError) as raised:
        _downgraded(monkeypatch, recorder)
    assert recorder.all("drop_column") == []
    # Both tables were locked first.
    assert recorder.names()[-2:] == ["LOCK", "SELECT"]
    message = str(raised.value)
    assert message == (
        f"Refusing to downgrade schema {SCHEMA}: t2012_flag_split found 2 feature "
        "flag(s) with a split or a rule value: checkout-copy (production), "
        "price-tier (development, staging). This downgrade drops "
        "feature_flags.split and flag_environment_settings.split, and the release "
        "it rolls back to serves neither a split nor a rule value. Clear them "
        "while the release that wrote them is still serving, or restore the "
        "backup taken before the upgrade to it. Nothing was changed."
    )
    assert re.search(r"alembic_version|\bstamp\b", message, re.I) is None


@pytest.mark.parametrize("count, shown", [(20, 20), (21, 20), (35, 20)])
def test_the_refusal_names_at_most_twenty_flags_and_counts_the_rest(count, shown):
    flags = {f"flag-{n:02d}": ["production"] for n in range(count)}
    message = _module()._in_use_refusal(flags)
    assert f"found {count} feature flag(s)" in message
    assert message.count(" (production)") == shown
    assert (f" and {count - shown} more." in message) == (count > shown)


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_offline_mode_is_refused(monkeypatch, direction):
    module = _module()
    recorder = _Recorder(as_sql=True)
    monkeypatch.setattr(module, "op", recorder)
    with pytest.raises(RuntimeError) as raised:
        getattr(module, direction)()
    assert str(raised.value) == module._OFFLINE_REFUSAL
    assert recorder.calls == []


def test_the_rule_values_are_found_by_shape_only():
    """Never the bare key at the top of the rules, which a legacy flat object
    has too, and never the recursive path, which every condition matches."""
    code = _module()._HAS_RULE_VALUE
    assert "? 'value'" not in code
    assert "$.**" not in code and "jsonb_path" not in code
    for shape in ("'groups'", "'rules'", "'default_rule'"):
        assert shape in code, shape


#: The database test files of this revision, and how many tests each defines.
DATABASE_TESTS = {
    "backend/tests/integration/database/test_flag_split_migration.py": 23,
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
