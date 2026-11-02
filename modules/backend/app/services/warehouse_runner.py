"""Warehouse analysis runs and source previews: admission, execution, results.

Admission
---------
Every query sent to a customer's warehouse on someone's behalf -- the
statements of an analysis run, or a source preview -- is admitted here first,
in three steps that never wait for capacity:

1. **An executor slot is reserved** (:meth:`WarehouseExecutor.try_submit`).  A
   full executor answers 429 ``warehouse_busy`` and nothing is written, so a
   refusal for capacity never uses a slot of the daily limit.  The reserved
   job does nothing until step 3.
2. **One transaction**, under ``pg_advisory_xact_lock`` on a constant key, so
   admissions across every API task are serialised:

   a. runs that stopped reporting progress (no heartbeat for their total
      deadline plus 300 s) are marked ``failed``/``abandoned``;
   b. the **per-connection daily limit**: every run and preview on the
      connection since 00:00 UTC counts, whatever its status; at the limit the
      answer is 429 ``daily_run_limit_reached`` with ``Retry-After`` set to
      the seconds until 00:00 UTC;
   c. the **per-deployment limit**: queued plus running analyses and previews
      across every connection, at most ``WAREHOUSE_MAX_CONCURRENT_RUNS``,
      else 429 ``warehouse_busy``;
   d. the row is inserted ``queued``.  The partial unique index
      ``uq_warehouse_runs_one_in_flight`` allows one queued or running row per
      connection -- analyses and previews alike -- so a second one fails as
      409 ``run_in_progress``.

   If step 2 refuses, the reservation is cancelled and the slot freed.
3. The reservation is released with the committed run's id, and the job runs.

Connection tests and source validation read metadata only; they are not
recorded, not counted by either limit, and bounded by the executor's thread
count and their own 30-second total.

Execution
---------
The job runs on the warehouse executor, within the run's total deadline
(``(1 + metrics) x query_timeout_seconds + 60 s``), with its own database
session.  It writes a heartbeat between statements.  A warehouse failure
fails the run with its code; a metric whose returned rows are refused is
"not computed" with its reason, and the others still report.  A failed run
never reports numbers.

Proportion results come from the core estimator ``/results`` uses, and mean
results from the core estimator for centred sums (both through
:mod:`.warehouse_estimators`); SRM from
:func:`backend.app.services.srm_service.compute_srm`, skipped for adaptive
allocation.  A mean metric the core estimator refuses (a negative variance) is
"not computed" as ``result_invalid``; one with fewer than 2 units in a variant
as ``fewer_than_2_units``.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.services.srm_service import compute_srm
from backend.app.services.sufficient_stats_analysis import (
    SufficientStatsNotComputed,
    SufficientStatsRefused,
)
from modules.backend.app.models.warehouse_analysis_run import (
    IN_FLIGHT_STATUSES,
    WarehouseAnalysisRun,
)
from modules.backend.app.services import warehouse_estimators
from modules.backend.app.services.warehouse_clients import WarehouseClient
from modules.backend.app.services.warehouse_query_builder import (
    LABEL_MAX_CHARS,
    BuiltQuery,
    is_utc_session_offset,
)
from modules.backend.app.services.warehouse_run_accounting import (
    daily_limit_reached,
    resets_at,
    runs_counted_today,
    seconds_until_reset,
)
from modules.backend.app.services.warehouse_sufficient_stats import (
    MetricSufficientStatistics,
    WarehouseResultRefused,
    parse_diagnostics_rows,
    parse_metric_rows,
)
from modules.backend.app.warehouse.deadlines import Deadline
from modules.backend.app.warehouse.errors import (
    MESSAGES,
    WarehouseError,
    WarehouseErrorCode,
)
from modules.backend.app.warehouse.executor import JobKind, WarehouseExecutor

logger = logging.getLogger(__name__)

#: The advisory lock every admission takes (``hashtext`` of this name).
ADMISSION_LOCK_NAME = "experimently.warehouse.runs"
#: A run with no heartbeat for its total deadline plus this is abandoned.
STALE_MARGIN_SECONDS = 300
#: The longest a reserved job waits for its admission to commit.
ADMISSION_WAIT_SECONDS = 30.0
RESULTS_SCHEMA = "experimently.warehouse.results/v1"

SessionFactory = Callable[[], Session]
Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# -- refusals -------------------------------------------------------------------


class AdmissionRefused(Exception):
    """A run or preview was not admitted.  Carries the HTTP answer."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers or {}
        self.extra = extra or {}

    def detail(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.extra}


