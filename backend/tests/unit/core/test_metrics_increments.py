"""
The business metrics in ``backend/app/core/metrics.py`` are incremented at
their real call sites (they used to be declared and never touched):

- ``experiment_assignments_total{experiment_id,variant_id}`` —
  ``POST /api/v1/tracking/assign``
- ``events_tracked_total{event_type}`` — ``POST /api/v1/tracking/track``,
  ``/tracking/events`` and ``/tracking/batch``
- ``feature_flag_evaluations_total{flag_key,result}`` —
  ``GET/POST /api/v1/feature-flags/evaluate/{key}``
- ``cache_hits_total`` / ``cache_misses_total{cache_type}`` —
  the rules ``EvaluationCache`` and the Redis-backed feature flag reads
- ``active_experiments_gauge`` — the experiment scheduler tick
- ``scheduler_*`` — ``record_scheduler_tick`` / ``record_scheduler_success``

The endpoint tests go through the real router against the test database
(``client`` fixture: superuser + API-key dependency overridden); nothing
about the metrics themselves is mocked.
"""

from __future__ import annotations

import uuid

import pytest
from prometheus_client import REGISTRY

from backend.app.core import metrics as prom
from backend.app.core.evaluation_cache import CACHE_METRIC_TYPE, EvaluationCache
from backend.app.core.scheduler import ExperimentScheduler
from backend.app.models.experiment import (
    Experiment,
    ExperimentStatus,
    ExperimentType,
    Variant,
)
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus


def sample(name: str, labels: dict | None = None) -> float:
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


