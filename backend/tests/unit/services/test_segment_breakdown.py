"""The rows of a segment breakdown, without a database (#1776).

``segment_breakdown`` places every assigned user in exactly one row and counts
their conversions there.  These tests drive its pure parts directly -- which
values are listed, which row a user is in, the order of the rows -- and
``segment_rows`` with its three readers replaced, so the counts are checked
against values written out here.  The statements themselves, and both routes,
are exercised against PostgreSQL in
``backend/tests/integration/api/test_breakdown_one_segment_per_user.py``.
"""

from __future__ import annotations

from typing import Dict, List, Optional

import pytest

from backend.app.services import segment_breakdown as sb

pytestmark = pytest.mark.unit

CONTROL = "c0000000-0000-0000-0000-000000000000"
TREATMENT = "t0000000-0000-0000-0000-000000000000"


def test_the_listed_values_and_the_length_rule_are_twenty_and_255():
    assert sb.MAX_LISTED_SEGMENTS == 20
    assert sb.MAX_VALUE_LENGTH == 255
    assert (sb.UNKNOWN, sb.OTHER) == ("unknown", "other")
    assert sb.NUL_IN_DIMENSION == "The dimension must not contain a NUL character."


# ---------------------------------------------------------------------------
# Which values are listed
# ---------------------------------------------------------------------------


def _users_per_value(counts: Dict[str, int]) -> List[str]:
    """One entry per user: ``counts[value]`` users of each value."""
    return [value for value, n in counts.items() for _ in range(n)]


def test_values_are_listed_by_users_then_by_value():
    values = _users_per_value({"b": 2, "a": 2, "c": 5, "d": 1})
    assert sb.listed_values(values) == ["c", "a", "b", "d"]


def test_at_most_the_limit_is_listed_and_the_least_common_value_is_not():
    limit = sb.MAX_LISTED_SEGMENTS
    counts = {f"v{i:02d}": 100 - i for i in range(limit + 1)}
    listed = sb.listed_values(_users_per_value(counts))
    assert len(listed) == limit
    assert f"v{limit:02d}" not in listed


def test_of_equally_common_values_the_largest_is_not_listed():
    limit = sb.MAX_LISTED_SEGMENTS
    names = [f"v{i:02d}" for i in range(limit + 1)]
    # Insertion order must not decide which one is left out.
    for order in (names, list(reversed(names))):
        listed = sb.listed_values(_users_per_value(dict.fromkeys(order, 3)))
        assert listed == names[:limit]


def test_unknown_and_other_values_take_no_listed_place():
    limit = sb.MAX_LISTED_SEGMENTS
    counts = {f"v{i:02d}": 1 for i in range(limit)}
    counts.update({"unknown": 50, "other": 50})
    values = _users_per_value(counts) + [None] * 50
    assert sb.listed_values(values) == sorted(f"v{i:02d}" for i in range(limit))


@pytest.mark.parametrize(
    "value, row",
    [
        (None, "unknown"),
        ("unknown", "unknown"),
        ("other", "other"),
        ("ios", "ios"),
        ("android", "other"),
        ("iOS", "other"),
        (" ios ", "other"),
    ],
)
def test_the_row_of_a_value(value, row):
    assert sb.segment_of(value, {"ios"}) == row


def test_the_fold_puts_the_listed_values_first_then_other_then_unknown():
    values = {
        "u1": "ios",
        "u2": "unknown",
        "u3": "other",
        "u4": None,
        "u5": "web",
        "u6": "web",
    }
    row_of, order = sb.fold(values)
    assert order == ["web", "ios", "other", "unknown"]
    assert row_of == {
        "u1": "ios",
        "u2": "unknown",
        "u3": "other",
        "u4": "unknown",
        "u5": "web",
        "u6": "web",
    }


def test_the_fold_lists_other_and_unknown_only_when_they_have_users():
    row_of, order = sb.fold({"u1": "ios", "u2": "web"})
    assert order == ["ios", "web"]
    assert set(row_of.values()) == {"ios", "web"}


# ---------------------------------------------------------------------------
# segment_rows, with its readers replaced
# ---------------------------------------------------------------------------


class _Readers:
    """The three reads segment_rows makes, answered from plain dicts."""

    def __init__(
        self,
        assigned: Dict[str, List[str]],
        values: Dict[str, str],
        converters: Dict[Optional[str], Dict[str, List[str]]],
    ):
        self.assigned = assigned
        self.values = values
        self.converters = converters
        self.calls: List[str] = []

    def assignment_times(self, db, experiment_id):
        self.calls.append("assignment_times")
        return {v: dict.fromkeys(users) for v, users in self.assigned.items()}

    def user_values(self, db, experiment_id, dimension):
        self.calls.append(f"user_values:{dimension}")
        return dict(self.values)

    def converting_user_ids(self, db, experiment_id, variant_id, event_name):
        self.calls.append(f"converting_user_ids:{variant_id}:{event_name}")
        return set(self.converters.get(event_name, {}).get(variant_id, []))


