"""Saving the global safety settings writes one audit entry (#1970). DB-free."""

import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend.app.models.audit_log import AuditLog
from backend.app.models.safety import SafetySettings
from backend.app.schemas.safety import SafetySettingsUpdate
from backend.app.services.safety_service import SafetyService


def _row(enabled=False, metrics=None):
    now = datetime.now(timezone.utc)
    return SafetySettings(
        id=uuid.uuid4(),
        enable_automatic_rollbacks=enabled,
        default_metrics=metrics,
        created_at=now,
        updated_at=now,
    )


def _added_entries(db):
    return [c.args[0] for c in db.add.call_args_list if isinstance(c.args[0], AuditLog)]


def _db(existing):
    db = MagicMock()
    db.query.return_value.first.return_value = existing
    return db


@pytest.mark.regression
@pytest.mark.asyncio
async def test_a_save_writes_one_entry_with_before_and_after():
    row = _row(enabled=False, metrics={"error_rate": {"critical_threshold": 0.05}})
    db = _db(row)
    actor = SimpleNamespace(id=uuid.uuid4(), email="root@example.com", username="root")

    await SafetyService(db).create_or_update_safety_settings(
        SafetySettingsUpdate(enable_automatic_rollbacks=True), actor=actor
    )

    (entry,) = _added_entries(db)
    assert entry.action_type == "safety_config_update"
    assert entry.entity_type == "safety_config"
    assert entry.entity_id == row.id
    assert entry.user_id == actor.id
    assert entry.user_email == "root@example.com"
    assert json.loads(entry.old_value)["enable_automatic_rollbacks"] is False
    assert json.loads(entry.new_value)["enable_automatic_rollbacks"] is True
    assert db.commit.call_count == 2  # the change, then the entry


@pytest.mark.asyncio
async def test_a_save_that_fails_writes_no_entry():
    db = _db(_row())
    db.commit.side_effect = RuntimeError("database is down")

    with pytest.raises(RuntimeError):
        await SafetyService(db).create_or_update_safety_settings(
            SafetySettingsUpdate(enable_automatic_rollbacks=True),
            actor=SimpleNamespace(id=uuid.uuid4(), email="root@example.com"),
        )

    assert _added_entries(db) == []
