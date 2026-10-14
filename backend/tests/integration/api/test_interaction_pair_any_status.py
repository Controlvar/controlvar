"""``GET /api/v1/interactions/{a}/{b}`` answers for two experiments in any status.

Two experiments sharing 30 users are put in each status by SQL, and the pair
route reports their overlap and the beta interaction test for every
combination: drafts, running, paused, completed and archived alike.
"""

from __future__ import annotations

import pytest

from backend.app.models.experiment import Experiment, ExperimentStatus
from backend.tests.integration.api.test_interaction_pair_api import (
    _cleanup,
    _make,
    _seed,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

STATUSES = [
    ExperimentStatus.DRAFT,
    ExperimentStatus.ACTIVE,
    ExperimentStatus.PAUSED,
    ExperimentStatus.COMPLETED,
    ExperimentStatus.ARCHIVED,
]


@pytest.fixture
def shared_pair(make_experiment, db_session):
    a = _make(make_experiment, db_session, "Status A")
    b = _make(make_experiment, db_session, "Status B")
    assignments = []
    for i in range(30):
        assignments.append((a, "control" if i % 2 else "treatment", f"st-{i}"))
        assignments.append((b, "treatment" if i % 3 else "control", f"st-{i}"))
    # Users in A only.
    assignments += [(a, "control", f"a-only-{i}") for i in range(10)]
    _seed(db_session, assignments, [])
    yield a, b
    _cleanup(db_session, [a, b])


@pytest.mark.regression
@pytest.mark.parametrize("status_a", STATUSES)
@pytest.mark.parametrize(
    "status_b", [ExperimentStatus.DRAFT, ExperimentStatus.ARCHIVED]
)
def test_the_pair_route_answers_in_any_status(
    admin_client, db_session, shared_pair, status_a, status_b
):
    a, b = shared_pair
    for experiment, status in ((a, status_a), (b, status_b)):
        db_session.query(Experiment).filter(Experiment.id == experiment.id).update(
            {"status": status}, synchronize_session=False
        )
    db_session.commit()

    resp = admin_client.get(f"/api/v1/interactions/{a.id}/{b.id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["shared_users"] == 30
    assert body["overlap_coefficient"] == pytest.approx(30 / 40)
    assert body["analysis_status"] == "beta"
    rows = body["interaction_results"]
    assert {row["experiment_id"] for row in rows} == {str(a.id), str(b.id)}
    assert all(row["unavailable_reason"] == "too_few_shared_users" for row in rows)
