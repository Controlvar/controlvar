"""What a submitted wizard draft creates, through the wizard routes.

A draft of each type is walked through ``PUT /api/v1/wizard/drafts/{id}/step``
and submitted; the experiment it creates is read back with
``GET /api/v1/experiments/{id}``:

* ``primary_metric_id`` and ``guardrail_metric_ids`` are the event names the
  experiment's metrics count, the first as the primary metric;
* an ``ab`` draft gets Control and Variant A at 50% each, a ``multivariate``
  draft three variants at 33/33/34, and a ``feature_flag_rollout`` draft a
  held-back control configured ``{"enabled": false}`` and a treatment
  configured ``{"enabled": true}``, at 50% each.
"""

from __future__ import annotations

import pytest

from backend.app.models.user import UserRole
from backend.tests.integration.api.route_helpers import (
    api_client,
    auth,
    local_sign_in,
    make_user,
)

pytestmark = [pytest.mark.integration, pytest.mark.regression]

EXPECTED_VARIANTS = {
    "ab": [("Control", True, 50, None), ("Variant A", False, 50, None)],
    "multivariate": [
        ("Control", True, 33, None),
        ("Variant A", False, 33, None),
        ("Variant B", False, 34, None),
    ],
    "feature_flag_rollout": [
        ("Control", True, 50, {"enabled": False}),
        ("Treatment", False, 50, {"enabled": True}),
    ],
}


@pytest.fixture(autouse=True)
def _sign_in(monkeypatch):
    local_sign_in(monkeypatch)


def _step(client, user, draft_id, step, data):
    resp = client.put(
        f"/api/v1/wizard/drafts/{draft_id}/step",
        json={"step": step, "data": data},
        headers=auth(user),
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize("experiment_type", sorted(EXPECTED_VARIANTS))
def test_a_submitted_draft_creates_the_documented_experiment(
    db_session, experiment_type
):
    with api_client(db_session) as client:
        user = make_user(db_session, UserRole.DEVELOPER, tag="wizard")
        created = client.post(
            "/api/v1/wizard/drafts",
            json={"experiment_type": experiment_type},
            headers=auth(user),
        )
        assert created.status_code in (200, 201), created.text
        draft_id = created.json()["id"]
        _step(
            client, user, draft_id, "choose_type", {"experiment_type": experiment_type}
        )
        _step(
            client,
            user,
            draft_id,
            "define_hypothesis",
            {
                "name": f"Wizard {experiment_type} {draft_id[:6]}",
                "hypothesis": "A green button increases checkout completion",
                "primary_metric_id": "checkout_completed",
                "guardrail_metric_ids": ["page_error", "refund_requested"],
            },
        )
        _step(client, user, draft_id, "targeting", {"targeting_rules": []})
        _step(
            client, user, draft_id, "sample_size", {"baseline_rate": 0.12, "mde": 0.05}
        )
        submitted = client.post(
            f"/api/v1/wizard/drafts/{draft_id}/submit", headers=auth(user)
        )
        assert submitted.status_code == 200, submitted.text
        assert submitted.json()["success"] is True, submitted.json()

        experiment = client.get(
            f"/api/v1/experiments/{submitted.json()['experiment_id']}",
            headers=auth(user),
        ).json()
        assert experiment["status"].lower() == "draft"
        metrics = {m["event_name"]: m["is_primary"] for m in experiment["metrics"]}
        assert metrics == {
            "checkout_completed": True,
            "page_error": False,
            "refund_requested": False,
        }
        variants = sorted(
            (
                v["name"],
                v["is_control"],
                v["traffic_allocation"],
                v.get("configuration") or None,
            )
            for v in experiment["variants"]
        )
        assert variants == sorted(EXPECTED_VARIANTS[experiment_type])
