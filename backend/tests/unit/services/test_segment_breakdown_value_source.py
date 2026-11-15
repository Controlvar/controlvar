"""A breakdown's rows from a per-user value source, without a database (#448).

``segment_breakdown.segment_rows`` reads each user's value of the dimension
from a value source: ``user_values`` (the user's first keyed event of the
experiment) unless the caller passes another as ``value_source``.  These tests
pin that seam:

* with no ``value_source`` the rows are exactly the rows ``user_values`` gives,
  and ``user_values`` is looked up when ``segment_rows`` is called, so the two
  breakdown routes, which pass none, are unchanged;
* a source passed in is called once, with the same arguments, at the same
  point, and its values are folded by the same rule (20 listed values,
  ``other``, ``unknown``).

The readers are replaced as in ``test_segment_breakdown.py``, so every count is
checked against values written out here.
"""

from __future__ import annotations

import inspect
from typing import Dict, List, Optional

import pytest

from backend.app.services import segment_breakdown as sb

pytestmark = pytest.mark.unit

CONTROL = "c0000000-0000-0000-0000-000000000000"
TREATMENT = "t0000000-0000-0000-0000-000000000000"
DB = object()
EXPERIMENT = "exp-448"


class _Readers:
    """``assignment_times`` and ``converting_user_ids`` from plain dicts, with
    every call recorded in ``calls``; ``user_values`` answers ``values``."""

    def __init__(
        self,
        assigned: Dict[str, List[str]],
        values: Dict[str, str],
        converters: Dict[Optional[str], Dict[str, List[str]]],
    ):
        self.assigned = assigned
        self.values = values
        self.converters = converters
        self.calls: List[tuple] = []

    def assignment_times(self, db, experiment_id):
        self.calls.append(("assignment_times", db, experiment_id))
        return {v: dict.fromkeys(users) for v, users in self.assigned.items()}

    def user_values(self, db, experiment_id, dimension):
        self.calls.append(("user_values", db, experiment_id, dimension))
        return dict(self.values)

    def converting_user_ids(self, db, experiment_id, variant_id, event_name):
        self.calls.append(("converting_user_ids", variant_id, event_name))
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


class _Source:
    """A value source answering ``values``; records each call."""

    def __init__(self, values: Dict[str, str]):
        self.values = values
        self.calls: List[tuple] = []

    def __call__(self, db, experiment_id, dimension):
        self.calls.append((db, experiment_id, dimension))
        return dict(self.values)


def _as_table(rows, event_name="purchase"):
    """``[(segment, {variant: (users, converting users)})]``, in row order."""
    return [
        (
            row.value,
            {
                variant: (row.users[variant], row.converters[event_name][variant])
                for variant in row.users
            },
        )
        for row in rows
    ]


#: Six users per variant: three ios, two web and one with no value, plus a
#: user who is not assigned.
ASSIGNED = {
    CONTROL: ["c1", "c2", "c3", "c4", "c5", "c6"],
    TREATMENT: ["t1", "t2", "t3", "t4", "t5", "t6"],
}
EVENT_VALUES = {
    **dict.fromkeys(("c1", "c2", "c3", "t1", "t2", "t3"), "ios"),
    **dict.fromkeys(("c4", "c5", "t4", "t5"), "web"),
    "ghost": "ios",
}
CONVERTERS = {"purchase": {CONTROL: ["c1", "c6"], TREATMENT: ["t1", "t2", "t4"]}}
EVENT_ROWS = [
    ("ios", {CONTROL: (3, 1), TREATMENT: (3, 2)}),
    ("web", {CONTROL: (2, 0), TREATMENT: (2, 1)}),
    ("unknown", {CONTROL: (1, 1), TREATMENT: (1, 0)}),
]


