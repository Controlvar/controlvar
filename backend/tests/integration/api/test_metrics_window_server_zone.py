"""The metrics routes count the window asked for whatever the server's time zone (#1795).

``raw_metrics.timestamp``, ``aggregated_metrics.period_start`` and
``error_logs.timestamp`` are ``timestamp without time zone`` columns that hold
UTC. A request's ``start_date`` / ``end_date`` with ``Z`` or an offset is an
aware datetime; PostgreSQL converts an aware value to the *session's* time zone
before comparing it with such a column, so on a server whose ``TimeZone`` is
not UTC the window moved by the server's offset and the route counted the wrong
rows. Every test here sets the session zone itself (the route's session runs
``SET TIME ZONE``), so a CI server that happens to run in UTC does not hide the
defect: the ``America/Los_Angeles`` and ``Asia/Kolkata`` cases fail on the old
code on any server, and the ``UTC`` case is the positive control. ``server-default`` sets nothing and
uses whatever the server runs in (UTC in CI, ``America/Los_Angeles`` on the
cluster the defect was reproduced on).

The window is 12:00:00 to 13:00:00 UTC on 2026-10-15. Four rows sit around it,
one second inside and one second outside each edge, in each of the three
tables; the answer is exactly the two inside.

Everything runs on PostgreSQL through the real routes.
"""

import uuid
from datetime import datetime, timedelta

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
    ErrorLog,
    MetricType,
    RawMetric,
)

pytestmark = [pytest.mark.integration, pytest.mark.regression]

SCHEMA = "test_experimentation"
SUMMARY_URL = "/api/v1/metrics/summary"
AGGREGATED_URL = "/api/v1/metrics/aggregated"
ERRORS_URL = "/api/v1/metrics/errors"

#: The same window written three ways: UTC, a half-hour offset east of it, and
#: a naive date (which means UTC).
WINDOWS = {
    "Z": ("2026-10-15T12:00:00Z", "2026-10-15T13:00:00Z"),
    "+05:30": ("2026-10-15T17:30:00+05:30", "2026-10-15T18:30:00+05:30"),
    "-07:00": ("2026-10-15T05:00:00-07:00", "2026-10-15T06:00:00-07:00"),
    "naive": ("2026-10-15T12:00:00", "2026-10-15T13:00:00"),
}

#: (label, stored UTC time, inside the window?)
ROWS = [
    ("before-1s", datetime(2026, 10, 15, 11, 59, 59), False),
    ("start+1s", datetime(2026, 10, 15, 12, 0, 1), True),
    ("end-1s", datetime(2026, 10, 15, 12, 59, 59), True),
    ("after+1s", datetime(2026, 10, 15, 13, 0, 1), False),
]


