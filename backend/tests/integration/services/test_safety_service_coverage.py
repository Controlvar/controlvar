"""
Integration tests for SafetyService (backend/app/services/safety_service.py).

These tests exercise SafetyService against a real Postgres database (via the
`db_session` / `make_feature_flag` fixtures defined in
backend/tests/integration/conftest.py). See also
backend/tests/unit/services/test_safety_service.py (mock-based unit tests)
and backend/tests/integration/api/test_safety_api.py (HTTP-layer tests).

This file previously documented a large number of product bugs in
SafetyService (method-name shadowing, Pydantic v1 `orm_mode` vs. Pydantic
v2 `.from_orm()`, model/schema column mismatches, an async method called
without `await`, etc). SafetyService has since been rewritten
(backend/app/services/safety_service.py) and the response schemas now use
`ConfigDict(from_attributes=True)` (backend/app/schemas/safety.py), which
fixes all of those issues. The tests below exercise the *current*,
corrected behaviour:

* No more name shadowing: the plain CRUD helpers are `get_safety_settings_
  record(db)` / `get_feature_flag_safety_config_record(db, feature_flag_id)`
  (static), distinct from the async instance methods `get_safety_settings()`
  / `get_feature_flag_safety_config(id)`.
* `create_feature_flag_safety_config(db, data, feature_flag_id=None)` takes
  the flag id explicitly (or reads `data.feature_flag_id` when present) and
  raises `ValueError` (not `AttributeError`) if neither is supplied.
* A rollback never creates a safety config (#1206): `execute_rollback` reads
  the flag's stored config, and its record's `safety_config_id` is that
  config's id, or NULL for a flag on the global defaults. The helpers that
  created one (`get_or_create_safety_config`, `_get_or_add_safety_config`,
  `create_rollback_record`) are gone.
* `check_feature_flag_safety` sources metric values from `get_error_metrics`/
  `get_latency_metrics` for recognised names and marks anything else as
  "unmeasured" (healthy, current_value 0, listed in
  `details["unmeasured_metrics"]`). A metric is unhealthy only when it
  breaches `critical_threshold` (per `comparison_type`); warning-only
  breaches are reported in `MetricStatus.details["warning"]` without
  affecting `is_healthy`.
* `get_latency_metrics` reads real `RawMetric.value` rows (`metric_type ==
  "latency"`), so latency stats are no longer always zero.
* `should_rollback` is now `async` and correctly awaits
  `check_feature_flag_safety`; it requires an ACTIVE flag with
  `rollout_percentage > 0`, a flag-level config with `enabled=True`, and
  global `enable_automatic_rollbacks=True` before it will even evaluate the
  safety check.
* `execute_rollback` uses the real `SafetyRollbackRecord` columns, so a
  successful call persists a record and updates the flag's
  `rollout_percentage` to `target_percentage`; failures (e.g. an unknown
  flag) report `success=False` with no partial writes.
"""

