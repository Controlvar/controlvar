"""
Rules engine for evaluating targeting rules.

This module provides functions for evaluating targeting rules against user contexts.
"""

import hashlib
import logging
import math
from datetime import datetime
from datetime import time as datetime_time
from typing import Any, Dict, Optional

import semver

from backend.app.core import pattern_match
from backend.app.core.log_once import EVALUATION_NOTES

try:
    import pytz

    HAS_PYTZ = True
except ImportError:
    HAS_PYTZ = False

from backend.app.schemas.targeting_rule import (
    Condition,
    LogicalOperator,
    OperatorType,
    RuleGroup,
    TargetingRule,
    TargetingRules,
)

logger = logging.getLogger(__name__)

# Type alias for user context
UserContext = Dict[str, Any]


def _out_of_range(operator: OperatorType) -> pattern_match.PatternUnevaluable:
    """The condition's value is out of range for the comparison (``OUT_OF_RANGE``).

    An integer too large for a float, or a timestamp no date can hold, cannot
    be compared. Like a pattern condition that cannot be evaluated, the
    condition is unevaluable and the caller that owns the ruleset abandons it:
    it is never reduced to ``False`` inside a group, where an enclosing ``NOT``
    would turn it into a match. The digest names the operator, never the value.
    """
    return pattern_match.PatternUnevaluable(
        pattern_match.UnevaluableReason.OUT_OF_RANGE,
        pattern_match.pattern_digest(operator.value),
    )


# Logging on the evaluation path. A user's attribute values are never logged,
# at any level, and neither is the text of an exception raised while handling
# one (a parse error repeats its input). Only type names, operators and fixed
# strings are. A value that does not suit an operator is logged at DEBUG; a
# problem in the rule itself goes through _note_rule_problem, which logs one
# WARNING per (operator, reason) rather than one per evaluation.
# backend/tests/unit/core/test_evaluation_log_arguments.py enforces this.


def _note_rule_problem(operator: Any, reason: str) -> None:
    """Warn, once per ``(operator, reason)``, that a condition's definition is unusable."""
    if EVALUATION_NOTES.first(operator, reason):
        logger.warning(
            "A %s condition in a targeting rule cannot be evaluated: %s. "
            "It does not match; this is logged once.",
            operator,
            reason,
        )


# ---------------------------------------------------------------------------
# Segment membership (#440)
# ---------------------------------------------------------------------------

#: The context key that carries the user's segment memberships: a
#: ``frozenset`` of the ids of the referenced segments the user is a member
#: of. The caller (the flag service, experiment assignment) resolves it from
#: the database and assigns it after ``expand_context``, removing whatever the
#: request sent under this name. ``$`` cannot appear in a rule attribute
#: (``Condition.validate_attribute_name``), so no rule can read it directly.
SEGMENT_MEMBERSHIP_KEY = "$segments"

#: The operators whose answer is membership, not an attribute comparison.
SEGMENT_OPERATORS = frozenset({OperatorType.IN_SEGMENT, OperatorType.NOT_IN_SEGMENT})


class SegmentMembershipUnavailable(Exception):
    """Membership of a referenced segment could not be decided.

    The whole ruleset is abandoned, as for ``PatternUnevaluable``: answering
    "not a member" instead would let ``not_in_segment``, or a ``NOT`` group,
    admit users whose membership is unknown. ``segment_ids`` names the
    segments (ids only, never a user's id or attributes).
    """

    def __init__(self, segment_ids: Any = ()) -> None:
        # Logged by the callers: only short text, which is what a segment
        # id is; anything else a stored rule holds is named, not repeated.
        self.segment_ids = tuple(
            sorted(
                s if isinstance(s, str) and len(s) <= 64 else "(not a segment id)"
                for s in segment_ids
            )
        )
        super().__init__("segment membership could not be decided")


class TimeWindowUnreadable(Exception):
    """A ``time_window`` value the engine cannot read; the ruleset is abandoned.

    Raised by :func:`check_time_window`, after ``_note_rule_problem`` has
    reported the reason, instead of answering ``False`` for the condition
    (#1145). Both condition evaluators call it before looking the attribute
    up, so a user who does not send the attribute is refused too. It is
    handled exactly where ``PatternUnevaluable`` is: the whole ruleset is
    abandoned, so a flag answers off with reason ``error``, a user is not
    eligible for an experiment, and a segment's membership is unavailable;
    neither a later rule nor ``default_rule`` applies. It carries no part of
    the rule or the context.
    """

    def __init__(self) -> None:
        super().__init__("a time_window value could not be read")


def segment_condition_holds(condition: Condition, user_context: UserContext):
    """The answer to a segment condition, or ``None`` for any other condition.

    Called by both evaluators before the attribute lookup. Membership is
    read only from :data:`SEGMENT_MEMBERSHIP_KEY`, and only when it is a
    ``frozenset``; absent, or any other type (a list sent in a request, for
    example), raises :class:`SegmentMembershipUnavailable`. The condition's
    ``attribute`` is never read, so a context attribute named ``segment``
    has no effect on it.
    """
    operator = condition.operator
    if operator not in SEGMENT_OPERATORS:
        return None
    members = user_context.get(SEGMENT_MEMBERSHIP_KEY)
    if not isinstance(members, frozenset) or not isinstance(condition.value, str):
        raise SegmentMembershipUnavailable((condition.value,))
    inside = condition.value in members
    return inside if operator == OperatorType.IN_SEGMENT else not inside


