# Feature flag database models
import enum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    event,
    false,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.declarative import declared_attr
from sqlalchemy.orm import relationship
from sqlalchemy.orm.base import NEVER_SET, NO_VALUE

from backend.app.core.database_config import get_schema_name

from .base import Base, BaseModel


class FeatureFlagStatus(enum.Enum):
    """Feature flag status enum."""

    INACTIVE = "INACTIVE"
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


#: The answer to a request that would turn an archived flag on (#631).
ARCHIVED_FLAG_DETAIL = "This flag is archived. Unarchive it before turning it on."


class ArchivedFlagError(Exception):
    """An archived flag was asked to turn on (#631).

    An archived flag leaves ARCHIVED only through ``/unarchive``, which makes
    it INACTIVE. The routes refuse first, through
    ``feature_flag_service.transition``; the listener at the end of this module
    raises it for any other writer that assigns ``status`` through the ORM.
    """

    def __init__(self) -> None:
        super().__init__(ARCHIVED_FLAG_DETAIL)


def flag_status_name(value: object) -> str:
    """The upper-case name of a flag status given as the enum or a string.

    The column loads as :class:`FeatureFlagStatus`, but several writers assign
    the string value, so every comparison goes through this.
    """
    return str(getattr(value, "value", value)).upper()


#: The check on a flag's value type and its two values: the same name and SQL
#: as migration ``607c51a59bf5``.  A boolean flag stores neither value; a
#: string or number flag stores both, of that JSON type, and they differ; a json
#: flag stores both, each an object or an array, and they differ.  Every
#: non-boolean branch says IS NOT NULL because PostgreSQL passes a check whose
#: expression is NULL.  Equality is JSONB's (``3`` equals ``3.0``).
FLAG_VALUES_CHECK = "check_flag_values"
FLAG_VALUES_CHECK_SQL = (
    "(value_type = 'boolean' AND on_value IS NULL AND off_value IS NULL)"
    " OR (value_type = 'string'"
    " AND on_value IS NOT NULL AND off_value IS NOT NULL"
    " AND jsonb_typeof(on_value) = 'string' AND jsonb_typeof(off_value) = 'string'"
    " AND on_value <> off_value)"
    " OR (value_type = 'number'"
    " AND on_value IS NOT NULL AND off_value IS NOT NULL"
    " AND jsonb_typeof(on_value) = 'number' AND jsonb_typeof(off_value) = 'number'"
    " AND on_value <> off_value)"
    " OR (value_type = 'json'"
    " AND on_value IS NOT NULL AND off_value IS NOT NULL"
    " AND jsonb_typeof(on_value) IN ('object', 'array')"
    " AND jsonb_typeof(off_value) IN ('object', 'array')"
    " AND on_value <> off_value)"
)


