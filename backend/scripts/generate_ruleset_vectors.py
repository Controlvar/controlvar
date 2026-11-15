"""
Generate ``tests/sdk-contract/ruleset-vectors.json``: the differential corpus
for SDK-local flag evaluation (#226).

Every answer in the file comes from the server. For each corpus flag the
generator builds the ruleset entry with
:mod:`backend.app.services.sdk_ruleset` (what ``GET /api/v1/sdk/ruleset``
serves), and for each case it runs the flag service's own
``FeatureFlagService.evaluate_flag_detailed`` -- the body of
``/feature-flags/evaluate/{key}`` -- on the same stored flag, user and
context. Nothing in the file is written by hand.

What the SDKs check against it (in their own jobs):

* the ``ruleset`` document loads, and ``local_operators`` equals the SDK's own
  operator list;
* for **every** case the SDK either defers to the server or gives exactly
  ``expected`` (``{enabled, reason}``);
* for every case marked ``must_local`` it answers locally, with exactly
  ``expected``; ``counts.must_local`` is the number of such cases, so an SDK
  that defers everything fails;
* a case of a string, number or json flag (#1497) also carries
  ``expected_value``: the value ``/feature-flags/evaluate/{key}`` answers in
  ``value``. It is a key of its own, beside ``expected``, so an SDK that does
  not read values compares ``expected`` exactly as before; one that does
  gives exactly that value, compared as JSON, whenever it answers locally.
  The corpus holds every such type as an active local flag, an active remote
  flag, an inactive flag and an archived flag.

``must_local`` is the domain rule of the local evaluator, applied on the
server side: the flag is ``evaluation: "local"`` (or inactive), ``user_id``
and every context key and string are well-formed Unicode, and every condition
of every rule the server tries -- in priority order, up to and including the
first that matches -- is in its operator's domain for this context:

* an absent attribute: always (false, except ``is_null``: true);
* an explicit null: always (``is_null`` true, ``neq`` true, everything else
  false);
* ``is_null``/``is_not_null``: any value, except a string containing one of
  ``whitespace_code_points``;
* string operators, and ``eq``/``neq``/``in``/``not_in`` against strings: a
  string;
* numeric operators, and ``eq``/``neq``/``in``/``not_in`` against numbers: a
  number that is not a bool and whose magnitude is at most
  ``max_safe_integer``.

A condition out of its domain defers the whole evaluation, even when a sibling
has already decided the group: the server evaluates every condition of a rule
it tries, and one it cannot evaluate turns the answer into ``reason: error``.

Usage (from the repository root, venv active):

    python -m backend.scripts.generate_ruleset_vectors          # rewrite the file
    python -m backend.scripts.generate_ruleset_vectors --check  # exit 1 on drift

``backend/tests/unit/services/test_sdk_ruleset_vectors.py`` runs the same
comparison as ``--check`` in every backend job (it needs no database and no
``.git``, so it also runs in ``scripts/core_build.sh``'s copy). A change to
the rules engine, the targeting adapter or the flag service that moves any
answer fails it until the file is regenerated -- and then the SDK jobs fail
until the SDKs agree.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]

if __name__ == "__main__":  # pragma: no cover - run as a script
    # The evaluator needs settings; generate under the test settings, as the
    # drift test does. (Imported by the test suite, this changes nothing.)
    os.environ.setdefault("APP_ENV", "test")
    os.environ.setdefault("TESTING", "true")
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

from backend.app.core.flag_values import (
    BOOLEAN,
    JSON,
    MAX_SAFE_INTEGER,
    NUMBER,
    STRING,
    VALUE_TYPES,
    is_portable_number,
    served_value,
)
from backend.app.core.rules_engine import evaluate_rule
from backend.app.core.targeting_adapter import (
    expand_context,
    normalise_targeting_rules,
)
from backend.app.models import register_core_models
from backend.app.models.feature_flag import (
    FeatureFlag,
    FeatureFlagStatus,
)
from backend.app.schemas.targeting_rule import Condition, RuleGroup
from backend.app.services import feature_flag_service
from backend.app.services.feature_flag_service import (
    FeatureFlagService,
)
from backend.app.services.metrics_service import MetricsService
from backend.app.services.sdk_ruleset import (
    BUCKETING,
    EQUALITY_OPERATORS,
    LIST_OPERATORS,
    LOCAL_OPERATORS,
    NUMERIC_OPERATORS,
    PRESENCE_OPERATORS,
    RULESET_SCHEMA,
    STRING_OPERATORS,
    WHITESPACE_CODE_POINTS,
    build_flag_entry,
    build_ruleset,
    canonical_json,
)
from backend.app.services.segment_membership import SegmentMemberships

VECTORS_PATH = REPO_ROOT / "tests" / "sdk-contract" / "ruleset-vectors.json"

#: A context that does not carry the attribute under test.
ABSENT = {"other": "x"}

#: The user every targeting case evaluates for (flag rollout 0, rule 100, so
#: the answer is the match).
USER = "user-1"

_WHITESPACE = frozenset(WHITESPACE_CODE_POINTS)

# ---------------------------------------------------------------------------
# Context batteries
# ---------------------------------------------------------------------------

STRINGS = [
    "",
    "x",
    "abc",
    "ab",
    "b",
    "xabcx",
    "ABC",
    "a b",
    "日本",
    "é",
    "\U0001f600",
    "true",
    "5",
    "10",
    " x ",
    "﻿abc",
]
NUMBERS = [
    0,
    1,
    5,
    5.0,
    5.5,
    10,
    -1,
    -0.0,
    0.1,
    MAX_SAFE_INTEGER,
    -MAX_SAFE_INTEGER,
    MAX_SAFE_INTEGER + 1,
    MAX_SAFE_INTEGER + 2,
    -(MAX_SAFE_INTEGER + 2),
    10**400,
    1e308,
]
NUMERIC_STRINGS = ["10", " 10 ", "1_0", "inf", "1e3", "٣", "0x10"]
OTHER_TYPES = [None, True, False, [], {}, ["x"], [5], {"k": 1}]
PRESENCE_VALUES = (
    [None, "", "x", " x ", "x y", "\t\n", 0, 0.0, False, True, [], {}, ["x"]]
    + [{"k": None}, 10**400]
    + [chr(code) for code in WHITESPACE_CODE_POINTS]
)
USERS = [f"user-{index}" for index in range(20)] + [
    "ユーザー",
    "user:with:colons",
    "\U0001f642",
    " spaced ",
    "a" * 300,
]


def _string_battery() -> List[Any]:
    return STRINGS + [None, True, 5, [], {"k": 1}, " "]


def _number_battery() -> List[Any]:
    return NUMBERS + [None, True, False, "10", "x", "5", [], [5]]


# ---------------------------------------------------------------------------
# The corpus
# ---------------------------------------------------------------------------


class Corpus:
    """Flags (as stored) and the cases evaluated against them."""

    def __init__(self) -> None:
        self.flags: Dict[str, Dict[str, Any]] = {}
        self.cases: List[Tuple[str, str, Optional[Dict[str, Any]]]] = []

    def flag(
        self,
        key: str,
        targeting_rules: Any,
        rollout_percentage: int = 0,
        status: str = "ACTIVE",
        values: Optional[Tuple[str, Any, Any]] = None,
    ) -> str:
        """A stored flag. *values* is ``(value_type, on_value, off_value)`` for
        a string, number or json flag; a boolean flag stores none, as its row
        does, and its line in the file is the one it had before #1497."""
        assert key not in self.flags, key
        self.flags[key] = {
            "key": key,
            "status": status,
            "rollout_percentage": rollout_percentage,
            "targeting_rules": targeting_rules,
        }
        if values is not None:
            value_type, on_value, off_value = values
            assert value_type != BOOLEAN, key
            self.flags[key].update(
                value_type=value_type, on_value=on_value, off_value=off_value
            )
        return key

    def case(self, key: str, user_id: str, context: Optional[Dict[str, Any]]) -> None:
        self.cases.append((key, user_id, context))

    def values(self, key: str, attribute: str, values: List[Any]) -> None:
        """The attribute absent, then set to each value once (by JSON form)."""
        self.case(key, USER, ABSENT)
        seen = set()
        for value in values:
            form = json.dumps(value)
            if form not in seen:
                seen.add(form)
                self.case(key, USER, {attribute: value})


