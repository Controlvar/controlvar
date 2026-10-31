"""What a feature flag serves: its value type and its two values (#1497).

A flag has one ``value_type``: ``boolean`` (the default), ``string``, ``number``
or ``json``. It serves ``on_value`` to the users it is on for -- a user who
matches a targeting rule and falls inside its percentage, or matches no rule
and falls inside the rollout percentage -- and ``off_value`` to everyone else,
and to everyone while it is off (DECISIONS T229).

A boolean flag serves ``true`` and ``false``. It stores neither value (both
columns NULL, as the ``check_flag_values`` constraint requires), reads
``true``/``false``, and a request may send those two back and nothing else.

The rules a string, number or json flag's values follow, in one place, so the
API's write checks and the evaluate route's reading of a stored row cannot
disagree:

* the JSON type is the flag's: a string, a number (never a boolean), or, for
  ``json``, an object or an array (``null`` is never a value);
* at most :data:`MAX_VALUE_DEPTH` levels deep, every object and array a level
  and the value itself the first (``nested_deeper_than``, iterative);
* no string or key holding NUL or a lone surrogate, which PostgreSQL refuses;
* every number in it a portable one (:func:`is_portable_number`): finite and
  from ``-MAX_SAFE_INTEGER`` to ``MAX_SAFE_INTEGER``;
* at most :data:`MAX_VALUE_BYTES` bytes as compact JSON in UTF-8 (the measure
  of the targeting-context limit, #1178);
* the two values differ, compared as PostgreSQL compares ``jsonb``
  (:func:`values_equal`): the database's own check refuses an equal pair, and
  the API answers 422 first rather than letting that become a 500.

Every message names the field and never the value.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from typing import Any, List, Optional, Tuple

from pydantic_core import PydanticCustomError

from backend.app.core.context_limits import compact_size, nested_deeper_than
from backend.app.core.log_once import EVALUATION_NOTES
from backend.app.schemas.storable_text import (
    contains_unstorable_text,
    unstorable_text_message,
)

logger = logging.getLogger(__name__)

BOOLEAN = "boolean"
STRING = "string"
NUMBER = "number"
JSON = "json"

#: Every value type, the default first.
VALUE_TYPES = (BOOLEAN, STRING, NUMBER, JSON)

#: The two value fields of a flag, in the order their errors are reported.
VALUE_FIELDS = ("on_value", "off_value")

#: The largest integer both Python and JavaScript represent exactly. A rule
#: value of greater magnitude makes its flag server-evaluated (sdk_ruleset);
#: a flag value of greater magnitude is refused.
MAX_SAFE_INTEGER = 2**53 - 1

#: Largest value accepted, in bytes of compact UTF-8 JSON.
MAX_VALUE_BYTES = 8192
#: Deepest nesting of JSON objects and arrays accepted in a value, the value
#: itself counted as level 1.
MAX_VALUE_DEPTH = 10

#: The answer to a boolean flag sent any other value.
BOOLEAN_VALUES = (
    "a boolean flag's on_value is true and its off_value is false; to serve "
    "another value, create a string, number or json flag"
)

#: The answer to an update that sends a ``value_type`` other than the stored one.
VALUE_TYPE_READ_ONLY = (
    "value_type is read-only once a flag is created. "
    "Create a new flag to serve another type."
)

_TYPE_WORDS = {
    STRING: "a string",
    NUMBER: "a number",
    JSON: "a JSON object or array",
}


def is_portable_number(value: Any) -> bool:
    """A JSON number both languages compare identically.

    Not a bool (checked first: ``True`` is an ``int`` in Python), finite, and
    of magnitude at most :data:`MAX_SAFE_INTEGER`. The magnitude is checked
    before anything converts the value, so a huge ``int`` never reaches a
    float conversion.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(value, int):
        return -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER
    return math.isfinite(value) and abs(value) <= MAX_SAFE_INTEGER


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _numbers_portable(value: Any) -> bool:
    """Every number in *value* (keys aside) is portable. Iterative."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
        elif _is_number(item) and not is_portable_number(item):
            return False
    return True


def _of_type(value_type: str, value: Any) -> bool:
    if value_type == STRING:
        return isinstance(value, str)
    if value_type == NUMBER:
        return _is_number(value)
    if value_type == JSON:
        return isinstance(value, (dict, list))
    return False


def value_error(
    field: str, value_type: str, value: Any
) -> Optional[PydanticCustomError]:
    """Why *value* cannot be *field* of a *value_type* flag, or ``None``.

    For a boolean flag, ``on_value`` may only be ``true`` and ``off_value``
    only ``false`` (JSON booleans, not ``1`` or ``"true"``). For the other
    types the checks run in the module docstring's order, the type first, and
    the first one that fails is the answer.
    """
    if value_type == BOOLEAN:
        if value is (field == "on_value"):
            return None
        return PydanticCustomError("boolean_values", BOOLEAN_VALUES)
    if not _of_type(value_type, value):
        return PydanticCustomError(
            "value_type_mismatch",
            f"{field} must be {_TYPE_WORDS.get(value_type, value_type)}, "
            f"because value_type is {value_type}",
        )
    if nested_deeper_than(value, MAX_VALUE_DEPTH):
        return PydanticCustomError(
            "value_too_deep",
            f"{field} nested more than {MAX_VALUE_DEPTH} levels deep",
        )
    if contains_unstorable_text(value):
        return PydanticCustomError("unstorable_text", unstorable_text_message(field))
    if not _numbers_portable(value):
        return PydanticCustomError(
            "value_out_of_range",
            f"{field}: a number is outside -{MAX_SAFE_INTEGER} to {MAX_SAFE_INTEGER}",
        )
    if compact_size(value) > MAX_VALUE_BYTES:
        return PydanticCustomError(
            "value_too_large", f"{field} larger than {MAX_VALUE_BYTES} bytes"
        )
    return None


def values_equal(a: Any, b: Any) -> bool:
    """Whether PostgreSQL's ``jsonb`` ``=`` holds two JSON values equal.

    Numbers compare as numbers (``3`` equals ``3.0``); a boolean never equals
    a number (``true`` is not ``1``); strings compare exactly; objects are
    equal when they have the same keys and equal values; arrays when they have
    the same length and equal items in order. Iterative.

    Python's ``==`` is not this: ``True == 1``. Comparing the canonical JSON
    text is not either: ``"3" != "3.0"``.
    """
    stack = [(a, b)]
    while stack:
        x, y = stack.pop()
        if isinstance(x, bool) or isinstance(y, bool):
            if not (isinstance(x, bool) and isinstance(y, bool) and x is y):
                return False
        elif _is_number(x) or _is_number(y):
            if not (_is_number(x) and _is_number(y) and x == y):
                return False
        elif isinstance(x, str) or isinstance(y, str):
            if not (isinstance(x, str) and isinstance(y, str) and x == y):
                return False
        elif isinstance(x, dict) or isinstance(y, dict):
            if not (isinstance(x, dict) and isinstance(y, dict)):
                return False
            if x.keys() != y.keys():
                return False
            stack.extend((x[key], y[key]) for key in x)
        elif isinstance(x, (list, tuple)) or isinstance(y, (list, tuple)):
            if not (isinstance(x, (list, tuple)) and isinstance(y, (list, tuple))):
                return False
            if len(x) != len(y):
                return False
            stack.extend(zip(x, y))
        elif not (x is None and y is None):
            return False
    return True


def write_errors(
    value_type: str,
    sent: Mapping[str, Any],
    stored: Optional[Mapping[str, Any]] = None,
) -> List[Tuple[str, PydanticCustomError]]:
    """The refusals of a write's values, as ``(field, error)`` pairs.

    *sent* holds the value fields the request carries (a field left out is
    absent). *stored* holds the flag's stored values on an update, ``None``
    on a create. The pair checked is the pair as it will be stored: each value
    as sent, otherwise as stored (EMF-5). So an update that sends only
    ``on_value`` equal to the stored ``off_value`` is refused here, and never
    reaches the database's check.
    """
    errors: List[Tuple[str, PydanticCustomError]] = []
    if value_type == BOOLEAN:
        for field in VALUE_FIELDS:
            if field in sent:
                error = value_error(field, value_type, sent[field])
                if error is not None:
                    errors.append((field, error))
        return errors

    pair = {}
    for field in VALUE_FIELDS:
        if field in sent:
            error = value_error(field, value_type, sent[field])
            if error is not None:
                errors.append((field, error))
            else:
                pair[field] = sent[field]
        elif stored is not None and stored.get(field) is not None:
            pair[field] = stored[field]
        else:
            errors.append(
                (
                    field,
                    PydanticCustomError(
                        "value_required",
                        f"{field} is required for a string, number or json flag",
                    ),
                )
            )
    if not errors and values_equal(pair["on_value"], pair["off_value"]):
        field = "off_value" if "off_value" in sent else "on_value"
        other = "on_value" if field == "off_value" else "off_value"
        errors.append(
            (
                field,
                PydanticCustomError(
                    "values_equal",
                    f"{field} equals {other}; a flag serves a different value "
                    "when it is off",
                ),
            )
        )
    return errors


def stored_values(value_type: str, on_value: Any, off_value: Any) -> Tuple[Any, Any]:
    """``(on_value, off_value)`` as a row stores them: NULL for a boolean flag."""
    if value_type == BOOLEAN:
        return None, None
    return on_value, off_value


def read_values(value_type: Any, on_value: Any, off_value: Any) -> Tuple[Any, Any]:
    """``(on_value, off_value)`` as a response reads them.

    A boolean flag (or a row with no type yet, such as one not flushed) reads
    ``true`` and ``false``; any other type reads what is stored, so a row of a
    type this release does not know is still answered, never refused.
    """
    if value_type is None or value_type == BOOLEAN:
        return True, False
    return on_value, off_value


def flag_value_type(flag: Any) -> str:
    """The flag's value type; a row with none yet (not flushed) is boolean."""
    return getattr(flag, "value_type", None) or BOOLEAN


