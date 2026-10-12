# Feature flag management service
# backend/app/services/feature_flag_service.py
import enum
import hashlib
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.app.core.pattern_match import PatternUnevaluable, report_unevaluable
from backend.app.core.rules_engine import (
    SegmentMembershipUnavailable,
    TimeWindowUnreadable,
)
from backend.app.core.targeting_adapter import (
    expand_context,
    match_targeting_rule,
    normalise_targeting_rules,
    segment_ids_in,
)
from backend.app.models.feature_flag import (
    ArchivedFlagError,
    FeatureFlag,
    FeatureFlagStatus,
    flag_status_name,
)
from backend.app.schemas.feature_flag import (
    READ_ONLY_FIELDS,
    FeatureFlagCreate,
    FeatureFlagRead,
    FeatureFlagUpdate,
    flag_to_read,
)
from backend.app.schemas.metrics import ErrorLogCreate
from backend.app.services.metrics_service import MetricsService
from backend.app.services.segment_membership import (
    SegmentMemberships,
    log_unavailable,
    resolve_segment_memberships,
    with_memberships,
)

logger = logging.getLogger(__name__)

# Reasons reported by ``evaluate_flag_detailed``:
#   targeting_rule - a targeting rule matched; its rollout % decided
#   rollout        - no rule matched (or none defined); the global rollout % decided
#   inactive       - the flag is not ACTIVE
#   error          - evaluation raised, or a pattern condition could not be
#                    evaluated; the flag defaulted to off
REASON_TARGETING_RULE = "targeting_rule"
REASON_ROLLOUT = "rollout"
REASON_INACTIVE = "inactive"
REASON_ERROR = "error"

# The operators a legacy (list-shaped) ``context`` condition understands. A
# tuple, not a set: a stored operator can be any JSON value, a list among them.
_LEGACY_OPERATORS = ("eq", "ne", "gt", "lt", "contains", "in")


class FlagStatusReadOnly(Exception):
    """A request's ``status`` differs from the flag's status.

    ``status`` is read-only (#94): a GET body may send it back, so it is
    accepted when it equals the stored status (on create, the status
    ``is_active`` gives the new flag), compared case-insensitively, and never
    written.  Any other value is refused rather than ignored -- ignoring it was
    the silent no-op #94 reported.  Not a ``ValueError``: the routes answer a
    ``ValueError`` with its text, and this one has a fixed 422.
    """


def _status_value(status: Any) -> str:
    """``FeatureFlagStatus`` or its string value, as the upper-case string."""
    return str(getattr(status, "value", status))


def _check_sent_status(
    flag_data: Union[FeatureFlagCreate, FeatureFlagUpdate], expected: Any
) -> None:
    """Raise :class:`FlagStatusReadOnly` when a sent ``status`` is not *expected*."""
    if "status" not in flag_data.model_fields_set:
        return
    sent = flag_data.status
    if not (isinstance(sent, str) and sent.upper() == _status_value(expected)):
        raise FlagStatusReadOnly()


class FlagVerb(enum.Enum):
    """What a request asks of a flag's status (#631)."""

    ON = "on"
    OFF = "off"
    ARCHIVE = "archive"
    UNARCHIVE = "unarchive"


def transition(current: Any, verb: FlagVerb) -> FeatureFlagStatus:
    """The status a flag in *current* moves to for *verb* (#631).

    The one rule every route that changes a flag's status follows:

    * ON: ACTIVE, except that an ARCHIVED flag is refused with
      :class:`ArchivedFlagError` -- it has to be unarchived first;
    * OFF: INACTIVE, except that an ARCHIVED flag stays ARCHIVED (off is
      already what an archived flag serves, so this succeeds and changes
      nothing);
    * ARCHIVE: ARCHIVED, from any status;
    * UNARCHIVE: an ARCHIVED flag becomes INACTIVE; any other flag keeps its
      status.

    *current* may be the enum or its string value. Nothing is written here, so
    a caller can refuse before it changes anything.
    """
    stored = FeatureFlagStatus(flag_status_name(current))
    archived = stored is FeatureFlagStatus.ARCHIVED
    if verb is FlagVerb.ON:
        if archived:
            raise ArchivedFlagError()
        return FeatureFlagStatus.ACTIVE
    if verb is FlagVerb.OFF:
        return stored if archived else FeatureFlagStatus.INACTIVE
    if verb is FlagVerb.ARCHIVE:
        return FeatureFlagStatus.ARCHIVED
    if verb is FlagVerb.UNARCHIVE:
        return FeatureFlagStatus.INACTIVE if archived else stored
    raise ValueError(f"unknown verb {verb!r}")  # pragma: no cover


