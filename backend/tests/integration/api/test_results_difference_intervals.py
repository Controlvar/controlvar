"""
``GET /api/v1/results/{id}`` gives each treatment its difference from the
control with an interval, the relative change's interval and the incremental
impact (#1496), through the real routes and a real database.

One experiment, 500 users in the control and 400 in the treatment, assigned
through the ORM, with two metrics:

* a conversion metric ``signup``: no control user converts and 12 treatment
  users do.  The control's rate is 0, so the relative change and its interval
  are null while the difference, its interval and the impact are there, and
  the response is plain JSON (no NaN, no Infinity);
* a metric analysed as a mean (each type in ``MEAN_METRIC_TYPES``): 100
  control users and 120 treatment users each send one ``purchase`` with
  value 1.0, so its value total and its event count are the same per user.

The expected numbers come from statsmodels (Newcombe's interval) and from the
builders on the same per-user values, which ``test_difference_intervals.py``
holds to independent oracles.

Rows created here are deleted in fixture teardown because the shared test
database is not truncated between tests.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from statsmodels.stats.proportion import confint_proportions_2indep

from backend.app.models.analysis_snapshot import AnalysisSnapshot
from backend.app.models.assignment import Assignment
from backend.app.models.event import Event
from backend.app.models.experiment import (
    ExperimentStatus,
    ExperimentType,
    Metric,
    MetricType,
    Variant,
)
from backend.app.services.sufficient_stats_analysis import (
    DIFFERENCE_FIELDS,
    MEAN_METRIC_TYPES,
    BinomialVariant,
    per_user_mean_metric_result,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

USERS = {"control": 500, "treatment": 400}
SIGNUPS = {"control": 0, "treatment": 12}
PURCHASES = {"control": 100, "treatment": 120}


def _user(exp, arm, i):
    return f"rdi-{exp.key}-{arm}-{i}"


def _cleanup(db_session, exp):
    db_session.rollback()
    for model in (AnalysisSnapshot, Event, Assignment):
        db_session.query(model).filter(model.experiment_id == exp.id).delete(
            synchronize_session=False
        )
    db_session.commit()


@pytest.fixture(params=MEAN_METRIC_TYPES)
def experiment(request, db_session, make_experiment):
    suffix = uuid.uuid4().hex[:8]
    exp = make_experiment(
        name=f"Difference intervals {suffix}",
        key=f"difference-intervals-{suffix}",
        status=ExperimentStatus.ACTIVE,
        experiment_type=ExperimentType.A_B,
        start_date=datetime.now(timezone.utc) - timedelta(days=2),
    )
    db_session.add_all(
        [
            Variant(
                experiment_id=exp.id,
                name="control",
                is_control=True,
                traffic_allocation=50,
            ),
            Variant(
                experiment_id=exp.id,
                name="treatment",
                is_control=False,
                traffic_allocation=50,
            ),
            Metric(
                experiment_id=exp.id,
                name="Signed up",
                event_name="signup",
                metric_type=MetricType.CONVERSION,
                is_primary=True,
                minimum_sample_size=5,
            ),
            Metric(
                experiment_id=exp.id,
                name="Purchases per user",
                event_name="purchase",
                metric_type=MetricType(request.param),
                is_primary=False,
                minimum_sample_size=5,
            ),
        ]
    )
    db_session.commit()
    db_session.refresh(exp)
    at = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    rows = []
    for variant in exp.variants:
        arm = variant.name
        for i in range(USERS[arm]):
            rows.append(
                Assignment(
                    experiment_id=exp.id,
                    variant_id=variant.id,
                    user_id=_user(exp, arm, i),
                )
            )
        for event, how_many in (("signup", SIGNUPS[arm]), ("purchase", PURCHASES[arm])):
            for i in range(how_many):
                rows.append(
                    Event(
                        event_type=event,
                        event_name=event,
                        user_id=_user(exp, arm, i),
                        experiment_id=exp.id,
                        variant_id=variant.id,
                        value=1.0,
                        created_at=at,
                    )
                )
    db_session.add_all(rows)
    db_session.commit()
    db_session.refresh(exp)
    yield exp
    _cleanup(db_session, exp)


def _metrics(client, exp):
    response = client.get(f"/api/v1/results/{exp.id}", params={"use_cache": "false"})
    assert response.status_code == 200, response.text
    for word in ("NaN", "Infinity"):
        assert word not in response.text
    return {m["metric_name"]: m for m in response.json()["metrics"]}


def _arms(metric):
    by_name = {v["variant_name"]: v for v in metric["variants"]}
    return by_name["control"], by_name["treatment"]


def test_a_proportion_with_a_control_at_0_has_a_difference_and_no_relative_change(
    admin_client, experiment
):
    control, treatment = _arms(_metrics(admin_client, experiment)["Signed up"])
    low, high = confint_proportions_2indep(
        12, 400, 0, 500, method="newcomb", compare="diff", alpha=0.05
    )
    assert all(control[field] is None for field in DIFFERENCE_FIELDS)
    assert treatment["absolute_difference"] == pytest.approx(0.03, abs=1e-15)
    assert treatment["absolute_difference_ci"] == pytest.approx(
        [float(low), float(high)], abs=1e-12
    )
    assert treatment["absolute_difference_ci"] == pytest.approx(
        [0.015138, 0.051699], abs=5e-7
    )
    assert treatment["incremental_impact"] == pytest.approx(12.0, abs=1e-9)
    assert treatment["incremental_impact_ci"] == pytest.approx(
        [float(low) * 400, float(high) * 400], abs=1e-9
    )
    assert treatment["relative_improvement_pct"] is None
    assert treatment["relative_improvement_pct_ci"] is None


def test_a_mean_metric_carries_the_relative_interval_and_the_impact(
    admin_client, experiment
):
    metric = _metrics(admin_client, experiment)["Purchases per user"]
    assert metric["analysis"] == "mean"
    control, treatment = _arms(metric)
    expected = per_user_mean_metric_result(
        [
            (
                BinomialVariant("c", "control", True),
                USERS["control"],
                PURCHASES["control"],
                [1.0] * PURCHASES["control"],
            ),
            (
                BinomialVariant("t", "treatment", False),
                USERS["treatment"],
                PURCHASES["treatment"],
                [1.0] * PURCHASES["treatment"],
            ),
        ],
        0.05,
        "benjamini_hochberg",
        metric=SimpleNamespace(
            id="m", name="m", metric_type=metric["metric_type"], is_primary=False
        ),
    )
    [want] = [v for v in expected["variants"] if not v["is_control"]]
    assert all(control[field] is None for field in DIFFERENCE_FIELDS)
    assert treatment["absolute_difference"] == pytest.approx(0.3 - 0.2, rel=1e-9)
    for field in DIFFERENCE_FIELDS:
        assert treatment[field] == pytest.approx(want[field], rel=1e-9), field
    low, high = treatment["relative_improvement_pct_ci"]
    assert low > 0 and high > low
    assert treatment["incremental_impact"] == pytest.approx(
        treatment["absolute_difference"] * 400, rel=1e-12
    )
