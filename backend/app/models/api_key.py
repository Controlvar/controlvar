import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    String,
    Text,
    or_,
    update,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.declarative import declared_attr
from sqlalchemy.orm import relationship

from backend.app.core.api_key_scopes import SCOPES_MAX_LENGTH
from backend.app.core.database_config import get_schema_name

from .base import Base, BaseModel

# last_used_at is written at most once per this window per key (issue #198).
LAST_USED_RESOLUTION = timedelta(seconds=60)


def generate_api_key() -> str:
    """Generate a random API key with a prefix."""
    prefix = "eptk"  # Short for "experimentation toolkit"
    random_part = secrets.token_hex(16)
    return f"{prefix}_{random_part}"


class APIKey(Base, BaseModel):
    """API Key model for API authentication."""

    __tablename__ = "api_keys"

    # Stores SHA-256 hash of the API key (never plaintext).
    key = Column(String(64), unique=True, nullable=False, index=True)
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    expires_at = Column(DateTime, nullable=True)
    last_used_at = Column(DateTime, nullable=True)

    # Comma-separated scope names, read with backend.app.core.api_key_scopes.
    # sdk:ruleset (backend/app/api/sdk_scope.py) and mcp:read
    # (backend/app/api/read_scope.py) are enforced; other names are labels.
    # The create request refuses a set whose joined form is longer (#1936).
    scopes = Column(String(SCOPES_MAX_LENGTH), nullable=True)

    # User relationship
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey(f"{get_schema_name()}.users.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Relationship with explicit back_populates
    user = relationship("User", back_populates="api_keys")

    @declared_attr
    def __table_args__(cls):
        return ({"schema": get_schema_name()},)

    def __repr__(self):
        return f"<APIKey {self.name}>"

    @property
    def is_expired(self) -> bool:
        """Check if the API key is expired."""
        if not self.expires_at:
            return False
        return self.expires_at < datetime.utcnow()

    @property
    def is_valid(self) -> bool:
        """Check if the API key is valid (active and not expired)."""
        return self.is_active and not self.is_expired

    def update_last_used(self, db_session, now: Optional[datetime] = None) -> bool:
        """Record that the key was just used, at most once per resolution window.

        ``get_api_key`` calls this on every authenticated SDK request, which is
        the hot path (``SDK_RATE_LIMIT_PER_MINUTE`` defaults to 6000 per IP).
        Writing on every request would turn a read path into a write path and
        make concurrent requests on one key queue on its row lock. So the write
        happens only when the stored value is null or older than
        ``LAST_USED_RESOLUTION``: at most about one UPDATE per key per minute,
        which is still precise enough for what the field is for -- finding
        unused keys and confirming a rotation.

        The UPDATE repeats the staleness test in its WHERE clause, so workers
        that race past the in-memory check change nothing. It pins
        ``updated_at`` to itself: using a key is not editing it.

        Returns True when a row was written. The caller owns error handling.
        """
        if now is None:
            # Naive UTC, like expires_at and is_expired.
            now = datetime.now(timezone.utc).replace(tzinfo=None)
        cutoff = now - LAST_USED_RESOLUTION
        if self.last_used_at is not None and self.last_used_at > cutoff:
            return False

        cls = type(self)
        result = db_session.execute(
            update(cls)
            .where(
                cls.id == self.id,
                or_(cls.last_used_at.is_(None), cls.last_used_at <= cutoff),
            )
            .values(last_used_at=now, updated_at=cls.updated_at)
            .execution_options(synchronize_session=False)
        )
        db_session.commit()
        return bool(result.rowcount)

    @classmethod
    def create_for_user(
        cls, db_session, user_id, name, description=None, scopes=None, expires_at=None
    ):
        """Create a new API key for a user and return (model, plaintext_key)."""
        from backend.app.core.security import hash_api_key

        plaintext_key = generate_api_key()
        key_hash = hash_api_key(plaintext_key)
        api_key = cls(
            user_id=user_id,
            key=key_hash,
            name=name,
            description=description,
            scopes=scopes,
            expires_at=expires_at,
        )
        db_session.add(api_key)
        db_session.commit()
        db_session.refresh(api_key)
        return api_key, plaintext_key
