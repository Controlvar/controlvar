"""
Unit tests for backend.app.core.targeting_adapter.

Covers the dashboard -> engine operator mapping (every operator), the
dotted-key / prefix-alias context expansion, numeric coercion, legacy list
passthrough (``None``) and native ``TargetingRules`` passthrough.
"""

import pytest

from backend.app.core.rules_engine import apply_operator, evaluate_condition
from backend.app.core.targeting_adapter import (
    DASHBOARD_OPERATORS,
    DASHBOARD_RULE_ID,
    TargetingRuleShapeError,
    coerce_number,
    convert_dashboard_condition,
    expand_context,
    match_targeting_rule,
    normalise_targeting_rules,
)
from backend.app.schemas.targeting_rule import (
    Condition,
    LogicalOperator,
    OperatorType,
    TargetingRules,
)


def dashboard(groups, logical_operator="AND", **extra):
    """Build a dashboard-shape rules dict from ``[(logical_operator, [conditions])]``."""
    return {
        "logical_operator": logical_operator,
        "groups": [
            {"logical_operator": op, "conditions": conditions}
            for op, conditions in groups
        ],
        **extra,
    }


def cond(attribute, operator, value=None):
    return {"attribute": attribute, "operator": operator, "value": value}


def matches(raw, context) -> bool:
    return (
        match_targeting_rule(normalise_targeting_rules(raw), expand_context(context))
        is not None
    )


# ---------------------------------------------------------------------------
# Shape detection
# ---------------------------------------------------------------------------


class TestNormaliseShapes:
    def test_none_and_empty_return_none(self):
        assert normalise_targeting_rules(None) is None
        assert normalise_targeting_rules({}) is None
        assert normalise_targeting_rules([]) is None
        assert normalise_targeting_rules("") is None

    def test_legacy_list_returns_none(self):
        legacy = [
            {"type": "user_id", "user_ids": ["a"], "percentage": 100},
            {
                "type": "context",
                "conditions": [
                    {"attribute": "country", "operator": "eq", "value": "US"}
                ],
            },
        ]
        assert normalise_targeting_rules(legacy) is None

    def test_unknown_dict_shape_returns_none(self):
        # The old unit-test fixture shape: attribute -> allowed values
        assert (
            normalise_targeting_rules({"country": ["US", "CA"], "user_group": "beta"})
            is None
        )

    def test_native_shape_passthrough(self):
        raw = {
            "version": "1.0",
            "rules": [
                {
                    "id": "us-premium",
                    "rule": {
                        "operator": "and",
                        "conditions": [
                            {"attribute": "country", "operator": "eq", "value": "US"},
                            {"attribute": "tier", "operator": "eq", "value": "premium"},
                        ],
                    },
                    "rollout_percentage": 40,
                    "priority": 2,
                }
            ],
        }
        rules = normalise_targeting_rules(raw)
        assert isinstance(rules, TargetingRules)
        assert rules.rules[0].id == "us-premium"
        assert rules.rules[0].rollout_percentage == 40
        assert rules.rules[0].rule.conditions[0].operator == OperatorType.EQUALS

    def test_native_instance_passthrough(self):
        rules = TargetingRules(rules=[])
        assert normalise_targeting_rules(rules) is rules

    def test_native_with_empty_rules_is_empty_ruleset(self):
        # seed_demo_data's new_dashboard_ui shape
        rules = normalise_targeting_rules({"operator": "and", "rules": []})
        assert rules is not None and rules.rules == []
        assert match_targeting_rule(rules, {"anything": 1}) is None

    def test_invalid_native_returns_none(self):
        # conditions where TargetingRule objects are expected
        raw = {"rules": [{"attribute": "x", "operator": "in", "value": [1]}]}
        assert normalise_targeting_rules(raw) is None

    def test_dashboard_shape_produces_single_rule(self):
        raw = dashboard(
            [("AND", [cond("os", "equals", "iOS"), cond("tier", "equals", "premium")])],
            logical_operator="OR",
        )
        rules = normalise_targeting_rules(raw)
        assert len(rules.rules) == 1
        rule = rules.rules[0]
        assert rule.id == DASHBOARD_RULE_ID
        assert rule.rollout_percentage == 100
        assert rule.rule.operator == LogicalOperator.OR
        assert rule.rule.conditions == []
        assert len(rule.rule.groups) == 1
        group = rule.rule.groups[0]
        assert group.operator == LogicalOperator.AND
        assert [c.attribute for c in group.conditions] == ["os", "tier"]

    def test_dashboard_rollout_percentage_and_id_are_honoured(self):
        raw = dashboard(
            [("AND", [cond("a", "equals", "1")])], id="custom", rollout_percentage="25"
        )
        rule = normalise_targeting_rules(raw).rules[0]
        assert rule.id == "custom"
        assert rule.rollout_percentage == 25

    def test_dashboard_empty_groups_means_no_targeting(self):
        rules = normalise_targeting_rules({"logical_operator": "AND", "groups": []})
        assert rules is not None and rules.rules == []

    def test_dashboard_groups_without_conditions_are_dropped(self):
        raw = dashboard(
            [("AND", []), ("AND", [cond("a", "equals", "1")])], logical_operator="OR"
        )
        rule = normalise_targeting_rules(raw).rules[0]
        assert len(rule.rule.groups) == 1
        # An empty group must not match everyone under OR
        assert matches(raw, {"a": "2"}) is False
        assert matches(raw, {"a": "1"}) is True

    def test_dashboard_invalid_condition_returns_none(self):
        assert (
            normalise_targeting_rules(
                dashboard([("AND", [cond("a", "bogus_op", "1")])])
            )
            is None
        )
        assert (
            normalise_targeting_rules(dashboard([("AND", [cond("", "equals", "1")])]))
            is None
        )
        assert normalise_targeting_rules(dashboard([("AND", ["not-a-dict"])])) is None

    def test_top_level_or_and_group_and(self):
        raw = dashboard(
            [
                ("AND", [cond("user.role", "in", ["beta", "internal"])]),
                ("AND", [cond("user.email", "ends_with", "@acme.com")]),
            ],
            logical_operator="OR",
        )
        assert matches(raw, {"role": "beta"}) is True
        assert matches(raw, {"email": "eve@acme.com"}) is True
        assert matches(raw, {"role": "free", "email": "eve@other.com"}) is False
        assert matches(raw, {}) is False


