"""
AuditLogService: writes ComplianceAuditEvents to the database.

Used by API endpoints to log create/update/delete operations.
Events carry configurable retention expiry dates for SOC 2 Type 2 and
ISO 27001 compliance, and are signed through ``hooks.audit_signer``.
The core default writes no signature (the column is nullable); the
compliance module installs the HMAC-SHA256 signer.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from backend.app.core import hooks
from backend.app.core.config import settings
from backend.app.models.compliance_audit_event import (
    AuditAction,
    AuditOutcome,
    ComplianceAuditEvent,
)

logger = logging.getLogger(__name__)

# Fields whose values contain sensitive data and should be redacted before logging.
_SENSITIVE_FIELDS = {
    "password",
    "hashed_password",
    "token",
    "api_key",
    "secret",
    "access_token",
    "refresh_token",
}


def _redact_sensitive(data: Optional[Any]) -> Optional[Any]:
    """Replace sensitive field values with [REDACTED].

    Performs case-insensitive substring matching so that fields like
    'hashed_password', 'ACCESS_TOKEN', etc. are also caught.
    Recurses into nested dicts and lists.
    """
    if data is None:
        return None
    if isinstance(data, dict):
        result = {}
        for k, v in data.items():
            if any(s in k.lower() for s in _SENSITIVE_FIELDS):
                result[k] = "[REDACTED]"
            else:
                result[k] = _redact_sensitive(v)
        return result
    if isinstance(data, list):
        return [_redact_sensitive(item) for item in data]
    return data


def _get_retention_expiry(standard: str = "soc2") -> datetime:
    """Compute retention expiry date based on compliance standard.

    Args:
        standard: "soc2" (default, 12 months) or "iso27001" (24 months).

    Returns:
        UTC datetime marking when this event may be archived/deleted.
    """
    if standard == "iso27001":
        days = settings.AUDIT_RETENTION_DAYS_ISO27001
    else:
        days = settings.AUDIT_RETENTION_DAYS_SOC2
    return datetime.now(timezone.utc) + timedelta(days=days)


class AuditLogService:
    """Service for creating and querying compliance audit events.

    ``log()`` only flushes, and ``get_db`` closes the session without
    committing, so a caller whose change has already been committed must
    commit the record itself -- and roll back if that fails, so the session
    stays usable for the rest of the request.

    Usage in endpoints::

        audit = AuditLogService(db)
        try:
            audit.log(
                action=AuditAction.CREATE,
                resource_type="feature_flag",
                outcome=AuditOutcome.SUCCESS,
                resource_id=str(flag.id),
                actor_id=str(current_user.id),
                new_value={"key": flag.key},
            )
            db.commit()
        except Exception:
            db.rollback()
            logger.warning("Compliance audit logging failed", exc_info=True)
    """

    def __init__(self, db: Session) -> None:
        self._db = db

    def log(
        self,
        action: AuditAction,
        resource_type: str,
        outcome: AuditOutcome,
        resource_id: Optional[Any] = None,
        actor_id: Optional[str] = None,
        actor_ip: Optional[str] = None,
        actor_user_agent: Optional[str] = None,
        session_id: Optional[str] = None,
        request_id: Optional[str] = None,
        old_value: Optional[Dict] = None,
        new_value: Optional[Dict] = None,
        retention_standard: str = "soc2",
    ) -> ComplianceAuditEvent:
        """Create and persist a compliance audit event.

        Sensitive fields in old_value/new_value are automatically redacted.
        The event is HMAC-signed before persisting.
        db.flush() is called so the record is visible within the current
        transaction without requiring an explicit db.commit().

        Args:
            action: The action performed (CREATE, UPDATE, DELETE, LOGIN, …).
            resource_type: Human-readable resource category, e.g. "feature_flag".
            outcome: SUCCESS, FAILURE, or DENIED.
            resource_id: The ID of the affected resource (any type, stringified).
            actor_id: The ID of the user who performed the action.
            actor_ip: The remote IP address of the actor.
            actor_user_agent: The HTTP User-Agent string.
            session_id: Session identifier for correlation.
            request_id: Request identifier for distributed tracing.
            old_value: The state of the resource before the action.
            new_value: The state of the resource after the action.
            retention_standard: "soc2" (default) or "iso27001".

        Returns:
            The persisted ComplianceAuditEvent instance.
        """
        event = ComplianceAuditEvent(
            id=uuid.uuid4(),
            timestamp=datetime.now(timezone.utc),
            actor_id=actor_id,
            actor_ip=actor_ip,
            actor_user_agent=actor_user_agent,
            session_id=session_id,
            request_id=request_id,
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id) if resource_id is not None else None,
            old_value=_redact_sensitive(old_value),
            new_value=_redact_sensitive(new_value),
            outcome=outcome,
            retention_expires_at=_get_retention_expiry(retention_standard),
        )

        # Sign the event for tamper detection. Signing is the compliance
        # module's, installed through the seam; the core default returns
        # None, which the nullable hmac_signature column
        # accepts. Looked up on the module, not bound at import time, so a
        # signer installed after this module loaded is still used.
        event.hmac_signature = hooks.audit_signer.sign(event)

        self._db.add(event)
        self._db.flush()  # Persist within caller's transaction

        return event

    def get_events(
        self,
        resource_type: Optional[str] = None,
        actor_id: Optional[str] = None,
        action: Optional[AuditAction] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        page: int = 1,
        limit: int = 50,
    ) -> Dict:
        """Query audit events with optional filtering and pagination.

        Args:
            resource_type: Filter by resource type (e.g. "feature_flag").
            actor_id: Filter by actor user ID string.
            action: Filter by AuditAction enum value.
            start_time: Only return events at or after this UTC datetime.
            end_time: Only return events at or before this UTC datetime.
            page: 1-indexed page number (default 1).
            limit: Records per page (default 50).

        Returns:
            Dict with keys: items, total, page, limit.
        """
        query = self._db.query(ComplianceAuditEvent)

        if resource_type:
            query = query.filter(ComplianceAuditEvent.resource_type == resource_type)
        if actor_id:
            query = query.filter(ComplianceAuditEvent.actor_id == actor_id)
        if action:
            query = query.filter(ComplianceAuditEvent.action == action)
        if start_time:
            query = query.filter(ComplianceAuditEvent.timestamp >= start_time)
        if end_time:
            query = query.filter(ComplianceAuditEvent.timestamp <= end_time)

        total = query.count()
        items = (
            query.order_by(ComplianceAuditEvent.timestamp.desc())
            .offset((page - 1) * limit)
            .limit(limit)
            .all()
        )

        return {"items": items, "total": total, "page": page, "limit": limit}
