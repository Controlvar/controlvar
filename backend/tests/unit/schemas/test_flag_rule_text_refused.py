"""The flag request models refuse NUL and unpaired surrogates in
``targeting_rules`` and ``tags`` anywhere, in a key or a value, nested or not
(#2163).

The database cannot store either character, so such a request answered 500.
``FeatureFlagCreate``, ``FeatureFlagUpdate`` and ``FlagEnvironmentUpdate`` now
refuse it before the rules are read: one error, at the field, with the fixed
message, whatever else in the rules is wrong. The routes are
``backend/tests/integration/api/test_flag_text_refused.py``.
"""

from __future__ import annotations

import copy
from typing import Any, Callable, Dict

import pytest
from pydantic import ValidationError

from backend.app.schemas.feature_flag import FeatureFlagCreate, FeatureFlagUpdate
from backend.app.schemas.flag_environment import FlagEnvironmentUpdate

pytestmark = [pytest.mark.unit, pytest.mark.regression]

CHARACTERS = {"nul": "\x00", "surrogate": "\ud800"}

DASHBOARD = {
    "logical_operator": "AND",
    "groups": [
        {
            "id": "g1",
            "logical_operator": "AND",
            "conditions": [
                {
                    "id": "c1",
                    "attribute": "country",
                    "operator": "equals",
                    "value": "US",
                }
            ],
        }
    ],
}
NATIVE = {
    "rules": [
        {
            "id": "r1",
            "name": "n",
            "description": "d",
            "rule": {
                "operator": "and",
                "conditions": [
                    {"attribute": "country", "operator": "eq", "value": "US"}
                ],
            },
            "rollout_percentage": 100,
        }
    ]
}


def _dashboard(change: Callable[[Dict[str, Any], str], Any]):
    def build(character: str) -> Dict[str, Any]:
        rules = copy.deepcopy(DASHBOARD)
        change(rules, character)
        return rules

    return build


def _native(change: Callable[[Dict[str, Any], str], Any]):
    def build(character: str) -> Dict[str, Any]:
        rules = copy.deepcopy(NATIVE)
        change(rules, character)
        return rules

    return build


def _dc(rules):
    return rules["groups"][0]["conditions"][0]


def _nr(rules):
    return rules["rules"][0]


def _nc(rules):
    return rules["rules"][0]["rule"]["conditions"][0]


def _deep_key(rules, c):
    """A key 50 objects down inside a condition's validation_schema."""
    inner: Dict[str, Any] = {"k" + c: 1}
    for _ in range(50):
        inner = {"n": inner}
    _nc(rules)["validation_schema"] = inner


#: Every string of either rules shape, keys included; some of them another
#: check refused before this one existed (an attribute, an operator, a
#: semantic version, a pattern).
RULE_PLACES = {
    "dashboard-value": _dashboard(lambda r, c: _dc(r).update(value="US" + c)),
    "dashboard-list-item": _dashboard(
        lambda r, c: _dc(r).update(operator="in", value=["US", "CA" + c])
    ),
    "dashboard-condition-id": _dashboard(lambda r, c: _dc(r).update(id="c1" + c)),
    "dashboard-group-id": _dashboard(lambda r, c: r["groups"][0].update(id="g" + c)),
    "dashboard-top-level-id": _dashboard(lambda r, c: r.update(id="t" + c)),
    "dashboard-attribute": _dashboard(
        lambda r, c: _dc(r).update(attribute="country" + c)
    ),
    "dashboard-operator": _dashboard(lambda r, c: _dc(r).update(operator="equals" + c)),
    "dashboard-logical-operator": _dashboard(
        lambda r, c: r.update(logical_operator="AND" + c)
    ),
    "dashboard-group-logical-operator": _dashboard(
        lambda r, c: r["groups"][0].update(logical_operator="AND" + c)
    ),
    "dashboard-pattern": _dashboard(
        lambda r, c: _dc(r).update(operator="regex", value="^US" + c + "$")
    ),
    "dashboard-semantic-version": _dashboard(
        lambda r, c: _dc(r).update(operator="semver_gte", value="1.2.3" + c)
    ),
    "dashboard-condition-key": _dashboard(lambda r, c: _dc(r).update({"z" + c: 1})),
    "native-value": _native(lambda r, c: _nc(r).update(value="US" + c)),
    "native-list-item": _native(
        lambda r, c: _nc(r).update(operator="in", value=["US", "CA" + c])
    ),
    "native-rule-id": _native(lambda r, c: _nr(r).update(id="r1" + c)),
    "native-rule-name": _native(lambda r, c: _nr(r).update(name="n" + c)),
    "native-rule-description": _native(lambda r, c: _nr(r).update(description="d" + c)),
    "native-version": _native(lambda r, c: r.update(version="1.0" + c)),
    "native-attribute": _native(lambda r, c: _nc(r).update(attribute="country" + c)),
    "native-operator": _native(lambda r, c: _nc(r).update(operator="eq" + c)),
    "native-group-operator": _native(
        lambda r, c: _nr(r)["rule"].update(operator="and" + c)
    ),
    "native-additional-value": _native(
        lambda r, c: _nc(r).update(additional_value="x" + c)
    ),
    "native-attribute-type": _native(
        lambda r, c: _nc(r).update(attribute_type="string" + c)
    ),
    "native-validation-schema-key": _native(
        lambda r, c: _nc(r).update(validation_schema={"type" + c: "string"})
    ),
    "native-validation-schema-value": _native(
        lambda r, c: _nc(r).update(validation_schema={"type": "string" + c})
    ),
    "native-validation-schema-deep-key": _native(_deep_key),
    "native-pattern": _native(
        lambda r, c: _nc(r).update(operator="match_regex", value="^US" + c + "$")
    ),
    "native-semantic-version": _native(
        lambda r, c: _nc(r).update(operator="semantic_version", value="1.2.3" + c)
    ),
    "native-default-rule-id": _native(
        lambda r, c: r.update(default_rule={**copy.deepcopy(_nr(r)), "id": "all" + c})
    ),
    "native-nested-group-value": _native(
        lambda r, c: _nr(r)["rule"].update(
            groups=[
                {
                    "operator": "and",
                    "conditions": [
                        {"attribute": "plan", "operator": "eq", "value": "p" + c}
                    ],
                }
            ]
        )
    ),
    "native-rule-key": _native(lambda r, c: _nr(r).update({"z" + c: 1})),
}

