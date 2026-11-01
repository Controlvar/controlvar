"""
``GET /api/v1/results/{id}`` analyses a count, duration or custom metric per
assigned user (#439), through the real routes, a real database and events
sent to ``POST /api/v1/tracking/track``.

* A count metric's per-user value is the number of the metric's events the
  user sent, whatever their ``value``: ``AnalysisService.event_counts``, a
  ``COUNT`` with no value predicate.
* A duration or custom metric's is the total of those events' ``value``
  within ``VALUE_BOUND``, as revenue's (``value_totals``).
* A user with none is 0, and the arms are compared with Welch's t-test.

The SQL is what these tests hold; the unit tests in
``backend/tests/unit/services/test_value_metrics_per_user.py`` fake the
readers and so guard only the routing.  The expected numbers come from scipy
on the per-user arrays.

Events that must count nowhere are added, as in
``test_revenue_per_user_results.py``: one from a user with no assignment,
another event's, one tagged with the control's variant from a user assigned
to the treatment, an assigned user's stored with no variant, and an event of a
type in ``EXPOSURE_EVENT_TYPES`` that carries the metric's event name.

Rows created here are deleted in fixture teardown because the shared test
database is not truncated between tests.
"""

from __future__ import annotations

import json
import math
import uuid
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from scipy import stats

from backend.app.core.stats_engine import ENGINE_VERSION
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

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

USERS = 10


def _user(exp, arm, i):
    return f"vmr-{exp.key}-{arm}-{i}"


def _cleanup(db_session, exp):
    db_session.rollback()
    for model in (AnalysisSnapshot, Event, Assignment):
        db_session.query(model).filter(model.experiment_id == exp.id).delete(
            synchronize_session=False
        )
    db_session.query(Event).filter(Event.user_id.like(f"vmr-{exp.key}-%")).delete(
        synchronize_session=False
    )
    db_session.commit()


