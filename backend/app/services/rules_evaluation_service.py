"""
Enhanced rules evaluation service with performance monitoring.

This service provides advanced rule evaluation capabilities with:
- Performance metrics and monitoring
- Error tracking and logging
- Integration with assignment service
- Rule compilation and caching for performance
- Batch evaluation support
"""

import hashlib
import json
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from backend.app.core.evaluation_cache import EvaluationCache
from backend.app.core.log_once import EVALUATION_NOTES
from backend.app.core.pattern_match import PatternUnevaluable, report_unevaluable
from backend.app.core.rule_compiler import RuleCompiler
from backend.app.core.rules_engine import (
    SegmentMembershipUnavailable,
    TimeWindowUnreadable,
    UserContext,
    check_time_window,
    segment_condition_holds,
)
from backend.app.core.rules_engine import (
    apply_operator as base_apply_operator,
)
from backend.app.schemas.targeting_rule import (
    Condition,
    LogicalOperator,
    OperatorType,
    RuleGroup,
    TargetingRule,
    TargetingRules,
)

logger = logging.getLogger(__name__)

# Logging here follows backend/app/core/rules_engine.py: never a user's
# attribute value or an exception's text, at any level; only type names,
# operators, caller-owned identifiers and fixed strings
# (backend/tests/unit/core/test_evaluation_log_arguments.py).


# The longest user value, in characters, that a json_path condition parses as
# JSON (64 KiB). A longer one does not match, and is not parsed.
JSON_PATH_MAX_VALUE_LENGTH = 64 * 1024


@dataclass
class RuleEvaluationMetrics:
    """Metrics for rule evaluation performance."""

    rule_id: str
    evaluation_time_ms: float
    matched: bool
    error: Optional[str] = None
    complexity_score: int = 0
    attribute_count: int = 0


@dataclass
class EvaluationResult:
    """Result of a rule evaluation."""

    matched: bool
    matched_rule_id: Optional[str] = None
    variant: Optional[str] = None
    error: Optional[str] = None
    cached: bool = False
    evaluation_time_ms: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EvaluationMetrics:
    """Metrics collected during rule evaluation."""

    total_evaluations: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    total_errors: int = 0
    avg_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    p99_latency_ms: float = 0.0
    latency_samples: deque = field(default_factory=lambda: deque(maxlen=1000))

    def record_latency(self, latency_ms: float):
        """Record a latency sample."""
        self.latency_samples.append(latency_ms)
        self._update_percentiles()

    def _update_percentiles(self):
        """Update percentile calculations."""
        if not self.latency_samples:
            return

        sorted_samples = sorted(self.latency_samples)
        n = len(sorted_samples)

        self.avg_latency_ms = sum(sorted_samples) / n

        if n > 0:
            p95_idx = int(n * 0.95)
            p99_idx = int(n * 0.99)
            self.p95_latency_ms = sorted_samples[min(p95_idx, n - 1)]
            self.p99_latency_ms = sorted_samples[min(p99_idx, n - 1)]


