"""
``EXPERIMENT_SCHEDULER_INTERVAL_MINUTES`` sets how often the experiment
scheduler runs (#484).

The scheduler used to run every 15 minutes with no setting to change it, while
the safety, rollout and bandit loops each had one. These tests pin the three
parts of the contract: the lifespan hands the configured value to the running
scheduler, the default stays 15, and a value below 1 is refused.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.app.core.config import Settings, settings
from backend.app.main import app, lifespan

SCHEDULERS = (
    "experiment_scheduler",
    "rollout_scheduler",
    "metrics_scheduler",
    "safety_scheduler",
    "bandit_scheduler_runner",
)


@pytest.mark.regression
@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [1, 7])
async def test_lifespan_passes_the_setting_to_the_experiment_scheduler(
    monkeypatch, configured
):
    import backend.app.main as main

    # Nothing really starts: every scheduler's start/stop is replaced.
    for name in SCHEDULERS:
        scheduler = getattr(main, name)

        async def _noop():
            return None

        monkeypatch.setattr(scheduler, "start", _noop)
        monkeypatch.setattr(scheduler, "stop", _noop)

    experiment_scheduler = main.experiment_scheduler
    seen_at_start: list[int] = []

    async def _record_start():
        seen_at_start.append(experiment_scheduler.interval_minutes)

    monkeypatch.setattr(experiment_scheduler, "start", _record_start)
    # Restore the singleton's interval afterwards; the lifespan sets it.
    monkeypatch.setattr(experiment_scheduler, "interval_minutes", 15)
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    monkeypatch.setattr(settings, "EXPERIMENT_SCHEDULER_INTERVAL_MINUTES", configured)

    async with lifespan(app):
        pass

    assert seen_at_start == [configured]


@pytest.mark.regression
def test_the_default_is_15_minutes():
    assert Settings.model_fields["EXPERIMENT_SCHEDULER_INTERVAL_MINUTES"].default == 15
    assert Settings(_env_file=None).EXPERIMENT_SCHEDULER_INTERVAL_MINUTES == 15


@pytest.mark.asyncio
async def test_the_loop_ticks_then_sleeps_the_interval_in_seconds(monkeypatch):
    """The setting is in minutes; the loop sleeps ``interval_minutes * 60`` seconds.

    The loop runs for exactly three ticks, with a sleep that records its
    argument and stops the loop at the third. At the default of 15 minutes that
    is ``[900, 900, 900]`` (minutes passed as seconds would give ``[15, 15, 15]``,
    ``* 120`` would give ``[1800, ...]``) and each sleep follows a tick, so three
    sleeps are three ticks. Both fakes also stop the loop on their own after a
    bounded number of calls, so a deleted sleep or tick fails the assertions
    below instead of hanging the run.
    """
    import asyncio

    import backend.app.core.scheduler as scheduler_module
    from backend.app.core.scheduler import ExperimentScheduler

    scheduler = ExperimentScheduler()
    scheduler.is_running = True
    sleeps: list[float] = []
    processed: list[int] = []
    locked: list[str] = []

    async def process_scheduled_experiments():
        processed.append(1)
        return {"items_processed": 0, "items_failed": 0}

    async def run_locked_tick(name, tick, **_kwargs):
        locked.append(name)
        # A loop that never sleeps would otherwise spin here for ever.
        if len(locked) > 10:
            scheduler.is_running = False
        return await tick()

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 3 or len(sleeps) > 10:
            scheduler.is_running = False

    monkeypatch.setattr(
        scheduler, "process_scheduled_experiments", process_scheduled_experiments
    )
    monkeypatch.setattr(scheduler_module, "run_locked_tick", run_locked_tick)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    await scheduler._run_scheduler()

    assert scheduler.interval_minutes == 15
    assert sleeps == [900, 900, 900]
    assert locked == [scheduler_module.SCHEDULER_NAME] * 3
    assert len(processed) == 3


@pytest.mark.regression
def test_the_schedule_form_quotes_the_default():
    """The experiment page's schedule form (#1454) tells the user how late a
    scheduled draft can start: the default interval, in minutes. Reads the
    component's source, not docs/."""
    import re
    from pathlib import Path

    form = (
        Path(__file__).resolve().parents[4]
        / "frontend/src/components/experiments/ExperimentScheduleSection.tsx"
    )
    quoted = re.findall(r"every (\d+) minutes by default", form.read_text("utf-8"))
    assert quoted, f"{form.name} no longer says how often the scheduler checks"
    assert {int(n) for n in quoted} == {
        Settings.model_fields["EXPERIMENT_SCHEDULER_INTERVAL_MINUTES"].default
    }


@pytest.mark.regression
def test_the_environment_variable_sets_it(monkeypatch):
    monkeypatch.setenv("EXPERIMENT_SCHEDULER_INTERVAL_MINUTES", "2")
    assert Settings(_env_file=None).EXPERIMENT_SCHEDULER_INTERVAL_MINUTES == 2


@pytest.mark.regression
@pytest.mark.parametrize("value", [0, -1, -15])
def test_a_value_below_one_is_refused(value):
    with pytest.raises(ValidationError, match="EXPERIMENT_SCHEDULER_INTERVAL_MINUTES"):
        Settings(_env_file=None, EXPERIMENT_SCHEDULER_INTERVAL_MINUTES=value)


@pytest.mark.regression
@pytest.mark.parametrize("value", ["0", "-5"])
def test_a_value_below_one_from_the_environment_is_refused(monkeypatch, value):
    monkeypatch.setenv("EXPERIMENT_SCHEDULER_INTERVAL_MINUTES", value)
    with pytest.raises(ValidationError, match="EXPERIMENT_SCHEDULER_INTERVAL_MINUTES"):
        Settings(_env_file=None)
