"""
Scheduler for feature flag rollout schedules.

This module provides scheduling functionality for automatically
progressing feature flag rollout schedules based on defined triggers.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from backend.app.core.config import settings as app_settings
from backend.app.core.logging import get_logger
from backend.app.core.scheduler_tick import run_in_worker, run_locked_tick
from backend.app.db.session import SessionLocal
from backend.app.models.audit_log import ActionType, EntityType
from backend.app.models.feature_flag import FeatureFlag
from backend.app.models.rollout_schedule import (
    RolloutSchedule,
    RolloutScheduleStatus,
    RolloutStage,
    RolloutStageStatus,
    TriggerType,
)
from backend.app.services.audit_service import (
    SYSTEM_ROLLOUT_SCHEDULER,
    AuditService,
    audit_changes,
    audit_snapshot,
)
from backend.app.services.notification_service import NotificationService

logger = get_logger(__name__)

SCHEDULER_NAME = "rollout"


class ScheduleNoLongerActive(Exception):
    """The schedule left ACTIVE after this tick read it.

    Raised once the tick holds the flag and schedule locks and finds, on a
    fresh read, that the schedule was paused, cancelled or deleted while the
    tick was waiting. Nothing failed: the tick rolls that schedule back and
    counts it as skipped.
    """

    def __init__(self, schedule_id: Any, status: Optional[str]):
        self.schedule_id = schedule_id
        self.status = status
        super().__init__(
            f"Rollout schedule {schedule_id} is no longer active "
            f"({status or 'deleted'})"
        )


def _as_utc(value: datetime) -> datetime:
    """
    Return ``value`` as a timezone-aware UTC datetime.

    ``rollout_stages`` timestamps are stored in ``timestamp without time zone``
    columns, so SQLAlchemy hands back naive datetimes while the scheduler
    works with ``datetime.now(timezone.utc)``; comparing the two raises
    ``TypeError``. Naive values are UTC by convention throughout the app.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def stage_started_at(stage: RolloutStage) -> Optional[datetime]:
    """When ``stage`` started, as an aware UTC datetime, or ``None``.

    The scheduler measures a time-based stage's duration and the schedule's
    ``min_stage_duration`` from this, and from nothing else.

    ``started_at`` is written once, when the stage goes from pending to in
    progress, so editing the stage does not move it (#1528). A stage that a
    release older than migration ``7b6b4b4fe246`` started -- an older replica
    during a rolling deploy -- has none; for it this falls back to
    ``updated_at``, which that release wrote at the start and which an edit
    moves, as before.
    """
    if stage.started_at is not None:
        return _as_utc(stage.started_at)
    if stage.updated_at is not None:
        return _as_utc(stage.updated_at)
    return None


