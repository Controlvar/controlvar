"""
Shared rules for recognising conversion events.

Producers disagree on ``Event.event_type``:

* ``EventService.track_conversion`` writes ``event_type="conversion"`` and
  ``event_name=<metric event name>``.
* The public tracking API (``POST /api/v1/tracking/track``/``batch``) and every
  SDK write the event's own name in both columns (``event_type="purchase"``,
  ``event_name="purchase"``) — ``event_type`` is free text there.

A metric is therefore matched on ``event_name`` only; ``event_type`` is used
just to exclude exposure rows.  Every analysis path (results engine, streaming
results, bandit scheduler, segment breakdowns) must use these helpers so the
same event is counted the same way everywhere.

Every reader of a metric's conversions, counts or values takes the metric's
``MetricEvents`` (``metric_events(metric)``), never its bare event name, and
selects them with ``metric_event_filter`` or ``any_metric_event_filter``.
``None`` in its place is the legacy convention below.  A bare name raises
``TypeError``.  ``conversion_event_filter`` is the name rule those two apply;
it is called with a name only where no metric is read (holdout results, the
CUPED covariate).

``results_batch`` reads the conversions of several metrics in one statement:
it selects them with ``any_metric_event_filter`` and
``conversion_event_filter(None)``, and tells the metrics apart by keying each
row by the ``MetricEvents`` whose ``event_name`` it carries.  That key is
``conversion_event_filter``'s name equality written a second time, so a change
to how a name matches is made in both.

A binary conversion metric counts **converting users**, not conversion events:
a user assigned to a variant who sent one or more matching conversion events
counts once (#233).  ``count_converting_users`` is that one definition, and
every analysis path that reports ``conversions`` next to a ``sample_size`` of
assigned users must use it, so the numerator can never exceed the denominator.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

from sqlalchemy import String, and_, any_, bindparam, func
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Session

from backend.app.models.assignment import Assignment
from backend.app.models.event import Event, EventType

#: ``event_type`` values that record that a user *saw* a variant, never a conversion.
EXPOSURE_EVENT_TYPES = (EventType.EXPOSURE.value, "experiment_exposure")


@dataclass(frozen=True)
class MetricEvents:
    """
    The events one metric counts: its event name and its conversion window.

    Every reader of a metric's conversions, counts or values takes one of
    these in place of the metric's event name, or ``None`` for the legacy
    ``event_type = "conversion"`` convention (an experiment with no metric, or
    a metric with no event name).  It is hashable, so a result keyed by it
    keeps two metrics on one event apart once they can differ.
    ``metric_events`` builds it from a ``Metric`` row.

    ``window_hours`` is always ``None``: metrics have no conversion window yet
    (#1934).  A window is refused here, so no reader can count without one it
    was given.
    """

    event_name: str
    window_hours: Optional[int]

    def __post_init__(self) -> None:
        if not isinstance(self.event_name, str) or not self.event_name:
            raise TypeError(
                "MetricEvents needs a non-empty event name; "
                "None stands for the legacy events"
            )
        if self.window_hours is not None:
            raise NotImplementedError(
                "conversion windows are not read yet: window_hours must be None"
            )


def metric_events(metric: Any) -> Optional[MetricEvents]:
    """
    The events ``metric`` counts, or ``None`` when it names no event.

    ``None`` (also for ``metric=None``) is the legacy ``event_type =
    "conversion"`` convention, which ``conversion_event_filter(None)`` and
    ``conversion_event_filter("")`` both select: an experiment's results read
    it for a metric with an empty event name, and for no metric at all.
    """
    name = getattr(metric, "event_name", None) if metric is not None else None
    if not name:
        return None
    return MetricEvents(name, None)


def metric_event_name(events: Optional[MetricEvents]) -> Optional[str]:
    """
    The event name ``events`` reads, or ``None`` for the legacy events.

    Raises ``TypeError`` for anything but a ``MetricEvents`` or ``None``, a
    bare event name included: a reader that is still handed a name is a caller
    that was not moved to ``metric_events``, and it fails here rather than
    answering without the metric's window.
    """
    if events is None:
        return None
    if not isinstance(events, MetricEvents):
        raise TypeError(
            "a metric's events are read through metric_events(metric), "
            f"not a {type(events).__name__}"
        )
    return events.event_name


def conversion_event_filter(event_name: Optional[str]):
    """
    SQLAlchemy criterion selecting the conversion events of one metric.

    With a metric ``event_name`` the criterion is
    ``Event.event_name == event_name AND event_type not an exposure``.
    Without one (experiment has no metric rows) it falls back to the legacy
    ``event_type == "conversion"`` convention.
    """
    if not event_name:
        return Event.event_type == EventType.CONVERSION.value
    return and_(
        Event.event_name == event_name,
        Event.event_type.notin_(EXPOSURE_EVENT_TYPES),
    )


def any_conversion_event_filter(event_names: Iterable[Optional[str]]):
    """
    Criterion selecting conversion events for *any* of the given metric names.

    Used for experiment-wide totals.  Falls back to the legacy convention when
    the list is empty.
    """
    names = [name for name in event_names if name]
    if not names:
        return Event.event_type == EventType.CONVERSION.value
    return and_(
        Event.event_name.in_(names),
        Event.event_type.notin_(EXPOSURE_EVENT_TYPES),
    )


def metric_event_filter(events: Optional[MetricEvents]):
    """``conversion_event_filter`` for one metric's ``MetricEvents``."""
    return conversion_event_filter(metric_event_name(events))


def any_metric_event_filter(events: Iterable[Optional[MetricEvents]]):
    """``any_conversion_event_filter`` for several metrics' ``MetricEvents``."""
    return any_conversion_event_filter([metric_event_name(e) for e in events])


#: How many user ids one assignment lookup binds as a single array.
ASSIGNMENT_LOOKUP_CHUNK = 10_000


def _assigned_pairs(
    db: Session, experiment_id: Any, user_ids: Iterable[str]
) -> Set[Tuple[str, str]]:
    """
    ``(variant_id, user_id)`` of the assignments these users have in the experiment.

    The conversion counts below never join ``events`` to ``assignments`` in
    SQL.  Without planner statistics -- a freshly seeded database, before
    autovacuum has analysed it -- PostgreSQL can run that join as a nested
    loop over a sequential scan, which took more than 8 s on 30,000 users in
    CI and blocked the live-results stream (#233).  Instead the converting
    users are read from ``events`` alone, and their assignments are looked up
    here by the unique ``(experiment_id, user_id)`` index, a bounded number of
    ids at a time.
    """
    ids = list(user_ids)
    pairs: Set[Tuple[str, str]] = set()
    for i in range(0, len(ids), ASSIGNMENT_LOOKUP_CHUNK):
        chunk = ids[i : i + ASSIGNMENT_LOOKUP_CHUNK]
        rows = (
            db.query(Assignment.variant_id, Assignment.user_id)
            .filter(
                Assignment.experiment_id == experiment_id,
                Assignment.user_id
                == any_(bindparam("user_ids", chunk, type_=ARRAY(String))),
            )
            .all()
        )
        pairs.update((str(variant_id), str(user_id)) for variant_id, user_id in rows)
    return pairs


def _converters(
    db: Session, experiment_id: Any, variant_id: Any, criterion: Any
) -> List[Tuple[str, Any]]:
    """``(user_id, first matching created_at)`` per user with an event matching
    ``criterion`` tagged with ``variant_id``; read from ``events`` alone."""
    rows = (
        db.query(Event.user_id, func.min(Event.created_at))
        .filter(
            Event.experiment_id == experiment_id,
            Event.variant_id == variant_id,
            criterion,
        )
        .group_by(Event.user_id)
        .all()
    )
    return [(str(user_id), first) for user_id, first in rows]


def _assigned_matching(
    db: Session, experiment_id: Any, variant_id: Any, criterion: Any
) -> List[Tuple[str, Any]]:
    """The users of ``variant_id`` with an event matching ``criterion`` who are
    assigned to that variant."""
    converters = _converters(db, experiment_id, variant_id, criterion)
    pairs = _assigned_pairs(db, experiment_id, (user_id for user_id, _ in converters))
    variant = str(variant_id)
    return [(u, first) for u, first in converters if (variant, u) in pairs]


def _assigned_converters(
    db: Session, experiment_id: Any, variant_id: Any, events: Optional[MetricEvents]
) -> List[Tuple[str, Any]]:
    """The converters of ``variant_id`` who are assigned to that variant."""
    return _assigned_matching(
        db, experiment_id, variant_id, metric_event_filter(events)
    )


def count_assigned_users_matching(
    db: Session, experiment_id: Any, variant_id: Any, criterion: Any
) -> int:
    """
    Number of users assigned to ``variant_id`` with an event matching ``criterion``.

    ``count_converting_users`` with the event criterion supplied by the
    caller: the same users-not-events count, the same restriction to users
    assigned to the variant the event is tagged with, and the same query
    shape (no SQL join of ``events`` to ``assignments``).  The bandit
    scheduler uses it for an experiment with no metric configured, where
    every event other than an experiment view is a success; everything else calls
    ``count_converting_users``.
    """
    return len(_assigned_matching(db, experiment_id, variant_id, criterion))


def count_converting_users(
    db: Session,
    experiment_id: Any,
    variant_id: Any,
    events: Optional[MetricEvents],
) -> int:
    """
    Number of users assigned to ``variant_id`` with at least one conversion.

    This is the numerator of every conversion rate the analysis endpoints
    report: a user who purchased three times counts once, and an event from a
    user with no assignment to that variant does not count, so the result is
    never larger than the variant's assignment count.
    """
    return len(_assigned_converters(db, experiment_id, variant_id, events))


def count_converting_users_any(
    db: Session,
    experiment_id: Any,
    events: Iterable[Optional[MetricEvents]],
) -> int:
    """Users in the experiment with a conversion on *any* of the given metrics."""
    rows = (
        db.query(Event.variant_id, Event.user_id)
        .filter(
            Event.experiment_id == experiment_id,
            any_metric_event_filter(events),
        )
        .distinct()
        .all()
    )
    events = {(str(variant_id), str(user_id)) for variant_id, user_id in rows}
    pairs = _assigned_pairs(db, experiment_id, {user_id for _, user_id in events})
    return len({user_id for variant_id, user_id in events & pairs})


def first_conversion_times(
    db: Session,
    experiment_id: Any,
    variant_id: Any,
    events: Optional[MetricEvents],
) -> List[str]:
    """
    The ``created_at`` of each converting user's *first* conversion.

    One entry per user counted by ``count_converting_users``.  Bucketing these
    by day gives the users who converted for the first time that day, so the
    running sum of a daily series equals the converting-user total.
    """
    return [
        first
        for _, first in _assigned_converters(db, experiment_id, variant_id, events)
        if first is not None
    ]


def converting_user_ids(
    db: Session,
    experiment_id: Any,
    variant_id: Any,
    events: Optional[MetricEvents],
) -> Set[str]:
    """
    The users ``count_converting_users`` counts, as a set of ids.

    The same definition: a user assigned to ``variant_id`` with at least one
    conversion tagged with that variant.  CUPED's outcome is read with it, so
    its converters are exactly ``/results``' (#217).
    """
    return {
        user_id
        for user_id, _ in _assigned_converters(db, experiment_id, variant_id, events)
    }


#: How many user ids one covariate query binds as a single array.
COVARIATE_LOOKUP_CHUNK = 50_000


def _naive_utc(value: Any) -> datetime:
    """A stored ``events.created_at`` string as a naive UTC ``datetime``.

    A value with no offset is UTC already (``normalize_event_timestamp``'s
    rule).  Raises ``ValueError`` (or ``TypeError``) for a value that cannot
    be read: migration 1ab99332f0ba left such legacy values as they were.
    """
    moment = datetime.fromisoformat(str(value).strip())
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    return moment


def covariate_user_ids(
    db: Session,
    assigned_at: Mapping[str, datetime],
    event_name: str,
    lookback_days: int,
) -> Tuple[Set[str], int]:
    """
    The users with a pre-assignment event of ``event_name``, for CUPED (#217).

    A user counts when at least one of their events matching
    ``conversion_event_filter(event_name)``, with any tag or none:

    * happened in ``[assigned_at - lookback_days, assigned_at)``: an event at
      the moment of assignment is not history;
    * and was stored by the server before ``assigned_at``
      (``events.updated_at < assigned_at``): history sent after the user was
      assigned is not read for that user.

    ``assigned_at`` maps each user id to their assignment's ``created_at``.
    Both it and ``events.updated_at`` are naive UTC (``timestamp without time
    zone``, written from ``utcnow``), and they are compared in Python as they
    are, never through an aware value or the database session's time zone.
    Comparing a naive value with an aware one raises ``TypeError``.

    One query per ``COVARIATE_LOOKUP_CHUNK`` users reads ``events`` alone (no
    SQL join to ``assignments``, as ``_assigned_pairs`` explains), bounded by
    the earliest window start and the latest assignment; each user's own
    window is applied here.  The bounds are compared through
    ``UTCTimestampString``, the format the column stores.

    Returns:
        The user ids, and how many matching rows were skipped because their
        stored ``created_at`` could not be read.
    """
    if not assigned_at or not event_name:
        return set(), 0
    lookback = timedelta(days=lookback_days)
    lower = min(assigned_at.values()) - lookback
    upper = max(assigned_at.values())
    users = list(assigned_at)
    found: Set[str] = set()
    skipped = 0
    for i in range(0, len(users), COVARIATE_LOOKUP_CHUNK):
        chunk = users[i : i + COVARIATE_LOOKUP_CHUNK]
        rows = (
            db.query(Event.user_id, Event.created_at, Event.updated_at)
            .filter(
                Event.user_id
                == any_(bindparam("user_ids", chunk, type_=ARRAY(String))),
                conversion_event_filter(event_name),
                Event.created_at >= lower,
                Event.created_at < upper,
            )
            .all()
        )
        for user_id, created_at, received_at in rows:
            try:
                happened = _naive_utc(created_at)
            except (TypeError, ValueError):
                skipped += 1
                continue
            user_id = str(user_id)
            assigned = assigned_at[user_id]
            if assigned - lookback <= happened < assigned and received_at < assigned:
                found.add(user_id)
    return found, skipped


def assignment_times(db: Session, experiment_id: Any) -> Dict[str, Dict[str, datetime]]:
    """Each variant's assigned users and their assignment times, in one query."""
    by_variant: Dict[str, Dict[str, datetime]] = {}
    rows = (
        db.query(Assignment.variant_id, Assignment.user_id, Assignment.created_at)
        .filter(Assignment.experiment_id == experiment_id)
        .all()
    )
    for variant_id, user_id, created_at in rows:
        by_variant.setdefault(str(variant_id), {})[str(user_id)] = created_at
    return by_variant
