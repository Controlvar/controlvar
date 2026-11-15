"""
Service for managing feature flag rollout schedules.

This module provides functionality for creating, updating, and managing
rollout schedules for feature flags.
"""

import functools
from datetime import datetime, timezone
from typing import Any, Callable, List, Optional, Tuple, TypeVar
from uuid import UUID

from sqlalchemy import and_
from sqlalchemy.orm import Session

from backend.app.core.logging import get_logger
from backend.app.models.feature_flag import FeatureFlag
from backend.app.models.rollout_schedule import (
    RolloutSchedule,
    RolloutScheduleStatus,
    RolloutStage,
    RolloutStageStatus,
    TriggerType,
)
from backend.app.schemas.rollout_schedule import (
    RolloutScheduleCreate,
    RolloutScheduleUpdate,
    RolloutStageCreate,
    RolloutStageUpdate,
)

logger = get_logger(__name__)


class RolloutRuleConflict(ValueError):
    """A change that a schedule's rules, as they are stored now, refuse (#1460).

    The routes answer it with 409, in an ``except`` placed before their
    ``except ValueError`` (400). It is a ``ValueError`` so that a route that
    lacks the mapping still refuses the change, with 400. Its text is built
    only from stored or validated values (stage orders, percentages, statuses
    and a schedule id), never from text a request sent.
    """


#: A stage in one of these states has started: it keeps its target and its
#: place in the order, and no stage may be added or moved at or before it.
STARTED_STAGE_STATUSES = (
    RolloutStageStatus.IN_PROGRESS,
    RolloutStageStatus.COMPLETED,
    RolloutStageStatus.FAILED,
)

#: What a request may still change on a stage that is in progress.
IN_PROGRESS_FIXED_FIELDS = frozenset({"target_percentage", "stage_order"})

_Method = TypeVar("_Method", bound=Callable[..., Any])


def _end_transaction(db: Session) -> None:
    """Roll ``db`` back; a failure to do so never hides the error being raised."""
    try:
        db.rollback()
    except Exception:
        logger.exception("Rolling back a refused rollout change failed")


def _ends_transaction_unless_done(method: _Method) -> _Method:
    """End the session's transaction, in the calling thread, when ``method``
    raises or finds nothing to change (returns ``None``).

    The methods it wraps take the flag and schedule locks
    (``_lock_schedule_for_change``) and commit when they succeed. A refusal
    after the locks (a ``RolloutRuleConflict``, a status re-check, a row
    deleted while the request waited, any error) used to leave the
    transaction open, so its locks lasted until the request's session was
    closed, which ``deps.get_db`` does on the event loop. The rollout tick runs
    on that same loop and locks the flag of every ACTIVE schedule: a tick that
    reached the flag first waited on a lock only the loop could release, and
    the API process hung (#1604 review, finding 1). Rolling back here, in the
    worker thread that ran the request, releases the locks before control
    returns to the loop. One place, so that every refusal of these methods,
    present and future, is covered.
    """

    @functools.wraps(method)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        db = kwargs["db"] if "db" in kwargs else args[0]
        try:
            result = method(*args, **kwargs)
        except BaseException:
            _end_transaction(db)
            raise
        if result is None:
            _end_transaction(db)
        return result

    return wrapper  # type: ignore[return-value]


