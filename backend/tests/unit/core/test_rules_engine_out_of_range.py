"""A context value out of range for a comparison makes the condition unevaluable.

An integer too large for a float, or a timestamp no date can hold, cannot be
compared. ``apply_operator`` raises ``PatternUnevaluable`` with reason
``OUT_OF_RANGE`` for it, the way a pattern condition that cannot be evaluated
does, and the caller that owns the ruleset abandons the ruleset: a flag
answers ``enabled: false`` with reason ``error``, as before. Every value that
can be compared keeps its answer.

The sweep walks every member of ``OperatorType``, so a new operator is covered
without editing this file.
"""

from __future__ import annotations

import itertools
import math
from unittest import mock

import pytest

from backend.app.core import rules_engine
from backend.app.core.pattern_match import PatternUnevaluable, UnevaluableReason
from backend.app.core.rules_engine import (
    SegmentMembershipUnavailable,
    TimeWindowUnreadable,
    apply_operator,
)
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.schemas.targeting_rule import OperatorType
from backend.app.services import feature_flag_service
from backend.app.services.feature_flag_service import FeatureFlagService
from backend.app.services.metrics_service import MetricsService
from backend.app.services.segment_membership import SegmentMemberships

pytestmark = [pytest.mark.unit, pytest.mark.regression]

#: Values a client can send in its context (JSON allows the large integers and,
#: as Python's json reads it, ``Infinity`` and ``NaN``).
HOSTILE = [
    10**400,
    -(10**400),
    10**4000,
    10**17,
    -(10**17),
    math.inf,
    -math.inf,
    math.nan,
    "1" * 400,
    1e20,
    [10**400],
    {"lat": 10**400, "lon": 1},
]

#: Rule values: numeric strings (the dashboard stores every value as a
#: string), numbers, ISO dates, and each operator's own shape.
RULE_VALUES = [
    "18",
    "1234",
    "0.5",
    18,
    18.5,
    "2026-01-01T00:00:00",
    "2026-01-01T00:00:00Z",
    "2026-01-01",
    "abc",
    ["18", "pro"],
    [18, 1234],
    "^a.*$",
    "2.0.0",
    {"lat": 48.85, "lon": 2.35, "radius": 50},
    [48.85, 2.35],
    {"start_time": "09:00", "end_time": "17:00"},
    {"min": 1, "max": 5},
]

#: Second values: comparisons, upper bounds, a radius.
ADDITIONAL_VALUES = [
    None,
    "gte",
    "between",
    "100",
    100,
    "2030-01-01T00:00:00",
    {"radius": 10},
]

#: Raised on purpose, and handled by every caller that owns a ruleset
#: (TimeWindowUnreadable: a rule's window that cannot be read).
ALLOWED = (PatternUnevaluable, SegmentMembershipUnavailable, TimeWindowUnreadable)


def test_no_value_makes_any_operator_raise_anything_else():
    """Every operator x hostile value x rule value: an answer, or an allowed raise."""
    escaped = []
    for operator, hostile, rule_value, additional in itertools.product(
        list(OperatorType), HOSTILE, RULE_VALUES, ADDITIONAL_VALUES
    ):
        try:
            apply_operator(operator, hostile, rule_value, additional)
        except ALLOWED:
            continue
        except Exception as exc:  # anything else is a finding
            escaped.append(
                f"{operator.value}({_short(hostile)}, {rule_value!r}, {additional!r})"
                f" -> {type(exc).__name__}"
            )
    by_operator = sorted({line.split("(", 1)[0] for line in escaped})
    assert not escaped, (
        f"{len(escaped)} calls raised, operators {by_operator}; first: {escaped[:5]}"
    )


#: Integers float() cannot hold.
OUT_OF_RANGE_NUMBERS = [10**400, -(10**400), 10**4000]
#: Values datetime.fromtimestamp cannot represent: from about 7e16 seconds its
#: year no longer fits the C library (OSError), beyond time_t it overflows.
OUT_OF_RANGE_DATES = [
    10**400,
    -(10**400),
    10**4000,
    math.inf,
    -math.inf,
    1e20,
    10**17,
    -(10**17),
]

