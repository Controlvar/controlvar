"""An anonymous id linked to the known id of the same person (#449).

One row per anonymous id that has been linked:

* ``anonymous_id``: the primary key, so an anonymous id is linked at most once;
* ``user_id``: the known id it is linked to (``ix_identity_aliases_user_id``
  finds every anonymous id of one known id);
* ``created_at``: when the link was stored, by the database's clock.

``check_identity_aliases_ids_differ`` refuses a row that links an id to
itself. Both columns have the type of ``events.user_id`` and
``assignments.user_id``, and neither references another table: there is no
foreign key, so creating the table takes no lock on any existing table.

Revision ``t449_identity_aliases`` creates the table with these names.
Downgrading below that revision drops the table and every link in it.
"""

from sqlalchemy import CheckConstraint, Column, DateTime, Index, String, text
from sqlalchemy.ext.declarative import declared_attr

from backend.app.core.database_config import get_schema_name

from .base import Base

#: The constraint and index names. The revision spells the same names as its
#: own literals: a migration imports nothing from the application.
IDS_DIFFER_CHECK = "check_identity_aliases_ids_differ"
USER_ID_INDEX = "ix_identity_aliases_user_id"


class IdentityAlias(Base):
    """One anonymous id, and the known id it is linked to."""

    __tablename__ = "identity_aliases"

    anonymous_id = Column(String(255), primary_key=True)
    user_id = Column(String(255), nullable=False)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    @declared_attr
    def __table_args__(cls):
        return (
            CheckConstraint("anonymous_id <> user_id", name=IDS_DIFFER_CHECK),
            Index(USER_ID_INDEX, "user_id"),
            {"schema": get_schema_name()},
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<IdentityAlias>"
