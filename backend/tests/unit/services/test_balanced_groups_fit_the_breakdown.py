"""Every group an experiment balances within has a breakdown row of its own (#448).

An experiment that balances within groups names at most ``MAX_GROUPS`` values
(``backend/app/core/balanced_blocks.py``), plus ``other`` and ``unknown``. Its
per-group results are breakdown rows, built by ``segment_breakdown``, which
lists at most ``MAX_LISTED_SEGMENTS`` values by themselves and counts the rest
under ``other``. So the first number must never exceed the second, or a named
group could be folded into ``other`` on its results page. Both modules also
name the two reserved groups with the same words.
"""

import pytest

from backend.app.core import balanced_blocks
from backend.app.services import segment_breakdown

pytestmark = pytest.mark.unit


def test_every_named_group_fits_in_the_breakdowns_listed_rows():
    assert balanced_blocks.MAX_GROUPS <= segment_breakdown.MAX_LISTED_SEGMENTS, (
        f"MAX_GROUPS {balanced_blocks.MAX_GROUPS} > MAX_LISTED_SEGMENTS "
        f"{segment_breakdown.MAX_LISTED_SEGMENTS}: a named group would be "
        "counted under other in its results"
    )


def test_the_reserved_groups_are_the_breakdowns_reserved_rows():
    assert balanced_blocks.OTHER == segment_breakdown.OTHER
    assert balanced_blocks.UNKNOWN == segment_breakdown.UNKNOWN


def test_a_stored_group_is_never_longer_than_a_listed_value():
    assert balanced_blocks.MAX_VALUE_LENGTH <= segment_breakdown.MAX_VALUE_LENGTH
