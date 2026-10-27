"""
``backend/scripts/retag_events_from_assignments.py``: tag an experiment's
events that have no variant with the variant each user was assigned when the
event was received.

It is the undo of the rule that ``/tracking/track`` and ``/tracking/batch``
tag an event with the user's variant only while the experiment is ACTIVE
(``test_tracking_inactive_experiment_events.py``). Rows are written directly
here, with the receive times (``events.updated_at``) and assignment times
the cases need.

Rows created here are deleted in teardown: the shared test database is not
truncated between tests.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from backend.app.models.assignment import Assignment
from backend.app.models.event import Event
from backend.app.models.experiment import ExperimentStatus, Variant
from backend.app.services.event_matching import count_converting_users
from backend.scripts.retag_events_from_assignments import (
    main,
    retag_events_from_assignments,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

#: When the users were assigned (naive UTC, as ``assignments.created_at``).
T0 = datetime(2026, 9, 1, 9, 0, 0)


@pytest.fixture
def rows(db_session, make_experiment):
    """A completed experiment and its events, one per case:

    * ``paused_a`` / ``paused_b``: no variant, from assigned users, received
      during the pause (T0+1h, T0+2h): tagged;
    * ``late_b``: no variant, user B, received at T0+10h: tagged only without
      a receive-time bound;
    * ``before``: no variant, user C, received before C was assigned: kept;
    * ``stranger``: no variant, a user with no assignment: kept;
    * ``tagged``: already has user A's variant: kept;
    * ``history``: no experiment at all, user A: kept;
    * ``other``: no variant, user B, another experiment: kept.
    """
    suffix = uuid.uuid4().hex[:8]
    exp = make_experiment(
        name=f"Retag {suffix}", key=f"retag-{suffix}", status=ExperimentStatus.COMPLETED
    )
    other = make_experiment(
        name=f"Retag other {suffix}",
        key=f"retag-other-{suffix}",
        status=ExperimentStatus.PAUSED,
    )
    control = Variant(experiment_id=exp.id, name="control", is_control=True)
    treatment = Variant(experiment_id=exp.id, name="treatment", is_control=False)
    other_control = Variant(experiment_id=other.id, name="control", is_control=True)
    db_session.add_all([control, treatment, other_control])
    db_session.commit()

    user_a, user_b, user_c = (f"retag-{suffix}-{n}" for n in "abc")
    db_session.add_all(
        [
            Assignment(
                experiment_id=exp.id,
                variant_id=treatment.id,
                user_id=user_a,
                created_at=T0,
            ),
            Assignment(
                experiment_id=exp.id,
                variant_id=control.id,
                user_id=user_b,
                created_at=T0,
            ),
            Assignment(
                experiment_id=exp.id,
                variant_id=control.id,
                user_id=user_c,
                created_at=T0 + timedelta(hours=3),
            ),
            Assignment(
                experiment_id=other.id,
                variant_id=other_control.id,
                user_id=user_b,
                created_at=T0,
            ),
        ]
    )

    def event(user_id, received, experiment=exp, variant=None):
        return Event(
            event_type="purchase",
            event_name="purchase",
            user_id=user_id,
            experiment_id=experiment.id if experiment else None,
            variant_id=variant.id if variant else None,
            created_at=received.replace(tzinfo=timezone.utc).isoformat(),
            updated_at=received,
        )

    events = {
        "paused_a": event(user_a, T0 + timedelta(hours=1)),
        "paused_b": event(user_b, T0 + timedelta(hours=2)),
        "late_b": event(user_b, T0 + timedelta(hours=10)),
        "before": event(user_c, T0 + timedelta(hours=1)),
        "stranger": event(f"retag-{suffix}-nobody", T0 + timedelta(hours=1)),
        "tagged": event(user_a, T0 + timedelta(minutes=30), variant=treatment),
        "history": event(user_a, T0 + timedelta(hours=1), experiment=None),
        "other": event(user_b, T0 + timedelta(hours=1), experiment=other),
    }
    db_session.add_all(events.values())
    db_session.commit()
    ids = {name: row.id for name, row in events.items()}
    yield exp, {"control": control.id, "treatment": treatment.id}, ids
    db_session.rollback()
    db_session.query(Event).filter(Event.id.in_(list(ids.values()))).delete(
        synchronize_session=False
    )
    db_session.query(Assignment).filter(
        Assignment.experiment_id.in_([exp.id, other.id])
    ).delete(synchronize_session=False)
    db_session.commit()


def _state(db_session, ids):
    """Each case's (variant_id, updated_at), read afresh."""
    db_session.expire_all()
    by_id = {
        row.id: (row.variant_id, row.updated_at)
        for row in db_session.query(Event).filter(Event.id.in_(list(ids.values())))
    }
    return {name: by_id[row_id] for name, row_id in ids.items()}


def _converting(db_session, exp, variants):
    return {
        name: count_converting_users(db_session, exp.id, variant_id, "purchase")
        for name, variant_id in variants.items()
    }


def test_a_receive_window_tags_only_the_events_received_in_it(db_session, rows):
    exp, variants, ids = rows
    before = _state(db_session, ids)
    assert _converting(db_session, exp, variants) == {"control": 0, "treatment": 1}
    window = {
        "received_from": (T0 + timedelta(minutes=45)).replace(tzinfo=timezone.utc),
        "received_until": T0 + timedelta(hours=5),
    }

    # Counting writes nothing.
    assert retag_events_from_assignments(db_session, exp.id, **window) == 2
    assert _state(db_session, ids) == before

    assert retag_events_from_assignments(db_session, exp.id, apply=True, **window) == 2
    after = _state(db_session, ids)
    assert after == {
        **before,
        "paused_a": (variants["treatment"], before["paused_a"][1]),
        "paused_b": (variants["control"], before["paused_b"][1]),
    }
    assert _converting(db_session, exp, variants) == {"control": 1, "treatment": 1}

    # Done again, the window has nothing left to tag.
    assert retag_events_from_assignments(db_session, exp.id, apply=True, **window) == 0


def test_without_a_window_every_event_received_after_the_assignment_is_tagged(
    db_session, rows, capsys
):
    exp, variants, ids = rows
    before = _state(db_session, ids)

    assert main([str(exp.id)]) == 0
    assert capsys.readouterr().out.startswith("would tag (run again with --apply): 3 ")
    assert _state(db_session, ids) == before

    assert main([str(exp.id), "--apply"]) == 0
    assert capsys.readouterr().out.startswith("tagged: 3 ")
    after = _state(db_session, ids)
    assert after == {
        **before,
        "paused_a": (variants["treatment"], before["paused_a"][1]),
        "paused_b": (variants["control"], before["paused_b"][1]),
        "late_b": (variants["control"], before["late_b"][1]),
    }
    assert _converting(db_session, exp, variants) == {"control": 1, "treatment": 1}
