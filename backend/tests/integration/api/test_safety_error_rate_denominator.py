"""What the safety check's ``error_rate`` divides by, and what ``error_count`` does not.

* One request to ``GET /api/v1/feature-flags/user/{user_id}`` records one
  evaluation of every active flag, so each active flag's ``total_evaluations``
  in the window rises by one; a flag that is not active gains none.
* ``error_count`` counts errors only: the same errors read the same
  ``error_count`` whether the flag was evaluated a few times or many, while
  ``error_rate`` follows the evaluations. A ``critical_threshold`` on
  ``error_count`` breaches at a number of errors whatever the traffic.
"""

import uuid
from datetime import datetime

import pytest

from backend.app.models.audit_log import AuditLog
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.metrics.metric import ErrorLog, MetricType, RawMetric
from backend.app.models.safety import FeatureFlagSafetyConfig, SafetyRollbackRecord
from backend.app.services.feature_flag_service import FeatureFlagService
from backend.app.services.safety_service import SafetyService

pytestmark = [pytest.mark.integration]

PREFIX = "safety-denominator-"
SAFETY = "/api/v1/safety"


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


def _flag(db_session, status=FeatureFlagStatus.ACTIVE) -> FeatureFlag:
    flag = FeatureFlag(
        key=f"{PREFIX}{uuid.uuid4().hex[:10]}",
        name="safety denominator",
        status=status,
        rollout_percentage=50,
    )
    db_session.add(flag)
    db_session.commit()
    db_session.refresh(flag)
    return flag


def _window(db_session, flag) -> dict:
    db_session.expire_all()
    return SafetyService(db_session).get_error_metrics(db_session, flag.id)


def _errors(db_session, flag, count: int) -> None:
    for _ in range(count):
        db_session.add(
            ErrorLog(
                feature_flag_id=flag.id,
                error_type="crash",
                message="an error behind the flag",
                timestamp=datetime.utcnow(),
            )
        )
    db_session.commit()


def _evaluations(db_session, flag, count: int) -> None:
    for _ in range(count):
        db_session.add(
            RawMetric(
                feature_flag_id=flag.id,
                metric_type=MetricType.FLAG_EVALUATION.value,
                timestamp=datetime.utcnow(),
            )
        )
    db_session.commit()


def test_a_user_flags_request_counts_one_evaluation_of_every_active_flag(db_session):
    first, second = _flag(db_session), _flag(db_session)
    off = _flag(db_session, status=FeatureFlagStatus.INACTIVE)
    assert [_window(db_session, f)["total_evaluations"] for f in (first, second)] == [
        0,
        0,
    ]

    FeatureFlagService(db_session).get_user_flags("denominator-user-1")
    for flag in (first, second):
        assert _window(db_session, flag)["total_evaluations"] == 1

    FeatureFlagService(db_session).get_user_flags("denominator-user-2")
    for flag in (first, second):
        assert _window(db_session, flag)["total_evaluations"] == 2
    assert _window(db_session, off)["total_evaluations"] == 0


def test_error_count_does_not_change_with_how_often_the_flag_is_evaluated(db_session):
    quiet, busy = _flag(db_session), _flag(db_session)
    for flag in (quiet, busy):
        _errors(db_session, flag, 3)
    _evaluations(db_session, quiet, 10)
    _evaluations(db_session, busy, 1000)

    quiet_window, busy_window = _window(db_session, quiet), _window(db_session, busy)
    assert quiet_window["error_count"] == busy_window["error_count"] == 3
    assert quiet_window["error_rate"] == pytest.approx(0.3)
    assert busy_window["error_rate"] == pytest.approx(0.003)


def test_a_critical_threshold_on_error_count_breaches_whatever_the_traffic(
    admin_client, db_session
):
    flag = _flag(db_session)
    resp = admin_client.post(
        f"{SAFETY}/feature-flags/{flag.id}/config",
        json={
            "metrics": {
                "error_rate": {"critical_threshold": 0.5},
                "error_count": {"critical_threshold": 2},
            }
        },
    )
    assert resp.status_code == 200, resp.text
    _errors(db_session, flag, 3)
    _evaluations(db_session, flag, 1000)

    check = admin_client.get(f"{SAFETY}/feature-flags/{flag.id}/check").json()
    by_name = {m["name"]: m for m in check["metrics"]}
    assert by_name["error_rate"]["is_healthy"] is True
    assert by_name["error_count"]["is_healthy"] is False
    assert check["is_healthy"] is False
