"""``GET /api/v1/metrics/summary`` counts errors by the safety check's rule (#1206).

The summary's ``error_rate`` used to count every ``error_logs`` row in its
period, including the error reports the safety check does not count (those
stored with ``request_data["source"] = "client"``). Both now read the one rule,
``counted_error_filter()`` in ``backend/app/models/metrics/metric.py``, and the
summary reports the reports it leaves out as ``uncounted_error_count``.

What still differs, and is documented on ``MetricsSummary.error_rate``: the
period (``start_date`` to ``end_date``, by default 30 days, against the check's
last 15 minutes), the denominator (aggregated flag evaluations against the
check's raw ones) and the unit (a percentage against a fraction). The first
test builds equal windows and denominators, so the two differ by the unit
alone; the second shows the period and the denominator.

Everything runs on PostgreSQL through the real route: a mocked session would
ignore the filter under test.
"""

import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import JSON, null, text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.core.config import settings
from backend.app.core.security import create_local_access_token
from backend.app.main import app
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.metrics.metric import (
    AggregatedMetric,
    AggregationPeriod,
    ErrorLog,
    MetricType,
    RawMetric,
)
from backend.app.services.safety_service import SafetyService

pytestmark = [pytest.mark.integration, pytest.mark.regression]

SCHEMA = "test_experimentation"
SUMMARY_URL = "/api/v1/metrics/summary"


@pytest.fixture(autouse=True)
def _local_auth(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")


@pytest.fixture
def client(db_session):
    engine = db_session.get_bind()
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    def override_get_db():
        session = factory()
        session.execute(text(f"SET search_path TO {SCHEMA}"))
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[deps.get_db] = override_get_db
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
    finally:
        app.dependency_overrides.pop(deps.get_db, None)


def _flag(make_feature_flag) -> FeatureFlag:
    return make_feature_flag(
        key=f"summary-errors-{uuid.uuid4().hex[:8]}",
        status=FeatureFlagStatus.ACTIVE,
        rollout_percentage=100,
    )


def _evaluations(db_session, flag, *, raw: int, aggregated: int) -> None:
    """``raw`` flag evaluations now (what the check divides by) and an
    aggregated day of ``aggregated`` evaluations (what the summary divides by)."""
    now = datetime.utcnow()
    if raw:
        db_session.add(
            RawMetric(
                metric_type=MetricType.FLAG_EVALUATION.value,
                feature_flag_id=flag.id,
                user_id="u1",
                count=raw,
                timestamp=now - timedelta(seconds=30),
            )
        )
    if aggregated:
        db_session.add(
            AggregatedMetric(
                metric_type=MetricType.FLAG_EVALUATION.value,
                period=AggregationPeriod.DAY.value,
                period_start=now - timedelta(minutes=1),
                feature_flag_id=flag.id,
                count=aggregated,
            )
        )


def _summary(client, user, flag) -> dict:
    resp = client.get(
        SUMMARY_URL,
        params={"feature_flag_id": str(flag.id)},
        headers={"Authorization": f"Bearer {create_local_access_token(user)}"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_the_summary_counts_errors_by_the_safety_checks_rule(
    client, db_session, viewer_user, make_feature_flag
):
    """The five stored shapes of ``test_error_report_source.py``: four count,
    one does not, on the summary exactly as on the check.

    Windows and denominators are equal by construction (every row is inside
    the check's 15 minutes, and 100 evaluations are both raw and aggregated),
    so the summary's ``error_rate`` is the check's times 100: the unit is the
    only difference left.
    """
    flag = _flag(make_feature_flag)
    shapes = {
        "earlier report": {"source": "client", "feature_flag_key": flag.key},
        "server report": {"source": "server", "feature_flag_key": flag.key},
        "failed evaluation": {"context": {"country": "US"}},
        "no request data": null(),
        "json null": JSON.NULL,
    }
    for name, request_data in shapes.items():
        db_session.add(
            ErrorLog(
                error_type=name.replace(" ", "_"),
                feature_flag_id=flag.id,
                message=name,
                request_data=request_data,
            )
        )
    _evaluations(db_session, flag, raw=100, aggregated=100)
    db_session.commit()

    summary = _summary(client, viewer_user, flag)
    assert summary["total_evaluations"] == 100
    assert summary["error_rate"] == pytest.approx(4.0)
    assert summary["uncounted_error_count"] == 1

    db_session.expire_all()
    check = SafetyService(db_session).get_error_metrics(db_session, flag.id)
    assert check["error_count"] == 4
    assert check["uncounted_error_count"] == summary["uncounted_error_count"]
    assert check["total_evaluations"] == summary["total_evaluations"]
    assert summary["error_rate"] == pytest.approx(check["error_rate"] * 100)


def test_the_summary_still_differs_from_the_check_in_period_and_denominator(
    client, db_session, viewer_user, make_feature_flag
):
    """The two differences besides the unit that ``error_rate``'s description
    names.

    Period: an error 20 minutes old is in the summary's 30 days and outside the
    check's 15 minutes. Denominator: with raw evaluations and none aggregated,
    the summary has no rate (``null``) while the check has one.
    """
    older = _flag(make_feature_flag)
    db_session.add(
        ErrorLog(
            error_type="failed_evaluation",
            feature_flag_id=older.id,
            message="older",
            request_data={"context": {}},
            timestamp=datetime.utcnow() - timedelta(minutes=20),
        )
    )
    _evaluations(db_session, older, raw=10, aggregated=10)

    unaggregated = _flag(make_feature_flag)
    db_session.add(
        ErrorLog(
            error_type="failed_evaluation",
            feature_flag_id=unaggregated.id,
            message="now",
            request_data={"context": {}},
        )
    )
    _evaluations(db_session, unaggregated, raw=4, aggregated=0)
    db_session.commit()

    service = SafetyService(db_session)

    summary = _summary(client, viewer_user, older)
    assert summary["error_rate"] == pytest.approx(10.0)
    assert summary["uncounted_error_count"] == 0
    check = service.get_error_metrics(db_session, older.id)
    assert check["error_count"] == 0
    assert check["error_rate"] == 0.0

    summary = _summary(client, viewer_user, unaggregated)
    assert summary["total_evaluations"] == 0
    assert summary["error_rate"] is None
    check = service.get_error_metrics(db_session, unaggregated.id)
    assert check["error_count"] == 1
    assert check["error_rate"] == pytest.approx(0.25)
