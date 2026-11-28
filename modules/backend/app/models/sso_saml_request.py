"""A SAML sign-in the API has started and not finished (#123).

One row per request sent to an identity provider:

* ``id``: the request's id, the primary key;
* ``config_id``: the single sign-on configuration it started from. Not a
  foreign key, so creating the table takes no lock on ``sso_configs``;
* ``return_to``: where the sign-in returns;
* ``handoff_hash``: the hash of the hand-off secret the starting browser holds;
* ``created_at``: when it started, by the database's clock;
* ``expires_at``: when it stops being accepted;
* ``used_at``: when it completed; empty until then.

``ix_sso_saml_requests_expires_at`` finds the expired rows. Nothing reads or
writes the table yet.

Revision ``modules_0006_saml_requests`` creates the table with these names.
Downgrading below that revision drops the table and every row in it.
"""

from sqlalchemy import CHAR, Column, DateTime, Index, String, text
from sqlalchemy.dialects.postgresql import UUID

from backend.app.core.database_config import get_schema_name
from backend.app.models.base import Base

#: The index name. The revision spells the same name as its own literal: a
#: migration imports nothing from the application.
EXPIRES_AT_INDEX = "ix_sso_saml_requests_expires_at"


class SSOSAMLRequest(Base):
    """One SAML sign-in in progress."""

    __tablename__ = "sso_saml_requests"
    __table_args__ = (
        Index(EXPIRES_AT_INDEX, "expires_at"),
        {"schema": get_schema_name()},
    )

    id = Column(String(64), primary_key=True)
    config_id = Column(UUID(as_uuid=True), nullable=False)
    return_to = Column(String(512), nullable=False)
    handoff_hash = Column(CHAR(43), nullable=False)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used_at = Column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<SSOSAMLRequest>"
