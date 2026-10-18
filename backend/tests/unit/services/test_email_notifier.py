"""
Unit tests for EmailNotifier service (EP-030 Batch 1B).

Tests cover all 20 scenarios:
1.  test_init_with_settings
2.  test_send_when_disabled_returns_false
3.  test_send_email_no_recipients_returns_false
4.  test_send_email_tries_sendgrid_first
5.  test_send_email_falls_back_to_smtp
6.  test_send_email_logs_when_both_unavailable
7.  test_send_via_sendgrid_success
8.  test_send_via_sendgrid_api_error_returns_false
9.  test_send_via_sendgrid_no_api_key_returns_false
10. test_send_via_smtp_success
11. test_send_via_smtp_connection_error_returns_false
12. test_send_safety_rollback_email_success
13. test_send_safety_rollback_email_html_contains_flag_name
14. test_send_experiment_started_email_success
15. test_send_experiment_completed_email_with_winner
16. test_send_experiment_completed_email_no_winner
17. test_send_rollout_completed_email_success
18. test_render_safety_rollback_html_contains_error_rate
19. test_render_experiment_email_html
20. test_exception_never_propagates
"""

import smtplib
from unittest.mock import MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_notifier(
    email_enabled: bool = True,
    sendgrid_api_key: str = "SG.test-key",
    smtp_host: str = "smtp.example.com",
    smtp_port: int = 587,
    smtp_username: str = "user@example.com",
    smtp_password: str = "secret",
    email_from_address: str = "platform@example.com",
    email_from_name: str = "Controlvar",
    notification_admin_emails=None,
):
    """Build an EmailNotifier with mocked settings."""
    if notification_admin_emails is None:
        notification_admin_emails = ["admin@example.com"]

    with patch("backend.app.services.email_notifier.settings") as mock_settings:
        mock_settings.EMAIL_ENABLED = email_enabled
        mock_settings.SENDGRID_API_KEY = sendgrid_api_key
        mock_settings.SMTP_HOST = smtp_host
        mock_settings.SMTP_PORT = smtp_port
        mock_settings.SMTP_USERNAME = smtp_username
        mock_settings.SMTP_PASSWORD = smtp_password
        mock_settings.EMAIL_FROM_ADDRESS = email_from_address
        mock_settings.EMAIL_FROM_NAME = email_from_name
        mock_settings.NOTIFICATION_ADMIN_EMAILS = notification_admin_emails

        from backend.app.services.email_notifier import EmailNotifier

        notifier = EmailNotifier()
        # Persist settings on the object so post-construction they stay consistent
        notifier._enabled = email_enabled
        notifier._sendgrid_api_key = sendgrid_api_key
        notifier._smtp_host = smtp_host
        notifier._smtp_port = smtp_port
        notifier._smtp_username = smtp_username
        notifier._smtp_password = smtp_password
        notifier._from_address = email_from_address
        notifier._from_name = email_from_name
        notifier._admin_emails = notification_admin_emails

    return notifier


# ---------------------------------------------------------------------------
# 1. test_init_with_settings
# ---------------------------------------------------------------------------


class TestInit:
    def test_init_with_settings(self):
        """EmailNotifier reads all required settings in __init__."""
        from backend.app.services.email_notifier import EmailNotifier

        with patch("backend.app.services.email_notifier.settings") as mock_settings:
            mock_settings.EMAIL_ENABLED = True
            mock_settings.SENDGRID_API_KEY = "SG.abc"
            mock_settings.SMTP_HOST = "smtp.test.com"
            mock_settings.SMTP_PORT = 465
            mock_settings.SMTP_USERNAME = "u"
            mock_settings.SMTP_PASSWORD = "p"
            mock_settings.EMAIL_FROM_ADDRESS = "no-reply@test.com"
            mock_settings.EMAIL_FROM_NAME = "Test Platform"
            mock_settings.NOTIFICATION_ADMIN_EMAILS = ["a@test.com"]

            notifier = EmailNotifier()

        assert notifier._enabled is True
        assert notifier._sendgrid_api_key == "SG.abc"
        assert notifier._smtp_host == "smtp.test.com"
        assert notifier._smtp_port == 465
        assert notifier._from_address == "no-reply@test.com"
        assert notifier._from_name == "Test Platform"
        assert notifier._admin_emails == ["a@test.com"]