# ---------------------------------------------------------------------------
# Operator mapping — every dashboard operator
# ---------------------------------------------------------------------------


class TestOperatorMapping:
    def test_every_dashboard_operator_is_covered(self):
        expected = {
            "equals",
            "not_equals",
            "contains",
            "not_contains",
            "starts_with",
            "ends_with",
            "greater_than",
            "less_than",
            "greater_than_or_equal",
            "less_than_or_equal",
            "in",
            "not_in",
            "regex",
            "is_null",
            "is_not_null",
            "semver_eq",
            "semver_gt",
            "semver_lt",
            "semver_gte",
            "semver_lte",
            "geo_within_radius",
            "time_window",
            "array_contains",
            "array_intersects",
            "in_segment",
            "not_in_segment",
        }
        assert set(DASHBOARD_OPERATORS) == expected

    @pytest.mark.parametrize(
        "dashboard_op, engine_op",
        [
            ("equals", OperatorType.EQUALS),
            ("not_equals", OperatorType.NOT_EQUALS),
            ("contains", OperatorType.CONTAINS),
            ("not_contains", OperatorType.NOT_CONTAINS),
            ("starts_with", OperatorType.STARTS_WITH),
            ("ends_with", OperatorType.ENDS_WITH),
            ("greater_than", OperatorType.GREATER_THAN),
            ("less_than", OperatorType.LESS_THAN),
            ("greater_than_or_equal", OperatorType.GREATER_THAN_OR_EQUAL),
            ("less_than_or_equal", OperatorType.LESS_THAN_OR_EQUAL),
            ("in", OperatorType.IN),
            ("not_in", OperatorType.NOT_IN),
            ("regex", OperatorType.MATCH_REGEX),
            ("is_null", OperatorType.IS_NULL),
            ("is_not_null", OperatorType.IS_NOT_NULL),
            ("geo_within_radius", OperatorType.GEO_DISTANCE),
            ("time_window", OperatorType.TIME_WINDOW),
            ("array_contains", OperatorType.CONTAINS_ALL),
            ("array_intersects", OperatorType.CONTAINS_ANY),
        ],
    )
    def test_simple_operator_map(self, dashboard_op, engine_op):
        value = {
            "greater_than": "5",
            "less_than": "5",
            "greater_than_or_equal": "5",
            "less_than_or_equal": "5",
            "in": ["a"],
            "not_in": ["a"],
            "regex": "^a",
            "geo_within_radius": {"lat": 1, "lon": 2, "radius": 3},
            "time_window": {"start": "09:00", "end": "17:00"},
            "array_contains": ["a"],
            "array_intersects": ["a"],
        }.get(dashboard_op, "x")
        converted = convert_dashboard_condition(cond("attr", dashboard_op, value))
        assert converted.operator == engine_op
        assert converted.attribute == "attr"

    @pytest.mark.parametrize(
        "dashboard_op, comparison",
        [
            ("semver_eq", "eq"),
            ("semver_gt", "gt"),
            ("semver_lt", "lt"),
            ("semver_gte", "gte"),
            ("semver_lte", "lte"),
        ],
    )
    def test_semver_operators_map_to_semantic_version_with_additional_value(
        self, dashboard_op, comparison
    ):
        converted = convert_dashboard_condition(
            cond("app.version", dashboard_op, "3.2.1")
        )
        assert converted.operator == OperatorType.SEMANTIC_VERSION
        assert converted.value == "3.2.1"
        assert converted.additional_value == comparison

    def test_semver_short_versions_are_padded(self):
        assert (
            convert_dashboard_condition(cond("v", "semver_gte", "17")).value == "17.0.0"
        )
        assert (
            convert_dashboard_condition(cond("v", "semver_gte", "17.4")).value
            == "17.4.0"
        )
        assert (
            convert_dashboard_condition(cond("v", "semver_gte", "v1.2.3")).value
            == "1.2.3"
        )
        assert (
            convert_dashboard_condition(cond("v", "semver_gte", "1.2.3-beta.1")).value
            == "1.2.3-beta.1"
        )

    def test_semver_invalid_value_raises(self):
        with pytest.raises(TargetingRuleShapeError):
            convert_dashboard_condition(cond("v", "semver_gte", "seventeen"))
        with pytest.raises(TargetingRuleShapeError):
            convert_dashboard_condition(cond("v", "semver_gte", ""))

    def test_semver_gte_evaluates_against_context(self):
        raw = dashboard([("AND", [cond("os_version", "semver_gte", "17.0.0")])])
        assert matches(raw, {"os_version": "17.4.0"}) is True
        assert matches(raw, {"os_version": "17.0.0"}) is True
        assert matches(raw, {"os_version": "16.7.0"}) is False
        assert matches(raw, {}) is False

    def test_is_null_and_is_not_null(self):
        converted = convert_dashboard_condition(cond("email", "is_null", "ignored"))
        assert converted.value is None

        is_null = dashboard([("AND", [cond("email", "is_null")])])
        assert matches(is_null, {}) is True  # attribute absent
        assert matches(is_null, {"email": None}) is True
        assert matches(is_null, {"email": ""}) is True
        assert matches(is_null, {"email": "a@b.c"}) is False

        is_not_null = dashboard([("AND", [cond("email", "is_not_null")])])
        assert matches(is_not_null, {}) is False
        assert matches(is_not_null, {"email": None}) is False
        assert matches(is_not_null, {"email": "a@b.c"}) is True

    def test_in_accepts_list_scalar_and_comma_string(self):
        assert convert_dashboard_condition(cond("r", "in", ["a", "b"])).value == [
            "a",
            "b",
        ]
        assert convert_dashboard_condition(cond("r", "in", "a")).value == ["a"]
        assert convert_dashboard_condition(
            cond("r", "in", "beta, internal ,")
        ).value == ["beta", "internal"]
        assert convert_dashboard_condition(cond("r", "in", '["x", "y"]')).value == [
            "x",
            "y",
        ]
        assert convert_dashboard_condition(cond("r", "in", 7)).value == [7]
        with pytest.raises(TargetingRuleShapeError):
            convert_dashboard_condition(cond("r", "in", ""))
        with pytest.raises(TargetingRuleShapeError):
            convert_dashboard_condition(cond("r", "in", None))

    def test_in_and_not_in_evaluate(self):
        raw = dashboard([("AND", [cond("region", "in", "US, GB")])])
        assert matches(raw, {"region": "US"}) is True
        assert matches(raw, {"region": "DE"}) is False
        raw = dashboard([("AND", [cond("region", "not_in", ["US", "GB"])])])
        assert matches(raw, {"region": "DE"}) is True
        assert matches(raw, {"region": "US"}) is False

    def test_array_contains_wraps_scalar_and_maps_to_contains_all(self):
        converted = convert_dashboard_condition(cond("tags", "array_contains", "beta"))
        assert converted.operator == OperatorType.CONTAINS_ALL
        assert converted.value == ["beta"]
        raw = dashboard([("AND", [cond("tags", "array_contains", "beta, vip")])])
        assert matches(raw, {"tags": ["beta", "vip", "x"]}) is True
        assert matches(raw, {"tags": ["beta"]}) is False

    def test_array_intersects_maps_to_contains_any(self):
        raw = dashboard([("AND", [cond("tags", "array_intersects", ["beta", "vip"])])])
        assert matches(raw, {"tags": ["vip"]}) is True
        assert matches(raw, {"tags": ["x"]}) is False

    def test_string_operators(self):
        assert matches(
            dashboard([("AND", [cond("email", "contains", "acme")])]),
            {"email": "a@acme.com"},
        )
        assert not matches(
            dashboard([("AND", [cond("email", "not_contains", "acme")])]),
            {"email": "a@acme.com"},
        )
        assert matches(
            dashboard([("AND", [cond("email", "starts_with", "a@")])]),
            {"email": "a@acme.com"},
        )
        assert matches(
            dashboard([("AND", [cond("email", "ends_with", "@acme.com")])]),
            {"email": "a@acme.com"},
        )
        assert matches(
            dashboard([("AND", [cond("email", "regex", r"^[a-z]+@acme\.com$")])]),
            {"email": "a@acme.com"},
        )
        assert not matches(
            dashboard([("AND", [cond("email", "regex", r"^\d+$")])]),
            {"email": "a@acme.com"},
        )

    def test_regex_requires_pattern(self):
        with pytest.raises(TargetingRuleShapeError):
            convert_dashboard_condition(cond("e", "regex", ""))

    def test_equals_not_equals(self):
        assert matches(
            dashboard([("AND", [cond("os", "equals", "iOS")])]), {"os": "iOS"}
        )
        assert not matches(
            dashboard([("AND", [cond("os", "equals", "iOS")])]), {"os": "Android"}
        )
        assert matches(
            dashboard([("AND", [cond("os", "not_equals", "iOS")])]), {"os": "Android"}
        )
        assert not matches(
            dashboard([("AND", [cond("os", "not_equals", "iOS")])]), {"os": "iOS"}
        )

    def test_equals_editor_string_matches_typed_context(self):
        # The editor stores "true"/"17"; SDKs send True/17.
        assert matches(
            dashboard([("AND", [cond("employee", "equals", "true")])]),
            {"employee": True},
        )
        assert not matches(
            dashboard([("AND", [cond("employee", "equals", "true")])]),
            {"employee": False},
        )
        assert matches(
            dashboard([("AND", [cond("employee", "equals", "false")])]),
            {"employee": False},
        )
        assert matches(dashboard([("AND", [cond("age", "equals", "17")])]), {"age": 17})
        assert not matches(
            dashboard([("AND", [cond("age", "equals", "17")])]), {"age": 18}
        )
        assert matches(
            dashboard([("AND", [cond("employee", "equals", True)])]), {"employee": True}
        )
        # two strings are never coerced
        assert not matches(
            dashboard([("AND", [cond("flag", "equals", "1")])]), {"flag": "true"}
        )

    def test_geo_within_radius_shapes(self):
        for value in (
            {"lat": 37.7749, "lon": -122.4194, "radius": 50, "unit": "km"},
            [37.7749, -122.4194, 50, "km"],
            "37.7749,-122.4194,50,km",
        ):
            converted = convert_dashboard_condition(
                cond("location", "geo_within_radius", value)
            )
            assert converted.operator == OperatorType.GEO_DISTANCE
            assert converted.value == [37.7749, -122.4194]
            assert converted.additional_value == {"radius": 50.0, "unit": "km"}

        raw = dashboard(
            [
                (
                    "AND",
                    [cond("location", "geo_within_radius", "37.7749,-122.4194,50,km")],
                )
            ]
        )
        assert matches(raw, {"location": {"lat": 37.78, "lon": -122.41}}) is True
        assert matches(raw, {"location": [34.05, -118.24]}) is False

    def test_geo_within_radius_invalid(self):
        with pytest.raises(TargetingRuleShapeError):
            convert_dashboard_condition(cond("location", "geo_within_radius", "1,2"))
        with pytest.raises(TargetingRuleShapeError):
            convert_dashboard_condition(
                cond(
                    "location", "geo_within_radius", {"lat": "x", "lon": 1, "radius": 1}
                )
            )

    def test_time_window_keeps_both_spellings(self):
        converted = convert_dashboard_condition(
            cond("now", "time_window", {"start": "09:00", "end": "17:00"})
        )
        assert converted.value["start_time"] == "09:00"
        assert converted.value["end_time"] == "17:00"
        converted = convert_dashboard_condition(
            cond(
                "now",
                "time_window",
                '{"start_time": "09:00", "end_time": "17:00", "days": [0,1,2,3,4]}',
            )
        )
        assert converted.value["start"] == "09:00"
        assert converted.value["days"] == [0, 1, 2, 3, 4]
        with pytest.raises(TargetingRuleShapeError):
            convert_dashboard_condition(cond("now", "time_window", {"days": [1]}))
        with pytest.raises(TargetingRuleShapeError):
            convert_dashboard_condition(cond("now", "time_window", "not json"))

    def test_time_window_evaluates(self):
        raw = dashboard(
            [("AND", [cond("now", "time_window", {"start": "09:00", "end": "17:00"})])]
        )
        assert matches(raw, {"now": "2026-01-05T12:00:00"}) is True
        assert matches(raw, {"now": "2026-01-05T20:00:00"}) is False