import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from backend.app.models.audit_log import ActionType, AuditLog
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.metrics.metric import ErrorLog, MetricType, RawMetric
from backend.app.models.safety import (
    FeatureFlagSafetyConfig,
    RollbackTriggerType,
    SafetyRollbackRecord,
    SafetySettings,
)
from backend.app.schemas.safety import (
    FeatureFlagSafetyConfigCreate,
    FeatureFlagSafetyConfigUpdate,
    MetricThreshold,
    SafetySettingsCreate,
    SafetySettingsUpdate,
)
from backend.app.services.safety_service import (
    DEFAULT_CONFIG_ID,
    SafetyService,
    _as_threshold,
    _breaches,
    _metric_to_trigger,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _wipe_safety_settings(db_session):
    """Delete all SafetySettings rows (there should only ever be 0 or 1)."""
    db_session.query(SafetySettings).delete()
    db_session.commit()


@pytest.fixture
def safety_settings_guard(db_session):
    """Snapshot the global SafetySettings singleton row (if any) before the
    test runs, and restore that exact state afterwards - regardless of
    whether the test deletes rows, creates rows, or mutates rows. The
    safety_settings table is a process-wide singleton that is *not*
    truncated between tests, so tests that need a known starting state
    (e.g. "no settings row exists yet") must use this fixture rather than
    assuming a clean table.
    """
    original = db_session.query(SafetySettings).first()
    snapshot = None
    if original is not None:
        snapshot = {
            "enable_automatic_rollbacks": original.enable_automatic_rollbacks,
            "default_metrics": original.default_metrics,
        }
    yield
    db_session.rollback()  # in case the test left an aborted transaction
    rows = db_session.query(SafetySettings).all()
    if snapshot is None:
        for row in rows:
            db_session.delete(row)
        db_session.commit()
    else:
        if not rows:
            db_session.add(SafetySettings(**snapshot))
        else:
            first, *extra = rows
            first.enable_automatic_rollbacks = snapshot["enable_automatic_rollbacks"]
            first.default_metrics = snapshot["default_metrics"]
            db_session.add(first)
            for row in extra:
                db_session.delete(row)
        db_session.commit()


def _add_error_log(db_session, flag, error_type="boom", timestamp=None):
    db_session.add(
        ErrorLog(
            feature_flag_id=flag.id,
            error_type=error_type,
            message="synthetic test error",
            **({"timestamp": timestamp} if timestamp is not None else {}),
        )
    )


def _add_evaluations(db_session, flag, n=1, timestamp=None):
    for _ in range(n):
        db_session.add(
            RawMetric(
                feature_flag_id=flag.id,
                metric_type=MetricType.FLAG_EVALUATION.value,
                **({"timestamp": timestamp} if timestamp is not None else {}),
            )
        )


def _add_latency_sample(db_session, flag, value, timestamp=None):
    db_session.add(
        RawMetric(
            feature_flag_id=flag.id,
            metric_type=MetricType.LATENCY.value,
            value=value,
            **({"timestamp": timestamp} if timestamp is not None else {}),
        )
    )


# ---------------------------------------------------------------------------
# Static: SafetySettings CRUD (get_safety_settings_record / create / update)
# ---------------------------------------------------------------------------


class TestSafetySettingsStatic:
    def test_get_record_returns_none_when_table_empty(
        self, db_session, safety_settings_guard
    ):
        _wipe_safety_settings(db_session)
        assert SafetyService.get_safety_settings_record(db_session) is None

    def test_get_record_returns_none_is_not_a_coroutine(self):
        """Sanity check that the shadowing bug is gone: this is a plain
        staticmethod, distinct from the async instance get_safety_settings."""
        import inspect

        assert not inspect.iscoroutinefunction(SafetyService.get_safety_settings_record)
        assert inspect.iscoroutinefunction(SafetyService.get_safety_settings)

    def test_create_succeeds_when_none_exists(self, db_session, safety_settings_guard):
        _wipe_safety_settings(db_session)
        data = SafetySettingsCreate(
            enable_automatic_rollbacks=True,
            default_metrics={
                "error_rate": MetricThreshold(
                    warning_threshold=0.05, critical_threshold=0.1
                )
            },
        )
        created = SafetyService.create_safety_settings(db_session, data)

        assert created.id is not None
        assert created.enable_automatic_rollbacks is True
        # Nested MetricThreshold objects are serialised to plain dicts for JSONB.
        assert created.default_metrics["error_rate"]["critical_threshold"] == 0.1

        fetched = SafetyService.get_safety_settings_record(db_session)
        assert fetched.id == created.id

    def test_create_raises_value_error_when_already_exists(
        self, db_session, safety_settings_guard
    ):
        _wipe_safety_settings(db_session)
        db_session.add(
            SafetySettings(enable_automatic_rollbacks=False, default_metrics=None)
        )
        db_session.commit()

        with pytest.raises(ValueError, match="already exist"):
            SafetyService.create_safety_settings(
                db_session, SafetySettingsCreate(enable_automatic_rollbacks=True)
            )

    def test_update_modifies_existing_row(self, db_session, safety_settings_guard):
        _wipe_safety_settings(db_session)
        db_session.add(
            SafetySettings(
                enable_automatic_rollbacks=False,
                default_metrics={"latency": {"warning_threshold": 100}},
            )
        )
        db_session.commit()

        updated = SafetyService.update_safety_settings(
            db_session, SafetySettingsUpdate(enable_automatic_rollbacks=True)
        )

        assert updated.enable_automatic_rollbacks is True
        # default_metrics was not part of the update payload (exclude_unset),
        # so it must be untouched.
        assert updated.default_metrics == {"latency": {"warning_threshold": 100}}

    def test_update_returns_none_when_table_empty(
        self, db_session, safety_settings_guard
    ):
        _wipe_safety_settings(db_session)
        result = SafetyService.update_safety_settings(
            db_session, SafetySettingsUpdate(enable_automatic_rollbacks=True)
        )
        assert result is None


# ---------------------------------------------------------------------------
# Static: FeatureFlagSafetyConfig CRUD
# ---------------------------------------------------------------------------


class TestFeatureFlagSafetyConfigStatic:
    def test_get_record_none_when_missing(self, db_session, make_feature_flag):
        flag = make_feature_flag()
        assert (
            SafetyService.get_feature_flag_safety_config_record(db_session, flag.id)
            is None
        )

    def test_create_with_explicit_feature_flag_id(self, db_session, make_feature_flag):
        flag = make_feature_flag()
        data = FeatureFlagSafetyConfigCreate(
            enabled=True,
            metrics={"error_rate": MetricThreshold(critical_threshold=0.2)},
            rollback_percentage=15,
        )

        created = SafetyService.create_feature_flag_safety_config(
            db_session, data, feature_flag_id=flag.id
        )

        assert created.feature_flag_id == flag.id
        assert created.rollback_percentage == 15
        assert created.metrics["error_rate"]["critical_threshold"] == 0.2

    def test_create_raises_value_error_when_no_flag_id_available(self, db_session):
        """Neither the explicit feature_flag_id kwarg nor data.feature_flag_id
        is supplied (FeatureFlagSafetyConfigCreate has no such field)."""
        data = FeatureFlagSafetyConfigCreate(
            enabled=True, metrics={}, rollback_percentage=0
        )
        with pytest.raises(ValueError, match="feature_flag_id is required"):
            SafetyService.create_feature_flag_safety_config(db_session, data)

    def test_create_raises_value_error_when_flag_missing(self, db_session):
        data = FeatureFlagSafetyConfigCreate(
            enabled=True, metrics={}, rollback_percentage=0
        )
        with pytest.raises(ValueError, match="does not exist"):
            SafetyService.create_feature_flag_safety_config(
                db_session, data, feature_flag_id=uuid.uuid4()
            )

    def test_create_raises_value_error_when_already_exists(
        self, db_session, make_feature_flag
    ):
        flag = make_feature_flag()
        data = FeatureFlagSafetyConfigCreate(
            enabled=True, metrics={}, rollback_percentage=0
        )
        SafetyService.create_feature_flag_safety_config(
            db_session, data, feature_flag_id=flag.id
        )

        with pytest.raises(ValueError, match="already exists"):
            SafetyService.create_feature_flag_safety_config(
                db_session, data, feature_flag_id=flag.id
            )

    def test_update_modifies_existing_config(self, db_session, make_feature_flag):
        flag = make_feature_flag()
        config = SafetyService.create_feature_flag_safety_config(
            db_session,
            FeatureFlagSafetyConfigCreate(enabled=True, metrics={}),
            feature_flag_id=flag.id,
        )
        assert config.enabled is True
        assert config.rollback_percentage == 0

        updated = SafetyService.update_feature_flag_safety_config(
            db_session,
            flag.id,
            FeatureFlagSafetyConfigUpdate(enabled=False, rollback_percentage=42),
        )

        assert updated.enabled is False
        assert updated.rollback_percentage == 42
        # metrics was not part of the update payload; unchanged.
        assert updated.metrics == {}

    def test_update_returns_none_when_no_config_exists(
        self, db_session, make_feature_flag
    ):
        flag = make_feature_flag()
        result = SafetyService.update_feature_flag_safety_config(
            db_session, flag.id, FeatureFlagSafetyConfigUpdate(enabled=False)
        )
        assert result is None


# ---------------------------------------------------------------------------
# Instance (sync): get_error_metrics / get_latency_metrics / _get_metric_value
# ---------------------------------------------------------------------------


class TestGetErrorMetrics:
    def test_raises_for_missing_flag(self, db_session):
        service = SafetyService(db_session)
        with pytest.raises(ValueError, match="does not exist"):
            service.get_error_metrics(db_session, uuid.uuid4())

    def test_returns_zero_metrics_when_no_data(self, db_session, make_feature_flag):
        flag = make_feature_flag()
        service = SafetyService(db_session)

        result = service.get_error_metrics(db_session, flag.id)

        assert result["error_count"] == 0
        assert result["total_evaluations"] == 0
        assert result["error_rate"] == 0
        assert result["error_types"] == {}
        assert result["timeframe_minutes"] == 15

    def test_computes_rate_and_error_type_breakdown(
        self, db_session, make_feature_flag
    ):
        flag = make_feature_flag()
        # 3 errors: 2 "rate_limit", 1 "timeout".
        _add_error_log(db_session, flag, error_type="rate_limit")
        _add_error_log(db_session, flag, error_type="rate_limit")
        _add_error_log(db_session, flag, error_type="timeout")
        # 10 total evaluations (must be metric_type == "flag_evaluation").
        _add_evaluations(db_session, flag, n=10)
        db_session.commit()

        service = SafetyService(db_session)
        result = service.get_error_metrics(db_session, flag.id)

        assert result["error_count"] == 3
        assert result["total_evaluations"] == 10
        assert result["error_rate"] == pytest.approx(0.3)
        assert result["error_types"] == {"rate_limit": 2, "timeout": 1}

    def test_non_evaluation_raw_metrics_are_not_counted(
        self, db_session, make_feature_flag
    ):
        """total_evaluations only sums RawMetric rows with metric_type ==
        'flag_evaluation'; other metric types (e.g. latency samples) must
        not inflate the denominator."""
        flag = make_feature_flag()
        _add_latency_sample(db_session, flag, 42.0)
        db_session.commit()

        service = SafetyService(db_session)
        result = service.get_error_metrics(db_session, flag.id)

        assert result["total_evaluations"] == 0

    def test_excludes_data_outside_timeframe_window(
        self, db_session, make_feature_flag
    ):
        flag = make_feature_flag()
        old_time = datetime.utcnow() - timedelta(hours=2)
        _add_error_log(db_session, flag, error_type="old_error", timestamp=old_time)
        _add_evaluations(db_session, flag, n=1, timestamp=old_time)
        db_session.commit()

        service = SafetyService(db_session)
        result = service.get_error_metrics(db_session, flag.id, timeframe_minutes=15)

        assert result["error_count"] == 0
        assert result["total_evaluations"] == 0
        assert result["error_types"] == {}


@pytest.fixture
def error_log_rows_unloadable(monkeypatch):
    """Make any query that loads whole ``ErrorLog`` rows raise.

    Aggregate queries over ``ErrorLog`` columns are unaffected.
    """
    from sqlalchemy.orm import Query

    def loads_error_logs(query) -> bool:
        return any(d.get("type") is ErrorLog for d in query.column_descriptions)

    original_all = Query.all
    original_iter = Query.__iter__

    def guarded_all(self):
        if loads_error_logs(self):
            raise AssertionError("ErrorLog rows were loaded with .all()")
        return original_all(self)

    def guarded_iter(self):
        if loads_error_logs(self):
            raise AssertionError("ErrorLog rows were loaded by iteration")
        return original_iter(self)

    monkeypatch.setattr(Query, "all", guarded_all)
    monkeypatch.setattr(Query, "__iter__", guarded_iter)


@pytest.mark.regression
class TestGetErrorMetricsAggregates:
    """``get_error_metrics`` counts and groups errors in the database."""

    def test_counts_without_loading_error_rows(
        self, db_session, make_feature_flag, error_log_rows_unloadable
    ):
        """The count and the breakdown are right with row loading disabled."""
        flag = make_feature_flag()
        for error_type, n in (("crash", 5), ("timeout", 2), ("api_error", 1)):
            for _ in range(n):
                _add_error_log(db_session, flag, error_type=error_type)
        _add_error_log(
            db_session,
            flag,
            error_type="old",
            timestamp=datetime.utcnow() - timedelta(hours=2),
        )
        _add_evaluations(db_session, flag, n=16)
        db_session.commit()

        # The guard is live: loading the rows directly raises.
        with pytest.raises(AssertionError, match="ErrorLog rows were loaded"):
            db_session.query(ErrorLog).filter(ErrorLog.feature_flag_id == flag.id).all()
        with pytest.raises(AssertionError, match="ErrorLog rows were loaded"):
            list(db_session.query(ErrorLog).filter(ErrorLog.feature_flag_id == flag.id))

        result = SafetyService(db_session).get_error_metrics(db_session, flag.id)

        assert result["error_count"] == 8
        assert result["total_evaluations"] == 16
        assert result["error_rate"] == pytest.approx(0.5)
        assert result["error_types"] == {"crash": 5, "timeout": 2, "api_error": 1}
        assert result["uncounted_error_count"] == 0
        assert set(result) == {
            "error_count",
            "uncounted_error_count",
            "total_evaluations",
            "error_rate",
            "error_types",
            "timeframe_minutes",
            "start_time",
            "end_time",
        }

    def test_breakdown_lists_the_most_frequent_types_and_counts_all(
        self, db_session, make_feature_flag, monkeypatch, error_log_rows_unloadable
    ):
        """Past the breakdown limit, the most frequent types are listed and every
        error is still counted."""
        from backend.app.services import safety_service

        monkeypatch.setattr(safety_service, "ERROR_TYPE_BREAKDOWN_LIMIT", 2)
        flag = make_feature_flag()
        for error_type, n in (("a", 3), ("b", 2), ("c", 1), ("d", 1)):
            for _ in range(n):
                _add_error_log(db_session, flag, error_type=error_type)
        db_session.commit()

        result = SafetyService(db_session).get_error_metrics(db_session, flag.id)

        assert result["error_count"] == 7
        assert result["error_types"] == {"a": 3, "b": 2}


class TestGetLatencyMetrics:
    def test_raises_for_missing_flag(self, db_session):
        service = SafetyService(db_session)
        with pytest.raises(ValueError, match="does not exist"):
            service.get_latency_metrics(db_session, uuid.uuid4())

    def test_returns_zero_structure_when_no_data(self, db_session, make_feature_flag):
        flag = make_feature_flag()
        service = SafetyService(db_session)

        result = service.get_latency_metrics(db_session, flag.id)

        assert result["avg_latency"] == 0
        assert result["max_latency"] == 0
        assert result["min_latency"] == 0
        assert result["p95_latency"] == 0
        assert result["total_requests"] == 0

    def test_computes_real_latency_statistics(self, db_session, make_feature_flag):
        flag = make_feature_flag()
        for value in (10.0, 20.0, 30.0, 40.0, 50.0):
            _add_latency_sample(db_session, flag, value)
        db_session.commit()

        service = SafetyService(db_session)
        result = service.get_latency_metrics(db_session, flag.id)

        assert result["total_requests"] == 5
        assert result["avg_latency"] == pytest.approx(30.0)
        assert result["max_latency"] == pytest.approx(50.0)
        assert result["min_latency"] == pytest.approx(10.0)
        assert result["p95_latency"] == pytest.approx(50.0)

    def test_value_type_filter_excludes_non_latency_metrics(
        self, db_session, make_feature_flag
    ):
        """Only RawMetric rows with metric_type == 'latency' are counted."""
        flag = make_feature_flag()
        _add_evaluations(db_session, flag, n=3)
        db_session.commit()

        service = SafetyService(db_session)
        result = service.get_latency_metrics(db_session, flag.id)

        assert result["total_requests"] == 0

    def test_excludes_data_outside_timeframe_window(
        self, db_session, make_feature_flag
    ):
        flag = make_feature_flag()
        old_time = datetime.utcnow() - timedelta(hours=2)
        _add_latency_sample(db_session, flag, 999.0, timestamp=old_time)
        db_session.commit()

        service = SafetyService(db_session)
        result = service.get_latency_metrics(db_session, flag.id, timeframe_minutes=15)

        assert result["total_requests"] == 0
        assert result["avg_latency"] == 0


class TestGetMetricValue:
    def test_error_metric_name_uses_error_metrics(self, db_session, make_feature_flag):
        flag = make_feature_flag()
        _add_error_log(db_session, flag)
        _add_evaluations(db_session, flag, n=4)
        db_session.commit()

        service = SafetyService(db_session)
        value = service._get_metric_value(flag.id, "error_rate")

        assert value == pytest.approx(0.25)

    def test_latency_metric_name_maps_to_avg_latency(
        self, db_session, make_feature_flag
    ):
        flag = make_feature_flag()
        _add_latency_sample(db_session, flag, 100.0)
        _add_latency_sample(db_session, flag, 200.0)
        db_session.commit()

        service = SafetyService(db_session)
        value = service._get_metric_value(flag.id, "latency")

        assert value == pytest.approx(150.0)

    def test_unrecognized_metric_name_returns_none(self, db_session, make_feature_flag):
        flag = make_feature_flag()
        service = SafetyService(db_session)
        assert service._get_metric_value(flag.id, "custom_business_metric") is None


class TestPureHelperFunctions:
    """Direct coverage of the small module-level helper functions."""

    def test_metric_to_trigger_error_metrics(self):
        assert _metric_to_trigger("error_rate") == RollbackTriggerType.ERROR_RATE
        assert _metric_to_trigger("error_count") == RollbackTriggerType.ERROR_RATE

    def test_metric_to_trigger_latency_metrics(self):
        assert _metric_to_trigger("latency") == RollbackTriggerType.LATENCY
        assert _metric_to_trigger("p95_latency") == RollbackTriggerType.LATENCY

    def test_metric_to_trigger_custom_metric(self):
        assert _metric_to_trigger("queue_depth") == RollbackTriggerType.CUSTOM_METRIC

    def test_breaches_greater_than_default(self):
        assert _breaches(10, 5, "greater_than") is True
        assert _breaches(5, 10, "greater_than") is False
        assert _breaches(10, None, "greater_than") is False

    def test_breaches_less_than(self):
        assert _breaches(1, 5, "less_than") is True
        assert _breaches(10, 5, "less_than") is False

    def test_breaches_equal_to(self):
        assert _breaches(5, 5, "equal_to") is True
        assert _breaches(4, 5, "equal_to") is False

    def test_as_threshold_accepts_dict_and_model_and_default(self):
        from_dict = _as_threshold({"critical_threshold": 1.0})
        assert isinstance(from_dict, MetricThreshold)
        assert from_dict.critical_threshold == 1.0

        model = MetricThreshold(critical_threshold=2.0)
        assert _as_threshold(model) is model

        default = _as_threshold("not-a-dict-or-model")
        assert isinstance(default, MetricThreshold)
        assert default.critical_threshold is None


# ---------------------------------------------------------------------------
# Async global settings
# ---------------------------------------------------------------------------


class TestAsyncSafetySettings:
    @pytest.mark.asyncio
    async def test_get_creates_default_row_when_missing(
        self, db_session, safety_settings_guard
    ):
        _wipe_safety_settings(db_session)
        service = SafetyService(db_session)

        result = await service.get_safety_settings()

        assert result.enable_automatic_rollbacks is False
        assert result.default_metrics is None
        persisted = db_session.query(SafetySettings).first()
        assert persisted is not None
        assert persisted.id == result.id

    @pytest.mark.asyncio
    async def test_get_returns_existing_row(self, db_session, safety_settings_guard):
        _wipe_safety_settings(db_session)
        db_session.add(
            SafetySettings(enable_automatic_rollbacks=True, default_metrics=None)
        )
        db_session.commit()

        service = SafetyService(db_session)
        result = await service.get_safety_settings()

        assert result.enable_automatic_rollbacks is True

    @pytest.mark.asyncio
    async def test_async_get_safety_settings_is_the_same_function(self):
        assert (
            SafetyService.async_get_safety_settings is SafetyService.get_safety_settings
        )

    @pytest.mark.asyncio
    async def test_create_or_update_creates_row_when_missing(
        self, db_session, safety_settings_guard
    ):
        _wipe_safety_settings(db_session)
        service = SafetyService(db_session)
        data = SafetySettingsCreate(
            enable_automatic_rollbacks=True,
            default_metrics={
                "error_rate": MetricThreshold(
                    warning_threshold=0.1, critical_threshold=0.2
                )
            },
        )

        result = await service.create_or_update_safety_settings(data)

        assert result.enable_automatic_rollbacks is True
        assert result.default_metrics["error_rate"].critical_threshold == 0.2
        persisted = db_session.query(SafetySettings).first()
        assert persisted is not None
        assert persisted.enable_automatic_rollbacks is True

    @pytest.mark.asyncio
    async def test_create_or_update_updates_existing_row(
        self, db_session, safety_settings_guard
    ):
        _wipe_safety_settings(db_session)
        db_session.add(
            SafetySettings(enable_automatic_rollbacks=False, default_metrics=None)
        )
        db_session.commit()

        service = SafetyService(db_session)
        data = SafetySettingsCreate(
            enable_automatic_rollbacks=True, default_metrics=None
        )

        result = await service.create_or_update_safety_settings(data)

        assert result.enable_automatic_rollbacks is True
        assert db_session.query(SafetySettings).count() == 1, (
            "update must not create a second row"
        )


# ---------------------------------------------------------------------------
# Async feature flag safety config
# ---------------------------------------------------------------------------


class TestAsyncFeatureFlagSafetyConfig:
    @pytest.mark.asyncio
    async def test_get_missing_flag_raises_404(self, db_session):
        service = SafetyService(db_session)
        with pytest.raises(HTTPException) as exc_info:
            await service.get_feature_flag_safety_config(uuid.uuid4())
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_get_returns_existing_config(self, db_session, make_feature_flag):
        flag = make_feature_flag()
        db_session.add(
            FeatureFlagSafetyConfig(
                feature_flag_id=flag.id,
                enabled=False,
                metrics={"error_rate": {"critical_threshold": 0.5}},
                rollback_percentage=33,
            )
        )
        db_session.commit()

        service = SafetyService(db_session)
        result = await service.get_feature_flag_safety_config(flag.id)

        assert result.id != DEFAULT_CONFIG_ID
        assert result.enabled is False
        assert result.rollback_percentage == 33
        assert result.metrics["error_rate"].critical_threshold == 0.5

    @pytest.mark.asyncio
    async def test_get_missing_config_returns_defaults_from_settings(
        self, db_session, make_feature_flag, safety_settings_guard
    ):
        """With no stored config the service returns a default built from the
        global settings, marked with DEFAULT_CONFIG_ID, and writes nothing."""
        flag = make_feature_flag()
        _wipe_safety_settings(db_session)
        db_session.add(
            SafetySettings(
                enable_automatic_rollbacks=False,
                default_metrics={"error_rate": {"critical_threshold": 0.9}},
            )
        )
        db_session.commit()

        service = SafetyService(db_session)
        response = await service.get_feature_flag_safety_config(flag.id)

        assert response.id == DEFAULT_CONFIG_ID
        assert str(response.id) == "00000000-0000-0000-0000-000000000000"
        assert response.feature_flag_id == flag.id
        assert response.enabled is True
        assert response.rollback_percentage == 0
        assert response.metrics["error_rate"].critical_threshold == 0.9
        # Nothing was persisted for this flag.
        assert (
            SafetyService.get_feature_flag_safety_config_record(db_session, flag.id)
            is None
        )

    @pytest.mark.asyncio
    async def test_get_missing_config_creates_default_settings_row(
        self, db_session, make_feature_flag, safety_settings_guard
    ):
        flag = make_feature_flag()
        _wipe_safety_settings(db_session)

        service = SafetyService(db_session)
        response = await service.get_feature_flag_safety_config(flag.id)

        # get_safety_settings() created the default settings row on first use,
        # and with no default_metrics the default config has no metrics.
        assert db_session.query(SafetySettings).first() is not None
        assert response.metrics == {}

    @pytest.mark.asyncio
    async def test_async_alias_is_the_same_function(self):
        assert (
            SafetyService.async_get_feature_flag_safety_config
            is SafetyService.get_feature_flag_safety_config
        )

    @pytest.mark.asyncio
    async def test_create_or_update_missing_flag_raises_404(self, db_session):
        service = SafetyService(db_session)
        data = FeatureFlagSafetyConfigCreate(
            enabled=True, metrics={}, rollback_percentage=10
        )
        with pytest.raises(HTTPException) as exc_info:
            await service.create_or_update_feature_flag_safety_config(
                uuid.uuid4(), data
            )
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_create_or_update_creates_new_row(
        self, db_session, make_feature_flag
    ):
        flag = make_feature_flag()
        service = SafetyService(db_session)
        data = FeatureFlagSafetyConfigCreate(
            enabled=True,
            metrics={"latency": MetricThreshold(warning_threshold=100)},
            rollback_percentage=5,
        )

        result = await service.create_or_update_feature_flag_safety_config(
            flag.id, data
        )

        assert result.rollback_percentage == 5
        assert result.metrics["latency"].warning_threshold == 100
        persisted = SafetyService.get_feature_flag_safety_config_record(
            db_session, flag.id
        )
        assert persisted is not None
        assert persisted.rollback_percentage == 5

    @pytest.mark.asyncio
    async def test_create_or_update_updates_existing_row(
        self, db_session, make_feature_flag
    ):
        flag = make_feature_flag()
        config = FeatureFlagSafetyConfig(
            feature_flag_id=flag.id, enabled=True, metrics={}, rollback_percentage=0
        )
        db_session.add(config)
        db_session.commit()

        service = SafetyService(db_session)
        data = FeatureFlagSafetyConfigCreate(
            enabled=False, metrics={}, rollback_percentage=99
        )

        result = await service.create_or_update_feature_flag_safety_config(
            flag.id, data
        )

        assert result.enabled is False
        assert result.rollback_percentage == 99
        db_session.refresh(config)
        assert config.rollback_percentage == 99
        assert config.enabled is False
        assert (
            db_session.query(FeatureFlagSafetyConfig)
            .filter(FeatureFlagSafetyConfig.feature_flag_id == flag.id)
            .count()
            == 1
        )


# ---------------------------------------------------------------------------
# check_feature_flag_safety
# ---------------------------------------------------------------------------


class TestCheckFeatureFlagSafety:
    @pytest.mark.asyncio
    async def test_missing_flag_raises_404(self, db_session):
        service = SafetyService(db_session)
        with pytest.raises(HTTPException) as exc_info:
            await service.check_feature_flag_safety(uuid.uuid4())
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_disabled_config_is_healthy_with_no_metrics(
        self, db_session, make_feature_flag
    ):
        flag = make_feature_flag()
        db_session.add(
            FeatureFlagSafetyConfig(
                feature_flag_id=flag.id,
                enabled=False,
                metrics={},
                rollback_percentage=0,
            )
        )
        db_session.commit()

        service = SafetyService(db_session)
        result = await service.check_feature_flag_safety(flag.id)

        assert result.is_healthy is True
        assert result.metrics == []
        assert "disabled" in result.details["message"]

    @pytest.mark.asyncio
    async def test_enabled_config_with_no_metrics_is_healthy(
        self, db_session, make_feature_flag
    ):
        flag = make_feature_flag()
        db_session.add(
            FeatureFlagSafetyConfig(
                feature_flag_id=flag.id, enabled=True, metrics={}, rollback_percentage=0
            )
        )
        db_session.commit()

        service = SafetyService(db_session)
        result = await service.check_feature_flag_safety(flag.id)

        assert result.is_healthy is True
        assert result.metrics == []
        assert result.details["feature_flag_key"] == flag.key

    @pytest.mark.asyncio
    async def test_error_rate_metric_breaches_critical_threshold(
        self, db_session, make_feature_flag
    ):
        flag = make_feature_flag()
        _add_error_log(db_session, flag)
        _add_error_log(db_session, flag)
        _add_evaluations(db_session, flag, n=4)  # error_rate == 0.5
        db_session.add(
            FeatureFlagSafetyConfig(
                feature_flag_id=flag.id,
                enabled=True,
                metrics={"error_rate": {"critical_threshold": 0.1}},
                rollback_percentage=0,
            )
        )
        db_session.commit()

        service = SafetyService(db_session)
        result = await service.check_feature_flag_safety(flag.id)

        assert result.is_healthy is False
        assert len(result.metrics) == 1
        metric = result.metrics[0]
        assert metric.name == "error_rate"
        assert metric.current_value == pytest.approx(0.5)
        assert metric.threshold == pytest.approx(0.1)
        assert metric.is_healthy is False

    @pytest.mark.asyncio
    async def test_metric_within_threshold_is_healthy(
        self, db_session, make_feature_flag
    ):
        flag = make_feature_flag()
        _add_evaluations(db_session, flag, n=10)  # no errors -> error_rate == 0
        db_session.add(
            FeatureFlagSafetyConfig(
                feature_flag_id=flag.id,
                enabled=True,
                metrics={"error_rate": {"critical_threshold": 0.1}},
                rollback_percentage=0,
            )
        )
        db_session.commit()

        service = SafetyService(db_session)
        result = await service.check_feature_flag_safety(flag.id)

        assert result.is_healthy is True
        assert result.metrics[0].is_healthy is True

    @pytest.mark.asyncio
    async def test_warning_only_breach_is_reported_but_still_healthy(
        self, db_session, make_feature_flag
    ):
        flag = make_feature_flag()
        _add_latency_sample(db_session, flag, 150.0)
        db_session.add(
            FeatureFlagSafetyConfig(
                feature_flag_id=flag.id,
                enabled=True,
                metrics={
                    "avg_latency": {
                        "warning_threshold": 100.0,
                        "critical_threshold": 500.0,
                    }
                },
                rollback_percentage=0,
            )
        )
        db_session.commit()

        service = SafetyService(db_session)
        result = await service.check_feature_flag_safety(flag.id)

        assert result.is_healthy is True
        metric = result.metrics[0]
        assert metric.is_healthy is True
        assert metric.details["warning"] is True

    @pytest.mark.asyncio
    async def test_less_than_comparison_type(self, db_session, make_feature_flag):
        flag = make_feature_flag()
        _add_latency_sample(db_session, flag, 5.0)
        db_session.add(
            FeatureFlagSafetyConfig(
                feature_flag_id=flag.id,
                enabled=True,
                metrics={
                    "avg_latency": {
                        "critical_threshold": 10.0,
                        "comparison_type": "less_than",
                    }
                },
                rollback_percentage=0,
            )
        )
        db_session.commit()

        service = SafetyService(db_session)
        result = await service.check_feature_flag_safety(flag.id)

        # 5.0 < 10.0 -> breaches "less_than" -> unhealthy.
        assert result.is_healthy is False

    @pytest.mark.asyncio
    async def test_equal_to_comparison_type(self, db_session, make_feature_flag):
        flag = make_feature_flag()
        _add_latency_sample(db_session, flag, 10.0)
        db_session.add(
            FeatureFlagSafetyConfig(
                feature_flag_id=flag.id,
                enabled=True,
                metrics={
                    "avg_latency": {
                        "critical_threshold": 10.0,
                        "comparison_type": "equal_to",
                    }
                },
                rollback_percentage=0,
            )
        )
        db_session.commit()

        service = SafetyService(db_session)
        result = await service.check_feature_flag_safety(flag.id)

        assert result.is_healthy is False

    @pytest.mark.asyncio
    async def test_unrecognized_metric_name_is_reported_unmeasured(
        self, db_session, make_feature_flag
    ):
        flag = make_feature_flag()
        db_session.add(
            FeatureFlagSafetyConfig(
                feature_flag_id=flag.id,
                enabled=True,
                metrics={"queue_depth": {"critical_threshold": 5}},
                rollback_percentage=0,
            )
        )
        db_session.commit()

        service = SafetyService(db_session)
        result = await service.check_feature_flag_safety(flag.id)

        assert result.is_healthy is True
        metric = result.metrics[0]
        assert metric.current_value == 0.0
        assert metric.is_healthy is True
        assert result.details["unmeasured_metrics"] == ["queue_depth"]

    @pytest.mark.asyncio
    async def test_threshold_falls_back_to_warning_then_zero(
        self, db_session, make_feature_flag
    ):
        flag = make_feature_flag()
        db_session.add(
            FeatureFlagSafetyConfig(
                feature_flag_id=flag.id,
                enabled=True,
                metrics={
                    "avg_latency": {"warning_threshold": 42.0},
                    "error_rate": {},
                },
                rollback_percentage=0,
            )
        )
        db_session.commit()

        service = SafetyService(db_session)
        result = await service.check_feature_flag_safety(flag.id)

        by_name = {m.name: m for m in result.metrics}
        assert by_name["avg_latency"].threshold == pytest.approx(42.0)
        assert by_name["error_rate"].threshold == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# should_rollback (now async)
# ---------------------------------------------------------------------------


class TestShouldRollback:
    @pytest.mark.asyncio
    async def test_missing_flag_returns_false(self, db_session):
        service = SafetyService(db_session)
        assert await service.should_rollback(db_session, uuid.uuid4()) == (
            False,
            None,
            None,
        )

    @pytest.mark.asyncio
    async def test_inactive_flag_returns_false(self, db_session, make_feature_flag):
        flag = make_feature_flag(
            status=FeatureFlagStatus.INACTIVE, rollout_percentage=50
        )
        service = SafetyService(db_session)
        assert await service.should_rollback(db_session, flag.id) == (False, None, None)

    @pytest.mark.asyncio
    @pytest.mark.regression
    async def test_zero_rollout_flag_recommends_a_rollback_to_zero(
        self, db_session, make_feature_flag, safety_settings_guard
    ):
        """Flipped for #629: a flag at 0% may still serve the users its targeting
        rules match, and a rollback to 0 turns it off, so it is a candidate."""
        flag = make_feature_flag(status=FeatureFlagStatus.ACTIVE, rollout_percentage=0)
        _add_error_log(db_session, flag)
        _add_evaluations(db_session, flag, n=2)  # error_rate == 0.5
        db_session.add(
            FeatureFlagSafetyConfig(
                feature_flag_id=flag.id,
                enabled=True,
                metrics={"error_rate": {"critical_threshold": 0.1}},
                rollback_percentage=0,
            )
        )
        _wipe_safety_settings(db_session)
        db_session.add(
            SafetySettings(enable_automatic_rollbacks=True, default_metrics=None)
        )
        db_session.commit()

        service = SafetyService(db_session)
        should, _, _ = await service.should_rollback(db_session, flag.id)
        assert should is True

    @pytest.mark.asyncio
    async def test_zero_rollout_with_a_partial_target_returns_false(
        self, db_session, make_feature_flag, safety_settings_guard
    ):
        """A rollback to 5% cannot lower a flag already at 0%."""
        flag = make_feature_flag(status=FeatureFlagStatus.ACTIVE, rollout_percentage=0)
        _add_error_log(db_session, flag)
        _add_evaluations(db_session, flag, n=2)  # error_rate == 0.5
        db_session.add(
            FeatureFlagSafetyConfig(
                feature_flag_id=flag.id,
                enabled=True,
                metrics={"error_rate": {"critical_threshold": 0.1}},
                rollback_percentage=5,
            )
        )
        _wipe_safety_settings(db_session)
        db_session.add(
            SafetySettings(enable_automatic_rollbacks=True, default_metrics=None)
        )
        db_session.commit()

        service = SafetyService(db_session)
        assert await service.should_rollback(db_session, flag.id) == (False, None, None)

    @pytest.mark.asyncio
    async def test_config_disabled_returns_false(self, db_session, make_feature_flag):
        flag = make_feature_flag(status=FeatureFlagStatus.ACTIVE, rollout_percentage=50)
        db_session.add(
            FeatureFlagSafetyConfig(
                feature_flag_id=flag.id,
                enabled=False,
                metrics={},
                rollback_percentage=0,
            )
        )
        db_session.commit()

        service = SafetyService(db_session)
        assert await service.should_rollback(db_session, flag.id) == (False, None, None)

    @pytest.mark.asyncio
    async def test_global_auto_rollback_disabled_returns_false(
        self, db_session, make_feature_flag, safety_settings_guard
    ):
        flag = make_feature_flag(status=FeatureFlagStatus.ACTIVE, rollout_percentage=50)
        db_session.add(
            FeatureFlagSafetyConfig(
                feature_flag_id=flag.id,
                enabled=True,
                metrics={"error_rate": {"critical_threshold": 0.01}},
                rollback_percentage=0,
            )
        )
        _wipe_safety_settings(db_session)
        db_session.add(
            SafetySettings(enable_automatic_rollbacks=False, default_metrics=None)
        )
        db_session.commit()

        service = SafetyService(db_session)
        assert await service.should_rollback(db_session, flag.id) == (False, None, None)

    @pytest.mark.asyncio
    async def test_healthy_check_returns_false(
        self, db_session, make_feature_flag, safety_settings_guard
    ):
        flag = make_feature_flag(status=FeatureFlagStatus.ACTIVE, rollout_percentage=50)
        _add_evaluations(db_session, flag, n=10)  # no errors -> healthy
        db_session.add(
            FeatureFlagSafetyConfig(
                feature_flag_id=flag.id,
                enabled=True,
                metrics={"error_rate": {"critical_threshold": 0.5}},
                rollback_percentage=0,
            )
        )
        _wipe_safety_settings(db_session)
        db_session.add(
            SafetySettings(enable_automatic_rollbacks=True, default_metrics=None)
        )
        db_session.commit()

        service = SafetyService(db_session)
        assert await service.should_rollback(db_session, flag.id) == (False, None, None)

    @pytest.mark.asyncio
    async def test_unhealthy_error_rate_recommends_rollback(
        self, db_session, make_feature_flag, safety_settings_guard
    ):
        flag = make_feature_flag(status=FeatureFlagStatus.ACTIVE, rollout_percentage=50)
        _add_error_log(db_session, flag)
        _add_evaluations(db_session, flag, n=2)  # error_rate == 0.5
        db_session.add(
            FeatureFlagSafetyConfig(
                feature_flag_id=flag.id,
                enabled=True,
                metrics={"error_rate": {"critical_threshold": 0.1}},
                rollback_percentage=0,
            )
        )
        _wipe_safety_settings(db_session)
        db_session.add(
            SafetySettings(enable_automatic_rollbacks=True, default_metrics=None)
        )
        db_session.commit()

        service = SafetyService(db_session)
        should, reason, details = await service.should_rollback(db_session, flag.id)

        assert should is True
        assert "error_rate" in reason
        assert details["trigger_type"] == RollbackTriggerType.ERROR_RATE
        assert details["trigger_value"] == pytest.approx(0.5)
        assert details["threshold_value"] == pytest.approx(0.1)
        assert "safety_check" in details

    @pytest.mark.asyncio
    async def test_unhealthy_latency_maps_to_latency_trigger(
        self, db_session, make_feature_flag, safety_settings_guard
    ):
        flag = make_feature_flag(status=FeatureFlagStatus.ACTIVE, rollout_percentage=50)
        _add_latency_sample(db_session, flag, 900.0)
        db_session.add(
            FeatureFlagSafetyConfig(
                feature_flag_id=flag.id,
                enabled=True,
                metrics={"avg_latency": {"critical_threshold": 500.0}},
                rollback_percentage=0,
            )
        )
        _wipe_safety_settings(db_session)
        db_session.add(
            SafetySettings(enable_automatic_rollbacks=True, default_metrics=None)
        )
        db_session.commit()

        service = SafetyService(db_session)
        should, reason, details = await service.should_rollback(db_session, flag.id)

        assert should is True
        assert details["trigger_type"] == RollbackTriggerType.LATENCY

    @pytest.mark.asyncio
    async def test_unhealthy_with_no_failing_metric_falls_back_to_generic_reason(
        self, db_session, make_feature_flag, safety_settings_guard
    ):
        """`check_feature_flag_safety` never actually returns is_healthy=False
        with an empty/all-healthy metrics list in practice (is_healthy is
        only False when at least one metric is unhealthy), so this fallback
        branch is unreachable through normal use. Exercise it directly via
        the explicitly-sanctioned mocking of the metric-source call."""
        from backend.app.schemas.safety import SafetyCheckResponse

        flag = make_feature_flag(status=FeatureFlagStatus.ACTIVE, rollout_percentage=50)
        db_session.add(
            FeatureFlagSafetyConfig(
                feature_flag_id=flag.id,
                enabled=True,
                metrics={"error_rate": {"critical_threshold": 0.1}},
                rollback_percentage=0,
            )
        )
        _wipe_safety_settings(db_session)
        db_session.add(
            SafetySettings(enable_automatic_rollbacks=True, default_metrics=None)
        )
        db_session.commit()

        service = SafetyService(db_session)
        unhealthy_no_metrics = SafetyCheckResponse(
            feature_flag_id=flag.id,
            is_healthy=False,
            metrics=[],
            last_checked=datetime.utcnow(),
        )
        with patch.object(
            SafetyService,
            "check_feature_flag_safety",
            new=AsyncMock(return_value=unhealthy_no_metrics),
        ):
            should, reason, details = await service.should_rollback(db_session, flag.id)

        assert should is True
        assert reason == "Multiple issues detected"
        assert details["trigger_type"] == RollbackTriggerType.AUTOMATIC
        assert details["trigger_value"] is None
        assert details["threshold_value"] is None

    @pytest.mark.asyncio
    async def test_metric_source_exception_is_swallowed(
        self, db_session, make_feature_flag, safety_settings_guard
    ):
        flag = make_feature_flag(status=FeatureFlagStatus.ACTIVE, rollout_percentage=50)
        db_session.add(
            FeatureFlagSafetyConfig(
                feature_flag_id=flag.id,
                enabled=True,
                metrics={"error_rate": {"critical_threshold": 0.1}},
                rollback_percentage=0,
            )
        )
        _wipe_safety_settings(db_session)
        db_session.add(
            SafetySettings(enable_automatic_rollbacks=True, default_metrics=None)
        )
        db_session.commit()

        service = SafetyService(db_session)
        with patch.object(
            SafetyService,
            "check_feature_flag_safety",
            new=AsyncMock(side_effect=RuntimeError("metrics backend unavailable")),
        ):
            result = await service.should_rollback(db_session, flag.id)

        assert result == (False, None, None)


# ---------------------------------------------------------------------------
# execute_rollback / rollback_feature_flag
# ---------------------------------------------------------------------------


class TestExecuteRollback:
    def test_missing_flag_returns_failure_response_with_no_side_effects(
        self, db_session
    ):
        service = SafetyService(db_session)

        result = service.execute_rollback(db_session, uuid.uuid4(), reason="probe")

        assert result.success is False
        assert result.previous_percentage is None
        assert result.new_percentage is None
        assert "does not exist" in result.message
        assert result.details == {"reason": "probe"}

    @pytest.mark.regression
    def test_happy_path_records_the_rollback_and_creates_no_config_for_a_flag_without_one(
        self, db_session, make_feature_flag, admin_user
    ):
        """#1206: a flag with no safety config of its own still has none after
        a rollback, so it stays on the global defaults; its record's
        ``safety_config_id`` is NULL. This test asserted the opposite until
        the rollback stopped creating one."""
        flag = make_feature_flag(status=FeatureFlagStatus.ACTIVE, rollout_percentage=75)
        assert (
            SafetyService.get_feature_flag_safety_config_record(db_session, flag.id)
            is None
        )

        service = SafetyService(db_session)
        # executed_by_user_id is a real FK to users.id; must reference an
        # actual persisted User row.
        executor_id = admin_user.id
        result = service.execute_rollback(
            db_session,
            flag.id,
            reason="metric threshold breached",
            trigger_type=RollbackTriggerType.CUSTOM_METRIC,
            trigger_value=9.9,
            threshold_value=5.0,
            executed_by_user_id=executor_id,
        )

        assert result.success is True
        assert result.previous_percentage == 75
        assert result.new_percentage == 0
        assert result.trigger_type == "custom_metric"
        assert result.details == {
            "reason": "metric threshold breached",
            "trigger_value": 9.9,
            "threshold_value": 5.0,
            "metrics_data": None,
            # A rollback to 0 turns the flag off and pauses its schedules (#629).
            "deactivated": True,
            "paused_schedules": [],
        }
        assert result.rollback_record_id is not None

        db_session.refresh(flag)
        assert flag.rollout_percentage == 0
        assert flag.status == FeatureFlagStatus.INACTIVE

        record = (
            db_session.query(SafetyRollbackRecord)
            .filter(SafetyRollbackRecord.id == result.rollback_record_id)
            .one()
        )
        assert record.feature_flag_id == flag.id
        assert record.previous_percentage == 75
        assert record.target_percentage == 0
        assert record.success is True
        assert record.executed_by_user_id == executor_id
        # No safety config was created: the flag is still on the defaults.
        assert (
            SafetyService.get_feature_flag_safety_config_record(db_session, flag.id)
            is None
        )
        assert record.safety_config_id is None

    @pytest.mark.regression
    def test_every_rollback_is_recorded_and_audited_with_its_reason(
        self, db_session, make_feature_flag, admin_user
    ):
        """A rollback writes a ``safety_rollback_records`` row and a
        ``safety_rollback`` audit entry, both carrying the reason; a flag on
        the global defaults gets no safety config for it (#1206)."""
        flag = make_feature_flag(status=FeatureFlagStatus.ACTIVE, rollout_percentage=60)
        service = SafetyService(db_session)

        result = service.execute_rollback(
            db_session,
            flag.id,
            reason="checkout errors observed",
            trigger_type=RollbackTriggerType.MANUAL,
            target_percentage=20,
            executed_by_user_id=admin_user.id,
        )

        assert result.success is True
        record = (
            db_session.query(SafetyRollbackRecord)
            .filter(SafetyRollbackRecord.id == result.rollback_record_id)
            .one()
        )
        assert record.feature_flag_id == flag.id
        assert record.trigger_type == "manual"
        assert record.trigger_reason == "checkout errors observed"
        assert (record.previous_percentage, record.target_percentage) == (60, 20)
        assert record.success is True
        assert record.executed_by_user_id == admin_user.id
        assert record.safety_config_id is None
        assert (
            SafetyService.get_feature_flag_safety_config_record(db_session, flag.id)
            is None
        )
        (entry,) = (
            db_session.query(AuditLog)
            .filter(
                AuditLog.entity_id == flag.id,
                AuditLog.action_type == ActionType.SAFETY_ROLLBACK.value,
            )
            .all()
        )
        assert entry.reason == "checkout errors observed"

    def test_happy_path_with_existing_config_and_custom_target_percentage(
        self, db_session, make_feature_flag
    ):
        flag = make_feature_flag(
            status=FeatureFlagStatus.ACTIVE, rollout_percentage=100
        )
        db_session.add(
            FeatureFlagSafetyConfig(
                feature_flag_id=flag.id,
                enabled=True,
                metrics={},
                rollback_percentage=25,
            )
        )
        db_session.commit()

        service = SafetyService(db_session)
        result = service.execute_rollback(
            db_session, flag.id, reason="partial rollback", target_percentage=25
        )

        assert result.success is True
        assert result.previous_percentage == 100
        assert result.new_percentage == 25
        db_session.refresh(flag)
        assert flag.rollout_percentage == 25

    def test_default_trigger_type_is_manual(self, db_session, make_feature_flag):
        flag = make_feature_flag(status=FeatureFlagStatus.ACTIVE, rollout_percentage=10)
        service = SafetyService(db_session)

        result = service.execute_rollback(db_session, flag.id)

        assert result.trigger_type == "manual"
        assert result.message == (
            f"Feature flag '{flag.key}' turned off and rolled back from 10% to 0%"
        )


class TestRollbackFeatureFlagAsync:
    @pytest.mark.asyncio
    async def test_rollback_feature_flag_missing_flag_raises_404(self, db_session):
        service = SafetyService(db_session)
        with pytest.raises(HTTPException) as exc_info:
            await service.rollback_feature_flag(uuid.uuid4())
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_rollback_feature_flag_happy_path_persists_record(
        self, db_session, make_feature_flag
    ):
        flag = make_feature_flag(status=FeatureFlagStatus.ACTIVE, rollout_percentage=80)
        service = SafetyService(db_session)

        result = await service.rollback_feature_flag(
            flag.id, percentage=10, reason="load test"
        )

        assert result.success is True
        assert result.previous_percentage == 80
        assert result.new_percentage == 10
        assert result.rollback_record_id is not None

        db_session.refresh(flag)
        assert flag.rollout_percentage == 10
        assert (
            db_session.query(SafetyRollbackRecord)
            .filter(SafetyRollbackRecord.id == result.rollback_record_id)
            .count()
            == 1
        )

    @pytest.mark.asyncio
    async def test_rollback_feature_flag_default_percentage_is_zero(
        self, db_session, make_feature_flag
    ):
        # ACTIVE: a flag that is already off has nothing to roll back (#629).
        flag = make_feature_flag(status=FeatureFlagStatus.ACTIVE, rollout_percentage=50)
        service = SafetyService(db_session)

        result = await service.rollback_feature_flag(flag.id)

        assert result.success is True, result.message
        assert result.new_percentage == 0
        assert result.trigger_type == "manual"

    @pytest.mark.asyncio
    async def test_async_rollback_feature_flag_is_the_same_function(self):
        assert (
            SafetyService.async_rollback_feature_flag
            is SafetyService.rollback_feature_flag
        )

    @pytest.mark.asyncio
    async def test_rollback_feature_flag_passes_through_trigger_type_and_executor(
        self, db_session, make_feature_flag, admin_user
    ):
        # ACTIVE: a flag that is already off has nothing to roll back (#629).
        flag = make_feature_flag(status=FeatureFlagStatus.ACTIVE, rollout_percentage=40)
        service = SafetyService(db_session)
        executor_id = admin_user.id

        result = await service.rollback_feature_flag(
            flag.id,
            percentage=0,
            reason="automated safety rollback",
            trigger_type=RollbackTriggerType.AUTOMATIC,
            executed_by_user_id=executor_id,
        )

        assert result.trigger_type == "automatic"
        record = (
            db_session.query(SafetyRollbackRecord)
            .filter(SafetyRollbackRecord.id == result.rollback_record_id)
            .one()
        )
        assert record.executed_by_user_id == executor_id
        assert record.trigger_type == "automatic"
