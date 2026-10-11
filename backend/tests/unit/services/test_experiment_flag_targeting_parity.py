"""
Experiment targeting answers a rule the way flag targeting does (#1144).

An experiment is targeted by ``RulesEvaluationService``, through
``AssignmentService._evaluate_experiment_targeting``; a flag by the rules
engine, through ``FeatureFlagService._match_targeting_rule``. Both read the
same stored rules through the targeting adapter. Until #1144 they answered
some of them differently: every ``semver_*`` comparison meant ">=" for an
experiment, so ``semver_lt 2.0.0`` enrolled 3.0.0 and refused 1.5.0, and
``geo_within_radius`` never matched an experiment's user.

This file drives both real evaluators (neither is rebuilt here) with the same
stored rules, user and context, and asserts the same answer, over:

* every case of ``tests/sdk-contract/ruleset-vectors.json`` whose rules the
  adapter can evaluate;
* a battery for each operator the adapter supports
  (``targeting_adapter.DASHBOARD_OPERATORS``), plus the native
  ``semantic_version`` and ``geo_distance`` spellings;
* the cases of #1144, with their expected answers.

``time_window`` is left out of all three: the two evaluators read its value
differently, and that is #1145.

The parity holds for conditions without ``attribute_type``, which is every
condition the dashboard stores, and no case here sets one. A native condition
that sets ``attribute_type`` is a separate, known difference: the experiment
path checks the context value against it before any operator runs
(``RulesEvaluationService.validate_user_context``), and a value that fails the
check refuses the whole ruleset, where a flag still evaluates it. A
``geo_distance`` with ``"attribute_type": "geo_coordinate"`` refuses a
``{"lat", "lon"}`` location, and a ``semantic_version`` with
``"attribute_type": "semantic_version"`` refuses ``"v3.0.0"``.

The rules both evaluators are given have every rule-level
``rollout_percentage`` at 100. A partial rollout is not an operator: an
experiment buckets a matching user on the rule's id and the flag service on
the flag's key, by design (#533), so at 100 the answer is what the conditions
say and nothing else.
"""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Set
from unittest import mock
from unittest.mock import MagicMock, Mock

import pytest

from backend.app.core.targeting_adapter import (
    DASHBOARD_OPERATORS,
    normalise_targeting_rules,
)
from backend.app.models.experiment import ExperimentStatus
from backend.app.schemas.targeting_rule import OperatorType, RuleGroup, TargetingRules
from backend.app.services import assignment_service, feature_flag_service
from backend.app.services.assignment_service import AssignmentService
from backend.app.services.feature_flag_service import FeatureFlagService
from backend.scripts import generate_ruleset_vectors as generator

pytestmark = pytest.mark.unit

USER = generator.USER
#: Not a member of the vectors' segment (``generator.VECTOR_SEGMENT_MEMBERS``).
OTHER_USER = "user-7"

#: The operator whose two readings differ; #1145 makes them one.
TIME_WINDOW_ISSUE = "#1145"


# ---------------------------------------------------------------------------
# The two evaluators, driven as the services drive them
# ---------------------------------------------------------------------------


def experiment_answer(
    raw: Any, user_id: str, context: Optional[Dict[str, Any]]
) -> Optional[str]:
    """The id of the rule an experiment admits this user under, or ``None``."""
    service = AssignmentService(Mock())
    experiment = SimpleNamespace(id="parity", targeting_rules=copy.deepcopy(raw))
    with mock.patch.object(
        assignment_service, "resolve_segment_memberships", generator.stub_memberships
    ):
        result = service._evaluate_experiment_targeting(
            experiment,
            AssignmentService._build_targeting_context(user_id, context),
            user_id=user_id,
        )
    if not result["eligible"]:
        return None
    # Only rules with something to evaluate are compared, so an admitted user
    # always matched a rule.
    assert result["rule_id"] is not None, result
    return result["rule_id"]


def flag_answer(
    raw: Any, user_id: str, context: Optional[Dict[str, Any]]
) -> Optional[str]:
    """The id of the rule a flag matches for this user, or ``None``.

    A ruleset the flag service abandons answers ``None``: a pattern it cannot
    evaluate, a segment whose membership is unknown, or any other error
    (``evaluate_flag_detailed`` turns each into "off, reason ``error``"). An
    experiment does not admit the user in any of those cases either.
    """
    flag = SimpleNamespace(key="parity", targeting_rules=copy.deepcopy(raw))
    with mock.patch.object(
        feature_flag_service, "resolve_segment_memberships", generator.stub_memberships
    ):
        try:
            matched = FeatureFlagService(db=None)._match_targeting_rule(
                flag, user_id, context or {}
            )
        except Exception:
            return None
    return None if matched is None else matched[0]


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


