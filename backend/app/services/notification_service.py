"""
Notification service — orchestrates Slack, Email, and Webhook channels.

Sends structured event payloads on key platform events such as experiments
starting/ending, safety rollbacks, and rollout stage advances.

All methods swallow exceptions so that notification failures never disrupt
the primary scheduler logic.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from backend.app.core.config import settings
from backend.app.schemas.scheduler import NotificationEvent
from backend.app.services.safety_service import breach_words, format_metric_reading

logger = logging.getLogger(__name__)


class NotificationService:
    """Orchestrates Slack, Email, and Webhook notifications for scheduler events."""

    def __init__(self):
        self._webhook_url = getattr(settings, "NOTIFICATION_WEBHOOK_URL", "")
        # Lazy-load notifiers to avoid import errors if SDKs are missing
        self._slack = self._make_slack_notifier()
        self._email = self._make_email_notifier()

    # ------------------------------------------------------------------
    # Notifier factories (safe even if notifier modules are missing)
    # ------------------------------------------------------------------

    def _make_slack_notifier(self):
        try:
            from backend.app.services.slack_notifier import SlackNotifier

            return SlackNotifier()
        except Exception:
            return _NullNotifier()

    def _make_email_notifier(self):
        try:
            from backend.app.services.email_notifier import EmailNotifier

            return EmailNotifier()
        except Exception:
            return _NullNotifier()

    # ------------------------------------------------------------------
    # Delivery log helper
    # ------------------------------------------------------------------

    def _log_delivery(
        self,
        db,
        *,
        event_type: str,
        channel,
        recipient: str,
        status,
        subject: Optional[str] = None,
        error_message: Optional[str] = None,
        payload: Optional[dict] = None,
    ) -> None:
        """Record a delivery attempt in the database. Silently skips if db is None."""
        if db is None:
            return
        try:
            from backend.app.models.notification import NotificationDeliveryLog

            record = NotificationDeliveryLog(
                event_type=event_type,
                channel=channel,
                recipient=recipient,
                subject=subject,
                status=status,
                error_message=error_message,
                payload=payload,
            )
            db.add(record)
            db.commit()
        except Exception as exc:
            logger.warning("Failed to log notification delivery: %s", exc)

    # ------------------------------------------------------------------
    # Core webhook send
    # ------------------------------------------------------------------

    def send_webhook(self, url: str, event: NotificationEvent) -> bool:
        """POST a NotificationEvent as JSON to the given webhook URL."""
        try:
            payload = event.model_dump()
            response = httpx.post(url, json=payload, timeout=5)
            if 200 <= response.status_code < 300:
                logger.debug(
                    "Webhook notification sent successfully to %s (status=%d)",
                    url,
                    response.status_code,
                )
                return True
            else:
                logger.warning(
                    "Webhook notification failed: url=%s status=%d",
                    url,
                    response.status_code,
                )
                return False
        except Exception as exc:
            logger.error("Webhook notification error for url=%s: %s", url, exc)
            return False

    def _send(self, event: NotificationEvent) -> bool:
        """Send an event to the configured webhook URL, if set."""
        url = self._webhook_url
        if not url:
            logger.debug(
                "Webhook skipped (NOTIFICATION_WEBHOOK_URL not configured): event_type=%s",
                event.event_type,
            )
            return False
        return self.send_webhook(url, event)

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    # ------------------------------------------------------------------
    # Convenience notification helpers
    # ------------------------------------------------------------------

    def notify_experiment_started(
        self,
        experiment_id: str,
        experiment_name: str,
        owner_email: Optional[str] = None,
        db=None,
        resumed: bool = False,
    ) -> bool:
        """Notify all channels that an experiment has been automatically started.

        ``resumed`` is true when the scheduler activated a PAUSED experiment
        at its scheduled resume time; the message then says it was resumed.
        The event type is ``experiment_started`` either way.
        """
        verb = "resumed" if resumed else "started"
        try:
            event = NotificationEvent(
                event_type="experiment_started",
                experiment_id=experiment_id,
                new_status="active",
                message=f"Experiment '{experiment_name}' has been {verb} automatically.",
                timestamp=self._now_iso(),
                metadata={"experiment_name": experiment_name},
            )
            webhook_ok = self._send(event)

            # Slack
            try:
                self._slack.send_experiment_started(
                    experiment_name=experiment_name,
                    owner_email=owner_email,
                )
            except Exception as exc:
                logger.warning("Slack experiment_started failed: %s", exc)

            # Email
            if owner_email:
                try:
                    self._email.send_experiment_started_email(
                        experiment_name=experiment_name,
                        owner_email=owner_email,
                    )
                except Exception as exc:
                    logger.warning("Email experiment_started failed: %s", exc)

            return webhook_ok
        except Exception as exc:
            logger.error("notify_experiment_started error: %s", exc)
            return False

    def notify_experiment_ended(
        self,
        experiment_id: str,
        experiment_name: str,
        winning_variant: Optional[str] = None,
        owner_email: Optional[str] = None,
        db=None,
    ) -> bool:
        """Notify all channels that an experiment has been completed."""
        try:
            msg = f"Experiment '{experiment_name}' has been completed automatically."
            metadata = {"experiment_name": experiment_name}
            if winning_variant:
                msg += f" Winning variant: {winning_variant}."
                metadata["winning_variant"] = winning_variant

            event = NotificationEvent(
                event_type="experiment_ended",
                experiment_id=experiment_id,
                old_status="active",
                new_status="completed",
                message=msg,
                timestamp=self._now_iso(),
                metadata=metadata,
            )
            webhook_ok = self._send(event)

            # Slack
            try:
                self._slack.send_experiment_completed(
                    experiment_name=experiment_name,
                    winner=winning_variant,
                )
            except Exception as exc:
                logger.warning("Slack experiment_ended failed: %s", exc)

            # Email
            if owner_email:
                try:
                    self._email.send_experiment_completed_email(
                        experiment_name=experiment_name,
                        owner_email=owner_email,
                        winner=winning_variant,
                    )
                except Exception as exc:
                    logger.warning("Email experiment_ended failed: %s", exc)

            return webhook_ok
        except Exception as exc:
            logger.error("notify_experiment_ended error: %s", exc)
            return False

    def notify_safety_rollback(
        self,
        feature_flag_id: str,
        feature_flag_name: str,
        reason: str,
        metric_name: Optional[str] = None,
        value: Optional[float] = None,
        threshold: Optional[float] = None,
        db=None,
        comparison: Optional[str] = None,
    ) -> bool:
        """Notify all channels that a feature flag was automatically rolled back.

        ``metric_name``, ``value`` and ``threshold`` describe the metric that
        breached its threshold. The Slack alert and the webhook ``message``
        show them with the metric's unit (``format_metric_reading``: an error
        rate as a percentage, a latency in ms) and say ``above``, ``below`` or
        ``equal to`` the threshold by ``comparison``, the threshold's
        ``comparison_type`` (``breach_words``; None reads as ``greater_than``).
        The webhook ``metadata`` carries the raw numbers, with ``error_rate``
        set only when the metric is ``error_rate``. Without a metric the alert
        gives only the reason.
        """
        try:
            label = value_text = threshold_text = relation = None
            summary = ""
            if metric_name and value is not None and threshold is not None:
                label = metric_name.replace("_", " ")
                value_text = format_metric_reading(metric_name, value)
                threshold_text = format_metric_reading(metric_name, threshold)
                relation, _ = breach_words(comparison)
                summary = (
                    f": {label} {value_text} {relation} threshold {threshold_text}"
                )

            event = NotificationEvent(
                event_type="safety_rollback",
                feature_flag_id=feature_flag_id,
                message=(
                    f"Feature flag '{feature_flag_name}' was automatically rolled "
                    f"back{summary}. Reason: {reason}"
                ),
                timestamp=self._now_iso(),
                metadata={
                    "feature_flag_name": feature_flag_name,
                    "reason": reason,
                    "metric": metric_name,
                    "value": value,
                    "threshold": threshold,
                    "error_rate": value if metric_name == "error_rate" else None,
                },
            )
            webhook_ok = self._send(event)

            # Slack
            try:
                self._slack.send_safety_rollback_alert(
                    flag_name=feature_flag_name,
                    reason=reason,
                    metric_label=label,
                    value=value_text,
                    threshold=threshold_text,
                    relation=relation or "above",
                )
            except Exception as exc:
                logger.warning("Slack safety_rollback failed: %s", exc)

            return webhook_ok
        except Exception as exc:
            logger.error("notify_safety_rollback error: %s", exc)
            return False

    def notify_rollout_advanced(
        self,
        feature_flag_id: str,
        stage_name: str,
        new_percentage: int,
        from_percentage: int = 0,
        db=None,
    ) -> bool:
        """Notify all channels that a rollout schedule has advanced to a new stage."""
        try:
            event = NotificationEvent(
                event_type="rollout_advanced",
                feature_flag_id=feature_flag_id,
                message=(
                    f"Rollout advanced to stage '{stage_name}' — "
                    f"feature flag is now at {new_percentage}% rollout."
                ),
                timestamp=self._now_iso(),
                metadata={
                    "stage_name": stage_name,
                    "new_percentage": new_percentage,
                    "from_percentage": from_percentage,
                },
            )
            webhook_ok = self._send(event)

            # Slack
            try:
                self._slack.send_rollout_advanced(
                    flag_name=feature_flag_id,
                    from_pct=from_percentage,
                    to_pct=new_percentage,
                    stage_name=stage_name,
                )
            except Exception as exc:
                logger.warning("Slack rollout_advanced failed: %s", exc)

            return webhook_ok
        except Exception as exc:
            logger.error("notify_rollout_advanced error: %s", exc)
            return False


# ---------------------------------------------------------------------------
# Null notifier — used when SDK is unavailable
# ---------------------------------------------------------------------------


class _NullNotifier:
    """No-op notifier that returns False for all calls."""

    def __getattr__(self, name):
        def _noop(*args, **kwargs):
            return False

        return _noop
