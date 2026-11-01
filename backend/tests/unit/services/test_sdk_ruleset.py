"""
The ruleset builder (``backend/app/services/sdk_ruleset.py``).

What an SDK receives for each kind of stored flag, which rule values keep a
flag local, what is left out, and when the version moves.
"""

from __future__ import annotations

import re
import uuid

import pytest
import re2

from backend.app.core import pattern_match
from backend.app.core.flag_values import MAX_SAFE_INTEGER
from backend.app.models import register_core_models
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.schemas.targeting_rule import Condition, OperatorType
from backend.app.services import sdk_ruleset
from backend.app.services.sdk_ruleset import (
    LOCAL_OPERATORS,
    build_flag_entry,
    build_ruleset,
    condition_is_local,
)

pytestmark = pytest.mark.unit

register_core_models()


def _flag(key="flag", rules=None, rollout=0, status=FeatureFlagStatus.ACTIVE, **extra):
    return FeatureFlag(
        key=key,
        name=extra.pop("name", key),
        status=status,
        rollout_percentage=rollout,
        targeting_rules=rules,
        **extra,
    )


def _native(*conditions, op="and", priority=0, rule_id="r", rollout=100):
    return {
        "rules": [
            {
                "id": rule_id,
                "priority": priority,
                "rollout_percentage": rollout,
                "rule": {"operator": op, "conditions": list(conditions)},
            }
        ]
    }


def _c(attribute, operator, value):
    return {"attribute": attribute, "operator": operator, "value": value}


# ---------------------------------------------------------------------------
# Rule-value domains: each rejected type, by operator
# ---------------------------------------------------------------------------

REJECTED = [
    # eq / neq: a string or a portable number, nothing else
    ("eq", True),
    ("eq", False),
    ("eq", None),
    ("eq", ["x"]),
    ("eq", {"k": 1}),
    ("eq", MAX_SAFE_INTEGER + 1),
    ("eq", -(MAX_SAFE_INTEGER + 1)),
    ("eq", 10**400),
    ("eq", float("inf")),
    ("eq", float("nan")),
    ("eq", 1e300),
    ("neq", True),
    ("neq", None),
    # string operators: a string only (no number, bool or null)
    ("contains", 5),
    ("contains", 5.0),
    ("contains", True),
    ("contains", None),
    ("not_contains", 0),
    ("starts_with", ["x"]),
    ("ends_with", {"k": "x"}),
    # numeric operators: a portable number only (no numeric string, no bool)
    ("gt", "10"),
    ("gt", "1_0"),
    ("gte", True),
    ("lt", None),
    ("lte", MAX_SAFE_INTEGER + 1),
    ("gt", float("inf")),
    ("gt", [5]),
    # lists: non-empty, all strings or all portable numbers
    ("in", []),
    ("in", ["x", 5]),
    ("in", [True]),
    ("in", [None]),
    ("in", [["x"]]),
    ("in", [5, MAX_SAFE_INTEGER + 2]),
    ("not_in", [1, True]),
    ("not_in", [{"k": 1}]),
]


@pytest.mark.parametrize("operator, value", REJECTED, ids=repr)
def test_a_rule_value_outside_its_domain_is_not_local(operator, value):
    condition = Condition.model_construct(
        attribute="a", operator=OperatorType(operator), value=value
    )
    assert condition_is_local(condition) is False


ACCEPTED = [
    ("eq", "x"),
    ("eq", ""),
    ("eq", 5),
    ("eq", 5.5),
    ("eq", -0.0),
    ("eq", MAX_SAFE_INTEGER),
    ("eq", -MAX_SAFE_INTEGER),
    ("neq", "x"),
    ("contains", ""),
    ("starts_with", "é"),
    ("gt", 0),
    ("lte", 1.5),
    ("in", ["x"]),
    ("in", [5, 5.5]),
    ("not_in", ["a", "b"]),
    ("is_null", None),
    ("is_not_null", "ignored"),
]


@pytest.mark.parametrize("operator, value", ACCEPTED, ids=repr)
def test_a_rule_value_in_its_domain_is_local(operator, value):
    condition = Condition.model_construct(
        attribute="a", operator=OperatorType(operator), value=value
    )
    assert condition_is_local(condition) is True


@pytest.mark.parametrize(
    "operator",
    [op for op in OperatorType if op.value not in LOCAL_OPERATORS],
    ids=lambda op: op.value,
)
def test_every_other_operator_is_remote(operator):
    condition = Condition.model_construct(attribute="a", operator=operator, value="x")
    assert condition_is_local(condition) is False


