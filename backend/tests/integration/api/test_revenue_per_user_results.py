"""
``GET /api/v1/results/{id}`` analyses a revenue metric per assigned user (#439),
through the real routes, a real database and events sent to
``POST /api/v1/tracking/track``.

Each assigned user is one unit worth the total of their purchase values, 0
for a user who bought nothing.  The arms are compared with Welch's t-test, and
each treatment carries its difference from the control per user with the
interval.  The expected numbers come from scipy on the per-user arrays.

The dataset is fixed, 10 users per arm, assigned through the ORM so the arms
are known:

* control: 20 and 35.5, and one purchase sent with no value;
* treatment: a repeat buyer (20, then 40), 60, 35.5 and 12.25.

Events that must count nowhere are added: a purchase from a user with no
assignment, one tagged with the control's variant from a user assigned to the
treatment, one with no variant, another event's value, and an event of a type
in ``EXPOSURE_EVENT_TYPES`` that carries the metric's event name and a value.

A refund, a negative value, is added to its buyer's total: that has its own
small experiment.

The export and the analyses that stay on the share of buyers are held for
every type ``/results`` analyses as a mean (#439): the same events, with the
metric's type set to each of ``MEAN_METRIC_TYPES``.

Rows created here are deleted in fixture teardown because the shared test
database is not truncated between tests.
"""

from __future__ import annotations

import asyncio
import json
import math
import uuid
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from scipy import stats

from backend.app.models.analysis_snapshot import AnalysisSnapshot
from backend.app.models.assignment import Assignment
from backend.app.models.event import Event, EventType
from backend.app.models.experiment import (
    ExperimentStatus,
    ExperimentType,
    Metric,
    MetricType,
    Variant,
)
from backend.app.services.results_streaming_service import ResultsStreamingService
from backend.app.services.sufficient_stats_analysis import MEAN_METRIC_TYPES

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

USERS = 10
#: Purchases sent through the tracking API: (arm, user index, value or None).
PURCHASES = [
    ("control", 0, 20.0),
    ("control", 1, 35.5),
    ("control", 2, None),
    ("treatment", 0, 20.0),
    ("treatment", 0, 40.0),
    ("treatment", 1, 60.0),
    ("treatment", 2, 35.5),
    ("treatment", 3, 12.25),
]
CONTROL_Y = np.array([20.0, 35.5] + [0.0] * 8)
TREATMENT_Y = np.array([60.0, 60.0, 35.5, 12.25] + [0.0] * 6)
#: The same purchases counted per user, for a count metric: every event is 1,
#: the no-value one included.
CONTROL_COUNTS = np.array([1.0, 1.0, 1.0] + [0.0] * 7)
TREATMENT_COUNTS = np.array([2.0, 1.0, 1.0, 1.0] + [0.0] * 6)
#: Each mean type's per-user arrays for the dataset above.
PER_USER = {
    metric_type: (
        (CONTROL_COUNTS, TREATMENT_COUNTS)
        if metric_type == "count"
        else (CONTROL_Y, TREATMENT_Y)
    )
    for metric_type in MEAN_METRIC_TYPES
}


def _cleanup(db_session, exp):
    db_session.rollback()
    for model in (AnalysisSnapshot, Event, Assignment):
        db_session.query(model).filter(model.experiment_id == exp.id).delete(
            synchronize_session=False
        )
    db_session.query(Event).filter(Event.user_id.like(f"rpu-{exp.key}-%")).delete(
        synchronize_session=False
    )
    db_session.commit()


def _user(exp, arm, i):
    return f"rpu-{exp.key}-{arm}-{i}"


def _make(db_session, make_experiment, users, metric_type=MetricType.REVENUE):
    suffix = uuid.uuid4().hex[:8]
    exp = make_experiment(
        name=f"Checkout total {suffix}",
        key=f"checkout-total-{suffix}",
        status=ExperimentStatus.ACTIVE,
        experiment_type=ExperimentType.A_B,
        start_date=datetime.now(timezone.utc) - timedelta(days=2),
        bayesian_enabled=True,
        bayesian_config={"prior_family": "beta", "alpha": 1.0, "beta": 1.0},
        sequential_testing_enabled=True,
        sequential_testing_config={"method": "msprt"},
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
                name="Order value",
                event_name="purchase",
                metric_type=metric_type,
                is_primary=True,
                minimum_sample_size=5,
            ),
        ]
    )
    db_session.commit()
    db_session.refresh(exp)
    db_session.add_all(
        Assignment(
            experiment_id=exp.id,
            variant_id=variant.id,
            user_id=_user(exp, variant.name, i),
        )
        for variant in exp.variants
        for i in range(users)
    )
    db_session.commit()
    db_session.refresh(exp)
    return exp