def evaluate_targeting_rules(
    targeting_rules: TargetingRules, user_context: UserContext
) -> Optional[TargetingRule]:
    """
    Evaluate a set of targeting rules against a user context.
    Returns the first matching rule, or None if no rules match.

    Args:
        targeting_rules: The full targeting rules configuration
        user_context: Dictionary containing user attributes

    Returns:
        The first matching targeting rule, or None if no rules match
    """
    if not targeting_rules or not targeting_rules.rules:
        return targeting_rules.default_rule

    # Sort rules by priority (lower number = higher priority)
    sorted_rules = sorted(targeting_rules.rules, key=lambda r: r.priority)

    for rule in sorted_rules:
        # Check if rule matches
        if evaluate_rule(rule, user_context):
            # Check rollout percentage
            if should_include_in_rollout(rule, user_context):
                return rule

    # If no rules match, use default rule
    return targeting_rules.default_rule


def evaluate_rule(rule: TargetingRule, user_context: UserContext) -> bool:
    """
    Evaluate if a targeting rule matches a user context.

    Args:
        rule: The targeting rule to evaluate
        user_context: Dictionary containing user attributes

    Returns:
        True if the rule matches, False otherwise
    """
    return evaluate_rule_group(rule.rule, user_context)


def evaluate_rule_group(rule_group: RuleGroup, user_context: UserContext) -> bool:
    """
    Evaluate a rule group against a user context.

    Args:
        rule_group: The rule group to evaluate
        user_context: Dictionary containing user attributes

    Returns:
        True if the rule group matches, False otherwise
    """
    operator = rule_group.operator

    # Evaluate conditions
    condition_results = [
        evaluate_condition(condition, user_context)
        for condition in rule_group.conditions
    ]

    # Evaluate nested groups
    if rule_group.groups:
        group_results = [
            evaluate_rule_group(group, user_context) for group in rule_group.groups
        ]
    else:
        group_results = []

    # Combine all results
    all_results = condition_results + group_results

    # If there are no conditions or groups, return True
    if not all_results:
        return True

    # Apply logical operator
    if operator == LogicalOperator.AND:
        return all(all_results)
    elif operator == LogicalOperator.OR:
        return any(all_results)
    elif operator == LogicalOperator.NOT:
        # NOT operator should negate the combined result of all conditions and groups
        # If there's only one condition/group, simply negate it
        # If there are multiple, assume they're combined with AND
        if len(all_results) == 1:
            return not all_results[0]
        else:
            return not all(all_results)

    # Default fallback
    _note_rule_problem(operator, "unknown logical operator")
    return False


def evaluate_condition(condition: Condition, user_context: UserContext) -> bool:
    """
    Evaluate a single condition against a user context.

    Args:
        condition: The condition to evaluate
        user_context: Dictionary containing user attributes

    Returns:
        True if the condition matches, False otherwise
    """
    # A segment condition is answered from the resolved membership, before
    # (and instead of) any attribute lookup.
    segment_answer = segment_condition_holds(condition, user_context)
    if segment_answer is not None:
        return segment_answer

    # A time_window that cannot be read refuses the evaluation (the whole
    # ruleset, as PatternUnevaluable does), for a user who does not send the
    # attribute too.
    if condition.operator == OperatorType.TIME_WINDOW:
        check_time_window(condition.operator, condition.value)

    attribute = condition.attribute
    operator = condition.operator
    expected_value = condition.value

    # Get actual value from user context
    if attribute not in user_context:
        logger.debug("Attribute not found in user context (operator=%s)", operator)
        # Presence operators are the one case where a missing attribute is
        # meaningful: "is empty" holds, "is not empty" does not.
        if operator == OperatorType.IS_NULL:
            return True
        return False

    actual_value = user_context[attribute]

    # Apply operator
    return apply_operator(
        operator, actual_value, expected_value, condition.additional_value
    )


def _is_empty_value(value: Any) -> bool:
    """True for ``None``, blank strings and empty collections (dashboard "is empty")."""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    return False


def _values_equal(actual_value: Any, expected_value: Any) -> bool:
    """
    Equality with a small amount of type leniency.

    The dashboard rule editor stores every value as a string ("true", "17")
    while SDK contexts carry typed values (``True``, ``17``). Exact equality is
    tried first; when exactly one side is a string and the other a bool or
    number, the string is interpreted as that type before comparing. Two
    strings, or two typed values, are never coerced.
    """
    if actual_value == expected_value:
        return True

    scalar = (bool, int, float)
    if isinstance(expected_value, str) and isinstance(actual_value, scalar):
        typed, text = actual_value, expected_value
    elif isinstance(actual_value, str) and isinstance(expected_value, scalar):
        typed, text = expected_value, actual_value
    else:
        return False

    if isinstance(typed, bool):
        lowered = text.strip().lower()
        if lowered in ("true", "1", "yes"):
            return typed is True
        if lowered in ("false", "0", "no"):
            return typed is False
        return False

    try:
        return float(text) == float(typed)
    except (TypeError, ValueError):
        return False


def _substring_of(item: Any, text: str) -> bool:
    """Whether *item* is a substring of *text*; an item that is not a string is not."""
    return isinstance(item, str) and item in text


