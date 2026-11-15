"""``STALE_FLAG_DAYS``: the days before the flag list marks a flag stale (#1808).

One value for the installation, 30 by default, from 1 to 3650. A value outside
that range, or one that is not a whole number, stops the process when the
settings are built, with pydantic's message under the variable's name.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.app.core.config import Settings

pytestmark = [pytest.mark.unit, pytest.mark.regression]


def test_the_default_is_30_days():
    assert Settings.model_fields["STALE_FLAG_DAYS"].default == 30
    assert Settings(_env_file=None).STALE_FLAG_DAYS == 30


@pytest.mark.parametrize("value", [1, 3650])
def test_the_ends_of_the_range_are_accepted(value):
    assert Settings(_env_file=None, STALE_FLAG_DAYS=value).STALE_FLAG_DAYS == value


@pytest.mark.parametrize("value", ["1", "3650", " 7 "])
def test_the_environment_variable_sets_it(monkeypatch, value):
    monkeypatch.setenv("STALE_FLAG_DAYS", value)
    assert Settings(_env_file=None).STALE_FLAG_DAYS == int(value)


@pytest.mark.parametrize(
    "value, message",
    [
        (0, "greater than or equal to 1"),
        (-1, "greater than or equal to 1"),
        (3651, "less than or equal to 3650"),
        ("abc", "valid integer"),
    ],
)
def test_a_value_outside_the_range_is_refused_naming_it(value, message):
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, STALE_FLAG_DAYS=value)
    text = str(caught.value)
    assert "STALE_FLAG_DAYS" in text
    assert message in text


@pytest.mark.parametrize(
    "value, message",
    [
        ("0", "greater than or equal to 1"),
        ("-1", "greater than or equal to 1"),
        ("3651", "less than or equal to 3650"),
        ("abc", "valid integer"),
    ],
)
def test_a_value_outside_the_range_from_the_environment_is_refused(
    monkeypatch, value, message
):
    monkeypatch.setenv("STALE_FLAG_DAYS", value)
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None)
    text = str(caught.value)
    assert "STALE_FLAG_DAYS" in text
    assert message in text
