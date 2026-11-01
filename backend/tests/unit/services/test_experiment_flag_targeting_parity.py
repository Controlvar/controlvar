"""
Experiment targeting answers a rule the way flag targeting does (#1144, #1145, #1159).

An experiment is targeted by ``RulesEvaluationService``, through
``AssignmentService._evaluate_experiment_targeting``; a flag by the rules
engine, through ``FeatureFlagService._match_targeting_rule``. Both read the
same stored rules through the targeting adapter. Until #1144 they answered
some of them differently: every ``semver_*`` comparison meant ">=" for an
experiment, so ``semver_lt 2.0.0`` enrolled 3.0.0 and refused 1.5.0, and
``geo_within_radius`` never matched an experiment's user. Until #1145
``time_window`` was read two ways: an experiment read ISO datetimes in
``start``/``end`` and never matched an ``HH:MM`` window, while a flag read
``HH:MM`` and treated a window with no key it reads as always open.

This file drives both real evaluators (neither is rebuilt here) with the same
stored rules, user and context, and asserts the same answer, over:

* every case of ``tests/sdk-contract/ruleset-vectors.json`` whose rules the
  adapter can evaluate;
* a battery for each operator the adapter supports
  (``targeting_adapter.DASHBOARD_OPERATORS``), plus the native
  ``semantic_version``, ``geo_distance`` and ``time_window`` spellings;
* a battery for the native ``array_length``, each comparison alone and inside
  a ``NOT``, and one for conditions that carry ``attribute_type`` (#1159);
* the cases of #1144 and #1145, with their expected answers, including a
  ``time_window`` the engine cannot read: the whole ruleset is abandoned, as
  for a pattern that cannot be evaluated (the flag is off with reason
  ``error``, the user is not eligible, a segment is unavailable).

Until #1159 an experiment read two more things its own way. ``array_length``
was an exact length of a list, whatever ``additional_value`` said, so
``gt 2`` never enrolled a three-item list and a ``NOT`` around it enrolled
everyone. And a condition's ``attribute_type`` was checked against the
context's value before any operator ran: a value that failed the check refused
the whole ruleset, where a flag still evaluated it (a ``geo_distance`` with
``"attribute_type": "geo_coordinate"`` refused a ``{"lat", "lon"}`` location,
and a ``semantic_version`` with ``"attribute_type": "semantic_version"``
refused ``"v2.0.0"``). Now ``array_length`` is the rules engine's on both, and
neither evaluator reads ``attribute_type``.

The rules both evaluators are given have every rule-level
``rollout_percentage`` at 100. A partial rollout is not an operator: an
experiment buckets a matching user on the rule's id and the flag service on
the flag's key, by design (#533), so at 100 the answer is what the conditions
say and nothing else.
"""

from __future__ import annotations

import copy
import json
import uuid
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Set, Tuple
from unittest import mock
from unittest.mock import MagicMock, Mock

import pytest

from backend.app.core import rules_engine
from backend.app.core.targeting_adapter import (
    DASHBOARD_OPERATORS,
    normalise_targeting_rules,
)
from backend.app.models.experiment import ExperimentStatus
from backend.app.models.feature_flag import FeatureFlagStatus
from backend.app.models.segment import SegmentStatus
from backend.app.schemas.targeting_rule import (
    Condition,
    LogicalOperator,
    OperatorType,
    RuleGroup,
    TargetingRule,
    TargetingRules,
)
from backend.app.services import (
    assignment_service,
    feature_flag_service,
    segment_membership,
)
from backend.app.services.assignment_service import AssignmentService
from backend.app.services.feature_flag_service import FeatureFlagService
from backend.app.services.rules_evaluation_service import RulesEvaluationService
from backend.scripts import generate_ruleset_vectors as generator

pytestmark = pytest.mark.unit

USER = generator.USER
#: Not a member of the vectors' segment (``generator.VECTOR_SEGMENT_MEMBERS``).
OTHER_USER = "user-7"

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


