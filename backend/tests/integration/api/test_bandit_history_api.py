"""A bandit's weight history, through ``GET /api/v1/bandit/{id}/history`` (#1958).

The route reads ``bandit_state_history``, which the bandit writes one row per
variant at every weight update, every row of one update sharing its
``tick_at``. Against a real PostgreSQL database, with DynamoDB patched out so
PostgreSQL is the scheduler's source:

* ``[]`` before the first update;
* one entry per update, newest first, each ``weights`` exactly that update's
  rows keyed by variant id, and ``tick_at`` the stored instant in UTC;
* ``limit`` counts updates, not rows, and keeps the newest;
* ``limit`` is 1 to 1000, anything else ``422``;
* ``404`` for an unknown experiment and for a fixed one, even with rows;
* no other experiment's rows, even one stored at the same instant;
* a manual override (``PUT /weights``) is not an update and is not listed;
* an update older than the analysis-history retention window is purged.

Who may read it is ``test_experiment_data_read_rule.py``'s, cell by cell with
real tokens. The route is beta: the last test reads the document the
application builds, which needs no database.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List
from unittest.mock import patch

import pytest

from backend.app.core.bandit_scheduler import BanditScheduler
from backend.app.core.stats_engine import ENGINE_VERSION
from backend.app.models.assignment import Assignment
from backend.app.models.bandit_state import BanditState, BanditStateHistory
from backend.app.models.event import Event
from backend.app.models.experiment import (
    ExperimentStatus,
    ExperimentType,
    Metric,
    MetricType,
    Variant,
)
from backend.app.services.analysis_snapshot_service import purge_expired_history
from backend.tests.integration.services.test_bandit_scheduler_db import _seed_variant

pytestmark = [pytest.mark.integration]

HISTORY = "/api/v1/bandit/{}/history"
ARMS = ("arm_a", "arm_b", "arm_c")
#: Four instants a minute apart, oldest first, an hour ago: well inside the
#: retention window whenever the suite runs.
T1 = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)
T2 = T1 + timedelta(minutes=1)
T3 = T1 + timedelta(minutes=2)
T4 = T1 + timedelta(minutes=3)


@pytest.fixture(autouse=True)
def _no_dynamodb():
    with patch.object(BanditScheduler, "_stats_from_dynamodb", return_value=None):
        yield


@pytest.fixture
def make_bandit(db_session, make_experiment):
    """A bandit experiment with three variants and a primary metric; its
    history, state and traffic rows are removed afterwards."""
    created = []

    def make(algorithm="thompson_sampling", status=ExperimentStatus.ACTIVE):
        suffix = uuid.uuid4().hex[:8]
        experiment = make_experiment(
            name=f"Bandit history {suffix}",
            key=f"bandit-history-{suffix}",
            status=status,
            experiment_type=ExperimentType.BANDIT,
            optimization_type=algorithm,
        )
        for i, name in enumerate(ARMS):
            db_session.add(
                Variant(
                    experiment_id=experiment.id,
                    name=name,
                    is_control=(i == 0),
                    traffic_allocation=34 if i == 0 else 33,
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
        for model in (BanditStateHistory, BanditState, Event, Assignment):
            db_session.query(model).filter(model.experiment_id == experiment_id).delete(
                synchronize_session=False
            )
    db_session.commit()


def _arm(experiment, name):
    return next(v for v in experiment.variants if v.name == name)


def _insert_tick(db_session, experiment, tick_at, weights: Dict[str, float]):
    """One update's rows, written directly: one row per variant named in
    *weights* (by variant name), all at *tick_at*."""
    for name, weight in weights.items():
        db_session.add(
            BanditStateHistory(
                experiment_id=experiment.id,
                variant_id=_arm(experiment, name).id,
                algorithm=experiment.optimization_type,
                alpha=1.0,
                beta=1.0,
                weight=weight,
                successes=0,
                failures=0,
                pulls=0,
                engine_version=ENGINE_VERSION,
                tick_at=tick_at,
            )
        )
    db_session.commit()


def _split(a, b, c):
    """Weights by arm name."""
    return {"arm_a": a, "arm_b": b, "arm_c": c}


def _ids(experiment, weights: Dict[str, float]) -> Dict[str, float]:
    return {str(_arm(experiment, n).id): w for n, w in weights.items()}


def _stored(db_session, experiment) -> Dict[datetime, Dict[str, float]]:
    """The experiment's history rows, as {tick_at: {variant_id: weight}}."""
    db_session.expire_all()
    rows = (
        db_session.query(BanditStateHistory)
        .filter(BanditStateHistory.experiment_id == experiment.id)
        .all()
    )
    ticks: Dict[datetime, Dict[str, float]] = {}
    for row in rows:
        ticks.setdefault(row.tick_at, {})[str(row.variant_id)] = row.weight
    return ticks


