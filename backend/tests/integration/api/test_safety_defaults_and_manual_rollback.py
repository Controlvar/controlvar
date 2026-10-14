"""Which thresholds a flag is checked against, and what a manual rollback sends.

* The global ``default_metrics`` apply only to a flag with no safety
  configuration of its own. A flag that has one is checked against its own
  thresholds only, so a flag can be taken off a default threshold by giving it
  a configuration without that metric.
* A rollback of a flag that has no configuration leaves it with none, so it
  is still checked against the defaults (#1206; until then the rollback
  stored a configuration with no thresholds).
* A manual rollback (``POST .../rollback``) sends no Slack alert and no
  webhook event: only the safety monitor's automatic rollback notifies.
* Any signed-in user can read the global settings; changing them takes a
  superuser.
* Each metric of a check carries its thresholds, its comparison and whether
  the platform could measure it.
* A flag turned back on while one of its own critical thresholds is still
  breached is rolled back again by the monitor's next pass.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app.models.audit_log import AuditLog
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.metrics.metric import ErrorLog, MetricType, RawMetric
from backend.app.models.safety import (
    FeatureFlagSafetyConfig,
    SafetyRollbackRecord,
    SafetySettings,
)
from backend.app.services.safety_service import DEFAULT_CONFIG_ID

pytestmark = [pytest.mark.integration]

PREFIX = "safety-defaults-"
NIL_UUID = "00000000-0000-0000-0000-000000000000"
SAFETY = "/api/v1/safety"
SCHEMA = "test_experimentation"

#: A latency default every recorded evaluation breaches, and an error-rate one.
DEFAULT_METRICS = {
    "p95_latency": {"critical_threshold": 1.0, "comparison_type": "greater_than"},
    "error_rate": {"critical_threshold": 0.5, "comparison_type": "greater_than"},
}


@pytest.fixture
def global_defaults(db_session):
    """The global settings with ``DEFAULT_METRICS``, restored afterwards."""
    row = db_session.query(SafetySettings).first()
    created = row is None
    if created:
        row = SafetySettings(enable_automatic_rollbacks=False, default_metrics=None)
        db_session.add(row)
        previous = None
    else:
        previous = (row.enable_automatic_rollbacks, row.default_metrics)
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
            RawMetric,
            ErrorLog,
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


def _flag(db_session, latency_ms: float = 0.0) -> FeatureFlag:
    """An ACTIVE flag at 50%, with three latency rows of *latency_ms* when set."""
    flag = FeatureFlag(
        key=f"{PREFIX}{uuid.uuid4().hex[:10]}",
        name="safety defaults",
        status=FeatureFlagStatus.ACTIVE,
        rollout_percentage=50,
    )
    db_session.add(flag)
    db_session.commit()
    db_session.refresh(flag)
    if latency_ms:
        for _ in range(3):
            db_session.add(
                RawMetric(
                    feature_flag_id=flag.id,
                    metric_type=MetricType.LATENCY.value,
                    value=latency_ms,
                    timestamp=datetime.utcnow(),
                )
            )
        db_session.commit()
    return flag


def _check(client, flag) -> dict:
    resp = client.get(f"{SAFETY}/feature-flags/{flag.id}/check")
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_a_flag_with_its_own_configuration_is_checked_against_its_own_thresholds_only(
    admin_client, db_session, global_defaults
):
    on_defaults = _flag(db_session, latency_ms=50.0)
    own_config = _flag(db_session, latency_ms=50.0)
    resp = admin_client.post(
        f"{SAFETY}/feature-flags/{own_config.id}/config",
        json={
            "metrics": {
                "error_rate": {
                    "critical_threshold": 0.5,
                    "comparison_type": "greater_than",
                }
            }
        },
    )
    assert resp.status_code == 200, resp.text

    defaults = _check(admin_client, on_defaults)
    assert {m["name"] for m in defaults["metrics"]} == {"p95_latency", "error_rate"}
    assert defaults["is_healthy"] is False

    own = _check(admin_client, own_config)
    assert [m["name"] for m in own["metrics"]] == ["error_rate"]
    assert own["is_healthy"] is True


@pytest.mark.regression
def test_a_rollback_leaves_a_flag_without_a_configuration_on_the_defaults(
    admin_client, db_session, global_defaults
):
    """#1206: renamed and inverted from
    ``test_a_rollback_gives_a_flag_without_a_configuration_one_with_no_thresholds``,
    which pinned the rollback that stored an empty configuration."""
    flag = _flag(db_session, latency_ms=50.0)
    before = admin_client.get(f"{SAFETY}/feature-flags/{flag.id}/config").json()
    assert before["id"] == NIL_UUID == str(DEFAULT_CONFIG_ID)
    assert set(before["metrics"]) == set(DEFAULT_METRICS)

    resp = admin_client.post(
        f"{SAFETY}/feature-flags/{flag.id}/rollback", params={"percentage": 10}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["success"] is True

    after = admin_client.get(f"{SAFETY}/feature-flags/{flag.id}/config").json()
    assert after["id"] == NIL_UUID
    assert set(after["metrics"]) == set(DEFAULT_METRICS)
    check = _check(admin_client, flag)
    assert {m["name"] for m in check["metrics"]} == set(DEFAULT_METRICS)
    assert check["is_healthy"] is False


def test_a_manual_rollback_sends_no_alert_and_no_webhook_event(
    admin_client, db_session
):
    """Every way out is watched: the rollback notification, the webhook
    (``_send``, ``send_webhook`` and the HTTP call under them) and Slack's one
    sender, so a direct call to any of them fails the test."""
    flag = _flag(db_session)
    service = "backend.app.services.notification_service"
    with (
        patch(f"{service}.NotificationService.notify_safety_rollback") as notify,
        patch(f"{service}.NotificationService._send") as send,
        patch(f"{service}.NotificationService.send_webhook") as webhook,
        patch(f"{service}.httpx.post") as http_post,
        patch(
            "backend.app.services.slack_notifier.SlackNotifier._send_message"
        ) as slack,
    ):
        resp = admin_client.post(
            f"{SAFETY}/feature-flags/{flag.id}/rollback",
            params={"percentage": 0, "reason": "manual"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["success"] is True
    for channel in (notify, send, webhook, http_post, slack):
        channel.assert_not_called()


def test_any_signed_in_user_can_read_the_global_settings_and_only_a_superuser_change_them(
    viewer_client,
):
    read = viewer_client.get(f"{SAFETY}/settings")
    assert read.status_code == 200, read.text
    assert "enable_automatic_rollbacks" in read.json()

    write = viewer_client.post(
        f"{SAFETY}/settings", json={"enable_automatic_rollbacks": True}
    )
    assert write.status_code == 403, write.text


def test_each_metric_of_a_check_carries_its_thresholds_and_whether_it_was_measured(
    admin_client, db_session
):
    flag = _flag(db_session)
    resp = admin_client.post(
        f"{SAFETY}/feature-flags/{flag.id}/config",
        json={
            "metrics": {
                "error_rate": {
                    "warning_threshold": 0.02,
                    "critical_threshold": 0.05,
                    "comparison_type": "greater_than",
                },
                "queue_depth": {"critical_threshold": 5},
            }
        },
    )
    assert resp.status_code == 200, resp.text

    metrics = {m["name"]: m for m in _check(admin_client, flag)["metrics"]}
    assert metrics["error_rate"]["details"] == {
        "comparison_type": "greater_than",
        "warning_threshold": 0.02,
        "critical_threshold": 0.05,
        "warning": False,
        "measured": True,
    }
    assert metrics["queue_depth"]["details"]["measured"] is False


@pytest.fixture
def automatic_rollbacks(db_session):
    """Global automatic rollbacks on, restored afterwards."""
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


@pytest.fixture
def session_factory(db_session):
    # autoflush=False, as backend.app.db.session.SessionLocal is configured.
    factory = sessionmaker(bind=db_session.get_bind(), autoflush=False)

    def make():
        session = factory()
        session.execute(text(f"SET search_path TO {SCHEMA}"))
        return session

    return make


def _safety_tick(session_factory) -> None:
    from backend.app.core.safety_scheduler import SafetyScheduler

    scheduler = SafetyScheduler(interval_minutes=1)
    with (
        patch("backend.app.core.safety_scheduler.SessionLocal", session_factory),
        patch.object(scheduler, "_notification_service", MagicMock()),
    ):
        asyncio.run(scheduler.check_feature_flags_safety())


@pytest.mark.usefixtures("automatic_rollbacks")
def test_a_flag_turned_back_on_while_its_threshold_is_still_breached_is_rolled_back_again(
    admin_client, db_session, session_factory
):
    flag = _flag(db_session)
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
            rollback_percentage=0,
        )
    )
    for _ in range(10):
        db_session.add(
            RawMetric(
                feature_flag_id=flag.id, metric_type=MetricType.FLAG_EVALUATION.value
            )
        )
    for i in range(5):
        db_session.add(
            ErrorLog(feature_flag_id=flag.id, error_type="crash", message=f"e{i}")
        )
    db_session.commit()

    def records() -> int:
        db_session.expire_all()
        return (
            db_session.query(SafetyRollbackRecord)
            .filter(SafetyRollbackRecord.feature_flag_id == flag.id)
            .count()
        )

    _safety_tick(session_factory)
    assert records() == 1

    on = admin_client.post(f"/api/v1/feature-flags/{flag.id}/activate")
    assert on.status_code == 200, on.text
    assert on.json()["status"] == "active"

    _safety_tick(session_factory)
    assert records() == 2
    db_session.expire_all()
    stored = db_session.query(FeatureFlag).filter(FeatureFlag.id == flag.id).one()
    assert stored.status == FeatureFlagStatus.INACTIVE


def test_the_check_reads_the_last_15_minutes_of_errors_and_latency(
    admin_client, db_session
):
    """A row 14 minutes old counts and one 16 minutes old does not, for the
    error metrics and the latency metrics alike."""
    flag = _flag(db_session)
    resp = admin_client.post(
        f"{SAFETY}/feature-flags/{flag.id}/config",
        json={
            "metrics": {
                "error_count": {"critical_threshold": 100},
                "total_evaluations": {"critical_threshold": 100},
                "max_latency": {"critical_threshold": 100000},
            }
        },
    )
    assert resp.status_code == 200, resp.text
    now = datetime.utcnow()
    for minutes, latency in ((14, 50.0), (16, 5000.0)):
        at = now - timedelta(minutes=minutes)
        db_session.add(
            ErrorLog(
                feature_flag_id=flag.id,
                error_type="crash",
                message=f"{minutes} minutes ago",
                timestamp=at,
            )
        )
        db_session.add(
            RawMetric(
                feature_flag_id=flag.id,
                metric_type=MetricType.FLAG_EVALUATION.value,
                timestamp=at,
            )
        )
        db_session.add(
            RawMetric(
                feature_flag_id=flag.id,
                metric_type=MetricType.LATENCY.value,
                value=latency,
                timestamp=at,
            )
        )
    db_session.commit()

    values = {
        m["name"]: m["current_value"] for m in _check(admin_client, flag)["metrics"]
    }
    assert values == {
        "error_count": 1.0,
        "total_evaluations": 1.0,
        "max_latency": 50.0,
    }