class FeatureFlag(Base, BaseModel):
    """Feature flag model for feature toggles."""

    __tablename__ = "feature_flags"

    key = Column(String(100), unique=True, nullable=False, index=True)
    name = Column(String(100), nullable=False)
    description = Column(Text)
    status = Column(
        Enum(FeatureFlagStatus),
        default=FeatureFlagStatus.INACTIVE,
        nullable=False,
        index=True,
    )
    owner_id = Column(
        UUID(as_uuid=True),
        ForeignKey(f"{get_schema_name()}.users.id", ondelete="SET NULL"),
    )
    targeting_rules = Column(JSONB)  # Rules for flag enablement
    rollout_percentage = Column(Integer, default=0, nullable=False)  # Gradual rollout
    # What the flag serves when it is off.  The request schemas type it and
    # accept only false for now (D40), so every row holds false; evaluation does
    # not read it.  ``server_default`` is load-bearing: the previous release's
    # image inserts rows without naming this column, and the database fills it.
    default_value = Column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    # The flag's value type (boolean, string, number or json) and the two values
    # a string, number or json flag serves: ``on_value`` to the users it is on
    # for, ``off_value`` to everyone else (#1497).  A boolean flag keeps both
    # NULL.  ``check_flag_values`` in ``__table_args__`` holds the combinations;
    # nothing reads or writes these columns yet.  ``server_default`` is
    # load-bearing, as for ``default_value``.  ``none_as_null``: Python ``None``
    # is stored as SQL NULL, not as JSON ``null``, which the check refuses.
    value_type = Column(
        String(16),
        nullable=False,
        default="boolean",
        server_default=text("'boolean'"),
    )
    on_value = Column(JSONB(none_as_null=True), nullable=True)
    off_value = Column(JSONB(none_as_null=True), nullable=True)
    variants = Column(JSONB)  # For multivariate flags
    tags = Column(JSONB)  # For categorization

    # EP-057: Multi-Tenant Workspace isolation (nullable for backwards-compatibility).
    # The seam: a bare indexed UUID, not a ForeignKey. `workspaces` is the
    # workspaces module's table, and a ForeignKey here is the only thing in
    # the core ORM that reaches across the boundary — with it, importing this
    # module without the module's models raises NoReferencedTableError. The
    # constraint is attached from the module's side instead -- see
    # models/workspace.py, which appends it (use_alter, ON DELETE SET NULL)
    # whenever the module's models are loaded; the core leaves the column
    # unconstrained and nothing reads it.
    workspace_id = Column(
        UUID(as_uuid=True),
        nullable=True,
        index=True,
    )

    # Relationships
    owner = relationship("User", back_populates="feature_flags")
    overrides = relationship(
        "FeatureFlagOverride",
        back_populates="feature_flag",
        cascade="all, delete-orphan",
    )
    # Deleting a flag keeps its events (#855). The events.feature_flag_id FK
    # is ON DELETE SET NULL; passive_deletes leaves that to the database, so
    # the ORM neither deletes the events nor loads them to clear the column.
    events = relationship("Event", back_populates="feature_flag", passive_deletes=True)
    reports = relationship(
        "Report", back_populates="feature_flag", cascade="all, delete"
    )
    rollout_schedules = relationship(
        "RolloutSchedule",
        back_populates="feature_flag",
        cascade="all, delete-orphan",
    )
    raw_metrics = relationship(
        "RawMetric", back_populates="feature_flag", cascade="all, delete-orphan"
    )
    aggregated_metrics = relationship(
        "AggregatedMetric", back_populates="feature_flag", cascade="all, delete-orphan"
    )
    error_logs = relationship(
        "ErrorLog", back_populates="feature_flag", cascade="all, delete-orphan"
    )
    safety_config = relationship(
        "FeatureFlagSafetyConfig",
        back_populates="feature_flag",
        cascade="all, delete-orphan",
        uselist=False,  # One-to-one relationship
    )

    @declared_attr
    def __table_args__(cls):
        schema_name = get_schema_name()
        return (
            # Composite index for owner + status
            Index(f"{schema_name}_feature_flag_owner_status", "owner_id", "status"),
            # Check that rollout percentage is between 0 and 100
            CheckConstraint(
                "rollout_percentage >= 0 AND rollout_percentage <= 100",
                name="check_rollout_percentage",
            ),
            CheckConstraint(FLAG_VALUES_CHECK_SQL, name=FLAG_VALUES_CHECK),
            {"schema": schema_name},
        )

    def __repr__(self):
        return f"<FeatureFlag {self.key}>"


@event.listens_for(FeatureFlag.status, "set", active_history=True)
def _an_archived_flag_is_not_turned_on(target, value, oldvalue, initiator):
    """Refuse ARCHIVED -> ACTIVE for every ORM writer of ``status`` (#631).

    Old and new are compared by name, so the enum and its string value are the
    same status. A previous value that was never loaded or set (a flag being
    constructed, as the seed scripts do) is not a change. ``active_history``
    loads the stored value when the instance has been expired, so a writer that
    assigns after a commit is still seen.

    A Core ``UPDATE`` or raw SQL never reaches this listener.
    """
    if oldvalue is NO_VALUE or oldvalue is NEVER_SET or oldvalue is None:
        return
    if (
        flag_status_name(oldvalue) == FeatureFlagStatus.ARCHIVED.value
        and flag_status_name(value) == FeatureFlagStatus.ACTIVE.value
    ):
        raise ArchivedFlagError()


class FeatureFlagOverride(Base, BaseModel):
    """Override model for user-specific feature flag settings."""

    __tablename__ = "feature_flag_overrides"

    feature_flag_id = Column(
        UUID(as_uuid=True),
        ForeignKey(f"{get_schema_name()}.feature_flags.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id = Column(String(255), nullable=False)  # External user identifier
    value = Column(
        JSONB, nullable=False
    )  # Can be boolean or variant name for multivariate flags
    reason = Column(String(255))  # Optional explanation
    expires_at = Column(DateTime)  # Optional expiration

    # Relationships
    feature_flag = relationship("FeatureFlag", back_populates="overrides")

    @declared_attr
    def __table_args__(cls):
        schema_name = get_schema_name()
        return (
            # Composite unique constraint on feature flag + user
            Index(
                f"{schema_name}_override_feature_user",
                "feature_flag_id",
                "user_id",
                unique=True,
            ),
            {"schema": schema_name},
        )

    def __repr__(self):
        return f"<FeatureFlagOverride {self.feature_flag_id}:{self.user_id}>"
