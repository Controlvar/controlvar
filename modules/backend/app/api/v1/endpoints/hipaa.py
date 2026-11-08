"""
EP-050: HIPAA Compliance API endpoints.

All endpoints require ADMIN role unless stated otherwise.

Prefix: /api/v1/hipaa

Endpoints:
  POST   /baa                  — Create BAA configuration (ADMIN)
  GET    /baa                  — List BAA configurations (ADMIN)
  GET    /baa/{baa_id}         — Get specific BAA (ADMIN)
  DELETE /baa/{baa_id}         — Deactivate BAA (soft delete) (ADMIN)
  GET    /audit-logs           — Paginated PHI audit log (ADMIN)
  POST   /audit-logs           — Manually log PHI access (ADMIN)
  GET    /report               — HIPAA compliance report (ADMIN)
  POST   /encrypt              — Encrypt a PHI value (ADMIN/DEVELOPER)
  POST   /decrypt              — Decrypt a PHI value (ADMIN)
  GET    /data-residency       — Current data residency config (ADMIN)
  GET    /status               — HIPAA readiness status (ADMIN)
"""

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from backend.app.api import deps
from backend.app.api.off_loop_route import OffLoopRoute
from backend.app.db.session import get_db
from backend.app.models.user import User, UserRole
from modules.backend.app.schemas.hipaa import (
    BAAConfigCreate,
    BAAConfigResponse,
    DataResidencyResponse,
    HIPAAReportResponse,
    HIPAAStatusResponse,
    PHIAuditLogCreate,
    PHIAuditLogListResponse,
    PHIAuditLogResponse,
    PHIDecryptRequest,
    PHIDecryptResponse,
    PHIEncryptRequest,
    PHIEncryptResponse,
)
from modules.backend.app.services.hipaa_service import HIPAAService

logger = logging.getLogger(__name__)

router = APIRouter(route_class=OffLoopRoute)

# Roles allowed for HIPAA admin operations
_ADMIN_ROLES = (UserRole.ADMIN,)
_ADMIN_DEVELOPER_ROLES = (UserRole.ADMIN, UserRole.DEVELOPER)


def _require_admin(current_user: User) -> None:
    """Raise 403 if the current user does not have ADMIN role."""
    is_superuser = getattr(current_user, "is_superuser", False)
    is_admin = getattr(current_user, "role", None) == UserRole.ADMIN
    if not is_superuser and not is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="HIPAA endpoints require ADMIN role",
        )


def _require_admin_or_developer(current_user: User) -> None:
    """Raise 403 if the current user does not have ADMIN or DEVELOPER role."""
    is_superuser = getattr(current_user, "is_superuser", False)
    role = getattr(current_user, "role", None)
    if not is_superuser and role not in _ADMIN_DEVELOPER_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This endpoint requires ADMIN or DEVELOPER role",
        )


# ---------------------------------------------------------------------------
# BAA Configuration endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/baa",
    response_model=BAAConfigResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a BAA configuration",
)
def create_baa(
    payload: BAAConfigCreate,
    current_user: User = Depends(deps.get_current_active_user),
    db: Session = Depends(get_db),
) -> BAAConfigResponse:
    """
    Create a new Business Associate Agreement configuration.

    **Access**: ADMIN only.
    """
    _require_admin(current_user)
    svc = HIPAAService()
    baa = svc.create_baa(
        db=db,
        data=payload.model_dump(),
        created_by=current_user.id,
    )
    return BAAConfigResponse.model_validate(baa)


@router.get(
    "/baa",
    response_model=List[BAAConfigResponse],
    summary="List BAA configurations",
)
def list_baas(
    active_only: bool = Query(True, description="Return only active BAAs"),
    current_user: User = Depends(deps.get_current_active_user),
    db: Session = Depends(get_db),
) -> List[BAAConfigResponse]:
    """
    List Business Associate Agreement configurations.

    **Access**: ADMIN only.
    """
    _require_admin(current_user)
    svc = HIPAAService()
    baas = svc.get_baa_configs(db=db, active_only=active_only)
    return [BAAConfigResponse.model_validate(b) for b in baas]


