"""The rollout scheduler passes over a schedule whose end_date has passed (#1142).

``docs/feature-flags/rollouts.md`` says that once a schedule's ``end_date``
has passed, no stage of it starts or completes by itself and the schedule stays
``active``: ``RolloutScheduler.process_rollout_schedules`` selects only the
active schedules with no ``end_date`` or one still in the future. Each test
runs one real tick against the test database, with the schedule of
``test_rollout_scheduler_row_isolation._two_stage``: stage 1 (10%) is due to
complete and stage 2 (50%) may start at once.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.app.models.rollout_schedule import (
    RolloutSchedule,
    RolloutScheduleStatus,
    RolloutStageStatus,
)
from backend.tests.integration.services.test_rollout_scheduler_row_isolation import (
    _factory,
    _state,
    _tick,
    _two_stage,
)

pytestmark = [pytest.mark.integration]


def _with_end_date(db_session, end_date: datetime):
    flag_id, schedule_id, stage_ids = _two_stage(db_session, min_stage_duration=None)
    session = _factory(db_session)()
    try:
        session.get(RolloutSchedule, schedule_id).end_date = end_date
        session.commit()
    finally:
        session.close()
    return flag_id, schedule_id, stage_ids


def test_a_schedule_past_its_end_date_is_passed_over(db_session):
    ids = _with_end_date(db_session, datetime.now(timezone.utc) - timedelta(hours=1))

    _tick(db_session)

    assert _state(db_session, *ids) == (
        10,
        RolloutScheduleStatus.ACTIVE,
        [RolloutStageStatus.IN_PROGRESS, RolloutStageStatus.PENDING],
    )


def test_a_schedule_before_its_end_date_advances(db_session):
    ids = _with_end_date(db_session, datetime.now(timezone.utc) + timedelta(days=1))

    _tick(db_session)

    assert _state(db_session, *ids) == (
        50,
        RolloutScheduleStatus.ACTIVE,
        [RolloutStageStatus.COMPLETED, RolloutStageStatus.IN_PROGRESS],
    )
