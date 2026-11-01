"""Feature-flag targeting rules are validated when saved (#535).

``validate_flag_targeting`` runs on ``targeting_rules`` in every flag create and
update (``FeatureFlagCreate`` / ``FeatureFlagUpdate``). Before it, both models
declared ``targeting_rules: Optional[Any]`` and stored any JSON: a flat
``{"country": ["US"]}``, an unknown operator, a string, a number. The flag
evaluator reads rules it cannot convert as "no rules", so the flag's global
rollout decided instead, with nothing said to the caller.

Pinned here, without a database:

* the accept/refuse table, with the exact message for each refusal (V5), and
  both request models refusing each row (V3);
* the message never repeats the submitted value, for a non-ASCII marker in
  every place a value can be (V4);
* parity: for every accepted value, what the flag evaluator does with the
  stored value is what the validator's reading gives -- ``enabled``,
  ``reason`` and ``rule_id``, over a grid of users and contexts (V6);
* nothing accepted is dropped: an accepted non-empty value is never read as
  "no rules" by ``normalise_targeting_rules`` (V7);
* dashboard rules hold at most 1,000 groups and conditions in all, counted
  before any condition is converted (#828); experiments are not capped;
* the SDK ruleset vectors split exactly 155 accepted / 4 refused / 2 legacy
  rows the API now refuses (V12);
* native rules: a key outside the schema is refused at every level -- the
  rule, its condition group, nested groups at any depth, conditions, and
  ``default_rule`` and ``default_rule.rule`` -- and a ``rollout_percentage``,
  dashboard or native, must be an integer from 0 to 100 (``100.0`` is
  accepted; ``33.5``, ``true`` and text are not). Experiments are unchanged.

The experiment validator is unchanged: its own table
(``test_experiment_targeting_validation.py``) is imported here, not copied.
"""

from __future__ import annotations

import copy
import json
import pathlib
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from backend.app.core import targeting_adapter
from backend.app.core.rules_engine import SEGMENT_MEMBERSHIP_KEY
from backend.app.core.targeting_adapter import (
    MAX_LIST_ITEMS,
    TargetingRulesError,
    expand_context,
    match_targeting_rule,
    normalise_targeting_rules,
    segment_ids_in,
    validate_experiment_targeting,
    validate_flag_targeting,
)
from backend.app.core.validation_errors import render_validation_errors
from backend.app.models.feature_flag import FeatureFlagStatus
from backend.app.schemas.experiment import ExperimentUpdate
from backend.app.schemas.feature_flag import FeatureFlagCreate, FeatureFlagUpdate
from backend.app.services import feature_flag_service as ffs
from backend.scripts import generate_ruleset_vectors as ruleset_vectors
from backend.tests.unit.core.test_experiment_targeting_validation import (
    ACCEPTED as EXPERIMENT_ACCEPTED,
)
from backend.tests.unit.core.test_experiment_targeting_validation import (
    NATIVE_US,
    US,
    cond,
    dash,
)
from backend.tests.unit.core.test_experiment_targeting_validation import (
    REFUSED as EXPERIMENT_REFUSED,
)

pytestmark = [pytest.mark.unit]

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
VECTORS = json.loads(
    (REPO_ROOT / "tests" / "sdk-contract" / "ruleset-vectors.json").read_text("utf-8")
)
BUILDER_OUTPUTS = json.loads(
    (
        REPO_ROOT
        / "frontend"
        / "src"
        / "tests"
        / "fixtures"
        / "flag-builder-outputs.json"
    ).read_text("utf-8")
)["rows"]

#: Experiment rows the flag validator refuses, and the flag message: a flag's
#: rollout percentage is an integer (``33.5`` would be applied as 33).
_FLAG_REFUSES = {
    "rollout_percentage 33.5": "rollout_percentage: must be an integer from 0 to 100",
}

NATIVE_RULE = NATIVE_US["rules"][0]
NATIVE_EQ = {"attribute": "country", "operator": "eq", "value": "US"}


def native(rule_extra=None, group=None, **top):
    """Native rules: one rule built from ``NATIVE_RULE``."""
    rule = {**NATIVE_RULE, **(rule_extra or {})}
    if group is not None:
        rule["rule"] = group
    return {"rules": [rule], **top}


