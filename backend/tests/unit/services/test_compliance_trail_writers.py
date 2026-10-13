"""What writes the compliance audit trail in core, and what each event holds (#1142).

The trail (``ComplianceAuditEvent``, table ``audit_events_v2``) is written
through ``AuditLogService.log``, and every write names its ``AuditAction``
member in the source. The source checks below read every call that names one
in ``backend/app``, so a new writer, a new action, a new outcome or a changed
snapshot fails here until this file says so. The warehouse module's writers,
the only other ones, are held by the modules' own test of them
(``test_warehouse_compliance_writers.py``).
"""

from __future__ import annotations

import ast
import pathlib
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.app.api import deps
from backend.app.api.v1.endpoints import compliance
from backend.app.core import hooks
from backend.app.main import app
from backend.app.models.compliance_audit_event import AuditAction, AuditOutcome
from backend.app.models.user import UserRole
from backend.app.services.audit_log_service import AuditLogService
from backend.tests.source_scan import compliance_writes, member, row_changes

REPO = pathlib.Path(__file__).resolve().parents[4]
CORE = REPO / "backend" / "app"
MODEL = "backend/app/models/compliance_audit_event.py"
FLAGS = "backend/app/api/v1/endpoints/feature_flags.py"
EXPERIMENTS = "backend/app/api/v1/endpoints/experiments.py"


def _files():
    for path in sorted(CORE.rglob("*.py")):
        yield path.relative_to(REPO).as_posix(), ast.parse(path.read_text())


def _writes():
    """(file, function, action, resource_type, outcome, call, function node)."""
    return [
        (path, *write)
        for path, tree in _files()
        if path != MODEL
        for write in compliance_writes(tree)
    ]


def test_only_these_actions_write_an_event():
    written = {(path, action, resource) for path, _, action, resource, *_ in _writes()}
    assert written == {
        (FLAGS, "CREATE", "feature_flag"),
        (FLAGS, "UPDATE", "feature_flag"),
        (FLAGS, "DELETE", "feature_flag"),
        (EXPERIMENTS, "CREATE", "experiment"),
        (EXPERIMENTS, "UPDATE", "experiment"),
        (EXPERIMENTS, "DELETE", "experiment"),
    }

    # Core writes nothing else: not READ, LOGIN, LOGOUT, LOGIN_FAILED,
    # ROLE_GRANT, ROLE_REVOKE, KEY_REVOKE, EXPORT or REPORT_GENERATED, which
    # still exist to filter on, and not KEY_CREATE, which only the warehouse
    # module writes.
    never = set(AuditAction.__members__) - {a for _, a, _ in written}
    assert never == {
        "READ",
        "LOGIN",
        "LOGOUT",
        "LOGIN_FAILED",
        "ROLE_GRANT",
        "ROLE_REVOKE",
        "KEY_REVOKE",
        "KEY_CREATE",
        "EXPORT",
        "REPORT_GENERATED",
    }


def test_every_event_is_written_with_outcome_success():
    assert {o.value for o in AuditOutcome} == {"SUCCESS", "FAILURE", "DENIED"}
    for path, function, action, _, outcome, call, _ in _writes():
        assert outcome == "SUCCESS", f"{path}:{call.lineno} {function} {action}"
    named = set()
    for path, tree in _files():
        if path == MODEL:
            continue
        for node in ast.walk(tree):
            found = member(node, "AuditOutcome")
            if found:
                named.add(found)
    # FAILURE and DENIED are never written.
    assert named == {"SUCCESS"}


def test_flag_status_changes_and_experiment_lifecycle_write_no_event():
    by_function = {(path, fn) for path, fn, *_ in _writes()}
    assert {fn for path, fn in by_function if path == FLAGS} == {
        "create_feature_flag",
        "update_feature_flag",
        "delete_feature_flag",
    }
    assert {fn for path, fn in by_function if path == EXPERIMENTS} == {
        "create_experiment",
        "update_experiment",
        "delete_experiment",
    }
    # Sign-ins, role and API key changes write none either: no other file.
    assert {path for path, _ in by_function} == {FLAGS, EXPERIMENTS}


def _keys(value, function):
    if isinstance(value, ast.Dict):
        return {k.value for k in value.keys}
    assert isinstance(value, ast.Name), ast.dump(value)
    for node in ast.walk(function):
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == value.id for t in node.targets)
            and isinstance(node.value, ast.Dict)
        ):
            return {k.value for k in node.value.keys}
    raise AssertionError(f"no dict literal for {value.id}")


def test_each_event_carries_a_snapshot_of_a_few_fields():
    snapshots = {}
    for _, _, action, resource, _, call, function in _writes():
        keywords = {k.arg: k.value for k in call.keywords}
        snapshots[(resource, action)] = {
            side: _keys(keywords[side], function)
            for side in ("old_value", "new_value")
            if side in keywords
        }
        assert {"actor_id", "resource_id"} <= set(keywords)
    assert snapshots == {
        ("feature_flag", "CREATE"): {"new_value": {"key", "name"}},
        ("feature_flag", "UPDATE"): {
            "old_value": {"key", "name", "status", "rollout_percentage"},
            "new_value": {"key", "name", "status", "rollout_percentage"},
        },
        ("feature_flag", "DELETE"): {"old_value": {"key", "name", "status"}},
        ("experiment", "CREATE"): {"new_value": {"name"}},
        ("experiment", "UPDATE"): {
            "old_value": {"name", "status", "targeting_rules"},
            "new_value": {"name", "status", "targeting_rules"},
        },
        ("experiment", "DELETE"): {"old_value": {"name", "status", "owner_id"}},
    }