def dashboard(
    attribute: str, operator: str, value: Any, group_operator: str = "AND"
) -> Dict[str, Any]:
    """One condition in the shape the dashboard stores."""
    condition = {"attribute": attribute, "operator": operator, "value": value}
    return {
        "logical_operator": "AND",
        "groups": [{"logical_operator": group_operator, "conditions": [condition]}],
    }


def native(
    attribute: str,
    operator: str,
    value: Any,
    additional_value: Any = None,
    group_operator: str = "and",
    attribute_type: Optional[str] = None,
) -> Dict[str, Any]:
    """One condition in the native ``TargetingRules`` shape."""
    condition: Dict[str, Any] = {
        "attribute": attribute,
        "operator": operator,
        "value": value,
    }
    if additional_value is not None:
        condition["additional_value"] = additional_value
    if attribute_type is not None:
        condition["attribute_type"] = attribute_type
    return {
        "version": "1.0",
        "rules": [
            {
                "id": "native",
                "rule": {"operator": group_operator, "conditions": [condition]},
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


#: The vectors' flags whose rules the adapter does not evaluate, so their cases
#: are not compared: the legacy list shape, a value with no rule (rollout only,
#: empty, or no rules), and rules the adapter cannot convert. Every other case
#: is compared.
NOT_COMPARED = {
    "drapeau-été",
    "empty-list",
    "empty-object",
    "legacy-context",
    "legacy-user-ids",
    "no-rules",
    "rollout-0",
    "rollout-1",
    "rollout-100",
    "rollout-25",
    "rollout-50",
    "rollout-99",
    "unconvertible-attribute",
    "unconvertible-number",
    "unconvertible-operator",
}


def vector_cases():
    vectors = json.loads(generator.VECTORS_PATH.read_text(encoding="utf-8"))
    stored = {flag["key"]: flag for flag in vectors["stored_flags"]}
    cases, with_time_window, not_compared = [], 0, set()
    for case in vectors["cases"]:
        raw = stored[case["flag"]]["targeting_rules"]
        rules = evaluable(raw)
        if rules is None:
            not_compared.add(case["flag"])
            continue
        if OperatorType.TIME_WINDOW in operators_in(rules):
            with_time_window += 1
        cases.append(
            (case["id"], at_full_rollout(raw), case["user_id"], case["context"])
        )
    return cases, with_time_window, not_compared


def test_the_vectors_get_the_same_answer_from_both_evaluators():
    cases, with_time_window, not_compared = vector_cases()
    # Exact, so a case cannot stop being compared unseen: 2039 of the 2242
    # cases have rules to evaluate, the time_window flag's two among them (it
    # was left out until #1145) and the value flags' 51 (#1497), and the 203
    # others are NOT_COMPARED's.
    assert not_compared == NOT_COMPARED
    assert len(cases) == 2039, len(cases)
    assert with_time_window == 2, with_time_window
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

#: A Monday, 12:00 UTC: the moment a ``time_window`` case is evaluated at. It is
#: the attribute's value, so no case reads the clock.
MOMENT = "2026-01-05T12:00:00+00:00"
MOMENTS = [
    MOMENT,
    "2026-01-05T10:00:00+00:00",
    "2026-01-05T13:00:30Z",  # the end minute is included
    "2026-01-05T14:00:00+00:00",
    "2026-01-05T12:00:00",
    "2026-01-10T12:00:00+00:00",  # a Saturday
    "2026-02-02T12:00:00+00:00",  # a Monday in February
    "not a time",
    5,
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
    "time_window": ("now", {"start": "11:00", "end": "13:00"}, MOMENTS),
}

#: The native spellings of the operators #1144 and #1145 are about. A native
#: ``time_window`` must carry ``start`` and ``end`` (the schema asks for them),
#: which neither evaluator reads.
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
    **{
        f"time_window/{spelling}": (
            native("now", "time_window", {"start": "x", "end": "y", **keys}),
            "now",
            MOMENTS,
        )
        for spelling, keys in {
            "start_time": {"start_time": "11:00", "end_time": "13:00"},
            "days": {"days": [0]},
            "dates": {"start_date": "2026-01-01", "end_date": "2026-01-31"},
            "timezone": {
                "start_time": "12:30",
                "end_time": "13:30",
                "timezone": "Europe/Paris",
            },
        }.items()
    },
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
    assert set(BATTERY) == set(DASHBOARD_OPERATORS)


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
# 2b. array_length and attribute_type (#1159)
# ---------------------------------------------------------------------------

#: Values with each length around the rules' 2 (lists, text and objects), and
#: values with no length. Every value is also tried absent and for two users.
LENGTHS = [
    [],
    ["a"],
    ["a", "b"],
    ["a", "b", "c"],
    "",
    "ab",
    "abc",
    {},
    {"a": 1},
    {"a": 1, "b": 2},
    {"a": 1, "b": 2, "c": 3},
    None,
    2,
    2.5,
    True,
]

#: Each comparison ``array_length`` takes, alone and inside a ``NOT``.
ARRAY_LENGTH_BATTERY: Dict[str, Any] = {
    **{
        f"{comparison or 'none'}/{wrapper}": native(
            "tags", "array_length", 2, comparison, group_operator=wrapper
        )
        for comparison in (None, "eq", "gt", "gte", "lt", "lte")
        for wrapper in ("and", "not")
    },
    **{
        f"between/{wrapper}": native(
            "tags",
            "array_length",
            {"min": 1, "max": 2},
            "between",
            group_operator=wrapper,
        )
        for wrapper in ("and", "not")
    },
}


@pytest.mark.regression
@pytest.mark.parametrize("spelling", sorted(ARRAY_LENGTH_BATTERY))
def test_array_length_means_one_thing(spelling):
    """The rules engine's ``array_length`` on both evaluators: the length of a
    list, text or object, compared as ``additional_value`` says. An experiment
    used to read only an exact length of a list, so ``gt``, ``between`` and
    text never enrolled anyone, and a ``NOT`` around them enrolled everyone."""
    raw = ARRAY_LENGTH_BATTERY[spelling]
    assert evaluable(raw) is not None
    cases = battery_cases(spelling, raw, "tags", LENGTHS)
    found = disagreements(cases)
    assert not found, "\n".join(found)
    answers = {flag_answer(raw, user_id, context) for _, raw, user_id, context in cases}
    assert answers == {None, "native"}, answers


#: Conditions that carry ``attribute_type``: the two of #1159, each alone and
#: inside a ``NOT``, and a type that does not match the attribute's values.
ATTRIBUTE_TYPE_BATTERY: Dict[str, Any] = {
    **{
        f"geo_coordinate/{wrapper}": (
            native(
                "location",
                "geo_distance",
                [48.8566, 2.3522],
                50,
                group_operator=wrapper,
                attribute_type="geo_coordinate",
            ),
            "location",
            LOCATIONS,
        )
        for wrapper in ("and", "not")
    },
    **{
        f"semantic_version/{wrapper}": (
            native(
                "app_version",
                "semantic_version",
                "2.0.0",
                "gte",
                group_operator=wrapper,
                attribute_type="semantic_version",
            ),
            "app_version",
            SEMVER_ACTUALS,
        )
        for wrapper in ("and", "not")
    },
    "number on text": (
        native("plan", "eq", "premium", attribute_type="number"),
        "plan",
        ["premium", "basic", 1, None],
    ),
}


@pytest.mark.regression
@pytest.mark.parametrize("spelling", sorted(ATTRIBUTE_TYPE_BATTERY))
def test_attribute_type_conditions_enrol_like_flags(spelling):
    """``attribute_type`` decides nothing: an experiment used to check the
    context's value against it first and refuse the whole ruleset when the
    value failed the check, where a flag answered the condition."""
    raw, attribute, values = ATTRIBUTE_TYPE_BATTERY[spelling]
    assert evaluable(raw) is not None
    cases = battery_cases(spelling, raw, attribute, values)
    found = disagreements(cases)
    assert not found, "\n".join(found)
    answers = {flag_answer(raw, user_id, context) for _, raw, user_id, context in cases}
    assert answers == {None, "native"}, answers


#: The table of #1159: each of these users is enrolled, on both evaluators.
ATTRIBUTE_TYPE_ISSUE_CASES = [
    ("geo_coordinate/and", {"location": {"lat": 48.86, "lon": 2.35}}),
    ("semantic_version/and", {"app_version": "v2.0.0"}),
    ("number on text", {"plan": "premium"}),
]


@pytest.mark.regression
@pytest.mark.parametrize(
    "spelling, context",
    ATTRIBUTE_TYPE_ISSUE_CASES,
    ids=[case[0] for case in ATTRIBUTE_TYPE_ISSUE_CASES],
)
def test_the_attribute_type_issue_cases(spelling, context):
    raw = ATTRIBUTE_TYPE_BATTERY[spelling][0]
    assert experiment_answer(raw, USER, context) == "native"
    assert flag_answer(raw, USER, context) == "native"


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


# ---------------------------------------------------------------------------
# 4. The cases of #1145
# ---------------------------------------------------------------------------

#: (label, stored rules, enrolled) at MOMENT. Each answer used to differ
#: between the two evaluators, or be "always open" on flags, unless marked.
TIME_WINDOW_CASES = [
    # The issue's three rows.
    (
        "dashboard ISO now-1h..now+1h (#1156 would read it)",
        dashboard(
            "now",
            "time_window",
            {"start": "2026-01-05T11:00:00+00:00", "end": "2026-01-05T13:00:00+00:00"},
        ),
        False,
    ),
    (
        "dashboard HH:MM including now",
        dashboard("now", "time_window", {"start": "11:00", "end": "13:00"}),
        True,
    ),
    (
        "dashboard HH:MM excluding now (unchanged)",
        dashboard("now", "time_window", {"start": "14:00", "end": "15:00"}),
        False,
    ),
    (
        "native start/end in 2000",
        native(
            "now",
            "time_window",
            {"start": "2000-01-01T00:00:00", "end": "2000-01-02T00:00:00"},
        ),
        False,
    ),
    (
        "native start/end in 2099",
        native(
            "now",
            "time_window",
            {"start": "2099-01-01T00:00:00", "end": "2099-01-02T00:00:00"},
        ),
        False,
    ),
    # No key the engine reads: these match nobody.
    (
        "native ISO start/end including now",
        native(
            "now",
            "time_window",
            {"start": "2026-01-05T11:00:00+00:00", "end": "2026-01-05T13:00:00+00:00"},
        ),
        False,
    ),
    (
        "native HH:MM start/end",
        native("now", "time_window", {"start": "11:00", "end": "13:00"}),
        False,
    ),
    (
        "native start/end with a timezone only",
        native(
            "now",
            "time_window",
            {"start": "11:00", "end": "13:00", "timezone": "UTC"},
        ),
        False,
    ),
    # The keys the engine reads, in the native shape.
    (
        "native start_time/end_time including now",
        native(
            "now",
            "time_window",
            {"start": "x", "end": "y", "start_time": "11:00", "end_time": "13:00"},
        ),
        True,
    ),
    (
        "NOT around an HH:MM window including now",
        dashboard("now", "time_window", {"start": "11:00", "end": "13:00"}, "NOT"),
        False,
    ),
    (
        "NOT around an HH:MM window excluding now",
        dashboard("now", "time_window", {"start": "14:00", "end": "15:00"}, "NOT"),
        True,
    ),
    (
        "native start_date/end_date including now",
        native(
            "now",
            "time_window",
            {
                "start": "x",
                "end": "y",
                "start_date": "2026-01-01",
                "end_date": "2026-01-31",
            },
        ),
        True,
    ),
]


@pytest.mark.regression
@pytest.mark.parametrize(
    "label, raw, enrolled",
    TIME_WINDOW_CASES,
    ids=[case[0] for case in TIME_WINDOW_CASES],
)
def test_the_time_window_cases(label, raw, enrolled):
    context = {"now": MOMENT}
    assert evaluable(raw) is not None
    assert (experiment_answer(raw, USER, context) is not None) is enrolled
    assert (flag_answer(raw, USER, context) is not None) is enrolled


def constructed(
    value: Any, group_operator: LogicalOperator = LogicalOperator.AND
) -> TargetingRules:
    """A ``time_window`` rule the schema would refuse, built as stored data can be.

    Stored rules are not re-validated on evaluation, so the engine must answer
    a value no form or API accepts today: an empty object, or not an object.
    """
    condition = Condition.model_construct(
        attribute="now",
        operator=OperatorType.TIME_WINDOW,
        value=value,
        additional_value=None,
        attribute_type=None,
        validation_schema=None,
    )
    group = RuleGroup.model_construct(
        operator=group_operator, conditions=[condition], groups=[]
    )
    rule = TargetingRule.model_construct(
        id="constructed",
        name=None,
        description=None,
        rule=group,
        rollout_percentage=100,
    )
    return TargetingRules.model_construct(
        version="1.0", rules=[rule], default_rule=None
    )


UNREADABLE_WINDOWS = [
    ({}, "no days, start_time/end_time or start_date/end_date"),
    (
        {"start": "11:00", "end": "13:00"},
        "no days, start_time/end_time or start_date/end_date",
    ),
    (
        {"begin": "11:00", "until": "13:00"},
        "no days, start_time/end_time or start_date/end_date",
    ),
    ("11:00-13:00", "the value is not an object"),
    (["11:00", "13:00"], "the value is not an object"),
    (None, "the value is not an object"),
]


NO_KEY = "no days, start_time/end_time or start_date/end_date"
NOT_HH_MM = "start_time or end_time is not HH:MM"
#: An ISO window around MOMENT, and one in 2000.
ISO_NOW = {"start": "2026-01-05T11:00:00+00:00", "end": "2026-01-05T13:00:00+00:00"}
ISO_2000 = {"start": "2000-01-01T00:00:00", "end": "2000-01-02T00:00:00"}

#: (label, rules, reason): a window the engine cannot read, alone or inside a
#: NOT, in each shape that reaches it.
UNREADABLE_RULES = [
    *[
        (f"{wrapper.value} {value!r}", constructed(value, wrapper), reason)
        for value, reason in UNREADABLE_WINDOWS
        for wrapper in (LogicalOperator.AND, LogicalOperator.NOT)
    ],
    (
        "native NOT ISO including now",
        native("now", "time_window", ISO_NOW, group_operator="not"),
        NO_KEY,
    ),
    (
        "native NOT ISO in 2000",
        native("now", "time_window", ISO_2000, group_operator="not"),
        NO_KEY,
    ),
    (
        "native NOT HH:MM start/end",
        native(
            "now",
            "time_window",
            {"start": "11:00", "end": "13:00"},
            group_operator="not",
        ),
        NO_KEY,
    ),
    (
        "dashboard ISO including now",
        dashboard("now", "time_window", ISO_NOW),
        NOT_HH_MM,
    ),
    (
        "dashboard NOT ISO including now",
        dashboard("now", "time_window", ISO_NOW, "NOT"),
        NOT_HH_MM,
    ),
    (
        "dashboard NOT ISO in 2000",
        dashboard("now", "time_window", ISO_2000, "NOT"),
        NOT_HH_MM,
    ),
    (
        "dashboard NOT a days value that is not a list",
        dashboard(
            "now",
            "time_window",
            {"start": "11:00", "end": "13:00", "days": "monday"},
            "NOT",
        ),
        "days is not a list",
    ),
]


DISABLED_BY_ERROR = {"enabled": False, "reason": "error", "rule_id": None}


def flag_detailed(
    raw: Any, context: Dict[str, Any], rollout: int = 100, db: Any = None
) -> Dict[str, Any]:
    """The flag service's whole answer (``evaluate_flag_detailed``), as an SDK
    gets it. Metrics are not written, and no error-log row may be."""
    flag = SimpleNamespace(
        id="00000000-0000-0000-0000-000000001145",
        key="parity",
        status=FeatureFlagStatus.ACTIVE,
        rollout_percentage=rollout,
        targeting_rules=copy.deepcopy(raw),
    )
    with mock.patch.object(feature_flag_service, "MetricsService") as metrics:
        answer = FeatureFlagService(db=db or MagicMock()).evaluate_flag_detailed(
            flag, USER, context
        )
    metrics.log_error.assert_not_called()
    return answer


@pytest.mark.regression
@pytest.mark.parametrize(
    "label, rules, reason", UNREADABLE_RULES, ids=[case[0] for case in UNREADABLE_RULES]
)
def test_an_unreadable_window_matches_nobody_and_is_reported(
    monkeypatch, label, rules, reason
):
    """The whole ruleset is abandoned, reported through ``_note_rule_problem``.

    The flag answers off with reason ``error`` and the user is not eligible
    for the experiment, whatever groups enclose the condition, and neither a
    value that is not a time nor a missing attribute hides it.
    """
    notes: List[Any] = []
    monkeypatch.setattr(
        rules_engine,
        "_note_rule_problem",
        lambda operator, why: notes.append((operator, why)),
    )
    for context in ({"now": MOMENT}, {"now": "not a time"}, {}):
        notes.clear()
        assert flag_detailed(rules, context) == DISABLED_BY_ERROR, context
        assert notes == [(OperatorType.TIME_WINDOW, reason)], context
        notes.clear()
        assert experiment_answer(rules, USER, context) is None, context
        assert notes == [(OperatorType.TIME_WINDOW, reason)], context


def two_rules(window: Any, group_operator: str = "and") -> Dict[str, Any]:
    """Rule 1 at 0% leaves out the users in ``window``; rule 2 admits US users."""
    return {
        "version": "1.0",
        "rules": [
            {
                "id": "excl",
                "priority": 1,
                "rollout_percentage": 0,
                "rule": {
                    "operator": group_operator,
                    "conditions": [
                        {"attribute": "now", "operator": "time_window", "value": window}
                    ],
                },
            },
            {
                "id": "us",
                "priority": 2,
                "rollout_percentage": 100,
                "rule": {
                    "operator": "and",
                    "conditions": [
                        {"attribute": "country", "operator": "eq", "value": "US"}
                    ],
                },
            },
        ],
    }


def exclusion(window: Any) -> Dict[str, Any]:
    """Only rule 1 of :func:`two_rules`: a 0% rule, with the flag at 100%."""
    raw = two_rules(window)
    raw["rules"] = raw["rules"][:1]
    return raw


#: Rule 1's window as the engine reads it, and as it cannot.
READABLE = {
    "start": "09:00",
    "end": "17:00",
    "start_time": "09:00",
    "end_time": "17:00",
}
UNREADABLE = {"start": "09:00", "end": "17:00"}
EVENING = "2026-01-05T20:00:00+00:00"


@pytest.mark.regression
def test_an_unreadable_window_in_a_zero_percent_rule_turns_the_flag_off():
    """A rule written to leave users out does not let them through to the
    global rollout when its window cannot be read."""
    for context in ({"now": MOMENT}, {"now": EVENING}, {}):
        assert flag_detailed(exclusion(UNREADABLE), context) == DISABLED_BY_ERROR
        assert experiment_answer(exclusion(UNREADABLE), USER, context) is None


def test_a_zero_percent_rule_with_a_readable_window_still_decides():
    raw = exclusion(READABLE)
    assert flag_detailed(raw, {"now": MOMENT}) == {
        "enabled": False,
        "reason": "targeting_rule",
        "rule_id": "excl",
    }
    assert flag_detailed(raw, {"now": EVENING}) == {
        "enabled": True,
        "reason": "rollout",
        "rule_id": None,
    }


@pytest.mark.regression
@pytest.mark.parametrize("group_operator", ["and", "not"])
def test_an_unreadable_window_abandons_the_whole_ruleset(group_operator):
    """A later rule does not answer instead, on flags or experiments."""
    raw = two_rules(UNREADABLE, group_operator)
    for country in ("US", "FR"):
        context = {"now": MOMENT, "country": country}
        assert flag_detailed(raw, context) == DISABLED_BY_ERROR, country
        assert flag_answer(raw, USER, context) is None, country
        assert experiment_answer(raw, USER, context) is None, country


def test_a_readable_window_lets_the_next_rule_answer():
    raw = two_rules({**READABLE, "start_time": "14:00", "end_time": "15:00"})
    context = {"now": MOMENT, "country": "US"}
    assert flag_detailed(raw, context) == {
        "enabled": True,
        "reason": "targeting_rule",
        "rule_id": "us",
    }
    assert experiment_answer(raw, USER, context) == "us"


SEGMENT = "5e9a1c00-0000-4000-8000-000000001145"


def not_in_segment_answers(
    segment_window: Any, context: Dict[str, Any]
) -> Tuple[Dict[str, Any], bool]:
    """(the flag's answer, experiment eligibility) for ``not_in_segment`` over a
    rules segment holding ``segment_window``, resolved by the real
    ``resolve_segment_memberships`` on a stub database. The flag is at 0%."""
    row = SimpleNamespace(
        id=uuid.UUID(SEGMENT),
        kind="rules",
        status=SegmentStatus.ACTIVE,
        rules=dashboard("now", "time_window", segment_window),
    )
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = [row]
    raw = dashboard("segment", "not_in_segment", SEGMENT)
    flag = flag_detailed(raw, context, rollout=0, db=db)
    targeting = AssignmentService(db)._evaluate_experiment_targeting(
        SimpleNamespace(id="parity", targeting_rules=copy.deepcopy(raw)),
        AssignmentService._build_targeting_context(USER, context),
        user_id=USER,
    )
    return flag, targeting["eligible"]


@pytest.mark.regression
def test_not_in_segment_over_a_segment_whose_window_cannot_be_read():
    """The segment's membership is unavailable, so the flag is off with reason
    ``error`` and the user is not eligible."""
    for window in (ISO_NOW, {"start": "11:00", "end": "13:00", "days": "monday"}):
        assert not_in_segment_answers(window, {"now": MOMENT}) == (
            DISABLED_BY_ERROR,
            False,
        ), window


@pytest.mark.regression
def test_a_segment_whose_window_cannot_be_read_is_unavailable():
    """``resolve_segment_memberships`` answers it, as for a pattern that cannot
    be evaluated: unavailable, not "not a member"."""
    row = SimpleNamespace(
        id=uuid.UUID(SEGMENT),
        kind="rules",
        status=SegmentStatus.ACTIVE,
        rules=dashboard("now", "time_window", ISO_NOW),
    )
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = [row]
    memberships = segment_membership.resolve_segment_memberships(
        db, USER, {"now": MOMENT}, [SEGMENT]
    )
    assert memberships.unavailable == frozenset({SEGMENT})
    assert memberships.members == frozenset()


@pytest.mark.regression
def test_the_cached_experiment_entry_point_abandons_the_ruleset_too():
    """``RulesEvaluationService.evaluate`` answers no match with the fixed
    refusal text, and counts no evaluation error."""
    service = RulesEvaluationService()
    result = service.evaluate(
        constructed({}, LogicalOperator.NOT), {"now": MOMENT}, skip_cache=True
    )
    assert result.matched is False
    assert result.error == str(rules_engine.TimeWindowUnreadable())
    assert service.metrics.total_errors == 0


def test_not_in_segment_over_a_segment_whose_window_can_be_read():
    window = {"start": "11:00", "end": "13:00"}
    # A member: not_in_segment does not hold; the flag is at 0%.
    assert not_in_segment_answers(window, {"now": MOMENT}) == (
        {"enabled": False, "reason": "rollout", "rule_id": None},
        False,
    )
    # Not a member: not_in_segment holds.
    assert not_in_segment_answers(window, {"now": EVENING}) == (
        {"enabled": True, "reason": "targeting_rule", "rule_id": "dashboard"},
        True,
    )


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
