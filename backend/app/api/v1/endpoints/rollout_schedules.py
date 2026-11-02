"""
API routes for managing feature flag rollout schedules.

This module provides endpoints for creating, updating, and monitoring
feature flag rollout schedules.
"""

from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from fastapi.exceptions import RequestValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.api import deps
from backend.app.core.logging import get_logger
from backend.app.core.permissions import Action, can_act_on_feature_flag
from backend.app.models.audit_log import ActionType, EntityType
from backend.app.models.feature_flag import FeatureFlag
from backend.app.models.rollout_schedule import (
    RolloutSchedule,
    RolloutScheduleStatus,
    RolloutStage,
)
from backend.app.models.user import User
from backend.app.schemas.rollout_schedule import (
    END_DATE_AFTER_START,
    RolloutScheduleCreate,
    RolloutScheduleListResponse,
    RolloutScheduleResponse,
    RolloutScheduleUpdate,
    RolloutStageCreate,
    RolloutStageResponse,
    RolloutStageUpdate,
)
from backend.app.services.audit_service import (
    AuditService,
    audit_changes,
    audit_snapshot,
)
from backend.app.services.rollout_service import RolloutRuleConflict, RolloutService

logger = get_logger(__name__)

router = APIRouter()

#: The database's rule that a schedule's end is after its start (#962).
SCHEDULE_DATES_CONSTRAINT = "check_rollout_schedule_dates"


def _violated_constraint(error: IntegrityError) -> Optional[str]:
    """The name of the constraint the database refused the write on, or None.

    psycopg2 reports it as ``diag.constraint_name``; a NOT NULL violation, for
    one, names none. Matched by name only, never by the message's text.
    """
    diag = getattr(error.orig, "diag", None)
    return getattr(diag, "constraint_name", None)


def _schedule_dates_refused() -> RequestValidationError:
    """The 422 for a schedule whose end would not be after its start.

    The same ``type``, ``loc`` and ``msg`` as the request model's own refusal,
    for the case only the database can judge: a PUT that sends one date,
    compared with the stored other one. Nothing in it comes from the request.
    """
    return RequestValidationError(
        [
            {
                "type": "value_error",
                "loc": ("body",),
                "msg": f"Value error, {END_DATE_AFTER_START}",
            }
        ]
    )


def _require_update_on_flag(
    db: Session,
    user: User,
    *,
    flag_id: Optional[UUID] = None,
    schedule_id: Optional[UUID] = None,
    stage_id: Optional[UUID] = None,
) -> FeatureFlag:
    """Refuse unless *user* may change the feature flag this request would move.

    A schedule changes its flag's rollout percentage, so every change to a
    schedule or a stage needs UPDATE on that flag.  The flag is reached from
    whichever id the route has: the flag itself (create), the schedule, or the
    stage (stage -> schedule -> flag).  Called before each route's ``try``, whose
    ``except Exception`` would otherwise turn these 404s and 403s into 500s.
    """
    if stage_id is not None:
        stage = db.query(RolloutStage).filter(RolloutStage.id == stage_id).first()
        if stage is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Rollout stage not found with ID: {stage_id}",
            )
        schedule_id = stage.rollout_schedule_id
    if schedule_id is not None:
        schedule = (
            db.query(RolloutSchedule).filter(RolloutSchedule.id == schedule_id).first()
        )
        if schedule is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Rollout schedule not found with ID: {schedule_id}",
            )
        flag_id = schedule.feature_flag_id
    flag = db.query(FeatureFlag).filter(FeatureFlag.id == flag_id).first()
    if flag is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Feature flag not found with ID: {flag_id}",
        )
    if not can_act_on_feature_flag(user, flag.owner_id, Action.UPDATE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not enough permissions to change this feature flag's rollout",
        )
    return flag


def _record_rollout(
    db: Session, user: User, flag_id: UUID, before: dict, verb: str
) -> None:
    """The audit entry for a committed schedule or stage change: a
    ``feature_flag_update`` on the schedule's flag, with the reason
    "rollout schedule <verb>". It fails open: the change stands.
    """
    flag = db.query(FeatureFlag).filter(FeatureFlag.id == flag_id).first()
    after = audit_snapshot(EntityType.FEATURE_FLAG, flag) if flag else before
    old_value, new_value = audit_changes(before, after)
    AuditService.record_after_commit(
        db,
        actor=user,
        action=ActionType.FEATURE_FLAG_UPDATE,
        entity_type=EntityType.FEATURE_FLAG,
        entity_id=flag_id,
        entity_name=after.get("name") or str(flag_id),
        before=old_value,
        after=new_value,
        reason=f"rollout schedule {verb}",
    )


