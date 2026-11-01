"""
Metrics API endpoints.

This module provides API endpoints for retrieving metrics related to
feature flag evaluations, performance, and errors.
"""

from datetime import datetime, timedelta
from typing import Annotated, Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.exceptions import RequestValidationError
from pydantic import AfterValidator
from sqlalchemy.orm import Session

from backend.app.api.deps import get_current_user, get_db
from backend.app.models.user import User
from backend.app.schemas.metrics import (
    AggregatedMetricResponse,
    AggregationPeriod,
    ErrorLogResponse,
    MetricsFilterParams,
    MetricsSummary,
    MetricType,
)
from backend.app.services.metrics_service import SUMMARY_DEFAULT_PERIOD, MetricsService

router = APIRouter()

#: PostgreSQL stores a UTC offset of at most 15:59:59 either way, and fails a
#: query that compares with a larger one ("time zone displacement out of
#: range"), which answered 500 (#956, #1561).
OFFSET_LIMIT = timedelta(hours=16)

#: The fixed messages of the summary's two date refusals. Neither includes the
#: value sent. Pydantic prefixes a validator's message with "Value error, ";
#: the second refusal is raised by hand, so it carries the prefix itself.
OFFSET_TOO_LARGE = "the UTC offset must be less than 16 hours"
END_DATE_TOO_EARLY = (
    "Value error, end_date is too early for the default 30-day period; "
    "send start_date as well"
)


def _storable_offset(value: Optional[datetime]) -> Optional[datetime]:
    """Refuse a date whose UTC offset is 16 hours or more either way.

    A refusal is a validation error, i.e. 422 at ``["query", <field>]``. A
    naive date, ``Z`` and offsets up to 15:59 either way pass unchanged. Used
    as the ``AfterValidator`` inside the parameter's ``Annotated``, with the
    ``Query`` beside it and ``None`` as the plain default: with the default
    given as ``= Query(None, ...)`` instead, the validator is not run at all.
    """
    if value is not None:
        offset = value.utcoffset()
        if offset is not None and abs(offset) >= OFFSET_LIMIT:
            raise ValueError(OFFSET_TOO_LARGE)
    return value


def _end_date_too_early() -> RequestValidationError:
    """The 422 for an ``end_date`` whose default start falls before year 1.

    The same shape as every other request validation error, through the
    application's handler; nothing in it comes from the request.
    """
    return RequestValidationError(
        [
            {
                "type": "value_error",
                "loc": ("query", "end_date"),
                "msg": END_DATE_TOO_EARLY,
            }
        ]
    )


