"""A user's ``full_name``: the first and last name joined by a space, or
whichever one is set, and ``None`` when neither is."""

import pytest

from backend.app.models.user import User

pytestmark = [pytest.mark.unit]


@pytest.mark.parametrize(
    "first, last, full",
    [
        ("Pat", "Lee", "Pat Lee"),
        ("Pat", None, "Pat"),
        (None, "Lee", "Lee"),
        (None, None, None),
    ],
)
def test_full_name_is_the_names_that_are_set_joined_by_a_space(first, last, full):
    assert User(first_name=first, last_name=last).full_name == full
