"""
The flag ruleset that server-side SDKs download to evaluate flags locally.

``GET /api/v1/sdk/ruleset`` returns every feature flag in the deployment in
one normalised shape, so a server-side SDK can answer
``/feature-flags/evaluate/{key}`` itself -- or, when it cannot be sure of
giving the server's answer, ask the server as it does today. This module
builds that document; the route only serves it.

The contract (``schema`` 1, beta)::

    {"schema": 1, "version": "<sha256 hex>", "bucketing": "md5-mod100-v1",
     "flags": [
       {"key": "new_search", "active": true, "evaluation": "local",
        "rollout_percentage": 25,
        "rules": [{"id": "dashboard", "rollout_percentage": 100,
                   "match": {"op": "and",
                             "conditions": [{"attribute": "user.country",
                                             "operator": "in",
                                             "value": ["US", "CA"]}],
                             "groups": []}}],
        "default_rule": null},
       {"key": "old_flag", "active": false},
       {"key": "semver_flag", "active": true, "evaluation": "remote"}]}

* Stored rules are read through the same
  :func:`~backend.app.core.targeting_adapter.normalise_targeting_rules` the
  flag service evaluates, so the dashboard, native and legacy shapes reach an
  SDK in one form, and rules are listed in the order the server tries them
  (priority, then stored order).
* ``default_rule`` carries only its ``id`` and ``rollout_percentage``: the
  server returns it without evaluating its conditions.
* A flag is ``"evaluation": "remote"`` -- and ships no rules -- when it uses
  the legacy list shape, an operator outside :data:`LOCAL_OPERATORS`, or a
  rule value outside that operator's portable domain (see
  :func:`condition_is_local`). An SDK asks the server about such a flag.
  Pattern conditions are always remote, because Python and JavaScript regular
  expressions are different dialects; this module classifies them by operator
  name and never compiles or runs a pattern.
* Inactive flags ship as ``{"key", "active": false}``: the server answers
  them ``enabled: false, reason: "inactive"`` whatever their rules say.
* Nothing else about a flag is included: not its name, description, owner,
  tags, variants or workspace.
* ``version`` is the sha256 of the canonical JSON of everything else
  (:func:`canonical_json`); the route serves it as the ETag.
* ``bucketing`` names the server's flag bucketing function:
  ``int(md5(f"{user_id}:{flag_key}").hexdigest(), 16) % 100 < percentage``.
  An SDK that does not know the value, or the ``schema``, evaluates nothing
  locally.

``tests/sdk-contract/ruleset-vectors.json`` (generated from this module and
the server's own evaluator by ``backend/scripts/generate_ruleset_vectors.py``)
pins the operator list, the value domains and the answers the SDKs must give.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Dict, Iterable, List, Optional

from backend.app.core.targeting_adapter import normalise_targeting_rules
from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus
from backend.app.schemas.targeting_rule import (
    Condition,
    OperatorType,
    RuleGroup,
    TargetingRule,
)

#: The ruleset document's format. An SDK that does not know it evaluates
#: nothing locally.
RULESET_SCHEMA = 1

#: The server's flag bucketing function, by name (see the module docstring).
BUCKETING = "md5-mod100-v1"

#: The largest integer both Python and JavaScript represent exactly. A number
#: of greater magnitude on either side of a comparison is answered by the
#: server.
MAX_SAFE_INTEGER = 2**53 - 1

#: Operators an SDK may evaluate itself (the engine's names). Every other
#: operator makes its flag ``evaluation: "remote"``.
LOCAL_OPERATORS = (
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

#: Code points either language treats as whitespace: Python's ``str.isspace``
#: set plus U+FEFF, which JavaScript's ``\s`` also matches. The server trims
#: strings for ``is_null``/``is_not_null`` and the two languages trim different
#: sets, so an SDK answers those operators on a string only when it contains
#: none of these; otherwise it asks the server. A fixed list, not either
#: language's own test, so both SDKs defer on exactly the same strings.
WHITESPACE_CODE_POINTS = (
    *range(0x0009, 0x000D + 1),
    *range(0x001C, 0x001F + 1),
    0x0020,
    0x0085,
    0x00A0,
    0x1680,
    *range(0x2000, 0x200A + 1),
    0x2028,
    0x2029,
    0x202F,
    0x205F,
    0x3000,
    0xFEFF,
)

STRING_OPERATORS = frozenset({"contains", "not_contains", "starts_with", "ends_with"})
EQUALITY_OPERATORS = frozenset({"eq", "neq"})
LIST_OPERATORS = frozenset({"in", "not_in"})
NUMERIC_OPERATORS = frozenset({"gt", "gte", "lt", "lte"})
PRESENCE_OPERATORS = frozenset({"is_null", "is_not_null"})

EVALUATION_LOCAL = "local"
EVALUATION_REMOTE = "remote"


# ---------------------------------------------------------------------------
# Value domains
# ---------------------------------------------------------------------------


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


def condition_is_local(condition: Condition) -> bool:
    """Whether an SDK may evaluate *condition* itself.

    The operator must be in :data:`LOCAL_OPERATORS` and the rule value in its
    domain:

    * ``contains``/``not_contains``/``starts_with``/``ends_with``: a string;
    * ``eq``/``neq``: a string, or a portable number;
    * ``in``/``not_in``: a non-empty list whose items are all strings, or all
      portable numbers;
    * ``gt``/``gte``/``lt``/``lte``: a portable number;
    * ``is_null``/``is_not_null``: any (the value is not read).

    Anything else -- a bool, null, a list or object where a scalar is
    expected, a numeric string for a numeric operator, a mixed list -- is not.
    """
    operator = _operator_name(condition.operator)
    value = condition.value
    if operator not in LOCAL_OPERATORS:
        return False
    if operator in PRESENCE_OPERATORS:
        return True
    if operator in STRING_OPERATORS:
        return isinstance(value, str)
    if operator in EQUALITY_OPERATORS:
        return isinstance(value, str) or is_portable_number(value)
    if operator in NUMERIC_OPERATORS:
        return is_portable_number(value)
    if operator in LIST_OPERATORS:
        if not isinstance(value, list) or not value:
            return False
        return all(isinstance(item, str) for item in value) or all(
            is_portable_number(item) for item in value
        )
    return False  # pragma: no cover - every local operator is handled above


def _operator_name(operator: Any) -> str:
    return operator.value if isinstance(operator, OperatorType) else str(operator)


# ---------------------------------------------------------------------------
# Flag entries
# ---------------------------------------------------------------------------


def is_active(flag: FeatureFlag) -> bool:
    """The flag service's own test: the enum member or its raw value."""
    return flag.status in (FeatureFlagStatus.ACTIVE, FeatureFlagStatus.ACTIVE.value)


