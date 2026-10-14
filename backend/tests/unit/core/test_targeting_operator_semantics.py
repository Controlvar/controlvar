"""What the targeting operators answer, on flags and on experiments alike (#1142).

``docs/Enhanced_Rules_Engine_Reference.md`` gives each operator's meaning.
These tests hold the meanings no other test holds, each through both
evaluators: flag targeting (``targeting_adapter.match_targeting_rule``, what
``FeatureFlagService`` calls) and experiment targeting
(``RulesEvaluationService.evaluate_rules_with_validation``, what assignment
calls). No database, no clock: every moment is the attribute's value.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, Optional, Tuple
from unittest.mock import MagicMock

import pytest

from backend.app.core.pattern_match import PatternUnevaluable
from backend.app.core.rules_engine import TimeWindowUnreadable, evaluate_rule_group
from backend.app.core.targeting_adapter import (
    match_targeting_rule,
    normalise_targeting_rules,
    validate_experiment_targeting,
    validate_flag_targeting,
)
from backend.app.schemas.targeting_rule import (
    AttributeType,
    Condition,
    LogicalOperator,
    OperatorType,
    RuleGroup,
    TargetingRule,
    TargetingRules,
)
from backend.app.services.assignment_service import AssignmentService
from backend.app.services.rules_evaluation_service import RulesEvaluationService

pytestmark = pytest.mark.unit


def _rules(*conditions: Condition, operator=LogicalOperator.AND) -> TargetingRules:
    return TargetingRules(
        rules=[
            TargetingRule(
                id="r", rule=RuleGroup(operator=operator, conditions=list(conditions))
            )
        ]
    )


def _answers(rules: TargetingRules, context: Dict[str, Any]) -> Tuple[bool, bool]:
    """(the flag's rule matched, the experiment's rule matched)."""
    flag = match_targeting_rule(rules, context) is not None
    matched, _ = RulesEvaluationService().evaluate_rules_with_validation(rules, context)
    return flag, matched is not None


def _condition(
    operator: OperatorType, value: Any, additional: Optional[Any] = None
) -> Condition:
    return Condition(
        attribute="a", operator=operator, value=value, additional_value=additional
    )


# ---------------------------------------------------------------------------
# Comparisons
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, inside",
    [(18, True), (65, True), (40, True), (17, False), (66, False), ("18", True)],
)
def test_between_includes_both_bounds_for_numbers(value, inside):
    rules = _rules(_condition(OperatorType.BETWEEN, 18, 65))
    assert _answers(rules, {"a": value}) == (inside, inside)


@pytest.mark.parametrize(
    "moment, inside",
    [
        ("2024-01-01T00:00:00", True),
        ("2024-12-31T00:00:00", True),
        ("2024-06-01T12:00:00", True),
        ("2023-12-31T23:59:59", False),
        ("2024-12-31T00:00:01", False),
    ],
)
def test_between_includes_both_bounds_for_dates(moment, inside):
    rules = _rules(
        _condition(OperatorType.BETWEEN, "2024-01-01T00:00:00", "2024-12-31T00:00:00")
    )
    assert _answers(rules, {"a": moment}) == (inside, inside)


@pytest.mark.parametrize(
    "operator, moment, holds",
    [
        (OperatorType.BEFORE, "2023-12-31T23:59:59", True),
        (OperatorType.BEFORE, "2024-01-01T00:00:00", False),
        (OperatorType.AFTER, "2024-01-01T00:00:01", True),
        (OperatorType.AFTER, "2024-01-01T00:00:00", False),
    ],
)
def test_before_and_after_are_strict(operator, moment, holds):
    rules = _rules(_condition(operator, "2024-01-01T00:00:00"))
    assert _answers(rules, {"a": moment}) == (holds, holds)


def test_before_and_after_read_a_unix_timestamp_in_the_attribute():
    moment = 1_700_000_000  # 2023-11-14, before 2024 and after 2020
    before = _rules(_condition(OperatorType.BEFORE, "2024-01-01T00:00:00"))
    after = _rules(_condition(OperatorType.AFTER, "2020-01-01T00:00:00"))
    assert _answers(before, {"a": moment}) == (True, True)
    assert _answers(after, {"a": moment}) == (True, True)


@pytest.mark.parametrize(
    "version, holds",
    [("2.0.0", True), ("2.0.0+build.7", True), ("2.0.1", False), ("1.9.9", False)],
)
def test_a_semantic_version_with_no_comparison_is_eq(version, holds):
    rules = _rules(_condition(OperatorType.SEMANTIC_VERSION, "2.0.0"))
    assert _answers(rules, {"a": version}) == (holds, holds)


@pytest.mark.parametrize(
    "value, holds",
    [
        (None, True),
        ("", True),
        ("   ", True),
        ([], True),
        ({}, True),
        (0, False),
        (False, False),
        ("x", False),
        (["x"], False),
    ],
)
def test_is_null_holds_for_an_empty_value_and_is_not_null_for_any_other(value, holds):
    is_null = _rules(_condition(OperatorType.IS_NULL, None))
    is_not_null = _rules(_condition(OperatorType.IS_NOT_NULL, None))
    assert _answers(is_null, {"a": value}) == (holds, holds)
    assert _answers(is_not_null, {"a": value}) == (not holds, not holds)


@pytest.mark.parametrize(
    "condition",
    [
        _condition(OperatorType.EQUALS, "x"),
        _condition(OperatorType.NOT_EQUALS, "x"),
        _condition(OperatorType.GREATER_THAN, 1),
        _condition(OperatorType.IN, ["x"]),
        _condition(OperatorType.NOT_IN, ["x"]),
        _condition(OperatorType.CONTAINS, "x"),
        _condition(OperatorType.NOT_CONTAINS, "x"),
        _condition(OperatorType.STARTS_WITH, "x"),
        _condition(OperatorType.CONTAINS_ALL, ["x"]),
        _condition(OperatorType.CONTAINS_ANY, ["x"]),
        _condition(OperatorType.MATCH_REGEX, "x"),
        _condition(OperatorType.IS_NOT_NULL, None),
        _condition(OperatorType.SEMANTIC_VERSION, "1.0.0", "gte"),
        _condition(OperatorType.GEO_DISTANCE, [1.0, 2.0], 10),
        _condition(
            OperatorType.TIME_WINDOW, {"start": "00:00", "end": "23:59", "days": [0]}
        ),
        _condition(OperatorType.BETWEEN, 1, 2),
        _condition(OperatorType.BEFORE, "2024-01-01T00:00:00"),
        _condition(OperatorType.AFTER, "2024-01-01T00:00:00"),
    ],
    ids=lambda c: c.operator.value,
)
def test_a_condition_on_a_missing_attribute_does_not_hold(condition):
    assert _answers(_rules(condition), {"b": "x"}) == (False, False)
    assert _answers(_rules(_condition(OperatorType.IS_NULL, None)), {"b": "x"}) == (
        True,
        True,
    )


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "context, holds",
    [
        ({"a": "x", "b": "y"}, False),
        ({"a": "x", "b": "z"}, True),
        ({"a": "w", "b": "z"}, True),
    ],
)
def test_not_holds_when_not_all_of_its_conditions_hold(context, holds):
    group = RuleGroup(
        operator=LogicalOperator.NOT,
        conditions=[
            Condition(attribute="a", operator=OperatorType.EQUALS, value="x"),
            Condition(attribute="b", operator=OperatorType.EQUALS, value="y"),
        ],
    )
    service = RulesEvaluationService()
    assert evaluate_rule_group(group, context) is holds
    assert service._evaluate_rule_group_enhanced(group, context) is holds


# ---------------------------------------------------------------------------
# A ruleset that cannot be evaluated
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "condition, context",
    [
        (_condition(OperatorType.GREATER_THAN, 18), {"a": 10**400}),
        (_condition(OperatorType.BEFORE, "2026-01-01T00:00:00"), {"a": 10**18}),
    ],
    ids=["number", "timestamp"],
)
@pytest.mark.parametrize("group", [LogicalOperator.AND, LogicalOperator.NOT])
def test_an_out_of_range_comparison_makes_the_user_ineligible_for_an_experiment(
    condition, context, group
):
    rules = _rules(condition, operator=group)
    matched, metrics = RulesEvaluationService().evaluate_rules_with_validation(
        rules, context
    )
    assert matched is None
    assert metrics is not None and metrics.rule_id == "error"
    with pytest.raises(PatternUnevaluable):
        match_targeting_rule(rules, context)
    outcome = AssignmentService(MagicMock())._evaluate_experiment_targeting(
        SimpleNamespace(targeting_rules=rules.model_dump(mode="json")),
        context,
    )
    assert outcome["eligible"] is False


UNREADABLE = Condition(
    attribute="now",
    operator=OperatorType.TIME_WINDOW,
    value={"start": "2024-01-01T09:00:00", "end": "2024-01-31T17:00:00"},
)
US = Condition(attribute="country", operator=OperatorType.EQUALS, value="US")


def test_a_user_decided_by_an_earlier_rule_never_reaches_an_unreadable_window():
    rules = TargetingRules(
        rules=[
            TargetingRule(id="us", priority=1, rule=RuleGroup(conditions=[US])),
            TargetingRule(
                id="window", priority=2, rule=RuleGroup(conditions=[UNREADABLE])
            ),
        ]
    )
    moment = "2024-01-10T12:00:00"
    assert match_targeting_rule(rules, {"country": "US", "now": moment}).id == "us"
    with pytest.raises(TimeWindowUnreadable):
        match_targeting_rule(rules, {"country": "FR", "now": moment})


def test_a_window_in_default_rule_is_never_read():
    rules = TargetingRules(
        rules=[TargetingRule(id="us", rule=RuleGroup(conditions=[US]))],
        default_rule=TargetingRule(
            id="default", rule=RuleGroup(conditions=[UNREADABLE])
        ),
    )
    context = {"country": "FR", "now": "2024-01-10T12:00:00"}
    assert match_targeting_rule(rules, context).id == "default"
    matched, _ = RulesEvaluationService().evaluate_rules_with_validation(rules, context)
    assert matched.id == "default"


@pytest.mark.parametrize(
    "window",
    [
        {"start": "x", "end": "y", "start_time": "09:00"},
        {"start": "x", "end": "y", "end_time": "17:00"},
        {"start": "x", "end": "y", "start_date": "2024-01-01"},
        {"start": "x", "end": "y", "end_date": "2024-01-31"},
    ],
)
def test_a_window_with_one_half_of_a_pair_cannot_be_read(window):
    rules = _rules(
        Condition(attribute="now", operator=OperatorType.TIME_WINDOW, value=window)
    )
    with pytest.raises(TimeWindowUnreadable):
        match_targeting_rule(rules, {"now": "2024-01-10T12:00:00"})
    matched, _ = RulesEvaluationService().evaluate_rules_with_validation(
        rules, {"now": "2024-01-10T12:00:00"}
    )
    assert matched is None


@pytest.mark.parametrize(
    "moment, inside",
    [("2024-01-10T12:00:00+05:00", True), ("2024-01-10T07:00:00Z", False)],
)
def test_a_window_without_a_timezone_compares_the_moments_own_clock_time(
    moment, inside
):
    window = {
        "start": "11:00",
        "end": "13:00",
        "start_time": "11:00",
        "end_time": "13:00",
    }
    rules = _rules(
        Condition(attribute="now", operator=OperatorType.TIME_WINDOW, value=window)
    )
    assert _answers(rules, {"now": moment}) == (inside, inside)


# ---------------------------------------------------------------------------
# array_length (#1159)
# ---------------------------------------------------------------------------

BETWEEN_1_AND_2 = {"min": 1, "max": 2}


@pytest.mark.regression
@pytest.mark.parametrize(
    "value, comparison, attribute, holds",
    [
        (2, None, ["x", "y"], True),
        (2, None, ["x"], False),
        (2, "eq", "ab", True),
        (1, "eq", {"k": 1}, True),
        (2, "gt", [1, 2, 3], True),
        (2, "gt", [1, 2], False),
        (2, "gte", [1, 2], True),
        (2, "gte", [1], False),
        (2, "lt", [1], True),
        (2, "lt", [1, 2], False),
        (2, "lte", [1, 2], True),
        (2, "lte", [1, 2, 3], False),
        (BETWEEN_1_AND_2, "between", [1], True),
        (BETWEEN_1_AND_2, "between", [1, 2], True),
        (BETWEEN_1_AND_2, "between", [], False),
        (BETWEEN_1_AND_2, "between", [1, 2, 3], False),
        (BETWEEN_1_AND_2, "between", "abc", False),
        (0, None, 5, False),
        (0, None, None, False),
        (1, None, True, False),
    ],
)
def test_array_length_compares_the_length_as_additional_value_says(
    value, comparison, attribute, holds
):
    """The same answer from both evaluators: an experiment used to read only an
    exact length of a list, whatever ``additional_value`` said."""
    rules = _rules(_condition(OperatorType.ARRAY_LENGTH, value, comparison))
    raw = rules.model_dump(mode="json", exclude_none=True)
    assert validate_flag_targeting(raw) is not None
    assert validate_experiment_targeting(raw) is not None
    assert _answers(rules, {"a": attribute}) == (holds, holds)


# ---------------------------------------------------------------------------
# The operators the reference leaves out
# ---------------------------------------------------------------------------

LEFT_OUT = {
    "json_path": (_condition(OperatorType.JSON_PATH, "$.b", 1), {"a": {"b": 1}}),
    "percentage_bucket": (
        Condition(
            attribute="user_id", operator=OperatorType.PERCENTAGE_BUCKET, value=100
        ),
        {"user_id": "u1"},
    ),
}


@pytest.mark.parametrize("name", sorted(LEFT_OUT))
def test_the_native_shape_accepts_json_path_and_percentage_bucket(name):
    condition, _ = LEFT_OUT[name]
    raw = _rules(condition).model_dump(mode="json", exclude_none=True)
    assert validate_flag_targeting(raw) is not None
    assert validate_experiment_targeting(raw) is not None


def test_flags_and_experiments_answer_json_path_and_percentage_bucket_differently():
    differ = {
        name
        for name, (condition, context) in LEFT_OUT.items()
        if len(set(_answers(_rules(condition), context))) == 2
    }
    assert differ == {"json_path", "percentage_bucket"}


# ---------------------------------------------------------------------------
# attribute_type (#1159)
# ---------------------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize("attribute_type", list(AttributeType), ids=lambda t: t.value)
def test_attribute_type_is_read_by_neither_evaluator(attribute_type):
    """Saved, and then ignored: the condition is answered from the value. An
    experiment used to refuse the whole ruleset when the value did not have
    the condition's type (here, text under every type but ``string`` and
    ``json``)."""
    typed = Condition(
        attribute="a",
        operator=OperatorType.EQUALS,
        value="x",
        attribute_type=attribute_type,
    )
    raw = _rules(typed).model_dump(mode="json", exclude_none=True)
    assert raw["rules"][0]["rule"]["conditions"][0]["attribute_type"] == (
        attribute_type.value
    )
    assert validate_flag_targeting(raw) is not None
    assert validate_experiment_targeting(raw) is not None
    for context, holds in (({"a": "x"}, True), ({"a": "y"}, False), ({}, False)):
        assert _answers(_rules(typed), context) == (holds, holds), context


# ---------------------------------------------------------------------------
# The reference's usage examples, as the builder saves them
# ---------------------------------------------------------------------------


def _dashboard(*groups, operator: str = "AND") -> dict:
    return {
        "logical_operator": operator,
        "groups": [{"logical_operator": "AND", "conditions": list(g)} for g in groups],
    }


def _cond(attribute: str, operator: str, value: Any) -> dict:
    return {"attribute": attribute, "operator": operator, "value": value}


def _dashboard_answers(raw: dict, context: Dict[str, Any]) -> Tuple[bool, bool]:
    return _answers(normalise_targeting_rules(validate_flag_targeting(raw)), context)


IOS = _dashboard(
    [
        _cond("platform", "equals", "iOS"),
        _cond("os_version", "semver_gte", "15"),
        _cond("device_type", "in", "iPhone, iPad"),
    ]
)


@pytest.mark.parametrize(
    "context, holds",
    [
        ({"platform": "iOS", "os_version": "16.2.0", "device_type": "iPhone"}, True),
        ({"platform": "iOS", "os_version": "15.0.0", "device_type": "iPad"}, True),
        ({"platform": "iOS", "os_version": "14.9.0", "device_type": "iPhone"}, False),
        ({"platform": "iOS", "os_version": "16.2.0", "device_type": "Mac"}, False),
    ],
)
def test_the_ios_example(context, holds):
    assert _dashboard_answers(IOS, context) == (holds, holds)


@pytest.mark.parametrize("radius, holds", [(15, True), (10, False)])
def test_the_san_francisco_example(radius, holds):
    raw = _dashboard(
        [
            _cond(
                "location",
                "geo_within_radius",
                {"lat": 37.7749, "lon": -122.4194, "radius": radius},
            )
        ]
    )
    oakland = {"location": [37.8044, -122.2712]}  # 13.4 km from the rule's point
    assert _dashboard_answers(raw, oakland) == (holds, holds)


@pytest.mark.parametrize(
    "context, holds",
    [
        ({"country": "US", "age": 25}, True),
        ({"country": "US", "age": 18}, False),
        ({"country": "CA", "verified": True}, True),
        ({"country": "CA", "verified": False}, False),
        ({"country": "GB", "age": 30, "verified": True}, False),
    ],
)
def test_the_us_or_canada_example(context, holds):
    raw = _dashboard(
        [_cond("country", "equals", "US"), _cond("age", "greater_than", "18")],
        [_cond("country", "equals", "CA"), _cond("verified", "equals", "true")],
        operator="OR",
    )
    assert _dashboard_answers(raw, context) == (holds, holds)
