"""
Pydantic schemas for data export and report generation endpoints.
These define the API contract for EP-020.
"""

from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class ExportFormat(str, Enum):
    CSV = "csv"
    JSON = "json"


class ExportScope(str, Enum):
    SUMMARY = "summary"  # Aggregated results only
    EVENTS = "events"  # Raw events
    ASSIGNMENTS = "assignments"  # Assignment data


class ReportType(str, Enum):
    EXPERIMENT_SUMMARY = "experiment_summary"
    EXPERIMENT_RESULTS = "experiment_results"
    FEATURE_FLAG_USAGE = "feature_flag_usage"
    PLATFORM_OVERVIEW = "platform_overview"


class ExportRequest(BaseModel):
    format: ExportFormat = ExportFormat.CSV
    scope: ExportScope = ExportScope.SUMMARY
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    include_variants: bool = True
    include_metrics: bool = True


class ReportRequest(BaseModel):
    report_type: ReportType
    experiment_ids: Optional[List[str]] = None  # None = all experiments
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    format: ExportFormat = ExportFormat.JSON


class ExperimentExportRow(BaseModel):
    """One row in a CSV/JSON export of experiment data."""

    experiment_id: str
    experiment_name: str
    status: str
    experiment_type: str
    start_date: Optional[str]
    end_date: Optional[str]
    duration_days: Optional[float]
    total_assignments: int
    total_events: int
    winner_variant: Optional[str]
    recommendation: Optional[str]


class VariantExportRow(BaseModel):
    """
    One variant of one experiment, with its primary-metric result.

    The result columns come from the results API's computation; they are None
    when the experiment's results cannot be computed (no control variant, or
    no metric).
    """

    experiment_id: str
    experiment_name: str
    variant_id: str
    variant_name: str
    is_control: bool
    assignments: Optional[int]
    conversions: Optional[int]
    conversion_rate: Optional[float]
    p_value: Optional[float]
    is_significant: Optional[bool]
    relative_improvement_pct: Optional[float]


class FeatureFlagExportRow(BaseModel):
    flag_id: str
    flag_key: str
    flag_name: str
    status: str
    rollout_percentage: int
    total_evaluations: int
    enabled_evaluations: int
    enabled_rate: float
    created_at: str
    updated_at: str


class PlatformOverviewReport(BaseModel):
    generated_at: str
    period_start: Optional[str]
    period_end: Optional[str]
    total_experiments: int
    active_experiments: int
    completed_experiments: int
    total_feature_flags: int
    active_feature_flags: int
    total_assignments: int
    total_events: int
    experiments_with_winners: int = Field(
        description=(
            "Experiments created in the period whose current results name a "
            "winner (the export's winner_variant rule). Recomputed on every call."
        )
    )
    average_experiment_duration_days: Optional[float]
