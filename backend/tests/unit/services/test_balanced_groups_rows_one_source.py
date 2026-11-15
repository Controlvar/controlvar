"""The balanced-groups rows are built one way: by ``segment_rows`` (#448).

``GET /results/{id}/balanced-groups`` lists each user under the group stored
on their assignment. Its rows must come from ``segment_breakdown.segment_rows``
with ``stored_strata`` as the value source, so that a group is folded, ordered
and counted exactly as a breakdown's segment is. A second way of grouping the
stored groups (a ``GROUP BY`` on them, a counter over them) would make two
answers that can drift apart.

This reads the source of the results route module and of
``balanced_groups_results.py`` and requires:

* the only function that names ``balance_group`` is ``stored_strata``, and it
  neither groups nor counts;
* no ``group_by`` call in either module names ``balance_group``;
* ``_balanced_groups`` gets its rows from ``_compute_dimensional_breakdown``
  with a ``value_source``, which passes it to ``segment_rows``.

A finding names the file and the line.
"""

import ast
from pathlib import Path
from typing import Iterator, List, Tuple

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
RESULTS = "backend/app/api/v1/endpoints/results.py"
SOURCE = "backend/app/services/balanced_groups_results.py"
MODULES = (RESULTS, SOURCE)
GROUP_COLUMN = "balance_group"
COUNTING = ("group_by", "Counter", "count")


def _functions(tree: ast.AST) -> Iterator[Tuple[str, ast.AST]]:
    """Every function, with its dotted name (nested ones included)."""

    def walk(node: ast.AST, prefix: str) -> Iterator[Tuple[str, ast.AST]]:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{prefix}{child.name}"
                yield name, child
                yield from walk(child, f"{name}.")
            elif isinstance(child, ast.ClassDef):
                yield from walk(child, f"{prefix}{child.name}.")

    yield from walk(tree, "")


def _names_group_column(node: ast.AST) -> List[int]:
    return [
        n.lineno
        for n in ast.walk(node)
        if (isinstance(n, ast.Attribute) and n.attr == GROUP_COLUMN)
        or (isinstance(n, ast.Constant) and n.value == GROUP_COLUMN)
    ]


def _calls(node: ast.AST, names) -> List[Tuple[str, int]]:
    found = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            func = n.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else getattr(func, "id", "")
            )
            if name in names:
                found.append((name, n.lineno))
    return found


def grouping_problems(sources) -> List[str]:
    """What breaks the one-source rule, as ``file:line: reason`` lines.

    ``sources`` maps a path to its text.
    """
    problems: List[str] = []
    for path, text in sources.items():
        tree = ast.parse(text)
        innermost = {}
        for name, function in _functions(tree):
            for line in _names_group_column(function):
                innermost[line] = name
        for line, name in sorted(innermost.items()):
            if name.split(".")[-1] != "stored_strata":
                problems.append(f"{path}:{line}: {name} reads {GROUP_COLUMN}")
        module_level = set(_names_group_column(tree)) - set(innermost)
        for line in sorted(module_level):
            problems.append(f"{path}:{line}: module code reads {GROUP_COLUMN}")
        for name, function in _functions(tree):
            if name.split(".")[-1] == "stored_strata":
                for call, line in _calls(function, COUNTING):
                    problems.append(f"{path}:{line}: stored_strata calls {call}")
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "group_by":
                # The line of the argument: a chained call starts at its chain.
                for line in sorted(
                    {ln for arg in n.args for ln in _names_group_column(arg)}
                ):
                    problems.append(f"{path}:{line}: a group_by on {GROUP_COLUMN}")
    return problems


def _sources():
    return {path: (REPO_ROOT / path).read_text() for path in MODULES}


def test_the_rows_are_built_only_through_segment_rows():
    problems = grouping_problems(_sources())
    assert not problems, "\n".join(problems)


def test_the_route_passes_the_stored_groups_to_the_breakdowns_rows():
    tree = ast.parse(_sources()[RESULTS])
    functions = dict(_functions(tree))
    breakdown = functions["_compute_dimensional_breakdown"]
    assert any(
        kw.arg == "value_source"
        for call in ast.walk(breakdown)
        if isinstance(call, ast.Call) and getattr(call.func, "id", "") == "segment_rows"
        for kw in call.keywords
    ), "_compute_dimensional_breakdown does not pass value_source to segment_rows"
    balanced = functions["_balanced_groups"]
    calls = [
        call
        for call in ast.walk(balanced)
        if isinstance(call, ast.Call)
        and getattr(call.func, "id", "") == "_compute_dimensional_breakdown"
    ]
    assert len(calls) == 1, (
        f"{RESULTS}: _balanced_groups builds its rows {len(calls)} times"
    )
    assert any(kw.arg == "value_source" for kw in calls[0].keywords)
    assert any(
        isinstance(n, ast.Name) and n.id == "stored_strata"
        for n in ast.walk(functions["_balanced_groups.stored_groups"])
    )


def test_the_scan_names_a_second_grouping():
    """The scan is not vacuous: a GROUP BY on the stored group in the route
    module is reported with its file and line."""
    planted = (
        "def _count_groups(db, experiment_id):\n"
        "    return (\n"
        "        db.query(Assignment.balance_group, func.count(Assignment.id))\n"
        "        .group_by(Assignment.balance_group)\n"
        "        .all()\n"
        "    )\n"
    )
    found = grouping_problems({RESULTS: planted})
    assert f"{RESULTS}:3: _count_groups reads {GROUP_COLUMN}" in found, found
    assert f"{RESULTS}:4: a group_by on {GROUP_COLUMN}" in found, found
    counted = grouping_problems(
        {
            SOURCE: "def stored_strata(db):\n    return Counter(r.balance_group for r in db)\n"
        }
    )
    assert f"{SOURCE}:2: stored_strata calls Counter" in counted, counted
