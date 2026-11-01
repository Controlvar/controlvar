"""
Scheduler for background tasks.

This module provides scheduling functionality for recurring tasks
such as experiment status updates based on scheduled dates.
"""

import asyncio
from datetime import datetime, timezone
from typing import Dict, Optional

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from backend.app.api import experiment_cache
from backend.app.core.logging import get_logger
from backend.app.core.metrics import update_active_experiments
from backend.app.core.scheduler_tick import run_locked_tick
from backend.app.db.session import SessionLocal
from backend.app.models.audit_log import ActionType, EntityType
from backend.app.models.experiment import Experiment, ExperimentStatus
from backend.app.services.audit_service import (
    SYSTEM_EXPERIMENT_SCHEDULER,
    AuditService,
    audit_changes,
    audit_snapshot,
)
from backend.app.services.experiment_service import (
    ExperimentService,
    stamp_rollout_rule_id,
)
from backend.app.services.notification_service import NotificationService

logger = get_logger(__name__)

SCHEDULER_NAME = "experiment"

#: The one warning for a due draft the check below refuses. Fixed text: the id
#: is the only value in it, never the experiment's name or anything a user typed.
NOT_READY_TO_START = (
    "Scheduled start skipped for experiment %s: it is not ready to start (it "
    "needs at least two variants, a control and a metric, and a split URL "
    "experiment needs a valid split_url_config), so it stays a draft"
)


