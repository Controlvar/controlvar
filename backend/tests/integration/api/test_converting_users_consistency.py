"""
Every analysis endpoint counts converting users, the same way (#233).

A binary conversion metric's numerator is the number of assigned users with at
least one conversion event, not the number of conversion events.  Before the
fix ``/results`` (Fisher inputs, rates, CI, lift), ``/daily``, the summary,
``/sequential``, ``/bayesian`` and both segment breakdowns counted events,
while ``/cuped`` counted users.  A repeat purchaser therefore counted three
times on one endpoint and once on another, and a variant whose conversion
events outnumbered its users broke the analysis outright:

* ``/results`` answered 500 (a conversion rate above 100% made the CI's
  variance negative);
* ``/bayesian`` answered 500 (``conversions must be <= total``), and the
  ``bayesian_results`` block of ``/results`` was silently null.

The fixture below has a multi-conversion user in BOTH arms, a user with no
events at all, a conversion event from a user assigned to nothing, and a
``country`` on every event so the breakdowns can be summed back up.

Rows created here are deleted in fixture teardown because the shared test
database is not truncated between tests.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import event

from backend.app.core.bandit_scheduler import BanditScheduler
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
from backend.app.services.analysis_service import AnalysisService
from backend.app.services.event_matching import (
    count_converting_users,
    count_converting_users_any,
    first_conversion_times,
)
from backend.app.services.results_streaming_service import ResultsStreamingService
from backend.app.services.sequential_testing_service import SequentialTestingService

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

#: Per variant: users assigned, and users with at least one purchase.
EXPECTED = {
    "control": {"n": 5, "converted": 2},
    "treatment": {"n": 5, "converted": 3},
}


def _cleanup(db_session, exp):
    db_session.rollback()
    for model in (AnalysisSnapshot, Event, Assignment):
        db_session.query(model).filter(model.experiment_id == exp.id).delete(
            synchronize_session=False
        )
    db_session.commit()


def _new_experiment(db_session, make_experiment, label, **overrides):
    suffix = uuid.uuid4().hex[:8]
    start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ) - timedelta(days=3)
    exp = make_experiment(
        name=f"{label} {suffix}",
        key=f"{label.lower().replace(' ', '-')}-{suffix}",
        status=ExperimentStatus.ACTIVE,
        experiment_type=ExperimentType.A_B,
        start_date=start,
        sequential_testing_enabled=True,
        sequential_testing_config={"method": "msprt"},
        bayesian_enabled=True,
        bayesian_config={"prior_family": "beta", "alpha": 1.0, "beta": 1.0},
        **overrides,
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
                name="Purchase",
                event_name="purchase",
                metric_type=MetricType.CONVERSION,
                is_primary=True,
            ),
        ]
    )
    db_session.commit()
    db_session.refresh(exp)
    return exp, start


def _variants(exp):
    by_name = {v.name: v for v in exp.variants}
    return by_name["control"], by_name["treatment"]


def _event(exp, variant, user_id, name, country, at):
    return Event(
        event_type=name,
        event_name=name,
        user_id=user_id,
        experiment_id=exp.id,
        variant_id=variant.id,
        value=1.0,
        event_metadata={"country": country},
        created_at=at.isoformat(),
    )


@pytest.fixture
def experiment(db_session, make_experiment):
    """
    Five users per arm, two segments (``US``, ``DE``).

    control:   c0 buys 3 times on 2 days (US); c1 buys once (DE);
               c2 views only (US); c3 views only (DE); c4 has no events.
    treatment: t0 buys 4 times on 2 days (DE); t1 buys once (US);
               t2 buys twice on 1 day (US); t3 views only (DE); t4 no events.
    Plus a purchase from ``ghost``, who is assigned to nothing, tagged with the
    treatment variant.

    Events: control 2 users / 4 purchases; treatment 3 users / 7 purchases.
    """
    exp, start = _new_experiment(db_session, make_experiment, "Converting users")
    control, treatment = _variants(exp)
    day1 = start + timedelta(days=1, hours=12)
    day2 = start + timedelta(days=2, hours=12)
    rows = []
    plan = {
        control: [
            ("US", [day1, day1, day2]),
            ("DE", [day2]),
            ("US", []),
            ("DE", []),
            (None, None),
        ],
        treatment: [
            ("DE", [day1, day1 + timedelta(hours=1), day2, day2]),
            ("US", [day2]),
            ("US", [day1, day1 + timedelta(minutes=5)]),
            ("DE", []),
            (None, None),
        ],
    }
    for variant, users in plan.items():
        for i, (country, purchases) in enumerate(users):
            user_id = f"cu-{exp.key}-{variant.name}-{i}"
            rows.append(
                Assignment(experiment_id=exp.id, variant_id=variant.id, user_id=user_id)
            )
            if purchases is None:
                continue  # assigned, never seen again
            rows.append(_event(exp, variant, user_id, "page_view", country, day1))
            rows.extend(
                _event(exp, variant, user_id, "purchase", country, at)
                for at in purchases
            )
    rows.append(_event(exp, treatment, f"cu-{exp.key}-ghost", "purchase", "US", day1))
    db_session.add_all(rows)
    db_session.commit()
    db_session.refresh(exp)
    yield exp
    _cleanup(db_session, exp)


@pytest.fixture
def over_converted(db_session, make_experiment):
    """
    Conversion events outnumber assigned users in the control arm.

    control: 2 users, each buys 3 times (6 events, 2 users).
    treatment: 2 users, one buys once.
    """
    exp, start = _new_experiment(db_session, make_experiment, "Over converted")
    control, treatment = _variants(exp)
    day1 = start + timedelta(days=1, hours=12)
    rows = []
    for variant, purchases_per_user in ((control, [3, 3]), (treatment, [1, 0])):
        for i, purchases in enumerate(purchases_per_user):
            user_id = f"oc-{exp.key}-{variant.name}-{i}"
            rows.append(
                Assignment(experiment_id=exp.id, variant_id=variant.id, user_id=user_id)
            )
            rows.extend(
                _event(
                    exp, variant, user_id, "purchase", "US", day1 + timedelta(hours=k)
                )
                for k in range(purchases)
            )
    db_session.add_all(rows)
    db_session.commit()
    db_session.refresh(exp)
    yield exp
    _cleanup(db_session, exp)


def _get(client, path, **params):
    response = client.get(path, params={"use_cache": "false", **params})
    assert response.status_code == 200, response.text
    return response.json()


def _results_counts(results):
    primary = next(m for m in results["metrics"] if m["is_primary"])
    return {
        v["variant_name"]: {"n": v["sample_size"], "converted": v["conversions"]}
        for v in primary["variants"]
    }


def _sequential_inputs(client, exp):
    """The counts ``/sequential`` hands the mSPRT, captured on the way in."""
    captured = {}
    original = SequentialTestingService.run_sequential_analysis

    def spy(self, **kwargs):
        captured.update(kwargs)
        return original(self, **kwargs)

    with patch.object(SequentialTestingService, "run_sequential_analysis", spy):
        _get(client, f"/api/v1/results/{exp.id}/sequential")
    return {
        "control": {
            "n": captured["control_total"],
            "converted": captured["control_successes"],
        },
        "treatment": {
            "n": captured["treatment_total"],
            "converted": captured["treatment_successes"],
        },
    }


def _bayesian_counts(block, prior_alpha=1.0, prior_beta=1.0):
    """Invert the Beta posterior: alpha = a0 + conversions, beta = b0 + misses."""
    assert block is not None and block["is_enabled"] is True
    out = {}
    for v in block["variant_results"]:
        a = v["posterior"]["alpha"] - prior_alpha
        b = v["posterior"]["beta"] - prior_beta
        out[v["variant_key"]] = {"n": round(a + b), "converted": round(a)}
    return out


def _cumulative_counts(daily):
    out = {}
    for series in daily["series"]:
        last = series["cumulative"][-1]
        out[series["variant_name"]] = {
            "n": last["sample_size"],
            "converted": last["conversions"],
        }
    return out


class _SharedSession:
    """The test session, handed to the streaming service; its close() is a no-op."""

    def __init__(self, session):
        self._session = session

    def __getattr__(self, name):
        return getattr(self._session, name)

    def close(self):
        pass


def _streaming_counts(db_session, exp):
    """``GET /ws/experiments/{id}/results``'s snapshot, computed on the test session."""
    service = ResultsStreamingService(lambda: _SharedSession(db_session))
    snapshot = asyncio.run(service.get_live_snapshot(str(exp.id)))
    assert snapshot.get("event") == "results_update", snapshot
    return {
        v["name"]: {"n": v["participant_count"], "converted": v["conversion_count"]}
        for v in snapshot["variants"]
    }


