"""
Custom role and direct permission grant models for RBAC Post-MVP.
These supplement the static ROLE_PERMISSIONS dict in permissions.py.
"""

from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.declarative import declared_attr
from sqlalchemy.orm import relationship

from backend.app.core.database_config import get_schema_name
from backend.app.models.base import Base, BaseModel


class CustomRole(Base, BaseModel):
    """
    DB-backed custom role with configurable permissions.
    Supplements the 4 built-in roles in UserRole enum.
    """

    __tablename__ = "custom_roles"

    name = Column(String(64), nullable=False, unique=True, index=True)
    description = Column(Text, nullable=True)
    is_system_role = Column(Boolean, default=False, nullable=False)
    # permissions stored as JSONB: [{"resource": "experiment", "actions": ["read", "list"]}]
    permissions = Column(JSONB, nullable=False, default=list)
    created_by_id = Column(
        UUID(as_uuid=True),
        ForeignKey(f"{get_schema_name()}.users.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Relationships
    user_assignments = relationship(
        "UserCustomRole", back_populates="role", cascade="all, delete-orphan"
    )

    @declared_attr
    def __table_args__(cls):
        return ({"schema": get_schema_name()},)


class UserCustomRole(Base, BaseModel):
    """Many-to-many: users <-> custom roles (with audit fields)."""

    __tablename__ = "user_custom_roles"

    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey(f"{get_schema_name()}.users.id", ondelete="CASCADE"),
        nullable=False,
    )
    role_id = Column(
        UUID(as_uuid=True),
        ForeignKey(f"{get_schema_name()}.custom_roles.id", ondelete="CASCADE"),
        nullable=False,
    )
    assigned_by_id = Column(
        UUID(as_uuid=True),
        ForeignKey(f"{get_schema_name()}.users.id", ondelete="SET NULL"),
        nullable=True,
    )
    reason = Column(Text, nullable=True)
    assigned_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    #: The assignment's activation time (#891).  ``RBACService.assign_role``
    #: sets it, and the permission checks read only an assignment that has
    #: one; an assignment recorded before custom roles took effect has none
    #: until the role is assigned again.  No default:
    #: ``modules_0004_rbac_activation`` adds the column empty, and the previous
    #: release's INSERTs, which do not name it, leave it empty.
    activated_at = Column(DateTime(timezone=True), nullable=True)

    role = relationship("CustomRole", back_populates="user_assignments")

    @declared_attr
    def __table_args__(cls):
        return (
            Index(
                f"{get_schema_name()}_user_custom_roles_user_idx",
                "user_id",
            ),
            Index(
                f"{get_schema_name()}_user_custom_roles_role_idx",
                "role_id",
            ),
            {"schema": get_schema_name()},
        )


class DirectPermissionGrant(Base, BaseModel):
    """Direct permission grant to a specific user (not via role)."""

    __tablename__ = "direct_permission_grants"

    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey(f"{get_schema_name()}.users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    resource = Column(String(64), nullable=False)
    actions = Column(JSONB, nullable=False)  # ["read", "list"]
    granted_by_id = Column(
        UUID(as_uuid=True),
        ForeignKey(f"{get_schema_name()}.users.id", ondelete="SET NULL"),
        nullable=True,
    )
    reason = Column(Text, nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    #: The grant's activation time (#891).  ``RBACService.grant_direct_permission``
    #: sets it, and the permission checks read only a grant that has one; a
    #: grant recorded before custom roles took effect has none, and gives
    #: nothing until the permission is granted again.  No default:
    #: ``modules_0004_rbac_activation`` adds the column empty, and the previous
    #: release's INSERTs, which do not name it, leave it empty.
    activated_at = Column(DateTime(timezone=True), nullable=True)

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return datetime.now(timezone.utc) > self.expires_at

    @property
    def is_valid(self) -> bool:
        return self.is_active and not self.is_expired

    @declared_attr
    def __table_args__(cls):
        return (
            Index(
                f"{get_schema_name()}_direct_grants_user_resource_idx",
                "user_id",
                "resource",
            ),
            {"schema": get_schema_name()},
        )
