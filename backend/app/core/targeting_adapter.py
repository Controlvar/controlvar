"""
Targeting rule adapter: dashboard / native rule shapes -> Enhanced Rules Engine.

Feature flag ``targeting_rules`` are stored as JSONB and historically arrived in
three incompatible shapes:

* the **dashboard editor** shape written by the frontend targeting builder::

      {"logical_operator": "AND"|"OR",
       "groups": [{"logical_operator": "AND"|"OR",
                   "conditions": [{"attribute", "operator", "value"}]}]}

  with operators such as ``equals``, ``semver_gte``, ``is_null`` and dotted
  attributes such as ``user.country`` / ``app.version``;
* the **native** :class:`~backend.app.schemas.targeting_rule.TargetingRules`
  shape used by the Enhanced Rules Engine (``{"rules": [...], "default_rule"}``);
* a **legacy list** shape (``[{"type": "user_id"|"context", ...}]``) that
  :class:`~backend.app.services.feature_flag_service.FeatureFlagService`
  evaluates itself.

:func:`normalise_targeting_rules` turns the first two into ``TargetingRules``
and returns ``None`` for the legacy list (or anything it cannot understand) so
the caller keeps its existing behaviour. :func:`expand_context` flattens an SDK
context into the flat lookup dict the engine expects and adds the
``user.<k>`` / ``device.<k>`` / ``app.<k>`` aliases documented in the flag docs.
:func:`match_targeting_rule` finds the first matching rule *without* applying
the engine's own rollout hashing, so the flag service can bucket users with the
flag-level hash exactly as it does for the global rollout percentage.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any, Dict, Iterator, List, Optional, Tuple

from pydantic import ValidationError

from backend.app.core.log_once import EVALUATION_NOTES
from backend.app.core.rules_engine import (
    ARRAY_LENGTH_COMPARISONS,
    GEO_COMPARISONS,
    GEO_UNITS,
    OPERATORS_NOT_ON_FLAGS,
    SEGMENT_OPERATORS,
    SEMVER_COMPARISONS,
    TimeWindowUnreadable,
    between_bounds_problem,
    check_time_window,
    date_value_readable,
    evaluate_rule,
    length_bounds_readable,
    length_value_readable,
    number_value_readable,
    percentage_value_readable,
    radius_readable,
)
from backend.app.schemas.targeting_rule import (
    Condition,
    LogicalOperator,
    OperatorType,
    RuleGroup,
    TargetingRule,
    TargetingRules,
)

logger = logging.getLogger(__name__)

# Rule id used for the single rule produced from the dashboard shape.
DASHBOARD_RULE_ID = "dashboard"

# Top-level context keys are also reachable under these prefixes so dashboard
# attributes such as ``user.country`` resolve against ``{"country": "US"}``.
CONTEXT_ALIAS_PREFIXES = ("user", "device", "app")

# Dashboard operator -> (engine operator, semantic-version comparison).
_SIMPLE_OPERATOR_MAP: Dict[str, OperatorType] = {
    "equals": OperatorType.EQUALS,
    "not_equals": OperatorType.NOT_EQUALS,
    "contains": OperatorType.CONTAINS,
    "not_contains": OperatorType.NOT_CONTAINS,
    "starts_with": OperatorType.STARTS_WITH,
    "ends_with": OperatorType.ENDS_WITH,
    "greater_than": OperatorType.GREATER_THAN,
    "less_than": OperatorType.LESS_THAN,
    "greater_than_or_equal": OperatorType.GREATER_THAN_OR_EQUAL,
    "less_than_or_equal": OperatorType.LESS_THAN_OR_EQUAL,
    "in": OperatorType.IN,
    "not_in": OperatorType.NOT_IN,
    "regex": OperatorType.MATCH_REGEX,
    "is_null": OperatorType.IS_NULL,
    "is_not_null": OperatorType.IS_NOT_NULL,
    "geo_within_radius": OperatorType.GEO_DISTANCE,
    "time_window": OperatorType.TIME_WINDOW,
    "array_contains": OperatorType.CONTAINS_ALL,
    "array_intersects": OperatorType.CONTAINS_ANY,
}

_SEMVER_OPERATOR_MAP: Dict[str, str] = {
    "semver_eq": "eq",
    "semver_gt": "gt",
    "semver_lt": "lt",
    "semver_gte": "gte",
    "semver_lte": "lte",
}

#: Segment membership (#440): the dashboard names are the engine's names.
_SEGMENT_OPERATOR_MAP: Dict[str, OperatorType] = {
    "in_segment": OperatorType.IN_SEGMENT,
    "not_in_segment": OperatorType.NOT_IN_SEGMENT,
}

#: Every dashboard operator the adapter understands.
DASHBOARD_OPERATORS = (
    frozenset(_SIMPLE_OPERATOR_MAP)
    | frozenset(_SEMVER_OPERATOR_MAP)
    | frozenset(_SEGMENT_OPERATOR_MAP)
)

#: The attribute a segment condition names. It is not read from the context:
#: membership is resolved by the server.
SEGMENT_ATTRIBUTE = "segment"

#: Most distinct segments one ruleset may reference.
MAX_REFERENCED_SEGMENTS = 10

_NUMERIC_OPERATORS = frozenset(
    {
        OperatorType.GREATER_THAN,
        OperatorType.GREATER_THAN_OR_EQUAL,
        OperatorType.LESS_THAN,
        OperatorType.LESS_THAN_OR_EQUAL,
    }
)
_LIST_OPERATORS = frozenset(
    {
        OperatorType.IN,
        OperatorType.NOT_IN,
        OperatorType.CONTAINS_ALL,
        OperatorType.CONTAINS_ANY,
    }
)

_NUMBER_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")


class TargetingRuleShapeError(ValueError):
    """Raised internally when a dashboard rule cannot be converted."""


def _is_dashboard_rules_shape(raw: Dict[str, Any]) -> bool:
    """True for the dashboard editor shape ``{"logical_operator", "groups": [...]}``.

    This is how experiment assignment decides which shape stored rules are in
    (``AssignmentService._coerce_targeting_rules``), and how
    :func:`validate_experiment_targeting` decides it, so the two cannot
    disagree. It is deliberately not :func:`normalise_targeting_rules`'s
    ``"groups" in raw`` test: a dict carrying both ``groups`` and ``rules`` is
    native here.
    """
    return "rules" not in raw and ("groups" in raw or "logical_operator" in raw)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def normalise_targeting_rules(
    raw: Any, owner: str = "targeting rules"
) -> Optional[TargetingRules]:
    """
    Convert stored ``targeting_rules`` JSON into engine ``TargetingRules``.

    ``owner`` names the rules in the warning logged when they cannot be
    converted (``flag:<key>``, ``experiment:<id>``). That warning is logged
    once per owner and shape, not on every evaluation, and names only the
    exception type: its text can repeat the rule's content.

    Returns:
        ``TargetingRules`` for the native shape (has ``rules``) and for the
        dashboard shape (has ``groups``); ``None`` for empty input, for the
        legacy list shape (the flag service keeps evaluating that itself) and
        for the shapes it checks that cannot be interpreted, so callers fall
        back to the flag's global rollout percentage.

    Raises:
        Stored content can still raise another exception: an integer too
        large for a float in a geo value (``OverflowError``), a version part
        ``int()`` cannot read (``ValueError``). The write validators refuse
        those, so only a direct database write leaves one. Every caller that
        reads more than one owner's rules guards its own call, so one such
        row affects only its owner (:func:`report_unreadable_rules`, #1674).
    """
    if raw is None:
        return None
    if isinstance(raw, TargetingRules):
        return raw
    if isinstance(raw, list):
        # Legacy [{"type": ..., "conditions": ...}] shape — owned by the service.
        return None
    if not isinstance(raw, dict) or not raw:
        return None

    if "groups" in raw:
        try:
            return _from_dashboard(raw)
        except (TargetingRuleShapeError, ValidationError, TypeError) as exc:
            if EVALUATION_NOTES.first(owner, "dashboard rules not converted"):
                logger.warning(
                    "Ignoring dashboard targeting rules for %s that could not be "
                    "converted (%s); logged once.",
                    owner,
                    type(exc).__name__,
                )
            return None

    if "rules" in raw:
        try:
            return TargetingRules.model_validate(raw)
        except ValidationError as exc:
            if EVALUATION_NOTES.first(owner, "native rules failed validation"):
                logger.warning(
                    "Ignoring native targeting rules for %s that failed validation "
                    "(%s); logged once.",
                    owner,
                    type(exc).__name__,
                )
            return None

    return None


#: What a flag does when its stored rules cannot be read (#1674): the
#: consequence :func:`report_unreadable_rules` logs for ``flag:<key>``.
FLAG_RULES_UNREADABLE = (
    'that flag answers false (reason "error") and the SDK ruleset marks it '
    '"remote"; the other flags are unaffected'
)

#: The same for a rules segment (``segment:<id>``).
SEGMENT_RULES_UNREADABLE = (
    "its membership is unavailable, so the flags and experiments that use it "
    "are not evaluated; the other segments are unaffected"
)


def report_unreadable_rules(owner: str, exc: Exception, consequence: str) -> None:
    """Log, once per owner, that its stored rules raised when read (#1674).

    ``owner`` is ``flag:<key>`` or ``segment:<id>``; ``consequence`` is fixed
    text (:data:`FLAG_RULES_UNREADABLE`, :data:`SEGMENT_RULES_UNREADABLE`).
    The exception's type only, never its text, which can repeat the rule's
    content.
    """
    if EVALUATION_NOTES.first(owner, "stored rules unreadable"):
        logger.warning(
            "Cannot read the stored targeting rules for %s (%s); %s. Logged once.",
            owner,
            type(exc).__name__,
            consequence,
        )


def expand_context(context: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Flatten an SDK context into the lookup dict the rules engine expects.

    The result contains:

    * every raw key as given (nested dicts are kept as values too);
    * a dotted key for every nested dict entry, recursively
      (``{"app": {"version": "3.2.1"}}`` also answers ``app.version``);
    * ``user.<k>``, ``device.<k>`` and ``app.<k>`` aliases for every top-level
      key (``{"country": "US"}`` also answers ``user.country``; ``{"os_version":
      "17.4.0"}`` also answers ``device.os_version``);
    * the bare key for dotted keys under those prefixes (``user.country`` in
      the context also answers ``country``).

    Explicit keys always win over aliases, so ``{"country": "US", "user":
    {"country": "DE"}}`` resolves ``user.country`` to ``"DE"``.
    """
    if not context or not isinstance(context, dict):
        return {}

    flat: Dict[str, Any] = {}
    _flatten_into(flat, context, prefix="")

    # Forward aliases: top-level key -> <prefix>.key (nested dicts are already
    # reachable through their dotted keys, so they are not aliased themselves)
    for key, value in list(flat.items()):
        if "." in key or isinstance(value, dict):
            continue
        for prefix in CONTEXT_ALIAS_PREFIXES:
            flat.setdefault(f"{prefix}.{key}", value)

    # Reverse aliases: <prefix>.key -> key
    for key, value in list(flat.items()):
        head, sep, tail = key.partition(".")
        if sep and head in CONTEXT_ALIAS_PREFIXES and tail and "." not in tail:
            flat.setdefault(tail, value)

    return flat


def match_targeting_rule(
    rules: Optional[TargetingRules], context: Dict[str, Any]
) -> Optional[TargetingRule]:
    """
    Return the first rule (by priority) whose conditions match ``context``.

    Unlike :func:`backend.app.core.rules_engine.evaluate_targeting_rules` this
    does **not** apply the rule's ``rollout_percentage``: the caller buckets the
    user itself (the flag service hashes ``user_id:flag.key`` so a rule at N%
    selects the same users as the global rollout at N%). When nothing matches
    the ``default_rule`` is returned, mirroring the engine.

    :class:`backend.app.core.pattern_match.PatternUnevaluable` propagates
    unchanged: when a pattern condition cannot be evaluated the caller abandons
    the whole ruleset, and neither a later rule nor ``default_rule`` applies.
    :class:`backend.app.core.rules_engine.TimeWindowUnreadable` (a
    ``time_window`` that cannot be read) propagates in the same way.
    """
    if rules is None:
        return None
    for rule in sorted(rules.rules, key=lambda r: r.priority):
        if evaluate_rule(rule, context):
            return rule
    return rules.default_rule


# ---------------------------------------------------------------------------
# Strict validation of experiment and feature-flag targeting rules (on write)
# ---------------------------------------------------------------------------

#: Most items a list operator (``in``, ``not_in``, ``array_contains``,
#: ``array_intersects``) may carry.
MAX_LIST_ITEMS = 1000

_DASHBOARD_TOP_KEYS = frozenset(
    {"logical_operator", "groups", "rollout_percentage", "id"}
)
_DASHBOARD_GROUP_KEYS = frozenset({"id", "logical_operator", "conditions"})
_DASHBOARD_CONDITION_KEYS = frozenset({"id", "attribute", "operator", "value"})
_NATIVE_TOP_KEYS = frozenset(TargetingRules.model_fields)
_NATIVE_RULE_KEYS = frozenset(TargetingRule.model_fields)
_NATIVE_GROUP_KEYS = frozenset(RuleGroup.model_fields)
_NATIVE_CONDITION_KEYS = frozenset(Condition.model_fields)
_LOGICAL_OPERATOR_NAMES = frozenset({"and", "or", "not"})
_MAX_RULE_ID_LENGTH = 100

# RuleValidator's messages can carry the submitted value; each ERROR issue is
# reported as the fixed text of the first prefix its message starts with.
_RULE_VALIDATOR_CODES = (
    ("Invalid regex pattern", "pattern is not valid"),
    ("Invalid semantic version", "version is not valid"),
    ("GEO_DISTANCE requires", "location is not valid"),
    ("Invalid latitude", "latitude is out of range"),
    ("Invalid longitude", "longitude is out of range"),
    ("Coordinates must be numeric", "location is not valid"),
    ("Condition attribute is required", "attribute is required"),
    ("Rule nesting too deep", "rules are nested too deeply"),
    ("Duplicate rule IDs", "rule ids must be unique"),
    ("Rule ID is required", "rule id is required"),
    ("Invalid rollout percentage", "rollout percentage must be 0 to 100"),
)


class TargetingRulesError(ValueError):
    """Targeting rules refused on write.

    ``path`` locates the problem (``groups[0].conditions[1].operator``) and
    ``code`` says what it is. Both are fixed text: neither ever carries a
    value from the submitted rules, so the message is safe to return in a 422.

    ``subject`` names the rules in the message when ``path`` is empty, so the
    line reads on its own (``targeting rules: unknown key``). The experiment
    validator passes none, and its messages are unchanged.
    """

    def __init__(self, path: str, code: str, *, subject: str = "") -> None:
        self.path = path
        self.code = code
        where = path or subject
        super().__init__(f"{where}: {code}" if where else code)


#: What ``validate_flag_targeting`` calls the rules when a problem has no path.
FLAG_RULES_SUBJECT = "targeting rules"

_EXPERIMENT = "experiment"
_FLAG = "flag"


def validate_experiment_targeting(value: Any) -> Optional[TargetingRules]:
    """
    Refuse experiment ``targeting_rules`` that assignment would not apply as shown.

    The shape is decided by :func:`_is_dashboard_rules_shape`, the predicate
    experiment assignment uses. Dashboard rules are converted with
    ``_from_dashboard``, native rules with ``TargetingRules.model_validate``,
    and the result is checked by :class:`~backend.app.core.rule_validation.RuleValidator`
    (ERROR issues only).

    ``None``, ``{}`` and ``{"groups": []}`` mean "no rules" and are accepted.

    Returns:
        What assignment will evaluate: the ``TargetingRules``, or ``None``
        when the value carries no rules. The stored value is never changed.

    Raises:
        TargetingRulesError: with a fixed message keyed by path.
    """
    return _validate_targeting(value, kind=_EXPERIMENT)


def validate_flag_targeting(value: Any) -> Optional[TargetingRules]:
    """
    Refuse feature-flag ``targeting_rules`` the flag evaluator would not apply as shown.

    The same checks as :func:`validate_experiment_targeting`, with these
    differences, each because of how flags are evaluated:

    * a list (the legacy shape) is refused: ``a list of rules is not
      supported; use the groups shape``;
    * a top-level ``name`` on dashboard rules: ``name: not supported``;
    * native rules without ``rules``: ``rules: required``, even beside a
      ``default_rule``, which :func:`normalise_targeting_rules` does not read
      without ``rules``. It is checked after the unknown-key check, so a flat
      object still answers ``unknown key``;
    * a problem with no path reads ``targeting rules: <reason>``;
    * native rules: a key outside the schema is refused at every level --
      the rule (``rules[0]: unknown key``), its condition group
      (``rules[0].rule``), nested groups at any depth
      (``rules[0].rule.groups[1]``), conditions, and ``default_rule`` and
      ``default_rule.rule`` -- where the model would otherwise ignore it;
    * native groups nested more than 10 levels below ``rules[i].rule``
      (``<path>.groups: nested too deeply``), and native values with more
      than 1000 rules, groups and conditions together, are refused before
      the walk goes further;
    * dashboard rules with more than 1000 groups and conditions in all
      (each group is one, and each of its conditions one) are refused
      before any group is checked or converted: ``targeting rules: at most
      1000 groups and conditions are allowed``;
    * ``rollout_percentage``, on dashboard rules and on a native rule, must be
      an integer from 0 to 100. A float with no fractional part (``100.0``)
      is accepted, as the native model accepts it; ``33.5``, ``true`` and
      text are refused;
    * the operators ``json_path`` and ``percentage_bucket``, which flag
      evaluation does not implement, are refused (#1204; experiments accept
      them). Both validators refuse the other words the engine does not know,
      the values it cannot read and a dashboard geo comparison it ignores
      (:func:`_check_supported_vocabulary`; #1204, #1525, #1526, #1666,
      #1669, #1673), and an integer too large for a float that the shape
      step cannot read is refused as a value that is not a number
      (:func:`overflow_refusal`, #1672).

    For every value it accepts, what it returns is what
    :func:`normalise_targeting_rules` gives the flag evaluator (``None`` and
    an empty ``TargetingRules`` both meaning "no rules").

    Raises:
        TargetingRulesError: with a fixed message keyed by path.
    """
    try:
        return _validate_targeting(value, kind=_FLAG)
    except TargetingRulesError as err:
        if err.path:
            raise
        raise TargetingRulesError("", err.code, subject=FLAG_RULES_SUBJECT) from None


#: What :func:`validate_segment_rules` calls the rules when a problem has no path.
SEGMENT_RULES_SUBJECT = "rules"

#: Limits on one segment's rules. They are the audience preview's limits
#: (``backend.app.core.segment_preview_limits`` (a), (a'), (b) and (c)), so
#: every saved segment also passes the preview's static checks.
MAX_SEGMENT_GROUPS = 20
MAX_SEGMENT_CONDITIONS = 50
MAX_SEGMENT_REGEX_CONDITIONS = 10
MAX_SEGMENT_LIST_ELEMENTS = 1000

_SEGMENT_TOP_KEYS = frozenset({"logical_operator", "groups"})


def validate_segment_rules(value: Any) -> TargetingRules:
    """
    Refuse segment rules that membership would not evaluate as written.

    Segment rules use the dashboard shape that flag and experiment targeting
    use, ``{"logical_operator"?, "groups": [{"logical_operator"?,
    "conditions": [{"attribute", "operator", "value"}]}]}``, with the
    dashboard operators (``equals``, ``in``, ``semver_gte``, ...). Each
    condition gets the flag validator's checks. On top of those:

    * only ``logical_operator`` and ``groups`` may appear at the top level, so
      the legacy segment shape (``{"operator", "conditions"}``), the native
      shape (``{"rules": [...]}``), ``rollout_percentage`` and ``id`` all
      answer ``rules: unknown key``;
    * ``groups`` must be a non-empty list (``groups: at least one group is
      required``), and every group needs a condition: a segment with no
      condition would match every user;
    * at most :data:`MAX_SEGMENT_GROUPS` groups, :data:`MAX_SEGMENT_CONDITIONS`
      conditions, :data:`MAX_SEGMENT_REGEX_CONDITIONS` ``regex`` conditions
      and :data:`MAX_SEGMENT_LIST_ELEMENTS` list values in total.

    No database access. The value is never changed.

    Returns:
        The ``TargetingRules`` membership evaluates; never empty.

    Raises:
        TargetingRulesError: with a fixed message keyed by path, never
        carrying a submitted value.
    """
    try:
        return _validate_segment_rules(value)
    except TargetingRulesError as err:
        if err.path:
            raise
        raise TargetingRulesError("", err.code, subject=SEGMENT_RULES_SUBJECT) from None


def _validate_segment_rules(value: Any) -> TargetingRules:
    # Imported here: the counting module imports nothing from this one, and
    # only the write path and membership need it.
    from backend.app.core.segment_preview_limits import count_ruleset

    if not isinstance(value, dict):
        raise TargetingRulesError("", "must be an object")
    if any(key not in _SEGMENT_TOP_KEYS for key in value):
        raise TargetingRulesError("", "unknown key")
    groups = value.get("groups")
    if groups is None:
        raise TargetingRulesError("groups", "at least one group is required")
    if not isinstance(groups, list):
        raise TargetingRulesError("groups", "must be a list")
    if not groups:
        raise TargetingRulesError("groups", "at least one group is required")

    # Counted on the submitted value before anything is converted, so the
    # work below is bounded.
    counts = count_ruleset(value)
    if counts["groups"] > MAX_SEGMENT_GROUPS:
        raise TargetingRulesError(
            "groups", f"at most {MAX_SEGMENT_GROUPS} groups are allowed"
        )
    if counts["conditions"] > MAX_SEGMENT_CONDITIONS:
        raise TargetingRulesError(
            "", f"at most {MAX_SEGMENT_CONDITIONS} conditions are allowed"
        )
    if counts["regex_conditions"] > MAX_SEGMENT_REGEX_CONDITIONS:
        raise TargetingRulesError(
            "",
            f"at most {MAX_SEGMENT_REGEX_CONDITIONS} regex conditions are allowed",
        )
    if counts["list_elements"] > MAX_SEGMENT_LIST_ELEMENTS:
        raise TargetingRulesError("", _SEGMENT_LIST_LIMIT)

    rules = _validated_dashboard(value, _FLAG)
    # Membership of one segment is never decided by another's (#440).
    nested = next(_segment_conditions(rules, "groups"), None)
    if nested is not None:
        raise TargetingRulesError(
            _join(nested[0], "operator"), "a segment cannot refer to a segment"
        )
    # ``"beta, staff"`` is a list for ``in`` once converted, so the total
    # is counted again on what membership evaluates.
    if _converted_list_elements(rules) > MAX_SEGMENT_LIST_ELEMENTS:
        raise TargetingRulesError("", _SEGMENT_LIST_LIMIT)
    _check_list_sizes(rules, "groups")
    _check_rule_validator(rules, "groups")
    return rules


_SEGMENT_LIST_LIMIT = (
    f"at most {MAX_SEGMENT_LIST_ELEMENTS} list values are allowed in total"
)


def _converted_list_elements(rules: TargetingRules) -> int:
    total = 0
    stack = [rule.rule for rule in rules.rules]
    while stack:
        group = stack.pop()
        for condition in group.conditions:
            if isinstance(condition.value, (list, tuple, set)):
                total += len(condition.value)
        stack.extend(group.groups or [])
    return total


def _validate_targeting(value: Any, *, kind: str) -> Optional[TargetingRules]:
    """The checks both validators share; ``kind`` is ``"experiment"`` or ``"flag"``."""
    if value is None:
        return None
    if isinstance(value, list):
        if kind == _FLAG:
            raise TargetingRulesError(
                "", "a list of rules is not supported; use the groups shape"
            )
        raise TargetingRulesError(
            "", "a list of rules is not supported for experiments"
        )
    if not isinstance(value, dict):
        raise TargetingRulesError("", "must be an object")
    if not value:
        return None

    # An integer too large for a float makes the shape step raise
    # OverflowError (#1672); it is answered as a value that is not a number,
    # around the shape step only (the read paths keep their behaviour).
    if _is_dashboard_rules_shape(value):
        groups_base = "groups"
        try:
            rules = _validated_dashboard(value, kind)
        except OverflowError:
            raise overflow_refusal(value, dashboard=True) from None
    else:
        groups_base = None
        try:
            rules = _validated_native(value, kind)
        except OverflowError:
            raise overflow_refusal(value, dashboard=False) from None

    if not rules.rules and rules.default_rule is None:
        return None
    _check_list_sizes(rules, groups_base)
    _check_segment_conditions(rules, groups_base)
    _check_rule_validator(rules, groups_base)
    # Last, so a value refused before #1204 keeps its message.
    _check_supported_vocabulary(rules, groups_base, kind, value)
    return rules


#: The answer for a dashboard condition the converter cannot read, and for a
#: native condition the ``Condition`` model refuses.
_DASHBOARD_VALUE_NOT_VALID = "value is not valid for the operator"
_NATIVE_NOT_VALID = "not valid"


def overflow_refusal(
    raw: Any, *, dashboard: bool, subject: str = ""
) -> TargetingRulesError:
    """The refusal for submitted rules whose shape step raised
    ``OverflowError`` (#1672).

    ``float()`` raises ``OverflowError`` for an integer too large for a
    float, such as a 400-digit JSON integer, and neither the dashboard
    converter (``_geo_value``) nor the ``Condition`` model catches it, so a
    geo coordinate, or a dashboard radius, that large answered 500. Only the
    write paths turn it into this: :func:`_validate_targeting` around
    :func:`_validated_dashboard` and :func:`_validated_native`, and the
    segment schema around :func:`validate_segment_rules`. Those two are also
    read gates, and are not changed: stored rules answer as before.

    The answer is the one a value that is not a number gets at the same
    place, at the first condition in document order that cannot be read
    (a group's conditions, then its nested groups; ``rules``, then
    ``default_rule``):

    * dashboard (``raw["groups"]``): the first condition whose conversion
      overflows, ``groups[i].conditions[j].value: value is not valid for the
      operator``. The converter stops at the first condition it cannot
      convert, so no earlier condition failed;
    * native: the first condition the ``Condition`` model refuses or
      overflows on, ``<path>.conditions[k]: not valid``, the model's own
      answer for a coordinate that is not a number. The model goes on past a
      condition it refuses, so that condition comes before a later one that
      overflowed.

    When no condition is found the path is empty (``not valid``, read with
    ``subject``). The submitted value is read with type checks and an
    explicit stack, and nothing here raises.
    """
    if dashboard:
        path, code = _first_dashboard_overflow(raw), _DASHBOARD_VALUE_NOT_VALID
    else:
        path, code = _first_native_refusal(raw), _NATIVE_NOT_VALID
    if path is None:
        return TargetingRulesError("", _NATIVE_NOT_VALID, subject=subject)
    return TargetingRulesError(path, code, subject=subject)


def _first_dashboard_overflow(raw: Any) -> Optional[str]:
    groups = raw.get("groups") if isinstance(raw, dict) else None
    if not isinstance(groups, list):
        return None
    for gi, group in enumerate(groups):
        conditions = group.get("conditions") if isinstance(group, dict) else None
        if not isinstance(conditions, list):
            continue
        for ci, condition in enumerate(conditions):
            try:
                convert_dashboard_condition(condition)
            except OverflowError:
                return f"groups[{gi}].conditions[{ci}].value"
            except Exception:  # refused another way: not this one
                continue
    return None


def _first_native_refusal(raw: Any) -> Optional[str]:
    if not isinstance(raw, dict):
        return None
    roots: List[Tuple[Any, str]] = []
    rules = raw.get("rules")
    if isinstance(rules, list):
        roots.extend(
            (rule.get("rule"), f"rules[{index}].rule")
            for index, rule in enumerate(rules)
            if isinstance(rule, dict)
        )
    default_rule = raw.get("default_rule")
    if isinstance(default_rule, dict):
        roots.append((default_rule.get("rule"), "default_rule.rule"))
    for root, root_path in roots:
        stack = [(root, root_path)]
        while stack:
            group, path = stack.pop()
            if not isinstance(group, dict):
                continue
            conditions = group.get("conditions")
            for ci, condition in enumerate(
                conditions if isinstance(conditions, list) else ()
            ):
                try:
                    Condition.model_validate(condition)
                except Exception:  # ValidationError, or the OverflowError
                    return f"{path}.conditions[{ci}]"
            children = group.get("groups")
            if isinstance(children, list):
                stack.extend(
                    (child, f"{path}.groups[{gi}]")
                    for gi, child in reversed(list(enumerate(children)))
                )
    return None


def _validated_dashboard(raw: Dict[str, Any], kind: str) -> TargetingRules:
    if "name" in raw:
        # Read by no evaluator, and the builder cannot keep it.
        if kind == _FLAG:
            raise TargetingRulesError("name", "not supported")
        raise TargetingRulesError("name", "not supported for experiment rules")
    if any(key not in _DASHBOARD_TOP_KEYS for key in raw):
        raise TargetingRulesError("", "unknown key")
    if "groups" not in raw:
        raise TargetingRulesError("logical_operator", "given without groups")
    _check_logical_operator(raw, "logical_operator")
    if "rollout_percentage" in raw:
        percentage = raw["rollout_percentage"]
        if kind == _FLAG:
            _check_flag_percentage(percentage, "rollout_percentage")
        elif (
            isinstance(percentage, bool)
            or not isinstance(percentage, (int, float))
            or not 0 <= percentage <= 100
        ):
            raise TargetingRulesError(
                "rollout_percentage", "must be a number from 0 to 100"
            )
    if "id" in raw:
        rule_id = raw["id"]
        if (
            not isinstance(rule_id, str)
            or not rule_id.strip()
            or len(rule_id) > _MAX_RULE_ID_LENGTH
        ):
            raise TargetingRulesError(
                "id", f"must be text of 1 to {_MAX_RULE_ID_LENGTH} characters"
            )

    groups = raw["groups"]
    if not isinstance(groups, list):
        raise TargetingRulesError("groups", "must be a list")
    if kind == _FLAG:
        _check_dashboard_flag_size(groups)
    for gi, group in enumerate(groups):
        gpath = f"groups[{gi}]"
        if not isinstance(group, dict):
            raise TargetingRulesError(gpath, "must be an object")
        if any(key not in _DASHBOARD_GROUP_KEYS for key in group):
            raise TargetingRulesError(gpath, "unknown key")
        _check_logical_operator(group, f"{gpath}.logical_operator")
        conditions = group.get("conditions")
        if not isinstance(conditions, list) or not conditions:
            raise TargetingRulesError(
                f"{gpath}.conditions", "at least one condition is required"
            )
        for ci, condition in enumerate(conditions):
            cpath = f"{gpath}.conditions[{ci}]"
            if not isinstance(condition, dict):
                raise TargetingRulesError(cpath, "must be an object")
            if any(key not in _DASHBOARD_CONDITION_KEYS for key in condition):
                raise TargetingRulesError(cpath, "unknown key")
            attribute = condition.get("attribute")
            if not isinstance(attribute, str) or not attribute.strip():
                raise TargetingRulesError(f"{cpath}.attribute", "attribute is required")
            operator = condition.get("operator")
            if (
                not isinstance(operator, str)
                or operator.strip() not in DASHBOARD_OPERATORS
            ):
                raise TargetingRulesError(f"{cpath}.operator", "unknown operator")

    try:
        return _from_dashboard(raw)
    except (TargetingRuleShapeError, ValidationError, TypeError, ValueError):
        pass
    # ``_from_dashboard`` refused it. Find the condition, for the path only:
    # the decision above is the converter's own.
    for gi, group in enumerate(groups):
        for ci, condition in enumerate(group["conditions"]):
            cpath = f"groups[{gi}].conditions[{ci}]"
            try:
                convert_dashboard_condition(condition)
            except ValidationError as exc:
                if any("attribute" in error.get("loc", ()) for error in exc.errors()):
                    raise TargetingRulesError(
                        f"{cpath}.attribute",
                        "attribute may contain only letters, digits, underscores and dots",
                    ) from None
                raise TargetingRulesError(
                    f"{cpath}.value", "value is not valid for the operator"
                ) from None
            except (TargetingRuleShapeError, TypeError, ValueError):
                raise TargetingRulesError(
                    f"{cpath}.value", "value is not valid for the operator"
                ) from None
    raise TargetingRulesError("", "rules could not be converted")


def _check_dashboard_flag_size(groups: List[Any]) -> None:
    """Refuse dashboard flag rules with more than :data:`MAX_NATIVE_NODES`
    groups and conditions in all, before any group is checked or converted.

    Counted in the builder's units: each item of ``groups`` is one, and each
    item of a group's ``conditions`` list is one. A group that is not an
    object, or whose ``conditions`` is not a list, adds only itself; the
    checks after this one refuse it.
    """
    count = len(groups)
    for group in groups:
        if isinstance(group, dict):
            conditions = group.get("conditions")
            if isinstance(conditions, list):
                count += len(conditions)
    if count > MAX_NATIVE_NODES:
        raise TargetingRulesError(
            "", f"at most {MAX_NATIVE_NODES} groups and conditions are allowed"
        )


def _validated_native(raw: Dict[str, Any], kind: str) -> TargetingRules:
    if "groups" in raw:
        raise TargetingRulesError("groups", "cannot be combined with rules")
    if any(key not in _NATIVE_TOP_KEYS for key in raw):
        raise TargetingRulesError("", "unknown key")
    if "rules" not in raw and (kind == _FLAG or "default_rule" not in raw):
        # The flag evaluator reads native rules only when ``rules`` is present
        # (``normalise_targeting_rules``); a lone ``default_rule`` is ignored.
        raise TargetingRulesError("rules", "required")
    if kind == _FLAG:
        _check_native_flag_rules(raw)
    try:
        return TargetingRules.model_validate(raw)
    except ValidationError as exc:
        # The location only, made of field names and indices; never the text.
        loc = exc.errors()[0].get("loc", ()) if exc.errors() else ()
        path = "".join(
            f"[{part}]" if isinstance(part, int) else f".{part}" for part in loc
        ).lstrip(".")
        if any(
            not isinstance(part, int) and part not in _NATIVE_PATH_WORDS for part in loc
        ):
            path = ""
        raise TargetingRulesError(path, "not valid") from None


#: Words that may appear in a native path: the schema's own field names. A loc
#: part outside this set would be a submitted key and is not reported.
_NATIVE_PATH_WORDS = frozenset(
    {
        "version",
        "rules",
        "default_rule",
        "id",
        "name",
        "description",
        "rule",
        "rollout_percentage",
        "priority",
        "operator",
        "conditions",
        "groups",
        "attribute",
        "value",
        "additional_value",
        "attribute_type",
        "validation_schema",
    }
)


def _check_flag_percentage(value: Any, path: str) -> None:
    """A flag's rollout percentage is an integer from 0 to 100.

    ``100.0`` is accepted (the native model reads it as ``100``); ``33.5``
    would be applied as 33 and ``true`` as 1, so both are refused, as is text.
    """
    whole = isinstance(value, int) or (isinstance(value, float) and value.is_integer())
    if isinstance(value, bool) or not whole or not 0 <= value <= 100:
        raise TargetingRulesError(path, "must be an integer from 0 to 100")


#: Deepest nesting of native groups a flag may carry: ``rules[i].rule`` is
#: level 0. The same limit as ``RuleValidator.max_rule_depth``.
MAX_NATIVE_GROUP_DEPTH = 10
#: Most rules, groups and conditions one native value may carry, counted
#: together; also the most groups and conditions dashboard flag rules may
#: carry (:func:`_check_dashboard_flag_size`).
MAX_NATIVE_NODES = 1000


def _check_native_flag_rules(raw: Dict[str, Any]) -> None:
    """Refuse a key outside the schema at every native level, for flags.

    ``TargetingRules.model_validate`` ignores unknown nested keys, so a
    misspelt ``rollout_percentage`` or ``priority`` would take its default and
    a misspelt ``conditions`` would leave an empty group. Paths are built from
    indices and schema words only; a value of the wrong type is left to the
    model, which reports it.

    The walk is bounded before any per-level path is built: groups nested
    more than :data:`MAX_NATIVE_GROUP_DEPTH` levels below ``rules[i].rule``
    are refused (``<path>.groups: nested too deeply``), and so is a value
    with more than :data:`MAX_NATIVE_NODES` rules, groups and conditions
    together. Groups are walked with an explicit stack, not by recursion.
    """
    count = 0

    def counted(more: int) -> None:
        nonlocal count
        count += more
        if count > MAX_NATIVE_NODES:
            raise TargetingRulesError(
                "",
                f"at most {MAX_NATIVE_NODES} rules, groups and conditions are allowed",
            )

    rules = raw.get("rules")
    rules = rules if isinstance(rules, list) else []
    counted(len(rules) + ("default_rule" in raw))

    def each_rule():
        for index, rule in enumerate(rules):
            yield f"rules[{index}]", rule
        if "default_rule" in raw:
            yield "default_rule", raw["default_rule"]

    for path, rule in each_rule():
        if not isinstance(rule, dict):
            continue
        if any(key not in _NATIVE_RULE_KEYS for key in rule):
            raise TargetingRulesError(path, "unknown key")
        if "rollout_percentage" in rule:
            _check_flag_percentage(
                rule["rollout_percentage"], f"{path}.rollout_percentage"
            )
        # Depth first, in document order: a group, its conditions, then its
        # nested groups.
        counted(1)
        stack: List[Tuple[str, Any, int]] = [(f"{path}.rule", rule.get("rule"), 0)]
        while stack:
            gpath, group, depth = stack.pop()
            if not isinstance(group, dict):
                continue
            if any(key not in _NATIVE_GROUP_KEYS for key in group):
                raise TargetingRulesError(gpath, "unknown key")
            conditions = group.get("conditions")
            if isinstance(conditions, list):
                counted(len(conditions))
                for ci, condition in enumerate(conditions):
                    if isinstance(condition, dict) and any(
                        key not in _NATIVE_CONDITION_KEYS for key in condition
                    ):
                        raise TargetingRulesError(
                            f"{gpath}.conditions[{ci}]", "unknown key"
                        )
            nested = group.get("groups")
            if isinstance(nested, list) and nested:
                if depth >= MAX_NATIVE_GROUP_DEPTH:
                    raise TargetingRulesError(f"{gpath}.groups", "nested too deeply")
                counted(len(nested))
                stack += [
                    (f"{gpath}.groups[{gi}]", child, depth + 1)
                    for gi, child in reversed(list(enumerate(nested)))
                ]


def _check_logical_operator(container: Dict[str, Any], path: str) -> None:
    if "logical_operator" not in container or container["logical_operator"] is None:
        return
    operator = container["logical_operator"]
    if (
        not isinstance(operator, str)
        or operator.strip().lower() not in _LOGICAL_OPERATOR_NAMES
    ):
        raise TargetingRulesError(path, "must be and, or or not")


def _rule_base(
    rules: TargetingRules, rule_id: Optional[str], groups_base: Optional[str]
) -> str:
    """Path of the rule an issue belongs to, in the submitted value's terms."""
    if groups_base is not None:
        return ""  # dashboard: one rule, its groups are the submitted groups
    for index, rule in enumerate(rules.rules):
        if rule.id == rule_id:
            return f"rules[{index}].rule"
    if rules.default_rule is not None and rules.default_rule.id == rule_id:
        return "default_rule.rule"
    return ""


def _join(base: str, tail: Optional[str]) -> str:
    tail = (tail or "").lstrip(".")
    if base and tail:
        return f"{base}.{tail}"
    return base or tail


def _check_list_sizes(rules: TargetingRules, groups_base: Optional[str]) -> None:
    def walk(group: RuleGroup, path: str) -> None:
        for ci, condition in enumerate(group.conditions):
            if (
                condition.operator in _LIST_OPERATORS
                and isinstance(condition.value, (list, tuple, set))
                and len(condition.value) > MAX_LIST_ITEMS
            ):
                raise TargetingRulesError(
                    _join(path, f"conditions[{ci}].value"),
                    f"at most {MAX_LIST_ITEMS} values are allowed",
                )
        for gi, nested in enumerate(group.groups or []):
            walk(nested, _join(path, f"groups[{gi}]"))

    for index, rule in enumerate(rules.rules):
        walk(rule.rule, "" if groups_base is not None else f"rules[{index}].rule")
    if rules.default_rule is not None:
        walk(rules.default_rule.rule, "default_rule.rule")


def is_segment_id(value: Any) -> bool:
    """A segment id as stored: canonical lowercase hyphenated UUID text."""
    if not isinstance(value, str) or len(value) != 36:
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _segment_conditions(
    rules: TargetingRules, groups_base: Optional[str]
) -> Iterator[Tuple[str, Condition]]:
    """Every segment condition in ``rules`` with its path, ``default_rule`` last.

    Paths are in the submitted value's terms, as for the other checks: for
    the dashboard shape ``groups[i].conditions[j]``, for the native shape
    ``rules[i].rule...`` and ``default_rule.rule...``. Groups are walked with
    an explicit stack.
    """
    roots = [
        (rule.rule, "" if groups_base is not None else f"rules[{index}].rule")
        for index, rule in enumerate(rules.rules)
    ]
    if rules.default_rule is not None:
        roots.append((rules.default_rule.rule, "default_rule.rule"))
    for root, root_path in roots:
        stack = [(root, root_path)]
        while stack:
            group, path = stack.pop()
            for ci, condition in enumerate(group.conditions):
                if condition.operator in SEGMENT_OPERATORS:
                    yield _join(path, f"conditions[{ci}]"), condition
            stack.extend(
                (child, _join(path, f"groups[{gi}]"))
                for gi, child in reversed(list(enumerate(group.groups or [])))
            )


def segment_ids_in(rules: Optional[TargetingRules]) -> frozenset:
    """The segment ids ``rules`` reference, ``default_rule`` included.

    Only text values are collected; a stored value that is not text cannot
    be a segment id, and :func:`~backend.app.core.rules_engine.segment_condition_holds`
    refuses it when it is evaluated.
    """
    if rules is None:
        return frozenset()
    return frozenset(
        condition.value
        for _path, condition in _segment_conditions(rules, None)
        if isinstance(condition.value, str)
    )


def _check_segment_conditions(
    rules: TargetingRules, groups_base: Optional[str]
) -> None:
    """Refuse a segment condition the evaluator would not apply as written.

    * the attribute must be ``segment`` and the value a segment id (the
      canonical text of a UUID);
    * a segment condition in a native ``default_rule`` is refused: the
      evaluator returns ``default_rule`` without evaluating its conditions,
      so the condition would mean "every user";
    * at most :data:`MAX_REFERENCED_SEGMENTS` distinct segments.

    Whether each segment exists and is active is checked against the
    database by the routes that write rules
    (``backend.app.services.segment_membership.segment_reference_problem``).
    """
    referenced = set()
    for path, condition in _segment_conditions(rules, groups_base):
        if path.startswith("default_rule"):
            raise TargetingRulesError(
                _join(path, "operator"),
                "a segment condition is not evaluated in default_rule",
            )
        if condition.attribute != SEGMENT_ATTRIBUTE:
            raise TargetingRulesError(
                _join(path, "attribute"), 'must be "segment" for this operator'
            )
        if not is_segment_id(condition.value):
            raise TargetingRulesError(_join(path, "value"), "must be a segment id")
        referenced.add(condition.value)
    if len(referenced) > MAX_REFERENCED_SEGMENTS:
        raise TargetingRulesError(
            "", f"at most {MAX_REFERENCED_SEGMENTS} segments may be referenced"
        )


def _check_rule_validator(rules: TargetingRules, groups_base: Optional[str]) -> None:
    # Imported here: rule_validation is only needed on the write path.
    from backend.app.core.rule_validation import RuleValidator, ValidationSeverity

    result = RuleValidator().validate_targeting_rules(rules)
    for issue in result.issues:
        if issue.severity != ValidationSeverity.ERROR:
            continue
        code = next(
            (
                fixed
                for prefix, fixed in _RULE_VALIDATOR_CODES
                if issue.message.startswith(prefix)
            ),
            "not valid",
        )
        path = _join(
            _rule_base(rules, issue.rule_id, groups_base), issue.condition_path
        )
        raise TargetingRulesError(path, code)


# ---------------------------------------------------------------------------
# Words the evaluator never matches (#1204)
# ---------------------------------------------------------------------------

#: The reason for each value :func:`_unreadable_value` refuses (#1525, #1526):
#: plan D211 v1.4-B3's texts, with the principal engineer's wording for a
#: number. The same limits as the words' reasons below apply.
DATE_VALUE_REASON = (
    "not a date; use an ISO 8601 date such as 2030-01-31 or 2030-01-31T09:00:00Z, "
    "or a Unix timestamp"
)
LENGTH_VALUE_REASON = "not a whole number; use a number such as 3"
LENGTH_BOUNDS_REASON = "between needs a value with min and max, both whole numbers"
RADIUS_REASON = "radius must be a number of 0 or more"
NO_RADIUS_REASON = "a radius is required; use a number of 0 or more"
PERCENTAGE_REASON = "not a number; use a percentage from 0 to 100"
NUMBER_REASON = "not a number; use a number such as 5"
DASHBOARD_COMPARISON_REASON = (
    "comparison is not read in the dashboard shape, so the rule would mean within "
    "the radius; to use gt, lt, gte or eq, write the rule in the native shape"
)
#: A ``between`` bound neither of its paths reads (#1666), and two dates one of
#: which has a time zone; and a dashboard window the engine cannot read
#: (#1669). Plan D211 v1.4-B3-1666's texts.
BETWEEN_VALUE_REASON = (
    "not a number or a date; between needs two numbers such as 18 and 65 (text "
    "must be digits, with at most one dot), or two dates, each ISO 8601 such as "
    "2030-01-31 or a Unix timestamp in seconds"
)
BETWEEN_ZONE_REASON = (
    "one bound has a time zone and the other does not, so no date is ever between "
    "them; give both a zone, such as Z, or neither (a Unix timestamp has none)"
)
DASHBOARD_TIME_WINDOW_REASON = (
    "unreadable time window; give start and end as HH:MM such as 09:00 and 17:00, "
    "days as numbers 0 to 6, start_date and end_date as YYYY-MM-DD in order, a "
    "timezone as a name such as Europe/Paris"
)
VALUE_REASONS = (
    DATE_VALUE_REASON,
    LENGTH_VALUE_REASON,
    LENGTH_BOUNDS_REASON,
    RADIUS_REASON,
    NO_RADIUS_REASON,
    PERCENTAGE_REASON,
    NUMBER_REASON,
    DASHBOARD_COMPARISON_REASON,
    BETWEEN_VALUE_REASON,
    BETWEEN_ZONE_REASON,
    DASHBOARD_TIME_WINDOW_REASON,
)

#: The reason for each word :func:`_check_supported_vocabulary` refuses. Each is
#: one line of at most 200 characters, the most the dashboard shows as one
#: condition's problem (``describeTargetingIssue``), and none repeats a value.
NOT_ON_FLAGS_REASON = (
    "not supported for feature flags; json_path and percentage_bucket are for "
    "experiments only"
)
VERSION_COMPARISON_REASON = (
    "unknown version comparison; use eq, gt, gte, lt or lte, or leave it out for eq"
)
LENGTH_COMPARISON_REASON = (
    "unknown length comparison; use eq, gt, gte, lt, lte or between, or leave it "
    "out for eq"
)
DISTANCE_COMPARISON_REASON = (
    "unknown distance comparison; use lte, lt, gte, gt or eq, or leave it out for lte"
)
DISTANCE_UNIT_REASON = "unknown distance unit; use km, mi or m, or leave it out for km"
TIME_WINDOW_REASON = (
    "unreadable time window; give days (0 to 6), start_time and end_time "
    "(HH:MM), or start_date and end_date (YYYY-MM-DD); a timezone must be a "
    "name such as Europe/Paris"
)
VOCABULARY_REASONS = (
    NOT_ON_FLAGS_REASON,
    VERSION_COMPARISON_REASON,
    LENGTH_COMPARISON_REASON,
    DISTANCE_COMPARISON_REASON,
    DISTANCE_UNIT_REASON,
    TIME_WINDOW_REASON,
)


def _silent(_operator: Any, _reason: str) -> None:
    """A ``note`` for ``check_time_window`` that logs nothing (a save is not an
    evaluation)."""


#: Every reason the walker gives, the values' first: the words' limits hold
#: for both.
VOCABULARY_REASONS = VALUE_REASONS + VOCABULARY_REASONS


def _known_comparison(word: Any, known: frozenset) -> bool:
    """Whether the engine reads ``word`` as one of ``known``: a false-y word is
    read as ``eq``, any other must be one of the strings."""
    return not word or (isinstance(word, str) and word in known)


def _carries_a_comparison(raw_condition: Any) -> bool:
    """Whether a dashboard ``geo_within_radius`` condition, as submitted,
    carries a comparison the converter drops (#1526).

    ``_geo_value`` reads ``lat``, ``lon``, ``radius`` and ``unit`` only, so
    the rule means "within the radius" whatever else it says. That is: in
    the object, a ``comparison`` other than absent, ``null`` or ``"lte"``;
    in the list or the text, any fifth item or part, even an empty one.
    """
    value = raw_condition.get("value") if isinstance(raw_condition, dict) else None
    if isinstance(value, dict):
        return value.get("comparison") not in (None, "lte")
    if isinstance(value, str):
        return len(value.split(",")) > 4
    if isinstance(value, (list, tuple)):
        return len(value) > 4
    return False


def _unreadable_value(
    condition: Condition, dashboard: bool, raw_condition: Any
) -> Optional[Tuple[str, str]]:
    """``(field, reason)`` for a value in ``condition`` its evaluator cannot
    read (#1525, #1666, #1673), or a dashboard geo comparison it ignores
    (#1526); ``None`` otherwise. Run after :func:`_unsupported_word` found
    nothing, so a word is known when its value is looked at.

    ``raw_condition`` is the condition as submitted, for the dashboard shape
    (``None`` for the native shape): the converter drops a geo comparison,
    so only the submitted value shows it. Each value is judged by the
    ``rules_engine`` predicate that mirrors its branch. ``gt``, ``gte``,
    ``lt`` and ``lte`` are judged in both shapes, on the converted value:
    the dashboard converter refuses text that is not a number, but passes an
    integer too large for a float and NaN, which are refused here (#1673).
    ``between`` exists in the native shape only.
    """
    operator = condition.operator
    value = condition.value
    extra = condition.additional_value
    if operator in (OperatorType.BEFORE, OperatorType.AFTER):
        if not date_value_readable(value):
            return "value", DATE_VALUE_REASON
    elif operator == OperatorType.ARRAY_LENGTH:
        if extra == "between":
            if not length_bounds_readable(value):
                return "value", LENGTH_BOUNDS_REASON
        elif not length_value_readable(value):
            return "value", LENGTH_VALUE_REASON
    elif operator == OperatorType.GEO_DISTANCE:
        if dashboard and _carries_a_comparison(raw_condition):
            return "value", DASHBOARD_COMPARISON_REASON
        # The engine reads the radius from an object's ``radius``, or takes
        # additional_value itself as the radius.
        field = "value" if dashboard else "additional_value"
        if isinstance(extra, dict):
            if "radius" not in extra:
                return field, NO_RADIUS_REASON
            extra = extra["radius"]
        if not radius_readable(extra):
            return field, RADIUS_REASON
    elif operator == OperatorType.PERCENTAGE_BUCKET:
        if not percentage_value_readable(value):
            return "value", PERCENTAGE_REASON
    elif operator in _NUMERIC_OPERATORS:
        if not number_value_readable(value):
            return "value", NUMBER_REASON
    elif operator == OperatorType.BETWEEN:
        problem = between_bounds_problem(value, extra)
        if problem == "time zone":
            return "additional_value", BETWEEN_ZONE_REASON
        if problem is not None:
            return problem, BETWEEN_VALUE_REASON
    return None


def _unsupported_word(
    condition: Condition, kind: str, dashboard: bool
) -> Optional[Tuple[str, str]]:
    """``(field, reason)`` for a word in ``condition`` its evaluator never
    matches, or ``None``. ``field`` is the condition's key the path ends in."""
    operator = condition.operator
    extra = condition.additional_value
    if kind == _FLAG and operator in OPERATORS_NOT_ON_FLAGS:
        return "operator", NOT_ON_FLAGS_REASON
    if operator == OperatorType.SEMANTIC_VERSION and not _known_comparison(
        extra, SEMVER_COMPARISONS
    ):
        return "additional_value", VERSION_COMPARISON_REASON
    if operator == OperatorType.ARRAY_LENGTH and not _known_comparison(
        extra, ARRAY_LENGTH_COMPARISONS
    ):
        return "additional_value", LENGTH_COMPARISON_REASON
    if operator == OperatorType.GEO_DISTANCE and isinstance(extra, dict):
        # The engine reads an absent or null comparison as lte, and an absent
        # unit as km; an explicit null unit matches nothing. The dashboard
        # converter keeps only the unit, which the submitted value carries.
        comparison = extra.get("comparison")
        if comparison is not None and not (
            isinstance(comparison, str) and comparison in GEO_COMPARISONS
        ):
            return "additional_value", DISTANCE_COMPARISON_REASON
        if "unit" in extra:
            unit = extra["unit"]
            if not isinstance(unit, str) or unit.lower() not in GEO_UNITS:
                return ("value" if dashboard else "additional_value"), (
                    DISTANCE_UNIT_REASON
                )
    if operator == OperatorType.TIME_WINDOW:
        # The engine's own check, so the two cannot drift (#1204), in both
        # shapes (#1669). ``condition`` is the converted one, and for the
        # dashboard shape its value is the window every reader evaluates:
        # ``_time_window_value`` copies the submitted window and adds
        # ``start_time``/``end_time``, so a submitted ``start_time`` is read,
        # not the ``start`` beside it.
        try:
            check_time_window(operator, condition.value, note=_silent)
        except TimeWindowUnreadable:
            return "value", (
                DASHBOARD_TIME_WINDOW_REASON if dashboard else TIME_WINDOW_REASON
            )
    return None


def _check_supported_vocabulary(
    rules: TargetingRules, groups_base: Optional[str], kind: str, raw: Any
) -> None:
    """Refuse a word the evaluator that will read the rules never matches (#1204).

    Each condition of ``rules`` and of ``default_rule``, at any depth and
    inside ``NOT`` too, in document order (a group's conditions, then its
    nested groups):

    * on a flag (``kind`` ``"flag"``), the operators ``json_path`` and
      ``percentage_bucket``, which flag evaluation does not implement
      (``rules_engine.OPERATORS_NOT_ON_FLAGS``); experiments implement both;
    * a ``semantic_version`` or ``array_length`` ``additional_value`` outside
      ``SEMVER_COMPARISONS`` or ``ARRAY_LENGTH_COMPARISONS`` (a false-y one is
      read as ``eq``);
    * a ``geo_distance`` ``comparison`` outside ``GEO_COMPARISONS`` (absent or
      ``null`` is ``lte``), and a ``unit`` that is not text in ``GEO_UNITS``
      once lowercased (absent is ``km``; an explicit ``null`` matches nothing);
    * a ``time_window`` value ``rules_engine.check_time_window`` cannot read,
      in either shape: for the dashboard shape (``groups_base`` set), the
      converted window, with its own reason (#1669);
    * then, for the same condition, a value the evaluator cannot read
      (#1525, :func:`_unreadable_value`): a ``before`` or ``after`` value that
      is not a date, an ``array_length`` value that is not a whole number (or,
      for ``between``, not ``min`` and ``max``), a ``geo_distance`` radius
      that is missing, below 0 or not a number, a ``percentage_bucket`` value
      that is not a number, a ``gt``, ``gte``, ``lt`` or ``lte`` value that is
      not a number (in the dashboard shape, an integer too large for a float
      or NaN, which the converter passes, #1673), and a native ``between``
      whose bounds are not two numbers or two dates with the same time-zone
      form (#1666); and a dashboard ``geo_within_radius`` that carries a
      comparison, which the converter drops (#1526).

    Such a condition is accepted by the schema and never holds, holds where
    it says it does not (a dashboard comparison), or abandons the whole
    ruleset (the window, a timestamp no date can hold, an integer no float
    can hold). The path is in the submitted value's terms:
    ``rules[i].rule.conditions[k].operator`` (or ``.additional_value``,
    ``.value``) for the native shape, ``groups[i].conditions[j].value`` for
    the dashboard shape.

    ``raw`` is the value as submitted. For the dashboard shape, the
    submitted condition at ``groups[i].conditions[j]`` is read beside the
    converted rule's condition at the same place: the converter maps each
    condition to one condition in the same place, and
    :func:`_validated_dashboard` has refused a group with no condition, the
    one thing it skips. The native shape ignores ``raw``.

    Called from :func:`_validate_targeting` and, for segments,
    :func:`check_segment_vocabulary`, both on the write path only. Never from
    :func:`validate_segment_rules` or :func:`_check_rule_validator`: those
    also gate reads, and a stored segment that failed there would become
    unavailable, every flag naming it answering ``error``.
    """
    dashboard = groups_base is not None
    roots = [
        (rule.rule, "" if dashboard else f"rules[{index}].rule")
        for index, rule in enumerate(rules.rules)
    ]
    if rules.default_rule is not None:
        roots.append((rules.default_rule.rule, "default_rule.rule"))
    for root, root_path in roots:
        stack = [(root, root_path, raw if dashboard else None)]
        while stack:
            group, path, raw_group = stack.pop()
            for ci, condition in enumerate(group.conditions):
                problem = _unsupported_word(
                    condition, kind, dashboard
                ) or _unreadable_value(
                    condition, dashboard, _submitted(raw_group, "conditions", ci)
                )
                if problem is not None:
                    field, reason = problem
                    raise TargetingRulesError(
                        _join(path, f"conditions[{ci}].{field}"), reason
                    )
            stack.extend(
                (
                    child,
                    _join(path, f"groups[{gi}]"),
                    _submitted(raw_group, "groups", gi),
                )
                for gi, child in reversed(list(enumerate(group.groups or [])))
            )


def _submitted(raw_group: Any, key: str, index: int) -> Any:
    """``raw_group[key][index]`` of the submitted dashboard value, or ``None``
    (always, for the native shape, which passes no submitted group)."""
    items = raw_group.get(key) if isinstance(raw_group, dict) else None
    if isinstance(items, list) and 0 <= index < len(items):
        return items[index]
    return None


def check_segment_vocabulary(rules: TargetingRules, raw: Any) -> None:
    """Refuse segment rules with a word membership never matches (#1204), or a
    value it cannot read (#1525, #1526, #1669, #1673).

    ``rules`` are what :func:`validate_segment_rules` returned for ``raw``,
    the rules as submitted or stored. Segment rules are in the dashboard
    shape and evaluated as a flag's are, so what can be refused is a
    ``geo_within_radius`` with a distance unit membership does not know, a
    comparison (which the converter drops) or a radius below 0 or not a
    number; a ``time_window`` the engine cannot read; and a
    ``greater_than``, ``greater_than_or_equal``, ``less_than`` or
    ``less_than_or_equal`` value that is an integer too large for a float, or
    NaN. Called on a segment's write path (``schemas.segment``) and by
    ``check_targeting_rules``; never from :func:`validate_segment_rules`,
    which also decides at read time whether a stored segment can be
    evaluated.

    Raises:
        TargetingRulesError: with a fixed message keyed by path.
    """
    _check_supported_vocabulary(rules, "groups", _FLAG, raw)


# ---------------------------------------------------------------------------
# Dashboard shape conversion
# ---------------------------------------------------------------------------


def _from_dashboard(raw: Dict[str, Any]) -> TargetingRules:
    groups: List[RuleGroup] = []
    for index, group in enumerate(raw.get("groups") or []):
        if not isinstance(group, dict):
            raise TargetingRuleShapeError(f"group {index} is not an object")
        conditions = [
            convert_dashboard_condition(condition)
            for condition in group.get("conditions") or []
        ]
        if not conditions:
            # An empty group would match everyone; the editor creates these
            # as placeholders, so they contribute nothing.
            continue
        groups.append(
            RuleGroup(
                operator=_logical_operator(group.get("logical_operator")),
                conditions=conditions,
            )
        )

    if not groups:
        # "No targeting" — an empty rule list falls through to the global rollout.
        return TargetingRules(rules=[])

    rule = TargetingRule(
        id=str(raw.get("id") or DASHBOARD_RULE_ID),
        name=raw.get("name"),
        rule=RuleGroup(
            operator=_logical_operator(raw.get("logical_operator")),
            conditions=[],
            groups=groups,
        ),
        rollout_percentage=_rollout_percentage(raw.get("rollout_percentage")),
        priority=0,
    )
    return TargetingRules(rules=[rule])


def convert_dashboard_condition(condition: Dict[str, Any]) -> Condition:
    """
    Convert one dashboard ``{"attribute", "operator", "value"}`` condition.

    Raises :class:`TargetingRuleShapeError` for unknown operators, blank
    attributes or values that cannot be interpreted for the operator.
    """
    if not isinstance(condition, dict):
        raise TargetingRuleShapeError("condition is not an object")

    attribute = str(condition.get("attribute") or "").strip()
    if not attribute:
        raise TargetingRuleShapeError("condition attribute is required")

    operator = str(condition.get("operator") or "").strip()
    value = condition.get("value")

    if operator in _SEMVER_OPERATOR_MAP:
        return Condition(
            attribute=attribute,
            operator=OperatorType.SEMANTIC_VERSION,
            value=_semver_value(value),
            additional_value=_SEMVER_OPERATOR_MAP[operator],
        )

    segment_operator = _SEGMENT_OPERATOR_MAP.get(operator)
    if segment_operator is not None:
        # The value is the segment id, kept as stored; the write path checks
        # it (``_check_segment_conditions``).
        return Condition(attribute=attribute, operator=segment_operator, value=value)

    engine_operator = _SIMPLE_OPERATOR_MAP.get(operator)
    if engine_operator is None:
        raise TargetingRuleShapeError(f"unknown operator {operator!r}")

    if engine_operator in (OperatorType.IS_NULL, OperatorType.IS_NOT_NULL):
        return Condition(attribute=attribute, operator=engine_operator, value=None)

    if engine_operator in _LIST_OPERATORS:
        return Condition(
            attribute=attribute, operator=engine_operator, value=_list_value(value)
        )

    if engine_operator in _NUMERIC_OPERATORS:
        return Condition(
            attribute=attribute, operator=engine_operator, value=_numeric_value(value)
        )

    if engine_operator == OperatorType.GEO_DISTANCE:
        point, extra = _geo_value(value)
        return Condition(
            attribute=attribute,
            operator=engine_operator,
            value=point,
            additional_value=extra,
        )

    if engine_operator == OperatorType.TIME_WINDOW:
        return Condition(
            attribute=attribute,
            operator=engine_operator,
            value=_time_window_value(value),
        )

    if engine_operator == OperatorType.MATCH_REGEX:
        if not isinstance(value, str) or not value:
            raise TargetingRuleShapeError("regex operator requires a pattern string")
        return Condition(attribute=attribute, operator=engine_operator, value=value)

    return Condition(attribute=attribute, operator=engine_operator, value=value)


# ---------------------------------------------------------------------------
# Value helpers
# ---------------------------------------------------------------------------


def buckets_on_rule_id(raw: Any) -> bool:
    """True when stored dashboard rules admit only part of the matching users.

    That is: the dashboard shape (:func:`_is_dashboard_rules_shape`), at least
    one group with conditions (``_from_dashboard`` drops a group without any,
    and with no group left there is no rule to bucket on), and a
    ``rollout_percentage`` that ``_from_dashboard`` reads as below 100. Only then does the rules engine bucket a user, on
    ``"<user_id>:<rule id>"``, so only then does the rule's id decide who is
    admitted (#533).
    """
    return (
        isinstance(raw, dict)
        and _is_dashboard_rules_shape(raw)
        and isinstance(raw.get("groups"), list)
        and any(
            isinstance(group, dict) and group.get("conditions")
            for group in raw["groups"]
        )
        and _rollout_percentage(raw.get("rollout_percentage")) < 100
    )


def stored_rule_id(raw: Any) -> Optional[str]:
    """The top-level ``id`` of stored dashboard rules, or ``None``.

    ``None`` exactly when ``_from_dashboard`` would fall back to
    :data:`DASHBOARD_RULE_ID`: no ``id``, or an empty one.
    """
    if isinstance(raw, dict) and _is_dashboard_rules_shape(raw) and raw.get("id"):
        return str(raw["id"])
    return None


def _logical_operator(value: Any) -> LogicalOperator:
    text = str(value or "AND").strip().lower()
    if text == "or":
        return LogicalOperator.OR
    if text == "not":
        return LogicalOperator.NOT
    return LogicalOperator.AND


def _rollout_percentage(value: Any) -> int:
    if value is None or value == "":
        return 100
    try:
        return max(0, min(100, int(float(value))))
    except (TypeError, ValueError):
        return 100


def coerce_number(value: Any) -> Any:
    """Turn editor strings such as ``"17"`` / ``"3.5"`` into numbers; leave the rest alone."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if _NUMBER_RE.match(text):
            try:
                number = float(text)
            except ValueError:
                return value
            return (
                int(number)
                if number.is_integer() and "." not in text and "e" not in text.lower()
                else number
            )
    return value


def _numeric_value(value: Any) -> Any:
    coerced = coerce_number(value)
    if isinstance(coerced, bool) or not isinstance(coerced, (int, float)):
        raise TargetingRuleShapeError(
            f"numeric comparison requires a number, got {value!r}"
        )
    return coerced


def _list_value(value: Any) -> List[Any]:
    """
    Normalise a list-operator value.

    Accepts a JSON array, a scalar (wrapped) or the editor's comma-separated
    string (``"beta, internal"``). Items are stripped; empties are dropped.
    """
    if value is None:
        raise TargetingRuleShapeError("list operator requires at least one value")
    if isinstance(value, str) and value.strip().startswith("["):
        # A JSON array pasted into the editor.
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            pass
    if isinstance(value, (list, tuple, set)):
        items: List[Any] = [
            item.strip() if isinstance(item, str) else item for item in value
        ]
    elif isinstance(value, str):
        items = [part.strip() for part in value.split(",")]
    else:
        items = [value]
    items = [item for item in items if not (isinstance(item, str) and item == "")]
    if not items:
        raise TargetingRuleShapeError("list operator requires at least one value")
    return items


def _semver_value(value: Any) -> str:
    """Pad short editor versions (``"17"``, ``"17.4"``) to full semver."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str) or not value.strip():
        raise TargetingRuleShapeError("semver operator requires a version string")
    text = value.strip()
    if text[:1] in ("v", "V"):
        text = text[1:]
    core, sep, rest = _split_semver_suffix(text)
    parts = core.split(".")
    if not all(part.isdigit() for part in parts) or len(parts) > 3:
        raise TargetingRuleShapeError(f"invalid semantic version {value!r}")
    while len(parts) < 3:
        parts.append("0")
    return ".".join(str(int(part)) for part in parts) + sep + rest


def _split_semver_suffix(text: str):
    """Split ``1.2.3-beta+build`` into (``1.2.3``, ``-``/``+``, remainder)."""
    for index, char in enumerate(text):
        if char in "-+":
            return text[:index], char, text[index + 1 :]
    return text, "", ""


def _geo_value(value: Any):
    """
    Normalise ``geo_within_radius`` into ``([lat, lon], {"radius", "unit"?})``.

    Accepts ``{"lat", "lon", "radius", "unit"?}``, ``[lat, lon, radius, unit?]``
    or the string ``"lat,lon,radius[,unit]"``.
    """
    unit: Optional[str] = None
    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",")]
        if len(parts) < 3:
            raise TargetingRuleShapeError(
                "geo_within_radius requires 'lat,lon,radius[,unit]'"
            )
        lat, lon, radius = parts[0], parts[1], parts[2]
        unit = parts[3] if len(parts) > 3 and parts[3] else None
    elif isinstance(value, dict):
        lat = value.get("lat", value.get("latitude"))
        lon = value.get("lon", value.get("lng", value.get("longitude")))
        radius = value.get("radius")
        unit = value.get("unit")
    elif isinstance(value, (list, tuple)) and len(value) >= 3:
        lat, lon, radius = value[0], value[1], value[2]
        unit = value[3] if len(value) > 3 else None
    else:
        raise TargetingRuleShapeError("geo_within_radius requires lat, lon and radius")

    try:
        point = [float(lat), float(lon)]
        radius_value = float(radius)
    except (TypeError, ValueError):
        raise TargetingRuleShapeError(
            "geo_within_radius coordinates and radius must be numeric"
        )

    extra: Dict[str, Any] = {"radius": radius_value}
    if unit:
        extra["unit"] = str(unit)
    return point, extra


