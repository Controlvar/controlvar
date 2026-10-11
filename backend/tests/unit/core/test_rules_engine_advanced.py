"""
Advanced unit tests for the enhanced rules engine with custom attribute targeting.

This module tests the new features including:
- Custom attribute validation
- Advanced operators (semantic version, geo distance, etc.)
- Performance monitoring
- Integration with assignment service
"""

import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from unittest.mock import Mock, patch

import pytest

from backend.app.core.rule_validation import RuleValidator, ValidationSeverity
from backend.app.schemas.targeting_rule import (
    AttributeType,
    Condition,
    LogicalOperator,
    OperatorType,
    RuleGroup,
    TargetingRule,
    TargetingRules,
)
from backend.app.services.rules_evaluation_service import (
    AttributeValidationResult,
    RuleEvaluationMetrics,
    RulesEvaluationService,
)


class TestAdvancedOperators:
    """Tests for advanced custom attribute operators."""

    def setup_method(self):
        """Setup test data."""
        self.service = RulesEvaluationService()

    @pytest.mark.regression
    def test_semantic_version_operator(self):
        """semantic_version follows its comparison, as on flags (#1144).

        This test used to pin the service's own comparison, which ignored
        ``additional_value`` and answered ">=" for every comparison.
        """

        def semver(actual, expected, comparison):
            return self.service._apply_operator_enhanced(
                OperatorType.SEMANTIC_VERSION, actual, expected, comparison
            )

        # gte
        assert semver("1.2.3", "1.2.0", "gte") is True
        assert semver("2.0.0", "1.9.9", "gte") is True
        assert semver("1.2.3", "1.2.3", "gte") is True
        assert semver("1.1.0", "1.2.0", "gte") is False
        assert semver("0.9.9", "1.0.0", "gte") is False

        # The other comparisons, each of which used to answer as gte
        assert semver("3.0.0", "2.0.0", "lt") is False
        assert semver("1.5.0", "2.0.0", "lt") is True
        assert semver("9.9.9", "2.0.0", "lte") is False
        assert semver("2.0.0", "2.0.0", "lte") is True
        assert semver("2.1.0", "2.0.0", "eq") is False
        assert semver("2.0.0", "2.0.0", "eq") is True
        assert semver("2.0.0", "2.0.0", "gt") is False
        assert semver("2.1.0", "2.0.0", "gt") is True

        # No comparison means eq, as on flags
        assert semver("2.1.0", "2.0.0", None) is False
        assert semver("2.0.0", "2.0.0", None) is True

        # A pre-release comes before its release (semver.org, item 11)
        assert semver("1.2.3-beta.1", "1.2.3", "gte") is False
        assert semver("1.2.3", "1.2.3-alpha.1", "gte") is True

    @pytest.mark.regression
    def test_geo_distance_operator(self):
        """geo_distance is the rules engine's, as on flags (#1144)."""

        def geo(actual, target, radius):
            return self.service._apply_operator_enhanced(
                OperatorType.GEO_DISTANCE, actual, target, radius
            )

        # New York coordinates
        ny_coords = [40.7128, -74.0060]
        # Los Angeles coordinates
        la_coords = [34.0522, -118.2437]

        # Test within distance (should be false - NYC to LA is ~2500 miles)
        assert geo(ny_coords, la_coords, 1000) is False

        # Test close coordinates (within 1km)
        close_coords = [40.7130, -74.0058]  # Very close to NYC
        assert geo(ny_coords, close_coords, 1) is True

        # Test same coordinates
        assert geo(ny_coords, ny_coords, 0) is True

        # The dashboard's geo_within_radius stores the radius as an object and
        # SDKs send a location as {"lat", "lon"}; neither used to match.
        assert geo({"lat": 40.7130, "lon": -74.0058}, ny_coords, {"radius": 1}) is True
        assert (
            geo({"lat": 34.0522, "lon": -118.2437}, ny_coords, {"radius": 1}) is False
        )

    @pytest.mark.regression
    def test_time_window_operator(self):
        """time_window is the rules engine's, as on flags (#1145).

        This test used to pin the service's own reading, ISO datetimes in
        ``start``/``end``, which flags never read. A window with none of the
        keys the engine reads now matches nobody, on experiments as on flags.
        """

        def window(moment, value):
            return self.service._apply_operator_enhanced(
                OperatorType.TIME_WINDOW, moment, value
            )

        moment = datetime(2024, 1, 2, 10, 0, 0)  # a Tuesday
        business_hours = {
            "start": "09:00",
            "end": "17:00",
            "start_time": "09:00",
            "end_time": "17:00",
        }

        # Inside and outside the hours
        assert window(moment.isoformat(), business_hours) is True
        assert (
            window((moment - timedelta(hours=2)).isoformat(), business_hours) is False
        )
        assert window(moment, {**business_hours, "days": [1]}) is True
        assert window(moment, {**business_hours, "days": [0]}) is False

        # A timestamp, and a date range
        january = {
            "start": "x",
            "end": "y",
            "start_date": "2024-01-01",
            "end_date": "2024-01-31",
        }
        assert window(moment.timestamp(), january) is True
        assert window((moment + timedelta(days=40)).timestamp(), january) is False

        # ISO start/end, what this test used to pin as a match (#1156)
        iso = {
            "start": (moment - timedelta(hours=1)).isoformat(),
            "end": (moment + timedelta(hours=1)).isoformat(),
        }
        assert window(moment.isoformat(), iso) is False

    def test_percentage_bucket_operator(self):
        """Test percentage bucket operator."""
        # Test deterministic bucketing
        user_id = "test-user-123"

        # Test with 50% - should be consistent
        result1 = self.service._evaluate_percentage_bucket(user_id, 50)
        result2 = self.service._evaluate_percentage_bucket(user_id, 50)
        assert result1 == result2  # Should be deterministic

        # Test with 0% and 100%
        assert self.service._evaluate_percentage_bucket(user_id, 0) is False
        assert self.service._evaluate_percentage_bucket(user_id, 100) is True

    def test_json_path_operator(self):
        """Test JSON path operator."""
        json_data = {
            "user": {
                "profile": {
                    "tier": "premium",
                    "preferences": ["feature_a", "feature_b"],
                }
            }
        }

        # Test simple path
        assert (
            self.service._evaluate_json_path(
                json_data, "$.user.profile.tier", "premium"
            )
            is True
        )
        assert (
            self.service._evaluate_json_path(json_data, "$.user.profile.tier", "basic")
            is False
        )

        # Test non-existent path
        assert (
            self.service._evaluate_json_path(json_data, "$.user.nonexistent", "value")
            is False
        )

        # Test with JSON string
        json_string = json.dumps(json_data)
        assert (
            self.service._evaluate_json_path(
                json_string, "$.user.profile.tier", "premium"
            )
            is True
        )

    def test_array_length_operator(self):
        """Test array length operator."""
        # Test array length comparison
        assert self.service._evaluate_array_length([1, 2, 3], 3) is True
        assert self.service._evaluate_array_length([1, 2, 3], 2) is False
        assert self.service._evaluate_array_length([], 0) is True

        # Test with non-array
        assert self.service._evaluate_array_length("not an array", 5) is False


