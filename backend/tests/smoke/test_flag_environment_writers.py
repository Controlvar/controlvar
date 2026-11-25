"""A flag's staging and development settings are written in one place (#2011).

``test_audit_non_route_inventory.py`` classifies the statements that write
``FlagEnvironmentSetting`` outside the route modules, with the model as their
first argument, and says which route records each. Some ways to write the
table are not such a statement, and a route module is not read there. This
file reads every module of ``backend/app`` (and ``modules/backend/app`` when
it is present), the migrations apart, and refuses these:

* the model's table object, ``FlagEnvironmentSetting.__table__`` or
  ``.__mapper__``, whatever is done with it: an upsert on the table object is
  not seen as a write of the model;
* the table's name in any string that is not a docstring, SQL text above all.
  Only the model's ``__tablename__`` has it;
* another name for the model, which would hide a statement on it:
  ``import ... as``, or the model itself, alone or in a literal or a
  conditional, assigned, looped over or made a parameter's default;
* these changes of a stored row, but for the one statement in each of the
  service's two writers:
  - a statement that writes the model (``insert``, ``pg_insert``, ``update``
    or ``delete`` with it first), or a row constructed;
  - a query's ``update()`` or ``delete()``, on the model or on a name bound
    to such a query;
  - an attribute of a row set or deleted, with ``=``, ``+=``, ``del``, a
    ``for`` or ``with`` target, or ``setattr`` and ``delattr``;
  - a row passed to ``session.delete``, ``session.merge`` or
    ``flag_modified``;
  - ``bulk_update_mappings``, ``bulk_insert_mappings`` or
    ``bulk_save_objects`` given the model or a row.

A row, in the last four, is a name bound in the same function (for the
module's own code, in that code) by an assignment, a ``for``, a ``with`` or a
comprehension from an expression that names the model, calls ``setting_of``, ``settings_of``
or ``save_environment_setting``, or names such a row; a parameter annotated
with the model; or such an expression itself. A name bound from an expression
that only mentions a row counts too, so a write to an object that is not one
can be refused.

Not seen: a row handed to another function as a parameter with no annotation,
kept on an object (``self.row``) or answered by any other function; the table
reached through ``inspect``, or through ``metadata`` without its name; a name
built at run time (``getattr``, ``exec``); ``insert`` under a name of its own
other than ``pg_insert``; ``set_attribute`` and the other functions of
``sqlalchemy.orm.attributes`` but ``flag_modified``; a table name put together
from parts; and a module outside those two trees.

The source is read from this file's own path (no git), so the check runs the
same in core-build's copy of the tree.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterable, Iterator, List, Set, Tuple

import pytest
from sqlalchemy import inspect

from backend.app.models.feature_flag import FeatureFlag, FlagEnvironmentSetting
from backend.tests.smoke.test_audit_non_route_inventory import statement_kind
from backend.tests.source_scan import row_changes

pytestmark = [pytest.mark.smoke, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[3]
TREES = (REPO_ROOT / "backend" / "app", REPO_ROOT / "modules" / "backend" / "app")
MIGRATIONS = "/db/migrations/"

MODEL = "FlagEnvironmentSetting"
TABLE = "flag_environment_settings"
MODEL_FILE = "backend/app/models/feature_flag.py"
SERVICE = "backend/app/services/flag_environment_service.py"

#: The service's writers: (function, what it does).
WRITERS = {
    ("save_environment_setting", f"{TABLE} insert"),
    ("turn_off_environment_settings", f"{TABLE} update"),
}

#: The service's functions that answer stored rows.
HAND_OUT = {"setting_of", "settings_of", "save_environment_setting"}

#: The calls that write the rows, or the model's rows, they are given.
GIVEN = {
    "bulk_update_mappings",
    "bulk_insert_mappings",
    "bulk_save_objects",
    "delete",
    "merge",
    "flag_modified",
}

FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


def _modules() -> Iterator[Tuple[str, ast.Module]]:
    for root in TREES:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            rel = path.relative_to(REPO_ROOT).as_posix()
            if MIGRATIONS not in rel:
                yield rel, ast.parse(path.read_text(encoding="utf-8"), filename=rel)


def _docstrings(tree: ast.Module) -> Set[int]:
    """The ids of the string nodes that are a statement of their own: the
    docstrings, and any other text nothing reads."""
    return {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }


def _last(node: ast.AST):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def table_objects(tree: ast.Module) -> List[int]:
    """The lines that read the model's table object, or the mapper it is on."""
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr in ("__table__", "__mapper__")
        and _last(node.value) == MODEL
    ]