def evaluable(raw: Any) -> Optional[TargetingRules]:
    """The adapter's reading of *raw*, when it has a rule to evaluate."""
    if not isinstance(raw, dict):
        # The legacy list shape is evaluated by the flag service alone.
        return None
    rules = normalise_targeting_rules(raw, owner="parity")
    if rules is None or (not rules.rules and rules.default_rule is None):
        return None
    return rules


def operators_in(rules: TargetingRules) -> Set[OperatorType]:
    found: Set[OperatorType] = set()

    def walk(group: RuleGroup) -> None:
        for condition in group.conditions:
            found.add(condition.operator)
        for nested in group.groups or []:
            walk(nested)

    for rule in rules.rules:
        walk(rule.rule)
    if rules.default_rule is not None:
        walk(rules.default_rule.rule)
    return found


def at_full_rollout(raw: Dict[str, Any]) -> Dict[str, Any]:
    """*raw* with every rule-level ``rollout_percentage`` at 100."""
    raw = copy.deepcopy(raw)
    if "rules" in raw:
        for rule in raw.get("rules") or []:
            if isinstance(rule, dict):
                rule["rollout_percentage"] = 100
        if isinstance(raw.get("default_rule"), dict):
            raw["default_rule"]["rollout_percentage"] = 100
    else:
        raw.pop("rollout_percentage", None)
    return raw


def dashboard(attribute: str, operator: str, value: Any) -> Dict[str, Any]:
    """One condition in the shape the dashboard stores."""
    condition = {"attribute": attribute, "operator": operator, "value": value}
    return {
        "logical_operator": "AND",
        "groups": [{"logical_operator": "AND", "conditions": [condition]}],
    }


def native(
    attribute: str, operator: str, value: Any, additional_value: Any = None
) -> Dict[str, Any]:
    """One condition in the native ``TargetingRules`` shape."""
    condition: Dict[str, Any] = {
        "attribute": attribute,
        "operator": operator,
        "value": value,
    }
    if additional_value is not None:
        condition["additional_value"] = additional_value
    return {
        "version": "1.0",
        "rules": [
            {
                "id": "native",
                "rule": {"operator": "and", "conditions": [condition]},
                "rollout_percentage": 100,
            }
        ],
    }


def disagreements(cases) -> List[str]:
    """Every ``(label, raw, user_id, context)`` the two evaluators answer apart."""
    out = []
    for label, raw, user_id, context in cases:
        experiment = experiment_answer(raw, user_id, context)
        flag = flag_answer(raw, user_id, context)
        if experiment != flag:
            out.append(
                f"{label}: user={user_id} context={context!r} "
                f"experiment={experiment!r} flag={flag!r}"
            )
    return out


# ---------------------------------------------------------------------------
# 1. The SDK ruleset vectors
# ---------------------------------------------------------------------------


def vector_cases():
    vectors = json.loads(generator.VECTORS_PATH.read_text(encoding="utf-8"))
    stored = {flag["key"]: flag for flag in vectors["stored_flags"]}
    cases, skipped_time_window = [], 0
    for case in vectors["cases"]:
        raw = stored[case["flag"]]["targeting_rules"]
        rules = evaluable(raw)
        if rules is None:
            continue
        if OperatorType.TIME_WINDOW in operators_in(rules):
            skipped_time_window += 1  # #1145
            continue
        cases.append(
            (case["id"], at_full_rollout(raw), case["user_id"], case["context"])
        )
    return cases, skipped_time_window


def test_the_vectors_get_the_same_answer_from_both_evaluators():
    cases, skipped_time_window = vector_cases()
    # Not vacuous: most of the 2181 cases have rules to evaluate, and the
    # time_window flag's cases are the only ones left out for #1145.
    assert len(cases) > 1500, len(cases)
    assert 0 < skipped_time_window <= 2, skipped_time_window
    found = disagreements(cases)
    assert not found, f"{len(found)} of {len(cases)} cases disagree:\n" + "\n".join(
        found[:25]
    )


