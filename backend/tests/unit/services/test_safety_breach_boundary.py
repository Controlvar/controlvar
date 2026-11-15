"""A reading equal to the threshold is not a breach (UC-G11-1).

``safety_service._breaches`` decides whether a metric trips its rule.
``greater_than`` means strictly greater and ``less_than`` strictly less: a
reading that sits exactly on the limit leaves the flag alone. The helper's
other tests live in ``backend/tests/integration/services`` and run only in the
integration jobs; this one runs in "Unit Tests" and uses a NONZERO limit, so
``>=`` for ``>`` (or ``<=`` for ``<``) cannot hide behind a zero threshold.
"""

import pytest

from backend.app.services.safety_service import _breaches

pytestmark = [pytest.mark.unit, pytest.mark.regression]

LIMIT = 0.05


@pytest.mark.parametrize("comparison", ["greater_than", "less_than"])
def test_a_reading_on_the_limit_is_not_a_breach(comparison):
    assert _breaches(LIMIT, LIMIT, comparison) is False


def test_a_reading_just_above_the_limit_breaches_greater_than_only():
    assert _breaches(LIMIT + 0.001, LIMIT, "greater_than") is True
    assert _breaches(LIMIT + 0.001, LIMIT, "less_than") is False


def test_a_reading_just_below_the_limit_breaches_less_than_only():
    assert _breaches(LIMIT - 0.001, LIMIT, "less_than") is True
    assert _breaches(LIMIT - 0.001, LIMIT, "greater_than") is False
