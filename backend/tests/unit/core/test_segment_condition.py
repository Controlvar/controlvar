"""Segment conditions in both evaluators (#440).

``in_segment`` / ``not_in_segment`` are answered by
``rules_engine.segment_condition_holds`` from the resolved membership under
``SEGMENT_MEMBERSHIP_KEY``, before any attribute lookup, in the flag engine
(``evaluate_condition``) and in experiment targeting
(``RulesEvaluationService._evaluate_condition_enhanced``):

* membership counts only as a ``frozenset``; absent, or any other type,
  raises ``SegmentMembershipUnavailable`` -- never "not a member", which
  would make ``not_in_segment`` (or a ``NOT`` group) match;
* the condition's attribute is never read, and ``apply_operator`` is never
  reached;
* a customer attribute called ``segment`` with any other operator is an
  ordinary attribute, and stays required on experiments.
"""

from __future__ import annotations

import pytest

from backend.app.core import rules_engine
from backend.app.core.rules_engine import (
    SEGMENT_MEMBERSHIP_KEY,
    SegmentMembershipUnavailable,
    evaluate_condition,
    segment_condition_holds,
)
from backend.app.core.targeting_adapter import (
    match_targeting_rule,
    validate_experiment_targeting,
    validate_flag_targeting,
)
from backend.app.schemas.targeting_rule import (
    Condition,
    LogicalOperator,
    OperatorType,
    RuleGroup,
    TargetingRule,
    TargetingRules,
)
from backend.app.services.rules_evaluation_service import RulesEvaluationService

pytestmark = pytest.mark.unit

SEG = "0b8a3e1c-4a3e-4c55-9a40-000000000440"
OTHER = "0b8a3e1c-4a3e-4c55-9a40-000000000441"


def _cond(op: OperatorType, value=SEG, attribute="segment") -> Condition:
    return Condition(attribute=attribute, operator=op, value=value)


def _rules(*conditions: Condition, operator=LogicalOperator.AND) -> TargetingRules:
    return TargetingRules(
        rules=[
            TargetingRule(
                id="r",
                rule=RuleGroup(operator=operator, conditions=list(conditions)),
                rollout_percentage=100,
            )
        ]
    )


def _flag_matches(rules: TargetingRules, ctx) -> bool:
    return match_targeting_rule(rules, ctx) is not None


def _experiment_matches(rules: TargetingRules, ctx) -> bool:
    """The experiment evaluator, as assignment runs it."""
    service = RulesEvaluationService()
    rule, _metrics = service.evaluate_rules_with_validation(
        rules, ctx, track_metrics=False
    )
    return rule is not None


EVALUATORS = [
    pytest.param(_flag_matches, id="flag"),
    pytest.param(_experiment_matches, id="experiment"),
]


# ---------------------------------------------------------------------------
# C5: frozenset only; absent raises
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "op, members, expected",
    [
        (OperatorType.IN_SEGMENT, frozenset({SEG}), True),
        (OperatorType.IN_SEGMENT, frozenset({OTHER}), False),
        (OperatorType.IN_SEGMENT, frozenset(), False),
        (OperatorType.NOT_IN_SEGMENT, frozenset({SEG}), False),
        (OperatorType.NOT_IN_SEGMENT, frozenset(), True),
    ],
)
def test_membership_answers_from_a_frozenset(op, members, expected):
    assert segment_condition_holds(_cond(op), {SEGMENT_MEMBERSHIP_KEY: members}) is (
        expected
    )


@pytest.mark.parametrize(
    "members",
    [[SEG], (SEG,), {SEG}, SEG, {SEG: True}, None],
    ids=["list", "tuple", "set", "text", "dict", "null"],
)
@pytest.mark.parametrize("op", [OperatorType.IN_SEGMENT, OperatorType.NOT_IN_SEGMENT])
def test_any_other_type_raises(op, members):
    with pytest.raises(SegmentMembershipUnavailable) as raised:
        segment_condition_holds(_cond(op), {SEGMENT_MEMBERSHIP_KEY: members})
    assert raised.value.segment_ids == (SEG,)


@pytest.mark.parametrize("op", [OperatorType.IN_SEGMENT, OperatorType.NOT_IN_SEGMENT])
def test_absent_membership_raises(op):
    with pytest.raises(SegmentMembershipUnavailable):
        segment_condition_holds(_cond(op), {"segment": SEG, "country": "US"})


@pytest.mark.parametrize("evaluate", EVALUATORS)
@pytest.mark.parametrize("op", [OperatorType.IN_SEGMENT, OperatorType.NOT_IN_SEGMENT])
@pytest.mark.parametrize(
    "operator", [LogicalOperator.AND, LogicalOperator.OR, LogicalOperator.NOT]
)
def test_both_evaluators_raise_when_absent_even_under_not(evaluate, op, operator):
    rules = _rules(_cond(op), operator=operator)
    with pytest.raises(SegmentMembershipUnavailable):
        evaluate(rules, {"country": "US", SEGMENT_MEMBERSHIP_KEY: [SEG]})
    with pytest.raises(SegmentMembershipUnavailable):
        evaluate(rules, {"country": "US"})


