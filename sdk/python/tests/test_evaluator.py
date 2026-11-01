"""The local evaluator against ``tests/sdk-contract/ruleset-vectors.json``, which the server
generates from its own ruleset builder and flag evaluator (drift-checked in the backend suite).

The property: for every case the SDK either defers to the server or gives exactly the server's
``{enabled, reason}`` and ``value`` (a value case's ``expected_value``; ``enabled`` for a boolean
flag); every ``must_local`` case is answered locally; and nothing else is. The number of local
answers is pinned, so an SDK that defers everything fails.

Standard library and pytest only; runs on Python 3.9.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import random
from pathlib import Path

import pytest

from controlvar import consistent_hash
from controlvar.evaluator import (
    BUCKETING,
    DEFER,
    LOCAL_OPERATORS,
    MAX_SAFE_INTEGER,
    RULESET_SCHEMA,
    VALUE_TYPES,
    WHITESPACE_CODE_POINTS,
    evaluate_locally,
    flag_bucket,
    flatten_context,
    index_ruleset,
    is_portable_number,
    is_well_formed,
    reads_locally,
    to_wire_context,
)

VECTORS_PATH = Path(__file__).resolve().parents[3] / "tests" / "sdk-contract" / "ruleset-vectors.json"
VECTORS = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))

#: Pinned. A change here is a change to what this SDK must answer locally: it moves only with a
#: regenerated vectors file (and the backend pin in test_sdk_ruleset_vectors.py).
EXPECTED_COUNTS = {"cases": 2242, "flags": 161, "must_local": 1333}

RULESET = index_ruleset(VECTORS["ruleset"])


def answer(case):
    # A case's context is what the server received; Python parses it exactly (10**400 stays
    # an int), so it is evaluated as parsed.
    return evaluate_locally(RULESET, case["flag"], case["user_id"], case["context"])


def server_value(case):
    """The server's ``value`` for a case: ``expected_value``, or ``enabled`` for a boolean flag."""
    return case["expected_value"] if "expected_value" in case else case["expected"]["enabled"]


def same_json(a, b):
    """Equal as JSON, so a type change ("10" for 10, 1 for True, 10.0 for 10) is a difference."""
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


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
    wrong = []
    for case, result in RESULTS:
        if result is DEFER:
            continue
        local = {"enabled": result["enabled"], "reason": result["reason"], "value": result["value"]}
        server = dict(case["expected"], value=server_value(case))
        if not same_json(local, server):
            wrong.append((case["id"], local, server))
    assert wrong == []


def test_every_value_flags_must_local_case_is_answered_locally_with_the_servers_value_and_type():
    flags = {flag["key"]: flag for flag in VECTORS["ruleset"]["flags"]}
    value_cases = [(case, result) for case, result in RESULTS if "expected_value" in case and case["must_local"]]
    # Every value type the ruleset names, each as a local and an inactive/archived flag (F5's corpus).
    assert len(value_cases) == 39
    assert sorted({flags[case["flag"]].get("value_type") for case, _ in value_cases}) == sorted(VALUE_TYPES)
    wrong = [
        (case["id"], result)
        for case, result in value_cases
        if result is DEFER
        or result["value_type"] != flags[case["flag"]]["value_type"]
        or not same_json(result["value"], case["expected_value"])
    ]
    assert wrong == []


def test_a_boolean_flags_local_answer_is_its_enabled_typed_boolean():
    boolean = [(case, result) for case, result in RESULTS if "expected_value" not in case and result is not DEFER]
    assert len(boolean) == EXPECTED_COUNTS["must_local"] - 39
    assert all(result["value"] is result["enabled"] and result["value_type"] == "boolean" for _, result in boolean)


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


# ----------------------------------------------------------------------- flag values, and entries this SDK cannot read

ON_FOR_EVERYONE = {"active": True, "evaluation": "local", "rollout_percentage": 100, "rules": [], "default_rule": None}
PLAIN_ANSWER = {"enabled": True, "reason": "rollout", "value": True, "value_type": "boolean"}


def document_with(*entries):
    """A schema-1 document holding a boolean flag ``plain`` (on for everyone) and the given entries."""
    return {
        "schema": 1,
        "version": "v",
        "bucketing": "md5-mod100-v1",
        "flags": [dict(ON_FOR_EVERYONE, key="plain"), *entries],
    }


@pytest.mark.parametrize(
    "value_type, on, off",
    [("string", "on-copy", "off-copy"), ("number", 10, 2.5), ("json", {"color": "blue"}, ["fallback"])],
)
def test_a_value_flag_serves_its_on_value_when_on_and_its_off_value_when_off_or_turned_off(value_type, on, off):
    values = {"value_type": value_type, "on_value": on, "off_value": off}
    ruleset = index_ruleset(
        document_with(
            dict(ON_FOR_EVERYONE, key="on", **values),
            dict(ON_FOR_EVERYONE, key="outside", **dict(values, rollout_percentage=0)),
            {"key": "killed", "active": False, "value_type": value_type, "off_value": off},
        )
    )
    assert evaluate_locally(ruleset, "on", "user-1", None) == {
        "enabled": True,
        "reason": "rollout",
        "value": on,
        "value_type": value_type,
    }
    assert evaluate_locally(ruleset, "outside", "user-1", None) == {
        "enabled": False,
        "reason": "rollout",
        "value": off,
        "value_type": value_type,
    }
    assert evaluate_locally(ruleset, "killed", "user-1", None) == {
        "enabled": False,
        "reason": "inactive",
        "value": off,
        "value_type": value_type,
    }


