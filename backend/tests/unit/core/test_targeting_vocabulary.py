"""Targeting rules refuse, at save, a word their evaluator never matches (#1204).

A condition is refused on create and update when the evaluator that will read
it never matches it:

* on a flag, the operators ``json_path`` and ``percentage_bucket``, which flag
  evaluation does not implement (experiments implement both, and accept them);
* a ``semantic_version`` or ``array_length`` comparison, or a ``geo_distance``
  ``comparison`` or ``unit``, outside the words ``apply_operator`` reads, on
  flags, experiments and segments;
* in the native shape, a ``time_window`` that ``rules_engine.check_time_window``
  cannot read, such as one given only as ``start`` and ``end`` (plan D211,
  ruling 1). The dashboard shape is not checked.

The words the engine reads are taken from its source here (``ENGINE_WORDS``),
not from the constants the write path uses, so the tests below hold the
constants to the engine rather than to themselves. Pinned, without a
database:

* (1a) every ``OperatorType``, with every word the engine reads and unknown
  sentinels, is refused on save or has a context that matches it, on flags
  and on experiments, with no exemption;
* (1b) ``rules_engine``'s constants are exactly those words; each matches some
  input, and no sentinel ever does;
* the exact message for each refusal on each kind it applies to (the reasons
  are plan D211's text, verbatim), and the values the contract accepts;
* (1e) the walker is reached from write paths only, and a stored segment with
  an unknown unit stays available, so a flag naming it still evaluates;
* (1h) a stored rule that never matches keeps answering as it did;
* (1j, 1n) the dashboard ``{"start", "end"}`` window and the reference page's
  example window are accepted;
* (1o) ``TIME_WINDOW_KEYS`` has one definition;
* (1m) each reason is one line of at most 200 characters, and the dashboard
  line the frontend tests render is the API's.
"""

from __future__ import annotations

import ast
import copy
import json
import re
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple
from unittest.mock import MagicMock

import pytest

from backend.app.core import rules_engine, targeting_adapter
from backend.app.core.log_once import EVALUATION_NOTES
from backend.app.core.pattern_match import PatternUnevaluable
from backend.app.core.rules_engine import (
    SEGMENT_MEMBERSHIP_KEY,
    SegmentMembershipUnavailable,
    TimeWindowUnreadable,
    apply_operator,
)
from backend.app.core.targeting_adapter import (
    TargetingRulesError,
    match_targeting_rule,
    normalise_targeting_rules,
    validate_experiment_targeting,
    validate_flag_targeting,
    validate_segment_rules,
)
from backend.app.models.segment import SegmentStatus
from backend.app.schemas import segment as segment_schema
from backend.app.schemas.targeting_rule import OperatorType, TargetingRules
from backend.app.services import segment_membership
from backend.app.services.rules_evaluation_service import RulesEvaluationService
from backend.tests.unit.docs.test_time_window_doc import _example as reference_example
from backend.tests.unit.services.test_experiment_flag_targeting_parity import (
    USER,
    flag_detailed,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
BACKEND = REPO_ROOT / "backend"
FRONTEND_FIXTURE = json.loads(
    (
        REPO_ROOT
        / "frontend"
        / "src"
        / "tests"
        / "fixtures"
        / "targeting-vocabulary-refusal.json"
    ).read_text("utf-8")
)

# The reasons, verbatim from plan D211 S1 (EMD-1).
NOT_ON_FLAGS = (
    "not supported for feature flags; json_path and percentage_bucket are for "
    "experiments only"
)
VERSION = (
    "unknown version comparison; use eq, gt, gte, lt or lte, or leave it out for eq"
)
LENGTH = (
    "unknown length comparison; use eq, gt, gte, lt, lte or between, or leave it "
    "out for eq"
)
DISTANCE = (
    "unknown distance comparison; use lte, lt, gte, gt or eq, or leave it out for lte"
)
UNIT = "unknown distance unit; use km, mi or m, or leave it out for km"
WINDOW = (
    "unreadable time window; see the time_window example on the Targeting Rules: "
    "Operator Reference page"
)

FLAG, EXPERIMENT, SEGMENT = "flag", "experiment", "segment"
#: The kinds whose rules may be native (segments take the dashboard shape only).
NATIVE_KINDS = (FLAG, EXPERIMENT)


class _Absent:
    """``additional_value`` left out of the condition."""

    def __repr__(self) -> str:
        return "absent"


_ABSENT = _Absent()

#: Words the engine does not read, tried wherever a word is (plan D211, 1a).
SENTINELS: Tuple[Any, ...] = ("ne", "neq", "within", "GTE", 1, True)

POINT = [37.7749, -122.4194]
#: San Francisco, and Oakland 13.4 km from it.
CENTRE = {"a": POINT}
OAKLAND = {"a": [37.8044, -122.2712]}
MOMENT = "2024-01-10T12:00:00"  # a Wednesday (day 2)
SEGMENT_ID = "5e9a1c00-0000-4000-8000-000000001204"


# ---------------------------------------------------------------------------
# The words apply_operator reads, from its source
# ---------------------------------------------------------------------------

ENGINE_TREE = ast.parse(Path(rules_engine.__file__).read_text("utf-8"))


def _function(name: str) -> ast.FunctionDef:
    for node in ENGINE_TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in rules_engine.py")


def _branch(operator: str) -> List[ast.stmt]:
    """The body of ``apply_operator``'s ``operator == OperatorType.<operator>``."""
    for node in ast.walk(_function("apply_operator")):
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name)
            and node.test.left.id == "operator"
            and isinstance(node.test.ops[0], ast.Eq)
            and isinstance(node.test.comparators[0], ast.Attribute)
            and node.test.comparators[0].attr == operator
        ):
            return node.body
    raise AssertionError(f"apply_operator has no {operator} branch")


