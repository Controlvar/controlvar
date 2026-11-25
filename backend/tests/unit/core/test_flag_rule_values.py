"""``flag_values.rule_values``: the values a flag's stored rules carry (#2012).

* The dashboard shape's one rule carries the top-level ``value``; the native
  shape's rules carry ``rules[i].value`` and ``default_rule.value``; a
  condition's ``value`` is not a rule's.
* The legacy list, no rules and an object that is neither shape carry none.
* The key is the rule's id as the evaluator reads it, checked against
  ``targeting_adapter.normalise_targeting_rules`` on every shape it reads.
* It is pure: it changes nothing it is given, and nothing in the application
  calls it yet.

``backend/tests/integration/database/test_flag_split_migration.py`` holds
``t2012_flag_split``'s SQL to the same answers, over :data:`STORED_RULES`.
"""

from __future__ import annotations

import ast
import copy
from pathlib import Path

import pytest

from backend.app.core import flag_values, targeting_adapter
from backend.app.core.flag_values import rule_values
from backend.app.core.targeting_adapter import normalise_targeting_rules

pytestmark = [pytest.mark.unit, pytest.mark.regression]

APP = Path(flag_values.__file__).resolve().parents[1]
REPO_ROOT = APP.parents[1]


def _condition(value="US") -> dict:
    return {"attribute": "country", "operator": "equals", "value": value}


def _dashboard(**top) -> dict:
    """A dashboard-shape rule whose condition has a value of its own."""
    return {
        "logical_operator": "AND",
        "groups": [{"logical_operator": "AND", "conditions": [_condition()]}],
        **top,
    }


def _rule(rule_id="r1", **extra) -> dict:
    """A native rule whose conditions, nested ones too, have values."""
    condition = {"attribute": "plan", "operator": "eq", "value": "pro"}
    return {
        "id": rule_id,
        "rule": {
            "operator": "and",
            "conditions": [condition],
            "groups": [{"operator": "or", "conditions": [condition]}],
        },
        "rollout_percentage": 50,
        "priority": 0,
        **extra,
    }


#: ``(name, stored targeting_rules, what rule_values answers)``. The migration
#: test stores every one of these and compares the SQL's answer with the third
#: column, so a shape belongs here rather than in one of the two tests.
STORED_RULES = [
    # No rules at all.
    ("none", None, {}),
    ("empty-object", {}, {}),
    ("empty-list", [], {}),
    # The dashboard shape.
    ("dashboard", _dashboard(), {}),
    ("dashboard-percentage", _dashboard(rollout_percentage=25), {}),
    ("dashboard-value", _dashboard(value="B"), {"dashboard": "B"}),
    ("dashboard-value-id", _dashboard(id="vip", value="B"), {"vip": "B"}),
    ("dashboard-value-empty-id", _dashboard(id="", value="B"), {"dashboard": "B"}),
    ("dashboard-value-null", _dashboard(value=None), {"dashboard": None}),
    ("dashboard-value-false", _dashboard(value=False), {"dashboard": False}),
    ("dashboard-value-object", _dashboard(value={"a": [1]}), {"dashboard": {"a": [1]}}),
    ("dashboard-no-condition", {"groups": [], "value": "B"}, {"dashboard": "B"}),
    ("dashboard-groups-null", {"groups": None, "value": "B"}, {"dashboard": "B"}),
    ("dashboard-groups-text", {"groups": "x", "value": 1}, {"dashboard": 1}),
    (
        "dashboard-wins-over-native",
        {**_dashboard(), "rules": [_rule(value="B")], "default_rule": _rule(value="B")},
        {},
    ),
    (
        "dashboard-wins-and-has-a-value",
        {**_dashboard(value="A"), "rules": [_rule(value="B")]},
        {"dashboard": "A"},
    ),
    # The native shape.
    ("native", {"rules": [_rule()]}, {}),
    ("native-empty", {"rules": []}, {}),
    ("native-value", {"rules": [_rule(value="B")]}, {"r1": "B"}),
    (
        "native-second-value",
        {"rules": [_rule("r1"), _rule("r2", value="B"), _rule("r3", value=3)]},
        {"r2": "B", "r3": 3},
    ),
    ("native-value-null", {"rules": [_rule(value=None)]}, {"r1": None}),
    (
        "native-default",
        {"rules": [_rule()], "default_rule": _rule("everyone", value="B")},
        {"everyone": "B"},
    ),
    (
        "native-default-only",
        {"version": "1.0", "rules": [], "default_rule": _rule("everyone", value="B")},
        {"everyone": "B"},
    ),
    (
        "native-both",
        {
            "rules": [_rule("r1", value="B")],
            "default_rule": _rule("everyone", value="C"),
        },
        {"r1": "B", "everyone": "C"},
    ),
    ("native-default-null", {"rules": [_rule()], "default_rule": None}, {}),
    ("native-default-text", {"rules": [_rule()], "default_rule": "value"}, {}),
    ("native-default-list", {"rules": [], "default_rule": ["value"]}, {}),
    ("native-top-level-value", {"rules": [_rule()], "value": "B"}, {}),
    ("native-odd-entries", {"rules": [None, 1, "value", ["value"], {}]}, {}),
    ("native-entry-without-id", {"rules": [{"value": "B"}]}, {"": "B"}),
    ("native-rules-null", {"rules": None, "default_rule": _rule(value=1)}, {"r1": 1}),
    ("native-rules-object", {"rules": {"value": "B"}}, {}),
    ("native-rules-text", {"rules": "value"}, {}),
    # Neither shape.
    ("default-rule-alone", {"default_rule": _rule(value="B")}, {}),
    ("operator-alone", {"logical_operator": "AND", "value": "B"}, {}),
    ("legacy-flat", {"value": ["x"]}, {}),
    ("legacy-flat-attribute", {"country": ["US"], "value": "B"}, {}),
    (
        "legacy-list",
        [
            {
                "type": "context",
                "conditions": [{"attribute": "plan", "operator": "eq", "value": "pro"}],
                "value": "B",
                "percentage": 50,
            },
            {"type": "user_id", "user_ids": ["u1"], "value": "B"},
        ],
        {},
    ),
    ("a-string", "value", {}),
    ("a-number", 3, {}),
    ("a-boolean", True, {}),
]

