"""Unit tests for metrics scheduler."""

import asyncio
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from backend.app.core import scheduler_tick
from backend.app.core.metrics_scheduler import SCHEDULER_NAME, MetricsScheduler
from backend.app.core.scheduler_tick import STATUS_SUCCESS, run_locked_tick
from backend.app.models.metrics.metric import AggregationPeriod

PURGE = "backend.app.services.analysis_snapshot_service.purge_expired_history"


@pytest.fixture
def mock_db_session():
    """Create a mock database session for testing."""
    return MagicMock()


@pytest.fixture
def scheduler():
    """Create a metrics scheduler for testing."""
    scheduler = MetricsScheduler(interval_minutes=5)
    return scheduler


def test_the_scheduler_the_api_starts_runs_every_15_minutes():
    from backend.app.core.metrics_scheduler import metrics_scheduler

    assert metrics_scheduler.interval_minutes == 15
    assert MetricsScheduler().interval_minutes == 15


@pytest.mark.asyncio
async def test_scheduler_start_stop(scheduler):
    """Test that scheduler can be started and stopped correctly."""
    # Test starting the scheduler
    with patch("asyncio.create_task") as mock_create_task:
        mock_task = AsyncMock()
        mock_create_task.return_value = mock_task

        await scheduler.start()

        # Verify the scheduler is running
        assert scheduler.is_running is True
        assert scheduler.task is mock_task
        mock_create_task.assert_called_once()

    # Test starting the scheduler when it's already running
    with patch("asyncio.create_task") as mock_create_task:
        await scheduler.start()

        # Verify create_task was not called again
        mock_create_task.assert_not_called()

    # Test stopping the scheduler
    # Use patch to prevent awaiting AsyncMock directly
    with patch.object(scheduler, "task") as mock_task:
        await scheduler.stop()

        # Verify the scheduler is stopped
        assert scheduler.is_running is False
        assert mock_task.cancel.called


@pytest.mark.skip(reason="This test is flaky and needs to be refactored")
@pytest.mark.asyncio
async def test_scheduler_run_scheduler(scheduler):
    """Test the scheduler loop runs and handles exceptions."""
    # Mock the aggregate_metrics method to track calls
    with patch.object(scheduler, "aggregate_metrics") as mock_aggregate:
        # Create a future that's already cancelled
        cancelled_future = asyncio.Future()
        cancelled_future.set_exception(asyncio.CancelledError())

        # Mock sleep to return None first, then return the cancelled future
        with patch("asyncio.sleep", side_effect=[None, cancelled_future]):
            mock_aggregate.return_value = None

            # Set scheduler to running
            scheduler.is_running = True

            # This should run until CancelledError is raised by the mocked sleep
            with pytest.raises(asyncio.CancelledError):
                await scheduler._run_scheduler()

            # Verify aggregate_metrics was called once
            mock_aggregate.assert_called_once()

    # Test error handling in the scheduler loop
    with patch.object(scheduler, "aggregate_metrics") as mock_aggregate:
        # Create a future that's already cancelled
        cancelled_future = asyncio.Future()
        cancelled_future.set_exception(asyncio.CancelledError())

        # Mock sleep to return None first, then return the cancelled future
        with patch("asyncio.sleep", side_effect=[None, cancelled_future]):
            # Make aggregate_metrics raise an exception
            mock_aggregate.side_effect = Exception("Test exception")

            # Set scheduler to running
            scheduler.is_running = True

            # This should catch the exception from aggregate_metrics and continue
            with pytest.raises(asyncio.CancelledError):
                await scheduler._run_scheduler()

            # Verify aggregate_metrics was called once
            mock_aggregate.assert_called_once()


@pytest.mark.asyncio
async def test_scheduler_aggregate_metrics(scheduler, mock_db_session):
    """Test that scheduler aggregates metrics for different time periods."""
    # Create current time for testing
    current_time = datetime.now(timezone.utc)

    # Mock MetricsService to track calls to aggregate_metrics
    with patch("backend.app.core.metrics_scheduler.MetricsService") as mock_service:
        # Configure mock to return different counts for different aggregation periods
        mock_service.aggregate_metrics.side_effect = [10, 20, 30, 40, 50, 60]

        # Mock SessionLocal to return our mock session
        with patch(
            "backend.app.core.metrics_scheduler.SessionLocal",
            return_value=mock_db_session,
        ):
            # Mock datetime.now to return a consistent time
            with patch("backend.app.core.metrics_scheduler.datetime") as mock_datetime:
                mock_datetime.now.return_value = current_time
                # Pass through other datetime methods
                mock_datetime.side_effect = lambda *args, **kw: datetime(*args, **kw)

                # Run the aggregation
                await scheduler.aggregate_metrics()

                # Check that MetricsService.aggregate_metrics was called for each period
                assert mock_service.aggregate_metrics.call_count == 6

                # Verify the calls with expected parameters
                expected_calls = [
                    # Minute aggregation for the last hour
                    call(
                        db=mock_db_session,
                        period=AggregationPeriod.MINUTE,
                        start_time=current_time - timedelta(hours=1),
                        end_time=current_time,
                    ),
                    # Hourly aggregation for the last day
                    call(
                        db=mock_db_session,
                        period=AggregationPeriod.HOUR,
                        start_time=current_time - timedelta(days=1),
                        end_time=current_time,
                    ),
                    # Daily aggregation for the last month
                    call(
                        db=mock_db_session,
                        period=AggregationPeriod.DAY,
                        start_time=current_time - timedelta(days=30),
                        end_time=current_time,
                    ),
                    # Weekly aggregation for the last year
                    call(
                        db=mock_db_session,
                        period=AggregationPeriod.WEEK,
                        start_time=current_time - timedelta(days=365),
                        end_time=current_time,
                    ),
                    # Monthly aggregation for all time
                    call(
                        db=mock_db_session,
                        period=AggregationPeriod.MONTH,
                        start_time=None,
                        end_time=current_time,
                    ),
                    # Total aggregation (single record for all time)
                    call(
                        db=mock_db_session,
                        period=AggregationPeriod.TOTAL,
                        start_time=None,
                        end_time=current_time,
                    ),
                ]
                mock_service.aggregate_metrics.assert_has_calls(expected_calls)

                # Verify the session was closed
                assert mock_db_session.close.called


