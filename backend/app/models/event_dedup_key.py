"""The events stored once by their ``event_id`` (#1543).

One row per event that was stored with an ``event_id``: the user, the key
``backend/app/services/event_dedup.py`` computed from the event's experiment,
flag, event name and ``event_id``, and the id of the event that claimed it.
``EventService.track_event_once`` claims a key with ``INSERT ... ON CONFLICT
DO NOTHING RETURNING`` in the same transaction as the event's own insert, so
of two requests carrying the same key exactly one stores its event.

There is deliberately no foreign key to ``events``: creating one takes a lock
on ``events`` that waits for every open transaction writing to it. A key is
kept when its event is deleted. It then names an experiment or flag id that
nothing resolves to again, so it never matches a new event, and nothing purges
keys. An operator erasing one person's events by hand deletes their rows here
too (``user_id``).
"""

from sqlalchemy import Column, DateTime, String, text
from sqlalchemy.dialects.postgresql import UUID

from backend.app.models.base import Base


class EventDedupKey(Base):
    """An ``event_id`` that has been stored, for one user."""

    __tablename__ = "event_dedup_keys"

    #: The same type and length as ``events.user_id``.
    user_id = Column(String(255), primary_key=True)
    #: ``event_dedup.dedup_key(...)``: 64 lowercase hex characters.
    dedup_key = Column(String(64), primary_key=True)
    #: The ``events.id`` of the event that claimed the key.
    stored_event_id = Column(UUID(as_uuid=True), nullable=False)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<EventDedupKey {self.stored_event_id}>"
