"""The demo seed's safety thresholds are ones the safety check reads (#1205).

``backend/scripts/seed_demo_data.py`` gives ``new_dashboard_ui`` a safety
configuration and writes the global ``default_metrics``. Both used to be
``{threshold, window_minutes, comparison}``, keys ``MetricThreshold`` does not
read, so the flag never reached critical and a flag without its own
configuration had no usable default.

Here the seed's own functions run against a session that records what they
add, and those rows are read back by ``SafetyService`` -- the check the safety
monitor and ``GET /safety/feature-flags/{id}/check`` run -- with the error
rate fixed. No database, no network.
"""

from __future__ import annotations

import asyncio
import copy
import uuid
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from backend.app.models import register_core_models
from backend.app.models.feature_flag import FeatureFlag
from backend.app.models.safety import FeatureFlagSafetyConfig, SafetySettings
from backend.app.services.safety_service import DEFAULT_CONFIG_ID, SafetyService
from backend.scripts import seed_demo_data as seed

pytestmark = [pytest.mark.unit]

#: The demo's intent: an error rate above 5% is critical.
CRITICAL = 0.05


class _Rows:
    """``query(Model).filter(...).first()`` answering one row per model."""

    def __init__(self, row: Any) -> None:
        self.row = row

    def filter(self, *args: Any, **kwargs: Any) -> "_Rows":
        return self

    def first(self) -> Any:
        return self.row


class _Session:
    """Finds the rows it is given, keeps what it is added, ids as a flush would."""

    def __init__(self, rows: Optional[Dict[type, Any]] = None) -> None:
        self.rows = rows or {}
        self.added: List[Any] = []
        self.commits = 0

    def query(self, model: type, *args: Any) -> _Rows:
        return _Rows(self.rows.get(model))

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    def add_all(self, objs: List[Any]) -> None:
        self.added.extend(objs)

    def flush(self) -> None:
        now = datetime.utcnow()
        for obj in self.added:
            for column, value in (
                ("id", uuid.uuid4()),
                ("created_at", now),
                ("updated_at", now),
            ):
                if hasattr(obj, column) and getattr(obj, column) is None:
                    setattr(obj, column, value)

    def commit(self) -> None:
        self.flush()
        self.commits += 1

    def one(self, cls: type) -> Any:
        (row,) = [obj for obj in self.added if isinstance(obj, cls)]
        return row


@pytest.fixture(scope="module")
def seeded() -> SimpleNamespace:
    """What the seed writes on a new database."""
    register_core_models()
    session = _Session()
    flags = seed.seed_feature_flags(session, SimpleNamespace(id=uuid.uuid4()))
    seed.seed_safety_settings(session)
    return SimpleNamespace(
        flags=flags,
        config=session.one(FeatureFlagSafetyConfig),
        settings=session.one(SafetySettings),
    )


def _check(flag: Any, config: Any, settings: Any, error_rate: float, monkeypatch):
    """Run the safety check over the seeded rows at ``error_rate``."""
    evaluations = 1000

    def error_metrics(self, db, feature_flag_id, timeframe_minutes=15):
        return {
            "error_count": round(error_rate * evaluations),
            "uncounted_error_count": 0,
            "total_evaluations": evaluations,
            "error_rate": error_rate,
            "timeframe_minutes": timeframe_minutes,
        }

    monkeypatch.setattr(SafetyService, "get_error_metrics", error_metrics)
    service = SafetyService(
        _Session(
            {
                FeatureFlag: flag,
                FeatureFlagSafetyConfig: config,
                SafetySettings: settings,
            }
        )
    )
    check = asyncio.run(service.check_feature_flag_safety(flag.id))
    decision = asyncio.run(service.should_rollback(service.db, flag.id))
    return check, decision


def _error_rate_status(check: Any) -> Any:
    (status,) = [m for m in check.metrics if m.name == "error_rate"]
    return status


