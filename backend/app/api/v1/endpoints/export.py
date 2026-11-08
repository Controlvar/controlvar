"""
Data export and reporting endpoints (EP-020).

GET  /api/v1/export/experiments           — Download all experiments as CSV/JSON
GET  /api/v1/export/variants              — Download variant results as CSV/JSON
GET  /api/v1/export/feature-flags         — Download feature flag data as CSV/JSON
GET  /api/v1/export/reports/overview      — Platform overview report (JSON)
GET  /api/v1/export/reports/experiments/{id} — Single-experiment report (JSON or CSV)
"""

from datetime import datetime
from typing import Any, Dict, Optional, Union
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from backend.app.api import deps
from backend.app.api.deps import get_db
from backend.app.api.off_loop_route import OffLoopRoute
from backend.app.models.user import User
from backend.app.schemas.export import ExportFormat, ExportRequest, ExportScope
from backend.app.services.export_service import ExportService

router = APIRouter(route_class=OffLoopRoute)

# The 200 of a route that can answer CSV: FastAPI documents application/json
# by default, and this adds the text/csv the route also returns.
_CSV_OR_JSON: Dict[Union[int, str], Dict[str, Any]] = {
    200: {"content": {"text/csv": {"schema": {"type": "string"}}}}
}
_SCOPE_DESCRIPTION = (
    "Data scope. Only `summary` is available: `events` and `assignments` answer 422."
)


def _require_summary_scope(scope: ExportScope) -> None:
    """Refuse a scope other than ``summary``: no other scope is available."""
    if scope != ExportScope.SUMMARY:
        raise HTTPException(
            status_code=422,
            detail=(
                f"scope={scope.value} is not supported; "
                "the export is available with scope=summary only"
            ),
        )


# ---------------------------------------------------------------------------
# Export: Experiments
# ---------------------------------------------------------------------------


