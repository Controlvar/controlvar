"""Targeting rules refuse, at save, a value their evaluator cannot read (#1525)
and a dashboard geo comparison it ignores (#1526).

A condition is refused on create and update when the branch of the evaluator
that reads it fails on the rule's own value, before any user's value can make
a difference (plan D211 v1.4-B3, S13): a ``before`` or ``after`` value that is
not a date, an ``array_length`` value that is not a whole number (or, for
``between``, not ``min`` and ``max``), a ``geo_distance`` radius that is
missing, below 0 or not a number, a ``percentage_bucket`` value that is not a
number (experiments; flags refuse the operator), and a native ``gt``, ``gte``,
``lt`` or ``lte`` value that is not a number. A dashboard ``geo_within_radius``
that carries a comparison is refused too: the converter drops it, so the rule
means "within the radius" whatever it says. A readable value whose range is
empty (a length below 0, a percentage of 0) stays accepted.

Pinned, without a database:

* (B1) every value of the corpus below, for each operator and kind it applies
  to, is refused on save or holds for some witness context, and every value
  some witness holds for is accepted. The witnesses are evaluated by the
  evaluators themselves, on rules that were never checked by the write path;
* (B2) the corpus's labels are the save's answers, and its size per operator
  and kind is written out, so it cannot shrink unnoticed;
* (B3) the exact message for each new reason at each path, ``NOT`` and
  ``default_rule`` included, and no message repeats the submitted value;
* (B5, G4, T3) stored rules keep their answers on every reader, for each
  value a save now refuses;
* (B6) each request model is total over JSON types at each position a
  predicate reads;
* (B8) #1204's words are judged first, and a message given before is kept;
* (B9) the readable values whose range is empty stay accepted;
* (G1, G2) a dashboard geo comparison is refused in all three spellings;
* (G6) the walker reads each submitted dashboard condition beside its
  converted one.
"""

from __future__ import annotations

import copy
import hashlib
import uuid
from types import SimpleNamespace
from typing import Any, Callable, Dict, FrozenSet, List, NamedTuple, Optional, Tuple
from unittest.mock import MagicMock, Mock

import pytest
from pydantic import ValidationError

from backend.app.core import targeting_adapter
from backend.app.core.pattern_match import PatternUnevaluable
from backend.app.core.rules_engine import (
    SegmentMembershipUnavailable,
    TimeWindowUnreadable,
)
from backend.app.core.targeting_adapter import (
    convert_dashboard_condition,
    match_targeting_rule,
    validate_segment_rules,
)
from backend.app.models.segment import SegmentStatus
from backend.app.schemas.experiment import ExperimentCreate
from backend.app.schemas.feature_flag import FeatureFlagCreate
from backend.app.schemas.segment import SegmentCreate
from backend.app.schemas.targeting_rule import TargetingRules
from backend.app.services import segment_membership
from backend.app.services.assignment_service import AssignmentService
from backend.app.services.rules_evaluation_service import RulesEvaluationService
from backend.tests.unit.core.test_targeting_vocabulary import (
    DASH_US,
    DISTANCE,
    EXPERIMENT,
    FALSEY,
    FLAG,
    LENGTH,
    NATIVE_KINDS,
    NOT_ON_FLAGS,
    POINT,
    SEGMENT,
    UNIT,
    US,
    cond,
    dashboard,
    native,
    refusal,
)
from backend.tests.unit.services.test_experiment_flag_targeting_parity import (
    USER,
    flag_detailed,
)

pytestmark = pytest.mark.unit

# The reasons, verbatim from plan D211 v1.4-B3 (texts 1 to 7, and the
# principal engineer's PE-B3-13 for a number).
DATE = (
    "not a date; use an ISO 8601 date such as 2030-01-31 or 2030-01-31T09:00:00Z, "
    "or a Unix timestamp"
)
LENGTH_VALUE = "not a whole number; use a number such as 3"
BETWEEN = "between needs a value with min and max, both whole numbers"
RADIUS = "radius must be a number of 0 or more"
NO_RADIUS = "a radius is required; use a number of 0 or more"
PERCENTAGE = "not a number; use a percentage from 0 to 100"
NUMBER = "not a number; use a number such as 5"
DASH_COMPARISON = (
    "comparison is not read in the dashboard shape, so the rule would mean within "
    "the radius; to use gt, lt, gte or eq, write the rule in the native shape"
)

ALL_KINDS = frozenset({FLAG, EXPERIMENT, SEGMENT})
NAN, INF = float("nan"), float("inf")
#: An integer no float can hold (PE-B3-5).
HUGE = 10**400

#: San Francisco; Oakland, 13.4 km from it; and near its antipode.
SF, OAK, FAR = POINT, [37.8044, -122.2712], [-37.7749, 57.8056]
SF_TEXT = "37.7749,-122.4194"


def dash_geo(value: Any, attribute: str = "a") -> Dict[str, Any]:
    """A dashboard ``geo_within_radius`` condition on ``attribute``."""
    return {"attribute": attribute, "operator": "geo_within_radius", "value": value}


def at_sf(radius: Any, **more: Any) -> Dict[str, Any]:
    """The dashboard object form of a point at ``SF``."""
    return {"lat": SF[0], "lon": SF[1], "radius": radius, **more}


def _show(value: Any) -> str:
    """A short label for a value (a 400-digit integer is not printed whole)."""
    text = repr(value)
    return text if len(text) <= 80 else f"<{type(value).__name__} of {len(text)} chars>"


# ---------------------------------------------------------------------------
# Witnesses: contexts that span each operator's domain (plan S4)
# ---------------------------------------------------------------------------

#: Dates decades either side of every corpus value, naive and aware. ISO text
#: only: the oracle never reads a timestamp in the local timezone.
DATE_WITNESSES = [
    {"a": "1900-01-01T00:00:00"},
    {"a": "2100-01-01T00:00:00"},
    {"a": "1900-01-01T00:00:00+00:00"},
    {"a": "2100-01-01T00:00:00+00:00"},
]
#: Lists of every length from 0 to 12, above the largest corpus length (10).
LENGTH_WITNESSES = [{"a": [0] * n} for n in range(13)]
GEO_WITNESSES = [{"a": SF}, {"a": OAK}, {"a": FAR}]
NUMBER_WITNESSES = [
    {"a": n} for n in (-1e9, -100, -5.5, -5, 0, 0.5, 1, 5, 5.5, 10, 1000, 1e9)
]


def _bucket(user: str) -> int:
    return int(hashlib.md5(user.encode(), usedforsecurity=False).hexdigest(), 16) % 100


