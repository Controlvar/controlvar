"""The real safety check, run the way the scheduler now runs it (#1690).

The scheduler runs ``SafetyScheduler.check_feature_flags_safety`` in a worker
thread, on a private event loop (``_check_off_loop``). An await added to the
check would raise no error of its own there: a harmless one simply runs, and
the process-wide Redis pool in ``backend.app.api.deps`` would be created on
the private loop, which closes when the tick ends, and then be reused by the
API's requests, whose cache errors are swallowed.

So the first test drives the check by hand, with no event loop at all, the
cache on and Redis unreachable, and makes two checks that catch different
things: ``send(None)`` must finish the coroutine (an await that suspends
fails that), and the check must not have asked for the Redis pool (an await
of the experiment cache drop does not suspend with no loop, and fails only
that). It also rolls a breaching flag back, so the rollback path is covered.

The second runs the offloaded tick through ``run_locked_tick``, under the
real advisory lock: of twenty flags, it rolls back exactly the five that
breach.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.core.config import settings
from backend.app.core.safety_scheduler import SCHEDULER_NAME, SafetyScheduler
from backend.app.core.scheduler_tick import STATUS_SUCCESS, run_locked_tick
from backend.app.models.audit_log import AuditLog
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.metrics.metric import ErrorLog, MetricType, RawMetric
from backend.app.models.safety import (
    FeatureFlagSafetyConfig,
    SafetyRollbackRecord,
    SafetySettings,
)

pytestmark = [pytest.mark.integration, pytest.mark.regression]

PREFIX = "c1690-"
SCHEMA = "test_experimentation"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
def automatic_rollbacks(db_session):
    """Global automatic rollbacks on for the test, restored afterwards."""
    row = db_session.query(SafetySettings).first()
    created = row is None
    if created:
        row = SafetySettings(enable_automatic_rollbacks=True, default_metrics=None)
        db_session.add(row)
        previous = None
    else:
        previous = row.enable_automatic_rollbacks
        row.enable_automatic_rollbacks = True
    db_session.commit()
    yield
    db_session.rollback()
    row = db_session.query(SafetySettings).first()
    if created:
        db_session.delete(row)
    else:
        row.enable_automatic_rollbacks = previous
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
        db_session.query(AuditLog).filter(AuditLog.entity_id.in_(flag_ids)).delete(
            synchronize_session=False
        )
        db_session.query(FeatureFlag).filter(FeatureFlag.id.in_(flag_ids)).delete(
            synchronize_session=False
        )
    db_session.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _flag(db_session) -> FeatureFlag:
    """An ACTIVE flag at 50%, monitored (error rate above 5%), rolled back to 0."""
    row = FeatureFlag(
        key=f"{PREFIX}{uuid.uuid4().hex[:10]}",
        name="c1690 off the loop",
        status=FeatureFlagStatus.ACTIVE,
        rollout_percentage=50,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    db_session.add(
        FeatureFlagSafetyConfig(
            feature_flag_id=row.id,
            enabled=True,
            metrics={
                "error_rate": {
                    "critical_threshold": 0.05,
                    "comparison_type": "greater_than",
                }
            },
            rollback_percentage=0,
        )
    )
    db_session.commit()
    return row


def _traffic(db_session, flag, errors: int) -> None:
    """100 evaluations in the window, ``errors`` of them errors."""
    db_session.add_all(
        RawMetric(feature_flag_id=flag.id, metric_type=MetricType.FLAG_EVALUATION.value)
        for _ in range(100)
    )
    db_session.add_all(
        ErrorLog(feature_flag_id=flag.id, error_type="crash", message=f"c1690 {i}")
        for i in range(errors)
    )
    db_session.commit()


def _rollbacks(db_session, flag) -> list:
    return [
        (r.previous_percentage, r.target_percentage)
        for r in db_session.query(SafetyRollbackRecord).filter(
            SafetyRollbackRecord.feature_flag_id == flag.id
        )
    ]


# ---------------------------------------------------------------------------
# The real check never suspends and never asks for the Redis pool
# ---------------------------------------------------------------------------


def test_the_real_safety_check_never_suspends_and_never_asks_for_the_redis_pool(
    db_session, session_factory, automatic_rollbacks, monkeypatch
):
    # The cache on, and Redis unreachable whatever the job provides: a pool
    # created here would be created for nothing that answers.
    monkeypatch.setattr(settings, "CACHE_ENABLED", True)
    monkeypatch.setattr(settings, "REDIS_HOST", "127.0.0.1")
    monkeypatch.setattr(settings, "REDIS_PORT", 1)
    monkeypatch.setattr(deps, "_redis_pool", None)
    pool_requests = []
    real_get_redis_pool = deps.get_redis_pool

    async def recording_get_redis_pool():
        pool_requests.append(threading.get_ident())
        return await real_get_redis_pool()

    monkeypatch.setattr(deps, "get_redis_pool", recording_get_redis_pool)
    # Without the redis package the cache never asks for the pool, and the
    # two pool checks below would pass whatever the check did.
    assert deps.REDIS_AVAILABLE

    flag = _flag(db_session)
    _traffic(db_session, flag, errors=20)

    scheduler = SafetyScheduler(interval_minutes=1)
    with (
        patch("backend.app.core.safety_scheduler.SessionLocal", session_factory),
        patch.object(scheduler, "_notification_service", MagicMock()),
    ):
        check = scheduler.check_feature_flags_safety()
        try:
            check.send(None)
        except StopIteration as finished:
            result = finished.value
        else:
            check.close()
            pytest.fail(
                "the safety check suspended: something it awaits waits on an "
                "event loop, and in the worker it would run on a private one"
            )

    assert result["items_failed"] == 0, result
    assert result["metadata"]["rollbacks"] >= 1, result
    assert pool_requests == [], "the safety check asked for the shared Redis pool"
    assert deps._redis_pool is None, "the safety check created the shared Redis pool"

    db_session.expire_all()
    stored = db_session.get(FeatureFlag, flag.id)
    assert (stored.status, stored.rollout_percentage) == (FeatureFlagStatus.INACTIVE, 0)
    assert _rollbacks(db_session, flag) == [(50, 0)]
    assert db_session.query(AuditLog).filter(AuditLog.entity_id == flag.id).count() >= 1


# ---------------------------------------------------------------------------
# The offloaded tick rolls back exactly the breaching flags
# ---------------------------------------------------------------------------


def test_the_offloaded_tick_rolls_back_the_five_breaching_flags_of_twenty(
    db_session, session_factory, automatic_rollbacks
):
    flags = [_flag(db_session) for _ in range(20)]
    breaching = flags[::4]
    for flag in flags:
        _traffic(db_session, flag, errors=20 if flag in breaching else 0)

    scheduler = SafetyScheduler(interval_minutes=1)
    notifications = MagicMock()
    with (
        patch("backend.app.core.safety_scheduler.SessionLocal", session_factory),
        patch.object(scheduler, "_notification_service", notifications),
    ):
        outcome = asyncio.run(
            run_locked_tick(
                SCHEDULER_NAME,
                scheduler._check_off_loop,
                engine=db_session.get_bind(),
                record=False,
            )
        )

    assert outcome.acquired, "another holder had the safety lock"
    assert outcome.status == STATUS_SUCCESS, outcome
    # The database is shared with other tests: at least these twenty.
    assert outcome.result.items_processed >= 20, outcome.result
    assert outcome.result.items_failed == 0, outcome.result
    assert outcome.result.metadata["rollbacks"] >= 5, outcome.result

    db_session.expire_all()
    for flag in flags:
        stored = db_session.get(FeatureFlag, flag.id)
        if flag in breaching:
            assert (stored.status, stored.rollout_percentage) == (
                FeatureFlagStatus.INACTIVE,
                0,
            )
            assert _rollbacks(db_session, flag) == [(50, 0)]
        else:
            assert (stored.status, stored.rollout_percentage) == (
                FeatureFlagStatus.ACTIVE,
                50,
            )
            assert _rollbacks(db_session, flag) == []
    alerted = {
        c.kwargs["feature_flag_name"]
        for c in notifications.notify_safety_rollback.call_args_list
    }
    assert {flag.key for flag in breaching} <= alerted
    assert not {flag.key for flag in flags if flag not in breaching} & alerted
