"""The audit-write alarm's half in the API: the lines it counts (#834).

The fargate stack's ``AuditWriteFailures`` metric filter counts the API log
lines matching

    { ($.logger = "backend.app.services.audit_service")
      && (($.level = "error") || ($.level = "critical")) }

built from ``AUDIT_SERVICE_LOGGER`` in ``infrastructure/cdk/stacks/names.py``
(pinned literally in ``infrastructure/tests/test_standing_alarms.py``), and
``experimentation-api-audit-write-failed-<env>`` fires on one such line in 5
minutes. The pattern names the line's logger and level, not its text, so:

* a failed write must render as a JSON line whose ``logger`` is that constant
  and whose ``level`` is ``error`` -- moving or renaming the module would
  silence the alarm (``test_a_failed_write_renders_the_line_the_filter_counts``);
* the module's error-level calls must be exactly the two "Failed to create
  audit log" lines, or a third would be counted as a lost entry
  (``test_the_modules_error_lines_are_exactly_the_two_audit_failures``; the
  line ``docs/api/audit-logging.md`` quotes is checked in
  ``backend/tests/unit/docs/test_audit_write_alarm_docs.py``);
* the API's final logging configuration must still be the JSON one when the
  line is written (``test_the_api_writes_the_line_as_json_after_startup``).

``matches`` below imitates the pattern with exact comparisons only, which is
all the pattern uses. Whether CloudWatch's own engine agrees is not checked
here (``aws logs test-metric-filter`` needs credentials).
"""

from __future__ import annotations

import ast
import contextlib
import importlib.util
import io
import json
import logging
import os
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend.app.models.audit_log import ActionType, EntityType
from backend.app.services.audit_service import AuditService

REPO_ROOT = Path(__file__).resolve().parents[4]
NAMES_PY = REPO_ROOT / "infrastructure" / "cdk" / "stacks" / "names.py"
AUDIT_SERVICE_PY = REPO_ROOT / "backend" / "app" / "services" / "audit_service.py"


def _load_names():
    """``infrastructure/cdk/stacks/names.py``, by path: it is stdlib only."""
    name = "stacks_names_for_audit_write_failed_line"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, NAMES_PY)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


names = _load_names()


def matches(line: dict) -> bool:
    """The metric filter's pattern, on one parsed JSON line."""
    logger, level = line.get("logger"), line.get("level")
    return logger == names.AUDIT_SERVICE_LOGGER and level in ("error", "critical")


@contextlib.contextmanager
def _json_lines():
    """Every record rendered as the production JSON line, into a buffer.

    As ``backend/tests/unit/core/test_logger.py`` does it; the root handlers
    and the structlog configuration are restored afterwards.
    """
    import structlog

    from backend.app.core.logger import configure_logging

    root = logging.root
    handlers, root_level = list(root.handlers), root.level
    saved = structlog.get_config()
    buffer = io.StringIO()
    configure_logging(log_level="INFO", json_logs=True, stream=buffer)
    try:
        yield buffer
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in handlers:
            root.addHandler(handler)
        root.setLevel(root_level)
        structlog.configure(**saved)


def _parsed(buffer: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]


def _entry() -> dict:
    return {
        "actor": SimpleNamespace(id=uuid.uuid4(), email="ana@example.com"),
        "action": ActionType.TOGGLE_DISABLE,
        "entity_type": EntityType.FEATURE_FLAG,
        "entity_id": uuid.uuid4(),
        "entity_name": "New checkout",
    }


def _after_commit_fails():
    db = MagicMock()
    db.commit.side_effect = RuntimeError("the row")
    assert AuditService.record_after_commit(db, **_entry()) is None


def _savepoint_fails():
    db = MagicMock()
    # The caller's own flush succeeds; the entry's flush in the savepoint fails.
    db.flush.side_effect = [None, RuntimeError("the row")]
    assert AuditService.record_in_savepoint(db, **_entry()) is None


def _toggle_operation_fails():
    import asyncio

    db = MagicMock()
    db.commit.side_effect = RuntimeError("the row")
    with pytest.raises(RuntimeError):
        asyncio.run(
            AuditService.log_toggle_operation(
                db,
                user_id=uuid.uuid4(),
                user_email="ana@example.com",
                action_type="toggle_disable",
                entity_id=uuid.uuid4(),
                entity_name="New checkout",
                old_value="active",
                new_value="inactive",
            )
        )


@pytest.mark.regression
@pytest.mark.parametrize(
    "fail",
    [_after_commit_fails, _savepoint_fails, _toggle_operation_fails],
    ids=["record_after_commit", "record_in_savepoint", "log_toggle_operation"],
)
def test_a_failed_write_renders_the_line_the_filter_counts(fail):
    """#834: each way an entry can fail writes exactly one line the filter counts."""
    with _json_lines() as buffer:
        fail()
    lines = _parsed(buffer)
    counted = [line for line in lines if matches(line)]
    assert len(counted) == 1, lines
    (line,) = counted
    assert line["logger"] == names.AUDIT_SERVICE_LOGGER, line
    assert line["level"] == "error", line
    assert line["event"].startswith(names.AUDIT_WRITE_FAILED_SEARCH), line


