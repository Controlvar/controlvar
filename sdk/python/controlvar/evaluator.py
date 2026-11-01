"""Local flag evaluation from the server's ruleset (``GET /api/v1/sdk/ruleset``, beta).

The rule: a local answer is always the answer ``GET /api/v1/feature-flags/evaluate/{key}``
would give for the same ruleset version, or there is no local answer. Whenever this module
cannot be sure, it returns :data:`DEFER` and the client asks the server exactly as it does in
server mode. It never guesses, and it never answers "not matched" for something it could not
evaluate.

What is answered locally (everything else defers):

* the operators in :data:`LOCAL_OPERATORS`, only on the value types listed in
  :func:`evaluate_condition`: strings against strings, numbers against numbers, no coercion;
* numbers that are not ``bool``, finite, and of magnitude at most :data:`MAX_SAFE_INTEGER`
  (``bool`` is rejected before ``int``; the magnitude is checked before anything converts);
* ``is_null`` / ``is_not_null`` on a string only when it contains none of
  :data:`WHITESPACE_CODE_POINTS` (the server strips, and Python's and JavaScript's whitespace
  sets differ: this fixed list is their union, not ``str.isspace``);
* well-formed Unicode only: a lone surrogate in the user id, a context key or a context value
  defers.

Every condition and group of a rule that is tried is evaluated: a DEFER anywhere in it defers
the whole evaluation, even when a sibling has already decided the result, because the server
evaluates them all and can fail on the one this module would have skipped.

A flag's value is answered with its ``enabled``: ``on_value`` when enabled, ``off_value``
otherwise, and ``enabled`` itself for a boolean flag (an entry with no ``value_type``). An entry
this SDK cannot read DEFERs that one flag and leaves the rest of the document in use: an
``evaluation`` other than ``local`` or ``remote``, a ``value_type`` other than ``string``,
``number`` or ``json``, or value fields missing or of the wrong type. A newer server can
therefore add a flag shape without stopping local evaluation for every other flag. A known
shape that is malformed (an active ``local`` entry without integer percentages, say) still
rejects the whole document.

``tests/sdk-contract/ruleset-vectors.json`` is generated from the server and pins every
answer; ``tests/test_evaluator.py`` runs this module against it.

Standard library only, and no import from the rest of the package (a backend test loads this
file on its own); Python 3.9+.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union

__all__ = [
    "BUCKETING",
    "DEFER",
    "LOCAL_OPERATORS",
    "MAX_SAFE_INTEGER",
    "RULESET_SCHEMA",
    "VALUE_TYPES",
    "WHITESPACE_CODE_POINTS",
    "IndexedRuleset",
    "copy_value",
    "entry_value_type",
    "evaluate_condition",
    "evaluate_group",
    "evaluate_locally",
    "flag_bucket",
    "flatten_context",
    "in_rollout",
    "index_ruleset",
    "is_portable_number",
    "is_supported_format",
    "is_value_of_type",
    "is_well_formed",
    "reads_locally",
    "to_wire_context",
]

#: The ruleset format this SDK understands. Any other ``schema`` evaluates nothing locally.
RULESET_SCHEMA = 1

#: The flag bucketing function this SDK implements (see :func:`flag_bucket`).
BUCKETING = "md5-mod100-v1"

#: The largest integer both Python and JavaScript represent exactly.
MAX_SAFE_INTEGER = 2**53 - 1

#: The value types a ruleset entry may name in ``value_type``. A boolean flag's entry names none;
#: any other ``value_type`` defers that flag.
VALUE_TYPES: Tuple[str, ...] = ("string", "number", "json")

#: Operators this SDK evaluates itself (the server's names). A copy of ``local_operators`` in
#: ``tests/sdk-contract/ruleset-vectors.json``; a test fails when they differ.
LOCAL_OPERATORS: Tuple[str, ...] = (
    "contains",
    "ends_with",
    "eq",
    "gt",
    "gte",
    "in",
    "is_not_null",
    "is_null",
    "lt",
    "lte",
    "neq",
    "not_contains",
    "not_in",
    "starts_with",
)

#: Code points either Python or JavaScript treats as whitespace. A copy of
#: ``whitespace_code_points`` in ``tests/sdk-contract/ruleset-vectors.json``; a test fails when
#: they differ. Deliberately a fixed list: ``str.isspace`` misses U+FEFF, which JavaScript's
#: ``\s`` matches.
WHITESPACE_CODE_POINTS: Tuple[int, ...] = (
    0x0009, 0x000A, 0x000B, 0x000C, 0x000D,
    0x001C, 0x001D, 0x001E, 0x001F,
    0x0020, 0x0085, 0x00A0, 0x1680,
    0x2000, 0x2001, 0x2002, 0x2003, 0x2004, 0x2005, 0x2006, 0x2007, 0x2008, 0x2009, 0x200A,
    0x2028, 0x2029, 0x202F, 0x205F, 0x3000, 0xFEFF,
)  # fmt: skip

_LOCAL = frozenset(LOCAL_OPERATORS)
_WHITESPACE = frozenset(chr(code) for code in WHITESPACE_CODE_POINTS)
_STRING_OPERATORS = frozenset({"contains", "not_contains", "starts_with", "ends_with"})
_MATCH_OPERATORS = frozenset({"eq", "neq", "in", "not_in"})
_NUMERIC_OPERATORS = frozenset({"gt", "gte", "lt", "lte"})
_ALIAS_PREFIXES = ("user", "device", "app")
_MAX_DEPTH = 64


class _Defer:
    """The single "ask the server" value."""

    _instance: Optional["_Defer"] = None

    def __new__(cls) -> "_Defer":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "DEFER"

    def __bool__(self) -> bool:  # never mistaken for an answer
        raise TypeError("DEFER is not a boolean; compare with `is DEFER`")


#: "Ask the server": the answer cannot be given locally.
DEFER = _Defer()

#: ``{"enabled": bool, "reason": "targeting_rule" | "rollout" | "inactive", "value": Any,
#: "value_type": "boolean" | "string" | "number" | "json"}``. ``value`` is the server's: the
#: flag's ``on_value`` when enabled, its ``off_value`` otherwise, ``enabled`` for a boolean flag.
#: A JSON value is the ruleset's own object: copy it (:func:`copy_value`) before handing it out.
LocalAnswer = Dict[str, Any]
Outcome = Union[bool, _Defer]


# --------------------------------------------------------------------------- the document


class IndexedRuleset:
    """A validated schema-1 ruleset, indexed by flag key. Immutable once built."""

    __slots__ = ("schema", "bucketing", "version", "flags")

    def __init__(self, schema: int, bucketing: str, version: str, flags: Dict[str, Dict[str, Any]]):
        self.schema = schema
        self.bucketing = bucketing
        self.version = version
        self.flags = flags


def _is_percentage(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _valid_group(group: Any, depth: int) -> bool:
    if depth > _MAX_DEPTH or not isinstance(group, dict):
        return False
    if (
        not isinstance(group.get("op"), str)
        or not isinstance(group.get("conditions"), list)
        or not isinstance(group.get("groups"), list)
    ):
        return False
    for condition in group["conditions"]:
        if (
            not isinstance(condition, dict)
            or not isinstance(condition.get("attribute"), str)
            or not isinstance(condition.get("operator"), str)
        ):
            return False
    return all(_valid_group(child, depth + 1) for child in group["groups"])


def _valid_flag(flag: Any) -> bool:
    """Whether an entry may be indexed. Only a ``local`` entry's shape is checked: an inactive
    entry, a ``remote`` one and one with an ``evaluation`` this SDK does not know are indexed
    and never evaluated from their rules (see :func:`evaluate_locally`)."""
    if not isinstance(flag, dict) or not isinstance(flag.get("key"), str):
        return False
    if not isinstance(flag.get("active"), bool):
        return False
    if not flag["active"] or flag.get("evaluation") != "local":
        return True
    if not _is_percentage(flag.get("rollout_percentage")) or not isinstance(flag.get("rules"), list):
        return False
    for rule in flag["rules"]:
        if (
            not isinstance(rule, dict)
            or not isinstance(rule.get("id"), str)
            or not _is_percentage(rule.get("rollout_percentage"))
            or not _valid_group(rule.get("match"), 0)
        ):
            return False
    default_rule = flag.get("default_rule")
    if default_rule is not None:
        if not isinstance(default_rule, dict) or not _is_percentage(default_rule.get("rollout_percentage")):
            return False
    return True


def is_supported_format(body: Any) -> bool:
    """Whether a parsed body names a format this SDK understands (checked before its shape)."""
    return (
        isinstance(body, dict)
        and body.get("schema") == RULESET_SCHEMA
        and not isinstance(body.get("schema"), bool)
        and body.get("bucketing") == BUCKETING
    )


def index_ruleset(body: Any) -> Optional[IndexedRuleset]:
    """Index a parsed ruleset body, or ``None`` when it is not a well-formed schema-1 document.

    Call :func:`is_supported_format` first: an unknown format is a different failure from a
    malformed one.
    """
    if not is_supported_format(body):
        return None
    if not isinstance(body.get("version"), str) or not isinstance(body.get("flags"), list):
        return None
    flags: Dict[str, Dict[str, Any]] = {}
    for flag in body["flags"]:
        if not _valid_flag(flag):
            return None
        flags[flag["key"]] = flag
    return IndexedRuleset(body["schema"], body["bucketing"], body["version"], flags)


# --------------------------------------------------------------------------- values


def is_portable_number(value: Any) -> bool:
    """A JSON number both languages compare identically.

    ``bool`` is rejected first (``True`` is an ``int``); the magnitude is compared before any
    conversion, so a huge ``int`` never reaches a float operation, and ``inf``/``nan`` fail the
    comparison.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER


