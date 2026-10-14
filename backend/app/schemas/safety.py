"""
Schemas for safety monitoring and rollback functionality.
"""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator
from pydantic_core import PydanticCustomError


class HealthStatus(str, Enum):
    """Health status of a safety check."""

    HEALTHY = "healthy"
    WARNING = "warning"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


class MetricThreshold(BaseModel):
    """Threshold configuration for a safety metric."""

    warning_threshold: Optional[float] = None
    critical_threshold: Optional[float] = None
    comparison_type: str = "greater_than"  # "greater_than", "less_than", "equal_to"


class MetricStatus(BaseModel):
    """Status of a metric with its current value and thresholds."""

    name: str
    description: Optional[str] = None
    current_value: float
    threshold: float
    unit: Optional[str] = None
    is_healthy: bool
    details: Optional[Dict[str, Any]] = None


class SafetySettingsBase(BaseModel):
    """Base schema for safety settings."""

    enable_automatic_rollbacks: bool = Field(
        False, description="Whether to enable automatic rollbacks"
    )
    default_metrics: Optional[Dict[str, MetricThreshold]] = Field(
        None, description="Default metrics to monitor with thresholds"
    )


class SafetySettingsCreate(SafetySettingsBase):
    """Schema for creating safety settings."""


class SafetySettingsUpdate(SafetySettingsBase):
    """Schema for updating safety settings."""


class SafetySettingsResponse(SafetySettingsBase):
    """Response schema for safety settings."""

    id: UUID
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class FeatureFlagSafetyConfigBase(BaseModel):
    """Base schema for feature flag safety configuration."""

    feature_flag_id: UUID
    enabled: bool = Field(
        True, description="Whether safety monitoring is enabled for this feature flag"
    )
    metrics: Dict[str, MetricThreshold] = Field(
        {}, description="Metrics to monitor with thresholds for this feature flag"
    )
    rollback_percentage: int = Field(
        0, description="Percentage to roll back to if automatic rollback is triggered"
    )


class FeatureFlagSafetyConfigCreate(BaseModel):
    """Schema for creating feature flag safety configuration."""

    enabled: bool = Field(
        True, description="Whether safety monitoring is enabled for this feature flag"
    )
    metrics: Dict[str, MetricThreshold] = Field(
        {}, description="Metrics to monitor with thresholds for this feature flag"
    )
    # Bounded on the request schemas only. The base and the response stay
    # unbounded so that a row stored before the bound (say 150) still reads
    # back; the monitor clamps it when it uses it (#629).
    rollback_percentage: int = Field(
        0,
        ge=0,
        le=100,
        description="Percentage to roll back to if automatic rollback is triggered",
    )


class FeatureFlagSafetyConfigUpdate(BaseModel):
    """Create or update a flag's safety configuration.

    Every field is optional. A field left out keeps its stored value, or takes
    its default (enabled true, metrics {}, rollback_percentage 0) when the call
    creates the configuration. null is refused for every field (422).
    """

    # Every field is stored in a NOT NULL column (#954). Defaulting to None
    # only means "not sent": pydantic does not validate a default, while an
    # explicit null reaches refuse_null.
    enabled: bool = Field(None)
    metrics: Dict[str, MetricThreshold] = Field(None)
    rollback_percentage: int = Field(None, ge=0, le=100)

    @field_validator("enabled", "metrics", "rollback_percentage", mode="before")
    @classmethod
    def refuse_null(cls, value: Any, info: ValidationInfo) -> Any:
        """Refuse an explicit null; a field left out never gets here."""
        if value is None:
            raise PydanticCustomError(
                "null_not_allowed", f"{info.field_name} cannot be null"
            )
        return value


class FeatureFlagSafetyConfigResponse(FeatureFlagSafetyConfigBase):
    """Response schema for feature flag safety configuration."""

    id: UUID
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @field_validator("metrics", mode="before")
    @classmethod
    def stored_null_metrics_is_empty(cls, value: Any) -> Any:
        """A row written before #954 can hold a JSON null ``metrics``: read it
        as no thresholds, so its reads and the safety monitor keep working."""
        return {} if value is None else value


class MetricValue(BaseModel):
    """Value of a safety metric."""

    value: float
    status: HealthStatus
    threshold: Optional[MetricThreshold] = None


class SafetyCheckResponse(BaseModel):
    """Response schema for safety check."""

    feature_flag_id: UUID
    is_healthy: bool
    metrics: List[MetricStatus]
    last_checked: datetime = Field(default_factory=datetime.utcnow)
    details: Optional[Dict[str, Any]] = None


class SafetyRollbackRecordBase(BaseModel):
    """Base schema for safety rollback record."""

    feature_flag_id: UUID
    trigger_type: str = Field(
        ..., description="Type of trigger: 'automatic', 'manual', 'scheduled'"
    )
    trigger_reason: str = Field(..., description="Reason for the rollback")
    previous_percentage: int
    target_percentage: int


class SafetyRollbackRecordCreate(SafetyRollbackRecordBase):
    """Schema for creating safety rollback record."""


class SafetyRollbackRecordResponse(SafetyRollbackRecordBase):
    """Response schema for safety rollback record."""

    id: UUID
    # None for a flag that had no safety configuration of its own (#1206).
    safety_config_id: Optional[UUID] = None
    created_at: datetime
    success: bool
    executed_by_user_id: Optional[UUID] = None  # NULL for automatic rollbacks

    model_config = ConfigDict(from_attributes=True)


class RollbackResponse(BaseModel):
    """Response schema for rollback operation."""

    success: bool
    feature_flag_id: UUID
    message: str
    trigger_type: Optional[str] = None
    previous_percentage: Optional[int] = None
    new_percentage: Optional[int] = None
    rollback_record_id: Optional[UUID] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    details: Optional[Dict[str, Any]] = None
