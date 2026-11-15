"""
RBAC service — custom role management and effective permissions resolution.

Custom roles and direct grants take effect (#891): an assignment or a grant
with an activation time (``activated_at``) adds its permissions to the user's
built-in role in every permission check, through
:func:`granted_permission_pairs`, which ``register.py`` installs as the
``rbac.granted_permissions`` capability. Rows recorded before custom roles
took effect have no activation time and grant nothing until the role is
assigned or the grant made again.
"""

from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple
from uuid import UUID

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from backend.app.core.permissions import (
    Action,
    ResourceType,
    can_read_all_audit_logs,
    check_permission,
)
from backend.app.models.user import User, UserRole
from modules.backend.app.models.custom_role import (
    CustomRole,
    DirectPermissionGrant,
    UserCustomRole,
)
from modules.backend.app.schemas.rbac import (
    CustomRoleCreate,
    CustomRoleUpdate,
    EffectivePermissionsResponse,
)

#: The resources ``GET /rbac/users/{user_id}/permissions`` lists: the three a
#: custom role or a direct grant can hold (``permissions.GRANTABLE``).
SHOWN_RESOURCES = (ResourceType.EXPERIMENT, ResourceType.FEATURE_FLAG)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def granted_permission_pairs(session: Session, user_id: Any) -> List[Tuple[str, str]]:
    """The ``rbac.granted_permissions`` provider: what *user_id*'s rows hold.

    Two indexed SELECTs on *session*, which it never flushes, commits or
    writes to:

    * the permissions of every custom role assigned to the user by an
      assignment with an activation time;
    * the actions of every direct grant of the user that has an activation
      time, is active and has not expired, by the database's clock.

    A role's stored entry is ``{"resource": str, "actions": [str, ...]}``; an
    entry of any other shape, and an action that is not a string, is skipped,
    never raised on. Pairs outside ``permissions.GRANTABLE`` are returned as
    stored: the core drops them.
    """
    pairs: List[Tuple[str, str]] = []
    with session.no_autoflush:
        roles = (
            session.query(CustomRole.permissions)
            .join(UserCustomRole, UserCustomRole.role_id == CustomRole.id)
            .filter(
                UserCustomRole.user_id == user_id,
                UserCustomRole.activated_at.isnot(None),
            )
            .all()
        )
        grants = (
            session.query(DirectPermissionGrant.resource, DirectPermissionGrant.actions)
            .filter(
                DirectPermissionGrant.user_id == user_id,
                DirectPermissionGrant.is_active.is_(True),
                DirectPermissionGrant.activated_at.isnot(None),
                or_(
                    DirectPermissionGrant.expires_at.is_(None),
                    DirectPermissionGrant.expires_at > func.now(),
                ),
            )
            .all()
        )
    for (permissions,) in roles:
        if not isinstance(permissions, list):
            continue
        for entry in permissions:
            if not isinstance(entry, dict):
                continue
            resource, actions = entry.get("resource"), entry.get("actions")
            if isinstance(resource, str) and isinstance(actions, list):
                pairs.extend((resource, a) for a in actions if isinstance(a, str))
    for resource, actions in grants:
        if isinstance(resource, str) and isinstance(actions, list):
            pairs.extend((resource, a) for a in actions if isinstance(a, str))
    return pairs


