"""Stored metric rows are returned with their ``metadata`` (#1625).

The metric response models declare a field ``metadata`` and read ORM rows with
``from_attributes=True``. On a SQLAlchemy model the attribute named
``metadata`` is the class-level ``MetaData()`` registry; the column is
``meta_data``. Without a real validation alias every stored row failed
validation and ``GET /api/v1/metrics/errors`` and ``/aggregated`` answered 500
(an empty list answered 200, which hid it).

Each route is read here with one stored row, and the one model no route uses
(``RawMetricResponse``) is validated from a stored row directly. The response
keeps the field name ``metadata``.
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
from backend.app.models.metrics.metric import (
    AggregatedMetric,
    AggregationPeriod,
    ErrorLog,
    MetricType,
    RawMetric,
)
from backend.app.schemas.metrics import RawMetricResponse

pytestmark = [pytest.mark.integration, pytest.mark.regression]

SCHEMA = "test_experimentation"


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


def _get(client, user, url, params):
    return client.get(
        url,
        params=params,
        headers={"Authorization": f"Bearer {create_local_access_token(user)}"},
    )


def test_the_error_log_returns_a_stored_row_with_its_metadata(
    client, db_session, viewer_user, make_feature_flag
):
    flag = make_feature_flag(key=f"err-meta-{uuid.uuid4().hex[:8]}")
    stored = {"browser": "Chrome", "attempt": 3, "nested": {"ok": True}}
    db_session.add(
        ErrorLog(
            error_type="rule_evaluation_error",
            feature_flag_id=flag.id,
            message="Failed to evaluate targeting rule",
            timestamp=datetime(2026, 10, 15, 12, 0, 0),
            meta_data=stored,
        )
    )
    db_session.commit()

    resp = _get(
        client,
        viewer_user,
        "/api/v1/metrics/errors",
        {"feature_flag_id": str(flag.id)},
    )
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["metadata"] == stored
    assert "meta_data" not in rows[0]


def test_the_error_log_returns_a_row_with_no_metadata(
    client, db_session, viewer_user, make_feature_flag
):
    flag = make_feature_flag(key=f"err-none-{uuid.uuid4().hex[:8]}")
    db_session.add(
        ErrorLog(
            error_type="rule_evaluation_error",
            feature_flag_id=flag.id,
            message="no metadata stored",
            timestamp=datetime(2026, 10, 15, 12, 0, 0),
        )
    )
    db_session.commit()

    resp = _get(
        client,
        viewer_user,
        "/api/v1/metrics/errors",
        {"feature_flag_id": str(flag.id)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()[0]["metadata"] is None


def test_aggregated_metrics_return_a_stored_row_with_its_metadata(
    client, db_session, viewer_user, make_feature_flag
):
    flag = make_feature_flag(key=f"agg-meta-{uuid.uuid4().hex[:8]}")
    stored = {"source": "nightly", "buckets": [1, 2, 3]}
    db_session.add(
        AggregatedMetric(
            metric_type=MetricType.FLAG_EVALUATION.value,
            period=AggregationPeriod.DAY.value,
            period_start=datetime(2026, 10, 15, 0, 0, 0),
            feature_flag_id=flag.id,
            count=7,
            meta_data=stored,
        )
    )
    db_session.commit()

    resp = _get(
        client,
        viewer_user,
        "/api/v1/metrics/aggregated",
        {"feature_flag_id": str(flag.id)},
    )
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["metadata"] == stored
    assert "meta_data" not in rows[0]


def test_a_stored_raw_metric_validates_with_its_metadata(db_session):
    """No route returns ``RawMetricResponse`` today; the model reads the same
    way, so a route that starts to would fail the same way."""
    stored = {"browser": "Chrome", "device": "mobile"}
    row = RawMetric(
        metric_type=MetricType.FLAG_EVALUATION.value,
        timestamp=datetime(2026, 10, 15, 12, 0, 0),
        value=4.5,
        count=1,
        meta_data=stored,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)

    response = RawMetricResponse.model_validate(row)
    assert response.metadata == stored
    assert response.model_dump()["metadata"] == stored
    # The field is still filled by its own name.
    assert RawMetricResponse(
        **{**response.model_dump(), "metadata": {"a": 1}}
    ).metadata == {"a": 1}
