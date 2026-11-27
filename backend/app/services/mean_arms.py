"""
Each arm's per-user values for a mean metric, read in one place (#2214).

A revenue, count, duration or custom metric (``MEAN_METRIC_TYPES``) is
analysed as its mean per assigned user.  The statistic on its per-user
values, ``per_user_mean_metric_result``, takes only the ``PreparedMeanArms``
that ``prepare_mean_arms`` returns: anything else raises ``TypeError``.  So
every path that analyses a mean reads its values through the same readers,
and a rule about which values count (the conversion window today) applies to
all of them alike; a path that reads the values some other way and hands them
to the statistic fails when it runs.

``prepare_mean_arms`` reads through the service it is given, so a service
that overrides the readers is read through its overrides: a count metric's
value is ``event_counts`` (the user's number of the metric's events), every
other mean type's is ``value_totals`` (the total of their events' values).
``AnalysisService`` reads them from the database; the batched export's
service (``results_batch``) answers them from rows it has already read.  With
``members`` (an ID-list segment's members, per variant), each arm's values
are read with ``_member_values`` for those members only (``segment_results``).

Nothing here changes a value: each arm's values are exactly what the reader
returned, and an assigned user who has none is 0 in the statistic.

``prepared_for_tests`` wraps values already in hand, for the tests of the
statistic and the fixture generator in ``backend/scripts``.  It is not for a
results path, which reads its values with ``prepare_mean_arms``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Mapping, Optional, Sequence, Set, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.app.models.event import Event
from backend.app.models.experiment import Metric
from backend.app.schemas.tracking import VALUE_BOUND
from backend.app.services.event_matching import (
    MetricEvents,
    metric_event_filter,
    metric_events,
    windowed,
    windowed_users,
)
from backend.app.services.sufficient_stats_analysis import (
    EVENT_COUNT_METRIC_TYPES,
    MEAN_METRIC_TYPES,
    BinomialVariant,
    PerUserTotals,
    metric_type_name,
)


@dataclass(frozen=True)
class PreparedMeanArms:
    """One mean metric's arms, as ``prepare_mean_arms`` read them.

    ``arms`` holds one ``(variant, n, n_converted, values)`` per variant, in
    the order of the counts it was given: the users assigned to the variant,
    how many of them converted, and the value of each assigned user who has
    one.  ``window_hours`` is the metric's conversion window (None for none),
    which the values were read within.
    """

    arms: Tuple[PerUserTotals, ...]
    window_hours: Optional[int] = None


def prepare_mean_arms(
    reader: Any,
    experiment: Any,
    metric: Any,
    counts: Sequence[Mapping[str, Any]],
    *,
    members: Optional[Mapping[str, Set[str]]] = None,
) -> PreparedMeanArms:
    """Read each arm's per-user values of a mean metric.

    Args:
        reader: the service whose ``event_counts`` and ``value_totals`` read
            the values: an ``AnalysisService``, or a service that overrides
            them.
        experiment: the experiment; its ``id`` is what the readers are given.
        metric: the metric, of a type in ``MEAN_METRIC_TYPES``; its events and
            conversion window are ``metric_events(metric)``.
        counts: each variant's row of ``calculate_metric_results``'s
            ``variant_results`` (``variant_id``, ``variant_name``,
            ``is_control``, ``sample_size``, ``conversions``), one arm each.
        members: for an ID-list segment, each variant id's members (user ids
            as text): only their values are read, with ``_member_values``.

    Raises:
        TypeError: the metric is not a mean metric (a conversion metric is a
            proportion, ``binomial_metric_result``).
        ValueError: no row is the control's, or ``members`` is given to the
            batched export's service, whose rows carry no user ids.
    """
    if metric_type_name(metric) not in MEAN_METRIC_TYPES:
        raise TypeError(
            f"a {metric_type_name(metric)} metric is not analysed as a mean; "
            f"prepare_mean_arms reads {', '.join(MEAN_METRIC_TYPES)} metrics"
        )
    rows = list(counts)
    if not any(row["is_control"] for row in rows):
        raise ValueError("No variant is the control")
    if members is not None and _reads_batched_rows(reader):
        raise ValueError(
            "the batched export's rows carry no user ids, so they cannot be "
            "read for a segment's members"
        )
    events = metric_events(metric)
    arms: List[PerUserTotals] = []
    for row in rows:
        if members is None:
            values = _read(reader, experiment.id, row["variant_id"], metric, events)
        else:
            values = _member_values(
                reader.db,
                experiment.id,
                row["variant_id"],
                metric,
                members.get(str(row["variant_id"]), set()),
            )
        arms.append(
            (
                BinomialVariant(
                    row["variant_id"], row["variant_name"], row["is_control"]
                ),
                row["sample_size"],
                row["conversions"],
                values,
            )
        )
    return PreparedMeanArms(
        arms=tuple(arms), window_hours=events.window_hours if events else None
    )


def prepared_for_tests(
    variants: Sequence[PerUserTotals], *, window_hours: Optional[int] = None
) -> PreparedMeanArms:
    """``PreparedMeanArms`` around values already in hand, unchanged.

    For the tests of the statistic and ``generate_results_interval_fixtures``:
    each row is ``(variant, n, n_converted, values)`` as ``PreparedMeanArms``
    holds it, and is passed on as it is, so the statistic's own refusals (a
    count that is not a non-negative integer, a value that is not finite) are
    still the statistic's.  It is not for a results path, which reads its
    values with ``prepare_mean_arms``.
    """
    return PreparedMeanArms(arms=tuple(variants), window_hours=window_hours)


def _read(
    reader: Any,
    experiment_id: Any,
    variant_id: Any,
    metric: Any,
    events: Optional[MetricEvents],
) -> List[float]:
    """The variant's values through ``reader``: its event counts for a count
    metric, its value totals for any other mean type."""
    read = (
        reader.event_counts
        if metric_type_name(metric) in EVENT_COUNT_METRIC_TYPES
        else reader.value_totals
    )
    return read(experiment_id, variant_id, events)


def _reads_batched_rows(reader: Any) -> bool:
    """Whether ``reader`` is the batched export's service."""
    # Imported here: results_batch imports analysis_service, which imports
    # this module.
    from backend.app.services.results_batch import _BatchedAnalysisService

    return isinstance(reader, _BatchedAnalysisService)