def _lock_schedule_for_change(
    db: Session, schedule: RolloutSchedule
) -> Tuple[Optional[FeatureFlag], RolloutSchedule, List[RolloutStage]]:
    """Lock the schedule's flag, then the schedule, and read its stages again.

    Every change that a schedule's rules judge takes this first. The flag row
    is locked ``FOR UPDATE``, then the schedule row: the order the manual
    advance, the rollout scheduler and the safety rollback use, so two of them
    never wait on each other in a cycle. The schedule and its stages are then
    read from the database (``populate_existing``), replacing the copies the
    session holds: a change committed while this call waited, by another
    request or by the scheduler, is seen, and the rules are judged on it.

    Nothing is changed before this point, so a refusal after it leaves nothing
    pending; the locks go when the session's transaction ends.

    Returns ``(flag or None, schedule, stages in stage_order)``.

    Raises:
        ValueError: the schedule was deleted while this call waited.
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
    if current is None:
        raise ValueError("Schedule not found")
    stages = (
        db.query(RolloutStage)
        .filter(RolloutStage.rollout_schedule_id == current.id)
        .populate_existing()
        .order_by(RolloutStage.stage_order, RolloutStage.id)
        .all()
    )
    return feature_flag, current, stages


def _check_targets(entries: List[Tuple[int, int]], max_percentage: int) -> None:
    """Refuse targets that decrease in ``stage_order`` or exceed the maximum.

    ``entries`` is ``(stage_order, target_percentage)`` for every stage of the
    schedule as the change would leave it. The whole schedule is judged, so a
    schedule that already breaks a rule accepts only a change that mends it.
    """
    ordered = sorted(entries, key=lambda entry: entry[0])
    for (a, p), (b, q) in zip(ordered, ordered[1:]):
        if q < p:
            raise RolloutRuleConflict(
                "Stage targets must not decrease in stage_order: "
                f"stage_order {a} targets {p}% and stage_order {b} would target {q}%."
            )
    for b, q in ordered:
        if q > max_percentage:
            raise RolloutRuleConflict(
                "A stage's target cannot exceed the schedule's max_percentage: "
                f"stage_order {b} would target {q}% and max_percentage is "
                f"{max_percentage}%."
            )


def _check_after_started(stages: List[RolloutStage], new_order: int) -> None:
    """Refuse a stage added or moved at or before a stage that has started (#1536).

    The scheduler starts the lowest pending stage when no pending stage comes
    after the one in progress, so a pending stage placed before a started one
    would move the flag backwards.
    """
    started = [s for s in stages if s.status in STARTED_STAGE_STATUSES]
    if not started:
        return
    top = max(started, key=lambda s: s.stage_order)
    if new_order <= top.stage_order:
        raise RolloutRuleConflict(
            "Stages that have started keep their place: "
            f"stage_order {top.stage_order} is {top.status.value}, "
            "so a stage can only be added or moved after it."
        )


def _refuse_another_active(db: Session, schedule: RolloutSchedule) -> None:
    """Refuse a second ACTIVE schedule for the schedule's flag.

    Called with the flag locked, so two activations of the flag's schedules
    take turns and the second sees the first.
    """
    other = (
        db.query(RolloutSchedule)
        .filter(
            and_(
                RolloutSchedule.feature_flag_id == schedule.feature_flag_id,
                RolloutSchedule.id != schedule.id,
                RolloutSchedule.status == RolloutScheduleStatus.ACTIVE,
            )
        )
        .populate_existing()
        .order_by(RolloutSchedule.created_at, RolloutSchedule.id)
        .first()
    )
    if other is not None:
        raise RolloutRuleConflict(
            f"Another rollout schedule of this flag is active ({other.id}). "
            "A flag runs one schedule at a time: pause or cancel that one first."
        )


def _check_starts_in_order(stages: List[RolloutStage], stage: RolloutStage) -> None:
    """Refuse to start ``stage`` while another is in progress or an earlier one
    has not completed: stages start in ``stage_order``, one at a time."""
    blocking = next(
        (
            s
            for s in stages
            if s.id != stage.id and s.status == RolloutStageStatus.IN_PROGRESS
        ),
        None,
    )
    if blocking is None:
        blocking = next(
            (
                s
                for s in stages
                if s.id != stage.id
                and s.stage_order < stage.stage_order
                and s.status != RolloutStageStatus.COMPLETED
            ),
            None,
        )
    if blocking is not None:
        raise RolloutRuleConflict(
            "Stages start in stage_order, one at a time: "
            f"stage_order {blocking.stage_order} is {blocking.status.value}, "
            "so this stage cannot start yet."
        )


class RolloutService:
    """
    Service for managing feature flag rollout schedules.

    This service handles:
    - Creating and updating rollout schedules
    - Managing rollout stages
    - Processing stage transitions
    - Validating schedule configurations
    """

    @staticmethod
    def create_rollout_schedule(
        db: Session, data: RolloutScheduleCreate, owner_id: UUID
    ) -> RolloutSchedule:
        """
        Create a new rollout schedule with the provided stages.

        Args:
            db: Database session
            data: Schedule data including stages
            owner_id: ID of the user creating the schedule

        Returns:
            The created schedule
        """
        # First check if the feature flag exists
        feature_flag = (
            db.query(FeatureFlag).filter(FeatureFlag.id == data.feature_flag_id).first()
        )

        if not feature_flag:
            raise ValueError(f"Feature flag with ID {data.feature_flag_id} not found")

        # Create the schedule
        schedule = RolloutSchedule(
            name=data.name,
            description=data.description,
            feature_flag_id=data.feature_flag_id,
            owner_id=owner_id,
            start_date=data.start_date,
            end_date=data.end_date,
            config_data=data.config_data,
            max_percentage=data.max_percentage,
            min_stage_duration=data.min_stage_duration,
            status=RolloutScheduleStatus.DRAFT,
        )

        db.add(schedule)
        db.flush()  # Flush to get the ID for the schedule

        # Create the stages
        for stage_data in data.stages:
            stage = RolloutStage(
                rollout_schedule_id=schedule.id,
                name=stage_data.name,
                description=stage_data.description,
                stage_order=stage_data.stage_order,
                target_percentage=stage_data.target_percentage,
                trigger_type=TriggerType(stage_data.trigger_type.value),
                trigger_configuration=stage_data.trigger_configuration,
                start_date=stage_data.start_date,
                status=RolloutStageStatus.PENDING,
            )
            db.add(stage)

        # Commit the transaction
        db.commit()
        db.refresh(schedule)

        return schedule

    @staticmethod
    def get_rollout_schedule(
        db: Session, schedule_id: UUID
    ) -> Optional[RolloutSchedule]:
        """
        Get a rollout schedule by ID.

        Args:
            db: Database session
            schedule_id: ID of the schedule to retrieve

        Returns:
            The schedule if found, None otherwise
        """
        return (
            db.query(RolloutSchedule).filter(RolloutSchedule.id == schedule_id).first()
        )

    @staticmethod
    def get_rollout_schedules(
        db: Session,
        feature_flag_id: Optional[UUID] = None,
        owner_id: Optional[UUID] = None,
        status: Optional[RolloutScheduleStatus] = None,
        skip: int = 0,
        limit: int = 100,
    ) -> Tuple[List[RolloutSchedule], int]:
        """
        Get rollout schedules with optional filtering.

        Args:
            db: Database session
            feature_flag_id: Optional filter by feature flag ID
            owner_id: Optional filter by owner ID
            status: Optional filter by status
            skip: Number of records to skip (for pagination)
            limit: Maximum number of records to return

        Returns:
            A tuple of (schedules, total_count)
        """
        query = db.query(RolloutSchedule)

        if feature_flag_id:
            query = query.filter(RolloutSchedule.feature_flag_id == feature_flag_id)

        if owner_id:
            query = query.filter(RolloutSchedule.owner_id == owner_id)

        if status:
            query = query.filter(RolloutSchedule.status == status)

        # Count total for pagination
        total = query.count()

        # Apply pagination
        schedules = (
            query.order_by(RolloutSchedule.created_at.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )

        return schedules, total

    @staticmethod
    @_ends_transaction_unless_done
    def update_rollout_schedule(
        db: Session, schedule_id: UUID, data: RolloutScheduleUpdate
    ) -> Optional[RolloutSchedule]:
        """
        Update an existing rollout schedule.

        Args:
            db: Database session
            schedule_id: ID of the schedule to update
            data: Updated schedule data

        Returns:
            The updated schedule if found, None otherwise
        """
        schedule = (
            db.query(RolloutSchedule).filter(RolloutSchedule.id == schedule_id).first()
        )

        if not schedule:
            return None

        # Validate status transition
        if data.status and data.status != schedule.status:
            if not RolloutService._validate_status_transition(
                schedule.status, data.status
            ):
                raise ValueError(
                    f"Invalid status transition from {schedule.status.value} to {data.status.value}"
                )

            # If activating, ensure there's at least one stage
            if data.status == RolloutScheduleStatus.ACTIVE:
                stages_count = (
                    db.query(RolloutStage)
                    .filter(RolloutStage.rollout_schedule_id == schedule_id)
                    .count()
                )

                if stages_count == 0:
                    raise ValueError("Cannot activate a schedule with no stages")

        _, schedule, stages = _lock_schedule_for_change(db, schedule)

        # Update basic fields
        update_data = data.model_dump(exclude_unset=True)

        # Lowering max_percentage below a stage's target (#1460).
        new_max = update_data.get("max_percentage")
        if new_max is not None:
            over = next((s for s in stages if s.target_percentage > new_max), None)
            if over is not None:
                raise RolloutRuleConflict(
                    "max_percentage cannot be below a stage's target: "
                    f"stage_order {over.stage_order} targets {over.target_percentage}%."
                )

        # A second ACTIVE schedule for the flag (#1460). The status
        # transition check above refuses every status a request sends today
        # (the request's enum is not the model's), so this is reached only
        # once that check accepts ACTIVE; /activate is the way in.
        if (
            data.status is not None
            and data.status.value == RolloutScheduleStatus.ACTIVE.value
        ):
            _refuse_another_active(db, schedule)

        # ``stages`` is refused by RolloutScheduleUpdate (#1529); stages change
        # through the stage routes only. Never set it as a schedule attribute.
        update_data.pop("stages", None)

        for key, value in update_data.items():
            setattr(schedule, key, value)

        db.add(schedule)
        db.commit()
        db.refresh(schedule)

        return schedule

    @staticmethod
    @_ends_transaction_unless_done
    def add_rollout_stage(
        db: Session, schedule_id: UUID, data: RolloutStageCreate
    ) -> Optional[RolloutStage]:
        """
        Add a new stage to an existing rollout schedule.

        Args:
            db: Database session
            schedule_id: ID of the schedule to add a stage to
            data: Stage data

        Returns:
            The created stage if successful, None otherwise
        """
        schedule = (
            db.query(RolloutSchedule).filter(RolloutSchedule.id == schedule_id).first()
        )

        if not schedule:
            return None

        _, schedule, stages = _lock_schedule_for_change(db, schedule)

        # Get max order of existing stages
        max_order = max((s.stage_order for s in stages), default=0)

        # If no order is specified, put it at the end
        if data.stage_order is None:
            data.stage_order = max_order + 1

        # The rules, on the schedule as the change would leave it: the stages
        # at or after the new order move one place on (#1460, #1536).
        _check_after_started(stages, data.stage_order)
        _check_targets(
            [
                (
                    s.stage_order + 1
                    if s.stage_order >= data.stage_order
                    else s.stage_order,
                    s.target_percentage,
                )
                for s in stages
            ]
            + [(data.stage_order, data.target_percentage)],
            schedule.max_percentage,
        )

        # If order is in the middle, we need to shift other stages
        for stage in stages:
            if stage.stage_order >= data.stage_order:
                stage.stage_order += 1
                db.add(stage)

        # Create the new stage
        stage = RolloutStage(
            rollout_schedule_id=schedule_id,
            name=data.name,
            description=data.description,
            stage_order=data.stage_order,
            target_percentage=data.target_percentage,
            trigger_type=TriggerType(data.trigger_type.value),
            trigger_configuration=data.trigger_configuration,
            start_date=data.start_date,
            status=RolloutStageStatus.PENDING,
        )

        db.add(stage)
        db.commit()
        db.refresh(stage)

        return stage

    @staticmethod
    @_ends_transaction_unless_done
    def update_rollout_stage(
        db: Session, stage_id: UUID, data: RolloutStageUpdate
    ) -> Optional[RolloutStage]:
        """
        Update an existing rollout stage.

        Args:
            db: Database session
            stage_id: ID of the stage to update
            data: Updated stage data

        Returns:
            The updated stage if found, None otherwise
        """
        stage = db.query(RolloutStage).filter(RolloutStage.id == stage_id).first()

        if not stage:
            return None

        schedule = (
            db.query(RolloutSchedule)
            .filter(RolloutSchedule.id == stage.rollout_schedule_id)
            .first()
        )
        if not schedule:
            raise ValueError("Schedule not found")

        # ``stage`` is one of the rows read again here, so its status is the
        # stored one from now on.
        _, schedule, stages = _lock_schedule_for_change(db, schedule)
        if not any(s.id == stage.id for s in stages):
            return None  # deleted while this request waited

        # The fields the request body sets (#1460): a stage that has
        # completed or failed takes none; one in progress keeps its target
        # and its place in the order.
        sent = data.model_fields_set
        if sent and stage.status in (
            RolloutStageStatus.COMPLETED,
            RolloutStageStatus.FAILED,
        ):
            raise RolloutRuleConflict(
                f"A stage that is {stage.status.value} cannot be changed."
            )
        if stage.status == RolloutStageStatus.IN_PROGRESS and (
            sent & IN_PROGRESS_FIXED_FIELDS
        ):
            raise RolloutRuleConflict(
                "target_percentage and stage_order cannot be changed on a stage "
                "that is in_progress; its name, description, trigger_type, "
                "trigger_configuration and start_date still can."
            )

        old_order = stage.stage_order
        new_order = data.stage_order if data.stage_order is not None else old_order

        def order_after(s: RolloutStage) -> int:
            """Where ``s`` is once the stage moves from old_order to new_order."""
            if s.id == stage.id:
                return new_order
            if new_order > old_order and old_order < s.stage_order <= new_order:
                return s.stage_order - 1
            if new_order < old_order and new_order <= s.stage_order < old_order:
                return s.stage_order + 1
            return s.stage_order

        # A pending stage is not moved at or before a stage that has started
        # (#1536), and the schedule as the change would leave it keeps its
        # targets in order and within max_percentage (#1460).
        if data.stage_order is not None:
            _check_after_started(
                [s for s in stages if s.id != stage.id], data.stage_order
            )
        new_target = (
            data.target_percentage
            if data.target_percentage is not None
            else stage.target_percentage
        )
        _check_targets(
            [
                (
                    order_after(s),
                    new_target if s.id == stage.id else s.target_percentage,
                )
                for s in stages
            ],
            schedule.max_percentage,
        )

        # Move the stages between the old and the new order one place.
        if new_order != old_order:
            for s in stages:
                moved = order_after(s)
                if s.id != stage.id and moved != s.stage_order:
                    s.stage_order = moved
                    db.add(s)

        # Update fields
        update_data = data.model_dump(exclude_unset=True)

        if "trigger_type" in update_data:
            update_data["trigger_type"] = TriggerType(update_data["trigger_type"].value)

        for key, value in update_data.items():
            setattr(stage, key, value)

        db.add(stage)
        db.commit()
        db.refresh(stage)

        return stage

    @staticmethod
    def delete_rollout_stage(db: Session, stage_id: UUID) -> bool:
        """
        Delete a rollout stage.

        Args:
            db: Database session
            stage_id: ID of the stage to delete

        Returns:
            True if the stage was deleted, False otherwise
        """
        stage = db.query(RolloutStage).filter(RolloutStage.id == stage_id).first()

        if not stage:
            return False

        # Don't allow deleting stages that are not in PENDING status
        if stage.status != RolloutStageStatus.PENDING:
            raise ValueError("Cannot delete stages that are not in PENDING status")

        # Get the schedule to validate
        schedule = (
            db.query(RolloutSchedule)
            .filter(RolloutSchedule.id == stage.rollout_schedule_id)
            .first()
        )

        if not schedule:
            return False

        # Don't allow deleting stages from active schedules
        if schedule.status == RolloutScheduleStatus.ACTIVE:
            raise ValueError("Cannot delete stages from active schedules")

        # Update order of subsequent stages
        stages_to_shift = (
            db.query(RolloutStage)
            .filter(
                and_(
                    RolloutStage.rollout_schedule_id == stage.rollout_schedule_id,
                    RolloutStage.stage_order > stage.stage_order,
                )
            )
            .all()
        )

        for s in stages_to_shift:
            s.stage_order -= 1
            db.add(s)

        # Delete the stage
        db.delete(stage)
        db.commit()

        return True

    @staticmethod
    @_ends_transaction_unless_done
    def activate_rollout_schedule(
        db: Session, schedule_id: UUID
    ) -> Optional[RolloutSchedule]:
        """
        Activate a rollout schedule.

        Args:
            db: Database session
            schedule_id: ID of the schedule to activate

        Returns:
            The activated schedule if successful, None otherwise
        """
        schedule = (
            db.query(RolloutSchedule).filter(RolloutSchedule.id == schedule_id).first()
        )

        if not schedule:
            return None

        # Validate status transition
        if (
            schedule.status != RolloutScheduleStatus.DRAFT
            and schedule.status != RolloutScheduleStatus.PAUSED
        ):
            raise ValueError(
                f"Cannot activate schedule in {schedule.status.value} status"
            )

        # Check that there's at least one stage
        stages_count = (
            db.query(RolloutStage)
            .filter(RolloutStage.rollout_schedule_id == schedule_id)
            .count()
        )

        if stages_count == 0:
            raise ValueError("Cannot activate a schedule with no stages")

        # With the flag locked, read the schedule again: another request may
        # have activated it, or another schedule of the flag, while this one
        # waited (#1460).
        _, schedule, _ = _lock_schedule_for_change(db, schedule)
        if (
            schedule.status != RolloutScheduleStatus.DRAFT
            and schedule.status != RolloutScheduleStatus.PAUSED
        ):
            raise ValueError(
                f"Cannot activate schedule in {schedule.status.value} status"
            )
        _refuse_another_active(db, schedule)

        # Update status
        schedule.status = RolloutScheduleStatus.ACTIVE
        db.add(schedule)
        db.commit()
        db.refresh(schedule)

        return schedule

    @staticmethod
    def pause_rollout_schedule(
        db: Session, schedule_id: UUID
    ) -> Optional[RolloutSchedule]:
        """
        Pause an active rollout schedule.

        Args:
            db: Database session
            schedule_id: ID of the schedule to pause

        Returns:
            The paused schedule if successful, None otherwise
        """
        schedule = (
            db.query(RolloutSchedule).filter(RolloutSchedule.id == schedule_id).first()
        )

        if not schedule:
            return None

        # Validate status transition
        if schedule.status != RolloutScheduleStatus.ACTIVE:
            raise ValueError(f"Cannot pause schedule in {schedule.status.value} status")

        # Update status
        schedule.status = RolloutScheduleStatus.PAUSED
        db.add(schedule)
        db.commit()
        db.refresh(schedule)

        return schedule

    @staticmethod
    def cancel_rollout_schedule(
        db: Session, schedule_id: UUID
    ) -> Optional[RolloutSchedule]:
        """
        Cancel a rollout schedule.

        Args:
            db: Database session
            schedule_id: ID of the schedule to cancel

        Returns:
            The cancelled schedule if successful, None otherwise
        """
        schedule = (
            db.query(RolloutSchedule).filter(RolloutSchedule.id == schedule_id).first()
        )

        if not schedule:
            return None

        # Cannot cancel completed schedules
        if schedule.status == RolloutScheduleStatus.COMPLETED:
            raise ValueError("Cannot cancel a completed schedule")

        # Update status
        schedule.status = RolloutScheduleStatus.CANCELLED
        db.add(schedule)
        db.commit()
        db.refresh(schedule)

        return schedule

    @staticmethod
    def delete_rollout_schedule(db: Session, schedule_id: UUID) -> bool:
        """
        Delete a rollout schedule.

        Args:
            db: Database session
            schedule_id: ID of the schedule to delete

        Returns:
            True if the schedule was deleted, False otherwise
        """
        schedule = (
            db.query(RolloutSchedule).filter(RolloutSchedule.id == schedule_id).first()
        )

        if not schedule:
            return False

        # Cannot delete active schedules
        if schedule.status == RolloutScheduleStatus.ACTIVE:
            raise ValueError(
                "Cannot delete an active schedule. Pause or cancel it first."
            )

        # Delete the schedule (cascades to stages)
        db.delete(schedule)
        db.commit()

        return True

    @staticmethod
    @_ends_transaction_unless_done
    def manually_advance_stage(db: Session, stage_id: UUID) -> Optional[RolloutStage]:
        """
        Manually advance a stage that has manual triggers.

        Args:
            db: Database session
            stage_id: ID of the stage to advance

        Returns:
            The updated stage if successful, None otherwise
        """
        stage = db.query(RolloutStage).filter(RolloutStage.id == stage_id).first()

        if not stage:
            return None

        # Must be a manual trigger type
        if stage.trigger_type != TriggerType.MANUAL:
            raise ValueError(
                "Can only manually advance stages with manual trigger type"
            )

        # Must be in PENDING or IN_PROGRESS status
        valid_statuses = [RolloutStageStatus.PENDING, RolloutStageStatus.IN_PROGRESS]
        if stage.status not in valid_statuses:
            raise ValueError(f"Cannot advance stage in {stage.status.value} status")

        # Get the schedule
        schedule = (
            db.query(RolloutSchedule)
            .filter(RolloutSchedule.id == stage.rollout_schedule_id)
            .first()
        )

        if not schedule:
            raise ValueError("Schedule not found")

        # Schedule must be active
        if schedule.status != RolloutScheduleStatus.ACTIVE:
            raise ValueError(
                f"Cannot advance stage for schedule in {schedule.status.value} status"
            )

        # Take the flag lock, then read the schedule again from the database
        # and lock it (#629). The check above used an unlocked read: a pause
        # committed since then, possibly by a writer holding this flag lock,
        # is seen here. ``populate_existing`` replaces the copies in the
        # session rather than trusting them, the stages' included. Nothing is
        # changed before this point, so a refusal leaves nothing pending.
        feature_flag, schedule, stages = _lock_schedule_for_change(db, schedule)
        if not any(s.id == stage.id for s in stages):
            return None  # deleted while this request waited
        if schedule.status != RolloutScheduleStatus.ACTIVE:
            raise ValueError(
                f"Cannot advance stage for schedule in {schedule.status.value} status"
            )
        if stage.status not in valid_statuses:
            raise ValueError(f"Cannot advance stage in {stage.status.value} status")

        current_time = datetime.now(timezone.utc)

        if stage.status == RolloutStageStatus.PENDING:
            # Stages start in stage_order, one at a time (#1460).
            _check_starts_in_order(stages, stage)

            # Activate the stage, and record when it started (#1528).
            stage.status = RolloutStageStatus.IN_PROGRESS
            stage.started_at = current_time
            stage.updated_at = current_time

            # Update feature flag percentage
            if feature_flag:
                feature_flag.rollout_percentage = stage.target_percentage
                feature_flag.updated_at = current_time
                db.add(feature_flag)

        elif stage.status == RolloutStageStatus.IN_PROGRESS:
            # Complete the stage
            stage.status = RolloutStageStatus.COMPLETED
            stage.completed_date = current_time
            stage.updated_at = current_time

            # The next stage: the lowest pending one after it, from the
            # stages read under the lock.
            next_stage = min(
                (
                    s
                    for s in stages
                    if s.stage_order > stage.stage_order
                    and s.status == RolloutStageStatus.PENDING
                ),
                key=lambda s: s.stage_order,
                default=None,
            )

            if next_stage:
                # Activate next stage, and record when it started (#1528).
                next_stage.status = RolloutStageStatus.IN_PROGRESS
                next_stage.started_at = current_time
                next_stage.updated_at = current_time
                db.add(next_stage)

                # Update feature flag percentage
                if feature_flag:
                    feature_flag.rollout_percentage = next_stage.target_percentage
                    feature_flag.updated_at = current_time
                    db.add(feature_flag)
            else:
                # No more stages, mark schedule as completed
                schedule.status = RolloutScheduleStatus.COMPLETED
                schedule.updated_at = current_time
                db.add(schedule)

        db.add(stage)
        db.commit()
        db.refresh(stage)

        return stage

    @staticmethod
    def _validate_status_transition(
        current_status: RolloutScheduleStatus, new_status: RolloutScheduleStatus
    ) -> bool:
        """
        Validate if a status transition is allowed.

        Args:
            current_status: Current status of the schedule
            new_status: New status to transition to

        Returns:
            True if the transition is valid, False otherwise
        """
        # Define allowed transitions
        allowed_transitions = {
            RolloutScheduleStatus.DRAFT: [
                RolloutScheduleStatus.ACTIVE,
                RolloutScheduleStatus.CANCELLED,
            ],
            RolloutScheduleStatus.ACTIVE: [
                RolloutScheduleStatus.PAUSED,
                RolloutScheduleStatus.COMPLETED,
                RolloutScheduleStatus.CANCELLED,
            ],
            RolloutScheduleStatus.PAUSED: [
                RolloutScheduleStatus.ACTIVE,
                RolloutScheduleStatus.CANCELLED,
            ],
            RolloutScheduleStatus.COMPLETED: [],  # No transitions from completed
            RolloutScheduleStatus.CANCELLED: [],  # No transitions from cancelled
        }

        return new_status in allowed_transitions.get(current_status, [])