class RulesEvaluationService:
    """Enhanced rules evaluation service with validation and monitoring."""

    def __init__(
        self,
        cache_max_size: int = 10000,
        cache_ttl: float = 300.0,
        compiler_cache_size: int = 1000,
        enable_metrics: bool = True,
    ):
        """
        Initialize the service.

        Args:
            cache_max_size: Maximum number of evaluation results to cache
            cache_ttl: Time-to-live for cached results in seconds
            compiler_cache_size: Maximum number of compiled rules to cache
            enable_metrics: Whether to collect metrics
        """
        # Legacy attributes
        self.evaluation_metrics: List[RuleEvaluationMetrics] = []
        self.attribute_cache: Dict[str, Any] = {}
        self.error_counts = defaultdict(int)
        self.performance_stats = defaultdict(list)

        # New caching and compilation components
        self.evaluation_cache = EvaluationCache(
            max_size=cache_max_size, default_ttl=cache_ttl
        )
        self.rule_compiler = RuleCompiler(cache_max_size=compiler_cache_size)
        self.enable_metrics = enable_metrics
        self.metrics = EvaluationMetrics()

        # Constructed per request by AssignmentService, so DEBUG.
        logger.debug("RulesEvaluationService initialized")

    def evaluate_rules_with_validation(
        self,
        targeting_rules: TargetingRules,
        user_context: UserContext,
        track_metrics: bool = True,
        owner: str = "experiment",
    ) -> Tuple[Optional[TargetingRule], Optional[RuleEvaluationMetrics]]:
        """
        Evaluate targeting rules with monitoring.

        A condition's ``attribute_type`` is not read: the context's value is
        given to the operator as it is, as on flags (#1159).

        Args:
            targeting_rules: The targeting rules to evaluate
            user_context: User context for evaluation
            track_metrics: Whether to track evaluation metrics
            owner: Names the ruleset in the warning logged when a pattern
                condition cannot be evaluated (``experiment:<id>``).

        Returns:
            Tuple of (matched_rule, evaluation_metrics)
        """
        start_time = time.time()
        matched_rule = None
        metrics = None

        try:
            # Evaluate rules with enhanced operators
            matched_rule = self._evaluate_targeting_rules_enhanced(
                targeting_rules, user_context
            )

            # Track metrics if requested
            if track_metrics:
                evaluation_time = (time.time() - start_time) * 1000
                complexity = self._calculate_rule_complexity(targeting_rules)

                metrics = RuleEvaluationMetrics(
                    rule_id=matched_rule.id if matched_rule else "no_match",
                    evaluation_time_ms=evaluation_time,
                    matched=matched_rule is not None,
                    complexity_score=complexity,
                    attribute_count=len(user_context),
                )

                self.evaluation_metrics.append(metrics)
                self.performance_stats["evaluation_time"].append(evaluation_time)

        except SegmentMembershipUnavailable:
            # The caller decides what an undecidable segment means (experiment
            # assignment refuses, and logs the segment ids once).
            raise

        except PatternUnevaluable as exc:
            # A pattern condition could not be evaluated: the whole ruleset is
            # abandoned (no rule and no default_rule), so the user is not
            # eligible. Caught here, above the recursive group evaluator, so an
            # enclosing NOT can never turn it into a match.
            matched_rule = None
            report_unevaluable(exc, owner)
            if track_metrics:
                metrics = RuleEvaluationMetrics(
                    rule_id="error",
                    evaluation_time_ms=(time.time() - start_time) * 1000,
                    matched=False,
                    error=str(exc),
                )

        except TimeWindowUnreadable as exc:
            # The same for a time_window the engine cannot read (#1145),
            # already reported by the rules engine.
            matched_rule = None
            if track_metrics:
                metrics = RuleEvaluationMetrics(
                    rule_id="error",
                    evaluation_time_ms=(time.time() - start_time) * 1000,
                    matched=False,
                    error=str(exc),
                )

        except Exception as e:
            logger.error(
                "Error evaluating targeting rules for %s (%s)", owner, type(e).__name__
            )
            self.error_counts["evaluation_error"] += 1

            if track_metrics:
                # The type only: the text can repeat an attribute value.
                metrics = RuleEvaluationMetrics(
                    rule_id="error",
                    evaluation_time_ms=(time.time() - start_time) * 1000,
                    matched=False,
                    error=f"Evaluation failed ({type(e).__name__})",
                )

        return matched_rule, metrics

    def _evaluate_targeting_rules_enhanced(
        self, targeting_rules: TargetingRules, user_context: UserContext
    ) -> Optional[TargetingRule]:
        """Evaluate targeting rules with enhanced operators."""
        if not targeting_rules or not targeting_rules.rules:
            return targeting_rules.default_rule

        # Sort rules by priority (lower number = higher priority)
        sorted_rules = sorted(targeting_rules.rules, key=lambda r: r.priority)

        for rule in sorted_rules:
            # Check if rule matches with enhanced evaluation
            if self._evaluate_rule_enhanced(rule, user_context):
                # Check rollout percentage (use base implementation)
                from backend.app.core.rules_engine import should_include_in_rollout

                if should_include_in_rollout(rule, user_context):
                    return rule

        return targeting_rules.default_rule

    def _evaluate_rule_enhanced(
        self, rule: TargetingRule, user_context: UserContext
    ) -> bool:
        """Evaluate a rule with enhanced operators."""
        return self._evaluate_rule_group_enhanced(rule.rule, user_context)

    def _evaluate_rule_group_enhanced(
        self, rule_group: RuleGroup, user_context: UserContext
    ) -> bool:
        """Evaluate a rule group with enhanced operators."""
        operator = rule_group.operator

        # Evaluate conditions with enhanced operators
        condition_results = [
            self._evaluate_condition_enhanced(condition, user_context)
            for condition in rule_group.conditions
        ]

        # Evaluate nested groups
        if rule_group.groups:
            group_results = [
                self._evaluate_rule_group_enhanced(group, user_context)
                for group in rule_group.groups
            ]
        else:
            group_results = []

        # Combine all results
        all_results = condition_results + group_results

        # If there are no conditions or groups, return True
        if not all_results:
            return True

        # Apply logical operator
        if operator == LogicalOperator.AND:
            return all(all_results)
        elif operator == LogicalOperator.OR:
            return any(all_results)
        elif operator == LogicalOperator.NOT:
            if len(all_results) == 1:
                return not all_results[0]
            else:
                return not all(all_results)

        # Default fallback
        if EVALUATION_NOTES.first(operator, "unknown logical operator"):
            logger.warning(
                "Unknown logical operator %s in targeting rules; logged once.",
                operator,
            )
        return False

    def _evaluate_condition_enhanced(
        self, condition: Condition, user_context: UserContext
    ) -> bool:
        """Evaluate a condition with enhanced operators."""
        # A segment condition is answered from the resolved membership,
        # before (and instead of) any attribute lookup, as on the flag path.
        segment_answer = segment_condition_holds(condition, user_context)
        if segment_answer is not None:
            return segment_answer

        # As on the flag path: a time_window that cannot be read refuses the
        # evaluation (the whole ruleset, as PatternUnevaluable does), for a
        # user who does not send the attribute too.
        if condition.operator == OperatorType.TIME_WINDOW:
            check_time_window(condition.operator, condition.value)

        attribute = condition.attribute
        operator = condition.operator
        expected_value = condition.value
        additional_value = condition.additional_value

        # Get actual value from user context. A missing attribute counts as
        # null so the is_null / is_not_null operators behave like the core
        # engine (see rules_engine.evaluate_condition).
        if attribute not in user_context:
            if operator == OperatorType.IS_NULL:
                return True
            if operator == OperatorType.IS_NOT_NULL:
                return False
            logger.debug("Attribute not found in user context (operator=%s)", operator)
            return False

        actual_value = user_context[attribute]

        # Use enhanced operator application
        return self._apply_operator_enhanced(
            operator, actual_value, expected_value, additional_value
        )

    def _apply_operator_enhanced(
        self,
        operator: OperatorType,
        actual_value: Any,
        expected_value: Any,
        additional_value: Optional[Any] = None,
    ) -> bool:
        """Apply an operator for experiment targeting.

        Only the operators below are this service's own. Every other one,
        ``semantic_version``, ``geo_distance`` (#1144), ``time_window``
        (#1145) and ``array_length`` (#1159) among them, is
        ``rules_engine.apply_operator``, which also answers flag targeting, so
        an experiment and a flag answer those rules alike.
        """

        # Handle new custom operators
        if operator == OperatorType.PERCENTAGE_BUCKET:
            return self._evaluate_percentage_bucket(actual_value, expected_value)
        elif operator == OperatorType.JSON_PATH:
            return self._evaluate_json_path(
                actual_value, expected_value, additional_value
            )

        # Fall back to base implementation for standard operators
        return base_apply_operator(
            operator, actual_value, expected_value, additional_value
        )

    def _evaluate_percentage_bucket(self, user_id: Any, percentage: Any) -> bool:
        """Evaluate if user falls within percentage bucket."""
        try:
            # Create deterministic hash
            hash_input = str(user_id)
            hash_value = int(
                hashlib.md5(hash_input.encode(), usedforsecurity=False).hexdigest(), 16
            )
            bucket = hash_value % 100

            return bucket < float(percentage)

        except (ValueError, TypeError):
            logger.debug("Failed to evaluate percentage bucket")
            return False

    def _evaluate_json_path(
        self, json_data: Any, json_path: str, expected_value: Any
    ) -> bool:
        """Evaluate JSONPath expression.

        A string value is parsed as JSON first. One that cannot be parsed does
        not match: it is longer than ``JSON_PATH_MAX_VALUE_LENGTH``, is not JSON,
        or is nested too deeply for the parser. That is the user's value, not a
        fault in the rule, so it is logged at DEBUG with the exception's type
        only.
        """
        try:
            # Parse JSON string if needed
            if isinstance(json_data, str):
                if len(json_data) > JSON_PATH_MAX_VALUE_LENGTH:
                    logger.debug("JSON path value is over the length limit")
                    return False
                try:
                    json_data = json.loads(json_data)
                except (ValueError, RecursionError) as exc:
                    # ValueError includes json.JSONDecodeError and a number too
                    # long to convert; RecursionError is a deeply nested value.
                    logger.debug(
                        "JSON path value cannot be parsed (%s)", type(exc).__name__
                    )
                    return False

            # Ensure we have a dict or list to work with
            if not isinstance(json_data, (dict, list)):
                return False

            # Handle simple paths like $.field or $.field.subfield
            path_parts = json_path[2:].split(".")  # Remove '$.'

            current = json_data
            for part in path_parts:
                if isinstance(current, dict) and part in current:
                    current = current[part]
                elif isinstance(current, list) and part.isdigit():
                    current = current[int(part)]
                else:
                    return False

            return current == expected_value

        except (ValueError, TypeError, json.JSONDecodeError, IndexError, KeyError):
            logger.debug("Failed to evaluate JSON path")
            return False

    def _calculate_rule_complexity(self, targeting_rules: TargetingRules) -> int:
        """Calculate complexity score for rules."""
        complexity = 0

        def calculate_group_complexity(group: RuleGroup) -> int:
            score = len(group.conditions)
            if group.groups:
                score += sum(calculate_group_complexity(g) for g in group.groups)
                score += len(group.groups)  # Add complexity for nesting
            return score

        for rule in targeting_rules.rules:
            complexity += calculate_group_complexity(rule.rule)

        if targeting_rules.default_rule:
            complexity += calculate_group_complexity(targeting_rules.default_rule.rule)

        return complexity

    def get_performance_stats(self) -> Dict[str, Any]:
        """Get performance statistics."""
        if not self.evaluation_metrics:
            return {}

        evaluation_times = [m.evaluation_time_ms for m in self.evaluation_metrics]

        return {
            "total_evaluations": len(self.evaluation_metrics),
            "avg_evaluation_time_ms": sum(evaluation_times) / len(evaluation_times),
            "min_evaluation_time_ms": min(evaluation_times),
            "max_evaluation_time_ms": max(evaluation_times),
            "error_count": sum(1 for m in self.evaluation_metrics if m.error),
            "match_rate": sum(1 for m in self.evaluation_metrics if m.matched)
            / len(self.evaluation_metrics),
            "avg_complexity_score": sum(
                m.complexity_score for m in self.evaluation_metrics
            )
            / len(self.evaluation_metrics),
            "error_breakdown": dict(self.error_counts),
        }

    def clear_metrics(self):
        """Clear collected metrics."""
        self.evaluation_metrics.clear()
        self.error_counts.clear()
        self.performance_stats.clear()
        self.attribute_cache.clear()

    def evaluate(
        self,
        rules: TargetingRules,
        user_context: Dict[str, Any],
        skip_cache: bool = False,
    ) -> EvaluationResult:
        """
        Evaluate targeting rules for a user with caching.

        This is the simplified API that integrates caching and compilation.

        Args:
            rules: The targeting rules to evaluate
            user_context: User context with attributes
            skip_cache: If True, bypass cache and force evaluation

        Returns:
            EvaluationResult with matched status and rule info
        """
        start_time = time.time()

        try:
            # Check cache first (unless skipped)
            if not skip_cache:
                cached_result = self._check_cache(rules, user_context)
                if cached_result:
                    cached_result.evaluation_time_ms = (time.time() - start_time) * 1000
                    if self.enable_metrics:
                        self.metrics.total_evaluations += 1
                        self.metrics.cache_hits += 1
                        self.metrics.record_latency(cached_result.evaluation_time_ms)
                    return cached_result

            # Cache miss - perform evaluation
            if self.enable_metrics:
                self.metrics.cache_misses += 1

            # Compile rules if needed
            self._compile_rules(rules)

            # Evaluate rules using existing enhanced evaluation
            matched_rule = self._evaluate_targeting_rules_enhanced(rules, user_context)

            # Build result
            result = EvaluationResult(
                matched=matched_rule is not None,
                matched_rule_id=matched_rule.id if matched_rule else None,
                cached=False,
                evaluation_time_ms=(time.time() - start_time) * 1000,
            )

            # Cache result
            if not skip_cache and matched_rule:
                self._cache_result(rules, user_context, result)

            # Record metrics
            if self.enable_metrics:
                self.metrics.total_evaluations += 1
                self.metrics.record_latency(result.evaluation_time_ms)

            return result

        except PatternUnevaluable as exc:
            # Same ruleset-level abandonment as evaluate_rules_with_validation.
            report_unevaluable(exc, "targeting rules")
            return EvaluationResult(
                matched=False,
                error=str(exc),
                evaluation_time_ms=(time.time() - start_time) * 1000,
            )

        except TimeWindowUnreadable as exc:
            # The same for a time_window the engine cannot read (#1145).
            return EvaluationResult(
                matched=False,
                error=str(exc),
                evaluation_time_ms=(time.time() - start_time) * 1000,
            )

        except Exception as e:
            # No traceback: its last line is the exception's text.
            logger.error("Error evaluating targeting rules (%s)", type(e).__name__)

            if self.enable_metrics:
                self.metrics.total_errors += 1
                self.metrics.total_evaluations += 1

            return EvaluationResult(
                matched=False,
                error=f"Evaluation failed ({type(e).__name__})",
                evaluation_time_ms=(time.time() - start_time) * 1000,
            )

    def batch_evaluate(
        self, rules: TargetingRules, user_contexts: List[Dict[str, Any]]
    ) -> List[EvaluationResult]:
        """
        Evaluate rules for multiple users in batch.

        Optimizations:
        - Compile rules once, reuse for all evaluations
        - Leverage cache for repeated contexts
        - Process efficiently in single pass

        Args:
            rules: The targeting rules to evaluate
            user_contexts: List of user contexts

        Returns:
            List of EvaluationResults in same order as input
        """
        # Pre-compile rules once
        self._compile_rules(rules)

        results = []
        for user_context in user_contexts:
            result = self.evaluate(rules, user_context)
            results.append(result)

        return results

    def invalidate_rule_cache(self, rule_id: str):
        """
        Invalidate all cached evaluations for a specific rule.

        Args:
            rule_id: The rule ID to invalidate
        """
        self.evaluation_cache.invalidate_rule(rule_id)
        logger.info("Invalidated cached evaluations for rule %s", rule_id)

    def invalidate_user_cache(self, user_id: str):
        """
        Invalidate all cached evaluations for a specific user.

        Args:
            user_id: The user ID to invalidate
        """
        self.evaluation_cache.invalidate_user(user_id)
        logger.info("Invalidated cached evaluations for one user")

    def get_metrics(self) -> EvaluationMetrics:
        """
        Get current evaluation metrics.

        Returns:
            EvaluationMetrics with statistics
        """
        return self.metrics

    def reset_metrics(self):
        """Reset metrics counters."""
        self.metrics = EvaluationMetrics()
        logger.info("Metrics reset")

    def _check_cache(
        self, rules: TargetingRules, user_context: Dict[str, Any]
    ) -> Optional[EvaluationResult]:
        """
        Check cache for existing evaluation result.

        Args:
            rules: Targeting rules
            user_context: User context

        Returns:
            Cached EvaluationResult or None
        """
        # Generate cache key from rules and context
        if not rules.rules:
            return None

        rule_id = rules.rules[0].id
        cached = self.evaluation_cache.get(rule_id, user_context)

        if cached is not None:
            return EvaluationResult(
                matched=cached, matched_rule_id=rule_id if cached else None, cached=True
            )

        return None

    def _cache_result(
        self,
        rules: TargetingRules,
        user_context: Dict[str, Any],
        result: EvaluationResult,
    ):
        """
        Cache evaluation result.

        Args:
            rules: Targeting rules
            user_context: User context
            result: Evaluation result to cache
        """
        if not rules.rules or not result.matched_rule_id:
            return

        rule_id = result.matched_rule_id
        self.evaluation_cache.set(rule_id, user_context, result.matched)

    def _compile_rules(self, rules: TargetingRules) -> List:
        """
        Compile targeting rules for optimized evaluation.

        Args:
            rules: Targeting rules to compile

        Returns:
            List of compiled rules
        """
        compiled = []
        for rule in rules.rules:
            try:
                compiled_rule = self.rule_compiler.compile(rule)
                compiled.append(compiled_rule)
            except Exception as e:
                # Compiled on every evaluate() call: warn once per rule.
                if EVALUATION_NOTES.first(rule.id, "rule failed to compile"):
                    logger.warning(
                        "Targeting rule %s failed to compile (%s); logged once.",
                        rule.id,
                        type(e).__name__,
                    )
                # Continue with other rules

        return compiled