# ---------------------------------------------------------------------------
# 2. test_send_when_disabled_returns_false
# ---------------------------------------------------------------------------


class TestSendWhenDisabled:
    def test_send_when_disabled_returns_false(self):
        """_send_email returns False immediately when EMAIL_ENABLED is False."""
        notifier = _make_notifier(email_enabled=False)
        result = notifier._send_email(
            to_addresses=["user@example.com"],
            subject="Test",
            html_body="<p>Hello</p>",
        )
        assert result is False


# ---------------------------------------------------------------------------
# 3. test_send_email_no_recipients_returns_false
# ---------------------------------------------------------------------------


class TestSendEmailNoRecipients:
    def test_send_email_no_recipients_returns_false(self):
        """_send_email returns False when the recipients list is empty."""
        notifier = _make_notifier(email_enabled=True)
        result = notifier._send_email(
            to_addresses=[],
            subject="Test",
            html_body="<p>Hello</p>",
        )
        assert result is False


# ---------------------------------------------------------------------------
# 4. test_send_email_tries_sendgrid_first
# ---------------------------------------------------------------------------


class TestSendEmailTriesSendgridFirst:
    def test_send_email_tries_sendgrid_first(self):
        """_send_email calls _send_via_sendgrid first when api key is set."""
        notifier = _make_notifier()

        with (
            patch.object(notifier, "_send_via_sendgrid", return_value=True) as mock_sg,
            patch.object(notifier, "_send_via_smtp", return_value=True) as mock_smtp,
        ):
            result = notifier._send_email(
                to_addresses=["user@example.com"],
                subject="Test",
                html_body="<p>Hello</p>",
            )

        assert result is True
        mock_sg.assert_called_once()
        mock_smtp.assert_not_called()


# ---------------------------------------------------------------------------
# 5. test_send_email_falls_back_to_smtp
# ---------------------------------------------------------------------------


class TestSendEmailFallsBackToSmtp:
    def test_send_email_falls_back_to_smtp(self):
        """_send_email falls back to SMTP when SendGrid fails."""
        notifier = _make_notifier()

        with (
            patch.object(notifier, "_send_via_sendgrid", return_value=False) as mock_sg,
            patch.object(notifier, "_send_via_smtp", return_value=True) as mock_smtp,
        ):
            result = notifier._send_email(
                to_addresses=["user@example.com"],
                subject="Test",
                html_body="<p>Hello</p>",
            )

        assert result is True
        mock_sg.assert_called_once()
        mock_smtp.assert_called_once()


# ---------------------------------------------------------------------------
# 6. test_send_email_logs_when_both_unavailable
# ---------------------------------------------------------------------------


class TestSendEmailLogsBothUnavailable:
    def test_send_email_logs_when_both_unavailable(self):
        """_send_email returns False and logs when both SendGrid and SMTP fail."""
        notifier = _make_notifier()

        with (
            patch.object(notifier, "_send_via_sendgrid", return_value=False),
            patch.object(notifier, "_send_via_smtp", return_value=False),
            patch("backend.app.services.email_notifier.logger") as mock_logger,
        ):
            result = notifier._send_email(
                to_addresses=["user@example.com"],
                subject="Test",
                html_body="<p>Hello</p>",
            )

        assert result is False
        # A warning/error should have been logged
        assert mock_logger.warning.called or mock_logger.error.called


# ---------------------------------------------------------------------------
# 7. test_send_via_sendgrid_success
# ---------------------------------------------------------------------------


