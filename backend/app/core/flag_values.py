"""What a feature flag serves: its value type and its two values (#1497), and
its split and its rules' own values (#2012).

A flag has one ``value_type``: ``boolean`` (the default), ``string``, ``number``
or ``json``. It serves ``on_value`` to the users it is on for -- a user who
matches a targeting rule and falls inside its percentage, or matches no rule
and falls inside the rollout percentage -- and ``off_value`` to everyone else,
and to everyone while it is off (DECISIONS T229).

A string, number or json flag may also have, in each environment, a split and
targeting rules with a value of their own. Neither changes who the flag is on
for; they change what those users are served (:func:`serve`): the matched
rule's own value if it has one, otherwise the split's value for the user, and
only then ``on_value``. No request sets either yet. A boolean flag's are never
read.

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

A split's values and a rule's own value follow the same rules;
:func:`split_errors` gives the rest of a split's.

Every message names the field and never the value.
"""

from __future__ import annotations

import hashlib
import logging
import math
from collections.abc import Iterable, Mapping
from typing import Any, Dict, List, Optional, Tuple

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

#: Fewest and most entries of a split.
MIN_SPLIT_ENTRIES = 3
MAX_SPLIT_ENTRIES = 10
#: What a split's weights add up to: a weight is a percentage of the users the
#: flag is on for.
SPLIT_TOTAL = 100
#: Exactly the keys of a split's entry.
SPLIT_ENTRY_KEYS = frozenset({"value", "weight"})

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

