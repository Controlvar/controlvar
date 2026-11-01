"""The value rules of a string, number or json flag (#1497).

``backend.app.core.flag_values`` is the one module the create schema, the
update check and the evaluate route read; these pin its answers. The routes
and the database's ``check_flag_values`` are pinned against the same rules in
``backend/tests/integration/api/test_flag_values_api.py``.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from backend.app.core import flag_values
from backend.app.core.flag_values import (
    MAX_SAFE_INTEGER,
    MAX_VALUE_BYTES,
    MAX_VALUE_DEPTH,
    is_portable_number,
    read_values,
    served_value,
    stored_values,
    value_error,
    values_equal,
    write_errors,
)
from backend.app.core.log_once import EVALUATION_NOTES
from backend.app.services import sdk_ruleset

pytestmark = [pytest.mark.unit]


def _nested(depth):
    """An array nested ``depth`` levels deep: ``[[...[1]...]]``."""
    value = [1]
    for _ in range(depth - 1):
        value = [value]
    return value


def _sized(size):
    """A JSON object of exactly ``size`` bytes of compact UTF-8 JSON."""
    # {"a":"<n chars>"} is 8 bytes plus n.
    return {"a": "x" * (size - 8)}


def _refusal(field, value_type, value):
    error = value_error(field, value_type, value)
    return None if error is None else (error.type, error.message())


# --- one definition of a portable number (EMF-6) -----------------------------


def test_the_ruleset_and_the_value_rules_share_one_portable_number():
    """The ruleset reads the same function object, so they cannot drift."""
    assert sdk_ruleset.is_portable_number is is_portable_number
    assert MAX_SAFE_INTEGER == 2**53 - 1


@pytest.mark.parametrize(
    "value, portable",
    [
        (0, True),
        (MAX_SAFE_INTEGER, True),
        (-MAX_SAFE_INTEGER, True),
        (2.5, True),
        (MAX_SAFE_INTEGER + 1, False),
        (-(MAX_SAFE_INTEGER + 1), False),
        (10**400, False),
        (float("inf"), False),
        (float("-inf"), False),
        (float("nan"), False),
        (True, False),
        ("1", False),
    ],
    ids=repr,
)
def test_is_portable_number(value, portable):
    assert is_portable_number(value) is portable


# --- one value ----------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize(
    "value_type, value, refusal",
    [
        # The type comes first.
        (
            "number",
            "ten",
            (
                "value_type_mismatch",
                "on_value must be a number, because value_type is number",
            ),
        ),
        (
            "number",
            True,
            (
                "value_type_mismatch",
                "on_value must be a number, because value_type is number",
            ),
        ),
        (
            "string",
            3,
            (
                "value_type_mismatch",
                "on_value must be a string, because value_type is string",
            ),
        ),
        (
            "json",
            "blue",
            (
                "value_type_mismatch",
                "on_value must be a JSON object or array, because value_type is json",
            ),
        ),
        (
            "json",
            7,
            (
                "value_type_mismatch",
                "on_value must be a JSON object or array, because value_type is json",
            ),
        ),
        # Depth, before anything that recurses.
        (
            "json",
            _nested(MAX_VALUE_DEPTH + 1),
            ("value_too_deep", "on_value nested more than 10 levels deep"),
        ),
        # Text PostgreSQL refuses.
        (
            "string",
            "a\x00b",
            (
                "unstorable_text",
                "on_value must not contain NUL or unpaired surrogate characters",
            ),
        ),
        (
            "string",
            "\ud800",
            (
                "unstorable_text",
                "on_value must not contain NUL or unpaired surrogate characters",
            ),
        ),
        (
            "json",
            {"\x00": 1},
            (
                "unstorable_text",
                "on_value must not contain NUL or unpaired surrogate characters",
            ),
        ),
        # Numbers.
        (
            "number",
            float("nan"),
            (
                "value_out_of_range",
                "on_value: a number is outside -9007199254740991 to 9007199254740991",
            ),
        ),
        (
            "number",
            float("inf"),
            (
                "value_out_of_range",
                "on_value: a number is outside -9007199254740991 to 9007199254740991",
            ),
        ),
        (
            "number",
            2**53,
            (
                "value_out_of_range",
                "on_value: a number is outside -9007199254740991 to 9007199254740991",
            ),
        ),
        (
            "json",
            {"a": [1, 10**400]},
            (
                "value_out_of_range",
                "on_value: a number is outside -9007199254740991 to 9007199254740991",
            ),
        ),
        # Size, last.
        (
            "json",
            _sized(MAX_VALUE_BYTES + 1),
            ("value_too_large", "on_value larger than 8192 bytes"),
        ),
        (
            "string",
            "é" * 4096,
            ("value_too_large", "on_value larger than 8192 bytes"),
        ),
    ],
    ids=lambda v: repr(v)[:40],
)
def test_a_value_outside_the_rules_is_refused(value_type, value, refusal):
    assert _refusal("on_value", value_type, value) == refusal


@pytest.mark.parametrize(
    "value_type, value",
    [
        ("string", ""),
        ("string", "Buy now"),
        ("number", 0),
        ("number", -2.5),
        ("number", MAX_SAFE_INTEGER),
        ("number", -MAX_SAFE_INTEGER),
        ("json", {}),
        ("json", []),
        ("json", {"nested": {"ok": [True, None, "x"]}}),
        ("json", _nested(MAX_VALUE_DEPTH)),
        ("json", _sized(MAX_VALUE_BYTES)),
        ("string", "x" * (MAX_VALUE_BYTES - 2)),
    ],
    ids=lambda v: repr(v)[:40],
)
def test_a_value_at_each_limit_is_accepted(value_type, value):
    assert value_error("on_value", value_type, value) is None


def test_the_size_is_the_compact_utf8_json():
    """A string of 8,190 one-byte characters is 8,192 bytes with its quotes."""
    assert value_error("off_value", "string", "x" * (MAX_VALUE_BYTES - 2)) is None
    assert value_error("off_value", "string", "x" * (MAX_VALUE_BYTES - 1)) is not None


@pytest.mark.regression
@pytest.mark.parametrize(
    "field, value, accepted",
    [
        ("on_value", True, True),
        ("off_value", False, True),
        ("on_value", False, False),
        ("off_value", True, False),
        ("on_value", 1, False),
        ("off_value", 0, False),
        ("on_value", "true", False),
        ("off_value", "blue", False),
    ],
    ids=repr,
)
def test_a_boolean_flag_takes_true_and_false_and_nothing_else(field, value, accepted):
    """D40: a boolean flag serves false when off; ``1`` is not ``true``."""
    refusal = _refusal(field, "boolean", value)
    if accepted:
        assert refusal is None
    else:
        assert refusal == (
            "boolean_values",
            "a boolean flag's on_value is true and its off_value is false; to "
            "serve another value, create a string, number or json flag",
        )


def test_no_message_repeats_the_value():
    secret = "s3cr3t-value-" + "z" * MAX_VALUE_BYTES
    for value_type in ("number", "json", "string", "boolean"):
        error = value_error("on_value", value_type, secret)
        assert error is not None
        assert "s3cr3t" not in error.message()


# --- JSONB equality (PF-5) ----------------------------------------------------

#: The table the integration test also runs through PostgreSQL's own ``<>``.
JSONB_EQUALITY = [
    (3, 3.0, True),
    (3, 4, False),
    (0, -0.0, True),
    ({"a": 1}, {"a": 1.0}, True),
    ([1, 2], [2, 1], False),
    ([1, 2], [1, 2], True),
    ({"a": True}, {"a": 1}, False),
    ([True], [1], False),
    ({"a": None}, {}, False),
    ({"a": 1, "b": 2}, {"b": 2, "a": 1}, True),
    ("a", "A", False),
    ("Buy now", "Buy now", True),
    ([], {}, False),
    ({"a": [1, {"b": 2.50}]}, {"a": [1, {"b": 2.5}]}, True),
]


@pytest.mark.regression
@pytest.mark.parametrize("a, b, equal", JSONB_EQUALITY, ids=repr)
def test_values_equal_is_jsonb_equality(a, b, equal):
    assert values_equal(a, b) is equal
    assert values_equal(b, a) is equal


def test_python_equality_and_canonical_text_are_not_it():
    """Why the module has its own comparison: ``True == 1`` in Python, and
    ``"3" != "3.0"`` as text."""
    assert {"a": True} == {"a": 1}
    assert not values_equal({"a": True}, {"a": 1})
    assert sdk_ruleset.canonical_json(3) != sdk_ruleset.canonical_json(3.0)
    assert values_equal(3, 3.0)


# --- a write's values ---------------------------------------------------------


def _pairs(errors):
    return [(field, error.type) for field, error in errors]


@pytest.mark.regression
def test_a_create_needs_both_values():
    assert _pairs(write_errors("number", {}, None)) == [
        ("on_value", "value_required"),
        ("off_value", "value_required"),
    ]
    assert _pairs(write_errors("number", {"on_value": 1}, None)) == [
        ("off_value", "value_required")
    ]


@pytest.mark.regression
def test_an_update_checks_the_pair_as_it_will_be_stored():
    """EMF-5: on "a" / off "b" stored; sending on "b" makes them equal."""
    stored = {"on_value": "a", "off_value": "b"}
    errors = write_errors("string", {"on_value": "b"}, stored)
    assert [(f, e.type, e.message()) for f, e in errors] == [
        (
            "on_value",
            "values_equal",
            "on_value equals off_value; the two values must differ, or turning the "
            "flag off changes nothing",
        )
    ]
    assert write_errors("string", {"on_value": "c"}, stored) == []
    assert _pairs(write_errors("string", {"off_value": "a"}, stored)) == [
        ("off_value", "values_equal")
    ]
    # Both sent: swapping them is fine; making them equal is reported at off.
    assert write_errors("string", {"on_value": "b", "off_value": "a"}, stored) == []
    assert _pairs(
        write_errors("string", {"on_value": "z", "off_value": "z"}, stored)
    ) == [("off_value", "values_equal")]


def test_equality_is_not_checked_over_a_refused_value():
    assert _pairs(write_errors("number", {"on_value": "x", "off_value": 1}, None)) == [
        ("on_value", "value_type_mismatch")
    ]


@pytest.mark.regression
def test_a_boolean_flag_get_body_is_accepted():
    """PF-3: ``on_value: true, off_value: false`` sent back is no error."""
    assert write_errors("boolean", {"on_value": True, "off_value": False}, None) == []
    assert write_errors("boolean", {}, None) == []
    assert _pairs(write_errors("boolean", {"off_value": True}, None)) == [
        ("off_value", "boolean_values")
    ]


def test_storage_and_reading_of_a_boolean_flag():
    assert stored_values("boolean", True, False) == (None, None)
    assert stored_values("string", "a", "b") == ("a", "b")
    assert read_values("boolean", None, None) == (True, False)
    assert read_values(None, None, None) == (True, False)
    assert read_values("json", {"a": 1}, []) == ({"a": 1}, [])


# --- serving a stored row (PF-1 (b)) ---------------------------------------


def _row(**kwargs):
    base = {"key": "f", "value_type": "string", "on_value": "on", "off_value": "off"}
    return SimpleNamespace(**{**base, **kwargs})


@pytest.mark.regression
def test_served_value_is_on_when_enabled_and_off_otherwise():
    assert served_value(_row(), True) == (True, "on")
    assert served_value(_row(), False) == (True, "off")
    assert served_value(_row(value_type="boolean"), True) == (True, True)
    assert served_value(_row(value_type="boolean"), False) == (True, False)
    assert served_value(SimpleNamespace(key="new"), False) == (True, False)


@pytest.mark.regression
@pytest.mark.parametrize(
    "row",
    [
        _row(value_type="number", on_value=10**400, off_value=1),
        _row(value_type="number", on_value=1, off_value=2**60),
        _row(value_type="json", on_value=_nested(MAX_VALUE_DEPTH + 1), off_value=[]),
        _row(value_type="json", on_value=_sized(MAX_VALUE_BYTES + 1), off_value=[]),
        _row(value_type="date", on_value="2026-01-01", off_value="2026-01-02"),
    ],
    ids=["1e400", "2^60", "too-deep", "too-large", "unknown-type"],
)
def test_a_stored_value_outside_the_rules_is_not_served(row, caplog):
    """Either value, for every user: the flag answers without a value."""
    EVALUATION_NOTES.clear()
    with caplog.at_level(logging.WARNING, logger=flag_values.__name__):
        assert served_value(row, True) == (False, None)
        assert served_value(row, False) == (False, None)
    warnings = [r for r in caplog.records if r.name == flag_values.__name__]
    assert len(warnings) == 1, "logged once per process, not per evaluation"
    assert "Flag f:" in warnings[0].getMessage()
    for value in (row.on_value, row.off_value):
        assert repr(value)[:20] not in warnings[0].getMessage()
