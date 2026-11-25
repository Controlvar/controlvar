"""A flag's split and its rules' own values, as ``flag_values`` reads them (#2012).

* ``split_errors``: what a split is, and where each refusal sits. Every message
  names the field and never a value.
* ``percent_bucket``, ``split_bucket`` and ``split_value``: the bucket and the
  pick, in stored order, at every boundary.
* ``serve``: the off value unless the flag is on for the user; then the matched
  rule's own value, then the split's, then the on value. A boolean flag never
  reads either.
* ``servable_values``: one unservable value anywhere and the flag has none to
  serve, for every user.
* ``has_split_or_rule_value``: what makes an active flag's ruleset entry remote.

No request sets a split or a rule's value yet: these are the checks the read
path makes of a stored row.
"""

from __future__ import annotations

import copy
import json
import logging
from types import SimpleNamespace

import pytest

from backend.app.core import flag_values
from backend.app.core.flag_values import (
    MAX_SAFE_INTEGER,
    MAX_SPLIT_ENTRIES,
    MAX_VALUE_BYTES,
    MAX_VALUE_DEPTH,
    MIN_SPLIT_ENTRIES,
    has_split_or_rule_value,
    percent_bucket,
    servable_values,
    serve,
    served_value,
    split_bucket,
    split_errors,
    split_value,
)
from backend.app.core.log_once import EVALUATION_NOTES
from backend.app.core.targeting_adapter import (
    TargetingRulesError,
    _check_flag_percentage,
)

pytestmark = pytest.mark.unit

RED_GREEN_BLUE = [
    {"value": "red", "weight": 34},
    {"value": "green", "weight": 33},
    {"value": "blue", "weight": 33},
]


def _split(*pairs) -> list:
    return [{"value": value, "weight": weight} for value, weight in pairs]


def _found(value_type, split) -> list:
    return [(where, error.type) for where, error in split_errors(value_type, split)]


# ---------------------------------------------------------------------------
# What a split is
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value_type, split",
    [
        ("string", RED_GREEN_BLUE),
        ("string", _split(("a", 100), ("b", 0), ("c", 0))),
        ("string", _split(("a", 50), ("b", 50), ("c", 0))),
        ("string", _split(*((f"v{n}", 10) for n in range(10)))),
        # A value may be the flag's on or off value, an empty one, or falsy.
        ("string", _split(("on", 34), ("off", 33), ("", 33))),
        ("number", _split((0, 34), (2.5, 33), (-MAX_SAFE_INTEGER, 33))),
        ("json", _split(({}, 34), ([], 33), ({"a": [1, None, True]}, 33))),
        # 34.0 is a whole number, as a rule's percentage reads it.
        ("string", _split(("a", 34.0), ("b", 33), ("c", 33.0))),
        # Equal only as Python compares them: PostgreSQL holds these apart.
        ("json", _split(([1], 34), ([True], 33), (["1"], 33))),
    ],
)
def test_a_split_that_is_one(value_type, split):
    assert split_errors(value_type, split) == []


@pytest.mark.parametrize("split", [[], RED_GREEN_BLUE[:1], RED_GREEN_BLUE[:2]])
def test_fewer_than_three_values_are_refused_at_the_split(split):
    assert _found("string", split) == [((), "split_too_short")]
    ((_, error),) = split_errors("string", split)
    assert error.message() == "A split needs at least 3 values."
    assert MIN_SPLIT_ENTRIES == 3


def test_more_than_ten_values_are_refused_at_the_split():
    ten = _split(*((f"v{n}", 10) for n in range(10)))
    eleven = [*ten, {"value": "v10", "weight": 0}]
    assert _found("string", ten) == []
    assert _found("string", eleven) == [((), "split_too_long")]
    ((_, error),) = split_errors("string", eleven)
    assert error.message() == "A split can have at most 10 values."
    assert MAX_SPLIT_ENTRIES == 10


