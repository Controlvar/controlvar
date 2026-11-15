"""The paging bound and the page check (#1001).

``backend/app/api/query_params.py`` holds the bound, the database's own:
``MAX_OFFSET`` (2**63 - 1, the largest bigint PostgreSQL takes as an OFFSET)
for ``skip`` and ``offset``, ``MAX_PAGE`` (2**63) for ``page``, and
``check_page_offset``, which refuses a page whose ``(page - 1) * <size>`` is
past ``MAX_OFFSET`` with a 422 at ``["query", "page"]`` naming the route's own
size parameter and no value sent. Which routes carry them is
``test_paging_params_declared.py``'s; that the database then answers is
``backend/tests/integration/api/test_offset_bounds_api.py``'s.
"""

from __future__ import annotations

import pytest
from fastapi.exceptions import RequestValidationError

from backend.app.api.query_params import (
    MAX_OFFSET,
    MAX_PAGE,
    check_page_offset,
    page_offset_too_large,
)

pytestmark = [pytest.mark.unit, pytest.mark.regression]


def test_the_bound_is_the_largest_bigint():
    assert MAX_OFFSET == 9223372036854775807 == 2**63 - 1
    assert MAX_PAGE == 2**63


@pytest.mark.parametrize(
    ("page", "size"),
    [
        (1, 1),
        (MAX_PAGE, 1),
        (2, MAX_OFFSET),
        (2**62, 2),
        (1, 10**30),
    ],
    ids=[
        "first",
        "last page of 1",
        "second page of the largest",
        "2**62 of 2",
        "page 1",
    ],
)
def test_a_page_the_database_can_start_passes(page, size):
    assert check_page_offset(page, size, "limit") is None


@pytest.mark.parametrize(
    ("page", "size", "size_param"),
    [
        (MAX_PAGE + 1, 1, "limit"),
        (2**62 + 1, 2, "limit"),
        (3, MAX_OFFSET, "page_size"),
        (2**33, 2**31, "page_size"),
    ],
)
def test_a_page_that_starts_past_the_largest_offset_is_refused(page, size, size_param):
    with pytest.raises(RequestValidationError) as caught:
        check_page_offset(page, size, size_param)
    assert caught.value.errors() == [
        {
            "type": "value_error",
            "loc": ("query", "page"),
            "msg": f"(page - 1) * {size_param} must be at most 9223372036854775807",
        }
    ]
    if page > 10**6:
        assert str(page) not in str(caught.value.errors())


def test_the_message_names_the_size_parameter_and_no_value():
    assert page_offset_too_large("limit") == (
        "(page - 1) * limit must be at most 9223372036854775807"
    )
    assert page_offset_too_large("page_size") == (
        "(page - 1) * page_size must be at most 9223372036854775807"
    )