#: What :func:`split_errors` answers, whatever the split holds.
SPLIT_ON_A_BOOLEAN_FLAG = (
    "A boolean flag has no split; create a string, number or json flag to serve one."
)
SPLIT_IS_A_LIST = "A split is a list of values and weights."
SPLIT_TOO_SHORT = f"A split needs at least {MIN_SPLIT_ENTRIES} values."
SPLIT_TOO_LONG = f"A split can have at most {MAX_SPLIT_ENTRIES} values."
SPLIT_ENTRY = "An entry of a split has a value and a weight, and nothing else."
SPLIT_WEIGHT = f"A weight is a whole number from 0 to {SPLIT_TOTAL}."
SPLIT_VALUES_EQUAL = (
    "value equals an earlier entry's value; the values of a split must differ"
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
    ``on_value`` equal to the stored ``off_value`` is refused here rather than
    by the database's check. The update route locks the row while it checks,
    so *stored* is the committed pair; ``FeatureFlagService`` answers a check
    violation that still reaches the commit the same way.
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
        errors.append(values_equal_refusal(sent))
    return errors


def values_equal_refusal(sent: Iterable[str]) -> Tuple[str, PydanticCustomError]:
    """The ``values_equal`` refusal of a write that sent the fields *sent*.

    It sits at ``off_value`` when that was sent, otherwise at ``on_value``:
    always a field the client sent.
    """
    field = "off_value" if "off_value" in sent else "on_value"
    other = "on_value" if field == "off_value" else "off_value"
    return (
        field,
        PydanticCustomError(
            "values_equal",
            f"{field} equals {other}; the two values must differ, or turning the "
            "flag off changes nothing",
        ),
    )


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


def servable_values(flag: Any) -> Optional[Tuple[Any, Any]]:
    """``(on_value, off_value)``: the two values *flag* serves, or ``None``.

    A boolean flag serves ``True`` and ``False``; its split and its rules'
    values, if a direct database write left any, are not read. A string,
    number or json flag serves its stored pair, and its split's and its rules'
    values where :func:`serve` says so. ``None`` when the flag has no value to
    serve: a type this release does not know, or a stored value, split or
    rule's value outside the rules above (only a direct database write, or a
    release with wider limits, can leave one). All of them are checked, so the
    flag answers the same way for every user, and the flag's key, never a
    value, is logged once per process.

    The evaluate route (:func:`serve`) and the SDK ruleset
    (``backend.app.services.sdk_ruleset``) both read a stored row through this
    function, so they cannot disagree about which rows have a value (PF-1).
    """
    value_type = flag_value_type(flag)
    if value_type == BOOLEAN:
        return True, False
    on_value = getattr(flag, "on_value", None)
    off_value = getattr(flag, "off_value", None)
    if value_type not in VALUE_TYPES or _unservable(
        value_type, on_value, off_value, flag
    ):
        key = getattr(flag, "key", None)
        if EVALUATION_NOTES.first(f"flag:{key}", "unservable value"):
            logger.warning(
                "Flag %s: a stored value, split or rule value is outside the value "
                "rules (type %s); evaluation answers it without a value, and the "
                "SDK ruleset lists it as evaluated by the server",
                key,
                value_type if value_type in VALUE_TYPES else "unknown",
            )
        return None
    return on_value, off_value


def _unservable(value_type: str, on_value: Any, off_value: Any, flag: Any) -> bool:
    """Whether any value *flag* stores is outside the rules of *value_type*."""
    values = [on_value, off_value]
    values += rule_values(getattr(flag, "targeting_rules", None)).values()
    if any(value_error("value", value_type, value) is not None for value in values):
        return True
    split = _split_of(flag)
    return split is not None and bool(split_errors(value_type, split))


def served_value(flag: Any, enabled: bool) -> Tuple[bool, Any]:
    """``(True, value)``: which of *flag*'s two values a user it is *enabled*
    for, or not, is served.

    ``on_value`` when *enabled*, ``off_value`` otherwise; a boolean flag's
    value is *enabled* itself. ``(False, None)`` when the flag has no value to
    serve (:func:`servable_values`): the evaluate route then answers without
    ``value``. A split or a rule's own value is :func:`serve`'s.
    """
    pair = servable_values(flag)
    if pair is None:
        return False, None
    return True, pair[0] if enabled else pair[1]


def serve(flag: Any, evaluation: Mapping[str, Any], user_id: str) -> Tuple[bool, Any]:
    """``(True, value)``: what *flag* serves *user_id*, given its *evaluation*
    for them (``FeatureFlagService.evaluate_flag_detailed``'s answer).

    Not enabled: the off value. Enabled: the value of the rule that matched
    (``rule_id``) if it has one, otherwise the split's value for this user
    (:func:`split_value`), otherwise the on value. A boolean flag's value is
    ``enabled``. ``(False, None)`` when the flag has no value to serve
    (:func:`servable_values`), whatever the evaluation says.

    *flag* is the flag row, or its view in an environment: the rules and the
    split are that environment's. Pure: it reads nothing but its arguments.
    """
    servable, value = served_value(flag, evaluation["enabled"])
    if not servable or not evaluation["enabled"] or flag_value_type(flag) == BOOLEAN:
        return servable, value
    own = rule_values(getattr(flag, "targeting_rules", None))
    if evaluation["rule_id"] in own:
        return True, own[evaluation["rule_id"]]
    split = _split_of(flag)
    if split is not None:
        return True, split_value(split, split_bucket(user_id, flag.key))
    return True, value


def has_split_or_rule_value(flag: Any) -> bool:
    """Whether *flag* has a split or a rule with a value of its own, in the
    environment it is given for. A boolean flag's are never read, so it has
    neither."""
    if flag_value_type(flag) == BOOLEAN:
        return False
    return _split_of(flag) is not None or bool(
        rule_values(getattr(flag, "targeting_rules", None))
    )


# ---------------------------------------------------------------------------
# The split (#2012)
# ---------------------------------------------------------------------------


def _split_of(flag: Any) -> Any:
    """The flag's split, or ``None`` for none."""
    return getattr(flag, "split", None)


def _is_weight(weight: Any) -> bool:
    """A whole number from 0 to 100, as a targeting rule's
    ``rollout_percentage`` is read: ``100.0`` is one; ``33.5``, ``true`` and
    text are not."""
    whole = isinstance(weight, int) or (
        isinstance(weight, float) and weight.is_integer()
    )
    return whole and not isinstance(weight, bool) and 0 <= weight <= SPLIT_TOTAL


def split_errors(
    value_type: str, split: Any
) -> List[Tuple[Tuple[Any, ...], PydanticCustomError]]:
    """Why *split* cannot be the split of a *value_type* flag, as
    ``(where, error)`` pairs; none when it can.

    A split is a list of :data:`MIN_SPLIT_ENTRIES` to :data:`MAX_SPLIT_ENTRIES`
    entries, each exactly ``{"value", "weight"}``. A value follows
    :func:`value_error`; a weight is a whole number from 0 to 100; the weights
    add up to :data:`SPLIT_TOTAL`; no two values are equal
    (:func:`values_equal`). A value may be the flag's on or off value, and a
    weight may be 0. ``None`` is not a split: a flag without one has ``None``.

    *where* is below the split: ``()`` for the split itself (its type, the
    number of entries, the total, a boolean flag's), ``(i,)`` for an entry that
    is not one, ``(i, "value")`` and ``(i, "weight")`` otherwise; of two equal
    values the later one is named.
    """
    if value_type == BOOLEAN:
        return [((), PydanticCustomError("split_boolean", SPLIT_ON_A_BOOLEAN_FLAG))]
    if not isinstance(split, list):
        return [((), PydanticCustomError("split_type", SPLIT_IS_A_LIST))]
    if len(split) < MIN_SPLIT_ENTRIES:
        return [((), PydanticCustomError("split_too_short", SPLIT_TOO_SHORT))]
    if len(split) > MAX_SPLIT_ENTRIES:
        return [((), PydanticCustomError("split_too_long", SPLIT_TOO_LONG))]

    errors: List[Tuple[Tuple[Any, ...], PydanticCustomError]] = []
    total, values = 0, []
    for index, entry in enumerate(split):
        if not isinstance(entry, dict) or entry.keys() != SPLIT_ENTRY_KEYS:
            error = PydanticCustomError("split_entry", SPLIT_ENTRY)
            errors.append(((index,), error))
            continue
        value = entry["value"]
        error = value_error("value", value_type, value)
        # ``==`` first, because evaluation asks this of every split it serves:
        # two values ``values_equal`` holds equal are ``==``, and ``==`` is
        # quick to say no. Alone it is not the test: ``True == 1``.
        if error is None and any(v == value and values_equal(v, value) for v in values):
            error = PydanticCustomError("split_values_equal", SPLIT_VALUES_EQUAL)
        if error is None:
            values.append(value)
        else:
            errors.append(((index, "value"), error))
        if _is_weight(entry["weight"]):
            total += int(entry["weight"])
        else:
            error = PydanticCustomError("split_weight", SPLIT_WEIGHT)
            errors.append(((index, "weight"), error))
    if not errors and total != SPLIT_TOTAL:
        text = f"Weights must add up to {SPLIT_TOTAL}; they add up to {total}."
        errors.append(((), PydanticCustomError("split_total", text)))
    return errors


def percent_bucket(text: str) -> int:
    """The bucket of *text*, 0 to 99: its whole MD5 digest, read as one
    big-endian number, modulo 100.

    The flag rollout (``"{user_id}:{flag_key}"``) and the split
    (:func:`split_bucket`) share it. It is not
    ``consistent_hash.bucket_of``, which reads the digest's first four bytes.
    """
    digest = hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()
    return int(digest, 16) % 100


def split_bucket(user_id: str, flag_key: str) -> int:
    """The user's bucket in the flag's split, 0 to 99.

    Not the rollout's: the text hashed has ``:split`` after it, so a flag on
    for some of the users still serves every one of its split's values to
    them, and a user's value stays theirs while the percentage moves. No
    environment enters it: the same split serves a user the same value in
    every environment.
    """
    return percent_bucket(f"{user_id}:{flag_key}:split")


def split_value(split: Any, bucket: int) -> Any:
    """The value *split* serves in *bucket*: its entries take the buckets in
    stored order, each as many as its weight, so one of weight 0 takes none.

    *split* is one that :func:`split_errors` refuses nothing of.
    """
    below = 0
    for entry in split:
        below += int(entry["weight"])
        if bucket < below:
            return entry["value"]
    raise ValueError("the weights of a split add up to 100")


#: The id of the one rule a dashboard-shape value without an id stands for:
#: ``targeting_adapter.DASHBOARD_RULE_ID``, which is not imported here so that
#: the adapter can import this module.
_DASHBOARD_RULE_ID = "dashboard"


def rule_values(stored_rules: Any) -> Dict[str, Any]:
    """``{rule id: value}``: the rules in *stored_rules* that carry a value.

    *stored_rules* is a flag's ``targeting_rules`` as stored, in any shape. A
    rule carries a value when it has the key ``value``, whatever the key
    holds; a condition's ``value`` is not one. The shape is told apart as
    ``targeting_adapter.normalise_targeting_rules`` tells it:

    * the dashboard shape (it has ``groups``) is one rule, whose ``value`` is
      the top-level key and whose id is the stored ``id`` or ``dashboard``;
    * the native shape (it has ``rules`` and no ``groups``) has one rule per
      entry of ``rules``, and ``default_rule``;
    * nothing else has one: no rules, the legacy list, an object that is
      neither shape.

    Pure. It does not look at the flag's type, and whether a value may be
    served is :func:`value_error`'s question, not this one's.
    """
    if not isinstance(stored_rules, dict):
        return {}
    if "groups" in stored_rules:
        if "value" not in stored_rules:
            return {}
        return {
            str(stored_rules.get("id") or _DASHBOARD_RULE_ID): stored_rules["value"]
        }
    if "rules" not in stored_rules:
        return {}
    rules = stored_rules["rules"]
    native = [*(rules if isinstance(rules, list) else ())]
    native.append(stored_rules.get("default_rule"))
    return {
        str(rule.get("id", "")): rule["value"]
        for rule in native
        if isinstance(rule, dict) and "value" in rule
    }