def _one_user_per_bucket() -> List[Dict[str, Any]]:
    """One user id in each of percentage_bucket's buckets, 0 to 99."""
    found: Dict[int, str] = {}
    index = 0
    while len(found) < 100:
        found.setdefault(_bucket(f"w{index}"), f"w{index}")
        index += 1
    return [{"a": found[bucket]} for bucket in range(100)]


BUCKET_WITNESSES = _one_user_per_bucket()


# ---------------------------------------------------------------------------
# The corpus (B1, B2): each group's accepted and refused values
# ---------------------------------------------------------------------------


class Group(NamedTuple):
    kinds: FrozenSet[str]
    build: Callable[[Any], Dict[str, Any]]
    witnesses: List[Dict[str, Any]]
    accepted: Tuple[Any, ...]
    refused: Tuple[Any, ...]


#: At least one accepted value per input form each predicate reads (PE-B3-7):
#: ISO text (a date, naive, Z, an offset, the basic form), an integer, a
#: float and a boolean timestamp.
DATES = (
    (
        "2026-01-01",
        "2026-01-01T09:30:00",
        "2026-01-01T09:30:00Z",
        "2026-01-01T09:30:00+05:30",
        "20260101",
        1700000000,
        1.7e9,
        True,
    ),
    # 1e12, -1e12, 1e17 and 1e20 are timestamps no date holds, each raising a
    # different exception (PE-B3-6); refusal is asserted, never which.
    (
        "not-a-date",
        "next week",
        "",
        "nan",
        "2026-13-01",
        "01/31/2030",
        1e12,
        -1e12,
        1e17,
        1e20,
        NAN,
        INF,
        -INF,
        HUGE,
        None,
        [1],
        {},
        {"date": "2026-01-01"},
    ),
)
#: int() reads: an integer, a float (3.7 is 3), a boolean, text with spaces or
#: an underscore. Not 0: "lt 0" is an empty range (B9).
LENGTHS = (
    (3, 3.7, True, "3", " 3 ", "1_0"),
    (
        "abc",
        "3.0",
        "nan",
        "",
        None,
        [3],
        {"min": 1, "max": 3},
        {},
        NAN,
        INF,
        -INF,
        "1" * 5000,
    ),
)
BOUNDS = (
    (
        {"min": 1, "max": 3},
        {"min": "1", "max": "3"},
        {"min": 1.5, "max": 3.9},
        {"min": True, "max": " 3 "},
        {"min": 0, "max": "1_0"},
        {"min": 1, "max": 3, "note": "x"},
    ),
    (
        3,
        "1-3",
        [1, 3],
        {"min": 1},
        {"max": 3},
        {"min": "a", "max": 3},
        {"min": 1, "max": None},
        {"min": 1, "max": INF},
        {"min": NAN, "max": 3},
        {},
        None,
        "",
    ),
)
#: additional_value as the radius itself, or as an object with ``radius``.
RADII = (
    (
        10,
        10.5,
        "10",
        True,
        0,
        "inf",
        INF,
        {"radius": 10},
        {"radius": "10"},
        {"radius": 0},
        {"radius": 10, "unit": "mi"},
        {"radius": INF},
    ),
    (
        -1,
        -0.5,
        "-1",
        "abc",
        "",
        "nan",
        NAN,
        -INF,
        "-inf",
        HUGE,
        [10],
        {"radius": -1},
        {"radius": "abc"},
        {"radius": "nan"},
        {"radius": None},
        {"radius": [1]},
        {"radius": HUGE},
        {"radius": -INF},
        {},
        {"unit": "km"},
        {"comparison": "lte"},
    ),
)
#: The dashboard object, list and text forms. A missing radius, or one that
#: is not a number, is refused by the converter, as before.
DASH_RADII = (
    (
        at_sf(10),
        at_sf("10"),
        at_sf(0),
        at_sf(True),
        at_sf("inf"),
        at_sf(10, comparison=None),
        at_sf(10, comparison="lte"),
        [SF[0], SF[1], 10],
        [SF[0], SF[1], 10, "mi"],
        f"{SF_TEXT},10",
        f"{SF_TEXT},10,km",
        f"{SF_TEXT},0",
    ),
    (
        at_sf(-1),
        at_sf("-1"),
        at_sf("nan"),
        at_sf(NAN),
        at_sf("-inf"),
        at_sf(-INF),
        [SF[0], SF[1], -1],
        f"{SF_TEXT},-1",
        f"{SF_TEXT},nan",
        f"{SF_TEXT},-inf",
        {"lat": SF[0], "lon": SF[1]},
        f"{SF_TEXT},abc",
        [SF[0], SF[1]],
    ),
)
PERCENTAGES = (
    (50, 50.5, "50", " 50 ", True, 0.5, 100, 150, "inf", INF, "1e2"),
    ("abc", "", "nan", NAN, None, [50], {}, {"pct": 50}, HUGE),
)
NUMBERS = (
    (5, 5.5, -5, 0, "5", " 5 ", "5.0", "1e3", "1_0", True),
    ("abc", "", "nan", "five", NAN, None, [5], {}, {"n": 5}, HUGE),
)


def _groups() -> Dict[str, Group]:
    groups: Dict[str, Group] = {}
    for operator in ("before", "after"):
        groups[operator] = Group(
            frozenset(NATIVE_KINDS),
            lambda v, o=operator: native(cond(o, v)),
            DATE_WITNESSES,
            *DATES,
        )
    for word in (*FALSEY, "eq", "gt", "gte", "lt", "lte"):
        groups[f"array_length {word!r}"] = Group(
            frozenset(NATIVE_KINDS),
            lambda v, w=word: native(cond("array_length", v, w)),
            LENGTH_WITNESSES,
            *LENGTHS,
        )
    groups["array_length 'between'"] = Group(
        frozenset(NATIVE_KINDS),
        lambda v: native(cond("array_length", v, "between")),
        LENGTH_WITNESSES,
        *BOUNDS,
    )
    groups["geo_distance radius"] = Group(
        frozenset(NATIVE_KINDS),
        lambda v: native(cond("geo_distance", SF, v)),
        GEO_WITNESSES,
        *RADII,
    )
    groups["geo_within_radius radius"] = Group(
        ALL_KINDS, lambda v: dashboard(dash_geo(v)), GEO_WITNESSES, *DASH_RADII
    )
    groups["percentage_bucket"] = Group(
        frozenset({EXPERIMENT}),
        lambda v: native(cond("percentage_bucket", v)),
        BUCKET_WITNESSES,
        *PERCENTAGES,
    )
    for operator in ("gt", "gte", "lt", "lte"):
        groups[operator] = Group(
            frozenset(NATIVE_KINDS),
            lambda v, o=operator: native(cond(o, v)),
            NUMBER_WITNESSES,
            *NUMBERS,
        )
    return groups