class RolloutScheduler:
    """Handles scheduled tasks for feature flag rollouts."""

    def __init__(self, interval_minutes: Optional[int] = None):
        """
        Initialize the rollout scheduler.

        Args:
            interval_minutes: How often to check for schedules that need to be
                updated (in minutes). Defaults to
                ``settings.ROLLOUT_CHECK_INTERVAL_MINUTES``.
        """
        if interval_minutes is None:
            interval_minutes = app_settings.ROLLOUT_CHECK_INTERVAL_MINUTES
        self.interval_minutes = interval_minutes
        self.is_running = False
        self.task: Optional[asyncio.Task] = None
        self._notification_service = NotificationService()

    async def start(self):
        """Start the scheduler."""
        if self.is_running:
            logger.warning("Rollout scheduler is already running")
            return

        self.is_running = True
        self.task = asyncio.create_task(self._run_scheduler())
        logger.info(
            f"Rollout scheduler started with {self.interval_minutes} minute interval"
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
        logger.info("Rollout scheduler stopped")

    async def _run_scheduler(self):
        """Run the scheduler loop."""
        while self.is_running:
            try:
                # One tick under the advisory lock; records the run and
                # skips when another replica holds the lock. The tick itself
                # runs in a worker thread.
                await run_locked_tick(SCHEDULER_NAME, self._process_off_loop)

                # Wait for the next interval
                await asyncio.sleep(self.interval_minutes * 60)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in rollout scheduler: {e!s}")
                # Wait a bit before trying again
                await asyncio.sleep(60)

    async def _process_off_loop(self) -> Dict[str, int]:
        """
        Run :meth:`process_rollout_schedules` in a worker thread.

        The tick is synchronous SQLAlchemy behind ``async def``s that never
        suspend on the API's event loop: its first query checks a connection
        out of the pool. On the loop's own thread that checkout, when every
        connection is held by a request waiting for the loop, waits the whole
        pool timeout and stops the API answering anything for that long. Here
        it runs unchanged in one worker thread, on a private event loop and
        with its own session, as the safety tick does
        (``SafetyScheduler._check_off_loop``); its notifications go with it,
        each still sent through ``asyncio.to_thread`` after its commit.

        The coroutine is created inside the thread. ``run_in_worker`` on the
        coroutine function would only create it, never run it: the tick would
        be recorded as a success that advanced nothing.

        The body must not await anything bound to the API's event loop (the
        process-wide Redis pool in ``backend.app.api.deps`` above all: created
        on the private loop, it would be reused by every request after the
        tick, on a loop that has closed). It awaits nothing of the kind today;
        ``test_ticks_off_the_loop.py`` runs it this way with the cache on and
        fails if it asks for the Redis pool.
        """
        return await run_in_worker(
            lambda: asyncio.run(self.process_rollout_schedules()),
            label=f"{SCHEDULER_NAME} tick",
        )

    async def process_rollout_schedules(self) -> Dict[str, int]:
        """
        Process rollout schedules that need updates based on their triggers.

        This checks for:
        1. Schedules in ACTIVE status that have pending stages
        2. For each schedule, finds stages that should be activated
        3. Updates feature flag rollout percentages accordingly

        Returns ``{"items_processed": n, "items_failed": m}`` for the run record.
        """
        logger.info("Processing rollout schedules")

        schedules_updated = 0
        stages_processed = 0
        skipped_count = 0
        failed_count = 0

        # Use a new database session for this task
        db = SessionLocal()
        try:
            current_time = datetime.now(timezone.utc)

            # Get active rollout schedules
            active_schedules = (
                db.query(RolloutSchedule)
                .filter(
                    and_(
                        RolloutSchedule.status == RolloutScheduleStatus.ACTIVE,
                        or_(
                            RolloutSchedule.end_date.is_(None),
                            RolloutSchedule.end_date > current_time,
                        ),
                    )
                )
                # A fixed processing order, oldest schedule first: the logs of
                # one tick read the same way every time, and a schedule's turn
                # does not depend on the table's physical row order.
                # created_at has a server default; id breaks ties.
                .order_by(RolloutSchedule.created_at, RolloutSchedule.id)
                .all()
            )

            if not active_schedules:
                logger.info("No active rollout schedules found")
                return {"items_processed": 0, "items_failed": 0}

            logger.info(f"Found {len(active_schedules)} active rollout schedules")

            # Process each active schedule. Each one runs inside its own
            # savepoint and is committed on its own, and a failure rolls the
            # session back before the next schedule: one schedule the
            # database refuses no longer leaves the session failed for every
            # schedule after it, and no pending change of one schedule is
            # committed by another (#593).
            # The ids are read now: a rollback expires every loaded instance.
            for schedule_id, schedule in [(s.id, s) for s in active_schedules]:
                try:
                    with db.begin_nested():
                        (
                            updated,
                            transitions,
                            notifications,
                        ) = await self._process_schedule(db, schedule, current_time)
                    db.commit()
                except ScheduleNoLongerActive as exc:
                    # Paused or cancelled while this tick waited for the
                    # flag lock. Leaving the savepoint has undone this
                    # schedule's changes; ending the transaction releases
                    # the locks. A skip, not a failure (#629).
                    db.rollback()
                    skipped_count += 1
                    logger.info(
                        "Rollout schedule %s is no longer active (%s); skipped",
                        schedule_id,
                        exc.status or "deleted",
                    )
                    continue
                except Exception:
                    db.rollback()
                    failed_count += 1
                    logger.exception(
                        "Error processing rollout schedule %s", schedule_id
                    )
                    continue

                if updated:
                    schedules_updated += 1
                stages_processed += transitions
                # Each send in a worker thread: it is a synchronous HTTP call
                # (#1690). After the commit, so nothing uncommitted is announced.
                for notification in notifications:
                    try:
                        await asyncio.to_thread(
                            self._notification_service.notify_rollout_advanced,
                            **notification,
                        )
                    except Exception as exc:
                        logger.warning("Notification failed (non-critical): %s", exc)

            if schedules_updated > 0:
                logger.info(
                    f"Updated {schedules_updated} rollout schedules with {stages_processed} stage transitions"
                )
            else:
                logger.info("No rollout schedules required updates")

        except Exception as e:
            failed_count += 1
            logger.error(f"Error processing rollout schedules: {e!s}")
        finally:
            db.close()

        return {
            "items_processed": schedules_updated,
            "items_failed": failed_count,
            "metadata": {
                "stage_transitions": stages_processed,
                "schedules_skipped": skipped_count,
            },
        }

    async def _process_schedule(
        self, db: Session, schedule: RolloutSchedule, current_time: datetime
    ) -> Tuple[bool, int, List[Dict[str, Any]]]:
        """
        Advance one schedule as far as its triggers allow in this tick.

        Changes are added to ``db`` and flushed, never committed: the caller
        owns the transaction. Returns whether the schedule changed, how many
        stages were started, and the notifications to send once the change is
        committed.
        """
        notifications: List[Dict[str, Any]] = []

        # Lock the flag, then the schedule, before reading the stages, and
        # read them from the database (``populate_existing``): a stage added,
        # moved or advanced while this tick waited for the lock is seen, so
        # the tick never starts a stage from a stale list (#1536). The API's
        # changes take the same locks in the same order.
        self._lock_active_schedule(db, schedule)
        current_active_stage = (
            db.query(RolloutStage)
            .populate_existing()
            .filter(
                and_(
                    RolloutStage.rollout_schedule_id == schedule.id,
                    RolloutStage.status == RolloutStageStatus.IN_PROGRESS,
                )
            )
            .first()
        )

        next_pending_stages = (
            db.query(RolloutStage)
            .populate_existing()
            .filter(
                and_(
                    RolloutStage.rollout_schedule_id == schedule.id,
                    RolloutStage.status == RolloutStageStatus.PENDING,
                )
            )
            .order_by(RolloutStage.stage_order)
            .all()
        )

        # No active stage: start the first pending stage if it is eligible.
        if not current_active_stage:
            if not next_pending_stages:
                return False, 0, notifications
            next_stage = next_pending_stages[0]
            if not self._is_stage_eligible_for_activation(next_stage, current_time):
                return False, 0, notifications
            await self._activate_stage(db, schedule, next_stage, current_time)
            notifications.append(self._advance_notification(schedule, next_stage))
            return True, 1, notifications

        # An active stage: complete it once it is due, then start the next.
        if not self._is_stage_eligible_for_completion(
            current_active_stage, current_time
        ):
            return False, 0, notifications

        next_stage = None
        if next_pending_stages:
            next_stage = next(
                (
                    stage
                    for stage in next_pending_stages
                    if stage.stage_order > current_active_stage.stage_order
                ),
                next_pending_stages[0],
            )

        # Minimum duration between stages, measured from the start of the
        # active stage. It is checked before anything is changed, so a
        # schedule that has to wait leaves nothing pending in the session.
        min_duration_hours = schedule.min_stage_duration or 0
        if next_stage is not None and min_duration_hours > 0:
            min_duration = timedelta(hours=min_duration_hours)
            # A stage with no start cannot have run for the minimum. (Only a
            # time-based stage with a start is due above, so this is defensive.)
            started = stage_started_at(current_active_stage)
            if started is None or current_time - started < min_duration:
                until = (
                    "the stage has a start"
                    if started is None
                    else started + min_duration
                )
                logger.info(
                    f"Minimum duration not met for next stage in schedule {schedule.id}. "
                    f"Will wait until {until}"
                )
                return False, 0, notifications

        current_active_stage.status = RolloutStageStatus.COMPLETED
        current_active_stage.completed_date = current_time
        current_active_stage.updated_at = current_time
        db.add(current_active_stage)

        transitions = 0
        if next_stage is not None:
            # Start the next stage only once its own trigger allows it (a
            # TIME_BASED stage with a future start_date stays PENDING and is
            # picked up by a later run).
            if self._is_stage_eligible_for_activation(next_stage, current_time):
                await self._activate_stage(db, schedule, next_stage, current_time)
                notifications.append(self._advance_notification(schedule, next_stage))
                transitions = 1
            else:
                logger.info(
                    f"Stage {next_stage.id} ({next_stage.name}) in schedule {schedule.id} "
                    "is not yet eligible for activation; leaving it pending"
                )
        else:
            # This was the last stage, mark the schedule as completed. The
            # same locks as a stage start (flag, then schedule) and the same
            # fresh read: a schedule paused while this tick was waiting is
            # not overwritten with COMPLETED.
            self._lock_active_schedule(db, schedule)
            schedule.status = RolloutScheduleStatus.COMPLETED
            schedule.updated_at = current_time
            db.add(schedule)
            logger.info(f"Rollout schedule {schedule.id} completed")

        return True, transitions, notifications

    @staticmethod
    def _advance_notification(
        schedule: RolloutSchedule, stage: RolloutStage
    ) -> Dict[str, Any]:
        return {
            "feature_flag_id": str(schedule.feature_flag_id),
            "stage_name": stage.name,
            "new_percentage": stage.target_percentage,
        }

    def _is_stage_eligible_for_activation(
        self, stage: RolloutStage, current_time: datetime
    ) -> bool:
        """
        Check if a stage is eligible for activation based on its trigger.

        Args:
            stage: The stage to check
            current_time: The current time

        Returns:
            True if the stage should be activated, False otherwise
        """
        if stage.status != RolloutStageStatus.PENDING:
            return False

        if stage.trigger_type == TriggerType.TIME_BASED:
            # For time-based triggers, check if the scheduled time has passed
            if not stage.start_date:
                # If no specific start date, stage can be activated immediately
                return True

            return _as_utc(stage.start_date) <= current_time

        elif stage.trigger_type == TriggerType.METRIC_BASED:
            # For metric-based triggers, this would check if metrics meet criteria
            # This requires integration with a metrics system and is more complex
            # For now, return False as this is not implemented
            logger.info(
                f"Metric-based activation for stage {stage.id} not yet implemented"
            )
            return False

        elif stage.trigger_type == TriggerType.MANUAL:
            # Manual stages are only activated manually, never by the scheduler
            return False

        return False

    def _is_stage_eligible_for_completion(
        self, stage: RolloutStage, current_time: datetime
    ) -> bool:
        """
        Check if a stage is eligible for completion based on its criteria.

        Args:
            stage: The stage to check
            current_time: The current time

        Returns:
            True if the stage should be completed, False otherwise
        """
        if stage.status != RolloutStageStatus.IN_PROGRESS:
            return False

        if stage.trigger_type == TriggerType.TIME_BASED:
            # For time-based triggers, check if a minimum time has passed
            # This could be based on a duration in the trigger configuration
            trigger_config = stage.trigger_configuration or {}
            duration_hours = trigger_config.get("duration", 24)  # Default to 24 hours

            # Complete once the duration has passed since the stage started.
            started = stage_started_at(stage)
            if started is not None:
                return current_time >= started + timedelta(hours=duration_hours)

            return False

        elif stage.trigger_type == TriggerType.METRIC_BASED:
            # Similar to activation, this would check metrics
            logger.info(
                f"Metric-based completion for stage {stage.id} not yet implemented"
            )
            return False

        elif stage.trigger_type == TriggerType.MANUAL:
            # Manual stages are only completed manually
            return False

        return False

    @staticmethod
    def _lock_active_schedule(
        db: Session, schedule: RolloutSchedule
    ) -> Optional[FeatureFlag]:
        """
        Lock the schedule's flag, then re-read and lock the schedule itself.

        The tick read the schedule as ACTIVE at its start, without a lock. A
        writer may have paused it since and committed, possibly while holding
        the flag lock this call waits for. So the status is read again from
        the database once the flag lock is held: ``populate_existing``
        replaces the copy in the session's identity map rather than trusting
        it. ``FOR UPDATE`` on the schedule row holds off a later pause until
        this transaction ends. The order, flag then schedule, matches the
        other writers in this codebase, none of which locks a schedule and
        then a flag.

        Returns the locked flag, or ``None`` when it no longer exists.

        Raises:
            ScheduleNoLongerActive: the schedule is no longer ACTIVE.
        """
        feature_flag = (
            db.query(FeatureFlag)
            .filter(FeatureFlag.id == schedule.feature_flag_id)
            .with_for_update()
            .first()
        )
        current = (
            db.query(RolloutSchedule)
            .filter(RolloutSchedule.id == schedule.id)
            .populate_existing()
            .with_for_update()
            .one_or_none()
        )
        if current is None or current.status != RolloutScheduleStatus.ACTIVE:
            status = current.status.value if current is not None else None
            raise ScheduleNoLongerActive(schedule.id, status)
        return feature_flag

    async def _activate_stage(
        self,
        db: Session,
        schedule: RolloutSchedule,
        stage: RolloutStage,
        current_time: datetime,
    ) -> bool:
        """
        Activate a rollout stage and update the feature flag.

        The changes are flushed, not committed: the caller owns the
        transaction and sends the notification after its commit.

        The audit entry (``feature_flag_update`` on the flag, by the rollout
        scheduler) is added to the same transaction, not a savepoint: if it
        cannot be written, the stage start fails with it and neither is kept,
        so the flag's rollout does not widen unrecorded. The next tick tries
        again (#221).

        Args:
            db: Database session
            schedule: The rollout schedule
            stage: The stage to activate
            current_time: The current time

        Returns:
            True once the stage and the flag are updated.

        Raises:
            ScheduleNoLongerActive: the schedule left ACTIVE while this tick
                waited for the flag lock. The caller rolls the schedule back
                and counts it as skipped.
            LookupError: the schedule's feature flag does not exist. The
                caller rolls the schedule back, so the stage stays PENDING.
        """
        feature_flag = self._lock_active_schedule(db, schedule)

        if not feature_flag:
            raise LookupError(
                f"Feature flag {schedule.feature_flag_id} not found for rollout schedule {schedule.id}"
            )

        before = audit_snapshot(EntityType.FEATURE_FLAG, feature_flag)

        # Mark the stage as in progress, and record when it started: the
        # instant its duration is measured from (#1528). updated_at is still
        # written, so an older replica reading the row during a rolling deploy
        # measures from the same instant.
        stage.status = RolloutStageStatus.IN_PROGRESS
        stage.started_at = current_time
        stage.updated_at = current_time
        db.add(stage)

        # Update the feature flag's rollout percentage

        feature_flag.rollout_percentage = stage.target_percentage
        feature_flag.updated_at = current_time
        db.add(feature_flag)

        old_value, new_value = audit_changes(
            before, audit_snapshot(EntityType.FEATURE_FLAG, feature_flag)
        )
        AuditService.record(
            db,
            actor=SYSTEM_ROLLOUT_SCHEDULER,
            action=ActionType.FEATURE_FLAG_UPDATE,
            entity_type=EntityType.FEATURE_FLAG,
            entity_id=feature_flag.id,
            entity_name=feature_flag.name or feature_flag.key,
            before=old_value,
            after=new_value,
            reason="rollout schedule stage started",
        )
        db.flush()

        logger.info(
            f"Activated stage {stage.id} ({stage.name}) in schedule {schedule.id} - "
            f"Updated feature flag {feature_flag.key} to {stage.target_percentage}% rollout"
        )
        return True


# Create a singleton instance of the scheduler
rollout_scheduler = RolloutScheduler()