@pytest.mark.asyncio
async def test_scheduler_aggregate_metrics_exception(scheduler, mock_db_session):
    """Test that scheduler handles exceptions during metrics aggregation."""
    # Mock MetricsService to raise an exception
    with patch("backend.app.core.metrics_scheduler.MetricsService") as mock_service:
        mock_service.aggregate_metrics.side_effect = Exception(
            "Test aggregation exception"
        )

        # Mock SessionLocal to return our mock session
        with patch(
            "backend.app.core.metrics_scheduler.SessionLocal",
            return_value=mock_db_session,
        ):
            # Run the aggregation - should not raise an exception
            await scheduler.aggregate_metrics()

            # Verify the session was closed despite the exception
            assert mock_db_session.close.called


@pytest.mark.asyncio
async def test_scheduler_aggregate_metrics_partial_failure(scheduler, mock_db_session):
    """Test that scheduler continues processing when some aggregation periods fail."""
    # Mock MetricsService to succeed for some periods and fail for others
    with patch("backend.app.core.metrics_scheduler.MetricsService") as mock_service:
        mock_service.aggregate_metrics.side_effect = [
            10,  # MINUTE succeeds
            Exception("Hour aggregation failed"),  # HOUR fails
            30,  # DAY succeeds
            Exception("Week aggregation failed"),  # WEEK fails
            50,  # MONTH succeeds
            60,  # TOTAL succeeds
        ]

        # Mock SessionLocal to return our mock session
        with patch(
            "backend.app.core.metrics_scheduler.SessionLocal",
            return_value=mock_db_session,
        ):
            # Run the aggregation
            await scheduler.aggregate_metrics()

            # Verify all periods were attempted despite some failures
            assert mock_service.aggregate_metrics.call_count == 6

            # Verify the session was closed
            assert mock_db_session.close.called


# ---------------------------------------------------------------------------
# #1690: the tick's synchronous body runs off the event loop
# ---------------------------------------------------------------------------


def _no_running_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return True
    return False


@pytest.mark.regression
@pytest.mark.asyncio
async def test_the_aggregation_runs_off_the_event_loop_and_all_six_periods_run(
    scheduler,
):
    """#1690: on the loop, the six GROUP BYs over raw_metrics stopped the API
    answering anything for seconds every 15 minutes. The purge, the session
    and every aggregation run in a worker thread, with no event loop there."""
    loop_thread = threading.get_ident()
    seen = []

    def record(what):
        seen.append((what, threading.get_ident(), _no_running_loop()))

    def aggregate(**kwargs):
        record(kwargs["period"])
        return 1

    session = MagicMock()
    session.close.side_effect = lambda: record("close")

    def open_session():
        record("open")
        return session

    with (
        patch("backend.app.core.metrics_scheduler.MetricsService") as service,
        patch(
            "backend.app.core.metrics_scheduler.SessionLocal", side_effect=open_session
        ),
        patch(PURGE, side_effect=lambda: record("purge") or {}),
    ):
        service.aggregate_metrics.side_effect = aggregate
        result = await scheduler.aggregate_metrics()

    assert [what for what, _, _ in seen] == [
        "purge",
        "open",
        AggregationPeriod.MINUTE,
        AggregationPeriod.HOUR,
        AggregationPeriod.DAY,
        AggregationPeriod.WEEK,
        AggregationPeriod.MONTH,
        AggregationPeriod.TOTAL,
        "close",
    ], "the aggregation did not run (a coroutine function given to to_thread?)"
    on_the_loop = [
        what for what, ident, no_loop in seen if ident == loop_thread or not no_loop
    ]
    assert on_the_loop == [], f"ran on the event loop's thread: {on_the_loop}"
    assert result == {"items_processed": 6, "items_failed": 0}


