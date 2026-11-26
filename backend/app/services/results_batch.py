"""
The results of several experiments, read together, for the data export (#1817).

``AnalysisService.get_experiment_results`` reads the database for each metric
and variant of one experiment, so an export that needs every experiment's
results ran about 18 statements for each of them. ``results_many`` reads what
a group of experiments needs together and then runs that same computation once
per experiment: ``_BatchedAnalysisService`` overrides the seven methods it
reads the database with, and nothing else. Every answer is the one
``get_experiment_results(id, include_bayesian=False)`` gives, but for the last
digits of a number computed from per-user sums of inexact values.

For a group of ``RESULTS_BATCH_SIZE`` experiments (``load_group``), one
statement each:

* the experiments with their variants and metrics mirror
  ``AnalysisService._load_experiment``;
* ``experiment_totals`` (two statements) mirrors the users and events of
  ``AnalysisService._experiment_totals``.

Then, for each sub-group of it that ``RESULTS_ROW_BUDGET`` makes
(``load_rows``), one statement each, in this order, and no statement where
there is nothing to read:

* ``metric_rows`` mirrors ``event_matching._converters`` and
  ``count_converting_users_any``, ``AnalysisService.event_counts`` and
  ``AnalysisService.value_totals``, for the metrics that name their event
  (``metric_events``);
* ``legacy_rows`` mirrors them for an empty event name, and for an experiment
  with no metric;
* ``assigned_pairs`` mirrors ``event_matching._assigned_pairs``, one statement
  for each ``ASSIGNMENT_LOOKUP_CHUNK`` users;
* ``assignment_counts`` mirrors ``AnalysisService._assignment_count`` and the
  counts ``srm_service.compute_srm_for_experiment`` reads. It is last, so that
  a user stored while the sub-group is read is counted as assigned.

An experiment expected to read more than ``RESULTS_BATCH_ROW_LIMIT`` rows is
computed by ``AnalysisService`` with its own reads.

``backend/tests/integration/api/test_export_api.py`` compares the results with
those of each experiment read alone, so a change to one of these reads that is
not mirrored here fails there. A read added to ``get_experiment_results`` that
no overridden method makes runs once per experiment, and fails that file's
statement counts.

Nothing is written: no analysis snapshot, no cache entry and no Bayesian
decision.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from itertools import islice
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple
from uuid import UUID

from sqlalchemy import String, and_, any_, bindparam, func, or_
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Session, joinedload

from backend.app.models.assignment import Assignment
from backend.app.models.event import Event
from backend.app.models.experiment import Experiment
from backend.app.schemas.tracking import VALUE_BOUND
from backend.app.services.analysis_service import AnalysisService
from backend.app.services.event_matching import (
    ASSIGNMENT_LOOKUP_CHUNK,
    MetricEvents,
    any_metric_event_filter,
    conversion_event_filter,
    metric_event_name,
    metric_events,
)
from backend.app.services.srm_service import _is_fixed_allocation, compute_srm

logger = logging.getLogger(__name__)

#: How many experiments are read together. Grouping only: nothing is refused
#: because of it.
RESULTS_BATCH_SIZE = 10

#: How many event rows one sub-group is expected to read at most
#: (``GroupReads.weight``). An experiment expected to read more is a sub-group
#: on its own; nothing is refused.
RESULTS_ROW_BUDGET = 50_000

#: How many event rows an experiment may be expected to read and still be read
#: here. One expected to read more is computed by ``AnalysisService`` with its
#: own reads; nothing is refused.
RESULTS_BATCH_ROW_LIMIT = 250_000

#: ``(events, value total)`` of one user: the value total is None when none of
#: the user's values counts.
UserRow = Tuple[int, Optional[float]]


@dataclass
class EventRows:
    """One sub-group's converting users, those assigned to their event's variant,
    and its assigned users.

    ``users`` maps ``(experiment id, MetricEvents, variant id)`` to one
    ``UserRow`` per user; the ``MetricEvents`` is None for the legacy events.
    ``read`` holds each ``(experiment id, MetricEvents)`` the rows were read
    for, so that ``of`` can refuse any other: a key that matches nothing read
    raises instead of answering no users. ``converted`` is how many users of
    an experiment converted on any of its metrics. ``assignments`` maps an
    experiment id to its variants.
    """

    users: Dict[Tuple[str, Optional[MetricEvents], str], List[UserRow]] = field(
        default_factory=dict
    )
    read: Set[Tuple[str, Optional[MetricEvents]]] = field(default_factory=set)
    converted: Dict[str, int] = field(default_factory=dict)
    #: Assigned users per variant, counted after the rows above are read: a
    #: user stored in between is assigned and not yet converted.
    assignments: Dict[str, Dict[str, int]] = field(default_factory=dict)

    def of(
        self, experiment_id: Any, variant_id: Any, events: Optional[MetricEvents]
    ) -> List[UserRow]:
        """The rows of the users ``metric_event_filter(events)`` finds.

        Raises ``TypeError`` for a bare event name (``metric_event_name``),
        and ``KeyError`` for events of the experiment that were not read.
        """
        metric_event_name(events)
        experiment = str(experiment_id)
        if (experiment, events) not in self.read:
            raise KeyError(
                f"{events!r} of experiment {experiment} was not read for these rows"
            )
        return self.users.get((experiment, events, str(variant_id)), [])


@dataclass
class GroupReads:
    """What one group's results are computed from, by experiment id.

    ``users`` is the assigned users and ``events`` the events of the
    experiment. ``rows`` holds the sub-group being computed, one at a time.
    """

    experiments: Dict[str, Experiment] = field(default_factory=dict)
    users: Dict[str, int] = field(default_factory=dict)
    events: Dict[str, int] = field(default_factory=dict)
    rows: EventRows = field(default_factory=EventRows)

    def weight(self, experiment_id: Any) -> int:
        """The event rows the experiment is expected to read at most.

        One for each assigned user and each event name its metrics read, and
        one more for each user when it reads the legacy events. (It always
        reads at least one of the two.)
        """
        experiment = self.experiments.get(str(experiment_id))
        if experiment is None:
            return 0
        reads = len(_event_names(experiment)) + (1 if _reads_legacy(experiment) else 0)
        return reads * self.users.get(str(experiment_id), 0)


def _metric_events(experiment: Experiment) -> Set[MetricEvents]:
    """The ``MetricEvents`` of the experiment's metrics that name their event."""
    metrics = experiment.metric_definitions or []
    return {
        events for metric in metrics if (events := metric_events(metric)) is not None
    }


