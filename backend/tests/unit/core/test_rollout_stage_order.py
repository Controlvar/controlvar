"""Which stage of a rollout schedule starts next, and who starts it (#1142).

``docs/feature-flags/rollouts.md`` says a time-based stage starts once its
``start_date`` has passed and every stage before it is complete, that a
time-based stage after a manual stage therefore never starts by itself (the
scheduler never completes a manual stage), and that advancing a manual stage
that is in progress completes it and starts the next stage at once, whatever
that stage's trigger and ``start_date``.

The scheduler's tick (``RolloutScheduler._process_schedule``) and the manual
advance (``RolloutService.manually_advance_stage``) are run here against a
session that answers each query with the rows a test gives it; nothing reaches
a database.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List
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

pytestmark = pytest.mark.unit

NOW = datetime(2027, 1, 8, 12, 0, tzinfo=timezone.utc)


class _Query:
    """A query that answers ``first``, ``one_or_none`` and ``all`` from a list."""

    def __init__(self, rows: List[Any]):
        self._rows = rows

    def filter(self, *args: Any, **kwargs: Any) -> "_Query":
        return self

    order_by = with_for_update = populate_existing = filter

    def first(self) -> Any:
        return self._rows[0] if self._rows else None

    one_or_none = first

    def all(self) -> List[Any]:
        return list(self._rows)


class _Session:
    """Answers each ``query(Model)`` with the next row list given for that model."""

    def __init__(self, answers: Dict[Any, List[List[Any]]]):
        self._answers = {model: list(lists) for model, lists in answers.items()}
        self.added: List[Any] = []
        self.commit = MagicMock()
        self.refresh = MagicMock()

    def query(self, model: Any) -> _Query:
        return _Query(self._answers[model].pop(0))

    def add(self, row: Any) -> None:
        self.added.append(row)


def _schedule() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        feature_flag_id=uuid.uuid4(),
        status=RolloutScheduleStatus.ACTIVE,
        min_stage_duration=None,
        updated_at=NOW - timedelta(days=30),
    )


def _stage(order: int, trigger: TriggerType, status: RolloutStageStatus, **kw: Any):
    return SimpleNamespace(
        id=uuid.uuid4(),
        rollout_schedule_id=kw.get("schedule_id"),
        name=f"stage {order}",
        stage_order=order,
        target_percentage=order * 25,
        trigger_type=trigger,
        status=status,
        start_date=kw.get("start_date"),
        trigger_configuration=kw.get("trigger_configuration"),
        updated_at=kw.get("updated_at", NOW - timedelta(days=30)),
        completed_date=None,
    )


def _tick(active, pending):
    """One scheduler pass over one schedule; returns (result, started stages)."""
    scheduler = RolloutScheduler(interval_minutes=1)
    scheduler._activate_stage = AsyncMock(return_value=True)
    scheduler._lock_active_schedule = MagicMock()
    session = _Session({RolloutStage: [[active] if active else [], pending]})
    result = asyncio.run(scheduler._process_schedule(session, _schedule(), NOW))
    started = [c.args[2] for c in scheduler._activate_stage.call_args_list]
    return result, started


PAST = NOW - timedelta(days=1)


def test_a_time_based_stage_after_a_manual_stage_in_progress_never_starts():
    manual = _stage(1, TriggerType.MANUAL, RolloutStageStatus.IN_PROGRESS)
    timed = _stage(
        2, TriggerType.TIME_BASED, RolloutStageStatus.PENDING, start_date=PAST
    )
    (changed, transitions, _), started = _tick(manual, [timed])
    assert (changed, transitions, started) == (False, 0, [])
    assert manual.status is RolloutStageStatus.IN_PROGRESS
    assert timed.status is RolloutStageStatus.PENDING


def test_a_time_based_stage_after_a_pending_manual_stage_never_starts():
    manual = _stage(1, TriggerType.MANUAL, RolloutStageStatus.PENDING)
    timed = _stage(
        2, TriggerType.TIME_BASED, RolloutStageStatus.PENDING, start_date=PAST
    )
    (changed, transitions, _), started = _tick(None, [manual, timed])
    assert (changed, transitions, started) == (False, 0, [])


def test_a_time_based_stage_starts_once_the_stage_before_it_is_complete():
    first = _stage(
        1,
        TriggerType.TIME_BASED,
        RolloutStageStatus.IN_PROGRESS,
        updated_at=NOW - timedelta(hours=25),
    )
    second = _stage(
        2, TriggerType.TIME_BASED, RolloutStageStatus.PENDING, start_date=PAST
    )
    (changed, transitions, _), started = _tick(first, [second])
    assert (changed, transitions, started) == (True, 1, [second])
    assert first.status is RolloutStageStatus.COMPLETED


def test_a_time_based_stage_is_not_completed_before_24_hours():
    first = _stage(
        1,
        TriggerType.TIME_BASED,
        RolloutStageStatus.IN_PROGRESS,
        updated_at=NOW - timedelta(hours=23),
    )
    second = _stage(
        2, TriggerType.TIME_BASED, RolloutStageStatus.PENDING, start_date=PAST
    )
    (changed, transitions, _), started = _tick(first, [second])
    assert (changed, transitions, started) == (False, 0, [])
    assert first.status is RolloutStageStatus.IN_PROGRESS


def test_a_time_based_stage_with_a_future_start_date_waits_after_the_one_before():
    first = _stage(
        1,
        TriggerType.TIME_BASED,
        RolloutStageStatus.IN_PROGRESS,
        updated_at=NOW - timedelta(hours=25),
    )
    second = _stage(
        2,
        TriggerType.TIME_BASED,
        RolloutStageStatus.PENDING,
        start_date=NOW + timedelta(days=1),
    )
    (changed, transitions, _), started = _tick(first, [second])
    assert (changed, transitions, started) == (True, 0, [])
    assert first.status is RolloutStageStatus.COMPLETED
    assert second.status is RolloutStageStatus.PENDING


@pytest.mark.parametrize(
    "trigger, start_date",
    [
        (TriggerType.TIME_BASED, NOW + timedelta(days=7)),
        (TriggerType.TIME_BASED, None),
        (TriggerType.MANUAL, None),
    ],
)
def test_advancing_a_manual_stage_in_progress_starts_the_next_stage_at_once(
    trigger, start_date
):
    schedule = _schedule()
    canary = _stage(
        1, TriggerType.MANUAL, RolloutStageStatus.IN_PROGRESS, schedule_id=schedule.id
    )
    nxt = _stage(
        2,
        trigger,
        RolloutStageStatus.PENDING,
        start_date=start_date,
        schedule_id=schedule.id,
    )
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
    session.commit.assert_called_once()
