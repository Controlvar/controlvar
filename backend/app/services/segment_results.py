"""
One experiment's results counted for the members of one ID-list segment
(#1933), for ``GET /api/v1/results/{experiment_id}/segments/{segment_id}``.

The population P is the users assigned to the experiment whose user id is,
by exact string match, in the segment's list when the request runs. The
answer is the released engine run on P's rows, and nothing else: every value
in ``metrics``, ``summary`` and ``srm``, and ``sample_size_adequate``, equals
what ``GET /api/v1/results/{experiment_id}?use_cache=false`` answers on a
database where every assignment and event row of a user outside P has been
deleted. The one exception is the recommendation: the whole experiment's
sample ratio check overrides it as well as P's own (the #880 rule, applied
twice), so a broken randomisation is never read through a segment that
happens to look balanced.

Nothing here changes the unfiltered path. The engine functions are called as
``AnalysisService.get_experiment_results`` calls them; the reads that cannot
be shared are mirrored, each beside the function it mirrors:

* ``_member_values`` mirrors ``AnalysisService.event_counts`` and
  ``AnalysisService.value_totals`` (they return bare values, so their rows
  cannot be filtered afterwards);
* ``_summary`` mirrors ``AnalysisService.calculate_experiment_summary``;
* ``population_srm`` mirrors ``srm_service.compute_srm_for_experiment``.

``backend/tests/integration/api/test_segment_results_api.py`` compares the
two paths on the same database (a segment of every assigned user, and a
pruned database), so a change to one of them that is not mirrored here fails
there.

Membership is read once per request (``resolve_population``): the list's ids
in primary-key order, ``MEMBER_PAGE`` at a time, each page looked up in the
assignments by the unique ``(experiment_id, user_id)`` index
(``_assigned_pairs``; never an SQL join, #233). Every metric then intersects
with that one pass's P_v, so converting users never exceed users. The pages
are read under READ COMMITTED: a list changed while the request runs may be
counted partly before and partly after the change, and ``member_count`` is
the number of ids that pass read.

Nothing is written: no analysis snapshot (it would overwrite the experiment's
row for the day), no cache entry (a change to the list does not touch the
segment row, so no key could tell a stale answer), and no Bayesian analysis
(it stores ``experiment.bayesian_decision``).

``COMPUTE_SLOT`` lets one computation run per API process. In #1933's review a
reimplementation of this design took 23 to 36 s and about 440 MB more memory
for a 1,000,000-id list on a laptop, so two at once could exceed the Helm
chart's default 1 GiB limit, and the dashboard's latest-wins does not cancel a request
on the server. The route answers 503 with ``Retry-After`` while the slot is
taken.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from sqlalchemy import String, any_, bindparam, func
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Session, joinedload

from backend.app.models.assignment import Assignment
from backend.app.models.event import Event
from backend.app.models.experiment import Experiment, Metric
from backend.app.models.segment import SegmentMember
from backend.app.schemas.tracking import VALUE_BOUND
from backend.app.services.analysis_service import AnalysisService
from backend.app.services.analysis_settings import resolve_analysis_settings
from backend.app.services.event_matching import (
    _assigned_pairs,
    any_conversion_event_filter,
    conversion_event_filter,
    converting_user_ids,
)
from backend.app.services.srm_service import (
    _is_fixed_allocation,
    compute_srm,
    recommendation_under_srm,
    sample_ratio_check,
)
from backend.app.services.sufficient_stats_analysis import (
    EVENT_COUNT_METRIC_TYPES,
    MEAN_METRIC_TYPES,
    BinomialVariant,
    binomial_metric_result,
    binomial_variant_results,
    metric_type_name,
    per_user_mean_metric_result,
)

logger = logging.getLogger(__name__)

#: How many of the list's ids one membership page reads (and one assignment
#: lookup binds).
MEMBER_PAGE = 10_000

#: How many members' ids one ``total_events`` count binds.
EVENT_COUNT_CHUNK = 10_000

#: One segment computation per API process at a time (#1933).
COMPUTE_SLOT = threading.BoundedSemaphore(1)

#: The 503 ``detail`` while ``COMPUTE_SLOT`` is taken.
BUSY = "Another segment's results are being computed. Try again shortly."

#: The ``Retry-After`` header of that 503, in seconds.
BUSY_RETRY_AFTER_SECONDS = 5

#: The 422 ``msg`` for a rules segment. Fixed text: it names no id.
RULES_SEGMENT_REFUSED = (
    "This is a rules segment. Results can be computed for the members of an "
    "id_list segment only: the attributes a rules segment matches are not "
    "stored for every assigned user."
)


@dataclass
class Population:
    """The members assigned to the experiment, read once.

    ``by_variant`` maps a variant id to its members' user ids (P_v);
    ``member_count`` is how many ids the list held in that read.
    """

    by_variant: Dict[str, Set[str]] = field(default_factory=dict)
    member_count: int = 0

    def users(self) -> Set[str]:
        found: Set[str] = set()
        for users in self.by_variant.values():
            found |= users
        return found


def resolve_population(db: Session, experiment_id: Any, segment_id: Any) -> Population:
    """Read the list's ids once, page by page, and keep those assigned.

    Pages of ``MEMBER_PAGE`` ids in primary-key order, the next page after the
    last id read (a keyset, so a page costs the same however deep it is), each
    looked up in the experiment's assignments by ``_assigned_pairs``.
    """
    population = Population()
    last: Optional[str] = None
    while True:
        query = db.query(SegmentMember.member_id).filter(
            SegmentMember.segment_id == segment_id
        )
        if last is not None:
            query = query.filter(SegmentMember.member_id > last)
        page = [
            member_id
            for (member_id,) in query.order_by(SegmentMember.member_id)
            .limit(MEMBER_PAGE)
            .all()
        ]
        if not page:
            break
        population.member_count += len(page)
        for variant_id, user_id in _assigned_pairs(db, experiment_id, page):
            population.by_variant.setdefault(variant_id, set()).add(user_id)
        if len(page) < MEMBER_PAGE:
            break
        last = page[-1]
    return population


def _metric_counts(
    db: Session,
    experiment: Experiment,
    metric: Metric,
    population: Population,
    confidence_level: float,
) -> Dict[str, Any]:
    """``AnalysisService.calculate_metric_results`` on P: each variant's
    members, and those of them who converted (``converting_user_ids``, the
    definition every results path counts by)."""
    control = next((v for v in experiment.variants if v.is_control), None)
    if control is None:
        raise ValueError(f"Experiment {experiment.id} has no control variant")
    counts = []
    for variant in experiment.variants:
        members = population.by_variant.get(str(variant.id), set())
        converted = converting_user_ids(
            db, experiment.id, variant.id, metric.event_name
        )
        counts.append((variant, len(members), len(converted & members)))
    return {
        "metric_id": str(metric.id),
        "variant_results": binomial_variant_results(counts, confidence_level),
    }


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
    value is 0.
    """
    if metric_type_name(metric) in EVENT_COUNT_METRIC_TYPES:
        rows = (
            db.query(Event.user_id, func.count(Event.id))
            .filter(
                Event.experiment_id == experiment_id,
                Event.variant_id == variant_id,
                conversion_event_filter(metric.event_name),
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
                conversion_event_filter(metric.event_name),
                func.abs(Event.value) <= VALUE_BOUND,
            )
            .group_by(Event.user_id)
            .all()
        )
    return [float(value) for user_id, value in rows if str(user_id) in members]


