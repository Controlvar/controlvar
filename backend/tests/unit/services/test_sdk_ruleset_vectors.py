"""
``tests/sdk-contract/ruleset-vectors.json``: current, pinned, and coherent.

* **Drift.** The file is regenerated in-process from the server's ruleset
  builder and flag evaluator and must match byte for byte. It reads the file
  by path and never shells out, so it runs the same in a checkout, in CI and in
  ``scripts/core_build.sh``'s copy (which has no ``.git``).
* **Anti-vacuity.** The case counts, and the number of ``must_local`` cases an
  SDK has to answer itself, are pinned here. A generator that stops marking
  cases ``must_local`` -- or loses a class of them -- fails, and so does an SDK
  that defers everything (its job asserts the same count).
* **One operator list.** The builder's local operators equal the list in the
  file and the launch set, literally.
* **Coherence.** A reference local evaluator, written from the ruleset
  contract and reading only the ``ruleset`` document in the file, answers
  exactly the ``must_local`` cases, and gives the server's answer on every one
  of them. That proves each ruleset entry carries what an SDK needs, and that
  the ``must_local`` marking and the contract agree.
"""

from __future__ import annotations

import difflib
import hashlib
import json
from typing import Any, Dict, Optional

import pytest

from backend.app.services import sdk_ruleset
from backend.scripts import generate_ruleset_vectors as generator

pytestmark = pytest.mark.unit

#: Pinned. A change here is a change to what the SDKs must answer locally:
#: regenerate the vectors and say why in the pull request.
EXPECTED_COUNTS = {"flags": 149, "cases": 2191, "must_local": 1294}

#: The launch local-operator set (engine names).
LAUNCH_LOCAL_OPERATORS = {
    "eq",
    "neq",
    "in",
    "not_in",
    "contains",
    "not_contains",
    "starts_with",
    "ends_with",
    "is_null",
    "is_not_null",
    "gt",
    "gte",
    "lt",
    "lte",
}


@pytest.fixture(scope="module")
def committed_text() -> str:
    assert generator.VECTORS_PATH.exists(), (
        "tests/sdk-contract/ruleset-vectors.json is missing; run "
        "`python -m backend.scripts.generate_ruleset_vectors`"
    )
    return generator.VECTORS_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def vectors(committed_text) -> Dict[str, Any]:
    return json.loads(committed_text)


# ---------------------------------------------------------------------------
# Drift and pins
# ---------------------------------------------------------------------------


def test_the_committed_vectors_are_what_the_server_generates(committed_text):
    generated = generator.render(generator.build_vectors())
    if generated != committed_text:
        diff = "".join(
            list(
                difflib.unified_diff(
                    committed_text.splitlines(keepends=True),
                    generated.splitlines(keepends=True),
                    "committed",
                    "generated",
                    n=0,
                )
            )[:40]
        )
        pytest.fail(
            "tests/sdk-contract/ruleset-vectors.json no longer matches what the "
            "server answers. If the change to the ruleset builder, the rules "
            "engine, the targeting adapter or the flag service is intended, run "
            "`python -m backend.scripts.generate_ruleset_vectors` and commit the "
            "file; the SDK jobs then check the SDKs against it.\n" + diff
        )


def test_the_counts_are_pinned(vectors):
    cases = vectors["cases"]
    recount = {
        "flags": len(vectors["stored_flags"]),
        "cases": len(cases),
        "must_local": sum(1 for case in cases if case["must_local"]),
    }
    assert recount == vectors["counts"]
    assert vectors["counts"] == EXPECTED_COUNTS


def test_the_local_operator_list_is_one_list(vectors):
    assert list(sdk_ruleset.LOCAL_OPERATORS) == vectors["local_operators"]
    assert set(vectors["local_operators"]) == LAUNCH_LOCAL_OPERATORS
    assert len(vectors["local_operators"]) == len(LAUNCH_LOCAL_OPERATORS)


def test_the_whitespace_list_is_the_union_of_both_languages(vectors):
    python_whitespace = {code for code in range(0x110000) if chr(code).isspace()}
    # Python's set, plus U+FEFF, which JavaScript's \s matches and Python's
    # str.isspace does not.
    assert set(vectors["whitespace_code_points"]) == python_whitespace | {0xFEFF}
    assert len(vectors["whitespace_code_points"]) == 30
    assert vectors["whitespace_code_points"] == list(sdk_ruleset.WHITESPACE_CODE_POINTS)


