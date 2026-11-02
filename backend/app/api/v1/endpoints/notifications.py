"""
Notification preferences and delivery log API endpoints.

Provides endpoints for:
- Managing per-user notification preferences
- Admin-level listing of all user preferences
- Viewing notification delivery log with filtering
- Sending test notifications to verify channel connectivity
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from backend.app.api import deps
from backend.app.api.enum_filters import enum_filter
from backend.app.core.logger import unexpected_failure
from backend.app.models.notification import (
    NotificationDeliveryLog,
    NotificationPreference,
    NotificationStatus,
)
from backend.app.models.user import User, UserRole
from backend.app.schemas.notification import (
    NotificationDeliveryLogListResponse,
    NotificationPreferenceResponse,
    NotificationPreferenceUpdate,
    TestNotificationRequest,
)
from backend.app.schemas.scheduler import NotificationEvent
from backend.app.services.notification_service import NotificationService

router = APIRouter()
logger = logging.getLogger(__name__)

#: The delivery log's ``status`` filter values, as its entries spell them (#955).
DELIVERY_STATUS_SPELLINGS = ("sent", "failed", "skipped")

DELIVERY_STATUS_DESCRIPTION = (
    "Only entries with this delivery status: `sent`, `failed` or `skipped`, in "
    "any case. Any other value answers 422; an empty value means no filter."
)


@router.get("/preferences", response_model=NotificationPreferenceResponse)
def get_my_preferences(
    current_user: User = Depends(deps.get_current_active_user),
    db: Session = Depends(deps.get_db),
):
    """Get current user's notification preferences. Creates defaults if none exist."""
    prefs = db.query(NotificationPreference).filter_by(user_id=current_user.id).first()
    if not prefs:
        prefs = NotificationPreference(user_id=current_user.id)
        db.add(prefs)
        db.commit()
        db.refresh(prefs)
    return prefs


@router.put("/preferences", response_model=NotificationPreferenceResponse)
def update_my_preferences(
    update: NotificationPreferenceUpdate,
    current_user: User = Depends(deps.get_current_active_user),
    db: Session = Depends(deps.get_db),
):
    """Update current user's notification preferences."""
    prefs = db.query(NotificationPreference).filter_by(user_id=current_user.id).first()
    if not prefs:
        prefs = NotificationPreference(user_id=current_user.id)
        db.add(prefs)
    update_data = update.model_dump(exclude_none=True)
    for field, value in update_data.items():
        setattr(prefs, field, value)
    db.commit()
    db.refresh(prefs)
    return prefs


@router.get("/admin/preferences", response_model=list[NotificationPreferenceResponse])
def list_all_preferences(
    current_user: User = Depends(deps.get_current_active_user),
    db: Session = Depends(deps.get_db),
):
    """List all users' notification preferences. ADMIN only."""
    if current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Admin access required")
    return db.query(NotificationPreference).all()


@router.get("/delivery-log", response_model=NotificationDeliveryLogListResponse)
def get_delivery_log(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    event_type: Optional[str] = None,
    status: Optional[str] = Query(None, description=DELIVERY_STATUS_DESCRIPTION),
    current_user: User = Depends(deps.get_current_active_user),
    db: Session = Depends(deps.get_db),
):
    """List notification delivery log entries. ADMIN only."""
    if current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Admin access required")

    # After the permission check (#955); the member goes to the query.
    status_member = enum_filter(
        status, NotificationStatus, "status", DELIVERY_STATUS_SPELLINGS
    )

    query = db.query(NotificationDeliveryLog)
    if event_type:
        query = query.filter(NotificationDeliveryLog.event_type == event_type)
    if status_member is not None:
        query = query.filter(NotificationDeliveryLog.status == status_member)

    total = query.count()
    offset = (page - 1) * limit
    items = (
        query.order_by(NotificationDeliveryLog.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    return NotificationDeliveryLogListResponse(
        items=items, total=total, page=page, limit=limit
    )


@router.post("/test")
def send_test_notification(
    request: TestNotificationRequest,
    current_user: User = Depends(deps.get_current_active_user),
):
    """Send a test notification. DEVELOPER+ role required."""
    if current_user.role not in (UserRole.ADMIN, UserRole.DEVELOPER):
        raise HTTPException(status_code=403, detail="Developer access required")

    try:
        svc = NotificationService()

        if request.channel.value == "slack":
            result = svc._slack.send_generic_alert(
                title="Test Notification",
                message=request.message,
                severity="info",
                channel=request.recipient,
            )
        elif request.channel.value == "email":
            result = svc._email.send_safety_rollback_email(
                flag_name="test-flag",
                error_rate=0.0,
                reason=request.message,
                recipients=[request.recipient] if request.recipient else [],
            )
        else:
            event = NotificationEvent(
                event_type="test",
                message=request.message,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
            result = svc.send_webhook(svc._webhook_url or "http://localhost", event)

        return {
            "success": result,
            "channel": request.channel,
            "message": request.message,
        }

    except Exception as exc:
        raise unexpected_failure(
            exc,
            "Test notification",
            "Could not send the test notification",
            logger=logger,
        )