GROUPS = _groups()


def _stored(raw: Any) -> Optional[TargetingRules]:
    """The rules as an evaluator reads them when stored: no write-path check.
    ``None`` where the read gate refuses them (nothing then matches)."""
    raw = copy.deepcopy(raw)
    try:
        if "groups" in raw:
            return targeting_adapter._from_dashboard(raw)
        return TargetingRules.model_validate(raw)
    except Exception:
        return None


def _holds(kind: str, rules: TargetingRules, context: Dict[str, Any]) -> bool:
    """Whether ``kind``'s evaluator matches the rules for ``context``; a
    ruleset it abandons does not match. A segment's membership is decided as a
    flag's rules are (``segment_membership``)."""
    try:
        if kind == EXPERIMENT:
            matched, _ = RulesEvaluationService().evaluate_rules_with_validation(
                rules, dict(context), track_metrics=False
            )
            return matched is not None
        return match_targeting_rule(rules, dict(context)) is not None
    except (PatternUnevaluable, TimeWindowUnreadable, SegmentMembershipUnavailable):
        return False


def _some_witness_holds(kind: str, group: Group, value: Any) -> bool:
    rules = _stored(group.build(value))
    return rules is not None and any(_holds(kind, rules, w) for w in group.witnesses)


def _rows() -> List[Tuple[str, str]]:
    return [(name, kind) for name, g in GROUPS.items() for kind in sorted(g.kinds)]


def _values(group: Group) -> Tuple[Any, ...]:
    return (*group.accepted, *group.refused)


@pytest.mark.regression
@pytest.mark.parametrize("name, kind", _rows(), ids=lambda v: str(v))
def test_every_value_is_refused_or_can_match(name, kind):
    """(B1, the silent direction) A value the save accepts holds for some
    witness: a value the evaluator cannot read is not saved to never match."""
    group = GROUPS[name]
    problems = [
        f"{name} {_show(value)}: accepted on a {kind} and no context matches"
        for value in _values(group)
        if refusal(kind, group.build(value)) is None
        and not _some_witness_holds(kind, group, value)
    ]
    assert problems == []


@pytest.mark.regression
@pytest.mark.parametrize("name, kind", _rows(), ids=lambda v: str(v))
def test_every_usable_value_is_accepted(name, kind):
    """(B1, the loud direction) A value that holds for some witness is not
    refused on save."""
    group = GROUPS[name]
    problems = [
        f"{name} {_show(value)}: refused on a {kind} but a context matches"
        for value in _values(group)
        if refusal(kind, group.build(value)) is not None
        and _some_witness_holds(kind, group, value)
    ]
    assert problems == []


#: (group, kind) -> (accepted, refused) values in the corpus (B2).
CORPUS_SIZE = {
    **{(o, k): (8, 18) for o in ("before", "after") for k in NATIVE_KINDS},
    **{
        (f"array_length {w!r}", k): (6, 12)
        for w in (*FALSEY, "eq", "gt", "gte", "lt", "lte")
        for k in NATIVE_KINDS
    },
    **{("array_length 'between'", k): (6, 12) for k in NATIVE_KINDS},
    **{("geo_distance radius", k): (12, 21) for k in NATIVE_KINDS},
    **{("geo_within_radius radius", k): (12, 13) for k in sorted(ALL_KINDS)},
    ("percentage_bucket", EXPERIMENT): (11, 9),
    **{(o, k): (10, 10) for o in ("gt", "gte", "lt", "lte") for k in NATIVE_KINDS},
}


@pytest.mark.regression
def test_the_corpus_is_the_saves_answer_and_cannot_shrink():
    """(B2) Each labelled value gets its label from the save: the set refused
    is exactly the set labelled refused. The corpus's size per group and kind
    is written out above, so deleting values fails here."""
    labelled, refused = set(), set()
    sizes = {}
    for name, kind in _rows():
        group = GROUPS[name]
        sizes[(name, kind)] = (len(group.accepted), len(group.refused))
        for label, values in (("accepted", group.accepted), ("refused", group.refused)):
            for index, value in enumerate(values):
                row = (name, kind, f"{label}[{index}]", _show(value))
                if label == "refused":
                    labelled.add(row)
                if refusal(kind, group.build(value)) is not None:
                    refused.add(row)
    assert sizes == CORPUS_SIZE
    assert refused == labelled, {
        "refused but labelled accepted": sorted(refused - labelled),
        "accepted but labelled refused": sorted(labelled - refused),
    }
    assert len(refused) == sum(r for _, r in CORPUS_SIZE.values())


# ---------------------------------------------------------------------------
# (B3, B8) The refusals, with their exact messages
# ---------------------------------------------------------------------------

_RULE = "rules[0].rule"
_FIRST = f"{_RULE}.conditions[0]"


def _in_not(condition: Dict[str, Any]) -> Dict[str, Any]:
    return native(US, groups=[{"operator": "not", "conditions": [condition]}])


def _in_default_rule(condition: Dict[str, Any]) -> Dict[str, Any]:
    return native(
        US,
        default_rule={
            "id": "d",
            "rule": {"operator": "and", "conditions": [condition]},
        },
    )


def _three_groups(geo_value: Any) -> Dict[str, Any]:
    """Three groups, a ``NOT`` among them, with a geo condition at
    ``groups[1].conditions[1]`` (G6)."""
    return {
        "logical_operator": "OR",
        "groups": [
            {
                "logical_operator": "AND",
                "conditions": [
                    DASH_US,
                    {"attribute": "plan", "operator": "in", "value": "pro, team"},
                ],
            },
            {
                "logical_operator": "NOT",
                "conditions": [
                    {"attribute": "v", "operator": "semver_gte", "value": "2"},
                    {
                        "attribute": "a",
                        "operator": " geo_within_radius ",
                        "value": geo_value,
                    },
                ],
            },
            {
                "logical_operator": "OR",
                "conditions": [
                    {"attribute": "age", "operator": "greater_than", "value": "18"},
                    dash_geo(f"{SF_TEXT},5,mi"),
                ],
            },
        ],
    }


NATIVE = frozenset(NATIVE_KINDS)