@pytest.mark.parametrize("weights", [(34, 33, 32), (34, 33, 34), (0, 0, 0)])
def test_weights_that_do_not_add_up_to_100_are_refused_at_the_split(weights):
    split = _split(*zip(("red", "green", "blue"), weights))
    assert _found("string", split) == [((), "split_total")]
    ((_, error),) = split_errors("string", split)
    assert error.message() == (
        f"Weights must add up to 100; they add up to {sum(weights)}."
    )


@pytest.mark.parametrize("split", [None, {}, "red", 3, True, {"value": 1}])
def test_anything_but_a_list_is_refused_at_the_split(split):
    assert _found("string", split) == [((), "split_type")]


@pytest.mark.parametrize("split", [RED_GREEN_BLUE, [], None])
def test_a_boolean_flag_has_no_split(split):
    """A create that does not send ``value_type`` makes a boolean flag."""
    assert _found("boolean", split) == [((), "split_boolean")]


@pytest.mark.parametrize(
    "entry",
    [
        None,
        "green",
        ["green", 33],
        {},
        {"value": "green"},
        {"weight": 33},
        {"value": "green", "weight": 33, "name": "g"},
        {"Value": "green", "weight": 33},
    ],
)
def test_an_entry_is_exactly_a_value_and_a_weight(entry):
    split = [RED_GREEN_BLUE[0], entry, RED_GREEN_BLUE[2]]
    assert _found("string", split) == [((1,), "split_entry")]


WEIGHTS = [
    *(0, 1, 33, 99, 100, 33.0, 100.0, 0.0),
    *(-1, 101, 33.5, 100.5, True, False, "33", "", None, [], {}, 10**400),
    *(float("inf"), float("-inf"), float("nan")),
]


def test_a_weight_is_what_a_rules_percentage_is():
    """The same rule as the check of a targeting rule's ``rollout_percentage``
    (``33.5``, ``true`` and ``"33"`` are refused, ``33.0`` is 33)."""

    def percentage(value) -> bool:
        try:
            _check_flag_percentage(value, "rollout_percentage")
        except (TargetingRulesError, OverflowError):
            return False
        return True

    accepted = [weight for weight in WEIGHTS if flag_values._is_weight(weight)]
    assert accepted == [weight for weight in WEIGHTS if percentage(weight)]
    assert accepted == [0, 1, 33, 99, 100, 33.0, 100.0, 0.0]
    for weight in WEIGHTS:
        split = [RED_GREEN_BLUE[0], {"value": "green", "weight": weight}]
        split.append({"value": "blue", "weight": 0})
        found = _found("string", split)
        if weight in accepted and not isinstance(weight, bool):
            assert ((1, "weight"), "split_weight") not in found, weight
        else:
            assert found == [((1, "weight"), "split_weight")], weight


@pytest.mark.parametrize(
    "value_type, value, error_type",
    [
        ("string", 5, "value_type_mismatch"),
        ("string", None, "value_type_mismatch"),
        ("string", True, "value_type_mismatch"),
        ("string", "x" * (MAX_VALUE_BYTES + 1), "value_too_large"),
        ("string", "a\x00b", "unstorable_text"),
        ("number", "5", "value_type_mismatch"),
        ("number", True, "value_type_mismatch"),
        ("number", MAX_SAFE_INTEGER + 1, "value_out_of_range"),
        ("number", float("nan"), "value_out_of_range"),
        ("json", "text", "value_type_mismatch"),
        ("json", None, "value_type_mismatch"),
        ("json", {"a": 10**400}, "value_out_of_range"),
    ],
)
def test_a_value_follows_the_rules_of_the_flags_type(value_type, value, error_type):
    others = {
        "string": ("red", "blue"),
        "number": (1, 2),
        "json": ({"a": 1}, {"a": 2}),
    }[value_type]
    split = _split((others[0], 34), (value, 33), (others[1], 33))
    assert _found(value_type, split) == [((1, "value"), error_type)]


def test_a_value_nested_too_deep_is_refused_at_its_entry():
    deep = []
    for _ in range(MAX_VALUE_DEPTH):
        deep = [deep]
    split = _split(({"a": 1}, 34), (deep, 33), ({"a": 2}, 33))
    assert _found("json", split) == [((1, "value"), "value_too_deep")]


