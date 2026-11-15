"""``GET /api/v1/metrics/summary`` refuses two dates it cannot use with 422 (#1561).

Both used to answer 500:

* a ``start_date`` or ``end_date`` whose UTC offset is 16 hours or more either
  way. PostgreSQL stores offsets up to 15:59:59 and failed the query with
  "time zone displacement out of range". The nightly API fuzz sent
  ``end_date=0562-05-04T20:38:36.1-19:52`` (#1561), one of the #956 family;
* an ``end_date`` sent without ``start_date`` whose default start, 30 days
  earlier, would fall before year 1: computing it raised ``OverflowError``.

Each refusal sits at ``["query", <field>]`` with a fixed ``msg`` that does not
repeat the value. Everything else answers as before: offsets up to 15:59
either way, ``Z``, a naive date, and a year-1 ``end_date`` sent with a
``start_date``. The OpenAPI document does not change; that is
``test_openapi_snapshot.py``'s to pin.

Everything runs on PostgreSQL through the real route: which offsets are
refused is decided by what the database can store, so only the database can
show that the ones still accepted are answered.
"""

import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.core.config import settings
from backend.app.core.security import create_local_access_token
from backend.app.main import app
from backend.app.models.feature_flag import FeatureFlagStatus
from backend.app.models.metrics.metric import (
    AggregatedMetric,
    AggregationPeriod,
    MetricType,
)

pytestmark = [pytest.mark.integration, pytest.mark.regression]

SCHEMA = "test_experimentation"
SUMMARY_URL = "/api/v1/metrics/summary"

#: The two fixed messages, written out: the test pins the text, not whatever
#: the route's constants happen to hold.
OFFSET_MSG = "Value error, the UTC offset must be less than 16 hours"
TOO_EARLY_MSG = (
    "Value error, end_date is too early for the default 30-day period; "
    "send start_date as well"
)


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


def _get(client, user, params: dict):
    return client.get(
        SUMMARY_URL,
        params=params,
        headers={"Authorization": f"Bearer {create_local_access_token(user)}"},
    )


def _assert_refused(resp, field: str, msg: str, sent: str) -> None:
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert len(detail) == 1, detail
    assert detail[0]["loc"] == ["query", field]
    assert detail[0]["type"] == "value_error"
    assert detail[0]["msg"] == msg
    assert sent not in resp.text


@pytest.mark.parametrize(
    ("field", "sent", "params"),
    [
        pytest.param(
            "end_date",
            "0562-05-04T20:38:36.1-19:52",
            {"period": "hour"},
            id="end_date-19:52-the-fuzz-request",
        ),
        pytest.param("start_date", "2026-10-15T12:00:00+16:00", {}, id="start+16:00"),
        pytest.param("end_date", "2026-10-15T12:00:00-16:00", {}, id="end-16:00"),
    ],
)
def test_an_offset_of_16_hours_or_more_answers_422(
    client, viewer_user, field, sent, params
):
    resp = _get(client, viewer_user, {**params, field: sent})
    _assert_refused(resp, field, OFFSET_MSG, sent)
    offset = sent[-6:]
    assert offset not in resp.text


def test_an_end_date_whose_default_start_falls_before_year_1_answers_422(
    client, viewer_user
):
    sent = "0001-01-05T00:00:00Z"
    resp = _get(client, viewer_user, {"end_date": sent})
    _assert_refused(resp, "end_date", TOO_EARLY_MSG, sent)
    assert "0001-01-05" not in resp.text


def test_the_same_end_date_with_a_start_date_is_answered(client, viewer_user):
    """The message's advice works: with ``start_date`` sent, no default start is
    computed, and the year-1 period is summarised (empty) as before."""
    resp = _get(
        client,
        viewer_user,
        {"start_date": "0001-01-01T00:00:00Z", "end_date": "0001-01-05T00:00:00Z"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["total_evaluations"] == 0


@pytest.mark.parametrize("field", ["start_date", "end_date"])
@pytest.mark.parametrize(
    "sent",
    [
        "2026-10-15T12:00:00+15:59",
        "2026-10-15T12:00:00-15:59",
        "2026-10-15T12:00:00Z",
        "2026-10-15T12:00:00",
    ],
    ids=["+15:59", "-15:59", "Z", "naive"],
)
def test_offsets_under_16_hours_utc_and_naive_dates_are_answered(
    client, viewer_user, field, sent
):
    resp = _get(client, viewer_user, {field: sent})
    assert resp.status_code == 200, resp.text
    assert "total_evaluations" in resp.json()


def test_a_period_bounded_at_15_59_either_way_reads_the_stored_metrics(
    client, db_session, viewer_user, make_feature_flag
):
    """The largest offsets still accepted reach the database intact: a period
    whose start is 12:00 UTC written at -15:59 and whose end is the same instant
    written at +15:59 holds exactly the day's row stored at 12:00 UTC."""
    flag = make_feature_flag(
        key=f"summary-dates-{uuid.uuid4().hex[:8]}",
        status=FeatureFlagStatus.ACTIVE,
        rollout_percentage=100,
    )
    db_session.add(
        AggregatedMetric(
            metric_type=MetricType.FLAG_EVALUATION.value,
            period=AggregationPeriod.DAY.value,
            period_start=datetime(2026, 10, 15, 12, 0, 0),
            feature_flag_id=flag.id,
            count=7,
        )
    )
    db_session.commit()

    resp = _get(
        client,
        viewer_user,
        {
            "feature_flag_id": str(flag.id),
            "start_date": "2026-10-14T20:01:00-15:59",
            "end_date": "2026-10-16T03:59:00+15:59",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["total_evaluations"] == 7

    # A start one minute later leaves the row out, so the 7 above is the
    # offsets being honoured, not a period wide enough to hold anything.
    later_start = _get(
        client,
        viewer_user,
        {
            "feature_flag_id": str(flag.id),
            "start_date": "2026-10-14T20:02:00-15:59",
            "end_date": "2026-10-16T03:59:00+15:59",
        },
    )
    assert later_start.status_code == 200, later_start.text
    assert later_start.json()["total_evaluations"] == 0