def _bandit_counts(db_session, exp):
    """The bandit scheduler's PostgreSQL arm statistics (pulls, successes)."""
    control, treatment = _variants(exp)
    stats = BanditScheduler(db_session)._stats_from_postgres(
        exp.id, [str(control.id), str(treatment.id)], exp
    )
    assert stats is not None
    return {
        v.name: {"n": stats[str(v.id)].pulls, "converted": stats[str(v.id)].successes}
        for v in (control, treatment)
    }


def _breakdown_conversions(results):
    """Converting users and users seen, summed over the ``country`` segments."""
    totals = {
        "control": {"n": 0, "converted": 0},
        "treatment": {"n": 0, "converted": 0},
    }
    for segment in results["breakdown"]["segments"]:
        for v in segment["variants"]:
            totals[v["variant_name"]]["n"] += v["sample_size"]
            totals[v["variant_name"]]["converted"] += v["conversions"]
    return totals


def _segmented_conversions(db_session, exp):
    """Same sum over ``AnalysisService.get_segmented_results`` (the other breakdown)."""
    names = {str(v.id): v.name for v in exp.variants}
    totals = {
        "control": {"n": 0, "converted": 0},
        "treatment": {"n": 0, "converted": 0},
    }
    segmented = AnalysisService(db_session).get_segmented_results(
        experiment_id=exp.id, segment_by="country"
    )
    for segment in segmented["segments"]:
        for metric in segment["metrics"]:
            for v in metric["variants"]:
                totals[names[v["variant_id"]]]["n"] += v["sample_size"]
                totals[names[v["variant_id"]]]["converted"] += v["conversions"]
    return totals


