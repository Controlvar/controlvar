"""
``POST /api/v1/tracking/assign`` for an experiment that is not ACTIVE.

A draft, paused, completed or archived experiment answers every user,
enrolled or not, with HTTP 200, its control variant, ``assigned: false`` and
``reason`` set to its status. Nothing is recorded: no assignment, no view
event, no holdout population row. Stored assignments are kept, so a resumed
experiment answers each enrolled user with their variant again. Only a key no
experiment has answers 404 (``No experiment has the key '<k>'.``), and a
draft with no control variant
(``test_tracking_api.py::TestAssign::test_a_draft_with_no_control_variant_returns_404``).
``POST /api/v1/tracking/assign/batch`` is unchanged
(``test_tracking_assign_batch.py``).

Rows created here are deleted in teardown: the shared test database is not
truncated between tests.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import update

from backend.app.models.assignment import Assignment
from backend.app.models.event import Event
from backend.app.models.experiment import (
    Experiment,
    ExperimentStatus,
    Metric,
    MetricType,
    Variant,
)
from backend.app.models.global_holdout import GlobalHoldout
from backend.app.models.holdout_population import HoldoutPopulation
from backend.app.services.global_holdout_service import GlobalHoldoutService

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

NOT_RUNNING = [
    ExperimentStatus.DRAFT,
    ExperimentStatus.PAUSED,
    ExperimentStatus.COMPLETED,
    ExperimentStatus.ARCHIVED,
]


def test_the_reasons_are_exactly_the_four_statuses_that_are_not_active():
    """The ``reason`` an experiment that is not ACTIVE answers is its status's
    value, so the set of reasons is pinned here as literal strings: the SDKs
    (and anything reading the answers) see exactly these four."""
    assert {s.value for s in ExperimentStatus} - {"active"} == {
        "draft",
        "paused",
        "completed",
        "archived",
    }
    assert {s.value for s in NOT_RUNNING} == {
        "draft",
        "paused",
        "completed",
        "archived",
    }


@pytest.fixture
def experiment(db_session, make_experiment):
    """An ACTIVE experiment with a control and a treatment variant (each with
    its own configuration), a primary metric, and one user already assigned to
    the treatment."""
    suffix = uuid.uuid4().hex[:8]
    exp = make_experiment(
        name=f"Assign not running {suffix}",
        key=f"assign-not-running-{suffix}",
        status=ExperimentStatus.ACTIVE,
        start_date=datetime.now(timezone.utc) - timedelta(days=1),
    )
    variants = {}
    for name, is_control in (("control", True), ("treatment", False)):
        variants[name] = Variant(
            experiment_id=exp.id,
            name=name,
            is_control=is_control,
            traffic_allocation=50,
            configuration={"color": name},
        )
    db_session.add_all(
        [
            *variants.values(),
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
    enrolled = f"anr-{suffix}-enrolled"
    db_session.add(
        Assignment(
            experiment_id=exp.id,
            variant_id=variants["treatment"].id,
            user_id=enrolled,
        )
    )
    db_session.commit()
    db_session.refresh(exp)
    yield exp, {n: v.id for n, v in variants.items()}, enrolled
    db_session.rollback()
    for model in (Event, Assignment):
        db_session.query(model).filter(model.experiment_id == exp.id).delete(
            synchronize_session=False
        )
    db_session.commit()


def _set_status(db_session, exp, status: ExperimentStatus) -> None:
    db_session.execute(
        update(Experiment).where(Experiment.id == exp.id).values(status=status)
    )
    db_session.commit()


def _assign(client, exp, user_id):
    return client.post(
        "/api/v1/tracking/assign",
        json={"experiment_key": exp.key, "user_id": user_id},
    )


def _answer(response) -> tuple:
    assert response.status_code == 200, response.text
    body = response.json()
    return (
        body["variant_id"],
        body["variant_name"],
        body["is_control"],
        body["configuration"],
        body["assigned"],
        body["reason"],
    )


@pytest.mark.regression
def test_each_status_answers_an_enrolled_and_a_new_user(
    admin_client, db_session, experiment
):
    """Per status, the exact answer for a user enrolled in the treatment and
    for a user the experiment has never seen: ACTIVE as before, every other
    status the control with ``assigned: false`` and the status as the reason."""
    exp, variant_ids, enrolled = experiment
    control = (
        str(variant_ids["control"]),
        "control",
        True,
        {"color": "control"},
        False,
    )
    answers = {}
    for status in [ExperimentStatus.ACTIVE, *NOT_RUNNING]:
        _set_status(db_session, exp, status)
        answers[status.value] = (
            _answer(_assign(admin_client, exp, enrolled)),
            _answer(_assign(admin_client, exp, f"anr-new-{uuid.uuid4().hex[:10]}")),
        )

    active_enrolled, active_new = answers.pop("active")
    assert active_enrolled == (
        str(variant_ids["treatment"]),
        "treatment",
        False,
        {"color": "treatment"},
        True,
        "assigned",
    )
    assert active_new[1] in {"control", "treatment"}
    assert answers == {
        status.value: ((*control, status.value), (*control, status.value))
        for status in NOT_RUNNING
    }


@pytest.fixture
def measurable_holdout(db_session):
    """An active, measurable global holdout; any other active holdout is
    parked for the test and restored afterwards."""
    parked = [
        row.id
        for row in db_session.query(GlobalHoldout).filter(GlobalHoldout.is_active)
    ]
    db_session.execute(
        update(GlobalHoldout)
        .where(GlobalHoldout.id.in_(parked))
        .values(is_active=False)
    )
    db_session.commit()
    holdout = GlobalHoldoutService(db_session).create_holdout(
        name=f"assign-not-running-{uuid.uuid4().hex[:8]}",
        holdout_percentage=20,
        is_active=True,
    )
    assert holdout.is_measurable
    yield holdout
    db_session.rollback()
    # holdout_population rows go with it (ON DELETE CASCADE).
    db_session.query(GlobalHoldout).filter(GlobalHoldout.id == holdout.id).delete()
    db_session.execute(
        update(GlobalHoldout).where(GlobalHoldout.id.in_(parked)).values(is_active=True)
    )
    db_session.commit()


@pytest.mark.regression
def test_an_answer_for_an_experiment_that_is_not_running_records_nothing(
    admin_client, db_session, experiment, measurable_holdout
):
    """No assignment row, no event (no view event) and no holdout population
    row, for enrolled and new users, in every status that is not ACTIVE."""
    exp, _, enrolled = experiment

    def counts():
        db_session.expire_all()
        return (
            db_session.query(Assignment)
            .filter(Assignment.experiment_id == exp.id)
            .count(),
            db_session.query(Event).filter(Event.experiment_id == exp.id).count(),
            db_session.query(HoldoutPopulation)
            .filter(HoldoutPopulation.holdout_id == measurable_holdout.id)
            .count(),
        )

    before = counts()
    assert before == (1, 0, 0)
    new_users = [f"anr-new-{uuid.uuid4().hex[:10]}" for _ in range(10)]
    for status in NOT_RUNNING:
        _set_status(db_session, exp, status)
        for user_id in [enrolled, *new_users]:
            assert _answer(_assign(admin_client, exp, user_id))[4:] == (
                False,
                status.value,
            )
    assert counts() == before


@pytest.mark.regression
def test_a_resumed_experiment_answers_enrolled_users_with_their_variant(
    admin_client, experiment
):
    """Through the lifecycle API: paused, everyone gets the control; resumed,
    the enrolled user gets the treatment again; completed, the control."""
    exp, variant_ids, enrolled = experiment

    def lifecycle(action):
        response = admin_client.post(f"/api/v1/experiments/{exp.id}/{action}")
        assert response.status_code == 200, response.text

    def answer():
        variant_id, _, _, _, assigned, reason = _answer(
            _assign(admin_client, exp, enrolled)
        )
        return variant_id, assigned, reason

    treatment, control = str(variant_ids["treatment"]), str(variant_ids["control"])
    assert answer() == (treatment, True, "assigned")
    lifecycle("pause")
    assert answer() == (control, False, "paused")
    lifecycle("start")
    assert answer() == (treatment, True, "assigned")
    lifecycle("complete")
    assert answer() == (control, False, "completed")


def test_a_key_no_experiment_has_is_404_naming_the_key(admin_client):
    key = f"missing-{uuid.uuid4().hex[:8]}"
    response = admin_client.post(
        "/api/v1/tracking/assign", json={"experiment_key": key, "user_id": "u1"}
    )
    assert response.status_code == 404, response.text
    assert response.json() == {"detail": f"No experiment has the key '{key}'."}