def _metric_result(
    db: Session,
    experiment: Experiment,
    metric: Metric,
    raw: Dict[str, Any],
    population: Population,
    alpha: float,
    correction_method: str,
) -> Dict[str, Any]:
    """The metric's schema-shaped result, as ``get_experiment_results`` maps
    it: a mean per assigned user for ``MEAN_METRIC_TYPES``, a proportion
    otherwise."""
    if metric_type_name(metric) in MEAN_METRIC_TYPES:
        return per_user_mean_metric_result(
            [
                (
                    BinomialVariant(
                        v["variant_id"], v["variant_name"], v["is_control"]
                    ),
                    v["sample_size"],
                    v["conversions"],
                    _member_values(
                        db,
                        experiment.id,
                        v["variant_id"],
                        metric,
                        population.by_variant.get(str(v["variant_id"]), set()),
                    ),
                )
                for v in raw["variant_results"]
            ],
            alpha,
            correction_method,
            metric=metric,
        )
    return binomial_metric_result(
        [
            (
                BinomialVariant(v["variant_id"], v["variant_name"], v["is_control"]),
                v["sample_size"],
                v["conversions"],
            )
            for v in raw["variant_results"]
        ],
        alpha,
        correction_method,
        metric=metric,
    )


def _summary(
    db: Session, experiment: Experiment, population: Population
) -> Dict[str, Any]:
    """Mirrors ``AnalysisService.calculate_experiment_summary`` on P.

    ``total_users`` is P's size; ``total_events`` counts every event tagged
    with the experiment from a user in P, of any type and any variant tag;
    ``total_conversions`` counts the users in P with a conversion on any
    metric tagged with the variant they are assigned to.
    """
    start_date = experiment.start_date
    end_date = experiment.end_date
    if start_date:
        try:
            start_dt = AnalysisService._parse_dt(experiment.start_date)
            end_dt = (
                AnalysisService._parse_dt(experiment.end_date)
                if experiment.end_date
                else datetime.now(timezone.utc)
            )
            duration_days = (end_dt - start_dt).days
        except (ValueError, TypeError):
            duration_days = None
    else:
        duration_days = None

    users = sorted(population.users())
    total_events = 0
    for i in range(0, len(users), EVENT_COUNT_CHUNK):
        chunk = users[i : i + EVENT_COUNT_CHUNK]
        total_events += (
            db.query(func.count(Event.id))
            .filter(
                Event.experiment_id == experiment.id,
                Event.user_id
                == any_(bindparam("user_ids", chunk, type_=ARRAY(String))),
            )
            .scalar()
            or 0
        )

    rows = (
        db.query(Event.variant_id, Event.user_id)
        .filter(
            Event.experiment_id == experiment.id,
            any_conversion_event_filter(
                m.event_name for m in (experiment.metric_definitions or [])
            ),
        )
        .distinct()
        .all()
    )
    converted = {
        str(user_id)
        for variant_id, user_id in rows
        if str(user_id) in population.by_variant.get(str(variant_id), set())
    }

    return {
        "total_users": len(users),
        "total_events": total_events,
        "total_conversions": len(converted),
        "start_date": start_date,
        "end_date": end_date,
        "duration_days": duration_days,
    }


