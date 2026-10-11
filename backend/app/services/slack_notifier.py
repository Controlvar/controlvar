"""
Slack notification service for the experimentation platform.

Sends structured Slack Block Kit messages to configured channels when
significant platform events occur: safety rollbacks, experiment lifecycle
changes, rollout stage advances, and generic alerts.

Design rules:
- ALL public methods swallow exceptions — notification failure must NEVER raise.
- Uses slack_sdk.WebClient (not httpx directly).
- Token, default channel, and enabled flag are read from application settings.
- Returns True on success, False on any failure.
- If SLACK_ENABLED is False or the token is empty, logs debug and returns False.
- If slack_sdk is not installed, all methods return False gracefully.
"""

import logging
from typing import Dict, List, Optional

from backend.app.core.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional SDK import — graceful degradation when slack_sdk is not installed
# ---------------------------------------------------------------------------

try:
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    SLACK_SDK_AVAILABLE = True
except ImportError:  # pragma: no cover
    SLACK_SDK_AVAILABLE = False
    # Placeholders so the type system (and mocks) remain consistent
    WebClient = None  # type: ignore[assignment,misc]
    SlackApiError = Exception  # type: ignore[assignment,misc]


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class SlackNotifier:
    """Sends Slack Block Kit notifications for platform events."""

    def __init__(self) -> None:
        self._enabled: bool = getattr(settings, "SLACK_ENABLED", False)
        self._token: str = getattr(settings, "SLACK_BOT_TOKEN", "")
        self._default_channel: str = getattr(
            settings, "SLACK_DEFAULT_CHANNEL", "#platform-alerts"
        )
        self._sdk_available: bool = SLACK_SDK_AVAILABLE

    # ------------------------------------------------------------------
    # Core send method
    # ------------------------------------------------------------------

    def _send_message(
        self,
        channel: Optional[str],
        blocks: List[dict],
        text: str,
    ) -> bool:
        """
        Post a Block Kit message to Slack.

        Args:
            channel: Slack channel to post to. Falls back to _default_channel
                     when None.
            blocks:  Slack Block Kit block list.
            text:    Fallback plain-text summary (for notifications/accessibility).

        Returns:
            True on success, False on any failure (all exceptions swallowed).
        """
        if not self._enabled:
            logger.debug(
                "Slack notifications are disabled (SLACK_ENABLED=False); skipping."
            )
            return False

        if not self._token:
            logger.debug(
                "Slack bot token is empty (SLACK_BOT_TOKEN not set); skipping."
            )
            return False

        if not self._sdk_available:
            logger.debug("slack_sdk is not installed; Slack notification skipped.")
            return False

        target_channel = channel or self._default_channel

        try:
            client = WebClient(token=self._token)
            client.chat_postMessage(
                channel=target_channel,
                blocks=blocks,
                text=text,
            )
            logger.debug(
                "Slack message sent successfully to channel=%s", target_channel
            )
            return True
        except SlackApiError as exc:
            logger.error("SlackApiError sending to channel=%s: %s", target_channel, exc)
            return False
        except Exception as exc:
            logger.error(
                "Unexpected error sending Slack message to channel=%s: %s",
                target_channel,
                exc,
            )
            return False

    # ------------------------------------------------------------------
    # Block Kit builders
    # ------------------------------------------------------------------

    def _build_safety_block(
        self,
        flag_name: str,
        reason: str,
        metric_label: Optional[str] = None,
        value: Optional[str] = None,
        threshold: Optional[str] = None,
    ) -> List[dict]:
        """Build Block Kit blocks for a safety rollback alert.

        ``value`` and ``threshold`` arrive formatted with their unit; without
        a metric the alert shows the flag and the reason only.
        """
        fields = [{"type": "mrkdwn", "text": f"*Flag:*\n{flag_name}"}]
        if metric_label:
            fields += [
                {"type": "mrkdwn", "text": f"*{metric_label.title()}:*\n{value}"},
                {"type": "mrkdwn", "text": f"*Threshold:*\n{threshold}"},
            ]
        fields.append({"type": "mrkdwn", "text": f"*Reason:*\n{reason}"})
        return [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "🚨 Safety Rollback Alert",
                },
            },
            {"type": "section", "fields": fields},
            {"type": "divider"},
        ]

    def _build_experiment_block(
        self,
        name: str,
        status: str,
        details: Optional[Dict] = None,
    ) -> List[dict]:
        """Build Block Kit blocks for an experiment lifecycle notification."""
        fields = [
            {"type": "mrkdwn", "text": f"*Experiment:*\n{name}"},
            {"type": "mrkdwn", "text": f"*Status:*\n{status}"},
        ]

        if details:
            for key, value in details.items():
                fields.append({"type": "mrkdwn", "text": f"*{key.title()}:*\n{value}"})

        return [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"Experiment {status.title()}",
                },
            },
            {"type": "section", "fields": fields},
            {"type": "divider"},
        ]

    # ------------------------------------------------------------------
    # Public notification methods
    # ------------------------------------------------------------------

    def send_safety_rollback_alert(
        self,
        flag_name: str,
        reason: str,
        metric_label: Optional[str] = None,
        value: Optional[str] = None,
        threshold: Optional[str] = None,
        channel: Optional[str] = None,
        relation: str = "above",
    ) -> bool:
        """
        Send a Slack alert when a feature flag has been automatically rolled back
        by the safety monitoring system.

        Args:
            metric_label: The breaching metric, as words (``"error rate"``,
                          ``"avg latency"``); None when no single metric is known.
            value:        Its measured value, formatted with its unit (``"7.143%"``,
                          ``"523.5 ms"``).
            threshold:    Its threshold, formatted the same way.
            relation:     How the value stands to the threshold, as the alert's
                          line words it: ``"above"`` (the default), ``"below"``
                          or ``"equal to"``.

        Returns:
            True on success, False on any failure (exceptions swallowed).
        """
        try:
            blocks = self._build_safety_block(
                flag_name=flag_name,
                reason=reason,
                metric_label=metric_label,
                value=value,
                threshold=threshold,
            )
            if metric_label:
                text = (
                    f"Safety Rollback: '{flag_name}' rolled back — "
                    f"{metric_label} {value} {relation} threshold {threshold}."
                )
            else:
                text = f"Safety Rollback: '{flag_name}' rolled back. Reason: {reason}"
            return self._send_message(channel=channel, blocks=blocks, text=text)
        except Exception as exc:
            logger.error("Error in send_safety_rollback_alert: %s", exc)
            return False

    def send_experiment_started(
        self,
        experiment_name: str,
        owner_email: Optional[str] = None,
        channel: Optional[str] = None,
    ) -> bool:
        """
        Send a Slack notification when an experiment has started.

        Returns:
            True on success, False on any failure (exceptions swallowed).
        """
        try:
            details: Dict = {}
            if owner_email:
                details["owner"] = owner_email

            blocks = self._build_experiment_block(
                name=experiment_name,
                status="started",
                details=details if details else None,
            )
            text = f"Experiment '{experiment_name}' has started."
            return self._send_message(channel=channel, blocks=blocks, text=text)
        except Exception as exc:
            logger.error("Error in send_experiment_started: %s", exc)
            return False

    def send_experiment_completed(
        self,
        experiment_name: str,
        winner: Optional[str] = None,
        channel: Optional[str] = None,
    ) -> bool:
        """
        Send a Slack notification when an experiment has completed.

        Returns:
            True on success, False on any failure (exceptions swallowed).
        """
        try:
            details: Dict = {}
            if winner:
                details["winner"] = winner

            blocks = self._build_experiment_block(
                name=experiment_name,
                status="completed",
                details=details if details else None,
            )
            text = f"Experiment '{experiment_name}' has completed."
            if winner:
                text += f" Winner: {winner}."
            return self._send_message(channel=channel, blocks=blocks, text=text)
        except Exception as exc:
            logger.error("Error in send_experiment_completed: %s", exc)
            return False

    def send_rollout_advanced(
        self,
        flag_name: str,
        from_pct: int,
        to_pct: int,
        stage_name: Optional[str] = None,
        channel: Optional[str] = None,
    ) -> bool:
        """
        Send a Slack notification when a rollout schedule advances to a new stage.

        Returns:
            True on success, False on any failure (exceptions swallowed).
        """
        try:
            stage_label = f" (stage: {stage_name})" if stage_name else ""
            blocks = [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": "Rollout Advanced",
                    },
                },
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*Flag:*\n{flag_name}"},
                        {
                            "type": "mrkdwn",
                            "text": f"*Progress:*\n{from_pct}% → {to_pct}%",
                        },
                        *(
                            [{"type": "mrkdwn", "text": f"*Stage:*\n{stage_name}"}]
                            if stage_name
                            else []
                        ),
                    ],
                },
                {"type": "divider"},
            ]
            text = (
                f"Rollout for '{flag_name}' advanced from {from_pct}% to "
                f"{to_pct}%{stage_label}."
            )
            return self._send_message(channel=channel, blocks=blocks, text=text)
        except Exception as exc:
            logger.error("Error in send_rollout_advanced: %s", exc)
            return False

    def send_rollout_completed(
        self,
        flag_name: str,
        final_pct: int = 100,
        channel: Optional[str] = None,
    ) -> bool:
        """
        Send a Slack notification when a rollout schedule has fully completed.

        Returns:
            True on success, False on any failure (exceptions swallowed).
        """
        try:
            blocks = [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": "Rollout Completed",
                    },
                },
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*Flag:*\n{flag_name}"},
                        {
                            "type": "mrkdwn",
                            "text": f"*Final Rollout:*\n{final_pct}%",
                        },
                    ],
                },
                {"type": "divider"},
            ]
            text = f"Rollout for '{flag_name}' is complete at {final_pct}% of users."
            return self._send_message(channel=channel, blocks=blocks, text=text)
        except Exception as exc:
            logger.error("Error in send_rollout_completed: %s", exc)
            return False

    def send_generic_alert(
        self,
        title: str,
        message: str,
        severity: str = "info",
        channel: Optional[str] = None,
    ) -> bool:
        """
        Send a generic Slack alert with a title, message, and severity level.

        Args:
            title:    Short summary shown in the header block.
            message:  Full message body shown in the section block.
            severity: One of "info", "warning", "error", "critical".
                      Visually distinguished in the header text.
            channel:  Optional override channel.

        Returns:
            True on success, False on any failure (exceptions swallowed).
        """
        try:
            severity_emoji = {
                "info": "ℹ️",
                "warning": "⚠️",
                "error": "🔴",
                "critical": "🚨",
            }.get(severity.lower(), "ℹ️")

            blocks = [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": f"{severity_emoji} {title}",
                    },
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": message,
                    },
                },
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": f"Severity: *{severity}*",
                        }
                    ],
                },
                {"type": "divider"},
            ]
            return self._send_message(
                channel=channel,
                blocks=blocks,
                text=f"[{severity.upper()}] {title}: {message}",
            )
        except Exception as exc:
            logger.error("Error in send_generic_alert: %s", exc)
            return False