# ---------------------------------------------------------------------------
# 2. Every operator the adapter supports
# ---------------------------------------------------------------------------

SEMVER_ACTUALS = [
    "1.5.0",
    "1.9.9",
    "2.0.0",
    "2.0.1",
    "2.1.0",
    "3.0.0",
    "2.0.0-beta.1",
    "2.0.0+build.7",
    "v2.0.0",
    "2.0",
    "not-a-version",
    20,
]
PARIS = {"lat": 48.8566, "lon": 2.3522, "radius": 50, "unit": "km"}
LOCATIONS = [
    {"lat": 48.86, "lon": 2.35},  # Paris
    {"lat": 48.8049, "lon": 2.1204},  # Versailles, about 17 km
    {"lat": 49.2583, "lon": 4.0317},  # Reims, about 130 km
    {"lat": 51.5074, "lon": -0.1278},  # London, about 340 km
    [48.86, 2.35],
    [51.5074, -0.1278],
    "48.86,2.35",
    {"lat": 91, "lon": 2.35},
    {"lat": "x", "lon": 2.35},
]

#: Dashboard operator -> (attribute, stored value, context values). Every
#: context value is also tried absent (``{}``) and for two users.
BATTERY: Dict[str, Any] = {
    "equals": ("country", "US", ["US", "us", "FR", 1, True, None]),
    "not_equals": ("country", "US", ["US", "FR", None]),
    "contains": ("email", "@example.com", ["a@example.com", "a@b.org", 5]),
    "not_contains": ("email", "@example.com", ["a@example.com", "a@b.org", 5]),
    "starts_with": ("email", "admin", ["admin@x.org", "user@x.org", 5]),
    "ends_with": ("email", ".org", ["a@b.org", "a@b.com", 5]),
    "greater_than": ("age", "30", [29, 30, 31, "31", 30.5, "abc", True]),
    "less_than": ("age", "30", [29, 30, 31, "29", 29.5, "abc"]),
    "greater_than_or_equal": ("age", "30", [29, 30, 31, "30", "abc"]),
    "less_than_or_equal": ("age", "30", [29, 30, 31, "30", "abc"]),
    "in": ("country", ["US", "CA"], ["US", "CA", "FR", ["US"], 1]),
    "not_in": ("country", ["US", "CA"], ["US", "FR", 1]),
    "regex": ("email", "^ab", ["abc", "xbc", 5]),
    "is_null": ("plan", None, [None, "", "pro", [], 0]),
    "is_not_null": ("plan", None, [None, "", "pro", [], 0]),
    "array_contains": ("tags", ["a", "b"], [["a", "b", "c"], ["a"], [], "ab"]),
    "array_intersects": ("tags", ["a", "z"], [["a"], ["b"], [], "a"]),
    "geo_within_radius": ("location", PARIS, LOCATIONS),
    "semver_eq": ("app_version", "2.0.0", SEMVER_ACTUALS),
    "semver_gt": ("app_version", "2.0.0", SEMVER_ACTUALS),
    "semver_gte": ("app_version", "2.0.0", SEMVER_ACTUALS),
    "semver_lt": ("app_version", "2.0.0", SEMVER_ACTUALS),
    "semver_lte": ("app_version", "2.0.0", SEMVER_ACTUALS),
    "in_segment": ("segment", generator.VECTOR_SEGMENT, []),
    "not_in_segment": ("segment", generator.VECTOR_SEGMENT, []),
}

#: The native spellings of the two operators #1144 is about.
NATIVE_BATTERY: Dict[str, Any] = {
    **{
        f"semantic_version/{comparison}": (
            native("app_version", "semantic_version", "2.0.0", comparison),
            "app_version",
            SEMVER_ACTUALS,
        )
        for comparison in (None, "eq", "gt", "gte", "lt", "lte")
    },
    "geo_distance/radius": (
        native("location", "geo_distance", [48.8566, 2.3522], 50),
        "location",
        LOCATIONS,
    ),
    "geo_distance/object": (
        native("location", "geo_distance", [48.8566, 2.3522], {"radius": 50}),
        "location",
        LOCATIONS,
    ),
}


def battery_cases(label, raw, attribute, values):
    contexts = [{attribute: value} for value in values] + [{}, None]
    return [
        (label, raw, user_id, context)
        for context in contexts
        for user_id in (USER, OTHER_USER)
    ]


