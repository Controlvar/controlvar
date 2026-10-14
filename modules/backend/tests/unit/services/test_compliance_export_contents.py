"""What the compliance export downloads (#1142).

``GET /api/v1/compliance/export`` answers a download named
``audit_export.json`` or ``audit_export.csv`` holding every matching event,
oldest first, with eight fields and never ``old_value`` or ``new_value``.
"""

from __future__ import annotations

import csv
import io
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from backend.app.models.compliance_audit_event import (
    AuditAction,
    AuditOutcome,
    ComplianceAuditEvent,
)
from backend.app.models.user import UserRole
from modules.backend.app.api.v1.endpoints import compliance_reports
from modules.backend.app.services.compliance_report_service import (
    ComplianceReportService,
)

FIELDS = [
    "id",
    "timestamp",
    "action",
    "resource_type",
    "resource_id",
    "actor_id",
    "outcome",
    "hmac_signature",
]


def _event(minutes_ago):
    return SimpleNamespace(
        id=uuid.uuid4(),
        timestamp=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
        action=AuditAction.UPDATE,
        resource_type="experiment",
        resource_id=str(uuid.uuid4()),
        actor_id=uuid.uuid4(),
        outcome=AuditOutcome.SUCCESS,
        hmac_signature="a" * 64,
        old_value={"name": "before"},
        new_value={"name": "after"},
    )


def _service(events):
    db = MagicMock()
    query = db.query.return_value
    query.filter.return_value = query
    query.order_by.return_value.all.return_value = events
    return ComplianceReportService(db), query


@pytest.mark.parametrize("fmt", ["json", "csv"])
def test_every_matching_event_oldest_first_with_eight_fields(fmt):
    events = [_event(30), _event(10)]
    service, query = _service(events)

    content = service.export_events(format=fmt)

    (ordering,) = query.order_by.call_args.args
    assert str(ordering) == str(ComplianceAuditEvent.timestamp.asc())
    if fmt == "json":
        rows = json.loads(content)
        assert [list(r) for r in rows] == [FIELDS, FIELDS]
    else:
        reader = csv.DictReader(io.StringIO(content))
        assert reader.fieldnames == FIELDS
        rows = list(reader)
    assert [r["id"] for r in rows] == [str(e.id) for e in events]
    assert "old_value" not in content and "new_value" not in content
    assert "before" not in content and "after" not in content


@pytest.mark.parametrize("fmt", ["json", "csv"])
def test_the_response_is_a_named_download(fmt):
    admin = SimpleNamespace(id=uuid.uuid4(), role=UserRole.ADMIN, is_superuser=False)
    with patch.object(compliance_reports, "ComplianceReportService") as service:
        service.return_value.export_events.return_value = ""
        response = compliance_reports.export_audit_events(
            format=fmt,
            start_time=None,
            end_time=None,
            current_user=admin,
            db=MagicMock(),
        )
    assert response.headers["content-disposition"] == (
        f"attachment; filename=audit_export.{fmt}"
    )
