"""
Global Holdout model for keeping a percentage of new users out of every
experiment (EP-022, #445).

While a holdout is active, ``POST /api/v1/tracking/assign`` answers a new user
the holdout buckets with the control variant and no assignment, and so does
``POST /api/v1/tracking/split-url/assign``, which answers through it (a split
URL experiment's visitor is then served the page they asked for).  Feature
flags ignore it.

Who a holdout covered is recorded in ``holdout_population``
(``backend/app/models/holdout_population.py``), so the held-out users can be
compared with everyone else first seen while it was active.  That comparison
needs three things this model carries:

* ``hash_salt``: each holdout buckets users with its own salt, so the users one
  holdout kept out are not the same users the next one keeps out.  A new row
  gets ``holdout:<random token>`` from the Python default below.  The legacy
  salt ``global_holdout_v1`` is the server default, kept only so an older image
  after a rollback can still insert a row; a holdout with the legacy salt is
  never measurable.
* ``activated_at`` / ``deactivated_at``: when it started and ended.  Both are
  set by ``GlobalHoldoutService`` and never cleared; an ended holdout cannot
  restart.
* a partial unique index on ``is_active`` ``WHERE is_active``: at most one
  active holdout, enforced by the database.
"""

import secrets

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.declarative import declared_attr
from sqlalchemy.orm import relationship

from backend.app.core.database_config import get_schema_name

from .base import Base, BaseModel

#: The salt every holdout used before #445.  Only the row that was active at
#: the upgrade keeps it (so old and new tasks agree on its buckets during a
#: blue/green deploy); such a row is never measurable.
LEGACY_HOLDOUT_SALT = "global_holdout_v1"

#: Name of the partial unique index that allows at most one active holdout.
ONE_ACTIVE_INDEX = "uq_global_holdouts_one_active"


def new_holdout_salt() -> str:
    """A fresh per-holdout salt: ``holdout:`` and 32 random hex characters.

    Random rather than derived from ``id``: ``id`` is itself a Python default,
    and nothing guarantees it is computed before this one.
    """
    return f"holdout:{secrets.token_hex(16)}"


class GlobalHoldout(Base, BaseModel):
    """Global holdout configuration reserving new users from all experiments."""

    __tablename__ = "global_holdouts"

    name = Column(String(200), nullable=False, unique=True)
    description = Column(Text, nullable=True)
    holdout_percentage = Column(Integer, nullable=False, default=10)
    is_active = Column(Boolean, nullable=False, default=False)
    owner_id = Column(
        UUID(as_uuid=True),
        ForeignKey(f"{get_schema_name()}.users.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Naive UTC, like BaseModel.created_at.
    activated_at = Column(DateTime, nullable=True)
    deactivated_at = Column(DateTime, nullable=True)
    hash_salt = Column(
        String(64),
        nullable=False,
        default=new_holdout_salt,
        server_default=LEGACY_HOLDOUT_SALT,
    )

    # Relationships
    owner = relationship("User")

    @declared_attr
    def __table_args__(cls):
        schema_name = get_schema_name()
        return (
            Index(f"{schema_name}_holdout_active", "is_active"),
            Index(
                ONE_ACTIVE_INDEX,
                "is_active",
                unique=True,
                postgresql_where=text("is_active"),
            ),
            CheckConstraint(
                "holdout_percentage >= 1 AND holdout_percentage <= 20",
                name="check_holdout_percentage_range",
            ),
            {"schema": schema_name},
        )

    @property
    def is_measurable(self) -> bool:
        """True when who this holdout covered is recorded from its start.

        A row with the legacy salt never is, whatever ``activated_at`` says:
        its buckets are the ones every pre-#445 holdout used, so its held-out
        users are the previous holdouts' held-out users.
        """
        return self.hash_salt != LEGACY_HOLDOUT_SALT and self.activated_at is not None

    def __repr__(self):
        return f"<GlobalHoldout {self.name} ({self.holdout_percentage}%)>"
