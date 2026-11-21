"""The visual editor's one-time tickets, sessions and preview links (#1545).

One row per credential. Only the SHA-256 of a token is stored
(``core.security.hash_api_key``), never the token. ``kind`` says what the row
is. The editor's sign-in is a chain, ticket -> claim -> session, linked
through ``parent_id``: a claim points at the ticket it spent, and a session at
the claim it was exchanged for. A row is never reused: a ticket or a claim is
spent by setting ``used_at``, and any credential is ended by setting
``revoked_at`` with the reason in ``revoked_reason``.

The two checks below admit every kind and reason the design names, including
``scheduled``, which no code writes yet, so it needs no second migration.
They are plain ``VARCHAR`` columns with a check, not a native enum: a new
value is a new check, never an ``ALTER TYPE``.

Starting an experiment, by ``POST /start`` or by the scheduler, revokes every
editor ticket, session and claim of that experiment (``revoked_reason``
``started``); preview links are left alone.
"""

import uuid

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.declarative import declared_attr

from backend.app.core.database_config import get_schema_name

from .base import Base

#: What a row is.
EDITOR_TICKET = "editor_ticket"
EDITOR_SESSION = "editor_session"
EDITOR_CLAIM = "editor_claim"
PREVIEW = "preview"
VISUAL_TOKEN_KINDS = (EDITOR_TICKET, EDITOR_SESSION, EDITOR_CLAIM, PREVIEW)
#: The kinds that sign someone in to the editor: a start revokes these.
EDITOR_KINDS = (EDITOR_TICKET, EDITOR_SESSION, EDITOR_CLAIM)

#: Why a credential was ended: the editor was opened again for the
#: experiment; the experiment was started; a spent ticket or claim was
#: presented again; the editor signed out; a start date was set.
REOPENED = "reopened"
STARTED = "started"
TICKET_REUSED = "ticket_reused"
SIGNED_OUT = "signed_out"
SCHEDULED = "scheduled"
VISUAL_TOKEN_REVOKED_REASONS = (
    REOPENED,
    STARTED,
    TICKET_REUSED,
    SIGNED_OUT,
    SCHEDULED,
)

#: The checks: the same names and SQL as migration ``4955571d232c``.
VISUAL_TOKEN_KIND_CHECK = "ck_visual_tokens_kind"
VISUAL_TOKEN_KIND_CHECK_SQL = (
    "kind IN ('editor_ticket', 'editor_session', 'editor_claim', 'preview')"
)
VISUAL_TOKEN_REASON_CHECK = "ck_visual_tokens_revoked_reason"
VISUAL_TOKEN_REASON_CHECK_SQL = (
    "revoked_reason IS NULL OR revoked_reason IN "
    "('reopened', 'started', 'ticket_reused', 'signed_out', 'scheduled')"
)


class VisualToken(Base):
    """A ticket, session, claim or preview link of one visual experiment."""

    __tablename__ = "visual_tokens"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kind = Column(String(20), nullable=False)
    #: ``hash_api_key(token)``: 64 lowercase hex characters.
    token_hash = Column(String(64), nullable=False, unique=True)
    experiment_id = Column(
        UUID(as_uuid=True),
        ForeignKey(f"{get_schema_name()}.experiments.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: A preview link names its variant, not a foreign key: a variants PUT
    #: re-creates the rows, and the link must survive it.
    variant_name = Column(String(100), nullable=True)
    #: Who minted it. Deleting the user deletes their credentials.
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey(f"{get_schema_name()}.users.id", ondelete="CASCADE"),
        nullable=True,
    )
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    revoked_reason = Column(String(20), nullable=True)
    #: A claim's ticket, or a session's claim.
    parent_id = Column(
        UUID(as_uuid=True),
        ForeignKey(f"{get_schema_name()}.visual_tokens.id", ondelete="CASCADE"),
        nullable=True,
    )

    @declared_attr
    def __table_args__(cls):
        schema_name = get_schema_name()
        return (
            CheckConstraint(VISUAL_TOKEN_KIND_CHECK_SQL, name=VISUAL_TOKEN_KIND_CHECK),
            CheckConstraint(
                VISUAL_TOKEN_REASON_CHECK_SQL, name=VISUAL_TOKEN_REASON_CHECK
            ),
            Index("ix_visual_tokens_experiment_id", "experiment_id"),
            Index("ix_visual_tokens_parent_id", "parent_id"),
            {"schema": schema_name},
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<VisualToken {self.kind} {self.id}>"
