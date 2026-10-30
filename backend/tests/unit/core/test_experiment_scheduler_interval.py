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