def _compared_words(body: List[ast.stmt], name: str) -> FrozenSet[str]:
    """Every text ``name`` is compared with (``==`` or ``in``) in ``body``."""
    words: Set[str] = set()
    for statement in body:
        for node in ast.walk(statement):
            if not (isinstance(node, ast.Compare) and isinstance(node.left, ast.Name)):
                continue
            if node.left.id != name:
                continue
            for comparator in node.comparators:
                items = (
                    comparator.elts
                    if isinstance(comparator, (ast.Tuple, ast.List, ast.Set))
                    else [comparator]
                )
                words |= {
                    i.value
                    for i in items
                    if isinstance(i, ast.Constant) and isinstance(i.value, str)
                }
    return frozenset(words)


def _operators_with_a_branch() -> Set[str]:
    """The ``OperatorType`` names ``apply_operator`` tests ``operator`` against."""
    names: Set[str] = set()
    for node in ast.walk(_function("apply_operator")):
        if (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Name)
            and node.left.id == "operator"
        ):
            for comparator in node.comparators:
                items = (
                    comparator.elts
                    if isinstance(comparator, (ast.Tuple, ast.List))
                    else [comparator]
                )
                names |= {i.attr for i in items if isinstance(i, ast.Attribute)}
    return names


ENGINE_WORDS: Dict[str, FrozenSet[str]] = {
    "semantic_version": _compared_words(_branch("SEMANTIC_VERSION"), "comparison_type"),
    "array_length": _compared_words(_branch("ARRAY_LENGTH"), "comparison_type"),
    "geo comparison": _compared_words(_branch("GEO_DISTANCE"), "comparison"),
    "geo unit": _compared_words(_function("_haversine_distance").body, "unit"),
}


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


def cond(operator: str, value: Any, additional: Any = _ABSENT, attribute: str = "a"):
    """A native condition."""
    condition = {"attribute": attribute, "operator": operator, "value": value}
    if additional is not _ABSENT:
        condition["additional_value"] = additional
    return condition


def native(*conditions: Dict[str, Any], groups=None, operator: str = "and", **top):
    """Native rules: one rule ``r`` holding ``conditions`` (and ``groups``)."""
    group: Dict[str, Any] = {"operator": operator, "conditions": list(conditions)}
    if groups is not None:
        group["groups"] = groups
    return {
        "rules": [{"id": "r", "rule": group, "rollout_percentage": 100, "priority": 0}],
        **top,
    }


def dashboard(*conditions: Dict[str, Any]) -> Dict[str, Any]:
    """Dashboard rules: one group holding ``conditions``."""
    return {
        "logical_operator": "AND",
        "groups": [{"logical_operator": "AND", "conditions": list(conditions)}],
    }


US = cond("eq", "US", attribute="country")
DASH_US = {"attribute": "country", "operator": "equals", "value": "US"}


def geo(**extra: Any) -> Dict[str, Any]:
    """A native ``geo_distance`` condition around ``POINT``."""
    return cond("geo_distance", POINT, {"radius": 10, **extra})


def dash_geo(value: Any) -> Dict[str, Any]:
    return {"attribute": "location", "operator": "geo_within_radius", "value": value}


def refusal(kind: str, rules: Any) -> Optional[str]:
    """The message ``kind``'s save refuses ``rules`` with, or ``None``.

    Each is what the request model's validator runs: ``validate_flag_targeting``
    (flags), ``validate_experiment_targeting`` (experiments, the wizard launch
    included) and the segment schema's ``_checked_segment_rules``."""
    value = copy.deepcopy(rules)
    try:
        if kind == FLAG:
            validate_flag_targeting(value)
        elif kind == EXPERIMENT:
            validate_experiment_targeting(value)
        else:
            segment_schema._checked_segment_rules(value)
    except ValueError as err:
        return str(err)
    return None


# ---------------------------------------------------------------------------
# The refusals, with their exact messages
# ---------------------------------------------------------------------------

_RULE = "rules[0].rule"
_FIRST = f"{_RULE}.conditions[0]"

