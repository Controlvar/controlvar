"""
``GET /api/v1/results/{id}/cuped`` adjusts for each user's own history (#217).

Each test builds an experiment with exact counts, so every number is known:
the users of each arm, which of them converted after assignment (tagged with
the experiment, as ``/results`` counts them) and which sent the covariate
event before (untagged, stored before the assignment).

* S5: every treatment is compared with the control.
* S6: the sample sizes and the unadjusted effect are ``/results``' own.
* C1-C3: the interval is at the stored ``confidence_level``, the correction is
  the stored ``correction_method`` across the treatments, and both are in the
  response.
* N1/N3/N4: a user with no history is X = 0 and still counted; a treatment
  with no users is n 0 with a reason; zero coverage leaves the label GA.
* W2: post-assignment events of the covariate's own name never enter X, and
  X is balanced between the arms.
* W3: the window and the receive guard hold under a Los Angeles process time
  zone and database session.
* X1-X4: an unexpected failure answers 500 with the fixed text; a metric that
  cannot be computed is listed with a reason; the lookback setting is read.
* The not-found mapping: only a missing experiment answers 404, and neither
  ``SufficientStatsNotComputed`` nor an unreadable stored timestamp does.
* EM 6: ``cuped_plus`` is computed as ``cuped`` and each row says so.
* Volume: the number of SQL statements does not grow with the users.

Rows created here are deleted in teardown: the shared test database is not
truncated between tests.
"""

import logging
import os
import time
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy import text
from sqlalchemy.engine import Engine

from backend.app.api.v1.endpoints import results as results_module
from backend.app.core.analysis_status import ANALYSIS_STATUS
from backend.app.core.database_config import get_schema_name
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
    BinomialVariant,
    SufficientStatsNotComputed,
    adjusted_p_values,
    cuped_metric_result,
    two_sided_z,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

URL = "/api/v1/results/{}/cuped"

#: Every assignment in these tests happens at this naive UTC moment, plus the
#: user's index in seconds; history is a day earlier, outcomes an hour later.
BASE = datetime(2026, 9, 20, 15, 0, 0, 250_000)


class Arm(SimpleNamespace):
    """``n`` users; ``converted`` of them convert; ``history`` of them have
    pre-period history; ``both`` of the converters are among the history."""


