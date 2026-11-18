import enum

from sqlalchemy import (
    Boolean,
    Column,
    Enum,
    ForeignKey,
    Index,
    String,
    Table,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.declarative import declared_attr
from sqlalchemy.orm import object_session, relationship

from backend.app.core.database_config import get_schema_name

from .base import Base, BaseModel
from .user_totp import UserTOTP


# Define UserRole enum for direct role assignment
class UserRole(str, enum.Enum):
    """User roles in the system."""

    ADMIN = "admin"
    ANALYST = "analyst"
    DEVELOPER = "developer"
    VIEWER = "viewer"


# Many-to-many relationship table for users and roles
def _get_users_table_name():
    return f"{get_schema_name()}.users"


def _get_roles_table_name():
    return f"{get_schema_name()}.roles"


def _get_permissions_table_name():
    return f"{get_schema_name()}.permissions"


user_role_association = Table(
    "user_role_association",
    Base.metadata,
    Column(
        "user_id",
        UUID(as_uuid=True),
        ForeignKey(f"{_get_users_table_name()}.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "role_id",
        UUID(as_uuid=True),
        ForeignKey(f"{_get_roles_table_name()}.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    schema=get_schema_name(),
)

# Many-to-many relationship table for roles and permissions
role_permission_association = Table(
    "role_permission_association",
    Base.metadata,
    Column(
        "role_id",
        UUID(as_uuid=True),
        ForeignKey(f"{_get_roles_table_name()}.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "permission_id",
        UUID(as_uuid=True),
        ForeignKey(f"{_get_permissions_table_name()}.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    schema=get_schema_name(),
)


#: An address belongs to one account whatever its letter case (#343).
EMAIL_LOWER_INDEX = "ix_users_email_lower"


def _email_lower_index(cls) -> Index:
    """The unique index on ``lower(email)``, built once per table.

    ``__table_args__`` is a ``declared_attr``, which runs again every time it
    is read after mapping, and an ``Index`` over a mapped column attaches
    itself to that column's table as it is built.  A second read would hand
    the table a second index of the same name, and ``create_all`` would then
    fail with "relation already exists".  Once the table exists, its own
    index is returned instead.
    """
    table = cls.__dict__.get("__table__")
    if table is not None:
        return next(i for i in table.indexes if i.name == EMAIL_LOWER_INDEX)
    return Index(EMAIL_LOWER_INDEX, func.lower(cls.email), unique=True)


class User(Base, BaseModel):
    """User model for authentication and ownership."""

    __tablename__ = "users"

    username = Column(String(100), unique=True, index=True)
    email = Column(String(100), unique=True, index=True)
    hashed_password = Column(String(255), nullable=True)
    is_active = Column(Boolean, default=True)
    is_superuser = Column(Boolean, default=False)
    role = Column(Enum(UserRole), default=UserRole.VIEWER)
    external_id = Column(String(255), unique=True, nullable=True)  # Cognito ID
    first_name = Column(String(100), nullable=True)
    last_name = Column(String(100), nullable=True)
    settings = Column(JSONB, default={})
    preferences = Column(JSONB, default={})

    # Relationships
    experiments = relationship("Experiment", back_populates="owner")
    feature_flags = relationship("FeatureFlag", back_populates="owner")
    reports = relationship("Report", back_populates="owner")
    audit_logs = relationship("AuditLog", back_populates="user")
    # Removing an account removes its API keys with it, and keeps the segments
    # and rollout schedules it created, with owner_id null (both columns are
    # ON DELETE SET NULL).
    api_keys = relationship(
        "APIKey", back_populates="user", cascade="all, delete-orphan"
    )
    segments = relationship("Segment", back_populates="owner")
    rollout_schedules = relationship("RolloutSchedule", back_populates="owner")

    def __init__(self, **kwargs):
        """Initialize a user with proper handling of full_name and preferences."""
        # Extract full_name if present and not None
        full_name = kwargs.pop("full_name", None)

        # Extract preferences if present or set to default empty dict
        preferences = kwargs.pop("preferences", {})

        # Make sure we handle the case where full_name is passed but is None
        if "full_name" in kwargs:
            kwargs.pop("full_name")

        # Initialize using parent class
        super().__init__(**kwargs)

        # Set preferences
        self.preferences = preferences

        # Set the full_name using the property setter if provided
        if full_name is not None:
            self.full_name = full_name

    @property
    def full_name(self):
        """Return the full name by combining first and last name."""
        if self.first_name and self.last_name:
            return f"{self.first_name} {self.last_name}"
        elif self.first_name:
            return self.first_name
        elif self.last_name:
            return self.last_name
        return None

    @full_name.setter
    def full_name(self, value):
        """Set first_name based on the full name provided."""
        if value:
            parts = value.split(" ", 1)
            self.first_name = parts[0]
            self.last_name = parts[1] if len(parts) > 1 else None
        else:
            self.first_name = None
            self.last_name = None

    @property
    def totp_enabled(self) -> bool:
        """Whether the account has two-step sign-in on (#1812): a ``user_totp``
        row with ``confirmed_at`` set.

        Read from the database each time, through the session the object
        belongs to; no column of ``users`` holds it. False for an object that
        belongs to no session (never saved, or detached).
        """
        session = object_session(self)
        if session is None or self.id is None:
            return False
        found = session.execute(
            select(UserTOTP.user_id).where(
                UserTOTP.user_id == self.id, UserTOTP.confirmed_at.is_not(None)
            )
        ).first()
        return found is not None

    @declared_attr
    def __table_args__(cls):
        return (
            # An address belongs to one account whatever its letter case
            # (#343), beside -- not instead of -- the exact index on ``email``.
            # A fixed name rather than one derived from the schema, so the
            # ``create_all`` of a fresh bootstrap and the core migration that
            # adds it to an existing database build the same index.
            _email_lower_index(cls),
            {"schema": get_schema_name()},
        )

    def __repr__(self):
        return f"<User {self.username}>"


class Role(Base, BaseModel):
    """Role model for grouping permissions."""

    __tablename__ = "roles"

    name = Column(String(50), unique=True, nullable=False, index=True)
    description = Column(String(255))

    @declared_attr
    def __table_args__(cls):
        return ({"schema": get_schema_name()},)

    def __repr__(self):
        return f"<Role {self.name}>"


class Permission(Base, BaseModel):
    """Permission model for defining granular access controls."""

    __tablename__ = "permissions"

    name = Column(String(50), unique=True, nullable=False, index=True)
    description = Column(String(255))
    resource = Column(
        String(50), nullable=False, index=True
    )  # e.g., 'experiment', 'feature_flag'
    action = Column(
        String(50), nullable=False
    )  # e.g., 'create', 'read', 'update', 'delete'

    @declared_attr
    def __table_args__(cls):
        return ({"schema": get_schema_name()},)

    def __repr__(self):
        return f"<Permission {self.name}>"
