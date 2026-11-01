"""
Logger calls on the rule-evaluation path may carry only an allow-listed set of arguments.

Evaluating targeting rules handles a user's attribute values on every flag
evaluation and every assignment. Those values, and the text of any exception
raised while handling them (a parse error repeats its input), must never reach
a log line, at any level. The marker test
(``test_evaluation_logs_no_attribute_values.py``) drives one case per operator;
this gate covers every call site, including the branches no case reaches.

In the modules below, every call ``<receiver>.<level>(...)`` for a logging
level must be ``logger.<level>`` with:

* a first argument that is a plain string literal (no f-string, no ``%`` or
  ``+`` or ``.format`` built in place);
* further arguments drawn only from: literals; ``type(<anything>).__name__``;
  the names in ``ALLOWED_NAMES``; the attribute chains in ``ALLOWED_ATTRIBUTES``;
* no keyword arguments (``exc_info`` logs the exception's text, ``extra`` can
  carry anything);
* never ``logger.exception``.

An allowed name is only as good as what it is bound to: where one is used, it
must be a parameter of the enclosing function and never rebound there, or --
``operator`` only -- be bound there only from an ``<x>.operator`` attribute.
``rule.id`` is allowed only where ``rule`` is bound, in the enclosing
function, solely as the loop variable of ``for rule in <x>.rules``.
Only the once-helpers may take a ``reason`` parameter, and every call of them
is checked: ``EVALUATION_NOTES.first(owner, reason)``,
``_note_rule_problem(operator, reason)`` and ``check_time_window``'s
``note(operator, reason)`` (``_note_rule_problem`` unless the write path
passes one that logs nothing) take an allowed owner and a string-literal
reason. The callers that pass ``owner`` pass an identifier they
own (``flag:<key>``, ``experiment:<id>``).
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterator, List, Tuple

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[4]

EVALUATION_MODULES = (
    "backend/app/core/rules_engine.py",
    "backend/app/core/targeting_adapter.py",
    "backend/app/services/rules_evaluation_service.py",
)

LEVELS = frozenset(
    {"debug", "info", "warning", "warn", "error", "critical", "fatal", "log"}
)

#: The operator (rule definition), a caller-owned owner/where, a fixed reason
#: (checked at every call of the helpers that take one) and a rule id.
ALLOWED_NAMES = frozenset({"operator", "owner", "where", "reason", "rule_id"})

ALLOWED_ATTRIBUTES = frozenset({("rule", "id"), ("OperatorType", "*")})

#: Calls whose second positional argument is a once-key reason and must be a
#: string literal, and whose first must be an allowed owner. ``note`` is
#: ``check_time_window``'s reporter, ``_note_rule_problem`` by default (#1204).
ONCE_KEY_CALLS = frozenset({"first", "_note_rule_problem", "note"})


def _attribute_chain(node: ast.AST) -> Tuple[str, ...]:
    parts: List[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return tuple(reversed(parts))
    return ()


def _is_type_name(node: ast.AST) -> bool:
    """``type(<x>).__name__``."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "__name__"
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "type"
        and len(node.value.args) == 1
        and not node.value.keywords
    )


