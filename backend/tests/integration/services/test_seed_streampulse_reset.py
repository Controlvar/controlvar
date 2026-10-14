"""The StreamPulse seed's ``--reset`` followed by a reseed starts the rollout story again.

``demo/streampulse/simulator/README.md`` tells a presenter to reset the whole
story with ``seed_streampulse.py --reset`` and a reseed. The story leaves Player
v2 at 100 % with no rules and its schedule completed; after the reset and the
reseed the flag is back where the story starts: 5 % with the employee rule, its
four-stage schedule active at stage 1, and its safety configuration rolling
back to 5 %.

The seed's key files and ``.env.local`` are moved to a temporary folder, and
the test removes what it seeded, whatever happens.
"""

from __future__ import annotations

import pytest

from backend.app.models.feature_flag import FeatureFlag
from backend.app.models.rollout_schedule import (
    RolloutSchedule,
    RolloutScheduleStatus,
    RolloutStageStatus,
)
from backend.app.models.safety import FeatureFlagSafetyConfig
from backend.scripts import seed_streampulse

pytestmark = [pytest.mark.integration]


@pytest.fixture
def story_seed(tmp_path, monkeypatch, db_session):
    """The seed's files in ``tmp_path``; nothing of the seed left afterwards."""
    monkeypatch.setattr(seed_streampulse, "STREAMPULSE_DIR", tmp_path)
    monkeypatch.setattr(seed_streampulse, "API_KEY_FILE", tmp_path / ".api_key")
    monkeypatch.setattr(
        seed_streampulse, "SERVER_KEY_FILE", tmp_path / ".api_key_server"
    )
    monkeypatch.setattr(seed_streampulse, "ENV_LOCAL_FILE", tmp_path / ".env.local")
    seed_streampulse.reset_streampulse(db_session)
    yield
    db_session.rollback()
    seed_streampulse.reset_streampulse(db_session)


def _player(db_session) -> FeatureFlag:
    db_session.expire_all()
    return (
        db_session.query(FeatureFlag)
        .filter(FeatureFlag.key == seed_streampulse.PLAYER_FLAG)
        .one()
    )


def _schedule(db_session, flag: FeatureFlag) -> RolloutSchedule:
    return (
        db_session.query(RolloutSchedule)
        .filter(RolloutSchedule.feature_flag_id == flag.id)
        .one()
    )


def test_reset_and_a_reseed_start_the_rollout_story_again(
    db_session, admin_user, story_seed
):
    seed_streampulse.seed_feature_flags(db_session, admin_user)

    # Where the story ends: 100 %, no rules, the schedule completed.
    player = _player(db_session)
    player.rollout_percentage = 100
    player.targeting_rules = {}
    schedule = _schedule(db_session, player)
    schedule.status = RolloutScheduleStatus.COMPLETED
    for stage in schedule.stages:
        stage.status = RolloutStageStatus.COMPLETED
    db_session.commit()

    counts = seed_streampulse.reset_streampulse(db_session)
    assert counts["feature_flags"] == len(seed_streampulse.FLAG_KEYS)
    assert (
        db_session.query(FeatureFlag)
        .filter(FeatureFlag.key.in_(seed_streampulse.FLAG_KEYS))
        .count()
        == 0
    )

    seed_streampulse.seed_feature_flags(db_session, admin_user)

    player = _player(db_session)
    assert player.rollout_percentage == 5
    assert player.targeting_rules == seed_streampulse.PLAYER_V2_RULES
    schedule = _schedule(db_session, player)
    assert schedule.status == RolloutScheduleStatus.ACTIVE
    stages = sorted(schedule.stages, key=lambda stage: stage.stage_order)
    assert [(s.target_percentage, s.status) for s in stages] == [
        (5, RolloutStageStatus.IN_PROGRESS),
        (25, RolloutStageStatus.PENDING),
        (50, RolloutStageStatus.PENDING),
        (100, RolloutStageStatus.PENDING),
    ]
    config = (
        db_session.query(FeatureFlagSafetyConfig)
        .filter(FeatureFlagSafetyConfig.feature_flag_id == player.id)
        .one()
    )
    assert config.enabled and config.rollback_percentage == 5
