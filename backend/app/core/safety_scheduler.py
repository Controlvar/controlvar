"""
Scheduler for feature flag safety monitoring.

This module provides scheduling functionality for automatically
monitoring feature flags for safety issues and triggering rollbacks.
"""

import asyncio
from typing import Any, Dict, Optional

from backend.app.core.config import settings as app_settings
from backend.app.core.logging import get_logger
from backend.app.core.scheduler_tick import run_locked_tick
from backend.app.db.session import SessionLocal
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.safety import RollbackTriggerType
from backend.app.services.notification_service import NotificationService
from backend.app.services.safety_service import (
    SafetyService,
    breach_comparison,
    describe_breach,
    rollback_change,
)

logger = get_logger(__name__)

SCHEDULER_NAME = "safety"


def _rollback_target_percentage(config: Any) -> int:
    """
    The percentage an automatic rollback should set the flag to.

    Reads ``rollback_percentage`` from the flag's safety config and clamps it to
    0-100; anything missing or non-numeric rolls back to 0 (fully off).

    The API refuses a value outside 0-100 (#629), but a row stored before that
    still reads back as it is. Clamping it here, with a warning naming the
    flag, keeps the monitor rolling that flag back instead of skipping it.
    """
    value = getattr(config, "rollback_percentage", None)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    clamped = max(0, min(100, int(value)))
    if not 0 <= value <= 100:
        logger.warning(
            "Stored rollback_percentage %s for feature flag %s is outside 0-100; "
            "using %s",
            value,
            getattr(config, "feature_flag_id", None),
            clamped,
        )
    return clamped