@router.get(
    "/baa/{baa_id}",
    response_model=BAAConfigResponse,
    summary="Get a specific BAA configuration",
)
def get_baa(
    baa_id: uuid.UUID,
    current_user: User = Depends(deps.get_current_active_user),
    db: Session = Depends(get_db),
) -> BAAConfigResponse:
    """
    Retrieve a specific Business Associate Agreement by ID.

    **Access**: ADMIN only.
    """
    _require_admin(current_user)
    svc = HIPAAService()
    baa = svc.get_baa_by_id(db=db, baa_id=baa_id)
    if baa is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"BAA configuration {baa_id} not found",
        )
    return BAAConfigResponse.model_validate(baa)


@router.delete(
    "/baa/{baa_id}",
    status_code=status.HTTP_200_OK,
    summary="Deactivate a BAA configuration",
)
def deactivate_baa(
    baa_id: uuid.UUID,
    current_user: User = Depends(deps.get_current_active_user),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """
    Soft-delete a BAA configuration by marking it inactive.

    **Access**: ADMIN only.
    """
    _require_admin(current_user)
    svc = HIPAAService()
    baa = svc.deactivate_baa(db=db, baa_id=baa_id)
    if baa is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"BAA configuration {baa_id} not found",
        )
    return {"message": f"BAA {baa_id} deactivated successfully", "baa_id": str(baa_id)}


# ---------------------------------------------------------------------------
# PHI Audit Log endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/audit-logs",
    response_model=PHIAuditLogListResponse,
    summary="List PHI audit log entries",
)
def list_audit_logs(
    start_date: Optional[datetime] = Query(
        None, description="Filter from this datetime (UTC)"
    ),
    end_date: Optional[datetime] = Query(
        None, description="Filter up to this datetime (UTC)"
    ),
    resource_type: Optional[str] = Query(
        None, description="Filter by resource type: experiment, feature_flag, user_data"
    ),
    user_id: Optional[uuid.UUID] = Query(None, description="Filter by user UUID"),
    page: int = Query(1, ge=1, description="Page number (1-indexed)"),
    page_size: int = Query(50, ge=1, le=200, description="Records per page"),
    current_user: User = Depends(deps.get_current_active_user),
    db: Session = Depends(get_db),
) -> PHIAuditLogListResponse:
    """
    Retrieve paginated PHI access audit logs.

    **Access**: ADMIN only.
    """
    _require_admin(current_user)
    svc = HIPAAService()
    result = svc.get_phi_audit_logs(
        db=db,
        start_date=start_date,
        end_date=end_date,
        resource_type=resource_type,
        user_id=user_id,
        page=page,
        page_size=page_size,
    )
    items = [PHIAuditLogResponse.model_validate(item) for item in result["items"]]
    return PHIAuditLogListResponse(
        items=items,
        total=result["total"],
        page=result["page"],
        page_size=result["page_size"],
    )


@router.post(
    "/audit-logs",
    response_model=PHIAuditLogResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Manually log a PHI access event",
)
def create_audit_log(
    payload: PHIAuditLogCreate,
    request: Request,
    current_user: User = Depends(deps.get_current_active_user),
    db: Session = Depends(get_db),
) -> PHIAuditLogResponse:
    """
    Manually log a PHI access event (for external or batch systems).

    **Access**: ADMIN only.
    """
    _require_admin(current_user)
    svc = HIPAAService()
    log_entry = svc.log_phi_access(
        db=db,
        user_id=payload.user_id,
        resource_type=payload.resource_type,
        resource_id=payload.resource_id,
        action=payload.action,
        phi_fields=payload.phi_fields_accessed,
        purpose=payload.purpose,
        request=request,
    )
    return PHIAuditLogResponse.model_validate(log_entry)


# ---------------------------------------------------------------------------
# HIPAA Report endpoint
# ---------------------------------------------------------------------------


@router.get(
    "/report",
    response_model=HIPAAReportResponse,
    summary="Generate HIPAA compliance report",
)
def get_hipaa_report(
    start_date: Optional[datetime] = Query(
        None, description="Report period start (UTC)"
    ),
    end_date: Optional[datetime] = Query(None, description="Report period end (UTC)"),
    current_user: User = Depends(deps.get_current_active_user),
    db: Session = Depends(get_db),
) -> HIPAAReportResponse:
    """
    Generate a HIPAA compliance summary report.

    Includes access counts by purpose and resource type, unique user counts,
    potential violations, and BAA coverage summary.

    **Access**: ADMIN only.
    """
    _require_admin(current_user)
    svc = HIPAAService()
    report = svc.generate_hipaa_report(db=db, start_date=start_date, end_date=end_date)
    return HIPAAReportResponse(**report)


