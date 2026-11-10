"""The pages that name the audit-write alarm spell the name the stack gives it (#834).

``docs/api/audit-logging.md`` tells an operator which alarm emails them when
an audit-log entry cannot be written, and the AWS CDK deployment page lists it
among the fargate stack's alarms. Both take the name from
``api_audit_write_failed_alarm_name`` in ``infrastructure/cdk/stacks/names.py``
(loaded by path: it is stdlib only), so a rename that misses a page fails here.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
NAMES_PY = REPO_ROOT / "infrastructure" / "cdk" / "stacks" / "names.py"
PAGES = (
    REPO_ROOT / "docs" / "api" / "audit-logging.md",
    REPO_ROOT / "docs" / "self-hosting" / "cdk.md",
)


def _load_names():
    name = "stacks_names_for_audit_write_alarm_docs"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, NAMES_PY)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_the_page_names_the_audit_write_alarm(page):
    names = _load_names()
    alarm = names.api_audit_write_failed_alarm_name("<env>")
    assert alarm == "experimentation-api-audit-write-failed-<env>", alarm
    assert f"`{alarm}`" in page.read_text(encoding="utf-8"), (page, alarm)


def test_the_audit_page_says_what_to_search_for():
    """The search text the alarm's description gives, and the log group."""
    names = _load_names()
    text = PAGES[0].read_text(encoding="utf-8")
    assert f"`{names.AUDIT_WRITE_FAILED_SEARCH}`" in text
    assert "`/ecs/experimentation-backend-<env>`" in text


def test_the_page_quotes_the_line_the_api_writes():
    """The ```text block is ``_log_failure``'s format string, each ``<...>`` as ``%s``.

    The format string itself is pinned in
    ``backend/tests/unit/services/test_audit_write_failed_line.py``.
    """
    page = PAGES[0].read_text(encoding="utf-8")
    blocks = re.findall(r"```text\n(Failed to create audit log[^\n]{0,200})\n```", page)
    assert len(blocks) == 1, blocks
    quoted = re.sub(r"<[^<>\n]{1,40}>", "%s", blocks[0])
    assert quoted == "Failed to create audit log for %s on %s %s (%s)", blocks[0]
