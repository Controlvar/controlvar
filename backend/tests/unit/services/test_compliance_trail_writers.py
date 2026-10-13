"""What writes the compliance audit trail, and what each event holds (#1142).

The trail (``ComplianceAuditEvent``, table ``audit_events_v2``) is written
through ``AuditLogService.log``, and every write names its ``AuditAction``
member in the source. The source checks below read every call that names one,
in ``backend/app`` and (when present) ``modules/backend/app``, so a new writer,
a new action, a new outcome or a changed snapshot fails here until this file
says so.
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

REPO = pathlib.Path(__file__).resolve().parents[4]
TREES = [REPO / "backend" / "app", REPO / "modules" / "backend" / "app"]
MODEL = "backend/app/models/compliance_audit_event.py"
FLAGS = "backend/app/api/v1/endpoints/feature_flags.py"
EXPERIMENTS = "backend/app/api/v1/endpoints/experiments.py"
WAREHOUSE = "modules/backend/app/api/v1/endpoints/warehouse_analysis.py"
MODULES_PRESENT = (REPO / "modules" / "backend" / "app").is_dir()


def _files():
    for tree in TREES:
        if tree.is_dir():
            for path in sorted(tree.rglob("*.py")):
                yield path.relative_to(REPO).as_posix(), ast.parse(path.read_text())


def _member(node, enum_name):
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == enum_name
    ):
        return node.attr
    return None


def _functions(tree):
    """Each node mapped to the name of the function that holds it."""
    owner = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(fn):
                owner.setdefault(node, fn.name)
    return owner


def _writes():
    """(file, function, action, resource_type, outcome, call, function node)."""
    found = []
    for path, tree in _files():
        if path == MODEL:
            continue
        owner = _functions(tree)
        fns = {
            n.name: n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call):
                continue
            values = list(call.args) + [k.value for k in call.keywords]
            actions = [_member(v, "AuditAction") for v in values]
            actions = [a for a in actions if a]
            if not actions:
                continue
            (action,) = actions
            keywords = {k.arg: k.value for k in call.keywords}
            resource = keywords.get("resource_type")
            if resource is None:
                position = [
                    i for i, a in enumerate(call.args) if _member(a, "AuditAction")
                ][0]
                resource = call.args[position + 1]
            outcome = _member(keywords.get("outcome"), "AuditOutcome")
            name = owner.get(call)
            found.append(
                (
                    path,
                    name,
                    action,
                    resource.value if isinstance(resource, ast.Constant) else None,
                    outcome,
                    call,
                    fns.get(name),
                )
            )
    return found


def test_only_these_actions_write_an_event():
    written = {(path, action, resource) for path, _, action, resource, *_ in _writes()}
    expected = {
        (FLAGS, "CREATE", "feature_flag"),
        (FLAGS, "UPDATE", "feature_flag"),
        (FLAGS, "DELETE", "feature_flag"),
        (EXPERIMENTS, "CREATE", "experiment"),
        (EXPERIMENTS, "UPDATE", "experiment"),
        (EXPERIMENTS, "DELETE", "experiment"),
    }
    if MODULES_PRESENT:
        expected |= {
            (WAREHOUSE, "CREATE", "warehouse_connection"),
            (WAREHOUSE, "UPDATE", "warehouse_connection"),
            (WAREHOUSE, "DELETE", "warehouse_connection"),
            (WAREHOUSE, "KEY_CREATE", "warehouse_connection"),
            (WAREHOUSE, "CREATE", "warehouse_source"),
            (WAREHOUSE, "UPDATE", "warehouse_source"),
            (WAREHOUSE, "DELETE", "warehouse_source"),
            (WAREHOUSE, "CREATE", "warehouse_analysis_run"),
        }
    assert written == expected

    # So nothing writes READ, LOGIN, LOGOUT, LOGIN_FAILED, ROLE_GRANT,
    # ROLE_REVOKE, KEY_REVOKE, EXPORT or REPORT_GENERATED, which still exist
    # to filter on.
    never = set(AuditAction.__members__) - {a for _, a, _ in written}
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


def test_every_event_is_written_with_outcome_success():
    assert {o.value for o in AuditOutcome} == {"SUCCESS", "FAILURE", "DENIED"}
    for path, function, action, _, outcome, call, _ in _writes():
        if path == WAREHOUSE:
            continue  # through _audit, checked below
        assert outcome == "SUCCESS", f"{path}:{call.lineno} {function} {action}"
    named = set()
    for path, tree in _files():
        if path == MODEL:
            continue
        for node in ast.walk(tree):
            member = _member(node, "AuditOutcome")
            if member:
                named.add(member)
    # FAILURE and DENIED are never written; the warehouse helper passes SUCCESS.
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
    assert {path for path, _ in by_function} <= {FLAGS, EXPERIMENTS, WAREHOUSE}


@pytest.mark.modules
def test_which_warehouse_routes_write_and_when_a_key_is_created():
    by_action = {}
    for path, function, action, resource, *_ in _writes():
        if path == WAREHOUSE:
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
    tree = ast.parse((REPO / WAREHOUSE).read_text())
    promoters = {
        fn.name
        for fn in ast.walk(tree)
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
        for node in ast.walk(fn)
        if isinstance(node, ast.Name) and node.id == "_promote_pending_key"
    }
    assert promoters == {"test_connection"}


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
    for path, _, action, resource, _, call, function in _writes():
        if path == WAREHOUSE:
            continue
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
    for path, tree in _files():
        source = (REPO / path).read_text()
        if "ComplianceAuditEvent" not in source and "audit_events_v2" not in source:
            continue
        if "/migrations/" in path:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in {"delete", "update", "merge"}, (
                    f"{path}:{node.lineno} changes stored events"
                )
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
                assert node.attr not in {"archived_at", "old_value", "new_value"}, (
                    f"{path}:{node.lineno} rewrites a stored event"
                )


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
