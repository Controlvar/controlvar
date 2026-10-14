"""The bandit weights, through ``/api/v1/bandit/{id}`` and a real database.

Each experiment's pulls and successes are assignment rows and conversion
events in PostgreSQL (DynamoDB is unavailable, as in a core deployment), and
the weights are what ``POST /bandit/{id}/update`` computes from them, checked
against values computed here:

* Thompson sampling: each variant's weight is its probability of being best
  under Beta(1 + successes, 1 + failures), by numerical integration;
* UCB1: ``mean + sqrt(2 ln(total_pulls) / variant_pulls)``, normalised, and
  all the weight shared equally by the variants with no pulls;
* epsilon-greedy: ``0.9 + 0.1 / n`` and ``0.1 / n``, whatever a request or the
  experiment carries; nothing sets epsilon;
* ``regret_reduction_pct`` is null before there is data, and while the
  weights do no better than a uniform split;
* the scheduler replaces a manual override on its next run while the
  experiment is active, and leaves it alone otherwise.
"""

from __future__ import annotations

import math
import uuid
from typing import Dict
from unittest.mock import patch

import pytest
from scipy import integrate, stats

from backend.app.core.bandit_scheduler import BanditScheduler
from backend.app.core.config import Settings
from backend.app.models.assignment import Assignment
from backend.app.models.bandit_state import BanditState
from backend.app.models.event import Event
from backend.app.models.experiment import (
    Experiment,
    ExperimentStatus,
    ExperimentType,
    Metric,
    MetricType,
    Variant,
)
from backend.tests.integration.services.test_bandit_scheduler_db import _seed_variant

pytestmark = [pytest.mark.integration, pytest.mark.regression]


@pytest.fixture(autouse=True)
def _no_dynamodb():
    with patch.object(BanditScheduler, "_stats_from_dynamodb", return_value=None):
        yield


@pytest.fixture
def make_bandit(db_session, make_experiment):
    created = []

    def make(algorithm: str, arms=("arm_a", "arm_b"), status=ExperimentStatus.ACTIVE):
        suffix = uuid.uuid4().hex[:8]
        experiment = make_experiment(
            name=f"Bandit page {suffix}",
            key=f"bandit-page-{suffix}",
            status=status,
            experiment_type=ExperimentType.BANDIT,
            optimization_type=algorithm,
        )
        for i, name in enumerate(arms):
            db_session.add(
                Variant(
                    experiment_id=experiment.id,
                    name=name,
                    is_control=(i == 0),
                    traffic_allocation=100 // len(arms),
                )
            )
        db_session.add(
            Metric(
                experiment_id=experiment.id,
                name="Purchase",
                event_name="purchase",
                metric_type=MetricType.CONVERSION,
                is_primary=True,
            )
        )
        db_session.commit()
        db_session.refresh(experiment)
        created.append(experiment.id)
        return experiment

    yield make
    db_session.rollback()
    for experiment_id in created:
        for model in (BanditState, Event, Assignment):
            db_session.query(model).filter(model.experiment_id == experiment_id).delete(
                synchronize_session=False
            )
    db_session.commit()


def _arm(experiment, name):
    return next(v for v in experiment.variants if v.name == name)


def _weights(body) -> Dict[str, float]:
    return {
        row["variant_name"]: row["current_weight"] for row in body["current_weights"]
    }


def _update(client, experiment, **kwargs):
    resp = client.post(f"/api/v1/bandit/{experiment.id}/update", **kwargs)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _p_b_beats_a(a1, b1, a2, b2) -> float:
    """P(X_b > X_a) for X_a ~ Beta(a1, b1) and X_b ~ Beta(a2, b2)."""

    def integrand(x):
        return stats.beta.pdf(x, a2, b2) * stats.beta.cdf(x, a1, b1)

    return integrate.quad(integrand, 0.0, 1.0)[0]


def test_thompson_weights_are_the_beta_1_plus_successes_posteriors(
    admin_client, db_session, make_bandit
):
    experiment = make_bandit("thompson_sampling")
    _seed_variant(db_session, experiment, _arm(experiment, "arm_a"), 3, 0)
    _seed_variant(db_session, experiment, _arm(experiment, "arm_b"), 2, 1)
    body = _update(admin_client, experiment)
    assert body["n_samples"] == 10_000
    # arm_a: Beta(1, 4); arm_b: Beta(2, 2). P(b best) = 6/7 = 0.857; a prior of
    # 0.5 would give 0.904 and one of 2 would give 0.803.
    expected = _p_b_beats_a(1.0, 4.0, 2.0, 2.0)
    assert expected == pytest.approx(6 / 7, abs=1e-9)
    weights = _weights(body)
    assert weights["arm_b"] == pytest.approx(expected, abs=0.015)
    assert weights["arm_a"] == pytest.approx(1 - expected, abs=0.015)


def test_ucb1_weights_are_the_normalised_scores(admin_client, db_session, make_bandit):
    experiment = make_bandit("ucb1")
    _seed_variant(db_session, experiment, _arm(experiment, "arm_a"), 10, 2)
    _seed_variant(db_session, experiment, _arm(experiment, "arm_b"), 20, 10)
    weights = _weights(_update(admin_client, experiment))
    score_a = 2 / 10 + math.sqrt(2 * math.log(30) / 10)
    score_b = 10 / 20 + math.sqrt(2 * math.log(30) / 20)
    assert weights["arm_a"] == pytest.approx(score_a / (score_a + score_b), rel=1e-9)
    assert weights["arm_b"] == pytest.approx(score_b / (score_a + score_b), rel=1e-9)
    assert min(weights.values()) > 0.0


