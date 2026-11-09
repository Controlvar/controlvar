"""
Audit Log API endpoints.

This module provides RESTful API endpoints for querying audit logs,
enabling administrators and authorized users to view the complete
audit trail of all system actions.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator, Literal, Optional
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Path,
    Query,
    Response,
    status,
)
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from sqlalchemy import desc
from sqlalchemy.orm import Session

from backend.app.api import deps
from backend.app.api.datetime_bounds import refuse_unstorable_offset
from backend.app.api.off_loop_route import OffLoopRoute
from backend.app.core.logger import unexpected_failure
from backend.app.core.permissions import can_read_all_audit_logs
from backend.app.models.audit_log import ActionType, AuditLog, EntityType
from backend.app.models.user import User
from backend.app.schemas.audit_log import (
    AuditLogListResponse,
    AuditLogResponse,
    AuditStatsResponse,
)
from backend.app.services import audit_export
from backend.app.services.audit_service import AuditService

logger = logging.getLogger(__name__)

AUDIT_LIST_FAILED = "Failed to retrieve audit logs"

# A refused filter names the accepted values and never repeats the submitted
# one, as the schema validators do (#528, #529).
INVALID_ENTITY_TYPE = (
    f"Invalid entity type; expected one of {[e.value for e in EntityType]}"
)
INVALID_ACTION_TYPE = (
    f"Invalid action type; expected one of {[a.value for a in ActionType]}"
)

# Create router with tag for documentation grouping
router = APIRouter(
    route_class=OffLoopRoute,
    tags=["Audit Logs"],
    responses={
        status.HTTP_401_UNAUTHORIZED: {
            "description": "Authentication failed",
            "content": {
                "application/json": {
                    "example": {
                        "error": {
                            "status_code": 401,
                            "message": "Could not validate credentials",
                        }
                    }
                }
            },
        },
        status.HTTP_403_FORBIDDEN: {
            "description": "Permission denied",
            "content": {
                "application/json": {
                    "example": {
                        "error": {
                            "status_code": 403,
                            "message": "Not enough permissions",
                        }
                    }
                }
            },
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "description": "Internal server error",
            "content": {
                "application/json": {
                    "example": {
                        "error": {
                            "status_code": 500,
                            "message": "Internal server error",
                        }
                    }
                }
            },
        },
    },
)


@dataclass(frozen=True)
class AuditLogFilters:
    """The filters a list or an export applies, after the access rule."""

    user_id: Optional[UUID]
    entity_type: Optional[EntityType]
    entity_id: Optional[UUID]
    action_type: Optional[ActionType]
    from_date: Optional[datetime]
    to_date: Optional[datetime]


def resolve_audit_log_filters(
    current_user: User,
    *,
    user_id: Optional[UUID],
    entity_type: Optional[str],
    entity_id: Optional[UUID],
    action_type: Optional[str],
    from_date: Optional[datetime],
    to_date: Optional[datetime],
) -> AuditLogFilters:
    """Apply the access rule to a list or export request, and check its filters.

    Superusers, ADMIN and ANALYST, and in the full profile anyone whose custom
    role or direct grant gives ``audit_log (read)``, see other users' entries
    too. Anyone else sees only their own: ``user_id`` is replaced by their own
    id, whatever was asked. An unknown ``entity_type`` or ``action_type``, or a
    ``to_date`` that is not after ``from_date``, answers 400. A date with a UTC
    offset the database cannot store (16 hours or more) answers 422.
    """
    # Other users' entries need can_read_all_audit_logs; anyone else reads
    # their own.
    if not can_read_all_audit_logs(current_user):
        user_id = current_user.id

    refuse_unstorable_offset("from_date", from_date)
    refuse_unstorable_offset("to_date", to_date)

    entity_type_enum = None
    if entity_type:
        try:
            entity_type_enum = EntityType(entity_type)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=INVALID_ENTITY_TYPE,
            )

    action_type_enum = None
    if action_type:
        try:
            action_type_enum = ActionType(action_type)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=INVALID_ACTION_TYPE,
            )

    if from_date and to_date and to_date <= from_date:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="to_date must be after from_date",
        )

    return AuditLogFilters(
        user_id=user_id,
        entity_type=entity_type_enum,
        entity_id=entity_id,
        action_type=action_type_enum,
        from_date=from_date,
        to_date=to_date,
    )


@router.get(
    "/",
    response_model=AuditLogListResponse,
    summary="List audit logs",
    response_description="Returns a paginated list of audit logs with filtering options",
)
async def list_audit_logs(
    *,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
    user_id: Optional[UUID] = Query(None, description="Filter by user ID"),
    entity_type: Optional[str] = Query(None, description="Filter by entity type"),
    entity_id: Optional[UUID] = Query(None, description="Filter by entity ID"),
    action_type: Optional[str] = Query(None, description="Filter by action type"),
    from_date: Optional[datetime] = Query(
        None, description="Filter logs from this date (ISO format)"
    ),
    to_date: Optional[datetime] = Query(
        None, description="Filter logs until this date (ISO format)"
    ),
    page: int = Query(1, ge=1, description="Page number (1-based)"),
    limit: int = Query(50, ge=1, le=1000, description="Number of records per page"),
) -> AuditLogListResponse:
    """
    Retrieve audit logs with filtering and pagination.

    This endpoint allows authorized users to query audit logs with various filters:
    - **user_id**: Filter by specific user
    - **entity_type**: Filter by entity type (feature_flag, experiment, user, etc.)
    - **entity_id**: Filter by specific entity
    - **action_type**: Filter by action type (toggle_enable, create, update, etc.)
    - **from_date**: Filter logs from this date
    - **to_date**: Filter logs until this date
    - **page**: Page number for pagination
    - **limit**: Number of records per page

    Superusers, ADMIN and ANALYST, and in the full profile anyone whose custom
    role or direct grant gives audit_log (read), see other users' entries too.
    Anyone else sees only their own entries; a `user_id` filter naming anyone
    else is replaced by their own id.
    """
    filters = resolve_audit_log_filters(
        current_user,
        user_id=user_id,
        entity_type=entity_type,
        entity_id=entity_id,
        action_type=action_type,
        from_date=from_date,
        to_date=to_date,
    )

    try:
        # Get audit logs
        audit_logs, total_count = AuditService.get_audit_logs(
            db=db,
            user_id=filters.user_id,
            entity_type=filters.entity_type,
            entity_id=filters.entity_id,
            action_type=filters.action_type,
            from_date=filters.from_date,
            to_date=filters.to_date,
            page=page,
            limit=limit,
        )

        # Convert to response models
        audit_log_responses = [
            AuditLogResponse.model_validate(log) for log in audit_logs
        ]

        # Calculate total pages
        total_pages = (total_count + limit - 1) // limit

        return AuditLogListResponse(
            items=audit_log_responses,
            total=total_count,
            page=page,
            limit=limit,
            total_pages=total_pages,
        )

    except ValidationError as e:
        # A row that does not fit the response model is not the caller's
        # mistake, and pydantic's text repeats the row: answer as below.
        raise unexpected_failure(
            e, "Audit log list", AUDIT_LIST_FAILED, db=db, logger=logger
        )
    except ValueError as e:
        # The service's own page/limit refusals.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except Exception as e:
        raise unexpected_failure(
            e, "Audit log list", AUDIT_LIST_FAILED, db=db, logger=logger
        )


AUDIT_EXPORT_FAILED = "Failed to export audit logs"


def export_cap_message(count: int) -> str:
    """The 422 detail for an export that matches more than the cap."""
    return (
        f"{count} entries match these filters; an export holds at most "
        f"{audit_export.EXPORT_MAX_ROWS}. Narrow from_date and to_date, or filter "
        "by action_type or entity_type."
    )


def _stream_items(query, fmt: str) -> Iterator[str]:
    """The export body: every entry ``query`` returns, newest first."""
    rows = query.order_by(desc(AuditLog.timestamp)).yield_per(
        audit_export.EXPORT_BATCH_SIZE
    )

    def items():
        try:
            for row in rows:
                yield audit_export.entry_item(row)
        except Exception as e:
            # The body ends here, short of X-Total-Count, as it did when the
            # body was sent while it was read. Only the exception type is
            # logged.
            logger.error("Audit log export stopped early (%s)", type(e).__name__)

    if fmt == "csv":
        return audit_export.csv_body(items())
    return audit_export.json_body(items())


@router.get(
    "/export",
    summary="Export audit logs as CSV or JSON",
    response_class=StreamingResponse,
    openapi_extra={"x-stability": "beta"},
    responses={
        200: {
            "description": (
                "Every entry the filters match, newest first, as a file. "
                "X-Total-Count is the number of entries the file holds."
            ),
            "headers": {
                "X-Total-Count": {
                    "description": "Number of entries in the file",
                    "schema": {"type": "integer"},
                },
                "Content-Disposition": {
                    "description": "attachment; filename=audit-log-<UTC>.<format>",
                    "schema": {"type": "string"},
                },
            },
            "content": {
                "application/json": {
                    "schema": {
                        "type": "array",
                        "items": {"$ref": "#/components/schemas/AuditLogResponse"},
                    }
                },
                "text/csv": {"schema": {"type": "string"}},
            },
        },
        400: {
            "description": "An unknown entity_type or action_type, or dates out of order"
        },
        422: {
            "description": (
                f"More than {audit_export.EXPORT_MAX_ROWS} entries match, or a "
                "parameter is not valid"
            )
        },
    },
)
def export_audit_logs(
    *,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
    export_format: Literal["json", "csv"] = Query(
        "json", alias="format", description="File format: json or csv"
    ),
    user_id: Optional[UUID] = Query(None, description="Filter by user ID"),
    entity_type: Optional[str] = Query(None, description="Filter by entity type"),
    entity_id: Optional[UUID] = Query(None, description="Filter by entity ID"),
    action_type: Optional[str] = Query(None, description="Filter by action type"),
    from_date: Optional[datetime] = Query(
        None, description="Entries at or after this time (ISO format)"
    ),
    to_date: Optional[datetime] = Query(
        None, description="Entries at or before this time (ISO format)"
    ),
) -> Response:
    """
    Download every audit entry the filters match, as CSV or JSON.

    The filters, the access rule and the order (newest first) are the list
    route's. Entries written after the request arrived are not included.
    More than 50,000 matching entries answers 422; the file is never cut
    short. `X-Total-Count` is the number of entries the file holds: compare
    it with the entries you received. Exporting writes no audit entry.
    """
    requested_at = datetime.now(timezone.utc)
    filters = resolve_audit_log_filters(
        current_user,
        user_id=user_id,
        entity_type=entity_type,
        entity_id=entity_id,
        action_type=action_type,
        from_date=from_date,
        to_date=to_date,
    )

    try:
        query = AuditService.filtered_query(
            db,
            user_id=filters.user_id,
            entity_type=filters.entity_type,
            entity_id=filters.entity_id,
            action_type=filters.action_type,
            from_date=filters.from_date,
            to_date=filters.to_date,
        ).filter(AuditLog.timestamp <= requested_at)
        total = query.count()
    except Exception as e:
        raise unexpected_failure(
            e, "Audit log export", AUDIT_EXPORT_FAILED, db=db, logger=logger
        )

    if total > audit_export.EXPORT_MAX_ROWS:
        raise HTTPException(
            status_code=422,
            detail=export_cap_message(total),
        )

    filename = audit_export.export_filename(requested_at, export_format)
    # The whole body is built here, before the response starts: at most
    # EXPORT_MAX_ROWS entries, read EXPORT_BATCH_SIZE at a time.
    body = "".join(_stream_items(query, export_format))
    return Response(
        content=body,
        media_type=audit_export.MEDIA_TYPES[export_format],
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Total-Count": str(total),
        },
    )


@router.get(
    "/entity/{entity_type}/{entity_id}",
    response_model=list[AuditLogResponse],
    summary="Get audit history for a specific entity",
    response_description="Returns audit logs for a specific entity",
)
async def get_entity_audit_history(
    *,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
    entity_type: str = Path(
        ..., description="Type of entity (feature_flag, experiment, etc.)"
    ),
    entity_id: UUID = Path(..., description="ID of the entity"),
    limit: int = Query(
        100, ge=1, le=1000, description="Maximum number of records to return"
    ),
) -> list[AuditLogResponse]:
    """
    Get audit history for a specific entity.

    This endpoint returns the complete audit trail for a specific entity,
    such as a feature flag or experiment. The logs are ordered by timestamp
    with the most recent first.

    Superusers, ADMIN and ANALYST only; other roles receive 403.
    """
    if not can_read_all_audit_logs(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not enough permissions to view entity audit history",
        )

    # Validate entity type
    try:
        entity_type_enum = EntityType(entity_type)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=INVALID_ENTITY_TYPE,
        )

    try:
        # Get audit logs for the entity
        audit_logs = AuditService.get_entity_audit_history(
            db=db,
            entity_type=entity_type_enum,
            entity_id=entity_id,
            limit=limit,
        )

        # Convert to response models
        return [AuditLogResponse.model_validate(log) for log in audit_logs]

    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve entity audit history",
        )


@router.get(
    "/user/{user_id}",
    response_model=list[AuditLogResponse],
    summary="Get audit logs for a specific user",
    response_description="Returns audit logs for a specific user's activity",
)
async def get_user_activity(
    *,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
    user_id: UUID = Path(..., description="ID of the user"),
    from_date: Optional[datetime] = Query(
        None, description="Filter logs from this date"
    ),
    to_date: Optional[datetime] = Query(
        None, description="Filter logs until this date"
    ),
    limit: int = Query(
        100, ge=1, le=1000, description="Maximum number of records to return"
    ),
) -> list[AuditLogResponse]:
    """
    Get audit logs for a specific user's activity.

    This endpoint returns all audit logs for a specific user's actions.
    Every user can view their own activity. Superusers, ADMIN and ANALYST
    can view any user's activity; other roles receive 403 for anyone else.
    """
    if not can_read_all_audit_logs(current_user) and str(current_user.id) != str(
        user_id
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not enough permissions to view this user's activity",
        )

    refuse_unstorable_offset("from_date", from_date)
    refuse_unstorable_offset("to_date", to_date)

    # Validate date range
    if from_date and to_date and to_date <= from_date:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="to_date must be after from_date",
        )

    try:
        # Get user activity
        audit_logs = AuditService.get_user_activity(
            db=db,
            user_id=user_id,
            from_date=from_date,
            to_date=to_date,
            limit=limit,
        )

        # Convert to response models
        return [AuditLogResponse.model_validate(log) for log in audit_logs]

    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve user activity",
        )


@router.get(
    "/stats",
    response_model=AuditStatsResponse,
    summary="Get audit log statistics",
    response_description="Returns statistics about audit logs",
)
async def get_audit_stats(
    *,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
    from_date: Optional[datetime] = Query(
        None, description="Filter logs from this date"
    ),
    to_date: Optional[datetime] = Query(
        None, description="Filter logs until this date"
    ),
) -> AuditStatsResponse:
    """
    Get audit log statistics.

    This endpoint returns statistical information about audit logs,
    including counts by action type, entity type, and most active users.

    Superusers, ADMIN and ANALYST only; other roles receive 403.
    """
    if not can_read_all_audit_logs(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not enough permissions to view audit statistics",
        )

    refuse_unstorable_offset("from_date", from_date)
    refuse_unstorable_offset("to_date", to_date)

    # Validate date range
    if from_date and to_date and to_date <= from_date:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="to_date must be after from_date",
        )

    try:
        # Get audit statistics
        stats = AuditService.get_audit_stats(
            db=db,
            from_date=from_date,
            to_date=to_date,
        )

        return AuditStatsResponse.model_validate(stats)

    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve audit statistics",
        )
