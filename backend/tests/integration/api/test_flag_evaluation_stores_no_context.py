"""A server-side flag evaluation stores no targeting context in ``raw_metrics``.

The context is the user's attributes as the client sent them. An evaluation
writes one ``flag_evaluation`` row (and a ``latency`` row) with the flag, the
user id and the matched rule; its ``meta_data`` is ``NULL`` and no column of
any of the flag's rows holds anything from the context.

``MetricsService.record_flag_evaluation`` used to be handed the context and
pass it on as ``meta_data=``, a keyword ``RawMetricCreate`` does not have, so
pydantic dropped it. This pins the stored result: spelling that keyword
``metadata=`` would turn every test here red.

Covered through ``GET`` and ``POST /api/v1/feature-flags/evaluate/{flag_key}``
and ``GET /api/v1/feature-flags/user/{user_id}``, with a key without any
scope created through ``POST /api/v1/api-keys``; ``deps.get_api_key`` is not
replaced.
"""

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.core.config import settings
from backend.app.core.security import create_local_access_token
from backend.app.main import app
from backend.app.models.feature_flag import FeatureFlagStatus
from backend.app.models.metrics.metric import MetricType, RawMetric

pytestmark = [pytest.mark.integration, pytest.mark.regression]

SCHEMA = "test_experimentation"
MARKER = "ctx-marker-7f3a"
CONTEXT = {"email": f"{MARKER}@example.com", "plan": MARKER, "app": {"v": MARKER}}
#: Every column of a flag's ``raw_metrics`` rows, as one text per row.
ROWS_AS_TEXT = text(
    f"SELECT r::text FROM {SCHEMA}.raw_metrics r WHERE r.feature_flag_id = :id"
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


@pytest.fixture
def headers(client, viewer_user) -> dict:
    resp = client.post(
        "/api/v1/api-keys",
        json={"name": f"k-{uuid.uuid4().hex[:6]}"},
        headers={"Authorization": f"Bearer {create_local_access_token(viewer_user)}"},
    )
    assert resp.status_code == 201, resp.text
    return {"X-API-Key": resp.json()["key"]}


def _assert_no_context_stored(db_session, flag):
    """One ``flag_evaluation`` row of *flag*, with no metadata, and no row of
    the flag holding the marker in any column."""
    db_session.expire_all()
    stored = (
        db_session.query(RawMetric.meta_data)
        .filter(
            RawMetric.feature_flag_id == flag.id,
            RawMetric.metric_type == MetricType.FLAG_EVALUATION.value,
        )
        .all()
    )
    assert [row.meta_data for row in stored] == [None]
    rows = db_session.execute(ROWS_AS_TEXT, {"id": flag.id}).scalars().all()
    # The flag_evaluation row and its latency row: the probe sees them.
    assert len(rows) == 2, rows
    assert [row for row in rows if MARKER in row] == []


@pytest.mark.parametrize(
    ("status", "rollout", "enabled"),
    [
        (FeatureFlagStatus.ACTIVE, 100, True),
        (FeatureFlagStatus.ACTIVE, 0, False),
        (FeatureFlagStatus.INACTIVE, 100, False),
    ],
    ids=["on", "outside-rollout", "inactive"],
)
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_an_evaluation_stores_nothing_from_the_context(
    client, db_session, make_feature_flag, headers, method, status, rollout, enabled
):
    flag = make_feature_flag(
        key=f"noctx-{uuid.uuid4().hex[:8]}",
        status=status,
        rollout_percentage=rollout,
    )
    url = f"/api/v1/feature-flags/evaluate/{flag.key}"
    if method == "GET":
        resp = client.get(
            url,
            params={"user_id": "u-1", "context": json.dumps(CONTEXT)},
            headers=headers,
        )
    else:
        resp = client.post(
            url, json={"user_id": "u-1", "context": CONTEXT}, headers=headers
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["enabled"] is enabled

    _assert_no_context_stored(db_session, flag)


def test_every_flag_evaluated_for_a_user_stores_nothing_from_the_context(
    client, db_session, make_feature_flag, headers
):
    on = make_feature_flag(
        key=f"noctx-on-{uuid.uuid4().hex[:8]}",
        status=FeatureFlagStatus.ACTIVE,
        rollout_percentage=100,
    )
    off = make_feature_flag(
        key=f"noctx-off-{uuid.uuid4().hex[:8]}",
        status=FeatureFlagStatus.ACTIVE,
        rollout_percentage=0,
    )
    resp = client.get(
        "/api/v1/feature-flags/user/u-1",
        params={"context": json.dumps(CONTEXT)},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()[on.key] is True
    assert resp.json()[off.key] is False

    _assert_no_context_stored(db_session, on)
    _assert_no_context_stored(db_session, off)