#: name -> (kinds, rules, message): the message is the API's ``msg`` after
#: ``Value error, ``.
VALUE_REFUSED: Dict[str, Tuple[FrozenSet[str], Any, str]] = {
    "before not a date": (
        NATIVE,
        native(US, cond("before", "not-a-date")),
        f"{_RULE}.conditions[1].value: {DATE}",
    ),
    "after an object, inside NOT": (
        NATIVE,
        _in_not(cond("after", {})),
        f"{_RULE}.groups[0].conditions[0].value: {DATE}",
    ),
    "before a timestamp no date holds, in default_rule": (
        NATIVE,
        _in_default_rule(cond("before", 1e20)),
        f"default_rule.rule.conditions[0].value: {DATE}",
    ),
    "array_length 'abc'": (
        NATIVE,
        native(cond("array_length", "abc")),
        f"{_FIRST}.value: {LENGTH_VALUE}",
    ),
    "array_length gt '3.0', inside NOT": (
        NATIVE,
        _in_not(cond("array_length", "3.0", "gt")),
        f"{_RULE}.groups[0].conditions[0].value: {LENGTH_VALUE}",
    ),
    "array_length between without max": (
        NATIVE,
        native(cond("array_length", {"min": 1}, "between")),
        f"{_FIRST}.value: {BETWEEN}",
    ),
    "array_length between a number, in default_rule": (
        NATIVE,
        _in_default_rule(cond("array_length", 3, "between")),
        f"default_rule.rule.conditions[0].value: {BETWEEN}",
    ),
    "geo_distance radius -1": (
        NATIVE,
        native(cond("geo_distance", SF, -1)),
        f"{_FIRST}.additional_value: {RADIUS}",
    ),
    "geo_distance radius 'abc' in an object, inside NOT": (
        NATIVE,
        _in_not(cond("geo_distance", SF, {"radius": "abc"})),
        f"{_RULE}.groups[0].conditions[0].additional_value: {RADIUS}",
    ),
    "geo_distance with an object without radius": (
        NATIVE,
        native(cond("geo_distance", SF, {"unit": "km"})),
        f"{_FIRST}.additional_value: {NO_RADIUS}",
    ),
    "geo_distance with an empty object": (
        NATIVE,
        native(cond("geo_distance", SF, {})),
        f"{_FIRST}.additional_value: {NO_RADIUS}",
    ),
    # PE-B3-4: an absent or null additional_value never reaches text 5; the
    # Condition model refuses it first, as before.
    "geo_distance without additional_value": (
        NATIVE,
        native(cond("geo_distance", SF)),
        f"{_FIRST}: not valid",
    ),
    "geo_distance with a null additional_value": (
        NATIVE,
        native(cond("geo_distance", SF, None)),
        f"{_FIRST}: not valid",
    ),
    "percentage_bucket 'abc' on an experiment": (
        frozenset({EXPERIMENT}),
        native(US, cond("percentage_bucket", "abc")),
        f"{_RULE}.conditions[1].value: {PERCENTAGE}",
    ),
    "gt 'abc'": (NATIVE, native(cond("gt", "abc")), f"{_FIRST}.value: {NUMBER}"),
    "lte null, in default_rule": (
        NATIVE,
        _in_default_rule(cond("lte", None)),
        f"default_rule.rule.conditions[0].value: {NUMBER}",
    ),
    "dashboard radius -1 in text": (
        ALL_KINDS,
        dashboard(DASH_US, dash_geo(f"{SF_TEXT},-1")),
        f"groups[0].conditions[1].value: {RADIUS}",
    ),
    "dashboard radius NaN in an object": (
        ALL_KINDS,
        dashboard(dash_geo(at_sf("nan"))),
        f"groups[0].conditions[0].value: {RADIUS}",
    ),
    "dashboard comparison gt": (
        ALL_KINDS,
        dashboard(DASH_US, dash_geo(at_sf(10, comparison="gt"))),
        f"groups[0].conditions[1].value: {DASH_COMPARISON}",
    ),
    "dashboard comparison at groups[1].conditions[1], in a NOT group": (
        ALL_KINDS,
        _three_groups(at_sf(10, comparison="gt")),
        f"groups[1].conditions[1].value: {DASH_COMPARISON}",
    ),
    "dashboard radius -1 at groups[1].conditions[1], in a NOT group": (
        ALL_KINDS,
        _three_groups(f"{SF_TEXT},-1"),
        f"groups[1].conditions[1].value: {RADIUS}",
    ),
    # (B8) #1204's checks come first; the operator check first of all.
    "percentage_bucket 'abc' on a flag": (
        frozenset({FLAG}),
        native(cond("percentage_bucket", "abc")),
        f"{_FIRST}.operator: {NOT_ON_FLAGS}",
    ),
    "array_length 'within' with a value that is not a number": (
        NATIVE,
        native(cond("array_length", "abc", "within")),
        f"{_FIRST}.additional_value: {LENGTH}",
    ),
    "geo_distance comparison 'within' with radius -1": (
        NATIVE,
        native(cond("geo_distance", SF, {"radius": -1, "comparison": "within"})),
        f"{_FIRST}.additional_value: {DISTANCE}",
    ),
    "geo_distance unit 'furlong' without a radius": (
        NATIVE,
        native(cond("geo_distance", SF, {"unit": "furlong"})),
        f"{_FIRST}.additional_value: {UNIT}",
    ),
    "dashboard unit 'yd' and comparison gt": (
        ALL_KINDS,
        dashboard(dash_geo(at_sf(10, unit="yd", comparison="gt"))),
        f"groups[0].conditions[0].value: {UNIT}",
    ),
    "dashboard fifth part 'gt' after unit 'yd'": (
        ALL_KINDS,
        dashboard(dash_geo(f"{SF_TEXT},10,yd,gt")),
        f"groups[0].conditions[0].value: {UNIT}",
    ),
    # Within #1525 and #1526, the comparison is judged before the radius.
    "dashboard comparison gt with radius -1": (
        ALL_KINDS,
        dashboard(dash_geo(at_sf(-1, comparison="gt"))),
        f"groups[0].conditions[0].value: {DASH_COMPARISON}",
    ),
    # One 422 names one condition, the first in document order: here the
    # unreadable date, where before #1525 the version comparison was named.
    "an unreadable date before an unknown version comparison": (
        NATIVE,
        native(cond("before", "x"), cond("semantic_version", "1.2.0", "ne")),
        f"{_FIRST}.value: {DATE}",
    ),
    # Refused before #1525, with the same message.
    "dashboard greater_than 'abc'": (
        ALL_KINDS,
        dashboard({"attribute": "a", "operator": "greater_than", "value": "abc"}),
        "groups[0].conditions[0].value: value is not valid for the operator",
    ),
    "dashboard geo without a radius": (
        ALL_KINDS,
        dashboard(dash_geo({"lat": SF[0], "lon": SF[1]})),
        "groups[0].conditions[0].value: value is not valid for the operator",
    ),
}