def dashboard(*conditions: Dict[str, Any], logical: str = "AND", rollout=None):
    rule: Dict[str, Any] = {
        "logical_operator": logical,
        "groups": [{"logical_operator": "AND", "conditions": list(conditions)}],
    }
    if rollout is not None:
        rule["rollout_percentage"] = rollout
    return rule


def cond(
    attribute: str, operator: str, value: Any = None, additional_value: Any = None
) -> Dict[str, Any]:
    out = {"attribute": attribute, "operator": operator, "value": value}
    if additional_value is not None:
        out["additional_value"] = additional_value
    return out


def native(*rules: Dict[str, Any], default_rule: Optional[Dict[str, Any]] = None):
    out: Dict[str, Any] = {"rules": list(rules)}
    if default_rule is not None:
        out["default_rule"] = default_rule
    return out


def nrule(rule_id: str, group: Dict[str, Any], rollout=100, priority=0):
    return {
        "id": rule_id,
        "rule": group,
        "rollout_percentage": rollout,
        "priority": priority,
    }


def group(op: str, *conditions: Dict[str, Any], groups=()) -> Dict[str, Any]:
    return {"operator": op, "conditions": list(conditions), "groups": list(groups)}


def build_corpus() -> Corpus:
    corpus = Corpus()
    _single_condition_flags(corpus)
    _presence_flags(corpus)
    _rule_value_domain_flags(corpus)
    _rollout_flags(corpus)
    _structure_flags(corpus)
    _context_shape_flags(corpus)
    _unicode_flags(corpus)
    _remote_flags(corpus)
    _segment_flags(corpus)
    _value_flags(corpus)
    return corpus


