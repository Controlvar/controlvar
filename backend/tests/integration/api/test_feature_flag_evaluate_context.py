"""
Integration tests for the SDK-facing flag evaluation endpoints with targeting
context:

* ``GET  /api/v1/feature-flags/evaluate/{key}?user_id=&context=<json>``
* ``POST /api/v1/feature-flags/evaluate/{key}`` ``{"user_id", "context"}``
* ``GET  /api/v1/feature-flags/user/{user_id}?context=<json>``

Rows created here (flags + the metric rows an evaluation records) are removed
again in the fixture teardown.
"""

import json
from urllib.parse import quote

import pytest

from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.models.metrics.metric import ErrorLog, RawMetric
from backend.tests.integration.helpers import unique_flag_key

AI_SEARCH_RULES = {
    "logical_operator": "AND",
    "groups": [
        {
            "logical_operator": "AND",
            "conditions": [
                {"attribute": "os", "operator": "equals", "value": "iOS"},
                {
                    "attribute": "os_version",
                    "operator": "semver_gte",
                    "value": "17.0.0",
                },
                {"attribute": "region", "operator": "equals", "value": "US"},
                {"attribute": "tier", "operator": "equals", "value": "premium"},
            ],
        }
    ],
}

IPHONE_15 = {"os": "iOS", "os_version": "17.4.0", "region": "US", "tier": "premium"}
PIXEL_7 = {"os": "Android", "os_version": "14.0.0", "region": "DE", "tier": "free"}


@pytest.fixture
def targeted_flag(db_session, make_feature_flag):
    """ACTIVE flag at 0% whose only way on is the AI-search dashboard rule."""
    flag = make_feature_flag(
        key=unique_flag_key("ctx-eval"),
        name="Context Eval",
        status=FeatureFlagStatus.ACTIVE,
        rollout_percentage=0,
        targeting_rules=AI_SEARCH_RULES,
    )
    yield flag
    db_session.rollback()
    db_session.query(RawMetric).filter(RawMetric.feature_flag_id == flag.id).delete()
    db_session.query(ErrorLog).filter(ErrorLog.feature_flag_id == flag.id).delete()
    db_session.query(FeatureFlag).filter(FeatureFlag.id == flag.id).delete()
    db_session.commit()


def _ctx(context: dict) -> str:
    return quote(json.dumps(context), safe="")


