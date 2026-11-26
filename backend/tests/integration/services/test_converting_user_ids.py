"""
``event_matching.converting_user_ids`` is the set ``count_converting_users`` counts.

The interaction test (#219) and CUPED (#217) read converters as a set of ids;
``/results`` counts them.  The two must be the same users, so the set's size
is checked against the count on data that each could get wrong: a user with
two conversions, an event from a user with no assignment, an event tagged
with the variant the user is not in, and a view row named after the metric.
"""

import uuid
from datetime import datetime, timezone

import pytest

from backend.app.models.assignment import Assignment
from backend.app.models.event import Event
from backend.app.models.experiment import ExperimentStatus, Variant
from backend.app.services.event_matching import (
    MetricEvents,
    converting_user_ids,
    count_converting_users,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


@pytest.fixture
def messy_experiment(db_session, make_experiment):
    suffix = uuid.uuid4().hex[:8]
    experiment = make_experiment(
        name=f"Converting ids {suffix}",
        key=f"converting-ids-{suffix}",
        status=ExperimentStatus.ACTIVE,
        start_date=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    control = Variant(experiment_id=experiment.id, name="control", is_control=True)
    treatment = Variant(experiment_id=experiment.id, name="treatment")
    db_session.add_all([control, treatment])
    db_session.flush()
    now = datetime.now(timezone.utc).isoformat()

    def event(user, variant, kind="purchase"):
        db_session.add(
            Event(
                event_type=kind,
                event_name="purchase",
                user_id=user,
                experiment_id=experiment.id,
                variant_id=variant.id,
                created_at=now,
            )
        )

    users = {}
    for variant in (control, treatment):
        for i in range(4):
            user = f"cid-{suffix}-{variant.name}-{i}"
            users.setdefault(variant.name, []).append(user)
            db_session.add(
                Assignment(
                    experiment_id=experiment.id, variant_id=variant.id, user_id=user
                )
            )
    # control: user 0 converts twice, user 1 once; user 2 has only a view row
    # named after the metric; user 3 has a conversion tagged treatment.
    c = users["control"]
    event(c[0], control)
    event(c[0], control)
    event(c[1], control)
    event(c[2], control, kind="exposure")
    event(c[3], treatment)
    # A user with no assignment at all.
    event(f"cid-{suffix}-nobody", control)
    db_session.commit()
    yield experiment, control, treatment, users
    db_session.rollback()
    for model in (Event, Assignment):
        db_session.query(model).filter(model.experiment_id == experiment.id).delete(
            synchronize_session=False
        )
    db_session.commit()


def test_converting_user_ids_is_what_count_converting_users_counts(
    db_session, messy_experiment
):
    experiment, control, treatment, users = messy_experiment
    for variant in (control, treatment):
        ids = converting_user_ids(
            db_session, experiment.id, variant.id, MetricEvents("purchase", None)
        )
        assert len(ids) == count_converting_users(
            db_session, experiment.id, variant.id, MetricEvents("purchase", None)
        )
    ids = converting_user_ids(
        db_session, experiment.id, control.id, MetricEvents("purchase", None)
    )
    assert ids == {users["control"][0], users["control"][1]}
    assert (
        converting_user_ids(
            db_session, experiment.id, treatment.id, MetricEvents("purchase", None)
        )
        == set()
    )
