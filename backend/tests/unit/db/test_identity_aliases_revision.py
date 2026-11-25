"""``t449_identity_aliases``, read without a database (#449).

* IA-M2: its id fits ``alembic_version.version_num`` (32 characters), its
  ``down_revision`` is the literal ``t1812_user_totp`` (0.47.0's core head),
  and its only child is ``t2011_flag_environments`` (#2011).
* IA-M5, without a database: the migration creates the model's columns with
  the model's types, nullability and server defaults, the model's named check
  and index, and no foreign key.
* The one bound: ``_LOCK_TIMEOUT`` is ``250ms`` (never 0), the ``SET`` is the
  revision's first statement and the reset its last, and the budget map
  declares the same quarter second.
* A lock timeout (SQLSTATE 55P03) becomes this revision's refusal, which names
  the table and the bound, says nothing was applied and says to run the
  upgrade again; any other error is raised as it was.
* Offline mode is refused in both directions.
* The database tests of this table are all collected and none is skipped:
  without PostgreSQL they error, so a skip would be the only way to look green
  without running them.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from backend.app.models.identity_alias import IdentityAlias
from backend.tests.unit.db.test_upgrade_lock_budget import _script, release

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REVISION = "t449_identity_aliases"
REPO_ROOT = Path(__file__).resolve().parents[4]


def _path() -> Path:
    return Path(_script().get_revision(REVISION).path)


def _module():
    spec = importlib.util.spec_from_file_location("identity_aliases_revision", _path())
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_it_extends_0_47_0s_core_head_and_fits_the_version_table():
    revision = _script().get_revision(REVISION)
    # Kept literal on purpose. A re-point changes this line, every CORE_HEAD
    # pin, the NAMED_DOWNGRADES lists in test_db_migrate_target_graph.py, the
    # sentences that give this revision's down_revision (the migrations page,
    # the rollback runbook, db-migrate.yml's help text) and the migration's own
    # docstring, together.
    assert revision.down_revision == "t1812_user_totp"
    assert revision.revision == REVISION
    assert len(revision.revision) <= 32


def test_this_revision_is_extended_by_t2011_flag_environments_and_nothing_else():
    """0.48.0 shipped this revision as the core head; ``t2011_flag_environments``
    (#2011) is its only child, and the heads test is the core head's now
    (``test_flag_split_revision.py``)."""
    script = _script()
    children = [
        r.revision
        for r in script.walk_revisions()
        if r.down_revision == REVISION
        or (isinstance(r.down_revision, tuple) and REVISION in r.down_revision)
    ]
    assert children == ["t2011_flag_environments"]
    assert REVISION not in script.get_heads()


class _Recorder:
    """Stands in for ``alembic.op``: records what ``upgrade`` runs and creates."""

    def __init__(self, as_sql: bool = False, fail_create=None) -> None:
        self.statements: list = []
        self.tables: list = []
        self.indexes: list = []
        self._as_sql = as_sql
        self._fail_create = fail_create

    def get_context(self):
        return SimpleNamespace(as_sql=self._as_sql)

    def get_bind(self):
        return SimpleNamespace(
            execute=lambda clause, *a: self.statements.append(str(clause))
        )

    def create_table(self, name, *items, **kwargs):
        if self._fail_create is not None:
            raise self._fail_create
        self.tables.append((name, items, kwargs))

    def create_index(self, name, table, columns, **kwargs):
        self.indexes.append((name, table, columns, kwargs))

    def drop_index(self, *args, **kwargs):  # pragma: no cover - not reached
        raise AssertionError("the downgrade must refuse offline mode first")

    def drop_table(self, *args, **kwargs):  # pragma: no cover - not reached
        raise AssertionError("the downgrade must refuse offline mode first")


def _upgraded(monkeypatch, recorder: _Recorder):
    module = _module()
    monkeypatch.setattr(module, "op", recorder)
    monkeypatch.setattr(module, "_relation_exists", lambda name, kind: False)
    module.upgrade()
    return module


def _default_sql(column) -> str:
    default = column.server_default
    return None if default is None else str(default.arg)


def test_the_migration_creates_the_models_table(monkeypatch):
    recorder = _Recorder()
    _upgraded(monkeypatch, recorder)
    ((name, items, kwargs),) = recorder.tables
    assert name == "identity_aliases" == IdentityAlias.__tablename__
    made = sa.Table(name, sa.MetaData(), *items, schema=kwargs["schema"])
    model = IdentityAlias.__table__
    assert made.schema == model.schema
    assert list(made.columns.keys()) == list(model.columns.keys())
    assert list(model.columns.keys()) == ["anonymous_id", "user_id", "created_at"]
    for column in model.columns:
        created = made.columns[column.name]
        assert type(created.type).__name__ == type(column.type).__name__, column.name
        assert getattr(created.type, "length", None) == getattr(
            column.type, "length", None
        ), column.name
        assert created.nullable == column.nullable, column.name
        assert _default_sql(created) == _default_sql(column), column.name
    assert made.columns["created_at"].type.timezone is True
    assert list(made.primary_key.columns.keys()) == ["anonymous_id"]
    assert list(model.primary_key.columns.keys()) == ["anonymous_id"]

    def _checks(table):
        return {
            (c.name, str(c.sqltext))
            for c in table.constraints
            if isinstance(c, sa.CheckConstraint)
        }

    assert (
        _checks(made)
        == _checks(model)
        == {("check_identity_aliases_ids_differ", "anonymous_id <> user_id")}
    )

    # No foreign key on either side: creating the table locks no other table.
    assert not made.foreign_keys
    assert not model.foreign_keys

    ((index_name, table, columns, kwargs),) = recorder.indexes
    (model_index,) = model.indexes
    assert index_name == model_index.name == "ix_identity_aliases_user_id"
    assert table == "identity_aliases"
    assert columns == [c.name for c in model_index.columns] == ["user_id"]
    assert kwargs["schema"] == model.schema
    assert not kwargs.get("unique") and not model_index.unique


def test_the_bound_is_a_quarter_second_set_first_and_reset_last(monkeypatch):
    recorder = _Recorder()
    module = _upgraded(monkeypatch, recorder)
    assert module._LOCK_TIMEOUT == "250ms"
    assert recorder.statements == [
        f"SET LOCAL lock_timeout = '{module._LOCK_TIMEOUT}'",
        "SET LOCAL lock_timeout TO DEFAULT",
    ]
    assert release("0.48.0").declared[REVISION] == 0.25


def test_the_lock_timeout_literal_lives_in_one_constant():
    """Nobody retypes the bound: the only ``250ms`` in the file is the
    constant's value, and the ``SET`` is built from the constant."""
    source = _path().read_text(encoding="utf-8")
    assert source.count('"250ms"') == 1
    assert "lock_timeout = '{_LOCK_TIMEOUT}'" in source
    tree = ast.parse(source)
    (constant,) = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(t, ast.Name) and t.id == "_LOCK_TIMEOUT" for t in node.targets
        )
    ]
    assert constant.value.value not in {"0", "0ms", "0s", ""}