#: name -> (kinds, rules, message). The message is the API's ``msg`` after
#: ``Value error, ``.
REFUSED: Dict[str, Tuple[FrozenSet[str], Any, str]] = {
    "json_path on a flag": (
        frozenset({FLAG}),
        native(cond("json_path", "$.b", 1)),
        f"{_FIRST}.operator: {NOT_ON_FLAGS}",
    ),
    "percentage_bucket on a flag, inside NOT": (
        frozenset({FLAG}),
        native(
            US,
            groups=[{"operator": "not", "conditions": [cond("percentage_bucket", 50)]}],
        ),
        f"{_RULE}.groups[0].conditions[0].operator: {NOT_ON_FLAGS}",
    ),
    "json_path in a flag's default_rule": (
        frozenset({FLAG}),
        native(
            US,
            default_rule={
                "id": "d",
                "rule": {
                    "operator": "and",
                    "conditions": [cond("json_path", "$.b", 1)],
                },
            },
        ),
        f"default_rule.rule.conditions[0].operator: {NOT_ON_FLAGS}",
    ),
    **{
        f"semantic_version {word!r}": (
            frozenset(NATIVE_KINDS),
            native(US, cond("semantic_version", "1.2.0", word)),
            f"{_RULE}.conditions[1].additional_value: {VERSION}",
        )
        for word in (*SENTINELS, " gte", "eq ", 5, ["gt"], {"op": "gt"}, "between")
    },
    **{
        f"array_length {word!r}": (
            frozenset(NATIVE_KINDS),
            native(cond("array_length", 2, word)),
            f"{_FIRST}.additional_value: {LENGTH}",
        )
        for word in (*SENTINELS, "BETWEEN", ["gt"])
    },
    **{
        f"geo_distance comparison {word!r}": (
            frozenset(NATIVE_KINDS),
            native(geo(comparison=word)),
            f"{_FIRST}.additional_value: {DISTANCE}",
        )
        for word in (*SENTINELS, "LTE", "lte ", ["lt"])
    },
    **{
        f"geo_distance unit {word!r}": (
            frozenset(NATIVE_KINDS),
            native(geo(unit=word)),
            f"{_FIRST}.additional_value: {UNIT}",
        )
        for word in ("furlong", "yd", " km", "", None, 5, True, ["km"])
    },
    "geo_distance comparison inside NOT, in a nested group": (
        frozenset(NATIVE_KINDS),
        native(
            US,
            groups=[
                {"operator": "and", "conditions": [US]},
                {"operator": "not", "conditions": [US, geo(comparison="outside")]},
            ],
        ),
        f"{_RULE}.groups[1].conditions[1].additional_value: {DISTANCE}",
    ),
    "dashboard unit furlong (the frontend fixture)": (
        frozenset({FLAG, EXPERIMENT, SEGMENT}),
        FRONTEND_FIXTURE["rules"],
        f"groups[0].conditions[1].value: {UNIT}",
    ),
    "dashboard unit in an object": (
        frozenset({FLAG, EXPERIMENT, SEGMENT}),
        dashboard(dash_geo({"lat": 37.7, "lon": -122.4, "radius": 5, "unit": "yd"})),
        f"groups[0].conditions[0].value: {UNIT}",
    ),
    "dashboard unit in a list": (
        frozenset({FLAG, EXPERIMENT, SEGMENT}),
        dashboard(dash_geo([37.7, -122.4, 5, "leagues"])),
        f"groups[0].conditions[0].value: {UNIT}",
    ),
    **{
        f"native time_window {name}": (
            frozenset(NATIVE_KINDS),
            native(US, cond("time_window", window)),
            f"{_RULE}.conditions[1].value: {WINDOW}",
        )
        for name, window in (
            ("start and end only", {"start": "09:00", "end": "17:00"}),
            (
                "ISO start and end",
                {"start": "2024-01-01T09:00:00", "end": "2024-01-31T17:00:00"},
            ),
            ("days outside 0-6", {"start": "x", "end": "y", "days": [7]}),
            ("start_time alone", {"start": "x", "end": "y", "start_time": "09:00"}),
            (
                "an unknown timezone",
                {"start": "x", "end": "y", "days": [1], "timezone": "Mars/Olympus"},
            ),
            (
                "start_date after end_date",
                {
                    "start": "x",
                    "end": "y",
                    "start_date": "2024-02-01",
                    "end_date": "2024-01-01",
                },
            ),
        )
    },
    "native time_window in default_rule": (
        frozenset(NATIVE_KINDS),
        native(
            US,
            default_rule={
                "id": "d",
                "rule": {
                    "operator": "and",
                    "conditions": [
                        cond("time_window", {"start": "09:00", "end": "17:00"})
                    ],
                },
            },
        ),
        f"default_rule.rule.conditions[0].value: {WINDOW}",
    ),
}


def _refused_rows() -> List[Tuple[str, str]]:
    return [
        (name, kind)
        for name, (kinds, _, _) in REFUSED.items()
        for kind in sorted(kinds)
    ]


@pytest.mark.regression
@pytest.mark.parametrize("name, kind", _refused_rows(), ids=lambda v: str(v))
def test_refused_with_fixed_text(name, kind):
    _, rules, message = REFUSED[name]
    assert refusal(kind, rules) == message


def test_experiments_accept_what_only_flags_refuse():
    """``json_path`` and ``percentage_bucket`` are refused on flags only."""
    for name, (kinds, rules, _) in REFUSED.items():
        if kinds == {FLAG}:
            assert refusal(EXPERIMENT, rules) is None, name


#: The false-y values the engine reads as ``eq``.
FALSEY = (_ABSENT, None, "", 0, False, [], {})

