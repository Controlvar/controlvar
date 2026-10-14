"""
Cost properties and latency benchmarks for the enhanced rules engine.

Two kinds of test live here, and the split is the point (#409).

``TestRulesEvaluationCost`` runs in every suite. It asserts the deterministic
property each latency number used to stand in for: how many conditions an
evaluation looks at, that the first matching rule ends the search, that the
validator visits each rule once, that concurrent evaluations agree with
sequential ones. Those are counts, so a slow or overloaded runner cannot fail
them and a genuinely more expensive engine cannot pass them.

``TestRulesLatencyBenchmarks`` keeps the absolute ceilings (average and p95
milliseconds, evaluations per second). It is marked ``benchmark`` and skipped
unless ``RUN_BENCHMARKS=1`` (see backend/tests/benchmark_gate.py): an absolute
number measures the machine as much as the code. Each ceiling is taken over
the fastest of several rounds, as ``timeit`` does, so even there one stalled
moment does not decide the result.

What used to be here: ``test_single_rule_performance`` asserted a single
measured average (``< 1 ms``) and p95 (``< 5 ms``) over 100 evaluations, in the
required unit suite. It failed #395's Backend Gate with nothing in that pull
request touching the rules engine, and every other test in this file had the
same shape.
"""

import random
import string
import threading
import time
from datetime import datetime
from statistics import mean, median, stdev
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

import pytest

from backend.app.core.rule_validation import RuleValidator
from backend.app.schemas.targeting_rule import (
    AttributeType,
    Condition,
    LogicalOperator,
    OperatorType,
    RuleGroup,
    TargetingRule,
    TargetingRules,
)
from backend.app.services.rules_evaluation_service import RulesEvaluationService

# Rounds per latency benchmark; the fastest one is asserted on.
BENCHMARK_ROUNDS = 5

# The operators the per-operator checks exercise, with the value each compares to.
OPERATORS_TO_TEST = [
    (OperatorType.EQUALS, "US"),
    (OperatorType.IN, ["US", "CA", "UK", "DE"]),
    (OperatorType.CONTAINS, "premium"),
    (
        OperatorType.MATCH_REGEX,
        r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$",
    ),
    (OperatorType.GREATER_THAN, 25),
    (OperatorType.SEMANTIC_VERSION, "1.2.0"),
    (OperatorType.GEO_DISTANCE, [40.7128, -74.0060]),
    (OperatorType.JSON_PATH, "$.premium"),
]

# Metric rule ids the service reports when it did not evaluate the rules.
NOT_EVALUATED = ("error", "validation_failed")


def _p95(times: List[float]) -> float:
    """The 95th-percentile sample (a single sample can be a GC pause)."""
    ordered = sorted(times)
    return ordered[max(0, int(round(0.95 * len(ordered))) - 1)]


def generate_random_user_context(
    include_advanced_attrs: bool = False,
) -> Dict[str, Any]:
    """Generate a random user context for testing."""
    countries = ["US", "CA", "UK", "DE", "FR", "JP", "AU", "BR"]
    tiers = ["basic", "premium", "enterprise"]

    context = {
        "user_id": f"user-{''.join(random.choices(string.ascii_lowercase, k=8))}",
        "country": random.choice(countries),
        "subscription_tier": random.choice(tiers),
        "age": random.randint(18, 80),
        "registered_user": random.choice([True, False]),
        "signup_date": datetime.now().isoformat(),
        "tags": random.choices(
            ["beta", "early-adopter", "power-user", "mobile"],
            k=random.randint(0, 3),
        ),
        "permissions": random.choices(
            ["read", "write", "admin", "delete"], k=random.randint(1, 4)
        ),
    }

    if include_advanced_attrs:
        context.update(
            {
                "app_version": f"{random.randint(1, 3)}.{random.randint(0, 9)}.{random.randint(0, 9)}",
                "location": [
                    round(random.uniform(-90, 90), 6),  # latitude
                    round(random.uniform(-180, 180), 6),  # longitude
                ],
                "device_info": {
                    "os": random.choice(["iOS", "Android", "Windows"]),
                    "version": f"{random.randint(1, 15)}.{random.randint(0, 9)}",
                },
                "user_preferences": {
                    "theme": random.choice(["light", "dark"]),
                    "notifications": random.choice([True, False]),
                },
            }
        )

    return context


