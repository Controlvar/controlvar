"""The reference's ``time_window`` example is a window the engine reads (#1145).

``docs/Enhanced_Rules_Engine_Reference.md`` used to give
``{"start": <ISO>, "end": <ISO>}``, two keys the engine does not read, so on a
flag that window was open at every moment. These tests read the example out of
the page and run it through both evaluators, flag targeting
(``rules_engine.evaluate_rule``) and experiment targeting
(``RulesEvaluationService``), at fixed moments inside and outside the window.
The moment is the attribute's value, so no clock is read.

They also pin the two other claims the section makes about behaviour: a window
with ISO datetimes in ``start``/``end`` is refused (the flag answers off with
reason ``error``, and no user is eligible for the experiment), and ``after``
with ``before`` in an ``AND`` group is a window between two moments.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Tuple
from unittest import mock
from unittest.mock import MagicMock

import pytest

from backend.app.core import rules_engine
from backend.app.core.targeting_adapter import convert_dashboard_condition
from backend.app.models.feature_flag import FeatureFlagStatus
from backend.app.schemas.targeting_rule import (
    Condition,
    LogicalOperator,
    OperatorType,
    RuleGroup,
    TargetingRule,
    TargetingRules,
)
from backend.app.services import feature_flag_service
from backend.app.services.feature_flag_service import FeatureFlagService
from backend.app.services.rules_evaluation_service import RulesEvaluationService

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
PAGE = REPO_ROOT / "docs" / "Enhanced_Rules_Engine_Reference.md"
HEADING = "### TIME_WINDOW (`time_window`)"

#: Inside the example's window: 09:00-17:00, Monday to Friday, 1-31 January 2024.
INSIDE = [
    "2024-01-10T12:00:00",  # a Wednesday
    "2024-01-01T09:00:00",  # the first minute (a Monday)
    "2024-01-31T17:00:59",  # the end minute is included (a Wednesday)
]
OUTSIDE = [
    "2024-01-10T08:59:00",  # before 09:00
    "2024-01-10T17:01:00",  # after 17:00
    "2024-01-13T12:00:00",  # a Saturday
    "2024-02-07T12:00:00",  # a Wednesday in February
    "2023-12-27T12:00:00",  # a Wednesday in December
]


def _section() -> str:
    text = PAGE.read_text(encoding="utf-8")
    start = text.index(HEADING)
    end = text.index("\n### ", start)
    return text[start:end]


def _prose() -> str:
    """The section with its line breaks folded, so rewrapping it changes nothing."""
    return " ".join(_section().split())


def _example() -> Dict[str, Any]:
    """The example's native condition, the section's ```json block, as the
    ``Condition`` keyword arguments it gives."""
    code = _section().split("```json\n", 1)[1].split("```", 1)[0]
    kwargs: Dict[str, Any] = json.loads(code)
    assert set(kwargs) <= set(Condition.model_fields), code
    kwargs["operator"] = OperatorType(kwargs["operator"])
    return kwargs


def _answers(
    condition: Condition,
    moment: str,
    group_operator: LogicalOperator = LogicalOperator.AND,
) -> Tuple[bool, bool]:
    """(flag targeting, experiment targeting): does a rule holding *condition*
    match a user whose attribute is *moment*?"""
    rule = TargetingRule(
        id="doc", rule=RuleGroup(operator=group_operator, conditions=[condition])
    )
    context = {condition.attribute: moment}
    flag = rules_engine.evaluate_rule(rule, context)
    experiment = RulesEvaluationService()._evaluate_rule_enhanced(rule, context)
    return flag, experiment


def test_the_example_is_a_native_time_window_condition():
    example = _example()
    assert example["operator"] is OperatorType.TIME_WINDOW
    # The schema accepts it, and it has keys the engine reads.
    Condition(**example)
    assert set(example["value"]) & set(rules_engine.TIME_WINDOW_KEYS)


@pytest.mark.parametrize("moment", INSIDE)
def test_the_example_matches_inside_its_window(moment):
    assert _answers(Condition(**_example()), moment) == (True, True)


@pytest.mark.parametrize("moment", OUTSIDE)
def test_the_example_does_not_match_outside_its_window(moment):
    assert _answers(Condition(**_example()), moment) == (False, False)


def _refused(condition: Condition, moment: str, group_operator: LogicalOperator):
    """:func:`_answer` for a ruleset whose one rule holds *condition*."""
    rules = TargetingRules(
        rules=[
            TargetingRule(
                id="doc",
                rule=RuleGroup(operator=group_operator, conditions=[condition]),
            )
        ]
    )
    return _answer(rules, {condition.attribute: moment})


def _answer(rules: TargetingRules, context: Dict[str, Any]):
    """(the flag's answer, the id of the experiment's matched rule) through the
    entry points flags and experiments use; the flag is at 100%."""
    flag = SimpleNamespace(
        id="00000000-0000-0000-0000-000000001145",
        key="doc",
        status=FeatureFlagStatus.ACTIVE,
        rollout_percentage=100,
        targeting_rules=rules,
    )
    with mock.patch.object(feature_flag_service, "MetricsService"):
        answer = FeatureFlagService(db=MagicMock()).evaluate_flag_detailed(
            flag, "user-1", context
        )
    matched, _ = RulesEvaluationService().evaluate_rules_with_validation(rules, context)
    return answer, (matched.id if matched is not None else None)


def test_iso_start_and_end_are_refused_in_either_shape():
    """The example this page used to give, in the native and the dashboard
    shape: for a user whose evaluation reaches it, the flag answers off with
    reason ``error`` and the experiment does not enrol them."""
    prose = _prose()
    assert (
        "For a user whose evaluation reaches the window, the evaluation is "
        "abandoned: the flag answers off for that user, with reason `error`, "
        "the experiment does not enrol them" in prose
    )
    assert "So a window with ISO datetimes in `start` and `end` is refused" in prose
    window = {"start": "2024-01-01T09:00:00", "end": "2024-01-31T17:00:00"}
    native = Condition(
        attribute="current_time", operator=OperatorType.TIME_WINDOW, value=window
    )
    dashboard = convert_dashboard_condition(
        {"attribute": "current_time", "operator": "time_window", "value": window}
    )
    off = {"enabled": False, "reason": "error", "rule_id": None}
    for condition in (native, dashboard):
        for group_operator in (LogicalOperator.AND, LogicalOperator.NOT):
            for moment in INSIDE + OUTSIDE:
                assert _refused(condition, moment, group_operator) == (off, None), (
                    condition,
                    group_operator,
                    moment,
                )


def test_a_user_decided_before_the_window_never_reaches_it():
    """Rule 1 (``country eq US``) decides a US user before rule 2's window is
    read; a FR user reaches it. A window in ``default_rule`` is never read."""
    assert (
        "A user decided by an earlier rule never reaches it, and a window in "
        "`default_rule` is never read." in _prose()
    )
    window = Condition(
        attribute="current_time",
        operator=OperatorType.TIME_WINDOW,
        value={"start": "2024-01-01T09:00:00", "end": "2024-01-31T17:00:00"},
    )
    us = Condition(attribute="country", operator=OperatorType.EQUALS, value="US")
    rules = TargetingRules(
        rules=[
            TargetingRule(id="us", priority=1, rule=RuleGroup(conditions=[us])),
            TargetingRule(id="window", priority=2, rule=RuleGroup(conditions=[window])),
        ]
    )
    moment = "2024-01-10T12:00:00"
    on = {"enabled": True, "reason": "targeting_rule", "rule_id": "us"}
    off = {"enabled": False, "reason": "error", "rule_id": None}
    assert _answer(rules, {"country": "US", "current_time": moment}) == (on, "us")
    assert _answer(rules, {"country": "FR", "current_time": moment}) == (off, None)

    in_default = TargetingRules(
        rules=[TargetingRule(id="us", rule=RuleGroup(conditions=[us]))],
        default_rule=TargetingRule(id="default", rule=RuleGroup(conditions=[window])),
    )
    default = {"enabled": True, "reason": "targeting_rule", "rule_id": "default"}
    assert _answer(in_default, {"country": "FR", "current_time": moment}) == (
        default,
        "default",
    )


def test_after_and_before_make_a_window_between_two_moments():
    assert "use `after` and `before` (below) in an `AND` group" in _prose()
    group = RuleGroup(
        operator=LogicalOperator.AND,
        conditions=[
            Condition(
                attribute="current_time",
                operator=OperatorType.AFTER,
                value="2024-01-01T09:00:00",
            ),
            Condition(
                attribute="current_time",
                operator=OperatorType.BEFORE,
                value="2024-01-31T17:00:00",
            ),
        ],
    )
    service = RulesEvaluationService()
    for moment, inside in (
        ("2024-01-13T12:00:00", True),  # a Saturday: no day rule here
        ("2024-01-10T20:00:00", True),  # an evening: no time-of-day rule here
        ("2024-01-01T08:00:00", False),
        ("2024-02-01T12:00:00", False),
    ):
        context = {"current_time": moment}
        assert rules_engine.evaluate_rule_group(group, context) is inside, moment
        assert service._evaluate_rule_group_enhanced(group, context) is inside, moment
