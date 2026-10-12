"""
Evaluating targeting rules never logs a user's attribute values, at any level.

Every operator's failure branches are driven with a marker in the user's
attributes, through both dispatchers -- ``rules_engine.apply_operator`` and
``RulesEvaluationService.evaluate_rules_with_validation`` (which has its own
implementations of three operators) -- and once end to end through
``FeatureFlagService.evaluate_flag_detailed``. For every case:

* no record, at any level, contains the marker;
* a value that does not suit the operator is logged at DEBUG only;
* a problem in the rule itself is logged at WARNING exactly once, however
  many times it is evaluated;
* at least one record was captured (a positive control: a capture that
  silently records nothing would otherwise pass everything above).

Capture is by replacing each module's ``logger`` with a recorder that keeps
every call at every level, as ``test_pattern_match.py`` does. caplog is not
used: importing the service runs ``setup_logging()``, which sets the root
level to INFO and replaces the root handlers, so DEBUG records would never be
created and "nothing at DEBUG contains the marker" would hold vacuously.

The marker is only ever an attribute value, never the ``user_id``: this
change is about attribute values. That no evaluation or assignment line logs
the ``user_id`` is pinned by
``backend/tests/unit/services/test_assignment_evaluation_logs_no_user_id.py``.
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, List, Tuple
from unittest.mock import MagicMock

import pytest

from backend.app.core import pattern_match, rules_engine, targeting_adapter
from backend.app.core.log_once import EVALUATION_NOTES
from backend.app.core.rules_engine import apply_operator
from backend.app.models.feature_flag import FeatureFlagStatus
from backend.app.schemas.targeting_rule import (
    Condition,
    LogicalOperator,
    OperatorType,
    RuleGroup,
    TargetingRule,
    TargetingRules,
)
from backend.app.services import feature_flag_service as ffs
from backend.app.services import rules_evaluation_service as res

pytestmark = pytest.mark.unit

MARKER = "PIIMARKER7"
#: For the branches that only a number reaches (no length, a timestamp out of
#: range, a latitude out of range). Its digits survive str(), repr() and float.
MARKER_NUM = 7310452961000000
MARKER_TEXTS = (MARKER, str(MARKER_NUM))

REPEAT = 3

LEVELS = ("debug", "info", "warning", "error", "critical")
WARNING_OR_ABOVE = ("warning", "error", "critical")


class Recorder:
    """Stands in for a module logger and keeps every call, formatted, at every level."""

    def __init__(self) -> None:
        self.records: List[Tuple[str, str, str]] = []

    def _bind(self, module: str) -> "SimpleNamespace":
        def at(level: str):
            def log(message, *args, **kwargs):
                text = message % args if args else str(message)
                if kwargs.get("exc_info"):
                    text += "\n" + traceback.format_exc()
                self.records.append((module, level, text))

            return log

        methods = {level: at(level) for level in LEVELS}
        methods["warn"] = methods["warning"]
        methods["exception"] = lambda message, *args, **kwargs: at("error")(
            message, *args, exc_info=True
        )
        methods["log"] = lambda _level, message, *args, **kwargs: at("info")(
            message, *args, **kwargs
        )
        methods["isEnabledFor"] = lambda _level: True
        return SimpleNamespace(**methods)

    def at_or_above_warning(self) -> List[Tuple[str, str, str]]:
        return [r for r in self.records if r[1] in WARNING_OR_ABOVE]

    def with_marker(self) -> List[Tuple[str, str, str]]:
        return [r for r in self.records if any(m in r[2] for m in MARKER_TEXTS)]


@pytest.fixture
def logs(monkeypatch) -> Recorder:
    recorder = Recorder()
    for module in (rules_engine, targeting_adapter, res, pattern_match, ffs):
        monkeypatch.setattr(module, "logger", recorder._bind(module.__name__))
    EVALUATION_NOTES.clear()
    yield recorder
    EVALUATION_NOTES.clear()


# -- the cases -----------------------------------------------------------------

DEBUG = "debug"  # a value that does not suit the operator: DEBUG only
ONCE = "once"  # a problem in the rule: one WARNING however often it is evaluated


@dataclass(frozen=True)
class Case:
    actual: Any
    expected: Any = None
    additional: Any = None
    expect: str = DEBUG

    def __str__(self) -> str:
        return f"{self.expect}:{self.actual!r}/{self.expected!r}/{self.additional!r}"


#: The operator has no branch that fails on a value; it is still evaluated with
#: the marker to show nothing is logged.
NO_FAILURE_BRANCH = "no failure branch"

DATE = "2024-06-01T10:00:00"
WINDOW = {"lat": 0, "lon": 0, "radius": 10}

#: Every OperatorType member, through rules_engine.apply_operator (the flag path,
#: and the service for every operator it does not implement itself).
ENGINE_CASES = {
    OperatorType.EQUALS: NO_FAILURE_BRANCH,
    OperatorType.NOT_EQUALS: NO_FAILURE_BRANCH,
    OperatorType.CONTAINS: NO_FAILURE_BRANCH,
    OperatorType.NOT_CONTAINS: NO_FAILURE_BRANCH,
    OperatorType.STARTS_WITH: NO_FAILURE_BRANCH,
    OperatorType.ENDS_WITH: NO_FAILURE_BRANCH,
    # Raises PatternUnevaluable when it cannot be evaluated; the ruleset owner
    # reports that with the pattern's digest (test_pattern_match.py).
    OperatorType.MATCH_REGEX: NO_FAILURE_BRANCH,
    OperatorType.IS_NULL: NO_FAILURE_BRANCH,
    OperatorType.IS_NOT_NULL: NO_FAILURE_BRANCH,
    OperatorType.GREATER_THAN: [Case(MARKER, 30)],
    OperatorType.GREATER_THAN_OR_EQUAL: [Case(MARKER, 30)],
    OperatorType.LESS_THAN: [Case(MARKER, 30)],
    OperatorType.LESS_THAN_OR_EQUAL: [Case(MARKER, 30)],
    OperatorType.IN: [Case(MARKER, "not-a-list", expect=ONCE)],
    OperatorType.NOT_IN: [Case(MARKER, "not-a-list", expect=ONCE)],
    OperatorType.BEFORE: [
        Case({"d": MARKER}, DATE),
        Case(MARKER, DATE),
        Case(DATE, {"not": "a date"}, expect=ONCE),
    ],
    OperatorType.AFTER: [
        Case({"d": MARKER}, DATE),
        Case(MARKER, DATE),
        Case(DATE, {"not": "a date"}, expect=ONCE),
    ],
    OperatorType.BETWEEN: [
        Case({"v": MARKER}, 1, 10),
        Case(MARKER, "2024-01-01", "2024-12-31"),
        Case(MARKER, 1, None, expect=ONCE),
        Case(DATE, {"not": "a date"}, "2024-12-31", expect=ONCE),
        Case(DATE, "2024-01-01", {"not": "a date"}, expect=ONCE),
    ],
    OperatorType.CONTAINS_ALL: [
        Case({"k": MARKER}, ["a"]),
        Case([MARKER], "not-a-list", expect=ONCE),
    ],
    OperatorType.CONTAINS_ANY: [
        Case({"k": MARKER}, ["a"]),
        Case([MARKER], "not-a-list", expect=ONCE),
    ],
    OperatorType.SEMANTIC_VERSION: [
        Case(MARKER, "1.0.0", "gte"),
        Case({"v": MARKER}, "1.0.0", "gte"),
        Case("1.2.3", 1, "gte", expect=ONCE),
        Case("1.2.3", "not-a-version", "gte", expect=ONCE),
        Case("1.2.3", "1.0.0", "sideways", expect=ONCE),
    ],
    OperatorType.ARRAY_LENGTH: [
        Case(MARKER_NUM, 3),
        Case([MARKER], "three", "eq", expect=ONCE),
        Case([MARKER], "three", "gte", expect=ONCE),
        Case([MARKER], [1, 2], "between", expect=ONCE),
        Case([MARKER], {"min": 1}, "between", expect=ONCE),
        Case([MARKER], {"min": "one", "max": 2}, "between", expect=ONCE),
        Case([MARKER], 1, "sideways", expect=ONCE),
    ],
    OperatorType.GEO_DISTANCE: [
        Case({"lat": MARKER, "lon": 1}, WINDOW),
        Case({"lat": MARKER_NUM, "lon": 0}, WINDOW),
        Case({"lat": 1, "lon": 1}, {"nowhere": 1}, expect=ONCE),
        Case({"lat": 1, "lon": 1}, {"lat": 95, "lon": 0, "radius": 1}, expect=ONCE),
        Case({"lat": 1, "lon": 1}, {"lat": 0, "lon": 0}, expect=ONCE),
        Case({"lat": 1, "lon": 1}, {**WINDOW, "radius": "far"}, expect=ONCE),
        Case({"lat": 1, "lon": 1}, {**WINDOW, "radius": -1}, expect=ONCE),
        Case({"lat": 1, "lon": 1}, {**WINDOW, "unit": "leagues"}, expect=ONCE),
        Case({"lat": 1, "lon": 1}, {**WINDOW, "comparison": "near"}, expect=ONCE),
    ],
    OperatorType.TIME_WINDOW: [
        Case(MARKER, {"days": [0]}),
        Case(MARKER_NUM, {"days": [0]}),
        Case(DATE, "not-an-object", expect=ONCE),
        Case(DATE, {"days": "monday"}, expect=ONCE),
        Case(DATE, {"days": [9]}, expect=ONCE),
        Case(DATE, {"start_time": "09:00"}, expect=ONCE),
        Case(DATE, {"start_time": "nine", "end_time": "five"}, expect=ONCE),
        Case(DATE, {"start_date": "2024-01-01"}, expect=ONCE),
        Case(DATE, {"start_date": "first", "end_date": "last"}, expect=ONCE),
        Case(DATE, {"start_date": "2024-02-01", "end_date": "2024-01-01"}, expect=ONCE),
        Case(DATE, {}, expect=ONCE),
        Case(DATE, {"start": "2024-01-01", "end": "2024-12-31"}, expect=ONCE),
        Case(DATE, {"days": [0], "timezone": "Nowhere/Atlantis"}, expect=ONCE),
        # The window is read before the value, so a window that cannot be read
        # is reported whatever the user sends, and the value is still not logged.
        Case(MARKER, {"start": "2024-01-01", "end": "2024-12-31"}, expect=ONCE),
    ],
    # Not implemented on the flag path: the condition never matches.
    OperatorType.PERCENTAGE_BUCKET: [Case(MARKER, 50, expect=ONCE)],
    OperatorType.JSON_PATH: [Case(MARKER, "$.a", expect=ONCE)],
    # Answered from resolved membership before any attribute is read
    # (segment_condition_holds); apply_operator never sees a value for them
    # (test_segment_condition.py), so there is no value to log.
    OperatorType.IN_SEGMENT: [],
    OperatorType.NOT_IN_SEGMENT: [],
}

#: The operators RulesEvaluationService implements itself (experiment targeting).
SERVICE_OPERATORS = {
    OperatorType.PERCENTAGE_BUCKET,
    OperatorType.JSON_PATH,
    OperatorType.ARRAY_LENGTH,
}

SERVICE_CASES = {
    OperatorType.PERCENTAGE_BUCKET: [Case(MARKER, "half")],
    OperatorType.JSON_PATH: [Case([MARKER], "$.5", "x")],
    OperatorType.ARRAY_LENGTH: [Case([MARKER], "three")],
}


def _flatten(table):
    return [
        pytest.param(op, case, id=f"{op.value}-{i}")
        for op, cases in table.items()
        if cases != NO_FAILURE_BRANCH
        for i, case in enumerate(cases)
    ]


def test_every_operator_has_an_entry():
    """A new operator fails here until its failure branches have cases."""
    missing = set(OperatorType) - set(ENGINE_CASES)
    assert not missing, f"no ENGINE_CASES entry for {sorted(m.value for m in missing)}"
    assert set(SERVICE_CASES) == SERVICE_OPERATORS


def test_the_service_implements_exactly_these_operators(logs, monkeypatch):
    """If the service takes over another operator, SERVICE_OPERATORS must say so."""
    delegated = []
    monkeypatch.setattr(
        res, "base_apply_operator", lambda op, *args: delegated.append(op) or False
    )
    service = res.RulesEvaluationService()
    for op in OperatorType:
        service._apply_operator_enhanced(op, "x", "y", None)
    assert set(OperatorType) - set(delegated) == SERVICE_OPERATORS


# -- driving a case -------------------------------------------------------------


def _rules(op: OperatorType, case: Case) -> TargetingRules:
    # model_construct: a rule-definition case is, by design, one the schema
    # would refuse on save; stored rules are not re-validated on evaluation.
    condition = Condition.model_construct(
        attribute="attr",
        operator=op,
        value=case.expected,
        additional_value=case.additional,
        attribute_type=None,
        validation_schema=None,
    )
    group = RuleGroup.model_construct(
        operator=LogicalOperator.AND, conditions=[condition], groups=[]
    )
    rule = TargetingRule.model_construct(
        id="r1", name=None, description=None, rule=group, rollout_percentage=100
    )
    return TargetingRules.model_construct(
        version="1.0", rules=[rule], default_rule=None
    )


def _via_engine(op, case):
    try:
        apply_operator(op, case.actual, case.expected, case.additional)
    except rules_engine.TimeWindowUnreadable:
        pass  # the evaluation is refused; the report is what is checked here


def _via_service(op, case):
    matched, _ = res.RulesEvaluationService().evaluate_rules_with_validation(
        _rules(op, case), {"attr": case.actual}, owner="experiment:exp-1"
    )
    assert matched is None


def _check(logs: Recorder, case: Case, drive) -> None:
    for _ in range(REPEAT):
        drive()
    assert logs.with_marker() == []
    assert logs.records, "nothing was captured: the capture is not working"
    loud = logs.at_or_above_warning()
    if case.expect == DEBUG:
        assert loud == []
    else:
        assert [level for _, level, _ in loud] == ["warning"], loud


@pytest.mark.regression
@pytest.mark.parametrize("op,case", _flatten(ENGINE_CASES))
def test_engine_failure_branches_log_no_value(logs, op, case):
    _check(logs, case, lambda: _via_engine(op, case))


@pytest.mark.regression
@pytest.mark.parametrize(
    "op,case",
    _flatten({op: c for op, c in ENGINE_CASES.items() if op not in SERVICE_OPERATORS})
    + _flatten(SERVICE_CASES),
)
def test_service_failure_branches_log_no_value(logs, op, case):
    _check(logs, case, lambda: _via_service(op, case))


@pytest.mark.parametrize(
    "op", [op for op, c in ENGINE_CASES.items() if c == NO_FAILURE_BRANCH]
)
def test_operators_without_a_failure_branch_log_nothing(logs, op):
    expected = None if op in (OperatorType.IS_NULL, OperatorType.IS_NOT_NULL) else "x"
    apply_operator(op, MARKER, expected)
    assert logs.records == []


# -- exceptions -----------------------------------------------------------------


@pytest.mark.regression
def test_an_exception_is_logged_by_type_only(logs, monkeypatch):
    """Both service entry points log an unexpected error without its text."""

    def raising(*args, **kwargs):
        raise ValueError(f"cannot handle {MARKER}")

    monkeypatch.setattr(res, "base_apply_operator", raising)
    service = res.RulesEvaluationService()
    rules = _rules(OperatorType.EQUALS, Case(MARKER, "x"))

    service.evaluate_rules_with_validation(
        rules, {"attr": MARKER}, owner="experiment:e"
    )
    service.evaluate(rules, {"attr": MARKER}, skip_cache=True)

    errors = [r for r in logs.records if r[1] == "error"]
    assert len(errors) == 2, logs.records
    assert all("ValueError" in text for _, _, text in errors)
    assert logs.with_marker() == []


@pytest.mark.parametrize(
    "operator", [OperatorType.CONTAINS_ALL, OperatorType.CONTAINS_ANY]
)
def test_contains_on_a_string_with_non_string_items_does_not_match(logs, operator):
    """A string value with non-string items no longer raises (#270), and logs no value."""
    assert apply_operator(operator, MARKER, [1]) is False
    assert logs.with_marker() == []


# -- the targeting adapter --------------------------------------------------------


def test_unconvertible_rules_are_reported_once_per_owner(logs):
    broken = {
        "logical_operator": "AND",
        "groups": [
            {"conditions": [{"attribute": "a", "operator": "gt", "value": MARKER}]}
        ],
    }
    for _ in range(REPEAT):
        assert targeting_adapter.normalise_targeting_rules(broken, "flag:a") is None
    assert targeting_adapter.normalise_targeting_rules(broken, "flag:b") is None
    loud = logs.at_or_above_warning()
    assert [text.split(" that ")[0] for _, _, text in loud] == [
        "Ignoring dashboard targeting rules for flag:a",
        "Ignoring dashboard targeting rules for flag:b",
    ]
    assert logs.with_marker() == []


# -- end to end: a flag with dashboard rules -------------------------------------


@pytest.mark.regression
def test_flag_evaluation_logs_no_attribute_values(logs, monkeypatch):
    monkeypatch.setattr(ffs, "MetricsService", MagicMock())
    flag = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000001",
        key="attribute-values",
        status=FeatureFlagStatus.ACTIVE,
        rollout_percentage=100,
        targeting_rules={
            "logical_operator": "AND",
            "groups": [
                {
                    "logical_operator": "AND",
                    "conditions": [
                        {"attribute": "age", "operator": "greater_than", "value": "30"},
                        {
                            "attribute": "app.version",
                            "operator": "semver_gte",
                            "value": "1.0.0",
                        },
                    ],
                }
            ],
        },
    )
    service = ffs.FeatureFlagService(db=MagicMock())
    for _ in range(REPEAT):
        result = service.evaluate_flag_detailed(
            flag, "user-1", {"age": MARKER, "app": {"version": MARKER}}
        )
        assert result == {"enabled": True, "reason": "rollout", "rule_id": None}

    assert logs.with_marker() == []
    assert logs.at_or_above_warning() == []
    engine = [r for r in logs.records if r[0] == rules_engine.__name__]
    assert len(engine) == 2 * REPEAT, logs.records  # both conditions, every time


# -- the once-helper ---------------------------------------------------------------


def test_the_once_helper_is_capped():
    from backend.app.core.log_once import OnceLog

    once = OnceLog(limit=3)
    assert [once.first("o", r) for r in "abc"] == [True, True, True]
    assert once.first("o", "a") is False
    assert once.first("o", "d") is True  # full: emptied, then remembered
    assert once.seen == {("o", "d")}
    assert once.first("o", "a") is True
