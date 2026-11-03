# models/event.py
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from sqlalchemy import Column, DateTime, Float, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.declarative import declared_attr
from sqlalchemy.orm import relationship
from sqlalchemy.types import TypeDecorator

from backend.app.core.database_config import get_schema_name

from .base import Base, BaseModel


class EventType(str, Enum):
    """Event type enum."""

    EXPOSURE = "exposure"  # User was exposed to a variant
    CONVERSION = "conversion"  # User completed a desired action
    CLICK = "click"  # User clicked on a tracked element
    PAGE_VIEW = "page_view"  # User viewed a tracked page
    CUSTOM = "custom"  # Custom event type


def normalize_event_timestamp(value: Any) -> str:
    """Return ``value`` as a UTC ISO-8601 string in one canonical format.

    ``events.created_at`` is a string column, so every time window over events
    is a string comparison.  That only orders correctly when every value is in
    UTC and in the same shape, so every value is converted to UTC and written
    the way ``datetime.isoformat()`` writes an aware UTC datetime:
    ``YYYY-MM-DDTHH:MM:SS+00:00``, or ``YYYY-MM-DDTHH:MM:SS.ffffff+00:00``
    when there are microseconds.  Those two shapes still sort in time order
    ('+' sorts before '.', and a value with no fraction is the earlier one),
    and they are what the server has always written for its own timestamps,
    so rows written that way compare correctly with new ones.  A value with
    no offset is taken to be UTC already.

    Accepts a ``datetime`` or an ISO-8601 string (``Z`` or a numeric offset).
    Raises ``ValueError`` for anything else, so an unreadable timestamp is
    refused rather than stored and compared as text.  That includes a value
    at the edge of the date range whose offset moves it past 0001-01-01 or
    9999-12-31 once converted to UTC (``datetime.astimezone`` raises
    ``OverflowError`` for it, #1684).
    """
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        moment = datetime.fromisoformat(value.strip())
    else:
        raise ValueError("event timestamp must be a datetime or an ISO-8601 string")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    try:
        return moment.astimezone(timezone.utc).isoformat()
    except OverflowError:
        raise ValueError(
            "event timestamp is outside the range that can be stored once it "
            "is converted to UTC (0001-01-01 to 9999-12-31)"
        ) from None


class UTCTimestampString(TypeDecorator):
    """A ``String`` column whose bound values are normalised to UTC.

    The database type is unchanged (no migration).  Normalising at the bind
    covers every writer -- the tracking API, ``EventService``, the seed
    scripts -- and every comparison: ``Event.created_at >= x`` binds ``x``
    through this type, so a window boundary given as a ``datetime`` or with
    an offset is compared in the same format as the stored values.  Values
    read back are returned as stored.
    """

    impl = String
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> Optional[str]:
        if value is None:
            return None
        return normalize_event_timestamp(value)


class Event(Base, BaseModel):
    """Event model for tracking user interactions and metric data."""

    __tablename__ = "events"

    event_type = Column(String(100), nullable=False, index=True)
    event_name = Column(String(255), nullable=True, index=True)
    user_id = Column(
        String(255), nullable=False, index=True
    )  # External user identifier
    experiment_id = Column(
        UUID(as_uuid=True),
        ForeignKey(f"{get_schema_name()}.experiments.id", ondelete="SET NULL"),
        nullable=True,
    )
    feature_flag_id = Column(
        UUID(as_uuid=True),
        ForeignKey(f"{get_schema_name()}.feature_flags.id", ondelete="SET NULL"),
        nullable=True,
    )
    variant_id = Column(
        UUID(as_uuid=True),
        ForeignKey(f"{get_schema_name()}.variants.id", ondelete="SET NULL"),
        nullable=True,
    )
    value = Column(Float)  # Numeric value if applicable
    event_metadata = Column(JSONB)  # Additional data
    # When the event happened, as a UTC ISO-8601 string (see
    # UTCTimestampString): the column is VARCHAR, so the format is what makes
    # time windows compare correctly.
    created_at = Column(UTCTimestampString, nullable=False, index=True)
    # When the server stored the row: naive UTC, set once on insert.  CUPED
    # reads a user's history only if it was stored before the user was
    # assigned (#217), so this must keep meaning "received at".  It is
    # declared here without BaseModel's ``onupdate``: an ORM update of an
    # event (re-tagging, scrubbing) must leave it as it was, or that history
    # would silently drop out of every covariate.  Python-side only; the
    # column is unchanged, so there is no migration.
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relationships
    experiment = relationship("Experiment", back_populates="events")
    feature_flag = relationship("FeatureFlag", back_populates="events")
    variant = relationship("Variant", back_populates="events")

    @declared_attr
    def __table_args__(cls):
        schema_name = get_schema_name()
        return (
            # Composite index for querying events by user + event type
            Index(f"{schema_name}_event_user_type", "user_id", "event_type"),
            # Composite index for experiment + event_type for quick metric calculations
            Index(
                f"{schema_name}_event_experiment_type", "experiment_id", "event_type"
            ),
            # Composite index for feature flag + event_type for quick metric calculations
            Index(
                f"{schema_name}_event_feature_flag_type",
                "feature_flag_id",
                "event_type",
            ),
            # Composite index for timestamp-based queries within an experiment
            Index(
                f"{schema_name}_event_experiment_timestamp",
                "experiment_id",
                "created_at",
            ),
            {"schema": schema_name},
        )

    def __repr__(self):
        return f"<Event {self.id}: {self.event_type} for user {self.user_id}>"