def served_value(flag: Any, enabled: bool) -> Tuple[bool, Any]:
    """``(True, value)``: what *flag* serves a user it is *enabled* for or not.

    ``on_value`` when *enabled*, ``off_value`` otherwise; a boolean flag's
    value is *enabled* itself. ``(False, None)`` when the flag has no value to
    serve: a type this release does not know, or a stored value outside the
    rules above (only a direct database write, or a release with wider
    limits, can leave one). The evaluate route then answers without
    ``value``, and the flag's key, never the value, is logged once per process.
    Both stored values are checked, so the flag answers the same way for every
    user.
    """
    value_type = flag_value_type(flag)
    if value_type == BOOLEAN:
        return True, bool(enabled)
    on_value = getattr(flag, "on_value", None)
    off_value = getattr(flag, "off_value", None)
    if value_type not in VALUE_TYPES or any(
        value_error(field, value_type, value) is not None
        for field, value in (("on_value", on_value), ("off_value", off_value))
    ):
        key = getattr(flag, "key", None)
        if EVALUATION_NOTES.first(f"flag:{key}", "unservable value"):
            logger.warning(
                "Flag %s: a stored value is outside the value rules (type %s); "
                "evaluation answers it without a value",
                key,
                value_type if value_type in VALUE_TYPES else "unknown",
            )
        return False, None
    return True, on_value if enabled else off_value