class TestSendViaSendgridSuccess:
    def test_send_via_sendgrid_success(self):
        """_send_via_sendgrid returns True on 202 response."""
        notifier = _make_notifier(sendgrid_api_key="SG.validkey")

        mock_response = MagicMock()
        mock_response.status_code = 202

        mock_client = MagicMock()
        mock_client.send.return_value = mock_response

        mock_sendgrid_cls = MagicMock(return_value=mock_client)

        # Mock Mail and other sendgrid helpers so the method body can execute
        mock_mail = MagicMock()
        mock_mail_cls = MagicMock(return_value=mock_mail)
        mock_email_cls = MagicMock()
        mock_to_cls = MagicMock()
        mock_content_cls = MagicMock()

        import backend.app.services.email_notifier as mod

        original_available = mod.SENDGRID_AVAILABLE
        mod.SENDGRID_AVAILABLE = True

        with (
            patch(
                "backend.app.services.email_notifier.SendGridAPIClient",
                mock_sendgrid_cls,
            ),
            patch("backend.app.services.email_notifier.Mail", mock_mail_cls),
            patch("backend.app.services.email_notifier.Email", mock_email_cls),
            patch("backend.app.services.email_notifier.To", mock_to_cls),
            patch("backend.app.services.email_notifier.Content", mock_content_cls),
        ):
            result = notifier._send_via_sendgrid(
                to_addresses=["user@example.com"],
                subject="Test Subject",
                html_body="<p>Hello</p>",
            )

        mod.SENDGRID_AVAILABLE = original_available
        assert result is True


# ---------------------------------------------------------------------------
# 8. test_send_via_sendgrid_api_error_returns_false
# ---------------------------------------------------------------------------


class TestSendViaSendgridApiError:
    def test_send_via_sendgrid_api_error_returns_false(self):
        """_send_via_sendgrid returns False on non-2xx status."""
        notifier = _make_notifier(sendgrid_api_key="SG.validkey")

        mock_response = MagicMock()
        mock_response.status_code = 500

        mock_client = MagicMock()
        mock_client.send.return_value = mock_response

        mock_sg = MagicMock()
        mock_sg.SendGridAPIClient.return_value = mock_client

        import backend.app.services.email_notifier as mod

        original_available = mod.SENDGRID_AVAILABLE
        mod.SENDGRID_AVAILABLE = True

        with patch(
            "backend.app.services.email_notifier.SendGridAPIClient",
            mock_sg.SendGridAPIClient,
        ):
            result = notifier._send_via_sendgrid(
                to_addresses=["user@example.com"],
                subject="Test",
                html_body="<p>Hello</p>",
            )

        mod.SENDGRID_AVAILABLE = original_available
        assert result is False


# ---------------------------------------------------------------------------
# 9. test_send_via_sendgrid_no_api_key_returns_false
# ---------------------------------------------------------------------------


class TestSendViaSendgridNoApiKey:
    def test_send_via_sendgrid_no_api_key_returns_false(self):
        """_send_via_sendgrid returns False when API key is empty."""
        notifier = _make_notifier(sendgrid_api_key="")

        result = notifier._send_via_sendgrid(
            to_addresses=["user@example.com"],
            subject="Test",
            html_body="<p>Hello</p>",
        )
        assert result is False


# ---------------------------------------------------------------------------
# 10. test_send_via_smtp_success
# ---------------------------------------------------------------------------


class TestSendViaSmtpSuccess:
    def test_send_via_smtp_success(self):
        """_send_via_smtp returns True when SMTP connection succeeds."""
        notifier = _make_notifier(smtp_host="smtp.example.com")

        mock_smtp_instance = MagicMock()

        with patch("smtplib.SMTP", return_value=mock_smtp_instance) as mock_smtp_cls:
            mock_smtp_instance.__enter__ = MagicMock(return_value=mock_smtp_instance)
            mock_smtp_instance.__exit__ = MagicMock(return_value=False)

            result = notifier._send_via_smtp(
                to_addresses=["user@example.com"],
                subject="Test Subject",
                html_body="<p>Hello</p>",
            )

        assert result is True