def _event_names(experiment: Experiment) -> Set[str]:
    """The event names the experiment's metrics read."""
    return {events.event_name for events in _metric_events(experiment)}


def _reads_legacy(experiment: Experiment) -> bool:
    """Whether the results read the legacy events: the experiment has no metric
    (its total does), or one with an empty event name."""
    metrics = experiment.metric_definitions or []
    return not metrics or any(metric_events(metric) is None for metric in metrics)


# ---------------------------------------------------------------------------
# The reads
# ---------------------------------------------------------------------------


def assignment_counts(db: Session, ids: Sequence[UUID]) -> Dict[str, Dict[str, int]]:
    """The assigned users of each variant of these experiments."""
    counts: Dict[str, Dict[str, int]] = {}
    rows = (
        db.query(
            Assignment.experiment_id,
            Assignment.variant_id,
            func.count(Assignment.id),
        )
        .filter(Assignment.experiment_id.in_(ids))
        .group_by(Assignment.experiment_id, Assignment.variant_id)
        .all()
    )
    for experiment_id, variant_id, count in rows:
        counts.setdefault(str(experiment_id), {})[str(variant_id)] = count
    return counts


def experiment_totals(
    db: Session, ids: Sequence[UUID]
) -> Tuple[Dict[str, int], Dict[str, int]]:
    """The assigned users, and the events, of each of these experiments."""
    users = (
        db.query(Assignment.experiment_id, func.count(Assignment.user_id.distinct()))
        .filter(Assignment.experiment_id.in_(ids))
        .group_by(Assignment.experiment_id)
        .all()
    )
    events = (
        db.query(Event.experiment_id, func.count(Event.id))
        .filter(Event.experiment_id.in_(ids))
        .group_by(Event.experiment_id)
        .all()
    )
    return (
        {str(experiment_id): count for experiment_id, count in users},
        {str(experiment_id): count for experiment_id, count in events},
    )


def _value_total():
    """A user's value total, as ``AnalysisService.value_totals`` adds it up."""
    return func.sum(Event.value).filter(func.abs(Event.value) <= VALUE_BOUND)


def metric_rows(
    db: Session, metrics: Mapping[UUID, Iterable[MetricEvents]]
) -> List[Any]:
    """Each user's events of each metric event name, per variant tag.

    ``metrics`` is the ``MetricEvents`` of each experiment's own metrics. A row
    is ``(experiment id, event name, variant id, user id, events, value
    total)``.
    """
    if not metrics:
        return []
    return (
        db.query(
            Event.experiment_id,
            Event.event_name,
            Event.variant_id,
            Event.user_id,
            func.count(Event.id),
            _value_total(),
        )
        .filter(
            or_(
                *(
                    and_(
                        Event.experiment_id == experiment_id,
                        any_metric_event_filter(
                            sorted(events, key=lambda e: e.event_name)
                        ),
                    )
                    for experiment_id, events in metrics.items()
                )
            )
        )
        .group_by(
            Event.experiment_id, Event.event_name, Event.variant_id, Event.user_id
        )
        .all()
    )


