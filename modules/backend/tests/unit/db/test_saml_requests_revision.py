"""``modules_0006_saml_requests``, read without a database (#123).

* Its id fits ``alembic_version.version_num`` (32 characters), its
  ``down_revision`` is the literal ``modules_0005_redshift``, and it is the
  modules branch's head.
* The migration creates the model's columns with the model's types,
  nullability and server defaults, the model's primary key and index, and no
  foreign key; it marks the primary key with its COMMENT only when it creates
  the table.
* The one bound: ``_LOCK_TIMEOUT`` is ``250ms`` (never 0), the ``SET`` is the
  revision's first statement and the reset its last, and 0.49.0's budget map
  declares the same quarter second.
* A lock timeout (SQLSTATE 55P03) becomes this revision's refusal, which names
  the table and the bound, says nothing was applied and says to run the
  upgrade again; any other error is raised as it was.
* Offline mode is refused in both directions, each with its own reason.
* The database tests of this table are all collected and none is skipped.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from backend.tests.unit.db.test_upgrade_lock_budget import _script, release
from modules.backend.app.models.sso_saml_request import SSOSAMLRequest

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REVISION = "modules_0006_saml_requests"
REPO_ROOT = Path(__file__).resolve().parents[5]


def _path() -> Path:
    return Path(_script().get_revision(REVISION).path)


def _module():
    spec = importlib.util.spec_from_file_location("saml_requests_revision", _path())
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_it_extends_modules_0005_redshift_and_fits_the_version_table():
    revision = _script().get_revision(REVISION)
    # Kept literal on purpose: a re-point changes this line, every
    # MODULES_HEAD pin and the documented downgrade target together.
    assert revision.down_revision == "modules_0005_redshift"
    assert revision.revision == REVISION
    assert len(revision.revision) <= 32


def test_it_is_the_modules_branch_head():
    heads = set(_script().get_heads())
    assert REVISION in heads
    assert len(heads) == 2, heads


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

    def execute(self, statement):
        self.statements.append(str(statement))

    def create_table(self, name, *items, **kwargs):
        if self._fail_create is not None:
            raise self._fail_create
        self.tables.append((name, items, kwargs))

    def create_index(self, name, table, columns, **kwargs):
        self.indexes.append((name, table, columns, kwargs))

    def drop_table(self, *args, **kwargs):  # pragma: no cover - not reached
        raise AssertionError("the downgrade must refuse offline mode first")


def _upgraded(monkeypatch, recorder: _Recorder, exists=lambda name, kind: False):
    module = _module()
    monkeypatch.setattr(module, "op", recorder)
    monkeypatch.setattr(module, "_relation_exists", exists)
    module.upgrade()
    return module


def _default_sql(column):
    default = column.server_default
    return None if default is None else str(default.arg)


def test_the_migration_creates_the_models_table(monkeypatch):
    recorder = _Recorder()
    _upgraded(monkeypatch, recorder)
    ((name, items, kwargs),) = recorder.tables
    assert name == "sso_saml_requests" == SSOSAMLRequest.__tablename__
    made = sa.Table(name, sa.MetaData(), *items, schema=kwargs["schema"])
    model = SSOSAMLRequest.__table__
    assert made.schema == model.schema
    assert list(made.columns.keys()) == list(model.columns.keys())
    assert list(model.columns.keys()) == [
        "id",
        "config_id",
        "return_to",
        "handoff_hash",
        "created_at",
        "expires_at",
        "used_at",
    ]
    for column in model.columns:
        created = made.columns[column.name]
        assert type(created.type).__name__ == type(column.type).__name__, column.name
        assert getattr(created.type, "length", None) == getattr(
            column.type, "length", None
        ), column.name
        assert getattr(created.type, "timezone", None) == getattr(
            column.type, "timezone", None
        ), column.name
        assert created.nullable == column.nullable, column.name
        assert _default_sql(created) == _default_sql(column), column.name
    assert list(made.primary_key.columns.keys()) == ["id"]
    assert list(model.primary_key.columns.keys()) == ["id"]
    assert made.primary_key.name == "sso_saml_requests_pkey"

    # No foreign key on either side: creating the table locks no other table.
    assert not made.foreign_keys
    assert not model.foreign_keys

    ((index_name, table, columns, kwargs),) = recorder.indexes
    (model_index,) = model.indexes
    assert index_name == model_index.name == "ix_sso_saml_requests_expires_at"
    assert table == "sso_saml_requests"
    assert columns == [c.name for c in model_index.columns] == ["expires_at"]
    assert kwargs["schema"] == model.schema
    assert not kwargs.get("unique") and not model_index.unique


def test_the_bound_is_a_quarter_second_set_first_and_reset_last(monkeypatch):
    recorder = _Recorder()
    module = _upgraded(monkeypatch, recorder)
    assert module._LOCK_TIMEOUT == "250ms"
    assert (
        recorder.statements[0] == f"SET LOCAL lock_timeout = '{module._LOCK_TIMEOUT}'"
    )
    assert recorder.statements[-1] == "SET LOCAL lock_timeout TO DEFAULT"
    assert release("0.49.0").declared[REVISION] == 0.25


def test_the_comment_marks_the_table_only_when_the_revision_creates_it(monkeypatch):
    recorder = _Recorder()
    module = _upgraded(monkeypatch, recorder)
    comments = [s for s in recorder.statements if s.startswith("COMMENT ON")]
    assert comments == [
        f'COMMENT ON CONSTRAINT "sso_saml_requests_pkey" ON '
        f'"{module._SCHEMA}"."sso_saml_requests" IS \'{module._CREATED_BY}\''
    ]

    present = _Recorder()
    _upgraded(monkeypatch, present, exists=lambda name, kind: True)
    assert present.tables == [] and present.indexes == []
    assert present.statements == [
        f"SET LOCAL lock_timeout = '{module._LOCK_TIMEOUT}'",
        "SET LOCAL lock_timeout TO DEFAULT",
    ]


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
    assert "sso_saml_requests" in message
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


@pytest.mark.parametrize(
    ("direction", "refusal"),
    [("upgrade", "_OFFLINE_REFUSAL"), ("downgrade", "_OFFLINE_DOWNGRADE_REFUSAL")],
)
def test_offline_mode_is_refused(monkeypatch, direction, refusal):
    """Each direction with its own reason: only the upgrade reads the catalog."""
    module = _module()
    recorder = _Recorder(as_sql=True)
    monkeypatch.setattr(module, "op", recorder)
    with pytest.raises(RuntimeError) as raised:
        getattr(module, direction)()
    assert str(raised.value) == getattr(module, refusal)
    assert ("reads the catalog" in str(raised.value)) == (direction == "upgrade")
    assert recorder.statements == [] and recorder.tables == []


#: The application files that may name the table or its model: the model, the
#: revision, the model's registration and the core list of module tables.
NAMING_FILES = {
    "modules/backend/app/models/sso_saml_request.py",
    "modules/backend/app/db/migrations/versions/modules_0006_saml_requests.py",
    "modules/backend/app/register.py",
    "backend/app/db/autogenerate_filters.py",
}


def test_nothing_reads_or_writes_the_table_yet():
    """The migrations page says nothing reads or writes the table yet. The
    change that first does adds its file to :data:`NAMING_FILES` and changes
    that sentence."""
    named = set()
    for package in ("backend/app", "modules/backend/app"):
        for path in sorted((REPO_ROOT / package).rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            if "sso_saml_request" in source or "SSOSAMLRequest" in source:
                named.add(str(path.relative_to(REPO_ROOT)))
    assert named == NAMING_FILES


#: The database test files of this table, and how many tests each defines.
DATABASE_TESTS = {
    "backend/tests/integration/database/test_modules_0006_transitions.py": 8,
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