# ---------------------------------------------------------------------------
# Numeric coercion
# ---------------------------------------------------------------------------


class TestNumericCoercion:
    def test_coerce_number(self):
        assert coerce_number("17") == 17 and isinstance(coerce_number("17"), int)
        assert coerce_number("17.5") == 17.5
        assert coerce_number(" 3 ") == 3
        assert coerce_number("abc") == "abc"
        assert coerce_number(True) is True
        assert coerce_number(None) is None
        assert coerce_number(2.0) == 2.0

    @pytest.mark.parametrize(
        "op",
        ["greater_than", "less_than", "greater_than_or_equal", "less_than_or_equal"],
    )
    def test_comparison_values_are_coerced(self, op):
        converted = convert_dashboard_condition(cond("age", op, "17"))
        assert converted.value == 17 and not isinstance(converted.value, str)
        with pytest.raises(TargetingRuleShapeError):
            convert_dashboard_condition(cond("age", op, "seventeen"))

    def test_comparisons_evaluate_against_numeric_context(self):
        assert matches(
            dashboard([("AND", [cond("age", "greater_than", "17")])]), {"age": 18}
        )
        assert not matches(
            dashboard([("AND", [cond("age", "greater_than", "17")])]), {"age": 17}
        )
        assert matches(
            dashboard([("AND", [cond("age", "greater_than_or_equal", "17")])]),
            {"age": 17},
        )
        assert matches(
            dashboard([("AND", [cond("age", "less_than", "17")])]), {"age": 16.5}
        )
        assert matches(
            dashboard([("AND", [cond("age", "less_than_or_equal", "17")])]), {"age": 17}
        )
        # numeric strings in the context still compare numerically
        assert matches(
            dashboard([("AND", [cond("age", "greater_than", "9")])]), {"age": "10"}
        )
        assert not matches(
            dashboard([("AND", [cond("age", "greater_than", "9")])]), {"age": "abc"}
        )