@pytest.mark.regression
def test_new_dashboard_ui_is_critical_above_a_5_percent_error_rate(seeded, monkeypatch):
    flag = seeded.flags["new_dashboard_ui"]
    assert seeded.config.feature_flag_id == flag.id

    above, rollback = _check(flag, seeded.config, seeded.settings, 0.06, monkeypatch)
    status = _error_rate_status(above)
    assert status.details["critical_threshold"] == CRITICAL, (
        f"the seeded error_rate threshold is not read: {seeded.config.metrics}"
    )
    assert status.details["comparison_type"] == "greater_than"
    assert status.threshold == CRITICAL
    assert above.is_healthy is False
    assert rollback[0] is True

    at, rollback = _check(flag, seeded.config, seeded.settings, CRITICAL, monkeypatch)
    assert at.is_healthy is True
    assert rollback[0] is False


@pytest.mark.regression
def test_a_flag_without_its_own_config_gets_the_seeded_default_thresholds(
    seeded, monkeypatch
):
    flag = seeded.flags["beta_features"]

    above, rollback = _check(flag, None, seeded.settings, 0.06, monkeypatch)
    status = _error_rate_status(above)
    assert above.details.get("feature_flag_key") == "beta_features"
    assert status.details["critical_threshold"] == CRITICAL, (
        f"the seeded default_metrics are not read: {seeded.settings.default_metrics}"
    )
    assert status.details["comparison_type"] == "greater_than"
    assert above.is_healthy is False
    assert rollback[0] is True

    at, _ = _check(flag, None, seeded.settings, CRITICAL, monkeypatch)
    assert at.is_healthy is True


def test_the_default_config_is_built_from_the_seeded_settings(seeded, monkeypatch):
    flag = seeded.flags["beta_features"]
    service = SafetyService(
        _Session({FeatureFlag: flag, SafetySettings: seeded.settings})
    )
    config = asyncio.run(service.get_feature_flag_safety_config(flag.id))
    assert config.id == DEFAULT_CONFIG_ID
    threshold = config.metrics["error_rate"]
    assert (threshold.critical_threshold, threshold.comparison_type) == (
        CRITICAL,
        "greater_than",
    )


@pytest.mark.regression
def test_a_rerun_replaces_the_thresholds_the_old_seed_wrote():
    register_core_models()
    flag = FeatureFlag(id=uuid.uuid4(), key="new_dashboard_ui")
    config = FeatureFlagSafetyConfig(
        feature_flag_id=flag.id, metrics=copy.deepcopy(seed.UNREAD_SAFETY_METRICS)
    )
    settings = SafetySettings(
        enable_automatic_rollbacks=True,
        default_metrics=copy.deepcopy(seed.UNREAD_SAFETY_METRICS),
    )
    rows = {
        FeatureFlag: flag,
        FeatureFlagSafetyConfig: config,
        SafetySettings: settings,
    }

    for _ in range(2):  # the second run finds nothing left to replace
        session = _Session(rows)
        seed.seed_feature_flags(session, SimpleNamespace(id=uuid.uuid4()))
        seed.seed_safety_settings(session)
        assert session.added == []
        assert config.metrics == seed.SAFETY_METRICS
        assert settings.default_metrics == seed.SAFETY_METRICS


def test_a_rerun_leaves_thresholds_set_since_alone():
    register_core_models()
    changed = {
        "error_rate": {"critical_threshold": 0.1, "comparison_type": "greater_than"}
    }
    flag = FeatureFlag(id=uuid.uuid4(), key="new_dashboard_ui")
    config = FeatureFlagSafetyConfig(
        feature_flag_id=flag.id, metrics=copy.deepcopy(changed)
    )
    settings = SafetySettings(enable_automatic_rollbacks=False, default_metrics=None)
    session = _Session(
        {FeatureFlag: flag, FeatureFlagSafetyConfig: config, SafetySettings: settings}
    )

    seed.seed_feature_flags(session, SimpleNamespace(id=uuid.uuid4()))
    seed.seed_safety_settings(session)

    assert config.metrics == changed
    assert settings.default_metrics is None
    assert settings.enable_automatic_rollbacks is False
