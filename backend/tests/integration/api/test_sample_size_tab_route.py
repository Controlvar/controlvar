"""The Sample Size tab's route, ``GET /api/v1/results/{id}/sample-size``.

* it plans from the control's rate as it is now, while ``GET /results/{id}``
  can serve a cached answer for up to five minutes: after more conversions,
  the tab's rate is newer than the one the results show;
* the power-analysis page's worked example: a 12% baseline and a 5% lift plan
  47,036 users per variant, and with 20,000 users in the smallest variant that
  is 43% of the plan and a power of 45% to detect the lift.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from backend.app.models.user import UserRole
from backend.tests.integration.api.route_helpers import (
    SCHEMA,
    api_client,
    api_key,
    assign,
    auth,
    create_experiment,
    fake_results_cache,
    local_sign_in,
    make_user,
    start,
    track,
)

pytestmark = [pytest.mark.integration, pytest.mark.regression]


@pytest.fixture(autouse=True)
def _sign_in(monkeypatch):
    local_sign_in(monkeypatch)


@pytest.fixture
def client(db_session):
    with api_client(db_session) as c:
        yield c


def _control_rate(results) -> float:
    (metric,) = results["metrics"]
    control = next(v for v in metric["variants"] if v["is_control"])
    return control["conversions"] / control["sample_size"]


def test_the_tab_is_not_cached_and_the_results_are(client, db_session, monkeypatch):
    fake_results_cache(monkeypatch)
    owner = make_user(db_session, UserRole.ADMIN, superuser=True, tag="tab")
    experiment = create_experiment(client, owner)
    start(client, experiment, owner)
    key = api_key(client, owner)
    event = experiment["metrics"][0]["event_name"]
    control_id = next(v["id"] for v in experiment["variants"] if v["is_control"])

    control_users = []
    for i in range(40):
        user_id = f"tab-{uuid.uuid4().hex[:6]}-{i}"
        if str(assign(client, key, experiment["key"], user_id)["variant_id"]) == str(
            control_id
        ):
            control_users.append(user_id)
    assert len(control_users) >= 6, control_users
    track(client, key, control_users[0], event, experiment_key=experiment["key"])

    url = f"/api/v1/results/{experiment['id']}"
    before = client.get(url, headers=auth(owner)).json()
    tab = client.get(f"{url}/sample-size", headers=auth(owner)).json()
    assert tab["baseline_source"] == "observed"
    assert tab["baseline_rate"] == pytest.approx(_control_rate(before))

    for user_id in control_users[1:4]:
        track(client, key, user_id, event, experiment_key=experiment["key"])
    newer = client.get(f"{url}/sample-size", headers=auth(owner)).json()
    assert newer["baseline_rate"] == pytest.approx(4 / len(control_users))
    cached = client.get(url, headers=auth(owner)).json()
    assert _control_rate(cached) == pytest.approx(1 / len(control_users))
    assert newer["baseline_rate"] > _control_rate(cached)


def test_twenty_thousand_users_are_43_percent_of_the_plan_at_45_percent_power(
    client, db_session
):
    owner = make_user(db_session, UserRole.ADMIN, superuser=True, tag="tab20k")
    experiment = create_experiment(client, owner)
    start(client, experiment, owner)
    try:
        for variant in experiment["variants"]:
            db_session.execute(
                text(
                    f"INSERT INTO {SCHEMA}.assignments "
                    "(id, experiment_id, variant_id, user_id, created_at, updated_at) "
                    "SELECT gen_random_uuid(), CAST(:e AS uuid), CAST(:v AS uuid), "
                    "'tab20k-' || :v || '-' || g, now(), now() "
                    "FROM generate_series(1, 20000) g"
                ),
                {"e": experiment["id"], "v": variant["id"]},
            )
        db_session.commit()
        resp = client.get(
            f"/api/v1/results/{experiment['id']}/sample-size",
            params={"baseline_conversion_rate": 0.12, "mde": 0.05},
            headers=auth(owner),
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["required_sample_size_per_variant"] == 47_036
        assert body["current_sample_size_per_variant"] == 20_000
        progress = body["current_sample_size_per_variant"] / 47_036
        assert round(100 * progress) == 43
        assert round(100 * body["achieved_power"]) == 45
    finally:
        db_session.rollback()
        db_session.execute(
            text(
                f"DELETE FROM {SCHEMA}.assignments WHERE experiment_id = CAST(:e AS uuid)"
            ),
            {"e": experiment["id"]},
        )
        db_session.commit()