@pytest.fixture
def owner(db_session):
    from backend.app.models.user import User, UserRole

    user = User(
        username=f"metrics_{uuid.uuid4().hex[:8]}",
        email=f"metrics_{uuid.uuid4().hex[:8]}@example.com",
        full_name="Metrics Owner",
        hashed_password="$2b$12$EixZaYVK1fsbw1ZfbX3OXePaWxn96p36WQoeG6Lruj3vjPGga31lW",
        is_active=True,
        is_superuser=True,
        role=UserRole.ADMIN,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def live_experiment(db_session, owner):
    suffix = uuid.uuid4().hex[:8]
    experiment = Experiment(
        name=f"Metrics {suffix}",
        key=f"metrics-{suffix}",
        description="metrics increments",
        hypothesis="counters move",
        owner_id=owner.id,
        status=ExperimentStatus.ACTIVE,
        experiment_type=ExperimentType.A_B,
    )
    db_session.add(experiment)
    db_session.commit()
    for name, is_control in (("control", True), ("treatment", False)):
        db_session.add(
            Variant(
                experiment_id=experiment.id,
                name=name,
                description=name,
                is_control=is_control,
                traffic_allocation=50,
                configuration={},
            )
        )
    db_session.commit()
    db_session.refresh(experiment)
    return experiment


@pytest.fixture
def live_flag(db_session, owner):
    suffix = uuid.uuid4().hex[:8]
    flag = FeatureFlag(
        key=f"metrics-flag-{suffix}",
        name=f"Metrics flag {suffix}",
        description="metrics",
        status=FeatureFlagStatus.ACTIVE,
        owner_id=owner.id,
        rollout_percentage=100,
    )
    db_session.add(flag)
    db_session.commit()
    db_session.refresh(flag)
    return flag


# ---------------------------------------------------------------------------
# Tracking endpoints
# ---------------------------------------------------------------------------


class TestTrackingCounters:
    def test_assign_increments_experiment_assignments_total(
        self, client, live_experiment
    ):
        user_id = f"u-{uuid.uuid4().hex[:8]}"
        resp = client.post(
            "/api/v1/tracking/assign",
            json={"experiment_key": live_experiment.key, "user_id": user_id},
        )
        assert resp.status_code == 200, resp.text
        variant_id = resp.json()["variant_id"]

        labels = {"experiment_id": str(live_experiment.id), "variant_id": variant_id}
        before = sample("experiment_assignments_total", labels)

        # A second (sticky) call for the same user still counts a decision.
        again = client.post(
            "/api/v1/tracking/assign",
            json={"experiment_key": live_experiment.key, "user_id": user_id},
        )
        assert again.status_code == 200
        assert sample("experiment_assignments_total", labels) == before + 1

    def test_a_not_running_answer_is_counted_under_its_status(
        self, client, db_session, live_experiment
    ):
        live_experiment.status = ExperimentStatus.PAUSED
        db_session.commit()
        labels = {"experiment_id": str(live_experiment.id), "variant_id": "paused"}
        before = sample("experiment_assignments_total", labels)

        resp = client.post(
            "/api/v1/tracking/assign",
            json={"experiment_key": live_experiment.key, "user_id": "u-paused"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["reason"] == "paused"
        assert sample("experiment_assignments_total", labels) == before + 1

    # Client-supplied event names are bucketed to the bounded label ``custom``
    # (``core.metrics.event_type_label``); only the platform's known types
    # (exposure, conversion, click, page_view, custom) are labelled verbatim.

    def test_track_increments_events_tracked_total(self, client, live_experiment):
        event_type = f"purchase_{uuid.uuid4().hex[:6]}"
        before = sample("events_tracked_total", {"event_type": "custom"})

        resp = client.post(
            "/api/v1/tracking/track",
            json={
                "experiment_key": live_experiment.key,
                "user_id": "u-track",
                "event_type": event_type,
                "value": 9.99,
            },
        )
        assert resp.status_code == 200, resp.text
        assert sample("events_tracked_total", {"event_type": "custom"}) == before + 1
        assert sample("events_tracked_total", {"event_type": event_type}) == 0.0

    def test_events_by_id_increments_events_tracked_total(
        self, client, live_experiment
    ):
        event_type = "click"  # a known type is labelled verbatim
        before = sample("events_tracked_total", {"event_type": event_type})

        resp = client.post(
            "/api/v1/tracking/events",
            json={
                "experiment_id": str(live_experiment.id),
                "user_id": "u-events",
                "event_type": event_type,
                "event_name": event_type,
            },
        )
        assert resp.status_code == 200, resp.text
        assert sample("events_tracked_total", {"event_type": event_type}) == before + 1

    def test_batch_increments_per_successful_event(self, client, live_experiment):
        event_type = f"batch_{uuid.uuid4().hex[:6]}"
        before = sample("events_tracked_total", {"event_type": "custom"})

        resp = client.post(
            "/api/v1/tracking/batch",
            json={
                "events": [
                    {
                        "experiment_key": live_experiment.key,
                        "user_id": "u-b1",
                        "event_type": event_type,
                    },
                    {
                        "experiment_key": live_experiment.key,
                        "user_id": "u-b2",
                        "event_type": event_type,
                    },
                    # unknown key -> failure, must not be counted
                    {
                        "experiment_key": f"missing-{uuid.uuid4().hex}",
                        "user_id": "u-b3",
                        "event_type": event_type,
                    },
                ]
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["success_count"] == 2 and body["failure_count"] == 1
        assert sample("events_tracked_total", {"event_type": "custom"}) == before + 2


# ---------------------------------------------------------------------------
# Feature flag evaluation
# ---------------------------------------------------------------------------


class TestFlagEvaluationCounters:
    def test_evaluate_increments_feature_flag_evaluations_total(
        self, client, live_flag
    ):
        labels = {"flag_key": live_flag.key, "result": "enabled"}
        before = sample("feature_flag_evaluations_total", labels)

        resp = client.get(
            f"/api/v1/feature-flags/evaluate/{live_flag.key}", params={"user_id": "u-1"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["enabled"] is True
        assert sample("feature_flag_evaluations_total", labels) == before + 1

        resp = client.post(
            f"/api/v1/feature-flags/evaluate/{live_flag.key}",
            json={"user_id": "u-2", "context": {}},
        )
        assert resp.status_code == 200, resp.text
        assert sample("feature_flag_evaluations_total", labels) == before + 2


# ---------------------------------------------------------------------------
# Caches
# ---------------------------------------------------------------------------


class TestCacheCounters:
    def test_evaluation_cache_hits_and_misses(self):
        hits = {"cache_type": CACHE_METRIC_TYPE}
        h0 = sample("cache_hits_total", hits)
        m0 = sample("cache_misses_total", hits)

        cache = EvaluationCache(max_size=10, default_ttl=60)
        ctx = {"country": "US"}
        assert cache.get("rule-1", ctx) is None  # miss
        cache.set("rule-1", ctx, True)
        assert cache.get("rule-1", ctx) is True  # hit
        assert cache.get("rule-1", ctx) is True  # hit

        assert sample("cache_misses_total", hits) == m0 + 1
        assert sample("cache_hits_total", hits) == h0 + 2


# ---------------------------------------------------------------------------
# Experiment scheduler gauge and scheduler metrics helpers
# ---------------------------------------------------------------------------


class TestSchedulerMetrics:
    def test_active_experiments_gauge_reflects_database(
        self, db_session, live_experiment
    ):
        ExperimentScheduler._update_active_experiments_gauge(db_session)
        expected = (
            db_session.query(Experiment)
            .filter(Experiment.status == ExperimentStatus.ACTIVE)
            .count()
        )
        assert expected >= 1
        assert sample("active_experiments_gauge") == float(expected)

    @pytest.mark.asyncio
    async def test_scheduler_tick_updates_gauge(
        self, db_session, live_experiment, monkeypatch
    ):
        from sqlalchemy.orm import sessionmaker

        import backend.app.core.scheduler as scheduler_module

        factory = sessionmaker(bind=db_session.get_bind())
        monkeypatch.setattr(scheduler_module, "SessionLocal", factory)

        result = await ExperimentScheduler().process_scheduled_experiments()
        assert set(result) >= {"items_processed", "items_failed"}
        assert sample("active_experiments_gauge") >= 1.0

    def test_record_scheduler_tick_and_success(self):
        name = f"sched-{uuid.uuid4().hex[:6]}"
        prom.record_scheduler_tick(name, "success", 0.25)
        prom.record_scheduler_tick(name, "skipped", 0.0)
        prom.record_scheduler_success(name)

        assert sample("scheduler_ticks_total", {"name": name, "status": "success"}) == 1
        assert sample("scheduler_ticks_total", {"name": name, "status": "skipped"}) == 1
        # skipped ticks are not observed in the duration histogram
        assert sample("scheduler_tick_duration_seconds_count", {"name": name}) == 1
        assert sample("scheduler_last_success_timestamp", {"name": name}) > 0
