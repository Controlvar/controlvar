"""
Rollout schedule schema models for validation and serialization.

This module defines Pydantic models for rollout schedule-related data structures.
These models are used for request/response validation and documentation.
"""

import math
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from backend.app.api.datetime_bounds import storable_offset

#: The largest ``stage_order`` a request may set. A schedule has a handful of
#: stages (the documented example has three, and their target percentages run
#: from 0 to 100), so this is well above any real schedule. A larger value
#: answers 422 naming the field.
MAX_STAGE_ORDER = 1000

#: The largest ``min_stage_duration`` a request may set, in hours: one year
#: (#1086). The column is a 32-bit integer, so 2**31 and more answered 500.
MAX_MIN_STAGE_DURATION_HOURS = 8760

#: The date rule's text. Raised as a ``ValueError``, so the 422's ``msg`` is
#: ``"Value error, " + END_DATE_AFTER_START``.
END_DATE_AFTER_START = "End date must be after start date"

#: The one message for ``stages`` sent to ``PUT /rollout-schedules/{id}``
#: (#1529). The ``{...}`` placeholders are literal.
STAGES_ON_UPDATE_REFUSED = (
    "stages is not applied by this endpoint; use "
    "POST /api/v1/rollout-schedules/{schedule_id}/stages to add a stage and "
    "PUT /api/v1/rollout-schedules/stages/{stage_id} to change one"
)

_MIN_STAGE_DURATION_DESCRIPTION = (
    "Minimum time (hours) between stages: a whole number from 0 to 8760, "
    "or null for no minimum"
)

#: The largest ``trigger_configuration.duration`` a request may set, in hours
#: (#1535): one year, as for ``min_stage_duration``.
MAX_STAGE_DURATION_HOURS = 8760

#: The one message for a ``trigger_configuration.duration`` that is not a
#: number of hours from 0 to 8760 (#1535). Nothing in it comes from the value.
DURATION_REFUSED = (
    "trigger_configuration.duration must be a number of hours from 0 to 8760"
)

_TRIGGER_CONFIGURATION_DESCRIPTION = (
    "Configuration for the trigger. When it has `duration`, that is the number "
    "of hours from 0 to 8760 a time-based stage runs before the scheduler "
    "completes it; without `duration` the default is 24. The rest of the "
    "object is not checked."
)


