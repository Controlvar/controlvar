"""
EmailNotifier service for sending HTML email notifications (EP-030 Batch 1B).

Supports:
- SendGrid (preferred) with graceful fallback to SMTP
- HTML email templates using simple string formatting
- Swallows all exceptions so notification failures never disrupt platform logic

Design rules:
- All public methods return True on success, False on failure
- Exceptions are NEVER propagated — they are logged and False is returned
- Email is opt-in: EMAIL_ENABLED must be True in settings
"""

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Dict, List, Optional

from backend.app.core.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional dependency: sendgrid
# ---------------------------------------------------------------------------

try:
    import sendgrid
    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Content, Email, Mail, To

    SENDGRID_AVAILABLE = True
except ImportError:
    SENDGRID_AVAILABLE = False
    # Define stubs so the rest of the module can reference them without errors;
    # actual usage is always guarded by SENDGRID_AVAILABLE checks.
    sendgrid = None  # type: ignore[assignment]
    SendGridAPIClient = None  # type: ignore[assignment]
    Mail = None  # type: ignore[assignment]
    Email = None  # type: ignore[assignment]
    To = None  # type: ignore[assignment]
    Content = None  # type: ignore[assignment]

# smtplib is stdlib — always available
SMTP_AVAILABLE = True


# ---------------------------------------------------------------------------
# EmailNotifier
# ---------------------------------------------------------------------------