@router.post(
    "/",
    response_model=RolloutScheduleResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new rollout schedule",
)
def create_rollout_schedule(
    *,
    db: Session = Depends(deps.get_db),
    data: RolloutScheduleCreate,
    current_user: User = Depends(deps.get_current_active_user),
) -> Any:
    """
    Create a new rollout schedule for a feature flag.

    This endpoint allows users to create a gradual rollout schedule for a feature flag.
    A schedule consists of multiple stages with defined criteria for progression.
    """
    flag = _require_update_on_flag(db, current_user, flag_id=data.feature_flag_id)
    before = audit_snapshot(EntityType.FEATURE_FLAG, flag)
    try:
        schedule = RolloutService.create_rollout_schedule(
            db=db, data=data, owner_id=current_user.id
        )
        _record_rollout(db, current_user, flag.id, before, "created")
        return schedule
    except RolloutRuleConflict as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except IntegrityError as e:
        if _violated_constraint(e) == SCHEDULE_DATES_CONSTRAINT:
            db.rollback()
            raise _schedule_dates_refused() from None
        # Every other constraint keeps the answer and the log line of the
        # ``except Exception`` below. Not a bare ``raise``: an exception
        # leaving this handler does not reach that one.
        logger.error(f"Error creating rollout schedule: {e!s}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred while creating the rollout schedule",
        )
    except Exception as e:
        logger.error(f"Error creating rollout schedule: {e!s}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred while creating the rollout schedule",
        )


@router.get(
    "/",
    response_model=RolloutScheduleListResponse,
    summary="Get list of rollout schedules",
)
def get_rollout_schedules(
    *,
    db: Session = Depends(deps.get_db),
    feature_flag_id: Optional[UUID] = Query(
        None, description="Filter by feature flag ID"
    ),
    status: Optional[RolloutScheduleStatus] = Query(
        None, description="Filter by status"
    ),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(
        100, ge=1, le=1000, description="Maximum number of records to return"
    ),
    current_user: User = Depends(deps.get_current_active_user),
) -> Any:
    """
    Get a list of rollout schedules with optional filtering.

    This endpoint returns a paginated list of rollout schedules.
    The results can be filtered by feature flag ID and status.
    """
    try:
        schedules, total = RolloutService.get_rollout_schedules(
            db=db,
            feature_flag_id=feature_flag_id,
            owner_id=None,  # Don't filter by owner - allow viewing all schedules
            status=status,
            skip=skip,
            limit=limit,
        )

        return {"items": schedules, "total": total, "skip": skip, "limit": limit}
    except Exception as e:
        logger.error(f"Error getting rollout schedules: {e!s}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred while retrieving rollout schedules",
        )


@router.get(
    "/{schedule_id}",
    response_model=RolloutScheduleResponse,
    summary="Get a specific rollout schedule",
)
def get_rollout_schedule(
    *,
    db: Session = Depends(deps.get_db),
    schedule_id: UUID = Path(..., description="The ID of the rollout schedule"),
    current_user: User = Depends(deps.get_current_active_user),
) -> Any:
    """
    Get a specific rollout schedule by ID.

    This endpoint returns detailed information about a rollout schedule,
    including all its stages and current status.
    """
    schedule = RolloutService.get_rollout_schedule(db=db, schedule_id=schedule_id)

    if not schedule:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Rollout schedule not found with ID: {schedule_id}",
        )

    return schedule


@router.put(
    "/{schedule_id}",
    response_model=RolloutScheduleResponse,
    summary="Update a rollout schedule",
)
def update_rollout_schedule(
    *,
    db: Session = Depends(deps.get_db),
    schedule_id: UUID = Path(..., description="The ID of the rollout schedule"),
    data: RolloutScheduleUpdate,
    current_user: User = Depends(deps.get_current_active_user),
) -> Any:
    """
    Update an existing rollout schedule.

    This endpoint updates the properties of a rollout schedule. It does not
    take `stages`: a request that contains it answers 422. Add a stage with
    `POST /api/v1/rollout-schedules/{schedule_id}/stages` and change one with
    `PUT /api/v1/rollout-schedules/stages/{stage_id}`.
    """
    flag = _require_update_on_flag(db, current_user, schedule_id=schedule_id)
    before = audit_snapshot(EntityType.FEATURE_FLAG, flag)
    try:
        schedule = RolloutService.update_rollout_schedule(
            db=db, schedule_id=schedule_id, data=data
        )

        if not schedule:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Rollout schedule not found with ID: {schedule_id}",
            )
        _record_rollout(db, current_user, flag.id, before, "updated")

        return schedule
    except RolloutRuleConflict as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except HTTPException:
        raise  # this route's own 404: not an unexpected failure
    except IntegrityError as e:
        if _violated_constraint(e) == SCHEDULE_DATES_CONSTRAINT:
            db.rollback()
            raise _schedule_dates_refused() from None
        # Every other constraint keeps the answer and the log line of the
        # ``except Exception`` below. Not a bare ``raise``: an exception
        # leaving this handler does not reach that one.
        logger.error(f"Error updating rollout schedule: {e!s}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred while updating the rollout schedule",
        )
    except Exception as e:
        logger.error(f"Error updating rollout schedule: {e!s}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred while updating the rollout schedule",
        )


