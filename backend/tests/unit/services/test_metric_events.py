"""
``event_matching.MetricEvents``: what every results reader takes for a metric.

A metric has no conversion window yet, so ``window_hours`` is always None and
one is refused. With None the criterion a reader applies compiles to the same
statement text as the event name's own (``conversion_event_filter``). A bare
event name handed to a reader raises before any statement runs, rather than
being read without the metric's window.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy.dialects import postgresql

from backend.app.services import event_matching
from backend.app.services.analysis_service import AnalysisService
from backend.app.services.event_matching import (
    MetricEvents,
    any_conversion_event_filter,
    any_metric_event_filter,
    conversion_event_filter,
    metric_event_filter,
    metric_event_name,
    metric_events,
)

pytestmark = pytest.mark.unit

PURCHASE = MetricEvents("purchase", None)


def _sql(criterion) -> str:
    return str(
        criterion.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


def test_metric_events_is_none_for_a_metric_with_no_event_name():
    assert metric_events(None) is None
    assert metric_events(SimpleNamespace(event_name="")) is None
    assert metric_events(SimpleNamespace(event_name=None)) is None
    assert metric_events(SimpleNamespace()) is None


def test_metric_events_carries_the_name_and_no_window():
    events = metric_events(SimpleNamespace(event_name="purchase"))
    assert events == PURCHASE
    assert (events.event_name, events.window_hours) == ("purchase", None)
    # Equal and hashable: a result keyed by it finds the same metric again.
    assert {events: 1}[metric_events(SimpleNamespace(event_name="purchase"))] == 1


@pytest.mark.parametrize("hours", [1, 168, 8760])
def test_a_window_of_1_to_8760_hours_is_carried(hours):
    events = metric_events(
        SimpleNamespace(event_name="purchase", conversion_window_hours=hours)
    )
    assert (events.event_name, events.window_hours) == ("purchase", hours)
    assert events != PURCHASE


@pytest.mark.parametrize("hours", [0, -1, 8761, 1.5, True, "24"])
def test_any_other_window_is_refused(hours):
    with pytest.raises(ValueError):
        MetricEvents("purchase", hours)


@pytest.mark.parametrize("name", ["", None, 7])
def test_metric_events_needs_an_event_name(name):
    with pytest.raises(TypeError):
        MetricEvents(name, None)


@pytest.mark.parametrize("bare", ["purchase", ("purchase", None), ["purchase"]])
def test_a_bare_name_is_refused(bare):
    with pytest.raises(TypeError):
        metric_event_name(bare)
    with pytest.raises(TypeError):
        metric_event_filter(bare)
    with pytest.raises(TypeError):
        any_metric_event_filter([PURCHASE, bare])


def test_the_criteria_are_the_event_names_own():
    assert metric_event_name(None) is None
    assert metric_event_name(PURCHASE) == "purchase"
    assert _sql(metric_event_filter(PURCHASE)) == _sql(
        conversion_event_filter("purchase")
    )
    assert _sql(metric_event_filter(None)) == _sql(conversion_event_filter(None))
    signup = MetricEvents("signup", None)
    assert _sql(any_metric_event_filter([PURCHASE, None, signup])) == _sql(
        any_conversion_event_filter(["purchase", None, "signup"])
    )
    assert _sql(any_metric_event_filter([None])) == _sql(
        any_conversion_event_filter([])
    )


@pytest.mark.parametrize(
    "read",
    [
        lambda db: event_matching.count_converting_users(db, "e", "v", "purchase"),
        lambda db: event_matching.converting_user_ids(db, "e", "v", "purchase"),
        lambda db: event_matching.first_conversion_times(db, "e", "v", "purchase"),
        lambda db: event_matching.count_converting_users_any(db, "e", ["purchase"]),
        lambda db: AnalysisService(db)._converting_users("e", "v", "purchase"),
        lambda db: AnalysisService(db).event_counts("e", "v", "purchase"),
        lambda db: AnalysisService(db).value_totals("e", "v", "purchase"),
    ],
    ids=[
        "count_converting_users",
        "converting_user_ids",
        "first_conversion_times",
        "count_converting_users_any",
        "_converting_users",
        "event_counts",
        "value_totals",
    ],
)
def test_a_reader_handed_a_bare_name_raises_before_reading(read):
    db = MagicMock()
    with pytest.raises(TypeError):
        read(db)
    db.query.return_value.filter.return_value.group_by.return_value.all.assert_not_called()


@pytest.mark.parametrize(
    "stored",
    [
        "2026-11-01T05:00:00+00:00",
        "2026-11-01T05:00:00.000001+00:00",
        "2026-11-01T05:00:00.5+00:00",
        "2026-11-01T22:00:00-08:00",
        "2026-11-01T05:00:00Z",
        "2026-11-01T05:00:00",
        " 2026-11-01T05:00:00+00:00",
        "2026-11-01T05:00:00+00:00 ",
        "2026-11-01 05:00:00+00:00",
        "2026-11-01+00:00",
        "2026-13-01T05:00:00+00:00",
        "2026-11-01T05:00:00-05:00+00:00",
        "1234567890T234567890+00:00",
        "not a time",
        "",
    ],
)
def test_the_quick_parse_reads_what_the_long_one_does(stored):
    """A windowed read parses each event's time with ``_event_instant``: the
    same answer as ``_naive_utc`` for every value, or the same refusal."""
    from backend.app.services.event_matching import _event_instant, _naive_utc

    try:
        expected = _naive_utc(stored)
    except (TypeError, ValueError) as exc:
        with pytest.raises(type(exc)):
            _event_instant(stored)
    else:
        assert _event_instant(stored) == expected
        assert _event_instant(stored).tzinfo is None
