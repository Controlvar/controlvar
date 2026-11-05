"""
ETL & Glue Job REST endpoints (P3-A).

Provides endpoints for managing AWS Glue ETL jobs, S3 partition
registration, and Glue crawler management.

Endpoints:
    POST /api/v1/etl/jobs/run              — trigger ETL job (ADMIN/DEVELOPER)
    GET  /api/v1/etl/jobs/{run_id}/status  — get job run status (any authenticated user)
    POST /api/v1/etl/partitions/add        — register a date's 24 hourly partitions (ADMIN)
    GET  /api/v1/etl/crawler/status        — get crawler state (any authenticated user)
    POST /api/v1/etl/crawler/run           — trigger crawler (ADMIN)

Every route works only on the names this deployment configured
(``GLUE_ETL_JOB_NAME``, ``GLUE_METRICS_JOB_NAME``, ``GLUE_CRAWLER_NAME``,
``GLUE_DATABASE``, ``GLUE_EVENTS_TABLE``). Any other name answers 404 with a
fixed detail before Glue is called; ``ETLService`` holds the check.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from backend.app.api import deps
from backend.app.api.off_loop_route import OffLoopRoute
from backend.app.models.user import User, UserRole
from modules.backend.app.schemas.etl import (
    ETLJobRequest,
    ETLJobResponse,
    ETLJobType,
    GlueCrawlerStatus,
    PartitionInfo,
)
from modules.backend.app.services.etl_service import ETLService

logger = logging.getLogger(__name__)

router = APIRouter(route_class=OffLoopRoute)

# ---------------------------------------------------------------------------
# Dependency — shared ETLService instance (override-able in tests)
# ---------------------------------------------------------------------------

_etl_service: Optional[ETLService] = None


async def get_etl_service() -> ETLService:
    """
    Dependency that returns a singleton ETLService.

    ``async`` so that FastAPI calls it on the event loop rather than in a
    worker thread; the service is built once per process, on first use.

    Can be overridden in tests via app.dependency_overrides.
    """
    global _etl_service
    if _etl_service is None:
        _etl_service = ETLService()
    return _etl_service


# ---------------------------------------------------------------------------
# Permission helpers
# ---------------------------------------------------------------------------


def _require_admin_or_developer(current_user: User) -> None:
    """Raise 403 if the user is not ADMIN or DEVELOPER."""
    allowed = {UserRole.ADMIN, UserRole.DEVELOPER}
    if current_user.role not in allowed and not current_user.is_superuser:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only ADMIN or DEVELOPER users can perform this action.",
        )


def _require_admin(current_user: User) -> None:
    """Raise 403 if the user is not ADMIN."""
    if current_user.role != UserRole.ADMIN and not current_user.is_superuser:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only ADMIN users can perform this action.",
        )


# ---------------------------------------------------------------------------
# POST /jobs/run — trigger ETL job
# ---------------------------------------------------------------------------


@router.post(
    "/jobs/run",
    response_model=ETLJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Trigger an ETL job run",
    description=(
        "Start a Glue ETL job for transforming raw S3 events to Parquet, "
        "aggregating metrics, or generating daily summaries. "
        "Requires ADMIN or DEVELOPER role."
    ),
    responses={
        202: {"description": "Job started successfully"},
        403: {"description": "Insufficient permissions"},
        404: {"description": "The job type's Glue job is not configured"},
        422: {"description": "Invalid request parameters"},
        500: {"description": "Failed to start Glue job"},
    },
)
def run_etl_job(
    request: ETLJobRequest,
    current_user: User = Depends(deps.get_current_active_user),
    svc: ETLService = Depends(get_etl_service),
) -> ETLJobResponse:
    """Trigger an AWS Glue ETL job run."""
    _require_admin_or_developer(current_user)

    logger.info(
        f"User {current_user.username} triggering ETL job "
        f"type={request.job_type} date={request.date}"
    )
    return svc.run_etl_job(request)


# ---------------------------------------------------------------------------
# GET /jobs/{run_id}/status — get job status
# ---------------------------------------------------------------------------


@router.get(
    "/jobs/{run_id}/status",
    response_model=ETLJobResponse,
    summary="Get ETL job run status",
    description=(
        "Retrieve the current status of a Glue job run. Any authenticated user "
        "can read. `job_name` must be one of the configured jobs "
        "(`GLUE_ETL_JOB_NAME` or `GLUE_METRICS_JOB_NAME`); any other name "
        'answers 404 "Unknown ETL job".'
    ),
    responses={
        200: {"description": "Job status returned"},
        404: {"description": "Unknown ETL job, or job run not found"},
    },
)
def get_job_status(
    run_id: str,
    job_name: str = Query(
        ...,
        description="Glue job name: GLUE_ETL_JOB_NAME or GLUE_METRICS_JOB_NAME",
    ),
    job_type: Optional[ETLJobType] = Query(None, description="ETL job type (optional)"),
    current_user: User = Depends(deps.get_current_active_user),
    svc: ETLService = Depends(get_etl_service),
) -> ETLJobResponse:
    """Return the current status of a Glue job run."""
    return svc.get_job_status(
        job_run_id=run_id,
        job_name=job_name,
        job_type=job_type,
    )


# ---------------------------------------------------------------------------
# POST /partitions/add — add Glue catalog partitions
# ---------------------------------------------------------------------------


@router.post(
    "/partitions/add",
    response_model=list[PartitionInfo],
    status_code=status.HTTP_201_CREATED,
    summary="Add S3 partitions to the Glue catalog",
    description=(
        "Register 24 hourly Hive-style partitions for a given date, under the "
        "Glue table's own storage location. Requires ADMIN role. `database` and "
        "`table` must be the configured `GLUE_DATABASE` and `GLUE_EVENTS_TABLE`; "
        'anything else answers 404 "Unknown Glue table". 201 means Glue '
        "registered every hour, or already had it: re-running a date is safe. "
        "A `date` that is not a calendar date in YYYY-MM-DD form answers 422."
    ),
    responses={
        201: {"description": "All 24 partitions are registered"},
        403: {"description": "Insufficient permissions"},
        404: {
            "description": (
                'Unknown Glue table, or "Glue table not found. Run the crawler '
                'first." when the configured table is not in the catalog'
            )
        },
        500: {
            "description": (
                "Could not read the Glue table, or could not register the "
                "partitions. Some hours may already be registered; re-run the "
                "date once the cause is fixed."
            )
        },
    },
)
def add_partitions(
    database: str = Query(..., description="Glue database name: GLUE_DATABASE"),
    table: str = Query(..., description="Glue table name: GLUE_EVENTS_TABLE"),
    date: str = Query(..., description="Date in YYYY-MM-DD format"),
    current_user: User = Depends(deps.get_current_active_user),
    svc: ETLService = Depends(get_etl_service),
) -> list[PartitionInfo]:
    """Register hourly S3 partitions in the Glue catalog for a given date."""
    _require_admin(current_user)

    logger.info(f"User {current_user.username} adding partitions for date={date}")
    return svc.add_partitions(database=database, table=table, date=date)


# ---------------------------------------------------------------------------
# GET /crawler/status — get crawler state
# ---------------------------------------------------------------------------


@router.get(
    "/crawler/status",
    response_model=GlueCrawlerStatus,
    summary="Get Glue crawler status",
    description=(
        "Return the current state of the Glue crawler. Any authenticated user "
        "can read. `crawler_name` may be omitted; if given it must be the "
        "configured `GLUE_CRAWLER_NAME`, and any other name answers 404 "
        '"Unknown crawler".'
    ),
    responses={
        200: {"description": "Crawler status returned"},
        404: {"description": "Unknown crawler, or crawler not found"},
    },
)
def get_crawler_status(
    crawler_name: Optional[str] = Query(
        None,
        description="Glue crawler name: GLUE_CRAWLER_NAME (the default)",
    ),
    current_user: User = Depends(deps.get_current_active_user),
    svc: ETLService = Depends(get_etl_service),
) -> GlueCrawlerStatus:
    """Return the current state of the Glue crawler."""
    return svc.get_crawler_status(crawler_name=crawler_name)


# ---------------------------------------------------------------------------
# POST /crawler/run — trigger crawler
# ---------------------------------------------------------------------------


@router.post(
    "/crawler/run",
    response_model=GlueCrawlerStatus,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Trigger the Glue crawler",
    description=(
        "Start the Glue crawler to discover new partitions and update the catalog. "
        "Requires ADMIN role. `crawler_name` may be omitted; if given it must be "
        "the configured `GLUE_CRAWLER_NAME`, and any other name answers 404 "
        '"Unknown crawler".'
    ),
    responses={
        202: {"description": "Crawler started"},
        403: {"description": "Insufficient permissions"},
        404: {"description": "Unknown crawler"},
        409: {"description": "Crawler already running"},
        500: {"description": "Failed to start crawler"},
    },
)
def run_crawler(
    crawler_name: Optional[str] = Query(
        None,
        description="Glue crawler name: GLUE_CRAWLER_NAME (the default)",
    ),
    current_user: User = Depends(deps.get_current_active_user),
    svc: ETLService = Depends(get_etl_service),
) -> GlueCrawlerStatus:
    """Start the Glue crawler and return its status."""
    _require_admin(current_user)

    logger.info(f"User {current_user.username} triggering the Glue crawler")
    return svc.run_crawler(crawler_name=crawler_name)
