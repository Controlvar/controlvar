"""The counter step of an experiment that balances within groups (#448).

A new, eligible user of a balanced experiment takes the next place of their
group's blocks (``backend.app.core.balanced_blocks``: the group, the block and
which variant each place holds). The place, the group's *seq*, comes from the
row of ``balance_group_counters`` for the experiment and the group, taken in a
SAVEPOINT of the assignment's own transaction (:func:`take_seq`): one
``INSERT ... ON CONFLICT DO UPDATE ... RETURNING``, under a
``statement_timeout`` of :data:`BALANCE_STEP_TIMEOUT_MS`. The increment commits
with the assignment row, so a rolled-back assignment gives its place back and
the seqs of a group stay exactly ``0 .. next_seq - 1``.

When no seq can be had -- the step waited longer than the timeout, the
process already has :data:`MAX_BALANCED_WAITS` steps in progress, or the
experiment has no ``balance_seed`` -- the user is assigned by the hash, as an
experiment that balances nothing assigns them, and stored with their group and
no seq. Such a user is counted in ``experiment_balance_fallback_total`` by
reason (:func:`note_fallback`) and is outside the blocks.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Iterable, Optional, Tuple

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from backend.app.core.log_once import OnceLog
from backend.app.core.metrics import (
    experiment_balance_fallback_total,
    experiment_balance_waits,
)
from backend.app.models.balance_group_counter import BalanceGroupCounter

logger = logging.getLogger(__name__)

#: Counter steps a process runs at once; past it a user is assigned by the
#: hash at once (reason ``busy``).
MAX_BALANCED_WAITS = 4
#: The ``statement_timeout`` of the counter step, in milliseconds. Read on
#: every call, so a test may raise it inside its own process.
BALANCE_STEP_TIMEOUT_MS = 250

#: The reasons a user is assigned outside the blocks.
REASON_TIMEOUT = "timeout"
REASON_BUSY = "busy"
REASON_NO_SEED = "no_seed"
FALLBACK_REASONS = (REASON_TIMEOUT, REASON_BUSY, REASON_NO_SEED)

#: SQLSTATEs of a counter step that waited too long: the statement timeout
#: this step sets (57014), and a ``lock_timeout`` set elsewhere (55P03).
_STEP_TIMED_OUT = frozenset({"57014", "55P03"})

_permits = threading.BoundedSemaphore(MAX_BALANCED_WAITS)
_waits_lock = threading.Lock()
_waits = {"current": 0, "peak": 0}
_fallback_notes = OnceLog()
_undecided_notes = OnceLog()


def _sqlstate(exc: BaseException) -> Optional[str]:
    orig = getattr(exc, "orig", None)
    return getattr(orig, "pgcode", None) or getattr(orig, "sqlstate", None)


def _wait_started() -> None:
    with _waits_lock:
        _waits["current"] += 1
        _waits["peak"] = max(_waits["peak"], _waits["current"])
        experiment_balance_waits.set(_waits["current"])


def _wait_ended() -> None:
    with _waits_lock:
        _waits["current"] -= 1
        experiment_balance_waits.set(_waits["current"])


def balanced_waits() -> Dict[str, int]:
    """The counter steps in progress in this process now, and the most at
    once since :func:`reset_balanced_wait_peak`."""
    with _waits_lock:
        return dict(_waits)


def reset_balanced_wait_peak() -> None:
    """Start counting the most steps at once from the steps in progress now."""
    with _waits_lock:
        _waits["peak"] = _waits["current"]


def take_seq(
    db: Session, experiment_id: Any, group: str
) -> Tuple[Optional[int], Optional[str]]:
    """The group's next seq, or why there is none: ``(seq, None)`` or
    ``(None, reason)``.

    Runs in the caller's transaction, which commits the increment with the
    assignment row (and rolls it back with it). The process's permit is taken
    before the statement, without waiting: none free is ``busy``. In a
    SAVEPOINT, the ``statement_timeout`` is set for the upsert alone; a step
    that runs past it (or past a ``lock_timeout`` set elsewhere) is rolled
    back to the savepoint, which also undoes the setting and leaves no row,
    and is ``timeout``. Any other database error propagates.
    """
    if not _permits.acquire(blocking=False):
        return None, REASON_BUSY
    _wait_started()
    try:
        counter = BalanceGroupCounter.__table__
        upsert = (
            pg_insert(counter)
            .values(experiment_id=experiment_id, balance_group=group, next_seq=1)
            .on_conflict_do_update(
                index_elements=[counter.c.experiment_id, counter.c.balance_group],
                set_={"next_seq": counter.c.next_seq + 1},
            )
            .returning(counter.c.next_seq - 1)
        )
        try:
            with db.begin_nested():
                # set_config(..., true) is SET LOCAL, with the value bound
                # rather than written into the statement.
                db.execute(
                    select(
                        func.set_config(
                            "statement_timeout",
                            str(int(BALANCE_STEP_TIMEOUT_MS)),
                            True,
                        )
                    )
                )
                seq = db.execute(upsert).scalar_one()
                db.execute(text("SET LOCAL statement_timeout TO DEFAULT"))
        except OperationalError as exc:
            if _sqlstate(exc) in _STEP_TIMED_OUT:
                return None, REASON_TIMEOUT
            raise
        return int(seq), None
    finally:
        _wait_ended()
        _permits.release()


def note_undecided_segments(experiment_id: Any, segment_ids: Iterable[str]) -> None:
    """Log once per process per experiment that a new user was balanced in
    ``unknown`` because membership of a segment it balances within could not
    be decided (the experiment and the segment ids only: never a user)."""
    if _undecided_notes.first(str(experiment_id), "undecided segments"):
        logger.warning(
            "Experiment %s: membership of a segment it balances within could not "
            "be decided (segments: %s), so a new user was balanced in unknown. "
            "Logged once.",
            experiment_id,
            ", ".join(sorted(str(s) for s in segment_ids)),
        )


def note_fallback(experiment_id: Any, reason: str) -> None:
    """Count a user assigned outside the blocks, and log it once per process
    per experiment (the experiment id only: never a user or a value)."""
    experiment_balance_fallback_total.labels(reason=reason).inc()
    if _fallback_notes.first(str(experiment_id), "balance fallback"):
        logger.warning(
            "Experiment %s: a new user was assigned by hash instead of by its "
            "group's block (%s); experiment_balance_fallback_total counts "
            "every such user by reason. Logged once.",
            experiment_id,
            reason,
        )
