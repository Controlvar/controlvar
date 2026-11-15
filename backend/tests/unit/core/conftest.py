"""
Fixtures for unit testing the rollout scheduler.

This module provides test fixtures for the rollout scheduler tests.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

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


@pytest.fixture
def current_time():
    """Return a fixed datetime for testing."""
    return datetime(2023, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def scheduler():
    """Return a scheduler instance for testing."""
    return RolloutScheduler(interval_minutes=1)


@pytest.fixture
def mock_feature_flag():
    """Create a mock feature flag."""
    flag = MagicMock(spec=FeatureFlag)
    flag.id = "flag-1"
    flag.key = "test-flag"
    flag.rollout_percentage = 0
    flag.updated_at = None
    return flag


@pytest.fixture
def mock_rollout_schedule():
    """Create a mock rollout schedule."""
    schedule = MagicMock(spec=RolloutSchedule)
    schedule.id = "schedule-1"
    schedule.name = "Test Schedule"
    schedule.feature_flag_id = "flag-1"
    schedule.status = RolloutScheduleStatus.ACTIVE
    schedule.min_stage_duration = None
    schedule.end_date = None
    schedule.updated_at = None
    return schedule


@pytest.fixture
def mock_pending_stage():
    """Create a mock pending stage."""
    stage = MagicMock(spec=RolloutStage)
    stage.id = "stage-1"
    stage.name = "Stage 1"
    stage.rollout_schedule_id = "schedule-1"
    stage.status = RolloutStageStatus.PENDING
    stage.stage_order = 1
    stage.trigger_type = TriggerType.TIME_BASED
    stage.start_date = None  # Eligible for immediate activation
    stage.target_percentage = 25
    stage.updated_at = None
    stage.started_at = None  # Not started (#1528)
    stage.completed_date = None
    stage.trigger_configuration = None
    return stage


@pytest.fixture
def mock_active_stage():
    """Create a mock active stage."""
    stage = MagicMock(spec=RolloutStage)
    stage.id = "stage-1"
    stage.name = "Stage 1"
    stage.rollout_schedule_id = "schedule-1"
    stage.status = RolloutStageStatus.IN_PROGRESS
    stage.stage_order = 1
    stage.trigger_type = TriggerType.TIME_BASED
    stage.start_date = None
    stage.target_percentage = 25
    stage.updated_at = datetime.now(timezone.utc) - timedelta(hours=25)
    # When it started (#1528): the scheduler measures from this.
    stage.started_at = stage.updated_at
    stage.completed_date = None
    stage.trigger_configuration = None
    return stage
