"""
Record flag evaluations that a server-side SDK made locally.

An SDK evaluating flags in-process makes no ``/feature-flags/evaluate`` call,
so the server writes no ``flag_evaluation`` row for it -- and safety
monitoring divides a flag's counted errors (evaluation failures and reports
sent with a server-side key) by exactly those rows
(``SafetyService.get_error_metrics``).  With no rows the error rate reads 0.0
and no automatic rollback can fire.  ``POST /api/v1/tracking/evaluations``
closes that gap: the SDK reports per-flag counts and this module turns each
accepted count into one ``RawMetric(flag_evaluation, count=n)`` row, stamped
with the server's receive time so it falls inside the monitor's window at once.

The ceilings
------------
The accepted count is capped, so that a runaway or misconfigured reporter
cannot inflate the denominator without limit:

* at most :data:`KEY_MINUTE_CEILING` per API key, flag and minute;
* at most :data:`FLAG_MINUTE_CEILING` per flag and minute, across every key.

Each cap is a counter row updated by one atomic ``INSERT ... ON CONFLICT DO
UPDATE ... RETURNING``: the row lock serialises concurrent reports for the same
counter across API tasks, and the returned running total says exactly how much
of this report fits.  Nothing reads a counter and then writes it.

When a cap is reached the excess is dropped and reported back in ``errors``.
Dropping evaluations can only make the error rate look *higher* than it is,
which errs towards a rollback rather than away from one.

Locks are taken in one global order (flags sorted by key; for each flag the
per-key row, then the per-flag row), so two reports touching the same flags
cannot deadlock.  Counter rows are pruned a few minutes after their minute,
with ``SKIP LOCKED`` so pruning never waits on, or deadlocks with, a report.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional
from uuid import UUID

from sqlalchemy import delete, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from backend.app.models.feature_flag import FeatureFlag
from backend.app.models.metrics.metric import MetricType, RawMetric
from backend.app.models.sdk_evaluation_count import (
    SdkEvaluationFlagCount,
    SdkEvaluationKeyCount,
)

#: Accepted evaluations per API key, flag and minute.
KEY_MINUTE_CEILING = 100_000
#: Accepted evaluations per flag and minute, summed over every API key.
FLAG_MINUTE_CEILING = 1_000_000
#: Counter rows older than this are deleted.
COUNTER_RETENTION = timedelta(minutes=5)
#: At most this many counter rows of each kind are pruned per report.
PRUNE_BATCH = 500

#: ``meta_data["source"]`` on the ``RawMetric`` rows this module writes.
SOURCE = "sdk_local"


@dataclass(frozen=True)
class EvaluationReport:
    """One flag's counts from one report, after merging duplicate entries."""

    flag_key: str
    count: int
    enabled_count: int
    window_start: datetime
    window_end: datetime


@dataclass
class RecordResult:
    """What was accepted, and every entry that was not (fully) accepted."""

    accepted: int = 0
    errors: List[Dict[str, Any]] = field(default_factory=list)


def minute_of(moment: datetime) -> datetime:
    """The minute *moment* falls in (naive UTC, seconds dropped)."""
    return moment.replace(second=0, microsecond=0)


def _newly_accepted(total_after: int, added: int, ceiling: int) -> int:
    """How much of *added* fits under *ceiling*, given the running total after it.

    The counter's total before this report is ``total_after - added``; the row
    lock taken by the upsert guarantees nothing else changed it in between.
    """
    before = total_after - added
    return max(0, min(total_after, ceiling) - min(before, ceiling))


def _reserve_for_key(
    db: Session, api_key_id: UUID, flag_key: str, minute: datetime, count: int
) -> int:
    """Add *count* to the key's counter; return how much fits under its cap."""
    table = SdkEvaluationKeyCount.__table__
    stmt = pg_insert(table).values(
        api_key_id=api_key_id, flag_key=flag_key, minute=minute, requested=count
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[table.c.api_key_id, table.c.flag_key, table.c.minute],
        set_={"requested": table.c.requested + stmt.excluded.requested},
    ).returning(table.c.requested)
    total = int(db.execute(stmt).scalar_one())
    return _newly_accepted(total, count, KEY_MINUTE_CEILING)


def _reserve_for_flag(db: Session, flag_key: str, minute: datetime, count: int) -> int:
    """Add *count* to the flag's all-keys counter; return how much fits under its cap."""
    table = SdkEvaluationFlagCount.__table__
    stmt = pg_insert(table).values(flag_key=flag_key, minute=minute, offered=count)
    stmt = stmt.on_conflict_do_update(
        index_elements=[table.c.flag_key, table.c.minute],
        set_={"offered": table.c.offered + stmt.excluded.offered},
    ).returning(table.c.offered)
    total = int(db.execute(stmt).scalar_one())
    return _newly_accepted(total, count, FLAG_MINUTE_CEILING)


