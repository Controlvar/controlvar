"""
What the balanced-groups results read from ``assignments`` (#448).

``GET /api/v1/results/{experiment_id}/balanced-groups`` lists an experiment
that balances its split within groups by the group each user was balanced in
when they were assigned, the group ``assign_user`` stored in
``assignments.balance_group``. Its rows are built by
``segment_breakdown.segment_rows``, as every breakdown's are, with
``stored_strata`` as the value source in place of ``user_values``: the same
fold, order, reserved rows and counts, so there is one way to build per-group
rows.

Both functions here read ``assignments`` only, never ``events`` (no statement
reads both tables, as ``event_matching._assigned_pairs`` explains, #233).
"""

from typing import Any, Dict

from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.app.models.assignment import Assignment


def stored_strata(db: Session, experiment_id: Any, dimension: str) -> Dict[str, str]:
    """
    Each assigned user's stored group: a ``segment_breakdown.ValueSource``.

    Maps the user id to ``assignments.balance_group`` for every assignment of
    the experiment that stored one. It selects that column and the user id
    only, from ``assignments`` only. The column is ``VARCHAR(100)``, so no
    value is longer than 100 characters, within ``segment_breakdown``'s
    ``MAX_VALUE_LENGTH``. A user whose row stored no group (an older server
    version assigned them) is absent, so ``segment_rows`` counts them under
    ``unknown``.

    ``dimension`` is not read: the group was decided by the experiment's
    ``balance_within`` when the user was assigned.
    """
    rows = (
        db.query(Assignment.user_id, Assignment.balance_group)
        .filter(
            Assignment.experiment_id == experiment_id,
            Assignment.balance_group.isnot(None),
        )
        .all()
    )
    return {str(user_id): group for user_id, group in rows}


def users_outside_blocks(db: Session, experiment_id: Any) -> int:
    """
    How many of the experiment's users were split without balancing.

    The assignments with no place in their group (``balance_seq`` NULL):
    users assigned by the hash because the server was busy balancing other
    users at that moment, and users an older server version assigned, whose
    row stored no group either.
    """
    return int(
        db.query(func.count(Assignment.id))
        .filter(
            Assignment.experiment_id == experiment_id,
            Assignment.balance_seq.is_(None),
        )
        .scalar()
        or 0
    )
