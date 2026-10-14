"""A split URL variant's traffic_allocation is from 0 to 100 (#1194).

Each variant's ``traffic_allocation`` is a percentage. Only the SUM was
checked, so ``-50`` and ``150`` were accepted because they add up to 100, and
so were ``-0.01`` and ``100.01``. Each allocation is now held to 0 to 100
inclusive, and the sum is still held to 100.

Every refusal names the allocation: the error's ``loc`` ends
``variants, <i>, traffic_allocation``, and under a request body it starts
``split_url_config``. The HTTP answers are
``backend/tests/integration/api/test_split_url_allocation_range_api.py``.
"""

from __future__ import annotations

from typing import Dict, Sequence

import pytest
from pydantic import ValidationError

from backend.app.schemas.experiment import ExperimentUpdate
from backend.app.schemas.split_url_config import SplitUrlConfig, SplitUrlVariant

LOW = "greater_than_equal"
HIGH = "less_than_equal"


def _config(*allocations: float) -> Dict:
    return {
        "variants": [
            {
                "name": f"variant {i}",
                "url": f"https://example.com/{i}",
                "traffic_allocation": allocation,
            }
            for i, allocation in enumerate(allocations)
        ]
    }


def _errors(error: ValidationError, prefix: Sequence = ()) -> Dict[tuple, str]:
    """``{loc: type}`` of every error, with ``prefix`` removed from each loc."""
    n = len(prefix)
    result = {}
    for e in error.errors():
        assert tuple(e["loc"][:n]) == tuple(prefix), e
        result[tuple(e["loc"][n:])] = e["type"]
    return result


def _at(i: int) -> tuple:
    return ("variants", i, "traffic_allocation")


#: (id, allocations, {loc: error type}). Every row's sum is right, so the sum
#: check is not what refuses it.
REFUSED = [
    ("minus-50-and-150", (-50, 150), {_at(0): LOW, _at(1): HIGH}),
    ("150-and-minus-50", (150, -50), {_at(0): HIGH, _at(1): LOW}),
    ("just-under-0-and-just-over-100", (-0.01, 100.01), {_at(0): LOW, _at(1): HIGH}),
    ("just-over-100-and-just-under-0", (100.01, -0.01), {_at(0): HIGH, _at(1): LOW}),
    ("only-the-last-of-three", (60, 50, -10), {_at(2): LOW}),
    ("only-the-middle-of-three", (-20, 120, 0), {_at(0): LOW, _at(1): HIGH}),
]

ACCEPTED = [
    ("0-and-100", (0, 100)),
    ("100-and-0", (100, 0)),
    ("floats-at-the-limits", (0.0, 100.0)),
    ("an-even-split", (50, 50)),
    ("three-ways", (0, 50, 50)),
    ("a-third-each", (33.33, 33.33, 33.34)),
]


@pytest.mark.regression
@pytest.mark.parametrize(
    "allocations, expected", [r[1:] for r in REFUSED], ids=[r[0] for r in REFUSED]
)
def test_an_allocation_outside_0_to_100_is_refused_though_the_sum_is_right(
    allocations, expected
):
    assert abs(sum(allocations) - 100) <= 0.01  # the sum check would pass

    with pytest.raises(ValidationError) as caught:
        SplitUrlConfig(**_config(*allocations))

    assert _errors(caught.value) == expected


@pytest.mark.parametrize(
    "allocations", [r[1] for r in ACCEPTED], ids=[r[0] for r in ACCEPTED]
)
def test_an_allocation_from_0_to_100_is_accepted(allocations):
    config = SplitUrlConfig(**_config(*allocations))

    assert [v.traffic_allocation for v in config.variants] == list(allocations)


@pytest.mark.regression
@pytest.mark.parametrize(
    "allocations, expected", [r[1:] for r in REFUSED], ids=[r[0] for r in REFUSED]
)
def test_the_refusal_names_split_url_config_variants_the_index_and_the_field(
    allocations, expected
):
    """What a request body's 422 carries: ``["body", <this loc>]``."""
    with pytest.raises(ValidationError) as caught:
        ExperimentUpdate(split_url_config=_config(*allocations))

    assert _errors(caught.value, prefix=("split_url_config",)) == expected


def test_the_limits_are_inclusive_and_exact():
    """0 and 100 pass, the nearest floats outside them do not."""
    SplitUrlVariant(name="a", url="https://a.example.com", traffic_allocation=0)
    SplitUrlVariant(name="a", url="https://a.example.com", traffic_allocation=100)
    for outside in (-1e-9, 100.000000001):
        with pytest.raises(ValidationError):
            SplitUrlVariant(
                name="a", url="https://a.example.com", traffic_allocation=outside
            )


def test_the_sum_must_still_be_100_within_the_tolerance():
    """The range check replaced nothing: in-range values must still add up."""
    with pytest.raises(ValidationError, match="Traffic allocations must sum to 100"):
        SplitUrlConfig(**_config(40, 40))
    with pytest.raises(ValidationError, match="Traffic allocations must sum to 100"):
        SplitUrlConfig(**_config(100, 100))
    assert SplitUrlConfig(**_config(50, 50.009)).variants