def test_the_header_names_the_contract(vectors):
    assert vectors["schema"] == sdk_ruleset.RULESET_SCHEMA == 1
    assert vectors["bucketing"] == sdk_ruleset.BUCKETING == "md5-mod100-v1"
    assert vectors["max_safe_integer"] == 2**53 - 1
    ruleset = vectors["ruleset"]
    content = {k: ruleset[k] for k in ("schema", "bucketing", "flags")}
    assert ruleset["version"] == sdk_ruleset.ruleset_version(content)


# ---------------------------------------------------------------------------
# Named cases: each class the corpus exists for, with the answer it must carry
# ---------------------------------------------------------------------------


def _find(vectors, flag: str, context: Any = generator.ABSENT, user: str = "user-1"):
    # By JSON form: 0, 0.0 and False are equal in Python but not in JSON, and
    # a context's key order matters.
    form = json.dumps(context)
    matches = [
        case
        for case in vectors["cases"]
        if case["flag"] == flag
        and case["user_id"] == user
        and json.dumps(case["context"]) == form
    ]
    assert len(matches) == 1, (flag, context, user, len(matches))
    return matches[0]


HUGE = 10**400


@pytest.mark.parametrize(
    "flag, context, user, must_local, expected",
    [
        # no short-circuit: the server evaluates the out-of-domain sibling and fails
        ("no-short-circuit-and", {"b": "yes", "a": HUGE}, "user-1", False, "error"),
        ("no-short-circuit-or", {"b": "yes", "a": HUGE}, "user-1", False, "error"),
        # ...but a rule after the first match is never evaluated
        (
            "later-rule-not-evaluated",
            {"b": "yes", "a": HUGE},
            "user-1",
            True,
            "targeting_rule",
        ),
        ("later-rule-not-evaluated", {"b": "no", "a": HUGE}, "user-1", False, "error"),
        # null rows
        ("not_in-str-0", {"a": None}, "user-1", True, "rollout"),
        ("neq-str-0", {"a": None}, "user-1", True, "targeting_rule"),
        ("presence-is_not_null", {"a": None}, "user-1", True, "rollout"),
        ("presence-is_null", {"a": []}, "user-1", True, "targeting_rule"),
        ("presence-is_null", {"a": {}}, "user-1", True, "targeting_rule"),
        ("presence-is_null", {"a": 0}, "user-1", True, "rollout"),
        ("presence-is_null", {"a": False}, "user-1", True, "rollout"),
        # whitespace: the server strips, the SDKs do not try
        ("presence-is_null", {"a": ""}, "user-1", True, "targeting_rule"),
        ("presence-is_null", {"a": "﻿"}, "user-1", False, "rollout"),
        ("presence-is_null", {"a": "\x1c"}, "user-1", False, "targeting_rule"),
        ("presence-is_null", {"a": "\x85"}, "user-1", False, "targeting_rule"),
        # types: bool before int, magnitude before conversion, no coercion
        ("eq-num-0", {"a": True}, "user-1", False, "rollout"),
        ("gt-0", {"a": HUGE}, "user-1", False, "error"),
        ("gt-0", {"a": 2**53}, "user-1", False, "targeting_rule"),
        ("gt-0", {"a": "10"}, "user-1", False, "targeting_rule"),
        ("eq-str-3", {"a": True}, "user-1", False, "targeting_rule"),
        ("gt-0", {"a": 10}, "user-1", True, "targeting_rule"),
        # prototype names are ordinary attributes
        ("proto-dunder", {"__proto__": "x"}, "user-1", True, "targeting_rule"),
        ("proto-is-null-constructor", {}, "user-1", True, "targeting_rule"),
        # ill-formed Unicode anywhere defers
        ("unicode", {"name": "été", "\ud800": "x"}, "user-1", False, "targeting_rule"),
        ("rollout-50", None, "user-\ud800", False, "error"),
        # default_rule is returned without evaluating its (remote) conditions
        ("default-rule", {"plan": "pro"}, "user-1", True, "targeting_rule"),
        # remote flags
        ("op-regex", {"email": "abc"}, "user-1", False, "targeting_rule"),
        ("legacy-user-ids", None, "user-1", False, "targeting_rule"),
    ],
)
def test_each_class_carries_its_answer(
    vectors, flag, context, user, must_local, expected
):
    case = _find(vectors, flag, context, user)
    assert case["must_local"] is must_local
    assert case["expected"]["reason"] == expected


