"""Event times compare in UTC, whatever offset the client sent (#235).

``events.created_at`` is a VARCHAR, so every time window over events is a
string comparison.  Before #235 a client timestamp was stored verbatim, offset
and all, and ``2026-10-01T01:00:00+02:00`` (23:00 UTC on 30 September) sorted
*after* ``2026-09-30T23:30`` -- outside a window it is inside.

The expected values below are worked out by hand from the offsets, not by
calling the code under test:

* ``2026-10-01T01:00:00+02:00`` is 2026-09-30 23:00 UTC -- inside
  [22:30Z, 23:30Z] on 30 September, but sorts after the window as text.
* ``2026-09-30T23:00:00-02:00`` is 2026-10-01 01:00 UTC -- outside the
  window, but sorts inside it as text.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from backend.app.models.event import Event
from backend.app.services.event_matching import MetricEvents, first_conversion_times
from backend.app.services.event_service import EventService

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

WINDOW_START = "2026-09-30T22:30:00Z"
WINDOW_END = "2026-09-30T23:30:00Z"

#: 01:00 at +02:00 is 23:00 UTC the previous day: inside the window.
INSIDE_WITH_OFFSET = "2026-10-01T01:00:00+02:00"
INSIDE_AS_UTC = "2026-09-30T23:00:00+00:00"
#: 23:00 at -02:00 is 01:00 UTC the next day: outside the window.
OUTSIDE_WITH_OFFSET = "2026-09-30T23:00:00-02:00"
OUTSIDE_AS_UTC = "2026-10-01T01:00:00+00:00"


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _track(service: EventService, experiment_id, user_id: str, timestamp: str):
    return service.track_event(
        {
            "user_id": user_id,
            "experiment_id": str(experiment_id),
            "event_type": "conversion",
            "event_name": "purchase",
            "timestamp": timestamp,
        }
    )


@pytest.mark.regression
def test_an_offset_event_lands_in_the_utc_window_it_belongs_to(
    db_session, make_experiment
):
    """01:00+02:00 is counted in [22:30Z, 23:30Z]; 23:00-02:00 is not."""
    experiment = make_experiment(name=_uid("utc-window"))
    service = EventService(db_session)
    inside_user = _uid("inside")
    outside_user = _uid("outside")
    _track(service, experiment.id, inside_user, INSIDE_WITH_OFFSET)
    _track(service, experiment.id, outside_user, OUTSIDE_WITH_OFFSET)

    in_window = service.get_events_by_experiment(
        experiment.id, start_date=WINDOW_START, end_date=WINDOW_END
    )
    counted = service.count_events_by_experiment(
        experiment.id, start_date=WINDOW_START, end_date=WINDOW_END
    )

    assert [e["user_id"] for e in in_window] == [inside_user]
    assert counted == 1


@pytest.mark.regression
def test_an_offset_timestamp_is_stored_as_utc(db_session, make_experiment):
    experiment = make_experiment(name=_uid("utc-stored"))
    service = EventService(db_session)
    inside = _track(service, experiment.id, _uid("u"), INSIDE_WITH_OFFSET)
    outside = _track(service, experiment.id, _uid("u"), OUTSIDE_WITH_OFFSET)

    db_session.expire_all()
    assert db_session.get(Event, inside.id).created_at == INSIDE_AS_UTC
    assert db_session.get(Event, outside.id).created_at == OUTSIDE_AS_UTC


@pytest.mark.regression
def test_a_direct_write_and_a_datetime_boundary_are_normalised_too(
    db_session, make_experiment
):
    """Writers outside EventService (the seed scripts) and filters that
    compare the column with a ``datetime`` (export, admin) go through the
    column type, so they get the same normalisation."""
    experiment = make_experiment(name=_uid("utc-column"))
    plus_two = timezone(timedelta(hours=2))
    user_id = _uid("direct")
    db_session.add(
        Event(
            event_type="conversion",
            event_name="purchase",
            user_id=user_id,
            experiment_id=experiment.id,
            created_at=datetime(2026, 10, 1, 1, 0, 0, tzinfo=plus_two),
        )
    )
    db_session.commit()

    start = datetime(2026, 9, 30, 22, 30, tzinfo=timezone.utc)
    end = datetime(2026, 9, 30, 23, 30, tzinfo=timezone.utc)
    rows = (
        db_session.query(Event)
        .filter(
            Event.experiment_id == experiment.id,
            Event.created_at >= start,
            Event.created_at <= end,
        )
        .all()
    )

    assert [(r.user_id, r.created_at) for r in rows] == [(user_id, INSIDE_AS_UTC)]


@pytest.mark.regression
def test_first_conversion_is_the_earliest_instant_not_the_smallest_text(
    db_session, make_experiment, make_variant, make_assignment
):
    """``first_conversion_times`` takes ``min(created_at)``: with offsets kept
    verbatim the 23:30Z event was 'first', though 01:00+02:00 is 23:00Z."""
    experiment = make_experiment(name=_uid("utc-first"))
    variant = make_variant(experiment)
    user_id = _uid("conv")
    make_assignment(experiment, variant, user_id)
    service = EventService(db_session)
    for timestamp in ("2026-09-30T23:30:00+00:00", INSIDE_WITH_OFFSET):
        service.track_event(
            {
                "user_id": user_id,
                "experiment_id": str(experiment.id),
                "variant_id": str(variant.id),
                "event_type": "conversion",
                "event_name": "purchase",
                "timestamp": timestamp,
            }
        )

    firsts = first_conversion_times(
        db_session, experiment.id, variant.id, MetricEvents("purchase", None)
    )

    assert firsts == [INSIDE_AS_UTC]
