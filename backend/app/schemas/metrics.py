"""
Pydantic schemas for metrics data structures.

This module defines schemas for validation and serialization of metrics-related data.
"""

from datetime import datetime
from typing import Any, Dict, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backend.app.models.metrics.metric import AggregationPeriod, MetricType


class MetricBase(BaseModel):
    """Base model for metrics data."""

    metric_type: MetricType = Field(..., description="Type of metric")
    feature_flag_id: Optional[UUID] = Field(None, description="Related feature flag ID")
    targeting_rule_id: Optional[str] = Field(
        None, description="ID of targeting rule if applicable"
    )
    user_id: Optional[str] = Field(None, description="User ID if applicable")
    segment_id: Optional[UUID] = Field(
        None, description="Related segment ID if applicable"
    )
    value: Optional[float] = Field(None, description="Numeric value if applicable")
    metadata: Optional[Dict[str, Any]] = Field(
        None, description="Additional metric metadata"
    )

    model_config = ConfigDict(use_enum_values=True)


class RawMetricCreate(MetricBase):
    """Schema for creating a new raw metric."""

    count: int = Field(1, description="Number of occurrences")
    timestamp: Optional[datetime] = Field(None, description="Timestamp of the metric")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "metric_type": "flag_evaluation",
                "feature_flag_id": "123e4567-e89b-12d3-a456-426614174000",
                "user_id": "user123",
                "value": 4.5,
                "count": 1,
                "metadata": {"browser": "Chrome", "device": "mobile"},
            }
        }
    )


class RawMetricResponse(MetricBase):
    """Schema for raw metric response."""

    id: UUID
    timestamp: datetime
    count: int = Field(1, description="Number of occurrences")
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(
        from_attributes=True,
        # Map schema field 'metadata' to model field 'meta_data'
        json_schema_extra={"metadata": {"alias": "meta_data"}},
    )


class AggregatedMetricBase(BaseModel):
    """Base model for aggregated metrics."""

    metric_type: MetricType = Field(..., description="Type of metric")
    period: AggregationPeriod = Field(..., description="Aggregation period")
    period_start: datetime = Field(..., description="Start of the aggregation period")
    feature_flag_id: Optional[UUID] = Field(None, description="Related feature flag ID")
    targeting_rule_id: Optional[str] = Field(
        None, description="ID of targeting rule if applicable"
    )
    segment_id: Optional[UUID] = Field(
        None, description="Related segment ID if applicable"
    )
    count: int = Field(..., description="Number of occurrences")
    sum_value: Optional[float] = Field(None, description="Sum of values")
    min_value: Optional[float] = Field(None, description="Minimum value")
    max_value: Optional[float] = Field(None, description="Maximum value")
    distinct_users: Optional[int] = Field(None, description="Count of distinct users")
    metadata: Optional[Dict[str, Any]] = Field(
        None, description="Additional aggregated data"
    )

    model_config = ConfigDict(use_enum_values=True)


class AggregatedMetricResponse(AggregatedMetricBase):
    """Schema for aggregated metric response."""

    id: UUID
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(
        from_attributes=True,
        # Map schema field 'metadata' to model field 'meta_data'
        json_schema_extra={"metadata": {"alias": "meta_data"}},
    )


class ErrorLogBase(BaseModel):
    """Base model for error logs."""

    error_type: str = Field(..., description="Type of error")
    feature_flag_id: Optional[UUID] = Field(None, description="Related feature flag ID")
    user_id: Optional[str] = Field(None, description="User ID if applicable")
    message: str = Field(..., description="Error message")
    stack_trace: Optional[str] = Field(None, description="Stack trace if available")
    request_data: Optional[Dict[str, Any]] = Field(
        None, description="Context of the request"
    )
    metadata: Optional[Dict[str, Any]] = Field(
        None, description="Additional debug data"
    )


class ErrorLogCreate(ErrorLogBase):
    """Schema for creating a new error log."""

    timestamp: Optional[datetime] = Field(None, description="Timestamp of the error")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "error_type": "rule_evaluation_error",
                "feature_flag_id": "123e4567-e89b-12d3-a456-426614174000",
                "user_id": "user123",
                "message": "Failed to evaluate targeting rule",
                "request_data": {"context": {"country": "US"}},
            }
        }
    )


class ErrorLogResponse(ErrorLogBase):
    """Schema for error log response."""

    id: UUID
    timestamp: datetime
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(
        from_attributes=True,
        # Map schema field 'metadata' to model field 'meta_data'
        json_schema_extra={"metadata": {"alias": "meta_data"}},
    )


class MetricsFilterParams(BaseModel):
    """Query parameters for filtering metrics."""

    metric_type: Optional[MetricType] = Field(None, description="Filter by metric type")
    feature_flag_id: Optional[UUID] = Field(
        None, description="Filter by feature flag ID"
    )
    segment_id: Optional[UUID] = Field(None, description="Filter by segment ID")
    targeting_rule_id: Optional[str] = Field(
        None, description="Filter by targeting rule ID"
    )
    start_date: Optional[datetime] = Field(None, description="Filter from this date")
    end_date: Optional[datetime] = Field(None, description="Filter until this date")
    period: Optional[AggregationPeriod] = Field(None, description="Aggregation period")

    model_config = ConfigDict(use_enum_values=True)


class MetricsSummary(BaseModel):
    """Summary statistics for metrics."""

    total_evaluations: int = Field(..., description="Total number of flag evaluations")
    unique_users: int = Field(..., description="Number of unique users")
    avg_latency: Optional[float] = Field(
        None, description="Average latency in milliseconds"
    )
    rule_match_rate: Optional[float] = Field(
        None, description="Percentage of rule matches"
    )
    error_rate: Optional[float] = Field(
        None,
        description=(
            "Errors that count toward the safety check, as a percentage of the "
            "aggregated flag evaluations in the period (it can exceed 100 when "
            "errors outnumber them); null when the period has no aggregated flag "
            "evaluations. Errors are counted by the safety check's rule: "
            "flag evaluations that failed on the server count, and only a report "
            "sent with a server-side key (an API key with the ``sdk:ruleset`` "
            "scope, whose owner can change feature flags) counts; the reports "
            "that do not count are in ``uncounted_error_count``. The value still "
            "differs from the safety check's ``error_rate`` in three ways: the "
            "period is ``start_date`` to ``end_date`` (by default the 30 days "
            "before now), not the last 15 minutes; the denominator is the "
            "aggregated flag evaluations of ``period``, not the raw flag "
            "evaluations; and the unit is a percentage, not a fraction."
        ),
    )
    uncounted_error_count: int = Field(
        ...,
        ge=0,
        description=(
            "Error reports in the period that are stored but do not count toward "
            "``error_rate`` or the safety check: those sent with a key that is "
            "not a server-side key, and those stored before 0.29.0. The safety "
            "check shows the same reports in its own window as "
            "``details.uncounted_error_count``."
        ),
    )
