"""Find where application code removes or rewrites stored rows of a model.

A test helper, not a test. ``row_changes`` reads one parsed module and lists
these places, each a call or a string in that module:

* a call named ``delete`` (with ``rewrites``, also ``update`` or ``merge``),
  bare or as an attribute (``delete(...)``, ``db.delete(...)``,
  ``sa.delete(...)``), whose arguments name a watched model or a row read
  from the database through one;
* such a call made on an expression that names a watched model
  (``db.query(Model).filter(...).delete()``);
* a string that deletes from (with ``rewrites``, or updates) a watched table:
  a literal, an f-string with its replacement fields left out, or a ``+``
  chain of those with every other operand left out. Case and runs of
  whitespace do not matter;
* with ``rewrites``, an assignment to an attribute of a row read from the
  database (``row.old_value = ...``).

Names: ``import X as Y`` and ``from M import X as Y`` make ``Y`` stand for a
model or a verb ``X``, and so does an assignment of ``X`` or ``obj.X`` to
``Y`` (``=``, annotated, or ``:=``). A row "read from the database" is a name bound, in
the same function (or in the module's own top-level code), by an assignment,
a ``for``, a ``with`` or a comprehension from an expression that names a
watched model and calls ``query``, ``execute``, ``get``, ``scalars``,
``scalar``, ``first``, ``one``, ``all`` or ``select``, or from an expression
naming such a row. A row only constructed (``Model(...)``) is not one. Calls
inside comprehensions, lambdas and closures count like any other.

It does not follow names across modules, a table name kept in a variable,
a verb passed as a value (``map(db.delete, rows)``), ``getattr`` or
``exec``. The work is bounded: three passes to settle the names, one walk
of each function (closures included) and of the module's own code, a string
read up to its first 10,000 characters, and a ``+`` chain up to 10,000
operands, read once from its outermost ``+``.
"""

from __future__ import annotations

import ast
from typing import Dict, Iterable, List, Optional, Set, Tuple

READS = {"query", "execute", "get", "scalars", "scalar", "first", "one", "all"}
REMOVERS = {"delete"}
REWRITERS = {"update", "merge"}
LIMIT = 10_000


def _names(node: ast.AST) -> Set[str]:
    found = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            found.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            found.add(sub.attr)
    return found


def _called(func: ast.AST) -> Optional[str]:
    if isinstance(func, ast.Attribute):
        return func.attr
    return getattr(func, "id", None)


def _reads(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and _called(sub.func) in READS | {"select"}:
            return True
    return False


def _targets(node: ast.AST) -> List[ast.AST]:
    if isinstance(node, ast.Assign):
        return list(node.targets)
    if isinstance(
        node, (ast.AnnAssign, ast.AugAssign, ast.For, ast.AsyncFor, ast.comprehension)
    ):
        return [node.target]
    if isinstance(node, ast.NamedExpr):
        return [node.target]
    if isinstance(node, ast.withitem) and node.optional_vars is not None:
        return [node.optional_vars]
    return []


def _value(node: ast.AST):
    if isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
        return node.iter
    if isinstance(node, ast.withitem):
        return node.context_expr
    return getattr(node, "value", None)


FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


def _scopes(tree: ast.AST) -> Iterable[ast.AST]:
    yield tree
    for node in ast.walk(tree):
        if isinstance(node, FUNCTIONS):
            yield node


def _own_nodes(scope: ast.AST) -> List[ast.AST]:
    """The nodes of *scope*: a function's include its closures', which can
    use its names; the module's own exclude every function's, which cannot
    see a name a function binds."""
    if not isinstance(scope, ast.Module):
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


def _aliases(tree: ast.AST, models: Set[str], verbs: Set[str]):
    """Every name that stands for a watched model, and each verb's other names."""
    models = set(models)
    verb_of: Dict[str, str] = {}
    nodes = list(ast.walk(tree))
    for node in nodes:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                original = alias.name.rsplit(".", 1)[-1]
                if alias.asname and original in models:
                    models.add(alias.asname)
                elif alias.asname and original in verbs:
                    verb_of[alias.asname] = original
    for _ in range(3):
        for node in nodes:
            if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
                continue
            value = node.value
            if isinstance(value, ast.Name):
                original = value.id
            elif isinstance(value, ast.Attribute):
                original = value.attr
            else:
                continue
            for target in _targets(node):
                if not isinstance(target, ast.Name):
                    continue
                if original in models:
                    models.add(target.id)
                elif original in verbs or original in verb_of:
                    verb_of[target.id] = verb_of.get(original, original)
    return models, verb_of


def _concatenation(node: ast.AST) -> bool:
    return isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add)


def _string(node: ast.AST) -> Optional[str]:
    """The text of a literal, an f-string or a ``+`` chain, holes left out."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value[:LIMIT]
    if isinstance(node, ast.JoinedStr):
        parts = [
            v.value
            for v in node.values
            if isinstance(v, ast.Constant) and isinstance(v.value, str)
        ]
        return " ".join(parts)[:LIMIT]
    if _concatenation(node):
        parts, stack, size, steps = [], [node], 0, 0
        while stack and size < LIMIT and steps < LIMIT:
            steps += 1
            item = stack.pop()
            if _concatenation(item):
                stack.extend((item.right, item.left))
                continue
            text = _string(item) if not isinstance(item, ast.BinOp) else None
            if text:
                parts.append(text)
                size += len(text)
        return " ".join(parts)[:LIMIT] if parts else None
    return None


def _rows_read(scope: ast.AST, models: Set[str]) -> Set[str]:
    rows: Set[str] = set()
    nodes = _own_nodes(scope)
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
    tables = {t.lower() for t in tables}
    verbs = REMOVERS | (REWRITERS if rewrites else set())
    models, verb_of = _aliases(tree, set(models), verbs)
    sql = ("delete from", "update ") if rewrites else ("delete from",)
    # A ``+`` chain is read once, from its outermost ``+``.
    inner = {
        id(part)
        for node in ast.walk(tree)
        if _concatenation(node)
        for part in (node.left, node.right)
        if _concatenation(part)
    }
    found: List[Tuple[int, str]] = []
    for scope in _scopes(tree):
        rows = _rows_read(scope, models)
        watched = models | rows
        for node in _own_nodes(scope):
            if isinstance(node, ast.Call):
                name = _called(node.func)
                name = verb_of.get(name, name)
                if name not in verbs:
                    continue
                args = list(node.args) + [k.value for k in node.keywords]
                if any(_names(a) & watched for a in args):
                    found.append((node.lineno, f"{name}() of a stored row"))
                elif isinstance(node.func, ast.Attribute) and (
                    _names(node.func.value) & models
                ):
                    found.append((node.lineno, f"{name}() on a query of the model"))
            elif (
                isinstance(node, (ast.Constant, ast.JoinedStr, ast.BinOp))
                and id(node) not in inner
            ):
                text = _string(node)
                if text:
                    text = " ".join(text.lower().split())
                    if any(v in text for v in sql) and any(t in text for t in tables):
                        found.append((node.lineno, "SQL that changes the table"))
            if rewrites and isinstance(
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
            position = [member(a, "AuditAction") for a in call.args].index(action)
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