#: The keys of a ``time_window`` value that say when it is open; ``timezone``
#: only changes how they are read. A window with none of them cannot be read
#: (#1145). It used to match at every moment, so a ``{"start": ..., "end": ...}``
#: window, the two keys the ``Condition`` schema asks for and which the engine
#: does not read, was always open.
TIME_WINDOW_KEYS = ("days", "start_time", "end_time", "start_date", "end_date")


def check_time_window(operator: Any, window: Any) -> None:
    """Raise :class:`TimeWindowUnreadable` when ``window`` cannot be read.

    Called before the user's value is looked at, so that neither that value nor
    an earlier key that does not match can hide a key that cannot be read.
    """
    if not isinstance(window, dict):
        _note_rule_problem(operator, "the value is not an object")
        raise TimeWindowUnreadable()
    if not any(key in window for key in TIME_WINDOW_KEYS):
        _note_rule_problem(
            operator, "no days, start_time/end_time or start_date/end_date"
        )
        raise TimeWindowUnreadable()

    timezone_name = window.get("timezone")
    if timezone_name:
        if not HAS_PYTZ:
            _note_rule_problem(operator, "a timezone needs pytz, not installed")
            raise TimeWindowUnreadable()
        try:
            pytz.timezone(timezone_name)
        except Exception:
            _note_rule_problem(operator, "the timezone is not known")
            raise TimeWindowUnreadable() from None

    if "days" in window:
        days = window["days"]
        if not isinstance(days, list):
            _note_rule_problem(operator, "days is not a list")
            raise TimeWindowUnreadable()
        if not all(isinstance(d, int) and 0 <= d <= 6 for d in days):
            _note_rule_problem(operator, "days must be 0-6")
            raise TimeWindowUnreadable()

    if "start_time" in window or "end_time" in window:
        if "start_time" not in window or "end_time" not in window:
            _note_rule_problem(operator, "start_time without end_time")
            raise TimeWindowUnreadable()
        if (
            _parse_time(window["start_time"]) is None
            or _parse_time(window["end_time"]) is None
        ):
            _note_rule_problem(operator, "start_time or end_time is not HH:MM")
            raise TimeWindowUnreadable()

    if "start_date" in window or "end_date" in window:
        if "start_date" not in window or "end_date" not in window:
            _note_rule_problem(operator, "start_date without end_date")
            raise TimeWindowUnreadable()
        start_date = _parse_date(window["start_date"])
        end_date = _parse_date(window["end_date"])
        if start_date is None or end_date is None:
            _note_rule_problem(operator, "start_date or end_date is not YYYY-MM-DD")
            raise TimeWindowUnreadable()
        if start_date > end_date:
            _note_rule_problem(operator, "start_date is after end_date")
            raise TimeWindowUnreadable()


