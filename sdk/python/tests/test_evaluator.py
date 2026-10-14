"""The local evaluator against ``tests/sdk-contract/ruleset-vectors.json``, which the server
generates from its own ruleset builder and flag evaluator (drift-checked in the backend suite).

The property: for every case the SDK either defers to the server or gives exactly the server's
``{enabled, reason}``; every ``must_local`` case is answered locally; and nothing else is. The
number of local answers is pinned, so an SDK that defers everything fails.

Standard library and pytest only; runs on Python 3.9.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import random
from pathlib import Path

import pytest

from experimentation import consistent_hash
from experimentation.evaluator import (
    BUCKETING,
    DEFER,
    LOCAL_OPERATORS,
    MAX_SAFE_INTEGER,
    RULESET_SCHEMA,
    WHITESPACE_CODE_POINTS,
    evaluate_locally,
    flag_bucket,
    flatten_context,
    index_ruleset,
    is_portable_number,
    is_well_formed,
    to_wire_context,
)

VECTORS_PATH = Path(__file__).resolve().parents[3] / "tests" / "sdk-contract" / "ruleset-vectors.json"
VECTORS = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))

#: Pinned. A change here is a change to what this SDK must answer locally: it moves only with a
#: regenerated vectors file (and the backend pin in test_sdk_ruleset_vectors.py).
EXPECTED_COUNTS = {"cases": 2191, "flags": 149, "must_local": 1294}

RULESET = index_ruleset(VECTORS["ruleset"])


def answer(case):
    # A case's context is what the server received; Python parses it exactly (10**400 stays
    # an int), so it is evaluated as parsed.
    return evaluate_locally(RULESET, case["flag"], case["user_id"], case["context"])


# ----------------------------------------------------------------------- pins


def test_the_counts_are_pinned():
    assert VECTORS["counts"] == EXPECTED_COUNTS
    assert len(VECTORS["cases"]) == EXPECTED_COUNTS["cases"]
    assert sum(1 for case in VECTORS["cases"] if case["must_local"]) == EXPECTED_COUNTS["must_local"]


def test_the_local_operator_list_is_the_same_list():
    assert list(LOCAL_OPERATORS) == VECTORS["local_operators"]


def test_the_whitespace_list_is_the_same_list():
    assert list(WHITESPACE_CODE_POINTS) == VECTORS["whitespace_code_points"]
    assert len(WHITESPACE_CODE_POINTS) == 30
    # Not str.isspace: that misses U+FEFF.
    assert not "﻿".isspace()
    assert 0xFEFF in WHITESPACE_CODE_POINTS


def test_the_schema_bucketing_and_number_limit_are_the_same():
    assert RULESET_SCHEMA == VECTORS["schema"]
    assert BUCKETING == VECTORS["bucketing"]
    assert MAX_SAFE_INTEGER == VECTORS["max_safe_integer"]
    assert RULESET is not None


# ----------------------------------------------------------------------- differential


RESULTS = [(case, answer(case)) for case in VECTORS["cases"]]


def test_no_local_answer_differs_from_the_server():
    wrong = [
        (case["id"], result, case["expected"])
        for case, result in RESULTS
        if result is not DEFER and result != case["expected"]
    ]
    assert wrong == []


def test_every_must_local_case_is_answered_locally():
    missed = [case["id"] for case, result in RESULTS if case["must_local"] and result is DEFER]
    assert missed == []


def test_nothing_outside_must_local_is_answered_locally():
    extra = [case["id"] for case, result in RESULTS if not case["must_local"] and result is not DEFER]
    assert extra == []


def test_the_number_of_local_answers_is_exactly_must_local():
    assert sum(1 for _, result in RESULTS if result is not DEFER) == EXPECTED_COUNTS["must_local"]


# ----------------------------------------------------------------------- named classes

HUGE = 10**400


@pytest.mark.parametrize(
    "flag, context",
    [
        # no short-circuit: the server evaluates the out-of-domain sibling and fails
        ("no-short-circuit-and", {"b": "yes", "a": HUGE}),
        ("no-short-circuit-or", {"b": "yes", "a": HUGE}),
        # numbers: bool before int, magnitude before any conversion, no coercion
        ("gt-0", {"a": HUGE}),
        ("gt-0", {"a": 2**53}),
        ("gt-0", {"a": float("inf")}),
        ("gt-0", {"a": float("nan")}),
        ("gt-0", {"a": "10"}),
        ("eq-num-0", {"a": True}),
        # whitespace the two languages disagree on
        ("presence-is_null", {"a": "﻿"}),
        ("presence-is_null", {"a": "\x1c"}),
        ("presence-is_null", {"a": "\x85"}),
        ("presence-is_null", {"a": " "}),
        # ill-formed Unicode in a key or a value
        ("unicode", {"name": "été", "\ud800": "x"}),
    ],
)
def test_defers(flag, context):
    assert evaluate_locally(RULESET, flag, "user-1", context) is DEFER


def test_bool_is_not_a_number_and_huge_ints_do_not_overflow():
    assert not is_portable_number(True)
    assert not is_portable_number(False)
    assert not is_portable_number(10**400)  # compared, never converted (no OverflowError)
    assert is_portable_number(MAX_SAFE_INTEGER)
    assert not is_portable_number(MAX_SAFE_INTEGER + 1)
    assert not is_portable_number(float("nan"))


def test_a_lone_surrogate_in_the_user_id_defers():
    assert evaluate_locally(RULESET, "rollout-50", "user-\ud800", None) is DEFER
    assert not is_well_formed({"ok": ["fine", "\udc00"]})
    assert is_well_formed({"emoji": "\U0001f600"})


def test_an_unknown_flag_key_defers():
    assert evaluate_locally(RULESET, "no-such-flag", "user-1", None) is DEFER


def test_a_ruleset_in_another_format_evaluates_nothing():
    assert index_ruleset(dict(VECTORS["ruleset"], schema=2)) is None
    assert index_ruleset(dict(VECTORS["ruleset"], bucketing="md5-mod100-v2")) is None
    assert index_ruleset(dict(VECTORS["ruleset"], schema=True)) is None


def test_prototype_names_are_ordinary_attributes():
    flat = flatten_context({"__proto__": "x", "constructor": 1})
    assert flat["__proto__"] == "x"
    assert flat["user.constructor"] == 1


# ----------------------------------------------------------------------- bucketing


def test_the_bucket_is_the_server_bucket():
    assert flag_bucket("user-123", "my-flag") == 79
    rng = random.Random(7)
    for i in range(2000):
        user = f"user-{rng.random()}-é-{i}"
        key = f"flag-{i % 13}"
        server = int(hashlib.md5(f"{user}:{key}".encode()).hexdigest(), 16) % 100  # noqa: S324
        assert flag_bucket(user, key) == server


def test_the_bucket_is_not_consistent_hash():
    assert int(consistent_hash("user-123", "my-flag") * 100) == 69
    assert flag_bucket("user-123", "my-flag") != 69


# ----------------------------------------------------------------------- the wire context


def test_the_wire_context_is_the_json_round_trip():
    def default(obj):
        if isinstance(obj, datetime.datetime):
            return obj.isoformat()
        raise TypeError

    stamp = datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc)
    assert to_wire_context({"t": (1, 2), 3: "x", "d": stamp}, default) == {
        "t": [1, 2],
        "3": "x",
        "d": "2026-01-02T03:04:05+00:00",
    }
    assert to_wire_context({}) is None
    assert to_wire_context(None) is None


def test_the_wire_context_defers_on_what_the_server_path_cannot_send():
    assert to_wire_context({"x": object()}) is DEFER
    cyclic = {}
    cyclic["self"] = cyclic
    assert to_wire_context(cyclic) is DEFER