@pytest.mark.parametrize("flag", ["segment-in", "segment-not-in", "segment-unknown"])
def test_segment_flags_are_remote(vectors, flag):
    """A flag that uses a segment is served remote, with no rules (#440).

    Membership lives on the server, so the ruleset carries neither the rules
    nor the segment id, and every case is one the SDK must ask about.
    """
    entry = next(e for e in vectors["ruleset"]["flags"] if e["key"] == flag)
    assert entry == {"active": True, "evaluation": "remote", "key": flag}
    cases = [case for case in vectors["cases"] if case["flag"] == flag]
    assert len(cases) == 3
    assert not any(case["must_local"] for case in cases)


@pytest.mark.parametrize(
    "flag, user, context, expected",
    [
        ("segment-in", "user-1", None, (True, "targeting_rule")),
        ("segment-in", "user-7", None, (False, "rollout")),
        # A list sent as "$segments" is not membership.
        (
            "segment-in",
            "user-7",
            {"$segments": [generator.VECTOR_SEGMENT]},
            (False, "rollout"),
        ),
        ("segment-not-in", "user-1", None, (False, "rollout")),
        ("segment-not-in", "user-7", None, (True, "targeting_rule")),
        # A segment whose membership is unknown: never "not a member".
        ("segment-unknown", "user-1", None, (False, "error")),
        ("segment-unknown", "user-7", None, (False, "error")),
    ],
)
def test_segment_cases_carry_the_server_answer(vectors, flag, user, context, expected):
    case = _find(vectors, flag, context, user)
    assert (case["expected"]["enabled"], case["expected"]["reason"]) == expected


def test_the_generator_stubs_the_resolver():
    """The generator answers membership from its table, never from a database."""
    flag = generator.stored_flag(
        {
            "key": "segment-probe",
            "status": "ACTIVE",
            "rollout_percentage": 0,
            "targeting_rules": generator.dashboard(
                generator.cond("segment", "in_segment", generator.VECTOR_SEGMENT)
            ),
        }
    )
    assert generator.server_answer(flag, "user-1", None) == {
        "enabled": True,
        "reason": "targeting_rule",
    }


def test_every_local_operator_and_reason_is_exercised_locally(vectors):
    entries = {entry["key"]: entry for entry in vectors["ruleset"]["flags"]}
    operators = set()
    reasons = set()
    enabled = set()
    for case in vectors["cases"]:
        if not case["must_local"]:
            continue
        reasons.add(case["expected"]["reason"])
        enabled.add(case["expected"]["enabled"])
        entry = entries[case["flag"]]
        for rule in entry.get("rules") or []:
            operators |= _operators(rule["match"])
    assert operators == LAUNCH_LOCAL_OPERATORS
    assert reasons == {"targeting_rule", "rollout", "inactive"}
    assert enabled == {True, False}


def _operators(group) -> set:
    found = {condition["operator"] for condition in group["conditions"]}
    for child in group["groups"]:
        found |= _operators(child)
    return found


# ---------------------------------------------------------------------------
# A reference local evaluator, from the contract alone
# ---------------------------------------------------------------------------

DEFER = "DEFER"


