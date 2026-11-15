"""
Shared runner for background scheduler ticks.

All five schedulers (experiment, rollout, metrics, safety, bandit) run their
periodic work through :func:`run_locked_tick`, which gives every tick the same
three guarantees:

1. **Single runner per tick** — the work runs under the PostgreSQL advisory
   lock from :mod:`backend.app.core.scheduler_lock`; a replica that does not
   get the lock skips the tick.
2. **Run history** — every executed tick is persisted through
   :meth:`SchedulerHealthService.record_run` (``scheduler_runs`` table), which
   feeds ``GET /api/v1/scheduler/health``. The write runs in a worker thread
   (:func:`run_in_worker`), never on the event loop's thread.
3. **Prometheus signal** — ``scheduler_last_success_timestamp{name}``,
   ``scheduler_tick_duration_seconds{name}`` and
   ``scheduler_ticks_total{name,status}`` from :mod:`backend.app.core.metrics`.

A tick callable may return ``None``, a :class:`TickResult`, or a mapping with
``items_processed`` / ``items_failed`` / ``metadata`` keys; anything else is
treated as "ran, nothing to report". Exceptions raised by the tick are
recorded as a failed run and then **re-raised** so each scheduler loop keeps
its own error handling and back-off.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping, Optional

from sqlalchemy.engine import Engine

from backend.app.core import metrics as prom
from backend.app.core.scheduler_lock import async_scheduler_lock

logger = logging.getLogger(__name__)

STATUS_SUCCESS = "success"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"


@dataclass
class TickResult:
    """What a scheduler tick reports back."""

    items_processed: int = 0
    items_failed: int = 0
    metadata: dict = field(default_factory=dict)

    @classmethod
    def coerce(cls, value: Any) -> "TickResult":
        if isinstance(value, TickResult):
            return value
        if isinstance(value, Mapping):
            return cls(
                items_processed=int(value.get("items_processed", 0) or 0),
                items_failed=int(value.get("items_failed", 0) or 0),
                metadata=dict(value.get("metadata") or {}),
            )
        return cls()


@dataclass
class TickOutcome:
    """Summary of one call to :func:`run_locked_tick`."""

    name: str
    status: str
    acquired: bool
    started_at: datetime
    completed_at: datetime
    duration_seconds: float
    result: TickResult
    error: Optional[str] = None

    @property
    def ran(self) -> bool:
        return self.acquired


def _persist_run(
    name: str,
    started_at: datetime,
    completed_at: datetime,
    status: str,
    result: TickResult,
    error: Optional[str],
) -> None:
    """Best-effort write of the run record; never raises."""
    try:
        from backend.app.db.session import SessionLocal
        from backend.app.services.scheduler_health_service import SchedulerHealthService

        db = SessionLocal()
        try:
            SchedulerHealthService().record_run(
                db=db,
                scheduler_name=name,
                started_at=started_at,
                completed_at=completed_at,
                status=status,
                items_processed=result.items_processed,
                items_failed=result.items_failed,
                error_msg=error,
                metadata=result.metadata or None,
            )
        finally:
            db.close()
    except Exception as exc:  # pragma: no cover - depends on DB availability
        logger.warning("scheduler %s: could not record run (%s)", name, exc)


async def run_locked_tick(
    name: str,
    tick: Callable[[], Awaitable[Any]],
    *,
    engine: Optional[Engine] = None,
    record: bool = True,
) -> TickOutcome:
    """
    Run one scheduler tick under the advisory lock and record the outcome.

    Args:
        name: Scheduler name (``experiment``, ``rollout``, ``metrics``,
            ``safety``, ``bandit``). Used for the lock key, the run record and
            the metric labels.
        tick: Zero-argument coroutine function doing the work.
        engine: Engine for the lock connection (defaults to the app engine).
        record: Persist the run through ``SchedulerHealthService`` (default).

    Returns:
        A :class:`TickOutcome`. ``outcome.acquired`` is ``False`` when the tick
        was skipped because another replica holds the lock.

    Raises:
        Whatever *tick* raised, after the failed run has been recorded.
    """
    started_at = datetime.now(timezone.utc)
    t0 = time.perf_counter()

    async with async_scheduler_lock(name, engine=engine) as acquired:
        if not acquired:
            completed_at = datetime.now(timezone.utc)
            prom.record_scheduler_tick(name, STATUS_SKIPPED, time.perf_counter() - t0)
            logger.info("scheduler %s: lock held elsewhere, tick skipped", name)
            return TickOutcome(
                name=name,
                status=STATUS_SKIPPED,
                acquired=False,
                started_at=started_at,
                completed_at=completed_at,
                duration_seconds=completed_at.timestamp() - started_at.timestamp(),
                result=TickResult(),
            )

        error: Optional[str] = None
        exc_to_raise: Optional[Exception] = None
        result = TickResult()
        try:
            raw = await tick()
            result = TickResult.coerce(raw)
            if result.items_failed and result.items_processed:
                status = STATUS_PARTIAL
            elif result.items_failed and not result.items_processed:
                status = STATUS_FAILED
                error = f"{result.items_failed} item(s) failed"
            else:
                status = STATUS_SUCCESS
        except asyncio.CancelledError:
            # Shutdown: release the lock (context manager) and propagate
            # without recording a bogus "failed" run.
            raise
        except Exception as exc:
            status = STATUS_FAILED
            error = str(exc) or exc.__class__.__name__
            exc_to_raise = exc

    completed_at = datetime.now(timezone.utc)
    duration = time.perf_counter() - t0

    prom.record_scheduler_tick(name, status, duration)
    if status == STATUS_SUCCESS:
        prom.record_scheduler_success(name)

    if record:
        # In a worker thread, like the tick bodies (#1690): the write checks a
        # connection out of the pool, and when every connection is held by a
        # request waiting for this event loop, a checkout made on the loop's
        # own thread waits the whole pool timeout and stops the API answering
        # for that long -- and then the run record is lost as well.
        await run_in_worker(
            functools.partial(
                _persist_run, name, started_at, completed_at, status, result, error
            ),
            label=f"{name} run record",
        )

    if exc_to_raise is not None:
        raise exc_to_raise

    return TickOutcome(
        name=name,
        status=status,
        acquired=True,
        started_at=started_at,
        completed_at=completed_at,
        duration_seconds=duration,
        result=result,
        error=error,
    )


async def run_in_worker(work: Callable[[], Any], *, label: str) -> Any:
    """
    Run *work* in a worker thread; the event loop awaits it (#1690).

    For a tick whose body is synchronous (SQLAlchemy queries, HTTP sends): run
    on the event loop, it stops the API answering anything else until it
    finishes. *work* must be a plain callable. A coroutine function cannot be
    handed over as it is -- ``asyncio.to_thread`` on one only creates the
    coroutine and never runs it, and the tick is recorded as a success that
    did nothing -- so wrap it to create and run the coroutine inside the
    thread, on a private event loop: ``lambda: asyncio.run(body())``.

    This coroutine does not return before the thread does, in both ways it
    can be cancelled: when its own task is cancelled (it waits for the
    thread, then re-raises the cancellation), and when the event loop is torn
    down (``asyncio.run`` cancels every task). The second holds because the
    worker is a plain ``Future`` from ``run_in_executor``, not a task, so
    teardown does not cancel it; a task such as
    ``ensure_future(to_thread(...))`` would be cancelled with the rest and
    this coroutine would return at once. :func:`run_locked_tick` releases the
    scheduler's advisory lock when the tick returns, so returning early would
    let another replica's tick run beside a thread that is still running.
    The context copy is what ``asyncio.to_thread`` does.

    Args:
        work: Zero-argument callable, run in the loop's default executor.
        label: What *work* does, for the one ERROR logged when it fails after
            the tick was cancelled (``"<label> failed after the tick was
            cancelled: ..."``).

    Returns:
        What *work* returned; raises what it raised.
    """
    worker = asyncio.get_running_loop().run_in_executor(
        None, functools.partial(contextvars.copy_context().run, work)
    )
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        await _wait_for_worker(worker, label)
        raise


async def _wait_for_worker(worker: "asyncio.Future[Any]", label: str) -> None:
    """
    Wait until *worker* is done, absorbing any further cancellation.

    ``asyncio.wait`` neither cancels *worker* nor raises its exception, so a
    second cancel during shutdown cannot end the wait early either.
    """
    while not worker.done():
        try:
            await asyncio.wait({worker})
        except asyncio.CancelledError:
            continue
    if not worker.cancelled() and worker.exception() is not None:
        logger.error(
            "%s failed after the tick was cancelled: %s", label, worker.exception()
        )


__all__ = [
    "STATUS_FAILED",
    "STATUS_PARTIAL",
    "STATUS_SKIPPED",
    "STATUS_SUCCESS",
    "TickOutcome",
    "TickResult",
    "run_in_worker",
    "run_locked_tick",
]
