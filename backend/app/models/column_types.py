# backend/app/models/column_types.py
"""Column types shared by more than one model module.

No mapped class lives here, so ``CORE_MODEL_MODULES`` does not list it.
"""

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import DateTime
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator):
    """A ``timestamp without time zone`` column whose bound values are UTC.

    The columns that use it hold UTC with no zone, but the API binds them in
    three shapes: an aware datetime (a request's dates, or the time a
    scheduler or a route records), an ISO string and a naive one. PostgreSQL
    converts an aware value to the session's time zone on the way into such a
    column and ignores the offset in a string, so on a session whose time zone
    is not UTC the stored value moved by the offset.

    - ``experiments.start_date`` and ``end_date``: west of UTC an end date
      landed before its start and completing answered 500 (#704).
    - ``rollout_stages.start_date``, ``completed_date`` and ``updated_at``:
      a stage date sent with ``Z`` or an offset was stored and served back
      shifted, so time-based stages started early west of UTC and late east
      of it; the scheduler measures a stage's duration from ``updated_at``
      (#1462).

    Every bound value is converted here to naive UTC, the stored form,
    whatever the session's zone; a naive value is taken to be UTC already. An
    ISO string is parsed with ``datetime.fromisoformat``. The database type is
    unchanged (no migration), and values read back are returned as stored.
    This applies to ORM and Core statements that bind through the column, not
    to raw ``text()``. ``UTCTimestampString`` in ``models/event.py`` does the
    same for ``events.created_at``.
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