@pytest.mark.parametrize("without_pulls", [("arm_c",), ("arm_b", "arm_c")])
def test_ucb1_variants_with_no_pulls_share_all_the_weight(
    admin_client, db_session, make_bandit, without_pulls
):
    experiment = make_bandit("ucb1", arms=("arm_a", "arm_b", "arm_c"))
    for name in ("arm_a", "arm_b", "arm_c"):
        if name not in without_pulls:
            _seed_variant(db_session, experiment, _arm(experiment, name), 50, 25)
    weights = _weights(_update(admin_client, experiment))
    for name, weight in weights.items():
        expected = 1 / len(without_pulls) if name in without_pulls else 0.0
        assert weight == pytest.approx(expected), weights


def test_epsilon_is_point_one_and_not_a_setting(admin_client, db_session, make_bandit):
    experiment = make_bandit("epsilon_greedy", arms=("arm_a", "arm_b", "arm_c"))
    _seed_variant(db_session, experiment, _arm(experiment, "arm_a"), 40, 4)
    _seed_variant(db_session, experiment, _arm(experiment, "arm_b"), 40, 20)
    _seed_variant(db_session, experiment, _arm(experiment, "arm_c"), 40, 8)
    expected = {"arm_a": 0.1 / 3, "arm_b": 0.9 + 0.1 / 3, "arm_c": 0.1 / 3}

    weights = _weights(_update(admin_client, experiment))
    assert weights == pytest.approx(expected, rel=1e-12)
    # Nothing a request sends changes it ...
    asked = _update(admin_client, experiment, params={"epsilon": 0.5})
    assert _weights(asked) == pytest.approx(expected, rel=1e-12)
    asked = _update(admin_client, experiment, json={"epsilon": 0.5})
    assert _weights(asked) == pytest.approx(expected, rel=1e-12)
    # ... nor the scheduler's own run ...
    BanditScheduler(db_session).update_experiment(experiment)
    state = db_session.query(BanditState).filter_by(experiment_id=experiment.id).one()
    db_session.refresh(state)
    by_id = {str(_arm(experiment, n).id): n for n in expected}
    assert {
        by_id[vid]: data["weight"] for vid, data in state.variant_weights.items()
    } == pytest.approx(expected, rel=1e-12)
    # ... and no setting is named after it.
    assert [name for name in Settings.model_fields if "epsilon" in name.lower()] == []


def test_regret_reduction_is_null_until_there_is_data_and_a_gain(
    admin_client, db_session, make_bandit
):
    experiment = make_bandit("epsilon_greedy")
    status = admin_client.get(f"/api/v1/bandit/{experiment.id}")
    assert status.status_code == 200, status.text
    assert status.json()["regret_reduction_pct"] is None

    # Equal rates: the weights do no better than a uniform split.
    _seed_variant(db_session, experiment, _arm(experiment, "arm_a"), 20, 5)
    _seed_variant(db_session, experiment, _arm(experiment, "arm_b"), 20, 5)
    assert _update(admin_client, experiment)["regret_reduction_pct"] is None

    # arm_b now converts at 15/40: weights 0.05 / 0.95 against 0.5 / 0.5.
    _seed_variant(db_session, experiment, _arm(experiment, "arm_b"), 20, 10)
    body = _update(admin_client, experiment)
    rate_a, rate_b = 5 / 20, 15 / 40
    uniform = (rate_a + rate_b) / 2
    bandit = 0.05 * rate_a + 0.95 * rate_b
    assert body["regret_reduction_pct"] == pytest.approx(
        (bandit - uniform) / uniform * 100.0, rel=1e-9
    )

    # arm_a catches up (15/40 as well): null again on the next update.
    _seed_variant(db_session, experiment, _arm(experiment, "arm_a"), 20, 10)
    assert _update(admin_client, experiment)["regret_reduction_pct"] is None


@pytest.mark.parametrize(
    "status, replaced",
    [(ExperimentStatus.ACTIVE, True), (ExperimentStatus.PAUSED, False)],
)
def test_the_scheduler_replaces_an_override_while_the_experiment_is_active(
    admin_client, db_session, make_bandit, status, replaced
):
    experiment = make_bandit("epsilon_greedy")
    arm_a, arm_b = _arm(experiment, "arm_a"), _arm(experiment, "arm_b")
    _seed_variant(db_session, experiment, arm_a, 20, 2)
    _seed_variant(db_session, experiment, arm_b, 20, 10)
    override = {str(arm_a.id): 0.7, str(arm_b.id): 0.3}
    resp = admin_client.put(
        f"/api/v1/bandit/{experiment.id}/weights", json={"weights": override}
    )
    assert resp.status_code == 200, resp.text
    db_session.query(Experiment).filter(Experiment.id == experiment.id).update(
        {"status": status}, synchronize_session=False
    )
    db_session.commit()

    BanditScheduler(db_session).run_once()

    weights = _weights(admin_client.get(f"/api/v1/bandit/{experiment.id}").json())
    if replaced:
        assert weights == pytest.approx({"arm_a": 0.05, "arm_b": 0.95}, rel=1e-12)
    else:
        assert weights == pytest.approx({"arm_a": 0.7, "arm_b": 0.3}, rel=1e-12)