@pytest.mark.parametrize(
    "value_type, values, later",
    [
        ("string", ("red", "green", "red"), 2),
        ("string", ("red", "red", "blue"), 1),
        ("number", (3, 4, 3.0), 2),
        ("number", (0, 1, -0.0), 2),
        ("json", ({"a": 1, "b": 2}, [1], {"b": 2.0, "a": 1}), 2),
    ],
)
def test_two_equal_values_are_refused_at_the_later(value_type, values, later):
    """Compared as PostgreSQL compares ``jsonb``: ``3`` is ``3.0``, and an
    object's keys have no order."""
    split = _split(*zip(values, (34, 33, 33)))
    assert _found(value_type, split) == [((later, "value"), "split_values_equal")]


def test_every_entry_is_reported_and_no_message_repeats_a_value():
    secret = "s3cr3t-value"
    split = _split((secret, "half"), (5, 33), (secret, 101), ("x", 33))
    errors = split_errors("string", split)
    assert [(where, error.type) for where, error in errors] == [
        ((0, "weight"), "split_weight"),
        ((1, "value"), "value_type_mismatch"),
        ((2, "value"), "split_values_equal"),
        ((2, "weight"), "split_weight"),
    ]
    for _, error in errors:
        assert secret not in error.message() and "half" not in error.message()


def test_checking_a_split_changes_nothing():
    split = copy.deepcopy(RED_GREEN_BLUE)
    split_errors("string", split)
    assert split == RED_GREEN_BLUE


# ---------------------------------------------------------------------------
# The bucket and the pick
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_the_split_bucket_is_not_the_rollouts():
    """``user-123`` and ``my-flag``: the rollout's bucket is 79, the split's 49."""
    assert percent_bucket("user-123:my-flag") == 79
    assert split_bucket("user-123", "my-flag") == 49
    assert split_bucket("user-123", "my-flag") == percent_bucket(
        "user-123:my-flag:split"
    )


@pytest.mark.parametrize(
    "bucket, value",
    [(0, "red"), (33, "red"), (34, "green"), (66, "green"), (67, "blue"), (99, "blue")],
)
def test_the_entries_take_the_buckets_in_stored_order(bucket, value):
    assert split_value(RED_GREEN_BLUE, bucket) == value


def test_reordering_the_entries_moves_the_buckets():
    """Order is part of what a split is: nothing sorts the entries."""
    reversed_ = list(reversed(RED_GREEN_BLUE))
    assert [split_value(reversed_, bucket) for bucket in (0, 32, 33, 65, 66, 99)] == [
        *("blue", "blue", "green", "green", "red", "red"),
    ]


def test_each_entry_takes_as_many_buckets_as_its_weight():
    for split in (
        RED_GREEN_BLUE,
        _split(("a", 0), ("b", 100), ("c", 0)),
        _split(("a", 1), ("b", 0), ("c", 99.0)),
        _split(*((f"v{n}", 10) for n in range(10))),
    ):
        served = [split_value(split, bucket) for bucket in range(100)]
        assert {e["value"]: served.count(e["value"]) for e in split} == {
            e["value"]: e["weight"] for e in split
        }


# ---------------------------------------------------------------------------
# What a flag serves
# ---------------------------------------------------------------------------


def _rule(rule_id, **extra) -> dict:
    condition = {"attribute": "plan", "operator": "eq", "value": rule_id}
    return {
        "id": rule_id,
        "rule": {"operator": "and", "conditions": [condition]},
        **extra,
    }


NATIVE = {
    "rules": [_rule("pro", value="pro-copy"), _rule("team")],
    "default_rule": _rule("everyone", value="everyone-copy"),
}
DASHBOARD = {
    "logical_operator": "AND",
    "groups": [
        {
            "logical_operator": "AND",
            "conditions": [{"attribute": "plan", "operator": "equals", "value": "pro"}],
        }
    ],
}