def _check_duration(
    configuration: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Refuse a ``duration`` that is not a number of hours from 0 to 8760.

    The scheduler reads ``duration`` as hours (default 24). A string, null or
    a number it cannot use left the stage in progress for ever, a negative
    one completed it at once, and ``true`` read as one hour (#1535). A
    boolean is refused although ``bool`` is an ``int``; an ``int`` is compared
    as it is (``math.isfinite`` raises ``OverflowError`` on a very large one).
    The rest of ``trigger_configuration`` is free-form and not checked.
    """
    if not configuration or "duration" not in configuration:
        return configuration
    value = configuration["duration"]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PydanticCustomError("value_error", DURATION_REFUSED)
    if isinstance(value, float) and not math.isfinite(value):
        raise PydanticCustomError("value_error", DURATION_REFUSED)
    if not 0 <= value <= MAX_STAGE_DURATION_HOURS:
        raise PydanticCustomError("value_error", DURATION_REFUSED)
    return configuration


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    """``value`` with no zone taken as UTC (#1534); a zoned value is kept."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _check_schedule_dates(
    start_date: Optional[datetime], end_date: Optional[datetime]
) -> None:
    """Refuse an end that is not after the start, when both are given.

    Both are compared as UTC instants, so a zoned and an unzoned value can be
    compared (#1534); before, that raised a ``TypeError`` and answered 500.
    Equal instants are refused, as the database's
    ``check_rollout_schedule_dates`` (``end_date > start_date``) refuses them.
    """
    if start_date is None or end_date is None:
        return
    if _as_utc(end_date) <= _as_utc(start_date):
        raise ValueError(END_DATE_AFTER_START)


class TriggerType(str, Enum):
    """Types of triggers for rollout schedule stages."""

    TIME_BASED = "time_based"
    METRIC_BASED = "metric_based"
    MANUAL = "manual"


class RolloutStageStatus(str, Enum):
    """Status of a rollout schedule stage."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class RolloutScheduleStatus(str, Enum):
    """Status of a rollout schedule."""

    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class MetricTrigger(BaseModel):
    """Configuration for a metric-based trigger."""

    metric_name: str = Field(..., description="Name of the metric to track")
    threshold: float = Field(..., description="Threshold value to trigger the stage")
    comparison: str = Field(..., description="Comparison operator (gt, lt, eq, etc.)")
    duration: Optional[int] = Field(
        None, description="Duration in hours the metric should meet the threshold"
    )


class TimeTrigger(BaseModel):
    """Configuration for a time-based trigger."""

    scheduled_date: datetime = Field(..., description="Date and time for the trigger")
    time_zone: Optional[str] = Field("UTC", description="Time zone for the trigger")


class RolloutStageBase(BaseModel):
    """Base model for rollout stage data."""

    name: str = Field(..., min_length=1, max_length=100, description="Stage name")
    description: Optional[str] = Field(None, description="Stage description")
    stage_order: int = Field(
        ...,
        ge=0,
        le=MAX_STAGE_ORDER,
        description="Order of the stage in the schedule",
    )
    target_percentage: int = Field(
        ..., ge=0, le=100, description="Target rollout percentage"
    )
    trigger_type: TriggerType = Field(..., description="Type of trigger for this stage")
    trigger_configuration: Optional[Dict[str, Any]] = Field(
        None, description="Configuration for the trigger"
    )
    start_date: Optional[datetime] = Field(None, description="For time-based triggers")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "Initial Rollout",
                "description": "First stage of rollout to 10% of users",
                "stage_order": 1,
                "target_percentage": 10,
                "trigger_type": "time_based",
                "trigger_configuration": {
                    "scheduled_date": "2023-12-01T00:00:00Z",
                    "time_zone": "UTC",
                },
                "start_date": "2023-12-01T00:00:00Z",
            }
        }
    )

    @field_validator("target_percentage")
    @classmethod
    def validate_percentage(cls, v: int) -> int:
        """Validate percentage is between 0 and 100."""
        if v < 0 or v > 100:
            raise ValueError("Percentage must be between 0 and 100")
        return v


class RolloutStageCreate(RolloutStageBase):
    """Model for creating a new rollout stage."""

    # The duration check is here and on RolloutStageUpdate, never on the
    # base: RolloutStageResponse inherits the base, and a stage stored with a
    # bad duration before the check (#1535) must still read back.
    trigger_configuration: Optional[Dict[str, Any]] = Field(
        None, description=_TRIGGER_CONFIGURATION_DESCRIPTION
    )

    @field_validator("trigger_configuration")
    @classmethod
    def validate_duration(
        cls, value: Optional[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """``duration``, when present, is a number of hours from 0 to 8760."""
        return _check_duration(value)


def _refuse_null(value: Any, info: ValidationInfo) -> Any:
    """Refuse an explicit ``null`` for a field that has no "cleared" state.

    The update models make every field optional so that a request sends only
    what it changes, and omitting a field leaves the stored value alone. An
    explicit ``null`` is different: it was applied to a NOT NULL column and
    answered 500. A ``mode="before"`` validator sees the ``null`` and not the
    default of a field the request left out. Nothing in the message comes from
    the value.
    """
    if value is None:
        raise PydanticCustomError(
            "value_error",
            "{field} cannot be null; leave it out to keep it",
            {"field": info.field_name},
        )
    return value


class RolloutStageUpdate(BaseModel):
    """Model for updating a rollout stage."""

    name: Optional[str] = Field(
        None, min_length=1, max_length=100, description="Stage name"
    )
    description: Optional[str] = Field(None, description="Stage description")
    stage_order: Optional[int] = Field(
        None,
        ge=0,
        le=MAX_STAGE_ORDER,
        description="Order of the stage in the schedule",
    )
    target_percentage: Optional[int] = Field(
        None, ge=0, le=100, description="Target rollout percentage"
    )
    trigger_type: Optional[TriggerType] = Field(
        None, description="Type of trigger for this stage"
    )
    trigger_configuration: Optional[Dict[str, Any]] = Field(
        None, description=_TRIGGER_CONFIGURATION_DESCRIPTION
    )
    start_date: Optional[datetime] = Field(None, description="For time-based triggers")

    # These four are NOT NULL in the table; the others can be cleared.
    _refuse_null_required = field_validator(
        "name", "stage_order", "target_percentage", "trigger_type", mode="before"
    )(_refuse_null)

    @field_validator("trigger_configuration")
    @classmethod
    def validate_duration(
        cls, value: Optional[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """``duration``, when present, is a number of hours from 0 to 8760."""
        return _check_duration(value)


class RolloutStageResponse(RolloutStageBase):
    """Model for rollout stage response data."""

    # Not bounded by MAX_STAGE_ORDER: that bound is on what a request sets. A
    # stored order can be higher -- adding a stage in the middle moves every
    # later stage up by one -- and a response must still describe it.
    stage_order: int = Field(
        ..., ge=0, description="Order of the stage in the schedule"
    )
    id: UUID
    rollout_schedule_id: UUID
    status: RolloutStageStatus
    completed_date: Optional[datetime] = None
    # Read-only: no request schema has the field, so a request that sends it
    # is ignored (#1528).
    started_at: Optional[datetime] = Field(
        None,
        description=(
            "When the stage started, in UTC: the time a time-based stage's "
            "duration and the schedule's min_stage_duration are measured from. "
            "Null until the stage starts, for a stage that completed or failed "
            "before it was recorded, and for a stage a release that did not "
            "record it started during an upgrade, while that stage runs and "
            "after; the scheduler measures such a stage from updated_at. "
            "Editing the stage does not change it."
        ),
    )
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class RolloutScheduleBase(BaseModel):
    """Base model for rollout schedule data."""

    name: str = Field(..., min_length=1, max_length=100, description="Schedule name")
    description: Optional[str] = Field(None, description="Schedule description")
    feature_flag_id: UUID = Field(..., description="ID of the feature flag")
    start_date: Optional[datetime] = Field(
        None, description="Date when the schedule should start"
    )
    end_date: Optional[datetime] = Field(
        None, description="Date when the schedule should end"
    )
    config_data: Optional[Dict[str, Any]] = Field(
        None, description="Additional configuration data"
    )
    max_percentage: int = Field(
        100, ge=0, le=100, description="Maximum rollout percentage"
    )
    # Not bounded here: this base is also the response's, and a stored value
    # from before the bound (#1086) must still read back. The bound is on
    # RolloutScheduleCreate and RolloutScheduleUpdate.
    min_stage_duration: Optional[int] = Field(
        None, description="Minimum time (hours) between stages"
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "Gradual Rollout",
                "description": "Gradually roll out new feature to users",
                "feature_flag_id": "123e4567-e89b-12d3-a456-426614174000",
                "start_date": "2023-12-01T00:00:00Z",
                "end_date": "2023-12-31T23:59:59Z",
                "config_data": {
                    "owner_email": "product@example.com",
                    "target_user_segment": "beta_users",
                },
                "max_percentage": 100,
                "min_stage_duration": 24,
            }
        }
    )

    @model_validator(mode="after")
    def validate_dates(self) -> "RolloutScheduleBase":
        """Validate date relationships."""
        _check_schedule_dates(self.start_date, self.end_date)
        return self


class RolloutScheduleCreate(RolloutScheduleBase):
    """Model for creating a new rollout schedule."""

    min_stage_duration: Optional[int] = Field(
        None,
        ge=0,
        le=MAX_MIN_STAGE_DURATION_HOURS,
        description=_MIN_STAGE_DURATION_DESCRIPTION,
    )
    stages: List[RolloutStageCreate] = Field(
        ..., min_length=1, description="Rollout stages"
    )

    @field_validator("start_date", "end_date")
    @classmethod
    def take_unzoned_as_utc(cls, value: Optional[datetime]) -> Optional[datetime]:
        """Refuse an offset the database cannot store, which answered 500
        (#956), and store a date with no zone as that moment in UTC (#1534)."""
        return _as_utc(storable_offset(value))

    @model_validator(mode="after")
    def validate_stages(self) -> "RolloutScheduleCreate":
        """Validate stages in the schedule."""
        if not self.stages:
            raise ValueError("At least one stage is required")

        # Check that the targets do not decrease in stage_order, whatever
        # order the list is in: the scheduler starts stages by stage_order
        # (#1460). A stable sort, so stages that share an order (refused
        # below) are compared in list order.
        stage_percentages = [
            stage.target_percentage
            for stage in sorted(self.stages, key=lambda stage: stage.stage_order)
        ]
        if not all(a <= b for a, b in zip(stage_percentages, stage_percentages[1:])):
            raise ValueError("Stage percentages must be non-decreasing in stage_order")

        # Check that max stage percentage doesn't exceed the schedule max
        if max(stage_percentages) > self.max_percentage:
            raise ValueError(
                f"Stage percentages cannot exceed the maximum of {self.max_percentage}%"
            )

        # Check that stage orders are sequential without gaps: n distinct
        # orders whose highest is n - 1 above their lowest.
        stage_orders = sorted(stage.stage_order for stage in self.stages)
        if (
            len(set(stage_orders)) != len(stage_orders)
            or stage_orders[-1] - stage_orders[0] != len(stage_orders) - 1
        ):
            raise ValueError("Stage orders must be sequential without gaps")

        return self


class RolloutScheduleUpdate(BaseModel):
    """Model for updating a rollout schedule."""

    name: Optional[str] = Field(
        None, min_length=1, max_length=100, description="Schedule name"
    )
    description: Optional[str] = Field(None, description="Schedule description")
    start_date: Optional[datetime] = Field(
        None, description="Date when the schedule should start"
    )
    end_date: Optional[datetime] = Field(
        None, description="Date when the schedule should end"
    )
    config_data: Optional[Dict[str, Any]] = Field(
        None, description="Additional configuration data"
    )
    max_percentage: Optional[int] = Field(
        None, ge=0, le=100, description="Maximum rollout percentage"
    )
    min_stage_duration: Optional[int] = Field(
        None,
        ge=0,
        le=MAX_MIN_STAGE_DURATION_HOURS,
        description=(
            _MIN_STAGE_DURATION_DESCRIPTION + "; null clears a stored minimum"
        ),
    )
    status: Optional[RolloutScheduleStatus] = Field(
        None, description="Status of the schedule"
    )
    # Kept, and refused whatever its value (#1529). Removing the field would
    # let pydantic drop the key silently, which is the defect: the update
    # never applied stages (a 200 that renamed the schedule and ignored them).
    stages: Optional[List[RolloutStageUpdate]] = Field(
        None,
        description=(
            "Not accepted. A request that contains `stages`, with any value "
            "including null, is refused with 422. Add a stage with "
            "`POST /api/v1/rollout-schedules/{schedule_id}/stages` and change "
            "one with `PUT /api/v1/rollout-schedules/stages/{stage_id}`."
        ),
        json_schema_extra={"deprecated": True},
    )

    # These three are NOT NULL in the table; the others can be cleared.
    _refuse_null_required = field_validator(
        "name", "max_percentage", "status", mode="before"
    )(_refuse_null)

    @field_validator("start_date", "end_date")
    @classmethod
    def take_unzoned_as_utc(cls, value: Optional[datetime]) -> Optional[datetime]:
        """Refuse an offset the database cannot store, which answered 500
        (#956), and store a date with no zone as that moment in UTC (#1534)."""
        return _as_utc(storable_offset(value))

    @field_validator("stages")
    @classmethod
    def refuse_stages(cls, value: Any) -> Any:
        """Refuse ``stages`` on presence, whatever its value (#1529).

        An ``after`` validator: an item that fails ``RolloutStageUpdate``
        (a ``stage_order`` above the bound, say) answers its own 422 first.
        It runs for a list, ``[]`` and null, and not when the key is absent:
        pydantic does not validate a default. Nothing in the message comes
        from the value.
        """
        raise PydanticCustomError("value_error", STAGES_ON_UPDATE_REFUSED)

    @model_validator(mode="after")
    def validate_dates(self) -> "RolloutScheduleUpdate":
        """When both dates are sent, the end is after the start (#962).

        A request that sends one date is judged against the stored one by the
        database's ``check_rollout_schedule_dates``; the route answers that
        refusal with this same 422.
        """
        _check_schedule_dates(self.start_date, self.end_date)
        return self


class RolloutScheduleResponse(RolloutScheduleBase):
    """Model for rollout schedule response data."""

    # Not bounded: a value stored before the request bound (#1086), -5 or
    # 100000 say, must still read back.
    min_stage_duration: Optional[int] = Field(
        None, description="Minimum time (hours) between stages"
    )
    id: UUID
    owner_id: Optional[UUID]
    status: RolloutScheduleStatus
    stages: List[RolloutStageResponse]
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class RolloutScheduleListResponse(BaseModel):
    """Paginated response model for rollout schedules."""

    items: List[RolloutScheduleResponse]
    total: int
    skip: int
    limit: int

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "items": [
                    {
                        "id": "123e4567-e89b-12d3-a456-426614174000",
                        "name": "Gradual Rollout",
                        "description": "Gradually roll out new feature to users",
                        "feature_flag_id": "123e4567-e89b-12d3-a456-426614174000",
                        "status": "active",
                        "start_date": "2023-12-01T00:00:00Z",
                        "end_date": "2023-12-31T23:59:59Z",
                        "created_at": "2023-01-01T00:00:00Z",
                        "updated_at": "2023-01-01T00:00:00Z",
                    }
                ],
                "total": 1,
                "skip": 0,
                "limit": 100,
            }
        },
        from_attributes=True,
    )
