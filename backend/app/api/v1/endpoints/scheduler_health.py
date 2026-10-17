"""
Scheduler health and notification REST endpoints.

Provides:
  GET  /api/v1/scheduler/health               — all schedulers health
  GET  /api/v1/scheduler/health/{name}        — specific scheduler health
  GET  /api/v1/scheduler/{name}/history       — run history (last 20)
  POST /api/v1/scheduler/notify/test          — send a test webhook notification
"""

from datetime import datetime, timezone
from typing import Any, List

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.app.api.deps import get_current_active_user, get_current_superuser, get_db
from backend.app.models.user import User
from backend.app.schemas.scheduler import (
    NotificationEvent,
    SchedulerHealthResponse,
    SchedulerName,
    SchedulerRunRecord,
)
from backend.app.services.notification_service import NotificationService
from backend.app.services.scheduler_health_service import SchedulerHealthService

router = APIRouter()


# ---------------------------------------------------------------------------
# GET /health  — all schedulers
# ---------------------------------------------------------------------------


@router.get("/health", response_model=List[SchedulerHealthResponse])
def get_all_health(
    *,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> Any:
    """
    Return health status for all four background schedulers.

    Accessible by ADMIN and DEVELOPER roles.
    """
    service = SchedulerHealthService()
    return service.get_all_health(db)


# ---------------------------------------------------------------------------
# GET /health/{name}  — specific scheduler
# ---------------------------------------------------------------------------


@router.get("/health/{name}", response_model=SchedulerHealthResponse)
def get_health_by_name(
    name: SchedulerName,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> Any:
    """
    Return health status for a single named scheduler.

    Path parameter ``name`` must be one of: experiment, rollout, metrics, safety.
    """
    service = SchedulerHealthService()
    return service.get_health(db, name)


# ---------------------------------------------------------------------------
# GET /{name}/history  — run history
# ---------------------------------------------------------------------------


@router.get("/{name}/history", response_model=List[SchedulerRunRecord])
def get_run_history(
    name: SchedulerName,
    limit: int = 20,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> Any:
    """
    Return the most recent run records for a named scheduler.

    Default limit is 20; pass ``?limit=N`` to override.
    """
    service = SchedulerHealthService()
    return service.get_run_history(db, name.value, limit=limit)


# ---------------------------------------------------------------------------
# POST /notify/test  — test webhook (ADMIN only)
# ---------------------------------------------------------------------------


@router.post("/notify/test")
def send_test_notification(
    *,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_superuser),
) -> Any:
    """
    Send a test webhook notification to the configured NOTIFICATION_WEBHOOK_URL.

    Requires superuser (ADMIN) privileges.
    """
    notification_service = NotificationService()
    test_event = NotificationEvent(
        event_type="test",
        message="This is a test notification from the Controlvar scheduler.",
        timestamp=datetime.now(timezone.utc).isoformat(),
        metadata={"triggered_by": str(current_user.id)},
    )

    url = notification_service._webhook_url
    if not url:
        return {
            "status": "skipped",
            "message": "NOTIFICATION_WEBHOOK_URL is not configured.",
            "sent": False,
        }

    sent = notification_service.send_webhook(url, test_event)
    return {
        "status": "sent" if sent else "failed",
        "message": (
            "Test notification sent successfully."
            if sent
            else "Test notification failed. Check NOTIFICATION_WEBHOOK_URL and server logs."
        ),
        "sent": sent,
    }
