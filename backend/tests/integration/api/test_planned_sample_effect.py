"""The Planned sample plans with the effect saved with the experiment (#1257).

``GET /api/v1/results/{id}/sample-size`` used to plan every experiment for a
5% lift unless the request sent ``mde``, and the results page never sends it:
an experiment created for a 10% lift, run to the 31,309 users per variant that
lift needs, showed 121,833 required and "Not yet" beside its winner.

Now, with no ``mde`` sent, the plan uses the primary metric's saved
``expected_effect`` when it is above 0 and below 1, and ``mde_source`` says
which effect was used: ``request``, ``experiment`` or ``default`` (0.05).

Rows created here are deleted in fixture teardown because the shared test
database is not truncated between tests.
"""

import math
import uuid
from typing import Dict, List, Optional, Tuple

import pytest
from sqlalchemy import text

from backend.app.models.assignment import Assignment
from backend.app.models.event import Event
from backend.app.models.experiment import (
    ExperimentStatus,
    ExperimentType,
    Metric,
    MetricType,
    Variant,
)
from backend.app.models.user import UserRole
from backend.app.services.power_calculator_service import (
    TREATMENT_RATE_CEILING_MESSAGE,
    sample_size_two_proportions,
)
from backend.tests.integration.api.route_helpers import (
    SCHEMA,
    api_client,
    auth,
    create_experiment,
    local_sign_in,
    make_user,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

# The V2 story: 1,569 of 31,309 control users converted (5.01%), 1,781 of
# 31,309 treatment users.
V2_ARMS = [("control", True, 31_309, 1_569), ("treatment", False, 31_309, 1_781)]


@pytest.fixture
def seed(db_session, make_experiment):
    """
    Build an experiment whose metrics are ``metrics`` (name, is_primary,
    expected_effect) and whose arms are (name, is_control, assigned users,
    converting users). Assignments and events are inserted in bulk.
    """
    made = []

    def _seed(
        arms: List[Tuple[str, bool, int, int]],
        metrics: Optional[List[Tuple[str, bool, Optional[float]]]] = None,
    ):
        suffix = uuid.uuid4().hex[:8]
        exp = make_experiment(
            name=f"Planned effect {suffix}",
            key=f"planned-effect-{suffix}",
            status=ExperimentStatus.ACTIVE,
            experiment_type=ExperimentType.A_B,
        )
        made.append(exp)
        rows = [
            Variant(
                experiment_id=exp.id,
                name=name,
                is_control=is_control,
                traffic_allocation=50,
            )
            for name, is_control, _, _ in arms
        ]
        for name, is_primary, effect in (
            metrics if metrics is not None else [("Purchase", True, None)]
        ):
            rows.append(
                Metric(
                    experiment_id=exp.id,
                    name=name,
                    event_name="purchase",
                    metric_type=MetricType.CONVERSION,
                    is_primary=is_primary,
                    expected_effect=effect,
                )
            )
        db_session.add_all(rows)
        db_session.commit()
        db_session.refresh(exp)
        by_name = {v.name: v for v in exp.variants}
        for name, _, assigned, converting in arms:
            params = {
                "e": str(exp.id),
                "v": str(by_name[name].id),
                "prefix": f"pe-{suffix}-{name}-",
            }
            db_session.execute(
                text(
                    f"INSERT INTO {SCHEMA}.assignments "
                    "(id, experiment_id, variant_id, user_id, created_at, updated_at) "
                    "SELECT gen_random_uuid(), CAST(:e AS uuid), CAST(:v AS uuid), "
                    ":prefix || g, now(), now() FROM generate_series(1, :n) g"
                ),
                {**params, "n": assigned},
            )
            if converting:
                db_session.execute(
                    text(
                        f"INSERT INTO {SCHEMA}.events "
                        "(id, event_type, event_name, user_id, experiment_id, "
                        "variant_id, value, created_at, updated_at) "
                        "SELECT gen_random_uuid(), 'purchase', 'purchase', "
                        ":prefix || g, CAST(:e AS uuid), CAST(:v AS uuid), 1.0, "
                        "'2026-10-01T00:00:00+00:00', now() "
                        "FROM generate_series(1, :n) g"
                    ),
                    {**params, "n": converting},
                )
        db_session.commit()
        return exp

    yield _seed
    db_session.rollback()
    for exp in made:
        for model in (Event, Assignment):
            db_session.query(model).filter(model.experiment_id == exp.id).delete(
                synchronize_session=False
            )
    db_session.commit()


def _get(client, exp_id, expect: int = 200, **params) -> Dict:
    response = client.get(f"/api/v1/results/{exp_id}/sample-size", params=params)
    assert response.status_code == expect, response.text
    return response.json()


def _plan(baseline: float, mde: float) -> int:
    return sample_size_two_proportions(baseline, baseline * (1 + mde), 0.05, 0.8, True)


@pytest.mark.regression
def test_the_planned_sample_uses_the_primary_metrics_planned_effect(client, seed):
    """v0.34.2 answered 121,833 (a 5% lift): the saved 10% was never read."""
    exp = seed(V2_ARMS, metrics=[("Purchase", True, 0.10)])
    data = _get(client, exp.id)
    assert (
        data["required_sample_size_per_variant"],
        data["mde"],
        data["baseline_source"],
    ) == (31_159, 0.10, "observed")
    assert data["mde_source"] == "experiment"
    assert data["baseline_rate"] == 1_569 / 31_309
    assert data["current_sample_size_per_variant"] == 31_309
    assert data["is_adequate"] is True
    assert data["achieved_power"] > 0.80
    assert data["mde_absolute"] == pytest.approx(0.10 * 1_569 / 31_309)
    assert _plan(1_569 / 31_309, 0.10) == 31_159


def test_with_no_saved_effect_the_plan_is_for_the_default_and_says_so(client, seed):
    exp = seed(V2_ARMS, metrics=[("Purchase", True, None)])
    data = _get(client, exp.id)
    assert (data["required_sample_size_per_variant"], data["mde"]) == (121_833, 0.05)
    assert data["mde_source"] == "default"
    assert data["is_adequate"] is False


def test_a_requested_mde_wins_over_the_saved_one(client, seed):
    exp = seed(
        [("control", True, 50, 6), ("treatment", False, 40, 4)],
        metrics=[("Purchase", True, 0.10)],
    )
    data = _get(client, exp.id, mde=0.2)
    assert (data["mde"], data["mde_source"]) == (0.2, "request")
    assert data["required_sample_size_per_variant"] == _plan(0.12, 0.2)


def test_only_the_primary_metrics_effect_is_a_plan(client, seed):
    """A secondary metric's effect is not read, even when the primary has none."""
    exp = seed(
        [("control", True, 50, 6), ("treatment", False, 40, 4)],
        metrics=[("Purchase", True, None), ("Add to cart", False, 0.10)],
    )
    data = _get(client, exp.id)
    assert (data["mde"], data["mde_source"]) == (0.05, "default")
    assert data["metric_name"] == "Purchase"


def test_with_no_primary_metric_the_effect_of_the_metric_planned_on_is_not_used(
    client, seed
):
    """The plan falls back to the first metric by name; its effect is not a plan."""
    exp = seed(
        [("control", True, 50, 6), ("treatment", False, 40, 4)],
        metrics=[("Add to cart", False, 0.10), ("Purchase", False, 0.20)],
    )
    data = _get(client, exp.id)
    assert data["metric_name"] == "Add to cart"
    assert (data["mde"], data["mde_source"]) == (0.05, "default")


def test_with_two_primaries_the_plan_reads_the_one_it_plans_on(client, seed):
    """Primaries order by name: 'Add to cart' is planned on, with its 0.2."""
    exp = seed(
        [("control", True, 50, 6), ("treatment", False, 40, 4)],
        metrics=[("Purchase", True, 0.10), ("Add to cart", True, 0.20)],
    )
    data = _get(client, exp.id)
    assert data["metric_name"] == "Add to cart"
    assert (data["mde"], data["mde_source"]) == (0.2, "experiment")
    metric_ids = {m.name: str(m.id) for m in exp.metric_definitions}
    assert data["metric_id"] == metric_ids["Add to cart"]


@pytest.mark.regression
@pytest.mark.parametrize(
    "stored", [0.0, 1.0, 1.5, -0.005, math.nan, math.inf, -math.inf]
)
def test_an_unusable_saved_effect_falls_back_to_the_default(client, seed, stored):
    """Stored outside (0, 1): 200 with the default, never a 500."""
    exp = seed(
        [("control", True, 50, 6), ("treatment", False, 40, 4)],
        metrics=[("Purchase", True, stored)],
    )
    data = _get(client, exp.id)
    assert (data["mde"], data["mde_source"]) == (0.05, "default")
    assert data["required_sample_size_per_variant"] == _plan(0.12, 0.05)


def test_a_saved_effect_with_no_traffic_still_reports_its_source(client, seed):
    exp = seed(
        [("control", True, 0, 0), ("treatment", False, 0, 0)],
        metrics=[("Purchase", True, 0.10)],
    )
    data = _get(client, exp.id)
    assert data["unavailable_reason"] == "no_control_data"
    assert (data["mde"], data["mde_source"]) == (0.10, "experiment")


def test_a_requested_baseline_the_saved_effect_raises_past_100_percent_is_refused(
    client, seed
):
    """The ceiling is checked against the effect the plan would use."""
    exp = seed(
        [("control", True, 0, 0), ("treatment", False, 0, 0)],
        metrics=[("Purchase", True, 0.5)],
    )
    data = _get(client, exp.id, expect=422, baseline_conversion_rate=0.7)
    assert data == {"detail": TREATMENT_RATE_CEILING_MESSAGE}
    # Without the saved effect, the default 5% keeps 0.7 below 100%.
    assert _get(client, exp.id, baseline_conversion_rate=0.7, mde=0.05)["mde"] == 0.05


def test_an_unknown_experiment_with_a_bad_baseline_is_404(client):
    """The experiment is loaded before the ceiling is checked: 404, not 422."""
    data = _get(client, uuid.uuid4(), expect=404, baseline_conversion_rate=0.6, mde=0.8)
    assert data == {"detail": "Experiment not found"}


# --- through the API: create and clone -----------------------------------------


@pytest.fixture
def api(db_session, monkeypatch):
    local_sign_in(monkeypatch)
    with api_client(db_session) as c:
        yield c


def test_an_effect_sent_at_create_and_kept_by_clone_drives_the_plan(api, db_session):
    owner = make_user(db_session, UserRole.ADMIN, superuser=True, tag="pe1257")
    suffix = uuid.uuid4().hex[:8]
    experiment = create_experiment(
        api,
        owner,
        metrics=[
            {
                "name": "Purchase",
                "event_name": f"purchase_{suffix}",
                "metric_type": "conversion",
                "is_primary": True,
                "expected_effect": 0.10,
            }
        ],
    )
    url = f"/api/v1/results/{experiment['id']}/sample-size"
    resp = api.get(url, params={"baseline_conversion_rate": 0.05}, headers=auth(owner))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # The wizard's own number for a 5% baseline and a 10% lift.
    assert (body["required_sample_size_per_variant"], body["mde_source"]) == (
        31_234,
        "experiment",
    )

    resp = api.post(
        f"/api/v1/experiments/{experiment['id']}/clone", headers=auth(owner)
    )
    assert resp.status_code in (200, 201), resp.text
    clone = resp.json()
    resp = api.get(
        f"/api/v1/results/{clone['id']}/sample-size",
        params={"baseline_conversion_rate": 0.05},
        headers=auth(owner),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["mde_source"] == "experiment"
    assert resp.json()["mde"] == 0.10


def test_a_put_that_resends_metrics_without_the_effect_clears_it(api, db_session):
    """PUT replaces the metrics: resent as read, the effect stays; resent
    without it, the plan is for the default and says so."""
    owner = make_user(db_session, UserRole.ADMIN, superuser=True, tag="pe1257put")
    suffix = uuid.uuid4().hex[:8]
    metric = {
        "name": "Purchase",
        "event_name": f"purchase_{suffix}",
        "metric_type": "conversion",
        "is_primary": True,
    }
    experiment = create_experiment(
        api, owner, metrics=[{**metric, "expected_effect": 0.10}]
    )
    url = f"/api/v1/experiments/{experiment['id']}"
    plan_url = f"/api/v1/results/{experiment['id']}/sample-size"

    def source() -> Tuple[float, str]:
        resp = api.get(plan_url, headers=auth(owner))
        assert resp.status_code == 200, resp.text
        return resp.json()["mde"], resp.json()["mde_source"]

    resp = api.put(
        url,
        json={"metrics": [{**metric, "expected_effect": 0.10}]},
        headers=auth(owner),
    )
    assert resp.status_code == 200, resp.text
    assert source() == (0.10, "experiment")

    resp = api.put(url, json={"metrics": [metric]}, headers=auth(owner))
    assert resp.status_code == 200, resp.text
    assert source() == (0.05, "default")