def test_counts_agree_across_endpoints(admin_client, db_session, experiment):
    """C1: per variant, n and converting users agree on every analysis path."""
    exp = experiment
    base = f"/api/v1/results/{exp.id}"

    results = _get(admin_client, base, breakdown="country")
    assert _results_counts(results) == EXPECTED
    assert results["summary"]["total_conversions"] == 5

    assert _sequential_inputs(admin_client, exp) == EXPECTED
    assert _bayesian_counts(_get(admin_client, f"{base}/bayesian")) == EXPECTED
    assert _bayesian_counts(results["bayesian_results"]) == EXPECTED
    assert _cumulative_counts(_get(admin_client, f"{base}/daily")) == EXPECTED
    assert _streaming_counts(db_session, exp) == EXPECTED
    assert _bandit_counts(db_session, exp) == EXPECTED

    # /cuped with no variance_reduction_config is method "none": the
    # unadjusted means, i.e. converting users / assigned users.
    cuped = _get(admin_client, f"{base}/cuped")
    (metric,) = cuped["metrics"]
    assert metric["method"] == "none"
    assert metric["adjusted_control_mean"] == pytest.approx(2 / 5)
    assert metric["adjusted_treatment_mean"] == pytest.approx(3 / 5)

    export = admin_client.get(
        "/api/v1/export/variants",
        params={
            "format": "json",
            "start_date": (exp.created_at - timedelta(seconds=1)).isoformat(),
        },
    )
    assert export.status_code == 200, export.text
    exported = {
        r["variant_name"]: {"n": r["assignments"], "converted": r["conversions"]}
        for r in export.json()
        if r["experiment_id"] == str(exp.id)
    }
    assert exported == EXPECTED

    # Every assigned user is in exactly one segment (c4 and t4, who have no
    # events, in "unknown"), and every converting user with them, so both
    # breakdowns add up to the headline.
    for summed in (
        _breakdown_conversions(results),
        _segmented_conversions(db_session, exp),
    ):
        assert summed == EXPECTED


def test_daily_series_counts_each_user_on_the_day_of_the_first_conversion(
    admin_client, experiment
):
    """A repeat purchaser adds to the daily series once, on their first day."""
    daily = _get(admin_client, f"/api/v1/results/{experiment.id}/daily")
    by_name = {s["variant_name"]: s for s in daily["series"]}
    per_day = {
        name: [p["conversions"] for p in s["values"]] for name, s in by_name.items()
    }
    # Days: start, start+1, start+2, today.  c0 first buys on day 1, c1 on
    # day 2; t0 and t2 on day 1, t1 on day 2.
    assert per_day["control"][:3] == [0, 1, 1]
    assert per_day["treatment"][:3] == [0, 2, 1]
    assert sum(per_day["control"]) == 2
    assert sum(per_day["treatment"]) == 3


