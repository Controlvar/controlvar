"""
Audit Service for comprehensive logging of user actions.

This service provides functionality to log all user actions and changes
in the experimentation platform, creating a comprehensive audit trail
for compliance, debugging, and analysis purposes.
"""

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID, uuid4

from sqlalchemy import and_, desc, func
from sqlalchemy.orm import Session

from backend.app.core.metrics import audit_write_failures_total
from backend.app.models.audit_log import ActionType, AuditLog, EntityType
from backend.app.models.user import UserRole

logger = logging.getLogger(__name__)


def _as_audit_text(value: Any) -> Optional[str]:
    """Render an audit value for the Text columns.

    Enum members become their ``.value``, dicts/lists are JSON-encoded, ``None``
    stays ``None`` and everything else goes through ``str()``.
    """
    if value is None:
        return None
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, default=str, sort_keys=True)
    return str(value)


def actor_email(user_email: Optional[str], username: Optional[str] = None) -> str:
    """The ``user_email`` an audit row records: the email, else the username,
    else ``"system"``. The column is NOT NULL and some accounts have no email.
    """
    return user_email or username or "system"


# ---------------------------------------------------------------------------
# The writer (#221): ``AuditService.record`` and ``record_after_commit``.
# ---------------------------------------------------------------------------

#: Longest ``reason`` stored: what ``ToggleRequest`` accepts. Longer text is cut.
AUDIT_REASON_MAX = 1000
#: ``entity_name`` is String(255).
AUDIT_NAME_MAX = 255
#: Longest JSON stored in ``old_value`` or ``new_value``. A longer value is
#: replaced by ``{"truncated": true, "changed_fields": [...]}``.
AUDIT_VALUE_MAX = 4000

#: Fields whose values an entry records, per entity. Every name is a column of
#: the entity's model (``test_audit_values.py`` checks it), so a field a base
#: does not have cannot be listed.
AUDIT_VALUE_FIELDS: Dict[EntityType, Tuple[str, ...]] = {
    EntityType.FEATURE_FLAG: (
        "key",
        "name",
        "status",
        "rollout_percentage",
        "value_type",
    ),
    EntityType.EXPERIMENT: (
        "key",
        "name",
        "status",
        "start_date",
        "end_date",
        "correction_method",
        "confidence_level",
    ),
    EntityType.USER: ("username", "role", "is_active", "is_superuser"),
    EntityType.API_KEY: ("name", "scopes", "expires_at", "user_id"),
    EntityType.HOLDOUT: (
        "name",
        "holdout_percentage",
        "is_active",
        "activated_at",
        "deactivated_at",
    ),
    EntityType.MUTUAL_EXCLUSION_GROUP: ("name", "traffic_allocation", "status"),
    EntityType.SEGMENT: ("name", "status"),
}

#: Fields an update records by name only, under ``changed_fields``: the entry
#: says that they changed, never what they hold. A flag's ``on_value`` and
#: ``off_value`` are user-authored content of up to 8 KiB, like its rules: a
#: value recorded beside them could push the whole entry over
#: ``AUDIT_VALUE_MAX`` and lose the ``rollout_percentage`` change with it
#: (Plan F ruling 3).
AUDIT_NAME_ONLY_FIELDS: Dict[EntityType, Tuple[str, ...]] = {
    EntityType.FEATURE_FLAG: (
        "description",
        "targeting_rules",
        "variants",
        "on_value",
        "off_value",
    ),
    EntityType.EXPERIMENT: ("description", "hypothesis", "targeting_rules", "metrics"),
    EntityType.USER: ("email",),
    EntityType.API_KEY: (),
    EntityType.HOLDOUT: ("description",),
    EntityType.MUTUAL_EXCLUSION_GROUP: ("description",),
    EntityType.SEGMENT: ("description", "rules"),
}