def _value_refused_rows() -> List[Tuple[str, str]]:
    return [
        (name, kind)
        for name, (kinds, _, _) in VALUE_REFUSED.items()
        for kind in sorted(kinds)
    ]


@pytest.mark.regression
@pytest.mark.parametrize("name, kind", _value_refused_rows(), ids=lambda v: str(v))
def test_a_value_is_refused_with_fixed_text(name, kind):
    _, rules, message = VALUE_REFUSED[name]
    assert refusal(kind, rules) == message


SECRET = "zzSECRETzz" + "x" * 5000

#: (kinds, rules, path) with the secret where each new reason reads it.
ECHO = [
    (NATIVE, native(cond("before", SECRET)), f"{_FIRST}.value"),
    (NATIVE, native(cond("array_length", SECRET)), f"{_FIRST}.value"),
    (
        NATIVE,
        native(cond("array_length", {"min": SECRET, "max": 3}, "between")),
        f"{_FIRST}.value",
    ),
    (NATIVE, native(cond("geo_distance", SF, SECRET)), f"{_FIRST}.additional_value"),
    (
        NATIVE,
        native(cond("geo_distance", SF, {"radius": SECRET})),
        f"{_FIRST}.additional_value",
    ),
    (
        NATIVE,
        native(cond("geo_distance", SF, {SECRET: 1})),
        f"{_FIRST}.additional_value",
    ),
    (
        frozenset({EXPERIMENT}),
        native(cond("percentage_bucket", SECRET)),
        f"{_FIRST}.value",
    ),
    (NATIVE, native(cond("gte", SECRET)), f"{_FIRST}.value"),
    (
        ALL_KINDS,
        dashboard(dash_geo(at_sf(10, comparison=SECRET))),
        "groups[0].conditions[0].value",
    ),
    (
        ALL_KINDS,
        dashboard(dash_geo(f"{SF_TEXT},10,km,{SECRET}")),
        "groups[0].conditions[0].value",
    ),
]


@pytest.mark.regression
def test_a_message_never_repeats_the_submitted_value():
    """(B3) A 5 kB value: the message is the path and a reason of at most 200
    characters, and the value is not in it."""
    for kinds, rules, path in ECHO:
        for kind in sorted(kinds):
            message = refusal(kind, rules)
            assert message is not None and message.startswith(f"{path}: "), (
                kind,
                path,
                message,
            )
            assert "zzSECRETzz" not in message
            assert len(message) <= len(path) + 2 + 200


def test_the_reasons_are_one_line_of_at_most_200_characters():
    for reason in (
        DATE,
        LENGTH_VALUE,
        BETWEEN,
        RADIUS,
        NO_RADIUS,
        PERCENTAGE,
        NUMBER,
        DASH_COMPARISON,
    ):
        assert reason in targeting_adapter.VOCABULARY_REASONS
        assert "\n" not in reason and len(reason) <= 200, reason


# ---------------------------------------------------------------------------
# (G1, G2) A dashboard geo comparison, in all three spellings
# ---------------------------------------------------------------------------

#: name -> (value, refused): what each spelling of a comparison gets.
SPELLINGS: Dict[str, Tuple[Any, bool]] = {
    **{
        f"object comparison {word!r}": (at_sf(10, comparison=word), True)
        for word in ("gt", "gte", "lt", "eq", "within", "LTE", "lte ", "", 5, False)
    },
    "object comparison null": (at_sf(10, comparison=None), False),
    "object comparison 'lte'": (at_sf(10, comparison="lte"), False),
    "object without comparison": (at_sf(10), False),
    # PE-B3-8 (decided by the engineering manager): any fifth item or part.
    **{
        f"list fifth item {item!r}": ([SF[0], SF[1], 10, "km", item], True)
        for item in ("gt", None, "lte", "")
    },
    "list of six items": ([SF[0], SF[1], 10, "km", "gt", "x"], True),
    "list of four items": ([SF[0], SF[1], 10, "km"], False),
    "list of three items": ([SF[0], SF[1], 10], False),
    **{
        f"text fifth part {part!r}": (f"{SF_TEXT},10,km,{part}", True)
        for part in ("gt", "lte", "", "null")
    },
    "text without a unit and a fifth part": (f"{SF_TEXT},10,,gt", True),
    "text of four parts": (f"{SF_TEXT},10,km", False),
    "text of four parts, the unit empty": (f"{SF_TEXT},10,", False),
    "text of three parts": (f"{SF_TEXT},10", False),
}


@pytest.mark.regression
@pytest.mark.parametrize("name", sorted(SPELLINGS))
@pytest.mark.parametrize("kind", sorted(ALL_KINDS))
def test_a_dashboard_geo_comparison_is_refused(name, kind):
    """(G1, G2) Refused at the condition's value with text 7, on flags,
    experiments and segments; a value without one still saves."""
    value, refused = SPELLINGS[name]
    expected = f"groups[0].conditions[1].value: {DASH_COMPARISON}" if refused else None
    assert refusal(kind, dashboard(DASH_US, dash_geo(value))) == expected


# ---------------------------------------------------------------------------
# (G6) The submitted condition is read beside its converted one
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_each_converted_dashboard_condition_is_the_submitted_one_in_its_place():
    """The walker reads ``groups[i].conditions[j]`` of the submitted value for
    the converted rule's condition at the same place (a comparison is only in
    the submitted one). That holds while the converter keeps every group and
    condition in place; this pins it over three groups, a ``NOT`` among
    them, with operators the converter treats differently."""
    raw = _three_groups(at_sf(10, comparison="lte"))
    converted = targeting_adapter._from_dashboard(copy.deepcopy(raw))
    (rule,) = converted.rules
    assert len(rule.rule.groups) == len(raw["groups"])
    for gi, (group, raw_group) in enumerate(zip(rule.rule.groups, raw["groups"])):
        assert len(group.conditions) == len(raw_group["conditions"]), gi
        for ci, (condition, raw_condition) in enumerate(
            zip(group.conditions, raw_group["conditions"])
        ):
            assert condition == convert_dashboard_condition(raw_condition), (gi, ci)