def apply_operator(
    operator: OperatorType,
    actual_value: Any,
    expected_value: Any,
    additional_value: Optional[Any] = None,
) -> bool:
    """
    Apply an operator to compare actual and expected values.

    Args:
        operator: The operator to apply
        actual_value: The actual value from user context
        expected_value: The expected value from the condition
        additional_value: Additional value for operators that need two values

    Returns:
        True if the operator evaluates to true, False otherwise
    """
    if operator in SEGMENT_OPERATORS:
        # Unreachable: both evaluators answer a segment condition with
        # segment_condition_holds before looking up an attribute. A caller
        # that gets here has no membership to compare, so it fails closed.
        raise SegmentMembershipUnavailable((expected_value,))
    # Presence operators ignore expected_value entirely
    if operator == OperatorType.IS_NULL:
        return _is_empty_value(actual_value)
    elif operator == OperatorType.IS_NOT_NULL:
        return not _is_empty_value(actual_value)

    # Handle None values
    if actual_value is None:
        # Only certain operators make sense with None
        if operator == OperatorType.EQUALS:
            return expected_value is None
        elif operator == OperatorType.NOT_EQUALS:
            return expected_value is not None
        elif operator == OperatorType.TIME_WINDOW:
            # For TIME_WINDOW, None means "use current time"
            pass  # Continue to the operator handler
        else:
            return False

    # Simple equality operators. _values_equal raises OverflowError for a
    # number too large for a float: the condition is unevaluable.
    if operator == OperatorType.EQUALS:
        try:
            return _values_equal(actual_value, expected_value)
        except OverflowError:
            raise _out_of_range(operator) from None
    elif operator == OperatorType.NOT_EQUALS:
        try:
            return not _values_equal(actual_value, expected_value)
        except OverflowError:
            raise _out_of_range(operator) from None

    # Collection operators
    elif operator == OperatorType.IN:
        # Check if actual_value is in expected_value (which should be a collection)
        if not isinstance(expected_value, (list, tuple, set)):
            _note_rule_problem(operator, "the value is not a list")
            return False
        try:
            return any(_values_equal(actual_value, item) for item in expected_value)
        except OverflowError:
            raise _out_of_range(operator) from None
    elif operator == OperatorType.NOT_IN:
        if not isinstance(expected_value, (list, tuple, set)):
            _note_rule_problem(operator, "the value is not a list")
            return False
        try:
            return not any(_values_equal(actual_value, item) for item in expected_value)
        except OverflowError:
            raise _out_of_range(operator) from None

    # String operators
    elif operator == OperatorType.CONTAINS:
        # Check if expected_value is in actual_value (string contains)
        if not isinstance(actual_value, str):
            # Convert to string for non-string types
            actual_value = str(actual_value)
        if not isinstance(expected_value, str):
            expected_value = str(expected_value)
        return expected_value in actual_value
    elif operator == OperatorType.NOT_CONTAINS:
        if not isinstance(actual_value, str):
            actual_value = str(actual_value)
        if not isinstance(expected_value, str):
            expected_value = str(expected_value)
        return expected_value not in actual_value
    elif operator == OperatorType.STARTS_WITH:
        if not isinstance(actual_value, str):
            actual_value = str(actual_value)
        if not isinstance(expected_value, str):
            expected_value = str(expected_value)
        return actual_value.startswith(expected_value)
    elif operator == OperatorType.ENDS_WITH:
        if not isinstance(actual_value, str):
            actual_value = str(actual_value)
        if not isinstance(expected_value, str):
            expected_value = str(expected_value)
        return actual_value.endswith(expected_value)
    elif operator == OperatorType.MATCH_REGEX:
        # RE2 syntax. Raises PatternUnevaluable when the pattern is refused or
        # the value cannot be evaluated; it is deliberately not turned into
        # False here, where an enclosing NOT would make the rule match. The
        # caller that owns the whole ruleset catches it.
        return pattern_match.search(expected_value, actual_value)

    # Numeric comparison operators
    elif operator == OperatorType.GREATER_THAN:
        try:
            return float(actual_value) > float(expected_value)
        except OverflowError:
            raise _out_of_range(operator) from None
        except (ValueError, TypeError):
            logger.debug(
                "%s: cannot compare %s with %s as numbers",
                operator,
                type(actual_value).__name__,
                type(expected_value).__name__,
            )
            return False
    elif operator == OperatorType.GREATER_THAN_OR_EQUAL:
        try:
            return float(actual_value) >= float(expected_value)
        except OverflowError:
            raise _out_of_range(operator) from None
        except (ValueError, TypeError):
            logger.debug(
                "%s: cannot compare %s with %s as numbers",
                operator,
                type(actual_value).__name__,
                type(expected_value).__name__,
            )
            return False
    elif operator == OperatorType.LESS_THAN:
        try:
            return float(actual_value) < float(expected_value)
        except OverflowError:
            raise _out_of_range(operator) from None
        except (ValueError, TypeError):
            logger.debug(
                "%s: cannot compare %s with %s as numbers",
                operator,
                type(actual_value).__name__,
                type(expected_value).__name__,
            )
            return False
    elif operator == OperatorType.LESS_THAN_OR_EQUAL:
        try:
            return float(actual_value) <= float(expected_value)
        except OverflowError:
            raise _out_of_range(operator) from None
        except (ValueError, TypeError):
            logger.debug(
                "%s: cannot compare %s with %s as numbers",
                operator,
                type(actual_value).__name__,
                type(expected_value).__name__,
            )
            return False

    # Date comparison operators
    elif operator in (OperatorType.BEFORE, OperatorType.AFTER):
        try:
            # Convert to datetime if not already
            if isinstance(actual_value, str):
                actual_date = datetime.fromisoformat(
                    actual_value.replace("Z", "+00:00")
                )
            elif isinstance(actual_value, (int, float)):
                actual_date = datetime.fromtimestamp(actual_value)
            elif isinstance(actual_value, datetime):
                actual_date = actual_value
            else:
                logger.debug(
                    "%s: a %s is not a date",
                    operator,
                    type(actual_value).__name__,
                )
                return False

            if isinstance(expected_value, str):
                expected_date = datetime.fromisoformat(
                    expected_value.replace("Z", "+00:00")
                )
            elif isinstance(expected_value, (int, float)):
                expected_date = datetime.fromtimestamp(expected_value)
            elif isinstance(expected_value, datetime):
                expected_date = expected_value
            else:
                _note_rule_problem(operator, "the value is not a date")
                return False

            if operator == OperatorType.BEFORE:
                return actual_date < expected_date
            else:  # AFTER
                return actual_date > expected_date

        except (OverflowError, OSError):
            # datetime.fromtimestamp: OSError for a timestamp whose year the C
            # library cannot hold (from about 7e16 seconds), OverflowError for
            # one beyond time_t (2**63). Either is out of range.
            raise _out_of_range(operator) from None
        except (ValueError, TypeError):
            logger.debug(
                "%s: cannot compare %s with %s as dates",
                operator,
                type(actual_value).__name__,
                type(expected_value).__name__,
            )
            return False

    # Range operators
    elif operator == OperatorType.BETWEEN:
        # Check if value is between expected_value and additional_value
        if additional_value is None:
            _note_rule_problem(operator, "no upper bound (additional_value)")
            return False

        try:
            # Handle numeric ranges
            if all(
                isinstance(v, (int, float))
                or (isinstance(v, str) and v.replace(".", "", 1).isdigit())
                for v in (actual_value, expected_value, additional_value)
            ):
                actual = float(actual_value)
                lower = float(expected_value)
                upper = float(additional_value)
                return lower <= actual <= upper

            # Handle date ranges
            if isinstance(actual_value, datetime):
                actual_date = actual_value
            elif isinstance(actual_value, str):
                actual_date = datetime.fromisoformat(
                    actual_value.replace("Z", "+00:00")
                )
            elif isinstance(actual_value, (int, float)):
                actual_date = datetime.fromtimestamp(actual_value)
            else:
                logger.debug(
                    "%s: a %s is neither a number nor a date",
                    operator,
                    type(actual_value).__name__,
                )
                return False

            if isinstance(expected_value, datetime):
                lower_date = expected_value
            elif isinstance(expected_value, str):
                lower_date = datetime.fromisoformat(
                    expected_value.replace("Z", "+00:00")
                )
            elif isinstance(expected_value, (int, float)):
                lower_date = datetime.fromtimestamp(expected_value)
            else:
                _note_rule_problem(operator, "the lower bound is not a date")
                return False

            if isinstance(additional_value, datetime):
                upper_date = additional_value
            elif isinstance(additional_value, str):
                upper_date = datetime.fromisoformat(
                    additional_value.replace("Z", "+00:00")
                )
            elif isinstance(additional_value, (int, float)):
                upper_date = datetime.fromtimestamp(additional_value)
            else:
                _note_rule_problem(operator, "the upper bound is not a date")
                return False

            return lower_date <= actual_date <= upper_date

        except (OverflowError, OSError):
            # float() of an integer too large for a float, or
            # datetime.fromtimestamp of a timestamp out of range: OSError from
            # about 7e16 seconds, OverflowError beyond time_t (2**63).
            raise _out_of_range(operator) from None
        except (ValueError, TypeError):
            logger.debug(
                "%s: cannot compare %s with bounds %s and %s",
                operator,
                type(actual_value).__name__,
                type(expected_value).__name__,
                type(additional_value).__name__,
            )
            return False

    # Array operators
    elif operator == OperatorType.CONTAINS_ALL:
        # Check if actual_value contains all elements in expected_value
        if not isinstance(expected_value, (list, tuple, set)):
            _note_rule_problem(operator, "the value is not a list")
            return False

        if not isinstance(actual_value, (list, tuple, set)):
            if isinstance(actual_value, str):
                # A string attribute matches by substring, and only a string
                # item can be a substring: any other item does not match, where
                # `1 in "abc"` would raise TypeError (#270).
                return all(_substring_of(item, actual_value) for item in expected_value)
            logger.debug(
                "%s: a %s is not a list", operator, type(actual_value).__name__
            )
            return False

        return all(item in actual_value for item in expected_value)

    elif operator == OperatorType.CONTAINS_ANY:
        # Check if actual_value contains any elements in expected_value
        if not isinstance(expected_value, (list, tuple, set)):
            _note_rule_problem(operator, "the value is not a list")
            return False

        if not isinstance(actual_value, (list, tuple, set)):
            if isinstance(actual_value, str):
                # As CONTAINS_ALL: a non-string item does not match (#270).
                return any(_substring_of(item, actual_value) for item in expected_value)
            logger.debug(
                "%s: a %s is not a list", operator, type(actual_value).__name__
            )
            return False

        return any(item in actual_value for item in expected_value)

    # Semantic version operators
    elif operator == OperatorType.SEMANTIC_VERSION:
        try:
            # Parse the versions
            if not isinstance(actual_value, str) or not isinstance(expected_value, str):
                if isinstance(actual_value, str):
                    _note_rule_problem(operator, "the version is not a string")
                else:
                    logger.debug(
                        "%s: a %s is not a version string",
                        operator,
                        type(actual_value).__name__,
                    )
                return False

            # Remove leading 'v' if present (be lenient)
            actual_clean = actual_value.lstrip("v")
            expected_clean = expected_value.lstrip("v")

            # Validate format. The parse error repeats its input, so it is not
            # logged; the user's version is parsed first, as before.
            try:
                actual_ver = semver.Version.parse(actual_clean)
            except ValueError:
                logger.debug("%s: the value is not a semantic version", operator)
                return False
            try:
                expected_ver = semver.Version.parse(expected_clean)
            except ValueError:
                _note_rule_problem(operator, "the version is not a semantic version")
                return False

            # Determine comparison type from additional_value
            comparison_type = additional_value if additional_value else "eq"

            # According to SemVer 2.0 spec, build metadata should be ignored when determining version precedence
            # We compare major, minor, patch, and prerelease, but not build
            if comparison_type == "eq":
                # For equality, we ignore build metadata per SemVer spec
                return (
                    actual_ver.major == expected_ver.major
                    and actual_ver.minor == expected_ver.minor
                    and actual_ver.patch == expected_ver.patch
                    and actual_ver.prerelease == expected_ver.prerelease
                )
            elif comparison_type == "gt":
                return actual_ver > expected_ver
            elif comparison_type == "gte":
                return actual_ver >= expected_ver
            elif comparison_type == "lt":
                return actual_ver < expected_ver
            elif comparison_type == "lte":
                return actual_ver <= expected_ver
            else:
                _note_rule_problem(operator, "unknown comparison")
                return False

        except Exception as e:
            logger.debug("%s: comparison failed (%s)", operator, type(e).__name__)
            return False

    # Array length operator
    elif operator == OperatorType.ARRAY_LENGTH:
        try:
            # Get the actual length of the collection
            try:
                actual_length = len(actual_value)
            except TypeError:
                # Value doesn't have length (e.g., int, float, None)
                logger.debug(
                    "%s: a %s has no length", operator, type(actual_value).__name__
                )
                return False

            # Determine comparison type from additional_value
            comparison_type = additional_value if additional_value else "eq"

            # Handle different comparison types
            if comparison_type == "eq":
                try:
                    return actual_length == int(expected_value)
                except (ValueError, TypeError):
                    _note_rule_problem(operator, "the length is not a number")
                    return False

            elif comparison_type == "gt":
                try:
                    return actual_length > int(expected_value)
                except (ValueError, TypeError):
                    _note_rule_problem(operator, "the length is not a number")
                    return False

            elif comparison_type == "lt":
                try:
                    return actual_length < int(expected_value)
                except (ValueError, TypeError):
                    _note_rule_problem(operator, "the length is not a number")
                    return False

            elif comparison_type == "gte":
                try:
                    return actual_length >= int(expected_value)
                except (ValueError, TypeError):
                    _note_rule_problem(operator, "the length is not a number")
                    return False

            elif comparison_type == "lte":
                try:
                    return actual_length <= int(expected_value)
                except (ValueError, TypeError):
                    _note_rule_problem(operator, "the length is not a number")
                    return False

            elif comparison_type == "between":
                # Expected value should be a dict with min and max
                if not isinstance(expected_value, dict):
                    _note_rule_problem(operator, "between needs {'min', 'max'}")
                    return False

                if "min" not in expected_value or "max" not in expected_value:
                    _note_rule_problem(operator, "between needs {'min', 'max'}")
                    return False

                try:
                    min_val = int(expected_value["min"])
                    max_val = int(expected_value["max"])
                    return min_val <= actual_length <= max_val
                except (ValueError, TypeError, KeyError):
                    _note_rule_problem(operator, "between bounds are not numbers")
                    return False

            else:
                _note_rule_problem(operator, "unknown comparison")
                return False

        except Exception as e:
            logger.debug("%s: evaluation failed (%s)", operator, type(e).__name__)
            return False

    # Geographic distance operator
    elif operator == OperatorType.GEO_DISTANCE:
        try:
            # The Condition schema validates GEO_DISTANCE as value=[lat, lon] plus
            # additional_value (a radius, or {"radius", "unit", "comparison"}).
            # Fold that form into the {"lat", "lon", "radius", ...} dict this
            # handler works with so both spellings evaluate identically.
            schema_form = isinstance(expected_value, (list, tuple))
            if schema_form and additional_value is not None:
                point = _extract_coordinates(expected_value)
                if point:
                    if isinstance(additional_value, dict):
                        expected_value = {**additional_value, **point}
                    else:
                        expected_value = {**point, "radius": additional_value}

            # Extract coordinates from actual_value
            actual_coords = _extract_coordinates(actual_value)
            if not actual_coords:
                logger.debug(
                    "%s: no coordinates in a %s",
                    operator,
                    type(actual_value).__name__,
                )
                return False

            # Extract coordinates from expected_value
            expected_coords = _extract_coordinates(expected_value)
            if not expected_coords:
                _note_rule_problem(operator, "the value has no coordinates")
                return False

            # Validate coordinates
            if not _validate_coordinates(actual_coords):
                logger.debug("%s: coordinates out of range", operator)
                return False

            if not _validate_coordinates(expected_coords):
                _note_rule_problem(operator, "the coordinates are out of range")
                return False

            # Extract radius from expected_value
            if not isinstance(expected_value, dict) or "radius" not in expected_value:
                _note_rule_problem(operator, "no radius")
                return False

            try:
                radius = float(expected_value["radius"])
            except (ValueError, TypeError):
                _note_rule_problem(operator, "the radius is not a number")
                return False

            # Validate radius (must be non-negative)
            if radius < 0:
                _note_rule_problem(operator, "the radius is negative")
                return False

            # Get unit (default to km)
            unit = expected_value.get("unit", "km")
            if isinstance(unit, str):
                unit = unit.lower()

            # Calculate distance using Haversine formula
            distance = _haversine_distance(
                actual_coords["lat"],
                actual_coords["lon"],
                expected_coords["lat"],
                expected_coords["lon"],
                unit,
            )

            if distance is None:
                logger.debug("%s: distance could not be calculated", operator)
                return False

            # Get comparison type (default to within radius, i.e., <=)
            comparison = expected_value.get("comparison", "lte")

            # Perform comparison
            if comparison == "lte" or comparison is None:
                return distance <= radius
            elif comparison == "lt":
                return distance < radius
            elif comparison == "gt":
                return distance > radius
            elif comparison == "gte":
                return distance >= radius
            elif comparison == "eq":
                # For equality, we use a small epsilon for floating point comparison
                epsilon = 0.01  # 10 meters tolerance
                return abs(distance - radius) < epsilon
            else:
                _note_rule_problem(operator, "unknown comparison")
                return False

        except Exception as e:
            logger.debug("%s: evaluation failed (%s)", operator, type(e).__name__)
            return False

    # Time window operator
    elif operator == OperatorType.TIME_WINDOW:
        # A window that cannot be read raises TimeWindowUnreadable here, before
        # the user's value is read: the evaluation is refused (the whole
        # ruleset, as PatternUnevaluable; see the class).
        check_time_window(operator, expected_value)
        try:
            # Parse actual_value to datetime
            dt = _parse_datetime(actual_value)

            # If a value was provided but couldn't be parsed, it's invalid
            if actual_value is not None and dt is None:
                logger.debug(
                    "%s: a %s is not a date and time",
                    operator,
                    type(actual_value).__name__,
                )
                return False

            # Apply timezone conversion if specified
            timezone_str = expected_value.get("timezone")
            if timezone_str:
                try:
                    tz = pytz.timezone(timezone_str)

                    # If dt is None (no value provided), use current time in the specified timezone
                    if dt is None:
                        dt = datetime.now(tz)
                    elif dt.tzinfo is None:
                        # For naive datetime with timezone specified, localize it to the specified timezone
                        # (treat it as being in that timezone, not UTC)
                        dt = tz.localize(dt)
                    else:
                        # Timezone-aware datetime, convert to specified timezone
                        dt = dt.astimezone(tz)
                except Exception as e:
                    logger.debug(
                        "%s: timezone conversion failed (%s)",
                        operator,
                        type(e).__name__,
                    )
                    return False
            elif dt is None:
                # Use current time if no value provided
                dt = datetime.now()

            # Check day of week if specified (Monday = 0, Sunday = 6)
            if "days" in expected_value and dt.weekday() not in expected_value["days"]:
                return False

            # Check time range if specified
            if "start_time" in expected_value:
                start_time = _parse_time(expected_value["start_time"])
                end_time = _parse_time(expected_value["end_time"])
                current_time = dt.time()

                # Helper function for inclusive time comparison
                # Treats times within the same minute as equal for end boundary
                def time_matches_or_before_end(
                    current: datetime_time, end: datetime_time
                ) -> bool:
                    # If hours and minutes match, consider it within range regardless of seconds
                    if current.hour == end.hour and current.minute == end.minute:
                        return True
                    return current <= end

                # Handle overnight time range (e.g., 22:00 to 02:00)
                if start_time > end_time:
                    # Overnight: match if time >= start OR time <= end (with minute-level inclusivity)
                    if not (
                        current_time >= start_time
                        or time_matches_or_before_end(current_time, end_time)
                    ):
                        return False
                else:
                    # Normal range: match if start <= time <= end (with minute-level inclusivity)
                    if not (
                        current_time >= start_time
                        and time_matches_or_before_end(current_time, end_time)
                    ):
                        return False

            # Check date range if specified (date only, ignore time)
            if "start_date" in expected_value:
                start_date = _parse_date(expected_value["start_date"])
                end_date = _parse_date(expected_value["end_date"])
                if not (start_date <= dt.date() <= end_date):
                    return False

            # All checks passed
            return True

        except Exception as e:
            logger.debug("%s: evaluation failed (%s)", operator, type(e).__name__)
            return False

    # Enhanced operators - delegate to evaluation service if available
    elif operator in ["percentage_bucket", "json_path"]:
        # For backward compatibility, these will be handled by the evaluation service
        # Fall back to False for now if not handled by evaluation service
        _note_rule_problem(operator, "not implemented for feature flags")
        return False

    # Unknown operator
    _note_rule_problem(operator, "unknown operator")
    return False