@pytest.fixture
def readers(monkeypatch):
    def install(assigned, values, converters):
        fake = _Readers(assigned, values, converters)
        monkeypatch.setattr(sb, "assignment_times", fake.assignment_times)
        monkeypatch.setattr(sb, "user_values", fake.user_values)
        monkeypatch.setattr(sb, "converting_user_ids", fake.converting_user_ids)
        return fake

    return install


def _as_table(rows, event_name="purchase"):
    """``{segment: {variant: (users, converting users)}}``."""
    return {
        row.value: {
            variant: (row.users[variant], row.converters[event_name][variant])
            for variant in row.users
        }
        for row in rows
    }


def test_each_assigned_user_is_in_one_row_and_converts_there(readers):
    readers(
        assigned={CONTROL: ["c1", "c2", "c3"], TREATMENT: ["t1", "t2", "t3"]},
        # c3 has no value; the ghost is not assigned.
        values={
            "c1": "ios",
            "c2": "web",
            "t1": "ios",
            "t2": "ios",
            "t3": "web",
            "ghost": "ios",
        },
        converters={"purchase": {CONTROL: ["c3"], TREATMENT: ["t1", "t3"]}},
    )

    rows = sb.segment_rows(None, "exp", "platform", ["purchase"])

    assert [row.value for row in rows] == ["ios", "web", "unknown"]
    assert _as_table(rows) == {
        "ios": {CONTROL: (1, 0), TREATMENT: (2, 1)},
        "web": {CONTROL: (1, 0), TREATMENT: (1, 1)},
        "unknown": {CONTROL: (1, 1), TREATMENT: (0, 0)},
    }
    # The rows add up to every assigned user and every converter.
    for variant, users, converted in ((CONTROL, 3, 1), (TREATMENT, 3, 2)):
        assert sum(r.users[variant] for r in rows) == users
        assert sum(r.converters["purchase"][variant] for r in rows) == converted


def test_each_event_name_is_counted_once_per_variant(readers):
    fake = readers(
        assigned={CONTROL: ["c1"], TREATMENT: ["t1"]},
        values={"c1": "ios", "t1": "ios"},
        converters={
            "purchase": {TREATMENT: ["t1"]},
            "signup": {CONTROL: ["c1"], TREATMENT: ["t1"]},
        },
    )

    rows = sb.segment_rows(None, "exp", "platform", ["purchase", "signup", "purchase"])

    assert _as_table(rows, "purchase") == {"ios": {CONTROL: (1, 0), TREATMENT: (1, 1)}}
    assert _as_table(rows, "signup") == {"ios": {CONTROL: (1, 1), TREATMENT: (1, 1)}}
    assert fake.calls == [
        "assignment_times",
        "user_values:platform",
        f"converting_user_ids:{CONTROL}:purchase",
        f"converting_user_ids:{TREATMENT}:purchase",
        f"converting_user_ids:{CONTROL}:signup",
        f"converting_user_ids:{TREATMENT}:signup",
    ]


def test_a_converter_assigned_after_the_assignments_were_read_is_left_out(readers):
    readers(
        assigned={CONTROL: ["c1"]},
        values={"c1": "ios", "late": "ios"},
        converters={None: {CONTROL: ["c1", "late"]}},
    )

    rows = sb.segment_rows(None, "exp", "platform", [None])

    assert _as_table(rows, None) == {"ios": {CONTROL: (1, 1)}}


def test_beyond_the_limit_values_are_counted_under_other(readers):
    limit = sb.MAX_LISTED_SEGMENTS
    # v00 has three users; v01..v20 one each, so v20 is the one left out.
    values = {"a1": "v00", "a2": "v00", "a3": "v00"}
    values.update({f"b{i:02d}": f"v{i:02d}" for i in range(1, limit + 1)})
    # A literal "other" value joins the folded row; a user with none is unknown.
    values["lit"] = "other"
    users = [*values, "none"]
    readers(
        assigned={CONTROL: users},
        values=values,
        converters={"purchase": {CONTROL: [f"b{limit:02d}", "lit", "a1"]}},
    )

    rows = sb.segment_rows(None, "exp", "platform", ["purchase"])

    expected_values = [f"v{i:02d}" for i in range(limit)]
    assert [r.value for r in rows] == [*expected_values, "other", "unknown"]
    table = _as_table(rows)
    assert table["v00"] == {CONTROL: (3, 1)}
    assert table["other"] == {CONTROL: (2, 2)}
    assert table["unknown"] == {CONTROL: (1, 0)}
    assert sum(r.users[CONTROL] for r in rows) == len(users)


def test_an_experiment_with_no_assignments_has_no_rows_and_reads_no_values(readers):
    fake = readers(assigned={}, values={"u1": "ios"}, converters={})

    assert sb.segment_rows(None, "exp", "platform", ["purchase"]) == []
    assert fake.calls == ["assignment_times"]