def _operational_error(pgcode: str) -> sa.exc.OperationalError:
    return sa.exc.OperationalError("CREATE TABLE", {}, SimpleNamespace(pgcode=pgcode))


def test_a_lock_timeout_refuses_with_the_rerun_message(monkeypatch):
    recorder = _Recorder(fail_create=_operational_error("55P03"))
    with pytest.raises(RuntimeError) as raised:
        _upgraded(monkeypatch, recorder)
    message = str(raised.value)
    module = _module()
    assert message == module._lock_refusal()
    assert "identity_aliases" in message
    assert "250ms" in message
    assert "Nothing was applied" in message
    assert message.splitlines()[-1].endswith("Run the upgrade again.")
    # #238: never tell an operator to touch the version table.
    assert "alembic_version" not in message
    assert raised.value.__cause__ is None and raised.value.__suppress_context__


def test_any_other_error_is_raised_as_it_was(monkeypatch):
    error = _operational_error("57014")
    recorder = _Recorder(fail_create=error)
    with pytest.raises(sa.exc.OperationalError) as raised:
        _upgraded(monkeypatch, recorder)
    assert raised.value is error


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_offline_mode_is_refused(monkeypatch, direction):
    module = _module()
    recorder = _Recorder(as_sql=True)
    monkeypatch.setattr(module, "op", recorder)
    with pytest.raises(RuntimeError) as raised:
        getattr(module, direction)()
    assert str(raised.value) == module._OFFLINE_REFUSAL
    assert recorder.statements == [] and recorder.tables == []


#: The database test files of this table, and how many tests each defines.
DATABASE_TESTS = {
    "backend/tests/integration/database/test_identity_aliases_migration.py": 9,
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