class TestAttributeValidation:
    """Tests for custom attribute validation."""

    def setup_method(self):
        """Setup test data."""
        self.service = RulesEvaluationService()

    def test_string_attribute_validation(self):
        """Test string attribute validation."""
        result = self.service._validate_attribute_value(
            "name", "John Doe", AttributeType.STRING
        )
        assert result.is_valid is True

        # Test non-string value
        result = self.service._validate_attribute_value(
            "name", 123, AttributeType.STRING
        )
        assert result.is_valid is False
        assert "must be a string" in result.error_message

    def test_number_attribute_validation(self):
        """Test number attribute validation."""
        # Test valid numbers
        result = self.service._validate_attribute_value("age", 25, AttributeType.NUMBER)
        assert result.is_valid is True

        result = self.service._validate_attribute_value(
            "score", 98.5, AttributeType.NUMBER
        )
        assert result.is_valid is True

        # Test string that can be converted to number
        result = self.service._validate_attribute_value(
            "count", "42", AttributeType.NUMBER
        )
        assert result.is_valid is True

        # Test invalid number
        result = self.service._validate_attribute_value(
            "age", "not a number", AttributeType.NUMBER
        )
        assert result.is_valid is False

    def test_boolean_attribute_validation(self):
        """Test boolean attribute validation."""
        result = self.service._validate_attribute_value(
            "active", True, AttributeType.BOOLEAN
        )
        assert result.is_valid is True

        result = self.service._validate_attribute_value(
            "active", False, AttributeType.BOOLEAN
        )
        assert result.is_valid is True

        # Test non-boolean
        result = self.service._validate_attribute_value(
            "active", "true", AttributeType.BOOLEAN
        )
        assert result.is_valid is False

    def test_array_attribute_validation(self):
        """Test array attribute validation."""
        result = self.service._validate_attribute_value(
            "tags", ["a", "b", "c"], AttributeType.ARRAY
        )
        assert result.is_valid is True

        result = self.service._validate_attribute_value(
            "empty", [], AttributeType.ARRAY
        )
        assert result.is_valid is True

        # Test non-array
        result = self.service._validate_attribute_value(
            "tags", "not an array", AttributeType.ARRAY
        )
        assert result.is_valid is False

    def test_date_attribute_validation(self):
        """Test date attribute validation."""
        # Test ISO string
        result = self.service._validate_attribute_value(
            "created_at", "2023-01-01T00:00:00Z", AttributeType.DATE
        )
        assert result.is_valid is True

        # Test datetime object
        result = self.service._validate_attribute_value(
            "updated_at", datetime.now(), AttributeType.DATE
        )
        assert result.is_valid is True

        # Test timestamp
        result = self.service._validate_attribute_value(
            "timestamp", 1672531200, AttributeType.DATE
        )
        assert result.is_valid is True

        # Test invalid date string
        result = self.service._validate_attribute_value(
            "invalid_date", "not a date", AttributeType.DATE
        )
        assert result.is_valid is False

    def test_geo_coordinate_validation(self):
        """Test geographic coordinate validation."""
        # Test valid coordinates
        result = self.service._validate_attribute_value(
            "location", [40.7128, -74.0060], AttributeType.GEO_COORDINATE
        )
        assert result.is_valid is True

        # Test invalid format
        result = self.service._validate_attribute_value(
            "location", [40.7128], AttributeType.GEO_COORDINATE
        )
        assert result.is_valid is False

        # Test out of range coordinates
        result = self.service._validate_attribute_value(
            "location", [91, -74], AttributeType.GEO_COORDINATE
        )
        assert result.is_valid is False

        result = self.service._validate_attribute_value(
            "location", [40, -181], AttributeType.GEO_COORDINATE
        )
        assert result.is_valid is False

    def test_semantic_version_validation(self):
        """Test semantic version validation."""
        # Test valid versions
        result = self.service._validate_attribute_value(
            "version", "1.2.3", AttributeType.SEMANTIC_VERSION
        )
        assert result.is_valid is True

        result = self.service._validate_attribute_value(
            "version", "1.0.0-beta.1", AttributeType.SEMANTIC_VERSION
        )
        assert result.is_valid is True

        # Test invalid versions
        result = self.service._validate_attribute_value(
            "version", "1.2", AttributeType.SEMANTIC_VERSION
        )
        assert result.is_valid is False

        result = self.service._validate_attribute_value(
            "version", "not a version", AttributeType.SEMANTIC_VERSION
        )
        assert result.is_valid is False