def build_flag_entry(flag: FeatureFlag) -> Dict[str, Any]:
    """The ruleset entry for one flag."""
    if not is_active(flag):
        return {"key": flag.key, "active": False}

    remote = {"key": flag.key, "active": True, "evaluation": EVALUATION_REMOTE}
    stored = flag.targeting_rules
    if isinstance(stored, list) and stored:
        # The legacy list shape has its own semantics (raw context, strict
        # comparisons); the server keeps evaluating it.
        return remote

    rules: List[Dict[str, Any]] = []
    default_rule: Optional[Dict[str, Any]] = None
    native = (
        normalise_targeting_rules(stored, owner=f"flag:{flag.key}") if stored else None
    )
    if native is not None:
        for rule in sorted(native.rules, key=lambda r: r.priority):
            match = _group_entry(rule.rule)
            if match is None:
                return remote
            rules.append(
                {
                    "id": rule.id,
                    "rollout_percentage": int(rule.rollout_percentage),
                    "match": match,
                }
            )
        if native.default_rule is not None:
            default_rule = _default_rule_entry(native.default_rule)

    return {
        "key": flag.key,
        "active": True,
        # TAMPER (PF-13, never merged): one flag served with an evaluation value
        # the published SDKs do not know.
        "evaluation": "local-v2" if flag.key == "unicode" else EVALUATION_LOCAL,
        "rollout_percentage": int(flag.rollout_percentage or 0),
        "rules": rules,
        "default_rule": default_rule,
    }


def _group_entry(group: RuleGroup) -> Optional[Dict[str, Any]]:
    """A rule group in ruleset form, or ``None`` when any part of it is remote."""
    conditions: List[Dict[str, Any]] = []
    for condition in group.conditions:
        if not condition_is_local(condition):
            return None
        conditions.append(
            {
                "attribute": condition.attribute,
                "operator": _operator_name(condition.operator),
                "value": None
                if _operator_name(condition.operator) in PRESENCE_OPERATORS
                else condition.value,
            }
        )
    groups: List[Dict[str, Any]] = []
    for child in group.groups or []:
        entry = _group_entry(child)
        if entry is None:
            return None
        groups.append(entry)
    return {
        "op": group.operator.value,
        "conditions": conditions,
        "groups": groups,
    }


def _default_rule_entry(rule: TargetingRule) -> Dict[str, Any]:
    return {"id": rule.id, "rollout_percentage": int(rule.rollout_percentage)}


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------


def canonical_json(value: Any) -> str:
    """The one serialisation the version is computed over.

    Sorted keys, no whitespace, ASCII only (``\\uXXXX`` escapes), so the same
    content always hashes the same.
    """
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def ruleset_version(content: Dict[str, Any]) -> str:
    """sha256 hex of the canonical JSON of *content* (without ``version``)."""
    return hashlib.sha256(canonical_json(content).encode("ascii")).hexdigest()


def build_ruleset(flags: Iterable[FeatureFlag]) -> Dict[str, Any]:
    """The whole ruleset document for *flags*, ``version`` included."""
    entries = sorted((build_flag_entry(flag) for flag in flags), key=lambda e: e["key"])
    content: Dict[str, Any] = {
        "schema": RULESET_SCHEMA,
        "bucketing": BUCKETING,
        "flags": entries,
    }
    return {**content, "version": ruleset_version(content)}