def create_simple_rule(rule_id: str, priority: int = 1) -> TargetingRule:
    """One condition: ``country == "US"``."""
    condition = Condition(attribute="country", operator=OperatorType.EQUALS, value="US")
    rule_group = RuleGroup(operator=LogicalOperator.AND, conditions=[condition])
    return TargetingRule(
        id=rule_id, rule=rule_group, rollout_percentage=100, priority=priority
    )


def create_complex_rule(rule_id: str, priority: int = 1) -> TargetingRule:
    """Nested groups and advanced operators (seven conditions in three groups)."""
    demo_group = RuleGroup(
        operator=LogicalOperator.AND,
        conditions=[
            Condition(
                attribute="country", operator=OperatorType.IN, value=["US", "CA", "UK"]
            ),
            Condition(
                attribute="subscription_tier",
                operator=OperatorType.EQUALS,
                value="premium",
            ),
            Condition(
                attribute="age", operator=OperatorType.GREATER_THAN_OR_EQUAL, value=21
            ),
        ],
    )
    advanced_group = RuleGroup(
        operator=LogicalOperator.AND,
        conditions=[
            Condition(
                attribute="app_version",
                operator=OperatorType.SEMANTIC_VERSION,
                value="1.2.0",
                attribute_type=AttributeType.SEMANTIC_VERSION,
            ),
            Condition(
                attribute="location",
                operator=OperatorType.GEO_DISTANCE,
                value=[40.7128, -74.0060],  # NYC coordinates
                additional_value=100,  # 100km radius
            ),
        ],
    )
    behavior_group = RuleGroup(
        operator=LogicalOperator.AND,
        conditions=[
            Condition(
                attribute="tags",
                operator=OperatorType.CONTAINS_ANY,
                value=["beta", "early-adopter"],
            ),
            Condition(
                attribute="permissions",
                operator=OperatorType.CONTAINS_ALL,
                value=["read", "write"],
            ),
        ],
    )
    main_group = RuleGroup(
        operator=LogicalOperator.OR,
        groups=[demo_group, advanced_group, behavior_group],
    )
    return TargetingRule(
        id=rule_id, rule=main_group, rollout_percentage=100, priority=priority
    )


def condition_count(group: RuleGroup) -> int:
    """Every condition in *group*, nested groups included."""
    return len(group.conditions) + sum(condition_count(g) for g in group.groups or [])


def operator_rules_and_contexts(
    operator: OperatorType, value: Any
) -> Tuple[TargetingRules, List[Dict[str, Any]]]:
    """A one-condition rule on ``test_attr`` and 100 contexts of a fitting type."""
    condition = Condition(
        attribute="test_attr",
        operator=operator,
        value=value,
        additional_value=10 if operator == OperatorType.GEO_DISTANCE else None,
    )
    targeting_rules = TargetingRules(
        version="1.0",
        rules=[
            TargetingRule(
                id=f"test_{operator}",
                rule=RuleGroup(operator=LogicalOperator.AND, conditions=[condition]),
                rollout_percentage=100,
                priority=1,
            )
        ],
    )

    contexts = []
    for _ in range(100):
        context = generate_random_user_context(include_advanced_attrs=True)
        if operator == OperatorType.SEMANTIC_VERSION:
            context["test_attr"] = (
                f"{random.randint(1, 3)}.{random.randint(0, 9)}.{random.randint(0, 9)}"
            )
        elif operator == OperatorType.GEO_DISTANCE:
            context["test_attr"] = [
                round(random.uniform(40, 41), 6),
                round(random.uniform(-75, -73), 6),
            ]
        elif operator == OperatorType.JSON_PATH:
            context["test_attr"] = {
                "user": {"tier": random.choice(["basic", "premium"])}
            }
        else:
            context["test_attr"] = random.choice(
                ["US", "premium", "test@example.com", 30]
            )
        contexts.append(context)
    return targeting_rules, contexts