class RBACService:
    @staticmethod
    def create_custom_role(
        db: Session, data: CustomRoleCreate, created_by_id: UUID
    ) -> CustomRole:
        """Create a new custom role. Raises ValueError if name already exists."""
        existing = db.query(CustomRole).filter(CustomRole.name == data.name).first()
        if existing:
            raise ValueError(f"Role '{data.name}' already exists")
        role = CustomRole(
            name=data.name,
            description=data.description,
            permissions=[p.model_dump() for p in data.permissions],
            created_by_id=created_by_id,
        )
        db.add(role)
        db.flush()
        return role

    @staticmethod
    def list_custom_roles(db: Session, include_system: bool = True) -> List[CustomRole]:
        """List all custom roles, optionally including system roles."""
        q = db.query(CustomRole)
        if not include_system:
            q = q.filter(CustomRole.is_system_role == False)
        return q.order_by(CustomRole.name).all()

    @staticmethod
    def get_custom_role(db: Session, role_name: str) -> Optional[CustomRole]:
        return db.query(CustomRole).filter(CustomRole.name == role_name).first()

    @staticmethod
    def update_custom_role(
        db: Session, role_name: str, data: CustomRoleUpdate
    ) -> CustomRole:
        role = db.query(CustomRole).filter(CustomRole.name == role_name).first()
        if not role:
            raise ValueError(f"Role '{role_name}' not found")
        if role.is_system_role:
            raise ValueError("Cannot modify system roles")
        if data.description is not None:
            role.description = data.description
        if data.permissions is not None:
            role.permissions = [p.model_dump() for p in data.permissions]
        db.flush()
        return role

    @staticmethod
    def delete_custom_role(db: Session, role_name: str) -> None:
        role = db.query(CustomRole).filter(CustomRole.name == role_name).first()
        if not role:
            raise ValueError(f"Role '{role_name}' not found")
        if role.is_system_role:
            raise ValueError("Cannot delete system roles")
        db.delete(role)
        db.flush()

    @staticmethod
    def assign_role(
        db: Session,
        user_id: UUID,
        role_name: str,
        assigned_by_id: UUID,
        reason: Optional[str] = None,
    ) -> UserCustomRole:
        """Assign a custom role to a user, so that it takes effect.

        Idempotent: assigning a role the user already has, with an activation
        time, changes nothing. An assignment recorded before custom roles took
        effect (no activation time) is activated: it takes effect from now.
        """
        role = db.query(CustomRole).filter(CustomRole.name == role_name).first()
        if not role:
            raise ValueError(f"Role '{role_name}' not found")
        existing = (
            db.query(UserCustomRole)
            .filter(
                UserCustomRole.user_id == user_id,
                UserCustomRole.role_id == role.id,
            )
            .first()
        )
        if existing:
            if existing.activated_at is None:
                existing.activated_at = _now()
                db.flush()
            return existing
        assignment = UserCustomRole(
            user_id=user_id,
            role_id=role.id,
            assigned_by_id=assigned_by_id,
            reason=reason,
            activated_at=_now(),
        )
        db.add(assignment)
        db.flush()
        return assignment

    @staticmethod
    def has_role(db: Session, user_id: UUID, role_name: str) -> bool:
        """Whether the user holds the custom role, taking effect.

        An assignment recorded before custom roles took effect (no activation
        time) does not count. False if the role does not exist.
        """
        return (
            db.query(UserCustomRole)
            .join(CustomRole, UserCustomRole.role_id == CustomRole.id)
            .filter(
                UserCustomRole.user_id == user_id,
                CustomRole.name == role_name,
                UserCustomRole.activated_at.isnot(None),
            )
            .first()
            is not None
        )

    @staticmethod
    def has_role_not_activated(db: Session, user_id: UUID, role_name: str) -> bool:
        """Whether an assignment of the role, recorded before custom roles took
        effect, is stored without an activation time."""
        return (
            db.query(UserCustomRole)
            .join(CustomRole, UserCustomRole.role_id == CustomRole.id)
            .filter(
                UserCustomRole.user_id == user_id,
                CustomRole.name == role_name,
                UserCustomRole.activated_at.is_(None),
            )
            .first()
            is not None
        )

    @staticmethod
    def revoke_role(db: Session, user_id: UUID, role_name: str) -> int:
        """Revoke a custom role from a user. No error if not assigned.

        Returns how many assignments were removed: 0 or 1.
        """
        role = db.query(CustomRole).filter(CustomRole.name == role_name).first()
        if not role:
            raise ValueError(f"Role '{role_name}' not found")
        removed = (
            db.query(UserCustomRole)
            .filter(
                UserCustomRole.user_id == user_id,
                UserCustomRole.role_id == role.id,
            )
            .delete()
        )
        db.flush()
        return removed

    @staticmethod
    def grant_direct_permission(
        db: Session,
        user_id: UUID,
        resource: str,
        actions: List[str],
        granted_by_id: UUID,
        reason: Optional[str] = None,
        expires_at: Optional[datetime] = None,
    ) -> DirectPermissionGrant:
        """Grant a direct permission to a user; it takes effect from now."""
        grant = DirectPermissionGrant(
            user_id=user_id,
            resource=resource,
            actions=actions,
            granted_by_id=granted_by_id,
            reason=reason,
            expires_at=expires_at,
            is_active=True,
            activated_at=_now(),
        )
        db.add(grant)
        db.flush()
        return grant

    @staticmethod
    def revoke_direct_permission(db: Session, user_id: UUID, resource: str) -> int:
        """Revoke all direct grants for a user+resource. Returns count deleted."""
        count = (
            db.query(DirectPermissionGrant)
            .filter(
                DirectPermissionGrant.user_id == user_id,
                DirectPermissionGrant.resource == resource,
            )
            .delete()
        )
        db.flush()
        return count

    @staticmethod
    def get_effective_permissions(
        db: Session, user: User
    ) -> EffectivePermissionsResponse:
        """What the permission checks allow *user*, for the grantable resources.

        Built on the checks themselves, so the display and the enforcement
        cannot drift apart: for ``experiment`` and ``feature_flag``, the
        actions ``check_permission`` admits (a resource with none is left
        out); ``audit_log: ["read"]`` exactly when ``can_read_all_audit_logs``
        answers yes. Nothing else is listed: no other resource can be held by
        a custom role or grant, and administration is decided by the built-in
        role or the superuser flag. *user* is a row of *db*, so its custom
        roles and grants are read on it. ``custom_roles`` lists only the roles
        assigned with an activation time.
        """
        perms = {}
        for resource in SHOWN_RESOURCES:
            actions = sorted(
                action.value
                for action in Action
                if check_permission(user, resource, action)
            )
            if actions:
                perms[resource.value] = actions
        if can_read_all_audit_logs(user):
            perms["audit_log"] = ["read"]

        custom_role_names = [
            name
            for (name,) in db.query(CustomRole.name)
            .join(UserCustomRole, UserCustomRole.role_id == CustomRole.id)
            .filter(
                UserCustomRole.user_id == user.id,
                UserCustomRole.activated_at.isnot(None),
            )
            .order_by(CustomRole.name)
            .all()
        ]

        # Build base_role string safely
        user_role = getattr(user, "role", UserRole.VIEWER)
        if hasattr(user_role, "value"):
            base_role_str = user_role.value
        else:
            base_role_str = str(user_role)

        return EffectivePermissionsResponse(
            user_id=str(user.id),
            username=user.username,
            base_role=base_role_str,
            custom_roles=custom_role_names,
            permissions=perms,
            is_superuser=bool(user.is_superuser),
        )