def _single_condition_flags(corpus: Corpus) -> None:
    """Each local operator against each in-domain rule value type."""
    string_ops = {
        "contains": ["b", "ab", ""],
        "not_contains": ["b", ""],
        "starts_with": ["ab", "x"],
        "ends_with": ["bc", "x"],
    }
    for op, rule_values in string_ops.items():
        for index, rule_value in enumerate(rule_values):
            # Hyphens, not the operator's underscore: gitleaks' generic-api-key
            # rule read two of the underscore keys as credentials (#1497). A
            # false positive in this generated file is fixed here, never with a
            # line pin in .gitleaksignore, which would mask whatever the next
            # regeneration puts on that line. core_build.sh's secrets step
            # scans tests/sdk-contract with no ignore file to keep it so.
            key = corpus.flag(
                f"str-{op.replace('_', '-')}-{index}",
                dashboard(cond("a", op, rule_value)),
            )
            corpus.values(key, "a", _string_battery())

    for op, dashboard_op in (("eq", "equals"), ("neq", "not_equals")):
        for index, rule_value in enumerate(["abc", "", "5", "true", " x "]):
            key = corpus.flag(
                f"{op}-str-{index}", dashboard(cond("a", dashboard_op, rule_value))
            )
            corpus.values(key, "a", _string_battery())
        for index, rule_value in enumerate([5, 5.0, 5.5, 0, -1, MAX_SAFE_INTEGER]):
            key = corpus.flag(
                f"{op}-num-{index}", dashboard(cond("a", dashboard_op, rule_value))
            )
            corpus.values(key, "a", _number_battery())

    for op in ("in", "not_in"):
        for index, rule_value in enumerate([["abc", "x"], "abc, x", "5, 10"]):
            key = corpus.flag(f"{op}-str-{index}", dashboard(cond("a", op, rule_value)))
            corpus.values(key, "a", _string_battery())
        for index, rule_value in enumerate([[5, 10], [5.5, -1]]):
            key = corpus.flag(f"{op}-num-{index}", dashboard(cond("a", op, rule_value)))
            corpus.values(key, "a", _number_battery())

    numeric_ops = {
        "greater_than": "gt",
        "greater_than_or_equal": "gte",
        "less_than": "lt",
        "less_than_or_equal": "lte",
    }
    for dashboard_op, short in numeric_ops.items():
        # "10" and "1e3" are coerced to numbers by the dashboard adapter.
        for index, rule_value in enumerate([5, 5.5, 0, "10", "1e3"]):
            key = corpus.flag(
                f"{short}-{index}", dashboard(cond("a", dashboard_op, rule_value))
            )
            corpus.values(key, "a", _number_battery() + NUMERIC_STRINGS)


def _presence_flags(corpus: Corpus) -> None:
    for op in ("is_null", "is_not_null"):
        key = corpus.flag(f"presence-{op}", dashboard(cond("a", op)))
        corpus.values(key, "a", PRESENCE_VALUES)
        corpus.case(key, USER, None)
        corpus.case(key, USER, {})


def _rule_value_domain_flags(corpus: Corpus) -> None:
    """Rule values outside their operator's domain: the flag is remote."""
    remote_values = [
        ("rv-eq-true", group("and", cond("a", "eq", True))),
        ("rv-eq-false", group("and", cond("a", "eq", False))),
        ("rv-eq-null", group("and", cond("a", "eq", None))),
        ("rv-neq-null", group("and", cond("a", "neq", None))),
        ("rv-eq-list", group("and", cond("a", "eq", ["x"]))),
        ("rv-eq-object", group("and", cond("a", "eq", {"k": 1}))),
        ("rv-eq-unsafe", group("and", cond("a", "eq", MAX_SAFE_INTEGER + 1))),
        ("rv-eq-huge", group("and", cond("a", "eq", 10**400))),
        ("rv-contains-int", group("and", cond("a", "contains", 5))),
        ("rv-contains-float", group("and", cond("a", "contains", 5.0))),
        ("rv-contains-bool", group("and", cond("a", "contains", True))),
        ("rv-contains-null", group("and", cond("a", "contains", None))),
        ("rv-starts-list", group("and", cond("a", "starts_with", ["x"]))),
        ("rv-gt-string", group("and", cond("a", "gt", "10"))),
        ("rv-gt-underscore", group("and", cond("a", "gt", "1_0"))),
        ("rv-gt-bool", group("and", cond("a", "gt", True))),
        ("rv-gt-unsafe", group("and", cond("a", "gt", -(MAX_SAFE_INTEGER + 2)))),
        ("rv-in-mixed", group("and", cond("a", "in", ["x", 5]))),
        ("rv-in-bool", group("and", cond("a", "in", [True]))),
        ("rv-in-null", group("and", cond("a", "in", [None]))),
        ("rv-in-empty", group("and", cond("a", "in", []))),
        ("rv-in-unsafe", group("and", cond("a", "in", [5, MAX_SAFE_INTEGER + 2]))),
        ("rv-not-in-nested", group("and", cond("a", "not_in", [["x"]]))),
    ]
    for key, rule_group in remote_values:
        corpus.flag(key, native(nrule("r1", rule_group)))
        corpus.values(key, "a", ["x", 10, True, None])