def reserve(
    db: Session, api_key_id: UUID, flag_key: str, minute: datetime, count: int
) -> int:
    """Take *count* evaluations through both caps; return the number accepted.

    Runs inside the caller's transaction: the counter rows stay locked until it
    commits or rolls back, so a rolled-back report leaves the counters as they
    were.
    """
    if count <= 0:
        return 0
    through_key = _reserve_for_key(db, api_key_id, flag_key, minute, count)
    if through_key == 0:
        return 0
    return _reserve_for_flag(db, flag_key, minute, through_key)


def _prune(db: Session, minute: datetime) -> None:
    """Delete a bounded batch of expired counter rows, skipping locked ones."""
    cutoff = minute - COUNTER_RETENTION
    for model, key_columns in (
        (
            SdkEvaluationKeyCount,
            ("api_key_id", "flag_key", "minute"),
        ),
        (SdkEvaluationFlagCount, ("flag_key", "minute")),
    ):
        table = model.__table__
        columns = [table.c[name] for name in key_columns]
        expired = (
            select(*columns)
            .where(table.c.minute < cutoff)
            .limit(PRUNE_BATCH)
            .with_for_update(skip_locked=True)
        )
        db.execute(delete(table).where(tuple_(*columns).in_(expired)))


def merge_reports(reports: Iterable[EvaluationReport]) -> List[EvaluationReport]:
    """One report per flag key, sorted by key (the lock order)."""
    merged: Dict[str, EvaluationReport] = {}
    for report in reports:
        seen = merged.get(report.flag_key)
        if seen is None:
            merged[report.flag_key] = report
            continue
        merged[report.flag_key] = EvaluationReport(
            flag_key=report.flag_key,
            count=seen.count + report.count,
            enabled_count=seen.enabled_count + report.enabled_count,
            window_start=min(seen.window_start, report.window_start),
            window_end=max(seen.window_end, report.window_end),
        )
    return [merged[key] for key in sorted(merged)]


def record_local_evaluations(
    db: Session,
    api_key_id: UUID,
    reports: Iterable[EvaluationReport],
    received_at: Optional[datetime] = None,
) -> RecordResult:
    """Write the accepted part of *reports* as ``flag_evaluation`` rows, and commit.

    *received_at* is the server's receive time (naive UTC); it stamps every
    row and chooses the counters' minute.  Unknown flag keys and capped counts
    are reported in :attr:`RecordResult.errors`.
    """
    now = received_at or datetime.utcnow()
    minute = minute_of(now)
    merged = merge_reports(reports)
    result = RecordResult()

    keys = [report.flag_key for report in merged]
    flag_ids: Dict[str, UUID] = (
        dict(
            db.query(FeatureFlag.key, FeatureFlag.id)
            .filter(FeatureFlag.key.in_(keys))
            .all()
        )
        if keys
        else {}
    )

    try:
        for report in merged:
            flag_id = flag_ids.get(report.flag_key)
            if flag_id is None:
                result.errors.append(
                    {
                        "flag_key": report.flag_key,
                        "code": "unknown_flag",
                        "message": f"Feature flag with key '{report.flag_key}' not found",
                        "requested": report.count,
                        "accepted": 0,
                    }
                )
                continue

            accepted = reserve(db, api_key_id, report.flag_key, minute, report.count)
            if accepted > 0:
                db.add(
                    RawMetric(
                        metric_type=MetricType.FLAG_EVALUATION.value,
                        timestamp=now,
                        feature_flag_id=flag_id,
                        user_id=None,
                        count=accepted,
                        meta_data={
                            "source": SOURCE,
                            "api_key_id": str(api_key_id),
                            "requested": report.count,
                            "enabled_count": report.enabled_count,
                            "window_start": report.window_start.isoformat(),
                            "window_end": report.window_end.isoformat(),
                        },
                    )
                )
                result.accepted += accepted
            if accepted < report.count:
                result.errors.append(
                    {
                        "flag_key": report.flag_key,
                        "code": "ceiling",
                        "message": (
                            "Evaluation count over the per-minute limit; "
                            f"{report.count - accepted} not recorded"
                        ),
                        "requested": report.count,
                        "accepted": accepted,
                    }
                )

        _prune(db, minute)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return result