def test_a_value_that_is_not_text_raises():
    condition = Condition.model_construct(
        attribute="segment", operator=OperatorType.IN_SEGMENT, value=[SEG]
    )
    with pytest.raises(SegmentMembershipUnavailable) as raised:
        segment_condition_holds(condition, {SEGMENT_MEMBERSHIP_KEY: frozenset({SEG})})
    assert raised.value.segment_ids == ("(not a segment id)",)


def test_a_non_segment_condition_is_not_answered_here():
    condition = _cond(OperatorType.EQUALS, value="x")
    assert segment_condition_holds(condition, {}) is None


# ---------------------------------------------------------------------------
# C6: before the attribute lookup; apply_operator never reached
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("evaluate", EVALUATORS)
def test_holds_before_lookup(evaluate):
    """A member is matched with no ``segment`` key in the context at all."""
    ctx = {"country": "US", SEGMENT_MEMBERSHIP_KEY: frozenset({SEG})}
    assert evaluate(_rules(_cond(OperatorType.IN_SEGMENT)), ctx) is True
    assert evaluate(_rules(_cond(OperatorType.NOT_IN_SEGMENT)), ctx) is False
    empty = {"country": "US", SEGMENT_MEMBERSHIP_KEY: frozenset()}
    assert evaluate(_rules(_cond(OperatorType.NOT_IN_SEGMENT)), empty) is True


@pytest.mark.parametrize("evaluate", EVALUATORS)
def test_a_context_attribute_named_segment_does_not_decide(evaluate):
    ctx = {"segment": SEG, SEGMENT_MEMBERSHIP_KEY: frozenset()}
    assert evaluate(_rules(_cond(OperatorType.IN_SEGMENT)), ctx) is False


def test_apply_operator_unreachable(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("apply_operator reached for a segment condition")

    monkeypatch.setattr(rules_engine, "apply_operator", refuse)
    ctx = {SEGMENT_MEMBERSHIP_KEY: frozenset({SEG})}
    assert evaluate_condition(_cond(OperatorType.IN_SEGMENT), ctx) is True
    service = RulesEvaluationService()
    monkeypatch.setattr(service, "_apply_operator_enhanced", refuse)
    assert service._evaluate_condition_enhanced(_cond(OperatorType.IN_SEGMENT), ctx)


@pytest.mark.parametrize("op", [OperatorType.IN_SEGMENT, OperatorType.NOT_IN_SEGMENT])
def test_apply_operator_fails_closed_if_ever_reached(op):
    with pytest.raises(SegmentMembershipUnavailable):
        rules_engine.apply_operator(op, SEG, SEG)


def test_segment_attribute_with_equals():
    """``segment equals enterprise`` compares the context attribute, as before."""
    rules = _rules(_cond(OperatorType.EQUALS, value="enterprise"))
    assert _flag_matches(rules, {"segment": "enterprise"}) is True
    assert _flag_matches(rules, {"segment": "smb"}) is False
    assert _experiment_matches(rules, {"segment": "enterprise"}) is True
    # A user without the attribute "segment" fails this condition (it is a
    # context attribute, not a segment condition), as on flags (#822).
    assert _experiment_matches(rules, {"country": "US"}) is False


@pytest.mark.parametrize("evaluate", EVALUATORS)
def test_required_attributes_skip_only_segment_conditions(evaluate):
    """A segment condition requires nothing from the context; the condition
    beside it still requires its own attribute."""
    rules = _rules(
        _cond(OperatorType.IN_SEGMENT),
        _cond(OperatorType.EQUALS, value="US", attribute="country"),
    )
    member = {SEGMENT_MEMBERSHIP_KEY: frozenset({SEG})}
    assert evaluate(rules, {"country": "US", **member}) is True
    assert evaluate(rules, member) is False
    assert evaluate(_rules(_cond(OperatorType.IN_SEGMENT)), member) is True


# ---------------------------------------------------------------------------
# Through the validators: what both write paths return evaluates the same way
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "validate", [validate_flag_targeting, validate_experiment_targeting]
)
def test_dashboard_segment_rules_evaluate_on_both_paths(validate):
    rules = validate(
        {
            "groups": [
                {
                    "conditions": [
                        {"attribute": "segment", "operator": "in_segment", "value": SEG}
                    ]
                }
            ]
        }
    )
    member = {SEGMENT_MEMBERSHIP_KEY: frozenset({SEG})}
    other = {SEGMENT_MEMBERSHIP_KEY: frozenset({OTHER})}
    assert _flag_matches(rules, member) and _experiment_matches(rules, member)
    assert not _flag_matches(rules, other) and not _experiment_matches(rules, other)