@router.delete(
    "/{schedule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a rollout schedule",
)
def delete_rollout_schedule(
    *,
    db: Session = Depends(deps.get_db),
    schedule_id: UUID = Path(..., description="The ID of the rollout schedule"),
    current_user: User = Depends(deps.get_current_active_user),
) -> None:
    """
    Delete a rollout schedule.

    This endpoint allows deletion of a rollout schedule. Active schedules
    cannot be deleted and must be paused or cancelled first.
    """
    flag = _require_update_on_flag(db, current_user, schedule_id=schedule_id)
    before = audit_snapshot(EntityType.FEATURE_FLAG, flag)
    try:
        success = RolloutService.delete_rollout_schedule(db=db, schedule_id=schedule_id)

        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Rollout schedule not found with ID: {schedule_id}",
            )
        _record_rollout(db, current_user, flag.id, before, "deleted")

    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error(f"Error deleting rollout schedule: {e!s}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred while deleting the rollout schedule",
        )


@router.post(
    "/{schedule_id}/activate",
    response_model=RolloutScheduleResponse,
    summary="Activate a rollout schedule",
)
def activate_rollout_schedule(
    *,
    db: Session = Depends(deps.get_db),
    schedule_id: UUID = Path(..., description="The ID of the rollout schedule"),
    current_user: User = Depends(deps.get_current_active_user),
) -> Any:
    """
    Activate a rollout schedule.

    This endpoint transitions a schedule from DRAFT or PAUSED to ACTIVE status,
    which enables its automatic processing by the scheduler.
    """
    flag = _require_update_on_flag(db, current_user, schedule_id=schedule_id)
    before = audit_snapshot(EntityType.FEATURE_FLAG, flag)
    try:
        schedule = RolloutService.activate_rollout_schedule(
            db=db, schedule_id=schedule_id
        )

        if not schedule:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Rollout schedule not found with ID: {schedule_id}",
            )
        _record_rollout(db, current_user, flag.id, before, "activated")

        return schedule
    except RolloutRuleConflict as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except HTTPException:
        raise  # this route's own 404: not an unexpected failure
    except Exception as e:
        logger.error(f"Error activating rollout schedule: {e!s}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred while activating the rollout schedule",
        )


@router.post(
    "/{schedule_id}/pause",
    response_model=RolloutScheduleResponse,
    summary="Pause a rollout schedule",
)
def pause_rollout_schedule(
    *,
    db: Session = Depends(deps.get_db),
    schedule_id: UUID = Path(..., description="The ID of the rollout schedule"),
    current_user: User = Depends(deps.get_current_active_user),
) -> Any:
    """
    Pause an active rollout schedule.

    This endpoint transitions a schedule from ACTIVE to PAUSED status,
    which temporarily suspends its processing by the scheduler.
    """
    flag = _require_update_on_flag(db, current_user, schedule_id=schedule_id)
    before = audit_snapshot(EntityType.FEATURE_FLAG, flag)
    try:
        schedule = RolloutService.pause_rollout_schedule(db=db, schedule_id=schedule_id)

        if not schedule:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Rollout schedule not found with ID: {schedule_id}",
            )
        _record_rollout(db, current_user, flag.id, before, "paused")

        return schedule
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error(f"Error pausing rollout schedule: {e!s}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred while pausing the rollout schedule",
        )


@router.post(
    "/{schedule_id}/cancel",
    response_model=RolloutScheduleResponse,
    summary="Cancel a rollout schedule",
)
def cancel_rollout_schedule(
    *,
    db: Session = Depends(deps.get_db),
    schedule_id: UUID = Path(..., description="The ID of the rollout schedule"),
    current_user: User = Depends(deps.get_current_active_user),
) -> Any:
    """
    Cancel a rollout schedule.

    This endpoint transitions a schedule to CANCELLED status,
    which permanently stops its processing by the scheduler.
    """
    flag = _require_update_on_flag(db, current_user, schedule_id=schedule_id)
    before = audit_snapshot(EntityType.FEATURE_FLAG, flag)
    try:
        schedule = RolloutService.cancel_rollout_schedule(
            db=db, schedule_id=schedule_id
        )

        if not schedule:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Rollout schedule not found with ID: {schedule_id}",
            )
        _record_rollout(db, current_user, flag.id, before, "cancelled")

        return schedule
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error(f"Error cancelling rollout schedule: {e!s}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred while cancelling the rollout schedule",
        )


