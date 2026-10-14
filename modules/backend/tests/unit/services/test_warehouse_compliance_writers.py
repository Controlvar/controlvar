"""The warehouse module's compliance-trail writers, and the trail across the profile (#1142).

Core's writers are held by ``backend/tests/unit/services/
test_compliance_trail_writers.py``. This one reads the modules' code: which
warehouse routes write an event and with which action, that only they write
``KEY_CREATE``, that across core and the modules nothing writes the nine
actions kept only for filtering, and that no module code deletes or rewrites
a stored event.
"""

from __future__ import annotations

import ast
import pathlib

from backend.app.models.compliance_audit_event import AuditAction
from backend.tests.source_scan import compliance_writes, member, row_changes
from modules.backend.app.api.v1.endpoints import warehouse_analysis

REPO = pathlib.Path(__file__).resolve().parents[5]
TREES = (REPO / "backend" / "app", REPO / "modules" / "backend" / "app")
WAREHOUSE = pathlib.Path(warehouse_analysis.__file__)
MODEL = "compliance_audit_event.py"


def _files():
    for tree in TREES:
        for path in sorted(tree.rglob("*.py")):
            yield path, ast.parse(path.read_text())


def _warehouse_writes():
    return compliance_writes(ast.parse(WAREHOUSE.read_text()))


def test_which_warehouse_routes_write_and_when_a_key_is_created():
    by_action = {}
    for function, action, resource, *_ in _warehouse_writes():
        by_action.setdefault((resource, action), set()).add(function)
    assert by_action == {
        ("warehouse_connection", "CREATE"): {"create_connection"},
        ("warehouse_connection", "UPDATE"): {"update_connection"},
        ("warehouse_connection", "DELETE"): {"delete_connection"},
        # /regenerate-key, and a pending key promoted by a passing test.
        ("warehouse_connection", "KEY_CREATE"): {
            "regenerate_key",
            "_promote_pending_key",
        },
        ("warehouse_source", "CREATE"): {"create_source"},
        ("warehouse_source", "UPDATE"): {"update_source", "validate_source"},
        ("warehouse_source", "DELETE"): {"delete_source"},
        ("warehouse_analysis_run", "CREATE"): {"start_run"},
    }
    tree = ast.parse(WAREHOUSE.read_text())
    promoters = {
        fn.name
        for fn in ast.walk(tree)
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
        for node in ast.walk(fn)
        if isinstance(node, ast.Name) and node.id == "_promote_pending_key"
    }
    assert promoters == {"test_connection"}
    # The one helper every warehouse write goes through records SUCCESS.
    outcomes = {
        member(node, "AuditOutcome")
        for node in ast.walk(tree)
        if member(node, "AuditOutcome")
    }
    assert outcomes == {"SUCCESS"}


def test_across_the_profile_only_the_warehouse_writes_key_create():
    written = {}
    for path, tree in _files():
        if path.name == MODEL:
            continue
        for _, action, *_ in compliance_writes(tree):
            written.setdefault(action, set()).add(path)
    assert written["KEY_CREATE"] == {WAREHOUSE}
    never = set(AuditAction.__members__) - set(written)
    assert never == {
        "READ",
        "LOGIN",
        "LOGOUT",
        "LOGIN_FAILED",
        "ROLE_GRANT",
        "ROLE_REVOKE",
        "KEY_REVOKE",
        "EXPORT",
        "REPORT_GENERATED",
    }


def test_no_module_code_changes_or_deletes_a_stored_event():
    changes = [
        (path, line, what)
        for path, tree in _files()
        if path.is_relative_to(REPO / "modules")
        for line, what in row_changes(
            tree, {"ComplianceAuditEvent"}, {"audit_events_v2"}, rewrites=True
        )
    ]
    assert changes == []
