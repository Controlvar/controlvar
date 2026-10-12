"""Every place that writes an ``error_logs`` row is accounted for.

Safety monitoring counts every ``error_logs`` row of a flag in its window
EXCEPT rows whose ``request_data["source"]`` is ``"client"``
(``SafetyService.get_error_metrics``). That is exclude polarity: it counts any
unstamped row. A row that carries no ``source`` at all -- whatever wrote it --
counts toward the flag's error rate and can roll the flag back, and so does a
``"client"`` row that an update rewrites. So a new writer is not harmless by
default: it has to decide what its rows mean. Error reports from ``POST /api/v1/tracking/errors[/batch]``
stamp ``"server"`` or ``"client"`` from the presented key; flag evaluations
that fail on the server write ``{"context": ...}`` and count on purpose.

This pins the exact set of writers, as (file, enclosing function), across
``backend/app``, ``modules``, ``backend/lambda``, ``backend/scripts`` and
``scripts`` (tests excluded). A writer is any of:

* a call to ``ErrorLog(...)``, under any name it is imported as;
* a call to a function named ``log_error`` (``MetricsService.log_error``);
* ``insert(ErrorLog)``, ``update(ErrorLog)``, ``bulk_insert_mappings(ErrorLog,
  ...)`` or ``bulk_update_mappings(ErrorLog, ...)``;
* ``.update(...)`` on a query of ``ErrorLog`` (``db.query(ErrorLog)...update``);
* any use of ``ErrorLog.__table__``;
* SQL text that inserts into, updates or copies into ``error_logs``, with or
  without a schema: a string literal (also one later passed to ``.format``,
  ``%`` or ``sql.SQL``) or an f-string, whose interpolated parts are read as
  placeholders (``f"INSERT INTO {schema}.error_logs"``).

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

#: SQL that writes ``error_logs``; the schema part is anything up to a dot
#: (``experimentation.``, ``"experimentation".``, ``{schema}.``, ``%s.``, ``{}.``).
_SQL_WRITE = re.compile(
    r"\b(?:insert\s+into|update|copy)\s+(?:\S{1,128}\.)?\"?error_logs\b",
    re.IGNORECASE,
)

#: The name every module imports the model by, unless it renames it.
_MODEL = "ErrorLog"


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _model_names(tree: ast.AST) -> Set[str]:
    """``ErrorLog`` and every name a module imports it as (``import ... as EL``)."""
    names = {_MODEL}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name.rsplit(".", 1)[-1] == _MODEL and alias.asname:
                    names.add(alias.asname)
    return names


def _queries_the_model(node: ast.AST, names: Set[str]) -> bool:
    """Whether ``node`` contains a call ``query(ErrorLog, ...)``."""
    return any(
        isinstance(sub, ast.Call)
        and _name(sub.func) == "query"
        and any(_name(arg) in names for arg in sub.args)
        for sub in ast.walk(node)
    )


def _sql_text(node: ast.AST) -> str:
    """The text of a string literal or an f-string (each interpolation read as ``x``)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
            else "x"
            for part in node.values
        )
    return ""


def _is_writer(node: ast.AST, names: Set[str] = frozenset({_MODEL})) -> bool:
    if isinstance(node, ast.Call):
        called = _name(node.func)
        if called in names or called == "log_error":
            return True
        if called in (
            "insert",
            "update",
            "bulk_insert_mappings",
            "bulk_update_mappings",
        ) and any(_name(arg) in names for arg in node.args[:1]):
            return True
        if (
            called == "update"
            and isinstance(node.func, ast.Attribute)
            and _queries_the_model(node.func.value, names)
        ):
            return True
    if (
        isinstance(node, ast.Attribute)
        and node.attr == "__table__"
        and _name(node.value) in names
    ):
        return True
    return bool(_SQL_WRITE.search(_sql_text(node)))


class _Sites(ast.NodeVisitor):
    def __init__(self, names: Set[str]) -> None:
        self.names = names
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
        if _is_writer(node, self.names):
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
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            sites = _Sites(_model_names(tree))
            sites.visit(tree)
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
        # The schema is configurable, so real SQL interpolates it.
        ("sql = f'INSERT INTO {schema}.error_logs (message) VALUES (1)'", True),
        ("sql = 'INSERT INTO {schema}.error_logs VALUES (1)'.format(schema=s)", True),
        ("sql = 'INSERT INTO %s.error_logs VALUES (1)' % schema", True),
        ("q = sql.SQL('INSERT INTO {}.error_logs VALUES (1)')", True),
        ('sql = f\'UPDATE "{schema}"."error_logs" SET request_data = NULL\'', True),
        ("db.execute(update(ErrorLog).values(request_data=None))", True),
        ("db.query(ErrorLog).filter(ErrorLog.id == 1).update({'message': 'm'})", True),
        ("db.bulk_update_mappings(ErrorLog, rows)", True),
        (
            "from backend.app.models.metrics.metric import ErrorLog as EL\n"
            "db.add(EL(error_type='x', message='m'))",
            True,
        ),
        ("self._log_error(request, exc)", False),
        ("db.query(ErrorLog).filter(ErrorLog.id == 1).all()", False),
        ("op.create_table('error_logs')", False),
        ("doc = 'reads error_logs rows'", False),
        ("doc = f'reads {n} error_logs rows'", False),
        ("db.query(ErrorLog).filter(ErrorLog.id == 1).delete()", False),
        ("settings.update({'error_logs': 1})", False),
    ],
)
def test_each_shape_is_recognised(source, is_writer):
    tree = ast.parse(source)
    names = _model_names(tree)
    assert any(_is_writer(node, names) for node in ast.walk(tree)) is is_writer
