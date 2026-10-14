"""A rollback leaves a flag without its own safety configuration on the global defaults (#1206).

Until this was fixed, every rollback of a flag with no safety configuration of
its own gave it one, with no thresholds (``metrics = {}``). From then on the
monitor found nothing to check for that flag: re-enabled, it could breach the
global default thresholds without being rolled back again. Now a rollback
reads the flag's configuration and never writes one:

* a flag without one still has none after a manual rollback (the API route,
  which the dashboard's button calls) or an automatic one (the safety
  monitor), so ``GET .../config`` keeps answering the defaults with the nil id,
  and the next breach of a default threshold rolls it back again;
* its rollback record's ``safety_config_id`` is NULL, and no record is lost;
* a flag with its own configuration keeps it exactly as it was, and its
  record names it.

Every rollback path goes through ``SafetyService.execute_rollback``, and the
helpers that used to add the configuration are gone.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.metrics.metric import ErrorLog, MetricType, RawMetric
from backend.app.models.safety import (
    FeatureFlagSafetyConfig,
    SafetyRollbackRecord,
    SafetySettings,
)
from backend.app.services.safety_service import DEFAULT_CONFIG_ID, SafetyService
from backend.tests.integration.conftest import make_client_for_user

pytestmark = [pytest.mark.integration, pytest.mark.regression]

PREFIX = "r1206-"
SCHEMA = "test_experimentation"

#: The global default thresholds: a flag on the defaults breaches them at a
#: 50% error rate.
DEFAULT_METRICS = {
    "error_rate": {"critical_threshold": 0.05, "comparison_type": "greater_than"}
}


@pytest.fixture
def session_factory(db_session):
    # autoflush=False, as backend.app.db.session.SessionLocal is configured.
    factory = sessionmaker(bind=db_session.get_bind(), autoflush=False)

    def make():
        session = factory()
        session.execute(text(f"SET search_path TO {SCHEMA}"))
        return session

    return make


@pytest.fixture
def defaults_with_automatic_rollbacks(db_session):
    """Global default thresholds and automatic rollbacks on, restored afterwards."""
    row = db_session.query(SafetySettings).first()
    created = row is None
    if created:
        row = SafetySettings()
        db_session.add(row)
        previous = None
    else:
        previous = (row.enable_automatic_rollbacks, row.default_metrics)
    row.enable_automatic_rollbacks = True
    row.default_metrics = DEFAULT_METRICS
    db_session.commit()
    yield
    db_session.rollback()
    row = db_session.query(SafetySettings).first()
    if created:
        db_session.delete(row)
    else:
        row.enable_automatic_rollbacks, row.default_metrics = previous
    db_session.commit()


@pytest.fixture(autouse=True)
def _remove_what_the_test_created(db_session):
    yield
    db_session.rollback()
    flag_ids = [
        row.id
        for row in db_session.query(FeatureFlag.id).filter(
            FeatureFlag.key.like(f"{PREFIX}%")
        )
    ]
    if flag_ids:
        for model in (
            SafetyRollbackRecord,
            FeatureFlagSafetyConfig,
            ErrorLog,
            RawMetric,
        ):
            db_session.query(model).filter(model.feature_flag_id.in_(flag_ids)).delete(
                synchronize_session=False
            )
        db_session.query(FeatureFlag).filter(FeatureFlag.id.in_(flag_ids)).delete(
            synchronize_session=False
        )
    db_session.commit()


def _flag(db_session, rollout_percentage: int = 50) -> FeatureFlag:
    row = FeatureFlag(
        key=f"{PREFIX}{uuid.uuid4().hex[:10]}",
        name="r1206",
        status=FeatureFlagStatus.ACTIVE,
        rollout_percentage=rollout_percentage,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _breach_the_defaults(db_session, flag) -> None:
    """10 evaluations and 5 errors in the window: a 50% error rate."""
    at = datetime.utcnow() - timedelta(minutes=1)
    for _ in range(10):
        db_session.add(
            RawMetric(
                feature_flag_id=flag.id,
                metric_type=MetricType.FLAG_EVALUATION.value,
                timestamp=at,
            )
        )
    for i in range(5):
        db_session.add(
            ErrorLog(
                feature_flag_id=flag.id,
                error_type="crash",
                message=f"r1206 {i}",
                timestamp=at,
            )
        )
    db_session.commit()


def _configs(db_session, flag) -> list:
    db_session.expire_all()
    return (
        db_session.query(FeatureFlagSafetyConfig)
        .filter(FeatureFlagSafetyConfig.feature_flag_id == flag.id)
        .all()
    )


def _records(db_session, flag) -> list:
    db_session.expire_all()
    return (
        db_session.query(SafetyRollbackRecord)
        .filter(SafetyRollbackRecord.feature_flag_id == flag.id)
        .order_by(SafetyRollbackRecord.created_at)
        .all()
    )


def _state(db_session, flag) -> tuple:
    db_session.expire_all()
    row = db_session.query(FeatureFlag).filter(FeatureFlag.id == flag.id).one()
    return row.status, row.rollout_percentage


def _monitor_pass(session_factory) -> None:
    """One pass of the safety monitor, as the background scheduler runs it."""
    from backend.app.core.safety_scheduler import SafetyScheduler

    scheduler = SafetyScheduler(interval_minutes=1)
    with (
        patch("backend.app.core.safety_scheduler.SessionLocal", session_factory),
        patch.object(scheduler, "_notification_service", MagicMock()),
    ):
        asyncio.run(scheduler.check_feature_flags_safety())


# ---------------------------------------------------------------------------
# A flag on the defaults stays on them
# ---------------------------------------------------------------------------
@pytest.mark.usefixtures("defaults_with_automatic_rollbacks")
def test_a_manual_rollback_leaves_the_flag_on_the_defaults_and_the_monitor_rolls_it_back_again(
    db_session, session_factory, admin_user
):
    flag = _flag(db_session)
    _breach_the_defaults(db_session, flag)
    client = make_client_for_user(db_session, admin_user)

    resp = client.post(
        f"/api/v1/safety/feature-flags/{flag.id}/rollback",
        params={"percentage": 10, "reason": "r1206"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["success"] is True, resp.text

    assert _configs(db_session, flag) == [], "the rollback gave the flag a config"
    (record,) = _records(db_session, flag)
    assert record.safety_config_id is None
    assert _state(db_session, flag) == (FeatureFlagStatus.ACTIVE, 10)
    config = client.get(f"/api/v1/safety/feature-flags/{flag.id}/config")
    assert config.status_code == 200, config.text
    assert config.json()["id"] == str(DEFAULT_CONFIG_ID)
    served = config.json()["metrics"]
    assert set(served) == set(DEFAULT_METRICS)
    assert served["error_rate"]["critical_threshold"] == 0.05

    # Still at 10% and still breaching the defaults: the next pass of the
    # monitor rolls it back again, to the defaults' 0%.
    _monitor_pass(session_factory)

    assert _state(db_session, flag) == (FeatureFlagStatus.INACTIVE, 0), (
        "the monitor no longer sees the default threshold the flag still breaches"
    )
    records = _records(db_session, flag)
    assert [r.trigger_type for r in records] == ["manual", "automatic"]
    assert [r.safety_config_id for r in records] == [None, None]
    assert _configs(db_session, flag) == []


@pytest.mark.usefixtures("defaults_with_automatic_rollbacks")
def test_an_automatic_rollback_leaves_the_flag_on_the_defaults_for_the_next_breach(
    db_session, session_factory
):
    flag = _flag(db_session)
    _breach_the_defaults(db_session, flag)

    _monitor_pass(session_factory)

    assert _state(db_session, flag) == (FeatureFlagStatus.INACTIVE, 0)
    (record,) = _records(db_session, flag)
    assert record.trigger_type == "automatic"
    assert record.safety_config_id is None
    assert _configs(db_session, flag) == []

    # Turned back on, still breaching: rolled back again.
    db_session.expire_all()
    row = db_session.query(FeatureFlag).filter(FeatureFlag.id == flag.id).one()
    row.status = FeatureFlagStatus.ACTIVE
    row.rollout_percentage = 50
    db_session.commit()

    _monitor_pass(session_factory)

    assert _state(db_session, flag) == (FeatureFlagStatus.INACTIVE, 0), (
        "the monitor no longer sees the default threshold the flag still breaches"
    )
    records = _records(db_session, flag)
    assert len(records) == 2
    assert [r.safety_config_id for r in records] == [None, None]
    assert _configs(db_session, flag) == []


# ---------------------------------------------------------------------------
# A flag with its own configuration keeps it
# ---------------------------------------------------------------------------
OWN_CONFIGS = {
    "thresholds": {
        "enabled": True,
        "metrics": {
            "error_rate": {"critical_threshold": 0.2},
            "latency_p95": {"critical_threshold": 800},
        },
        "rollback_percentage": 0,
    },
    "no_latency_threshold": {
        "enabled": True,
        "metrics": {"error_rate": {"critical_threshold": 0.2}},
        "rollback_percentage": 5,
    },
    "monitoring_off": {
        "enabled": False,
        "metrics": {"error_rate": {"critical_threshold": 0.2}},
        "rollback_percentage": 0,
    },
    "empty_on_purpose": {"enabled": True, "metrics": {}, "rollback_percentage": 0},
}


def _columns(config) -> dict:
    return {
        "id": config.id,
        "enabled": config.enabled,
        "metrics": config.metrics,
        "rollback_percentage": config.rollback_percentage,
        "created_at": config.created_at,
        "updated_at": config.updated_at,
    }


@pytest.mark.parametrize("case", sorted(OWN_CONFIGS))
def test_a_flag_with_its_own_config_keeps_it_unchanged(db_session, case):
    flag = _flag(db_session)
    db_session.add(
        FeatureFlagSafetyConfig(feature_flag_id=flag.id, **OWN_CONFIGS[case])
    )
    db_session.commit()
    (before,) = _configs(db_session, flag)
    before = _columns(before)

    result = SafetyService(db_session).execute_rollback(
        db_session, flag.id, reason="r1206", target_percentage=10
    )

    assert result.success is True, result.message
    (after,) = _configs(db_session, flag)
    assert _columns(after) == before
    (record,) = _records(db_session, flag)
    assert record.safety_config_id == before["id"]


@pytest.mark.usefixtures("defaults_with_automatic_rollbacks")
def test_an_empty_configuration_of_its_own_is_checked_against_nothing(
    db_session, session_factory
):
    """What an earlier release left at a rollback, and what an operator posts
    to stop the checks: ``metrics`` ``{}`` is the flag's own configuration,
    with no thresholds, so the global defaults do not apply to it. Upgrading
    does not remove such a configuration (#1206 stays open for that)."""
    flag = _flag(db_session)
    db_session.add(FeatureFlagSafetyConfig(feature_flag_id=flag.id, metrics={}))
    db_session.commit()
    _breach_the_defaults(db_session, flag)

    _monitor_pass(session_factory)

    assert _state(db_session, flag) == (FeatureFlagStatus.ACTIVE, 50)
    assert _records(db_session, flag) == []
    (config,) = _configs(db_session, flag)
    assert config.metrics == {}


# ---------------------------------------------------------------------------
# No path creates a configuration
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name",
    [
        "get_or_create_safety_config",
        "_get_or_add_safety_config",
        "create_rollback_record",
    ],
)
def test_the_helpers_that_created_a_config_at_a_rollback_are_gone(name):
    """Each of them added ``FeatureFlagSafetyConfig(metrics={})`` for a flag
    without one; ``execute_rollback`` is the only rollback writer."""
    assert not hasattr(SafetyService, name)