def group(*conditions, groups=None, **extra):
    """A native condition group."""
    value = {"operator": "and", "conditions": list(conditions), **extra}
    if groups is not None:
        value["groups"] = groups
    return value


def nested(levels: int) -> Dict[str, Any]:
    """A native group whose deepest nested group is ``levels`` below it, built
    without recursion so it can be very deep."""
    inner = group(NATIVE_EQ)
    for _ in range(levels):
        inner = group(groups=[inner])
    return inner


#: Every other value the experiment validator accepts, the flag validator
#: accepts; then the flag-only rows.
ACCEPTED: Dict[str, Any] = {
    **{n: v for n, v in EXPERIMENT_ACCEPTED.items() if n not in _FLAG_REFUSES},
    "rollout_percentage 100.0": dash([US], rollout_percentage=100.0),
    "native rollout_percentage 100.0": native({"rollout_percentage": 100.0}),
    "native rollout_percentage 0": native({"rollout_percentage": 0}),
    "native groups nested 10 levels deep": native(group=nested(10)),
    "native, 1000 rules, groups and conditions": native(
        group=group(*[NATIVE_EQ] * 998)
    ),
    # #828: the same total, 1,000 groups and conditions, in two shapes.
    "dashboard, 1 group and 999 conditions": dash([US] * 999),
    "dashboard, 500 groups of 1 condition": dash(*[[US]] * 500, top="OR"),
    "native, every schema key at every level": {
        "version": "1.0",
        "rules": [
            {
                "id": "all-keys",
                "name": "n",
                "description": "d",
                "rule": group(
                    {
                        "attribute": "country",
                        "operator": "eq",
                        "value": "US",
                        "additional_value": None,
                        "attribute_type": "string",
                        "validation_schema": None,
                    },
                    groups=[group(NATIVE_EQ, groups=[group(NATIVE_EQ)])],
                ),
                "rollout_percentage": 40,
                "priority": 2,
            }
        ],
        "default_rule": {
            "id": "fallback",
            "name": "f",
            "description": "d",
            "rule": {"operator": "or", "conditions": [NATIVE_EQ], "groups": None},
            "rollout_percentage": 10,
            "priority": 0,
        },
    },
}

#: The experiment rows whose flag message differs, and the flag message.
_FLAG_MESSAGE = {
    "list shape": "targeting rules: a list of rules is not supported; use the groups shape",
    "flat dict (the old documented example)": "targeting rules: unknown key",
    "unknown key": "targeting rules: unknown key",
    "dashboard with unknown key": "targeting rules: unknown key",
    "top-level name": "name: not supported",
    "native duplicate rule ids": "targeting rules: rule ids must be unique",
    "rollout_percentage text": "rollout_percentage: must be an integer from 0 to 100",
    "rollout_percentage 101": "rollout_percentage: must be an integer from 0 to 100",
    "rollout_percentage true": "rollout_percentage: must be an integer from 0 to 100",
}

DEFAULT_RULE = {
    "id": "everyone",
    "rule": {
        "operator": "and",
        "conditions": [{"attribute": "country", "operator": "eq", "value": "US"}],
    },
    "rollout_percentage": 100,
}

_INTEGER = "must be an integer from 0 to 100"
_NODES = "at most 1000 rules, groups and conditions are allowed"
#: The dashboard shape's cap (#828), in the builder's units.
_DASHBOARD_NODES = "at most 1000 groups and conditions are allowed"