@pytest.fixture
def built(db_session, make_experiment):
    """Builds experiments and deletes every row they made afterwards."""
    made = []

    def _build(
        arms,
        *,
        method="cuped",
        lookback=7,
        confidence_level=0.95,
        correction_method="benjamini_hochberg",
        metrics=None,
        covariate_metric_id=None,
        history_days_before=1,
    ):
        suffix = uuid.uuid4().hex[:8]
        config = {"method": method, "covariate_lookback_days": lookback}
        if covariate_metric_id is not None:
            config["covariate_metric_id"] = covariate_metric_id
        exp = make_experiment(
            name=f"CUPED {suffix}",
            key=f"cuped-{suffix}",
            status=ExperimentStatus.ACTIVE,
            experiment_type=ExperimentType.A_B,
            start_date=BASE - timedelta(days=1),
            variance_reduction_config=config,
            confidence_level=confidence_level,
            correction_method=correction_method,
        )
        made.append(exp.id)
        variants = {}
        for name, arm in arms.items():
            variants[name] = Variant(
                experiment_id=exp.id,
                name=name,
                is_control=(name == "control"),
                traffic_allocation=100 // len(arms),
            )
        db_session.add_all(variants.values())
        for spec in metrics or [("Purchase", "purchase", MetricType.CONVERSION)]:
            name, event_name, metric_type = spec
            db_session.add(
                Metric(
                    experiment_id=exp.id,
                    name=name,
                    event_name=event_name,
                    metric_type=metric_type,
                    is_primary=name == "Purchase",
                )
            )
        db_session.commit()
        rows = []
        for name, arm in arms.items():
            variant = variants[name]
            for i in range(arm.n):
                user = f"{suffix}-{name}-{i:05d}"
                assigned = BASE + timedelta(seconds=i)
                rows.append(
                    Assignment(
                        experiment_id=exp.id,
                        variant_id=variant.id,
                        user_id=user,
                        created_at=assigned,
                    )
                )
                converts = i < arm.converted
                # The first `both` converters have history, then the
                # non-converters fill the rest of `history`.
                if converts:
                    has_history = i < arm.both
                else:
                    has_history = i - arm.converted < arm.history - arm.both
                if converts:
                    rows.append(
                        Event(
                            event_type="purchase",
                            event_name="purchase",
                            user_id=user,
                            experiment_id=exp.id,
                            variant_id=variant.id,
                            created_at=(assigned + timedelta(hours=1)).isoformat(),
                            updated_at=assigned + timedelta(hours=1),
                        )
                    )
                if has_history:
                    happened = assigned - timedelta(days=history_days_before)
                    rows.append(
                        Event(
                            event_type="purchase",
                            event_name="purchase",
                            user_id=user,
                            created_at=happened.isoformat(),
                            updated_at=happened,
                        )
                    )
        db_session.add_all(rows)
        db_session.commit()
        db_session.refresh(exp)
        return exp

    yield _build

    db_session.rollback()
    if made:
        users = (
            db_session.query(Assignment.user_id)
            .filter(Assignment.experiment_id.in_(made))
            .subquery()
        )
        db_session.query(Event).filter(Event.user_id.in_(users)).delete(
            synchronize_session=False
        )
        for model in (AnalysisSnapshot, Assignment):
            db_session.query(model).filter(model.experiment_id.in_(made)).delete(
                synchronize_session=False
            )
        db_session.commit()


def _get(client, exp):
    response = client.get(URL.format(exp.id))
    assert response.status_code == 200, response.text
    return response.json()


def _expected(arms, **kwargs):
    """``cuped_metric_result`` on the sums the arms were built with."""
    sums = []
    for name, arm in arms.items():
        variant = BinomialVariant(name, name, name == "control")
        sums.append(
            (
                variant,
                arm.n,
                float(arm.converted),
                float(arm.history),
                float(arm.converted),
                float(arm.history),
                float(arm.both),
            )
        )
    kwargs.setdefault("confidence_level", 0.95)
    kwargs.setdefault("correction_method", "benjamini_hochberg")
    return cuped_metric_result(
        sums,
        kwargs.pop("confidence_level"),
        kwargs.pop("correction_method"),
        metric=SimpleNamespace(id="m", name="Purchase"),
        **kwargs,
    )


TWO = {
    "control": Arm(n=200, converted=20, history=60, both=12),
    "treatment": Arm(n=200, converted=36, history=60, both=20),
}


# ---------------------------------------------------------------------------
# The numbers
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_cuped_recovers_the_seeded_history(admin_client, built):
    """X is each user's own history: the numbers are the arms' exact sums."""
    exp = built(TWO)

    body = _get(admin_client, exp)

    [row] = body["metrics"]
    [want] = _expected(TWO)
    assert row["theta"] == pytest.approx(want["theta"], rel=1e-12)
    assert row["theta"] > 0.1
    assert row["variance_reduction_pct"] == pytest.approx(
        want["variance_reduction_pct"], rel=1e-9
    )
    assert row["variance_reduction_pct"] > 1.0
    assert row["adjusted_effect"] == pytest.approx(want["adjusted_effect"], rel=1e-12)
    assert row["covariate_coverage_pct"] == 30.0
    assert row["covariate_event_name"] == "purchase"
    assert row["unavailable_reason"] is None