# ---------------------------------------------------------------------------
# 11. test_send_via_smtp_connection_error_returns_false
# ---------------------------------------------------------------------------


class TestSendViaSmtpConnectionError:
    def test_send_via_smtp_connection_error_returns_false(self):
        """_send_via_smtp returns False when connection fails, exception swallowed."""
        notifier = _make_notifier(smtp_host="smtp.example.com")

        with patch(
            "smtplib.SMTP",
            side_effect=smtplib.SMTPConnectError(421, "Connection refused"),
        ):
            result = notifier._send_via_smtp(
                to_addresses=["user@example.com"],
                subject="Test",
                html_body="<p>Hello</p>",
            )

        assert result is False


# ---------------------------------------------------------------------------
# 12. test_send_safety_rollback_email_success
# ---------------------------------------------------------------------------


class TestSendSafetyRollbackEmailSuccess:
    def test_send_safety_rollback_email_success(self):
        """send_safety_rollback_email returns True when _send_email succeeds."""
        notifier = _make_notifier()

        with patch.object(notifier, "_send_email", return_value=True) as mock_send:
            result = notifier.send_safety_rollback_email(
                flag_name="my-feature",
                error_rate=0.15,
                reason="Error rate exceeded threshold",
                recipients=["ops@example.com"],
            )

        assert result is True
        mock_send.assert_called_once()
        args = mock_send.call_args
        assert args[1]["to_addresses"] == ["ops@example.com"]
        assert (
            "my-feature" in args[1]["subject"] or "my-feature" in args[1]["html_body"]
        )


# ---------------------------------------------------------------------------
# 13. test_send_safety_rollback_email_html_contains_flag_name
# ---------------------------------------------------------------------------


class TestSendSafetyRollbackEmailHtmlContainsFlagName:
    def test_send_safety_rollback_email_html_contains_flag_name(self):
        """The HTML generated for safety rollback contains the flag name."""
        notifier = _make_notifier()

        captured = {}

        def capture_send(to_addresses, subject, html_body):
            captured["html_body"] = html_body
            return True

        with patch.object(notifier, "_send_email", side_effect=capture_send):
            notifier.send_safety_rollback_email(
                flag_name="dark-launch-v2",
                error_rate=0.25,
                reason="Too many 500s",
                recipients=["team@example.com"],
            )

        assert "dark-launch-v2" in captured["html_body"]
        assert "25.0%" in captured["html_body"] or "0.25" in captured["html_body"]


# ---------------------------------------------------------------------------
# 14. test_send_experiment_started_email_success
# ---------------------------------------------------------------------------


class TestSendExperimentStartedEmailSuccess:
    def test_send_experiment_started_email_success(self):
        """send_experiment_started_email returns True and sends to owner."""
        notifier = _make_notifier()

        with patch.object(notifier, "_send_email", return_value=True) as mock_send:
            result = notifier.send_experiment_started_email(
                experiment_name="Homepage CTA Test",
                owner_email="researcher@example.com",
                hypothesis="Changing button color increases CTR",
            )

        assert result is True
        mock_send.assert_called_once()
        call_kwargs = mock_send.call_args[1]
        assert "researcher@example.com" in call_kwargs["to_addresses"]


# ---------------------------------------------------------------------------
# 15. test_send_experiment_completed_email_with_winner
# ---------------------------------------------------------------------------


class TestSendExperimentCompletedEmailWithWinner:
    def test_send_experiment_completed_email_with_winner(self):
        """send_experiment_completed_email includes winner info in HTML when provided."""
        notifier = _make_notifier()

        captured = {}

        def capture_send(to_addresses, subject, html_body):
            captured["html_body"] = html_body
            return True

        with patch.object(notifier, "_send_email", side_effect=capture_send):
            notifier.send_experiment_completed_email(
                experiment_name="Checkout Flow Test",
                owner_email="pm@example.com",
                winner="variant_b",
                results_summary="Variant B showed 12% lift",
            )

        assert (
            "variant_b" in captured["html_body"]
            or "Variant B" in captured["html_body"]
            or "winner" in captured["html_body"].lower()
        )
        assert "Checkout Flow Test" in captured["html_body"]


