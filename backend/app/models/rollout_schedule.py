"""
Rollout schedule database models for gradual feature flag deployment.

This module defines models for managing gradual rollout schedules for feature flags,
including staged percentage increases, time-based triggers, and progression criteria.
"""

import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy import (
    Enum as SQLAEnum,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.declarative import declared_attr
from sqlalchemy.orm import relationship

from backend.app.core.database_config import get_schema_name
from backend.app.models.column_types import UTCDateTime

from .base import Base, BaseModel


class TriggerType(Enum):
    """Types of triggers for rollout schedule stages."""

    TIME_BASED = "time_based"  # Trigger based on a specific date/time
    METRIC_BASED = "metric_based"  # Trigger based on a specific metric threshold
    MANUAL = "manual"  # Manually triggered by a user


class RolloutStageStatus(Enum):
    """Status of a rollout schedule stage."""

    PENDING = "pending"  # Stage is waiting to be processed
    IN_PROGRESS = "in_progress"  # Stage is currently being applied
    COMPLETED = "completed"  # Stage has been completed
    FAILED = "failed"  # Stage failed to apply


class RolloutScheduleStatus(Enum):
    """Status of a rollout schedule."""

    DRAFT = "draft"  # Schedule is being drafted, not yet active
    ACTIVE = "active"  # Schedule is active and being processed
    PAUSED = "paused"  # Schedule is temporarily paused
    COMPLETED = "completed"  # Schedule has been completed
    CANCELLED = "cancelled"  # Schedule was cancelled before completion


class RolloutSchedule(Base, BaseModel):
    """
    Rollout schedule model for managing gradual feature flag deployments.

    A rollout schedule contains multiple stages that define how a feature flag
    should be gradually rolled out to users over time.
    """

    __tablename__ = "rollout_schedules"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    feature_flag_id = Column(
        UUID(as_uuid=True),
        ForeignKey(f"{get_schema_name()}.feature_flags.id", ondelete="CASCADE"),
        nullable=False,
    )
    status = Column(
        SQLAEnum(RolloutScheduleStatus),
        default=RolloutScheduleStatus.DRAFT,
        nullable=False,
        index=True,
    )
    owner_id = Column(
        UUID(as_uuid=True),
        ForeignKey(f"{get_schema_name()}.users.id", ondelete="SET NULL"),
    )
    start_date = Column(DateTime(timezone=True), nullable=True)
    end_date = Column(DateTime(timezone=True), nullable=True)
    config_data = Column(JSONB, nullable=True)
    max_percentage = Column(Integer, default=100, nullable=False)
    min_stage_duration = Column(Integer, nullable=True)

    # Relationships
    feature_flag = relationship("FeatureFlag", back_populates="rollout_schedules")
    owner = relationship("User", back_populates="rollout_schedules")
    stages = relationship(
        "RolloutStage",
        back_populates="rollout_schedule",
        cascade="all, delete-orphan",
        order_by="RolloutStage.stage_order",
    )

    @declared_attr
    def __table_args__(cls):
        schema_name = get_schema_name()
        return (
            # Composite index for finding active schedules
            Index(
                f"{schema_name}_rollout_schedule_flag_status",
                "feature_flag_id",
                "status",
            ),
            # Ensure end_date is after start_date
            CheckConstraint(
                "end_date IS NULL OR start_date IS NULL OR end_date > start_date",
                name="check_rollout_schedule_dates",
            ),
            # Ensure max_percentage is between 0 and 100
            CheckConstraint(
                "max_percentage >= 0 AND max_percentage <= 100",
                name="check_max_percentage",
            ),
            {"schema": schema_name},
        )

    def __repr__(self):
        return f"<RolloutSchedule {self.name}>"


class RolloutStage(Base, BaseModel):
    """
    Rollout stage model for a single step in a rollout schedule.

    Each stage represents a target percentage and the criteria for
    transitioning to this stage.
    """

    __tablename__ = "rollout_stages"

    rollout_schedule_id = Column(
        UUID(as_uuid=True),
        ForeignKey(f"{get_schema_name()}.rollout_schedules.id", ondelete="CASCADE"),
        nullable=False,
    )
    name = Column(String(100), nullable=False)
    description = Column(Text)
    stage_order = Column(Integer, nullable=False)  # Order of the stage in the schedule
    target_percentage = Column(Integer, nullable=False)  # Target rollout percentage
    status = Column(
        SQLAEnum(RolloutStageStatus),
        default=RolloutStageStatus.PENDING,
        nullable=False,
    )
    trigger_type = Column(
        SQLAEnum(TriggerType),
        nullable=False,
    )
    trigger_configuration = Column(JSONB)  # Configuration for the trigger
    # UTC, stored without a zone, whatever the session's time zone; see
    # UTCDateTime (#1462). The database type is ``timestamp without time
    # zone``, as before.
    start_date = Column(UTCDateTime)  # For time-based triggers
    completed_date = Column(UTCDateTime)  # When the stage was completed
    # BaseModel's updated_at, bound as UTC: the scheduler reads it as the time
    # the stage started (time-based completion and min_stage_duration), and
    # the scheduler and the manual advance write it from an aware now. Only
    # this table's updated_at changes; BaseModel is left alone.
    updated_at = Column(
        UTCDateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    # Relationships
    rollout_schedule = relationship("RolloutSchedule", back_populates="stages")

    @declared_attr
    def __table_args__(cls):
        schema_name = get_schema_name()
        return (
            # Ensure target_percentage is between 0 and 100
            CheckConstraint(
                "target_percentage >= 0 AND target_percentage <= 100",
                name="check_target_percentage",
            ),
            # Index for efficiently finding stages for a schedule
            Index(
                f"{schema_name}_rollout_stage_schedule",
                "rollout_schedule_id",
                "stage_order",
            ),
            {"schema": schema_name},
        )

    def __repr__(self):
        return f"<RolloutStage {self.name} - {self.target_percentage}%>"