# ---------------------------------------------------------------------------
# PHI Encrypt / Decrypt endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/encrypt",
    response_model=PHIEncryptResponse,
    summary="Encrypt a PHI field value",
)
def encrypt_phi(
    payload: PHIEncryptRequest,
    current_user: User = Depends(deps.get_current_active_user),
    db: Session = Depends(get_db),
) -> PHIEncryptResponse:
    """
    Encrypt a PHI value using the configured PHI encryption key.

    Useful for testing encryption configuration and for batch encryption of
    PHI fields before storage.

    **Access**: ADMIN or DEVELOPER.
    """
    _require_admin_or_developer(current_user)
    svc = HIPAAService()
    try:
        ciphertext = svc.encrypt_phi_field(payload.value)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"PHI encryption not configured: {exc}",
        )
    return PHIEncryptResponse(field=payload.field, ciphertext=ciphertext)


@router.post(
    "/decrypt",
    response_model=PHIDecryptResponse,
    summary="Decrypt a PHI field value",
)
def decrypt_phi(
    payload: PHIDecryptRequest,
    request: Request,
    current_user: User = Depends(deps.get_current_active_user),
    db: Session = Depends(get_db),
) -> PHIDecryptResponse:
    """
    Decrypt a PHI ciphertext and log the access.

    Every decrypt operation is audit-logged per HIPAA requirements.

    **Access**: ADMIN only.
    """
    _require_admin(current_user)
    svc = HIPAAService()
    try:
        plaintext = svc.decrypt_phi_field(payload.ciphertext)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"PHI encryption not configured: {exc}",
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Decryption failed: {exc}",
        )

    # Audit log the decryption event
    try:
        svc.log_phi_access(
            db=db,
            user_id=current_user.id,
            resource_type="user_data",
            resource_id=current_user.id,
            action="READ",
            phi_fields=[payload.field],
            purpose="operations",
            request=request,
        )
    except Exception as audit_exc:
        logger.error(
            "Failed to audit-log PHI decryption (%s)", type(audit_exc).__name__
        )

    return PHIDecryptResponse(field=payload.field, plaintext=plaintext)


# ---------------------------------------------------------------------------
# Data Residency endpoint
# ---------------------------------------------------------------------------


@router.get(
    "/data-residency",
    response_model=DataResidencyResponse,
    summary="Get data residency configuration",
)
def get_data_residency(
    current_user: User = Depends(deps.get_current_active_user),
) -> DataResidencyResponse:
    """
    Return the current HIPAA data residency configuration.

    Shows which AWS regions are permitted for PHI storage.

    **Access**: ADMIN only.
    """
    _require_admin(current_user)
    from backend.app.core.config import settings
    from modules.backend.app.settings import settings as modules_settings

    return DataResidencyResponse(
        allowed_regions=modules_settings.HIPAA_ALLOWED_REGIONS,
        current_region=settings.AWS_REGION,
        hipaa_enabled=modules_settings.HIPAA_ENABLED,
    )


# ---------------------------------------------------------------------------
# HIPAA Status endpoint
# ---------------------------------------------------------------------------


@router.get(
    "/status",
    response_model=HIPAAStatusResponse,
    summary="HIPAA readiness status",
)
def get_hipaa_status(
    current_user: User = Depends(deps.get_current_active_user),
    db: Session = Depends(get_db),
) -> HIPAAStatusResponse:
    """
    Return a HIPAA readiness checklist.

    Reports whether all required HIPAA controls are configured:
    - Active BAA on file
    - PHI encryption key configured
    - Audit logging enabled
    - Data residency configured

    **Access**: ADMIN only.
    """
    _require_admin(current_user)
    svc = HIPAAService()
    status_data = svc.get_hipaa_status(db=db)
    return HIPAAStatusResponse(**status_data)