def table_names(tree: ast.Module) -> List[int]:
    """The lines of the strings that name the table, docstrings apart."""
    docstrings = _docstrings(tree)
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and TABLE in node.value.lower()
        and id(node) not in docstrings
    ]


#: The expressions a bound value can be the model through.
HOLDERS = (ast.Tuple, ast.List, ast.Set, ast.Dict, ast.IfExp, ast.BoolOp, ast.Starred)


def _holds_the_model(value: ast.AST) -> bool:
    """Whether *value* is the model, or a literal or a conditional with it in."""
    if _last(value) == MODEL:
        return True
    return isinstance(value, HOLDERS) and any(
        _holds_the_model(child) for child in ast.iter_child_nodes(value)
    )


def other_names(tree: ast.Module) -> List[int]:
    """The lines that give the model another name."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name.rsplit(".", 1)[-1] == MODEL and alias.asname:
                    found.append(node.lineno)
            continue
        _, value = _bound(node)
        if value is not None and _holds_the_model(value):
            found.append(value.lineno)
        elif isinstance(node, ast.arguments):
            found += [
                default.lineno
                for default in [*node.defaults, *node.kw_defaults]
                if default is not None and _holds_the_model(default)
            ]
    return found


# --- the rows a function holds, and what it does with them -------------------------


def _mentions(expression: ast.AST, names: Set[str]) -> bool:
    """Whether *expression* names the model or one of *names*, or calls a
    function of the service that answers rows."""
    for node in ast.walk(expression):
        if isinstance(node, ast.Name) and (node.id == MODEL or node.id in names):
            return True
        if isinstance(node, ast.Attribute) and node.attr == MODEL:
            return True
        if isinstance(node, ast.Call) and _last(node.func) in HAND_OUT:
            return True
    return False


def _bound(node: ast.AST):
    """``(targets, value)`` of a node that binds names, or ``([], None)``."""
    if isinstance(node, ast.Assign):
        return node.targets, node.value
    if isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
        return [node.target], node.value
    if isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
        return [node.target], node.iter
    if isinstance(node, ast.withitem) and node.optional_vars is not None:
        return [node.optional_vars], node.context_expr
    return [], None


def _own_nodes(scope: ast.AST) -> List[ast.AST]:
    """A function's nodes, its closures' included; the module's own code, every
    function left out."""
    if isinstance(scope, FUNCTIONS):
        return list(ast.walk(scope))
    nodes, stack = [], [scope]
    while stack:
        node = stack.pop()
        nodes.append(node)
        stack.extend(
            child
            for child in ast.iter_child_nodes(node)
            if not isinstance(child, FUNCTIONS)
        )
    return nodes


def _row_names(nodes: Iterable[ast.AST]) -> Set[str]:
    """The names these nodes bind to a stored row, or to what mentions one."""
    rows: Set[str] = {
        node.arg
        for node in nodes
        if isinstance(node, ast.arg)
        and node.annotation is not None
        and (_mentions(node.annotation, set()) or _quoted(node.annotation))
    }
    for _ in range(3):  # a name bound from a name bound from a row
        for node in nodes:
            targets, value = _bound(node)
            if value is None or not _mentions(value, rows):
                continue
            for target in targets:
                rows |= {
                    sub.id
                    for sub in ast.walk(target)
                    if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store)
                }
    return rows


def _quoted(annotation: ast.AST) -> bool:
    """Whether a string inside *annotation* names the model."""
    return any(
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and MODEL in node.value
        for node in ast.walk(annotation)
    )


def _changed_row(node: ast.AST, rows: Set[str]):
    """How *node* changes a stored row, or ``None``."""
    if isinstance(node, ast.Attribute):
        if isinstance(node.ctx, (ast.Store, ast.Del)) and _mentions(node.value, rows):
            return f"assignment to {ast.unparse(node)}"
        return None
    if not isinstance(node, ast.Call):
        return None
    name = _last(node.func)
    receiver = node.func.value if isinstance(node.func, ast.Attribute) else None
    given = list(node.args) + [keyword.value for keyword in node.keywords]
    if name in ("setattr", "delattr", "__setattr__", "__delattr__"):
        # ``setattr(row, ...)``, ``object.__setattr__(row, ...)`` and
        # ``row.__setattr__(...)``.
        if any(_mentions(one, rows) for one in [receiver, *given[:1]] if one):
            return f"{name} on {ast.unparse(given[0] if given else receiver)}"
    if name in GIVEN and any(_mentions(one, rows) for one in given):
        return f"{name}() of a stored row"
    if name in ("update", "delete") and receiver is not None:
        if _mentions(receiver, rows):
            return f"{name}() of a stored row"
    return None


def writes(tree: ast.Module) -> Set[Tuple[str, str]]:
    """(function, what) for each write of the model in *tree*."""
    owners = {}
    for function in ast.walk(tree):
        if isinstance(function, FUNCTIONS):
            for node in ast.walk(function):
                # The innermost function: a later walk of a nested one wins.
                owners[getattr(node, "lineno", 0)] = function.name
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            kind = statement_kind(node)
            if kind and (kind.startswith(TABLE) or kind == f"{MODEL}()"):
                found.add((owners.get(node.lineno, "<module>"), kind))
    for line, what in row_changes(tree, {MODEL}, {TABLE}, rewrites=True):
        function = owners.get(line, "<module>")
        # ``update(Model)`` is both: a statement on the model, listed above.
        statement = (function, f"{TABLE} update")
        if what != "update() of a stored row" or statement not in found:
            found.add((function, what))
    for scope in ast.walk(tree):
        if isinstance(scope, (ast.Module, *FUNCTIONS)):
            nodes = _own_nodes(scope)
            rows = _row_names(nodes)
            for node in nodes:
                what = _changed_row(node, rows)
                if what:
                    found.add((owners.get(node.lineno, "<module>"), what))
    return found


# --- the pins -------------------------------------------------------------------


def test_the_model_and_the_service_are_where_this_file_says():
    files = dict(_modules())
    assert {MODEL_FILE, SERVICE} <= set(files)
    assert len(files) > 200, "the walk sees too few modules"
    functions = {n.name for n in ast.walk(files[SERVICE]) if isinstance(n, FUNCTIONS)}
    assert HAND_OUT <= functions


def test_the_flag_model_has_no_relationship_to_the_settings():
    """A row reached through one would not be a row to the checks above."""
    assert list(inspect(FlagEnvironmentSetting).relationships) == []
    of_a_flag = inspect(FeatureFlag).relationships
    assert len(of_a_flag) > 0
    assert FlagEnvironmentSetting not in {one.mapper.class_ for one in of_a_flag}


def test_nothing_reads_the_table_object():
    found = [(rel, line) for rel, tree in _modules() for line in table_objects(tree)]
    assert found == [], found


def test_only_the_model_names_the_table():
    found = [(rel, line) for rel, tree in _modules() for line in table_names(tree)]
    assert [rel for rel, _ in found] == [MODEL_FILE], found


def test_the_model_has_one_name():
    found = [(rel, line) for rel, tree in _modules() for line in other_names(tree)]
    assert found == [], found


def test_the_settings_are_written_only_by_the_services_two_writers():
    found = {
        (rel, function, what)
        for rel, tree in _modules()
        for function, what in writes(tree)
    }
    expected = {(SERVICE, function, what) for function, what in WRITERS}
    assert found == expected, (
        f"another writer: {sorted(found - expected)}; gone: {sorted(expected - found)}"
    )


# --- each check fires on what it looks for ----------------------------------------

#: A module that only reads the model: every check passes on it.
READS = (
    '"""Reads flag_environment_settings."""\n'
    "from backend.app.models.feature_flag import FlagEnvironmentSetting\n"
    "from backend.app.services.flag_environment_service import setting_of, settings_of\n"
    "def read(db):\n"
    '    """The flag_environment_settings rows."""\n'
    "    rows = db.query(FlagEnvironmentSetting).all()\n"
    "    return {row.environment: row.is_active for row in rows}\n"
    "def read_one(self, db, flag, before: FlagEnvironmentSetting):\n"
    "    row = setting_of(db, flag.id, 'staging')\n"
    "    rows = settings_of(db, [flag.id])\n"
    "    flag.status = 'ACTIVE'\n"
    "    self.db = db\n"
    "    seen = {}\n"
    "    for key, one in rows.items():\n"
    "        seen[key] = one.rollout_percentage\n"
    "    db.refresh(row)\n"
    "    db.add(flag)\n"
    "    return before.is_active, row.is_active if row else None, seen\n"
)


def test_a_module_that_only_reads_passes_every_check():
    tree = ast.parse(READS)
    for check in (table_objects, table_names, other_names):
        assert check(tree) == [], check.__name__
    assert writes(tree) == set()


@pytest.mark.parametrize(
    "line, check",
    [
        # The forms an inventory of statements on the model cannot see.
        (
            "db.execute(pg_insert(FlagEnvironmentSetting.__table__).values(x=1)"
            ".on_conflict_do_update(set_={'rollout_percentage': 100}))",
            table_objects,
        ),
        ("table = models.FlagEnvironmentSetting.__table__", table_objects),
        (
            "db.execute(FlagEnvironmentSetting.__mapper__.local_table.insert())",
            table_objects,
        ),
        (
            "db.execute(text('UPDATE flag_environment_settings SET is_active = true'))",
            table_names,
        ),
        (
            "db.execute(text(f'UPDATE {schema}.flag_environment_settings SET x = 1'))",
            table_names,
        ),
        ("table = Table('FLAG_ENVIRONMENT_SETTINGS', metadata)", table_names),
        ("Setting = FlagEnvironmentSetting", other_names),
        ("S, x = FlagEnvironmentSetting, 1; db.execute(update(S))", other_names),
        ("S = FlagEnvironmentSetting if db else FeatureFlag", other_names),
        ("for S in (FlagEnvironmentSetting, X): db.execute(update(S))", other_names),
        ("MODELS = {'x': FlagEnvironmentSetting}", other_names),
        ("def inner(S=FlagEnvironmentSetting): db.execute(update(S))", other_names),
        (
            "from backend.app.models.feature_flag import FlagEnvironmentSetting as S",
            other_names,
        ),
        # Statements on the model, and a row constructed.
        (
            "db.execute(update(FlagEnvironmentSetting).values(is_active=True))",
            writes,
        ),
        ("db.execute(pg_insert(FlagEnvironmentSetting).values(x=1))", writes),
        ("db.execute(sa.delete(FlagEnvironmentSetting))", writes),
        ("db.add(FlagEnvironmentSetting(environment='staging'))", writes),
        # A query's, on the model or on a name bound to it.
        ("db.query(FlagEnvironmentSetting).update({'is_active': True})", writes),
        ("db.query(FlagEnvironmentSetting).delete()", writes),
        ("q = db.query(FlagEnvironmentSetting); q.update({'is_active': True})", writes),
        ("q = db.query(FlagEnvironmentSetting); q.filter(x).delete()", writes),
        # A row read through the model.
        ("row = db.query(FlagEnvironmentSetting).first(); db.delete(row)", writes),
        (
            "row = db.query(FlagEnvironmentSetting).first(); row.is_active = True",
            writes,
        ),
        ("row = db.get(FlagEnvironmentSetting, 1); row.is_active = True", writes),
        (
            "for row in db.query(FlagEnvironmentSetting): setattr(row, 'x', True)",
            writes,
        ),
        # A row the service's functions answer.
        ("row = setting_of(db, 1, 'staging'); row.is_active = True", writes),
        ("row = service.setting_of(db, 1, 'staging'); row.is_active = True", writes),
        ("row = setting_of(db, 1, 'staging'); row.rollout_percentage += 1", writes),
        ("row = setting_of(db, 1, 'staging'); setattr(row, 'x', True)", writes),
        (
            "row = setting_of(db, 1, 'staging'); object.__setattr__(row, 'x', True)",
            writes,
        ),
        ("row = setting_of(db, 1, 'staging'); row.__setattr__('x', True)", writes),
        ("row = setting_of(db, 1, 'staging'); delattr(row, 'x')", writes),
        ("row = setting_of(db, 1, 'staging'); del row.is_active", writes),
        ("row = setting_of(db, 1, 'staging'); db.delete(row)", writes),
        ("row = setting_of(db, 1, 'staging'); db.merge(row)", writes),
        ("setting_of(db, 1, 'staging').is_active = True", writes),
        ("db.delete(setting_of(db, 1, 'staging'))", writes),
        ("row = other = setting_of(db, 1, 'staging'); other.is_active = True", writes),
        ("row = setting_of(db, 1, 'staging'); two = row; two.is_active = True", writes),
        (
            "if db:\n        row = setting_of(db, 1, 'staging')\n"
            "    two = row; two.is_active = True",
            writes,
        ),
        ("if (row := setting_of(db, 1, 'staging')): row.is_active = True", writes),
        (
            "row, x = setting_of(db, 1, 'staging'), 1; row.is_active, x = True, 2",
            writes,
        ),
        ("row = setting_of(db, 1, 'staging'); row.is_active: bool = True", writes),
        ("row = setting_of(db, 1, 'x'); flag_modified(row, 'targeting_rules')", writes),
        ("for row in settings_of(db, [1]).values(): row.is_active = True", writes),
        ("for key, row in settings_of(db, [1]).items(): row.is_active = True", writes),
        ("rows = settings_of(db, [1]); rows[(1, 'staging')].is_active = True", writes),
        ("rows = settings_of(db, [1]); rows.get((1, 'x')).is_active = True", writes),
        ("[setattr(r, 'x', True) for r in settings_of(db, [1]).values()]", writes),
        (
            "row = FlagEnvironmentService(db).save_environment_setting(flag, 'x', {});"
            " row.is_active = True",
            writes,
        ),
        ("def inner(row: FlagEnvironmentSetting): row.is_active = True", writes),
        (
            "def inner(row: Optional['FlagEnvironmentSetting']): row.is_active = True",
            writes,
        ),
        # The session's bulk methods.
        ("db.bulk_update_mappings(FlagEnvironmentSetting, [{'id': 1}])", writes),
        ("db.bulk_insert_mappings(FlagEnvironmentSetting, [{'x': 1}])", writes),
        ("db.bulk_insert_mappings(models.FlagEnvironmentSetting, rows)", writes),
        ("db.bulk_update_mappings(mapper=FlagEnvironmentSetting, mappings=[])", writes),
        ("db.bulk_save_objects(settings_of(db, [1]).values())", writes),
        ("rows = settings_of(db, [1]); db.bulk_save_objects(rows.values())", writes),
        (
            "rows = db.query(FlagEnvironmentSetting).all(); db.bulk_save_objects(rows)",
            writes,
        ),
    ],
)
def test_each_check_finds_its_form(line, check):
    tree = ast.parse(READS + f"def write(db, schema):\n    {line}\n")
    assert check(tree), line


def test_a_write_in_a_route_is_named_with_its_function():
    """The name a reader of the failure needs, in a closure too."""
    tree = ast.parse(
        READS + "def route(db):\n"
        "    row = setting_of(db, 1, 'staging')\n"
        "    def inner():\n"
        "        row.is_active = True\n"
        "    inner()\n"
    )
    assert writes(tree) == {("inner", "assignment to row.is_active")}