def _make(db_session, make_experiment, metric_type, users, minimum_sample_size=5):
    suffix = uuid.uuid4().hex[:8]
    exp = make_experiment(
        name=f"Value metric {suffix}",
        key=f"value-metric-{suffix}",
        status=ExperimentStatus.ACTIVE,
        experiment_type=ExperimentType.A_B,
        start_date=datetime.now(timezone.utc) - timedelta(days=2),
        correction_method="none",
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
                name="Articles read",
                event_name="article_read",
                metric_type=metric_type,
                is_primary=True,
                minimum_sample_size=minimum_sample_size,
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


def _track(client, exp, user_id, value=None, event="article_read", send_value=False):
    body = {
        "event_type": event,
        "event_name": event,
        "user_id": user_id,
        "experiment_key": exp.key,
    }
    if value is not None or send_value:
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


def _stray_events(db_session, admin_client, exp, value=None):
    """Events that count in no arm, whatever the metric's type."""
    ids = {v.name: v.id for v in exp.variants}
    # No assignment: the event has no variant ...
    _track(admin_client, exp, f"vmr-{exp.key}-stranger", value)
    # ... another event's ...
    _track(admin_client, exp, _user(exp, "control", 5), value, event="add_to_cart")
    at = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    db_session.add_all(
        [
            # ... tagged with the control from a user assigned to the treatment ...
            Event(
                event_type="article_read",
                event_name="article_read",
                user_id=_user(exp, "treatment", 9),
                experiment_id=exp.id,
                variant_id=ids["control"],
                value=value,
                created_at=at,
            ),
            # ... an assigned user's event stored with no variant ...
            Event(
                event_type="article_read",
                event_name="article_read",
                user_id=_user(exp, "control", 3),
                experiment_id=exp.id,
                variant_id=None,
                value=value,
                created_at=at,
            ),
            # ... and an EXPOSURE_EVENT_TYPES event with the metric's name.
            Event(
                event_type=EventType.EXPOSURE.value,
                event_name="article_read",
                user_id=_user(exp, "control", 6),
                experiment_id=exp.id,
                variant_id=ids["control"],
                value=value,
                created_at=at,
            ),
        ]
    )
    db_session.commit()


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


def _per_user(per_user, users=USERS):
    return np.array(list(per_user) + [0.0] * (users - len(per_user)), dtype=float)


# ---------------------------------------------------------------------------
# Count: the number of events per assigned user, sent with no value
# ---------------------------------------------------------------------------

#: Events sent with no value: (arm, user index, how many).
COUNT_EVENTS = [
    ("control", 0, 1),
    ("control", 1, 3),
    ("control", 2, 1),
    ("treatment", 0, 2),
    ("treatment", 1, 1),
    ("treatment", 2, 4),
    ("treatment", 3, 1),
]
COUNT_CONTROL_Y = _per_user([1.0, 3.0, 1.0])
COUNT_TREATMENT_Y = _per_user([2.0, 1.0, 4.0, 1.0])


@pytest.fixture
def count_experiment(admin_client, db_session, make_experiment):
    exp = _make(db_session, make_experiment, MetricType.COUNT, USERS)
    for arm, i, times in COUNT_EVENTS:
        for _ in range(times):
            _track(admin_client, exp, _user(exp, arm, i))
    _stray_events(db_session, admin_client, exp)
    yield exp
    _cleanup(db_session, exp)


@pytest.mark.regression
def test_a_count_metric_counts_events_sent_with_no_value(
    admin_client, db_session, count_experiment
):
    """PE-5: a count read with ``value_totals``' predicate
    (``abs(value) <= VALUE_BOUND``) drops every event sent with no value, so
    the result would be 0 against 0."""
    stored = {
        value
        for (value,) in db_session.query(Event.value).filter(
            Event.experiment_id == count_experiment.id,
            Event.event_name == "article_read",
        )
    }
    # The events were stored with no value: this is what the count reads.
    assert stored == {None}

    metric, control, treatment = _arms(_results(admin_client, count_experiment))
    welch = stats.ttest_ind(COUNT_TREATMENT_Y, COUNT_CONTROL_Y, equal_var=False)

    assert metric["metric_type"] == "count"
    assert metric["analysis"] == "mean"
    assert (control["sample_size"], treatment["sample_size"]) == (USERS, USERS)
    assert (control["conversions"], treatment["conversions"]) == (3, 4)
    # 5 events over 10 users and 8 over 10; the stray events count nowhere.
    assert control["mean"] == pytest.approx(0.5, rel=1e-12)
    assert treatment["mean"] == pytest.approx(0.8, rel=1e-12)
    for variant, y in ((control, COUNT_CONTROL_Y), (treatment, COUNT_TREATMENT_Y)):
        assert variant["std_dev"] == pytest.approx(np.std(y, ddof=1), rel=1e-9)
    assert treatment["statistical_test_used"] == "welch_t_test"
    assert treatment["p_value"] == pytest.approx(welch.pvalue, rel=1e-9)
    assert treatment["absolute_difference"] == pytest.approx(0.3, rel=1e-9)
    assert treatment["absolute_difference_ci"] == pytest.approx(
        list(welch.confidence_interval(0.95)), rel=1e-9
    )
    assert treatment["relative_improvement_pct"] == pytest.approx(60.0, rel=1e-9)


#: Values a count does not read: each of these events counts 1.  The first
#: five fail ``value_totals``' predicate; -5 passes it.
ODD_VALUES = [None, math.nan, math.inf, -math.inf, 1e308, -5.0]


@pytest.fixture
def count_with_values(admin_client, db_session, make_experiment):
    exp = _make(db_session, make_experiment, MetricType.COUNT, 4)
    for value in ODD_VALUES:
        _track(admin_client, exp, _user(exp, "control", 0), value, send_value=True)
    _track(admin_client, exp, _user(exp, "treatment", 0), 3.0)
    yield exp
    _cleanup(db_session, exp)


def test_a_count_does_not_read_the_value(admin_client, count_with_values):
    _, control, treatment = _arms(_results(admin_client, count_with_values))
    # One user sent six events, whatever their values: 6 over 4 users.
    assert control["mean"] == pytest.approx(len(ODD_VALUES) / 4, rel=1e-12)
    assert control["conversions"] == 1
    assert treatment["mean"] == pytest.approx(1 / 4, rel=1e-12)


# ---------------------------------------------------------------------------
# Duration and custom: each user's total value, as revenue
# ---------------------------------------------------------------------------

#: (arm, user index, value or None).  A repeat sender, a negative value, an
#: event with no value and one beyond the bound, which adds nothing.
VALUE_EVENTS = [
    ("control", 0, 12.5),
    ("control", 1, 30.0),
    ("control", 2, 3.0),
    ("control", 2, 4.25),
    ("control", 3, None),
    ("treatment", 0, 40.0),
    ("treatment", 0, 20.0),
    ("treatment", 1, 18.5),
    ("treatment", 2, -2.0),
    ("treatment", 2, 9.0),
    ("treatment", 3, 7.75),
    ("treatment", 4, 1e13),
]
VALUE_CONTROL_Y = _per_user([12.5, 30.0, 7.25])
VALUE_TREATMENT_Y = _per_user([60.0, 18.5, 7.0, 7.75])


@pytest.fixture(params=[MetricType.DURATION, MetricType.CUSTOM], ids=lambda t: t.value)
def value_experiment(request, admin_client, db_session, make_experiment):
    exp = _make(db_session, make_experiment, request.param, USERS)
    for arm, i, value in VALUE_EVENTS:
        _track(admin_client, exp, _user(exp, arm, i), value)
    _stray_events(db_session, admin_client, exp, value=1000.0)
    yield exp
    _cleanup(db_session, exp)


def test_duration_and_custom_add_up_each_users_values(admin_client, value_experiment):
    metric, control, treatment = _arms(_results(admin_client, value_experiment))
    welch = stats.ttest_ind(VALUE_TREATMENT_Y, VALUE_CONTROL_Y, equal_var=False)

    assert metric["metric_type"] in ("duration", "custom")
    assert metric["analysis"] == "mean"
    # The senders, the no-value one and the one beyond the bound included.
    assert (control["conversions"], treatment["conversions"]) == (4, 5)
    assert control["mean"] == pytest.approx(49.75 / USERS, rel=1e-12)
    assert treatment["mean"] == pytest.approx(93.25 / USERS, rel=1e-12)
    for variant, y in ((control, VALUE_CONTROL_Y), (treatment, VALUE_TREATMENT_Y)):
        assert variant["std_dev"] == pytest.approx(np.std(y, ddof=1), rel=1e-9)
    assert treatment["statistical_test_used"] == "welch_t_test"
    assert treatment["p_value"] == pytest.approx(welch.pvalue, rel=1e-9)
    assert treatment["absolute_difference_ci"] == pytest.approx(
        list(welch.confidence_interval(0.95)), rel=1e-9
    )


# ---------------------------------------------------------------------------
# An experiment already running: the recommendation can change (PE-5)
# ---------------------------------------------------------------------------


@pytest.fixture
def running_count_primary(db_session, make_experiment):
    """100 users per arm.  Control user i sent i % 4 events, treatment user i
    sent i % 5, none with a value: 75 and 80 senders, 1.5 and 2.0 events per
    user."""
    exp = _make(
        db_session, make_experiment, MetricType.COUNT, 100, minimum_sample_size=100
    )
    ids = {v.name: v.id for v in exp.variants}
    at = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    db_session.add_all(
        Event(
            event_type="article_read",
            event_name="article_read",
            user_id=_user(exp, arm, i),
            experiment_id=exp.id,
            variant_id=ids[arm],
            created_at=at,
        )
        for arm, per_user in (("control", 4), ("treatment", 5))
        for i in range(100)
        for _ in range(i % per_user)
    )
    db_session.commit()
    yield exp
    _cleanup(db_session, exp)


def test_a_count_primary_can_change_its_recommendation(
    admin_client, db_session, running_count_primary
):
    """The same rows: as a conversion (the analysis before #439) the share of
    senders, 75 against 80 of 100, is not significant (Fisher p 0.4985); as a
    count, 1.5 against 2.0 events per user is (Welch p 0.0064), so the
    recommendation moves from CONTINUE_TESTING to SHIP_VARIANT."""
    exp = running_count_primary
    as_count = _results(admin_client, exp)
    _, _, treatment = _arms(as_count)
    assert treatment["p_value"] == pytest.approx(0.006359822708272411, rel=1e-9)
    assert as_count["summary"]["recommendation"] == "SHIP_VARIANT"
    assert as_count["summary"]["winning_variant_id"] == treatment["variant_id"]

    db_session.query(Metric).filter(Metric.experiment_id == exp.id).update(
        {"metric_type": MetricType.CONVERSION}, synchronize_session=False
    )
    db_session.commit()
    as_conversion = _results(admin_client, exp)
    metric, _, treatment = _arms(as_conversion)
    assert metric["analysis"] == "proportion"
    assert treatment["p_value"] == pytest.approx(0.4985, abs=5e-5)
    assert as_conversion["summary"]["recommendation"] == "CONTINUE_TESTING"


# ---------------------------------------------------------------------------
# Snapshot history: an earlier day keeps the engine and analysis it recorded
# ---------------------------------------------------------------------------


def test_an_earlier_days_snapshot_keeps_its_engine_and_analysis(
    admin_client, db_session, count_experiment
):
    """The frequentist row of an earlier day is not rewritten: it keeps the
    engine version and the payload it was recorded with.  Today's row is the
    running engine's, with the count metric analysed as a mean."""
    exp = count_experiment
    yesterday = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ) - timedelta(days=1)
    earlier = AnalysisSnapshot(
        experiment_id=exp.id,
        kind="frequentist",
        engine_version="1.4.0",
        as_of=yesterday,
        payload={"metrics": [{"metric_type": "count", "analysis": "proportion"}]},
    )
    db_session.add(earlier)
    db_session.commit()
    earlier_id = earlier.id

    _results(admin_client, exp)

    db_session.expire_all()
    rows = (
        db_session.query(AnalysisSnapshot)
        .filter(
            AnalysisSnapshot.experiment_id == exp.id,
            AnalysisSnapshot.kind == "frequentist",
        )
        .order_by(AnalysisSnapshot.as_of)
        .all()
    )
    assert len(rows) == 2
    kept, today = rows
    assert kept.id == earlier_id
    assert kept.engine_version == "1.4.0"
    assert kept.payload == {
        "metrics": [{"metric_type": "count", "analysis": "proportion"}]
    }
    assert today.engine_version == ENGINE_VERSION
    assert [m["analysis"] for m in today.payload["metrics"]] == ["mean"]