# ---------------------------------------------------------------------------
# 16. test_send_experiment_completed_email_no_winner
# ---------------------------------------------------------------------------


class TestSendExperimentCompletedEmailNoWinner:
    def test_send_experiment_completed_email_no_winner(self):
        """send_experiment_completed_email still sends without a winner."""
        notifier = _make_notifier()

        with patch.object(notifier, "_send_email", return_value=True) as mock_send:
            result = notifier.send_experiment_completed_email(
                experiment_name="Price Test",
                owner_email="analyst@example.com",
            )

        assert result is True
        mock_send.assert_called_once()


# ---------------------------------------------------------------------------
# 17. test_send_rollout_completed_email_success
# ---------------------------------------------------------------------------


class TestSendRolloutCompletedEmailSuccess:
    def test_send_rollout_completed_email_success(self):
        """send_rollout_completed_email returns True when _send_email succeeds."""
        notifier = _make_notifier()

        with patch.object(notifier, "_send_email", return_value=True) as mock_send:
            result = notifier.send_rollout_completed_email(
                flag_name="new-ui",
                recipients=["dev@example.com", "ops@example.com"],
                final_percentage=100,
            )

        assert result is True
        mock_send.assert_called_once()
        call_kwargs = mock_send.call_args[1]
        assert "dev@example.com" in call_kwargs["to_addresses"]
        assert "ops@example.com" in call_kwargs["to_addresses"]


# ---------------------------------------------------------------------------
# 18. test_render_safety_rollback_html_contains_error_rate
# ---------------------------------------------------------------------------


class TestRenderSafetyRollbackHtmlContainsErrorRate:
    def test_render_safety_rollback_html_contains_error_rate(self):
        """_render_safety_rollback_html formats error_rate as a percentage."""
        notifier = _make_notifier()

        html = notifier._render_safety_rollback_html(
            flag_name="beta-feature",
            error_rate=0.073,
            reason="Latency spike detected",
        )

        # Should contain flag name
        assert "beta-feature" in html
        # Should contain the reason
        assert "Latency spike detected" in html
        # Should contain the error rate in some format (7.3% or 0.073)
        assert "7.3%" in html or "0.073" in html or "7.3" in html


# ---------------------------------------------------------------------------
# 19. test_render_experiment_email_html
# ---------------------------------------------------------------------------


class TestRenderExperimentEmailHtml:
    def test_render_experiment_email_html(self):
        """_render_experiment_email_html produces HTML with experiment name and status."""
        notifier = _make_notifier()

        html = notifier._render_experiment_email_html(
            experiment_name="Search Ranking Test",
            status="completed",
            details={"winner": "control", "summary": "No significant difference"},
        )

        assert "Search Ranking Test" in html
        assert "completed" in html.lower() or "Completed" in html


# ---------------------------------------------------------------------------
# 20. test_exception_never_propagates
# ---------------------------------------------------------------------------


class TestExceptionNeverPropagates:
    def test_exception_never_propagates(self):
        """No public method should raise an exception even on catastrophic failure."""
        notifier = _make_notifier()

        # Make _send_email blow up
        with patch.object(
            notifier, "_send_email", side_effect=RuntimeError("Unexpected failure")
        ):
            # None of these should raise
            result1 = notifier.send_safety_rollback_email(
                flag_name="flag",
                error_rate=0.5,
                reason="reason",
                recipients=["a@b.com"],
            )
            result2 = notifier.send_experiment_started_email(
                experiment_name="exp", owner_email="x@y.com"
            )
            result3 = notifier.send_experiment_completed_email(
                experiment_name="exp2", owner_email="x@y.com"
            )
            result4 = notifier.send_rollout_completed_email(
                flag_name="flag2", recipients=["a@b.com"]
            )

        # All should return False (not raise)
        assert result1 is False
        assert result2 is False
        assert result3 is False
        assert result4 is False