def _rollout_flags(corpus: Corpus) -> None:
    for percentage in (0, 1, 25, 50, 99, 100):
        key = corpus.flag(f"rollout-{percentage}", {}, rollout_percentage=percentage)
        for user in USERS:
            corpus.case(key, user, None)
    key = corpus.flag("drapeau-été", None, rollout_percentage=50)
    for user in USERS[:10]:
        corpus.case(key, user, {"country": "FR"})
    # A rule's own rollout, beside the flag's.
    key = corpus.flag(
        "rule-rollout",
        dashboard(cond("user.plan", "equals", "pro"), rollout=30),
        rollout_percentage=70,
    )
    for user in USERS[:12]:
        corpus.case(key, user, {"plan": "pro"})
        corpus.case(key, user, {"plan": "free"})


def _structure_flags(corpus: Corpus) -> None:
    targeted = dashboard(cond("country", "equals", "US"))
    for status in ("INACTIVE", "ARCHIVED"):
        key = corpus.flag(f"status-{status.lower()}", targeted, 100, status=status)
        corpus.case(key, USER, {"country": "US"})
        corpus.case(key, USER, None)
        corpus.case(key, "user-\ud800", {"country": "US"})

    key = corpus.flag(
        "priority",
        native(
            nrule("late", group("and", cond("plan", "eq", "pro")), 100, priority=5),
            nrule("early", group("and", cond("country", "eq", "US")), 0, priority=1),
        ),
        rollout_percentage=100,
    )
    for context in ({"plan": "pro", "country": "US"}, {"plan": "pro"}, {}):
        corpus.case(key, USER, context)

    key = corpus.flag(
        "priority-tie",
        native(
            nrule("first", group("and", cond("plan", "eq", "pro")), 0),
            nrule("second", group("and", cond("plan", "eq", "pro")), 100),
        ),
        rollout_percentage=100,
    )
    corpus.case(key, USER, {"plan": "pro"})
    corpus.case(key, USER, {"plan": "free"})

    # default_rule: returned without evaluating its conditions, so a remote
    # operator there does not make the flag remote.
    key = corpus.flag(
        "default-rule",
        native(
            nrule("pro", group("and", cond("plan", "eq", "pro"))),
            default_rule=nrule(
                "fallback",
                group("and", cond("v", "semantic_version", "1.0.0", "gte")),
                40,
            ),
        ),
    )
    for user in USERS[:12]:
        corpus.case(key, user, {"plan": "free"})
    corpus.case(key, USER, {"plan": "pro"})
    key = corpus.flag(
        "default-only",
        native(default_rule=nrule("everyone", group("and"), 60)),
    )
    for user in USERS[:10]:
        corpus.case(key, user, None)

    groups = {
        "group-not-one": group("not", cond("country", "eq", "US")),
        "group-not-many": group(
            "not", cond("country", "eq", "US"), cond("plan", "eq", "pro")
        ),
        "group-or": group("or", cond("country", "eq", "US"), cond("plan", "eq", "pro")),
        "group-nested": group(
            "and",
            groups=[
                group("or", cond("country", "eq", "US"), cond("country", "eq", "CA")),
                group("not", cond("plan", "eq", "free")),
            ],
        ),
        "group-empty": group("and"),
    }
    contexts = [
        {},
        {"country": "US"},
        {"country": "CA", "plan": "pro"},
        {"country": "US", "plan": "free"},
        {"country": "DE", "plan": "pro"},
        {"country": "US", "plan": "pro"},
    ]
    for key, rule_group in groups.items():
        corpus.flag(key, native(nrule("r", rule_group)))
        for context in contexts:
            corpus.case(key, USER, context)

    # No short-circuit: a condition the SDK cannot evaluate defers the whole
    # evaluation even when a sibling decided the group; the server answers
    # these with reason "error".
    huge = 10**400
    key = corpus.flag(
        "no-short-circuit-and",
        native(nrule("r", group("and", cond("b", "eq", "no"), cond("a", "gt", 5)))),
    )
    for context in (
        {"b": "yes", "a": huge},
        {"b": "yes", "a": 10},
        {"b": "no", "a": 10},
    ):
        corpus.case(key, USER, context)
    key = corpus.flag(
        "no-short-circuit-or",
        native(nrule("r", group("or", cond("b", "eq", "yes"), cond("a", "gt", 5)))),
    )
    for context in ({"b": "yes", "a": huge}, {"b": "yes", "a": 1}, {"b": "no", "a": 1}):
        corpus.case(key, USER, context)
    # ...but a rule after the first match is never evaluated.
    key = corpus.flag(
        "later-rule-not-evaluated",
        native(
            nrule("first", group("and", cond("b", "eq", "yes")), priority=0),
            nrule("second", group("and", cond("a", "gt", 5)), priority=1),
        ),
    )
    for context in (
        {"b": "yes", "a": huge},
        {"b": "no", "a": huge},
        {"b": "no", "a": 9},
    ):
        corpus.case(key, USER, context)

    # Rules the adapter cannot convert: the server falls back to the rollout.
    for key, rules in (
        ("unconvertible-operator", dashboard(cond("a", "no_such_operator", "x"))),
        ("unconvertible-attribute", dashboard(cond("bad attr!", "equals", "x"))),
        ("unconvertible-number", dashboard(cond("a", "greater_than", "1_0"))),
        ("empty-object", {}),
        ("empty-list", []),
        ("no-rules", None),
    ):
        corpus.flag(key, rules, rollout_percentage=50)
        for user in USERS[:6]:
            corpus.case(key, user, {"a": "x"})


