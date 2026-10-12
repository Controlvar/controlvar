"""Which error reports count toward a flag's error rate and automatic rollback.

``POST /api/v1/tracking/errors`` and ``/errors/batch`` accept every valid API
key. A report counts toward the flag's error numbers only when it was sent
with a server-side key: a key carrying ``sdk:ruleset`` whose owner can change
feature flags when the report arrives. Every other report is stored with
``request_data["source"] = "client"`` and is not counted. Flag evaluations
that fail on the server keep counting.

Everything runs as in production: keys are created through
``POST /api/v1/api-keys``, ``deps.get_api_key`` is not replaced, and each
case ends with one real ``SafetyScheduler.check_feature_flags_safety`` tick
against the test database (no interval, no wall-clock wait), with global
automatic rollbacks on and a global ``default_metrics`` rule.

A case that expects no rollback also asserts that the check really ran
(healthy, the evaluation counted, the tick reported no failure): a monitor
that fails outright would otherwise pass it.
"""

import uuid
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import JSON, func, null, text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.core.config import settings
from backend.app.core.safety_scheduler import SafetyScheduler
from backend.app.core.security import create_local_access_token
from backend.app.main import app
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.metrics.metric import ErrorLog
from backend.app.models.safety import (
    FeatureFlagSafetyConfig,
    SafetyRollbackRecord,
    SafetySettings,
)
from backend.app.models.user import UserRole
from backend.app.services.feature_flag_service import FeatureFlagService
from backend.app.services.safety_service import SafetyService

pytestmark = [pytest.mark.integration, pytest.mark.regression]

SCHEMA = "test_experimentation"
KEYS_URL = "/api/v1/api-keys"
REPORT_URL = "/api/v1/tracking/errors"
BATCH_URL = "/api/v1/tracking/errors/batch"

#: ``request_data["source"]`` as stored: the values are part of the contract.
SERVER = "server"
CLIENT = "client"