class TestRulesEvaluationCost:
    """What an evaluation costs, counted rather than timed.

    The unit of cost is a condition evaluation: that is where the engine
    spends its time, and it is what a quadratic loop, a lost short-circuit or
    a re-evaluation would multiply. Every test below also asserts that the
    evaluation really happened (no error, no failed context validation) --
    otherwise an engine that bailed out early would look very cheap.
    """

    def setup_method(self):
        self.service = RulesEvaluationService()

    def evaluate_counting(
        self, targeting_rules: TargetingRules, user_contexts: List[Dict[str, Any]]
    ) -> List[Tuple[Optional[TargetingRule], int]]:
        """Evaluate each context; return (matched rule, conditions evaluated)."""
        real = self.service._evaluate_condition_enhanced
        outcomes = []
        with patch.object(
            self.service, "_evaluate_condition_enhanced", wraps=real
        ) as counter:
            for user_context in user_contexts:
                counter.reset_mock()
                matched_rule, metrics = self.service.evaluate_rules_with_validation(
                    targeting_rules=targeting_rules,
                    user_context=user_context,
                    track_metrics=True,
                )
                assert metrics is not None
                assert metrics.rule_id not in NOT_EVALUATED, (
                    f"the rules were not evaluated for {user_context}: {metrics.error}"
                )
                outcomes.append((matched_rule, counter.call_count))
        assert not self.service.error_counts, dict(self.service.error_counts)
        return outcomes

    @pytest.mark.regression
    def test_single_rule_performance(self):
        """A one-condition rule costs one condition evaluation, and is right.

        #409: this asserted a measured average under 1 ms and a p95 under
        5 ms over 100 evaluations, and failed a pull request that did not
        touch the rules engine. What "fast" meant for a single simple rule is
        that evaluating it looks at its one condition once -- no
        re-evaluation, no per-call recompilation of the condition -- and gets
        the answer right.
        """
        targeting_rules = TargetingRules(
            version="1.0", rules=[create_simple_rule("simple_rule")]
        )
        user_contexts = [generate_random_user_context() for _ in range(100)]

        outcomes = self.evaluate_counting(targeting_rules, user_contexts)

        for user_context, (matched_rule, evaluated) in zip(user_contexts, outcomes):
            assert evaluated == 1, (
                f"one condition, evaluated {evaluated} times for one user"
            )
            expected = "simple_rule" if user_context["country"] == "US" else None
            assert (matched_rule.id if matched_rule else None) == expected

    def test_multiple_simple_rules_performance(self):
        """Cost is linear in the rules tried, and the first match ends it.

        The old ceiling here was commented "should scale linearly". Ten
        one-condition rules: a US user matches the first and costs one
        condition; anyone else must be checked against all ten and costs ten.
        More than that is a lost short-circuit or a rescan.
        """
        rules = [create_simple_rule(f"rule_{i}", priority=i) for i in range(10)]
        targeting_rules = TargetingRules(version="1.0", rules=rules)
        user_contexts = [generate_random_user_context() for _ in range(100)]
        # Both paths, whatever the random draw.
        user_contexts[0]["country"] = "US"
        user_contexts[1]["country"] = "DE"

        outcomes = self.evaluate_counting(targeting_rules, user_contexts)

        for user_context, (matched_rule, evaluated) in zip(user_contexts, outcomes):
            if user_context["country"] == "US":
                assert matched_rule is not None and matched_rule.id == "rule_0"
                assert evaluated == 1, f"matched the first rule after {evaluated}"
            else:
                assert matched_rule is None
                assert evaluated == len(rules), (
                    f"{len(rules)} one-condition rules, {evaluated} evaluations"
                )

    def test_complex_rule_performance(self):
        """A nested rule costs at most its own conditions, once each."""
        rule = create_complex_rule("complex_rule")
        targeting_rules = TargetingRules(version="1.0", rules=[rule])
        size = condition_count(rule.rule)
        user_contexts = [
            generate_random_user_context(include_advanced_attrs=True)
            for _ in range(100)
        ]

        outcomes = self.evaluate_counting(targeting_rules, user_contexts)

        for _, evaluated in outcomes:
            assert 1 <= evaluated <= size, (
                f"a rule of {size} conditions cost {evaluated} evaluations"
            )

    def test_multiple_complex_rules_performance(self):
        """Five identical complex rules: a match stops at the first.

        A matching user costs at most one rule's conditions; a user no rule
        matches costs at most all five rules' conditions.
        """
        rules = [create_complex_rule(f"complex_rule_{i}", priority=i) for i in range(5)]
        targeting_rules = TargetingRules(version="1.0", rules=rules)
        size = condition_count(rules[0].rule)
        user_contexts = [
            generate_random_user_context(include_advanced_attrs=True) for _ in range(50)
        ]

        outcomes = self.evaluate_counting(targeting_rules, user_contexts)

        for matched_rule, evaluated in outcomes:
            if matched_rule is not None:
                assert matched_rule.id == "complex_rule_0"
                assert evaluated <= size, (
                    f"matched the first rule but evaluated {evaluated} conditions"
                )
            else:
                assert evaluated <= size * len(rules)

    def test_large_scale_rules_performance(self):
        """Twenty mixed rules: never more than every condition once."""
        rules = []
        for i in range(20):
            if i % 3 == 0:
                rules.append(create_complex_rule(f"complex_rule_{i}", priority=i))
            else:
                rules.append(create_simple_rule(f"simple_rule_{i}", priority=i))
        targeting_rules = TargetingRules(version="1.0", rules=rules)
        total = sum(condition_count(r.rule) for r in rules)
        user_contexts = [
            generate_random_user_context(include_advanced_attrs=True) for _ in range(50)
        ]

        outcomes = self.evaluate_counting(targeting_rules, user_contexts)

        for _, evaluated in outcomes:
            assert 1 <= evaluated <= total, (
                f"{total} conditions in the ruleset, {evaluated} evaluations"
            )

    def test_validation_performance(self):
        """The validator visits each rule exactly once, and passes these."""
        rules = [
            create_complex_rule(f"complex_rule_{i}", priority=i) for i in range(10)
        ]
        targeting_rules = TargetingRules(version="1.0", rules=rules)
        validator = RuleValidator()

        with patch.object(
            validator,
            "_validate_targeting_rule",
            wraps=validator._validate_targeting_rule,
        ) as per_rule:
            result = validator.validate_targeting_rules(targeting_rules)

        assert result.is_valid, "Complex rules should be valid"
        assert per_rule.call_count == len(rules), (
            f"{len(rules)} rules, validated {per_rule.call_count} times"
        )

    def test_memory_usage_scaling(self):
        """Test memory usage doesn't grow excessively with rule evaluations."""
        import tracemalloc

        rule = create_complex_rule("memory_test_rule")
        targeting_rules = TargetingRules(version="1.0", rules=[rule])

        tracemalloc.start()
        for i in range(1000):
            self.service.evaluate_rules_with_validation(
                targeting_rules=targeting_rules,
                user_context=generate_random_user_context(include_advanced_attrs=True),
                track_metrics=True,
            )
            # Clear metrics periodically to prevent unbounded growth
            if i % 100 == 0:
                self.service.clear_metrics()
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        # Memory usage should be reasonable (less than 10MB for this test)
        assert peak < 10 * 1024 * 1024, (
            f"Peak memory usage too high: {peak / 1024 / 1024:.2f} MB"
        )

    def test_concurrent_evaluation_performance(self):
        """Four threads at once get exactly the answers one thread gets.

        The old version timed this and asserted a throughput; what running it
        concurrently can actually break is correctness -- shared state in the
        service or engine leaking between evaluations. So every concurrent
        answer is compared with the sequential answer for the same context,
        with the workers released together so they genuinely overlap.
        """
        rule = create_complex_rule("concurrent_test_rule")
        targeting_rules = TargetingRules(version="1.0", rules=[rule])
        num_workers, per_worker = 4, 25
        contexts = {
            (w, i): {
                **generate_random_user_context(include_advanced_attrs=True),
                "user_id": f"worker_{w}_user_{i}",
            }
            for w in range(num_workers)
            for i in range(per_worker)
        }

        def answer(service: RulesEvaluationService, context: Dict[str, Any]):
            matched_rule, metrics = service.evaluate_rules_with_validation(
                targeting_rules=targeting_rules,
                user_context=context,
                track_metrics=True,
            )
            return (matched_rule.id if matched_rule else None, metrics.rule_id)

        sequential = {key: answer(self.service, ctx) for key, ctx in contexts.items()}

        shared = RulesEvaluationService()
        concurrent: Dict[Tuple[int, int], Any] = {}
        failures: List[BaseException] = []
        start = threading.Barrier(num_workers)

        def worker(w: int) -> None:
            try:
                start.wait()
                for i in range(per_worker):
                    concurrent[(w, i)] = answer(shared, contexts[(w, i)])
            except BaseException as exc:  # surfaced below, not lost in the thread
                failures.append(exc)

        threads = [
            threading.Thread(target=worker, args=(w,)) for w in range(num_workers)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert not failures, failures
        assert len(concurrent) == num_workers * per_worker
        assert concurrent == sequential
        assert not shared.error_counts, dict(shared.error_counts)

    def test_operator_specific_performance(self):
        """Every operator evaluates its one condition once, without erroring."""
        for operator, value in OPERATORS_TO_TEST:
            targeting_rules, user_contexts = operator_rules_and_contexts(
                operator, value
            )
            outcomes = self.evaluate_counting(targeting_rules, user_contexts)
            assert [evaluated for _, evaluated in outcomes] == [1] * len(
                user_contexts
            ), f"{operator}: one condition should cost one evaluation"


@pytest.mark.benchmark
class TestRulesLatencyBenchmarks:
    """Absolute latency ceilings. Skipped unless RUN_BENCHMARKS=1.

    These are the numbers the cost tests above stand in for. They are real
    targets, but they measure the machine too, so no required job runs them.
    """

    def setup_method(self):
        self.service = RulesEvaluationService()
        self.validator = RuleValidator()

    def benchmark_evaluation_time(
        self,
        targeting_rules: TargetingRules,
        user_contexts: List[Dict[str, Any]],
        rounds: int = BENCHMARK_ROUNDS,
    ) -> Dict[str, float]:
        """Per-evaluation timings (ms) of the fastest of *rounds* rounds."""
        best: Optional[List[float]] = None
        for _ in range(rounds):
            times = []
            for user_context in user_contexts:
                start_time = time.perf_counter()
                self.service.evaluate_rules_with_validation(
                    targeting_rules=targeting_rules,
                    user_context=user_context,
                    track_metrics=True,
                )
                times.append((time.perf_counter() - start_time) * 1000)
            if best is None or sum(times) < sum(best):
                best = times
        assert best is not None
        return {
            "avg_time_ms": mean(best),
            "median_time_ms": median(best),
            "max_time_ms": max(best),
            "p95_time_ms": _p95(best),
            "std_dev_ms": stdev(best) if len(best) > 1 else 0,
        }

    @pytest.mark.parametrize(
        ("rules", "advanced", "users", "avg_ms", "p95_ms"),
        [
            pytest.param(
                lambda: [create_simple_rule("simple_rule")],
                False,
                100,
                1.0,
                5.0,
                id="single_simple_rule",
            ),
            pytest.param(
                lambda: [
                    create_simple_rule(f"rule_{i}", priority=i) for i in range(10)
                ],
                False,
                100,
                5.0,
                20.0,
                id="multiple_simple_rules",
            ),
            pytest.param(
                lambda: [create_complex_rule("complex_rule")],
                True,
                100,
                10.0,
                50.0,
                id="single_complex_rule",
            ),
            pytest.param(
                lambda: [
                    create_complex_rule(f"complex_rule_{i}", priority=i)
                    for i in range(5)
                ],
                True,
                50,
                25.0,
                100.0,
                id="multiple_complex_rules",
            ),
            pytest.param(
                lambda: [
                    create_complex_rule(f"complex_rule_{i}", priority=i)
                    if i % 3 == 0
                    else create_simple_rule(f"simple_rule_{i}", priority=i)
                    for i in range(20)
                ],
                True,
                50,
                50.0,
                200.0,
                id="large_scale_rules",
            ),
        ],
    )
    def test_evaluation_latency(self, rules, advanced, users, avg_ms, p95_ms):
        targeting_rules = TargetingRules(version="1.0", rules=rules())
        user_contexts = [
            generate_random_user_context(include_advanced_attrs=advanced)
            for _ in range(users)
        ]

        results = self.benchmark_evaluation_time(targeting_rules, user_contexts)

        assert results["avg_time_ms"] < avg_ms, results
        assert results["p95_time_ms"] < p95_ms, results

    def test_validation_latency(self):
        rules = [
            create_complex_rule(f"complex_rule_{i}", priority=i) for i in range(10)
        ]
        targeting_rules = TargetingRules(version="1.0", rules=rules)

        best = float("inf")
        for _ in range(BENCHMARK_ROUNDS):
            start_time = time.perf_counter()
            self.validator.validate_targeting_rules(targeting_rules)
            best = min(best, (time.perf_counter() - start_time) * 1000)

        assert best < 100.0, f"Validation time too high: {best}ms"

    def test_concurrent_throughput(self):
        rule = create_complex_rule("concurrent_test_rule")
        targeting_rules = TargetingRules(version="1.0", rules=[rule])
        num_workers, per_worker = 4, 25

        def worker(worker_id: int) -> None:
            for i in range(per_worker):
                context = generate_random_user_context(include_advanced_attrs=True)
                context["user_id"] = f"worker_{worker_id}_user_{i}"
                RulesEvaluationService().evaluate_rules_with_validation(
                    targeting_rules=targeting_rules,
                    user_context=context,
                    track_metrics=True,
                )

        best = float("inf")
        for _ in range(BENCHMARK_ROUNDS):
            threads = [
                threading.Thread(target=worker, args=(w,)) for w in range(num_workers)
            ]
            start_time = time.perf_counter()
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            best = min(best, time.perf_counter() - start_time)

        evaluations_per_second = num_workers * per_worker / best
        assert evaluations_per_second > 50, (
            f"Throughput too low: {evaluations_per_second} eval/sec"
        )

    @pytest.mark.parametrize(
        ("operator", "value"),
        OPERATORS_TO_TEST,
        ids=[str(o) for o, _ in OPERATORS_TO_TEST],
    )
    def test_operator_latency(self, operator, value):
        targeting_rules, user_contexts = operator_rules_and_contexts(operator, value)

        results = self.benchmark_evaluation_time(targeting_rules, user_contexts)

        assert results["avg_time_ms"] < 5.0, (
            f"Operator {operator} too slow: {results['avg_time_ms']}ms"
        )