def _context_shape_flags(corpus: Corpus) -> None:
    """expand_context: aliases, nesting, first-writer-wins, prototype names."""
    shapes = [
        ("alias-user", "user.country", "US", [{"country": "US"}, {"country": "DE"}]),
        (
            "alias-device",
            "device.os",
            "ios",
            [{"os": "ios"}, {"device": {"os": "ios"}}],
        ),
        (
            "alias-reverse",
            "plan",
            "pro",
            [{"user": {"plan": "pro"}}, {"user.plan": "pro"}],
        ),
        (
            "alias-explicit-wins",
            "user.country",
            "DE",
            [{"country": "US", "user": {"country": "DE"}}, {"country": "DE"}],
        ),
        (
            "nested",
            "app.version",
            "3.2.1",
            [{"app": {"version": "3.2.1"}}, {"app": "x"}],
        ),
        ("deep", "a.b.c", "x", [{"a": {"b": {"c": "x"}}}, {"a": {"b": "x"}}]),
        (
            "first-writer",
            "a.b",
            "first",
            [{"a.b": "first", "a": {"b": "second"}}, {"a": {"b": "first"}, "a.b": "x"}],
        ),
        ("no-deep-reverse", "tier", "x", [{"user": {"plan": {"tier": "x"}}}]),
        ("proto-constructor", "constructor", "x", [{}, {"constructor": "x"}]),
        ("proto-dunder", "__proto__", "x", [{}, {"__proto__": "x"}]),
        ("proto-tostring", "toString", "x", [{}, {"toString": "y"}]),
    ]
    for key, attribute, value, contexts in shapes:
        corpus.flag(key, dashboard(cond(attribute, "equals", value)))
        for context in contexts:
            corpus.case(key, USER, context)
    for attribute in ("constructor", "__proto__", "hasOwnProperty", "valueOf"):
        key = corpus.flag(
            f"proto-is-null-{attribute}", dashboard(cond(attribute, "is_null"))
        )
        corpus.case(key, USER, {})
        corpus.case(key, USER, {attribute: "x"})


def _unicode_flags(corpus: Corpus) -> None:
    """Ill-formed Unicode anywhere defers; well-formed non-ASCII is local."""
    key = corpus.flag(
        "unicode", dashboard(cond("name", "starts_with", "é")), rollout_percentage=50
    )
    for context in (
        {"name": "été"},
        {"name": "\U0001f600é"},
        {"name": "\ud800"},
        {"name": "é\udc00"},
        {"name": "été", "\ud800": "x"},
        {"name": "été", "list": ["\udfff"]},
        {"name": "été", "nested": {"k": "\ud83d"}},
    ):
        corpus.case(key, USER, context)
    for user in ("user-\ud800", "\udc00", "ok-\U0001f600"):
        corpus.case(key, user, {"name": "été"})
        corpus.case("rollout-50", user, None)