TAG_PLACES = {
    "the-only-tag": lambda c: ["a" + c],
    "a-second-tag": lambda c: ["ok", "b" + c],
    "a-tag-of-another-type-first": lambda c: [1, "b" + c],
}

#: A create needs a key and a name; the other two models need nothing.
MODELS = {
    "create": (FeatureFlagCreate, {"key": "flag-text", "name": "Flag text"}),
    "update": (FeatureFlagUpdate, {}),
    "environment-update": (FlagEnvironmentUpdate, {}),
}
FLAG_MODELS = {name: MODELS[name] for name in ("create", "update")}


def _refusal(field: str) -> Dict[str, Any]:
    return {
        "type": "value_error",
        "loc": (field,),
        "msg": f"Value error, {field} must not contain NUL or unpaired surrogate characters",
    }


def _errors(model, body) -> list:
    with pytest.raises(ValidationError) as caught:
        model.model_validate(body)
    return [{k: e[k] for k in ("type", "loc", "msg")} for e in caught.value.errors()]


@pytest.mark.parametrize("character", CHARACTERS)
@pytest.mark.parametrize("place", RULE_PLACES)
@pytest.mark.parametrize("model", MODELS)
def test_the_rules_are_refused_wherever_the_character_is(model, place, character):
    cls, base = MODELS[model]
    rules = RULE_PLACES[place](CHARACTERS[character])

    assert _errors(cls, {**base, "targeting_rules": rules}) == [
        _refusal("targeting_rules")
    ]


@pytest.mark.parametrize("character", CHARACTERS)
@pytest.mark.parametrize("place", TAG_PLACES)
@pytest.mark.parametrize("model", FLAG_MODELS)
def test_the_tags_are_refused_wherever_the_character_is(model, place, character):
    cls, base = FLAG_MODELS[model]
    tags = TAG_PLACES[place](CHARACTERS[character])

    assert _errors(cls, {**base, "tags": tags}) == [_refusal("tags")]


@pytest.mark.parametrize("model", MODELS)
def test_rules_without_the_character_are_accepted_as_before(model):
    cls, base = MODELS[model]
    emoji = "\U0001f600"
    for rules in (
        None,
        {},
        DASHBOARD,
        NATIVE,
        RULE_PLACES["dashboard-value"](emoji),
        RULE_PLACES["native-validation-schema-key"](emoji),
    ):
        accepted = cls.model_validate({**base, "targeting_rules": rules})
        assert accepted.targeting_rules == rules


@pytest.mark.parametrize("model", FLAG_MODELS)
def test_tags_without_the_character_are_accepted_as_before(model):
    cls, base = FLAG_MODELS[model]
    for tags in (None, [], ["a", "b\U0001f600"]):
        assert cls.model_validate({**base, "tags": tags}).tags == tags
