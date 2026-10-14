"""``min_stage_duration`` holds back only the stages the scheduler starts.

A schedule's ``min_stage_duration`` (hours) is measured from the start of the
stage in progress. The scheduler's tick (``RolloutScheduler._process_schedule``)
does not complete that stage, or start the next one, before it has passed,
even when the stage's own 24 hours have. A manual advance
(``RolloutService.manually_advance_stage``) starts the next stage at once,
whatever ``min_stage_duration`` says.

The session answers each query with the rows a test gives it, as in
``test_rollout_stage_order.py``; nothing reaches a database.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.app.core.rollout_scheduler import RolloutScheduler
from backend.app.models.feature_flag import FeatureFlag
from backend.app.models.rollout_schedule import (
    RolloutSchedule,
    RolloutScheduleStatus,
    RolloutStage,
    RolloutStageStatus,
    TriggerType,
)
from backend.app.services.rollout_service import RolloutService
from backend.tests.unit.core.test_rollout_stage_order import (
    NOW,
    PAST,
    _schedule,
    _Session,
    _stage,
)

pytestmark = [pytest.mark.unit, pytest.mark.regression]

HOURS = 48


def _tick(schedule, active, pending):
    scheduler = RolloutScheduler(interval_minutes=1)
    scheduler._activate_stage = AsyncMock(return_value=True)
    scheduler._lock_active_schedule = MagicMock()
    session = _Session({RolloutStage: [[active], pending]})
    result = asyncio.run(scheduler._process_schedule(session, schedule, NOW))
    started = [c.args[2] for c in scheduler._activate_stage.call_args_list]
    return result, started


@pytest.mark.parametrize("hours_in", [25, 47])
def test_the_scheduler_waits_for_min_stage_duration(hours_in):
    schedule = _schedule()
    schedule.min_stage_duration = HOURS
    first = _stage(
        1,
        TriggerType.TIME_BASED,
        RolloutStageStatus.IN_PROGRESS,
        updated_at=NOW - timedelta(hours=hours_in),
    )
    second = _stage(
        2, TriggerType.TIME_BASED, RolloutStageStatus.PENDING, start_date=PAST
    )
    (changed, transitions, _), started = _tick(schedule, first, [second])
    assert (changed, transitions, started) == (False, 0, [])
    assert first.status is RolloutStageStatus.IN_PROGRESS
    assert second.status is RolloutStageStatus.PENDING


def test_the_scheduler_starts_the_next_stage_once_min_stage_duration_has_passed():
    schedule = _schedule()
    schedule.min_stage_duration = HOURS
    first = _stage(
        1,
        TriggerType.TIME_BASED,
        RolloutStageStatus.IN_PROGRESS,
        updated_at=NOW - timedelta(hours=HOURS + 1),
    )
    second = _stage(
        2, TriggerType.TIME_BASED, RolloutStageStatus.PENDING, start_date=PAST
    )
    (changed, transitions, _), started = _tick(schedule, first, [second])
    assert (changed, transitions, started) == (True, 1, [second])
    assert first.status is RolloutStageStatus.COMPLETED


@pytest.mark.parametrize("trigger", [TriggerType.TIME_BASED, TriggerType.MANUAL])
def test_a_manual_advance_does_not_wait_for_min_stage_duration(trigger):
    schedule = _schedule()
    schedule.min_stage_duration = HOURS
    canary = _stage(
        1,
        TriggerType.MANUAL,
        RolloutStageStatus.IN_PROGRESS,
        schedule_id=schedule.id,
        updated_at=NOW - timedelta(minutes=5),
    )
    nxt = _stage(2, trigger, RolloutStageStatus.PENDING, schedule_id=schedule.id)
    flag = SimpleNamespace(rollout_percentage=canary.target_percentage, updated_at=None)
    session = _Session(
        {
            RolloutStage: [[canary], [nxt]],
            RolloutSchedule: [[schedule], [schedule]],
            FeatureFlag: [[flag]],
        }
    )
    RolloutService.manually_advance_stage(session, canary.id)
    assert canary.status is RolloutStageStatus.COMPLETED
    assert nxt.status is RolloutStageStatus.IN_PROGRESS
    assert flag.rollout_percentage == nxt.target_percentage
    assert schedule.status is RolloutScheduleStatus.ACTIVE