def _remote_flags(corpus: Corpus) -> None:
    """Operators the SDKs never evaluate, and the legacy list shape."""
    remote = [
        ("op-semver", dashboard(cond("v", "semver_gte", "1.2"))),
        ("op-regex", dashboard(cond("email", "regex", "^ab"))),
        ("op-array-contains", dashboard(cond("tags", "array_contains", ["a"]))),
        ("op-array-intersects", dashboard(cond("tags", "array_intersects", ["a"]))),
        ("op-geo", dashboard(cond("loc", "geo_within_radius", "40.7,-74.0,10"))),
        (
            "op-time-window",
            dashboard(cond("t", "time_window", {"start": "09:00", "end": "17:00"})),
        ),
        (
            "op-before",
            native(nrule("r", group("and", cond("d", "before", "2030-01-01")))),
        ),
        (
            "op-after",
            native(nrule("r", group("and", cond("d", "after", "2020-01-01")))),
        ),
        (
            "op-array-length",
            native(nrule("r", group("and", cond("tags", "array_length", 2)))),
        ),
        (
            "op-array-length-gt",
            native(nrule("r", group("and", cond("tags", "array_length", 2, "gt")))),
        ),
        (
            "op-array-length-between",
            native(
                nrule(
                    "r",
                    group(
                        "and",
                        cond("tags", "array_length", {"min": 1, "max": 2}, "between"),
                    ),
                )
            ),
        ),
        (
            "op-json-path",
            native(nrule("r", group("and", cond("doc", "json_path", "$.a")))),
        ),
        (
            "op-percentage-bucket",
            native(nrule("r", group("and", cond("uid", "percentage_bucket", 50)))),
        ),
        (
            "remote-in-later-rule",
            native(
                nrule("first", group("and", cond("plan", "eq", "pro"))),
                nrule(
                    "second",
                    group("and", cond("v", "semantic_version", "1.0.0", "gte")),
                ),
            ),
        ),
        ("legacy-user-ids", [{"type": "user_id", "user_ids": ["user-1"]}]),
        (
            "legacy-context",
            [{"type": "context", "conditions": [cond("plan", "eq", "pro")]}],
        ),
    ]
    for key, rules in remote:
        corpus.flag(key, rules, rollout_percentage=30)
        # Only contexts whose server answer does not depend on the clock or
        # the time zone: the attribute is absent.
        corpus.case(key, USER, None)
        corpus.case(key, "user-7", {"plan": "pro"})
    corpus.case("op-semver", USER, {"v": "1.2.99999999999999999999"})
    corpus.case("op-regex", USER, {"email": "abc"})
    corpus.case("op-array-contains", USER, {"tags": ["a", "b"]})
    # array_length with the attribute sent: the length of text too, and each
    # comparison, matched and not (#1159).
    corpus.case("op-array-length", USER, {"tags": "ab"})
    corpus.case("op-array-length", USER, {"tags": ["a", "b", "c"]})
    corpus.case("op-array-length-gt", USER, {"tags": ["a", "b", "c"]})
    corpus.case("op-array-length-gt", USER, {"tags": ["a", "b"]})
    corpus.case("op-array-length-between", USER, {"tags": ["a", "b"]})
    corpus.case("op-array-length-between", USER, {"tags": ["a", "b", "c"]})


#: The segment the segment flags name, and who is a member of it. Membership
#: lives on the server (#440), so the generator answers it from this table
#: through :func:`stub_memberships`, never from a database.
VECTOR_SEGMENT = "5e9a1c00-0000-4000-8000-000000000440"
VECTOR_SEGMENT_MEMBERS = {VECTOR_SEGMENT: frozenset({USER})}
#: A segment id the table does not know: membership is unavailable.
VECTOR_UNKNOWN_SEGMENT = "5e9a1c00-0000-4000-8000-00000000dead"


def _segment_flags(corpus: Corpus) -> None:
    """Flags that use a segment: always remote, the SDKs ask the server."""
    flags = [
        ("segment-in", dashboard(cond("segment", "in_segment", VECTOR_SEGMENT))),
        (
            "segment-not-in",
            dashboard(cond("segment", "not_in_segment", VECTOR_SEGMENT)),
        ),
        (
            "segment-unknown",
            dashboard(cond("segment", "in_segment", VECTOR_UNKNOWN_SEGMENT)),
        ),
    ]
    for key, rules in flags:
        corpus.flag(key, rules)
        # USER is a member; user-7 is not, whatever its context says.
        corpus.case(key, USER, None)
        corpus.case(key, "user-7", None)
        corpus.case(key, "user-7", {"$segments": [VECTOR_SEGMENT]})


def stub_memberships(db, user_id, context, segment_ids) -> SegmentMemberships:
    """The resolver's answer from :data:`VECTOR_SEGMENT_MEMBERS`.

    Stands in for ``resolve_segment_memberships`` explicitly: the generator
    has no database, and must not rely on what a resolver given none does.
    """
    asked = frozenset(segment_ids)
    known = asked & frozenset(VECTOR_SEGMENT_MEMBERS)
    return SegmentMemberships(
        members=frozenset(s for s in known if user_id in VECTOR_SEGMENT_MEMBERS[s]),
        evaluated=asked,
        unavailable=asked - known,
    )


#: The two values of each value type the corpus serves (#1497): plain words
#: and small numbers, so nothing in the file reads as a credential to the
#: leak scan (PE-8). An integer and a fraction; an object and an array.
VALUE_PAIRS: Dict[str, Tuple[Any, Any]] = {
    STRING: ("on-copy", "off-copy"),
    NUMBER: (10, 2.5),
    JSON: ({"color": "blue"}, ["fallback"]),
}

#: How a value flag reaches an SDK, each with its own flag per value type.
VALUE_CLASSES = ("local", "remote", "inactive", "archived")


def value_flag_key(value_type: str, value_class: str) -> str:
    return f"value-{value_type}-{value_class}"