#: name -> (kinds, rules): the values the contract accepts.
ACCEPTED: Dict[str, Tuple[FrozenSet[str], Any]] = {
    **{
        f"semantic_version {word!r}": (
            frozenset(NATIVE_KINDS),
            native(cond("semantic_version", "1.2.0", word)),
        )
        for word in (*FALSEY, *sorted(ENGINE_WORDS["semantic_version"]))
    },
    **{
        f"array_length {word!r}": (
            frozenset(NATIVE_KINDS),
            native(
                cond(
                    "array_length",
                    {"min": 1, "max": 2} if word == "between" else 2,
                    word,
                )
            ),
        )
        for word in (*FALSEY, *sorted(ENGINE_WORDS["array_length"]))
    },
    "geo_distance with a radius alone": (
        frozenset(NATIVE_KINDS),
        native(cond("geo_distance", POINT, 10)),
    ),
    **{
        f"geo_distance comparison {word!r}": (
            frozenset(NATIVE_KINDS),
            native(geo(comparison=word)),
        )
        for word in (None, *sorted(ENGINE_WORDS["geo comparison"]))
    },
    **{
        f"geo_distance unit {word!r}": (frozenset(NATIVE_KINDS), native(geo(unit=word)))
        for word in (*sorted(ENGINE_WORDS["geo unit"]), "KM", "Miles", "METRES")
    },
    "json_path on an experiment": (
        frozenset({EXPERIMENT}),
        native(cond("json_path", "$.b", 1)),
    ),
    "percentage_bucket on an experiment": (
        frozenset({EXPERIMENT}),
        native(cond("percentage_bucket", 50)),
    ),
    **{
        f"native time_window {name}": (
            frozenset(NATIVE_KINDS),
            native(cond("time_window", window)),
        )
        for name, window in (
            (
                "with times",
                {
                    "start": "09:00",
                    "end": "17:00",
                    "start_time": "09:00",
                    "end_time": "17:00",
                },
            ),
            ("with days", {"start": "x", "end": "y", "days": [0, 1, 2, 3, 4]}),
            (
                "with dates",
                {
                    "start": "x",
                    "end": "y",
                    "start_date": "2024-01-01",
                    "end_date": "2024-01-31",
                },
            ),
            (
                "with a timezone",
                {"start": "x", "end": "y", "days": [5, 6], "timezone": "Europe/Paris"},
            ),
        )
    },
    # (1j) The dashboard shape is not checked: its {"start", "end"} window is
    # what the builder saves and what the vectors' op-time-window stores.
    "dashboard time_window HH:MM": (
        frozenset({FLAG, EXPERIMENT, SEGMENT}),
        dashboard(
            {
                "attribute": "t",
                "operator": "time_window",
                "value": {"start": "09:00", "end": "17:00"},
            }
        ),
    ),
    # Not checked either (ruling 1: the dashboard shape is not touched); it is
    # still abandoned when it is evaluated, as the reference page says.
    "dashboard time_window ISO": (
        frozenset({FLAG, EXPERIMENT, SEGMENT}),
        dashboard(
            {
                "attribute": "t",
                "operator": "time_window",
                "value": {"start": "2024-01-01T09:00:00", "end": "2024-01-31T17:00:00"},
            }
        ),
    ),
    **{
        f"dashboard unit {name}": (
            frozenset({FLAG, EXPERIMENT, SEGMENT}),
            dashboard(DASH_US, dash_geo(value)),
        )
        for name, value in (
            ("absent", "37.7749,-122.4194,15"),
            ("km", "37.7749,-122.4194,15,km"),
            ("MILES", "37.7749,-122.4194,15,MILES"),
            (
                "metres in an object",
                {"lat": 37.7, "lon": -122.4, "radius": 5, "unit": "metres"},
            ),
            ("mi in a list", [37.7, -122.4, 5, "mi"]),
        )
    },
}


def _accepted_rows() -> List[Tuple[str, str]]:
    return [
        (name, kind) for name, (kinds, _) in ACCEPTED.items() for kind in sorted(kinds)
    ]


@pytest.mark.parametrize("name, kind", _accepted_rows(), ids=lambda v: str(v))
def test_accepted(name, kind):
    _, rules = ACCEPTED[name]
    assert refusal(kind, rules) is None


@pytest.mark.regression
@pytest.mark.parametrize("kind", [FLAG, EXPERIMENT, SEGMENT])
def test_the_reference_pages_example_window_is_accepted(kind):
    """(1n) The window the time_window reason sends a writer to is accepted on
    flags and experiments (both shapes) and on segments (the dashboard shape)."""
    example = reference_example()
    condition = {**example, "operator": example["operator"].value}
    assert refusal(kind, dashboard(condition)) is None
    if kind != SEGMENT:
        assert refusal(kind, native(condition)) is None


def test_the_reasons_are_the_plans():
    """The write path's constants are plan D211's text, verbatim (EMD-1)."""
    assert targeting_adapter.VOCABULARY_REASONS == (
        NOT_ON_FLAGS,
        VERSION,
        LENGTH,
        DISTANCE,
        UNIT,
        WINDOW,
    )


def test_each_reason_is_one_line_of_at_most_200_characters():
    """(1m) The dashboard shows a condition's problem only when it is one line
    of at most 200 characters (``describeTargetingIssue``)."""
    for reason in targeting_adapter.VOCABULARY_REASONS:
        assert "\n" not in reason and len(reason) <= 200, reason