def population_srm(
    experiment: Experiment, population: Population
) -> Optional[Dict[str, Any]]:
    """The sample ratio check of P alone, or None.

    Mirrors ``compute_srm_for_experiment``: the variants' traffic allocations
    against P's users per variant, None for a bandit (an allocation that is
    not fixed), for fewer than two allocated variants, and when no member is
    assigned. A check that raises is logged and is None: it never turns the
    request into an error.
    """
    try:
        allocations = {
            str(variant.id): float(
                variant.traffic_allocation
                if variant.traffic_allocation is not None
                else 0
            )
            for variant in experiment.variants
        }
        if len(allocations) < 2:
            return None
        if not _is_fixed_allocation(experiment.optimization_type):
            return None
        observed = {
            variant_id: len(users)
            for variant_id, users in population.by_variant.items()
        }
        result = compute_srm(observed, allocations)
        return None if result is None else result.to_dict()
    except Exception as exc:
        logger.warning(
            "Segment SRM check failed for experiment %s (%s)",
            experiment.id,
            type(exc).__name__,
        )
        return None


def experiment_users(db: Session, experiment_id: Any) -> int:
    """Distinct users assigned to the experiment, members or not."""
    return (
        db.query(func.count(Assignment.user_id.distinct()))
        .filter(Assignment.experiment_id == experiment_id)
        .scalar()
        or 0
    )


def segment_results(
    db: Session,
    experiment_id: Any,
    segment_id: Any,
    confidence_level: Optional[float] = None,
    correction_method: Optional[str] = None,
) -> Dict[str, Any]:
    """The experiment's results for the segment's members, as a dict.

    Raises ``ValueError`` for an experiment that does not exist or, when it
    has a metric, has no control variant (the route's 404, as on
    ``GET /results/{id}``).
    """
    experiment = (
        db.query(Experiment)
        .options(
            joinedload(Experiment.variants),
            joinedload(Experiment.metric_definitions),
        )
        .filter(Experiment.id == experiment_id)
        .first()
    )
    if not experiment:
        raise ValueError(f"Experiment {experiment_id} not found")

    confidence_level, correction_method = resolve_analysis_settings(
        experiment, confidence_level, correction_method
    )
    population = resolve_population(db, experiment.id, segment_id)

    metric_definitions = AnalysisService._ordered_metrics(experiment)
    raw_results = [
        _metric_counts(db, experiment, metric, population, confidence_level)
        for metric in metric_definitions
    ]

    alpha = 1.0 - confidence_level
    metrics = [
        _metric_result(
            db, experiment, metric, raw, population, alpha, correction_method
        )
        for metric, raw in zip(metric_definitions, raw_results)
    ]
    sample_size_adequate = AnalysisService._sample_size_adequate(
        experiment, raw_results
    )
    summary = _summary(db, experiment, population)
    summary.update(
        AnalysisService._summarise_decision(experiment, metrics, sample_size_adequate)
    )

    # The #880 rule twice: the whole experiment's check, then P's own. Either
    # warning makes the recommendation INCONCLUSIVE; with both, the reason
    # gives P's p-value. Each fails open, as on the unfiltered path.
    whole = sample_ratio_check(db, experiment.id)
    try:
        summary.update(recommendation_under_srm(summary, whole))
    except Exception as exc:
        logger.warning(
            "SRM recommendation rule failed for experiment %s (%s)",
            experiment.id,
            type(exc).__name__,
        )
        whole = None
    srm = population_srm(experiment, population)
    try:
        summary.update(recommendation_under_srm(summary, srm))
    except Exception as exc:
        logger.warning(
            "Segment SRM recommendation rule failed for experiment %s (%s)",
            experiment.id,
            type(exc).__name__,
        )
        srm = None

    return {
        "experiment_id": str(experiment.id),
        "experiment_name": experiment.name,
        "status": (
            experiment.status.value
            if hasattr(experiment.status, "value")
            else str(experiment.status)
        ),
        "start_date": experiment.start_date,
        "end_date": experiment.end_date,
        "confidence_level": confidence_level,
        "correction_method": correction_method,
        "sample_size_adequate": sample_size_adequate,
        "computed_at": datetime.now(timezone.utc),
        "metrics": metrics,
        "summary": summary,
        "srm": srm,
        "member_count": population.member_count,
        "experiment_users": experiment_users(db, experiment.id),
        "experiment_srm_warning": None if whole is None else bool(whole["warning"]),
    }