def _value_flags(corpus: Corpus) -> None:
    """Every value type but boolean, as each class of ruleset entry.

    * ``local``: a rule (``plan`` equals ``pro``, 100%) and a 50% rollout, so
      one flag serves its on value by the rule and by the rollout and its off
      value outside the rollout; a ``plan`` that is not a string is outside
      the local domain, and the SDK asks the server.
    * ``remote``: a semantic-version rule, which only the server evaluates.
    * ``inactive`` and ``archived``: the off value for everyone.
    """
    local_rules = dashboard(cond("plan", "equals", "pro"))
    remote_rules = dashboard(cond("v", "semver_gte", "1.2"))
    for value_type in VALUE_TYPES:
        if value_type == BOOLEAN:
            continue
        values = (value_type, *VALUE_PAIRS[value_type])
        key = corpus.flag(
            value_flag_key(value_type, "local"),
            local_rules,
            rollout_percentage=50,
            values=values,
        )
        corpus.case(key, USER, {"plan": "pro"})
        for user in USERS[:8]:
            corpus.case(key, user, {"plan": "free"})
        corpus.case(key, USER, {"plan": 5})
        key = corpus.flag(
            value_flag_key(value_type, "remote"),
            remote_rules,
            rollout_percentage=30,
            values=values,
        )
        corpus.case(key, USER, {"v": "1.3.0"})
        corpus.case(key, USER, None)
        corpus.case(key, "user-7", None)
        for status in ("INACTIVE", "ARCHIVED"):
            key = corpus.flag(
                value_flag_key(value_type, status.lower()),
                local_rules,
                rollout_percentage=100,
                status=status,
                values=values,
            )
            corpus.case(key, USER, {"plan": "pro"})
            corpus.case(key, USER, None)


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _roundtrip(value: Any) -> Any:
    """What the file (and the server's JSON parser) turns *value* into."""
    return json.loads(json.dumps(value))


def stored_flag(spec: Dict[str, Any]) -> FeatureFlag:
    """A transient ``FeatureFlag`` carrying exactly what the database would.

    A boolean flag's ``value_type`` is ``boolean`` and its two value columns
    NULL, as the ``check_flag_values`` constraint requires.
    """
    return FeatureFlag(
        key=spec["key"],
        name=spec["key"],
        status=FeatureFlagStatus[spec["status"]],
        rollout_percentage=spec["rollout_percentage"],
        targeting_rules=_roundtrip(spec["targeting_rules"]),
        value_type=spec.get("value_type", BOOLEAN),
        on_value=_roundtrip(spec.get("on_value")),
        off_value=_roundtrip(spec.get("off_value")),
    )