# ---------------------------------------------------------------------------
# Context expansion
# ---------------------------------------------------------------------------


class TestExpandContext:
    def test_empty(self):
        assert expand_context(None) == {}
        assert expand_context({}) == {}
        assert expand_context("nope") == {}

    def test_raw_keys_kept(self):
        ctx = expand_context({"country": "US", "age": 30})
        assert ctx["country"] == "US"
        assert ctx["age"] == 30

    def test_dotted_keys_for_nested_dicts(self):
        ctx = expand_context({"app": {"version": "3.2.1", "build": {"number": 7}}})
        assert ctx["app.version"] == "3.2.1"
        assert ctx["app.build.number"] == 7
        assert ctx["app"] == {"version": "3.2.1", "build": {"number": 7}}

    def test_prefix_aliases_for_top_level_keys(self):
        ctx = expand_context(
            {"country": "US", "os_version": "17.4.0", "version": "3.2.1"}
        )
        assert ctx["user.country"] == "US"
        assert ctx["device.os_version"] == "17.4.0"
        assert ctx["app.version"] == "3.2.1"
        assert ctx["device.country"] == "US"

    def test_reverse_alias_for_prefixed_keys(self):
        ctx = expand_context({"user": {"country": "DE"}})
        assert ctx["user.country"] == "DE"
        assert ctx["country"] == "DE"

    def test_explicit_keys_win_over_aliases(self):
        ctx = expand_context({"country": "US", "user": {"country": "DE"}})
        assert ctx["country"] == "US"
        assert ctx["user.country"] == "DE"

    def test_nested_dicts_are_not_aliased_themselves(self):
        ctx = expand_context({"app": {"version": "1.0.0"}})
        assert "user.app" not in ctx
        assert "app.app" not in ctx

    def test_dashboard_dotted_attribute_matches_flat_context(self):
        raw = dashboard(
            [
                (
                    "AND",
                    [
                        cond("user.country", "equals", "US"),
                        cond("app.version", "semver_gte", "3.2.0"),
                    ],
                )
            ]
        )
        assert matches(
            raw, {"country": "US", "app_version": "3.2.1", "app": {"version": "3.2.1"}}
        )
        assert matches(raw, {"country": "US", "version": "3.2.1"})
        assert not matches(raw, {"country": "DE", "version": "3.2.1"})