@router.get(
    "/summary",
    response_model=MetricsSummary,
    summary="Get metrics summary",
    description=(
        "Get summary statistics for feature flag metrics over ``start_date`` to "
        "``end_date``, by default the 30 days before now. ``error_rate`` counts "
        "errors by the safety check's rule, and ``uncounted_error_count`` is the "
        "error reports that do not count."
    ),
)
def get_metrics_summary(
    feature_flag_id: Optional[UUID] = Query(
        None, description="Filter by feature flag ID"
    ),
    start_date: Annotated[
        Optional[datetime],
        Query(description="Start date for metrics"),
        AfterValidator(_storable_offset),
    ] = None,
    end_date: Annotated[
        Optional[datetime],
        Query(description="End date for metrics"),
        AfterValidator(_storable_offset),
    ] = None,
    period: AggregationPeriod = Query(
        AggregationPeriod.DAY, description="Aggregation period"
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> MetricsSummary:
    """
    Get a summary of metrics for analysis and dashboards.

    This endpoint provides aggregated statistics such as total evaluations,
    unique users, average latency, and error rates.

    A ``start_date`` or ``end_date`` whose UTC offset is 16 hours or more, or
    an ``end_date`` sent alone whose default start would fall before year 1,
    answers 422 (#1561); both used to answer 500.
    """
    if start_date is None and end_date is not None:
        # The service's own default, computed here so that a start before
        # year 1 is refused at end_date rather than raised from the service.
        try:
            start_date = end_date - SUMMARY_DEFAULT_PERIOD
        except OverflowError:
            raise _end_date_too_early() from None
    return MetricsService.get_metrics_summary(
        db=db,
        feature_flag_id=feature_flag_id,
        start_date=start_date,
        end_date=end_date,
        period=period,
    )


@router.get(
    "/aggregated",
    response_model=List[AggregatedMetricResponse],
    summary="Get aggregated metrics",
    description="Get aggregated metrics based on filter criteria.",
)
def get_aggregated_metrics(
    metric_type: Optional[MetricType] = Query(
        None, description="Filter by metric type"
    ),
    feature_flag_id: Optional[UUID] = Query(
        None, description="Filter by feature flag ID"
    ),
    segment_id: Optional[UUID] = Query(None, description="Filter by segment ID"),
    targeting_rule_id: Optional[str] = Query(
        None, description="Filter by targeting rule ID"
    ),
    period: Optional[AggregationPeriod] = Query(None, description="Aggregation period"),
    start_date: Optional[datetime] = Query(None, description="Start date for metrics"),
    end_date: Optional[datetime] = Query(None, description="End date for metrics"),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(
        100, ge=1, le=1000, description="Maximum number of records to return"
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> List[AggregatedMetricResponse]:
    """
    Get aggregated metrics for analysis and dashboards.

    This endpoint provides access to pre-aggregated metrics data based on
    different time periods and filter criteria.
    """
    filters = MetricsFilterParams(
        metric_type=metric_type,
        feature_flag_id=feature_flag_id,
        segment_id=segment_id,
        targeting_rule_id=targeting_rule_id,
        start_date=start_date,
        end_date=end_date,
        period=period,
    )

    return MetricsService.get_aggregated_metrics(
        db=db,
        params=filters,
        skip=skip,
        limit=limit,
    )


@router.get(
    "/errors",
    response_model=List[ErrorLogResponse],
    summary="Get error logs",
    description="Get error logs based on filter criteria.",
)
def get_error_logs(
    feature_flag_id: Optional[UUID] = Query(
        None, description="Filter by feature flag ID"
    ),
    start_date: Optional[datetime] = Query(None, description="Start date for errors"),
    end_date: Optional[datetime] = Query(None, description="End date for errors"),
    error_type: Optional[str] = Query(None, description="Filter by error type"),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(
        100, ge=1, le=1000, description="Maximum number of records to return"
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> List[ErrorLogResponse]:
    """
    Get error logs for debugging and monitoring.

    This endpoint provides access to error logs that occurred during
    feature flag evaluation and other system operations.
    """
    return MetricsService.get_error_logs(
        db=db,
        feature_flag_id=feature_flag_id,
        start_date=start_date,
        end_date=end_date,
        error_type=error_type,
        skip=skip,
        limit=limit,
    )


@router.post(
    "/aggregate",
    summary="Trigger metrics aggregation",
    description="Manually trigger the aggregation of raw metrics data.",
)
def trigger_aggregation(
    period: AggregationPeriod = Query(
        AggregationPeriod.DAY, description="Aggregation period"
    ),
    feature_flag_id: Optional[UUID] = Query(
        None, description="Filter by feature flag ID"
    ),
    metric_type: Optional[MetricType] = Query(
        None, description="Filter by metric type"
    ),
    start_time: Optional[datetime] = Query(
        None, description="Start time for aggregation"
    ),
    end_time: Optional[datetime] = Query(None, description="End time for aggregation"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Manually trigger the aggregation of raw metrics data.

    This endpoint is useful for testing or forcing aggregation outside
    of the scheduled aggregation tasks.
    """
    metrics_created = MetricsService.aggregate_metrics(
        db=db,
        period=period,
        start_time=start_time,
        end_time=end_time,
        feature_flag_id=feature_flag_id,
        metric_type=metric_type,
    )

    return {
        "status": "success",
        "aggregated_records": metrics_created,
        "period": period,
    }
