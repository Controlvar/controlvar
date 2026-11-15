"""Which feature flags look ready to remove, and why (#1808).

Computed when the flag list is read: no column, no migration, no scheduler,
and nothing here writes. N is ``STALE_FLAG_DAYS`` and the cutoff is N days
before now. A flag is stale when it is not ARCHIVED, was created at or before
the cutoff, is not tagged ``permanent``, has no ACTIVE rollout schedule, and at
least one of these holds, reported in this order:

``not_evaluated``
    the installation recorded at least one flag evaluation since the cutoff
    (``evaluations_seen``), and none of this flag;
``on_for_everyone``
    ACTIVE at 100% with no targeting rules, and unchanged since the cutoff;
``off_for_everyone``
    INACTIVE (whatever its rules), or ACTIVE at 0% with no targeting rules, and
    unchanged since the cutoff.

"Unchanged" is ``updated_at`` at or before the cutoff: every writer of it
changes a setting of the flag, so an edit (a rename included), a status
change, a rollout stage or a safety rollback restarts the count. An
evaluation is a ``raw_metrics`` row of type ``flag_evaluation``: each server
answer writes one, and so does each SDK report of local evaluations; the
per-minute counters of those reports are pruned after five minutes, so they
are never read here. Every boundary is inclusive.

The definition is beta: its conditions and threshold may change; the fields
the list carries will not.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Collection, Dict, Iterable, List, Sequence, Set, Tuple
from uuid import UUID

from sqlalchemy import select, true
from sqlalchemy.orm import Session

from backend.app.core.targeting_adapter import normalise_targeting_rules
from backend.app.models.feature_flag import FeatureFlag, flag_status_name
from backend.app.models.metrics.metric import MetricType, RawMetric
from backend.app.models.rollout_schedule import RolloutSchedule, RolloutScheduleStatus

NOT_EVALUATED = "not_evaluated"
ON_FOR_EVERYONE = "on_for_everyone"
OFF_FOR_EVERYONE = "off_for_everyone"
#: The reasons, in the order an item lists them.
REASONS = (NOT_EVALUATED, ON_FOR_EVERYONE, OFF_FOR_EVERYONE)

#: The tag that keeps a flag from ever being stale, compared without case or
#: surrounding spaces.
PERMANENT_TAG = "permanent"

_EVALUATION = MetricType.FLAG_EVALUATION.value


def utc_now() -> datetime:
    """Now, in UTC."""
    return datetime.now(timezone.utc)


async def stale_clock() -> datetime:
    """The time the list classifies at: now, in UTC.

    The list route's dependency, so a test sets the time by overriding it.
    ``async``: the list takes an API key, and on such a route the key's own
    check is the one synchronous step (``test_api_key_routes_shape.py``).
    """
    return utc_now()


def naive_utc(value: datetime) -> datetime:
    """*value* as the naive UTC the columns hold; a naive value is kept."""
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def cutoff_of(now: datetime, days: int) -> datetime:
    """N days before *now*, naive UTC."""
    return naive_utc(now) - timedelta(days=days)


def is_permanent(tags: Any) -> bool:
    """True when *tags* is a list holding the tag ``permanent``.

    Any other shape is no tags; nothing here raises.
    """
    if not isinstance(tags, list):
        return False
    return any(
        isinstance(tag, str) and tag.strip().casefold() == PERMANENT_TAG for tag in tags
    )


def has_no_rules(raw: Any, owner: str = "targeting rules") -> bool:
    """True when the stored ``targeting_rules`` select nobody.

    ``None``, ``{}`` and ``[]`` are no rules, and so is a dict that converts to
    no rule and no default rule (the dashboard's empty groups). A non-empty
    legacy list, and anything that cannot be read, counts as rules: the error
    leans toward a flag not being stale.
    """
    if raw is None:
        return True
    if isinstance(raw, (dict, list)) and not raw:
        return True
    if not isinstance(raw, dict):
        return False
    rules = normalise_targeting_rules(raw, owner=owner)
    if rules is None:
        return False
    return not rules.rules and rules.default_rule is None


def _old_enough(flag: Any, cutoff: datetime) -> bool:
    return naive_utc(flag.created_at) <= cutoff


def _may_be_stale(flag: Any, cutoff: datetime) -> bool:
    return (
        flag_status_name(flag.status) != "ARCHIVED"
        and _old_enough(flag, cutoff)
        and not is_permanent(flag.tags)
    )


def candidate_ids(flags: Iterable[Any], *, now: datetime, days: int) -> List[UUID]:
    """The ids of the flags that may be stale, before the database is asked."""
    cutoff = cutoff_of(now, days)
    return [flag.id for flag in flags if _may_be_stale(flag, cutoff)]


def _one_answer(flag: Any, cutoff: datetime) -> List[str]:
    """``on_for_everyone`` or ``off_for_everyone``, or neither."""
    if naive_utc(flag.updated_at) > cutoff:
        return []
    status = flag_status_name(flag.status)
    if status == "INACTIVE":
        return [OFF_FOR_EVERYONE]
    if status != "ACTIVE":
        return []
    percentage = flag.rollout_percentage
    if percentage not in (0, 100):
        return []
    if not has_no_rules(flag.targeting_rules, owner=f"flag:{flag.key}"):
        return []
    return [ON_FOR_EVERYONE if percentage == 100 else OFF_FOR_EVERYONE]


def classify(
    flags: Iterable[Any],
    *,
    now: datetime,
    days: int,
    evaluations_seen: bool,
    evaluated_ids: Collection[UUID],
    active_schedule_ids: Collection[UUID],
) -> Dict[UUID, List[str]]:
    """Each flag's id and its reasons, in ``REASONS`` order; ``[]`` when it is not stale.

    Pure: the database's answers come in as *evaluations_seen* (the
    installation recorded an evaluation since the cutoff), *evaluated_ids*
    (the flags it recorded one for) and *active_schedule_ids*.
    """
    cutoff = cutoff_of(now, days)
    answer: Dict[UUID, List[str]] = {}
    for flag in flags:
        if not _may_be_stale(flag, cutoff) or flag.id in active_schedule_ids:
            answer[flag.id] = []
            continue
        reasons = []
        if evaluations_seen and flag.id not in evaluated_ids:
            reasons.append(NOT_EVALUATED)
        reasons.extend(_one_answer(flag, cutoff))
        answer[flag.id] = reasons
    return answer


# ---------------------------------------------------------------------------
# The database's answers: one statement each, whatever the number of flags
# ---------------------------------------------------------------------------


def any_evaluation_since(db: Session, since: datetime) -> bool:
    """Whether the installation recorded any flag evaluation at or after *since*.

    ``ORDER BY timestamp DESC LIMIT 1`` reads the timestamp index backwards
    from the newest row, so it stops at the first evaluation however large the
    table is.
    """
    row = db.execute(
        select(RawMetric.id)
        .where(RawMetric.metric_type == _EVALUATION, RawMetric.timestamp >= since)
        .order_by(RawMetric.timestamp.desc())
        .limit(1)
    ).first()
    return row is not None


def evaluated_flag_ids(db: Session, ids: Sequence[UUID], since: datetime) -> Set[UUID]:
    """Those of *ids* with a flag evaluation at or after *since*.

    One statement. Per flag, a LATERAL ``ORDER BY timestamp DESC LIMIT 1``
    reads the (flag, timestamp) index backwards from the flag's newest row and
    stops at its first evaluation. A plain ``EXISTS`` let the planner probe the
    flag-only index instead, which on rows written in time order reads every
    row older than the window first.
    """
    if not ids:
        return set()
    newest = (
        select(RawMetric.id)
        .where(
            RawMetric.feature_flag_id == FeatureFlag.id,
            RawMetric.timestamp >= since,
            RawMetric.metric_type == _EVALUATION,
        )
        .order_by(RawMetric.timestamp.desc())
        .limit(1)
        .lateral()
    )
    rows = db.execute(
        select(FeatureFlag.id).join(newest, true()).where(FeatureFlag.id.in_(ids))
    )
    return {row[0] for row in rows}


def flags_with_an_active_schedule(db: Session, ids: Sequence[UUID]) -> Set[UUID]:
    """Those of *ids* that have a rollout schedule in ACTIVE status."""
    if not ids:
        return set()
    rows = db.execute(
        select(RolloutSchedule.feature_flag_id)
        .where(
            RolloutSchedule.feature_flag_id.in_(ids),
            RolloutSchedule.status == RolloutScheduleStatus.ACTIVE,
        )
        .distinct()
    )
    return {row[0] for row in rows}


def stale_reasons(
    db: Session, flags: Sequence[Any], *, now: datetime, days: int
) -> Tuple[Dict[UUID, List[str]], bool]:
    """Each flag's reasons, and ``evaluations_seen``.

    At most three statements, however many flags: the install-wide guard,
    the ACTIVE schedules and the evaluations of the flags that may be stale.
    """
    since = cutoff_of(now, days)
    evaluations_seen = any_evaluation_since(db, since)
    candidates = candidate_ids(flags, now=now, days=days)
    scheduled = flags_with_an_active_schedule(db, candidates)
    remaining = [flag_id for flag_id in candidates if flag_id not in scheduled]
    evaluated = evaluated_flag_ids(db, remaining, since) if evaluations_seen else set()
    reasons = classify(
        flags,
        now=now,
        days=days,
        evaluations_seen=evaluations_seen,
        evaluated_ids=evaluated,
        active_schedule_ids=scheduled,
    )
    return reasons, evaluations_seen