class EmailNotifier:
    """Send HTML email notifications via SendGrid or SMTP."""

    def __init__(self) -> None:
        self._enabled: bool = getattr(settings, "EMAIL_ENABLED", False)
        self._sendgrid_api_key: str = getattr(settings, "SENDGRID_API_KEY", "")
        self._smtp_host: str = getattr(settings, "SMTP_HOST", "")
        self._smtp_port: int = getattr(settings, "SMTP_PORT", 587)
        self._smtp_username: str = getattr(settings, "SMTP_USERNAME", "")
        self._smtp_password: str = getattr(settings, "SMTP_PASSWORD", "")
        self._from_address: str = getattr(
            settings, "EMAIL_FROM_ADDRESS", "platform@example.com"
        )
        self._from_name: str = getattr(settings, "EMAIL_FROM_NAME", "Controlvar")
        self._admin_emails: List[str] = list(
            getattr(settings, "NOTIFICATION_ADMIN_EMAILS", []) or []
        )

    # ------------------------------------------------------------------
    # Public notification methods
    # ------------------------------------------------------------------

    def send_safety_rollback_email(
        self,
        flag_name: str,
        error_rate: float,
        reason: str,
        recipients: List[str],
    ) -> bool:
        """
        Send an alert email when a safety rollback is triggered.

        Args:
            flag_name: The name/key of the rolled-back feature flag.
            error_rate: The observed error rate that triggered the rollback (0.0–1.0).
            reason: Human-readable reason for the rollback.
            recipients: List of email addresses to notify.

        Returns:
            True if sent successfully, False otherwise.
        """
        try:
            subject = (
                f"[Safety Rollback] Feature flag '{flag_name}' has been rolled back"
            )
            html_body = self._render_safety_rollback_html(flag_name, error_rate, reason)
            return self._send_email(
                to_addresses=recipients,
                subject=subject,
                html_body=html_body,
            )
        except Exception:
            logger.exception("Unexpected error in send_safety_rollback_email")
            return False

    def send_experiment_started_email(
        self,
        experiment_name: str,
        owner_email: str,
        hypothesis: Optional[str] = None,
    ) -> bool:
        """
        Notify the experiment owner that their experiment has started.

        Args:
            experiment_name: Display name of the experiment.
            owner_email: Email address of the experiment owner.
            hypothesis: Optional hypothesis text.

        Returns:
            True if sent successfully, False otherwise.
        """
        try:
            subject = f"[Experiment Started] {experiment_name}"
            details: Dict = {}
            if hypothesis:
                details["hypothesis"] = hypothesis
            html_body = self._render_experiment_email_html(
                experiment_name=experiment_name,
                status="started",
                details=details if details else None,
            )
            return self._send_email(
                to_addresses=[owner_email],
                subject=subject,
                html_body=html_body,
            )
        except Exception:
            logger.exception("Unexpected error in send_experiment_started_email")
            return False

    def send_experiment_completed_email(
        self,
        experiment_name: str,
        owner_email: str,
        winner: Optional[str] = None,
        results_summary: Optional[str] = None,
    ) -> bool:
        """
        Notify the experiment owner that their experiment has completed.

        Args:
            experiment_name: Display name of the experiment.
            owner_email: Email address of the experiment owner.
            winner: Optional winning variant name.
            results_summary: Optional free-text summary of results.

        Returns:
            True if sent successfully, False otherwise.
        """
        try:
            subject = f"[Experiment Completed] {experiment_name}"
            details: Dict = {}
            if winner:
                details["winner"] = winner
            if results_summary:
                details["summary"] = results_summary
            html_body = self._render_experiment_email_html(
                experiment_name=experiment_name,
                status="completed",
                details=details if details else None,
            )
            return self._send_email(
                to_addresses=[owner_email],
                subject=subject,
                html_body=html_body,
            )
        except Exception:
            logger.exception("Unexpected error in send_experiment_completed_email")
            return False

    def send_rollout_completed_email(
        self,
        flag_name: str,
        recipients: List[str],
        final_percentage: int = 100,
    ) -> bool:
        """
        Notify recipients that a feature flag rollout has completed.

        Args:
            flag_name: The name/key of the feature flag.
            recipients: List of email addresses to notify.
            final_percentage: The final rollout percentage reached.

        Returns:
            True if sent successfully, False otherwise.
        """
        try:
            subject = f"[Rollout Completed] Feature flag '{flag_name}' reached {final_percentage}%"
            html_body = self._render_rollout_email_html(
                flag_name=flag_name,
                percentage=final_percentage,
                status="completed",
            )
            return self._send_email(
                to_addresses=recipients,
                subject=subject,
                html_body=html_body,
            )
        except Exception:
            logger.exception("Unexpected error in send_rollout_completed_email")
            return False

    # ------------------------------------------------------------------
    # Core transport method
    # ------------------------------------------------------------------

    def _send_email(
        self,
        to_addresses: List[str],
        subject: str,
        html_body: str,
    ) -> bool:
        """
        Dispatch an email, trying SendGrid first then SMTP.

        Returns False immediately if email is disabled or no recipients given.
        Logs a warning if both transports fail.
        """
        if not self._enabled:
            logger.debug("Email notifications are disabled (EMAIL_ENABLED=False)")
            return False

        if not to_addresses:
            logger.debug("_send_email called with empty recipients list — skipping")
            return False

        # Attempt SendGrid first (if API key is configured)
        if self._sendgrid_api_key:
            if self._send_via_sendgrid(to_addresses, subject, html_body):
                return True

        # Fall back to SMTP
        if self._smtp_host:
            if self._send_via_smtp(to_addresses, subject, html_body):
                return True

        logger.warning(
            "Failed to send email '%s' to %s — both SendGrid and SMTP unavailable or failed",
            subject,
            to_addresses,
        )
        return False

    # ------------------------------------------------------------------
    # Transport implementations
    # ------------------------------------------------------------------

    def _send_via_sendgrid(
        self,
        to_addresses: List[str],
        subject: str,
        html_body: str,
    ) -> bool:
        """
        Send via SendGrid API.

        Returns True on 2xx response, False otherwise.
        Swallows all exceptions.
        """
        if not self._sendgrid_api_key:
            logger.debug("SendGrid API key is not configured — skipping SendGrid")
            return False

        if not SENDGRID_AVAILABLE or SendGridAPIClient is None:
            logger.debug("sendgrid package is not installed — skipping SendGrid")
            return False

        try:
            from_email = Email(self._from_address, self._from_name)
            message = Mail()
            message.from_email = from_email
            message.subject = subject
            message.add_content(Content("text/html", html_body))

            for addr in to_addresses:
                message.add_to(To(addr))

            client = SendGridAPIClient(self._sendgrid_api_key)
            response = client.send(message)

            if 200 <= response.status_code < 300:
                logger.debug(
                    "Email sent via SendGrid to %s (status=%d)",
                    to_addresses,
                    response.status_code,
                )
                return True

            logger.warning(
                "SendGrid returned non-2xx status %d for email '%s'",
                response.status_code,
                subject,
            )
            return False

        except Exception:
            logger.exception("Error sending email via SendGrid")
            return False

    def _send_via_smtp(
        self,
        to_addresses: List[str],
        subject: str,
        html_body: str,
    ) -> bool:
        """
        Send via SMTP (stdlib smtplib).

        Returns True on success, False otherwise.
        Swallows all exceptions.
        """
        if not self._smtp_host:
            logger.debug("SMTP_HOST is not configured — skipping SMTP")
            return False

        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = f"{self._from_name} <{self._from_address}>"
            msg["To"] = ", ".join(to_addresses)

            part = MIMEText(html_body, "html")
            msg.attach(part)

            with smtplib.SMTP(self._smtp_host, self._smtp_port) as server:
                server.ehlo()
                if self._smtp_username and self._smtp_password:
                    server.starttls()
                    server.login(self._smtp_username, self._smtp_password)
                server.sendmail(self._from_address, to_addresses, msg.as_string())

            logger.debug("Email sent via SMTP to %s", to_addresses)
            return True

        except Exception:
            logger.exception("Error sending email via SMTP")
            return False

    # ------------------------------------------------------------------
    # HTML template renderers
    # ------------------------------------------------------------------

    def _render_safety_rollback_html(
        self,
        flag_name: str,
        error_rate: float,
        reason: str,
    ) -> str:
        """Render a safety rollback alert email as an HTML string."""
        return f"""
<html>
<body style="font-family: Arial, sans-serif; color: #333;">
  <h2 style="color: #cc0000;">Safety Rollback Alert</h2>
  <p>Controlvar has automatically rolled back a feature flag
  due to a safety threshold violation.</p>
  <table style="border-collapse: collapse; width: 100%; max-width: 600px;">
    <tr>
      <td style="padding: 8px; border: 1px solid #ddd; font-weight: bold;">Feature Flag</td>
      <td style="padding: 8px; border: 1px solid #ddd;">{flag_name}</td>
    </tr>
    <tr>
      <td style="padding: 8px; border: 1px solid #ddd; font-weight: bold;">Error Rate</td>
      <td style="padding: 8px; border: 1px solid #ddd;">{error_rate:.1%}</td>
    </tr>
    <tr>
      <td style="padding: 8px; border: 1px solid #ddd; font-weight: bold;">Reason</td>
      <td style="padding: 8px; border: 1px solid #ddd;">{reason}</td>
    </tr>
  </table>
  <p style="color: #666; font-size: 12px; margin-top: 24px;">
    This is an automated message from Controlvar.
  </p>
</body>
</html>
"""

    def _render_experiment_email_html(
        self,
        experiment_name: str,
        status: str,
        details: Optional[Dict] = None,
    ) -> str:
        """Render an experiment lifecycle notification as an HTML string."""
        status_display = status.capitalize()
        status_color = "#2ecc71" if status == "completed" else "#3498db"

        details_rows = ""
        if details:
            for key, value in details.items():
                label = key.replace("_", " ").capitalize()
                details_rows += f"""
    <tr>
      <td style="padding: 8px; border: 1px solid #ddd; font-weight: bold;">{label}</td>
      <td style="padding: 8px; border: 1px solid #ddd;">{value}</td>
    </tr>"""

        winner_section = ""
        if details and "winner" in details:
            winner_section = f"""
  <p><strong>Winner:</strong> {details["winner"]}</p>
"""

        return f"""
<html>
<body style="font-family: Arial, sans-serif; color: #333;">
  <h2 style="color: {status_color};">Experiment {status_display}</h2>
  <p>The experiment <strong>{experiment_name}</strong> has {status}.</p>
  {winner_section}
  {f'<table style="border-collapse: collapse; width: 100%; max-width: 600px;">{details_rows}</table>' if details_rows else ""}
  <p style="color: #666; font-size: 12px; margin-top: 24px;">
    This is an automated message from Controlvar.
  </p>
</body>
</html>
"""

    def _render_rollout_email_html(
        self,
        flag_name: str,
        percentage: int,
        status: str,
    ) -> str:
        """Render a rollout lifecycle notification as an HTML string."""
        status_display = status.capitalize()

        return f"""
<html>
<body style="font-family: Arial, sans-serif; color: #333;">
  <h2 style="color: #27ae60;">Rollout {status_display}</h2>
  <p>The gradual rollout for feature flag <strong>{flag_name}</strong> has {status}.</p>
  <table style="border-collapse: collapse; width: 100%; max-width: 600px;">
    <tr>
      <td style="padding: 8px; border: 1px solid #ddd; font-weight: bold;">Feature Flag</td>
      <td style="padding: 8px; border: 1px solid #ddd;">{flag_name}</td>
    </tr>
    <tr>
      <td style="padding: 8px; border: 1px solid #ddd; font-weight: bold;">Final Rollout Percentage</td>
      <td style="padding: 8px; border: 1px solid #ddd;">{percentage}%</td>
    </tr>
    <tr>
      <td style="padding: 8px; border: 1px solid #ddd; font-weight: bold;">Status</td>
      <td style="padding: 8px; border: 1px solid #ddd;">{status_display}</td>
    </tr>
  </table>
  <p style="color: #666; font-size: 12px; margin-top: 24px;">
    This is an automated message from Controlvar.
  </p>
</body>
</html>
"""