def _parse_datetime(value: Any) -> Optional[datetime]:
    """
    Parse value into a datetime object.

    Supports:
    - datetime objects (returned as-is)
    - ISO format strings
    - Unix timestamps (int or float)
    - None (returns None, caller can use datetime.now())

    Args:
        value: The value to parse

    Returns:
        datetime object or None if parsing fails
    """
    try:
        # Already a datetime
        if isinstance(value, datetime):
            return value

        # None value (caller should use current time)
        if value is None:
            return None

        # ISO format string
        if isinstance(value, str):
            try:
                # Try parsing ISO format
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                logger.debug("A string is not an ISO date and time")
                return None

        # Unix timestamp (seconds since epoch)
        if isinstance(value, (int, float)):
            try:
                return datetime.fromtimestamp(value)
            except (ValueError, OSError):
                logger.debug("A %s is not a timestamp", type(value).__name__)
                return None

        return None

    except Exception as e:
        logger.debug("Date and time parsing failed (%s)", type(e).__name__)
        return None


def _parse_time(value: str) -> Optional[datetime_time]:
    """
    Parse time string in HH:MM format.

    Args:
        value: Time string (e.g., "09:00", "17:30")

    Returns:
        time object or None if parsing fails
    """
    try:
        if not isinstance(value, str):
            return None

        # Expected format: "HH:MM" or "HH:MM:SS"
        parts = value.split(":")
        if len(parts) == 2:
            hour, minute = int(parts[0]), int(parts[1])
            return datetime_time(hour, minute)
        elif len(parts) == 3:
            hour, minute, second = int(parts[0]), int(parts[1]), int(parts[2])
            return datetime_time(hour, minute, second)
        else:
            return None

    except (ValueError, AttributeError):
        return None