def test_the_local_operators_are_engine_operators():
    engine = {op.value for op in OperatorType}
    assert set(LOCAL_OPERATORS) <= engine
    assert len(set(LOCAL_OPERATORS)) == len(LOCAL_OPERATORS) == 14


# ---------------------------------------------------------------------------
# Flag entries
# ---------------------------------------------------------------------------


def test_a_dashboard_flag_is_local_with_one_normalised_rule():
    rules = {
        "logical_operator": "OR",
        "groups": [
            {
                "logical_operator": "AND",
                "conditions": [
                    _c("user.country", "in", "US, CA"),
                    _c("age", "greater_than", "18"),
                ],
            }
        ],
    }
    assert build_flag_entry(_flag(rules=rules, rollout=25)) == {
        "key": "flag",
        "active": True,
        "evaluation": "local",
        "rollout_percentage": 25,
        "rules": [
            {
                "id": "dashboard",
                "rollout_percentage": 100,
                "match": {
                    "op": "or",
                    "conditions": [],
                    "groups": [
                        {
                            "op": "and",
                            "conditions": [
                                {
                                    "attribute": "user.country",
                                    "operator": "in",
                                    "value": ["US", "CA"],
                                },
                                {"attribute": "age", "operator": "gt", "value": 18},
                            ],
                            "groups": [],
                        }
                    ],
                },
            }
        ],
        "default_rule": None,
    }


@pytest.mark.parametrize(
    "status", [FeatureFlagStatus.INACTIVE, FeatureFlagStatus.ARCHIVED, "INACTIVE"]
)
def test_an_inactive_flag_ships_only_its_key(status):
    flag = _flag(rules=_native(_c("a", "eq", "x")), rollout=100, status=status)
    assert build_flag_entry(flag) == {"key": "flag", "active": False}


def test_the_raw_active_value_counts_as_active():
    assert build_flag_entry(_flag(status="ACTIVE"))["active"] is True


def test_a_legacy_list_flag_is_remote_and_ships_no_rules():
    rules = [{"type": "user_id", "user_ids": ["alice@example.com"]}]
    assert build_flag_entry(_flag(rules=rules)) == {
        "key": "flag",
        "active": True,
        "evaluation": "remote",
    }


@pytest.mark.parametrize(
    "rules",
    [
        _native(_c("v", "semantic_version", "1.0.0") | {"additional_value": "gte"}),
        _native(_c("a", "eq", "x"), _c("secret", "eq", True)),
        _native(_c("a", "contains_any", ["x"])),
    ],
    ids=["remote-operator", "remote-value", "contains_any"],
)
def test_a_remote_flag_ships_none_of_its_rule_values(rules):
    entry = build_flag_entry(_flag(rules=rules))
    assert entry == {"key": "flag", "active": True, "evaluation": "remote"}


@pytest.mark.parametrize(
    "rules",
    [None, {}, [], {"groups": []}, {"groups": [{"conditions": [_c("a", "nope", 1)]}]}],
    ids=["none", "empty-object", "empty-list", "no-groups", "unconvertible"],
)
def test_rules_the_server_ignores_ship_as_no_rules(rules):
    entry = build_flag_entry(_flag(rules=rules, rollout=40))
    assert entry["evaluation"] == "local"
    assert entry["rules"] == [] and entry["default_rule"] is None
    assert entry["rollout_percentage"] == 40


def test_rules_are_listed_in_the_order_the_server_tries_them():
    rules = {
        "rules": [
            {"id": "c", "priority": 5, "rule": {"conditions": [_c("a", "eq", "x")]}},
            {"id": "a", "priority": 1, "rule": {"conditions": [_c("a", "eq", "y")]}},
            {"id": "b", "priority": 1, "rule": {"conditions": [_c("a", "eq", "z")]}},
        ]
    }
    entry = build_flag_entry(_flag(rules=rules))
    assert [rule["id"] for rule in entry["rules"]] == ["a", "b", "c"]


def test_the_default_rule_ships_without_its_conditions():
    rules = _native(_c("a", "eq", "x"))
    rules["default_rule"] = {
        "id": "fallback",
        "rollout_percentage": 30,
        "rule": {
            "conditions": [
                _c("v", "semantic_version", "1.0.0") | {"additional_value": "gte"}
            ]
        },
    }
    entry = build_flag_entry(_flag(rules=rules))
    assert entry["evaluation"] == "local"
    assert entry["default_rule"] == {"id": "fallback", "rollout_percentage": 30}


