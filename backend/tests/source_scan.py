"""Find where application code removes or rewrites stored rows of a model.

A test helper, not a test. ``row_changes`` reads one parsed module and lists
every place that deletes a row of the named models, or (with ``rewrites``)
changes one already stored:

* ``delete(Model)`` / ``update(Model)`` (SQLAlchemy's statements), and any
  ``.delete(...)`` / ``.update(...)`` / ``.merge(...)`` call given the model or
  a name read from the database through it;
* ``query(Model)....delete()`` / ``.update(...)``;
* a SQL string that deletes from (or updates) one of the named tables;
* with ``rewrites``, an assignment to an attribute of a row read from the
  database (``row.old_value = ...``).

"Read from the database" is a name assigned, in the same function (or at
module level), from an expression that names the model and calls ``query``,
``execute``, ``get``, ``scalars``, ``scalar``, ``first``, ``one``, ``all`` or
``select``, or from an expression naming such a name, or bound by a ``for``
over one. A row only constructed (``Model(...)``) is not one. Each module is
walked a bounded number of times (three passes to settle the names), so the
cost is linear in its size.
"""

from __future__ import annotations

import ast
from typing import Iterable, List, Set, Tuple

READS = {"query", "execute", "get", "scalars", "scalar", "first", "one", "all"}
REMOVERS = {"delete"}
REWRITERS = {"update", "merge"}


def _names(node: ast.AST) -> Set[str]:
    found = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            found.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            found.add(sub.attr)
    return found


def _reads(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            func = sub.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else getattr(func, "id", None)
            )
            if name in READS or name == "select":
                return True
    return False


def _targets(node: ast.AST) -> List[ast.AST]:
    if isinstance(node, (ast.Assign,)):
        return list(node.targets)
    if isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.For, ast.AsyncFor)):
        return [node.target]
    if isinstance(node, ast.withitem) and node.optional_vars is not None:
        return [node.optional_vars]
    return []


def _value(node: ast.AST):
    if isinstance(node, (ast.For, ast.AsyncFor)):
        return node.iter
    if isinstance(node, ast.withitem):
        return node.context_expr
    return getattr(node, "value", None)


def _scopes(tree: ast.AST) -> Iterable[ast.AST]:
    yield tree
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _rows_read(scope: ast.AST, models: Set[str]) -> Set[str]:
    rows: Set[str] = set()
    nodes = list(ast.walk(scope))
    for _ in range(3):
        for node in nodes:
            value = _value(node)
            if value is None or not _targets(node):
                continue
            named = _names(value)
            if (named & models and _reads(value)) or named & rows:
                for target in _targets(node):
                    for sub in ast.walk(target):
                        if isinstance(sub, ast.Name):
                            rows.add(sub.id)
    return rows


def row_changes(
    tree: ast.AST,
    models: Iterable[str],
    tables: Iterable[str],
    rewrites: bool = False,
) -> List[Tuple[int, str]]:
    """``(line, what)`` for each place *tree* removes (or rewrites) a stored row."""
    models = set(models)
    tables = {t.lower() for t in tables}
    verbs = REMOVERS | (REWRITERS if rewrites else set())
    sql = ("delete from", "update ") if rewrites else ("delete from",)
    found: List[Tuple[int, str]] = []
    for scope in _scopes(tree):
        rows = _rows_read(scope, models)
        watched = models | rows
        for node in ast.walk(scope):
            if isinstance(node, ast.Call):
                func = node.func
                name = (
                    func.attr
                    if isinstance(func, ast.Attribute)
                    else getattr(func, "id", None)
                )
                if name not in verbs:
                    continue
                args = list(node.args) + [k.value for k in node.keywords]
                if any(_names(a) & watched for a in args):
                    found.append((node.lineno, f"{name}() of a stored row"))
                elif isinstance(func, ast.Attribute) and _names(func.value) & models:
                    found.append((node.lineno, f"{name}() on a query of the model"))
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value[:10_000].lower()
                if any(v in text for v in sql) and any(t in text for t in tables):
                    found.append((node.lineno, "SQL that changes the table"))
            elif rewrites and isinstance(
                node, (ast.Assign, ast.AugAssign, ast.AnnAssign)
            ):
                for target in _targets(node):
                    if (
                        isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id in rows
                    ):
                        found.append(
                            (
                                node.lineno,
                                f"assignment to {target.value.id}.{target.attr}",
                            )
                        )
    return sorted(set(found))


def member(node: ast.AST, enum_name: str):
    """``X`` for an ``enum_name.X`` attribute, else None."""
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == enum_name
    ):
        return node.attr
    return None


def compliance_writes(tree: ast.AST):
    """Each call that names an ``AuditAction`` member: what it writes.

    ``(function, action, resource_type, outcome, call, function node)``, the
    resource type a string constant (``resource_type=`` or the argument after
    the action) or None, the outcome the ``AuditOutcome`` member passed as
    ``outcome=`` or None.
    """
    functions = {}
    owner = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions[fn.name] = fn
            for node in ast.walk(fn):
                owner.setdefault(node, fn.name)
    found = []
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call):
            continue
        values = list(call.args) + [k.value for k in call.keywords]
        actions = [a for a in (member(v, "AuditAction") for v in values) if a]
        if not actions:
            continue
        (action,) = actions
        keywords = {k.arg: k.value for k in call.keywords}
        resource = keywords.get("resource_type")
        if resource is None:
            position = [i for i, a in enumerate(call.args) if member(a, "AuditAction")][
                0
            ]
            resource = call.args[position + 1]
        name = owner.get(call)
        found.append(
            (
                name,
                action,
                resource.value if isinstance(resource, ast.Constant) else None,
                member(keywords.get("outcome"), "AuditOutcome"),
                call,
                functions.get(name),
            )
        )
    return found