def _flag(**kwargs) -> SimpleNamespace:
    base = {
        "key": "my-flag",
        "value_type": "string",
        "on_value": "on",
        "off_value": "off",
        "targeting_rules": None,
        "split": None,
    }
    return SimpleNamespace(**{**base, **kwargs})


def _answer(enabled=True, reason="rollout", rule_id=None) -> dict:
    return {"enabled": enabled, "reason": reason, "rule_id": rule_id}


#: ``user-123``'s bucket is 49: green's.
USER = "user-123"

SERVED = [
    # (flag, evaluation, value)
    ("off", _flag(split=RED_GREEN_BLUE), _answer(False), "off"),
    (
        "inactive",
        _flag(split=RED_GREEN_BLUE, targeting_rules=NATIVE),
        _answer(False, "inactive"),
        "off",
    ),
    ("error", _flag(split=RED_GREEN_BLUE), _answer(False, "error"), "off"),
    ("two values", _flag(), _answer(), "on"),
    ("split", _flag(split=RED_GREEN_BLUE), _answer(), "green"),
    (
        "a rule without a value serves the split",
        _flag(split=RED_GREEN_BLUE, targeting_rules=NATIVE),
        _answer(reason="targeting_rule", rule_id="team"),
        "green",
    ),
    (
        "a rule without a value, and no split",
        _flag(targeting_rules=NATIVE),
        _answer(reason="targeting_rule", rule_id="team"),
        "on",
    ),
    (
        "a rule's own value wins over the split",
        _flag(split=RED_GREEN_BLUE, targeting_rules=NATIVE),
        _answer(reason="targeting_rule", rule_id="pro"),
        "pro-copy",
    ),
    (
        "the default rule's own value",
        _flag(targeting_rules=NATIVE),
        _answer(reason="targeting_rule", rule_id="everyone"),
        "everyone-copy",
    ),
    (
        "matched and outside its percentage: the off value",
        _flag(split=RED_GREEN_BLUE, targeting_rules=NATIVE),
        _answer(False, "targeting_rule", "pro"),
        "off",
    ),
    (
        "no rule matched: the rollout serves the split",
        _flag(split=RED_GREEN_BLUE, targeting_rules=NATIVE),
        _answer(),
        "green",
    ),
    (
        "the dashboard shape's one rule",
        _flag(targeting_rules={**DASHBOARD, "value": "dash-copy"}),
        _answer(reason="targeting_rule", rule_id="dashboard"),
        "dash-copy",
    ),
    (
        "the dashboard shape's rule, by its own id",
        _flag(targeting_rules={**DASHBOARD, "id": "vip", "value": "dash-copy"}),
        _answer(reason="targeting_rule", rule_id="vip"),
        "dash-copy",
    ),
    (
        "a legacy list's rule has no value of its own",
        _flag(
            split=RED_GREEN_BLUE,
            targeting_rules=[{"id": "l", "type": "user_id", "value": "legacy"}],
        ),
        _answer(reason="targeting_rule", rule_id="l"),
        "green",
    ),
    (
        "falsy values are values",
        _flag(split=_split(("", 100), ("x", 0), ("y", 0))),
        _answer(),
        "",
    ),
    (
        "a boolean flag reads neither",
        _flag(
            value_type="boolean",
            on_value=None,
            off_value=None,
            split=RED_GREEN_BLUE,
            targeting_rules=NATIVE,
        ),
        _answer(reason="targeting_rule", rule_id="pro"),
        True,
    ),
    (
        "a boolean flag, off",
        _flag(
            value_type="boolean", on_value=None, off_value=None, split=RED_GREEN_BLUE
        ),
        _answer(False),
        False,
    ),
]


@pytest.mark.parametrize(
    "flag, evaluation, value",
    [case[1:] for case in SERVED],
    ids=[case[0] for case in SERVED],
)
def test_what_a_flag_serves(flag, evaluation, value):
    servable, served = serve(flag, evaluation, USER)
    assert (servable, served) == (True, value)
    assert type(served) is type(value)


