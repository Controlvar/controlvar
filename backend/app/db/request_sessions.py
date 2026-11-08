"""The database sessions of the request being handled.

Both ``get_db`` dependencies (``backend.app.api.deps.get_db`` and
``backend.app.db.session.get_db``) register the session they yield here, and
unregister it in their ``finally``. ``backend.app.api.off_loop_route`` reads
the registry to end each session's transaction before a synchronous handler
is sent to a worker thread.

The registry is a context variable holding an immutable tuple. A request's
dependencies and its handler run in the same context, so the handler sees
what its ``get_db`` registered; concurrent requests run in separate contexts
and never see each other's sessions. Each ``get_db`` removes its own session
when it finishes, so a context that serves several requests one after another
(an in-process test client does) starts each one with none.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Tuple

from sqlalchemy.orm import Session

_request_sessions: ContextVar[Tuple[Session, ...]] = ContextVar(
    "request_sessions", default=()
)


def register(session: Session) -> None:
    """Add ``session`` to the current request's sessions."""
    _request_sessions.set(_request_sessions.get() + (session,))


def unregister(session: Session) -> None:
    """Remove ``session`` from the current request's sessions."""
    _request_sessions.set(tuple(s for s in _request_sessions.get() if s is not session))


def current() -> Tuple[Session, ...]:
    """The sessions registered by the request being handled."""
    return _request_sessions.get()
