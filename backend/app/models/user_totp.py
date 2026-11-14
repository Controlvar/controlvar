"""An account's two-step sign-in: its authenticator secret and recovery codes (#1812).

One row per local account that has started setting two-step sign-in up.

* ``secret_enc``: the authenticator secret, sealed with a key derived from
  ``SECRET_KEY`` (``backend/app/core/two_factor_keys.py``), never stored in
  the clear.
* ``confirmed_at``: NULL while the account has asked for a secret but not yet
  proved it with a code. A row with NULL here changes nothing at sign-in.
* ``last_used_step``: the newest 30-second step a code was accepted for. A
  code is accepted only for a later step, so each code is used once.
  ``NOT NULL DEFAULT 0``: every step since 1970 is greater.
* ``failed_attempts``: wrong codes and recovery codes since the last accepted
  one. At ``CODE_STEP_CAP`` (``two_factor_service``) the code step refuses
  every code, a right one too, until the row is reset.
* ``recovery_hashes``: a JSON array of bcrypt hashes, one per unused recovery
  code. Using one removes its hash.

Deleting the user deletes the row (``ON DELETE CASCADE``). Deleting the row
turns two-step sign-in off for that account.
"""

from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.declarative import declared_attr

from backend.app.core.database_config import get_schema_name

from .base import Base


class UserTOTP(Base):
    """One account's two-step sign-in."""

    __tablename__ = "user_totp"

    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey(f"{get_schema_name()}.users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    secret_enc = Column(Text, nullable=False)
    confirmed_at = Column(DateTime(timezone=True), nullable=True)
    last_used_step = Column(BigInteger, nullable=False, server_default=text("0"))
    failed_attempts = Column(Integer, nullable=False, server_default=text("0"))
    recovery_hashes = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    @declared_attr
    def __table_args__(cls):
        return ({"schema": get_schema_name()},)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        state = "confirmed" if self.confirmed_at is not None else "pending"
        return f"<UserTOTP {self.user_id} {state}>"