def test_two_values_are_served_as_before():
    """Without a split and a rule's value, ``serve`` is ``served_value``."""
    for flag in (
        _flag(),
        _flag(targeting_rules={**DASHBOARD}),
        _flag(value_type="number", on_value=0, off_value=2.5),
        _flag(value_type="boolean", on_value=None, off_value=None),
        SimpleNamespace(key="new"),
    ):
        for enabled in (True, False):
            answer = _answer(enabled, "targeting_rule", "dashboard")
            assert serve(flag, answer, USER) == served_value(flag, enabled)


@pytest.mark.regression
@pytest.mark.parametrize(
    "fields",
    [
        {"split": []},
        {"split": RED_GREEN_BLUE[:2]},
        {"split": _split(("red", 50), ("green", 30), ("blue", 19))},
        {"split": _split(("red", 34), (5, 33), ("blue", 33))},
        {"split": {"red": 100}},
        {"split": [{"value": "red", "weight": 100, "name": "r"}] * 3},
        {"targeting_rules": {**DASHBOARD, "value": 5}},
        {"targeting_rules": {**DASHBOARD, "value": None}},
        {"targeting_rules": {"rules": [_rule("pro", value=True)]}},
        {"targeting_rules": {"rules": [], "default_rule": _rule("all", value=[])}},
        # The two values are checked as before, whatever the split says.
        {"split": RED_GREEN_BLUE, "on_value": 5},
    ],
    ids=[
        *("empty", "two-entries", "99", "an-entry", "not-a-list", "a-name"),
        *("rule-type", "rule-null", "native-type", "default-type", "on-value"),
    ],
)
def test_one_unservable_value_and_the_flag_serves_none(fields, caplog):
    """For every user, on or off, so the answer never depends on who asks; the
    flag's key, never a value, is logged once per process."""
    EVALUATION_NOTES.clear()
    flag = _flag(**fields)
    with caplog.at_level(logging.WARNING, logger=flag_values.__name__):
        assert servable_values(flag) is None
        for enabled in (True, False):
            answer = _answer(enabled, "targeting_rule", "dashboard")
            assert serve(flag, answer, USER) == (False, None)
            assert served_value(flag, enabled) == (False, None)
    warnings = [r for r in caplog.records if r.name == flag_values.__name__]
    assert len(warnings) == 1
    assert "Flag my-flag:" in warnings[0].getMessage()
    # ("red" is in "stored".)
    for value in ("green", "blue", "pro-copy"):
        assert value not in warnings[0].getMessage()


@pytest.mark.parametrize("split", [RED_GREEN_BLUE, [], "junk"])
def test_a_boolean_flags_split_is_not_looked_at(split):
    flag = _flag(value_type="boolean", on_value=None, off_value=None, split=split)
    flag.targeting_rules = {**DASHBOARD, "value": object}
    assert servable_values(flag) == (True, False)
    assert has_split_or_rule_value(flag) is False


@pytest.mark.parametrize(
    "fields, expected",
    [
        ({}, False),
        ({"targeting_rules": DASHBOARD}, False),
        ({"targeting_rules": {"rules": [_rule("pro")]}}, False),
        ({"targeting_rules": [{"type": "user_id", "value": "x"}]}, False),
        ({"split": RED_GREEN_BLUE}, True),
        ({"split": []}, True),
        ({"targeting_rules": {**DASHBOARD, "value": "x"}}, True),
        ({"targeting_rules": {**DASHBOARD, "value": None}}, True),
        ({"targeting_rules": NATIVE}, True),
        ({"split": RED_GREEN_BLUE, "value_type": "boolean"}, False),
    ],
)
def test_whether_a_flag_has_a_split_or_a_rule_value(fields, expected):
    assert has_split_or_rule_value(_flag(**fields)) is expected


def test_serving_changes_nothing_it_is_given():
    flag = _flag(split=copy.deepcopy(RED_GREEN_BLUE), targeting_rules=NATIVE)
    before = json.dumps([flag.split, flag.targeting_rules], sort_keys=True)
    serve(flag, _answer(), USER)
    assert json.dumps([flag.split, flag.targeting_rules], sort_keys=True) == before