def _member_values(
    db: Session,
    experiment_id: Any,
    variant_id: Any,
    metric: Metric,
    members: Set[str],
) -> List[float]:
    """Each member's value for a mean metric, for those who have one.

    Mirrors ``AnalysisService.event_counts`` (a count metric: the number of the
    metric's events, whatever their value) and ``AnalysisService.value_totals``
    (any other mean metric: the total of the values with ``abs(value) <=
    VALUE_BOUND``): the same events, tagged with the variant, kept for the
    variant's members. A member with no such event is not listed, so their
    value is 0. With a conversion window, only the events inside it count
    (``windowed_users``), as there.
    """
    events = metric_events(metric)
    if windowed(events):
        found = windowed_users(db, experiment_id, variant_id, events)
        if metric_type_name(metric) in EVENT_COUNT_METRIC_TYPES:
            return [float(r.events) for (_, u), r in found.items() if u in members]
        return [r.value for (_, u), r in found.items() if r.valued and u in members]
    if metric_type_name(metric) in EVENT_COUNT_METRIC_TYPES:
        rows = (
            db.query(Event.user_id, func.count(Event.id))
            .filter(
                Event.experiment_id == experiment_id,
                Event.variant_id == variant_id,
                metric_event_filter(metric_events(metric)),
            )
            .group_by(Event.user_id)
            .all()
        )
    else:
        rows = (
            db.query(Event.user_id, func.sum(Event.value))
            .filter(
                Event.experiment_id == experiment_id,
                Event.variant_id == variant_id,
                metric_event_filter(metric_events(metric)),
                func.abs(Event.value) <= VALUE_BOUND,
            )
            .group_by(Event.user_id)
            .all()
        )
    return [float(value) for user_id, value in rows if str(user_id) in members]
