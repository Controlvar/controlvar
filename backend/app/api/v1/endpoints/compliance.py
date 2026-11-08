"""
Compliance audit log API endpoints.

Provides read access to the SOC 2 / ISO 27001 compliance audit trail.
Access is restricted to ADMIN and ANALYST roles.

Endpoints:
  GET /audit-events      — Paginated audit event listing (ADMIN/ANALYST)
  GET /reports/{standard} — On-demand compliance report (ADMIN/ANALYST)
  GET /export             — Full audit export as JSON or CSV (ADMIN only)

CORE / MODULE SPLIT
-------------------
``/audit-events`` is core: it reads ``audit_events_v2`` through the core
``AuditLogService`` and must keep answering in every build
(``backend/tests/smoke/test_wiring.py`` asserts it, and the SOC 2
documentation points at it).

``/reports/{standard}`` and ``/export`` are the compliance module's: their
bodies live in ``compliance_reports.py`` and are reached through the
``core/optional_modules`` seam. The routes stay declared here so the URLs
and their OpenAPI entries exist in every profile; a build without the module
answers HTTP 501 rather than 404, so the refusal is explicit.
"""

import logging
from datetime import datetime
from typing import Any, Dict, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy.orm import Session

from backend.app.api import deps
from backend.app.api.datetime_bounds import refuse_unstorable_offset
from backend.app.api.off_loop_route import OffLoopRoute
from backend.app.core.optional_modules import (
    COMPLIANCE_REPORTING_UNAVAILABLE_DETAIL,
    compliance_export_handler,
    compliance_report_handler,
)
from backend.app.db.session import get_db
from backend.app.models.compliance_audit_event import AuditAction
from backend.app.models.user import User, UserRole
from backend.app.schemas.compliance_audit import (
    ComplianceAuditEventListResponse,
)
from backend.app.services.audit_log_service import AuditLogService

logger = logging.getLogger(__name__)

router = APIRouter(route_class=OffLoopRoute)

# Roles that may read the core compliance audit event listing
_ALLOWED_ROLES = (UserRole.ADMIN, UserRole.ANALYST)


def _require_role(current_user: User, allowed: tuple, detail: str) -> None:
    """The route's own access policy, checked before the seam is consulted.

    Who may call a route is a property of the route, not of the profile: a
    VIEWER asking for the SOC 2 report is refused with the same 403 in every
    build, rather than learning from a 501 which bodies this build lacks.
    """
    if getattr(current_user, "is_superuser", False):
        return
    if getattr(current_user, "role", None) in allowed:
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


@router.get(
    "/audit-events",
    response_model=ComplianceAuditEventListResponse,
    summary="List compliance audit events",
    response_description="Returns a paginated list of compliance audit events",
    responses={
        status.HTTP_403_FORBIDDEN: {
            "description": "Caller does not have ADMIN or ANALYST role",
        },
    },
)
def list_audit_events(
    resource_type: Optional[str] = Query(
        None,
        description="Filter by resource type, e.g. feature_flag or experiment",
    ),
    actor_id: Optional[UUID] = Query(
        None,
        description="Filter by actor (user) UUID",
    ),
    action: Optional[AuditAction] = Query(
        None,
        description="Filter by action (CREATE, UPDATE, DELETE, LOGIN, …)",
    ),
    start_time: Optional[datetime] = Query(
        None,
        description="Only return events at or after this UTC datetime (ISO 8601)",
    ),
    end_time: Optional[datetime] = Query(
        None,
        description="Only return events at or before this UTC datetime (ISO 8601)",
    ),
    page: int = Query(1, ge=1, description="Page number (1-indexed)"),
    limit: int = Query(50, ge=1, le=200, description="Records per page"),
    current_user: User = Depends(deps.get_current_active_user),
    db: Session = Depends(get_db),
) -> ComplianceAuditEventListResponse:
    """List compliance audit events.

    Returns a paginated, optionally filtered list of compliance-grade audit
    events. Each event is HMAC-signed for tamper-evidence.

    **Access control**: Requires ADMIN or ANALYST role. DEVELOPER and VIEWER
    roles will receive HTTP 403.

    **Filtering**: All query parameters are optional and can be combined.

    **Pagination**: Use `page` and `limit`; the response includes `total` so
    callers can compute the number of pages.
    """
    if not hasattr(current_user, "role") or current_user.role not in _ALLOWED_ROLES:
        # Superusers always get access regardless of role field
        if not (hasattr(current_user, "is_superuser") and current_user.is_superuser):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Compliance audit logs require ADMIN or ANALYST role",
            )

    refuse_unstorable_offset("start_time", start_time)
    refuse_unstorable_offset("end_time", end_time)

    service = AuditLogService(db)
    result = service.get_events(
        resource_type=resource_type,
        actor_id=str(actor_id) if actor_id else None,
        action=action,
        start_time=start_time,
        end_time=end_time,
        page=page,
        limit=limit,
    )
    return ComplianceAuditEventListResponse(**result)