def _get(client, experiment_id, **params) -> List[dict]:
    response = client.get(HISTORY.format(experiment_id), params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _at(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_no_update_yet_is_an_empty_list(viewer_client, make_bandit):
    experiment = make_bandit()
    assert _get(viewer_client, experiment.id) == []


def test_each_update_is_one_entry_newest_first_with_its_stored_weights(
    viewer_client, db_session, make_bandit
):
    """Two real updates by the scheduler, with more traffic in between so the
    two updates' weights differ."""
    experiment = make_bandit()
    _seed_variant(db_session, experiment, _arm(experiment, "arm_a"), 20, 2)
    _seed_variant(db_session, experiment, _arm(experiment, "arm_b"), 20, 10)
    assert BanditScheduler(db_session).update_experiment(experiment) is True
    _seed_variant(db_session, experiment, _arm(experiment, "arm_c"), 40, 30)
    assert BanditScheduler(db_session).update_experiment(experiment) is True

    stored = _stored(db_session, experiment)
    assert len(stored) == 2
    newest, oldest = sorted(stored, reverse=True)
    assert stored[newest] != stored[oldest]

    body = _get(viewer_client, experiment.id)
    assert [_at(tick["tick_at"]) for tick in body] == [newest, oldest]
    assert all(tick["tick_at"].endswith("Z") for tick in body), body
    variant_ids = {str(v.id) for v in experiment.variants}
    for tick in body:
        assert set(tick["weights"]) == variant_ids
        assert tick["weights"] == pytest.approx(stored[_at(tick["tick_at"])])
        assert sum(tick["weights"].values()) == pytest.approx(1.0)


def test_limit_counts_updates_and_keeps_the_newest(
    viewer_client, db_session, make_bandit
):
    experiment = make_bandit()
    _insert_tick(db_session, experiment, T1, _split(0.5, 0.3, 0.2))
    _insert_tick(db_session, experiment, T2, _split(0.4, 0.4, 0.2))
    _insert_tick(db_session, experiment, T3, _split(0.1, 0.8, 0.1))

    (only,) = _get(viewer_client, experiment.id, limit=1)
    assert _at(only["tick_at"]) == T3
    assert only["weights"] == pytest.approx(_ids(experiment, _split(0.1, 0.8, 0.1)))

    two = _get(viewer_client, experiment.id, limit=2)
    assert [_at(tick["tick_at"]) for tick in two] == [T3, T2]
    assert [len(tick["weights"]) for tick in two] == [3, 3]

    assert len(_get(viewer_client, experiment.id)) == 3


@pytest.mark.parametrize("limit", ["0", "1001", "-1", "x", "1.5"])
def test_a_limit_outside_1_to_1000_is_refused(viewer_client, make_bandit, limit):
    experiment = make_bandit()
    response = viewer_client.get(HISTORY.format(experiment.id), params={"limit": limit})
    assert response.status_code == 422, response.text
    assert response.json()["detail"][0]["loc"] == ["query", "limit"]


@pytest.mark.parametrize("limit", [1, 1000])
def test_the_limits_own_bounds_are_accepted(
    viewer_client, db_session, make_bandit, limit
):
    experiment = make_bandit()
    _insert_tick(db_session, experiment, T1, _split(0.5, 0.3, 0.2))
    assert len(_get(viewer_client, experiment.id, limit=limit)) == 1


def test_an_unknown_experiment_is_not_found(viewer_client):
    unknown = uuid.uuid4()
    response = viewer_client.get(HISTORY.format(unknown))
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == f"Experiment {unknown} not found"


def test_a_fixed_experiment_is_not_found_even_with_rows(
    viewer_client, db_session, make_bandit
):
    experiment = make_bandit(algorithm="fixed")
    _insert_tick(db_session, experiment, T1, _split(0.5, 0.3, 0.2))
    assert len(_stored(db_session, experiment)) == 1

    response = viewer_client.get(HISTORY.format(experiment.id))
    assert response.status_code == 404, response.text
    assert "uses fixed allocation" in response.json()["detail"]


def test_no_other_experiments_rows(viewer_client, db_session, make_bandit):
    """A at T1 and T3; B at T2, T3 (the same instant as A's T3) and T4. B's
    rows are newer than A's and one shares A's instant, so a query that
    drops the experiment filter in either place answers differently."""
    a, b = make_bandit(), make_bandit()
    _insert_tick(db_session, a, T1, _split(0.5, 0.3, 0.2))
    _insert_tick(db_session, a, T3, _split(0.2, 0.3, 0.5))
    _insert_tick(db_session, b, T2, _split(0.6, 0.2, 0.2))
    _insert_tick(db_session, b, T3, _split(0.7, 0.2, 0.1))
    _insert_tick(db_session, b, T4, _split(0.8, 0.1, 0.1))
    a_ids = {str(v.id) for v in a.variants}

    for params in ({}, {"limit": 2}):
        body = _get(viewer_client, a.id, **params)
        assert [_at(tick["tick_at"]) for tick in body] == [T3, T1], params
        for tick in body:
            assert set(tick["weights"]) == a_ids, params
        assert body[0]["weights"] == pytest.approx(_ids(a, _split(0.2, 0.3, 0.5)))


def test_a_manual_override_is_not_an_update(admin_client, db_session, make_bandit):
    """The docs' sequence on a draft: one update by POST /update, then an
    override by PUT /weights. The history holds the update only."""
    experiment = make_bandit(status=ExperimentStatus.DRAFT)
    updated = admin_client.post(f"/api/v1/bandit/{experiment.id}/update")
    assert updated.status_code == 200, updated.text
    before = _get(admin_client, experiment.id)
    assert [len(tick["weights"]) for tick in before] == [3]

    override = _ids(experiment, _split(0.1, 0.8, 0.1))
    response = admin_client.put(
        f"/api/v1/bandit/{experiment.id}/weights", json={"weights": override}
    )
    assert response.status_code == 200, response.text

    after = _get(admin_client, experiment.id)
    assert after == before
    assert after[0]["weights"] != pytest.approx(override)


def test_updates_older_than_the_retention_window_are_deleted(
    viewer_client, db_session, make_bandit
):
    """The analysis-history purge (ANALYSIS_HISTORY_RETENTION_DAYS, run by the
    metrics scheduler) deletes old updates, so the route no longer lists them."""
    experiment = make_bandit()
    old = datetime.now(timezone.utc) - timedelta(days=91)
    _insert_tick(db_session, experiment, old, _split(0.5, 0.3, 0.2))
    _insert_tick(db_session, experiment, T1, _split(0.4, 0.4, 0.2))
    assert len(_get(viewer_client, experiment.id)) == 2

    purge_expired_history(db_session, retention_days=90)

    assert [_at(tick["tick_at"]) for tick in _get(viewer_client, experiment.id)] == [T1]


def test_the_route_is_beta_in_the_document_the_application_builds():
    """``x-stability: beta`` on the served operation, and the limit's bounds.
    The committed snapshots are held by test_bandit_history_contract.py; this
    reads ``app.openapi()``, so a marker dropped from the route fails here
    even before ``make openapi`` is re-run. No database."""
    from backend.app.main import app

    operation = app.openapi()["paths"]["/api/v1/bandit/{experiment_id}/history"]["get"]
    assert operation.get("x-stability") == "beta"
    (limit,) = [p for p in operation["parameters"] if p["name"] == "limit"]
    assert limit["in"] == "query"
    assert (
        limit["schema"]["default"],
        limit["schema"]["minimum"],
        limit["schema"]["maximum"],
    ) == (200, 1, 1000)