def test_the_dashboard_line_is_the_apis():
    """(1m) The frontend tests render ``shown`` for ``msg``; ``msg`` is what
    every kind answers for the fixture's rules."""
    text = FRONTEND_FIXTURE["msg"].removeprefix("Value error, ")
    assert text == f"groups[0].conditions[1].value: {UNIT}"
    assert FRONTEND_FIXTURE["shown"] == f"Group 1, Condition 2: {UNIT}"
    for kind in (FLAG, EXPERIMENT, SEGMENT):
        assert refusal(kind, FRONTEND_FIXTURE["rules"]) == text


def test_a_message_never_repeats_the_submitted_value():
    marker = "zq1204marker"
    for rules in (
        native(cond("semantic_version", "1.2.0", marker)),
        native(cond("array_length", 2, marker)),
        native(geo(comparison=marker)),
        native(geo(unit=marker)),
        native(cond("time_window", {"start": marker, "end": marker})),
        dashboard(dash_geo(f"37.7,-122.4,5,{marker}")),
    ):
        message = refusal(FLAG, rules)
        assert message is not None and marker not in message


def test_a_refused_window_is_not_reported_as_an_evaluation(caplog):
    """The save calls the engine's own check without its once-only warning, so
    a stored window that cannot be read still gets that warning."""
    EVALUATION_NOTES.clear()
    with caplog.at_level("WARNING"):
        assert refusal(FLAG, native(cond("time_window", {"start": "x", "end": "y"})))
    assert not [r for r in caplog.records if "time_window" in r.getMessage()]
    assert EVALUATION_NOTES.seen == set()


# ---------------------------------------------------------------------------
# (1a) Every operator is refused on save or can match, on both kinds
# ---------------------------------------------------------------------------

#: For each operator with no word of its own: its value, ``additional_value``
#: and contexts, one of which it matches.
SIMPLE: Dict[OperatorType, Tuple[Any, Any, List[Dict[str, Any]]]] = {
    OperatorType.EQUALS: ("x", _ABSENT, [{"a": "x"}]),
    OperatorType.NOT_EQUALS: ("x", _ABSENT, [{"a": "y"}]),
    OperatorType.CONTAINS: ("x", _ABSENT, [{"a": "wxy"}]),
    OperatorType.NOT_CONTAINS: ("x", _ABSENT, [{"a": "y"}]),
    OperatorType.STARTS_WITH: ("x", _ABSENT, [{"a": "xy"}]),
    OperatorType.ENDS_WITH: ("x", _ABSENT, [{"a": "yx"}]),
    OperatorType.GREATER_THAN: (1, _ABSENT, [{"a": 2}]),
    OperatorType.GREATER_THAN_OR_EQUAL: (1, _ABSENT, [{"a": 1}]),
    OperatorType.LESS_THAN: (1, _ABSENT, [{"a": 0}]),
    OperatorType.LESS_THAN_OR_EQUAL: (1, _ABSENT, [{"a": 1}]),
    OperatorType.IN: (["x"], _ABSENT, [{"a": "x"}]),
    OperatorType.NOT_IN: (["x"], _ABSENT, [{"a": "y"}]),
    OperatorType.BEFORE: (
        "2024-01-01T00:00:00",
        _ABSENT,
        [{"a": "2023-06-01T00:00:00"}],
    ),
    OperatorType.AFTER: (
        "2024-01-01T00:00:00",
        _ABSENT,
        [{"a": "2024-06-01T00:00:00"}],
    ),
    OperatorType.BETWEEN: (1, 3, [{"a": 2}]),
    OperatorType.CONTAINS_ALL: (["x"], _ABSENT, [{"a": ["x", "y"]}]),
    OperatorType.CONTAINS_ANY: (["x", "z"], _ABSENT, [{"a": ["x", "y"]}]),
    OperatorType.MATCH_REGEX: ("^x", _ABSENT, [{"a": "xy"}]),
    OperatorType.IS_NULL: (None, _ABSENT, [{"a": None}, {}]),
    OperatorType.IS_NOT_NULL: (None, _ABSENT, [{"a": "x"}]),
    OperatorType.PERCENTAGE_BUCKET: (100, _ABSENT, [{"a": "user-1"}]),
    OperatorType.JSON_PATH: ("$.b", 1, [{"a": {"b": 1}}]),
}

SEMVER_CONTEXTS = [{"a": v} for v in ("0.5.0", "1.2.0", "2.0.0")]
LENGTH_CONTEXTS = [{"a": v} for v in ([1], [1, 2], [1, 2, 3])]
GEO_CONTEXTS = [CENTRE, OAKLAND]
WINDOW_CONTEXTS = [{"a": MOMENT}]

#: (label, window): the first three match MOMENT; the engine cannot read the
#: others.
WINDOWS = [
    (
        "times",
        {"start": "00:00", "end": "23:59", "start_time": "00:00", "end_time": "23:59"},
    ),
    ("days", {"start": "x", "end": "y", "days": [2]}),
    (
        "dates",
        {
            "start": "x",
            "end": "y",
            "start_date": "2024-01-01",
            "end_date": "2024-01-31",
        },
    ),
    ("start and end only", {"start": "09:00", "end": "17:00"}),
    (
        "ISO start and end",
        {"start": "2024-01-01T09:00:00", "end": "2024-01-31T17:00:00"},
    ),
    ("days 7", {"start": "x", "end": "y", "days": [7]}),
]