#: The six stored shapes a detector has to tell apart: the two shapes without a
#: value (their conditions have one), the three places a rule's value lives, and
#: a legacy flat object whose one key is ``value``.
SIX_SHAPES = (
    "dashboard",
    "dashboard-value",
    "native",
    "native-value",
    "native-default",
    "legacy-flat",
)


def test_the_table_has_its_stated_size_and_holds_the_six_shapes():
    names = [name for name, _, _ in STORED_RULES]
    assert len(names) == len(set(names)) == 41
    assert set(SIX_SHAPES) <= set(names)
    carrying = {name for name, _, values in STORED_RULES if values}
    assert len(carrying) == 18
    # The six divide: three carry a value and three do not.
    assert {name for name in SIX_SHAPES if name in carrying} == {
        "dashboard-value",
        "native-value",
        "native-default",
    }


@pytest.mark.parametrize(
    "stored, expected",
    [case[1:] for case in STORED_RULES],
    ids=[case[0] for case in STORED_RULES],
)
def test_the_values_the_stored_rules_carry(stored, expected):
    before = copy.deepcopy(stored)
    values = rule_values(stored)
    assert values == expected
    # ``False == 0`` and ``True == 1``: the value itself, not its equal.
    assert {k: type(v) for k, v in values.items()} == {
        k: type(v) for k, v in expected.items()
    }
    assert stored == before, "rule_values changed the rules it read"


def test_a_rules_id_is_the_one_the_evaluator_matches():
    """Every rule that carries a value and that the adapter reads is named by
    the adapter's id for it, so the matched rule's id finds its value."""
    compared = 0
    for name, stored, expected in STORED_RULES:
        if not expected:
            continue
        rules = normalise_targeting_rules(copy.deepcopy(stored), owner=f"test:{name}")
        if rules is None:
            continue
        ids = {rule.id for rule in rules.rules}
        if rules.default_rule is not None:
            ids.add(rules.default_rule.id)
        if not ids:
            # A dashboard rule with no condition is read as no rule.
            continue
        assert set(expected) <= ids, (name, sorted(expected), sorted(ids))
        compared += 1
    assert compared >= 10, compared


def test_the_dashboard_rule_id_is_the_adapters():
    assert flag_values._DASHBOARD_RULE_ID == targeting_adapter.DASHBOARD_RULE_ID


def _callers() -> list:
    """Every reference to ``rule_values`` in the application's code."""
    found = []
    for root in (APP, REPO_ROOT / "modules" / "backend" / "app"):
        for path in sorted(root.rglob("*.py")) if root.is_dir() else ():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                name = (
                    node.id
                    if isinstance(node, ast.Name)
                    else node.attr
                    if isinstance(node, ast.Attribute)
                    else None
                )
                if name == "rule_values":
                    found.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    return found


def test_nothing_in_the_application_calls_it_yet():
    assert _callers() == []