@pytest.fixture(autouse=True)
def _local_auth(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")


@pytest.fixture(params=["UTC", "America/Los_Angeles", "Asia/Kolkata", "server-default"])
def server_zone(request) -> str:
    return request.param


@pytest.fixture
def client(db_session, server_zone):
    engine = db_session.get_bind()
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    def override_get_db():
        session = factory()
        session.execute(text(f"SET search_path TO {SCHEMA}"))
        # What a server whose TimeZone is not UTC gives every session.
        if server_zone != "server-default":
            session.execute(text(f"SET TIME ZONE '{server_zone}'"))
        try:
            yield session
        finally:
            # SET TIME ZONE is session-level and close() returns the connection
            # to the pool as it is: put the server's own zone back first.
            session.rollback()
            session.execute(text("RESET TIME ZONE"))
            session.commit()
            session.close()

    app.dependency_overrides[deps.get_db] = override_get_db
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
    finally:
        app.dependency_overrides.pop(deps.get_db, None)


@pytest.fixture
def flag(db_session, make_feature_flag):
    flag = make_feature_flag(
        key=f"window-zone-{uuid.uuid4().hex[:8]}",
        status=FeatureFlagStatus.ACTIVE,
        rollout_percentage=100,
    )
    for label, when, _inside in ROWS:
        db_session.add(
            AggregatedMetric(
                metric_type=MetricType.FLAG_EVALUATION.value,
                period=AggregationPeriod.DAY.value,
                period_start=when,
                feature_flag_id=flag.id,
                count=1,
            )
        )
        db_session.add(
            RawMetric(
                metric_type=MetricType.FLAG_EVALUATION.value,
                feature_flag_id=flag.id,
                user_id=f"user-{label}",
                count=1,
                timestamp=when,
            )
        )
        db_session.add(
            ErrorLog(
                error_type="failed_evaluation",
                feature_flag_id=flag.id,
                message=label,
                request_data={"context": {}},
                timestamp=when,
            )
        )
    db_session.commit()
    return flag


def _get(client, user, url: str, params: dict):
    return client.get(
        url,
        params=params,
        headers={"Authorization": f"Bearer {create_local_access_token(user)}"},
    )


@pytest.mark.parametrize("zone", list(WINDOWS))
def test_the_summary_counts_exactly_the_rows_inside_the_window(
    client, viewer_user, flag, zone
):
    start, end = WINDOWS[zone]
    resp = _get(
        client,
        viewer_user,
        SUMMARY_URL,
        {"feature_flag_id": str(flag.id), "start_date": start, "end_date": end},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Two evaluation rows, two distinct users and two counted errors inside.
    assert body["total_evaluations"] == 2, body
    assert body["unique_users"] == 2, body
    assert body["error_rate"] == pytest.approx(100.0), body
    assert body["uncounted_error_count"] == 0, body


def test_the_summary_window_moved_one_second_leaves_a_row_out(
    client, viewer_user, flag
):
    """The 2 above is the edges being honoured: a start one second after the
    first inside row, sent with an offset, leaves exactly that row out."""
    resp = _get(
        client,
        viewer_user,
        SUMMARY_URL,
        {
            "feature_flag_id": str(flag.id),
            "start_date": "2026-10-15T17:30:02+05:30",
            "end_date": "2026-10-15T18:30:00+05:30",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_evaluations"] == 1, body
    assert body["unique_users"] == 1, body


@pytest.mark.parametrize("zone", list(WINDOWS))
def test_aggregated_returns_exactly_the_rows_inside_the_window(
    client, viewer_user, flag, zone
):
    start, end = WINDOWS[zone]
    resp = _get(
        client,
        viewer_user,
        AGGREGATED_URL,
        {"feature_flag_id": str(flag.id), "start_date": start, "end_date": end},
    )
    assert resp.status_code == 200, resp.text
    starts = sorted(r["period_start"] for r in resp.json())
    assert starts == ["2026-10-15T12:00:01", "2026-10-15T12:59:59"], starts


@pytest.mark.parametrize("zone", list(WINDOWS))
def test_errors_returns_exactly_the_rows_inside_the_window(
    client, viewer_user, flag, zone
):
    start, end = WINDOWS[zone]
    resp = _get(
        client,
        viewer_user,
        ERRORS_URL,
        {"feature_flag_id": str(flag.id), "start_date": start, "end_date": end},
    )
    assert resp.status_code == 200, resp.text
    messages = sorted(r["message"] for r in resp.json())
    assert messages == ["end-1s", "start+1s"], messages


def test_the_default_window_still_holds_the_last_30_days(
    client, db_session, viewer_user, make_feature_flag
):
    """Positive control: no dates sent, so the service's own default (30 days
    before now) applies. A row a minute old is counted and one 40 days old is
    not, on every server zone."""
    flag = make_feature_flag(
        key=f"window-default-{uuid.uuid4().hex[:8]}",
        status=FeatureFlagStatus.ACTIVE,
        rollout_percentage=100,
    )
    now = datetime.utcnow()
    for age in (timedelta(minutes=1), timedelta(days=40)):
        db_session.add(
            AggregatedMetric(
                metric_type=MetricType.FLAG_EVALUATION.value,
                period=AggregationPeriod.DAY.value,
                period_start=now - age,
                feature_flag_id=flag.id,
                count=1,
            )
        )
    db_session.commit()

    resp = _get(client, viewer_user, SUMMARY_URL, {"feature_flag_id": str(flag.id)})
    assert resp.status_code == 200, resp.text
    assert resp.json()["total_evaluations"] == 1, resp.text


#: Aware bounds whose UTC instant falls outside Python's datetime range (year 1
#: with an offset east of UTC, year 9999 with one west of it). Converting them
#: raised OverflowError, a 500 where the database compares them without error.
#: The offsets are under 16 hours, so the routes' offset guard lets them in.
OVERFLOW_START = "0001-01-01T00:00:00+05:00"
OVERFLOW_END = "9999-12-31T23:00:00-05:00"

#: (the bound sent, the window's other bound, the rows of ROWS it still holds)
OVERFLOW_WINDOWS = {
    "year-1-start": (
        {"start_date": OVERFLOW_START, "end_date": "2026-10-15T13:00:00Z"},
        ["before-1s", "end-1s", "start+1s"],
    ),
    "year-9999-end": (
        {"start_date": "2026-10-15T12:00:00Z", "end_date": OVERFLOW_END},
        ["after+1s", "end-1s", "start+1s"],
    ),
}


@pytest.mark.parametrize("window", list(OVERFLOW_WINDOWS))
def test_the_summary_answers_a_bound_outside_pythons_date_range(
    client, viewer_user, flag, window
):
    params, held = OVERFLOW_WINDOWS[window]
    resp = _get(
        client, viewer_user, SUMMARY_URL, {"feature_flag_id": str(flag.id), **params}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_evaluations"] == 3, body
    assert body["unique_users"] == 3, body
    assert body["error_rate"] == pytest.approx(100.0), body
    assert body["uncounted_error_count"] == 0, body
    assert set(body) == {
        "total_evaluations",
        "unique_users",
        "avg_latency",
        "rule_match_rate",
        "error_rate",
        "uncounted_error_count",
    }, body


@pytest.mark.parametrize("window", list(OVERFLOW_WINDOWS))
def test_aggregated_answers_a_bound_outside_pythons_date_range(
    client, viewer_user, flag, window
):
    params, held = OVERFLOW_WINDOWS[window]
    resp = _get(
        client, viewer_user, AGGREGATED_URL, {"feature_flag_id": str(flag.id), **params}
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 3, resp.text


@pytest.mark.parametrize("window", list(OVERFLOW_WINDOWS))
def test_errors_answers_a_bound_outside_pythons_date_range(
    client, viewer_user, flag, window
):
    params, held = OVERFLOW_WINDOWS[window]
    resp = _get(
        client, viewer_user, ERRORS_URL, {"feature_flag_id": str(flag.id), **params}
    )
    assert resp.status_code == 200, resp.text
    assert sorted(r["message"] for r in resp.json()) == held, resp.text