def test_a_boolean_flag_with_no_value_type_serves_enabled_whatever_else_its_entry_carries():
    ruleset = index_ruleset(
        document_with({"key": "off", "active": False}, dict(ON_FOR_EVERYONE, key="extra", on_value="x"))
    )
    assert evaluate_locally(ruleset, "plain", "user-1", None) == PLAIN_ANSWER
    assert evaluate_locally(ruleset, "extra", "user-1", None) == PLAIN_ANSWER
    assert evaluate_locally(ruleset, "off", "user-1", None) == {
        "enabled": False,
        "reason": "inactive",
        "value": False,
        "value_type": "boolean",
    }


def test_an_integral_float_is_a_number_and_stays_a_float():
    """OPEN-11: ``10.0`` is a number (JSON gives Python a float); it is served as saved."""
    ruleset = index_ruleset(document_with(dict(ON_FOR_EVERYONE, key="f", value_type="number", on_value=10.0, off_value=0)))
    result = evaluate_locally(ruleset, "f", "user-1", None)
    assert result["value"] == 10.0 and isinstance(result["value"], float)
    assert reads_locally(ruleset.flags["f"])


def _mistyped(**fields):
    return dict(ON_FOR_EVERYONE, key="f", **fields)


UNREADABLE = [
    ("an evaluation it does not know", {"key": "f", "active": True, "evaluation": "local_v3", "rollout_percentage": 100}),
    ("no evaluation at all", {"key": "f", "active": True}),
    # The ruleset publishes value_type as string | number | json; these are outside that set.
    ("a value_type it does not know (date)", _mistyped(value_type="date", on_value="a", off_value="b")),
    ("value_type boolean, which a ruleset never names", _mistyped(value_type="boolean", on_value=True, off_value=False)),
    ("a value_type that is not a string", _mistyped(value_type=7, on_value="a", off_value="b")),
    ("a null value_type", _mistyped(value_type=None, on_value="a", off_value="b")),
    ("an inactive entry with a value_type it does not know", {"key": "f", "active": False, "value_type": "date", "off_value": "b"}),
    # Mistyped or missing values.
    ("a string flag whose on_value is a number", _mistyped(value_type="string", on_value=10, off_value="off")),
    ("a string flag with no on_value", _mistyped(value_type="string", off_value="off")),
    ("a string flag with no off_value", _mistyped(value_type="string", on_value="on")),
    ("a number flag whose off_value is a string", _mistyped(value_type="number", on_value=10, off_value="2.5")),
    ("a number flag whose on_value is a boolean", _mistyped(value_type="number", on_value=True, off_value=2.5)),
    ("a number flag whose on_value is 2**53", _mistyped(value_type="number", on_value=2**53, off_value=1)),
    ("a number flag whose off_value is -(2**53)", _mistyped(value_type="number", on_value=1, off_value=-(2**53))),
    ("a number flag whose on_value is 10**400", _mistyped(value_type="number", on_value=10**400, off_value=1)),
    ("a number flag whose on_value is infinite", _mistyped(value_type="number", on_value=float("inf"), off_value=1)),
    ("a number flag whose off_value is -infinite", _mistyped(value_type="number", on_value=1, off_value=float("-inf"))),
    ("a number flag whose on_value is NaN", _mistyped(value_type="number", on_value=float("nan"), off_value=1)),
    ("a json flag whose on_value is a string", _mistyped(value_type="json", on_value='{"a":1}', off_value=[])),
    ("a json flag whose off_value is null", _mistyped(value_type="json", on_value={"a": 1}, off_value=None)),
    ("an inactive string flag with no off_value", {"key": "f", "active": False, "value_type": "string"}),
    ("an inactive number flag whose off_value is a string", {"key": "f", "active": False, "value_type": "number", "off_value": "1"}),
]


@pytest.mark.parametrize("entry", [entry for _, entry in UNREADABLE], ids=[label for label, _ in UNREADABLE])
def test_an_entry_this_sdk_cannot_read_defers_that_flag_and_the_document_still_loads(entry):
    ruleset = index_ruleset(document_with(entry))
    assert ruleset is not None
    assert evaluate_locally(ruleset, "f", "user-1", None) is DEFER
    assert evaluate_locally(ruleset, "plain", "user-1", None) == PLAIN_ANSWER
    if entry["active"]:
        assert not reads_locally(ruleset.flags["f"])


def test_a_number_value_written_1e400_parses_to_infinity_and_defers():
    text = json.dumps(document_with(_mistyped(value_type="number", on_value=7, off_value=1)))
    parsed = json.loads(text.replace('"on_value": 7', '"on_value": 1e400'))
    ruleset = index_ruleset(parsed)
    assert ruleset.flags["f"]["on_value"] == float("inf")
    assert evaluate_locally(ruleset, "f", "user-1", None) is DEFER
    assert evaluate_locally(ruleset, "plain", "user-1", None) == PLAIN_ANSWER


def test_a_malformed_entry_of_a_known_shape_still_rejects_the_whole_document():
    assert index_ruleset(document_with({"key": "f", "active": True, "evaluation": "local"})) is None
    assert index_ruleset(document_with(_mistyped(rollout_percentage=50.5))) is None
    assert index_ruleset(document_with(_mistyped(rollout_percentage=50.0))) is None
    assert index_ruleset(document_with({"key": 7, "active": True, "evaluation": "remote"})) is None
    assert index_ruleset(document_with({"key": "f", "active": "yes"})) is None


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