@pytest.mark.regression
@pytest.mark.parametrize("kind", sorted(ALL_KINDS))
def test_a_comparison_is_found_in_every_place(kind):
    """A comparison on either geo condition of the three groups is named at
    its own place; the other one, without a comparison, is not."""
    rules = _three_groups(at_sf(10))
    assert refusal(kind, rules) is None
    second = copy.deepcopy(rules)
    second["groups"][2]["conditions"][1]["value"] = f"{SF_TEXT},5,mi,gt"
    assert refusal(kind, second) == (
        f"groups[2].conditions[1].value: {DASH_COMPARISON}"
    )
    first = _three_groups({**at_sf(10), "comparison": "gt"})
    assert refusal(kind, first) == f"groups[1].conditions[1].value: {DASH_COMPARISON}"


# ---------------------------------------------------------------------------
# (B9) Readable values whose range is empty stay accepted (S13)
# ---------------------------------------------------------------------------

#: name -> (kinds, rules, witnesses or None). With witnesses, none of them
#: holds: the range is empty, like a rollout of 0%. Tightening any of these is
#: a decision, not drift.
EMPTY_OR_EDGE: Dict[str, Tuple[FrozenSet[str], Any, Optional[List[Dict]]]] = {
    "array_length between min above max": (
        NATIVE,
        native(cond("array_length", {"min": 3, "max": 1}, "between")),
        LENGTH_WITNESSES,
    ),
    "array_length lt 0": (
        NATIVE,
        native(cond("array_length", 0, "lt")),
        LENGTH_WITNESSES,
    ),
    "array_length eq -1": (
        NATIVE,
        native(cond("array_length", -1, "eq")),
        LENGTH_WITNESSES,
    ),
    "array_length eq 10**400": (
        NATIVE,
        native(cond("array_length", HUGE, "eq")),
        LENGTH_WITNESSES,
    ),
    "array_length lt 10**400 (every list)": (
        NATIVE,
        native(cond("array_length", HUGE, "lt")),
        None,
    ),
    **{
        f"percentage_bucket {_show(v)}": (
            frozenset({EXPERIMENT}),
            native(cond("percentage_bucket", v)),
            BUCKET_WITNESSES,
        )
        for v in (0, -5, -INF)
    },
    "geo_distance radius 0 with lt": (
        NATIVE,
        native(cond("geo_distance", SF, {"radius": 0, "comparison": "lt"})),
        GEO_WITNESSES,
    ),
    **{
        f"geo_distance radius infinity with {word}": (
            NATIVE,
            native(cond("geo_distance", SF, {"radius": INF, "comparison": word})),
            GEO_WITNESSES,
        )
        for word in ("gt", "gte", "eq")
    },
    "gt infinity": (NATIVE, native(cond("gt", INF)), NUMBER_WITNESSES),
    "lt -infinity": (NATIVE, native(cond("lt", -INF)), NUMBER_WITNESSES),
    # Hold only for an infinite attribute (an SDK can send 1e400).
    "gte infinity": (NATIVE, native(cond("gte", INF)), None),
    "lte -infinity": (NATIVE, native(cond("lte", -INF)), None),
    "lt infinity (every number)": (NATIVE, native(cond("lt", INF)), None),
    "time_window days []": (
        NATIVE,
        native(cond("time_window", {"start": "x", "end": "y", "days": []})),
        [{"a": "2024-01-10T12:00:00"}, {"a": "2024-01-13T12:00:00"}],
    ),
}


def _empty_rows() -> List[Tuple[str, str]]:
    return [
        (name, kind)
        for name, (kinds, _, _) in EMPTY_OR_EDGE.items()
        for kind in sorted(kinds)
    ]


@pytest.mark.regression
@pytest.mark.parametrize("name, kind", _empty_rows(), ids=lambda v: str(v))
def test_a_readable_value_whose_range_is_empty_is_accepted(name, kind):
    _, rules, witnesses = EMPTY_OR_EDGE[name]
    assert refusal(kind, rules) is None
    if witnesses is not None:
        stored = _stored(rules)
        assert stored is not None
        assert not any(_holds(kind, stored, w) for w in witnesses)


# ---------------------------------------------------------------------------
# (B6) Each request model is total over JSON types
# ---------------------------------------------------------------------------

#: Every JSON type, and the values that break a careless float() or int().
JSON_VALUES: Tuple[Any, ...] = (
    "x",
    "",
    0,
    -1,
    1.5,
    NAN,
    INF,
    -INF,
    True,
    None,
    [],
    {},
    [{"a": [1]}],
    {"a": {"b": [None]}},
    "9" * 5000,
    HUGE,
    -HUGE,
    # Timestamps no date holds, each raising a different exception (PE-B3-6).
    1e12,
    -1e12,
    1e17,
    1e20,
)

#: (name, kinds, build): each position a #1525 or #1526 check reads. A
#: dashboard geo value's numbers are read by the converter first, which this
#: change does not touch; its comparison and fifth item are read here.
POSITIONS: List[Tuple[str, FrozenSet[str], Callable[[Any], Dict[str, Any]]]] = [
    ("before value", NATIVE, lambda v: native(cond("before", v))),
    ("after value", NATIVE, lambda v: native(cond("after", v))),
    ("array_length value", NATIVE, lambda v: native(cond("array_length", v))),
    (
        "array_length between value",
        NATIVE,
        lambda v: native(cond("array_length", v, "between")),
    ),
    (
        "array_length between min",
        NATIVE,
        lambda v: native(cond("array_length", {"min": v, "max": 3}, "between")),
    ),
    ("geo_distance radius", NATIVE, lambda v: native(cond("geo_distance", SF, v))),
    (
        "geo_distance radius in an object",
        NATIVE,
        lambda v: native(cond("geo_distance", SF, {"radius": v})),
    ),
    (
        "percentage_bucket value",
        frozenset({EXPERIMENT}),
        lambda v: native(cond("percentage_bucket", v)),
    ),
    *[
        (f"{o} value", NATIVE, lambda v, o=o: native(cond(o, v)))
        for o in ("gt", "gte", "lt", "lte")
    ],
    (
        "dashboard comparison",
        ALL_KINDS,
        lambda v: dashboard(dash_geo(at_sf(10, comparison=v))),
    ),
    (
        "dashboard fifth item",
        ALL_KINDS,
        lambda v: dashboard(dash_geo([SF[0], SF[1], 10, "km", v])),
    ),
]