@router.get(
    "/experiments",
    summary="Export experiments to CSV or JSON",
    description=(
        "Download all (or filtered) experiments as a CSV or JSON file. "
        "Requires authentication. Available to all roles."
    ),
    tags=["Export"],
    responses=_CSV_OR_JSON,
)
def export_experiments(
    format: ExportFormat = Query(
        ExportFormat.CSV, description="Output format: csv or json"
    ),
    scope: ExportScope = Query(ExportScope.SUMMARY, description=_SCOPE_DESCRIPTION),
    start_date: Optional[datetime] = Query(
        None, description="Filter by created_at >= start_date (inclusive)"
    ),
    end_date: Optional[datetime] = Query(
        None, description="Filter by created_at <= end_date (inclusive)"
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> Response:
    """
    Download all experiments as CSV (default) or JSON.

    Supports optional date filtering using `start_date` and `end_date`.
    The file is sent as an attachment, built before the response starts.
    """
    _require_summary_scope(scope)
    request = ExportRequest(
        format=format,
        scope=scope,
        start_date=start_date,
        end_date=end_date,
    )
    service = ExportService(db)
    content, content_type = service.export_experiments(request)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"experiments_{timestamp}.{format.value}"

    return Response(
        content=content,
        media_type=content_type,
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ---------------------------------------------------------------------------
# Export: Variants
# ---------------------------------------------------------------------------


@router.get(
    "/variants",
    summary="Export variant results to CSV or JSON",
    description=(
        "Download per-variant results for all (or filtered) experiments. "
        "Each variant's numbers are its primary-metric result, as "
        "GET /api/v1/results/{experiment_id} reports it. "
        "Requires authentication."
    ),
    tags=["Export"],
    responses=_CSV_OR_JSON,
)
def export_variants(
    format: ExportFormat = Query(
        ExportFormat.CSV, description="Output format: csv or json"
    ),
    scope: ExportScope = Query(ExportScope.SUMMARY, description=_SCOPE_DESCRIPTION),
    start_date: Optional[datetime] = Query(
        None, description="Filter experiments created at or after this date"
    ),
    end_date: Optional[datetime] = Query(
        None, description="Filter experiments created at or before this date"
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> Response:
    """
    Download variant-level results as CSV (default) or JSON.

    Each row represents one variant within one experiment, with aggregated
    metrics such as assignments, conversions, p-value and statistical significance.
    """
    _require_summary_scope(scope)
    request = ExportRequest(
        format=format,
        scope=scope,
        start_date=start_date,
        end_date=end_date,
    )
    service = ExportService(db)
    content, content_type = service.export_variants(request)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"variants_{timestamp}.{format.value}"

    return Response(
        content=content,
        media_type=content_type,
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ---------------------------------------------------------------------------
# Export: Feature Flags
# ---------------------------------------------------------------------------


@router.get(
    "/feature-flags",
    summary="Export feature flag data to CSV or JSON",
    description=(
        "Download feature flag usage data as a CSV or JSON file. "
        "Requires authentication."
    ),
    tags=["Export"],
    responses=_CSV_OR_JSON,
)
def export_feature_flags(
    format: ExportFormat = Query(
        ExportFormat.CSV, description="Output format: csv or json"
    ),
    start_date: Optional[datetime] = Query(
        None, description="Filter flags created at or after this date"
    ),
    end_date: Optional[datetime] = Query(
        None, description="Filter flags created at or before this date"
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> Response:
    """
    Download feature flag data as CSV (default) or JSON.

    Includes rollout percentages, evaluation counts, and enabled rates for
    each feature flag.
    """
    request = ExportRequest(
        format=format,
        scope=ExportScope.SUMMARY,
        start_date=start_date,
        end_date=end_date,
    )
    service = ExportService(db)
    content, content_type = service.export_feature_flags(request)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"feature_flags_{timestamp}.{format.value}"

    return Response(
        content=content,
        media_type=content_type,
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ---------------------------------------------------------------------------
# Reports: Platform Overview
# ---------------------------------------------------------------------------


@router.get(
    "/reports/overview",
    summary="Platform overview report",
    description=(
        "Returns a JSON summary of platform-wide experiment and feature flag "
        "activity. Optionally filtered by a date range."
    ),
    tags=["Reports"],
)
def get_overview_report(
    start_date: Optional[datetime] = Query(
        None, description="Report period start (inclusive)"
    ),
    end_date: Optional[datetime] = Query(
        None, description="Report period end (inclusive)"
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> JSONResponse:
    """
    Generate a platform-wide overview report.

    Returns aggregate counts for experiments, feature flags, assignments, events,
    and experiment winners. Supports optional date-range filtering.
    """
    service = ExportService(db)
    report = service.generate_platform_overview(
        start_date=start_date,
        end_date=end_date,
    )
    return JSONResponse(content=report.model_dump())


# ---------------------------------------------------------------------------
# Reports: Single Experiment
# ---------------------------------------------------------------------------


@router.get(
    "/reports/experiments/{experiment_id}",
    summary="Full experiment report",
    description=(
        "Returns a report for a single experiment: with `format=json` (the "
        "default) its experiment row and variant rows, with `format=csv` its "
        "variant rows as CSV, in the columns of GET /api/v1/export/variants."
    ),
    tags=["Reports"],
    responses={
        **_CSV_OR_JSON,
        404: {"description": "No experiment has this id."},
    },
)
def get_experiment_report(
    experiment_id: UUID,
    format: ExportFormat = Query(
        ExportFormat.JSON, description="Output format: csv or json"
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> Response:
    """
    Generate a detailed report for a single experiment.

    JSON carries the experiment row and its variant rows; CSV carries the
    variant rows, with the same columns as ``/export/variants``.
    """
    service = ExportService(db)
    rows = service.experiment_report_rows(experiment_id)
    if rows is None:
        raise HTTPException(status_code=404, detail="Experiment not found")
    experiment_rows, variant_rows = rows

    if format == ExportFormat.CSV:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"experiment_report_{experiment_id}_{timestamp}.csv"
        return Response(
            content=service.variants_to_csv(variant_rows),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    return JSONResponse(
        content={
            "experiment_id": str(experiment_id),
            "experiments": [row.model_dump() for row in experiment_rows],
            "variants": [row.model_dump() for row in variant_rows],
        }
    )