class TestRulesEvaluationService:
    """Tests for the enhanced rules evaluation service."""

    def setup_method(self):
        """Setup test data."""
        self.service = RulesEvaluationService()
        self.user_context = {
            "user_id": "test-user-123",
            "country": "US",
            "subscription_tier": "premium",
            "app_version": "1.2.3",
            "location": [40.7128, -74.0060],
            "tags": ["beta", "early-adopter"],
            "active": True,
            "signup_date": "2023-01-01T00:00:00Z",
        }

    def test_enhanced_rule_evaluation(self):
        """Test evaluation with enhanced operators."""
        # Create rule with semantic version condition
        condition = Condition(
            attribute="app_version",
            operator=OperatorType.SEMANTIC_VERSION,
            value="1.2.0",
            additional_value="gte",
            attribute_type=AttributeType.SEMANTIC_VERSION,
        )

        rule_group = RuleGroup(operator=LogicalOperator.AND, conditions=[condition])

        targeting_rule = TargetingRule(
            id="version_test", rule=rule_group, rollout_percentage=100, priority=1
        )

        targeting_rules = TargetingRules(version="1.0", rules=[targeting_rule])

        # Test evaluation
        matched_rule, metrics = self.service.evaluate_rules_with_validation(
            targeting_rules, self.user_context
        )

        assert matched_rule is not None
        assert matched_rule.id == "version_test"
        assert metrics is not None
        assert metrics.matched is True

    def test_validation_with_invalid_attributes(self):
        """Test validation with invalid user context."""
        # Create invalid user context
        invalid_context = {
            "user_id": "test-user-123",
            "app_version": "invalid version",  # Invalid semantic version
        }

        condition = Condition(
            attribute="app_version",
            operator=OperatorType.SEMANTIC_VERSION,
            value="1.2.0",
            additional_value="gte",
            attribute_type=AttributeType.SEMANTIC_VERSION,
        )

        rule_group = RuleGroup(operator=LogicalOperator.AND, conditions=[condition])

        targeting_rule = TargetingRule(
            id="version_test", rule=rule_group, rollout_percentage=100, priority=1
        )

        targeting_rules = TargetingRules(version="1.0", rules=[targeting_rule])

        # Test evaluation should handle validation gracefully
        matched_rule, metrics = self.service.evaluate_rules_with_validation(
            targeting_rules, invalid_context, validate_attributes=True
        )

        # Should not match due to validation error
        assert matched_rule is None
        assert metrics is not None
        assert metrics.error is not None

    def test_performance_metrics_collection(self):
        """Test that performance metrics are collected."""
        # Clear any existing metrics
        self.service.clear_metrics()

        condition = Condition(
            attribute="country", operator=OperatorType.EQUALS, value="US"
        )

        rule_group = RuleGroup(operator=LogicalOperator.AND, conditions=[condition])

        targeting_rule = TargetingRule(
            id="simple_test", rule=rule_group, rollout_percentage=100, priority=1
        )

        targeting_rules = TargetingRules(version="1.0", rules=[targeting_rule])

        # Evaluate multiple times
        for i in range(5):
            self.service.evaluate_rules_with_validation(
                targeting_rules, self.user_context, track_metrics=True
            )

        # Check performance stats
        stats = self.service.get_performance_stats()
        assert stats["total_evaluations"] == 5
        assert stats["avg_evaluation_time_ms"] > 0
        assert stats["match_rate"] == 1.0  # All should match