#: For each position, rules with a readable value there, which every kind
#: saves.
CONTROLS: Dict[str, Dict[str, Any]] = {
    "before value": native(cond("before", "2026-01-01")),
    "after value": native(cond("after", 1700000000)),
    "array_length value": native(cond("array_length", 3)),
    "array_length between value": native(
        cond("array_length", {"min": 1, "max": 3}, "between")
    ),
    "array_length between min": native(
        cond("array_length", {"min": 1, "max": 3}, "between")
    ),
    "geo_distance radius": native(cond("geo_distance", SF, 10)),
    "geo_distance radius in an object": native(
        cond("geo_distance", SF, {"radius": 10})
    ),
    "percentage_bucket value": native(cond("percentage_bucket", 50)),
    **{f"{o} value": native(cond(o, 5)) for o in ("gt", "gte", "lt", "lte")},
    "dashboard comparison": dashboard(dash_geo(at_sf(10, comparison="lte"))),
    "dashboard fifth item": dashboard(dash_geo([SF[0], SF[1], 10, "km"])),
}

_VARIANTS = [
    {"name": "Control", "is_control": True, "traffic_allocation": 50},
    {"name": "Treatment", "is_control": False, "traffic_allocation": 50},
]
_METRICS = [
    {
        "name": "Conversion Rate",
        "event_name": "purchase",
        "metric_type": "conversion",
        "is_primary": True,
    }
]


def _request(kind: str, rules: Dict[str, Any]) -> None:
    """Validate ``rules`` in ``kind``'s create request, as the route does."""
    rules = copy.deepcopy(rules)
    if kind == FLAG:
        FeatureFlagCreate.model_validate(
            {"key": "b6-flag", "name": "B6", "targeting_rules": rules}
        )
    elif kind == EXPERIMENT:
        ExperimentCreate.model_validate(
            {
                "name": "B6 experiment",
                "description": "Totality over JSON types",
                "hypothesis": "Every value answers 422 or saves",
                "experiment_type": "a_b",
                "targeting_rules": rules,
                "variants": _VARIANTS,
                "metrics": _METRICS,
            }
        )
    else:
        SegmentCreate.model_validate({"name": "B6 segment", "rules": rules})


@pytest.mark.regression
@pytest.mark.parametrize("name", [p[0] for p in POSITIONS])
def test_every_request_model_is_total_over_json_types(name):
    """(B6) Each value is accepted or refused with a ValidationError (422);
    no other exception escapes (it would be a 500)."""
    _, kinds, build = next(p for p in POSITIONS if p[0] == name)
    # The bodies are valid: the same position with a readable value saves,
    # so a ValidationError below is about the value.
    for kind in sorted(kinds):
        _request(kind, CONTROLS[name])
    problems = []
    for value in JSON_VALUES:
        for kind in sorted(kinds):
            try:
                _request(kind, build(value))
            except ValidationError:
                pass
            except Exception as exc:  # the point of the test
                problems.append(
                    f"{name} {_show(value)} on a {kind}: {type(exc).__name__}"
                )
    assert problems == []


# ---------------------------------------------------------------------------
# (B5, G4, T3) Stored rules keep their answers on every reader
# ---------------------------------------------------------------------------

ROLLOUT_OFF = {"enabled": False, "reason": "rollout", "rule_id": None}
ERROR_OFF = {"enabled": False, "reason": "error", "rule_id": None}
NOT_MATCHED = (False, "No targeting rules matched")


def _experiment(raw: Any, context: Dict[str, Any]) -> Tuple[bool, str]:
    """``(eligible, reason)`` from experiment assignment's own evaluation."""
    service = AssignmentService(Mock())
    experiment = SimpleNamespace(id="b5", targeting_rules=copy.deepcopy(raw))
    result = service._evaluate_experiment_targeting(
        experiment,
        AssignmentService._build_targeting_context(USER, context),
        user_id=USER,
    )
    return result["eligible"], result["reason"]


#: For each operator: a value its branch reads in the attribute ``a``.
READ_IN_A = {
    "before": "2000-01-01T00:00:00",
    "after": "2000-01-01T00:00:00",
    "array_length": [1, 2],
    "geo_distance": SF,
    "percentage_bucket": "user-1",
    "gt": 5,
    "gte": 5,
    "lt": 5,
    "lte": 5,
}

#: The stored values whose evaluation abandons the whole ruleset, today: a
#: timestamp no date can hold, and an integer no float can hold for gt, gte,
#: lt and lte (and, on an experiment, for percentage_bucket). Every other
#: refused value never holds.
_ABANDONED = {
    *{
        (o, _show(v))
        for o in ("before", "after")
        for v in (1e17, 1e20, INF, -INF, HUGE)
    },
    *{(o, _show(HUGE)) for o in ("gt", "gte", "lt", "lte", "percentage_bucket")},
}


def _native_refused() -> List[Tuple[str, str, Any, str]]:
    """``(operator, label, value, field)`` for each native value the corpus
    labels refused, one comparison per operator (array_length's absent one
    and between)."""
    rows = []
    for name, operator, field in (
        ("before", "before", "value"),
        ("after", "after", "value"),
        ("array_length absent", "array_length", "value"),
        ("array_length 'between'", "array_length", "value"),
        ("geo_distance radius", "geo_distance", "additional_value"),
        ("percentage_bucket", "percentage_bucket", "value"),
        ("gt", "gt", "value"),
        ("gte", "gte", "value"),
        ("lt", "lt", "value"),
        ("lte", "lte", "value"),
    ):
        group = GROUPS[name]
        for value in group.refused:
            rows.append((name, operator, value, field))
    return rows


def _native_stored(name: str, operator: str, value: Any) -> Dict[str, Any]:
    """``country equals US OR <the refused condition>``, as stored."""
    condition = GROUPS[name].build(value)["rules"][0]["rule"]["conditions"][0]
    return {
        "rules": [
            {
                "id": "r",
                "rule": {"operator": "or", "conditions": [US, condition]},
                "rollout_percentage": 100,
                "priority": 0,
            }
        ]
    }


