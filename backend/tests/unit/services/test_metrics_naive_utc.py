"""``_naive_utc`` turns an aware bound into naive UTC and never raises (#1795).

An aware bound whose UTC instant falls outside Python's ``datetime`` range
(``0001-01-01T00:00+05:00``, ``9999-12-31T23:00-05:00``) made ``astimezone``
raise ``OverflowError``, a 500 where the database compares such a bound
without error. It is clamped to the end of the range it passed: a positive
offset underflows to ``datetime.min``, a negative one overflows to
``datetime.max``.
"""

from datetime import datetime, timedelta, timezone

import pytest

from backend.app.services.metrics_service import _naive_utc

pytestmark = [pytest.mark.unit, pytest.mark.regression]


def _at(offset_hours: float, *args: int) -> datetime:
    return datetime(*args, tzinfo=timezone(timedelta(hours=offset_hours)))


def test_an_aware_value_becomes_the_same_instant_in_naive_utc():
    assert _naive_utc(_at(5.5, 2026, 10, 15, 17, 30)) == datetime(2026, 10, 15, 12, 0)
    assert _naive_utc(_at(-7, 2026, 10, 15, 5, 0)) == datetime(2026, 10, 15, 12, 0)
    assert _naive_utc(_at(0, 2026, 10, 15, 12, 0)).tzinfo is None


def test_a_naive_value_and_none_are_returned_as_they_are():
    naive = datetime(2026, 10, 15, 12, 0)
    assert _naive_utc(naive) is naive
    assert _naive_utc(None) is None


def test_a_positive_offset_that_underflows_clamps_to_datetime_min():
    assert _naive_utc(_at(5, 1, 1, 1, 0, 0)) == datetime.min


def test_a_negative_offset_that_overflows_clamps_to_datetime_max():
    assert _naive_utc(_at(-5, 9999, 12, 31, 23, 0)) == datetime.max