def legacy_rows(db: Session, ids: Sequence[UUID]) -> List[Any]:
    """Each user's legacy events, whatever their names, per variant tag.

    A row is ``(experiment id, variant id, user id, events, value total)``.
    """
    if not ids:
        return []
    return (
        db.query(
            Event.experiment_id,
            Event.variant_id,
            Event.user_id,
            func.count(Event.id),
            _value_total(),
        )
        .filter(Event.experiment_id.in_(ids), conversion_event_filter(None))
        .group_by(Event.experiment_id, Event.variant_id, Event.user_id)
        .all()
    )


def assigned_pairs(
    db: Session, users: Mapping[UUID, Iterable[str]]
) -> Dict[UUID, Set[Tuple[UUID, str]]]:
    """``(variant id, user id)`` of the assignments these users have, per
    experiment: no SQL join, by the unique index, as ``_assigned_pairs``.
    """
    pairs: Dict[UUID, Set[Tuple[UUID, str]]] = {}
    wanted = (
        (experiment_id, user_id)
        for experiment_id, user_ids in users.items()
        for user_id in user_ids
    )
    while chunk := list(islice(wanted, ASSIGNMENT_LOOKUP_CHUNK)):
        by_experiment: Dict[UUID, List[str]] = {}
        for experiment_id, user_id in chunk:
            by_experiment.setdefault(experiment_id, []).append(user_id)
        rows = (
            db.query(
                Assignment.experiment_id, Assignment.variant_id, Assignment.user_id
            )
            .filter(
                or_(
                    *(
                        and_(
                            Assignment.experiment_id == experiment_id,
                            Assignment.user_id
                            == any_(
                                bindparam(
                                    "user_ids",
                                    user_ids,
                                    type_=ARRAY(String),
                                    unique=True,
                                )
                            ),
                        )
                        for experiment_id, user_ids in by_experiment.items()
                    )
                )
            )
            .all()
        )
        for experiment_id, variant_id, user_id in rows:
            pairs.setdefault(experiment_id, set()).add((variant_id, user_id))
    return pairs


# ---------------------------------------------------------------------------
# The groups
# ---------------------------------------------------------------------------


def load_group(db: Session, experiment_ids: Sequence[UUID]) -> GroupReads:
    """Everything one group is computed from but what ``load_rows`` reads."""
    experiments = (
        db.query(Experiment)
        .options(
            joinedload(Experiment.variants),
            joinedload(Experiment.metric_definitions),
        )
        .filter(Experiment.id.in_(experiment_ids))
        .all()
    )
    reads = GroupReads(experiments={str(e.id): e for e in experiments})
    if not reads.experiments:
        return reads
    ids = [experiment.id for experiment in experiments]
    reads.users, reads.events = experiment_totals(db, ids)
    return reads


def load_rows(db: Session, experiments: Sequence[Experiment]) -> EventRows:
    """The event rows one sub-group is computed from, then its assigned users."""
    if not experiments:
        return EventRows()
    named = {e.id: events for e in experiments if (events := _metric_events(e))}
    # Each row carries its event name; it is the rows of the MetricEvents that
    # names it (one per name: no metric has a window).
    by_name = {
        experiment_id: {events.event_name: events for events in metrics}
        for experiment_id, metrics in named.items()
    }
    rows: List[Tuple[Any, Optional[str], Any, str, int, Optional[float]]] = [
        *metric_rows(db, named),
        *(
            (experiment_id, None, *row)
            for experiment_id, *row in legacy_rows(
                db, [e.id for e in experiments if _reads_legacy(e)]
            )
        ),
    ]
    senders: Dict[UUID, Set[str]] = {}
    for experiment_id, _, _, user_id, _, _ in rows:
        senders.setdefault(experiment_id, set()).add(user_id)
    pairs = assigned_pairs(db, senders)

    loaded = EventRows()
    loaded.read = {
        (str(experiment_id), events)
        for experiment_id, metrics in named.items()
        for events in metrics
    } | {(str(e.id), None) for e in experiments if _reads_legacy(e)}
    converted: Dict[UUID, Set[str]] = {}
    for experiment_id, name, variant_id, user_id, count, total in rows:
        if (variant_id, user_id) not in pairs.get(experiment_id, ()):
            continue
        metric = None if name is None else by_name[experiment_id][name]
        key = (str(experiment_id), metric, str(variant_id))
        loaded.users.setdefault(key, []).append((count, total))
        # The legacy events count towards the total when no metric names its
        # event (any_metric_event_filter).
        if name is not None or experiment_id not in named:
            converted.setdefault(experiment_id, set()).add(user_id)
    loaded.converted = {str(e): len(user_ids) for e, user_ids in converted.items()}
    loaded.assignments = assignment_counts(db, [e.id for e in experiments])
    return loaded


