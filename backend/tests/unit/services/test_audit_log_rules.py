"""The audit log's rules, read from the code that writes and reads it (#1142).

A refused flag in a bulk toggle; the entity types; an entity's history order;
the experiment scheduler's reasons; what no entry holds; a failed write's one
ERROR line and counter; and nothing that deletes an entry.
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import desc

from backend.app.api import deps
from backend.app.core.metrics import audit_write_failures_total
from backend.app.main import app
from backend.app.models.audit_log import ActionType, AuditLog, EntityType
from backend.app.models.experiment import ExperimentStatus
from backend.app.models.feature_flag import FeatureFlagStatus
from backend.app.models.user import UserRole
from backend.app.services.audit_service import AuditService
from backend.tests.source_scan import row_changes

REPO = pathlib.Path(__file__).resolve().parents[4]
TREES = [REPO / "backend" / "app", REPO / "modules" / "backend" / "app"]
WRITERS = {"record", "record_after_commit", "record_in_savepoint", "log_action"}


def _sources():
    for tree in TREES:
        if tree.is_dir():
            for path in sorted(tree.rglob("*.py")):
                yield path.relative_to(REPO).as_posix(), ast.parse(path.read_text())


@pytest.mark.parametrize("role", [UserRole.ANALYST, UserRole.VIEWER])
def test_a_flag_the_caller_may_not_change_is_refused_and_left_as_it_was(role):
    user = SimpleNamespace(
        id=uuid.uuid4(), role=role, is_active=True, is_superuser=False
    )
    flag = SimpleNamespace(
        id=uuid.uuid4(),
        key="new-checkout",
        owner_id=user.id,
        status=FeatureFlagStatus.ACTIVE,
    )
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = flag
    app.dependency_overrides[deps.get_current_active_user] = lambda: user
    app.dependency_overrides[deps.get_db] = lambda: db
    try:
        with patch.object(AuditService, "record_after_commit") as recorded:
            response = TestClient(app).post(
                "/api/v1/feature-flags/bulk-toggle",
                json={"flag_ids": [str(flag.id)], "action": "disable"},
            )
    finally:
        app.dependency_overrides.pop(deps.get_current_active_user, None)
        app.dependency_overrides.pop(deps.get_db, None)
    assert response.status_code == 200, response.text
    (result,) = response.json()["results"]
    assert result["success"] is False
    assert result["error"] == "Not enough permissions to change this feature flag"
    assert flag.status == FeatureFlagStatus.ACTIVE
    recorded.assert_not_called()


def test_the_entity_types():
    assert {e.value for e in EntityType} == {
        "feature_flag",
        "experiment",
        "user",
        "role",
        "permission",
        "safety_config",
        "rollout_schedule",
        "api_key",
        "holdout",
        "mutual_exclusion_group",
        "segment",
    }


def test_an_entitys_history_is_newest_first():
    db = MagicMock()
    AuditService.get_entity_audit_history(db, EntityType.FEATURE_FLAG, uuid.uuid4())
    (ordering,) = db.query.return_value.filter.return_value.order_by.call_args.args
    assert str(ordering) == str(desc(AuditLog.timestamp))


def _experiment(status, **dates):
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="Checkout",
        status=status,
        start_date=None,
        end_date=None,
        resume_at=None,
        owner_id=uuid.uuid4(),
        # What Start requires; the scheduler starts only such a draft (#1454).
        variants=[SimpleNamespace(is_control=True), SimpleNamespace(is_control=False)],
        metric_definitions=[SimpleNamespace()],
        **dates,
    )


def test_the_scheduler_records_a_start_a_resume_and_an_end_with_their_reasons():
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    draft = _experiment(ExperimentStatus.DRAFT)
    draft.start_date = past
    paused = _experiment(ExperimentStatus.PAUSED)
    paused.resume_at = past
    active = _experiment(ExperimentStatus.ACTIVE)
    active.end_date = past

    db = MagicMock()
    db.query.return_value.filter.return_value.all.side_effect = [
        [draft, paused],
        [active],
    ]
    from backend.app.core import scheduler as scheduler_module

    scheduler = scheduler_module.ExperimentScheduler()
    scheduler._notification_service = MagicMock()
    recorded = []
    with (
        patch.object(scheduler_module, "SessionLocal", return_value=db),
        patch.object(scheduler_module, "audit_snapshot", return_value={}),
        patch.object(scheduler_module, "stamp_rollout_rule_id"),
        patch.object(
            scheduler,
            "_record_transition",
            side_effect=lambda db, experiment, action, before, reason: recorded.append(
                (experiment.id, action, reason)
            ),
        ),
    ):
        asyncio.run(scheduler.process_scheduled_experiments())
    assert recorded == [
        (draft.id, ActionType.EXPERIMENT_START, "scheduled start"),
        (paused.id, ActionType.EXPERIMENT_START, "scheduled resume"),
        (active.id, ActionType.EXPERIMENT_COMPLETE, "scheduled end"),
    ]

    # POST /experiments/schedules/process runs the same tick.
    tree = ast.parse(
        (
            REPO / "backend" / "app" / "api" / "v1" / "endpoints" / "experiments.py"
        ).read_text()
    )
    (route,) = [
        f
        for f in ast.walk(tree)
        if isinstance(f, ast.AsyncFunctionDef)
        and f.name == "trigger_schedule_processing"
    ]
    assert "experiment_scheduler.process_scheduled_experiments" in ast.unparse(route)


def test_no_entry_holds_error_text():
    assert set(AuditLog.__table__.columns.keys()) == {
        "id",
        "user_id",
        "user_email",
        "action_type",
        "entity_type",
        "entity_id",
        "entity_name",
        "old_value",
        "new_value",
        "reason",
        "timestamp",
        "created_at",
        "updated_at",
    }
    # No writer is handed anything an `except ... as error` bound.
    for path, tree in _sources():
        for handler in ast.walk(tree):
            if not (isinstance(handler, ast.ExceptHandler) and handler.name):
                continue
            for node in ast.walk(handler):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in WRITERS
                    and getattr(node.func.value, "id", None) == "AuditService"
                ):
                    used = {
                        n.id
                        for a in list(node.args) + [k.value for k in node.keywords]
                        for n in ast.walk(a)
                        if isinstance(n, ast.Name)
                    }
                    assert handler.name not in used, f"{path}:{node.lineno}"


def test_changes_made_directly_in_the_database_write_no_entry():
    for root in (
        REPO / "backend" / "app" / "db",
        REPO / "modules" / "backend" / "app" / "db",
    ):
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            text = path.read_text().upper()
            assert "CREATE TRIGGER" not in text and "CREATE RULE" not in text, path


def test_a_failed_write_logs_one_error_line_and_counts_it(caplog):
    db = MagicMock()
    db.commit.side_effect = RuntimeError("the row")
    before = audit_write_failures_total._value.get()
    entity_id = uuid.uuid4()
    with caplog.at_level("ERROR", logger="backend.app.services.audit_service"):
        result = AuditService.record_after_commit(
            db,
            actor=SimpleNamespace(id=uuid.uuid4(), email="ana@example.com"),
            action=ActionType.TOGGLE_DISABLE,
            entity_type=EntityType.FEATURE_FLAG,
            entity_id=entity_id,
            entity_name="New checkout",
        )
    assert result is None
    assert audit_write_failures_total._value.get() == before + 1
    lines = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert lines == [
        f"Failed to create audit log for toggle_disable on feature_flag {entity_id} (RuntimeError)"
    ]


def test_nothing_deletes_an_audit_entry():
    deletes = [
        (path, line, what)
        for path, tree in _sources()
        if "/migrations/" not in path
        for line, what in row_changes(tree, {"AuditLog"}, {"audit_logs"})
    ]
    assert deletes == []
    # Deleting a user empties user_id on their entries; it removes none.
    (foreign_key,) = AuditLog.__table__.c.user_id.foreign_keys
    assert foreign_key.ondelete == "SET NULL"
    routes = {
        (method, ctx.path)
        for route in app.routes
        for ctx in (getattr(route, "effective_route_contexts", lambda: ())() or ())
        for method in (getattr(ctx, "methods", None) or ())
    }
    assert not [r for r in routes if r[0] == "DELETE" and "/audit-logs" in r[1]]


@pytest.mark.parametrize(
    "source, found",
    [
        pytest.param(
            "def f(db):\n    db.execute(delete(AuditLog))\n",
            True,
            id="delete-statement",
        ),
        pytest.param(
            "def f(db):\n    db.query(AuditLog).filter(AuditLog.id == 1).delete()\n",
            True,
            id="query-delete",
        ),
        pytest.param(
            "def f(db, i):\n    entry = db.query(AuditLog).filter(AuditLog.id == i).first()\n    db.delete(entry)\n",
            True,
            id="session-delete",
        ),
        pytest.param(
            "def f(db):\n    for entry in db.query(AuditLog).all():\n        db.delete(entry)\n",
            True,
            id="loop-delete",
        ),
        pytest.param(
            "def f(db):\n    [db.delete(e) for e in db.query(AuditLog).all()]\n",
            True,
            id="comprehension",
        ),
        pytest.param(
            "def f(db):\n    if (e := db.query(AuditLog).first()):\n        db.delete(e)\n",
            True,
            id="walrus",
        ),
        pytest.param(
            "def f(db):\n    db.execute(text('DELETE FROM audit_logs'))\n",
            True,
            id="sql",
        ),
        pytest.param(
            'def f(db, schema):\n    db.execute(text(f"DELETE FROM {schema}.audit_logs"))\n',
            True,
            id="f-string",
        ),
        pytest.param(
            'def f(db, schema):\n    db.execute(text("DELETE\\n  FROM " + schema + ".audit_logs WHERE id = " + str(1)))\n',
            True,
            id="concatenation",
        ),
        pytest.param(
            "from backend.app.models.audit_log import AuditLog as AL\n\ndef f(db):\n    db.execute(delete(AL))\n",
            True,
            id="aliased-model",
        ),
        pytest.param(
            "def f(db):\n    model = AuditLog\n    db.query(model).delete()\n",
            True,
            id="assigned-model",
        ),
        pytest.param(
            "from sqlalchemy import delete as rm\n\ndef f(db):\n    db.execute(rm(AuditLog))\n",
            True,
            id="aliased-verb",
        ),
        pytest.param(
            "def f(db):\n    remove = db.delete\n    entry = db.query(AuditLog).first()\n    remove(entry)\n",
            True,
            id="assigned-verb",
        ),
        pytest.param(
            "def f(db):\n    entry = db.query(AuditLog).first()\n    def later():\n        db.delete(entry)\n    return later\n",
            True,
            id="closure",
        ),
        # Not a removal: a new entry, another model's row, a read, another table.
        pytest.param(
            "def f(db):\n    entry = AuditLog(user_email='a')\n    db.add(entry)\n",
            False,
            id="new-entry",
        ),
        pytest.param(
            "def f(db, i):\n    flag = db.query(FeatureFlag).get(i)\n    db.query(AuditLog).count()\n    db.delete(flag)\n",
            False,
            id="other-row",
        ),
        pytest.param(
            "def f(db):\n    entry = db.query(AuditLog).first()\n    return entry\ndef g(db, entry):\n    db.delete(entry)\n",
            False,
            id="other-function",
        ),
        pytest.param(
            'def f(db, schema):\n    db.execute(text(f"SELECT * FROM {schema}.audit_logs"))\n',
            False,
            id="a-read",
        ),
        pytest.param(
            'def f(db, schema):\n    db.execute(text("DELETE FROM " + schema + ".feature_flags"))\n',
            False,
            id="other-table",
        ),
    ],
)
def test_the_scan_sees_each_way_to_remove_an_entry(source, found):
    assert bool(row_changes(ast.parse(source), {"AuditLog"}, {"audit_logs"})) is found
