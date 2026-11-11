"""The overview's ``experiments_with_winners`` against a real database (#244).

An experiment is created, started and given traffic through the API and the
tracking API, then completed. Its results name a winner, so the overview for
a period holding only that experiment counts exactly 1, as many as the
experiment export's non-empty ``winner_variant`` cells for the same period.
The count used to be 0 for every request.

Rows created here are deleted in fixture teardown because the shared test
database is not truncated between tests.
"""

from __future__ import annotations

import uuid

import pytest

from backend.app.models.assignment import Assignment
from backend.app.models.event import Event
from backend.app.models.experiment import Experiment
from backend.app.models.user import UserRole
from backend.tests.integration.api.route_helpers import (
    api_client,
    api_key,
    assign,
    auth,
    create_experiment,
    local_sign_in,
    make_user,
    start,
    track,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

USERS = 60


@pytest.fixture(autouse=True)
def _sign_in(monkeypatch):
    local_sign_in(monkeypatch)


@pytest.fixture
def client(db_session):
    with api_client(db_session) as c:
        yield c


@pytest.fixture
def made(db_session):
    """Experiment ids whose assignments and events are deleted afterwards."""
    ids = []
    yield ids
    db_session.rollback()
    for model in (Event, Assignment):
        db_session.query(model).filter(model.experiment_id.in_(ids)).delete(
            synchronize_session=False
        )
    db_session.commit()


@pytest.mark.regression
def test_the_overview_counts_a_completed_experiment_with_a_winner(
    client, db_session, made
):
    owner = make_user(db_session, UserRole.ADMIN, superuser=True, tag="winners")
    experiment = create_experiment(client, owner)
    made.append(uuid.UUID(experiment["id"]))
    start(client, experiment, owner)
    key = api_key(client, owner)
    names = {str(v["id"]): v["name"] for v in experiment["variants"]}
    event = experiment["metrics"][0]["event_name"]
    prefix = f"winners-{uuid.uuid4().hex[:8]}"
    arms = {"control": 0, "treatment": 0}
    for i in range(USERS):
        user_id = f"{prefix}-{i}"
        arm = names[str(assign(client, key, experiment["key"], user_id)["variant_id"])]
        arms[arm] += 1
        # Four in five treatment users convert, one in ten control users.
        if (arm == "treatment" and i % 5) or (arm == "control" and i % 10 == 0):
            track(client, key, user_id, event, experiment_key=experiment["key"])
    assert min(arms.values()) >= 10, arms
    completed = client.post(
        f"/api/v1/experiments/{experiment['id']}/complete", headers=auth(owner)
    )
    assert completed.status_code == 200, completed.text

    results = client.get(
        f"/api/v1/results/{experiment['id']}",
        params={"use_cache": "false"},
        headers=auth(owner),
    )
    assert results.status_code == 200, results.text
    summary = results.json()["summary"]
    assert summary["has_winner"] is True

    # A period holding this experiment and no other.
    created = (
        db_session.query(Experiment.created_at)
        .filter(Experiment.id == uuid.UUID(experiment["id"]))
        .scalar()
    )
    period = {"start_date": created.isoformat(), "end_date": created.isoformat()}
    overview = client.get(
        "/api/v1/export/reports/overview", params=period, headers=auth(owner)
    )
    assert overview.status_code == 200, overview.text
    body = overview.json()
    assert body["total_experiments"] == 1
    assert body["experiments_with_winners"] == 1
    assert body["experiments_with_winners"] == int(summary["has_winner"])

    rows = client.get(
        "/api/v1/export/experiments",
        params={"format": "json", **period},
        headers=auth(owner),
    )
    assert rows.status_code == 200, rows.text
    assert [row["winner_variant"] for row in rows.json()] == ["treatment"]
