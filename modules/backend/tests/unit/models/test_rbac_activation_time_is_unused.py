"""Nothing writes or reads the activation time of an assignment or a grant (#891).

``modules_0004_rbac_activation`` adds ``activated_at`` to ``user_custom_roles``
and ``direct_permission_grants``, and the models declare it.  That change
gives no access, so no code writes the column or reads it: it is referenced by
the two column definitions and by the migration, and nowhere else.  The
change that adds a writer or a reader changes :data:`EXPECTED` with it.

``global_holdouts.activated_at`` exists in about twenty files, so the scan does
not look for the bare name.  Over every ``.py`` file of ``backend/app`` and
``modules/backend/app`` (by ``ast``, docstrings skipped), it looks for:

* ``UserCustomRole.activated_at`` or ``DirectPermissionGrant.activated_at``:
  a query on the column;
* ``activated_at=`` in a call of either class: a writer;
* in a module that imports either class, any ``.activated_at`` and any
  ``activated_at=``: an instance read or write;
* ``activated_at = Column(...)`` in either class body: the two definitions;
* the text ``activated_at`` in a string of a module that imports either class
  or names either table: raw SQL, ``getattr``.

A migration file counts once, as itself.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

pytestmark = pytest.mark.unit

REPO = pathlib.Path(__file__).resolve().parents[5]
TREES = (REPO / "backend" / "app", REPO / "modules" / "backend" / "app")

CLASSES = frozenset({"UserCustomRole", "DirectPermissionGrant"})
TABLES = ("user_custom_roles", "direct_permission_grants")
COLUMN = "activated_at"

MODEL = "modules/backend/app/models/custom_role.py"
MIGRATION = "modules/backend/app/db/migrations/versions/modules_0004_rbac_activation.py"

#: Every reference there may be: the two definitions and the migration.  A
#: reader or a writer anywhere else makes the set grow.
EXPECTED = {
    (MODEL, "definition", "UserCustomRole.activated_at"),
    (MODEL, "definition", "DirectPermissionGrant.activated_at"),
    (MIGRATION, "migration", ""),
}


def _docstrings(module: ast.Module) -> set[int]:
    """The ``id`` of every docstring constant in *module*."""
    found = set()
    for node in ast.walk(module):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                found.add(id(body[0].value))
    return found


def _called_class(call: ast.Call) -> str | None:
    func = call.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
    return name if name in CLASSES else None


def references(path: str, source: str) -> set[tuple[str, str, str]]:
    """``(path, kind, detail)`` for each reference to the column in *source*."""
    module = ast.parse(source)
    imports_a_class = any(
        isinstance(node, ast.ImportFrom)
        and any(alias.name in CLASSES for alias in node.names)
        for node in ast.walk(module)
    )
    defines_a_class = any(
        isinstance(node, ast.ClassDef) and node.name in CLASSES
        for node in ast.walk(module)
    )
    names_a_table = any(table in source for table in TABLES)
    in_scope = imports_a_class or defines_a_class
    docstrings = _docstrings(module)

    found: set[tuple[str, str, str]] = set()
    for node in ast.walk(module):
        if isinstance(node, ast.ClassDef) and node.name in CLASSES:
            for statement in node.body:
                if isinstance(statement, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id == COLUMN
                    for target in statement.targets
                ):
                    found.add((path, "definition", f"{node.name}.{COLUMN}"))
        elif isinstance(node, ast.Attribute) and node.attr == COLUMN:
            if isinstance(node.value, ast.Name) and node.value.id in CLASSES:
                found.add((path, "query", ast.unparse(node)))
            elif in_scope:
                found.add((path, "attribute", ast.unparse(node)))
        elif isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg != COLUMN:
                    continue
                cls = _called_class(node)
                if cls or in_scope:
                    found.add((path, "keyword", f"{ast.unparse(node.func)}({COLUMN}=)"))
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and COLUMN in node.value
            and id(node) not in docstrings
            and (in_scope or names_a_table)
        ):
            found.add((path, "string", node.value[:80]))

    if "/db/migrations/versions/" in path and found:
        return {(path, "migration", "")}
    return found


def _scan() -> set[tuple[str, str, str]]:
    found: set[tuple[str, str, str]] = set()
    for tree in TREES:
        for file in sorted(tree.rglob("*.py")):
            relative = file.relative_to(REPO).as_posix()
            found |= references(relative, file.read_text(encoding="utf-8"))
    return found


def test_only_the_two_definitions_and_the_migration_reference_the_column():
    found = _scan()
    assert found == EXPECTED, (
        "the activation time is written or read outside its definitions and "
        f"migration: {sorted(found - EXPECTED)}; missing: {sorted(EXPECTED - found)}"
    )


# ---------------------------------------------------------------------------
# Positive controls: the scan reads what it says, and ignores the holdouts
# ---------------------------------------------------------------------------
_HOLDOUT = '''
from backend.app.models.global_holdout import GlobalHoldout


def active(db):
    """activated_at is read here, on a holdout."""
    return db.query(GlobalHoldout).filter(GlobalHoldout.activated_at.isnot(None))


def when(holdout):
    return holdout.activated_at, getattr(holdout, "activated_at")
'''

_READER_AND_WRITER = """
from sqlalchemy import text

from modules.backend.app.models.custom_role import UserCustomRole


def active(db):
    return db.query(UserCustomRole).filter(UserCustomRole.activated_at.isnot(None))


def when(assignment):
    return assignment.activated_at


def make(user_id, role_id, now):
    return UserCustomRole(user_id=user_id, role_id=role_id, activated_at=now)


def raw(db):
    return db.execute(text("SELECT activated_at FROM user_custom_roles"))
"""


def test_the_holdouts_activation_time_is_not_counted():
    module = ast.parse(_HOLDOUT)
    # The parser does see it, twice as an attribute ...
    assert (
        sum(
            isinstance(node, ast.Attribute) and node.attr == COLUMN
            for node in ast.walk(module)
        )
        == 2
    )
    # ... and the scan counts none of it.
    assert references("fixture.py", _HOLDOUT) == set()


def test_a_reader_a_writer_and_raw_sql_are_each_counted():
    assert references("fixture.py", _READER_AND_WRITER) == {
        ("fixture.py", "query", "UserCustomRole.activated_at"),
        ("fixture.py", "attribute", "assignment.activated_at"),
        ("fixture.py", "keyword", "UserCustomRole(activated_at=)"),
        ("fixture.py", "string", "SELECT activated_at FROM user_custom_roles"),
    }


def test_the_scan_reads_the_model():
    """Without this, a scan that read nothing would find nothing to refuse."""
    found = references(MODEL, (REPO / MODEL).read_text(encoding="utf-8"))
    assert {kind for _, kind, _ in found} == {"definition"}
    assert len(found) == 2
