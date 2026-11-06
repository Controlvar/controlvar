"""The group of a user of an experiment balanced within saved segments (#448).

No database: :mod:`backend.app.core.balanced_blocks` and the request schema.

* A user is balanced in the first listed segment they belong to; a user in
  none of them is ``other``; a user whose membership of a segment listed
  before the first one they belong to cannot be decided is ``unknown``.
* The groups such a setting names are its segments, in the order listed.
* The request schema takes 1 to 10 segment ids, and the setting is stored
  with the ids alone.
"""

from __future__ import annotations

import uuid

import pytest

from backend.app.core import balanced_blocks as ba
from backend.app.schemas.balance_within import BalanceWithin

pytestmark = pytest.mark.unit

A = "00000000-0000-4000-8000-00000000000a"
B = "00000000-0000-4000-8000-00000000000b"
C = "00000000-0000-4000-8000-00000000000c"

GROUPS = [
    # (listed, members, undecided, group)
    ((A, B), {A}, set(), A),
    ((A, B), {B}, set(), B),
    # in two of them: the first listed, whatever order the ids sort in
    ((A, B), {A, B}, set(), A),
    ((B, A), {A, B}, set(), B),
    # in none
    ((A, B), set(), set(), "other"),
    # cannot be decided before the first one the user is in
    ((C, A), {A}, {C}, "unknown"),
    ((A, C), set(), {C}, "unknown"),
    # decided first: a later segment that cannot be decided does not matter
    ((A, C), {A}, {C}, A),
    # a segment that cannot be decided is never a member
    ((C,), {C}, {C}, "unknown"),
    # nothing listed
    ((), set(), set(), "other"),
]


@pytest.mark.regression
@pytest.mark.parametrize(("listed", "members", "undecided", "group"), GROUPS)
def test_the_first_listed_segment_a_user_belongs_to_is_their_group(
    listed, members, undecided, group
):
    found = ba.segment_group(listed, frozenset(members), frozenset(undecided))
    assert found == group, f"{listed}, members {sorted(members)}: {found} != {group}"


def test_a_segment_setting_names_its_segments_in_order():
    setting = {"segments": [B, A, C]}
    assert ba.balances_within_segments(setting)
    assert ba.balance_segments(setting) == (B, A, C)
    assert ba.named_groups(setting) == (B, A, C)


@pytest.mark.parametrize(
    "setting",
    [
        {"attribute": "tier", "values": ["gold"]},
        {"attribute": "web_device"},
        None,
        {},
    ],
)
def test_an_attribute_setting_has_no_segments(setting):
    assert not ba.balances_within_segments(setting)
    assert ba.balance_segments(setting) == ()


def test_a_stored_segment_list_keeps_only_its_text():
    """A list written past the API: what is not text is no segment."""
    assert ba.balance_segments({"segments": [A, 7, None, B]}) == (A, B)
    assert ba.balance_segments({"segments": "not a list"}) == ()


@pytest.mark.regression
def test_the_schema_takes_up_to_ten_segment_ids():
    ids = [str(uuid.uuid4()) for _ in range(10)]
    setting = BalanceWithin.model_validate({"segments": ids})
    assert setting.stored() == {"segments": ids}