class SafetyScheduler:
    """Handles scheduled tasks for feature flag safety monitoring."""

    def __init__(self, interval_minutes: Optional[int] = None):
        """
        Initialize the safety scheduler.

        Args:
            interval_minutes: How often to check feature flags for safety issues
                (in minutes). Defaults to ``settings.SAFETY_CHECK_INTERVAL_MINUTES``.
        """
        if interval_minutes is None:
            interval_minutes = app_settings.SAFETY_CHECK_INTERVAL_MINUTES
        self.interval_minutes = interval_minutes
        self.is_running = False
        self.task: Optional[asyncio.Task] = None
        self._notification_service = NotificationService()

    async def start(self):
        """Start the scheduler."""
        if self.is_running:
            logger.warning("Safety scheduler is already running")
            return

        self.is_running = True
        self.task = asyncio.create_task(self._run_scheduler())
        logger.info(
            f"Safety scheduler started with {self.interval_minutes} minute interval"
        )

    async def stop(self):
        """Stop the scheduler."""
        if not self.is_running:
            return

        self.is_running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
        logger.info("Safety scheduler stopped")

    async def _run_scheduler(self):
        """Run the scheduler loop."""
        while self.is_running:
            try:
                # One tick under the advisory lock; records the run and
                # skips when another replica holds the lock.
                await run_locked_tick(SCHEDULER_NAME, self.check_feature_flags_safety)

                # Wait for the next interval
                await asyncio.sleep(self.interval_minutes * 60)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in safety scheduler: {e!s}")
                # Wait a bit before trying again
                await asyncio.sleep(60)

    async def check_feature_flags_safety(self) -> Dict[str, int]:
        """
        Check all active feature flags for safety issues.

        This checks:
        1. Every ACTIVE feature flag, whatever its global rollout percentage:
           a flag at 0% whose targeting rules serve users is checked too (#629)
        2. For each flag, check if safety monitoring is enabled
        3. If enabled, check safety metrics
        4. If auto-rollback is enabled and safety check fails, trigger rollback
           -- unless ``rollback_change`` says the rollback would change nothing

        Returns ``{"items_processed": n, "items_failed": m}`` for the run record
        (``items_processed`` counts flags checked, ``metadata.rollbacks`` the
        automatic rollbacks executed).
        """
        logger.info("Checking feature flags safety")

        checked_count = 0
        failed_count = 0
        rollback_count = 0

        # Use a new database session for this task
        db = SessionLocal()
        try:
            # Every ACTIVE flag: one at 0% may still serve the users its
            # targeting rules match, and a rollback to 0 turns it off (#629).
            active_flags = (
                db.query(FeatureFlag)
                .filter(FeatureFlag.status == FeatureFlagStatus.ACTIVE)
                .all()
            )

            if not active_flags:
                logger.info("No active feature flags found")
                return {"items_processed": 0, "items_failed": 0}

            safety_service = SafetyService(db)

            # Check each feature flag for safety issues
            for feature_flag in active_flags:
                try:
                    # Get safety configuration for the feature flag
                    config = await safety_service.async_get_feature_flag_safety_config(
                        feature_flag.id
                    )

                    # Skip if safety monitoring is not enabled for this flag
                    if not config.enabled:
                        continue

                    # Check safety status
                    safety_check = await safety_service.check_feature_flag_safety(
                        feature_flag.id
                    )
                    checked_count += 1

                    # Log the safety check result
                    if safety_check.is_healthy:
                        logger.info(
                            f"Feature flag {feature_flag.key} ({feature_flag.id}) is healthy"
                        )
                    else:
                        logger.warning(
                            f"Feature flag {feature_flag.key} ({feature_flag.id}) has safety issues: {safety_check.details}"
                        )

                    # If auto-rollback is enabled and safety check fails, trigger rollback
                    settings = await safety_service.async_get_safety_settings()

                    if (
                        settings.enable_automatic_rollbacks
                        and not safety_check.is_healthy
                    ):
                        logger.warning(
                            f"Triggering automatic rollback for feature flag {feature_flag.key} ({feature_flag.id})"
                        )

                        # Find what metric triggered the rollback
                        trigger_reason = "Automatic rollback due to safety issues"
                        failing = next(
                            (m for m in safety_check.metrics if not m.is_healthy),
                            None,
                        )
                        if failing is not None:
                            trigger_reason = (
                                f"Automatic rollback due to {failing.name} "
                                f"{describe_breach(failing)}"
                            )

                        # Roll back to the percentage configured for this flag
                        # (e.g. back to the internal 5% stage), not always to 0.
                        target_percentage = _rollback_target_percentage(config)

                        if rollback_change(feature_flag, target_percentage) is None:
                            # Already rolled back; the error window is still hot
                            # from before the rollback. Do not write another
                            # record or send another notification every cycle.
                            logger.info(
                                f"Feature flag {feature_flag.key} is unhealthy but already at "
                                f"{feature_flag.rollout_percentage}% (rollback target {target_percentage}%); "
                                "waiting for the error window to clear"
                            )
                            continue

                        # Execute rollback
                        rollback_result = (
                            await safety_service.async_rollback_feature_flag(
                                feature_flag_id=feature_flag.id,
                                percentage=target_percentage,
                                reason=trigger_reason,
                                trigger_type=RollbackTriggerType.AUTOMATIC,
                            )
                        )

                        if rollback_result.success:
                            rollback_count += 1
                            logger.info(
                                f"Successfully rolled back feature flag {feature_flag.key}: {rollback_result.message}"
                            )
                            try:
                                # The alert shows the breaching metric's
                                # measured value and threshold (#1076).
                                self._notification_service.notify_safety_rollback(
                                    feature_flag_id=str(feature_flag.id),
                                    feature_flag_name=feature_flag.key,
                                    reason=trigger_reason,
                                    metric_name=failing.name if failing else None,
                                    value=failing.current_value if failing else None,
                                    threshold=failing.threshold if failing else None,
                                    comparison=(
                                        breach_comparison(failing) if failing else None
                                    ),
                                )
                            except Exception as exc:
                                logger.warning(
                                    "Notification failed (non-critical): %s", exc
                                )
                        else:
                            logger.error(
                                f"Failed to roll back feature flag {feature_flag.key}: {rollback_result.message}"
                            )

                except Exception as e:
                    failed_count += 1
                    logger.error(
                        f"Error checking safety for feature flag {feature_flag.id}: {e!s}"
                    )

        except Exception as e:
            failed_count += 1
            logger.error(f"Error checking feature flags safety: {e!s}")
        finally:
            db.close()

        return {
            "items_processed": checked_count,
            "items_failed": failed_count,
            "metadata": {"rollbacks": rollback_count},
        }


# Create a global instance of the safety scheduler
safety_scheduler = SafetyScheduler()