def _words(
    operator: OperatorType,
) -> List[Tuple[str, List[Dict[str, Any]], List[Dict[str, Any]]]]:
    """``(word, candidate conditions, contexts)`` for ``operator``: every word
    the engine reads for it, and the sentinels.

    A word is refused when every candidate is refused; otherwise some accepted
    candidate must match some context. Candidates differ only in values the
    word does not decide (a radius, a length's bounds). A new
    ``OperatorType`` raises ``KeyError`` until it is classified."""
    if operator == OperatorType.SEMANTIC_VERSION:
        return [
            (repr(w), [cond("semantic_version", "1.2.0", w)], SEMVER_CONTEXTS)
            for w in (_ABSENT, *sorted(ENGINE_WORDS["semantic_version"]), *SENTINELS)
        ]
    if operator == OperatorType.ARRAY_LENGTH:
        return [
            (
                repr(w),
                [
                    cond("array_length", 2, w),
                    cond("array_length", {"min": 1, "max": 2}, w),
                ],
                LENGTH_CONTEXTS,
            )
            for w in (_ABSENT, *sorted(ENGINE_WORDS["array_length"]), *SENTINELS)
        ]
    if operator == OperatorType.GEO_DISTANCE:
        rows = []
        for w in (None, *sorted(ENGINE_WORDS["geo comparison"]), *SENTINELS):
            candidates = [
                cond("geo_distance", POINT, {"radius": r, "comparison": w})
                for r in (0, 10)
            ]
            rows.append((f"comparison {w!r}", candidates, GEO_CONTEXTS))
        for w in (*sorted(ENGINE_WORDS["geo unit"]), "KM", *SENTINELS, None):
            candidates = [
                cond("geo_distance", POINT, {"radius": r, "unit": w}) for r in (0, 10)
            ]
            rows.append((f"unit {w!r}", candidates, GEO_CONTEXTS))
        return rows
    if operator == OperatorType.TIME_WINDOW:
        return [
            (label, [cond("time_window", w)], WINDOW_CONTEXTS) for label, w in WINDOWS
        ]
    if operator in (OperatorType.IN_SEGMENT, OperatorType.NOT_IN_SEGMENT):
        members = (
            frozenset({SEGMENT_ID})
            if operator == OperatorType.IN_SEGMENT
            else frozenset()
        )
        return [
            (
                operator.value,
                [cond(operator.value, SEGMENT_ID, attribute="segment")],
                [{SEGMENT_MEMBERSHIP_KEY: members}],
            )
        ]
    value, additional, contexts = SIMPLE[operator]
    return [(operator.value, [cond(operator.value, value, additional)], contexts)]


def _matches(kind: str, rules: TargetingRules, context: Dict[str, Any]) -> bool:
    """Whether ``kind``'s evaluator matches the rules' one rule for ``context``.
    A ruleset it abandons does not match."""
    try:
        if kind == FLAG:
            return match_targeting_rule(rules, dict(context)) is not None
        matched, _ = RulesEvaluationService().evaluate_rules_with_validation(
            rules, dict(context)
        )
        return matched is not None
    except (PatternUnevaluable, TimeWindowUnreadable, SegmentMembershipUnavailable):
        return False


def _validated(kind: str, raw: Any) -> Optional[TargetingRules]:
    try:
        if kind == FLAG:
            return validate_flag_targeting(copy.deepcopy(raw))
        return validate_experiment_targeting(copy.deepcopy(raw))
    except TargetingRulesError:
        return None


@pytest.mark.regression
@pytest.mark.parametrize("operator", list(OperatorType), ids=lambda o: o.value)
def test_every_native_operator_is_refused_or_can_match(operator):
    """(1a) No exemption: a word is refused on save, or matches some context,
    on flags and on experiments."""
    problems = []
    for word, candidates, contexts in _words(operator):
        for kind in NATIVE_KINDS:
            accepted = [
                r
                for r in (_validated(kind, native(c)) for c in candidates)
                if r is not None
            ]
            if not accepted:
                continue
            if not any(
                _matches(kind, rules, ctx) for rules in accepted for ctx in contexts
            ):
                problems.append(
                    f"{operator.value} {word}: accepted on a {kind} and no context "
                    "matches"
                )
    assert problems == []


def test_the_refusals_are_exercised():
    """Guards 1a: it must meet refusals, or it could pass by accepting nothing."""
    refused = {
        (operator.value, word, kind)
        for operator in OperatorType
        for word, candidates, _ in _words(operator)
        for kind in NATIVE_KINDS
        if all(_validated(kind, native(c)) is None for c in candidates)
    }
    assert ("json_path", "json_path", FLAG) in refused
    assert ("percentage_bucket", "percentage_bucket", FLAG) in refused
    assert ("json_path", "json_path", EXPERIMENT) not in refused
    assert ("semantic_version", "'ne'", EXPERIMENT) in refused
    assert ("array_length", "'within'", FLAG) in refused
    assert ("geo_distance", "comparison 'within'", FLAG) in refused
    assert ("geo_distance", "unit None", EXPERIMENT) in refused
    assert ("time_window", "start and end only", FLAG) in refused
    assert ("time_window", "start and end only", EXPERIMENT) in refused
    assert ("time_window", "times", FLAG) not in refused


