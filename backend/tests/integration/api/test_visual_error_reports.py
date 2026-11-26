"""The visual script's error reports never count toward a feature flag (#1545).

The browser script (sdk/visual) reports a visual change it could not apply
with ``POST /api/v1/tracking/errors``, carrying exactly the keys
``tests/sdk-contract/visual-error-reports.json`` documents: ``error_type``,
``experiment_key``, ``message`` and ``metadata``, never ``feature_flag_key``.
Here every documented reason, an ``own_ui`` report (a change aimed at the
page's root element or the script's banner), and the two goal reports
(``goal_not_found``, ``goal_ambiguous``: a click goal whose selector matched
no element or several) are sent the way the script sends them, with a key
that has no scopes (a website's key), while a feature flag whose key equals
the experiment's exists: each report is stored with no feature flag and with
``source`` ``client``, and the flag's safety numbers stay at zero.

Keys are created through ``POST /api/v1/api-keys`` and ``deps.get_api_key``
is not replaced.
"""

import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from backend.app.api import deps
from backend.app.core.config import settings
from backend.app.core.security import create_local_access_token
from backend.app.main import app
from backend.app.models.experiment import ExperimentStatus
from backend.app.models.feature_flag import FeatureFlagStatus
from backend.app.models.metrics.metric import ErrorLog
from backend.app.services.safety_service import SafetyService

pytestmark = [pytest.mark.integration]

SCHEMA = "test_experimentation"
CONTRACT = (
    Path(__file__).resolve().parents[4]
    / "tests"
    / "sdk-contract"
    / "visual-error-reports.json"
)
REPORTS = json.loads(CONTRACT.read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _local_auth(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "local")
    monkeypatch.setattr(settings, "DEV_AUTH_BYPASS", False)
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")


@pytest.fixture
def client(db_session):
    factory = sessionmaker(
        bind=db_session.get_bind(), autocommit=False, autoflush=False
    )

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


def _website_key(client, user) -> str:
    resp = client.post(
        "/api/v1/api-keys",
        json={"name": f"Website: shop-{uuid.uuid4().hex[:6]}", "scopes": []},
        headers={"Authorization": f"Bearer {create_local_access_token(user)}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["key"]


GOAL_REPORTS = ["goal_not_found", "goal_ambiguous"]


def _reports(experiment_key: str) -> list:
    """One report per documented reason, one own_ui report and one of each goal
    report, exactly as the script builds them."""
    sent = [t for t in REPORTS["types"] if t["sent"]]
    assert [t["error_type"] for t in sent] == [
        "visual_change_not_applied",
        "own_ui",
    ] + GOAL_REPORTS
    assert [t["metadata_keys"] for t in sent[1:]] == [["index"]] * 3
    reasons = sorted(sent[0]["reasons"])
    assert len(reasons) == 7
    own_ui = {
        "error_type": "own_ui",
        "experiment_key": experiment_key,
        "message": "A visual change was not applied (own_ui).",
        "metadata": {"index": 0},
    }
    goals = [
        {
            "error_type": goal,
            "experiment_key": experiment_key,
            "message": f"A click goal counted nothing on this page view ({goal}).",
            "metadata": {"index": 1},
        }
        for goal in GOAL_REPORTS
    ]
    return (
        [own_ui]
        + goals
        + [
            {
                "error_type": "visual_change_not_applied",
                "experiment_key": experiment_key,
                "message": f"A visual change was not applied ({reason}).",
                "metadata": {
                    "index": None
                    if reason in ("invalid", "unsupported_operation")
                    else 0,
                    "reason": reason,
                },
            }
            for reason in reasons
        ]
    )


def test_the_report_keys_are_the_documented_four_and_no_flag_key():
    assert REPORTS["request_keys"] == [
        "error_type",
        "experiment_key",
        "message",
        "metadata",
    ]
    for report in _reports("any-key"):
        assert sorted(report) == sorted(REPORTS["request_keys"])
        assert "feature_flag_key" not in report


def test_reports_sent_with_a_website_key_never_count_toward_a_flag_keyed_like_the_experiment(
    client, db_session, viewer_user, make_experiment, make_feature_flag
):
    key = f"visual-{uuid.uuid4().hex[:8]}"
    experiment = make_experiment(
        name=f"Visual {key}", key=key, status=ExperimentStatus.ACTIVE
    )
    flag = make_feature_flag(
        key=key, status=FeatureFlagStatus.ACTIVE, rollout_percentage=100
    )
    api_key = _website_key(client, viewer_user)

    for report in _reports(key):
        resp = client.post(
            "/api/v1/tracking/errors", json=report, headers={"X-API-Key": api_key}
        )
        assert resp.status_code == 201, resp.text

    db_session.expire_all()
    rows = (
        db_session.query(ErrorLog)
        .filter(
            ErrorLog.error_type.in_(
                ["visual_change_not_applied", "own_ui"] + GOAL_REPORTS
            )
        )
        .all()
    )
    mine = [
        row
        for row in rows
        if (row.request_data or {}).get("experiment_id") == str(experiment.id)
        or (row.request_data or {}).get("experiment_key") == key
    ]
    assert len(mine) == 10, [row.request_data for row in rows]
    types = sorted(row.error_type for row in mine)
    assert (
        types == sorted(GOAL_REPORTS) + ["own_ui"] + ["visual_change_not_applied"] * 7
    )
    assert [row.feature_flag_id for row in mine] == [None] * 10
    assert [(row.request_data or {}).get("source") for row in mine] == ["client"] * 10

    metrics = SafetyService(db_session).get_error_metrics(db_session, flag.id)
    assert metrics["error_count"] == 0, metrics
    assert metrics["uncounted_error_count"] == 0, metrics