@pytest.mark.regression
def test_cuped_reports_every_treatment(admin_client, built):
    """S5: three treatments, three rows, each naming its treatment."""
    arms = {
        "control": Arm(n=150, converted=15, history=40, both=8),
        "t1": Arm(n=150, converted=18, history=40, both=9),
        "t2": Arm(n=150, converted=24, history=40, both=12),
        "t3": Arm(n=150, converted=30, history=40, both=15),
    }
    exp = built(arms)

    body = _get(admin_client, exp)

    assert sorted(r["variant_name"] for r in body["metrics"]) == ["t1", "t2", "t3"]
    control = next(v for v in exp.variants if v.is_control)
    for row in body["metrics"]:
        assert row["control_variant_id"] == str(control.id)
        assert row["control_sample_size"] == 150
        assert row["treatment_sample_size"] == 150


@pytest.mark.regression
def test_s6_sizes_and_conversions_are_the_results_routes(
    admin_client, built, db_session
):
    """With method none, n and the unadjusted effect are exactly /results'.

    Three control converters buy twice: users are counted, not events."""
    exp = built(TWO, method="none")
    db_session.add_all(
        Event(
            event_type="purchase",
            event_name="purchase",
            user_id=assignment.user_id,
            experiment_id=exp.id,
            variant_id=assignment.variant_id,
            created_at=(assignment.created_at + timedelta(hours=2)).isoformat(),
            updated_at=assignment.created_at + timedelta(hours=2),
        )
        for assignment in db_session.query(Assignment)
        .filter(Assignment.experiment_id == exp.id)
        .order_by(Assignment.user_id)
        .limit(3)
    )
    db_session.commit()

    cuped = _get(admin_client, exp)
    results = admin_client.get(f"/api/v1/results/{exp.id}", params={"use_cache": False})
    assert results.status_code == 200, results.text

    by_name = {v["variant_name"]: v for v in results.json()["metrics"][0]["variants"]}
    [row] = cuped["metrics"]
    assert row["control_sample_size"] == by_name["control"]["sample_size"] == 200
    assert row["treatment_sample_size"] == by_name["treatment"]["sample_size"] == 200
    assert by_name["control"]["conversions"] == 20
    assert by_name["treatment"]["conversions"] == 36
    assert row["unadjusted_effect"] == pytest.approx(
        by_name["treatment"]["mean"] - by_name["control"]["mean"], rel=1e-12
    )
    assert row["theta"] == 0.0
    assert row["adjusted_effect"] == row["unadjusted_effect"]
    # No covariate is read for none.
    assert row["covariate_coverage_pct"] is None


@pytest.mark.regression
def test_cuped_uses_the_stored_confidence_level(admin_client, built):
    """C1: at a stored 0.90 the half-width is z(0.90) times the SE."""
    exp = built(TWO, confidence_level=0.90)

    body = _get(admin_client, exp)

    [row] = body["metrics"]
    half = (row["adjusted_ci_upper"] - row["adjusted_ci_lower"]) / 2
    assert half == pytest.approx(two_sided_z(0.90) * row["adjusted_se"], rel=1e-12)
    assert half != pytest.approx(1.959964 * row["adjusted_se"], rel=1e-3)
    # C3: the settings it used are in the response.
    assert body["confidence_level"] == 0.90
    assert body["correction_method"] == "benjamini_hochberg"
    assert body["covariate_lookback_days"] == 7
    assert body["method"] == "cuped"


def test_c2_the_stored_correction_applies_across_three_treatments(admin_client, built):
    arms = {
        "control": Arm(n=300, converted=30, history=90, both=18),
        "t1": Arm(n=300, converted=36, history=90, both=20),
        "t2": Arm(n=300, converted=45, history=90, both=25),
        "t3": Arm(n=300, converted=51, history=90, both=27),
    }
    exp = built(arms, correction_method="benjamini_hochberg")

    rows = {r["variant_name"]: r for r in _get(admin_client, exp)["metrics"]}

    raw = [rows[name]["adjusted_p_value"] for name in ("t1", "t2", "t3")]
    expected = adjusted_p_values(raw, "benjamini_hochberg")
    assert [rows[n]["corrected_p_value"] for n in ("t1", "t2", "t3")] == expected
    assert expected != raw


