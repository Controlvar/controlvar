"""What an automatic rollback leaves behind: its alert's channels and its record.

* ``NotificationService.notify_safety_rollback``, the one notification the
  safety monitor sends after an automatic rollback, goes to the webhook and
  Slack and never to e-mail.
* ``safety_rollback_records`` holds exactly the columns a rollback writes,
  with no column of its own for the breaching metric's value or threshold.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from backend.app.models.safety import SafetyRollbackRecord
from backend.app.services.notification_service import NotificationService

pytestmark = [pytest.mark.unit]


def test_an_automatic_rollback_alert_goes_to_the_webhook_and_slack_and_sends_no_email():
    service = NotificationService()
    service._slack = MagicMock()
    service._email = MagicMock()
    with patch.object(service, "_send", return_value=True) as webhook:
        service.notify_safety_rollback(
            feature_flag_id="flag-1",
            feature_flag_name="new-checkout-flow",
            reason="Automatic rollback due to error_rate above threshold (10% > 5%)",
            metric_name="error_rate",
            value=0.1,
            threshold=0.05,
            comparison="greater_than",
        )
    webhook.assert_called_once()
    assert webhook.call_args.args[0].event_type == "safety_rollback"
    service._slack.send_safety_rollback_alert.assert_called_once()
    assert service._email.method_calls == []


def test_a_rollback_record_has_exactly_these_columns():
    assert {column.name for column in SafetyRollbackRecord.__table__.columns} == {
        "id",
        "feature_flag_id",
        "safety_config_id",
        "trigger_type",
        "trigger_reason",
        "previous_percentage",
        "target_percentage",
        "success",
        "executed_by_user_id",
        "created_at",
        "updated_at",
    }
