"""
Safety monitoring API endpoints.

This module provides endpoints for managing safety monitoring and rollback functionality
for feature flags.
"""

from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from backend.app.api.deps import get_current_active_user, get_current_superuser, get_db
from backend.app.api.off_loop_route import OffLoopRoute
from backend.app.models.user import User
from backend.app.schemas.safety import (
    FeatureFlagSafetyConfigResponse,
    FeatureFlagSafetyConfigUpdate,
    RollbackResponse,
    SafetyCheckResponse,
    SafetySettingsResponse,
    SafetySettingsUpdate,
)
from backend.app.services.safety_service import SafetyService

router = APIRouter(route_class=OffLoopRoute)


@router.get("/settings", response_model=SafetySettingsResponse)
async def get_safety_settings(
    *,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> Any:
    """
    Get global safety settings.
    """
    safety_service = SafetyService(db)
    return await safety_service.async_get_safety_settings()


@router.post("/settings", response_model=SafetySettingsResponse)
async def update_safety_settings(
    *,
    db: Session = Depends(get_db),
    settings: SafetySettingsUpdate,
    current_user: User = Depends(get_current_superuser),
) -> Any:
    """
    Update global safety settings.

    Requires superuser privileges.
    """
    safety_service = SafetyService(db)
    return await safety_service.create_or_update_safety_settings(settings)


@router.get(
    "/feature-flags/{feature_flag_id}/config",
    response_model=FeatureFlagSafetyConfigResponse,
)
async def get_feature_flag_safety_config(
    *,
    db: Session = Depends(get_db),
    feature_flag_id: UUID,
    current_user: User = Depends(get_current_active_user),
) -> Any:
    """
    Get safety configuration for a feature flag.
    """
    safety_service = SafetyService(db)
    return await safety_service.async_get_feature_flag_safety_config(feature_flag_id)


@router.post(
    "/feature-flags/{feature_flag_id}/config",
    response_model=FeatureFlagSafetyConfigResponse,
)
async def update_feature_flag_safety_config(
    *,
    db: Session = Depends(get_db),
    feature_flag_id: UUID,
    config: FeatureFlagSafetyConfigUpdate,
    current_user: User = Depends(get_current_superuser),
) -> Any:
    """
    Update safety configuration for a feature flag.

    Requires superuser privileges.
    """
    safety_service = SafetyService(db)
    return await safety_service.create_or_update_feature_flag_safety_config(
        feature_flag_id, config
    )


@router.get(
    "/feature-flags/{feature_flag_id}/check", response_model=SafetyCheckResponse
)
async def check_feature_flag_safety(
    *,
    db: Session = Depends(get_db),
    feature_flag_id: UUID,
    current_user: User = Depends(get_current_active_user),
) -> Any:
    """
    Check the safety status of a feature flag.
    """
    safety_service = SafetyService(db)
    return await safety_service.check_feature_flag_safety(feature_flag_id)


@router.post(
    "/feature-flags/{feature_flag_id}/rollback", response_model=RollbackResponse
)
async def rollback_feature_flag(
    *,
    db: Session = Depends(get_db),
    feature_flag_id: UUID,
    percentage: Optional[int] = Query(0, ge=0, le=100),
    reason: Optional[str] = "Manual rollback",
    current_user: User = Depends(get_current_superuser),
) -> Any:
    """
    Roll back a feature flag to a safe state.

    Requires superuser privileges.
    """
    safety_service = SafetyService(db)
    return await safety_service.async_rollback_feature_flag(
        feature_flag_id=feature_flag_id,
        percentage=percentage,
        reason=reason,
        executed_by_user_id=current_user.id,
        actor=current_user,
    )
