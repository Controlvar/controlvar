"""
Pydantic schemas for notification preferences and delivery log.
"""

from datetime import datetime
from typing import Any, Dict, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backend.app.models.notification import NotificationChannel, NotificationStatus

# ---------------------------------------------------------------------------
# Notification Preferences
# ---------------------------------------------------------------------------


class NotificationPreferenceBase(BaseModel):
    notify_experiment_started: bool = True
    notify_experiment_completed: bool = True
    notify_safety_rollback: bool = True
    notify_rollout_advanced: bool = False
    slack_channel: Optional[str] = Field(None, max_length=255)
    email_override: Optional[str] = Field(None, max_length=255)


class NotificationPreferenceCreate(NotificationPreferenceBase):
    pass


class NotificationPreferenceUpdate(NotificationPreferenceBase):
    notify_experiment_started: Optional[bool] = None
    notify_experiment_completed: Optional[bool] = None
    notify_safety_rollback: Optional[bool] = None
    notify_rollout_advanced: Optional[bool] = None


class NotificationPreferenceResponse(NotificationPreferenceBase):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Delivery Log
# ---------------------------------------------------------------------------


class NotificationDeliveryLogResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    event_type: str
    channel: NotificationChannel
    recipient: str
    subject: Optional[str] = None
    status: NotificationStatus
    error_message: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None
    created_at: datetime


class NotificationDeliveryLogListResponse(BaseModel):
    items: list[NotificationDeliveryLogResponse]
    total: int
    page: int
    limit: int


# ---------------------------------------------------------------------------
# Test notification request
# ---------------------------------------------------------------------------


class TestNotificationRequest(BaseModel):
    channel: NotificationChannel = NotificationChannel.SLACK
    message: str = Field("Test notification from Controlvar", max_length=500)
    recipient: Optional[str] = None
