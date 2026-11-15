"""The child of ``test_balance_across_processes.py``: one API process (#448).

Run by ``multiprocessing``'s spawn start method, so it shares nothing with
the test's process but the database: its own interpreter, its own engine and
its own copy of every module. A ``monkeypatch`` in the parent does not reach
it, which is why it raises the counter step's timeout itself.

Not a test module (no ``test_`` prefix): pytest does not collect it.
"""

from __future__ import annotations

import time
from typing import List, Tuple

#: The counter step's timeout in the children, in milliseconds: long enough
#: that a step waiting behind the other processes' held requests never falls
#: back to the hash, so every user must be in the blocks.
CHILD_STEP_TIMEOUT_MS = 30_000


def run_child(
    db_url: str,
    schema: str,
    experiment_id: str,
    users: List[Tuple[str, str]],
    barrier,
    hold_ms: int,
) -> None:
    """Assign *users* (``(user_id, group)``) to the experiment, one by one,
    after every child has reached *barrier*. Each request is held *hold_ms*
    between its allocation and its commit. Raises (a non-zero exit code) on
    any failure."""
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    from backend.app.services import balanced_allocation
    from backend.app.services.assignment_service import AssignmentService

    balanced_allocation.BALANCE_STEP_TIMEOUT_MS = CHILD_STEP_TIMEOUT_MS

    real = AssignmentService._balanced_variant

    def held(self, *args, **kwargs):
        answer = real(self, *args, **kwargs)
        if hold_ms:
            time.sleep(hold_ms / 1000)
        return answer

    AssignmentService._balanced_variant = held

    engine = create_engine(db_url, poolclass=NullPool)

    @event.listens_for(engine, "connect")
    def _search_path(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute(f"SET search_path TO {schema}")
        cursor.close()

    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    barrier.wait(timeout=60)
    for user_id, group in users:
        session = factory()
        try:
            answer = AssignmentService(session).assign_user(
                user_id=user_id,
                experiment_id=experiment_id,
                track_exposure=False,
                context={"tier": group},
            )
            if answer.get("assigned") is not True:
                raise RuntimeError(f"{user_id} was not assigned: {answer}")
        finally:
            session.close()
    engine.dispose()