def test_c2_an_unknown_stored_correction_answers_500(admin_client, built, db_session):
    exp = built(TWO)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            results_module,
            "resolve_analysis_settings",
            lambda experiment: SimpleNamespace(
                confidence_level=0.95, correction_method="holm"
            ),
        )
        response = admin_client.get(URL.format(exp.id))
    assert response.status_code == 500
    assert response.json()["detail"].startswith(
        "Could not compute the CUPED results (request ID: "
    )


# ---------------------------------------------------------------------------
# Users and coverage
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_n1_users_with_no_history_stay_in_with_x_zero(admin_client, built):
    arms = {
        "control": Arm(n=100, converted=10, history=7, both=3),
        "treatment": Arm(n=100, converted=14, history=5, both=2),
    }
    exp = built(arms)

    [row] = _get(admin_client, exp)["metrics"]

    assert row["control_sample_size"] == 100
    assert row["treatment_sample_size"] == 100
    assert row["covariate_coverage_pct"] == 6.0


@pytest.mark.regression
def test_cuped_variant_without_users_is_not_one_user(admin_client, built):
    """N3: a treatment nobody was assigned to is n 0, with a reason."""
    arms = {
        "control": Arm(n=50, converted=5, history=10, both=2),
        "empty": Arm(n=0, converted=0, history=0, both=0),
        "treatment": Arm(n=50, converted=9, history=10, both=4),
    }
    exp = built(arms)

    rows = {r["variant_name"]: r for r in _get(admin_client, exp)["metrics"]}

    assert rows["empty"]["treatment_sample_size"] == 0
    assert rows["empty"]["unavailable_reason"] == "fewer_than_2_units"
    assert rows["empty"]["adjusted_effect"] is None
    assert rows["treatment"]["unavailable_reason"] is None


def test_n2_n4_zero_coverage_is_theta_zero_and_still_ga(admin_client, built):
    arms = {
        "control": Arm(n=80, converted=8, history=0, both=0),
        "treatment": Arm(n=80, converted=12, history=0, both=0),
    }
    exp = built(arms)

    body = _get(admin_client, exp)

    [row] = body["metrics"]
    assert row["theta"] == 0.0
    assert row["variance_reduction_pct"] == 0.0
    assert row["covariate_coverage_pct"] == 0.0
    assert row["adjusted_effect"] == row["unadjusted_effect"]
    assert body["analysis_status"] == "ga"
    assert body["analysis_notice"] is None
    assert ANALYSIS_STATUS["cuped"].status == "ga"


# ---------------------------------------------------------------------------
# W2 contamination, W3 time zone
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_w2_outcomes_after_assignment_never_enter_the_covariate(admin_client, built):
    """Treatment converts far more after assignment, under the covariate's own
    event name; history is identical in both arms.  X must stay balanced, and
    the adjusted effect must be the one the pre-period sums give."""
    arms = {
        "control": Arm(n=300, converted=15, history=90, both=9),
        "treatment": Arm(n=300, converted=90, history=90, both=27),
    }
    exp = built(arms)

    [row] = _get(admin_client, exp)["metrics"]

    # Balance: both arms 90 of 300 with history.
    assert row["covariate_coverage_pct"] == 30.0
    [want] = _expected(arms)
    assert row["adjusted_effect"] == pytest.approx(want["adjusted_effect"], rel=1e-12)