def sub_groups(reads: GroupReads, experiment_ids: Sequence[UUID]) -> List[List[UUID]]:
    """The group in order, split so that each part's weights stay within
    ``RESULTS_ROW_BUDGET``. An experiment over it is a part on its own."""
    parts: List[List[UUID]] = []
    total = 0
    for experiment_id in experiment_ids:
        weight = reads.weight(experiment_id)
        if not parts or total + weight > RESULTS_ROW_BUDGET:
            parts.append([])
            total = 0
        parts[-1].append(experiment_id)
        total += weight
    return parts


class _BatchedAnalysisService(AnalysisService):
    """``AnalysisService`` answering its reads from what a group has read."""

    def __init__(self, db: Session, reads: GroupReads) -> None:
        super().__init__(db)
        self._reads = reads

    def _load_experiment(self, experiment_id: Any) -> Optional[Experiment]:
        return self._reads.experiments.get(str(experiment_id))

    def _assignment_count(self, experiment_id: Any, variant_id: Any) -> int:
        counts = self._reads.rows.assignments.get(str(experiment_id), {})
        return counts.get(str(variant_id), 0)

    def _converting_users(
        self, experiment_id: Any, variant_id: Any, events: Optional[MetricEvents]
    ) -> int:
        return len(self._reads.rows.of(experiment_id, variant_id, events))

    def event_counts(
        self, experiment_id: Any, variant_id: Any, events: Optional[MetricEvents]
    ) -> List[float]:
        rows = self._reads.rows.of(experiment_id, variant_id, events)
        return [float(count) for count, _ in rows]

    def value_totals(
        self, experiment_id: Any, variant_id: Any, events: Optional[MetricEvents]
    ) -> List[float]:
        rows = self._reads.rows.of(experiment_id, variant_id, events)
        return [float(total) for _, total in rows if total is not None]

    def _experiment_totals(self, experiment: Experiment) -> Tuple[int, int, int]:
        key = str(experiment.id)
        return (
            self._reads.users.get(key, 0),
            self._reads.events.get(key, 0),
            self._reads.rows.converted.get(key, 0),
        )

    def _sample_ratio_check(self, experiment: Experiment) -> Optional[Dict[str, Any]]:
        """``srm_service.sample_ratio_check`` on the counts already read: it
        fails open too, and no statement ran that could need a rollback."""
        try:
            allocations = {
                str(variant.id): float(variant.traffic_allocation or 0)
                for variant in experiment.variants
            }
            if len(allocations) < 2:
                return None
            if not _is_fixed_allocation(experiment.optimization_type):
                return None
            observed = self._reads.rows.assignments.get(str(experiment.id), {})
            result = compute_srm(observed, allocations)
            return None if result is None else result.to_dict()
        except Exception as exc:
            logger.warning("SRM check failed for experiment %s: %s", experiment.id, exc)
            return None


def results_many(
    db: Session, experiment_ids: Iterable[UUID]
) -> Dict[str, Optional[Dict[str, Any]]]:
    """``AnalysisService.get_experiment_results_many`` (its docstring is the
    contract)."""
    ids = list(dict.fromkeys(experiment_ids))
    results: Dict[str, Optional[Dict[str, Any]]] = {}
    for start in range(0, len(ids), RESULTS_BATCH_SIZE):
        _compute_group(db, ids[start : start + RESULTS_BATCH_SIZE], results)
    return results


def _compute_group(
    db: Session, ids: Sequence[UUID], results: Dict[str, Optional[Dict[str, Any]]]
) -> None:
    """Add one group's results to ``results``."""
    reads = load_group(db, ids)
    for part in sub_groups(reads, ids):
        # The last part's rows and counts go before the next part's are read.
        reads.rows = EventRows()
        service: AnalysisService
        if reads.weight(part[0]) > RESULTS_BATCH_ROW_LIMIT:
            # The limit is above the budget, so it is alone in its part.
            service = AnalysisService(db)
        else:
            service = _BatchedAnalysisService(db, reads)
            reads.rows = load_rows(
                db, [e for i in part if (e := reads.experiments.get(str(i)))]
            )
        for experiment_id in part:
            key = str(experiment_id)
            try:
                results[key] = service.get_experiment_results(
                    experiment_id, include_bayesian=False
                )
            except ValueError as exc:
                logger.info("Export: no results for experiment %s: %s", key, exc)
                results[key] = None