# ---------------------------------------------------------------------------
# (1b) The constants are the words the engine reads
# ---------------------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize(
    "words, constant",
    [
        ("semantic_version", "SEMVER_COMPARISONS"),
        ("array_length", "ARRAY_LENGTH_COMPARISONS"),
        ("geo comparison", "GEO_COMPARISONS"),
        ("geo unit", "GEO_UNITS"),
    ],
)
def test_each_constant_is_the_words_the_engine_compares(words, constant):
    """(1b) A word added to an ``if`` chain without its constant would be
    refused on save, though the engine matches it; this names it."""
    read = ENGINE_WORDS[words]
    named = getattr(rules_engine, constant)
    assert read - named == set(), (
        f"{words}: {sorted(read - named)} compared in rules_engine but not in "
        f"{constant}, so a save refuses a word that matches"
    )
    assert named - read == set(), f"{constant}: {sorted(named - read)} never compared"


def test_the_engine_reads_the_words_the_contract_names():
    """The words themselves, so the source reading above cannot come back
    empty and pass."""
    assert ENGINE_WORDS == {
        "semantic_version": {"eq", "gt", "gte", "lt", "lte"},
        "array_length": {"eq", "gt", "gte", "lt", "lte", "between"},
        "geo comparison": {"lte", "lt", "gt", "gte", "eq"},
        "geo unit": {
            "km",
            "kilometers",
            "kilometre",
            "kilometres",
            "miles",
            "mi",
            "meters",
            "metres",
            "m",
        },
    }


@pytest.mark.regression
def test_no_operator_refused_on_flags_has_a_branch_of_its_own():
    """Were ``json_path`` implemented for flags, refusing it would be an
    over-refusal."""
    refused = rules_engine.OPERATORS_NOT_ON_FLAGS
    assert refused == {OperatorType.JSON_PATH, OperatorType.PERCENTAGE_BUCKET}
    assert {o.name for o in refused} & _operators_with_a_branch() == set()


@pytest.mark.parametrize("word", sorted(ENGINE_WORDS["semantic_version"]))
def test_each_version_comparison_matches_some_version(word):
    assert any(
        apply_operator(OperatorType.SEMANTIC_VERSION, v, "1.2.0", word)
        for v in ("0.5.0", "1.2.0", "2.0.0")
    )


@pytest.mark.parametrize("word", sorted(ENGINE_WORDS["array_length"]))
def test_each_length_comparison_matches_some_list(word):
    expected = {"min": 1, "max": 2} if word == "between" else 2
    assert any(
        apply_operator(OperatorType.ARRAY_LENGTH, v, expected, word)
        for v in ([1], [1, 2], [1, 2, 3])
    )


@pytest.mark.parametrize("word", sorted(ENGINE_WORDS["geo comparison"]))
def test_each_distance_comparison_matches_some_point(word):
    assert any(
        apply_operator(
            OperatorType.GEO_DISTANCE,
            point,
            POINT,
            {"radius": radius, "comparison": word},
        )
        for point in (CENTRE["a"], OAKLAND["a"])
        for radius in (0, 10)
    )


@pytest.mark.parametrize("word", sorted(ENGINE_WORDS["geo unit"]))
def test_each_unit_matches_at_the_centre_in_any_case(word):
    assert apply_operator(
        OperatorType.GEO_DISTANCE, POINT, POINT, {"radius": 0, "unit": word.upper()}
    )


@pytest.mark.parametrize("word", SENTINELS)
def test_no_sentinel_ever_matches(word):
    for version in ("0.5.0", "1.2.0", "2.0.0"):
        assert not apply_operator(OperatorType.SEMANTIC_VERSION, version, "1.2.0", word)
    for value in ([1], [1, 2], [1, 2, 3]):
        assert not apply_operator(OperatorType.ARRAY_LENGTH, value, 2, word)
    for point in (CENTRE["a"], OAKLAND["a"]):
        for radius in (0, 10):
            for key in ("comparison", "unit"):
                assert not apply_operator(
                    OperatorType.GEO_DISTANCE,
                    point,
                    POINT,
                    {"radius": radius, key: word},
                )


# ---------------------------------------------------------------------------
# (1e) Write paths only; (1h) stored rules keep their answers
# ---------------------------------------------------------------------------


