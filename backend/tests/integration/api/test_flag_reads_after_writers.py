"""The flag detail and list show every writer's change at once (#630).

The flag routes had an opt-in Redis cache (#100, on with ``CACHE_ENABLED``).
Only the flag routes themselves dropped its entries, so five writers that
change a flag elsewhere left the cached detail and list serving the old
values for up to an hour:

* ``POST /feature-flags/bulk-toggle``;
* ``POST /safety/feature-flags/{id}/rollback``;
* the safety scheduler's automatic rollback;
* ``POST /rollout-schedules/stages/{id}/advance``;
* the rollout scheduler's stage activation.

The cache is gone, so this holds by construction. The test keeps it that way:
with ``CACHE_ENABLED=True`` and a reachable Redis it reads the detail and the
list once (which primed both cache entries while the cache existed), runs one
writer -- the schedulers tick for real against the test database -- and
requires the served detail and list to equal a fresh read from the database.

What would let this pass on the old code, and is avoided:

* ``deps`` binds ``settings`` at import, so the flag is set on the object
  ``deps`` holds, and ``test_the_cache_is_switched_on`` proves it reached
  ``get_cache_control`` with a live Redis -- were the cache silently off, the
  writer tests could not fail;
* ``make_client_for_user`` overrides ``get_cache_control`` to disabled, so
  that override is removed;
* a ``TestClient`` outside a ``with`` block runs requests on different event
  loops and the pooled Redis client breaks quietly, so it is a context manager;
* the list is filtered by the flag's key, so the shared database's other flags
  cannot page it out of the first 100.

The Redis is ``REDIS_HOST``/``REDIS_PORT``, database 15, flushed. Without a
reachable Redis this skips, unless ``EXPERIMENTLY_REQUIRE_REDIS=1`` (the
integration-tests job), where a skip is a failure.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
import redis as sync_redis
from fastapi.encoders import jsonable_encoder
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.crud import crud_feature_flag
from backend.app.main import app
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.metrics.metric import ErrorLog, MetricType, RawMetric
from backend.app.models.rollout_schedule import (
    RolloutSchedule,
    RolloutScheduleStatus,
    RolloutStage,
    RolloutStageStatus,
    TriggerType,
)
from backend.app.models.safety import (
    FeatureFlagSafetyConfig,
    SafetyRollbackRecord,
    SafetySettings,
)
from backend.app.schemas.feature_flag import (
    FeatureFlagListItem,
    FeatureFlagListResponse,
)
from backend.app.services.feature_flag_service import FeatureFlagService
from backend.app.services.stale_flags import stale_reasons, utc_now
from backend.tests.integration.conftest import make_client_for_user

pytestmark = [pytest.mark.integration, pytest.mark.regression]

BASE = "/api/v1/feature-flags"
CACHE_DB = 15
PREFIX = "c630-"
SCHEMA = "test_experimentation"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def redis_server(monkeypatch):
    """A synchronous handle on the Redis the application will use, emptied."""
    # `deps` holds the settings object it imported; set the values on that one.
    monkeypatch.setattr(deps.settings, "REDIS_DB", CACHE_DB)
    monkeypatch.setattr(deps.settings, "REDIS_PASSWORD", None)
    monkeypatch.setattr(deps.settings, "REDIS_SSL", False)
    handle = sync_redis.Redis(
        host=str(deps.settings.REDIS_HOST),
        port=int(deps.settings.REDIS_PORT),
        db=CACHE_DB,
        decode_responses=True,
        socket_connect_timeout=2,
    )
    try:
        handle.ping()
    except sync_redis.RedisError as exc:
        message = (
            f"no Redis at {deps.settings.REDIS_HOST}:{deps.settings.REDIS_PORT}: {exc}"
        )
        if os.environ.get("EXPERIMENTLY_REQUIRE_REDIS") == "1":
            pytest.fail(message + " but EXPERIMENTLY_REQUIRE_REDIS=1")
        pytest.skip(message)
    handle.flushdb()
    yield handle
    handle.flushdb()
    handle.close()


@pytest.fixture
def cached_superuser_client(redis_server, db_session, admin_user, monkeypatch):
    """A superuser's client with the real ``get_cache_control`` and the cache on.

    A superuser, because the safety rollback route requires one.
    """
    monkeypatch.setattr(deps.settings, "CACHE_ENABLED", True)
    monkeypatch.setattr(deps, "_redis_pool", None)  # built on this client's loop
    make_client_for_user(db_session, admin_user)
    app.dependency_overrides.pop(deps.get_cache_control, None)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


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
        schedule_ids = [
            row.id
            for row in db_session.query(RolloutSchedule.id).filter(
                RolloutSchedule.feature_flag_id.in_(flag_ids)
            )
        ]
        if schedule_ids:
            db_session.query(RolloutStage).filter(
                RolloutStage.rollout_schedule_id.in_(schedule_ids)
            ).delete(synchronize_session=False)
            db_session.query(RolloutSchedule).filter(
                RolloutSchedule.id.in_(schedule_ids)
            ).delete(synchronize_session=False)
        for model in (SafetyRollbackRecord, FeatureFlagSafetyConfig):
            db_session.query(model).filter(model.feature_flag_id.in_(flag_ids)).delete(
                synchronize_session=False
            )
        for model in (ErrorLog, RawMetric):
            db_session.query(model).filter(model.feature_flag_id.in_(flag_ids)).delete(
                synchronize_session=False
            )
        db_session.query(FeatureFlag).filter(FeatureFlag.id.in_(flag_ids)).delete(
            synchronize_session=False
        )
    db_session.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hours_ago(hours: float) -> datetime:
    """A naive UTC timestamp, as ``rollout_stages`` stores them."""
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).replace(tzinfo=None)


def _flag(db_session, rollout_percentage: int = 50) -> FeatureFlag:
    row = FeatureFlag(
        key=f"{PREFIX}{uuid.uuid4().hex[:10]}",
        name="c630",
        status=FeatureFlagStatus.ACTIVE,
        rollout_percentage=rollout_percentage,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _read(client, flag):
    detail = client.get(f"{BASE}/{flag.id}")
    assert detail.status_code == 200, detail.text
    listing = client.get(f"{BASE}/", params={"search": flag.key})
    assert listing.status_code == 200, listing.text
    return detail.json(), listing.json()


def _fresh(session_factory, flag):
    """The detail and the list exactly as the routes build them, from a new
    database session."""
    session = session_factory()
    try:
        detail = FeatureFlagService(session).get_feature_flag(flag.id)
        items = crud_feature_flag.get_multi(session, skip=0, limit=100, search=flag.key)
        total = crud_feature_flag.count(session, search=flag.key)
        days = deps.settings.STALE_FLAG_DAYS
        reasons, seen = stale_reasons(session, items, now=utc_now(), days=days)
        listing = FeatureFlagListResponse(
            items=[FeatureFlagListItem.of(item, reasons[item.id]) for item in items],
            total=total,
            skip=0,
            limit=100,
            stale_days=days,
            evaluations_seen=seen,
        )
        return jsonable_encoder(detail), listing.model_dump(mode="json")
    finally:
        session.close()


# ---------------------------------------------------------------------------
# The five writers. Each changes the flag outside the per-flag routes and
# returns the (status, rollout_percentage) it must leave in the database.
# ---------------------------------------------------------------------------


def _bulk_toggle(client, db_session, session_factory, flag):
    resp = client.post(
        f"{BASE}/bulk-toggle", json={"flag_ids": [str(flag.id)], "action": "disable"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["results"][0]["success"] is True, resp.text
    return "inactive", 50


def _safety_rollback_route(client, db_session, session_factory, flag):
    resp = client.post(
        f"/api/v1/safety/feature-flags/{flag.id}/rollback",
        params={"percentage": 5, "reason": "c630"},
    )
    assert resp.status_code == 200, resp.text
    return "active", 5


def _safety_scheduler_tick(client, db_session, session_factory, flag):
    from backend.app.core.safety_scheduler import SafetyScheduler

    db_session.add(
        FeatureFlagSafetyConfig(
            feature_flag_id=flag.id,
            enabled=True,
            metrics={
                "error_rate": {
                    "critical_threshold": 0.05,
                    "comparison_type": "greater_than",
                }
            },
            rollback_percentage=5,
        )
    )
    # 100 evaluations and 20 errors in the window: a 20% error rate.
    for _ in range(100):
        db_session.add(
            RawMetric(
                feature_flag_id=flag.id, metric_type=MetricType.FLAG_EVALUATION.value
            )
        )
    for i in range(20):
        db_session.add(
            ErrorLog(feature_flag_id=flag.id, error_type="crash", message=f"c630 {i}")
        )
    db_session.commit()

    scheduler = SafetyScheduler(interval_minutes=1)
    with (
        patch("backend.app.core.safety_scheduler.SessionLocal", session_factory),
        patch.object(scheduler, "_notification_service", MagicMock()),
    ):
        asyncio.run(scheduler.check_feature_flags_safety())
    return "active", 5


def _schedule(db_session, flag, stages):
    schedule = RolloutSchedule(
        name=f"{PREFIX}{uuid.uuid4().hex[:6]}",
        feature_flag_id=flag.id,
        status=RolloutScheduleStatus.ACTIVE,
        max_percentage=100,
        min_stage_duration=0,
    )
    db_session.add(schedule)
    db_session.commit()
    db_session.refresh(schedule)
    rows = []
    for order, spec in zip(range(1, len(stages) + 1), stages):
        stage = RolloutStage(
            rollout_schedule_id=schedule.id,
            name=f"stage {order}",
            stage_order=order,
            **spec,
        )
        db_session.add(stage)
        rows.append(stage)
    db_session.commit()
    return [row.id for row in rows]


def _stage_advance_route(client, db_session, session_factory, flag):
    (stage_id,) = _schedule(
        db_session,
        flag,
        [
            {
                "target_percentage": 80,
                "trigger_type": TriggerType.MANUAL,
                "status": RolloutStageStatus.PENDING,
            }
        ],
    )
    resp = client.post(f"/api/v1/rollout-schedules/stages/{stage_id}/advance")
    assert resp.status_code == 200, resp.text
    return "active", 80


def _rollout_scheduler_tick(client, db_session, session_factory, flag):
    from backend.app.core.rollout_scheduler import RolloutScheduler

    # Stage 1 (50%) started 30 hours ago and is due to complete; stage 2
    # (80%) is pending with no start date, so the tick starts it.
    _schedule(
        db_session,
        flag,
        [
            {
                "target_percentage": 50,
                "trigger_type": TriggerType.TIME_BASED,
                "trigger_configuration": {"duration": 24},
                "status": RolloutStageStatus.IN_PROGRESS,
                "updated_at": _hours_ago(30),
            },
            {
                "target_percentage": 80,
                "trigger_type": TriggerType.TIME_BASED,
                "trigger_configuration": {"duration": 24},
                "status": RolloutStageStatus.PENDING,
            },
        ],
    )
    scheduler = RolloutScheduler(interval_minutes=1)
    scheduler._notification_service = MagicMock()
    with patch("backend.app.core.rollout_scheduler.SessionLocal", session_factory):
        asyncio.run(scheduler.process_rollout_schedules())
    return "active", 80


WRITERS = {
    "bulk_toggle": _bulk_toggle,
    "safety_rollback_route": _safety_rollback_route,
    "safety_scheduler_tick": _safety_scheduler_tick,
    "stage_advance_route": _stage_advance_route,
    "rollout_scheduler_tick": _rollout_scheduler_tick,
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_the_cache_is_switched_on(cached_superuser_client, redis_server):
    """Guards the rest: with the cache dependency off they could not fail."""
    control = cached_superuser_client.portal.call(deps.get_cache_control)
    assert control.enabled is True
    assert control.redis is not None


@pytest.mark.usefixtures("automatic_rollbacks")
@pytest.mark.parametrize("writer", list(WRITERS), ids=str)
def test_the_detail_and_list_show_the_writers_change(
    cached_superuser_client, redis_server, db_session, session_factory, writer
):
    client = cached_superuser_client
    flag = _flag(db_session)

    before_detail, before_list = _read(client, flag)
    assert before_detail["rollout_percentage"] == 50
    assert [item["id"] for item in before_list["items"]] == [str(flag.id)]

    status, percentage = WRITERS[writer](client, db_session, session_factory, flag)

    db_session.expire_all()
    stored = db_session.get(FeatureFlag, flag.id)
    stored_status = getattr(stored.status, "value", stored.status).lower()
    assert (stored_status, stored.rollout_percentage) == (status, percentage), (
        f"{writer} did not change the flag in the database; the test proves nothing"
    )

    served_detail, served_list = _read(client, flag)
    fresh_detail, fresh_list = _fresh(session_factory, flag)
    stale = []
    if served_detail != fresh_detail:
        stale.append(
            f"the detail served {served_detail['status']} "
            f"{served_detail['rollout_percentage']}%, the database holds "
            f"{fresh_detail['status']} {fresh_detail['rollout_percentage']}%"
        )
    if served_list != fresh_list:
        served = [(i["status"], i["rollout_percentage"]) for i in served_list["items"]]
        fresh = [(i["status"], i["rollout_percentage"]) for i in fresh_list["items"]]
        stale.append(f"the list served {served}, the database holds {fresh}")
    assert not stale, f"after {writer}: " + "; ".join(stale)