class TestGetEvaluateWithContext:
    def test_matching_context_enables_flag_with_reason(
        self, admin_client, targeted_flag
    ):
        resp = admin_client.get(
            f"/api/v1/feature-flags/evaluate/{targeted_flag.key}?user_id=dev-1&context={_ctx(IPHONE_15)}"
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {
            "key": targeted_flag.key,
            "enabled": True,
            "value": True,
            "value_type": "boolean",
            "config": None,
            "reason": "targeting_rule",
        }

    def test_non_matching_context_falls_back_to_rollout(
        self, admin_client, targeted_flag
    ):
        resp = admin_client.get(
            f"/api/v1/feature-flags/evaluate/{targeted_flag.key}",
            params={"user_id": "dev-2", "context": json.dumps(PIXEL_7)},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["enabled"] is False
        assert data["reason"] == "rollout"

    def test_no_context_keeps_existing_behaviour(self, admin_client, targeted_flag):
        resp = admin_client.get(
            f"/api/v1/feature-flags/evaluate/{targeted_flag.key}?user_id=dev-3"
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert set(data) == {
            "key",
            "enabled",
            "value",
            "value_type",
            "config",
            "reason",
        }
        assert data["value"] is data["enabled"]
        assert data["enabled"] is False
        assert data["reason"] == "rollout"

    @pytest.mark.parametrize(
        "bad", ["not-json", "[1,2]", '"string"', "42", "{bad json"]
    )
    def test_invalid_context_returns_422(self, admin_client, targeted_flag, bad):
        resp = admin_client.get(
            f"/api/v1/feature-flags/evaluate/{targeted_flag.key}",
            params={"user_id": "dev-4", "context": bad},
        )
        assert resp.status_code == 422, resp.text

    def test_empty_context_is_ignored(self, admin_client, targeted_flag):
        resp = admin_client.get(
            f"/api/v1/feature-flags/evaluate/{targeted_flag.key}?user_id=dev-5&context="
        )
        assert resp.status_code == 200, resp.text

    def test_unknown_key_returns_404(self, admin_client):
        resp = admin_client.get(
            f"/api/v1/feature-flags/evaluate/{unique_flag_key('missing')}",
            params={"user_id": "dev-6", "context": json.dumps(IPHONE_15)},
        )
        assert resp.status_code == 404


class TestPostEvaluate:
    def test_body_context_enables_flag(self, admin_client, targeted_flag):
        resp = admin_client.post(
            f"/api/v1/feature-flags/evaluate/{targeted_flag.key}",
            json={"user_id": "dev-7", "context": IPHONE_15},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {
            "key": targeted_flag.key,
            "enabled": True,
            "value": True,
            "value_type": "boolean",
            "config": None,
            "reason": "targeting_rule",
        }

    def test_body_without_context(self, admin_client, targeted_flag):
        resp = admin_client.post(
            f"/api/v1/feature-flags/evaluate/{targeted_flag.key}",
            json={"user_id": "dev-8"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["enabled"] is False
        assert resp.json()["reason"] == "rollout"

    def test_missing_user_id_returns_422(self, admin_client, targeted_flag):
        resp = admin_client.post(
            f"/api/v1/feature-flags/evaluate/{targeted_flag.key}",
            json={"context": IPHONE_15},
        )
        assert resp.status_code == 422

    def test_context_must_be_object(self, admin_client, targeted_flag):
        resp = admin_client.post(
            f"/api/v1/feature-flags/evaluate/{targeted_flag.key}",
            json={"user_id": "dev-9", "context": [1, 2]},
        )
        assert resp.status_code == 422

    def test_unknown_key_returns_404(self, admin_client):
        resp = admin_client.post(
            f"/api/v1/feature-flags/evaluate/{unique_flag_key('missing')}",
            json={"user_id": "dev-10", "context": IPHONE_15},
        )
        assert resp.status_code == 404

    def test_get_and_post_agree(self, admin_client, targeted_flag):
        get_resp = admin_client.get(
            f"/api/v1/feature-flags/evaluate/{targeted_flag.key}",
            params={"user_id": "dev-11", "context": json.dumps(IPHONE_15)},
        )
        post_resp = admin_client.post(
            f"/api/v1/feature-flags/evaluate/{targeted_flag.key}",
            json={"user_id": "dev-11", "context": IPHONE_15},
        )
        assert get_resp.json() == post_resp.json()


class TestUserFlagsWithContext:
    def test_context_query_param(self, admin_client, targeted_flag):
        on = admin_client.get(
            "/api/v1/feature-flags/user/dev-12",
            params={"context": json.dumps(IPHONE_15)},
        )
        assert on.status_code == 200, on.text
        assert on.json()[targeted_flag.key] is True

        off = admin_client.get(
            "/api/v1/feature-flags/user/dev-12",
            params={"context": json.dumps(PIXEL_7)},
        )
        assert off.status_code == 200, off.text
        assert off.json()[targeted_flag.key] is False

    def test_without_context(self, admin_client, targeted_flag):
        resp = admin_client.get("/api/v1/feature-flags/user/dev-13")
        assert resp.status_code == 200, resp.text
        assert resp.json()[targeted_flag.key] is False

    def test_invalid_context_returns_422(self, admin_client, targeted_flag):
        resp = admin_client.get(
            "/api/v1/feature-flags/user/dev-14", params={"context": "{"}
        )
        assert resp.status_code == 422
