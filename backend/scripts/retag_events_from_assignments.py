"""
Give an experiment's events the variant each user is assigned to, where the
tracking API stored them with none because the experiment was not running.

``POST /api/v1/tracking/track`` and ``/tracking/batch`` tag an event with the
user's variant only while its experiment is ACTIVE. An event received while
the experiment is a draft, paused, completed or archived keeps its
``experiment_id`` and has no ``variant_id``, so it counts in no variant's
results. For an experiment paused by mistake, this tags those events as the
routes tagged them before they read the experiment's status: each event of
the experiment with no variant, from a user who already had an assignment to
the experiment when the event was received, gets that assignment's variant::

    python -m backend.scripts.retag_events_from_assignments EXPERIMENT_ID \\
        --received-from 2026-10-01T09:00:00Z --received-until 2026-10-01T17:00:00Z

Without ``--apply`` it changes nothing and prints how many events it would
tag; with ``--apply`` it tags them in one transaction and prints how many it
tagged.

* The receive time is ``events.updated_at``, written once when the event is
  stored. ``--received-from`` (inclusive) and ``--received-until``
  (exclusive) bound it, so only the window the experiment was not running is
  tagged; a time with no offset is UTC.
* Without the bounds every such event of the experiment is tagged, including
  one sent through ``POST /api/v1/tracking/events`` with an ``experiment_id``
  and no ``variant_id``, which cannot be told apart from the others.
* An event from a user with no assignment to the experiment, or received
  before the user was assigned, is left as it is: the routes never tagged
  those. ``events.updated_at`` is left as it is too.
* A tagged event counts in its variant's results from the next computation.
  Results are cached (24 hours for a paused or completed experiment):
  ``GET /api/v1/results/{id}?use_cache=false`` computes them afresh.

Connection settings are the ``POSTGRES_*`` environment variables that
``backend.app.db.bootstrap`` reads. Exit status: 0 on success, 2 when the
database cannot be read or written.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, List, Optional, Sequence

from sqlalchemy import and_, func, select, update
from sqlalchemy.orm import Session

from backend.app.models.assignment import Assignment
from backend.app.models.event import Event


def _naive_utc(value: Optional[datetime]) -> Optional[datetime]:
    """``value`` as naive UTC, the form ``events.updated_at`` is stored in."""
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _criteria(
    experiment_id: Any,
    received_from: Optional[datetime],
    received_until: Optional[datetime],
) -> List[Any]:
    """The events to tag, joined to the assignment that tags each one."""
    criteria = [
        Event.experiment_id == experiment_id,
        Event.variant_id.is_(None),
        Assignment.experiment_id == Event.experiment_id,
        Assignment.user_id == Event.user_id,
        # The user was assigned when the event was received.
        Assignment.created_at <= Event.updated_at,
    ]
    if received_from is not None:
        criteria.append(Event.updated_at >= _naive_utc(received_from))
    if received_until is not None:
        criteria.append(Event.updated_at < _naive_utc(received_until))
    return criteria


def retag_events_from_assignments(
    db: Session,
    experiment_id: Any,
    *,
    received_from: Optional[datetime] = None,
    received_until: Optional[datetime] = None,
    apply: bool = False,
) -> int:
    """
    Tag the experiment's untagged events from its assignments.

    Returns how many events are tagged (``apply=True``, committed) or would
    be (``apply=False``, nothing written). ``(experiment_id, user_id)`` is
    unique on ``assignments``, so each event has at most one variant to take.
    """
    criteria = _criteria(experiment_id, received_from, received_until)
    if not apply:
        return int(
            db.execute(select(func.count(Event.id)).where(and_(*criteria))).scalar()
            or 0
        )
    result = db.execute(
        update(Event)
        .where(and_(*criteria))
        .values(variant_id=Assignment.variant_id)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return int(result.rowcount or 0)


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an ISO 8601 time: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m backend.scripts.retag_events_from_assignments",
        description=(
            "Tag an experiment's events that have no variant with the variant "
            "each user was assigned when the event was received."
        ),
    )
    parser.add_argument("experiment_id", type=uuid.UUID, help="the experiment's id")
    parser.add_argument(
        "--received-from",
        type=_timestamp,
        help="tag events received at or after this time (ISO 8601; UTC if no offset)",
    )
    parser.add_argument(
        "--received-until",
        type=_timestamp,
        help="tag events received before this time (ISO 8601; UTC if no offset)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the tags; without it, only count the events",
    )
    args = parser.parse_args(argv)

    from sqlalchemy import create_engine

    from backend.app.db.bootstrap import database_url

    engine = create_engine(database_url())
    try:
        with Session(engine) as db:
            count = retag_events_from_assignments(
                db,
                args.experiment_id,
                received_from=args.received_from,
                received_until=args.received_until,
                apply=args.apply,
            )
    except Exception as exc:
        print(
            "retag_events_from_assignments: could not read or update the "
            f"database ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 2
    finally:
        engine.dispose()

    verb = "tagged" if args.apply else "would tag (run again with --apply)"
    print(f"{verb}: {count} event(s) of experiment {args.experiment_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