def _allowed_owner(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return True
    if isinstance(node, ast.Name):
        return node.id in ALLOWED_NAMES
    chain = _attribute_chain(node)
    if len(chain) == 2:
        return chain in ALLOWED_ATTRIBUTES or (chain[0], "*") in ALLOWED_ATTRIBUTES
    return False


def _allowed_argument(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if _is_type_name(node):
        return True
    return _allowed_owner(node)


def _level_calls(tree: ast.AST) -> Iterator[ast.Call]:
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and (node.func.attr in LEVELS or node.func.attr == "exception")
        ):
            yield node


def _once_calls(tree: ast.AST) -> Iterator[ast.Call]:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name in ONCE_KEY_CALLS:
            yield node


def violations(source: str, filename: str = "<source>") -> List[str]:
    """Every breach of the contract in ``source``, as ``line: reason``."""
    tree = ast.parse(source, filename)
    found: List[str] = []

    for call in _level_calls(tree):
        where = f"{filename}:{call.lineno}"
        func = call.func
        if func.attr == "exception":
            found.append(f"{where}: logger.exception logs the exception's text")
            continue
        if not (isinstance(func.value, ast.Name) and func.value.id == "logger"):
            found.append(f"{where}: .{func.attr}() on something other than `logger`")
            continue
        if call.keywords:
            names = ", ".join(str(k.arg) for k in call.keywords)
            found.append(f"{where}: keyword arguments ({names})")
        args = list(call.args)
        if func.attr == "log" and args:
            args = args[1:]  # the level
        if not args:
            found.append(f"{where}: no message")
            continue
        message, rest = args[0], args[1:]
        if not (isinstance(message, ast.Constant) and isinstance(message.value, str)):
            found.append(
                f"{where}: the message is {type(message).__name__}, not a string literal"
            )
        for arg in rest:
            if isinstance(arg, ast.Starred) or not _allowed_argument(arg):
                found.append(f"{where}: argument `{ast.unparse(arg)}` is not allowed")

    for call in _once_calls(tree):
        where = f"{filename}:{call.lineno}"
        if len(call.args) != 2 or call.keywords:
            found.append(f"{where}: a once-key takes exactly (owner, reason)")
            continue
        owner, reason = call.args
        if not _allowed_owner(owner):
            found.append(f"{where}: once-key owner `{ast.unparse(owner)}`")
        if not (isinstance(reason, ast.Constant) and isinstance(reason.value, str)):
            # _note_rule_problem forwards its own `reason` parameter to first().
            if not (isinstance(reason, ast.Name) and reason.id == "reason"):
                found.append(f"{where}: once-key reason `{ast.unparse(reason)}`")

    found.extend(_binding_violations(tree, filename))
    return found


def _bound_names(scope: ast.AST) -> Iterator[Tuple[str, ast.AST, int]]:
    """``(name, bound value or statement, line)`` for every binding in ``scope``."""
    for node in ast.walk(scope):
        pairs: List[Tuple[ast.AST, ast.AST]] = []
        if isinstance(node, ast.Assign):
            pairs = [(target, node.value) for target in node.targets]
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value:
            pairs = [(node.target, node.value)]
        elif isinstance(node, (ast.AugAssign, ast.For, ast.AsyncFor)):
            pairs = [(node.target, node)]
        elif isinstance(node, ast.comprehension):
            pairs = [(node.target, node.iter)]
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            pairs = [(i.optional_vars, node) for i in node.items if i.optional_vars]
        elif isinstance(node, ast.ExceptHandler) and node.name:
            yield node.name, node, node.lineno
        for target, value in pairs:
            for name in ast.walk(target):
                if isinstance(name, ast.Name):
                    yield name.id, value, name.lineno


def _rule_id_violations(call: ast.Call, function, filename: str) -> List[str]:
    """
    ``rule.id`` is allowed only where ``rule`` is the loop variable of a
    ``for rule in <x>.rules`` in the enclosing function, and bound nowhere else
    there (not a parameter, not assigned).
    """
    uses = [arg for arg in call.args if _attribute_chain(arg) == ("rule", "id")]
    if not uses:
        return []
    where = f"{filename}:{call.lineno}"
    if function is None:
        return [f"{where}: `rule.id` used outside a function"]
    params = {
        a.arg
        for a in function.args.posonlyargs
        + function.args.args
        + function.args.kwonlyargs
    }
    if "rule" in params:
        return [f"{where}: `rule` in `rule.id` is a parameter of {function.name}"]
    bindings = [v for n, v, _ in _bound_names(function) if n == "rule"]
    loops_over_rules = [
        v
        for v in bindings
        if isinstance(v, (ast.For, ast.AsyncFor))
        and isinstance(v.iter, ast.Attribute)
        and v.iter.attr == "rules"
    ]
    if not bindings or len(loops_over_rules) != len(bindings):
        return [
            f"{where}: `rule` in `rule.id` is not bound only by `for rule in <x>.rules`"
        ]
    return []


def _binding_violations(tree: ast.AST, filename: str) -> List[str]:
    """
    An allowed name used in a checked call must be a parameter of the enclosing
    function and never rebound there; ``operator`` may instead be bound, only,
    from an ``<x>.operator`` attribute.
    """
    found: List[str] = []
    enclosing = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for inner in ast.walk(node):
                enclosing[id(inner)] = node  # walk is outer-first: innermost wins

    checked = list(_level_calls(tree)) + list(_once_calls(tree))
    for call in checked:
        found.extend(_rule_id_violations(call, enclosing.get(id(call)), filename))
        names = {
            arg.id
            for arg in call.args
            if isinstance(arg, ast.Name) and arg.id in ALLOWED_NAMES
        }
        for name in sorted(names):
            where = f"{filename}:{call.lineno}"
            function = enclosing.get(id(call))
            if function is None:
                found.append(f"{where}: `{name}` used outside a function")
                continue
            params = {
                a.arg
                for a in function.args.posonlyargs
                + function.args.args
                + function.args.kwonlyargs
            }
            bindings = [(v, line) for n, v, line in _bound_names(function) if n == name]
            if name in params:
                for _, line in bindings:
                    found.append(f"{filename}:{line}: parameter `{name}` is rebound")
            elif name == "operator" and bindings:
                for value, line in bindings:
                    if not (
                        isinstance(value, ast.Attribute) and value.attr == "operator"
                    ):
                        found.append(
                            f"{filename}:{line}: `operator` is bound to something "
                            "other than <x>.operator"
                        )
            else:
                found.append(f"{where}: `{name}` is not a parameter of {function.name}")

    # A `reason` parameter is logged as-is, so only the helpers whose every
    # call is checked for a literal reason may take one.
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            params = [a.arg for a in node.args.args + node.args.kwonlyargs]
            if "reason" in params and node.name not in ONCE_KEY_CALLS:
                found.append(
                    f"{filename}:{node.lineno}: a `reason` parameter outside "
                    f"{sorted(ONCE_KEY_CALLS)}"
                )
    return found


def _module_source(relative: str) -> str:
    return (REPO / relative).read_text(encoding="utf-8")


@pytest.mark.regression
@pytest.mark.parametrize("module", EVALUATION_MODULES)
def test_evaluation_logs_carry_only_allowed_arguments(module):
    source = _module_source(module)
    calls = list(_level_calls(ast.parse(source)))
    assert calls, f"no logger calls found in {module}: the scan is looking elsewhere"
    assert violations(source, module) == []


# -- the gate fires -----------------------------------------------------------
# Each planted source below is a shape the gate must refuse.

#: planted source -> the fragment the refusal must contain (so each fires for
#: the reason it names, not a side effect).
PLANTED = {
    "value at debug": (
        'def f(actual_value):\n    logger.debug("%s", actual_value)',
        "argument `actual_value` is not allowed",
    ),
    "exception f-string": (
        'def f():\n    logger.warning(f"{e}")',
        "the message is JoinedStr",
    ),
    "f-string of a value": (
        'def f(actual_value):\n    logger.warning(f"Failed {actual_value} > 1")',
        "the message is JoinedStr",
    ),
    "exception text as argument": (
        'def f(e):\n    logger.warning("Invalid: %s", e)',
        "argument `e` is not allowed",
    ),
    "str() of a value": (
        'def f(actual_value):\n    logger.debug("x %s", str(actual_value))',
        "argument `str(actual_value)` is not allowed",
    ),
    "type() without __name__": (
        'def f(actual_value):\n    logger.debug("%s", type(actual_value))',
        "argument `type(actual_value)` is not allowed",
    ),
    "traceback": (
        'def f():\n    logger.error("failed", exc_info=True)',
        "keyword arguments (exc_info)",
    ),
    "logger.exception": (
        'def f():\n    logger.exception("failed")',
        "logger.exception",
    ),
    "another receiver": (
        'def f():\n    log.warning("failed")',
        "on something other than `logger`",
    ),
    "concatenated message": (
        'def f(actual_value):\n    logger.info("x " + actual_value)',
        "the message is BinOp",
    ),
    "value as once reason": (
        "def f(operator, actual_value):\n"
        "    EVALUATION_NOTES.first(operator, actual_value)",
        "once-key reason `actual_value`",
    ),
    "value as once owner": (
        'def f(actual_value):\n    EVALUATION_NOTES.first(actual_value, "why")',
        "once-key owner `actual_value`",
    ),
    "allowed name rebound": (
        "def f(owner, actual_value):\n    owner = actual_value\n"
        '    logger.warning("%s", owner)',
        "parameter `owner` is rebound",
    ),
    "allowed name not a parameter": (
        "def f(actual_value):\n    rule_id = actual_value\n"
        '    logger.info("%s", rule_id)',
        "`rule_id` is not a parameter of f",
    ),
    "operator from a value": (
        "def f(actual_value):\n    operator = actual_value\n"
        '    logger.debug("%s", operator)',
        "`operator` is bound to something other than <x>.operator",
    ),
    "rule.id of a rebound value": (
        "def f(actual_value):\n    rule = actual_value\n"
        '    logger.warning("problem: %s", rule.id)',
        "`rule` in `rule.id` is not bound only by `for rule in <x>.rules`",
    ),
    "rule.id of a parameter": (
        'def f(rule):\n    logger.warning("problem: %s", rule.id)',
        "`rule` in `rule.id` is a parameter of f",
    ),
    "rule.id rebound inside the loop": (
        "def f(rules, actual_value):\n    for rule in rules.rules:\n"
        "        rule = actual_value\n"
        '        logger.warning("problem: %s", rule.id)',
        "`rule` in `rule.id` is not bound only by `for rule in <x>.rules`",
    ),
    "rule.id as a once owner": (
        "def f(actual_value):\n    rule = actual_value\n"
        '    EVALUATION_NOTES.first(rule.id, "why")',
        "`rule` in `rule.id` is not bound only by `for rule in <x>.rules`",
    ),
    "reason parameter elsewhere": (
        'def f(reason):\n    logger.warning("%s", reason)',
        "a `reason` parameter outside",
    ),
}


@pytest.mark.parametrize("source,fragment", list(PLANTED.values()), ids=list(PLANTED))
def test_a_planted_call_is_refused(source, fragment):
    refusals = violations(source)
    assert any(fragment in refusal for refusal in refusals), refusals


@pytest.mark.parametrize(
    "line",
    [
        "def f(operator, actual_value):\n"
        '    logger.debug("%s: a %s", operator, type(actual_value).__name__)',
        'def f(owner, e):\n    logger.warning("Rules for %s (%s)", owner, type(e).__name__)',
        'def f(rule_id):\n    logger.info("Invalidated rule %s", rule_id)',
        "def f(rules):\n    for rule in rules.rules:\n"
        '        EVALUATION_NOTES.first(rule.id, "rule failed to compile")\n'
        '        logger.warning("Rule %s failed", rule.id)',
        '_note_rule_problem(OperatorType.GEO_DISTANCE, "unknown distance unit")',
        "def f(condition):\n    operator = condition.operator\n"
        '    logger.debug("%s", operator)',
    ],
)
def test_the_allowed_shapes_pass(line):
    assert violations(line) == []
