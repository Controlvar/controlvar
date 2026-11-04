"""
Regression tests: rollout stage timestamps come back from PostgreSQL as naive
datetimes (``timestamp without time zone``) while the scheduler compares them
against ``datetime.now(timezone.utc)``.  Before the ``_as_utc`` normalisation
every active schedule failed with "can't compare offset-naive and
offset-aware datetimes" and no stage ever advanced.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.core.rollout_scheduler import RolloutScheduler, _as_utc
from backend.app.models.rollout_schedule import (
    RolloutStage,
    RolloutStageStatus,
    TriggerType,
)


@pytest.fixture
def scheduler():
    return RolloutScheduler()


def _naive_utc(delta: timedelta) -> datetime:
    return (datetime.now(timezone.utc) + delta).replace(tzinfo=None)


class TestAsUtc:
    def test_naive_is_assumed_utc(self):
        naive = datetime(2026, 9, 11, 12, 0, 0)
        assert _as_utc(naive) == datetime(2026, 9, 11, 12, 0, 0, tzinfo=timezone.utc)

    def test_aware_is_converted_to_utc(self):
        plus_two = timezone(timedelta(hours=2))
        aware = datetime(2026, 9, 11, 14, 0, 0, tzinfo=plus_two)
        assert _as_utc(aware) == datetime(2026, 9, 11, 12, 0, 0, tzinfo=timezone.utc)


class TestNaiveStageTimestamps:
    def test_past_naive_start_date_is_eligible_for_activation(self, scheduler):
        stage = MagicMock(spec=RolloutStage)
        stage.status = RolloutStageStatus.PENDING
        stage.trigger_type = TriggerType.TIME_BASED
        stage.start_date = _naive_utc(timedelta(days=-2))

        assert (
            scheduler._is_stage_eligible_for_activation(
                stage, datetime.now(timezone.utc)
            )
            is True
        )

    def test_future_naive_start_date_is_not_eligible(self, scheduler):
        stage = MagicMock(spec=RolloutStage)
        stage.status = RolloutStageStatus.PENDING
        stage.trigger_type = TriggerType.TIME_BASED
        stage.start_date = _naive_utc(timedelta(days=5))

        assert (
            scheduler._is_stage_eligible_for_activation(
                stage, datetime.now(timezone.utc)
            )
            is False
        )

    def test_naive_started_at_completion_check_does_not_raise(self, scheduler):
        stage = MagicMock(spec=RolloutStage)
        stage.status = RolloutStageStatus.IN_PROGRESS
        stage.trigger_type = TriggerType.TIME_BASED
        stage.trigger_configuration = {"duration": 24}
        # Edited a minute ago; started 30 hours ago (#1528).
        stage.updated_at = _naive_utc(timedelta(minutes=-1))
        stage.started_at = _naive_utc(timedelta(hours=-30))

        assert (
            scheduler._is_stage_eligible_for_completion(
                stage, datetime.now(timezone.utc)
            )
            is True
        )

        stage.started_at = _naive_utc(timedelta(hours=-1))
        stage.updated_at = _naive_utc(timedelta(hours=-30))
        assert (
            scheduler._is_stage_eligible_for_completion(
                stage, datetime.now(timezone.utc)
            )
            is False
        )

    def test_naive_updated_at_completion_check_does_not_raise(self, scheduler):
        """A stage with no ``started_at`` (one an older release started) is
        measured from ``updated_at``, as before #1528."""
        stage = MagicMock(spec=RolloutStage)
        stage.status = RolloutStageStatus.IN_PROGRESS
        stage.trigger_type = TriggerType.TIME_BASED
        stage.trigger_configuration = {"duration": 24}
        stage.started_at = None
        stage.updated_at = _naive_utc(timedelta(hours=-30))

        assert (
            scheduler._is_stage_eligible_for_completion(
                stage, datetime.now(timezone.utc)
            )
            is True
        )

        stage.updated_at = _naive_utc(timedelta(hours=-1))
        assert (
            scheduler._is_stage_eligible_for_completion(
                stage, datetime.now(timezone.utc)
            )
            is False
        )


class TestNextStageRespectsItsOwnStartDate:
    @pytest.mark.asyncio
    @patch("backend.app.core.rollout_scheduler.SessionLocal")
    async def test_completed_stage_does_not_activate_future_stage(
        self, mock_session_class
    ):
        """
        Finishing stage 1 must not jump straight to stage 2 when stage 2's
        TIME_BASED start_date is still in the future (the ShopLab search
        rollout is 10% now, 50% in five days, 100% in twelve).
        """
        from backend.app.models.rollout_schedule import (
            RolloutSchedule,
            RolloutScheduleStatus,
        )

        mock_session = MagicMock()
        mock_session_class.return_value = mock_session

        schedule = MagicMock(spec=RolloutSchedule)
        schedule.id = "schedule-1"
        schedule.feature_flag_id = "flag-1"
        schedule.status = RolloutScheduleStatus.ACTIVE
        schedule.min_stage_duration = None
        schedule.end_date = None

        active_stage = MagicMock(spec=RolloutStage)
        active_stage.id = "stage-1"
        active_stage.name = "Canary 10%"
        active_stage.status = RolloutStageStatus.IN_PROGRESS
        active_stage.stage_order = 1
        active_stage.trigger_type = TriggerType.TIME_BASED
        active_stage.trigger_configuration = {"duration": 24}
        active_stage.updated_at = _naive_utc(timedelta(hours=-30))  # naive, from the DB
        active_stage.started_at = _naive_utc(timedelta(hours=-30))

        future_stage = MagicMock(spec=RolloutStage)
        future_stage.id = "stage-2"
        future_stage.name = "Expand to 50%"
        future_stage.status = RolloutStageStatus.PENDING
        future_stage.stage_order = 2
        future_stage.trigger_type = TriggerType.TIME_BASED
        future_stage.start_date = _naive_utc(timedelta(days=5))

        schedules_query = MagicMock()
        schedules_query.filter.return_value = schedules_query
        schedules_query.order_by.return_value = schedules_query
        schedules_query.all.return_value = [schedule]
        # The tick locks the flag and re-reads the schedule before it reads
        # the stages (#1536).
        schedules_query.populate_existing.return_value = schedules_query
        schedules_query.with_for_update.return_value = schedules_query
        schedules_query.one_or_none.return_value = schedule
        active_query = MagicMock()
        active_query.populate_existing.return_value = active_query
        active_query.filter.return_value = active_query
        active_query.first.return_value = active_stage
        pending_query = MagicMock()
        pending_query.populate_existing.return_value = pending_query
        pending_query.filter.return_value = pending_query
        pending_query.order_by.return_value = pending_query
        pending_query.all.return_value = [future_stage]
        stage_queries = iter([active_query, pending_query])

        def query_side_effect(model):
            if model is RolloutSchedule:
                return schedules_query
            if model is RolloutStage:
                return next(stage_queries)
            return MagicMock()

        mock_session.query.side_effect = query_side_effect

        scheduler = RolloutScheduler(interval_minutes=1)
        scheduler._activate_stage = AsyncMock(return_value=True)

        await scheduler.process_rollout_schedules()

        assert active_stage.status == RolloutStageStatus.COMPLETED
        scheduler._activate_stage.assert_not_called()
        assert schedule.status == RolloutScheduleStatus.ACTIVE
        mock_session.commit.assert_called_once()