def _parse_date(value: str):
    """
    Parse date string in YYYY-MM-DD format.

    Args:
        value: Date string (e.g., "2024-01-01")

    Returns:
        date object or None if parsing fails
    """
    try:
        if not isinstance(value, str):
            return None

        # Expected format: "YYYY-MM-DD"
        dt = datetime.strptime(value, "%Y-%m-%d")
        return dt.date()

    except ValueError:
        return None


def _extract_coordinates(value: Any) -> Optional[Dict[str, float]]:
    """
    Extract latitude and longitude from various coordinate formats.

    Supports:
    - {"lat": 37.7749, "lon": -122.4194}
    - {"latitude": 37.7749, "longitude": -122.4194}
    - [37.7749, -122.4194]  (array format: [lat, lon])

    Args:
        value: The value to extract coordinates from

    Returns:
        Dictionary with 'lat' and 'lon' keys, or None if extraction fails
    """
    try:
        # Handle dict format
        if isinstance(value, dict):
            # Try lat/lon keys first
            if "lat" in value and "lon" in value:
                return {"lat": float(value["lat"]), "lon": float(value["lon"])}
            # Try latitude/longitude keys
            elif "latitude" in value and "longitude" in value:
                return {
                    "lat": float(value["latitude"]),
                    "lon": float(value["longitude"]),
                }
            # Try mixed formats
            elif "lat" in value and "longitude" in value:
                return {"lat": float(value["lat"]), "lon": float(value["longitude"])}
            elif "latitude" in value and "lon" in value:
                return {"lat": float(value["latitude"]), "lon": float(value["lon"])}

        # Handle array format [lat, lon]
        elif isinstance(value, (list, tuple)) and len(value) == 2:
            return {"lat": float(value[0]), "lon": float(value[1])}

        return None

    except (ValueError, TypeError, KeyError):
        return None