def server_answer(
    flag: FeatureFlag, user_id: str, context: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """The flag service's answer, with metrics recording switched off."""
    with (
        mock.patch.object(MetricsService, "record_flag_evaluation"),
        mock.patch.object(MetricsService, "log_error"),
        mock.patch.object(
            feature_flag_service, "resolve_segment_memberships", stub_memberships
        ),
    ):
        result = FeatureFlagService(db=None).evaluate_flag_detailed(
            flag, user_id, context
        )
    return {"enabled": bool(result["enabled"]), "reason": result["reason"]}


def server_value(flag: FeatureFlag, answer: Dict[str, Any]) -> Any:
    """The ``value`` ``/feature-flags/evaluate/{key}`` answers with *answer*.

    The route's own ``served_value``. Every corpus value is within the value
    rules, so every value flag here has one to serve.
    """
    servable, value = served_value(flag, answer["enabled"])
    assert servable, flag.key
    return value


def _well_formed(text: str) -> bool:
    return not any(0xD800 <= ord(ch) <= 0xDFFF for ch in text)


def _strings_well_formed(value: Any) -> bool:
    if isinstance(value, str):
        return _well_formed(value)
    if isinstance(value, list):
        return all(_strings_well_formed(item) for item in value)
    if isinstance(value, dict):
        return all(
            _well_formed(key) and _strings_well_formed(item)
            for key, item in value.items()
        )
    return True


def condition_in_domain(condition: Condition, flat: Dict[str, Any]) -> bool:
    """Whether an SDK can answer *condition* for this flattened context."""
    operator = (
        condition.operator.value
        if hasattr(condition.operator, "value")
        else condition.operator
    )
    if condition.attribute not in flat:
        return True
    value = flat[condition.attribute]
    if operator in PRESENCE_OPERATORS:
        if isinstance(value, str):
            return not any(ord(ch) in _WHITESPACE for ch in value)
        return True
    if value is None:
        return True
    rule_value = condition.value
    if operator in STRING_OPERATORS:
        return isinstance(value, str)
    if operator in EQUALITY_OPERATORS:
        if isinstance(rule_value, str):
            return isinstance(value, str)
        return is_portable_number(value)
    if operator in LIST_OPERATORS:
        if isinstance(rule_value[0], str):
            return isinstance(value, str)
        return is_portable_number(value)
    if operator in NUMERIC_OPERATORS:
        return is_portable_number(value)
    raise AssertionError(f"not a local operator: {operator}")


def group_in_domain(rule_group: RuleGroup, flat: Dict[str, Any]) -> bool:
    return all(condition_in_domain(c, flat) for c in rule_group.conditions) and all(
        group_in_domain(g, flat) for g in rule_group.groups or []
    )


def must_answer_locally(
    spec: Dict[str, Any],
    entry: Dict[str, Any],
    user_id: str,
    context: Optional[Dict[str, Any]],
) -> bool:
    """The local evaluator's domain, applied to one case (module docstring)."""
    if not _well_formed(user_id) or not _strings_well_formed(context):
        return False
    if not entry["active"]:
        return True
    if entry["evaluation"] != "local":
        return False
    stored = _roundtrip(spec["targeting_rules"])
    rules = (
        normalise_targeting_rules(stored, owner=f"flag:{spec['key']}")
        if stored
        else None
    )
    if rules is None:
        return True
    flat = expand_context(context or {})
    for rule in sorted(rules.rules, key=lambda r: r.priority):
        if not group_in_domain(rule.rule, flat):
            return False
        if evaluate_rule(rule, flat):
            return True
    return True


def iter_cases(corpus: Corpus) -> Iterator[Dict[str, Any]]:
    counters: Dict[str, int] = {}
    flags = {key: stored_flag(spec) for key, spec in corpus.flags.items()}
    entries = {key: build_flag_entry(flag) for key, flag in flags.items()}
    for key, user_id, context in corpus.cases:
        context = _roundtrip(context)
        counters[key] = counters.get(key, 0) + 1
        case: Dict[str, Any] = {
            "id": f"{key}#{counters[key]}",
            "flag": key,
            "user_id": user_id,
            "context": context,
            "expected": server_answer(flags[key], user_id, context),
        }
        if "value_type" in corpus.flags[key]:
            case["expected_value"] = server_value(flags[key], case["expected"])
        case["must_local"] = must_answer_locally(
            corpus.flags[key], entries[key], user_id, context
        )
        yield case


@contextlib.contextmanager
def _quiet_logs() -> Iterator[None]:
    """Silence the evaluator's logging: the corpus provokes its error paths."""
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        logging.disable(previous)


def build_vectors() -> Dict[str, Any]:
    """The whole vectors document."""
    register_core_models()
    corpus = build_corpus()
    with _quiet_logs():
        ruleset = build_ruleset(stored_flag(spec) for spec in corpus.flags.values())
        cases = list(iter_cases(corpus))
    return {
        "about": (
            "Differential corpus for SDK-local flag evaluation (#226). Generated by "
            "backend/scripts/generate_ruleset_vectors.py from the server's own ruleset "
            "builder and flag evaluator; do not edit by hand. For every case an SDK "
            "either defers to the server or returns exactly `expected`; for every "
            "case with `must_local` it answers locally with exactly `expected`."
        ),
        "schema": RULESET_SCHEMA,
        "bucketing": BUCKETING,
        "local_operators": list(LOCAL_OPERATORS),
        "max_safe_integer": MAX_SAFE_INTEGER,
        "whitespace_code_points": list(WHITESPACE_CODE_POINTS),
        "counts": {
            "flags": len(corpus.flags),
            "cases": len(cases),
            "must_local": sum(1 for case in cases if case["must_local"]),
        },
        "ruleset": ruleset,
        "stored_flags": [corpus.flags[key] for key in sorted(corpus.flags)],
        "cases": cases,
    }


# ---------------------------------------------------------------------------
# Serialisation: one flag or case per line, so a drift is a readable diff
# ---------------------------------------------------------------------------

_LINE_KEYS = ("stored_flags", "cases")


def ordered_json(value: Any) -> str:
    """Compact, ASCII-only, and in insertion order.

    Cases keep their contexts' key order: the server flattens a context
    first-writer-wins, so ``{"a.b": 1, "a": {"b": 2}}`` and its reverse give
    ``a.b`` different values. (A JavaScript object moves integer-like keys to
    the front, so no context here relies on the order of such keys.)
    """
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def render(vectors: Dict[str, Any]) -> str:
    lines = ["{"]
    keys = list(vectors)
    for index, key in enumerate(keys):
        comma = "," if index < len(keys) - 1 else ""
        value = vectors[key]
        if key in _LINE_KEYS or key == "ruleset":
            items = value["flags"] if key == "ruleset" else value
            lines.append(f"{json.dumps(key)}: " + ("{" if key == "ruleset" else "["))
            if key == "ruleset":
                for field in ("schema", "version", "bucketing"):
                    lines.append(
                        f"  {json.dumps(field)}: {canonical_json(value[field])},"
                    )
                lines.append('  "flags": [')
            encode = canonical_json if key == "ruleset" else ordered_json
            for position, item in enumerate(items):
                tail = "," if position < len(items) - 1 else ""
                lines.append(f"  {encode(item)}{tail}")
            lines.append("  ]" + "}" + comma if key == "ruleset" else "]" + comma)
        else:
            lines.append(f"{json.dumps(key)}: {canonical_json(value)}{comma}")
    lines.append("}")
    return "\n".join(lines) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare with the committed file and exit 1 if it differs",
    )
    parser.add_argument("--output", type=Path, default=VECTORS_PATH)
    args = parser.parse_args(argv)

    text = render(build_vectors())
    if args.check:
        current = (
            args.output.read_text(encoding="utf-8") if args.output.exists() else ""
        )
        if current != text:
            print(
                f"{args.output} is out of date: run "
                "`python -m backend.scripts.generate_ruleset_vectors`",
                file=sys.stderr,
            )
            return 1
        print(f"{args.output} is current")
        return 0
    args.output.write_text(text, encoding="utf-8")
    counts = json.loads(text)["counts"]
    print(f"wrote {args.output}: {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
