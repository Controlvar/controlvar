"""A feature flag change writes its audit entries in its own transaction (#1042).

``backend/tests/integration/api/test_flag_change_audit_strict.py`` drives each
flag route with the audit tables refusing writes. This file pins, from the
source, what keeps a new or edited flag route from writing its entry after
the change is committed again:

* the flag routes (``feature_flags.py``) and bulk toggle (``bulk_toggle.py``)
  call none of the writers that commit, or swallow a failure, on their own:
  ``record_after_commit``, ``record_in_savepoint``, ``log_action``,
  ``log_toggle_operation`` and ``log_bulk_toggle``;
* in those files every ``AuditService.record`` call and every compliance
  write (``source_scan.compliance_writes``) sits inside a
  ``with AuditService.audit_with_change(...)`` block, and no such block
  commits or awaits: the change and its entries are committed after the
  block, together;
* the five ``FeatureFlagService`` writers are called exactly five times in the
  application (``backend/app``, and ``modules/backend/app`` when it is
  present), each with ``record=`` set to something other than ``None``.
  ``record`` defaults to ``None`` for the tests that call the service
  directly, so a route that leaves it out would save its change alone.

The source is read from this file's own path (no git), so the check runs the
same in core-build's copy of the tree.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from backend.tests.source_scan import compliance_writes

pytestmark = [pytest.mark.smoke, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[3]
ENDPOINTS = REPO_ROOT / "backend" / "app" / "api" / "v1" / "endpoints"
#: The route modules whose flag changes are strict.
STRICT_FILES = ("feature_flags.py", "bulk_toggle.py")
#: The application trees searched for calls of the service's writers.
TREES = (REPO_ROOT / "backend" / "app", REPO_ROOT / "modules" / "backend" / "app")

#: Writers that commit, or swallow a failure, on their own.
BANNED = {
    "record_after_commit",
    "record_in_savepoint",
    "log_action",
    "log_toggle_operation",
    "log_bulk_toggle",
}
#: ``FeatureFlagService``'s writers, which take ``record=``.
SERVICE_WRITERS = {
    "create_feature_flag",
    "update_feature_flag",
    "delete_feature_flag",
    "activate_feature_flag",
    "deactivate_feature_flag",
}
SERVICE_WRITER_CALLS = 5


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _strict_trees():
    return [(name, _tree(ENDPOINTS / name)) for name in STRICT_FILES]


def _called(node: ast.AST):
    """The name a call calls (``x.y(...)`` gives ``y``), or None."""
    if not isinstance(node, ast.Call):
        return None
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return None


def _audit_blocks(tree: ast.AST):
    """Every ``with ...audit_with_change(...)`` statement."""
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.With, ast.AsyncWith))
        and any(
            _called(item.context_expr) == "audit_with_change" for item in node.items
        )
    ]


def _inside(blocks):
    """The ids of every node in the bodies of ``blocks``."""
    return {
        id(node)
        for block in blocks
        for statement in block.body
        for node in ast.walk(statement)
    }


def _is_audit_record(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "record"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "AuditService"
    )


def banned_calls(name: str, tree: ast.AST):
    return [
        f"{name}:{node.lineno} {_called(node)}"
        for node in ast.walk(tree)
        if _called(node) in BANNED
    ]


def writes_outside_a_block(name: str, tree: ast.AST):
    inside = _inside(_audit_blocks(tree))
    calls = [node for node in ast.walk(tree) if _is_audit_record(node)]
    calls += [write[4] for write in compliance_writes(tree)]
    return [f"{name}:{call.lineno}" for call in calls if id(call) not in inside]


def commits_or_awaits_in_a_block(name: str, tree: ast.AST):
    found = []
    for block in _audit_blocks(tree):
        for statement in block.body:
            for node in ast.walk(statement):
                if isinstance(node, ast.Await):
                    found.append(f"{name}:{node.lineno} await")
                elif _called(node) == "commit":
                    found.append(f"{name}:{node.lineno} commit")
    return found


def service_writer_calls():
    """(place, writer, the ``record=`` expression or None) for each call."""
    found = []
    for tree_root in TREES:
        if not tree_root.is_dir():
            continue
        for path in sorted(tree_root.rglob("*.py")):
            rel = path.relative_to(REPO_ROOT).as_posix()
            for node in ast.walk(_tree(path)):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in SERVICE_WRITERS
                ):
                    record = next(
                        (k.value for k in node.keywords if k.arg == "record"), None
                    )
                    found.append((f"{rel}:{node.lineno}", node.func.attr, record))
    return found


# --- the pins -------------------------------------------------------------------


def test_the_strict_routes_call_no_writer_that_commits_on_its_own():
    found = [hit for name, tree in _strict_trees() for hit in banned_calls(name, tree)]
    assert found == []


def test_every_entry_is_written_inside_an_audit_with_change_block():
    trees = _strict_trees()
    for name, tree in trees:
        assert _audit_blocks(tree), f"{name}: no audit_with_change block"
    found = [hit for name, tree in trees for hit in writes_outside_a_block(name, tree)]
    assert found == []


def test_no_audit_with_change_block_commits_or_awaits():
    found = [
        hit
        for name, tree in _strict_trees()
        for hit in commits_or_awaits_in_a_block(name, tree)
    ]
    assert found == []


def test_every_service_writer_call_passes_record():
    calls = service_writer_calls()
    assert len(calls) == SERVICE_WRITER_CALLS, calls
    missing = [
        (place, writer)
        for place, writer, record in calls
        if record is None or (isinstance(record, ast.Constant) and record.value is None)
    ]
    assert missing == []


# --- the checks fire on what they look for ----------------------------------------


GOOD = """
async def route(db, flag):
    def record(changed):
        with AuditService.audit_with_change(db, action=1, entity_type=2, entity_id=3):
            AuditService.record(db, actor=None)
            audit = AuditLogService(db)
            audit.log(action=AuditAction.UPDATE, resource_type="feature_flag",
                      outcome=AuditOutcome.SUCCESS, resource_id="x", actor_id="y")
    service.update_feature_flag(flag, data, record=record)
    flag.status = "ACTIVE"
    with AuditService.audit_with_change(db, action=1, entity_type=2, entity_id=3):
        entry = AuditService.record(db, actor=None)
    db.commit()
"""
LAST_WRITE = "entry = AuditService.record(db, actor=None)"


@pytest.mark.parametrize(
    "source, check",
    [
        (
            GOOD.replace("db.commit()", "AuditService.record_after_commit(db)"),
            banned_calls,
        ),
        (
            GOOD.replace("db.commit()", "await AuditService.log_action(db)"),
            banned_calls,
        ),
        (GOOD + "\nAuditService.record(db, actor=None)\n", writes_outside_a_block),
        (
            GOOD + "\naudit.log(action=AuditAction.CREATE, resource_type='x')\n",
            writes_outside_a_block,
        ),
        (
            GOOD.replace(LAST_WRITE, LAST_WRITE + "\n        db.commit()"),
            commits_or_awaits_in_a_block,
        ),
        (
            GOOD.replace(LAST_WRITE, LAST_WRITE + "\n        await asyncio.sleep(0)"),
            commits_or_awaits_in_a_block,
        ),
    ],
    ids=[
        "record_after_commit",
        "log_action",
        "record outside",
        "compliance write outside",
        "commit inside",
        "await inside",
    ],
)
def test_each_check_finds_its_defect(source, check):
    assert check("probe.py", ast.parse(GOOD)) == []
    assert check("probe.py", ast.parse(source)) != []