#: Native rules with a key outside the schema, or a rollout percentage that is
#: not an integer, at every level (V6/V7). The model ignores an unknown nested
#: key: ``rolout_percentage`` would leave the rule at 100%, ``priorty`` at
#: priority 0, and ``condtions`` an empty group that matches everyone.
NATIVE_REFUSED: Dict[str, Tuple[Any, str]] = {
    "native rule: rolout_percentage": (
        {"rules": [{"id": "r", "rule": group(NATIVE_EQ), "rolout_percentage": 10}]},
        "rules[0]: unknown key",
    ),
    "native rule: priorty": (native({"priorty": 5}), "rules[0]: unknown key"),
    "native second rule: unknown key": (
        {"rules": [NATIVE_RULE, {**NATIVE_RULE, "id": "r2", "enabled": True}]},
        "rules[1]: unknown key",
    ),
    "native group: condtions": (
        native(group={"operator": "and", "condtions": [NATIVE_EQ]}),
        "rules[0].rule: unknown key",
    ),
    "native nested group: condtions": (
        native(
            group=group(
                NATIVE_EQ,
                groups=[
                    group(NATIVE_EQ),
                    {"operator": "and", "condtions": [NATIVE_EQ]},
                ],
            )
        ),
        "rules[0].rule.groups[1]: unknown key",
    ),
    "native group nested three deep: condtions": (
        native(
            group=group(
                groups=[
                    group(
                        NATIVE_EQ,
                        groups=[
                            group(
                                NATIVE_EQ,
                                groups=[{"operator": "or", "condtions": [NATIVE_EQ]}],
                            )
                        ],
                    )
                ]
            )
        ),
        "rules[0].rule.groups[0].groups[0].groups[0]: unknown key",
    ),
    "native condition: unknown key": (
        native(group=group({**NATIVE_EQ, "values": ["US"]})),
        "rules[0].rule.conditions[0]: unknown key",
    ),
    "native nested condition: unknown key": (
        native(
            group=group(
                NATIVE_EQ, groups=[group(NATIVE_EQ, {**NATIVE_EQ, "negate": 1})]
            )
        ),
        "rules[0].rule.groups[0].conditions[1]: unknown key",
    ),
    "native default_rule: priorty": (
        {"rules": [], "default_rule": {**DEFAULT_RULE, "priorty": 1}},
        "default_rule: unknown key",
    ),
    "native default_rule.rule: condtions": (
        {
            "rules": [],
            "default_rule": {
                **DEFAULT_RULE,
                "rule": {"operator": "and", "condtions": [NATIVE_EQ]},
            },
        },
        "default_rule.rule: unknown key",
    ),
    "native default_rule nested group: unknown key": (
        {
            "rules": [],
            "default_rule": {
                **DEFAULT_RULE,
                "rule": group(NATIVE_EQ, groups=[group(NATIVE_EQ, negate=True)]),
            },
        },
        "default_rule.rule.groups[0]: unknown key",
    ),
    "native default_rule condition: unknown key": (
        {
            "rules": [],
            "default_rule": {**DEFAULT_RULE, "rule": group({**NATIVE_EQ, "op": "eq"})},
        },
        "default_rule.rule.conditions[0]: unknown key",
    ),
    # The first problem in document order is the one named.
    "native: the rule before its group": (
        native({"priorty": 1}, group={"operator": "and", "condtions": []}),
        "rules[0]: unknown key",
    ),
    "native: a group before its nested groups": (
        native(
            group=group(
                {**NATIVE_EQ, "values": 1},
                groups=[{"operator": "and", "condtions": []}],
            )
        ),
        "rules[0].rule.conditions[0]: unknown key",
    ),
    "native rollout_percentage 33.5": (
        native({"rollout_percentage": 33.5}),
        f"rules[0].rollout_percentage: {_INTEGER}",
    ),
    "native rollout_percentage true": (
        native({"rollout_percentage": True}),
        f"rules[0].rollout_percentage: {_INTEGER}",
    ),
    "native rollout_percentage text": (
        native({"rollout_percentage": "50"}),
        f"rules[0].rollout_percentage: {_INTEGER}",
    ),
    "native rollout_percentage 101": (
        native({"rollout_percentage": 101}),
        f"rules[0].rollout_percentage: {_INTEGER}",
    ),
    "native default_rule rollout_percentage 0.5": (
        {"rules": [], "default_rule": {**DEFAULT_RULE, "rollout_percentage": 0.5}},
        f"default_rule.rollout_percentage: {_INTEGER}",
    ),
    "rollout_percentage 100.5": (
        dash([US], rollout_percentage=100.5),
        f"rollout_percentage: {_INTEGER}",
    ),
    "native groups nested 11 levels deep": (
        native(group=nested(11)),
        "rules[0].rule" + ".groups[0]" * 10 + ".groups: nested too deeply",
    ),
    "native default_rule groups nested 11 levels deep": (
        {"rules": [], "default_rule": {**DEFAULT_RULE, "rule": nested(11)}},
        "default_rule.rule" + ".groups[0]" * 10 + ".groups: nested too deeply",
    ),
    "native, 1001 rules, groups and conditions": (
        native(group=group(*[NATIVE_EQ] * 999)),
        f"targeting rules: {_NODES}",
    ),
}

