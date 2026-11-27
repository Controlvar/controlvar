"""
Without ``metric_id``, the daily results answer for the metric the results
summary leads with (#2205).

``GET /api/v1/results/{id}/daily`` answers for the first metric the analysis
service lists, and ``GET /api/v1/experiments/{id}/daily-results`` lists them in
that order. The service listed them in database order, so the dashboard's trend
chart showed the metric added first while the summary beside it showed the
primary. They are now listed as ``GET /results`` lists them: the primary first,
then by name.

Each fixture adds the expected metric second, with the event name that sorts
last. The primary is also named after the other metric, so in each order the
database may read them (as added, by name, or by event name) the old code
answered for the other metric. With no primary the expected metric is
first by name, so the old code answered for it only when the database read the
metrics by name. The two metrics' conversions differ in both arms, so the
counts below say which metric was answered.

Rows created here are deleted in fixture teardown because the shared test
database is not truncated between tests.
"""

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

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

#: Per arm: users assigned, and users who converted on each metric.
PRIMARY_SECOND = {
    "Purchase": {"control": (4, 2), "treatment": (4, 3)},
    "Activation": {"control": (4, 1), "treatment": (4, 0)},
}
NO_PRIMARY = {
    "Checkout": {"control": (4, 1), "treatment": (4, 2)},
    "Signup": {"control": (4, 3), "treatment": (4, 1)},
}


def _experiment(db_session, make_experiment, metrics, converters):
    """An active experiment with four users per arm and *metrics* added in order.

    *metrics* is ``[(name, event_name, is_primary), ...]``; *converters* maps
    an event name to the users, per arm, who sent it once on the second day.
    """
    suffix = uuid.uuid4().hex[:8]
    start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ) - timedelta(days=2)
    exp = make_experiment(
        name=f"Daily primary {suffix}",
        key=f"daily-primary-{suffix}",
        status=ExperimentStatus.ACTIVE,
        experiment_type=ExperimentType.A_B,
        start_date=start,
    )
    control = Variant(
        experiment_id=exp.id, name="control", is_control=True, traffic_allocation=50
    )
    treatment = Variant(
        experiment_id=exp.id, name="treatment", is_control=False, traffic_allocation=50
    )
    db_session.add_all([control, treatment])
    db_session.commit()
    for name, event_name, is_primary in metrics:
        db_session.add(
            Metric(
                experiment_id=exp.id,
                name=name,
                event_name=event_name,
                metric_type=MetricType.CONVERSION,
                is_primary=is_primary,
            )
        )
        db_session.commit()

    day = start + timedelta(days=1, hours=12)
    rows = []
    for variant in (control, treatment):
        for i in range(4):
            user_id = f"dp-{suffix}-{variant.name}-{i}"
            rows.append(
                Assignment(experiment_id=exp.id, variant_id=variant.id, user_id=user_id)
            )
            for event_name, by_arm in converters.items():
                if i in by_arm[variant.name]:
                    rows.append(
                        Event(
                            event_type=event_name,
                            event_name=event_name,
                            user_id=user_id,
                            experiment_id=exp.id,
                            variant_id=variant.id,
                            value=1.0,
                            created_at=day.isoformat(),
                        )
                    )
    db_session.add_all(rows)
    db_session.commit()
    db_session.refresh(exp)
    return exp


def _cleanup(db_session, exp):
    db_session.rollback()
    for model in (AnalysisSnapshot, Event, Assignment):
        db_session.query(model).filter(model.experiment_id == exp.id).delete(
            synchronize_session=False
        )
    db_session.commit()


@pytest.fixture
def primary_added_second(db_session, make_experiment):
    """Activation (added first) and Purchase (added second, primary)."""
    exp = _experiment(
        db_session,
        make_experiment,
        [("Activation", "activate", False), ("Purchase", "purchase", True)],
        {
            "purchase": {"control": {0, 1}, "treatment": {0, 1, 2}},
            "activate": {"control": {2}, "treatment": set()},
        },
    )
    yield exp
    _cleanup(db_session, exp)


@pytest.fixture
def no_primary(db_session, make_experiment):
    """Signup (added first) and Checkout (added second), neither primary."""
    exp = _experiment(
        db_session,
        make_experiment,
        [("Signup", "account_created", False), ("Checkout", "order_placed", False)],
        {
            "order_placed": {"control": {0}, "treatment": {0, 1}},
            "account_created": {"control": {1, 2, 3}, "treatment": {3}},
        },
    )
    yield exp
    _cleanup(db_session, exp)


def _get(client, path, **params):
    response = client.get(path, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _metric_ids(exp):
    return {m.name: str(m.id) for m in exp.metric_definitions}


def _daily_totals(daily):
    """Each arm's (users, converted users) at the end of the daily series."""
    return {
        s["variant_name"]: (
            s["cumulative"][-1]["sample_size"],
            s["cumulative"][-1]["conversions"],
        )
        for s in daily["series"]
    }


def _summary_totals(results, name):
    metric = next(m for m in results["metrics"] if m["metric_name"] == name)
    return {
        v["variant_name"]: (v["sample_size"], v["conversions"])
        for v in metric["variants"]
    }


@pytest.mark.regression
def test_the_daily_series_without_a_metric_id_is_the_primary_metrics(
    admin_client, primary_added_second
):
    exp = primary_added_second
    ids = _metric_ids(exp)
    base = f"/api/v1/results/{exp.id}"

    daily = _get(admin_client, f"{base}/daily")
    assert daily["metric_id"] is None
    assert _daily_totals(daily) == PRIMARY_SECOND["Purchase"]

    for name, want in PRIMARY_SECOND.items():
        named = _get(admin_client, f"{base}/daily", metric_id=ids[name])
        assert _daily_totals(named) == want, name

    results = _get(admin_client, base, use_cache="false")
    assert results["metrics"][0]["metric_name"] == "Purchase"
    assert _summary_totals(results, "Purchase") == PRIMARY_SECOND["Purchase"]


@pytest.mark.regression
def test_with_no_primary_the_daily_series_is_the_first_metric_by_name(
    admin_client, no_primary
):
    exp = no_primary
    ids = _metric_ids(exp)
    base = f"/api/v1/results/{exp.id}"

    daily = _get(admin_client, f"{base}/daily")
    assert _daily_totals(daily) == NO_PRIMARY["Checkout"]

    for name, want in NO_PRIMARY.items():
        named = _get(admin_client, f"{base}/daily", metric_id=ids[name])
        assert _daily_totals(named) == want, name

    results = _get(admin_client, base, use_cache="false")
    assert results["metrics"][0]["metric_name"] == "Checkout"


@pytest.mark.regression
def test_the_daily_results_list_the_primary_first_then_by_name(
    admin_client, primary_added_second, no_primary
):
    for exp, order in (
        (primary_added_second, ["Purchase", "Activation"]),
        (no_primary, ["Checkout", "Signup"]),
    ):
        ids = _metric_ids(exp)
        days = _get(admin_client, f"/api/v1/experiments/{exp.id}/daily-results")
        assert days, exp.name
        for day in days:
            assert [m["metric_name"] for m in day["metrics"]] == order, day["date"]
            assert [m["metric_id"] for m in day["metrics"]] == [ids[n] for n in order]

        one = _get(
            admin_client,
            f"/api/v1/experiments/{exp.id}/daily-results",
            metric_id=ids[order[1]],
        )
        assert {m["metric_name"] for day in one for m in day["metrics"]} == {order[1]}