def _validate_coordinates(coords: Dict[str, float]) -> bool:
    """
    Validate that coordinates are within valid ranges.

    Args:
        coords: Dictionary with 'lat' and 'lon' keys

    Returns:
        True if coordinates are valid, False otherwise
    """
    try:
        lat = coords["lat"]
        lon = coords["lon"]

        # Latitude must be between -90 and 90
        if lat < -90 or lat > 90:
            return False

        # Longitude must be between -180 and 180
        if lon < -180 or lon > 180:
            return False

        return True

    except (KeyError, TypeError):
        return False


def _haversine_distance(
    lat1: float, lon1: float, lat2: float, lon2: float, unit: str = "km"
) -> Optional[float]:
    """
    Calculate the great circle distance between two points on Earth using the Haversine formula.

    Args:
        lat1: Latitude of first point in degrees
        lon1: Longitude of first point in degrees
        lat2: Latitude of second point in degrees
        lon2: Longitude of second point in degrees
        unit: Unit for distance ('km', 'miles', 'meters')

    Returns:
        Distance in the specified unit, or None if calculation fails
    """
    try:
        # Earth's radius in kilometers
        earth_radius_km = 6371.0

        # Convert latitude and longitude from degrees to radians
        lat1_rad = math.radians(lat1)
        lon1_rad = math.radians(lon1)
        lat2_rad = math.radians(lat2)
        lon2_rad = math.radians(lon2)

        # Haversine formula
        dlat = lat2_rad - lat1_rad
        dlon = lon2_rad - lon1_rad

        a = (
            math.sin(dlat / 2) ** 2
            + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon / 2) ** 2
        )
        c = 2 * math.asin(math.sqrt(a))

        # Distance in kilometers
        distance_km = earth_radius_km * c

        # Convert to requested unit
        if unit in ("km", "kilometers", "kilometre", "kilometres"):
            return distance_km
        elif unit in ("miles", "mi"):
            return distance_km * 0.621371  # km to miles
        elif unit in ("meters", "metres", "m"):
            return distance_km * 1000  # km to meters
        else:
            _note_rule_problem(OperatorType.GEO_DISTANCE, "unknown distance unit")
            return None

    except (ValueError, TypeError) as e:
        logger.debug("Distance calculation failed (%s)", type(e).__name__)
        return None