class Reference:
    """The ruleset contract, implemented from the document in the file."""

    def __init__(self, vectors: Dict[str, Any]) -> None:
        self.ruleset = vectors["ruleset"]
        self.flags = {entry["key"]: entry for entry in self.ruleset["flags"]}
        self.whitespace = frozenset(vectors["whitespace_code_points"])
        self.max_safe = vectors["max_safe_integer"]
        self.local = frozenset(vectors["local_operators"])

    # -- values ------------------------------------------------------------

    def number(self, value: Any) -> bool:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        return abs(value) <= self.max_safe  # inf and nan are not <= anything

    @staticmethod
    def well_formed(value: Any) -> bool:
        if isinstance(value, str):
            return not any(0xD800 <= ord(ch) <= 0xDFFF for ch in value)
        if isinstance(value, list):
            return all(Reference.well_formed(item) for item in value)
        if isinstance(value, dict):
            return all(
                Reference.well_formed(k) and Reference.well_formed(v)
                for k, v in value.items()
            )
        return True

    @staticmethod
    def flatten(context: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        flat: Dict[str, Any] = {}
        if not context:
            return flat

        def walk(node: Dict[str, Any], prefix: str) -> None:
            for key, value in node.items():
                full = prefix + key
                flat.setdefault(full, value)
                if isinstance(value, dict):
                    walk(value, full + ".")

        walk(context, "")
        for key, value in list(flat.items()):
            if "." in key or isinstance(value, dict):
                continue
            for prefix in ("user", "device", "app"):
                flat.setdefault(f"{prefix}.{key}", value)
        for key, value in list(flat.items()):
            head, dot, tail = key.partition(".")
            if dot and head in ("user", "device", "app") and tail and "." not in tail:
                flat.setdefault(tail, value)
        return flat

    # -- conditions --------------------------------------------------------

    def condition(self, condition: Dict[str, Any], flat: Dict[str, Any]):
        op = condition["operator"]
        rule = condition["value"]
        if op not in self.local:
            return DEFER
        if condition["attribute"] not in flat:
            return op == "is_null"
        value = flat[condition["attribute"]]
        if op in ("is_null", "is_not_null"):
            if value is None:
                empty = True
            elif isinstance(value, str):
                if any(ord(ch) in self.whitespace for ch in value):
                    return DEFER
                empty = value == ""
            elif isinstance(value, (list, dict)):
                empty = len(value) == 0
            else:
                empty = False
            return empty if op == "is_null" else not empty
        if value is None:
            return op == "neq"
        if op in ("contains", "not_contains", "starts_with", "ends_with"):
            if not isinstance(value, str):
                return DEFER
            return {
                "contains": rule in value,
                "not_contains": rule not in value,
                "starts_with": value.startswith(rule),
                "ends_with": value.endswith(rule),
            }[op]
        if op in ("eq", "neq", "in", "not_in"):
            items = rule if op in ("in", "not_in") else [rule]
            if isinstance(items[0], str):
                if not isinstance(value, str):
                    return DEFER
            elif not self.number(value):
                return DEFER
            hit = any(value == item for item in items)
            return hit if op in ("eq", "in") else not hit
        if not self.number(value):
            return DEFER
        return {
            "gt": value > rule,
            "gte": value >= rule,
            "lt": value < rule,
            "lte": value <= rule,
        }[op]

    def group(self, group: Dict[str, Any], flat: Dict[str, Any]):
        # Every condition and group is evaluated: no short-circuit.
        results = [self.condition(c, flat) for c in group["conditions"]]
        results += [self.group(g, flat) for g in group["groups"]]
        if any(result is DEFER for result in results):
            return DEFER
        if not results:
            return True
        if group["op"] == "and":
            return all(results)
        if group["op"] == "or":
            return any(results)
        return not all(results)

    # -- flags -------------------------------------------------------------

    @staticmethod
    def rollout(percentage: int, user_id: str, key: str) -> bool:
        if percentage <= 0:
            return False
        if percentage >= 100:
            return True
        digest = hashlib.md5(f"{user_id}:{key}".encode(), usedforsecurity=False)
        return int(digest.hexdigest(), 16) % 100 < percentage

    def evaluate(self, key: str, user_id: str, context: Optional[Dict[str, Any]]):
        if self.ruleset["schema"] != 1 or self.ruleset["bucketing"] != "md5-mod100-v1":
            return DEFER
        if not self.well_formed(user_id) or not self.well_formed(context):
            return DEFER
        entry = self.flags.get(key)
        if entry is None:
            return DEFER
        if not entry["active"]:
            return {"enabled": False, "reason": "inactive"}
        if entry["evaluation"] != "local":
            return DEFER
        flat = self.flatten(context)
        for rule in entry["rules"]:
            matched = self.group(rule["match"], flat)
            if matched is DEFER:
                return DEFER
            if matched:
                return {
                    "enabled": self.rollout(rule["rollout_percentage"], user_id, key),
                    "reason": "targeting_rule",
                }
        if entry["default_rule"] is not None:
            percentage = entry["default_rule"]["rollout_percentage"]
            return {
                "enabled": self.rollout(percentage, user_id, key),
                "reason": "targeting_rule",
            }
        return {
            "enabled": self.rollout(entry["rollout_percentage"], user_id, key),
            "reason": "rollout",
        }


def test_the_reference_evaluator_answers_exactly_the_must_local_cases(vectors):
    reference = Reference(vectors)
    wrong, missed, extra = [], [], []
    for case in vectors["cases"]:
        answer = reference.evaluate(case["flag"], case["user_id"], case["context"])
        if answer is DEFER:
            if case["must_local"]:
                missed.append(case["id"])
            continue
        if answer != case["expected"]:
            wrong.append((case["id"], answer, case["expected"]))
        if not case["must_local"]:
            extra.append(case["id"])
    assert not wrong, f"{len(wrong)} local answers differ from the server: {wrong[:5]}"
    assert not missed, f"{len(missed)} must_local cases deferred: {missed[:10]}"
    assert not extra, f"{len(extra)} cases answered but not must_local: {extra[:10]}"
