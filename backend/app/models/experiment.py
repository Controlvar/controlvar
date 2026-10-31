# backend/app/models/experiment.py
"""
Experiment-related database models for the experimentation platform.

This module defines models for experiments, variants, and metrics.
"""

import enum
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    event,
    text,
)
from sqlalchemy import (
    Enum as SQLAEnum,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.declarative import declared_attr
from sqlalchemy.orm import relationship
from sqlalchemy.orm.base import NEVER_SET, NO_VALUE
from sqlalchemy.types import TypeDecorator

from backend.app.core.database_config import get_schema_name

from .base import Base, BaseModel


class UTCDateTime(TypeDecorator):
    """A ``timestamp without time zone`` column whose bound values are UTC.

    ``experiments.start_date`` and ``end_date`` hold UTC with no zone, but
    the API binds them in three shapes: an aware datetime (a request's dates,
    completion), an ISO string (a start) and a naive one. PostgreSQL converts
    an aware value to the session's time zone on the way into such a column
    and ignores the offset in a string, so on a server whose time zone is not
    UTC the two dates moved apart by the offset: west of UTC an end date
    landed before its start and completing answered 500 (#704). Every bound
    value is converted here to naive UTC, the stored form, whatever the
    session's zone; a naive value is taken to be UTC already. The database
    type is unchanged (no migration), and values read back are returned as
    stored. ``UTCTimestampString`` in ``models/event.py`` does the same for
    ``events.created_at``.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> Optional[datetime]:
        if value is None:
            return None
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        if isinstance(value, datetime) and value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value


class ExperimentStatus(enum.Enum):
    """Experiment status enum."""

    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class ExperimentType(enum.Enum):
    """Types of experiments."""

    A_B = "a_b"  # Simple A/B test
    MULTIVARIATE = "mv"  # Test with multiple variants
    SPLIT_URL = "split_url"  # Split test with different URLs
    BANDIT = "bandit"  # Multi-armed bandit


class MetricType(enum.Enum):
    """Types of metrics."""

    CONVERSION = "conversion"  # Binary conversion event
    REVENUE = "revenue"  # Revenue/monetary value
    COUNT = "count"  # Event count
    DURATION = "duration"  # Time duration
    CUSTOM = "custom"  # Custom metric


#: What an experiment stores when its creator names no correction method or
#: confidence level (#580), and what the migration backfills.
DEFAULT_CORRECTION_METHOD = "benjamini_hochberg"
DEFAULT_CONFIDENCE_LEVEL = 0.95


class Experiment(Base, BaseModel):
    """Experiment model for A/B testing."""

    __tablename__ = "experiments"

    name = Column(String(100), nullable=False)
    # Stable, human-readable identifier used by the SDK-facing tracking API
    # (`experiment_key`).  Generated from the name on create when not given.
    key = Column(String(100), unique=True, nullable=True, index=True)
    description = Column(Text)
    hypothesis = Column(Text)
    status = Column(
        SQLAEnum(ExperimentStatus),
        default=ExperimentStatus.DRAFT,
        nullable=False,
        index=True,
    )
    experiment_type = Column(
        SQLAEnum(ExperimentType),
        default=ExperimentType.A_B,
        nullable=False,
    )
    owner_id = Column(
        UUID(as_uuid=True),
        ForeignKey(f"{get_schema_name()}.users.id", ondelete="SET NULL"),
    )
    # UTC, stored without a zone; see UTCDateTime.
    start_date = Column(UTCDateTime)
    end_date = Column(UTCDateTime)
    # When a PAUSED experiment is due to resume (#436).  Only a PAUSED
    # experiment may carry one: ``ck_experiments_resume_only_when_paused``
    # below refuses any other status with a value set.
    resume_at = Column(DateTime(timezone=True), nullable=True)
    targeting_rules = Column(JSONB)  # For user segmentation
    metrics = Column(JSONB)  # Metrics to track
    tags = Column(JSONB)  # For categorization
    # Free-form notes/insights managed via POST /experiments/{id}/metadata.
    # (Named experiment_metadata because `metadata` is reserved by SQLAlchemy.)
    experiment_metadata = Column(JSONB, nullable=True)

    # Issue #22: MAB optimization type
    optimization_type = Column(
        SQLAEnum(
            "fixed",
            "thompson_sampling",
            "ucb1",
            "epsilon_greedy",
            name="optimization_type_enum",
        ),
        nullable=False,
        default="fixed",
    )

    # EP-021: Sequential testing configuration
    sequential_testing_enabled = Column(Boolean, default=False, nullable=False)
    sequential_testing_method = Column(
        SQLAEnum("msprt", "always_valid", name="sequential_testing_method_enum"),
        nullable=True,
    )
    sequential_testing_config = Column(JSONB, nullable=True)

    # Issue #21: CUPED variance reduction configuration
    variance_reduction_config = Column(JSONB, nullable=True)

    # #580: how the frequentist results judge this experiment. The results,
    # the sample-size plan, the export and the report use these unless a
    # request names its own. Both are locked once the experiment leaves draft.
    # The server defaults are what the migration backfills, and what lets an
    # older image that does not know the columns keep inserting experiments.
    correction_method = Column(
        String(32),
        nullable=False,
        default=DEFAULT_CORRECTION_METHOD,
        server_default=text("'benjamini_hochberg'"),
    )
    confidence_level = Column(
        Float,
        nullable=False,
        default=DEFAULT_CONFIDENCE_LEVEL,
        server_default=text("0.95"),
    )

    # EP-022: Mutual exclusion group membership
    mutual_exclusion_group_id = Column(
        UUID(as_uuid=True),
        ForeignKey(
            f"{get_schema_name()}.mutual_exclusion_groups.id",
            ondelete="SET NULL",
        ),
        nullable=True,
    )

    # EP-035: Bayesian experimentation columns
    bayesian_enabled = Column(Boolean, default=False, nullable=False)
    bayesian_config = Column(JSONB, nullable=True)  # BayesianConfig serialized as JSON
    bayesian_decision = Column(String(32), nullable=True)  # BayesianDecision value

    # EP-036: Split URL testing configuration
    # Example: {"variants": [{"url": "https://example.com/a", "traffic_allocation": 50},
    #                         {"url": "https://example.com/b", "traffic_allocation": 50}],
    #           "cookie_name": "split_url_exp_key"}
    split_url_config = Column(JSONB, nullable=True)

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
    owner = relationship("User", back_populates="experiments")
    variants = relationship(
        "Variant",
        back_populates="experiment",
        cascade="all, delete-orphan",
        # Creation order, the id as the tie-breaker, so that "the first
        # treatment" is the same arm on every call (#929). Without an ORDER BY
        # PostgreSQL returns the rows in their physical order, which a row
        # rewritten later in the heap or a plain ANALYZE can change.
        order_by="[Variant.created_at, Variant.id]",
    )
    metric_definitions = relationship(
        "Metric", back_populates="experiment", cascade="all, delete-orphan"
    )
    events = relationship(
        "Event", back_populates="experiment", cascade="all, delete-orphan"
    )
    reports = relationship("Report", back_populates="experiment", cascade="all, delete")

    # Add assignments relationship
    assignments = relationship(
        "Assignment", back_populates="experiment", cascade="all, delete-orphan"
    )

    # EP-022: Mutual exclusion group relationship
    mutual_exclusion_group = relationship(
        "MutualExclusionGroup", back_populates="experiments"
    )

    # Issue #22: MAB bandit state (one-to-one)
    bandit_state = relationship(
        "BanditState", back_populates="experiment", uselist=False
    )

    @declared_attr
    def __table_args__(cls):
        schema_name = get_schema_name()
        return (
            # Composite index for finding active experiments within a date range
            Index(
                f"{schema_name}_experiment_status_dates",
                "status",
                "start_date",
                "end_date",
            ),
            # Owner + status index for quickly finding a user's active experiments
            Index(f"{schema_name}_experiment_owner_status", "owner_id", "status"),
            # Ensure end_date is after start_date
            CheckConstraint(
                "end_date IS NULL OR start_date IS NULL OR end_date > start_date",
                name="check_experiment_dates",
            ),
            # A resume time belongs to a paused experiment and to nothing else.
            # The status enum is stored by NAME, hence 'PAUSED'.
            CheckConstraint(
                "resume_at IS NULL OR status = 'PAUSED'",
                name="ck_experiments_resume_only_when_paused",
            ),
            # #580: the same names and SQL as migration 806901fb7735.
            CheckConstraint(
                "correction_method IN ('none', 'bonferroni', 'benjamini_hochberg')",
                name="ck_experiments_correction_method",
            ),
            CheckConstraint(
                "confidence_level >= 0.80 AND confidence_level <= 0.99",
                name="ck_experiments_confidence_level",
            ),
            {"schema": schema_name},
        )

    def __repr__(self):
        return f"<Experiment {self.name}>"


_variant_clock_lock = threading.Lock()
_last_variant_created_at: Optional[datetime] = None


def next_variant_created_at() -> datetime:
    """A ``created_at`` strictly later than any this process gave a variant (#1469).

    ``Experiment.variants`` is ordered by ``[created_at, id]``, and create,
    update and clone insert every variant of an experiment in one request. On
    a fast machine two rows can share a microsecond, and the random uuid then
    decides the order: which treatment sequential testing compares with the
    control, and which variant a bucket maps to for a new user. Handing out a
    strictly increasing value makes the order the insertion order.
    """
    global _last_variant_created_at
    with _variant_clock_lock:
        now = datetime.utcnow()
        last = _last_variant_created_at
        if last is not None and now <= last:
            now = last + timedelta(microseconds=1)
        _last_variant_created_at = now
        return now


@event.listens_for(Experiment.status, "set", active_history=True)
def _clear_resume_on_status_change(target, value, oldvalue, initiator):
    """Drop a scheduled resume whenever the status really changes (#436).

    A resume time is scheduled on a PAUSED experiment and means "resume this
    pause at T". Any change of status -- a start, a completion, an archive, a
    new pause after a resume, or the scheduler's own activation -- ends that
    pause, so the resume goes with it. Doing it here covers every writer that
    assigns ``Experiment.status`` through the ORM, present and future.

    ``active_history=True`` loads the previous value when the instance has
    been expired (after a commit), so re-assigning the same status is
    recognised as no change and keeps the resume. A previous value that was
    never loaded or set (a new, unsaved instance) is not a change either.

    A Core ``UPDATE`` never reaches this listener; the
    ``ck_experiments_resume_only_when_paused`` constraint then refuses a
    non-PAUSED row that still carries a resume time.
    """
    if oldvalue is NO_VALUE or oldvalue is NEVER_SET:
        return
    if value == oldvalue:
        return
    target.resume_at = None


class Variant(Base, BaseModel):
    """Variant model for experiment variations."""

    __tablename__ = "variants"

    experiment_id = Column(
        UUID(as_uuid=True),
        ForeignKey(f"{get_schema_name()}.experiments.id", ondelete="CASCADE"),
        nullable=False,
    )
    name = Column(String(100), nullable=False)
    description = Column(Text)
    is_control = Column(Boolean, default=False)
    traffic_allocation = Column(Integer, default=50)  # Percentage of traffic
    configuration = Column(JSONB)  # Settings specific to this variant

    # Relationships
    experiment = relationship("Experiment", back_populates="variants")
    assignments = relationship(
        "Assignment", back_populates="variant", cascade="all, delete-orphan"
    )
    # Add events relationship
    events = relationship(
        "Event", back_populates="variant", cascade="all, delete-orphan"
    )

    @declared_attr
    def __table_args__(cls):
        schema_name = get_schema_name()
        return (
            # Composite index for quickly finding variants of an experiment
            Index(f"{schema_name}_variant_experiment", "experiment_id"),
            # Check that traffic allocation is between 0 and 100
            CheckConstraint(
                "traffic_allocation >= 0 AND traffic_allocation <= 100",
                name="check_traffic_allocation",
            ),
            {"schema": schema_name},
        )

    def __repr__(self):
        return f"<Variant {self.name}>"


class Metric(Base, BaseModel):
    """Metric model for experiment measurements."""

    __tablename__ = "metrics"

    experiment_id = Column(
        UUID(as_uuid=True),
        ForeignKey(f"{get_schema_name()}.experiments.id", ondelete="CASCADE"),
        nullable=False,
    )
    name = Column(String(100), nullable=False)
    description = Column(Text)
    event_name = Column(String(100), nullable=False, index=True)
    metric_type = Column(
        SQLAEnum(MetricType), default=MetricType.CONVERSION, nullable=False
    )
    is_primary = Column(Boolean, default=False)
    aggregation_method = Column(String(50), default="average")
    minimum_sample_size = Column(Integer, default=100)
    expected_effect = Column(Float)
    event_value_path = Column(String(100))
    lower_is_better = Column(Boolean, default=False)

    # Relationships
    experiment = relationship("Experiment", back_populates="metric_definitions")

    @declared_attr
    def __table_args__(cls):
        schema_name = get_schema_name()
        return (
            # Composite index for experiment + event
            Index(
                f"{schema_name}_metric_experiment_event", "experiment_id", "event_name"
            ),
            # Ensure unique metric names within an experiment
            Index(
                f"{schema_name}_metric_experiment_name",
                "experiment_id",
                "name",
                unique=True,
            ),
            {"schema": schema_name},
        )

    def __repr__(self):
        return f"<Metric {self.name}>"


@event.listens_for(Variant, "before_insert")
def _order_variants_by_insertion(mapper, connection, target):
    """Stamp a variant that has no ``created_at`` with a strictly later one (#1469).

    Runs per instance in the order the unit of work inserts them, which is the
    order they were added. A ``created_at`` the caller set is kept.
    """
    if target.created_at is None:
        target.created_at = next_variant_created_at()