@pytest.mark.regression
@pytest.mark.asyncio
async def test_the_offloaded_tick_is_still_recorded_through_run_locked_tick(
    scheduler,
):
    """The scheduler loop still runs the tick through run_locked_tick: under
    the metrics lock, and recorded with what the worker thread returned."""
    locks = []
    recorded = []

    @asynccontextmanager
    async def lock(name, engine=None):
        locks.append(name)
        yield True

    def persist(name, started_at, completed_at, status, result, error):
        recorded.append(
            (name, status, result.items_processed, result.items_failed, error)
        )

    async def stop_after_one_tick(_seconds):
        scheduler.is_running = False

    with (
        patch.object(scheduler_tick, "async_scheduler_lock", lock),
        patch.object(scheduler_tick, "_persist_run", side_effect=persist),
        patch("backend.app.core.metrics_scheduler.MetricsService") as service,
        patch(
            "backend.app.core.metrics_scheduler.SessionLocal", return_value=MagicMock()
        ),
        patch(PURGE, return_value={}),
        patch.object(asyncio, "sleep", stop_after_one_tick),
    ):
        service.aggregate_metrics.side_effect = [1, 2, 3, 4, 5, 6]
        scheduler.is_running = True
        await scheduler._run_scheduler()

    assert locks == [SCHEDULER_NAME]
    assert recorded == [(SCHEDULER_NAME, STATUS_SUCCESS, 21, 0, None)]


@pytest.mark.regression
@pytest.mark.asyncio
async def test_a_cancelled_tick_keeps_the_lock_until_the_worker_thread_returns(
    scheduler,
):
    """Shutdown cancels the tick while the thread aggregates. The advisory
    lock is released when run_locked_tick unwinds, so the tick must not
    unwind until the thread returns: otherwise, in a rolling deploy, a new
    replica's startup tick aggregates beside the old replica's thread. A
    second cancel does not end the wait either. Deterministic: the worker
    blocks on an Event the test sets."""
    started = threading.Event()
    finish = threading.Event()
    events = []

    def blocked_worker():
        started.set()
        finish.wait(60)  # a safety bound so a failing test cannot hang a thread
        events.append("worker returned")
        return {"items_processed": 0, "items_failed": 0}

    @asynccontextmanager
    async def lock(name, engine=None):
        events.append("acquired")
        try:
            yield True
        finally:
            events.append("released")

    scheduler._aggregate_sync = blocked_worker
    with (
        patch.object(scheduler_tick, "async_scheduler_lock", lock),
        patch.object(scheduler_tick, "_persist_run") as persist,
    ):
        task = asyncio.create_task(
            run_locked_tick(SCHEDULER_NAME, scheduler.aggregate_metrics)
        )
        try:
            assert await asyncio.to_thread(started.wait, 30), "worker never started"
            for _ in range(2):
                task.cancel()
                # Deliver the cancellation. The fake lock releases without
                # suspending, so code that unwinds early has released by now.
                for _ in range(10):
                    await asyncio.sleep(0)
                assert events == ["acquired"], "lock released before the thread ended"
                assert not task.done()
        finally:
            finish.set()

        with pytest.raises(asyncio.CancelledError):
            await task

    assert events == ["acquired", "worker returned", "released"]
    persist.assert_not_called()  # a cancelled tick records no bogus run


@pytest.mark.regression
def test_loop_teardown_keeps_the_lock_until_the_worker_thread_returns(scheduler):
    """asyncio.run's teardown cancels EVERY task. If the worker were a task
    (ensure_future(to_thread(...))) it would be cancelled with the tick and
    the lock released while the thread still aggregates (measured: 1.9 s
    early). The worker is a plain Future, so teardown leaves it alone.
    Deterministic: the worker blocks on an Event the test sets."""
    started = threading.Event()
    finish = threading.Event()
    events = []

    def blocked_worker():
        started.set()
        finish.wait(60)  # a safety bound so a failing test cannot hang a thread
        events.append("worker returned")
        return {"items_processed": 0, "items_failed": 0}

    @asynccontextmanager
    async def lock(name, engine=None):
        events.append("acquired")
        try:
            yield True
        finally:
            events.append("released")

    async def spin():
        for _ in range(20):
            await asyncio.sleep(0)

    async def wait_started():
        assert await asyncio.to_thread(started.wait, 30), "worker never started"

    scheduler._aggregate_sync = blocked_worker
    loop = asyncio.new_event_loop()
    try:
        with (
            patch.object(scheduler_tick, "async_scheduler_lock", lock),
            patch.object(scheduler_tick, "_persist_run"),
        ):
            tick = loop.create_task(
                run_locked_tick(SCHEDULER_NAME, scheduler.aggregate_metrics)
            )
            loop.run_until_complete(wait_started())
            # What asyncio.run does on the way out: cancel every task.
            for task in asyncio.all_tasks(loop):
                task.cancel()
            loop.run_until_complete(spin())
            try:
                assert events == ["acquired"], "lock released before the thread ended"
                assert not tick.done()
            finally:
                finish.set()
            loop.run_until_complete(asyncio.gather(tick, return_exceptions=True))
        assert events == ["acquired", "worker returned", "released"]
    finally:
        finish.set()
        loop.run_until_complete(loop.shutdown_default_executor())
        loop.close()