@router.post(
    "/{schedule_id}/stages",
    response_model=RolloutStageResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a stage to a rollout schedule",
)
def add_rollout_stage(
    *,
    db: Session = Depends(deps.get_db),
    schedule_id: UUID = Path(..., description="The ID of the rollout schedule"),
    data: RolloutStageCreate,
    current_user: User = Depends(deps.get_current_active_user),
) -> Any:
    """
    Add a new stage to a rollout schedule.

    This endpoint allows adding a new stage to an existing rollout schedule.
    The stage defines a target percentage and criteria for activation.
    """
    flag = _require_update_on_flag(db, current_user, schedule_id=schedule_id)
    before = audit_snapshot(EntityType.FEATURE_FLAG, flag)
    try:
        stage = RolloutService.add_rollout_stage(
            db=db, schedule_id=schedule_id, data=data
        )

        if not stage:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Rollout schedule not found with ID: {schedule_id}",
            )
        _record_rollout(db, current_user, flag.id, before, "stage added")

        return stage
    except RolloutRuleConflict as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except HTTPException:
        raise  # this route's own 404: not an unexpected failure
    except Exception as e:
        logger.error(f"Error adding rollout stage: {e!s}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred while adding the rollout stage",
        )


@router.put(
    "/stages/{stage_id}",
    response_model=RolloutStageResponse,
    summary="Update a rollout stage",
)
def update_rollout_stage(
    *,
    db: Session = Depends(deps.get_db),
    stage_id: UUID = Path(..., description="The ID of the rollout stage"),
    data: RolloutStageUpdate,
    current_user: User = Depends(deps.get_current_active_user),
) -> Any:
    """
    Update an existing rollout stage.

    This endpoint allows updating the properties of a rollout stage,
    including its target percentage and trigger criteria.
    """
    flag = _require_update_on_flag(db, current_user, stage_id=stage_id)
    before = audit_snapshot(EntityType.FEATURE_FLAG, flag)
    try:
        stage = RolloutService.update_rollout_stage(db=db, stage_id=stage_id, data=data)

        if not stage:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Rollout stage not found with ID: {stage_id}",
            )
        _record_rollout(db, current_user, flag.id, before, "stage updated")

        return stage
    except RolloutRuleConflict as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except HTTPException:
        raise  # this route's own 404: not an unexpected failure
    except Exception as e:
        logger.error(f"Error updating rollout stage: {e!s}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred while updating the rollout stage",
        )


@router.delete(
    "/stages/{stage_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a rollout stage",
)
def delete_rollout_stage(
    *,
    db: Session = Depends(deps.get_db),
    stage_id: UUID = Path(..., description="The ID of the rollout stage"),
    current_user: User = Depends(deps.get_current_active_user),
) -> None:
    """
    Delete a rollout stage.

    This endpoint allows deletion of a pending rollout stage.
    Stages that are already in progress or completed cannot be deleted.
    """
    flag = _require_update_on_flag(db, current_user, stage_id=stage_id)
    before = audit_snapshot(EntityType.FEATURE_FLAG, flag)
    try:
        success = RolloutService.delete_rollout_stage(db=db, stage_id=stage_id)

        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Rollout stage not found with ID: {stage_id}",
            )
        _record_rollout(db, current_user, flag.id, before, "stage deleted")

    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error(f"Error deleting rollout stage: {e!s}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred while deleting the rollout stage",
        )


@router.post(
    "/stages/{stage_id}/advance",
    response_model=RolloutStageResponse,
    summary="Manually advance a stage",
)
def manually_advance_stage(
    *,
    db: Session = Depends(deps.get_db),
    stage_id: UUID = Path(..., description="The ID of the rollout stage"),
    current_user: User = Depends(deps.get_current_active_user),
) -> Any:
    """
    Manually advance a stage with manual trigger type.

    This endpoint allows manually triggering a stage transition
    for stages with manual trigger types.
    """
    flag = _require_update_on_flag(db, current_user, stage_id=stage_id)
    before = audit_snapshot(EntityType.FEATURE_FLAG, flag)
    try:
        stage = RolloutService.manually_advance_stage(db=db, stage_id=stage_id)

        if not stage:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Rollout stage not found with ID: {stage_id}",
            )
        _record_rollout(db, current_user, flag.id, before, "stage advanced")

        return stage
    except RolloutRuleConflict as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except HTTPException:
        raise  # this route's own 404: not an unexpected failure
    except Exception as e:
        logger.error(f"Error advancing rollout stage: {e!s}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred while advancing the rollout stage",
        )