def test_every_operator_the_adapter_supports_is_compared():
    """A new dashboard operator fails here until it has a battery."""
    assert set(BATTERY) | {"time_window"} == set(DASHBOARD_OPERATORS), (
        "time_window is compared by " + TIME_WINDOW_ISSUE
    )


@pytest.mark.regression
@pytest.mark.parametrize("operator", sorted(BATTERY))
def test_both_evaluators_answer_each_operator_alike(operator):
    attribute, value, values = BATTERY[operator]
    raw = dashboard(attribute, operator, value)
    assert evaluable(raw) is not None
    cases = battery_cases(operator, raw, attribute, values)
    found = disagreements(cases)
    assert not found, "\n".join(found)
    # The battery is not vacuous: it has users the operator admits and users
    # it does not.
    answers = {flag_answer(raw, user_id, context) for _, raw, user_id, context in cases}
    assert answers == {None, "dashboard"}, answers


@pytest.mark.regression
@pytest.mark.parametrize("spelling", sorted(NATIVE_BATTERY))
def test_both_evaluators_answer_the_native_spellings_alike(spelling):
    raw, attribute, values = NATIVE_BATTERY[spelling]
    cases = battery_cases(spelling, raw, attribute, values)
    found = disagreements(cases)
    assert not found, "\n".join(found)
    answers = {flag_answer(raw, user_id, context) for _, raw, user_id, context in cases}
    assert answers == {None, "native"}, answers


# ---------------------------------------------------------------------------
# 3. The cases of #1144
# ---------------------------------------------------------------------------

ISSUE_CASES = [
    ("semver_lt", "app_version", "2.0.0", {"app_version": "3.0.0"}, False),
    ("semver_lt", "app_version", "2.0.0", {"app_version": "1.5.0"}, True),
    ("semver_lte", "app_version", "2.0.0", {"app_version": "9.9.9"}, False),
    ("semver_lte", "app_version", "2.0.0", {"app_version": "2.0.0"}, True),
    ("semver_eq", "app_version", "2.0.0", {"app_version": "2.1.0"}, False),
    ("semver_eq", "app_version", "2.0.0", {"app_version": "2.0.0"}, True),
    ("semver_gt", "app_version", "2.0.0", {"app_version": "2.0.0"}, False),
    ("semver_gt", "app_version", "2.0.0", {"app_version": "2.1.0"}, True),
    ("geo_within_radius", "location", PARIS, {"location": LOCATIONS[0]}, True),
    ("geo_within_radius", "location", PARIS, {"location": LOCATIONS[3]}, False),
]


@pytest.mark.regression
@pytest.mark.parametrize(
    "operator, attribute, value, context, enrolled",
    ISSUE_CASES,
    ids=[f"{case[0]}-{json.dumps(case[3])}" for case in ISSUE_CASES],
)
def test_the_issue_cases(operator, attribute, value, context, enrolled):
    raw = dashboard(attribute, operator, value)
    assert (experiment_answer(raw, USER, context) is not None) is enrolled
    assert (flag_answer(raw, USER, context) is not None) is enrolled


@pytest.mark.regression
def test_a_stored_assignment_is_kept_whatever_targeting_now_answers():
    """Sticky assignments are kept: a 3.0.0 user enrolled under the old reading
    of ``semver_lt 2.0.0`` keeps the variant, and targeting is not asked."""
    db = MagicMock()
    experiment = SimpleNamespace(
        id="parity",
        status=ExperimentStatus.ACTIVE,
        targeting_rules=dashboard("app_version", "semver_lt", "2.0.0"),
    )
    db.query.return_value.options.return_value.filter.return_value.first.return_value = experiment
    service = AssignmentService(db)
    stored = SimpleNamespace(variant_id="variant-b")
    kept = {"variant_id": "variant-b", "assigned": True}
    with (
        mock.patch.object(service, "_stored_assignment", return_value=stored),
        mock.patch.object(service, "_sticky_result", return_value=kept) as sticky,
        mock.patch.object(
            service, "check_eligibility", side_effect=AssertionError("asked")
        ),
    ):
        result = service.assign_user(
            "user-3", "parity", context={"app_version": "3.0.0"}
        )
    assert result is kept
    sticky.assert_called_once()
    db.add.assert_not_called()