# ---------------------------------------------------------------------------
# Engine additions used by the adapter
# ---------------------------------------------------------------------------


class TestEngineExtensions:
    def test_is_null_operator_in_engine(self):
        assert apply_operator(OperatorType.IS_NULL, None, None) is True
        assert apply_operator(OperatorType.IS_NULL, "", None) is True
        assert apply_operator(OperatorType.IS_NULL, [], None) is True
        assert apply_operator(OperatorType.IS_NULL, 0, None) is False
        assert apply_operator(OperatorType.IS_NULL, False, None) is False
        assert apply_operator(OperatorType.IS_NOT_NULL, "x", None) is True
        assert apply_operator(OperatorType.IS_NOT_NULL, None, None) is False

    def test_is_null_with_missing_attribute(self):
        assert (
            evaluate_condition(
                Condition(attribute="x", operator=OperatorType.IS_NULL, value=None), {}
            )
            is True
        )
        assert (
            evaluate_condition(
                Condition(attribute="x", operator=OperatorType.IS_NOT_NULL, value=None),
                {},
            )
            is False
        )

    def test_eq_none_alone_does_not_match_missing_attribute(self):
        # Documents why IS_NULL exists: eq None needs the key to be present.
        assert (
            evaluate_condition(
                Condition(attribute="x", operator=OperatorType.EQUALS, value=None), {}
            )
            is False
        )
        assert (
            evaluate_condition(
                Condition(attribute="x", operator=OperatorType.EQUALS, value=None),
                {"x": None},
            )
            is True
        )

    def test_lenient_equality_only_between_string_and_typed(self):
        assert apply_operator(OperatorType.EQUALS, True, "true") is True
        assert apply_operator(OperatorType.EQUALS, "true", True) is True
        assert apply_operator(OperatorType.EQUALS, 17, "17") is True
        assert apply_operator(OperatorType.EQUALS, 17.0, "17") is True
        assert apply_operator(OperatorType.EQUALS, 17, "18") is False
        assert apply_operator(OperatorType.EQUALS, "17", "17.0") is False
        assert apply_operator(OperatorType.NOT_EQUALS, True, "true") is False
        assert apply_operator(OperatorType.IN, 17, ["16", "17"]) is True
        assert apply_operator(OperatorType.NOT_IN, 17, ["16", "17"]) is False

    def test_geo_distance_accepts_schema_shape(self):
        # value=[lat, lon] + additional_value radius is what the Condition schema validates
        assert (
            apply_operator(
                OperatorType.GEO_DISTANCE,
                {"lat": 37.78, "lon": -122.41},
                [37.7749, -122.4194],
                50,
            )
            is True
        )
        assert (
            apply_operator(
                OperatorType.GEO_DISTANCE,
                {"lat": 34.05, "lon": -118.24},
                [37.7749, -122.4194],
                {"radius": 50, "unit": "km"},
            )
            is False
        )


def test_the_dashboard_offers_more_than_20_operators_including_semver_geo_array_and_time():
    """The demo guide's "20+ targeting operators: semver, geo, array, time windows"."""
    assert len(DASHBOARD_OPERATORS) > 20, sorted(DASHBOARD_OPERATORS)
    for operator in (
        "semver_gte",
        "geo_within_radius",
        "array_contains",
        "time_window",
    ):
        assert operator in DASHBOARD_OPERATORS, operator