#: The eleven operators: (operator, [(rule value, second value)], out-of-range values).
CASES = [
    (OperatorType.EQUALS, [("18", None), ("1234", None)], OUT_OF_RANGE_NUMBERS),
    (OperatorType.NOT_EQUALS, [("18", None), ("1234", None)], OUT_OF_RANGE_NUMBERS),
    (OperatorType.IN, [(["18", "pro"], None)], OUT_OF_RANGE_NUMBERS),
    (OperatorType.NOT_IN, [(["18", "pro"], None)], OUT_OF_RANGE_NUMBERS),
    (OperatorType.GREATER_THAN, [("18", None), (18, None)], OUT_OF_RANGE_NUMBERS),
    (
        OperatorType.GREATER_THAN_OR_EQUAL,
        [("18", None), (18, None)],
        OUT_OF_RANGE_NUMBERS,
    ),
    (OperatorType.LESS_THAN, [("18", None), (18, None)], OUT_OF_RANGE_NUMBERS),
    (
        OperatorType.LESS_THAN_OR_EQUAL,
        [("18", None), (18, None)],
        OUT_OF_RANGE_NUMBERS,
    ),
    (
        OperatorType.BEFORE,
        [("2026-01-01T00:00:00", None), (1767225600, None)],
        OUT_OF_RANGE_DATES,
    ),
    (
        OperatorType.AFTER,
        [("2026-01-01T00:00:00", None), (1767225600, None)],
        OUT_OF_RANGE_DATES,
    ),
    (
        OperatorType.BETWEEN,
        [("1", "100"), ("2020-01-01T00:00:00", "2030-01-01T00:00:00")],
        None,  # per bound: numbers for numeric bounds, dates for date bounds
    ),
]


def _out_of_range_for(operator, rule_value, additional, values):
    if values is not None:
        return values
    # between: integers overflow the numeric bounds; every date value the date bounds.
    return OUT_OF_RANGE_NUMBERS if rule_value == "1" else OUT_OF_RANGE_DATES


@pytest.mark.parametrize(
    "operator, rules, values", CASES, ids=[case[0].value for case in CASES]
)
def test_an_out_of_range_value_is_unevaluable(operator, rules, values):
    for rule_value, additional in rules:
        for value in _out_of_range_for(operator, rule_value, additional, values):
            with pytest.raises(PatternUnevaluable) as raised:
                apply_operator(operator, value, rule_value, additional)
            assert raised.value.reason is UnevaluableReason.OUT_OF_RANGE, (
                operator.value,
                _short(value),
            )


def test_comparable_values_still_compare():
    """Values in range keep their answers; a huge numeric string is a huge number."""
    assert apply_operator(OperatorType.GREATER_THAN, 30, "18") is True
    assert apply_operator(OperatorType.GREATER_THAN, 10, "18") is False
    assert apply_operator(OperatorType.GREATER_THAN, math.inf, "18") is True
    assert apply_operator(OperatorType.GREATER_THAN, "1" * 400, "18") is True
    assert apply_operator(OperatorType.EQUALS, 18, "18") is True
    assert apply_operator(OperatorType.NOT_EQUALS, 19, "18") is True
    assert apply_operator(OperatorType.BEFORE, 0, "2026-01-01T00:00:00") is True
    assert apply_operator(OperatorType.BETWEEN, 50, "1", "100") is True
    assert apply_operator(OperatorType.GREATER_THAN, "abc", "18") is False


def test_an_oserror_from_fromtimestamp_is_out_of_range(monkeypatch):
    """fromtimestamp raises OSError, not OverflowError, for a timestamp whose year
    the C library cannot hold (from about 7e16 seconds up to 2**63 - 1)."""

    class _Datetime(rules_engine.datetime):
        @classmethod
        def fromtimestamp(cls, *args, **kwargs):
            raise OSError(75, "Value too large for defined data type")

    monkeypatch.setattr(rules_engine, "datetime", _Datetime)
    for operator, rule_value, additional in (
        (OperatorType.BEFORE, "2026-01-01T00:00:00", None),
        (OperatorType.AFTER, "2026-01-01T00:00:00", None),
        (OperatorType.BETWEEN, "2020-01-01T00:00:00", "2030-01-01T00:00:00"),
    ):
        with pytest.raises(PatternUnevaluable) as raised:
            apply_operator(operator, 10**17, rule_value, additional)
        assert raised.value.reason is UnevaluableReason.OUT_OF_RANGE