def _callers(function: str) -> Set[Tuple[str, str]]:
    """``(file, enclosing function)`` of every call of ``function`` under
    ``backend/app``, ``backend/scripts`` and, in a full checkout,
    ``modules/backend/app`` (plain file reads; no git)."""
    found: Set[Tuple[str, str]] = set()
    roots = (
        BACKEND / "app",
        BACKEND / "scripts",
        REPO_ROOT / "modules" / "backend" / "app",
    )
    for root in (r for r in roots if r.is_dir()):
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text("utf-8"))
            for outer in ast.walk(tree):
                if not isinstance(outer, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for node in ast.walk(outer):
                    if isinstance(node, ast.Call):
                        callee = node.func
                        name = (
                            callee.id
                            if isinstance(callee, ast.Name)
                            else getattr(callee, "attr", None)
                        )
                        if name == function:
                            found.add(
                                (path.relative_to(REPO_ROOT).as_posix(), outer.name)
                            )
    return found


@pytest.mark.regression
def test_the_walker_is_called_from_write_paths_only():
    """(1e) Not from ``validate_segment_rules`` or ``_check_rule_validator``:
    both also run when stored rules are read."""
    adapter = "backend/app/core/targeting_adapter.py"
    assert _callers("_check_supported_vocabulary") == {
        (adapter, "_validate_targeting"),
        (adapter, "check_segment_vocabulary"),
    }
    assert _callers("check_segment_vocabulary") == {
        ("backend/app/schemas/segment.py", "_checked_segment_rules"),
        ("backend/scripts/check_targeting_rules.py", "segment_finding_for"),
    }
    # _validate_targeting runs only for the request schemas (the wizard
    # launch builds an ExperimentCreate) and the check script.
    assert _callers("_validate_targeting") == {
        (adapter, "validate_experiment_targeting"),
        (adapter, "validate_flag_targeting"),
    }
    callers = _callers("validate_flag_targeting") | _callers(
        "validate_experiment_targeting"
    )
    assert {file for file, _ in callers} == {
        "backend/app/schemas/feature_flag.py",
        "backend/app/schemas/experiment.py",
        "backend/scripts/check_targeting_rules.py",
    }


FURLONG_SEGMENT = dashboard(dash_geo("37.7749,-122.4194,15,furlong"))


@pytest.mark.regression
def test_a_stored_segment_with_an_unknown_unit_stays_available():
    """(1e) Its condition never holds, as before; it is not "unavailable", so
    a flag naming it is not turned off with reason ``error``."""
    row = SimpleNamespace(
        id=uuid.UUID(SEGMENT_ID),
        kind="rules",
        status=SegmentStatus.ACTIVE,
        rules=copy.deepcopy(FURLONG_SEGMENT),
    )
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = [row]
    flag_rules = dashboard(
        {"attribute": "segment", "operator": "not_in_segment", "value": SEGMENT_ID}
    )
    assert flag_detailed(flag_rules, {"location": POINT}, rollout=0, db=db) == {
        "enabled": True,
        "reason": "targeting_rule",
        "rule_id": "dashboard",
    }

    memberships = segment_membership.resolve_segment_memberships(
        db, USER, {"location": POINT}, [SEGMENT_ID]
    )
    assert memberships.unavailable == frozenset()
    assert memberships.members == frozenset()
    validate_segment_rules(copy.deepcopy(FURLONG_SEGMENT))  # the read gate passes


#: Stored before #1204, each with a context it would hold for if its word were
#: known.
STORED = {
    "json_path": (cond("json_path", "$.b", 1), {"a": {"b": 1}}),
    "semantic_version ne": (cond("semantic_version", "1.2.0", "ne"), {"a": "2.0.0"}),
    "array_length within": (cond("array_length", 2, "within"), {"a": [1, 2]}),
    "geo comparison within": (geo(comparison="within"), CENTRE),
    "geo unit furlong": (geo(unit="furlong"), CENTRE),
}


@pytest.mark.regression
@pytest.mark.parametrize("name", sorted(STORED))
@pytest.mark.parametrize("operator, holds", [("and", False), ("not", True)])
def test_a_stored_rule_using_a_refused_word_keeps_its_answer(name, operator, holds):
    """(1h) Stored rules are not re-judged: the condition a flag stored before
    #1204 still does not hold, and a ``NOT`` around it holds for every user."""
    condition, context = STORED[name]
    stored = native(condition, operator=operator)
    assert refusal(FLAG, stored) is not None  # a save now refuses it
    rules = normalise_targeting_rules(copy.deepcopy(stored), owner="test")
    assert rules is not None
    assert (match_targeting_rule(rules, context) is not None) is holds
    expected = (
        {"enabled": True, "reason": "targeting_rule", "rule_id": "r"}
        if holds
        else {"enabled": False, "reason": "rollout", "rule_id": None}
    )
    assert flag_detailed(stored, context, rollout=0) == expected


@pytest.mark.parametrize("unit", ["m", "km", "mi"])
def test_eq_holds_within_0_01_of_the_radius_in_the_rules_unit(unit):
    """The reference page's ``eq``: within 0.01 of the radius, in the unit."""
    (lat, lon), (rule_lat, rule_lon) = OAKLAND["a"], POINT
    distance = rules_engine._haversine_distance(lat, lon, rule_lat, rule_lon, unit)
    assert distance > 1  # Oakland is 13.4 km away: not a degenerate case
    for offset, holds in ((0.005, True), (-0.005, True), (0.02, False)):
        assert (
            apply_operator(
                OperatorType.GEO_DISTANCE,
                OAKLAND["a"],
                POINT,
                {"radius": distance + offset, "unit": unit, "comparison": "eq"},
            )
            is holds
        ), offset


# ---------------------------------------------------------------------------
# (1o) One definition of TIME_WINDOW_KEYS
# ---------------------------------------------------------------------------

_DEFINITION = re.compile(r"^\s*TIME_WINDOW_KEYS\s*(?::[^=]*)?=(?!=)", re.MULTILINE)


@pytest.mark.regression
def test_time_window_keys_has_one_definition():
    """The walker calls ``check_time_window``; a copy of its keys could drift."""
    roots = [BACKEND, REPO_ROOT / "modules"]
    found = [
        path.relative_to(REPO_ROOT).as_posix()
        for root in roots
        if root.is_dir()
        for path in sorted(root.rglob("*.py"))
        for _ in _DEFINITION.finditer(path.read_text("utf-8"))
    ]
    assert found == ["backend/app/core/rules_engine.py"]
