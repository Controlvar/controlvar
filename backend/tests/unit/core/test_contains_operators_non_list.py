"""``contains_all`` / ``contains_any`` on an attribute that is not a list (#270).

A string attribute is matched by substring, as it always was, but only a
string item can be a substring of it. Before #270 a non-string item was tested
with ``item in "some string"``, which raises ``TypeError``; on the flag path
that turned every evaluation into ``reason: "error"`` with an error logged,
instead of the condition simply not matching. Every other attribute that is
not a list (``None``, a number, a bool, an object) does not match.

The SDKs' local evaluators do not implement these operators: they are not in
``LOCAL_OPERATORS`` (``backend/app/services/sdk_ruleset.py``,
``sdk/js/src/evaluator.ts``, ``sdk/python/controlvar/evaluator.py``), so
a flag that uses them is ``evaluation: "remote"`` and the server answers it.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend.app.core.rules_engine import apply_operator, evaluate_condition
from backend.app.models.feature_flag import FeatureFlagStatus
from backend.app.schemas.targeting_rule import Condition, OperatorType
from backend.app.services import feature_flag_service as ffs

pytestmark = [pytest.mark.unit, pytest.mark.regression]

ALL = OperatorType.CONTAINS_ALL
ANY = OperatorType.CONTAINS_ANY


@pytest.mark.parametrize(
    "operator, attribute, items, expected",
    [
        # A string attribute with non-string items: no match, no TypeError.
        (ALL, "premium", [1], False),
        (ANY, "premium", [1], False),
        (ALL, "tier-42", [42], False),
        (ANY, "tier-42", [42], False),
        (ALL, "premium", [None], False),
        (ANY, "premium", [["pre"]], False),
        (ALL, "premium", [{"k": "pre"}], False),
        (ANY, "True", [True], False),
        # Mixed items: a non-string item never matches, a string one still can.
        (ALL, "premium", ["pre", 1], False),
        (ANY, "premium", ["pre", 1], True),
        (ANY, "premium", [1, "zzz"], False),
    ],
)
def test_a_string_attribute_with_non_string_items_does_not_raise(
    operator, attribute, items, expected
):
    assert apply_operator(operator, attribute, items) is expected


@pytest.mark.parametrize(
    "operator, attribute, items, expected",
    [
        # Unchanged: a string attribute with string items matches by substring.
        (ALL, "premium", ["pre", "ium"], True),
        (ALL, "premium", ["pre", "zzz"], False),
        (ANY, "premium", ["zzz", "mium"], True),
        (ANY, "premium", ["zzz", "yyy"], False),
    ],
)
def test_a_string_attribute_with_string_items_matches_by_substring(
    operator, attribute, items, expected
):
    assert apply_operator(operator, attribute, items) is expected


@pytest.mark.parametrize("operator", [ALL, ANY])
@pytest.mark.parametrize(
    "attribute",
    [None, 0, 42, 4.2, True, False, {"k": "pre"}],
    ids=["none", "zero", "int", "float", "true", "false", "object"],
)
@pytest.mark.parametrize(
    "items",
    [["pre"], [42], [True], [None]],
    ids=["string", "int", "bool", "none"],
)
def test_an_attribute_that_is_neither_a_list_nor_a_string_does_not_match(
    operator, attribute, items
):
    assert apply_operator(operator, attribute, items) is False


@pytest.mark.parametrize(
    "operator, attribute, items, expected",
    [
        (ALL, ["read", "write"], ["read", "write"], True),
        (ALL, ["read", "write"], ["read", "delete"], False),
        (ANY, ["read", "write"], ["admin", "write"], True),
        (ANY, ["read", "write"], ["admin", "delete"], False),
        (ALL, [1, 2, 3], [1, 3], True),
        (ANY, [1, 2, 3], [4, 2], True),
        (ANY, [1, 2, 3], ["1"], False),
        (ALL, ("a", "b"), ["a"], True),
        (ANY, [], ["a"], False),
    ],
)
def test_a_list_attribute_is_unchanged(operator, attribute, items, expected):
    assert apply_operator(operator, attribute, items) is expected


@pytest.mark.parametrize("operator", [ALL, ANY])
@pytest.mark.parametrize("items", ["pre", 1, None], ids=["string", "int", "none"])
def test_a_rule_value_that_is_not_a_list_does_not_match(operator, items):
    assert apply_operator(operator, "premium", items) is False
    assert apply_operator(operator, ["pre"], items) is False


@pytest.mark.parametrize("operator", [ALL, ANY])
def test_evaluate_condition_on_a_string_attribute_does_not_raise(operator):
    condition = Condition(attribute="plan", operator=operator, value=[1, 2])
    assert evaluate_condition(condition, {"plan": "premium"}) is False


@pytest.mark.parametrize("operator", ["array_contains", "array_intersects"])
def test_a_flag_rule_on_a_string_attribute_falls_through_instead_of_erroring(
    operator, monkeypatch
):
    """The flag path of the issue: the rule does not match; the rollout answers."""
    monkeypatch.setattr(ffs, "MetricsService", MagicMock())
    flag = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000270",
        key="contains-on-a-string",
        status=FeatureFlagStatus.ACTIVE,
        rollout_percentage=100,
        targeting_rules={
            "logical_operator": "AND",
            "groups": [
                {
                    "logical_operator": "AND",
                    "conditions": [
                        {"attribute": "tier", "operator": operator, "value": [42]}
                    ],
                }
            ],
        },
    )
    service = ffs.FeatureFlagService(db=MagicMock())

    result = service.evaluate_flag_detailed(flag, "user-1", {"tier": "tier-42"})

    assert result == {"enabled": True, "reason": "rollout", "rule_id": None}