class FeatureFlagService:
    """
    Service for managing feature flags in the platform.

    This service handles:
    - Creating and updating feature flags
    - Evaluating feature flags for users
    - Managing targeting rules and rollout percentages
    """

    def __init__(self, db: Session):
        """Initialize with a database session."""
        self.db = db

    def get_feature_flag(self, flag_id: Union[str, UUID]) -> Optional[FeatureFlagRead]:
        """
        Get a feature flag by ID.

        Args:
            flag_id: ID of the feature flag

        Returns:
            The feature flag's response representation, or None if not found
        """
        flag = self.db.query(FeatureFlag).filter(FeatureFlag.id == flag_id).first()

        if not flag:
            return None

        return flag_to_read(flag)

    def get_feature_flags(
        self, skip: int = 0, limit: int = 100, status: Optional[str] = None
    ) -> List[FeatureFlagRead]:
        """
        Get all feature flags with optional status filter.

        Args:
            skip: Number of records to skip for pagination
            limit: Maximum number of records to return
            status: Optional status filter

        Returns:
            List of feature flag response representations
        """
        query = self.db.query(FeatureFlag)

        if status:
            query = query.filter(FeatureFlag.status == status)

        flags = query.offset(skip).limit(limit).all()
        return [flag_to_read(flag) for flag in flags]

    def count_feature_flags(self, status: Optional[str] = None) -> int:
        """
        Count feature flags with optional status filter.

        Args:
            status: Optional status filter

        Returns:
            Count of matching feature flags
        """
        query = self.db.query(func.count(FeatureFlag.id))

        if status:
            query = query.filter(FeatureFlag.status == status)

        return query.scalar() or 0

    def create_feature_flag(
        self, flag_data: FeatureFlagCreate, owner_id: UUID
    ) -> FeatureFlag:
        """Create a new feature flag owned by *owner_id*.

        *owner_id* is required on purpose: the request's ``owner_id`` is
        read-only and removed below, so the creator is always the owner and a
        caller that forgets it would store a flag with no owner.

        The read-only fields (``READ_ONLY_FIELDS``) are removed here, not only
        by the route, because each of them names a column: one that reached the
        row would rewrite the primary key, the owner or a timestamp.  A sent
        ``status`` must equal the status ``is_active`` gives the new flag.

        Raises:
            FlagStatusReadOnly: a sent ``status`` differs from that status.
        """
        status = (
            FeatureFlagStatus.ACTIVE
            if flag_data.is_active
            else FeatureFlagStatus.INACTIVE
        )
        _check_sent_status(flag_data, status)

        flag_dict = flag_data.model_dump(exclude=READ_ONLY_FIELDS | {"is_active"})
        try:
            flag = FeatureFlag(**flag_dict, status=status, owner_id=owner_id)

            # Add and commit to DB
            self.db.add(flag)
            self.db.commit()
            self.db.refresh(flag)

            return flag
        except Exception as e:
            logger.error(f"Error creating feature flag: {e!s}")
            self.db.rollback()
            raise

    def update_feature_flag(
        self, flag_id: Union[str, UUID, FeatureFlag], flag_data: FeatureFlagUpdate
    ) -> Optional[FeatureFlag]:
        """Update an existing feature flag; returns the stored row, or None.

        Only the fields the request sent change.  The read-only fields are
        removed here (see ``create_feature_flag``), and a sent ``status`` must
        equal the stored one.

        ``is_active`` sets the status through :func:`transition`: true turns
        the flag on and false turns it off.  An ARCHIVED flag sent false stays
        ARCHIVED, so its GET body can be sent back unchanged; sent true, it is
        refused before anything is written.

        Raises:
            FlagStatusReadOnly: a sent ``status`` differs from the stored one.
            ArchivedFlagError: ``is_active: true`` on an archived flag.
        """
        # Handle if a FeatureFlag object was passed instead of an ID
        if isinstance(flag_id, FeatureFlag):
            flag = flag_id
        else:
            flag = self.db.query(FeatureFlag).filter(FeatureFlag.id == flag_id).first()

        if not flag:
            return None

        stored = FeatureFlagStatus(_status_value(flag.status))
        _check_sent_status(flag_data, stored)

        update_data = flag_data.model_dump(exclude_unset=True, exclude=READ_ONLY_FIELDS)
        if "is_active" in update_data:
            verb = FlagVerb.ON if update_data.pop("is_active") else FlagVerb.OFF
            update_data["status"] = transition(stored, verb)

        try:
            for field, value in update_data.items():
                setattr(flag, field, value)

            # Commit changes
            self.db.commit()
            self.db.refresh(flag)

            return flag
        except Exception as e:
            logger.error(f"Error updating feature flag: {e!s}")
            self.db.rollback()
            raise

    def delete_feature_flag(self, flag: FeatureFlag) -> None:
        """
        Delete a feature flag.

        Args:
            flag: Feature flag model object
        """
        flag_id = str(flag.id)
        flag_name = flag.name

        self.db.delete(flag)
        self.db.commit()

        logger.info(f"Deleted feature flag {flag_id}: {flag_name}")

    def activate_feature_flag(self, flag: FeatureFlag) -> FeatureFlagRead:
        """
        Activate a feature flag.

        Args:
            flag: Feature flag model object

        Returns:
            The updated feature flag's response representation

        Raises:
            ArchivedFlagError: the flag is archived.
        """
        flag.status = transition(flag.status, FlagVerb.ON).value
        flag.updated_at = datetime.now(timezone.utc)

        self.db.commit()
        self.db.refresh(flag)

        logger.info(f"Activated feature flag {flag.id}: {flag.name}")
        return flag_to_read(flag)

    def deactivate_feature_flag(self, flag: FeatureFlag) -> FeatureFlagRead:
        """
        Deactivate a feature flag.

        Args:
            flag: Feature flag model object

        Returns:
            The updated feature flag's response representation
        """
        new_status = transition(flag.status, FlagVerb.OFF)
        if flag_status_name(flag.status) == new_status.value:
            # Already off, or archived: nothing to write.
            return flag_to_read(flag)
        flag.status = new_status.value
        flag.updated_at = datetime.now(timezone.utc)

        self.db.commit()
        self.db.refresh(flag)

        logger.info(f"Deactivated feature flag {flag.id}: {flag.name}")
        return flag_to_read(flag)

    def get_user_flags(
        self, user_id: str, context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Get all active feature flags with their evaluation for a specific user.

        Args:
            user_id: ID of the user
            context: Optional context data for rule evaluation

        Returns:
            Dictionary mapping flag keys to their values for this user
        """
        # Get all active flags
        flags = (
            self.db.query(FeatureFlag)
            .filter(FeatureFlag.status == FeatureFlagStatus.ACTIVE.value)
            .all()
        )

        # Segment membership is resolved once for every segment any flag
        # references (#440). A flag fails only if it references a segment
        # whose membership could not be decided, so each flag answers here
        # as it does on /evaluate.
        referenced = set()
        for flag in flags:
            rules = flag.targeting_rules
            if rules and not isinstance(rules, list):
                referenced |= segment_ids_in(
                    normalise_targeting_rules(rules, owner=f"flag:{flag.key}")
                )
        memberships = None
        if referenced:
            memberships = resolve_segment_memberships(
                self.db, user_id, expand_context(context or {}), referenced
            )
            if memberships.unavailable:
                log_unavailable("feature flags", memberships.unavailable)

        # Evaluate each flag for the user
        result = {}
        for flag in flags:
            if memberships is None:
                result[flag.key] = self.evaluate_flag(flag, user_id, context)
            else:
                result[flag.key] = self.evaluate_flag(
                    flag, user_id, context, memberships=memberships
                )

        return result

    def evaluate_flag(
        self,
        flag: FeatureFlag,
        user_id: str,
        context: Optional[Dict[str, Any]] = None,
        memberships: Optional[SegmentMemberships] = None,
    ) -> bool:
        """
        Evaluate a feature flag for a specific user.

        Args:
            flag: Feature flag model object
            user_id: ID of the user
            context: Optional context data for rule evaluation

        Returns:
            Boolean indicating if the flag is enabled for this user
        """
        if memberships is None:
            return self.evaluate_flag_detailed(flag, user_id, context)["enabled"]
        return self.evaluate_flag_detailed(
            flag, user_id, context, memberships=memberships
        )["enabled"]

    def evaluate_flag_detailed(
        self,
        flag: FeatureFlag,
        user_id: str,
        context: Optional[Dict[str, Any]] = None,
        memberships: Optional[SegmentMemberships] = None,
    ) -> Dict[str, Any]:
        """
        Evaluate a feature flag for a user and explain the outcome.

        Targeting rules are honoured in every stored shape:

        * dashboard editor shape (``{"logical_operator", "groups": [...]}``)
          and native ``TargetingRules`` — converted by
          :mod:`backend.app.core.targeting_adapter` and matched by the Enhanced
          Rules Engine against ``expand_context(context)`` (dotted keys plus
          ``user.``/``device.``/``app.`` aliases); the first matching rule's
          ``rollout_percentage`` decides, using the same ``user_id:flag.key``
          hash as the global rollout;
        * legacy list shape (``[{"type": "user_id"|"context", ...}]``) —
          evaluated by :meth:`_evaluate_rule` exactly as before.

        When no rule matches (or the rules cannot be interpreted) the flag's
        global ``rollout_percentage`` decides.

        When a pattern (``regex``) condition anywhere in the rules cannot be
        evaluated (RE2 refuses the pattern, or the value is too long or not
        encodable), the whole ruleset is abandoned: the flag is disabled with
        reason ``error``. Neither the global rollout nor a ``default_rule`` is
        consulted, and no error-log row is written, so the safety monitor does
        not count it.

        A rule that uses a segment (``in_segment``/``not_in_segment``, #440)
        is matched on membership resolved from the database for ``user_id``
        (:mod:`backend.app.services.segment_membership`), never on the
        context. When any referenced segment's membership cannot be decided
        (unknown, not active, rules not valid, a database error), the flag is
        disabled with reason ``error``, one WARNING names the segments, and
        no error-log row is written.

        Args:
            flag: Feature flag model object
            user_id: ID of the user
            context: Optional context data for rule evaluation
            memberships: Membership already resolved for this user
                (``get_user_flags`` resolves every flag's segments at once).
                Its unavailable segments were logged by the caller.

        Returns:
            ``{"enabled": bool, "reason": "targeting_rule" | "rollout" |
            "inactive" | "error", "rule_id": Optional[str]}``
        """
        # Track evaluation time for latency metrics
        start_time = time.time()

        # Initialize tracking variables
        targeting_rule_id: Optional[str] = None
        result = False
        reason = REASON_ROLLOUT
        error = None

        try:
            # If flag is not active, return False
            # Handle both enum member (FeatureFlagStatus.ACTIVE) and string value ("ACTIVE")
            # SQLAlchemy returns the enum member when reading from the DB column,
            # but some code paths store the raw string value.
            if flag.status not in (
                FeatureFlagStatus.ACTIVE,
                FeatureFlagStatus.ACTIVE.value,
            ):
                result = False
                reason = REASON_INACTIVE
            else:
                matched = self._match_targeting_rule(
                    flag, user_id, context or {}, memberships
                )
                if matched is not None:
                    targeting_rule_id, rule_percentage = matched
                    reason = REASON_TARGETING_RULE
                    result = self._evaluate_percentage_rollout(
                        flag, user_id, rule_percentage
                    )
                else:
                    # No rules, no match, or an uninterpretable shape: global rollout
                    reason = REASON_ROLLOUT
                    result = self._evaluate_percentage_rollout(flag, user_id)

        except PatternUnevaluable as exc:
            # Abandon the whole ruleset: falling through to the global rollout
            # would serve the flag to users a rule was written to leave out.
            report_unevaluable(exc, f"flag:{flag.key}")
            targeting_rule_id = None
            result = False
            reason = REASON_ERROR

        except TimeWindowUnreadable:
            # The same for a time_window the engine cannot read (#1145),
            # already reported by the rules engine. No error-log row: the
            # rule, not the flag's behaviour, is at fault.
            targeting_rule_id = None
            result = False
            reason = REASON_ERROR

        except SegmentMembershipUnavailable as exc:
            # The same: membership of a referenced segment is unknown, so the
            # ruleset is abandoned. No error-log row: the safety monitor
            # counts those, and a segment archived or a database blip is not
            # the flag misbehaving.
            if memberships is None:
                log_unavailable(f"flag:{flag.key}", exc.segment_ids)
            targeting_rule_id = None
            result = False
            reason = REASON_ERROR

        except Exception as e:
            # Log and record the error
            error = str(e)
            # The type only: the exception's text can repeat a user's value.
            # The full text is still recorded in error_logs below.
            logger.error(f"Error evaluating flag {flag.key}: {type(e).__name__}")

            # Default behavior on error is to return False
            result = False
            reason = REASON_ERROR

        finally:
            # Calculate latency
            latency_ms = (time.time() - start_time) * 1000  # Convert to milliseconds

            try:
                # Record metrics (in a separate try-except to avoid affecting the main flow)
                MetricsService.record_flag_evaluation(
                    db=self.db,
                    feature_flag_id=flag.id,
                    user_id=user_id,
                    value=result,
                    targeting_rule_id=targeting_rule_id,
                    latency_ms=latency_ms,
                    metadata={
                        "context": context,
                    },
                )

                # If there was an error, log it separately
                if error:
                    error_data = ErrorLogCreate(
                        error_type="flag_evaluation_error",
                        feature_flag_id=flag.id,
                        user_id=user_id,
                        message=error,
                        request_data={"context": context},
                    )
                    MetricsService.log_error(db=self.db, data=error_data)
            except Exception as metrics_error:
                # Don't let metrics collection errors affect flag evaluation
                # The type only: a database error's text repeats the row's
                # parameters, the user id among them.
                logger.error(
                    f"Failed to record metrics: {type(metrics_error).__name__}"
                )

        return {"enabled": result, "reason": reason, "rule_id": targeting_rule_id}

    def _match_targeting_rule(
        self,
        flag: FeatureFlag,
        user_id: str,
        context: Dict[str, Any],
        memberships: Optional[SegmentMemberships] = None,
    ) -> Optional[Tuple[str, int]]:
        """
        Find the first targeting rule of ``flag`` that matches ``user_id``/``context``.

        Returns:
            ``(rule_id, rollout_percentage)`` for the matching rule, or ``None``
            when the flag has no rules, none match, or the stored shape cannot
            be interpreted (the caller then applies the global rollout).

        Raises:
            SegmentMembershipUnavailable: the rules reference a segment whose
                membership could not be decided.
        """
        rules = flag.targeting_rules
        if not rules:
            return None

        # Legacy list shape — evaluated in-service, unchanged.
        if isinstance(rules, list):
            for i, rule in enumerate(rules):
                if not isinstance(rule, dict):
                    continue
                rule_id = str(rule.get("id", f"rule_{i}"))
                if self._evaluate_rule(rule, user_id, context):
                    percentage = rule.get("percentage", 100)
                    return rule_id, 100 if percentage is None else int(percentage)
            return None

        # Dashboard / native shapes — Enhanced Rules Engine.
        native_rules = normalise_targeting_rules(rules, owner=f"flag:{flag.key}")
        if native_rules is None:
            return None
        expanded = expand_context(context)
        segment_ids = segment_ids_in(native_rules)
        members = None
        if segment_ids:
            if memberships is None:
                memberships = resolve_segment_memberships(
                    self.db, user_id, expanded, segment_ids
                )
            undecided = memberships.undecided(segment_ids)
            if undecided:
                raise SegmentMembershipUnavailable(undecided)
            members = memberships.members
        matched = match_targeting_rule(
            native_rules, with_memberships(expanded, members)
        )
        if matched is None:
            return None
        return matched.id, int(matched.rollout_percentage)

    def _evaluate_rule(
        self, rule: Dict[str, Any], user_id: str, context: Dict[str, Any]
    ) -> bool:
        """
        Evaluate a targeting rule for a user.

        A ``context`` condition matches only through one of the operators in
        ``_LEGACY_OPERATORS``; any other operator, or none, does not match.

        Args:
            rule: Rule dictionary from feature flag
            user_id: ID of the user
            context: Context data for rule evaluation

        Returns:
            Boolean indicating if the rule matches
        """
        # Get rule type and conditions
        rule_type = rule.get("type", "")
        conditions = rule.get("conditions", [])

        # Handle different rule types
        if rule_type == "user_id":
            # Direct user ID match
            target_ids = rule.get("user_ids", [])
            return user_id in target_ids

        elif rule_type == "context":
            # Context attribute match (all conditions must match)
            if not conditions:
                return False

            for condition in conditions:
                attribute = condition.get("attribute", "")
                operator = condition.get("operator", "")
                value = condition.get("value")

                # Skip if attribute not in context
                if attribute not in context:
                    return False

                # Get context value
                context_value = context[attribute]

                # Evaluate based on operator
                if operator == "eq" and context_value != value:
                    return False
                elif operator == "ne" and context_value == value:
                    return False
                elif operator == "gt" and not (
                    isinstance(context_value, (int, float)) and context_value > value
                ):
                    return False
                elif operator == "lt" and not (
                    isinstance(context_value, (int, float)) and context_value < value
                ):
                    return False
                elif operator == "contains" and not (
                    isinstance(context_value, str)
                    and isinstance(value, str)
                    and value in context_value
                ):
                    return False
                elif operator == "in" and not (
                    isinstance(value, (list, tuple)) and context_value in value
                ):
                    return False
                elif operator not in _LEGACY_OPERATORS:
                    # Any other operator, or none, does not match (#733), as
                    # in the Lambda evaluator's ``evaluate_targeting_rules``
                    # (backend/lambda/feature_flag_evaluation/evaluator.py:181).
                    return False

            # All conditions passed
            return True

        # Unknown rule type
        return False

    def _evaluate_percentage_rollout(
        self, flag: FeatureFlag, user_id: str, percentage: Optional[int] = None
    ) -> bool:
        """
        Evaluate percentage-based rollout for a user.

        Args:
            flag: Feature flag model object
            user_id: ID of the user
            percentage: Rollout percentage (0-100), defaults to flag's percentage

        Returns:
            Boolean indicating if the user is in the rollout
        """
        # Use flag's percentage if not specified
        if percentage is None:
            percentage = flag.rollout_percentage or 0

        # If percentage is 0 or 100, short-circuit
        if percentage <= 0:
            return False
        if percentage >= 100:
            return True

        # Create a hash using user ID and flag key for deterministic assignment
        hash_input = f"{user_id}:{flag.key}"
        hash_value = int(
            hashlib.md5(hash_input.encode(), usedforsecurity=False).hexdigest(), 16
        )

        # Get bucket (0-99)
        bucket = hash_value % 100

        # User is in the rollout if their bucket is less than the percentage
        return bucket < percentage