#: Every ``action_type`` something in this profile writes. The inventories in
#: ``backend/tests/smoke/test_audit_inventory.py`` (routes and non-route
#: sites) pin it to the union of their ``Audited`` sets, and the dashboard's
#: list (``frontend/src/components/admin/audit/action-types.json``) is
#: compared with it.
WRITTEN_ACTION_TYPES: frozenset = frozenset(
    {
        ActionType.TOGGLE_ENABLE,
        ActionType.TOGGLE_DISABLE,
        ActionType.FEATURE_FLAG_CREATE,
        ActionType.FEATURE_FLAG_UPDATE,
        ActionType.FEATURE_FLAG_DELETE,
        ActionType.FEATURE_FLAG_ACTIVATE,
        ActionType.FEATURE_FLAG_DEACTIVATE,
        ActionType.EXPERIMENT_CREATE,
        ActionType.EXPERIMENT_UPDATE,
        ActionType.EXPERIMENT_DELETE,
        ActionType.EXPERIMENT_START,
        ActionType.EXPERIMENT_PAUSE,
        ActionType.EXPERIMENT_COMPLETE,
        ActionType.USER_CREATE,
        ActionType.USER_DELETE,
        ActionType.USER_LOGIN,
        ActionType.USER_ACTIVATE,
        ActionType.USER_DEACTIVATE,
        ActionType.ROLE_ASSIGN,
        ActionType.API_KEY_CREATE,
        ActionType.API_KEY_REVOKE,
        ActionType.HOLDOUT_CREATE,
        ActionType.HOLDOUT_UPDATE,
        ActionType.HOLDOUT_ACTIVATE,
        ActionType.HOLDOUT_DEACTIVATE,
        ActionType.MUTUAL_EXCLUSION_GROUP_CREATE,
        ActionType.MUTUAL_EXCLUSION_GROUP_UPDATE,
        ActionType.MUTUAL_EXCLUSION_GROUP_ARCHIVE,
        ActionType.SEGMENT_CREATE,
        ActionType.SEGMENT_UPDATE,
        ActionType.SEGMENT_ARCHIVE,
        ActionType.SAFETY_ROLLBACK,
    }
)

#: Action types only the modules write. In a core build the dashboard's list
#: may carry these and nothing else beyond ``WRITTEN_ACTION_TYPES``. The
#: modules' inventories (``modules/backend/tests/smoke/test_audit_inventory_modules.py``)
#: pin it to what the modules write that core does not.
MODULES_ONLY_ACTION_TYPES: frozenset = frozenset({ActionType.ROLE_UNASSIGN})


@dataclass(frozen=True)
class AuditActor:
    """Who an entry is recorded against: ``user_id`` and the ``user_email`` text."""

    id: Optional[UUID]
    email: Optional[str]
    username: Optional[str] = None

    @classmethod
    def of(cls, user: Any) -> "AuditActor":
        """The actor for a ``User``, read now (before any commit expires it)."""
        if isinstance(user, AuditActor):
            return user
        return cls(
            id=getattr(user, "id", None),
            email=getattr(user, "email", None),
            username=getattr(user, "username", None),
        )


# ---------------------------------------------------------------------------
# System actors (#221): changes the platform makes on its own. Each has no
# ``user_id`` and one reserved ``user_email``.
# ---------------------------------------------------------------------------

SYSTEM_EXPERIMENT_SCHEDULER = AuditActor(id=None, email="system:experiment-scheduler")
SYSTEM_ROLLOUT_SCHEDULER = AuditActor(id=None, email="system:rollout-scheduler")
SYSTEM_SAFETY_MONITOR = AuditActor(id=None, email="system:safety-monitor")
SYSTEM_COGNITO_SYNC = AuditActor(id=None, email="system:cognito-sync")
#: SSO sign-in changing an existing account's role from its mapped groups.
#: Written by the modules; reserved here so that every profile reads it the
#: same way.
SYSTEM_SSO_SYNC = AuditActor(id=None, email="system:sso-sync")

#: The reserved ``user_email`` values of the system actors.
SYSTEM_ACTOR_EMAILS: frozenset = frozenset(
    actor.email
    for actor in (
        SYSTEM_EXPERIMENT_SCHEDULER,
        SYSTEM_ROLLOUT_SCHEDULER,
        SYSTEM_SAFETY_MONITOR,
        SYSTEM_COGNITO_SYNC,
        SYSTEM_SSO_SYNC,
    )
)


def is_system_actor(user_id: Optional[UUID], user_email: Optional[str]) -> bool:
    """Whether an entry was made by the platform on its own.

    Both must hold: no ``user_id`` and a reserved ``user_email``. A deleted
    user's entries also have no ``user_id`` (the key is set to NULL), but they
    keep the user's own email, so they are never taken for automatic ones.
    """
    return user_id is None and user_email in SYSTEM_ACTOR_EMAILS


