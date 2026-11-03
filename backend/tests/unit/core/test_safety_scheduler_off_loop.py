"""The safety tick runs off the event loop (#1690).

``SafetyScheduler.check_feature_flags_safety`` is synchronous SQLAlchemy behind
``async def``s that never suspend. Run on the event loop, it stopped the API
answering anything for its whole run, every 5 minutes. The scheduler now runs
it through ``run_in_worker``: in one worker thread, on a private event loop.

Pinned here, each without a timing: the check runs once, on another thread and
another loop, and what it returns reaches the run record; a cancelled tick, and
a torn-down event loop, keep the advisory lock until the thread has returned.
The check is a stub; ``backend/tests/integration/services/
test_safety_tick_off_loop.py`` runs the real one.
"""

import asyncio
import threading
from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest

from backend.app.core import scheduler_tick
from backend.app.core.safety_scheduler import SCHEDULER_NAME, SafetyScheduler
from backend.app.core.scheduler_tick import STATUS_SUCCESS, run_locked_tick

pytestmark = pytest.mark.regression


@pytest.fixture
def safety():
    return SafetyScheduler(interval_minutes=5)


def _recording_lock(events):
    @asynccontextmanager
    async def lock(name, engine=None):
        events.append("acquired")
        try:
            yield True
        finally:
            events.append("released")

    return lock


def _blocked_check(started, finish, events):
    """A check that blocks its thread until the test sets ``finish``."""

    async def check():
        started.set()
        finish.wait(60)  # a safety bound so a failing test cannot hang a thread
        events.append("worker returned")
        return {"items_processed": 0, "items_failed": 0}

    return check


@pytest.mark.asyncio
async def test_the_safety_check_runs_once_off_the_loop_and_its_result_is_recorded(
    safety,
):
    """One tick of the real scheduler loop, through the real run_locked_tick.

    ``asyncio.to_thread(self.check_feature_flags_safety)`` would only create
    the coroutine in the thread and never run it: no call here, and a run
    recorded as a success with nothing checked."""
    api_loop = asyncio.get_running_loop()
    api_thread = threading.get_ident()
    calls = []
    locks = []
    recorded = []

    async def check():
        calls.append((threading.get_ident(), asyncio.get_running_loop()))
        return {"items_processed": 3, "items_failed": 0, "metadata": {"rollbacks": 1}}

    @asynccontextmanager
    async def lock(name, engine=None):
        locks.append(name)
        yield True

    def persist(name, started_at, completed_at, status, result, error):
        recorded.append(
            (
                name,
                status,
                result.items_processed,
                result.items_failed,
                result.metadata,
                error,
            )
        )

    async def stop_after_one_tick(_seconds):
        safety.is_running = False

    safety.check_feature_flags_safety = check
    with (
        patch.object(scheduler_tick, "async_scheduler_lock", lock),
        patch.object(scheduler_tick, "_persist_run", side_effect=persist),
        patch.object(asyncio, "sleep", stop_after_one_tick),
    ):
        safety.is_running = True
        await safety._run_scheduler()

    assert len(calls) == 1, f"the check ran {len(calls)} times, not once"
    thread, loop = calls[0]
    assert thread != api_thread, "the check ran on the event loop's thread"
    assert loop is not api_loop, "the check ran on the API's event loop"
    assert locks == [SCHEDULER_NAME]
    assert recorded == [(SCHEDULER_NAME, STATUS_SUCCESS, 3, 0, {"rollbacks": 1}, None)]


@pytest.mark.asyncio
async def test_a_cancelled_safety_tick_keeps_the_lock_until_the_worker_thread_returns(
    safety,
):
    """Shutdown cancels the tick while the thread checks flags. The advisory
    lock is released when run_locked_tick unwinds, so the tick must not unwind
    before the thread returns, or another replica's tick checks (and rolls
    back) beside it. A second cancel does not end the wait either."""
    started = threading.Event()
    finish = threading.Event()
    events = []

    safety.check_feature_flags_safety = _blocked_check(started, finish, events)
    with (
        patch.object(scheduler_tick, "async_scheduler_lock", _recording_lock(events)),
        patch.object(scheduler_tick, "_persist_run") as persist,
    ):
        task = asyncio.create_task(
            run_locked_tick(SCHEDULER_NAME, safety._check_off_loop)
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


def test_loop_teardown_keeps_the_safety_lock_until_the_worker_thread_returns(safety):
    """asyncio.run's teardown cancels EVERY task. A worker that is a task
    (ensure_future(to_thread(...))) is cancelled with the tick, and the lock
    is released while the thread still checks flags. run_in_worker's worker
    is a plain Future, which teardown leaves alone."""
    started = threading.Event()
    finish = threading.Event()
    events = []

    async def spin():
        for _ in range(20):
            await asyncio.sleep(0)

    async def wait_started():
        assert await asyncio.to_thread(started.wait, 30), "worker never started"

    safety.check_feature_flags_safety = _blocked_check(started, finish, events)
    loop = asyncio.new_event_loop()
    try:
        with (
            patch.object(
                scheduler_tick, "async_scheduler_lock", _recording_lock(events)
            ),
            patch.object(scheduler_tick, "_persist_run"),
        ):
            tick = loop.create_task(
                run_locked_tick(SCHEDULER_NAME, safety._check_off_loop)
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