def _track(client, exp, user_id, value, event="purchase"):
    body = {
        "event_type": event,
        "event_name": event,
        "user_id": user_id,
        "experiment_key": exp.key,
    }
    if value is not None:
        body["value"] = value
    # Python's own encoder, which writes NaN and Infinity as JavaScript does;
    # the test client's refuses them.
    response = client.post(
        "/api/v1/tracking/track",
        content=json.dumps(body),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code in (200, 201), response.text
    return response


def _variant_ids(exp):
    return {v.name: v.id for v in exp.variants}


@pytest.fixture
def experiment(admin_client, db_session, make_experiment):
    yield from _experiment(admin_client, db_session, make_experiment)


@pytest.fixture(params=MEAN_METRIC_TYPES)
def mean_experiment(request, admin_client, db_session, make_experiment):
    """The same experiment with its metric of each type analysed as a mean."""
    yield from _experiment(
        admin_client, db_session, make_experiment, MetricType(request.param)
    )


def _experiment(admin_client, db_session, make_experiment, metric_type=None):
    exp = _make(db_session, make_experiment, USERS, metric_type or MetricType.REVENUE)
    ids = _variant_ids(exp)
    for arm, i, value in PURCHASES:
        _track(admin_client, exp, _user(exp, arm, i), value)
    # Counted nowhere: no assignment (the event has no variant) ...
    _track(admin_client, exp, f"rpu-{exp.key}-stranger", 1000.0)
    # ... another event's value ...
    _track(admin_client, exp, _user(exp, "control", 5), 1000.0, event="add_to_cart")
    at = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    db_session.add_all(
        [
            # ... tagged with the control from a user assigned to the treatment ...
            Event(
                event_type="purchase",
                event_name="purchase",
                user_id=_user(exp, "treatment", 4),
                experiment_id=exp.id,
                variant_id=ids["control"],
                value=1000.0,
                created_at=at,
            ),
            # ... an assigned user's purchase stored with no variant ...
            Event(
                event_type="purchase",
                event_name="purchase",
                user_id=_user(exp, "control", 3),
                experiment_id=exp.id,
                variant_id=None,
                value=1000.0,
                created_at=at,
            ),
            # ... and an EXPOSURE_EVENT_TYPES event with the metric's name and a value.
            Event(
                event_type=EventType.EXPOSURE.value,
                event_name="purchase",
                user_id=_user(exp, "control", 6),
                experiment_id=exp.id,
                variant_id=ids["control"],
                value=1000.0,
                created_at=at,
            ),
        ]
    )
    db_session.commit()
    yield exp
    _cleanup(db_session, exp)


def _results(client, exp, **params):
    response = client.get(
        f"/api/v1/results/{exp.id}", params={"use_cache": "false", **params}
    )
    assert response.status_code == 200, response.text
    # A value the response could not carry as JSON would show here.
    for word in ("NaN", "Infinity"):
        assert word not in response.text
    return response.json()


def _arms(result):
    [metric] = result["metrics"]
    by_name = {v["variant_name"]: v for v in metric["variants"]}
    return metric, by_name["control"], by_name["treatment"]


def test_a_revenue_metric_is_analysed_per_assigned_user(admin_client, experiment):
    metric, control, treatment = _arms(_results(admin_client, experiment))
    welch = stats.ttest_ind(TREATMENT_Y, CONTROL_Y, equal_var=False)

    assert metric["metric_type"] == "revenue"
    assert metric["analysis"] == "mean"
    # Every assigned user is in n, buyers or not; the no-value buyer too.
    assert (control["sample_size"], treatment["sample_size"]) == (USERS, USERS)
    # The buyers: the users who sent the purchase, whatever its value.
    assert (control["conversions"], treatment["conversions"]) == (3, 4)
    # Non-buyers are 0, the repeat buyer is one user of 60, and the 1000s
    # count nowhere.
    assert control["mean"] == pytest.approx(5.55, rel=1e-9)
    assert treatment["mean"] == pytest.approx(16.775, rel=1e-9)
    assert control["std_dev"] == pytest.approx(np.std(CONTROL_Y, ddof=1), rel=1e-9)
    assert treatment["std_dev"] == pytest.approx(np.std(TREATMENT_Y, ddof=1), rel=1e-9)
    for variant, y in ((control, CONTROL_Y), (treatment, TREATMENT_Y)):
        half = stats.t.ppf(0.975, len(y) - 1) * stats.sem(y)
        assert variant["confidence_interval"] == pytest.approx(
            [y.mean() - half, y.mean() + half], rel=1e-9
        )
    assert treatment["statistical_test_used"] == "welch_t_test"
    assert treatment["p_value"] == pytest.approx(welch.pvalue, rel=1e-9)
    assert treatment["absolute_difference"] == pytest.approx(11.225, rel=1e-9)
    assert treatment["absolute_difference_ci"] == pytest.approx(
        list(welch.confidence_interval(0.95)), rel=1e-9
    )
    assert treatment["relative_improvement_pct"] == pytest.approx(
        (16.775 - 5.55) / 5.55 * 100, rel=1e-9
    )
    assert control["absolute_difference"] is None


def test_the_difference_interval_follows_the_requested_level(admin_client, experiment):
    _, _, treatment = _arms(_results(admin_client, experiment, confidence_level=0.9))
    welch = stats.ttest_ind(TREATMENT_Y, CONTROL_Y, equal_var=False)
    assert treatment["absolute_difference_ci"] == pytest.approx(
        list(welch.confidence_interval(0.9)), rel=1e-9
    )


def test_the_export_leaves_the_rate_empty_for_a_revenue_primary(
    admin_client, mean_experiment
):
    """The export's rows for a primary metric analysed as a mean (a revenue,
    count, duration or custom metric): no rate, the buyers, and the Welch
    p-value."""
    experiment = mean_experiment
    metric, _, treatment = _arms(_results(admin_client, experiment))
    assert metric["analysis"] == "mean"
    control_y, treatment_y = PER_USER[metric["metric_type"]]
    response = admin_client.get("/api/v1/export/variants", params={"format": "json"})
    assert response.status_code == 200, response.text
    rows = {
        row["variant_name"]: row
        for row in response.json()
        if row["experiment_id"] == str(experiment.id)
    }
    assert rows["control"]["conversion_rate"] is None
    assert rows["treatment"]["conversion_rate"] is None
    assert (rows["control"]["conversions"], rows["treatment"]["conversions"]) == (3, 4)
    assert rows["control"]["p_value"] is None
    assert rows["treatment"]["p_value"] == pytest.approx(treatment["p_value"])
    assert rows["treatment"]["p_value"] == pytest.approx(
        stats.ttest_ind(treatment_y, control_y, equal_var=False).pvalue, rel=1e-9
    )


class _SharedSession:
    """The test session, handed to the streaming service; close() is a no-op."""

    def __init__(self, session):
        self._session = session

    def __getattr__(self, name):
        return getattr(self._session, name)

    def close(self):
        pass


def test_the_other_analyses_of_a_revenue_metric_stay_on_the_share_of_buyers(
    admin_client, db_session, mean_experiment
):
    """The Bayesian and sequential analyses, CUPED, the sample-size plan, the
    daily series and the Live tab of a metric analysed as a mean (a revenue,
    count, duration or custom metric) read the users who sent its event, as
    for a conversion: 3 of 10 in the control and 4 of 10 in the treatment."""
    experiment = mean_experiment
    body = _results(admin_client, experiment)
    assert body["metrics"][0]["analysis"] == "mean"

    posteriors = {
        v["variant_key"]: v["posterior"]
        for v in body["bayesian_results"]["variant_results"]
    }
    # A beta(1, 1) prior updated with buyers and non-buyers.
    assert (posteriors["control"]["alpha"], posteriors["control"]["beta"]) == (4, 8)
    assert (posteriors["treatment"]["alpha"], posteriors["treatment"]["beta"]) == (
        5,
        7,
    )

    cuped = admin_client.get(f"/api/v1/results/{experiment.id}/cuped")
    assert cuped.status_code == 200, cuped.text
    assert {row["unavailable_reason"] for row in cuped.json()["metrics"]} == {
        "not_a_proportion_metric"
    }

    plan = admin_client.get(f"/api/v1/results/{experiment.id}/sample-size")
    assert plan.status_code == 200, plan.text
    assert plan.json()["analysed_as"] == "conversion"

    daily = admin_client.get(f"/api/v1/results/{experiment.id}/daily")
    assert daily.status_code == 200, daily.text
    buyers = {
        series["variant_name"]: sum(point["conversions"] for point in series["values"])
        for series in daily.json()["series"]
    }
    assert buyers == {"control": 3, "treatment": 4}

    # The Live tab's snapshot (GET /ws/experiments/{id}/results) is the share
    # of users who sent the event too.
    service = ResultsStreamingService(lambda: _SharedSession(db_session))
    live = asyncio.run(service.get_live_snapshot(str(experiment.id)))
    assert live.get("event") == "results_update", live
    assert {
        v["name"]: (v["conversion_count"], v["conversion_rate"])
        for v in live["variants"]
    } == {"control": (3, 0.3), "treatment": (4, 0.4)}

    # The sequential analysis is the one the same experiment gets with the
    # metric as a conversion.
    sequential = body["sequential_testing"]
    assert sequential is not None
    db_session.query(Metric).filter(Metric.experiment_id == experiment.id).update(
        {"metric_type": MetricType.CONVERSION}, synchronize_session=False
    )
    db_session.commit()
    as_conversion = _results(admin_client, experiment)
    assert as_conversion["metrics"][0]["analysis"] == "proportion"
    for block in ("confidence_sequence", "msprt_result"):
        assert sequential[block] == as_conversion["sequential_testing"][block]


#: Values that add nothing to their user's total; the user still bought.
NOT_COUNTED = [math.nan, math.inf, -math.inf, 1e308, math.nextafter(1e12, math.inf)]


@pytest.fixture
def unusual_values(admin_client, db_session, make_experiment):
    exp = _make(db_session, make_experiment, len(NOT_COUNTED) + 1)
    for i, value in enumerate(NOT_COUNTED):
        _track(admin_client, exp, _user(exp, "control", i), value)
    # The bound itself counts.
    _track(admin_client, exp, _user(exp, "treatment", 0), 1e12)
    _track(admin_client, exp, _user(exp, "treatment", 1), 10.0)
    _track(admin_client, exp, _user(exp, "treatment", 1), None)
    yield exp
    _cleanup(db_session, exp)


def test_values_that_are_not_finite_or_too_large_add_nothing(
    admin_client, db_session, unusual_values
):
    exp = unusual_values
    stored = sorted(
        repr(value)
        for (value,) in db_session.query(Event.value).filter(
            Event.experiment_id == exp.id, Event.user_id.like("%-control-%")
        )
    )
    # The values were stored as sent: this is what the analysis reads.
    assert stored == sorted(repr(v) for v in NOT_COUNTED)

    metric, control, treatment = _arms(_results(admin_client, exp))
    users = len(NOT_COUNTED) + 1
    assert metric["analysis"] == "mean"
    # Each of those buyers counts as a buyer and adds 0.
    assert control["conversions"] == len(NOT_COUNTED)
    assert control["mean"] == 0.0
    assert control["std_dev"] == 0.0
    assert treatment["conversions"] == 2
    assert treatment["mean"] == pytest.approx((1e12 + 10.0) / users, rel=1e-12)
    assert treatment["statistical_test_used"] == "welch_t_test"
    assert treatment["relative_improvement_pct"] is None
    y = np.array([1e12, 10.0] + [0.0] * (users - 2))
    welch = stats.ttest_ind(y, np.zeros(users), equal_var=False)
    assert treatment["p_value"] == pytest.approx(welch.pvalue, rel=1e-9)

    export = admin_client.get("/api/v1/export/variants", params={"format": "json"})
    assert export.status_code == 200, export.text


@pytest.fixture
def refund(admin_client, db_session, make_experiment):
    exp = _make(db_session, make_experiment, 4)
    # A buyer who pays 30 and is refunded 10, and a buyer of 5.
    _track(admin_client, exp, _user(exp, "control", 0), 30.0)
    _track(admin_client, exp, _user(exp, "control", 0), -10.0)
    _track(admin_client, exp, _user(exp, "control", 1), 5.0)
    _track(admin_client, exp, _user(exp, "treatment", 0), 12.0)
    yield exp
    _cleanup(db_session, exp)


def test_a_refund_is_subtracted_from_its_buyers_total(admin_client, refund):
    control_y = np.array([20.0, 5.0, 0.0, 0.0])
    treatment_y = np.array([12.0, 0.0, 0.0, 0.0])

    metric, control, treatment = _arms(_results(admin_client, refund))

    assert metric["analysis"] == "mean"
    # The refunded buyer is one buyer worth 30 - 10 = 20.
    assert control["conversions"] == 2
    assert control["mean"] == pytest.approx(6.25, rel=1e-12)
    assert control["std_dev"] == pytest.approx(np.std(control_y, ddof=1), rel=1e-9)
    welch = stats.ttest_ind(treatment_y, control_y, equal_var=False)
    assert treatment["p_value"] == pytest.approx(welch.pvalue, rel=1e-9)
    assert treatment["absolute_difference"] == pytest.approx(3.0 - 6.25, rel=1e-12)
