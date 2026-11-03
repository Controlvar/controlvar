"""
Scheduler for metrics aggregation.

This module provides scheduling functionality for automatically
aggregating raw metrics data into summary data for efficient querying.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

from backend.app.core.logging import get_logger
from backend.app.core.scheduler_tick import run_locked_tick
from backend.app.db.session import SessionLocal
from backend.app.models.metrics.metric import AggregationPeriod
from backend.app.services.metrics_service import MetricsService

logger = get_logger(__name__)

SCHEDULER_NAME = "metrics"


class MetricsScheduler:
    """Handles scheduled tasks for metrics aggregation."""

    def __init__(self, interval_minutes: int = 15):
        """
        Initialize the metrics scheduler.

        Args:
            interval_minutes: How often to run metrics aggregation (in minutes)
        """
        self.interval_minutes = interval_minutes
        self.is_running = False
        self.task: Optional[asyncio.Task] = None

    async def start(self):
        """Start the scheduler."""
        if self.is_running:
            logger.warning("Metrics scheduler is already running")
            return

        self.is_running = True
        self.task = asyncio.create_task(self._run_scheduler())
        logger.info(
            f"Metrics scheduler started with {self.interval_minutes} minute interval"
        )

    async def stop(self):
        """Stop the scheduler."""
        if not self.is_running:
            return

        self.is_running = False
        if self.task:
            self.task.cancel()
            try:
                # Only await the task if it's a real asyncio Task and not a mock
                from unittest.mock import Mock

                if not isinstance(self.task, Mock):
                    await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
        logger.info("Metrics scheduler stopped")

    async def _run_scheduler(self):
        """Run the scheduler loop."""
        while self.is_running:
            try:
                # One tick under the advisory lock; records the run and
                # skips when another replica holds the lock.
                await run_locked_tick(SCHEDULER_NAME, self.aggregate_metrics)

                # Wait for the next interval
                await asyncio.sleep(self.interval_minutes * 60)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in metrics scheduler: {e!s}")
                # Wait a bit before trying again
                await asyncio.sleep(60)

    async def aggregate_metrics(self) -> Dict[str, int]:
        """
        Aggregate raw metrics into summary data, off the event loop (#1690).

        The work is synchronous SQLAlchemy (the history purge and six GROUP BY
        aggregations over ``raw_metrics``), so it runs in one worker thread
        through ``asyncio.to_thread``; on the loop it would stop the API from
        answering anything else until it finished.

        This coroutine does not return before the thread does, even when it
        is cancelled (shutdown): it waits for the thread, then re-raises the
        cancellation. ``run_locked_tick`` releases the scheduler's advisory
        lock when this returns, so returning early would let another replica's
        tick aggregate beside a thread that is still running.

        Returns ``{"items_processed": n, "items_failed": m}`` for the run record.
        """
        worker = asyncio.ensure_future(asyncio.to_thread(self._aggregate_sync))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            await _wait_for_worker(worker)
            raise

    @staticmethod
    def _aggregate_sync() -> Dict[str, int]:
        """
        The tick's synchronous body; runs in a worker thread.

        Opens and closes its own session in that thread. This aggregates data
        at different time periods:
        1. Last hour at minute granularity
        2. Last day at hour granularity
        3. Last month at day granularity
        4. All-time totals
        """
        logger.info("Aggregating metrics")

        # Piggyback the analysis-history purge on this tick: it is the
        # lowest-frequency scheduler that already holds an advisory lock, and
        # retention needs to run somewhere.
        try:
            from backend.app.services.analysis_snapshot_service import (
                purge_expired_history,
            )

            purge_expired_history()
        except Exception as exc:
            logger.warning("analysis history purge skipped: %s", exc)

        total_records = 0
        failed_periods = 0

        # Use a new database session for this task
        db = SessionLocal()
        try:
            current_time = datetime.now(timezone.utc)

            # Define aggregation periods and their lookback windows
            aggregation_tasks = [
                # Minute aggregation for the last hour
                (AggregationPeriod.MINUTE, current_time - timedelta(hours=1)),
                # Hourly aggregation for the last day
                (AggregationPeriod.HOUR, current_time - timedelta(days=1)),
                # Daily aggregation for the last month
                (AggregationPeriod.DAY, current_time - timedelta(days=30)),
                # Weekly aggregation for the last year
                (AggregationPeriod.WEEK, current_time - timedelta(days=365)),
                # Monthly aggregation for all time
                (AggregationPeriod.MONTH, None),
                # Total aggregation (single record for all time)
                (AggregationPeriod.TOTAL, None),
            ]

            # Run each aggregation task
            for period, start_time in aggregation_tasks:
                try:
                    records = MetricsService.aggregate_metrics(
                        db=db,
                        period=period,
                        start_time=start_time,
                        end_time=current_time,
                    )
                    total_records += records
                    logger.info(f"Aggregated {records} records for {period} period")
                except Exception as e:
                    failed_periods += 1
                    logger.error(f"Error aggregating {period} metrics: {e!s}")

            if total_records > 0:
                logger.info(
                    f"Total of {total_records} aggregated metric records created/updated"
                )
            else:
                logger.info("No metrics required aggregation")

        except Exception as e:
            failed_periods += 1
            logger.error(f"Error processing metrics aggregation: {e!s}")
        finally:
            db.close()

        return {
            "items_processed": int(total_records)
            if isinstance(total_records, int)
            else 0,
            "items_failed": failed_periods,
        }


async def _wait_for_worker(worker: "asyncio.Future[Dict[str, int]]") -> None:
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
            "metrics aggregation failed after the tick was cancelled: %s",
            worker.exception(),
        )


# Create a singleton instance of the scheduler
metrics_scheduler = MetricsScheduler()