def _well_formed_str(value: str) -> bool:
    return not any(0xD800 <= ord(char) <= 0xDFFF for char in value)


def is_well_formed(value: Any, depth: int = 0) -> bool:
    """Every string in a JSON value, keys included, is free of lone surrogates."""
    if depth > 256:
        return False
    if isinstance(value, str):
        return _well_formed_str(value)
    if isinstance(value, list):
        return all(is_well_formed(item, depth + 1) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _well_formed_str(key) and is_well_formed(item, depth + 1)
            for key, item in value.items()
        )
    return True


def _contains_whitespace(value: str) -> bool:
    return any(char in _WHITESPACE for char in value)


# --------------------------------------------------------------------------- context


def flatten_context(context: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """The server's ``expand_context``.

    Nested objects flatten to dotted keys (first writer wins, in key order); a top-level
    attribute is also visible as ``user.``/``device.``/``app.`` + name; and ``user.x`` (one
    level) is also visible as ``x``.
    """
    flat: Dict[str, Any] = {}
    if not context:
        return flat

    def walk(node: Mapping[str, Any], prefix: str, depth: int) -> None:
        for key, value in node.items():
            full = prefix + key
            flat.setdefault(full, value)
            if isinstance(value, dict) and depth < 256:
                walk(value, full + ".", depth + 1)

    walk(context, "", 0)
    for key, value in list(flat.items()):
        if "." in key or isinstance(value, dict):
            continue
        for prefix in _ALIAS_PREFIXES:
            flat.setdefault(f"{prefix}.{key}", value)
    for key, value in list(flat.items()):
        head, dot, tail = key.partition(".")
        if dot and head in _ALIAS_PREFIXES and tail and "." not in tail:
            flat.setdefault(tail, value)
    return flat


def to_wire_context(attributes: Optional[Mapping[str, Any]], default: Any = None) -> Any:
    """The context exactly as the server would receive it, or :data:`DEFER`.

    The JSON round trip of what the server path sends (same ``json.dumps`` arguments, so
    tuples become lists, non-string keys become strings, datetimes become ISO strings). Empty
    or missing attributes are ``None``: the server path then sends no context. Attributes that
    cannot be serialised defer; the server path then fails the same way it does today.
    """
    if not attributes:
        return None
    try:
        text = json.dumps(dict(attributes), separators=(",", ":"), default=default)
        parsed = json.loads(text)
    except (TypeError, ValueError, RecursionError):
        return DEFER
    if not isinstance(parsed, dict):
        return DEFER
    return parsed


# --------------------------------------------------------------------------- conditions


def evaluate_condition(condition: Mapping[str, Any], flat: Mapping[str, Any]) -> Outcome:
    """One condition against the flattened context.

    * absent attribute: ``True`` for ``is_null``, ``False`` for everything else;
    * ``is_null``/``is_not_null``: ``None``, ``""``, ``[]`` and ``{}`` are null; a string
      containing any whitespace code point defers; any other value is not null;
    * an explicit ``None``: ``neq`` is ``True``, everything else ``False``;
    * ``contains``/``not_contains``/``starts_with``/``ends_with``: string against string;
    * ``eq``/``neq``/``in``/``not_in``: string against strings, or number against numbers;
    * ``gt``/``gte``/``lt``/``lte``: number against number;
    * any other pairing of types, and any operator not in :data:`LOCAL_OPERATORS`: DEFER.
    """
    op = condition.get("operator")
    rule = condition.get("value")
    if op not in _LOCAL:
        return DEFER
    attribute = condition.get("attribute")
    if attribute not in flat:
        return op == "is_null"
    value = flat[attribute]

    if op in ("is_null", "is_not_null"):
        if value is None:
            empty = True
        elif isinstance(value, str):
            if _contains_whitespace(value):
                return DEFER
            empty = value == ""
        elif isinstance(value, (list, dict)):
            empty = len(value) == 0
        else:
            empty = False
        return empty if op == "is_null" else not empty

    if value is None:
        return op == "neq"

    if op in _STRING_OPERATORS:
        if not isinstance(value, str) or not isinstance(rule, str) or not _well_formed_str(rule):
            return DEFER
        if op == "contains":
            return rule in value
        if op == "not_contains":
            return rule not in value
        if op == "starts_with":
            return value.startswith(rule)
        return value.endswith(rule)

    if op in _MATCH_OPERATORS:
        if op in ("in", "not_in"):
            items: Optional[List[Any]] = rule if isinstance(rule, list) else None
        else:
            items = [rule]
        if not items:
            return DEFER
        hit = False
        if isinstance(items[0], str):
            if not isinstance(value, str):
                return DEFER
            for item in items:
                if not isinstance(item, str) or not _well_formed_str(item):
                    return DEFER
                if item == value:
                    hit = True
        else:
            if not is_portable_number(value):
                return DEFER
            for item in items:
                if not is_portable_number(item):
                    return DEFER
                if item == value:
                    hit = True
        return hit if op in ("eq", "in") else not hit

    if op in _NUMERIC_OPERATORS:
        if not is_portable_number(value) or not is_portable_number(rule):
            return DEFER
        if op == "gt":
            return value > rule
        if op == "gte":
            return value >= rule
        if op == "lt":
            return value < rule
        return value <= rule

    return DEFER  # pragma: no cover - every local operator is handled above


def evaluate_group(group: Mapping[str, Any], flat: Mapping[str, Any], depth: int = 0) -> Outcome:
    """Every condition and child group is evaluated (no short-circuit), then any DEFER defers;
    an empty group is true; ``and`` is all, ``or`` is any, ``not`` is "not all"."""
    if depth > _MAX_DEPTH:
        return DEFER
    results: List[Outcome] = [evaluate_condition(c, flat) for c in group.get("conditions", [])]
    results += [evaluate_group(g, flat, depth + 1) for g in group.get("groups", [])]
    if any(result is DEFER for result in results):
        return DEFER
    if not results:
        return True
    op = group.get("op")
    if op == "and":
        return all(results)
    if op == "or":
        return any(results)
    if op == "not":
        return not all(results)
    return DEFER


# --------------------------------------------------------------------------- bucketing


def flag_bucket(user_id: str, flag_key: str) -> int:
    """``md5-mod100-v1``: the whole MD5 digest of the UTF-8 ``"{user_id}:{flag_key}"`` as a
    big-endian integer, mod 100. The server's flag bucket; NOT :func:`consistent_hash`."""
    digest = hashlib.md5(f"{user_id}:{flag_key}".encode("utf-8"), usedforsecurity=False)  # noqa: S324
    return int(digest.hexdigest(), 16) % 100


def in_rollout(percentage: int, user_id: str, flag_key: str) -> bool:
    """Whether ``user_id`` falls inside a rollout of ``percentage`` for ``flag_key``."""
    if percentage <= 0:
        return False
    if percentage >= 100:
        return True
    return flag_bucket(user_id, flag_key) < percentage


# --------------------------------------------------------------------------- flag values


def is_value_of_type(value: Any, value_type: Any) -> bool:
    """Whether ``value`` is a value of ``value_type`` as the server serves one: a ``str``; a
    number that is not a ``bool``, finite and within +/-(2**53 - 1) (:func:`is_portable_number`;
    ``10.0`` is a number); a ``dict`` or a ``list``. Any other ``value_type`` is ``False``."""
    if value_type == "string":
        return isinstance(value, str)
    if value_type == "number":
        return is_portable_number(value)
    if value_type == "json":
        return isinstance(value, (dict, list))
    return False


def copy_value(value: Any) -> Any:
    """A copy of a JSON value that shares nothing with it: a ``dict`` or ``list`` is copied
    deeply; a string, number, boolean or ``None`` is returned as it is."""
    if isinstance(value, (dict, list)):
        return copy.deepcopy(value)
    return value


def entry_value_type(flag: Mapping[str, Any]) -> Optional[str]:
    """The type of value a ruleset entry serves, or ``None`` when this SDK cannot read it.

    An entry with no ``value_type`` key is a boolean flag (``"boolean"``). ``None`` for a
    ``value_type`` outside :data:`VALUE_TYPES` (a newer server's, ``null`` or not a string),
    or when the value the entry needs (``off_value``, and ``on_value`` on an active entry) is
    missing or of another type.
    """
    if "value_type" not in flag:
        return "boolean"
    value_type = flag["value_type"]
    if not isinstance(value_type, str) or value_type not in VALUE_TYPES:
        return None
    if not is_value_of_type(flag.get("off_value"), value_type):
        return None
    if flag["active"] and not is_value_of_type(flag.get("on_value"), value_type):
        return None
    return value_type


def reads_locally(flag: Mapping[str, Any]) -> bool:
    """Whether an active flag can be answered from the ruleset at all: a ``local`` entry this
    SDK can read. Every other active flag is evaluated by the server, for every user."""
    return flag.get("evaluation") == "local" and entry_value_type(flag) is not None


# --------------------------------------------------------------------------- flags


def evaluate_locally(
    ruleset: IndexedRuleset,
    flag_key: str,
    user_id: str,
    context: Optional[Mapping[str, Any]],
) -> Union[LocalAnswer, _Defer]:
    """Evaluate one flag locally, or :data:`DEFER`.

    ``context`` must already be what the server would receive (see :func:`to_wire_context`),
    or ``None`` for none. The answer carries the flag's ``value`` and ``value_type`` (see
    :data:`LocalAnswer`); an entry this SDK cannot read (:func:`entry_value_type`, or an
    ``evaluation`` other than ``local`` on an active entry) defers.
    """
    if ruleset.schema != RULESET_SCHEMA or ruleset.bucketing != BUCKETING:
        return DEFER
    if not isinstance(user_id, str) or not isinstance(flag_key, str):
        return DEFER
    if not _well_formed_str(user_id) or not _well_formed_str(flag_key) or not is_well_formed(context):
        return DEFER
    flag = ruleset.flags.get(flag_key)
    if flag is None:
        return DEFER
    value_type = entry_value_type(flag)
    if value_type is None:
        return DEFER

    def answer(enabled: bool, reason: str) -> LocalAnswer:
        if value_type == "boolean":
            value = enabled
        else:
            value = flag["on_value"] if enabled else flag["off_value"]
        return {"enabled": enabled, "reason": reason, "value": value, "value_type": value_type}

    if not flag["active"]:
        return answer(False, "inactive")
    if flag.get("evaluation") != "local":
        return DEFER

    flat = flatten_context(context)
    for rule in flag.get("rules") or []:
        matched = evaluate_group(rule["match"], flat)
        if matched is DEFER:
            return DEFER
        if matched:
            return answer(in_rollout(rule["rollout_percentage"], user_id, flag_key), "targeting_rule")
    default_rule = flag.get("default_rule")
    if default_rule is not None:
        return answer(in_rollout(default_rule["rollout_percentage"], user_id, flag_key), "targeting_rule")
    return answer(in_rollout(flag.get("rollout_percentage") or 0, user_id, flag_key), "rollout")