@pytest.mark.regression
@pytest.mark.parametrize(
    "name, operator, value, field",
    _native_refused(),
    ids=[f"{r[0]} {_show(r[2])}" for r in _native_refused()],
)
def test_a_stored_native_rule_keeps_its_answer_with_a_healthy_sibling(
    name, operator, value, field
):
    """(B5) A value a save now refuses (B1 pins that), stored beside ``country
    equals US`` in an ``OR``, answers exactly as before, on a flag and an
    experiment: the US user is matched by the sibling, a DE user is not,
    unless the value abandons the whole ruleset, as it did. A guard, not a
    regression: it passes before this change too, and fails if a check of
    the write path reaches a read path."""
    stored = _native_stored(name, operator, value)
    kinds = GROUPS[name].kinds
    a = READ_IN_A[operator]
    us, de = {"country": "US", "a": a}, {"country": "DE", "a": a}
    abandoned = (operator, _show(value)) in _ABANDONED
    if FLAG in kinds:
        expected = (
            ERROR_OFF
            if abandoned
            else {
                "enabled": True,
                "reason": "targeting_rule",
                "rule_id": "r",
            }
        )
        assert flag_detailed(stored, us, rollout=0) == expected
        assert flag_detailed(stored, de, rollout=0) == (
            ERROR_OFF if abandoned else ROLLOUT_OFF
        )
    assert _experiment(stored, us) == (
        NOT_MATCHED if abandoned else (True, "Matched targeting rule: r")
    )
    assert _experiment(stored, de) == NOT_MATCHED


def _dashboard_stored(geo_value: Any) -> Dict[str, Any]:
    """``country equals US OR <the geo condition>``, in the dashboard shape."""
    return {
        "logical_operator": "OR",
        "groups": [{"conditions": [DASH_US]}, {"conditions": [dash_geo(geo_value)]}],
    }


SEGMENT_ID = "5e9a1c00-0000-4000-8000-000000001525"
_IN_SEGMENT = dashboard(
    {"attribute": "segment", "operator": "in_segment", "value": SEGMENT_ID}
)


def _segment_db(rules: Any) -> MagicMock:
    row = SimpleNamespace(
        id=uuid.UUID(SEGMENT_ID),
        kind="rules",
        status=SegmentStatus.ACTIVE,
        rules=copy.deepcopy(rules),
    )
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = [row]
    return db


#: The dashboard radii a save now refuses that the converter reads: the
#: first ten refused values of the corpus (the last three the converter
#: refuses itself, as before). Written out, not computed by asking the
#: converter, so a converter that came to refuse one fails below rather
#: than dropping the row.
STORED_DASH_RADII = DASH_RADII[1][:10]


@pytest.mark.regression
@pytest.mark.parametrize(
    "value", STORED_DASH_RADII, ids=[_show(v) for v in STORED_DASH_RADII]
)
def test_a_stored_dashboard_rule_keeps_its_answer_with_a_healthy_sibling(value):
    """(B5) A flag, an experiment and a segment with ``country equals US OR``
    a radius a save now refuses: the US user is matched, a DE user is not,
    and the segment is still evaluated, so a flag naming it is not turned
    off with reason ``error``. A guard: it passes before this change too."""
    stored = _dashboard_stored(value)
    us, de = {"country": "US", "a": SF}, {"country": "DE", "a": SF}
    matched = {"enabled": True, "reason": "targeting_rule", "rule_id": "dashboard"}
    assert flag_detailed(stored, us, rollout=0) == matched
    assert flag_detailed(stored, de, rollout=0) == ROLLOUT_OFF
    assert _experiment(stored, us) == (True, "Matched targeting rule: dashboard")
    assert _experiment(stored, de) == NOT_MATCHED

    validate_segment_rules(copy.deepcopy(stored))  # the read gate passes
    for context, member in ((us, True), (de, False)):
        memberships = segment_membership.resolve_segment_memberships(
            _segment_db(stored), USER, context, [SEGMENT_ID]
        )
        assert memberships.unavailable == frozenset()
        assert memberships.members == (
            frozenset({SEGMENT_ID}) if member else frozenset()
        )
    assert flag_detailed(_IN_SEGMENT, us, rollout=0, db=_segment_db(stored)) == matched
    assert (
        flag_detailed(_IN_SEGMENT, de, rollout=0, db=_segment_db(stored)) == ROLLOUT_OFF
    )


#: Each spelling of a comparison a save now refuses, as stored; ``within`` is
#: an unknown word, and was read as lte too.
STORED_COMPARISONS = {
    "object gt": at_sf(10, comparison="gt"),
    "object within": at_sf(10, comparison="within"),
    "list fifth item gt": [SF[0], SF[1], 10, "km", "gt"],
    "text fifth part gt": f"{SF_TEXT},10,km,gt",
}


@pytest.mark.regression
@pytest.mark.parametrize("name", sorted(STORED_COMPARISONS))
def test_a_stored_dashboard_comparison_still_means_within(name):
    """(G4) A stored dashboard geo rule with a comparison is evaluated as it
    was: within the 10 km radius, so the user at SF is matched and the one in
    Oakland, 13.4 km away, is not, on a flag, an experiment and a segment. A
    guard: it passes before this change too (G1 pins the refusal)."""
    stored = dashboard(dash_geo(STORED_COMPARISONS[name]))
    sf, oak = {"a": SF}, {"a": OAK}
    matched = {"enabled": True, "reason": "targeting_rule", "rule_id": "dashboard"}
    assert flag_detailed(stored, sf, rollout=0) == matched
    assert flag_detailed(stored, oak, rollout=0) == ROLLOUT_OFF
    assert _experiment(stored, sf) == (True, "Matched targeting rule: dashboard")
    assert _experiment(stored, oak) == NOT_MATCHED
    for context, member in ((sf, True), (oak, False)):
        memberships = segment_membership.resolve_segment_memberships(
            _segment_db(stored), USER, context, [SEGMENT_ID]
        )
        assert memberships.unavailable == frozenset()
        assert (SEGMENT_ID in memberships.members) is member


@pytest.mark.regression
def test_a_stored_unreadable_window_still_answers_error():
    """(T3) Stored windows the engine cannot read stay fail-closed: a native
    window given only as start and end turns a flag at rollout 100 off with
    reason ``error``; a dashboard one with ISO datetimes enrols nobody."""
    moment = {"a": "2024-01-10T12:00:00"}
    window = native(cond("time_window", {"start": "09:00", "end": "17:00"}))
    assert flag_detailed(window, moment, rollout=100) == ERROR_OFF
    iso = dashboard(
        {
            "attribute": "a",
            "operator": "time_window",
            "value": {"start": "2024-01-01T09:00:00", "end": "2024-01-31T17:00:00"},
        }
    )
    assert _experiment(iso, moment) == NOT_MATCHED


def test_the_witnesses_cover_every_bucket():
    """S4: percentage_bucket's witnesses reach bucket 0 and bucket 99."""
    assert sorted(_bucket(w["a"]) for w in BUCKET_WITNESSES) == list(range(100))