#: value -> the exact message. Every experiment row, then the flag-only rows.
REFUSED: Dict[str, Tuple[Any, str]] = {
    **{
        name: (value, _FLAG_MESSAGE.get(name, message))
        for name, (value, message) in EXPERIMENT_REFUSED.items()
    },
    **{
        name: (EXPERIMENT_ACCEPTED[name], message)
        for name, message in _FLAG_REFUSES.items()
    },
    "a number": (42, "targeting rules: must be an object"),
    "a float": (3.5, "targeting rules: must be an object"),
    "a string": ("x", "targeting rules: must be an object"),
    "true": (True, "targeting rules: must be an object"),
    "an empty list": (
        [],
        "targeting rules: a list of rules is not supported; use the groups shape",
    ),
    "a legacy list with an unknown operator": (
        [
            {
                "type": "context",
                "conditions": [
                    {"attribute": "plan", "operator": "equalz", "value": "pro"}
                ],
            }
        ],
        "targeting rules: a list of rules is not supported; use the groups shape",
    ),
    "a legacy user-id list": (
        [{"type": "user_id", "user_ids": ["user-1"]}],
        "targeting rules: a list of rules is not supported; use the groups shape",
    ),
    "native default_rule without rules": (
        {"default_rule": DEFAULT_RULE},
        "rules: required",
    ),
    "native default_rule and version without rules": (
        {"version": "1.0", "default_rule": DEFAULT_RULE},
        "rules: required",
    ),
    # ``rules: required`` comes after the unknown-key check.
    "unknown key beside default_rule": (
        {"default_rule": DEFAULT_RULE, "operator": "and"},
        "targeting rules: unknown key",
    ),
    "seed_demo_data's old new_dashboard_ui rules": (
        {"operator": "and", "rules": []},
        "targeting rules: unknown key",
    ),
    "dashboard name, lower-case operators": (
        {"logical_operator": "or", "groups": [{"conditions": [US]}], "name": "n"},
        "name: not supported",
    ),
    "one valid group and one bad group": (
        dash([US], [cond("age", "greater_than", "abc")], top="OR"),
        "groups[1].conditions[0].value: value is not valid for the operator",
    ),
    **NATIVE_REFUSED,
    # #828: one more than the cap, in the same two shapes as the accepted rows.
    "dashboard, 1 group and 1000 conditions": (
        dash([US] * 1000),
        f"targeting rules: {_DASHBOARD_NODES}",
    ),
    "dashboard, 500 groups and 501 conditions": (
        dash([US, US], *[[US]] * 499, top="OR"),
        f"targeting rules: {_DASHBOARD_NODES}",
    ),
    "dashboard, 100 groups of 50 conditions": (
        dash(*[[US] * 50] * 100, top="OR"),
        f"targeting rules: {_DASHBOARD_NODES}",
    ),
    # The size is checked before the groups are: the bad operator is not named.
    "dashboard over the cap with an unknown operator": (
        dash([cond("country", "equal", "US")] * 1000),
        f"targeting rules: {_DASHBOARD_NODES}",
    ),
}


def test_every_experiment_row_is_in_the_flag_tables():
    """The flag tables follow the experiment ones; a new experiment row is
    judged here too, not silently left out."""
    assert set(EXPERIMENT_ACCEPTED) <= set(ACCEPTED) | set(_FLAG_REFUSES)
    assert set(_FLAG_REFUSES) <= set(EXPERIMENT_ACCEPTED)
    assert not set(_FLAG_REFUSES) & set(ACCEPTED)
    assert set(EXPERIMENT_REFUSED) <= set(REFUSED)
    assert set(_FLAG_MESSAGE) <= set(EXPERIMENT_REFUSED)


@pytest.mark.regression
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, -0.5])
def test_a_rollout_percentage_outside_the_integers_0_to_100_is_refused(value):
    """Values JSON clients may not send (NaN, infinity), so not in the table
    the routes replay."""
    for rules, path in [
        (dash([US], rollout_percentage=value), "rollout_percentage"),
        (native({"rollout_percentage": value}), "rules[0].rollout_percentage"),
    ]:
        with pytest.raises(TargetingRulesError) as caught:
            validate_flag_targeting(rules)
        assert str(caught.value) == f"{path}: {_INTEGER}"