def should_include_in_rollout(rule: TargetingRule, user_context: UserContext) -> bool:
    """
    Determine if a user should be included in a rollout based on percentage and deterministic hashing.

    Args:
        rule: The targeting rule with rollout percentage
        user_context: Dictionary containing user attributes

    Returns:
        True if the user should be included in the rollout, False otherwise
    """
    rollout_percentage = rule.rollout_percentage

    # If rollout is 0%, no one gets the feature
    if rollout_percentage == 0:
        return False

    # If rollout is 100%, everyone gets the feature
    if rollout_percentage == 100:
        return True

    # Get a unique, stable identifier for the user
    user_id = get_stable_user_id(user_context)

    # Create a hash of the user ID and rule ID for deterministic bucketing
    hash_input = f"{user_id}:{rule.id}"
    hash_value = int(
        hashlib.md5(hash_input.encode(), usedforsecurity=False).hexdigest(), 16
    )

    # Calculate the bucket (0-99)
    bucket = hash_value % 100

    # User is included if their bucket is less than the rollout percentage
    return bucket < rollout_percentage


def get_stable_user_id(user_context: UserContext) -> str:
    """
    Get a stable identifier for the user from the context.

    Args:
        user_context: Dictionary containing user attributes

    Returns:
        A string identifier for the user
    """
    # Try common user identifier fields
    for field in [
        "user_id",
        "id",
        "email",
        "username",
        "device_id",
        "client_id",
        "session_id",
    ]:
        if field in user_context and user_context[field]:
            return str(user_context[field])

    # If no suitable ID is found, use a hash of all available data
    # This isn't ideal for stable bucketing but better than nothing
    context_str = str(sorted(user_context.items()))
    return hashlib.md5(context_str.encode(), usedforsecurity=False).hexdigest()
