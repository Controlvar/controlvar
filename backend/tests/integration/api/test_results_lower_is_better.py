"""
``GET /api/v1/results/{id}`` reads the primary metric's ``lower_is_better``
(#1377), through the real route and real rows.

Uninstalls: the treatment's users uninstall far less often than the
control's (10% against 30% of 200 each).  With ``lower_is_better`` true that
is the win: the treatment is the winner, the recommendation is to ship it,
and the Bayesian analysis gives it the probability to be best.  A metric row
whose ``lower_is_better`` is NULL (the column is nullable, and rows written
before it had a default can hold NULL) answers 200 and reads as higher is
better: the same drop is then a loss, and the control is kept.

Rows created here are deleted in fixture teardown because the shared test
database is not truncated between tests.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

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

pytestmark = [pytest.mark.integration, pytest.mark.requires_db, pytest.mark.regression]

#: Per arm: users assigned, and users with at least one uninstall event.
USERS = 200
UNINSTALLED = {"control": 60, "treatment": 20}


def _cleanup(db_session, exp):
    db_session.rollback()
    for model in (AnalysisSnapshot, Event, Assignment):
        db_session.query(model).filter(model.experiment_id == exp.id).delete(
            synchronize_session=False
        )
    db_session.commit()


@pytest.fixture
def experiment(db_session, make_experiment):
    suffix = uuid.uuid4().hex[:8]
    start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ) - timedelta(days=3)
    exp = make_experiment(
        name=f"Uninstall prompt {suffix}",
        key=f"uninstall-prompt-{suffix}",
        status=ExperimentStatus.ACTIVE,
        experiment_type=ExperimentType.A_B,
        start_date=start,
        bayesian_enabled=True,
        bayesian_config={"prior_family": "beta", "alpha": 1.0, "beta": 1.0},
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
                name="Uninstall",
                event_name="uninstall",
                metric_type=MetricType.CONVERSION,
                is_primary=True,
                minimum_sample_size=100,
                lower_is_better=True,
            ),
        ]
    )
    db_session.commit()
    db_session.refresh(exp)
    at = start + timedelta(days=1, hours=12)
    rows = []
    for variant in exp.variants:
        for i in range(USERS):
            user_id = f"lib-{exp.key}-{variant.name}-{i}"
            rows.append(
                Assignment(experiment_id=exp.id, variant_id=variant.id, user_id=user_id)
            )
            if i < UNINSTALLED[variant.name]:
                rows.append(
                    Event(
                        event_type="uninstall",
                        event_name="uninstall",
                        user_id=user_id,
                        experiment_id=exp.id,
                        variant_id=variant.id,
                        value=1.0,
                        created_at=at.isoformat(),
                    )
                )
    db_session.add_all(rows)
    db_session.commit()
    db_session.refresh(exp)
    yield exp
    _cleanup(db_session, exp)


def _results(client, exp):
    response = client.get(f"/api/v1/results/{exp.id}", params={"use_cache": "false"})
    assert response.status_code == 200, response.text
    return response.json()


def _ids(exp):
    by_name = {v.name: str(v.id) for v in exp.variants}
    return by_name["control"], by_name["treatment"]


def _best(results):
    """The variant the Bayesian analysis gives the probability to be best."""
    bayes = results["bayesian_results"]
    assert bayes is not None
    return {
        v["variant_key"]: v["probability_to_be_best"] for v in bayes["variant_results"]
    }


def test_a_drop_in_a_lower_is_better_metric_is_the_win(admin_client, experiment):
    control_id, treatment_id = _ids(experiment)
    results = _results(admin_client, experiment)

    (metric,) = results["metrics"]
    assert metric["lower_is_better"] is True
    assert metric["winning_variant_id"] == treatment_id
    treatment = next(v for v in metric["variants"] if v["variant_id"] == treatment_id)
    # The raw signed change: (10% - 30%) / 30%.
    assert treatment["relative_improvement_pct"] == pytest.approx(-66.6666667)
    assert treatment["is_significant"] is True

    summary = results["summary"]
    assert summary["has_winner"] is True
    assert summary["winning_variant_id"] == treatment_id
    assert summary["recommendation"] == "SHIP_VARIANT"
    reason = summary["recommendation_reason"]
    assert reason.startswith("treatment shows 66.7% improvement on Uninstall (p=")
    assert reason.endswith("): it fell by 66.7%, and lower is better for this metric.")

    assert results["bayesian_results"]["decision"] == "STOP_WINNER"
    assert _best(results)["treatment"] >= 0.975


def test_a_null_lower_is_better_reads_as_higher_is_better(
    admin_client, db_session, experiment
):
    """A NULL in the column answers 200 and is higher is better."""
    (metric_row,) = experiment.metric_definitions
    schema = get_schema_name()
    db_session.execute(
        text(f"UPDATE {schema}.metrics SET lower_is_better = NULL WHERE id = :id"),
        {"id": metric_row.id},
    )
    db_session.commit()
    stored = db_session.execute(
        text(f"SELECT lower_is_better FROM {schema}.metrics WHERE id = :id"),
        {"id": metric_row.id},
    ).scalar_one()
    assert stored is None
    db_session.expire_all()

    results = _results(admin_client, experiment)
    (metric,) = results["metrics"]
    assert metric["lower_is_better"] is False
    assert metric["winning_variant_id"] is None
    assert results["summary"]["recommendation"] == "KEEP_CONTROL"
    assert results["summary"]["recommendation_reason"] == (
        "Every treatment is significantly worse than control on Uninstall."
    )
    assert _best(results)["control"] >= 0.975
