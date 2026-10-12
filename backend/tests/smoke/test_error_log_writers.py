"""Every place that writes an ``error_logs`` row is accounted for.

Safety monitoring counts every ``error_logs`` row of a flag in its window
EXCEPT rows whose ``request_data["source"]`` is ``"client"``
(``SafetyService.get_error_metrics``). A row that carries no ``source`` at all
-- whatever wrote it -- counts toward the flag's error rate and can roll the
flag back. So a new writer is not harmless by default: it has to decide what
its rows mean. Error reports from ``POST /api/v1/tracking/errors[/batch]``
stamp ``"server"`` or ``"client"`` from the presented key; flag evaluations
that fail on the server write ``{"context": ...}`` and count on purpose.

This pins the exact set of writers, as (file, enclosing function), across
``backend/app``, ``modules``, ``backend/lambda``, ``backend/scripts`` and
``scripts`` (tests excluded). A writer is any of:

* a call to ``ErrorLog(...)``;
* a call to a function named ``log_error`` (``MetricsService.log_error``);
* ``insert(ErrorLog)`` or ``bulk_insert_mappings(ErrorLog, ...)``;
* any use of ``ErrorLog.__table__``;
* a string literal of SQL that inserts into, updates or copies into
  ``error_logs``.

A new site fails ``test_the_writers_are_exactly_these`` until it is listed in
``WRITERS``, and a removed one fails it until its entry goes. Sites are keyed
on file and function, never on line numbers. The source is read from this
file's own path (no git, no import of ``modules``), so the check runs the same
in core-build's copy of the tree, where ``modules/`` does not exist and its
entries are simply not expected.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Dict, Iterator, Set, Tuple

import pytest

pytestmark = [pytest.mark.smoke, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[3]
ROOTS = ("backend/app", "modules", "backend/lambda", "backend/scripts", "scripts")

HELPER = "the shared helper; its callers are listed here"
STAMPED = (
    'an error report: request_data["source"] is set from the presented key '
    "(report_source), so a report sent without a server-side key is not counted"
)
COUNTED = "a flag evaluation that failed on the server: no source, counted on purpose"

#: (file, enclosing function) -> what its rows mean.
WRITERS: Dict[Tuple[str, str], str] = {
    ("backend/app/services/metrics_service.py", "MetricsService.log_error"): HELPER,
    ("backend/app/api/v1/endpoints/client_errors.py", "report_client_error"): STAMPED,
    (
        "backend/app/api/v1/endpoints/client_errors.py",
        "report_client_errors_batch",
    ): STAMPED,
    (
        "backend/app/services/feature_flag_service.py",
        "FeatureFlagService.evaluate_flag_detailed",
    ): COUNTED,
}

_SQL_WRITE = re.compile(
    r"\b(?:insert\s+into|update|copy)\s+(?:[\w\"]+\.)?\"?error_logs\b", re.IGNORECASE
)


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _is_writer(node: ast.AST) -> bool:
    if isinstance(node, ast.Call):
        called = _name(node.func)
        if called in ("ErrorLog", "log_error"):
            return True
        if called in ("insert", "bulk_insert_mappings") and any(
            _name(arg) == "ErrorLog" for arg in node.args[:1]
        ):
            return True
    if (
        isinstance(node, ast.Attribute)
        and node.attr == "__table__"
        and _name(node.value) == "ErrorLog"
    ):
        return True
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return bool(_SQL_WRITE.search(node.value))
    return False


class _Sites(ast.NodeVisitor):
    def __init__(self) -> None:
        self.stack: list = []
        self.found: Set[str] = set()

    def _scoped(self, node) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_ClassDef = _scoped
    visit_FunctionDef = _scoped
    visit_AsyncFunctionDef = _scoped

    def generic_visit(self, node: ast.AST) -> None:
        if _is_writer(node):
            self.found.add(".".join(self.stack) or "<module>")
        super().generic_visit(node)


def _python_files(root: Path) -> Iterator[Path]:
    for path in sorted(root.rglob("*.py")):
        parts = path.relative_to(REPO_ROOT).parts
        if "tests" in parts or path.name.startswith("test_"):
            continue
        if "node_modules" in parts or "__pycache__" in parts:
            continue
        yield path


def _writers(repo_root: Path = REPO_ROOT) -> Set[Tuple[str, str]]:
    found: Set[Tuple[str, str]] = set()
    for root in ROOTS:
        base = repo_root / root
        if not base.is_dir():
            continue
        for path in _python_files(base):
            sites = _Sites()
            sites.visit(ast.parse(path.read_text(encoding="utf-8"), str(path)))
            relative = path.relative_to(repo_root).as_posix()
            found.update((relative, function) for function in sites.found)
    return found


def _expected() -> Set[Tuple[str, str]]:
    """``WRITERS``, less any entry under ``modules/`` when it is absent."""
    present = (REPO_ROOT / "modules").is_dir()
    return {site for site in WRITERS if present or not site[0].startswith("modules/")}


def test_the_writers_are_exactly_these():
    found = _writers()
    expected = _expected()
    unlisted = sorted(found - expected)
    gone = sorted(expected - found)
    assert not unlisted, (
        "error_logs writer not listed in WRITERS (decide whether its rows count "
        "toward automatic rollback, then list it): "
        + ", ".join(f"{file} :: {function}" for file, function in unlisted)
    )
    assert not gone, "listed in WRITERS but no longer writes: " + ", ".join(
        f"{file} :: {function}" for file, function in gone
    )


def test_the_scan_sees_the_writers_it_lists():
    """Guard the guard: the scan reads the roots and finds the known writers."""
    scanned = [
        path
        for root in ROOTS
        if (REPO_ROOT / root).is_dir()
        for path in _python_files(REPO_ROOT / root)
    ]
    assert len(scanned) > 300
    assert (
        "backend/app/services/feature_flag_service.py",
        "FeatureFlagService.evaluate_flag_detailed",
    ) in _writers()


@pytest.mark.parametrize(
    "source, is_writer",
    [
        ("ErrorLog(error_type='x', message='m')", True),
        ("MetricsService.log_error(db=db, data=data)", True),
        ("log_error(db, data)", True),
        ("db.execute(insert(ErrorLog).values(message='m'))", True),
        ("db.bulk_insert_mappings(ErrorLog, rows)", True),
        ("db.execute(ErrorLog.__table__.insert())", True),
        ("db.execute(text('INSERT INTO error_logs (message) VALUES (1)'))", True),
        ("sql = 'insert into experimentation.error_logs values (1)'", True),
        ("sql = 'UPDATE \"error_logs\" SET message = 1'", True),
        ("self._log_error(request, exc)", False),
        ("db.query(ErrorLog).filter(ErrorLog.id == 1).all()", False),
        ("op.create_table('error_logs')", False),
        ("doc = 'reads error_logs rows'", False),
    ],
)
def test_each_shape_is_recognised(source, is_writer):
    tree = ast.parse(source)
    assert any(_is_writer(node) for node in ast.walk(tree)) is is_writer