@pytest.mark.regression
def test_w3_window_and_guard_hold_in_los_angeles(
    admin_client, built, db_session, monkeypatch
):
    """Process TZ and PG session both America/Los_Angeles; fixtures naive UTC.

    History three hours before the assignment (the same UTC day) must count,
    and history received three hours after it must not.  An aware conversion
    of either side shifts the comparison by 7-8 hours and flips both.
    """
    monkeypatch.setenv("PGTZ", "America/Los_Angeles")
    saved_tz = os.environ.get("TZ")
    os.environ["TZ"] = "America/Los_Angeles"
    time.tzset()
    try:
        arms = {
            "control": Arm(n=40, converted=4, history=0, both=0),
            "treatment": Arm(n=40, converted=8, history=0, both=0),
        }
        exp = built(arms)
        assignments = (
            db_session.query(Assignment)
            .filter(Assignment.experiment_id == exp.id)
            .order_by(Assignment.user_id)
            .all()
        )
        same_day, backfilled = assignments[0], assignments[1]
        events = [
            Event(
                event_type="purchase",
                event_name="purchase",
                user_id=same_day.user_id,
                created_at=(same_day.created_at - timedelta(hours=3)).isoformat(),
                updated_at=same_day.created_at - timedelta(hours=3),
            ),
            Event(
                event_type="purchase",
                event_name="purchase",
                user_id=backfilled.user_id,
                created_at=(backfilled.created_at - timedelta(hours=3)).isoformat(),
                updated_at=backfilled.created_at + timedelta(hours=3),
            ),
        ]
        db_session.add_all(events)
        db_session.commit()
        # The probes: a new connection's session, and this process, really
        # are in Los Angeles.
        zone = db_session.execute(text("SHOW TIME ZONE")).scalar()
        assert zone == "America/Los_Angeles"
        assert time.strftime("%Z") in ("PDT", "PST")

        [row] = _get(admin_client, exp)["metrics"]

        # One user of 80 has history: the same-day one.
        assert row["covariate_coverage_pct"] == pytest.approx(100 / 80)
    finally:
        if saved_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = saved_tz
        time.tzset()


# ---------------------------------------------------------------------------
# Reasons, errors and the 404
# ---------------------------------------------------------------------------


def test_x2_no_control_and_no_treatment_are_reasons(admin_client, built):
    only_treatments = built(
        {
            "a": Arm(n=10, converted=1, history=2, both=1),
            "b": Arm(n=10, converted=2, history=2, both=1),
        }
    )
    only_control = built({"control": Arm(n=10, converted=1, history=2, both=1)})

    [row] = _get(admin_client, only_treatments)["metrics"]
    assert row["unavailable_reason"] == "no_control_variant"
    assert row["variant_id"] is None
    [row] = _get(admin_client, only_control)["metrics"]
    assert row["unavailable_reason"] == "no_treatment_variant"


def test_x2_a_mean_metric_is_listed_with_a_reason(admin_client, built):
    exp = built(
        TWO,
        metrics=[
            ("Purchase", "purchase", MetricType.CONVERSION),
            ("Revenue", "purchase", MetricType.REVENUE),
        ],
    )

    rows = {r["metric_name"]: r for r in _get(admin_client, exp)["metrics"]}

    assert rows["Revenue"]["unavailable_reason"] == "not_a_proportion_metric"
    assert rows["Revenue"]["adjusted_effect"] is None
    assert rows["Purchase"]["unavailable_reason"] is None


def test_x3_a_covariate_metric_that_is_not_the_experiments(admin_client, built):
    exp = built(TWO, covariate_metric_id=str(uuid.uuid4()))

    [row] = _get(admin_client, exp)["metrics"]

    assert row["unavailable_reason"] == "covariate_metric_not_found"
    assert row["adjusted_effect"] is None


def test_x3_a_covariate_metric_of_the_experiment_is_read(
    admin_client, built, db_session
):
    exp = built(
        TWO,
        metrics=[
            ("Purchase", "purchase", MetricType.CONVERSION),
            ("Signup", "signup", MetricType.CONVERSION),
        ],
    )
    signup = next(m for m in exp.metric_definitions if m.event_name == "signup")
    exp.variance_reduction_config = {
        "method": "cuped",
        "covariate_metric_id": str(signup.id),
    }
    db_session.commit()

    rows = {r["metric_name"]: r for r in _get(admin_client, exp)["metrics"]}

    # Nobody sent a signup before assignment: X is the signup history, all 0.
    assert rows["Purchase"]["covariate_event_name"] == "signup"
    assert rows["Purchase"]["covariate_coverage_pct"] == 0.0
    assert rows["Purchase"]["theta"] == 0.0


