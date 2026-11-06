"""Each demo seed stores its users' attributes on their exposures (#830).

The segment preview reads the attributes ``/tracking/assign`` stores on each
exposure. The ShopLab and StreamPulse seeds write their history directly, so
each copies the context it writes to ``Assignment.context`` into the
exposure's ``event_metadata``: StreamPulse the device attributes, ShopLab
only ``{"source"}``. A seeded demo then previews something other than "no
users have attributes".
"""

from __future__ import annotations

import pytest

from backend.app.models.assignment import Assignment
from backend.app.models.event import Event
from backend.app.services.event_matching import EXPOSURE_EVENT_TYPES
from backend.scripts import seed_shoplab, seed_streampulse

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


def _exposures_match_assignments(db_session, experiments) -> int:
    """Assert every seeded exposure carries its assignment's context; count them."""
    ids = [experiment.id for experiment in experiments.values()]
    db_session.expire_all()
    contexts = {
        (row.experiment_id, row.user_id): row.context
        for row in db_session.query(Assignment).filter(
            Assignment.experiment_id.in_(ids)
        )
    }
    exposures = (
        db_session.query(Event)
        .filter(
            Event.experiment_id.in_(ids),
            Event.event_type.in_(EXPOSURE_EVENT_TYPES),
        )
        .all()
    )
    assert exposures
    for event in exposures:
        context = contexts[(event.experiment_id, event.user_id)]
        assert isinstance(context, dict) and context
        assert event.event_metadata == context
    return len(exposures)


def test_shoplab_exposures_carry_the_source(db_session, admin_user):
    experiments = seed_shoplab.seed_experiments(db_session, admin_user)
    seed_shoplab.seed_history(db_session, experiments, users_per_experiment=5)

    assert _exposures_match_assignments(db_session, experiments) >= len(experiments)
    sample = (
        db_session.query(Event)
        .filter(
            Event.experiment_id.in_([e.id for e in experiments.values()]),
            Event.event_type.in_(EXPOSURE_EVENT_TYPES),
        )
        .first()
    )
    assert sample.event_metadata == {"source": "seed_shoplab"}


def test_streampulse_exposures_carry_the_device_attributes(db_session, admin_user):
    group = seed_streampulse.seed_mutual_exclusion_group(db_session, admin_user)
    experiments = seed_streampulse.seed_experiments(db_session, admin_user, group)
    seed_streampulse.seed_history(db_session, experiments, users_per_experiment=5)

    assert _exposures_match_assignments(db_session, experiments) >= len(experiments)
    sample = (
        db_session.query(Event)
        .filter(
            Event.experiment_id.in_([e.id for e in experiments.values()]),
            Event.event_type.in_(EXPOSURE_EVENT_TYPES),
        )
        .first()
    )
    assert sample.event_metadata["source"] == "seed_streampulse"
    assert sample.event_metadata["device_id"] == sample.user_id
    assert len(sample.event_metadata) > 2