def _time_window_value(value: Any) -> Dict[str, Any]:
    """
    Normalise ``time_window`` into the dict the engine reads.

    The dashboard shape gives a window as ``start``/``end`` (``HH:MM``), which
    the engine does not read; it reads ``start_time``/``end_time``. Both
    spellings are kept, so a window saved with either is read as times of
    day. This shape still requires one of the two pairs: a dashboard window
    given only as ``days`` or dates is refused here (``value is not valid for
    the operator``), although the native shape accepts one (#1527).
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            raise TargetingRuleShapeError("time_window requires a JSON object")
    if not isinstance(value, dict):
        raise TargetingRuleShapeError("time_window requires an object")

    window = dict(value)
    if "start_time" not in window and "start" in window:
        window["start_time"] = window["start"]
    if "end_time" not in window and "end" in window:
        window["end_time"] = window["end"]
    if "start" not in window and "start_time" in window:
        window["start"] = window["start_time"]
    if "end" not in window and "end_time" in window:
        window["end"] = window["end_time"]
    if "start" not in window or "end" not in window:
        raise TargetingRuleShapeError("time_window requires 'start' and 'end'")
    return window


def _flatten_into(out: Dict[str, Any], value: Dict[str, Any], prefix: str) -> None:
    for key, item in value.items():
        if not isinstance(key, str):
            key = str(key)
        full_key = f"{prefix}{key}" if prefix else key
        out.setdefault(full_key, item)
        if isinstance(item, dict):
            _flatten_into(out, item, prefix=f"{full_key}.")
