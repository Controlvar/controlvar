"""
Unit tests for AuditLogService.

Tests cover:
- log() creates ComplianceAuditEvent with correct fields
- log() signs event with HMAC
- log() sets retention expiry for soc2 and iso27001
- log() captures actor info (actor_id, actor_ip, actor_user_agent)
- log() stores old_value and new_value
- log() redacts sensitive fields (password, token, key, etc.)
- log() works for CREATE, UPDATE, DELETE, LOGIN actions
- get_events() returns paginated results
- get_events() filters by resource_type, actor_id, action, date range
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, call, patch

import pytest

from backend.app.models.compliance_audit_event import (
    AuditAction,
    AuditOutcome,
    ComplianceAuditEvent,
)
from backend.app.services.audit_log_service import AuditLogService, _redact_sensitive

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_mock_db():
    """Return a mock SQLAlchemy Session."""
    db = MagicMock()
    db.add = MagicMock()
    db.flush = MagicMock()
    return db


# ---------------------------------------------------------------------------
# Tests: _redact_sensitive helper
# ---------------------------------------------------------------------------


class TestRedactSensitive:
    """Tests for the _redact_sensitive() module-level helper."""

    def test_returns_none_for_none_input(self):
        assert _redact_sensitive(None) is None

    def test_non_sensitive_fields_pass_through(self):
        data = {"name": "my-flag", "rollout_percentage": 50}
        result = _redact_sensitive(data)
        assert result == data

    def test_password_field_is_redacted(self):
        result = _redact_sensitive({"password": "s3cr3t"})
        assert result["password"] == "[REDACTED]"

    def test_hashed_password_field_is_redacted(self):
        result = _redact_sensitive({"hashed_password": "bcrypt_hash"})
        assert result["hashed_password"] == "[REDACTED]"

    def test_token_field_is_redacted(self):
        result = _redact_sensitive({"token": "eyJhbGci..."})
        assert result["token"] == "[REDACTED]"

    def test_api_key_field_is_redacted(self):
        result = _redact_sensitive({"api_key": "sk-live-xyz"})
        assert result["api_key"] == "[REDACTED]"

    def test_secret_field_is_redacted(self):
        result = _redact_sensitive({"secret": "my-secret"})
        assert result["secret"] == "[REDACTED]"

    def test_access_token_field_is_redacted(self):
        result = _redact_sensitive({"access_token": "at-value"})
        assert result["access_token"] == "[REDACTED]"

    def test_refresh_token_field_is_redacted(self):
        result = _redact_sensitive({"refresh_token": "rt-value"})
        assert result["refresh_token"] == "[REDACTED]"

    def test_nested_dict_is_recursively_redacted(self):
        data = {"user": {"password": "secret", "name": "alice"}}
        result = _redact_sensitive(data)
        assert result["user"]["password"] == "[REDACTED]"
        assert result["user"]["name"] == "alice"

    def test_mixed_sensitive_and_safe_fields(self):
        data = {
            "key": "my-flag-key",
            "name": "My Flag",
            "api_key": "sk-xyz",
            "rollout_percentage": 75,
        }
        result = _redact_sensitive(data)
        assert result["key"] == "my-flag-key"
        assert result["name"] == "My Flag"
        assert result["api_key"] == "[REDACTED]"
        assert result["rollout_percentage"] == 75

    def test_case_insensitive_field_matching(self):
        result = _redact_sensitive({"PASSWORD": "secret"})
        assert result["PASSWORD"] == "[REDACTED]"

    @pytest.mark.regression
    def test_sensitive_field_in_object_inside_list_is_redacted(self):
        data = {"items": [{"password": "x", "name": "item-1"}]}
        result = _redact_sensitive(data)
        assert result == {"items": [{"password": "[REDACTED]", "name": "item-1"}]}

    @pytest.mark.regression
    def test_sensitive_field_inside_list_of_lists_is_redacted(self):
        data = {"matrix": [[{"token": "jwt-123", "id": 1}], [{"api_key": "sk-1"}]]}
        result = _redact_sensitive(data)
        assert result == {
            "matrix": [
                [{"token": "[REDACTED]", "id": 1}],
                [{"api_key": "[REDACTED]"}],
            ]
        }

    @pytest.mark.regression
    def test_sensitive_field_in_object_inside_list_inside_object_is_redacted(self):
        data = {
            "outer": {
                "entries": [
                    {"nested": {"secret": "top-secret", "label": "ok"}},
                ]
            }
        }
        result = _redact_sensitive(data)
        assert result == {
            "outer": {
                "entries": [
                    {"nested": {"secret": "[REDACTED]", "label": "ok"}},
                ]
            }
        }

    @pytest.mark.regression
    def test_non_sensitive_list_items_are_unchanged(self):
        data = {
            "tags": ["alpha", "beta"],
            "counts": [1, 2, 3],
            "objects": [{"name": "safe", "enabled": True}],
        }
        result = _redact_sensitive(data)
        assert result == data

    @pytest.mark.regression
    def test_input_structure_is_not_modified(self):
        data = {
            "password": "root-secret",
            "items": [
                {"token": "tok-1", "name": "first"},
                [{"api_key": "key-2"}, "plain"],
            ],
        }
        snapshot = {
            "password": "root-secret",
            "items": [
                {"token": "tok-1", "name": "first"},
                [{"api_key": "key-2"}, "plain"],
            ],
        }
        result = _redact_sensitive(data)
        assert data == snapshot
        assert result["items"] is not data["items"]
        assert result["items"][0] is not data["items"][0]
        assert result["items"][1] is not data["items"][1]


# ---------------------------------------------------------------------------
# Tests: AuditLogService.log()
# ---------------------------------------------------------------------------


class TestAuditLogServiceLog:
    """Tests for AuditLogService.log()."""

    def setup_method(self):
        self.db = make_mock_db()
        self.service = AuditLogService(self.db)

    def test_log_creates_compliance_audit_event(self):
        """log() adds a ComplianceAuditEvent to the session."""
        self.service.log(
            action=AuditAction.CREATE,
            resource_type="feature_flag",
            outcome=AuditOutcome.SUCCESS,
        )
        self.db.add.assert_called_once()
        event = self.db.add.call_args[0][0]
        assert isinstance(event, ComplianceAuditEvent)

    def test_log_event_has_correct_action(self):
        self.service.log(
            action=AuditAction.CREATE,
            resource_type="feature_flag",
            outcome=AuditOutcome.SUCCESS,
        )
        event = self.db.add.call_args[0][0]
        assert event.action == AuditAction.CREATE

    def test_log_event_has_correct_resource_type(self):
        self.service.log(
            action=AuditAction.UPDATE,
            resource_type="experiment",
            outcome=AuditOutcome.SUCCESS,
        )
        event = self.db.add.call_args[0][0]
        assert event.resource_type == "experiment"

    def test_log_event_has_correct_outcome(self):
        self.service.log(
            action=AuditAction.DELETE,
            resource_type="feature_flag",
            outcome=AuditOutcome.DENIED,
        )
        event = self.db.add.call_args[0][0]
        assert event.outcome == AuditOutcome.DENIED

    @pytest.mark.modules
    def test_log_event_signs_event_with_hmac(self):
        """log() sets a non-empty hmac_signature on the event."""
        self.service.log(
            action=AuditAction.CREATE,
            resource_type="feature_flag",
            outcome=AuditOutcome.SUCCESS,
        )
        event = self.db.add.call_args[0][0]
        assert event.hmac_signature is not None
        assert len(event.hmac_signature) == 64  # SHA-256 hex digest

    def test_log_event_sets_retention_expiry_for_soc2(self):
        """log() sets retention_expires_at based on SOC2 retention days."""
        from backend.app.core.config import settings

        before = datetime.now(timezone.utc)
        self.service.log(
            action=AuditAction.CREATE,
            resource_type="feature_flag",
            outcome=AuditOutcome.SUCCESS,
            retention_standard="soc2",
        )
        after = datetime.now(timezone.utc)
        event = self.db.add.call_args[0][0]
        assert event.retention_expires_at is not None
        expected_min = before + timedelta(days=settings.AUDIT_RETENTION_DAYS_SOC2)
        expected_max = after + timedelta(days=settings.AUDIT_RETENTION_DAYS_SOC2)
        assert expected_min <= event.retention_expires_at <= expected_max

    def test_log_event_sets_retention_expiry_for_iso27001(self):
        """log() sets retention_expires_at based on ISO27001 retention days."""
        from backend.app.core.config import settings

        before = datetime.now(timezone.utc)
        self.service.log(
            action=AuditAction.CREATE,
            resource_type="feature_flag",
            outcome=AuditOutcome.SUCCESS,
            retention_standard="iso27001",
        )
        after = datetime.now(timezone.utc)
        event = self.db.add.call_args[0][0]
        assert event.retention_expires_at is not None
        expected_min = before + timedelta(days=settings.AUDIT_RETENTION_DAYS_ISO27001)
        expected_max = after + timedelta(days=settings.AUDIT_RETENTION_DAYS_ISO27001)
        assert expected_min <= event.retention_expires_at <= expected_max

    def test_log_event_with_actor_info(self):
        """log() stores actor_id, actor_ip, actor_user_agent."""
        actor_id = str(uuid.uuid4())
        self.service.log(
            action=AuditAction.LOGIN,
            resource_type="session",
            outcome=AuditOutcome.SUCCESS,
            actor_id=actor_id,
            actor_ip="192.168.1.100",
            actor_user_agent="Mozilla/5.0",
        )
        event = self.db.add.call_args[0][0]
        assert event.actor_id == actor_id
        assert event.actor_ip == "192.168.1.100"
        assert event.actor_user_agent == "Mozilla/5.0"

    def test_log_event_with_resource_id(self):
        """log() stores resource_id as string."""
        rid = uuid.uuid4()
        self.service.log(
            action=AuditAction.UPDATE,
            resource_type="feature_flag",
            outcome=AuditOutcome.SUCCESS,
            resource_id=rid,
        )
        event = self.db.add.call_args[0][0]
        assert event.resource_id == str(rid)

    def test_log_event_with_old_and_new_values(self):
        """log() stores old_value and new_value dicts."""
        old = {"status": "inactive", "rollout_percentage": 0}
        new = {"status": "active", "rollout_percentage": 50}
        self.service.log(
            action=AuditAction.UPDATE,
            resource_type="feature_flag",
            outcome=AuditOutcome.SUCCESS,
            old_value=old,
            new_value=new,
        )
        event = self.db.add.call_args[0][0]
        assert event.old_value == old
        assert event.new_value == new

    def test_log_event_redacts_sensitive_fields_in_old_value(self):
        """log() redacts sensitive fields in old_value before storing."""
        old = {"name": "Flag A", "token": "secret-token"}
        self.service.log(
            action=AuditAction.UPDATE,
            resource_type="feature_flag",
            outcome=AuditOutcome.SUCCESS,
            old_value=old,
        )
        event = self.db.add.call_args[0][0]
        assert event.old_value["token"] == "[REDACTED]"
        assert event.old_value["name"] == "Flag A"

    def test_log_event_redacts_sensitive_fields_in_new_value(self):
        """log() redacts sensitive fields in new_value before storing."""
        new = {"name": "Flag B", "api_key": "sk-live-abc", "rollout_percentage": 100}
        self.service.log(
            action=AuditAction.CREATE,
            resource_type="feature_flag",
            outcome=AuditOutcome.SUCCESS,
            new_value=new,
        )
        event = self.db.add.call_args[0][0]
        assert event.new_value["api_key"] == "[REDACTED]"
        assert event.new_value["name"] == "Flag B"
        assert event.new_value["rollout_percentage"] == 100

    def test_log_create_action(self):
        self.service.log(
            action=AuditAction.CREATE,
            resource_type="feature_flag",
            outcome=AuditOutcome.SUCCESS,
        )
        event = self.db.add.call_args[0][0]
        assert event.action == AuditAction.CREATE

    def test_log_update_action(self):
        self.service.log(
            action=AuditAction.UPDATE,
            resource_type="feature_flag",
            outcome=AuditOutcome.SUCCESS,
        )
        event = self.db.add.call_args[0][0]
        assert event.action == AuditAction.UPDATE

    def test_log_delete_action(self):
        self.service.log(
            action=AuditAction.DELETE,
            resource_type="feature_flag",
            outcome=AuditOutcome.SUCCESS,
        )
        event = self.db.add.call_args[0][0]
        assert event.action == AuditAction.DELETE

    def test_log_login_action(self):
        self.service.log(
            action=AuditAction.LOGIN,
            resource_type="session",
            outcome=AuditOutcome.SUCCESS,
        )
        event = self.db.add.call_args[0][0]
        assert event.action == AuditAction.LOGIN

    def test_log_event_has_non_none_id(self):
        """log() generates a UUID id for the event."""
        self.service.log(
            action=AuditAction.CREATE,
            resource_type="feature_flag",
            outcome=AuditOutcome.SUCCESS,
        )
        event = self.db.add.call_args[0][0]
        assert event.id is not None

    def test_log_event_has_timestamp(self):
        """log() sets timestamp to a recent UTC datetime."""
        before = datetime.now(timezone.utc)
        self.service.log(
            action=AuditAction.CREATE,
            resource_type="feature_flag",
            outcome=AuditOutcome.SUCCESS,
        )
        after = datetime.now(timezone.utc)
        event = self.db.add.call_args[0][0]
        assert before <= event.timestamp <= after

    def test_log_calls_db_flush(self):
        """log() calls db.flush() to persist within the transaction."""
        self.service.log(
            action=AuditAction.CREATE,
            resource_type="feature_flag",
            outcome=AuditOutcome.SUCCESS,
        )
        self.db.flush.assert_called_once()

    def test_log_returns_compliance_audit_event(self):
        """log() returns the created ComplianceAuditEvent."""
        result = self.service.log(
            action=AuditAction.CREATE,
            resource_type="feature_flag",
            outcome=AuditOutcome.SUCCESS,
        )
        assert isinstance(result, ComplianceAuditEvent)

    def test_log_with_session_and_request_ids(self):
        """log() stores session_id and request_id."""
        self.service.log(
            action=AuditAction.UPDATE,
            resource_type="feature_flag",
            outcome=AuditOutcome.SUCCESS,
            session_id="sess-abc123",
            request_id="req-xyz789",
        )
        event = self.db.add.call_args[0][0]
        assert event.session_id == "sess-abc123"
        assert event.request_id == "req-xyz789"

    def test_log_without_actor_has_none_actor_fields(self):
        """log() with no actor info leaves actor fields as None."""
        self.service.log(
            action=AuditAction.CREATE,
            resource_type="feature_flag",
            outcome=AuditOutcome.SUCCESS,
        )
        event = self.db.add.call_args[0][0]
        assert event.actor_id is None
        assert event.actor_ip is None
        assert event.actor_user_agent is None


# ---------------------------------------------------------------------------
# Tests: AuditLogService.get_events()
# ---------------------------------------------------------------------------


class TestAuditLogServiceGetEvents:
    """Tests for AuditLogService.get_events() query/filter/pagination."""

    def _make_service_with_query_mock(self, events, total=None):
        """Return (service, query_mock) where query returns the given events."""
        db = MagicMock()
        query_mock = MagicMock()
        db.query.return_value = query_mock

        # Chain: .filter().filter()... .count() / .order_by().offset().limit().all()
        query_mock.filter.return_value = query_mock
        query_mock.count.return_value = total if total is not None else len(events)
        query_mock.order_by.return_value = query_mock
        query_mock.offset.return_value = query_mock
        query_mock.limit.return_value = query_mock
        query_mock.all.return_value = events

        service = AuditLogService(db)
        return service, db, query_mock

    def test_get_audit_events_paginated(self):
        """get_events() returns items, total, page, limit."""
        events = [MagicMock(spec=ComplianceAuditEvent) for _ in range(3)]
        service, db, _ = self._make_service_with_query_mock(events, total=10)

        result = service.get_events(page=1, limit=3)

        assert result["items"] == events
        assert result["total"] == 10
        assert result["page"] == 1
        assert result["limit"] == 3

    def test_get_audit_events_filtered_by_resource_type(self):
        """get_events(resource_type=...) applies a filter."""
        service, db, query_mock = self._make_service_with_query_mock([])

        service.get_events(resource_type="feature_flag")

        # filter() must have been called at least once
        query_mock.filter.assert_called()

    def test_get_audit_events_filtered_by_actor_id(self):
        """get_events(actor_id=...) applies a filter."""
        service, db, query_mock = self._make_service_with_query_mock([])
        actor_id = str(uuid.uuid4())

        service.get_events(actor_id=actor_id)

        query_mock.filter.assert_called()

    def test_get_audit_events_filtered_by_action(self):
        """get_events(action=AuditAction.CREATE) applies a filter."""
        service, db, query_mock = self._make_service_with_query_mock([])

        service.get_events(action=AuditAction.CREATE)

        query_mock.filter.assert_called()

    def test_get_audit_events_filtered_by_date_range(self):
        """get_events(start_time=..., end_time=...) applies two filters."""
        service, db, query_mock = self._make_service_with_query_mock([])
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = datetime(2024, 12, 31, tzinfo=timezone.utc)

        service.get_events(start_time=start, end_time=end)

        # Should have been called at least twice for start and end filters
        assert query_mock.filter.call_count >= 2

    def test_get_audit_events_default_page_is_1(self):
        """get_events() returns page=1 by default."""
        service, db, _ = self._make_service_with_query_mock([])
        result = service.get_events()
        assert result["page"] == 1

    def test_get_audit_events_default_limit_is_50(self):
        """get_events() uses limit=50 by default."""
        service, db, query_mock = self._make_service_with_query_mock([])
        result = service.get_events()
        assert result["limit"] == 50
        query_mock.limit.assert_called_with(50)

    def test_get_audit_events_no_filters_still_returns_all(self):
        """get_events() with no filters queries without extra filter calls."""
        events = [MagicMock(spec=ComplianceAuditEvent) for _ in range(5)]
        service, db, query_mock = self._make_service_with_query_mock(events, total=5)

        result = service.get_events()

        assert len(result["items"]) == 5
        assert result["total"] == 5

    def test_get_audit_events_second_page_uses_correct_offset(self):
        """get_events(page=2, limit=10) uses offset=10."""
        service, db, query_mock = self._make_service_with_query_mock([])
        service.get_events(page=2, limit=10)
        query_mock.offset.assert_called_with(10)

    def test_get_audit_events_orders_by_timestamp_desc(self):
        """get_events() orders results by timestamp descending."""
        service, db, query_mock = self._make_service_with_query_mock([])
        service.get_events()
        query_mock.order_by.assert_called_once()