@pytest.mark.regression
def test_more_conversion_events_than_users_is_not_a_500(admin_client, over_converted):
    """#233: 6 purchase events from 2 control users used to 500 /results and /bayesian."""
    exp = over_converted
    base = f"/api/v1/results/{exp.id}"
    want = {"control": {"n": 2, "converted": 2}, "treatment": {"n": 2, "converted": 1}}

    results = _get(admin_client, base)
    assert _results_counts(results) == want
    primary = next(m for m in results["metrics"] if m["is_primary"])
    for v in primary["variants"]:
        assert 0.0 <= v["mean"] <= 1.0
        low, high = v["confidence_interval"]
        assert 0.0 <= low <= high <= 1.0
    assert _bayesian_counts(results["bayesian_results"]) == want

    assert _bayesian_counts(_get(admin_client, f"{base}/bayesian")) == want
    assert _sequential_inputs(admin_client, exp) == want


@pytest.fixture
def converts_outside_the_window(db_session, make_experiment):
    """
    A finished experiment (start: 5 days ago, end: 2 days ago) whose converting
    users bought outside that window: control c0 before start_date, treatment
    t0 after end_date, and treatment t1 on the middle day.
    """
    today = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    exp, start = _new_experiment(
        db_session,
        make_experiment,
        "Outside window",
        end_date=today - timedelta(days=2, seconds=1),
    )
    exp.start_date = today - timedelta(days=5)
    db_session.commit()
    control, treatment = _variants(exp)
    purchases = {
        (control, 0): today - timedelta(days=9),  # before start_date
        (treatment, 0): today - timedelta(hours=12),  # after end_date
        (treatment, 1): today - timedelta(days=4, hours=-12),  # inside
    }
    rows = []
    for variant in (control, treatment):
        for i in range(2):
            user_id = f"ow-{exp.key}-{variant.name}-{i}"
            rows.append(
                Assignment(experiment_id=exp.id, variant_id=variant.id, user_id=user_id)
            )
            at = purchases.get((variant, i))
            if at is not None:
                rows.append(_event(exp, variant, user_id, "purchase", "US", at))
    db_session.add_all(rows)
    db_session.commit()
    db_session.refresh(exp)
    yield exp
    _cleanup(db_session, exp)


def test_daily_series_ends_at_the_results_count_when_users_convert_outside_the_window(
    admin_client, converts_outside_the_window
):
    """A first conversion before start_date lands on the first day, one after
    end_date on the last day, so the cumulative series ends at /results'."""
    exp = converts_outside_the_window
    base = f"/api/v1/results/{exp.id}"

    converted = {
        name: counts["converted"]
        for name, counts in _results_counts(_get(admin_client, base)).items()
    }
    assert converted == {"control": 1, "treatment": 2}

    daily = _get(admin_client, f"{base}/daily")
    by_name = {s["variant_name"]: s for s in daily["series"]}
    assert {
        name: s["cumulative"][-1]["conversions"] for name, s in by_name.items()
    } == converted
    per_day = {n: [p["conversions"] for p in s["values"]] for n, s in by_name.items()}
    assert per_day["control"] == [1, 0, 0]
    assert per_day["treatment"] == [0, 1, 1]


@pytest.mark.regression
def test_conversion_counts_never_join_events_to_assignments_in_sql(
    db_session, experiment
):
    """
    #233 follow-up: on a freshly seeded database (no planner statistics yet)
    PostgreSQL ran the events-to-assignments join as a nested loop over a
    sequential scan: more than 8 s for the demo data in CI, on the event loop
    of the live-results stream, so docs/websocket-streaming.md's snapshot
    never arrived.  The counts are now read from ``events`` alone and the
    assignments looked up by id, so no statement touches both tables.  The
    bandit scheduler's PostgreSQL fallback joined the two tables in SQL until
    #338; it now counts the same way.
    """
    exp = experiment
    control, treatment = _variants(exp)
    statements = []
    engine = db_session.get_bind()

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        counts = {
            v.name: count_converting_users(db_session, exp.id, v.id, "purchase")
            for v in (control, treatment)
        }
        firsts = {
            v.name: len(first_conversion_times(db_session, exp.id, v.id, "purchase"))
            for v in (control, treatment)
        }
        total = count_converting_users_any(db_session, exp.id, ["purchase"])
        streamed = _streaming_counts(db_session, exp)
        bandit = _bandit_counts(db_session, exp)
    finally:
        event.remove(engine, "before_cursor_execute", record)

    converted = {k: v["converted"] for k, v in EXPECTED.items()}
    assert counts == converted
    assert firsts == converted
    assert total == 5
    assert streamed == EXPECTED
    assert bandit == EXPECTED
    joined = [s for s in statements if ".events" in s and ".assignments" in s]
    assert statements, "no SQL was captured"
    assert joined == [], joined