@pytest.mark.regression
def test_very_deep_nesting_is_refused_at_the_cap():
    """100,000 levels: refused at level 10, before the walk goes deeper (the
    message would otherwise name a path 100,000 groups long)."""
    with pytest.raises(TargetingRulesError) as caught:
        validate_flag_targeting(native(group=nested(100_000)))
    assert str(caught.value) == (
        "rules[0].rule" + ".groups[0]" * 10 + ".groups: nested too deeply"
    )


@pytest.mark.regression
@pytest.mark.parametrize(
    "value",
    [
        native(group=group(*[NATIVE_EQ] * 100_000)),
        native(group=group(groups=[group(NATIVE_EQ)] * 100_000)),
        {"rules": [NATIVE_RULE] * 100_000},
    ],
    ids=["conditions", "groups", "rules"],
)
def test_a_very_wide_value_is_refused_at_the_node_bound(value):
    with pytest.raises(TargetingRulesError) as caught:
        validate_flag_targeting(value)
    assert str(caught.value) == f"targeting rules: {_NODES}"


@pytest.mark.regression
@pytest.mark.parametrize(
    "value",
    [dash([US] * 100_000), dash(*[[US] * 50] * 100, top="OR")],
    ids=["1 group of 100000 conditions", "100 groups of 50 conditions"],
)
def test_a_wide_dashboard_value_is_refused_at_the_condition_cap(monkeypatch, value):
    """#828: refused by counting, before a single condition is converted."""
    calls = 0
    convert = targeting_adapter.convert_dashboard_condition

    def counted(condition):
        nonlocal calls
        calls += 1
        return convert(condition)

    monkeypatch.setattr(targeting_adapter, "convert_dashboard_condition", counted)
    with pytest.raises(TargetingRulesError) as caught:
        validate_flag_targeting(value)
    assert str(caught.value) == f"targeting rules: {_DASHBOARD_NODES}"
    assert calls == 0
    # The counter is wired: a value under the cap does reach the converter.
    validate_flag_targeting(dash([US] * 3))
    assert calls == 3


def test_the_dashboard_cap_is_the_native_one():
    assert targeting_adapter.MAX_NATIVE_NODES == 1000
    assert _DASHBOARD_NODES == (
        f"at most {targeting_adapter.MAX_NATIVE_NODES} groups and conditions are allowed"
    )


def test_experiment_dashboard_rules_are_not_capped():
    """#828 caps flags only; experiments are unchanged (#734)."""
    over = dash([US] * 1001)
    assert validate_experiment_targeting(copy.deepcopy(over)) is not None
    assert ExperimentUpdate(targeting_rules=copy.deepcopy(over)).targeting_rules == over


def test_the_depth_cap_is_the_rule_validators():
    from backend.app.core.rule_validation import RuleValidator
    from backend.app.core.targeting_adapter import MAX_NATIVE_GROUP_DEPTH

    assert MAX_NATIVE_GROUP_DEPTH == RuleValidator().max_rule_depth == 10


def test_the_experiment_validator_still_ignores_native_extras():
    """Experiments are unchanged here (follow-up #734): the same values the
    flag validator refuses still pass the experiment one."""
    for name in [
        "native rule: priorty",
        "native group: condtions",
        "native nested group: condtions",
        "native condition: unknown key",
    ]:
        assert validate_experiment_targeting(copy.deepcopy(REFUSED[name][0]))
    assert validate_experiment_targeting(dash([US], rollout_percentage=33.5))
    # Depth is still judged by RuleValidator alone, in its own words.
    with pytest.raises(TargetingRulesError) as caught:
        validate_experiment_targeting(native(group=nested(11)))
    assert str(caught.value) == (
        "rules[0].rule" + ".groups[0]" * 11 + ": rules are nested too deeply"
    )


# --- the table (V5) and the request models (V3) ----------------------------