def test_nothing_changes_or_deletes_an_event_once_written():
    changes = [
        (path, line, what)
        for path, tree in _files()
        if "/migrations/" not in path
        for line, what in row_changes(
            tree, {"ComplianceAuditEvent"}, {"audit_events_v2"}, rewrites=True
        )
    ]
    assert changes == []


@pytest.mark.parametrize(
    "source, found",
    [
        ("def f(db):\n    db.execute(delete(ComplianceAuditEvent))\n", True),
        (
            "def f(db):\n    db.execute(update(ComplianceAuditEvent).values(outcome='X'))\n",
            True,
        ),
        (
            "def f(db, i):\n"
            "    event = db.query(ComplianceAuditEvent).get(i)\n"
            "    db.delete(event)\n",
            True,
        ),
        (
            "def f(db, i):\n"
            "    event = db.get(ComplianceAuditEvent, i)\n"
            "    event.old_value = {}\n",
            True,
        ),
        (
            "def f(db):\n    db.execute(text('UPDATE audit_events_v2 SET outcome = 1'))\n",
            True,
        ),
        # Not a change: signing a new event before it is stored.
        (
            "def f(db):\n"
            "    event = ComplianceAuditEvent(action='CREATE')\n"
            "    event.hmac_signature = 'x'\n"
            "    db.add(event)\n",
            False,
        ),
    ],
    ids=[
        "delete-statement",
        "update-statement",
        "session-delete",
        "rewrite",
        "sql",
        "new-event",
    ],
)
def test_the_scan_sees_each_way_to_change_a_stored_event(source, found):
    changes = row_changes(
        ast.parse(source), {"ComplianceAuditEvent"}, {"audit_events_v2"}, rewrites=True
    )
    assert bool(changes) is found


# ---------------------------------------------------------------------------
# Core and the compliance module
# ---------------------------------------------------------------------------


def _user(role, superuser=False):
    return SimpleNamespace(id=uuid.uuid4(), role=role, is_superuser=superuser)


@pytest.mark.parametrize(
    "call",
    [
        lambda user: compliance.generate_compliance_report(
            standard="soc2",
            start_time=None,
            end_time=None,
            current_user=user,
            db=MagicMock(),
        ),
        lambda user: compliance.export_audit_events(
            format="json",
            start_time=None,
            end_time=None,
            current_user=user,
            db=MagicMock(),
        ),
    ],
    ids=["report", "export"],
)
def test_core_answers_the_report_and_export_501_after_the_role_check(call):
    with (
        patch.object(compliance, "compliance_report_handler", return_value=None),
        patch.object(compliance, "compliance_export_handler", return_value=None),
    ):
        with pytest.raises(HTTPException) as refused:
            call(_user(UserRole.VIEWER))
        assert refused.value.status_code == 403

        with pytest.raises(HTTPException) as missing:
            call(_user(UserRole.ADMIN))
        assert missing.value.status_code == 501


def test_core_records_events_unsigned():
    db = MagicMock()
    previous = hooks.set_audit_signer(hooks.NullAuditSigner())
    try:
        event = AuditLogService(db).log(
            action=AuditAction.UPDATE,
            resource_type="experiment",
            outcome=AuditOutcome.SUCCESS,
            resource_id=uuid.uuid4(),
            actor_id=str(uuid.uuid4()),
            old_value={"name": "a"},
            new_value={"name": "b"},
        )
    finally:
        hooks.set_audit_signer(previous)
    assert event.hmac_signature is None
    db.add.assert_called_once_with(event)

    # With the compliance module, its signer replaces the core default.
    signer = MagicMock()
    signer.sign.return_value = "f" * 64
    previous = hooks.set_audit_signer(signer)
    try:
        signed = AuditLogService(MagicMock()).log(
            action=AuditAction.CREATE,
            resource_type="feature_flag",
            outcome=AuditOutcome.SUCCESS,
        )
    finally:
        hooks.set_audit_signer(previous)
    assert signed.hmac_signature == "f" * 64


def test_the_listing_carries_no_signature_header():
    admin = _user(UserRole.ADMIN, superuser=True)
    service = MagicMock()
    service.get_events.return_value = {"items": [], "total": 0, "page": 1, "limit": 50}
    app.dependency_overrides[deps.get_current_active_user] = lambda: admin
    app.dependency_overrides[deps.get_db] = lambda: MagicMock()
    try:
        with patch.object(compliance, "AuditLogService", return_value=service):
            response = TestClient(app).get("/api/v1/compliance/audit-events")
    finally:
        app.dependency_overrides.pop(deps.get_current_active_user, None)
        app.dependency_overrides.pop(deps.get_db, None)
    assert response.status_code == 200, response.text
    assert [h for h in response.headers if "signature" in h.lower()] == []
