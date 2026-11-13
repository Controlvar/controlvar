"""``t1812_user_totp``, read without a database (#1812).

* Its id fits ``alembic_version.version_num`` (32 characters): a longer one
  passes every database-free test and fails every upgrade.
* It extends the core head it sits on, ``4955571d232c`` (#1545).
* Its downgrade calls ``drop_column``, so ``test_documented_downgrade_targets``
  sees it, and every documented downgrade target below it must name it with
  what it drops. Written as ``drop_table`` alone it would cross every
  documented target unannounced.
* The migration creates the model's columns with the model's types,
  nullability and server defaults: ``last_used_step`` and ``failed_attempts``
  are NOT NULL DEFAULT 0, never NULL, so no stored row can refuse every code.
* The database tests of this table are all collected and none is skipped:
  without PostgreSQL they error, so a skip would be the only way to look green
  without running them.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa

from backend.app.models.user_totp import UserTOTP
from backend.tests.unit.docs.test_documented_downgrade_targets import (
    _script,
    drops_a_column,
)

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REVISION = "t1812_user_totp"
REPO_ROOT = Path(__file__).resolve().parents[4]


def _path() -> Path:
    return Path(_script().get_revision(REVISION).path)


def test_it_extends_the_core_head_it_sits_on_and_fits_the_version_table():
    revision = _script().get_revision(REVISION)
    # Kept literal on purpose. A re-point changes this line, every CORE_HEAD
    # pin, the NAMED_DOWNGRADES lists in test_db_migrate_target_graph.py, the
    # sentences that give this revision's down_revision (migrations.md, the
    # rollback runbook, db-migrate.yml's help text) and the migration's own
    # docstring, together.
    assert revision.down_revision == "4955571d232c"
    assert revision.revision == REVISION
    assert len(revision.revision) <= 32


def test_the_downgrade_drops_columns_as_the_downgrade_docs_test_reads_it():
    assert drops_a_column(str(_path())) is True


class _Recorder:
    """Stands in for ``alembic.op`` and records what ``upgrade`` creates."""

    def __init__(self) -> None:
        self.tables = []

    def get_bind(self):  # pragma: no cover - replaced below
        raise AssertionError("no database here")

    def create_table(self, name, *items, **kwargs):
        self.tables.append((name, items, kwargs))


def _created_columns(monkeypatch):
    spec = importlib.util.spec_from_file_location("user_totp_revision", _path())
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    recorder = _Recorder()
    monkeypatch.setattr(module, "op", recorder)
    monkeypatch.setattr(module, "_has_table", lambda: False)
    module.upgrade()
    ((name, items, _),) = recorder.tables
    assert name == "user_totp"
    return {item.name: item for item in items if isinstance(item, sa.Column)}, items


def _default_sql(column) -> str:
    default = column.server_default
    return None if default is None else str(default.arg)


def test_the_migration_creates_the_models_columns(monkeypatch):
    created, items = _created_columns(monkeypatch)
    model = {c.name: c for c in UserTOTP.__table__.columns}
    assert list(created) == list(model)
    for name, column in model.items():
        made = created[name]
        assert type(made.type).__name__ == type(column.type).__name__, name
        assert made.nullable == column.nullable, name
        assert _default_sql(made) == _default_sql(column), name
    assert [c.name for c in UserTOTP.__table__.primary_key.columns] == ["user_id"]
    (fk,) = UserTOTP.__table__.foreign_keys
    assert fk.ondelete == "CASCADE" and fk.column.table.name == "users"
    assert any(
        isinstance(item, sa.ForeignKeyConstraint) and item.ondelete == "CASCADE"
        for item in items
    )


def test_the_counters_are_not_null_with_a_zero_default():
    """A NULL ``last_used_step`` would refuse every code (``step < NULL`` is
    never true); the wrong-code count starts at 0."""
    for name in ("last_used_step", "failed_attempts"):
        column = UserTOTP.__table__.columns[name]
        assert column.nullable is False, name
        assert _default_sql(column) == "0", name
    assert isinstance(UserTOTP.__table__.columns["last_used_step"].type, sa.BigInteger)


#: The database test files of this table, and how many tests each defines.
DATABASE_TESTS = {
    "backend/tests/integration/database/test_user_totp_migration.py": 5,
    "backend/tests/integration/services/test_two_factor_service.py": 36,
    "backend/tests/integration/services/test_two_factor_reset_cli.py": 8,
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