def test_the_filter_does_not_count_a_written_entry_or_another_loggers_error():
    """Controls: the INFO line of a written entry, and another module's error."""
    db = MagicMock()
    with _json_lines() as buffer:
        assert AuditService.record_after_commit(db, **_entry()) is not None
        logging.getLogger("backend.app.services.feature_flag_service").error(
            "Failed to create audit log for nothing (RuntimeError)"
        )
    lines = _parsed(buffer)
    written = [line for line in lines if line["logger"] == names.AUDIT_SERVICE_LOGGER]
    assert [line["level"] for line in written] == ["info"], lines
    other = [line for line in lines if line["logger"] != names.AUDIT_SERVICE_LOGGER]
    assert [line["level"] for line in other] == ["error"], lines
    assert not [line for line in lines if matches(line)], lines


# --- the module's error-level calls ------------------------------------------

#: The logging methods that write a line at error level or above.
_ERROR_METHODS = {"error", "critical", "exception", "fatal"}
_ERROR_LEVELS = {"ERROR", "CRITICAL", "FATAL"}
_BELOW_ERROR_LEVELS = {"NOTSET", "DEBUG", "INFO", "WARNING", "WARN"}


def _is_error_call(call: ast.Call) -> bool:
    func = call.func
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr in _ERROR_METHODS:
        return True
    if func.attr == "log" and call.args:
        level = call.args[0]
        named = (
            level.attr
            if isinstance(level, ast.Attribute)
            else level.id
            if isinstance(level, ast.Name)
            else None
        )
        if named in _ERROR_LEVELS:
            return True
        if named in _BELOW_ERROR_LEVELS:
            return False
        if isinstance(level, ast.Constant) and isinstance(level.value, int):
            return level.value >= logging.ERROR
        # A level the walk cannot read is refused rather than guessed at.
        return True
    return False


def _error_format_strings() -> list[str]:
    tree = ast.parse(AUDIT_SERVICE_PY.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_error_call(node):
            args = node.args[1:] if node.func.attr == "log" else node.args
            first = args[0] if args else None
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.append(first.value)
            else:
                found.append(ast.unparse(node))
    return sorted(found)


def test_the_modules_error_lines_are_exactly_the_two_audit_failures():
    """The filter counts every error line of this module as a lost entry.

    So the module writes exactly two: ``_log_failure``'s and
    ``log_toggle_operation``'s. A third fails here until someone decides
    whether it is a lost entry. (The page half, that the line
    audit-logging.md quotes is ``_log_failure``'s, is in
    ``backend/tests/unit/docs/test_audit_write_alarm_docs.py``.)
    """
    formats = _error_format_strings()
    assert formats == [
        "Failed to create audit log (%s)",
        "Failed to create audit log for %s on %s %s (%s)",
    ], formats
    for text in formats:
        assert text.startswith(names.AUDIT_WRITE_FAILED_SEARCH), text


def test_the_walk_finds_every_error_shape():
    """The walk above, on the shapes it must count and the ones it must not."""
    source = "\n".join(
        [
            'logger.error("a")',
            'logger.critical("b")',
            'logger.exception("c")',
            'logger.log(logging.ERROR, "d")',
            'logger.log(50, "e")',
            # A level the walk cannot read counts, rather than being guessed.
            'logger.log(level, "f")',
            'logger.log(logging.WARNING, "w")',
            'logger.log(20, "w")',
            'logger.warning("w")',
            'logger.info("i")',
        ]
    )
    calls = [n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Call)]
    counted = [c.args[-1].value for c in calls if _is_error_call(c)]
    assert counted == list("abcdef"), counted


# --- the logging configuration the API ends on --------------------------------

_PROBE = """
import json, sys
import backend.app.main as main
assert main._monitoring_imports_ok is True, "main.py skipped configure_logging"
import backend.app.core.logging  # noqa: F401  (imported again after startup)
from backend.app.services.audit_service import AuditService
AuditService._log_failure("toggle_disable", "feature_flag", "1234", RuntimeError("x"))
"""


def test_the_api_writes_the_line_as_json_after_startup():
    """main.py's configure_logging is the configuration the line is written in.

    ``backend.app.core.logging`` sets up an upper-case "ERROR" JSON formatter
    when it is first imported; main.py imports it before configure_logging
    replaces every root handler. If that order flipped, or main.py's guarded
    import skipped configure_logging, the line would carry "ERROR" and neither
    log alarm would count it.
    """
    env = dict(os.environ, ENVIRONMENT="test", LOG_FORMAT="json")
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    lines = []
    for text in (result.stdout + result.stderr).splitlines():
        if names.AUDIT_WRITE_FAILED_SEARCH in text:
            lines.append(text)
    assert len(lines) == 1, (result.stdout[-2000:], result.stderr[-2000:])
    line = json.loads(lines[0])
    assert line["level"] == "error", line
    assert line["logger"] == names.AUDIT_SERVICE_LOGGER, line
    assert matches(line), line