@pytest.mark.regression
@pytest.mark.parametrize("name", list(ACCEPTED))
def test_accepted(name):
    value = copy.deepcopy(ACCEPTED[name])
    first = validate_flag_targeting(value)
    assert value == ACCEPTED[name], "the validator changed the value"
    assert validate_flag_targeting(value) == first
    # Stored as sent: neither model rewrites the value.
    assert FeatureFlagUpdate(targeting_rules=value).targeting_rules == ACCEPTED[name]
    created = FeatureFlagCreate(key="k", name="n", targeting_rules=value)
    assert created.targeting_rules == ACCEPTED[name]


@pytest.mark.regression
@pytest.mark.parametrize("name", list(REFUSED))
def test_refused(name):
    value, message = REFUSED[name]
    with pytest.raises(TargetingRulesError) as caught:
        validate_flag_targeting(copy.deepcopy(value))
    assert str(caught.value) == message


@pytest.mark.regression
@pytest.mark.parametrize("model", ["create", "update"])
@pytest.mark.parametrize("name", list(REFUSED))
def test_both_request_models_refuse(name, model):
    value, message = REFUSED[name]
    with pytest.raises(ValidationError) as caught:
        if model == "create":
            FeatureFlagCreate(key="k", name="n", targeting_rules=copy.deepcopy(value))
        else:
            FeatureFlagUpdate(targeting_rules=copy.deepcopy(value))
    errors = caught.value.errors()
    assert [(e["loc"], e["msg"]) for e in errors] == [
        (("targeting_rules",), f"Value error, {message}")
    ]


def test_null_and_omitted_are_accepted():
    assert FeatureFlagUpdate(targeting_rules=None).model_dump(exclude_unset=True) == {
        "targeting_rules": None
    }
    assert "targeting_rules" not in FeatureFlagUpdate().model_dump(exclude_unset=True)


def test_the_experiment_messages_are_unchanged():
    """The flag wording lives in the flag validator only."""
    for name in _FLAG_MESSAGE:
        value, message = EXPERIMENT_REFUSED[name]
        with pytest.raises(TargetingRulesError) as caught:
            validate_experiment_targeting(copy.deepcopy(value))
        assert str(caught.value) == message
    # The experiment validator still applies a lone default_rule.
    assert validate_experiment_targeting({"default_rule": DEFAULT_RULE}) is not None


# --- the message never repeats the submitted value (V4) --------------------

#: Non-ASCII, so a renderer that escapes or re-encodes it is caught too.
SENTINEL = "zqé漢-7731"

#: The marker as a key at every native level, and as a rollout percentage.
NATIVE_SENTINEL: Dict[str, Any] = {
    "native rule key": native({SENTINEL: 1}),
    "native group key": native(group=group(NATIVE_EQ, **{SENTINEL: 1})),
    "native nested group key": native(
        group=group(NATIVE_EQ, groups=[group(NATIVE_EQ, **{SENTINEL: 1})])
    ),
    "native condition key": native(group=group({**NATIVE_EQ, SENTINEL: 1})),
    "native nested condition key": native(
        group=group(groups=[group({**NATIVE_EQ, SENTINEL: 1})])
    ),
    "native default_rule key": {
        "rules": [],
        "default_rule": {**DEFAULT_RULE, SENTINEL: 1},
    },
    "native default_rule.rule key": {
        "rules": [],
        "default_rule": {**DEFAULT_RULE, "rule": group(NATIVE_EQ, **{SENTINEL: 1})},
    },
    "native default_rule nested group key": {
        "rules": [],
        "default_rule": {
            **DEFAULT_RULE,
            "rule": group(groups=[group(NATIVE_EQ, **{SENTINEL: 1})]),
        },
    },
    "native rollout_percentage": native({"rollout_percentage": SENTINEL}),
    "dashboard rollout_percentage": dash([US], rollout_percentage=SENTINEL),
}
NATIVE_SENTINEL_IDS = list(NATIVE_SENTINEL)
NATIVE_SENTINEL_VALUES = list(NATIVE_SENTINEL.values())