def test_cuped_without_a_covariate_metric_reads_each_metrics_own_event(
    admin_client, built
):
    """#1194: ``covariate_metric_id`` is optional, and absent it each metric's
    own event before the user's exposure is the history (the schema's
    description says so).

    Both metrics here have no covariate configured. Only ``purchase`` has
    history (30% of users), so the Purchase row reads it and the Signup row
    reads the signup history, which nobody has.
    """
    exp = built(
        TWO,
        metrics=[
            ("Purchase", "purchase", MetricType.CONVERSION),
            ("Signup", "signup", MetricType.CONVERSION),
        ],
    )
    assert "covariate_metric_id" not in exp.variance_reduction_config

    rows = {r["metric_name"]: r for r in _get(admin_client, exp)["metrics"]}

    assert rows["Purchase"]["covariate_event_name"] == "purchase"
    assert rows["Purchase"]["covariate_coverage_pct"] == 30.0
    assert rows["Purchase"]["unavailable_reason"] is None
    assert rows["Signup"]["covariate_event_name"] == "signup"
    assert rows["Signup"]["covariate_coverage_pct"] == 0.0


@pytest.mark.regression
def test_pe10_a_metric_with_no_event_name_never_reads_every_conversion(
    admin_client, built
):
    exp = built(TWO, metrics=[("Purchase", "", MetricType.CONVERSION)])

    [row] = _get(admin_client, exp)["metrics"]

    assert row["unavailable_reason"] == "metric_has_no_event_name"
    assert row["covariate_coverage_pct"] is None


def test_x4_the_lookback_setting_is_read(admin_client, built):
    """History 10 days before assignment: in a 14-day window, not in 7."""
    seven = built(TWO, lookback=7, history_days_before=10)
    fourteen = built(TWO, lookback=14, history_days_before=10)

    [row7] = _get(admin_client, seven)["metrics"]
    [row14] = _get(admin_client, fourteen)["metrics"]

    assert row7["covariate_coverage_pct"] == 0.0
    assert row14["covariate_coverage_pct"] == 30.0


def test_em6_cuped_plus_is_computed_as_cuped(admin_client, built):
    plus = built(TWO, method="cuped_plus")
    plain = built(TWO, method="cuped")

    plus_body = _get(admin_client, plus)
    [plus_row] = plus_body["metrics"]
    [plain_row] = _get(admin_client, plain)["metrics"]

    assert plus_body["method"] == "cuped_plus"
    assert plus_row["method"] == "cuped"
    assert plus_body["analysis_notice"] is None
    for key in ("theta", "adjusted_effect", "adjusted_se", "variance_reduction_pct"):
        assert plus_row[key] == plain_row[key]


def test_not_found_answers_404(admin_client):
    response = admin_client.get(URL.format(uuid.uuid4()))
    assert response.status_code == 404
    assert response.json()["detail"] == "Experiment not found"


@pytest.mark.regression
def test_not_computed_is_a_reason_row_never_a_404(admin_client, built, monkeypatch):
    """A ``SufficientStatsNotComputed`` (a ValueError) gives a 200 and a reason."""
    exp = built(TWO)

    def refuse(*args, **kwargs):
        raise SufficientStatsNotComputed("fewer_than_2_units", "secret text 7f3a")

    monkeypatch.setattr(results_module, "cuped_metric_result", refuse)
    response = admin_client.get(URL.format(exp.id))

    assert response.status_code == 200, response.text
    [row] = response.json()["metrics"]
    assert row["unavailable_reason"] == "fewer_than_2_units"
    assert "secret text" not in response.text


