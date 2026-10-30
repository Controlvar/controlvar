"""
Tests for EP-030 Batch 3A: Scheduler -> NotificationService wiring.

Verifies that each scheduler calls the appropriate NotificationService method
on key events, and that notification failures never disrupt primary scheduler logic.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from backend.app.models.feature_flag import FeatureFlagStatus

# ---------------------------------------------------------------------------
# Safety Scheduler Tests
# ---------------------------------------------------------------------------


class TestSafetySchedulerNotifications:
    """Tests that SafetyScheduler calls NotificationService on safety rollback events."""

    @pytest.mark.asyncio
    async def test_safety_rollback_calls_notify_safety_rollback(self):
        """notify_safety_rollback is called when a rollback succeeds."""
        mock_notification_service = MagicMock()
        mock_notification_service.notify_safety_rollback = MagicMock(return_value=True)

        # Build lightweight mocks for all DB/service collaborators
        mock_flag = MagicMock()
        mock_flag.id = "flag-uuid-1"
        mock_flag.key = "my-feature-flag"
        mock_flag.rollout_percentage = 50
        mock_flag.status = FeatureFlagStatus.ACTIVE

        mock_metric = MagicMock()
        mock_metric.is_healthy = False
        mock_metric.name = "error_rate"
        mock_metric.current_value = 0.15
        mock_metric.threshold = 0.05

        mock_safety_check = MagicMock()
        mock_safety_check.is_healthy = False
        mock_safety_check.details = "error_rate too high"
        mock_safety_check.metrics = [mock_metric]

        mock_config = MagicMock()
        mock_config.enabled = True

        mock_settings = MagicMock()
        mock_settings.enable_automatic_rollbacks = True

        mock_rollback_result = MagicMock()
        mock_rollback_result.success = True
        mock_rollback_result.message = "Rolled back to 0%"

        mock_safety_service = MagicMock()
        mock_safety_service.async_get_feature_flag_safety_config = AsyncMock(
            return_value=mock_config
        )
        mock_safety_service.check_feature_flag_safety = AsyncMock(
            return_value=mock_safety_check
        )
        mock_safety_service.async_get_safety_settings = AsyncMock(
            return_value=mock_settings
        )
        mock_safety_service.async_rollback_feature_flag = AsyncMock(
            return_value=mock_rollback_result
        )

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = [mock_flag]

        with (
            patch(
                "backend.app.core.safety_scheduler.SessionLocal", return_value=mock_db
            ),
            patch(
                "backend.app.core.safety_scheduler.SafetyService",
                return_value=mock_safety_service,
            ),
        ):
            from backend.app.core.safety_scheduler import SafetyScheduler

            scheduler = SafetyScheduler()
            scheduler._notification_service = mock_notification_service

            await scheduler.check_feature_flags_safety()

        mock_notification_service.notify_safety_rollback.assert_called_once()

    @pytest.mark.asyncio
    async def test_safety_rollback_notification_swallows_errors(self):
        """A notification failure does not propagate and scheduler continues."""
        mock_notification_service = MagicMock()
        mock_notification_service.notify_safety_rollback.side_effect = RuntimeError(
            "webhook down"
        )

        mock_flag = MagicMock()
        mock_flag.id = "flag-uuid-2"
        mock_flag.key = "another-flag"
        mock_flag.rollout_percentage = 30
        mock_flag.status = FeatureFlagStatus.ACTIVE

        mock_metric = MagicMock()
        mock_metric.is_healthy = False
        mock_metric.name = "latency"
        mock_metric.current_value = 500
        mock_metric.threshold = 200

        mock_safety_check = MagicMock()
        mock_safety_check.is_healthy = False
        mock_safety_check.metrics = [mock_metric]

        mock_config = MagicMock()
        mock_config.enabled = True

        mock_settings = MagicMock()
        mock_settings.enable_automatic_rollbacks = True

        mock_rollback_result = MagicMock()
        mock_rollback_result.success = True
        mock_rollback_result.message = "Rolled back"

        mock_safety_service = MagicMock()
        mock_safety_service.async_get_feature_flag_safety_config = AsyncMock(
            return_value=mock_config
        )
        mock_safety_service.check_feature_flag_safety = AsyncMock(
            return_value=mock_safety_check
        )
        mock_safety_service.async_get_safety_settings = AsyncMock(
            return_value=mock_settings
        )
        mock_safety_service.async_rollback_feature_flag = AsyncMock(
            return_value=mock_rollback_result
        )

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = [mock_flag]

        with (
            patch(
                "backend.app.core.safety_scheduler.SessionLocal", return_value=mock_db
            ),
            patch(
                "backend.app.core.safety_scheduler.SafetyService",
                return_value=mock_safety_service,
            ),
        ):
            from backend.app.core.safety_scheduler import SafetyScheduler

            scheduler = SafetyScheduler()
            scheduler._notification_service = mock_notification_service

            # Should not raise even though notification raises
            await scheduler.check_feature_flags_safety()

        # Notification was attempted
        mock_notification_service.notify_safety_rollback.assert_called_once()

    @pytest.mark.asyncio
    async def test_safety_no_rollback_when_healthy_no_notification(self):
        """No notification is sent when the feature flag is healthy."""
        mock_notification_service = MagicMock()

        mock_flag = MagicMock()
        mock_flag.id = "flag-uuid-3"
        mock_flag.key = "healthy-flag"
        mock_flag.rollout_percentage = 20
        mock_flag.status = FeatureFlagStatus.ACTIVE

        mock_safety_check = MagicMock()
        mock_safety_check.is_healthy = True
        mock_safety_check.metrics = []

        mock_config = MagicMock()
        mock_config.enabled = True

        mock_settings = MagicMock()
        mock_settings.enable_automatic_rollbacks = True

        mock_safety_service = MagicMock()
        mock_safety_service.async_get_feature_flag_safety_config = AsyncMock(
            return_value=mock_config
        )
        mock_safety_service.check_feature_flag_safety = AsyncMock(
            return_value=mock_safety_check
        )
        mock_safety_service.async_get_safety_settings = AsyncMock(
            return_value=mock_settings
        )

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = [mock_flag]

        with (
            patch(
                "backend.app.core.safety_scheduler.SessionLocal", return_value=mock_db
            ),
            patch(
                "backend.app.core.safety_scheduler.SafetyService",
                return_value=mock_safety_service,
            ),
        ):
            from backend.app.core.safety_scheduler import SafetyScheduler

            scheduler = SafetyScheduler()
            scheduler._notification_service = mock_notification_service

            await scheduler.check_feature_flags_safety()

        mock_notification_service.notify_safety_rollback.assert_not_called()

    @pytest.mark.asyncio
    async def test_safety_rollback_passes_flag_name(self):
        """notify_safety_rollback receives the correct feature_flag_name argument."""
        mock_notification_service = MagicMock()
        mock_notification_service.notify_safety_rollback = MagicMock(return_value=True)

        mock_flag = MagicMock()
        mock_flag.id = "flag-uuid-4"
        mock_flag.key = "checkout-v2"
        mock_flag.rollout_percentage = 10
        mock_flag.status = FeatureFlagStatus.ACTIVE

        mock_metric = MagicMock()
        mock_metric.is_healthy = False
        mock_metric.name = "error_rate"
        mock_metric.current_value = 0.2
        mock_metric.threshold = 0.05

        mock_safety_check = MagicMock()
        mock_safety_check.is_healthy = False
        mock_safety_check.metrics = [mock_metric]

        mock_config = MagicMock()
        mock_config.enabled = True

        mock_settings = MagicMock()
        mock_settings.enable_automatic_rollbacks = True

        mock_rollback_result = MagicMock()
        mock_rollback_result.success = True
        mock_rollback_result.message = "ok"

        mock_safety_service = MagicMock()
        mock_safety_service.async_get_feature_flag_safety_config = AsyncMock(
            return_value=mock_config
        )
        mock_safety_service.check_feature_flag_safety = AsyncMock(
            return_value=mock_safety_check
        )
        mock_safety_service.async_get_safety_settings = AsyncMock(
            return_value=mock_settings
        )
        mock_safety_service.async_rollback_feature_flag = AsyncMock(
            return_value=mock_rollback_result
        )

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = [mock_flag]

        with (
            patch(
                "backend.app.core.safety_scheduler.SessionLocal", return_value=mock_db
            ),
            patch(
                "backend.app.core.safety_scheduler.SafetyService",
                return_value=mock_safety_service,
            ),
        ):
            from backend.app.core.safety_scheduler import SafetyScheduler

            scheduler = SafetyScheduler()
            scheduler._notification_service = mock_notification_service

            await scheduler.check_feature_flags_safety()

        kwargs = mock_notification_service.notify_safety_rollback.call_args
        assert kwargs[1].get("feature_flag_name") == "checkout-v2" or (
            kwargs[0] and "checkout-v2" in str(kwargs)
        )

    @pytest.mark.asyncio
    async def test_safety_rollback_passes_reason(self):
        """notify_safety_rollback receives a non-empty reason string."""
        mock_notification_service = MagicMock()
        mock_notification_service.notify_safety_rollback = MagicMock(return_value=True)

        mock_flag = MagicMock()
        mock_flag.id = "flag-uuid-5"
        mock_flag.key = "payments-flag"
        mock_flag.rollout_percentage = 40
        mock_flag.status = FeatureFlagStatus.ACTIVE

        mock_metric = MagicMock()
        mock_metric.is_healthy = False
        mock_metric.name = "error_rate"
        mock_metric.current_value = 0.3
        mock_metric.threshold = 0.1

        mock_safety_check = MagicMock()
        mock_safety_check.is_healthy = False
        mock_safety_check.metrics = [mock_metric]

        mock_config = MagicMock()
        mock_config.enabled = True

        mock_settings = MagicMock()
        mock_settings.enable_automatic_rollbacks = True

        mock_rollback_result = MagicMock()
        mock_rollback_result.success = True
        mock_rollback_result.message = "ok"

        mock_safety_service = MagicMock()
        mock_safety_service.async_get_feature_flag_safety_config = AsyncMock(
            return_value=mock_config
        )
        mock_safety_service.check_feature_flag_safety = AsyncMock(
            return_value=mock_safety_check
        )
        mock_safety_service.async_get_safety_settings = AsyncMock(
            return_value=mock_settings
        )
        mock_safety_service.async_rollback_feature_flag = AsyncMock(
            return_value=mock_rollback_result
        )

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = [mock_flag]

        with (
            patch(
                "backend.app.core.safety_scheduler.SessionLocal", return_value=mock_db
            ),
            patch(
                "backend.app.core.safety_scheduler.SafetyService",
                return_value=mock_safety_service,
            ),
        ):
            from backend.app.core.safety_scheduler import SafetyScheduler

            scheduler = SafetyScheduler()
            scheduler._notification_service = mock_notification_service

            await scheduler.check_feature_flags_safety()

        kwargs = mock_notification_service.notify_safety_rollback.call_args
        # reason is passed as keyword argument
        reason_value = kwargs[1].get("reason", "")
        assert reason_value, "reason should be a non-empty string"
        assert (
            "rollback" in reason_value.lower() or "error_rate" in reason_value.lower()
        )


# ---------------------------------------------------------------------------
# Rollout Scheduler Tests
# ---------------------------------------------------------------------------


class TestRolloutSchedulerNotifications:
    """Tests that RolloutScheduler calls NotificationService when a stage advances.

    The notification is sent by the tick once the schedule's change is
    committed (#593), so these drive ``process_rollout_schedules`` with a
    mocked session: one ACTIVE schedule, no stage in progress, one pending
    stage that may start at once.
    """

    def _make_rollout_mocks(
        self, flag_id="flag-rollout-1", stage_name="Stage 1", target_pct=25
    ):
        """Build a mocked session holding one schedule ready to start a stage."""
        from backend.app.models.feature_flag import FeatureFlag
        from backend.app.models.rollout_schedule import (
            RolloutSchedule,
            RolloutScheduleStatus,
            RolloutStage,
            RolloutStageStatus,
            TriggerType,
        )

        mock_stage = MagicMock()
        mock_stage.id = "stage-uuid-1"
        mock_stage.name = stage_name
        mock_stage.target_percentage = target_pct
        mock_stage.status = RolloutStageStatus.PENDING
        mock_stage.trigger_type = TriggerType.TIME_BASED
        mock_stage.start_date = None
        mock_stage.updated_at = datetime.now(timezone.utc)

        mock_schedule = MagicMock()
        mock_schedule.id = "schedule-uuid-1"
        mock_schedule.feature_flag_id = flag_id
        mock_schedule.status = RolloutScheduleStatus.ACTIVE

        mock_flag = MagicMock()
        mock_flag.id = flag_id
        mock_flag.key = "my-rollout-flag"
        mock_flag.rollout_percentage = 0
        mock_flag.status = FeatureFlagStatus.ACTIVE

        schedules_query = MagicMock()
        schedules_query.filter.return_value = schedules_query
        schedules_query.order_by.return_value = schedules_query
        schedules_query.all.return_value = [mock_schedule]
        # The re-read of the schedule under the flag lock (#629).
        schedules_query.populate_existing.return_value = schedules_query
        schedules_query.with_for_update.return_value = schedules_query
        schedules_query.one_or_none.return_value = mock_schedule
        active_query = MagicMock()
        active_query.filter.return_value = active_query
        active_query.first.return_value = None
        pending_query = MagicMock()
        pending_query.filter.return_value = pending_query
        pending_query.order_by.return_value = pending_query
        pending_query.all.return_value = [mock_stage]
        flag_query = MagicMock()
        flag_query.filter.return_value = flag_query
        flag_query.with_for_update.return_value = flag_query
        flag_query.first.return_value = mock_flag
        stage_queries = iter([active_query, pending_query])

        def query_side_effect(model):
            if model is RolloutSchedule:
                return schedules_query
            if model is RolloutStage:
                return next(stage_queries)
            if model is FeatureFlag:
                return flag_query
            return MagicMock()

        mock_db = MagicMock()
        mock_db.query.side_effect = query_side_effect

        return mock_db, mock_schedule, mock_stage, mock_flag

    async def _tick(self, notification_service, mock_db):
        from backend.app.core.rollout_scheduler import RolloutScheduler

        scheduler = RolloutScheduler()
        scheduler._notification_service = notification_service
        with patch(
            "backend.app.core.rollout_scheduler.SessionLocal", return_value=mock_db
        ):
            return await scheduler.process_rollout_schedules()

    @pytest.mark.asyncio
    async def test_rollout_advance_calls_notify_rollout_advanced(self):
        """notify_rollout_advanced is called once the stage start is committed."""
        mock_notification_service = MagicMock()
        mock_notification_service.notify_rollout_advanced = MagicMock(return_value=True)

        mock_db, mock_schedule, mock_stage, mock_flag = self._make_rollout_mocks()
        order = []
        mock_db.commit.side_effect = lambda: order.append("commit")
        mock_notification_service.notify_rollout_advanced.side_effect = (
            lambda **kwargs: order.append("notify")
        )

        result = await self._tick(mock_notification_service, mock_db)

        assert result["items_processed"] == 1
        assert mock_flag.rollout_percentage == 25
        mock_notification_service.notify_rollout_advanced.assert_called_once()
        assert order == ["commit", "notify"]

    @pytest.mark.asyncio
    async def test_rollout_advance_notification_swallows_errors(self):
        """A notification failure does not undo or fail the stage start."""
        mock_notification_service = MagicMock()
        mock_notification_service.notify_rollout_advanced.side_effect = RuntimeError(
            "network error"
        )

        mock_db, mock_schedule, mock_stage, mock_flag = self._make_rollout_mocks()

        result = await self._tick(mock_notification_service, mock_db)

        # Stage was activated successfully despite notification error
        assert result["items_processed"] == 1
        assert result["items_failed"] == 0
        mock_db.rollback.assert_not_called()
        mock_notification_service.notify_rollout_advanced.assert_called_once()

    @pytest.mark.asyncio
    async def test_rollout_advance_passes_stage_name(self):
        """notify_rollout_advanced receives the correct stage_name."""
        mock_notification_service = MagicMock()

        mock_db, mock_schedule, mock_stage, mock_flag = self._make_rollout_mocks(
            stage_name="Beta Rollout"
        )

        await self._tick(mock_notification_service, mock_db)

        kwargs = mock_notification_service.notify_rollout_advanced.call_args
        assert kwargs[1].get("stage_name") == "Beta Rollout"

    @pytest.mark.asyncio
    async def test_rollout_advance_passes_percentage(self):
        """notify_rollout_advanced receives the correct new_percentage."""
        mock_notification_service = MagicMock()

        mock_db, mock_schedule, mock_stage, mock_flag = self._make_rollout_mocks(
            target_pct=75
        )

        await self._tick(mock_notification_service, mock_db)

        kwargs = mock_notification_service.notify_rollout_advanced.call_args
        assert kwargs[1].get("new_percentage") == 75
        assert kwargs[1].get("feature_flag_id") == "flag-rollout-1"


# ---------------------------------------------------------------------------
# Experiment Scheduler Tests
# ---------------------------------------------------------------------------


class TestExperimentSchedulerNotifications:
    """Tests that ExperimentScheduler calls NotificationService on experiment events."""

    def _make_experiment_mock(
        self, exp_id="exp-uuid-1", name="My Experiment", start_date=None, end_date=None
    ):
        """Build a mock Experiment for scheduler tests."""
        from backend.app.models.experiment import ExperimentStatus

        now = datetime.now(timezone.utc)
        mock_exp = MagicMock()
        mock_exp.id = exp_id
        mock_exp.name = name
        mock_exp.start_date = start_date or now
        mock_exp.end_date = end_date or now
        mock_exp.status = ExperimentStatus.DRAFT
        # What Start requires; the scheduler starts only such a draft (#1454).
        mock_exp.variants = [MagicMock(is_control=True), MagicMock(is_control=False)]
        mock_exp.metric_definitions = [MagicMock()]
        return mock_exp

    @pytest.mark.asyncio
    async def test_experiment_started_calls_notify(self):
        """notify_experiment_started is called for each activated experiment."""
        mock_notification_service = MagicMock()
        mock_notification_service.notify_experiment_started = MagicMock(
            return_value=True
        )

        mock_exp = self._make_experiment_mock()

        mock_db = MagicMock()
        # First query returns experiments_to_activate, second returns empty (nothing to complete)
        mock_db.query.return_value.filter.return_value.all.side_effect = [
            [mock_exp],  # experiments_to_activate
            [],  # experiments_to_complete
        ]

        with patch("backend.app.core.scheduler.SessionLocal", return_value=mock_db):
            from backend.app.core.scheduler import ExperimentScheduler

            scheduler = ExperimentScheduler()
            scheduler._notification_service = mock_notification_service

            await scheduler.process_scheduled_experiments()

        mock_notification_service.notify_experiment_started.assert_called_once()

    @pytest.mark.asyncio
    async def test_experiment_completed_calls_notify(self):
        """notify_experiment_ended is called for each completed experiment."""
        mock_notification_service = MagicMock()
        mock_notification_service.notify_experiment_ended = MagicMock(return_value=True)

        mock_exp = self._make_experiment_mock(name="Checkout Experiment")

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.side_effect = [
            [],  # nothing to activate
            [mock_exp],  # experiments_to_complete
        ]

        with patch("backend.app.core.scheduler.SessionLocal", return_value=mock_db):
            from backend.app.core.scheduler import ExperimentScheduler

            scheduler = ExperimentScheduler()
            scheduler._notification_service = mock_notification_service

            await scheduler.process_scheduled_experiments()

        mock_notification_service.notify_experiment_ended.assert_called_once()

    @pytest.mark.asyncio
    async def test_experiment_notification_swallows_errors(self):
        """A notification failure does not stop scheduling from completing."""
        mock_notification_service = MagicMock()
        mock_notification_service.notify_experiment_started.side_effect = RuntimeError(
            "slack down"
        )

        mock_exp = self._make_experiment_mock()

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.side_effect = [
            [mock_exp],
            [],
        ]

        with patch("backend.app.core.scheduler.SessionLocal", return_value=mock_db):
            from backend.app.core.scheduler import ExperimentScheduler

            scheduler = ExperimentScheduler()
            scheduler._notification_service = mock_notification_service

            # Must not raise
            await scheduler.process_scheduled_experiments()

        mock_notification_service.notify_experiment_started.assert_called_once()

    @pytest.mark.asyncio
    async def test_experiment_started_passes_name(self):
        """notify_experiment_started receives the correct experiment_name."""
        mock_notification_service = MagicMock()

        mock_exp = self._make_experiment_mock(name="Homepage Banner Test")

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.side_effect = [
            [mock_exp],
            [],
        ]

        with patch("backend.app.core.scheduler.SessionLocal", return_value=mock_db):
            from backend.app.core.scheduler import ExperimentScheduler

            scheduler = ExperimentScheduler()
            scheduler._notification_service = mock_notification_service

            await scheduler.process_scheduled_experiments()

        kwargs = mock_notification_service.notify_experiment_started.call_args
        assert kwargs[1].get("experiment_name") == "Homepage Banner Test" or (
            kwargs[0] and "Homepage Banner Test" in str(kwargs)
        )

    @pytest.mark.asyncio
    async def test_experiment_completed_passes_name(self):
        """notify_experiment_ended receives the correct experiment_name."""
        mock_notification_service = MagicMock()

        mock_exp = self._make_experiment_mock(name="Search Ranking V2")

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.side_effect = [
            [],
            [mock_exp],
        ]

        with patch("backend.app.core.scheduler.SessionLocal", return_value=mock_db):
            from backend.app.core.scheduler import ExperimentScheduler

            scheduler = ExperimentScheduler()
            scheduler._notification_service = mock_notification_service

            await scheduler.process_scheduled_experiments()

        kwargs = mock_notification_service.notify_experiment_ended.call_args
        assert kwargs[1].get("experiment_name") == "Search Ranking V2" or (
            kwargs[0] and "Search Ranking V2" in str(kwargs)
        )


# ---------------------------------------------------------------------------
# Integration-style wiring tests (verify attribute/import presence)
# ---------------------------------------------------------------------------


class TestSchedulerWiring:
    """Verify each scheduler is actually wired to the NotificationService."""

    def test_safety_scheduler_wired_to_notification_service(self):
        """SafetyScheduler instance exposes _notification_service after our wiring."""
        from backend.app.core.safety_scheduler import SafetyScheduler

        scheduler = SafetyScheduler()
        assert hasattr(scheduler, "_notification_service") or hasattr(
            scheduler, "notification_service"
        ), "SafetyScheduler must expose a notification_service attribute"

    def test_rollout_scheduler_wired_to_notification_service(self):
        """RolloutScheduler instance exposes _notification_service after our wiring."""
        from backend.app.core.rollout_scheduler import RolloutScheduler

        scheduler = RolloutScheduler()
        assert hasattr(scheduler, "_notification_service") or hasattr(
            scheduler, "notification_service"
        ), "RolloutScheduler must expose a notification_service attribute"

    def test_experiment_scheduler_wired_to_notification_service(self):
        """ExperimentScheduler instance exposes _notification_service after our wiring."""
        from backend.app.core.scheduler import ExperimentScheduler

        scheduler = ExperimentScheduler()
        assert hasattr(scheduler, "_notification_service") or hasattr(
            scheduler, "notification_service"
        ), "ExperimentScheduler must expose a notification_service attribute"

    def test_notification_service_imported_in_safety_scheduler(self):
        """The safety_scheduler module imports NotificationService."""
        import importlib

        import backend.app.core.safety_scheduler as mod

        # Re-import to get fresh module state
        importlib.reload(mod)

        scheduler = mod.SafetyScheduler()
        # After wiring, the scheduler should hold a NotificationService instance
        ns = getattr(scheduler, "_notification_service", None) or getattr(
            scheduler, "notification_service", None
        )
        assert ns is not None, (
            "SafetyScheduler must have a NotificationService instance"
        )
        # Verify it has the expected method
        assert callable(getattr(ns, "notify_safety_rollback", None)), (
            "notification_service must expose notify_safety_rollback"
        )

    def test_notification_service_imported_in_rollout_scheduler(self):
        """The rollout_scheduler module imports NotificationService."""
        import importlib

        import backend.app.core.rollout_scheduler as mod

        importlib.reload(mod)

        scheduler = mod.RolloutScheduler()
        ns = getattr(scheduler, "_notification_service", None) or getattr(
            scheduler, "notification_service", None
        )
        assert ns is not None, (
            "RolloutScheduler must have a NotificationService instance"
        )
        assert callable(getattr(ns, "notify_rollout_advanced", None)), (
            "notification_service must expose notify_rollout_advanced"
        )

    def test_notification_service_imported_in_experiment_scheduler(self):
        """The scheduler module imports NotificationService."""
        import importlib

        import backend.app.core.scheduler as mod

        importlib.reload(mod)

        scheduler = mod.ExperimentScheduler()
        ns = getattr(scheduler, "_notification_service", None) or getattr(
            scheduler, "notification_service", None
        )
        assert ns is not None, (
            "ExperimentScheduler must have a NotificationService instance"
        )
        assert callable(getattr(ns, "notify_experiment_started", None)), (
            "notification_service must expose notify_experiment_started"
        )