# ---------------------------------------------------------------------------
# Through the flag service: the answer is unchanged, off with reason "error"
# ---------------------------------------------------------------------------


def _dashboard(operator, value):
    return {
        "logical_operator": "or",
        "groups": [
            {
                "logical_operator": "and",
                "conditions": [
                    {"attribute": "a", "operator": operator, "value": value}
                ],
            }
        ],
    }


def _native(group):
    return {"rules": [{"id": "r", "rule": group, "rollout_percentage": 100}]}


def _cond(operator, value, additional=None):
    out = {"attribute": "a", "operator": operator, "value": value}
    if additional is not None:
        out["additional_value"] = additional
    return out


BIG = 10**400

FLAGS = {
    "equals": (_dashboard("equals", "18"), {"a": BIG}),
    "not_equals": (_dashboard("not_equals", "1234"), {"a": BIG}),
    "in": (_dashboard("in", ["18", "pro"]), {"a": BIG}),
    "not_in": (_dashboard("not_in", ["18", "pro"]), {"a": BIG}),
    "greater_than": (_dashboard("greater_than", "18"), {"a": BIG}),
    "greater_than_or_equal": (_dashboard("greater_than_or_equal", "18"), {"a": BIG}),
    "less_than": (_dashboard("less_than", "18"), {"a": BIG}),
    "less_than_or_equal": (_dashboard("less_than_or_equal", "18"), {"a": BIG}),
    "before": (
        _native({"operator": "and", "conditions": [_cond("before", "2026-01-01")]}),
        {"a": BIG},
    ),
    "after": (
        _native({"operator": "and", "conditions": [_cond("after", "2026-01-01")]}),
        {"a": math.inf},
    ),
    "between": (
        _native({"operator": "and", "conditions": [_cond("between", "1", "100")]}),
        {"a": BIG},
    ),
    # NOT around an out-of-range comparison: abandoned, never a match.
    "not-group": (
        _native({"operator": "not", "conditions": [_cond("gt", 18)]}),
        {"a": BIG},
    ),
    # A sibling that already decided the OR group does not rescue the ruleset.
    "or-with-true-sibling": (
        _native(
            {
                "operator": "or",
                "conditions": [{"attribute": "b", "operator": "eq", "value": "yes"}]
                + [_cond("gt", 5)],
            }
        ),
        {"b": "yes", "a": BIG},
    ),
}


def _memberships(db, user_id, context, segment_ids):
    asked = frozenset(segment_ids)
    return SegmentMemberships(
        members=frozenset(), evaluated=asked, unavailable=frozenset()
    )


@pytest.mark.parametrize("name", list(FLAGS))
def test_the_flag_answers_off_with_reason_error(name):
    rules, context = FLAGS[name]
    flag = FeatureFlag(
        key=f"range-{name}",
        name=name,
        status=FeatureFlagStatus.ACTIVE,
        rollout_percentage=100,
        targeting_rules=rules,
    )
    with (
        mock.patch.object(MetricsService, "record_flag_evaluation") as recorded,
        mock.patch.object(MetricsService, "log_error") as logged,
        mock.patch.object(
            feature_flag_service, "resolve_segment_memberships", _memberships
        ),
    ):
        answer = FeatureFlagService(db=None).evaluate_flag_detailed(
            flag, "user-1", context
        )

    assert answer == {"enabled": False, "reason": "error", "rule_id": None}
    assert logged.call_count == 0
    assert recorded.call_count == 1


def test_the_in_range_control_still_matches():
    flag = FeatureFlag(
        key="range-control",
        name="control",
        status=FeatureFlagStatus.ACTIVE,
        rollout_percentage=0,
        targeting_rules=_dashboard("greater_than", "18"),
    )
    with (
        mock.patch.object(MetricsService, "record_flag_evaluation"),
        mock.patch.object(MetricsService, "log_error") as logged,
    ):
        answer = FeatureFlagService(db=None).evaluate_flag_detailed(
            flag, "user-1", {"a": 30}
        )
    assert answer["enabled"] is True and answer["reason"] == "targeting_rule"
    assert logged.call_count == 0


def _short(value) -> str:
    text = repr(value)
    return text if len(text) <= 24 else f"{text[:12]}...({len(text)} chars)"