# ---------------------------------------------------------------------------
# Report generation — compliance module body (compliance_reports.py)
# ---------------------------------------------------------------------------


@router.get(
    "/reports/{standard}",
    summary="Generate compliance report",
    response_description="Returns a compliance audit summary report",
    responses={
        status.HTTP_400_BAD_REQUEST: {
            "description": "Unknown compliance standard",
        },
        status.HTTP_403_FORBIDDEN: {
            "description": "Caller does not have ADMIN or ANALYST role",
        },
        status.HTTP_501_NOT_IMPLEMENTED: {
            "description": "The compliance module is not installed in this deployment",
        },
    },
)
def generate_compliance_report(
    standard: str,
    start_time: Optional[datetime] = Query(
        None,
        description="Start of reporting period (ISO 8601 UTC). Defaults to standard look-back.",
    ),
    end_time: Optional[datetime] = Query(
        None,
        description="End of reporting period (ISO 8601 UTC). Defaults to now.",
    ),
    current_user: User = Depends(deps.get_current_active_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Generate an on-demand compliance report.

    Supported standards:
    - **soc2**: SOC 2 Type 2 — default look-back is 365 days.
    - **iso27001**: ISO 27001 — default look-back is 730 days.

    **Access control**: Requires ADMIN or ANALYST role.

    **Module**: compliance. Builds without it answer 501.
    """
    _require_role(
        current_user, _ALLOWED_ROLES, "Compliance reports require ADMIN or ANALYST role"
    )
    handler = compliance_report_handler()
    if handler is None:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=COMPLIANCE_REPORTING_UNAVAILABLE_DETAIL,
        )
    refuse_unstorable_offset("start_time", start_time)
    refuse_unstorable_offset("end_time", end_time)
    return handler(
        standard=standard,
        start_time=start_time,
        end_time=end_time,
        current_user=current_user,
        db=db,
    )


# ---------------------------------------------------------------------------
# Audit export — compliance module body (compliance_reports.py)
# ---------------------------------------------------------------------------


@router.get(
    "/export",
    summary="Export audit events",
    response_description="Download audit events as JSON or CSV",
    responses={
        status.HTTP_403_FORBIDDEN: {
            "description": "Caller does not have ADMIN role",
        },
        status.HTTP_501_NOT_IMPLEMENTED: {
            "description": "The compliance module is not installed in this deployment",
        },
    },
)
def export_audit_events(
    format: str = Query(
        "json",
        description="Export format: 'json' or 'csv'",
        pattern="^(json|csv)$",
    ),
    start_time: Optional[datetime] = Query(
        None,
        description="Only export events at or after this UTC datetime (ISO 8601)",
    ),
    end_time: Optional[datetime] = Query(
        None,
        description="Only export events at or before this UTC datetime (ISO 8601)",
    ),
    current_user: User = Depends(deps.get_current_active_user),
    db: Session = Depends(get_db),
) -> Response:
    """Export all audit events for chain-of-custody review.

    **Access control**: Requires ADMIN role only (stricter than report
    generation — this is a full data dump).

    **Module**: compliance. Builds without it answer 501.
    """
    _require_role(current_user, (UserRole.ADMIN,), "Audit export requires ADMIN role")
    handler = compliance_export_handler()
    if handler is None:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=COMPLIANCE_REPORTING_UNAVAILABLE_DETAIL,
        )
    refuse_unstorable_offset("start_time", start_time)
    refuse_unstorable_offset("end_time", end_time)
    return handler(
        format=format,
        start_time=start_time,
        end_time=end_time,
        current_user=current_user,
        db=db,
    )