def test_presence_conditions_ship_a_null_value():
    entry = build_flag_entry(_flag(rules=_native(_c("a", "is_null", "ignored"))))
    condition = entry["rules"][0]["match"]["conditions"][0]
    assert condition == {"attribute": "a", "operator": "is_null", "value": None}


# ---------------------------------------------------------------------------
# Patterns are classified by name: never compiled, never run
# ---------------------------------------------------------------------------


def test_the_builder_never_compiles_or_runs_a_stored_pattern(monkeypatch):
    patterns = {"((", "(x+x+)+y", "[z-a]"}
    calls = []

    def spy(name, original):
        def wrapper(pattern, *args, **kwargs):
            if pattern in patterns:
                calls.append((name, pattern))
            return original(pattern, *args, **kwargs)

        return wrapper

    monkeypatch.setattr(re2, "compile", spy("re2.compile", re2.compile))
    monkeypatch.setattr(re, "compile", spy("re.compile", re.compile))
    monkeypatch.setattr(re, "match", spy("re.match", re.match))
    monkeypatch.setattr(re, "search", spy("re.search", re.search))
    monkeypatch.setattr(
        pattern_match, "search", spy("pattern_match.search", pattern_match.search)
    )
    monkeypatch.setattr(
        pattern_match, "evaluate", spy("pattern_match.evaluate", pattern_match.evaluate)
    )
    monkeypatch.setattr(
        pattern_match,
        "compile_error",
        spy("pattern_match.compile_error", pattern_match.compile_error),
    )

    flags = [
        _flag(
            key=f"p{index}",
            rules={
                "groups": [{"conditions": [_c("email", "regex", pattern)]}],
            },
        )
        for index, pattern in enumerate(sorted(patterns))
    ] + [_flag(key="native", rules=_native(_c("email", "match_regex", "((")))]
    ruleset = build_ruleset(flags)
    assert calls == []
    assert {entry["evaluation"] for entry in ruleset["flags"]} == {"remote"}


# ---------------------------------------------------------------------------
# What is left out, and the version
# ---------------------------------------------------------------------------

ENTRY_KEYS = {
    "key",
    "active",
    "evaluation",
    "rollout_percentage",
    "rules",
    "default_rule",
}


def test_nothing_but_evaluation_fields_is_included():
    flag = _flag(
        rules=_native(_c("a", "eq", "x")),
        name="SENTINEL-NAME",
        description="SENTINEL-DESCRIPTION",
        owner_id=uuid.uuid4(),
        tags=["SENTINEL-TAG"],
        variants={"on": "SENTINEL-VARIANT"},
    )
    ruleset = build_ruleset([flag])
    assert set(ruleset) == {"schema", "version", "bucketing", "flags"}
    assert set(ruleset["flags"][0]) <= ENTRY_KEYS
    text = sdk_ruleset.canonical_json(ruleset)
    for sentinel in ("SENTINEL", str(flag.owner_id)):
        assert sentinel not in text


def _ruleset_version(*flags):
    return build_ruleset(flags)["version"]


def test_the_version_is_stable_and_order_free():
    a = _flag(key="a", rules=_native(_c("x", "eq", "1")), rollout=10)
    b = _flag(key="b", rollout=20)
    assert _ruleset_version(a, b) == _ruleset_version(b, a)
    assert len(_ruleset_version(a)) == 64


@pytest.mark.parametrize(
    "change",
    [
        {"rollout": 11},
        {"rules": _native(_c("x", "eq", "2"))},
        {"status": FeatureFlagStatus.INACTIVE},
        {"key": "a2"},
    ],
    ids=["rollout", "rules", "status", "key"],
)
def test_the_version_moves_with_anything_evaluation_reads(change):
    base = {"key": "a", "rules": _native(_c("x", "eq", "1")), "rollout": 10}
    assert _ruleset_version(_flag(**base)) != _ruleset_version(
        _flag(**{**base, **change})
    )


def test_the_version_ignores_what_evaluation_does_not_read():
    base = {"key": "a", "rules": _native(_c("x", "eq", "1")), "rollout": 10}
    assert _ruleset_version(_flag(**base)) == _ruleset_version(
        _flag(**base, name="renamed", description="new text", tags=["t"])
    )


def test_a_deleted_flag_moves_the_version():
    a = _flag(key="a")
    b = _flag(key="b")
    assert _ruleset_version(a, b) != _ruleset_version(a)