@pytest.mark.regression
@pytest.mark.parametrize(
    "value",
    [
        dash([cond("country", SENTINEL, "US")]),
        dash([cond(SENTINEL, "equals", "US")]),
        dash([cond("app.version", "semver_gte", SENTINEL)]),
        dash([cond("age", "greater_than", SENTINEL)]),
        dash([cond("email", "regex", "(" + SENTINEL)]),
        dash([cond("country", "equals", "US")], top=SENTINEL),
        {SENTINEL: ["US"]},
        dash([US], **{SENTINEL: 1}),
        {"groups": [{"conditions": [US], SENTINEL: True}]},
        {"rules": [{"id": SENTINEL, "rule": {"operator": SENTINEL}}]},
        {"default_rule": {"id": SENTINEL, "rule": {"operator": "and"}}},
        [{"type": "context", "conditions": [{"attribute": SENTINEL}]}],
        SENTINEL,
        *NATIVE_SENTINEL_VALUES,
    ],
    ids=[
        "operator",
        "attribute",
        "semver",
        "number",
        "regex",
        "logical_operator",
        "key",
        "dashboard key",
        "group key",
        "native",
        "default_rule",
        "list",
        "string",
        *NATIVE_SENTINEL_IDS,
    ],
)
@pytest.mark.parametrize("model", [FeatureFlagCreate, FeatureFlagUpdate])
def test_the_message_never_repeats_the_submitted_value(value, model):
    body = {"targeting_rules": value}
    if model is FeatureFlagCreate:
        body.update(key="k", name="n")
    with pytest.raises(ValidationError) as caught:
        model(**body)
    # What the 422 carries: the application's handler renders exactly this.
    text = render_validation_errors(caught.value.errors()).decode()
    assert '"loc":["targeting_rules"]' in text.replace(" ", "")
    assert "7731" not in text, text
    assert "\\u" not in text and "zq" not in text, text


# --- parity: accepted means evaluated as written (V6) ----------------------

USERS = ["user-1", "user-2", "user-3"]
CONTEXTS: List[Optional[Dict[str, Any]]] = [
    None,
    {"country": "US"},
    {"country": "GB", "plan": "premium"},
    {"user": {"country": "US", "plan": "beta"}},
    {"country": "US", "age": 30, "email": "ann@example.com", "employee": True},
    {"age": "17", "plan": "free", "tier": "premium", "browser": "chrome"},
    {"app_version": "3.4.1", "os_version": "17.4", "app": {"version": "3.2.0"}},
    {"device": {"type": "mobile", "os": "ios"}, "beta_features": ["x", "y"]},
]


def _flag(rules: Any) -> Any:
    return MagicMock(
        id="00000000-0000-0000-0000-000000000535",
        key="parity",
        status=FeatureFlagStatus.ACTIVE,
        rollout_percentage=50,
        targeting_rules=rules,
    )


def _accepted_corpus() -> List[Tuple[str, Any]]:
    rows = [(f"experiment-table: {n}", v) for n, v in ACCEPTED.items()]
    rows += [
        (f"vector: {f['key']}", f["targeting_rules"])
        for f in VECTORS["stored_flags"]
        if _accepts(f["targeting_rules"])
    ]
    rows += [
        (f"builder: {r['name']}", r["sends"])
        for r in BUILDER_OUTPUTS
        if r["outcome"] == "accepted"
    ]
    return rows


def _accepts(value: Any) -> bool:
    try:
        validate_flag_targeting(copy.deepcopy(value))
    except TargetingRulesError:
        return False
    return True


def _expected(service, flag, rules, user_id, context) -> Tuple[bool, str, Any]:
    """What the validator's reading of the rules gives, computed without the
    flag service's own matching."""
    ctx = expand_context(context or {})
    segment_ids = segment_ids_in(rules)
    if segment_ids:
        # Membership as the stubbed resolver gives it (#440).
        memberships = ruleset_vectors.stub_memberships(None, user_id, ctx, segment_ids)
        if memberships.undecided(segment_ids):
            return False, "error", None
        ctx[SEGMENT_MEMBERSHIP_KEY] = memberships.members
    matched = match_targeting_rule(rules, ctx) if rules else None
    if matched is None:
        return service._evaluate_percentage_rollout(flag, user_id), "rollout", None
    enabled = service._evaluate_percentage_rollout(
        flag, user_id, int(matched.rollout_percentage)
    )
    return enabled, "targeting_rule", matched.id


