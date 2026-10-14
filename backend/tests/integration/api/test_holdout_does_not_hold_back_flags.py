"""The global holdout keeps users out of experiments, not out of feature flags.

With an active global holdout of 20% (the most it can be), the users
``POST /api/v1/tracking/assign`` holds out (the control, with
``"assigned": false, "reason": "holdout"``) evaluate a flag at 100% as on,
through ``GET /api/v1/feature-flags/evaluate/{key}`` with the same API key:
once a winner ships behind a flag, the held-out users get it too.
"""

from __future__ import annotations

import uuid

import pytest

from backend.app.models.global_holdout import GlobalHoldout
from backend.app.models.user import UserRole
from backend.tests.integration.api.route_helpers import (
    api_client,
    api_key,
    auth,
    create_experiment,
    local_sign_in,
    make_user,
    start,
)
from backend.tests.integration.api.test_tracking_assign_batch import _parked_holdouts

pytestmark = [pytest.mark.integration, pytest.mark.regression]


@pytest.fixture
def a_holdout(db_session):
    with _parked_holdouts(db_session):
        holdout = GlobalHoldout(
            name=f"flags-holdout-{uuid.uuid4().hex[:8]}",
            description="A fifth is held out",
            holdout_percentage=20,
            is_active=True,
        )
        db_session.add(holdout)
        db_session.commit()
        try:
            yield holdout
        finally:
            db_session.rollback()
            db_session.query(GlobalHoldout).filter(
                GlobalHoldout.id == holdout.id
            ).update({"is_active": False}, synchronize_session=False)
            db_session.commit()


def test_held_out_users_still_get_a_shipped_flag(db_session, monkeypatch, a_holdout):
    local_sign_in(monkeypatch)
    with api_client(db_session) as client:
        owner = make_user(db_session, UserRole.ADMIN, superuser=True, tag="holdflag")
        experiment = create_experiment(client, owner)
        start(client, experiment, owner)
        suffix = uuid.uuid4().hex[:8]
        flag = client.post(
            "/api/v1/feature-flags/",
            json={
                "key": f"shipped-{suffix}",
                "name": "Shipped winner",
                "rollout_percentage": 100,
                "is_active": True,
            },
            headers=auth(owner),
        )
        assert flag.status_code in (200, 201), flag.text
        key = api_key(client, owner)

        held_out = 0
        for i in range(100):
            user_id = f"held-{suffix}-{i}"
            assigned = client.post(
                "/api/v1/tracking/assign",
                json={"experiment_key": experiment["key"], "user_id": user_id},
                headers=key,
            )
            assert assigned.status_code == 200, assigned.text
            if assigned.json()["reason"] != "holdout":
                continue
            assert assigned.json()["assigned"] is False
            held_out += 1

            evaluated = client.get(
                f"/api/v1/feature-flags/evaluate/shipped-{suffix}",
                params={"user_id": user_id},
                headers=key,
            )
            assert evaluated.status_code == 200, evaluated.text
            assert evaluated.json()["enabled"] is True
        assert held_out >= 5, held_out