class TestRuleValidator:
    """Tests for the rule validation framework."""

    def setup_method(self):
        """Setup test data."""
        self.validator = RuleValidator()

    def test_valid_rules_validation(self):
        """Test validation of valid rules."""
        condition = Condition(
            attribute="country", operator=OperatorType.EQUALS, value="US"
        )

        rule_group = RuleGroup(operator=LogicalOperator.AND, conditions=[condition])

        targeting_rule = TargetingRule(
            id="valid_rule",
            name="Valid Rule",
            rule=rule_group,
            rollout_percentage=50,
            priority=1,
        )

        targeting_rules = TargetingRules(version="1.0", rules=[targeting_rule])

        result = self.validator.validate_targeting_rules(targeting_rules)

        assert result.is_valid is True
        assert (
            len(
                [
                    issue
                    for issue in result.issues
                    if issue.severity == ValidationSeverity.ERROR
                ]
            )
            == 0
        )

    def test_invalid_rules_validation(self):
        """Test validation of invalid rules."""
        # Test invalid regex - this should pass Pydantic but fail our custom validation
        condition = Condition(
            attribute="email",
            operator=OperatorType.MATCH_REGEX,
            value="[invalid regex",  # Invalid regex
        )

        rule_group = RuleGroup(operator=LogicalOperator.AND, conditions=[condition])

        # Use valid percentage but invalid regex
        targeting_rule = TargetingRule(
            id="invalid_rule",
            rule=rule_group,
            rollout_percentage=50,  # Valid percentage
            priority=1,
        )

        targeting_rules = TargetingRules(version="1.0", rules=[targeting_rule])

        result = self.validator.validate_targeting_rules(targeting_rules)

        assert result.is_valid is False
        errors = [
            issue
            for issue in result.issues
            if issue.severity == ValidationSeverity.ERROR
        ]
        assert len(errors) > 0

        # Test that Pydantic validation also works for percentage
        with pytest.raises(Exception):
            TargetingRule(
                id="invalid_percentage",
                rule=rule_group,
                rollout_percentage=150,  # This should raise Pydantic error
                priority=1,
            )

    def test_duplicate_rule_ids_validation(self):
        """Test validation catches duplicate rule IDs."""
        condition = Condition(
            attribute="country", operator=OperatorType.EQUALS, value="US"
        )

        rule_group = RuleGroup(operator=LogicalOperator.AND, conditions=[condition])

        # Create two rules with same ID
        rule1 = TargetingRule(
            id="duplicate_id", rule=rule_group, rollout_percentage=50, priority=1
        )

        rule2 = TargetingRule(
            id="duplicate_id", rule=rule_group, rollout_percentage=50, priority=2
        )

        targeting_rules = TargetingRules(version="1.0", rules=[rule1, rule2])

        result = self.validator.validate_targeting_rules(targeting_rules)

        assert result.is_valid is False
        duplicate_errors = [
            issue for issue in result.issues if "Duplicate rule IDs" in issue.message
        ]
        assert len(duplicate_errors) > 0

    def test_performance_warning_validation(self):
        """Test validation identifies performance issues."""
        # Create rule with expensive regex
        condition = Condition(
            attribute="description",
            operator=OperatorType.MATCH_REGEX,
            value=".*complex.*regex.*pattern.*",
        )

        rule_group = RuleGroup(operator=LogicalOperator.AND, conditions=[condition])

        targeting_rule = TargetingRule(
            id="expensive_rule", rule=rule_group, rollout_percentage=100, priority=1
        )

        targeting_rules = TargetingRules(version="1.0", rules=[targeting_rule])

        result = self.validator.validate_targeting_rules(targeting_rules)

        # Should have performance warnings
        performance_warnings = [
            issue for issue in result.issues if "expensive" in issue.message.lower()
        ]
        assert len(performance_warnings) > 0

    def test_semantic_version_validation(self):
        """Test validation of semantic version operators."""
        # Valid semantic version
        valid_condition = Condition(
            attribute="app_version",
            operator=OperatorType.SEMANTIC_VERSION,
            value="1.2.3",
        )

        valid_group = RuleGroup(
            operator=LogicalOperator.AND, conditions=[valid_condition]
        )

        valid_rule = TargetingRule(
            id="valid_semver", rule=valid_group, rollout_percentage=100, priority=1
        )

        # Test valid case first
        targeting_rules = TargetingRules(version="1.0", rules=[valid_rule])

        result = self.validator.validate_targeting_rules(targeting_rules)
        assert result.is_valid is True

        # Test that Pydantic validation catches invalid semantic versions at creation time
        with pytest.raises(Exception):
            invalid_condition = Condition(
                attribute="app_version",
                operator=OperatorType.SEMANTIC_VERSION,
                value="1.2",  # Invalid format - should raise Pydantic error
            )

    def test_geo_coordinates_validation(self):
        """Test validation of geographic coordinates."""
        # Valid coordinates
        valid_condition = Condition(
            attribute="location",
            operator=OperatorType.GEO_DISTANCE,
            value=[40.7128, -74.0060],
            additional_value=10,  # 10km radius
        )

        valid_group = RuleGroup(
            operator=LogicalOperator.AND, conditions=[valid_condition]
        )

        valid_rule = TargetingRule(
            id="valid_geo", rule=valid_group, rollout_percentage=100, priority=1
        )

        # Test valid case first
        targeting_rules = TargetingRules(version="1.0", rules=[valid_rule])

        result = self.validator.validate_targeting_rules(targeting_rules)
        assert result.is_valid is True

        # Test that Pydantic validation catches invalid coordinates at creation time
        with pytest.raises(Exception):
            invalid_condition = Condition(
                attribute="location",
                operator=OperatorType.GEO_DISTANCE,
                value=[91, -74],  # Invalid latitude - should raise Pydantic error
                additional_value=10,
            )