@pytest.mark.regression
def test_an_unreadable_stored_timestamp_is_skipped_and_logged_once(
    admin_client, built, db_session, caplog
):
    """'2026-10-01 garbage' inside the window: 200, skipped, one WARNING line
    with the metric id and the count, and none of the stored text."""
    arms = {
        "control": Arm(n=20, converted=2, history=4, both=1),
        "treatment": Arm(n=20, converted=4, history=4, both=1),
    }
    exp = built(arms)
    user = (
        db_session.query(Assignment.user_id)
        .filter(Assignment.experiment_id == exp.id)
        .first()[0]
    )
    for _ in range(2):
        db_session.execute(
            text(
                f"INSERT INTO {get_schema_name()}.events (id, event_type, event_name, "
                "user_id, created_at, updated_at) VALUES (:id, 'purchase', "
                "'purchase', :u, '2026-09-19 garbage', :r)"
            ),
            {"id": uuid.uuid4(), "u": user, "r": BASE - timedelta(days=2)},
        )
    db_session.commit()

    with caplog.at_level(logging.WARNING, logger=results_module.logger.name):
        response = admin_client.get(URL.format(exp.id))

    assert response.status_code == 200, response.text
    assert "garbage" not in response.text
    [row] = response.json()["metrics"]
    assert row["covariate_coverage_pct"] == 20.0
    lines = [
        r.getMessage() for r in caplog.records if "could not read" in r.getMessage()
    ]
    metric = exp.metric_definitions[0]
    assert lines == [
        "CUPED skipped stored event timestamps it could not read, by metric: "
        f"{metric.id}=2"
    ]


@pytest.mark.regression
def test_cuped_failed_metric_is_reported(admin_client, built, monkeypatch):
    """X1: an unexpected failure answers 500 with the fixed text, logged."""
    exp = built(TWO)

    def explode(*args, **kwargs):
        raise ValueError("Invalid isoformat string: 'client text 51c2'")

    monkeypatch.setattr(results_module, "cuped_metric_result", explode)
    response = admin_client.get(URL.format(exp.id))

    assert response.status_code == 500
    assert response.json()["detail"].startswith(
        "Could not compute the CUPED results (request ID: "
    )
    assert "client text" not in response.text


# ---------------------------------------------------------------------------
# Volume: statements do not grow with users
# ---------------------------------------------------------------------------


def _statements(client, exp):
    count = 0

    def _count(*args, **kwargs):
        nonlocal count
        count += 1

    sa_event.listen(Engine, "before_cursor_execute", _count)
    try:
        _get(client, exp)
    finally:
        sa_event.remove(Engine, "before_cursor_execute", _count)
    return count


def test_volume_the_statement_count_does_not_grow_with_users(admin_client, built):
    small = built(
        {
            "control": Arm(n=50, converted=5, history=15, both=3),
            "treatment": Arm(n=50, converted=7, history=15, both=4),
        }
    )
    large = built(
        {
            "control": Arm(n=1000, converted=100, history=300, both=60),
            "treatment": Arm(n=1000, converted=140, history=300, both=80),
        }
    )

    assert _statements(admin_client, small) == _statements(admin_client, large)


# ---------------------------------------------------------------------------
# Winsorization on a conversion metric
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_winsorization_on_a_conversion_metric_is_a_reason_not_zeroed(
    admin_client, built
):
    """Clipping 0/1 outcomes at the 99th percentile turns every conversion
    into 0 when fewer than 1% convert: 10 of 2,000 here.  The comparison is
    listed as needing a mean metric instead of reported as no conversions."""
    arms = {
        "control": Arm(n=2000, converted=10, history=0, both=0),
        "treatment": Arm(n=2000, converted=14, history=0, both=0),
    }
    exp = built(arms, method="winsorization")

    body = _get(admin_client, exp)

    [row] = body["metrics"]
    assert row["unavailable_reason"] == "winsorization_needs_mean_metric"
    assert row["adjusted_control_mean"] is None
    assert row["method"] == "winsorization"
    assert row["control_sample_size"] == 2000
    assert body["method"] == "winsorization"
