"""The experiment tick's notifications go out after the commit, off the loop (#1690).

A notification is a synchronous HTTP call (webhook, Slack), bounded only by
its client's timeout; sent on the event loop, a slow receiver stopped the API
answering. Each is now sent in a worker thread, after the commit it announces
and after that commit's cache drop. The rest of the tick stays on the event
loop: the cache drop uses the process-wide Redis pool, which belongs to that
loop.

Pinned here with a mocked session: the order (drop, sends, drop, sends), the
loop each part runs on, and that one failing send stops neither the sends
after it nor the completions. ``backend/tests/integration/services/
test_scheduler_sends_after_commit.py`` checks the commit itself on the
database.
"""

import asyncio
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from backend.app.core import scheduler_tick
from backend.app.core.scheduler import ExperimentScheduler
from backend.app.models.experiment import ExperimentStatus

pytestmark = pytest.mark.regression

DROP = "backend.app.core.scheduler.experiment_cache.drop_cached_experiment_details"


def _experiment(experiment_id, status):
    """A mocked experiment that passes the check Start makes (#1454)."""
    now = datetime.now(timezone.utc)
    experiment = MagicMock()
    experiment.id = experiment_id
    experiment.name = f"name of {experiment_id}"
    experiment.start_date = now
    experiment.end_date = now
    experiment.resume_at = None
    experiment.status = status
    experiment.variants = [MagicMock(is_control=True), MagicMock(is_control=False)]
    experiment.metric_definitions = [MagicMock()]
    # Not a visual experiment. Only None reads as absent, so a mock's own
    # attribute would count as visual settings and fail the start check (#1545).
    experiment.visual_config = None
    return experiment


def _session(to_start, to_end):
    db = MagicMock()
    db.query.return_value.filter.return_value.all.side_effect = [to_start, to_end]
    return db


@pytest.mark.asyncio
async def test_each_cache_drop_runs_on_the_api_loop_before_its_sends_which_do_not():
    """One tick of the real scheduler loop. The tick stays on the API's loop
    (its cache drop is loop-bound): run whole in a worker on a private loop,
    the drops would not run on the API's loop."""
    api_loop = asyncio.get_running_loop()
    api_thread = threading.get_ident()
    events = []

    async def drop(ids):
        events.append(("drop", list(ids), asyncio.get_running_loop() is api_loop))

    def sent(kind):
        def record(**kwargs):
            on_a_worker = threading.get_ident() != api_thread
            events.append((kind, kwargs["experiment_id"], on_a_worker))
            return True

        return record

    notifications = MagicMock()
    notifications.notify_experiment_started.side_effect = sent("started")
    notifications.notify_experiment_ended.side_effect = sent("ended")

    @asynccontextmanager
    async def lock(name, engine=None):
        yield True

    scheduler = ExperimentScheduler()
    scheduler._notification_service = notifications

    async def stop_after_one_tick(_seconds):
        scheduler.is_running = False

    db = _session(
        [_experiment("to-start", ExperimentStatus.DRAFT)],
        [_experiment("to-end", ExperimentStatus.ACTIVE)],
    )
    with (
        patch("backend.app.core.scheduler.SessionLocal", return_value=db),
        patch(DROP, drop),
        patch.object(scheduler_tick, "async_scheduler_lock", lock),
        patch.object(scheduler_tick, "_persist_run"),
        patch.object(asyncio, "sleep", stop_after_one_tick),
    ):
        scheduler.is_running = True
        await scheduler._run_scheduler()

    # (what, experiment id(s), drop: on the API's loop / send: on a worker)
    assert events == [
        ("drop", ["to-start"], True),
        ("started", "to-start", True),
        ("drop", ["to-end"], True),
        ("ended", "to-end", True),
    ]


@pytest.mark.asyncio
async def test_a_failing_send_stops_neither_the_other_sends_nor_the_completions():
    """The first of two activation sends raises. The second is still sent,
    and the completion phase still completes, commits and notifies; the tick
    reports no failure. (A send whose error escaped to the tick's own
    handler would skip the completion phase.)"""
    to_start = [
        _experiment("start-a", ExperimentStatus.DRAFT),
        _experiment("start-b", ExperimentStatus.DRAFT),
    ]
    to_end = _experiment("end-c", ExperimentStatus.ACTIVE)
    notifications = MagicMock()
    notifications.notify_experiment_started.side_effect = [
        RuntimeError("webhook down"),
        True,
    ]
    notifications.notify_experiment_ended.return_value = True

    scheduler = ExperimentScheduler()
    scheduler._notification_service = notifications
    db = _session(to_start, [to_end])

    async def drop(ids):
        return None

    with (
        patch("backend.app.core.scheduler.SessionLocal", return_value=db),
        patch(DROP, drop),
    ):
        result = await scheduler.process_scheduled_experiments()

    started = [
        c.kwargs["experiment_id"]
        for c in notifications.notify_experiment_started.call_args_list
    ]
    assert started == ["start-a", "start-b"], "a failing send stopped the ones after"
    notifications.notify_experiment_ended.assert_called_once_with(
        experiment_id="end-c", experiment_name="name of end-c"
    )
    assert to_end.status == ExperimentStatus.COMPLETED
    assert db.commit.call_count == 2
    assert result == {
        "items_processed": 3,
        "items_failed": 0,
        "metadata": {"activated": 2, "completed": 1},
    }