def _busy() -> AdmissionRefused:
    return AdmissionRefused(
        429,
        "warehouse_busy",
        MESSAGES[WarehouseErrorCode.WAREHOUSE_BUSY],
        headers={"Retry-After": "30"},
    )


def _in_progress() -> AdmissionRefused:
    return AdmissionRefused(
        409, "run_in_progress", MESSAGES[WarehouseErrorCode.RUN_IN_PROGRESS]
    )


def _duration(seconds: int) -> str:
    hours, rest = divmod(max(seconds, 0), 3600)
    minutes = rest // 60
    return f"{hours} h {minutes} min" if hours else f"{minutes} min"


def daily_limit_refusal(
    connection_name: str, limit: int, now: datetime
) -> AdmissionRefused:
    wait = seconds_until_reset(now)
    reset = resets_at(now)
    return AdmissionRefused(
        429,
        "daily_run_limit_reached",
        (
            f"Not run: {connection_name} has reached its limit of {limit} analyses "
            "per day (UTC, previews included). The count resets at 00:00 UTC, in "
            f"{_duration(wait)}. An admin can change the limit in Warehouse › "
            f"Connections › {connection_name}."
        ),
        headers={"Retry-After": str(wait)},
        extra={
            "limit": limit,
            "resets_at": reset.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    )


# -- the reservation ------------------------------------------------------------


class Reservation:
    """Holds a reserved job until its admission commits, or is cancelled."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._run_id: Optional[UUID] = None
        self._cancelled = False

    def release(self, run_id: UUID) -> None:
        self._run_id = run_id
        self._event.set()

    def cancel(self) -> None:
        self._cancelled = True
        self._event.set()

    def wait(self, deadline: Deadline) -> Optional[UUID]:
        """The run id once released; ``None`` if cancelled or never released."""
        released = self._event.wait(min(ADMISSION_WAIT_SECONDS, deadline.remaining()))
        if not released or self._cancelled:
            return None
        return self._run_id


def reserve(
    executor: WarehouseExecutor,
    job: Callable[[Deadline, UUID], Any],
    *,
    kind: JobKind,
    connection_id: UUID,
    total_seconds: float,
) -> Tuple[Reservation, Any]:
    """Step 1: take an executor slot for ``job`` now, or refuse now."""
    reservation = Reservation()

    def gated(deadline: Deadline) -> Any:
        run_id = reservation.wait(deadline)
        if run_id is None:
            return None
        return job(deadline, run_id)

    try:
        future = executor.try_submit(
            gated,
            total_seconds=total_seconds,
            kind=kind,
            key=f"connection:{connection_id}",
        )
    except WarehouseError as exc:
        if exc.code is WarehouseErrorCode.RUN_IN_PROGRESS:
            raise _in_progress() from None
        raise _busy() from None
    return reservation, future


# -- step 2: the admission transaction ----------------------------------------


def _naive_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def sweep_abandoned(db: Session, now: datetime) -> int:
    """Mark in-flight rows that stopped reporting progress as abandoned."""
    rows = db.execute(
        select(
            WarehouseAnalysisRun.id,
            WarehouseAnalysisRun.request,
            WarehouseAnalysisRun.heartbeat_at,
            WarehouseAnalysisRun.created_at,
        ).where(WarehouseAnalysisRun.status.in_(IN_FLIGHT_STATUSES))
    ).all()
    stale: List[UUID] = []
    for run_id, request, heartbeat_at, created_at in rows:
        total = float((request or {}).get("total_seconds") or 0)
        last = heartbeat_at
        if last is None and created_at is not None:
            last = created_at.replace(tzinfo=timezone.utc)
        if last is None:
            continue
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if now - last > timedelta(seconds=total + STALE_MARGIN_SECONDS):
            stale.append(run_id)
    if stale:
        db.execute(
            update(WarehouseAnalysisRun)
            .where(
                WarehouseAnalysisRun.id.in_(stale),
                WarehouseAnalysisRun.status.in_(IN_FLIGHT_STATUSES),
            )
            .values(status="failed", error_code="abandoned", finished_at=now)
        )
    return len(stale)


@dataclass
class NewRun:
    """What step 2 inserts."""

    kind: str
    connection_id: UUID
    connection_name: str
    warehouse_type: str
    max_runs_per_day: int
    experiment_id: Optional[UUID]
    requested_by: Optional[UUID]
    request: Dict[str, Any]
    window_start: Optional[datetime]
    window_end: Optional[datetime]
    statements: List[Dict[str, Any]]


def admit(db: Session, new: NewRun, *, now: datetime, max_concurrent_runs: int) -> UUID:
    """Step 2, in one transaction; commits and returns the run id, or raises."""
    try:
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:name))"),
            {"name": ADMISSION_LOCK_NAME},
        )
        sweep_abandoned(db, now)
        used = runs_counted_today(db, new.connection_id, now)
        if daily_limit_reached(used, new.max_runs_per_day):
            raise daily_limit_refusal(new.connection_name, new.max_runs_per_day, now)
        in_flight = db.execute(
            select(func.count()).where(
                WarehouseAnalysisRun.status.in_(IN_FLIGHT_STATUSES)
            )
        ).scalar_one()
        if in_flight >= max_concurrent_runs:
            raise _busy()
        run = WarehouseAnalysisRun(
            kind=new.kind,
            status="queued",
            connection_id=new.connection_id,
            connection_name=new.connection_name,
            warehouse_type=new.warehouse_type,
            experiment_id=new.experiment_id,
            requested_by=new.requested_by,
            request=new.request,
            window_start=new.window_start,
            window_end=new.window_end,
            statements=new.statements,
            created_at=_naive_utc(now),
            updated_at=_naive_utc(now),
        )
        db.add(run)
        db.flush()
        run_id = run.id
        db.commit()
        return run_id
    except IntegrityError:
        db.rollback()
        raise _in_progress() from None
    except BaseException:
        db.rollback()
        raise


# -- execution --------------------------------------------------------------------


@dataclass(frozen=True)
class VariantInfo:
    id: UUID
    name: str
    is_control: bool
    traffic_allocation: Optional[float]


@dataclass(frozen=True)
class MetricPlan:
    """One metric of a run, as its source stood when the run was admitted.

    ``lower_is_better`` is the source's direction (#1386) and has no default:
    a construction site that forgets it fails with ``TypeError`` instead of
    analysing the metric as higher is better.
    """

    source_id: UUID
    name: str
    metric_type: str
    is_primary: bool
    lower_is_better: bool
    query: BuiltQuery


@dataclass(frozen=True)
class RunPlan:
    """Everything the job needs, loaded before it runs (no ORM objects)."""

    client: WarehouseClient
    diagnostics: BuiltQuery
    metrics: Tuple[MetricPlan, ...]
    variants: Tuple[VariantInfo, ...]
    variant_map: Mapping[str, UUID]
    fixed_allocation: bool
    alpha: float
    correction_method: str
    session_factory: SessionFactory
    clock: Clock = utc_now
    estimator: warehouse_estimators.BinomialEstimator = (
        warehouse_estimators.binomial_metric_result
    )
    mean_estimator: warehouse_estimators.MeanEstimator = (
        warehouse_estimators.mean_metric_result
    )


def statements_json(queries: Sequence[BuiltQuery]) -> List[Dict[str, Any]]:
    return [
        {"kind": q.kind, "dialect": q.dialect, "sha256": q.sha256, "sql": q.sql}
        for q in queries
    ]


_METADATA_FIELDS = (
    "job_id",
    "statement_handle",
    "location",
    "warehouse",
    "total_bytes_processed",
    "total_bytes_billed",
    "elapsed_ms",
)


def job_metadata(kind: str, result: Any) -> Dict[str, Any]:
    entry: Dict[str, Any] = {"statement": kind}
    for name in _METADATA_FIELDS:
        value = getattr(result, name, None)
        if isinstance(value, (int, str)) and not isinstance(value, bool):
            entry[name] = value
    return entry


def _heartbeat(factory: SessionFactory, run_id: UUID, now: datetime) -> None:
    with factory() as db:
        db.execute(
            update(WarehouseAnalysisRun)
            .where(WarehouseAnalysisRun.id == run_id)
            .values(heartbeat_at=now)
        )
        db.commit()


def _finish(
    factory: SessionFactory,
    run_id: UUID,
    now: datetime,
    *,
    error_code: Optional[str] = None,
    **values: Any,
) -> None:
    with factory() as db:
        db.execute(
            update(WarehouseAnalysisRun)
            .where(WarehouseAnalysisRun.id == run_id)
            .values(
                status="failed" if error_code else "succeeded",
                error_code=error_code,
                finished_at=now,
                heartbeat_at=now,
                **values,
            )
        )
        db.commit()


def _start(factory: SessionFactory, run_id: UUID, now: datetime) -> None:
    with factory() as db:
        db.execute(
            update(WarehouseAnalysisRun)
            .where(WarehouseAnalysisRun.id == run_id)
            .values(status="running", started_at=now, heartbeat_at=now)
        )
        db.commit()


def failure_code(exc: BaseException) -> str:
    """The fixed run error code for an exception raised while running."""
    if isinstance(exc, WarehouseError):
        return exc.code.value
    if isinstance(exc, WarehouseResultRefused):
        return exc.code
    return WarehouseErrorCode.INTERNAL.value


def run_message(code: Optional[str]) -> Optional[str]:
    """The fixed copy for a run error code."""
    if code is None:
        return None
    try:
        return WarehouseError(WarehouseErrorCode(code)).message
    except ValueError:
        return MESSAGES[WarehouseErrorCode.INTERNAL]


@dataclass(frozen=True)
class ArmStats:
    """One experiment variant's statistics, summed over the labels mapped to it.

    ``sum_d`` and ``sum_d2`` are centred on the metric statement's grand mean
    ``k``, which every label shares, so they add across labels like counts.
    """

    n: int = 0
    n_converted: int = 0
    sum_d: float = 0.0
    sum_d2: float = 0.0


def _map_labels(
    stats: MetricSufficientStatistics,
    variants: Sequence[VariantInfo],
    variant_map: Mapping[str, UUID],
) -> Tuple[Dict[UUID, ArmStats], Dict[UUID, List[str]], List[Dict[str, Any]]]:
    """Statistics per experiment variant, the labels behind each, and unmapped
    labels."""
    by_name = {v.name[:LABEL_MAX_CHARS]: v.id for v in variants}
    by_id = {str(v.id): v.id for v in variants}
    arms: Dict[UUID, ArmStats] = {v.id: ArmStats() for v in variants}
    labels: Dict[UUID, List[str]] = {v.id: [] for v in variants}
    unmapped: List[Dict[str, Any]] = []
    for row in stats.variants:
        label = row.variant[:LABEL_MAX_CHARS]
        target = variant_map.get(label) or by_name.get(label) or by_id.get(label)
        if target is None or target not in arms:
            unmapped.append({"label": label, "units": row.n})
            continue
        arm = arms[target]
        arms[target] = ArmStats(
            arm.n + row.n,
            arm.n_converted + row.n_converted,
            arm.sum_d + row.sum_d,
            arm.sum_d2 + row.sum_d2,
        )
        labels[target].append(label)
    return arms, labels, unmapped


def _not_computed(
    plan: MetricPlan, code: str, message: Optional[str] = None
) -> Dict[str, Any]:
    return {
        "metric_source_id": str(plan.source_id),
        "name": plan.name,
        "metric_type": plan.metric_type,
        "is_primary": plan.is_primary,
        "computed": False,
        "not_computed_reason": code,
        "message": message or f"Not computed: {run_message(code) or code}",
        "result": None,
    }


def _metric_result(
    plan: RunPlan,
    metric: MetricPlan,
    outcome: MetricSufficientStatistics,
    variants: Sequence[VariantInfo],
    arms: Mapping[UUID, ArmStats],
) -> Dict[str, Any]:
    """One metric's computed result, or its "not computed" entry.

    ``variants`` is every experiment variant, the control first.
    """
    refs = [
        warehouse_estimators.VariantRef(v.id, v.name, v.is_control) for v in variants
    ]
    if metric.metric_type == "mean":
        try:
            result = warehouse_estimators.mean_result(
                outcome.k,
                [
                    warehouse_estimators.VariantMeanSums(
                        ref, arms[ref.id].n, arms[ref.id].sum_d, arms[ref.id].sum_d2
                    )
                    for ref in refs
                ],
                alpha=plan.alpha,
                correction_method=plan.correction_method,
                metric=warehouse_estimators.MetricRef(
                    metric.source_id,
                    metric.name,
                    metric.is_primary,
                    "mean",
                    lower_is_better=metric.lower_is_better,
                ),
                estimator=plan.mean_estimator,
            )
        except SufficientStatsNotComputed as exc:
            # The estimator's own words: "Not computed: fewer than 2 units".
            return _not_computed(metric, exc.code, exc.message)
        except SufficientStatsRefused as exc:
            return _not_computed(metric, exc.code)
    else:
        result = warehouse_estimators.proportion_result(
            [
                warehouse_estimators.VariantCounts(
                    ref, arms[ref.id].n, arms[ref.id].n_converted
                )
                for ref in refs
            ],
            alpha=plan.alpha,
            correction_method=plan.correction_method,
            metric=warehouse_estimators.MetricRef(
                metric.source_id,
                metric.name,
                metric.is_primary,
                lower_is_better=metric.lower_is_better,
            ),
            estimator=plan.estimator,
        )
    return {
        "metric_source_id": str(metric.source_id),
        "name": metric.name,
        "metric_type": metric.metric_type,
        "is_primary": metric.is_primary,
        "computed": True,
        "not_computed_reason": None,
        "message": None,
        "result": result,
    }


def execute_run(plan: RunPlan, deadline: Deadline, run_id: UUID) -> Optional[str]:
    """Run the statements and store the results.  Returns the error code, if any.

    Called on the warehouse executor's thread.  Every outcome is written to the
    run row; the exception, if any, is not re-raised (its type name is logged).
    """
    factory = plan.session_factory
    _start(factory, run_id, plan.clock())
    try:
        values = _execute(plan, deadline, run_id)
    except BaseException as exc:  # every failure ends the run with a code
        code = failure_code(exc)
        if not isinstance(exc, (WarehouseError, WarehouseResultRefused)):
            logger.error(
                "warehouse run failed: %s",
                type(exc).__name__,
                extra={"warehouse_error": code, "run_id": str(run_id)},
            )
        _finish(factory, run_id, plan.clock(), error_code=code)
        return code
    _finish(factory, run_id, plan.clock(), **values)
    return None


def _execute(plan: RunPlan, deadline: Deadline, run_id: UUID) -> Dict[str, Any]:
    client = plan.client
    expect_offset = client.dialect.session_offset is not None
    metadata: List[Dict[str, Any]] = []

    answer = client.adapter.run_query(plan.diagnostics, deadline)
    metadata.append(job_metadata("diagnostics", answer))
    _heartbeat(plan.session_factory, run_id, plan.clock())
    diagnostics = parse_diagnostics_rows(
        list(answer.rows), expect_session_offset=expect_offset
    )

    parsed: List[Tuple[MetricPlan, Any]] = []
    for metric in plan.metrics:
        answer = client.adapter.run_query(metric.query, deadline)
        metadata.append(job_metadata("metric", answer))
        _heartbeat(plan.session_factory, run_id, plan.clock())
        try:
            parsed.append(
                (
                    metric,
                    parse_metric_rows(
                        list(answer.rows), expect_session_offset=expect_offset
                    ),
                )
            )
        except WarehouseResultRefused as refused:
            if refused.code == "timezone_not_utc":
                raise
            parsed.append((metric, refused))

    variants = sorted(plan.variants, key=lambda v: (not v.is_control, v.name))
    metric_results: List[Dict[str, Any]] = []
    sufficient: Dict[str, Any] = {
        "diagnostics": dataclasses.asdict(diagnostics),
        "metrics": {},
    }
    srm_counts: Optional[Dict[UUID, ArmStats]] = None
    variant_summary: Optional[List[Dict[str, Any]]] = None
    unmapped_labels: List[Dict[str, Any]] = []

    for metric, outcome in parsed:
        if isinstance(outcome, WarehouseResultRefused):
            metric_results.append(_not_computed(metric, outcome.code))
            continue
        sufficient["metrics"][str(metric.source_id)] = outcome.to_json()
        arms, labels, unmapped = _map_labels(outcome, variants, plan.variant_map)
        if srm_counts is None:
            srm_counts = arms
            unmapped_labels = unmapped
            variant_summary = [
                {
                    "variant_id": str(v.id),
                    "variant_name": v.name,
                    "is_control": v.is_control,
                    "labels": labels[v.id],
                    "units": arms[v.id].n,
                }
                for v in variants
            ]
        control = next((v for v in variants if v.is_control), None)
        if control is None or arms[control.id].n == 0:
            metric_results.append(_not_computed(metric, "no_units"))
            continue
        metric_results.append(_metric_result(plan, metric, outcome, variants, arms))

    srm: Optional[Dict[str, Any]] = None
    srm_skipped: Optional[str] = None
    if not plan.fixed_allocation:
        srm_skipped = "adaptive_allocation"
    elif srm_counts is not None:
        found = compute_srm(
            observed={str(k): arm.n for k, arm in srm_counts.items()},
            allocations={
                str(v.id): float(v.traffic_allocation or 0) for v in plan.variants
            },
        )
        srm = found.to_dict() if found is not None else None

    results = {
        "schema": RESULTS_SCHEMA,
        "diagnostics": dataclasses.asdict(diagnostics),
        "variants": variant_summary or [],
        "unmapped_labels": unmapped_labels,
        "srm": srm,
        "srm_skipped": srm_skipped,
        "metrics": metric_results,
    }
    return {
        "results": results,
        "sufficient_statistics": sufficient,
        "job_metadata": metadata,
    }


# -- previews -----------------------------------------------------------------------


def _signed_int(value: Any, name: str) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        raise WarehouseResultRefused("result_invalid", f"{name} is not an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text_value = value.strip()
        body = text_value[1:] if text_value[:1] in "+-" else text_value
        if body.isdigit() and len(body) <= 12:
            return int(text_value)
    raise WarehouseResultRefused("result_invalid", f"{name} is not an integer")


def _count(value: Any, name: str) -> int:
    number = _signed_int(value, name)
    if number is None or number < 0:
        raise WarehouseResultRefused("result_invalid", f"{name} is not a count")
    return number


def _epoch(value: Any, name: str) -> Optional[datetime]:
    number = _signed_int(value, name)
    if number is None:
        return None
    try:
        return datetime.fromtimestamp(number, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise WarehouseResultRefused(
            "result_invalid", f"{name} is out of range"
        ) from None


def parse_preview_rows(
    kind: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    is_mean: bool = False,
    expect_session_offset: bool = False,
) -> Dict[str, Any]:
    """Aggregates from a preview statement; nothing else is kept."""
    if not rows:
        raise WarehouseResultRefused("result_invalid", "a preview returns rows")
    if expect_session_offset and any(
        not is_utc_session_offset(row.get("session_offset")) for row in rows
    ):
        raise WarehouseResultRefused("timezone_not_utc", "session not in UTC")
    first = rows[0]
    summary: Dict[str, Any] = {
        "total_rows": _count(first.get("total_rows"), "total_rows"),
        "null_unit_rows": _count(first.get("null_unit_rows"), "null_unit_rows"),
        "earliest": _epoch(first.get("earliest"), "earliest"),
        "latest": _epoch(first.get("latest"), "latest"),
    }
    if kind == "metric":
        if len(rows) != 1:
            raise WarehouseResultRefused("result_invalid", "one row expected")
        summary["null_value_rows"] = (
            _count(first.get("null_value_rows"), "null_value_rows") if is_mean else None
        )
        return summary
    if len(rows) > 51:
        raise WarehouseResultRefused("too_many_variant_values", "too many labels")
    summary["null_variant_rows"] = _count(
        first.get("null_variant_rows"), "null_variant_rows"
    )
    variants = []
    for row in rows:
        label = row.get("variant")
        if label is None:
            continue
        if not isinstance(label, str):
            raise WarehouseResultRefused("result_invalid", "a label is not text")
        variants.append(
            {
                "label": label[:LABEL_MAX_CHARS],
                "units": _count(row.get("units"), "units"),
            }
        )
    summary["variants"] = variants
    return summary


def execute_preview(
    client: WarehouseClient,
    query: BuiltQuery,
    kind: str,
    is_mean: bool,
    session_factory: SessionFactory,
    clock: Clock,
    deadline: Deadline,
    run_id: UUID,
) -> Dict[str, Any]:
    """Run one preview statement; store and return its aggregates.

    Raises the failure (as a :class:`WarehouseError` or
    :class:`WarehouseResultRefused`) after recording it on the run row.
    """
    _start(session_factory, run_id, clock())
    failure: Optional[BaseException] = None
    summary: Dict[str, Any] = {}
    metadata: List[Dict[str, Any]] = []
    try:
        answer = client.adapter.run_query(query, deadline)
        metadata.append(job_metadata("preview", answer))
        summary = parse_preview_rows(
            kind,
            list(answer.rows),
            is_mean=is_mean,
            expect_session_offset=client.dialect.session_offset is not None,
        )
    except (WarehouseError, WarehouseResultRefused) as exc:
        failure = exc
    except BaseException as exc:
        logger.error(
            "warehouse preview failed: %s",
            type(exc).__name__,
            extra={"warehouse_error": "internal", "run_id": str(run_id)},
        )
        failure = WarehouseError(WarehouseErrorCode.INTERNAL)
    if failure is not None:
        _finish(session_factory, run_id, clock(), error_code=failure_code(failure))
        if isinstance(failure, WarehouseResultRefused):
            # The executor passes only WarehouseError through; every
            # result code is also one of its codes.
            raise WarehouseError(WarehouseErrorCode(failure_code(failure)))
        raise failure
    stored = {
        key: (value.isoformat() if isinstance(value, datetime) else value)
        for key, value in summary.items()
    }
    _finish(session_factory, run_id, clock(), results=stored, job_metadata=metadata)
    return summary


__all__ = [
    "ADMISSION_LOCK_NAME",
    "AdmissionRefused",
    "ArmStats",
    "MetricPlan",
    "NewRun",
    "Reservation",
    "RunPlan",
    "VariantInfo",
    "admit",
    "daily_limit_refusal",
    "execute_preview",
    "execute_run",
    "failure_code",
    "parse_preview_rows",
    "reserve",
    "run_message",
    "statements_json",
    "sweep_abandoned",
]