class TestIntegrationScenarios:
    """Tests for complex integration scenarios."""

    def setup_method(self):
        """Setup test data."""
        self.service = RulesEvaluationService()

    def test_complex_nested_rules(self):
        """Test complex nested rule scenarios."""
        # Create nested rules: (country=US AND tier=premium) OR (version>=1.2.0 AND location in NYC)

        # First group: country AND tier
        us_condition = Condition(
            attribute="country", operator=OperatorType.EQUALS, value="US"
        )

        premium_condition = Condition(
            attribute="subscription_tier", operator=OperatorType.EQUALS, value="premium"
        )

        us_premium_group = RuleGroup(
            operator=LogicalOperator.AND, conditions=[us_condition, premium_condition]
        )

        # Second group: version AND location
        version_condition = Condition(
            attribute="app_version",
            operator=OperatorType.SEMANTIC_VERSION,
            value="1.2.0",
            additional_value="gte",
            attribute_type=AttributeType.SEMANTIC_VERSION,
        )

        location_condition = Condition(
            attribute="location",
            operator=OperatorType.GEO_DISTANCE,
            value=[40.7128, -74.0060],  # NYC coordinates
            additional_value=50,  # 50km radius
        )

        version_location_group = RuleGroup(
            operator=LogicalOperator.AND,
            conditions=[version_condition, location_condition],
        )

        # Main group: OR of the two groups
        main_group = RuleGroup(
            operator=LogicalOperator.OR,
            groups=[us_premium_group, version_location_group],
        )

        targeting_rule = TargetingRule(
            id="complex_rule", rule=main_group, rollout_percentage=100, priority=1
        )

        targeting_rules = TargetingRules(version="1.0", rules=[targeting_rule])

        # Test user that matches first group
        us_premium_user = {
            "user_id": "user1",
            "country": "US",
            "subscription_tier": "premium",
            "app_version": "1.0.0",
            "location": [34.0522, -118.2437],  # LA coordinates
        }

        matched_rule, metrics = self.service.evaluate_rules_with_validation(
            targeting_rules, us_premium_user
        )

        assert matched_rule is not None
        assert matched_rule.id == "complex_rule"

        # Test user that matches second group
        nyc_user = {
            "user_id": "user2",
            "country": "CA",
            "subscription_tier": "basic",
            "app_version": "1.3.0",
            "location": [40.7500, -73.9900],  # Close to NYC
        }

        matched_rule, metrics = self.service.evaluate_rules_with_validation(
            targeting_rules, nyc_user
        )

        assert matched_rule is not None
        assert matched_rule.id == "complex_rule"

        # Test user that matches neither group
        no_match_user = {
            "user_id": "user3",
            "country": "CA",
            "subscription_tier": "basic",
            "app_version": "1.0.0",
            "location": [34.0522, -118.2437],  # LA coordinates
        }

        matched_rule, metrics = self.service.evaluate_rules_with_validation(
            targeting_rules, no_match_user
        )

        assert matched_rule is None

    def test_rule_priority_evaluation(self):
        """Test that rules are evaluated in correct priority order."""
        # Create high priority rule
        high_priority_condition = Condition(
            attribute="subscription_tier", operator=OperatorType.EQUALS, value="premium"
        )

        high_priority_group = RuleGroup(
            operator=LogicalOperator.AND, conditions=[high_priority_condition]
        )

        high_priority_rule = TargetingRule(
            id="high_priority",
            rule=high_priority_group,
            rollout_percentage=100,
            priority=1,  # Higher priority (lower number)
        )

        # Create low priority rule that would also match
        low_priority_condition = Condition(
            attribute="country", operator=OperatorType.EQUALS, value="US"
        )

        low_priority_group = RuleGroup(
            operator=LogicalOperator.AND, conditions=[low_priority_condition]
        )

        low_priority_rule = TargetingRule(
            id="low_priority",
            rule=low_priority_group,
            rollout_percentage=100,
            priority=10,  # Lower priority (higher number)
        )

        targeting_rules = TargetingRules(
            version="1.0",
            rules=[low_priority_rule, high_priority_rule],  # Add in reverse order
        )

        # User that matches both rules
        user_context = {
            "user_id": "test-user",
            "country": "US",
            "subscription_tier": "premium",
        }

        matched_rule, metrics = self.service.evaluate_rules_with_validation(
            targeting_rules, user_context
        )

        # Should match the high priority rule first
        assert matched_rule is not None
        assert matched_rule.id == "high_priority"

    def test_rollout_percentage_evaluation(self):
        """Test rollout percentage affects rule matching."""
        condition = Condition(
            attribute="country", operator=OperatorType.EQUALS, value="US"
        )

        rule_group = RuleGroup(operator=LogicalOperator.AND, conditions=[condition])

        # Rule with 0% rollout - should never match
        zero_rollout_rule = TargetingRule(
            id="zero_rollout", rule=rule_group, rollout_percentage=0, priority=1
        )

        # Rule with 100% rollout - should always match
        full_rollout_rule = TargetingRule(
            id="full_rollout", rule=rule_group, rollout_percentage=100, priority=2
        )

        zero_rollout_rules = TargetingRules(version="1.0", rules=[zero_rollout_rule])

        full_rollout_rules = TargetingRules(version="1.0", rules=[full_rollout_rule])

        user_context = {"user_id": "test-user", "country": "US"}

        # Test 0% rollout
        matched_rule, _ = self.service.evaluate_rules_with_validation(
            zero_rollout_rules, user_context
        )
        assert matched_rule is None  # Should not match due to 0% rollout

        # Test 100% rollout
        matched_rule, _ = self.service.evaluate_rules_with_validation(
            full_rollout_rules, user_context
        )
        assert matched_rule is not None  # Should match with 100% rollout
        assert matched_rule.id == "full_rollout"