class ExperimentScheduler:
    """Handles scheduled tasks for experiments."""

    def __init__(self, interval_minutes: int = 15):
        """
        Initialize the experiment scheduler.

        Args:
            interval_minutes: How often to check for experiments that need to be updated (in minutes)
        """
        self.interval_minutes = interval_minutes
        self.is_running = False
        self.task: Optional[asyncio.Task] = None
        self._notification_service = NotificationService()

    async def start(self):
        """Start the scheduler."""
        if self.is_running:
            logger.warning("Scheduler is already running")
            return

        self.is_running = True
        self.task = asyncio.create_task(self._run_scheduler())
        logger.info(
            f"Experiment scheduler started with {self.interval_minutes} minute interval"
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
        logger.info("Experiment scheduler stopped")

    async def _run_scheduler(self):
        """Run the scheduler loop."""
        while self.is_running:
            try:
                # One tick under the advisory lock; records the run and
                # skips when another replica holds the lock.
                await run_locked_tick(
                    SCHEDULER_NAME, self.process_scheduled_experiments
                )

                # Wait for the next interval
                await asyncio.sleep(self.interval_minutes * 60)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in experiment scheduler: {e!s}")
                # Wait a bit before trying again
                await asyncio.sleep(60)

    async def process_scheduled_experiments(self) -> Dict[str, int]:
        """
        Process experiments that need status updates based on their scheduled dates.

        This checks for:
        1. Experiments to activate: DRAFT ones whose start_date has passed,
           and PAUSED ones whose scheduled resume (``resume_at``) has passed.
           A DRAFT is started only if it passes the check ``POST /start``
           makes (two variants, a control, a metric); one that does not stays
           a DRAFT, is logged once per tick, and is not counted as failed
        2. Experiments in ACTIVE status that should be completed (time-based)

        After each commit, the cached detail of every experiment it started,
        resumed or completed is dropped (with ``CACHE_ENABLED`` on), so the
        experiment page shows the new status at once; a Redis failure there is
        logged and does not stop the tick.

        An experiment's ``bayesian_decision`` is a recommendation only: nothing
        here stops an experiment on it (see docs/api/bayesian.md).

        Returns ``{"items_processed": n, "items_failed": m}`` for the run record.
        """
        logger.info("Processing scheduled experiments")

        activated_count = 0
        completed_count = 0
        failed_count = 0
        activated_ids = []
        completed_ids = []

        # Use a new database session for this task
        db = SessionLocal()
        try:
            current_time = datetime.now(timezone.utc)

            # Find experiments to activate: a DRAFT whose start_date has
            # passed, or a PAUSED one whose scheduled resume has (#436). A
            # PAUSED experiment's start_date is when it first started and is
            # never a reason to activate it. One filter, so the query keeps
            # the shape the unit tests mock.
            experiments_to_activate = (
                db.query(Experiment)
                .filter(
                    or_(
                        and_(
                            Experiment.status == ExperimentStatus.DRAFT,
                            Experiment.start_date.isnot(None),
                            Experiment.start_date <= current_time,
                        ),
                        and_(
                            Experiment.status == ExperimentStatus.PAUSED,
                            Experiment.resume_at.isnot(None),
                            Experiment.resume_at <= current_time,
                        ),
                    )
                )
                .all()
            )

            # Activate experiments. Setting the status clears resume_at (the
            # listener on Experiment.status). Each row is flushed inside its
            # own savepoint, so a row the database refuses is rolled back on
            # its own and the rest of the tick still activates (#489).
            starts = ExperimentService(db)
            for experiment in experiments_to_activate:
                experiment_id = experiment.id
                savepoint = db.begin_nested()
                try:
                    resumed = experiment.status == ExperimentStatus.PAUSED
                    # A draft's scheduled first start makes the check Start
                    # makes (#1454); a draft that fails it stays a draft, its
                    # start_date kept, and is checked again next tick. A
                    # scheduled resume is not checked.
                    if not resumed and not starts._validate_experiment_for_start(
                        experiment
                    ):
                        savepoint.rollback()
                        logger.warning(NOT_READY_TO_START, experiment_id)
                        continue
                    due = experiment.resume_at if resumed else experiment.start_date
                    before = audit_snapshot(EntityType.EXPERIMENT, experiment)
                    # A scheduled first start stamps the rule id; a scheduled
                    # resume keeps the users the rule admits (#533).
                    stamp_rollout_rule_id(experiment, experiment.status)
                    experiment.status = ExperimentStatus.ACTIVE
                    experiment.updated_at = current_time
                    db.add(experiment)
                    db.flush()
                    self._record_transition(
                        db,
                        experiment,
                        ActionType.EXPERIMENT_START,
                        before,
                        "scheduled resume" if resumed else "scheduled start",
                    )
                    savepoint.commit()
                except Exception:
                    savepoint.rollback()
                    failed_count += 1
                    logger.exception("Error activating experiment %s", experiment_id)
                    continue
                activated_count += 1
                activated_ids.append(experiment_id)
                logger.info(
                    f"Activating experiment: {experiment_id} - {experiment.name} "
                    f"({'scheduled resume' if resumed else 'scheduled start'}: "
                    f"{due})"
                )
                try:
                    self._notification_service.notify_experiment_started(
                        experiment_id=str(experiment_id),
                        experiment_name=experiment.name,
                        resumed=resumed,
                    )
                except Exception as exc:
                    logger.warning("Notification failed (non-critical): %s", exc)

            # Commit all activation changes before checking for experiments to complete
            if activated_count > 0:
                db.commit()
                await experiment_cache.drop_cached_experiment_details(activated_ids)

            # Find experiments to complete (end_date has passed)
            experiments_to_complete = (
                db.query(Experiment)
                .filter(
                    and_(
                        Experiment.status == ExperimentStatus.ACTIVE,
                        Experiment.end_date.isnot(None),
                        Experiment.end_date <= current_time,
                    )
                )
                .all()
            )

            # Complete experiments, one savepoint per row as above.
            for experiment in experiments_to_complete:
                experiment_id = experiment.id
                savepoint = db.begin_nested()
                try:
                    before = audit_snapshot(EntityType.EXPERIMENT, experiment)
                    experiment.status = ExperimentStatus.COMPLETED
                    experiment.updated_at = current_time
                    db.add(experiment)
                    db.flush()
                    self._record_transition(
                        db,
                        experiment,
                        ActionType.EXPERIMENT_COMPLETE,
                        before,
                        "scheduled end",
                    )
                    savepoint.commit()
                except Exception:
                    savepoint.rollback()
                    failed_count += 1
                    logger.exception("Error completing experiment %s", experiment_id)
                    continue
                completed_count += 1
                completed_ids.append(experiment_id)
                logger.info(
                    f"Completing experiment: {experiment_id} - {experiment.name} "
                    f"(scheduled end: {experiment.end_date})"
                )
                try:
                    self._notification_service.notify_experiment_ended(
                        experiment_id=str(experiment_id),
                        experiment_name=experiment.name,
                    )
                except Exception as exc:
                    logger.warning("Notification failed (non-critical): %s", exc)

            # Commit completion changes
            if completed_count > 0:
                db.commit()
                await experiment_cache.drop_cached_experiment_details(completed_ids)

            # Log the results
            if activated_count > 0 or completed_count > 0:
                logger.info(
                    f"Updated {activated_count} experiments to ACTIVE, "
                    f"{completed_count} to COMPLETED"
                )
            else:
                logger.info("No experiments required scheduling updates")

            # Prometheus: active_experiments_gauge reflects the post-tick state.
            self._update_active_experiments_gauge(db)

        except Exception as e:
            failed_count += 1
            logger.error(f"Error processing scheduled experiments: {e!s}")
        finally:
            db.close()

        return {
            "items_processed": activated_count + completed_count,
            "items_failed": failed_count,
            "metadata": {
                "activated": activated_count,
                "completed": completed_count,
            },
        }

    @staticmethod
    def _record_transition(
        db: Session,
        experiment: Experiment,
        action: ActionType,
        before: Dict,
        reason: str,
    ) -> None:
        """The audit entry for a transition just flushed in the row's savepoint.

        It goes in a savepoint of its own inside the row's (#221): if the
        entry fails, only that inner savepoint is rolled back and one ERROR
        naming the exception type is logged; the transition still commits and
        the entry is not retried.
        """
        old_value, new_value = audit_changes(
            before, audit_snapshot(EntityType.EXPERIMENT, experiment)
        )
        AuditService.record_in_savepoint(
            db,
            actor=SYSTEM_EXPERIMENT_SCHEDULER,
            action=action,
            entity_type=EntityType.EXPERIMENT,
            entity_id=experiment.id,
            entity_name=experiment.name,
            before=old_value,
            after=new_value,
            reason=reason,
        )

    @staticmethod
    def _update_active_experiments_gauge(db: Session) -> None:
        """Set ``active_experiments_gauge`` from the database (best-effort)."""
        try:
            count = (
                db.query(Experiment)
                .filter(Experiment.status == ExperimentStatus.ACTIVE)
                .count()
            )
            if isinstance(count, int):
                update_active_experiments(count)
        except Exception as exc:  # pragma: no cover - metrics must never break the tick
            logger.debug(f"Could not update active_experiments_gauge: {exc}")


# Create a singleton instance of the scheduler
experiment_scheduler = ExperimentScheduler()