def _plain(value: Any) -> Any:
    """A value as an entry stores it: enum members by name (roles) or value."""
    if isinstance(value, UserRole):
        return value.name
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _read(source: Any, field: str) -> Any:
    if isinstance(source, dict):
        return source.get(field)
    return getattr(source, field, None)


def _fingerprint(value: Any) -> str:
    """Comparable text for a field recorded by name only (never stored)."""
    return json.dumps(value, default=str, sort_keys=True)


def audit_snapshot(entity_type: EntityType, source: Any) -> Dict[str, Any]:
    """The allow-listed fields of ``source`` (an ORM row or a dict), now.

    Value fields are kept as values; name-only fields are kept as a
    fingerprint, so a later ``audit_changes`` can tell that they changed.
    Take it before the change, while the row still holds the old values.
    """
    snap: Dict[str, Any] = {
        field: _plain(_read(source, field)) for field in AUDIT_VALUE_FIELDS[entity_type]
    }
    for field in AUDIT_NAME_ONLY_FIELDS[entity_type]:
        snap["#" + field] = _fingerprint(_read(source, field))
    return snap


def audit_identity(snapshot: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The value fields of a snapshot: what a create or a delete records."""
    if snapshot is None:
        return None
    return {k: v for k, v in snapshot.items() if not k.startswith("#")}


def audit_changes(
    before: Dict[str, Any], after: Dict[str, Any]
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """(old, new) for an update: only the fields that changed.

    A changed name-only field is listed in ``new["changed_fields"]`` without
    its contents. Both are None when nothing allow-listed changed.
    """
    old: Dict[str, Any] = {}
    new: Dict[str, Any] = {}
    changed_fields: List[str] = []
    for key in sorted(set(before) | set(after)):
        if before.get(key) == after.get(key):
            continue
        if key.startswith("#"):
            changed_fields.append(key[1:])
        else:
            old[key] = before.get(key)
            new[key] = after.get(key)
    if changed_fields:
        new["changed_fields"] = changed_fields
    return (old or None), (new or None)


def role_value(user: Any) -> Dict[str, Any]:
    """What ``role_assign`` records before and after, from every writer."""
    return {
        "role": _plain(getattr(user, "role", None)),
        "is_superuser": bool(getattr(user, "is_superuser", False)),
    }


def _value_text(value: Any) -> Optional[str]:
    """An old/new value as stored. A plain string is stored as it is."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, default=str, sort_keys=True)
    if len(text) <= AUDIT_VALUE_MAX:
        return text
    fields = sorted(value) if isinstance(value, dict) else []
    return json.dumps({"truncated": True, "changed_fields": fields}, sort_keys=True)


def _cut(text: Any, limit: int) -> Any:
    return text[:limit] if isinstance(text, str) else text


class AuditService:
    """Service for managing audit logs."""

    @staticmethod
    async def log_toggle_operation(
        db: Session,
        user_id: UUID,
        user_email: str,
        action_type: str,
        entity_id: UUID,
        entity_name: str,
        old_value: str,
        new_value: str,
        reason: Optional[str] = None,
        username: Optional[str] = None,
    ) -> UUID:
        """
        Log a feature flag toggle operation asynchronously.

        Args:
            db: Database session
            user_id: ID of the user performing the action
            user_email: Email of the user performing the action
            action_type: Type of action (e.g., "toggle_enable", "toggle_disable")
            entity_id: ID of the entity being modified
            entity_name: Name of the entity being modified
            old_value: Previous value/status
            new_value: New value/status
            reason: Optional reason for the action
            username: Recorded instead when ``user_email`` is empty

        Returns:
            UUID: ID of the created audit log entry

        Raises:
            Exception: If logging fails (after rolling the session back)
        """
        try:
            # Create audit log entry. old/new values are Text columns; callers
            # pass enum members (FeatureFlagStatus), dicts or plain strings, so
            # normalise here instead of failing the whole toggle at INSERT time.
            audit_log = AuditLog(
                user_id=user_id,
                user_email=actor_email(user_email, username),
                action_type=action_type,
                entity_type=EntityType.FEATURE_FLAG.value,
                entity_id=entity_id,
                entity_name=entity_name,
                old_value=_as_audit_text(old_value),
                new_value=_as_audit_text(new_value),
                reason=reason,
                timestamp=datetime.now(timezone.utc),
            )

            db.add(audit_log)
            db.commit()
            db.refresh(audit_log)

            logger.info(
                f"Audit log created: {action_type} on {entity_name} "
                f"by {user_email} ({old_value} -> {new_value})"
            )

            return audit_log.id

        except Exception as e:
            db.rollback()
            # Only the exception type is logged.
            logger.error("Failed to create audit log (%s)", type(e).__name__)
            raise

    @staticmethod
    def record(
        db: Session,
        *,
        actor: Any,
        action: ActionType,
        entity_type: EntityType,
        entity_id: UUID,
        entity_name: Optional[str],
        before: Any = None,
        after: Any = None,
        reason: Optional[str] = None,
    ) -> AuditLog:
        """Add one entry to ``db``'s transaction (or savepoint); never commits.

        The entry is written by whatever commits the caller's change, so the
        two are kept or lost together. ``actor`` is a ``User`` or an
        ``AuditActor``; ``user_email`` is its email, else its username, else
        ``"system"``. ``reason`` is cut to ``AUDIT_REASON_MAX`` characters,
        ``entity_name`` to ``AUDIT_NAME_MAX``, and a value longer than
        ``AUDIT_VALUE_MAX`` is replaced by the names of its fields.
        """
        who = AuditActor.of(actor)
        entry = AuditLog(
            id=uuid4(),
            user_id=who.id,
            user_email=actor_email(who.email, who.username),
            action_type=ActionType(action).value,
            entity_type=EntityType(entity_type).value,
            entity_id=entity_id,
            entity_name=_cut(entity_name, AUDIT_NAME_MAX),
            old_value=_value_text(before),
            new_value=_value_text(after),
            reason=_cut(reason, AUDIT_REASON_MAX),
            timestamp=datetime.now(timezone.utc),
        )
        db.add(entry)
        return entry

    @staticmethod
    def record_after_commit(db: Session, **entry: Any) -> Optional[UUID]:
        """Write one entry for a change the caller has already committed.

        Takes ``record``'s arguments, adds the entry and commits it. If that
        fails, the session is rolled back, one ERROR line naming the action,
        the entity and the exception type (no values, no exception text) is
        logged, ``audit_write_failures_total`` goes up by one, and None is
        returned: the committed change stands and the session is usable again.
        """
        try:
            row = AuditService.record(db, **entry)
            db.commit()
        except Exception as e:
            AuditService._write_failed(
                db,
                entry.get("action"),
                entry.get("entity_type"),
                entry.get("entity_id"),
                e,
            )
            return None
        AuditService._written(row.action_type, row.entity_type, row.entity_id)
        return row.id

    @staticmethod
    def record_in_savepoint(db: Session, **entry: Any) -> Optional[AuditLog]:
        """Write one entry inside a savepoint of the caller's open transaction.

        For a change the caller has made but not committed, when a failed
        entry must not cost the change. Takes ``record``'s arguments.

        The caller's pending changes are flushed first, outside the
        savepoint: if that flush fails it is the caller's own failure and
        is raised. The entry is then added and flushed in a savepoint. If
        that fails, only the savepoint is rolled back, one ERROR line naming
        the action, the entity and the exception type (no values, no
        exception text) is logged, ``audit_write_failures_total`` goes up by
        one and None is returned. The caller's transaction is left open with
        its change in it; the caller commits it as before.

        Never use ``record_after_commit`` inside an open transaction: it
        rolls back the whole session when the entry fails.
        """
        db.flush()
        savepoint = db.begin_nested()
        try:
            row = AuditService.record(db, **entry)
            db.flush()
            savepoint.commit()
        except Exception as e:
            try:
                savepoint.rollback()
            except Exception:
                logger.warning("Rollback of a failed audit savepoint also failed")
            AuditService._log_failure(
                entry.get("action"), entry.get("entity_type"), entry.get("entity_id"), e
            )
            return None
        AuditService._written(row.action_type, row.entity_type, row.entity_id)
        return row

    @staticmethod
    def _written(action: Any, entity_type: Any, entity_id: Any) -> None:
        # No values, no names: only what identifies the entry.
        logger.info(
            "Audit log created: %s on %s %s",
            getattr(action, "value", action),
            getattr(entity_type, "value", entity_type),
            entity_id,
        )

    @staticmethod
    def _write_failed(
        db: Session, action: Any, entity_type: Any, entity_id: Any, error: Exception
    ) -> None:
        try:
            db.rollback()
        except Exception:
            logger.warning("Rollback after a failed audit write also failed")
        AuditService._log_failure(action, entity_type, entity_id, error)

    @staticmethod
    def _log_failure(
        action: Any, entity_type: Any, entity_id: Any, error: Exception
    ) -> None:
        audit_write_failures_total.inc()
        # Only the exception type is logged: its text can carry the row.
        logger.error(
            "Failed to create audit log for %s on %s %s (%s)",
            getattr(action, "value", action),
            getattr(entity_type, "value", entity_type),
            entity_id,
            type(error).__name__,
        )

    @staticmethod
    async def log_action(
        db: Session,
        user_id: Optional[UUID],
        user_email: str,
        action_type: ActionType,
        entity_type: EntityType,
        entity_id: UUID,
        entity_name: str,
        old_value: Optional[str] = None,
        new_value: Optional[str] = None,
        reason: Optional[str] = None,
        username: Optional[str] = None,
    ) -> Optional[UUID]:
        """
        Log any user action: ``record_after_commit`` with these arguments.

        The caller commits its own change first. If the entry cannot be
        written, the session is rolled back, one ERROR line naming only the
        exception type is logged, and None is returned, so the committed
        change stands and the session is usable again.

        Returns:
            The ID of the audit log entry, or None if it could not be written.
        """
        try:
            return AuditService._create_audit_log_sync(
                db,
                user_id,
                actor_email(user_email, username),
                action_type,
                entity_type,
                entity_id,
                entity_name,
                old_value,
                new_value,
                reason,
            )
        except Exception as e:
            AuditService._write_failed(db, action_type, entity_type, entity_id, e)
            return None

    @staticmethod
    def _create_audit_log_sync(
        db: Session,
        user_id: Optional[UUID],
        user_email: str,
        action_type: ActionType,
        entity_type: EntityType,
        entity_id: UUID,
        entity_name: str,
        old_value: Optional[str] = None,
        new_value: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> UUID:
        """Write and commit one audit row on ``db`` (raises on failure)."""
        row = AuditService.record(
            db,
            actor=AuditActor(id=user_id, email=user_email),
            action=action_type,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_name=entity_name,
            before=_as_audit_text(old_value),
            after=_as_audit_text(new_value),
            reason=reason,
        )
        db.commit()
        AuditService._written(row.action_type, row.entity_type, row.entity_id)
        return row.id

    @staticmethod
    def filtered_query(
        db: Session,
        *,
        user_id: Optional[UUID] = None,
        entity_type: Optional[EntityType] = None,
        entity_id: Optional[UUID] = None,
        action_type: Optional[ActionType] = None,
        from_date: Optional[datetime] = None,
        to_date: Optional[datetime] = None,
    ):
        """The entries the list and the export return, unordered and unpaged.

        Every filter is optional. Both dates are inclusive: ``from_date``
        keeps entries at or after it, ``to_date`` entries at or before it.
        """
        query = db.query(AuditLog)
        if user_id:
            query = query.filter(AuditLog.user_id == user_id)
        if entity_type:
            query = query.filter(AuditLog.entity_type == entity_type.value)
        if entity_id:
            query = query.filter(AuditLog.entity_id == entity_id)
        if action_type:
            query = query.filter(AuditLog.action_type == action_type.value)
        if from_date:
            query = query.filter(AuditLog.timestamp >= from_date)
        if to_date:
            query = query.filter(AuditLog.timestamp <= to_date)
        return query

    @staticmethod
    def get_audit_logs(
        db: Session,
        user_id: Optional[UUID] = None,
        entity_type: Optional[EntityType] = None,
        entity_id: Optional[UUID] = None,
        action_type: Optional[ActionType] = None,
        from_date: Optional[datetime] = None,
        to_date: Optional[datetime] = None,
        page: int = 1,
        limit: int = 50,
    ) -> Tuple[List[AuditLog], int]:
        """
        Retrieve audit logs with filtering and pagination.

        Args:
            db: Database session
            user_id: Filter by user ID (optional)
            entity_type: Filter by entity type (optional)
            entity_id: Filter by entity ID (optional)
            action_type: Filter by action type (optional)
            from_date: Filter logs from this date (optional)
            to_date: Filter logs until this date (optional)
            page: Page number (1-based)
            limit: Number of records per page

        Returns:
            Tuple[List[AuditLog], int]: List of audit logs and total count

        Raises:
            ValueError: If pagination parameters are invalid
        """
        if page < 1:
            raise ValueError("Page number must be 1 or greater")
        if limit < 1 or limit > 1000:
            raise ValueError("Limit must be between 1 and 1000")

        query = AuditService.filtered_query(
            db,
            user_id=user_id,
            entity_type=entity_type,
            entity_id=entity_id,
            action_type=action_type,
            from_date=from_date,
            to_date=to_date,
        )

        # Get total count before pagination
        total_count = query.count()

        # Apply pagination and ordering
        offset = (page - 1) * limit
        audit_logs = (
            query.order_by(desc(AuditLog.timestamp)).offset(offset).limit(limit).all()
        )

        logger.info(
            f"Retrieved {len(audit_logs)} audit logs (page {page}, "
            f"limit {limit}, total {total_count})"
        )

        return audit_logs, total_count

    @staticmethod
    def get_entity_audit_history(
        db: Session,
        entity_type: EntityType,
        entity_id: UUID,
        limit: int = 100,
    ) -> List[AuditLog]:
        """
        Get audit history for a specific entity.

        Args:
            db: Database session
            entity_type: Type of entity
            entity_id: ID of the entity
            limit: Maximum number of records to return

        Returns:
            List[AuditLog]: List of audit logs for the entity
        """
        audit_logs = (
            db.query(AuditLog)
            .filter(
                and_(
                    AuditLog.entity_type == entity_type.value,
                    AuditLog.entity_id == entity_id,
                )
            )
            .order_by(desc(AuditLog.timestamp))
            .limit(limit)
            .all()
        )

        logger.info(
            f"Retrieved {len(audit_logs)} audit logs for {entity_type.value} {entity_id}"
        )

        return audit_logs

    @staticmethod
    def get_user_activity(
        db: Session,
        user_id: UUID,
        from_date: Optional[datetime] = None,
        to_date: Optional[datetime] = None,
        limit: int = 100,
    ) -> List[AuditLog]:
        """
        Get audit logs for a specific user's activity.

        Args:
            db: Database session
            user_id: ID of the user
            from_date: Filter logs from this date (optional)
            to_date: Filter logs until this date (optional)
            limit: Maximum number of records to return

        Returns:
            List[AuditLog]: List of audit logs for the user
        """
        query = db.query(AuditLog).filter(AuditLog.user_id == user_id)

        if from_date:
            query = query.filter(AuditLog.timestamp >= from_date)

        if to_date:
            query = query.filter(AuditLog.timestamp <= to_date)

        audit_logs = query.order_by(desc(AuditLog.timestamp)).limit(limit).all()

        logger.info(f"Retrieved {len(audit_logs)} audit logs for user {user_id}")

        return audit_logs

    @staticmethod
    def compute_diff(old_data: dict, new_data: dict) -> list:
        """
        Compute structured diff between two state dicts.

        Returns a list of dicts with keys:
            - field: str
            - old_value: Any
            - new_value: Any
            - changed: bool

        Only includes fields whose values differ between old_data and new_data.
        Results are sorted alphabetically by field name.
        """
        diffs = []
        all_keys = set(old_data.keys()) | set(new_data.keys())
        for key in sorted(all_keys):
            old_val = old_data.get(key)
            new_val = new_data.get(key)
            if old_val != new_val:
                diffs.append(
                    {
                        "field": key,
                        "old_value": old_val,
                        "new_value": new_val,
                        "changed": True,
                    }
                )
        return diffs

    @staticmethod
    def get_flag_change_history(
        db: Session,
        flag_id: UUID,
        limit: int = 50,
        offset: int = 0,
        user_id: Optional[UUID] = None,
    ) -> Tuple[List[AuditLog], int]:
        """
        Get all audit log entries for a specific feature flag, ordered by
        timestamp descending (most recent first).

        Args:
            db: Database session
            flag_id: UUID of the feature flag
            limit: Maximum number of records to return
            offset: Number of records to skip (for pagination)
            user_id: If given, only entries made by this user (the total
                count is then of those entries only)

        Returns:
            Tuple[List[AuditLog], int]: (audit logs, total count)
        """
        query = db.query(AuditLog).filter(AuditLog.entity_id == flag_id)
        if user_id is not None:
            query = query.filter(AuditLog.user_id == user_id)

        total_count = query.count()

        audit_logs = (
            query.order_by(desc(AuditLog.timestamp)).offset(offset).limit(limit).all()
        )

        logger.info(
            f"Retrieved {len(audit_logs)} history entries for feature flag {flag_id} "
            f"(total {total_count})"
        )

        return audit_logs, total_count

    @staticmethod
    async def log_bulk_toggle(
        db: Session,
        user_id: UUID,
        user_email: str,
        flag_ids: List[UUID],
        action: str,
        results: List[dict],
        reason: Optional[str] = None,
    ) -> List[UUID]:
        """
        Create one audit log entry per flag in a bulk toggle operation.

        Args:
            db: Database session
            user_id: ID of the user performing the action
            user_email: Email of the user performing the action
            flag_ids: List of feature flag UUIDs
            action: Action string, e.g. "toggle_enable" or "toggle_disable"
            results: List of dicts with keys: flag_id, flag_name, old_status, new_status
            reason: Optional reason for the bulk operation

        Returns:
            List[UUID]: List of audit log IDs created (one per flag)
        """
        log_ids = []
        for result in results:
            log_id = await AuditService.log_toggle_operation(
                db=db,
                user_id=user_id,
                user_email=user_email,
                action_type=action,
                entity_id=UUID(result["flag_id"])
                if isinstance(result["flag_id"], str)
                else result["flag_id"],
                entity_name=result.get("flag_name", ""),
                old_value=result.get("old_status", ""),
                new_value=result.get("new_status", ""),
                reason=reason,
            )
            log_ids.append(log_id)
        return log_ids

    @staticmethod
    def get_audit_stats(
        db: Session,
        from_date: Optional[datetime] = None,
        to_date: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """
        Get audit log statistics.

        Args:
            db: Database session
            from_date: Filter logs from this date (optional)
            to_date: Filter logs until this date (optional)

        Returns:
            Dict[str, Any]: Statistics about audit logs
        """
        query = db.query(AuditLog)

        if from_date:
            query = query.filter(AuditLog.timestamp >= from_date)

        if to_date:
            query = query.filter(AuditLog.timestamp <= to_date)

        # Total count
        total_logs = query.count()

        # Count by action type
        action_counts = (
            query.with_entities(AuditLog.action_type, func.count(AuditLog.id))
            .group_by(AuditLog.action_type)
            .all()
        )

        # Count by entity type
        entity_counts = (
            query.with_entities(AuditLog.entity_type, func.count(AuditLog.id))
            .group_by(AuditLog.entity_type)
            .all()
        )

        # Most active users
        user_counts = (
            query.with_entities(AuditLog.user_email, func.count(AuditLog.id))
            .group_by(AuditLog.user_email)
            .order_by(desc(func.count(AuditLog.id)))
            .limit(10)
            .all()
        )

        return {
            "total_logs": total_logs,
            "action_counts": dict(action_counts),
            "entity_counts": dict(entity_counts),
            "most_active_users": dict(user_counts),
            "date_range": {
                "from_date": from_date.isoformat() if from_date else None,
                "to_date": to_date.isoformat() if to_date else None,
            },
        }


def record_experiment_change(
    db: Session,
    user: Any,
    action: ActionType,
    experiment_id: Any,
    before: Optional[Dict[str, Any]] = None,
    reason: Optional[str] = None,
) -> None:
    """Write the audit entry for a committed change to one experiment.

    ``before`` is ``audit_snapshot`` taken before the change; without it the
    entry is a create and records the experiment's identity fields. A failed
    write is logged and does not undo the change.
    """
    from backend.app.models.experiment import Experiment

    try:
        experiment_id = UUID(str(experiment_id))
        row = db.query(Experiment).filter(Experiment.id == experiment_id).first()
        after = audit_snapshot(EntityType.EXPERIMENT, row) if row is not None else {}
        if before is None:
            old_value, new_value = None, audit_identity(after)
        else:
            old_value, new_value = audit_changes(before, after)
    except Exception as e:
        # The change is committed: reading it back must not turn it into a 500.
        AuditService._write_failed(db, action, EntityType.EXPERIMENT, experiment_id, e)
        return
    AuditService.record_after_commit(
        db,
        actor=user,
        action=action,
        entity_type=EntityType.EXPERIMENT,
        entity_id=experiment_id,
        entity_name=after.get("name") or str(experiment_id),
        before=old_value,
        after=new_value,
        reason=reason,
    )