@pytest.mark.regression
def test_what_is_accepted_is_what_the_flag_evaluator_applies(monkeypatch):
    monkeypatch.setattr(ffs, "MetricsService", MagicMock())
    monkeypatch.setattr(
        ffs, "resolve_segment_memberships", ruleset_vectors.stub_memberships
    )
    service = ffs.FeatureFlagService(db=MagicMock())
    corpus = _accepted_corpus()
    # The vectors' 155 accepted rows: 143, and since #1497 (F5) the 12 value
    # flags' (each value type, four ways a flag reaches an SDK).
    assert len(corpus) == len(ACCEPTED) + 155 + 529
    with_rules = 0
    diverged = []
    for name, raw in corpus:
        rules = validate_flag_targeting(copy.deepcopy(raw))
        with_rules += rules is not None
        flag = _flag(copy.deepcopy(raw))
        for context in CONTEXTS:
            for user_id in USERS:
                got = service.evaluate_flag_detailed(flag, user_id, context)
                want = _expected(service, flag, rules, user_id, context)
                if (got["enabled"], got["reason"], got["rule_id"]) != want:
                    diverged.append((name, context, user_id, got, want))
    assert diverged == []
    # Guards the comparison: were every row "no rules", it could tell nothing.
    assert with_rules >= 500, with_rules


def test_the_parity_grid_reaches_targeting_rules(monkeypatch):
    """Guards V6: the grid must contain users that a rule matches, through an
    alias (``user.country`` against a bare ``country``) among them."""
    monkeypatch.setattr(ffs, "MetricsService", MagicMock())
    service = ffs.FeatureFlagService(db=MagicMock())
    flag = _flag(dash([cond("user.country", "equals", "US")]))
    reasons = {
        service.evaluate_flag_detailed(flag, u, c)["reason"]
        for c in CONTEXTS
        for u in USERS
    }
    assert reasons == {"targeting_rule", "rollout"}


# --- nothing accepted is dropped (V7) --------------------------------------


def _corpus_for_drops() -> List[Tuple[str, Any]]:
    rows = _accepted_corpus()
    rows += [(f"refused: {n}", v) for n, (v, _m) in REFUSED.items()]
    rows += [
        (f"builder: {r['name']}", r["sends"])
        for r in BUILDER_OUTPUTS
        if r["outcome"] != "accepted"
    ]
    rows += [
        (f"vector: {f['key']}", f["targeting_rules"]) for f in VECTORS["stored_flags"]
    ]
    return rows


def _no_rules(rules: Any) -> bool:
    return rules is None or (not rules.rules and rules.default_rule is None)


@pytest.mark.regression
def test_an_accepted_value_is_never_read_as_no_rules():
    dropped = []
    differ = []
    for name, raw in _corpus_for_drops():
        try:
            validated = validate_flag_targeting(copy.deepcopy(raw))
        except TargetingRulesError:
            continue
        read = normalise_targeting_rules(copy.deepcopy(raw), owner="test")
        if isinstance(raw, dict) and raw and read is None and not _no_rules(validated):
            dropped.append(name)
        if not (_no_rules(validated) and _no_rules(read)) and validated != read:
            differ.append(name)
    assert dropped == []
    assert differ == []


# --- the SDK ruleset vectors (V12) -----------------------------------------


@pytest.mark.regression
def test_the_ruleset_vectors_split_exactly():
    """The vectors are stored through the ORM and keep being served; these are
    the ones a create or an update would now refuse."""
    accepted, refused, legacy = [], [], []
    for flag in VECTORS["stored_flags"]:
        rules = flag["targeting_rules"]
        if isinstance(rules, list) and rules:
            legacy.append(flag["key"])
        elif _accepts(rules):
            accepted.append(flag["key"])
        else:
            refused.append(flag["key"])
    assert (len(accepted), len(refused), len(legacy)) == (155, 4, 2)
    assert sorted(refused) == [
        "empty-list",
        "unconvertible-attribute",
        "unconvertible-number",
        "unconvertible-operator",
    ]
    assert sorted(legacy) == ["legacy-context", "legacy-user-ids"]


def test_list_operators_keep_their_limit():
    accepted = dash([cond("user_id", "in", [str(i) for i in range(MAX_LIST_ITEMS)])])
    assert validate_flag_targeting(accepted) is not None
    assert NATIVE_US["rules"][0]["id"] == validate_flag_targeting(NATIVE_US).rules[0].id