def test_value_source_is_a_keyword_only_argument_that_defaults_to_none():
    parameter = inspect.signature(sb.segment_rows).parameters["value_source"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is None


def test_without_a_value_source_the_rows_are_user_values_rows(readers):
    """No ``value_source``: ``user_values`` is read, looked up when called
    (here it is the fake installed after import), with the breakdown's own
    arguments, between the assignments and the converters."""
    fake = readers(ASSIGNED, EVENT_VALUES, CONVERTERS)

    rows = sb.segment_rows(DB, EXPERIMENT, "platform", ["purchase"])

    assert _as_table(rows) == EVENT_ROWS
    assert fake.calls == [
        ("assignment_times", DB, EXPERIMENT),
        ("user_values", DB, EXPERIMENT, "platform"),
        ("converting_user_ids", CONTROL, "purchase"),
        ("converting_user_ids", TREATMENT, "purchase"),
    ]


def test_value_source_none_and_user_values_give_the_same_rows(readers):
    fake = readers(ASSIGNED, EVENT_VALUES, CONVERTERS)

    default = sb.segment_rows(DB, EXPERIMENT, "platform", ["purchase"])
    explicit_none = sb.segment_rows(
        DB, EXPERIMENT, "platform", ["purchase"], value_source=None
    )
    explicit = sb.segment_rows(
        DB, EXPERIMENT, "platform", ["purchase"], value_source=sb.user_values
    )

    assert _as_table(default) == _as_table(explicit_none) == _as_table(explicit)
    assert _as_table(default) == EVENT_ROWS
    reads = [call for call in fake.calls if call[0] == "user_values"]
    assert reads == [("user_values", DB, EXPERIMENT, "platform")] * 3


def test_a_value_source_passed_in_places_the_users_and_user_values_is_not_read(
    readers,
):
    """The same users and converters, placed by another source: c1, t1 and t2
    move from ios to android, t4 loses its value and c6 gains one."""
    fake = readers(ASSIGNED, EVENT_VALUES, CONVERTERS)
    source = _Source(
        {
            **dict.fromkeys(("c1", "t1", "t2"), "android"),
            **dict.fromkeys(("c2", "c3", "t3"), "ios"),
            **dict.fromkeys(("c4", "c5", "c6", "t5"), "web"),
        }
    )

    rows = sb.segment_rows(
        DB, EXPERIMENT, "platform", ["purchase"], value_source=source
    )

    assert _as_table(rows) == [
        ("web", {CONTROL: (3, 1), TREATMENT: (1, 0)}),
        ("android", {CONTROL: (1, 1), TREATMENT: (2, 2)}),
        ("ios", {CONTROL: (2, 0), TREATMENT: (1, 0)}),
        ("unknown", {CONTROL: (0, 0), TREATMENT: (2, 1)}),
    ]
    assert source.calls == [(DB, EXPERIMENT, "platform")]
    assert fake.calls == [
        ("assignment_times", DB, EXPERIMENT),
        ("converting_user_ids", CONTROL, "purchase"),
        ("converting_user_ids", TREATMENT, "purchase"),
    ]
    # Per variant the rows add up to every assigned user and every converter.
    for variant, converted in ((CONTROL, 2), (TREATMENT, 3)):
        assert sum(r.users[variant] for r in rows) == len(ASSIGNED[variant])
        assert sum(r.converters["purchase"][variant] for r in rows) == converted


def test_a_value_source_passed_in_is_folded_by_the_same_rule(readers):
    """21 values: the 20 with the most users are listed, the rest and a
    literal ``other`` under ``other``, a user with no value under ``unknown``,
    exactly as ``user_values``' values are."""
    limit = sb.MAX_LISTED_SEGMENTS
    values = {"a1": "v00", "a2": "v00", "a3": "v00"}
    values.update({f"b{i:02d}": f"v{i:02d}" for i in range(1, limit + 1)})
    values["lit"] = "other"
    users = [*values, "none"]
    converters = {"purchase": {CONTROL: [f"b{limit:02d}", "lit", "a1"]}}

    readers({CONTROL: users}, values, converters)
    from_user_values = sb.segment_rows(DB, EXPERIMENT, "platform", ["purchase"])
    readers({CONTROL: users}, {}, converters)
    from_source = sb.segment_rows(
        DB, EXPERIMENT, "platform", ["purchase"], value_source=_Source(values)
    )

    assert _as_table(from_source) == _as_table(from_user_values)
    assert [r.value for r in from_source] == [
        *(f"v{i:02d}" for i in range(limit)),
        "other",
        "unknown",
    ]
    table = dict(_as_table(from_source))
    assert table["v00"] == {CONTROL: (3, 1)}
    assert table["other"] == {CONTROL: (2, 2)}
    assert table["unknown"] == {CONTROL: (1, 0)}


def test_a_value_source_is_not_called_for_an_experiment_with_no_assignments(
    readers,
):
    fake = readers({}, {"u1": "ios"}, {})
    source = _Source({"u1": "ios"})

    assert (
        sb.segment_rows(DB, EXPERIMENT, "platform", ["purchase"], value_source=source)
        == []
    )
    assert source.calls == []
    assert fake.calls == [("assignment_times", DB, EXPERIMENT)]