KEPT = (FeatureFlagStatus.ACTIVE, 100, 0)
ROLLED_BACK = (FeatureFlagStatus.INACTIVE, 0, 1)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _local_auth(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")


@pytest.fixture
def session_factory(db_session):
    engine = db_session.get_bind()
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    def make():
        session = factory()
        session.execute(text(f"SET search_path TO {SCHEMA}"))
        return session

    return make


@pytest.fixture
def client(session_factory):
    def override_get_db():
        session = session_factory()
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
def global_rule(db_session):
    """Turn on automatic rollbacks with one global rule; restore afterwards."""
    row = db_session.query(SafetySettings).first()
    created = row is None
    previous = (
        None if created else (row.enable_automatic_rollbacks, row.default_metrics)
    )

    def apply(metric: str) -> None:
        current = db_session.query(SafetySettings).first()
        if current is None:
            current = SafetySettings()
            db_session.add(current)
        current.enable_automatic_rollbacks = True
        threshold = 0.10 if metric == "error_rate" else 0
        current.default_metrics = {
            metric: {"critical_threshold": threshold, "comparison_type": "greater_than"}
        }
        db_session.commit()

    yield apply

    db_session.rollback()
    row = db_session.query(SafetySettings).first()
    if row is not None:
        if created:
            db_session.delete(row)
        else:
            row.enable_automatic_rollbacks, row.default_metrics = previous
        db_session.commit()


def _bearer(user) -> dict:
    return {"Authorization": f"Bearer {create_local_access_token(user)}"}


def _make_key(client, user, scopes=None) -> str:
    body = {"name": f"k-{uuid.uuid4().hex[:6]}"}
    if scopes is not None:
        body["scopes"] = scopes
    resp = client.post(KEYS_URL, json=body, headers=_bearer(user))
    assert resp.status_code == 201, resp.text
    return resp.json()["key"]


def _active_flag(make_feature_flag) -> FeatureFlag:
    return make_feature_flag(
        key=f"report-source-{uuid.uuid4().hex[:8]}",
        status=FeatureFlagStatus.ACTIVE,
        rollout_percentage=100,
    )


def _evaluate(client, key: str, flag: FeatureFlag) -> None:
    resp = client.get(
        f"/api/v1/feature-flags/evaluate/{flag.key}",
        params={"user_id": "u1"},
        headers={"X-API-Key": key},
    )
    assert resp.status_code == 200, resp.text


def _report(client, key: str, flag: FeatureFlag, route: str, **extra) -> None:
    item = {"feature_flag_key": flag.key, "error_type": "crash", "message": "m"}
    item.update(extra)
    if route == "single":
        resp = client.post(REPORT_URL, json=item, headers={"X-API-Key": key})
        assert resp.status_code == 201, resp.text
    else:
        resp = client.post(
            BATCH_URL, json={"errors": [item]}, headers={"X-API-Key": key}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["success_count"] == 1, resp.text


async def _tick(session_factory) -> dict:
    scheduler = SafetyScheduler()
    with (
        patch("backend.app.core.safety_scheduler.SessionLocal", session_factory),
        patch.object(scheduler, "_notification_service"),
    ):
        return await scheduler.check_feature_flags_safety()


def _outcome(db_session, flag: FeatureFlag) -> tuple:
    db_session.expire_all()
    current = db_session.get(FeatureFlag, flag.id)
    records = (
        db_session.query(func.count(SafetyRollbackRecord.id))
        .filter(SafetyRollbackRecord.feature_flag_id == flag.id)
        .scalar()
    )
    return current.status, current.rollout_percentage, records


def _stored_sources(db_session, flag: FeatureFlag) -> list:
    db_session.expire_all()
    rows = db_session.query(ErrorLog).filter(ErrorLog.feature_flag_id == flag.id).all()
    return [(row.request_data or {}).get("source") for row in rows]


async def _assert_kept_by_a_check_that_ran(
    db_session, session_factory, flag, metric
) -> None:
    """No rollback, and the check really ran: it counted the evaluation and none
    of the reports."""
    result = await _tick(session_factory)
    assert result["items_failed"] == 0, result
    assert _outcome(db_session, flag) == KEPT

    db_session.expire_all()
    check = await SafetyService(db_session).check_feature_flag_safety(flag.id)
    assert check.is_healthy is True, check
    assert check.details["total_evaluations"] == 1
    assert check.details["error_count"] == 0
    assert check.details["uncounted_error_count"] == 1
    measured = {m.name: m for m in check.metrics}
    assert measured[metric].details["measured"] is True
    assert measured[metric].current_value == 0.0


async def _assert_rolled_back(db_session, session_factory, flag) -> None:
    result = await _tick(session_factory)
    assert result["items_failed"] == 0, result
    assert _outcome(db_session, flag) == ROLLED_BACK


# ---------------------------------------------------------------------------
# Reports that do not count
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("metric", ["error_rate", "error_count"])
@pytest.mark.parametrize("route", ["single", "batch"])
async def test_a_report_sent_with_a_key_without_the_scope_does_not_roll_back(
    client,
    db_session,
    session_factory,
    viewer_user,
    make_feature_flag,
    global_rule,
    metric,
    route,
):
    global_rule(metric)
    flag = _active_flag(make_feature_flag)
    key = _make_key(client, viewer_user)
    _evaluate(client, key, flag)
    _report(client, key, flag, route)

    await _assert_kept_by_a_check_that_ran(db_session, session_factory, flag, metric)


@pytest.mark.asyncio
async def test_a_scoped_key_whose_owner_can_no_longer_change_flags_does_not_count(
    client, db_session, session_factory, developer_user, make_feature_flag, global_rule
):
    global_rule("error_rate")
    flag = _active_flag(make_feature_flag)
    key = _make_key(client, developer_user, scopes=["sdk:ruleset"])
    developer_user.role = UserRole.VIEWER
    db_session.commit()

    _evaluate(client, key, flag)
    _report(client, key, flag, "single")

    await _assert_kept_by_a_check_that_ran(
        db_session, session_factory, flag, "error_rate"
    )


@pytest.mark.asyncio
async def test_the_request_body_cannot_make_a_report_count(
    client, db_session, session_factory, viewer_user, make_feature_flag, global_rule
):
    global_rule("error_rate")
    flag = _active_flag(make_feature_flag)
    key = _make_key(client, viewer_user)
    _evaluate(client, key, flag)
    _report(
        client,
        key,
        flag,
        "batch",
        metadata={"source": "server", "scope": "sdk:ruleset", "trusted": True},
        source="server",
        request_data={"source": "server"},
    )

    await _assert_kept_by_a_check_that_ran(
        db_session, session_factory, flag, "error_rate"
    )


@pytest.mark.asyncio
async def test_a_scoped_key_of_the_same_owner_does_not_make_another_key_count(
    client, db_session, session_factory, developer_user, make_feature_flag, global_rule
):
    global_rule("error_rate")
    flag = _active_flag(make_feature_flag)
    _make_key(client, developer_user, scopes=["sdk:ruleset"])
    plain = _make_key(client, developer_user)
    _evaluate(client, plain, flag)
    _report(client, plain, flag, "single")

    await _assert_kept_by_a_check_that_ran(
        db_session, session_factory, flag, "error_rate"
    )


# ---------------------------------------------------------------------------
# What still rolls a flag back
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["single", "batch"])
async def test_a_report_sent_with_a_server_side_key_rolls_back(
    client,
    db_session,
    session_factory,
    developer_user,
    make_feature_flag,
    global_rule,
    route,
):
    global_rule("error_rate")
    flag = _active_flag(make_feature_flag)
    key = _make_key(client, developer_user, scopes=["sdk:ruleset"])
    _evaluate(client, key, flag)
    _report(client, key, flag, route)

    await _assert_rolled_back(db_session, session_factory, flag)


@pytest.mark.asyncio
async def test_an_evaluation_that_fails_on_the_server_still_rolls_back(
    client,
    db_session,
    session_factory,
    viewer_user,
    make_feature_flag,
    global_rule,
    monkeypatch,
):
    """Evaluated with a key without the scope: the server's own failure counts."""
    global_rule("error_rate")
    flag = _active_flag(make_feature_flag)
    key = _make_key(client, viewer_user)

    def fails(self, *args, **kwargs):
        raise RuntimeError("planted evaluation failure")

    monkeypatch.setattr(FeatureFlagService, "_match_targeting_rule", fails)
    _evaluate(client, key, flag)
    monkeypatch.undo()

    db_session.expire_all()
    rows = db_session.query(ErrorLog).filter(ErrorLog.feature_flag_id == flag.id).all()
    assert [row.error_type for row in rows] == ["flag_evaluation_error"]
    assert "source" not in (rows[0].request_data or {})

    await _assert_rolled_back(db_session, session_factory, flag)


# ---------------------------------------------------------------------------
# The stored source, the check endpoint and stored rows
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", ["single", "batch"])
def test_the_stored_source_follows_the_presented_key(
    client,
    db_session,
    admin_user,
    developer_user,
    analyst_user,
    viewer_user,
    make_feature_flag,
    route,
):
    flag = _active_flag(make_feature_flag)
    sent = [
        (_make_key(client, admin_user, scopes=["sdk:ruleset"]), SERVER),
        (_make_key(client, developer_user, scopes=["sdk:ruleset"]), SERVER),
        (_make_key(client, developer_user), CLIENT),
        (_make_key(client, analyst_user), CLIENT),
        (_make_key(client, viewer_user), CLIENT),
    ]
    for key, _expected in sent:
        _report(client, key, flag, route)

    assert sorted(_stored_sources(db_session, flag)) == sorted(
        expected for _key, expected in sent
    )


def test_the_check_endpoint_shows_the_reports_it_does_not_count(
    client, db_session, viewer_user, developer_user, make_feature_flag
):
    """``GET .../check`` reads the same numbers as the monitor."""
    flag = _active_flag(make_feature_flag)
    plain = _make_key(client, viewer_user)
    server = _make_key(client, developer_user, scopes=["sdk:ruleset"])
    for i in range(4):
        client.get(
            f"/api/v1/feature-flags/evaluate/{flag.key}",
            params={"user_id": f"u{i}"},
            headers={"X-API-Key": plain},
        )
    for _ in range(3):
        _report(client, plain, flag, "single")
    _report(client, server, flag, "batch")

    resp = client.get(
        f"/api/v1/safety/feature-flags/{flag.id}/check", headers=_bearer(viewer_user)
    )
    assert resp.status_code == 200, resp.text
    details = resp.json()["details"]
    assert details["error_count"] == 1
    assert details["uncounted_error_count"] == 3
    assert details["total_evaluations"] == 4

    db_session.expire_all()
    metrics = SafetyService(db_session).get_error_metrics(db_session, flag.id)
    assert metrics["error_count"] == 1
    assert metrics["uncounted_error_count"] == 3
    assert metrics["error_rate"] == pytest.approx(0.25)
    assert metrics["error_types"] == {"crash": 1}


def test_the_check_shows_the_reports_it_does_not_count_when_monitoring_is_off(
    client, db_session, viewer_user, make_feature_flag
):
    flag = _active_flag(make_feature_flag)
    db_session.add(
        FeatureFlagSafetyConfig(feature_flag_id=flag.id, enabled=False, metrics={})
    )
    db_session.commit()
    plain = _make_key(client, viewer_user)
    _report(client, plain, flag, "batch")

    resp = client.get(
        f"/api/v1/safety/feature-flags/{flag.id}/check", headers=_bearer(viewer_user)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["is_healthy"] is True and body["metrics"] == []
    assert body["details"]["error_count"] == 0
    assert body["details"]["uncounted_error_count"] == 1


def test_rows_of_every_stored_shape_are_counted_as_documented(
    db_session, make_feature_flag
):
    """Rows written before this change keep their meaning.

    Error reports stored earlier all say ``"source": "client"``, so they stop
    counting; every row without that value (a failed evaluation, no
    ``request_data``, JSON ``null``) counts.
    """
    flag = _active_flag(make_feature_flag)
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
    db_session.commit()

    stored = dict(
        db_session.query(ErrorLog.error_type, func.jsonb_typeof(ErrorLog.request_data))
        .filter(ErrorLog.feature_flag_id == flag.id)
        .all()
    )
    assert stored["no_request_data"] is None
    assert stored["json_null"] == "null"

    metrics = SafetyService(db_session).get_error_metrics(db_session, flag.id)
    assert metrics["error_count"] == 4
    assert metrics["uncounted_error_count"] == 1
    assert metrics["error_types"] == {
        "server_report": 1,
        "failed_evaluation": 1,
        "no_request_data": 1,
        "json_null": 1,
    }
