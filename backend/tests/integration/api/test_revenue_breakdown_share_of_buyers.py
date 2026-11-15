"""
The Breakdowns view of a revenue metric shows the share of users who sent the
metric's event, not revenue per user.  So does the Breakdowns view of a count,
duration or custom metric, which ``/results`` analyses as a mean too (#439):
the test runs for each of ``MEAN_METRIC_TYPES``.

``GET /api/v1/results/{id}?breakdown=<dimension>`` counts, per segment and
variant, the users with an event in the segment and the users among them who
sent the primary metric's event; a segment's ``mean`` is the second over the
first.  The event's value is not read.  The same experiment's headline metric
is revenue per assigned user, so the two differ and the documentation says so
(Limitations, "Experiments and results").

The dataset is one segment, ``country = US``, 4 users per arm, every event
carrying the segment in its metadata:

* control: users 0 and 1 buy (100 and 300); users 2 and 3 only add to cart;
* treatment: users 0, 1 and 2 buy (1000 each); user 3 only adds to cart.

Share of buyers: control 2 of 4 = 0.5, treatment 3 of 4 = 0.75.  Revenue per
user would be 100 and 750.

Rows created here are deleted in fixture teardown because the shared test
database is not truncated between tests.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

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
from backend.app.services.sufficient_stats_analysis import MEAN_METRIC_TYPES

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

USERS = 4
#: (arm, user index, event name, value)
EVENTS = [
    ("control", 0, "purchase", 100.0),
    ("control", 1, "purchase", 300.0),
    ("control", 2, "add_to_cart", None),
    ("control", 3, "add_to_cart", None),
    ("treatment", 0, "purchase", 1000.0),
    ("treatment", 1, "purchase", 1000.0),
    ("treatment", 2, "purchase", 1000.0),
    ("treatment", 3, "add_to_cart", None),
]


def _user(exp, arm, i):
    return f"rbs-{exp.key}-{arm}-{i}"


@pytest.fixture(params=MEAN_METRIC_TYPES)
def experiment(request, db_session, make_experiment):
    suffix = uuid.uuid4().hex[:8]
    exp = make_experiment(
        name=f"Breakdown revenue {suffix}",
        key=f"breakdown-revenue-{suffix}",
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
                name="Order value",
                event_name="purchase",
                metric_type=MetricType(request.param),
                is_primary=True,
                minimum_sample_size=1,
            ),
        ]
    )
    db_session.commit()
    db_session.refresh(exp)
    ids = {v.name: v.id for v in exp.variants}
    db_session.add_all(
        Assignment(
            experiment_id=exp.id,
            variant_id=ids[arm],
            user_id=_user(exp, arm, i),
        )
        for arm in ids
        for i in range(USERS)
    )
    at = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    db_session.add_all(
        Event(
            event_type=name,
            event_name=name,
            user_id=_user(exp, arm, i),
            experiment_id=exp.id,
            variant_id=ids[arm],
            value=value,
            event_metadata={"country": "US"},
            created_at=at,
        )
        for arm, i, name, value in EVENTS
    )
    db_session.commit()
    db_session.refresh(exp)
    yield exp
    db_session.rollback()
    for model in (AnalysisSnapshot, Event, Assignment):
        db_session.query(model).filter(model.experiment_id == exp.id).delete(
            synchronize_session=False
        )
    db_session.commit()


def test_a_revenue_metric_breakdown_is_the_share_of_buyers(admin_client, experiment):
    response = admin_client.get(
        f"/api/v1/results/{experiment.id}",
        params={"use_cache": "false", "breakdown": "country"},
    )
    assert response.status_code == 200, response.text
    body = response.json()

    # The headline metric is its mean per assigned user ...
    [metric] = body["metrics"]
    assert metric["metric_type"] in MEAN_METRIC_TYPES
    assert metric["analysis"] == "mean"

    # ... and the breakdown of the same experiment is the share of buyers.
    [segment] = body["breakdown"]["segments"]
    assert segment["segment_value"] == "US"
    arms = {v["variant_name"]: v for v in segment["variants"]}
    assert (arms["control"]["sample_size"], arms["control"]["conversions"]) == (4, 2)
    assert (arms["treatment"]["sample_size"], arms["treatment"]["conversions"]) == (
        4,
        3,
    )
    assert arms["control"]["mean"] == pytest.approx(0.5)
    assert arms["treatment"]["mean"] == pytest.approx(0.75)
