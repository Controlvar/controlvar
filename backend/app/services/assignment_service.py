# Experiment assignment service
# Analysis and reporting service
# backend/app/services/assignment_service.py
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union
from uuid import UUID

from sqlalchemy import and_, desc, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from backend.app.core.consistent_hash import bucket_of
from backend.app.core.log_once import EVALUATION_NOTES
from backend.app.core.rules_engine import SegmentMembershipUnavailable
from backend.app.core.targeting_adapter import (
    _is_dashboard_rules_shape,
    expand_context,
    normalise_targeting_rules,
    segment_ids_in,
)
from backend.app.models.assignment import Assignment
from backend.app.models.experiment import Experiment, ExperimentStatus, Variant
from backend.app.models.global_holdout import GlobalHoldout
from backend.app.models.holdout_population import HoldoutPopulation
from backend.app.schemas.targeting_rule import TargetingRules
from backend.app.services.event_service import EventService
from backend.app.services.global_holdout_service import GlobalHoldoutService
from backend.app.services.mutual_exclusion_service import MutualExclusionService
from backend.app.services.rules_evaluation_service import RulesEvaluationService
from backend.app.services.segment_membership import (
    log_unavailable,
    resolve_segment_memberships,
    with_memberships,
)

logger = logging.getLogger(__name__)

# Eligibility reasons returned by ``assign_user`` / ``check_eligibility``.
REASON_ASSIGNED = "assigned"
REASON_HOLDOUT = "holdout"
REASON_MUTUAL_EXCLUSION = "mutual_exclusion"
REASON_TARGETING = "targeting"

#: Postgres SQLSTATE for a unique violation.
_UNIQUE_VIOLATION = "23505"


def _is_unique_violation(exc: IntegrityError) -> bool:
    """True when the database refused *exc*'s write as a duplicate.

    Decided from the driver's SQLSTATE, never from the message. Which index
    refused it is settled by looking the row up afterwards, not by naming the
    index: its name depends on how the schema was built.
    """
    orig = getattr(exc, "orig", None)
    code = getattr(orig, "pgcode", None) or getattr(orig, "sqlstate", None)
    return code == _UNIQUE_VIOLATION


#: Keys of a native ``TargetingRules`` value that may be present while it
#: legitimately carries no rules (``{"rules": []}``, ``{"version": "1.0"}``).
_EMPTY_NATIVE_KEYS = frozenset({"rules", "version"})


def _note_ignored_rules(raw: Dict[str, Any], rules: TargetingRules, owner: str) -> None:
    """Warn, once per experiment per process, that stored rules yield nothing.

    A flat or unknown-key dict (``{"country": ["US"]}``) reaches
    ``TargetingRules(**raw)``, which ignores unknown keys, so it becomes "no
    rules" and everyone is eligible. Only the owner (the experiment id) is
    named: the value can carry anything.
    """
    if rules.rules or rules.default_rule is not None:
        return
    if not raw or set(raw) <= _EMPTY_NATIVE_KEYS:
        return
    if EVALUATION_NOTES.first(owner, "stored rules yield no rules"):
        logger.warning(
            "Targeting rules for %s contain no rules assignment can apply; "
            "every user is eligible. Logged once.",
            owner,
        )


class AssignmentService:
    """
    Service for managing user assignments to experiment variants.

    This service handles:
    - Assigning users to experiment variants using deterministic hashing
    - Eligibility checks for new users (global holdout, mutual exclusion
      groups, targeting rules)
    - Tracking user assignments
    - Retrieving user assignments for experiments
    - Managing sticky assignments
    """

    def __init__(self, db: Session):
        """Initialize with a database session."""
        self.db = db
        self.event_service = EventService(db)
        self.rules_evaluation_service = RulesEvaluationService()
        self.global_holdout_service = GlobalHoldoutService(db)
        self.mutual_exclusion_service = MutualExclusionService(db)

    def get_assignment(
        self, user_id: str, experiment_id: Union[str, UUID]
    ) -> Optional[Dict[str, Any]]:
        """
        Get a user's assignment for a specific experiment.

        Args:
            user_id: ID of the user
            experiment_id: ID of the experiment

        Returns:
            Dictionary containing the assignment data or None if not found
        """
        assignment = (
            self.db.query(Assignment)
            .filter(
                Assignment.user_id == user_id, Assignment.experiment_id == experiment_id
            )
            .order_by(desc(Assignment.created_at))
            .first()
        )

        if not assignment:
            return None

        # Get variant details
        variant = (
            self.db.query(Variant).filter(Variant.id == assignment.variant_id).first()
        )

        return {
            "id": str(assignment.id),
            "user_id": assignment.user_id,
            "experiment_id": str(assignment.experiment_id),
            "variant_id": str(assignment.variant_id),
            "variant_name": variant.name if variant else None,
            "is_control": variant.is_control if variant else None,
            "created_at": (
                assignment.created_at.isoformat()
                if hasattr(assignment.created_at, "isoformat")
                else assignment.created_at
            ),
            "updated_at": (
                assignment.updated_at.isoformat()
                if hasattr(assignment.updated_at, "isoformat")
                else assignment.updated_at
            ),
        }

    def assign_user(
        self,
        user_id: str,
        experiment_id: Union[str, UUID],
        track_exposure: bool = True,
        override_variant_id: Optional[Union[str, UUID]] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Assign a user to an experiment variant.

        New users go through the eligibility checks (global holdout, mutual
        exclusion group, targeting rules — in that order) before a variant is
        chosen.  Ineligible users get no ``Assignment`` row and no exposure
        event; the returned dict carries ``assigned=False``, the ``reason``
        (``holdout`` | ``mutual_exclusion`` | ``targeting``) and the control
        variant so callers can fall back to the default experience.  Existing
        (sticky) assignments always win and are returned unchanged even if the
        user would no longer be eligible.

        Args:
            user_id: ID of the user to assign
            experiment_id: ID of the experiment
            track_exposure: Whether to track an exposure event
            override_variant_id: Optional variant ID to force assignment
            context: Optional context data for targeting

        Returns:
            Dictionary containing the assignment data plus ``assigned`` and
            ``reason``

        Raises:
            ValueError: If the experiment is not active or has issues
        """
        # Get experiment with variants
        experiment = (
            self.db.query(Experiment)
            .options(joinedload(Experiment.variants))
            .filter(Experiment.id == experiment_id)
            .first()
        )

        if not experiment:
            raise ValueError(f"Experiment {experiment_id} not found")

        # Check experiment status
        if experiment.status != ExperimentStatus.ACTIVE:
            raise ValueError(
                f"Cannot assign users to experiment with status: {experiment.status}"
            )

        # Check for existing assignment (sticky assignment)
        existing_assignment = self._stored_assignment(user_id, experiment_id)

        if existing_assignment:
            logger.debug(f"Using existing assignment in experiment {experiment_id}")
            return self._sticky_result(
                user_id, experiment_id, existing_assignment, track_exposure, context
            )

        # A new user.  The active holdout is loaded once.  When it is
        # measurable the user is recorded in its population whatever the
        # answer below is (held out, assigned or refused): every user seen
        # goes in their arm.  The INSERT is executed, not committed: the
        # assigned path commits it with the assignment, the ineligible paths
        # once, after building their result (#445).
        holdout = self.global_holdout_service.get_active_holdout()
        holdout_check = self.global_holdout_service.is_user_in_holdout(
            user_id, holdout=holdout
        )
        recorded = self._record_holdout_population(holdout, user_id, holdout_check[0])

        # Eligibility gate for new users: holdout -> mutual exclusion -> targeting
        eligibility = self.check_eligibility(
            user_id, experiment, context, holdout_check=holdout_check
        )
        if not eligibility["eligible"]:
            logger.debug(
                f"Not eligible for experiment {experiment_id}: {eligibility['reason']}"
            )
            result = self._ineligible_result(user_id, experiment, eligibility)
            if recorded:
                self.db.commit()
            return result

        # Determine variant assignment
        if override_variant_id:
            # Use override if provided (useful for forcing assignment or testing)
            variant_id = override_variant_id

            # Verify override variant is valid for this experiment
            variant_valid = any(
                v.id == override_variant_id for v in experiment.variants
            )
            if not variant_valid:
                raise ValueError(
                    f"Override variant {override_variant_id} not valid for experiment {experiment_id}"
                )
        else:
            # Use deterministic hashing to assign variant
            variant_id = self._hash_user_to_variant(user_id, experiment)

        # Create new assignment
        assignment = Assignment(
            user_id=user_id,
            experiment_id=experiment_id,
            variant_id=variant_id,
        )

        self.db.add(assignment)
        try:
            self.db.commit()
        except IntegrityError as exc:
            # The check above and this insert are two statements, and another
            # API process can store this user's first assignment between them:
            # the unique index on (experiment_id, user_id) then refuses this
            # row (#1026). The user is assigned, so the answer is the stored
            # row, given as a sticky hit gives it. Any other refusal, or a
            # duplicate with no stored row to answer, is raised as before.
            self.db.rollback()
            stored = (
                self._stored_assignment(user_id, experiment_id)
                if _is_unique_violation(exc)
                else None
            )
            if stored is None:
                raise
            logger.info(
                "First assignment in experiment %s was stored by a concurrent "
                "request; answering the stored row",
                experiment_id,
            )
            if recorded:
                # The rollback undid this request's population row. The write
                # is idempotent, so the user is recorded once whichever
                # request stored the assignment.
                self._record_holdout_population(holdout, user_id, holdout_check[0])
                self.db.commit()
            return self._sticky_result(
                user_id, experiment_id, stored, track_exposure, context
            )
        self.db.refresh(assignment)

        logger.debug(f"Assigned variant {variant_id} in experiment {experiment_id}")

        # Track exposure event if requested
        if track_exposure:
            self.event_service.track_exposure(
                user_id=user_id,
                experiment_id=str(experiment_id),
                variant_id=str(variant_id),
                properties=context,
            )

        # Get full assignment details
        return self._assigned_result(user_id, experiment_id)

    # ------------------------------------------------------------------
    # Eligibility (global holdout, mutual exclusion, targeting)
    # ------------------------------------------------------------------

    def _record_holdout_population(
        self, holdout: Optional[GlobalHoldout], user_id: str, in_holdout: bool
    ) -> bool:
        """Record ``user_id`` in a measurable holdout's population; True if run.

        ``INSERT ... ON CONFLICT DO NOTHING``, executed in the caller's
        transaction and NOT committed here: a commit mid-path would expire
        the loaded experiment and holdout and cost reloads under the
        production session.  A failed write propagates, like a failed
        assignment insert: a silently lost row would shrink one arm.
        Nothing runs when no holdout is active, or the active one is not
        measurable (legacy salt or no ``activated_at``).
        """
        if holdout is None or not holdout.is_measurable:
            return False
        self.db.execute(
            pg_insert(HoldoutPopulation)
            .values(
                holdout_id=holdout.id,
                user_id=user_id,
                in_holdout=bool(in_holdout),
                first_seen_at=datetime.now(timezone.utc),
            )
            .on_conflict_do_nothing(index_elements=["holdout_id", "user_id"])
        )
        return True

    def check_eligibility(
        self,
        user_id: str,
        experiment: Experiment,
        context: Optional[Dict[str, Any]] = None,
        holdout_check: Optional[Tuple[bool, int, int]] = None,
    ) -> Dict[str, Any]:
        """
        Decide whether a *new* user may be assigned to ``experiment``.

        Checks run in order and the first failure wins:

        1. global holdout — an active ``GlobalHoldout`` that buckets the user
           (reason ``holdout``);
        2. mutual exclusion — the experiment belongs to a group and either the
           group's consistent hashing selects a different experiment for the
           user, or the user already holds an assignment in another experiment
           of the group that is not completed or archived (reason
           ``mutual_exclusion``);
        3. targeting — the experiment has targeting rules and the user context
           does not match them (reason ``targeting``).

        Sticky assignments are handled by ``assign_user`` before this runs.
        ``holdout_check`` is ``GlobalHoldoutService.is_user_in_holdout``'s
        answer when the caller already has it (``assign_user`` does, from the
        holdout it loaded once); left out, it is computed here.

        Returns:
            ``{"eligible": bool, "reason": str, "detail": Optional[str]}``
            (``reason`` is ``assigned`` when eligible).
        """
        # 1. Global holdout
        if holdout_check is None:
            holdout_check = self.global_holdout_service.is_user_in_holdout(user_id)
        in_holdout, holdout_percentage, bucket = holdout_check
        if in_holdout:
            return {
                "eligible": False,
                "reason": REASON_HOLDOUT,
                "detail": (
                    f"User is in the global holdout "
                    f"({holdout_percentage}%, bucket {bucket})"
                ),
            }

        # 2. Mutual exclusion group
        group_id = getattr(experiment, "mutual_exclusion_group_id", None)
        if group_id:
            if not self.mutual_exclusion_service.is_user_eligible_for_experiment(
                user_id, experiment.id
            ):
                return {
                    "eligible": False,
                    "reason": REASON_MUTUAL_EXCLUSION,
                    "detail": (
                        f"Mutual exclusion group {group_id} selected another "
                        f"experiment (or none) for this user, or the user is "
                        f"already enrolled in another experiment of the group"
                    ),
                }

        # 3. Targeting rules
        if getattr(experiment, "targeting_rules", None):
            targeting_context = self._build_targeting_context(user_id, context)
            targeting = self._evaluate_experiment_targeting(
                experiment, targeting_context, user_id=user_id
            )
            if not targeting["eligible"]:
                return {
                    "eligible": False,
                    "reason": REASON_TARGETING,
                    "detail": targeting.get("reason"),
                }

        return {"eligible": True, "reason": REASON_ASSIGNED, "detail": None}

    @staticmethod
    def _build_targeting_context(
        user_id: str, context: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Request context + ``user_id`` (for rollout bucketing), expanded with the
        shared alias rule (``country`` also answers ``user.country`` etc.)."""
        raw = dict(context or {})
        raw.setdefault("user_id", user_id)
        return expand_context(raw)

    def _assigned_result(
        self, user_id: str, experiment_id: Union[str, UUID]
    ) -> Dict[str, Any]:
        """``get_assignment`` result tagged as an actual assignment."""
        result = self.get_assignment(user_id, experiment_id) or {}
        result["assigned"] = True
        result["reason"] = REASON_ASSIGNED
        return result

    def _stored_assignment(
        self, user_id: str, experiment_id: Union[str, UUID]
    ) -> Optional[Assignment]:
        """The user's stored assignment in the experiment, or None."""
        return (
            self.db.query(Assignment)
            .filter(
                Assignment.user_id == user_id, Assignment.experiment_id == experiment_id
            )
            .order_by(desc(Assignment.created_at))
            .first()
        )

    def _sticky_result(
        self,
        user_id: str,
        experiment_id: Union[str, UUID],
        stored: Assignment,
        track_exposure: bool,
        context: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """The answer for a user who already holds *stored*.

        That row's variant, unchanged, with the user recorded as seen in it
        on every call that asks for the record.
        """
        variant_id = str(stored.variant_id)
        assignment_dict = self._assigned_result(user_id, experiment_id)
        if track_exposure:
            self.event_service.track_exposure(
                user_id=user_id,
                experiment_id=str(experiment_id),
                variant_id=variant_id,
                properties=context,
            )
        return assignment_dict

    @staticmethod
    def _control_variant(experiment: Experiment) -> Variant:
        """The experiment's control variant (first variant when none is flagged)."""
        variants = list(experiment.variants or [])
        if not variants:
            raise ValueError(f"Experiment {experiment.id} has no variants")
        return next((v for v in variants if v.is_control), variants[0])

    def _ineligible_result(
        self, user_id: str, experiment: Experiment, eligibility: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Assignment-shaped dict for a user that was not assigned.

        Carries the control variant so SDKs (which treat the response as the
        variant) render the default experience; ``id``/timestamps are ``None``
        because no ``Assignment`` row exists.
        """
        control = self._control_variant(experiment)
        return {
            "id": None,
            "user_id": user_id,
            "experiment_id": str(experiment.id),
            "variant_id": str(control.id),
            "variant_name": control.name,
            "is_control": bool(control.is_control),
            "created_at": None,
            "updated_at": None,
            "assigned": False,
            "reason": eligibility["reason"],
            "detail": eligibility.get("detail"),
        }

    def get_user_assignments(
        self, user_id: str, active_only: bool = True
    ) -> List[Dict[str, Any]]:
        """
        Get all assignments for a user.

        Args:
            user_id: ID of the user
            active_only: If True, only return assignments for active experiments

        Returns:
            List of assignment dictionaries
        """
        # Base query
        query = self.db.query(Assignment).filter(Assignment.user_id == user_id)

        if active_only:
            # Join with experiments to filter by status
            query = query.join(
                Experiment, Assignment.experiment_id == Experiment.id
            ).filter(Experiment.status == ExperimentStatus.ACTIVE)

        # Get latest assignment for each experiment
        subq = (
            self.db.query(
                Assignment.experiment_id,
                func.max(Assignment.created_at).label("latest_assignment"),
            )
            .filter(Assignment.user_id == user_id)
            .group_by(Assignment.experiment_id)
            .subquery()
        )

        query = query.join(
            subq,
            and_(
                Assignment.experiment_id == subq.c.experiment_id,
                Assignment.created_at == subq.c.latest_assignment,
            ),
        )

        assignments = query.all()

        # Format results
        results = []
        for assignment in assignments:
            # Get variant details
            variant = (
                self.db.query(Variant)
                .filter(Variant.id == assignment.variant_id)
                .first()
            )

            # Get experiment details
            experiment = (
                self.db.query(Experiment)
                .filter(Experiment.id == assignment.experiment_id)
                .first()
            )

            results.append(
                {
                    "id": str(assignment.id),
                    "user_id": assignment.user_id,
                    "experiment_id": str(assignment.experiment_id),
                    "experiment_name": experiment.name if experiment else None,
                    "variant_id": str(assignment.variant_id),
                    "variant_name": variant.name if variant else None,
                    "is_control": variant.is_control if variant else None,
                    "created_at": (
                        assignment.created_at.isoformat()
                        if hasattr(assignment.created_at, "isoformat")
                        else assignment.created_at
                    ),
                }
            )

        return results

    def assign_user_with_targeting(
        self,
        user_id: str,
        experiment_id: Union[str, UUID],
        user_context: Dict[str, Any],
        track_exposure: bool = True,
    ) -> Dict[str, Any]:
        """
        Assign a user to an experiment variant using targeting rules.

        Args:
            user_id: ID of the user to assign
            experiment_id: ID of the experiment
            user_context: User context for targeting evaluation
            track_exposure: Whether to track an exposure event

        Returns:
            Dictionary containing the assignment data and targeting info

        Raises:
            ValueError: If the experiment is not active or targeting fails
        """
        # Get experiment with variants
        experiment = (
            self.db.query(Experiment)
            .options(joinedload(Experiment.variants))
            .filter(Experiment.id == experiment_id)
            .first()
        )

        if not experiment:
            raise ValueError(f"Experiment {experiment_id} not found")

        # Check experiment status
        if experiment.status != ExperimentStatus.ACTIVE:
            raise ValueError(
                f"Cannot assign users to experiment with status: {experiment.status}"
            )

        # Ensure user_id is in context
        if "user_id" not in user_context:
            user_context["user_id"] = user_id

        # Check for existing assignment (sticky assignment)
        existing_assignment = (
            self.db.query(Assignment)
            .filter(
                Assignment.user_id == user_id, Assignment.experiment_id == experiment_id
            )
            .order_by(desc(Assignment.created_at))
            .first()
        )

        if existing_assignment:
            logger.debug(f"Using existing assignment in experiment {experiment_id}")
            assignment_dict = self.get_assignment(user_id, experiment_id)

            # Add targeting info
            assignment_dict.update(
                {
                    "targeting_matched": True,
                    "targeting_rule_id": "existing_assignment",
                    "user_context_validated": True,
                }
            )

            # Optionally track exposure event
            if track_exposure:
                self.event_service.track_exposure(
                    user_id=user_id,
                    experiment_id=str(experiment_id),
                    variant_id=str(existing_assignment.variant_id),
                    properties=user_context,
                )

            return assignment_dict

        # Evaluate targeting rules if experiment has them
        targeting_result = self._evaluate_experiment_targeting(
            experiment,
            user_context,
            user_id=user_id,
        )

        if not targeting_result["eligible"]:
            # User doesn't match targeting criteria
            logger.debug(f"Not eligible for experiment {experiment_id}: targeting")
            return {
                "assignment": None,
                "targeting_matched": False,
                "targeting_rule_id": None,
                "reason": targeting_result["reason"],
                "user_context_validated": targeting_result.get(
                    "validation_passed", True
                ),
                "evaluation_metrics": targeting_result.get("metrics"),
            }

        # Determine variant assignment
        variant_id = self._hash_user_to_variant(user_id, experiment)

        # Create new assignment
        assignment = Assignment(
            user_id=user_id,
            experiment_id=experiment_id,
            variant_id=variant_id,
        )

        self.db.add(assignment)
        self.db.commit()
        self.db.refresh(assignment)

        logger.debug(
            f"Assigned variant {variant_id} in experiment {experiment_id} via targeting"
        )

        # Track exposure event if requested
        if track_exposure:
            # Include targeting information in exposure event
            exposure_properties = user_context.copy()
            exposure_properties.update(
                {
                    "targeting_rule_id": targeting_result.get("rule_id"),
                    "targeting_matched": True,
                }
            )

            self.event_service.track_exposure(
                user_id=user_id,
                experiment_id=str(experiment_id),
                variant_id=str(variant_id),
                properties=exposure_properties,
            )

        # Get full assignment details and add targeting info
        assignment_dict = self.get_assignment(user_id, experiment_id)
        assignment_dict.update(
            {
                "targeting_matched": True,
                "targeting_rule_id": targeting_result.get("rule_id"),
                "user_context_validated": targeting_result.get(
                    "validation_passed", True
                ),
                "evaluation_metrics": targeting_result.get("metrics"),
            }
        )

        return assignment_dict

    def _evaluate_experiment_targeting(
        self,
        experiment: Experiment,
        user_context: Dict[str, Any],
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Evaluate if a user meets experiment targeting criteria.

        A rule that uses a segment (#440) is matched on membership resolved
        for ``user_id`` -- the request's user, never a ``user_id`` in the
        context -- by :mod:`backend.app.services.segment_membership`. When a
        referenced segment's membership cannot be decided, the user is not
        eligible and one WARNING names the segments.

        Args:
            experiment: The experiment model
            user_context: User context for evaluation (expanded by
                ``check_eligibility``)
            user_id: The user being assigned

        Returns:
            Dictionary with targeting evaluation results
        """
        try:
            # Check if experiment has targeting rules
            if (
                not hasattr(experiment, "targeting_rules")
                or not experiment.targeting_rules
            ):
                # No targeting rules - allow all users
                return {
                    "eligible": True,
                    "rule_id": None,
                    "reason": "No targeting rules defined",
                    "validation_passed": True,
                }

            # Parse targeting rules (native TargetingRules or dashboard shape)
            owner = f"experiment:{getattr(experiment, 'id', None)}"
            targeting_rules, skip_reason = self._coerce_targeting_rules(
                experiment.targeting_rules, owner=owner
            )
            if targeting_rules is None:
                return {
                    "eligible": True,
                    "rule_id": None,
                    "reason": skip_reason or "No targeting rules defined",
                    "validation_passed": True,
                }

            # Segment membership, resolved from the database (#440). The
            # context's own membership key is always removed.
            segment_ids = segment_ids_in(targeting_rules)
            members = None
            if segment_ids:
                if user_id is None:
                    raise SegmentMembershipUnavailable(segment_ids)
                memberships = resolve_segment_memberships(
                    self.db, user_id, user_context, segment_ids
                )
                undecided = memberships.undecided(segment_ids)
                if undecided:
                    raise SegmentMembershipUnavailable(undecided)
                members = memberships.members

            # Evaluate rules
            (
                matched_rule,
                metrics,
            ) = self.rules_evaluation_service.evaluate_rules_with_validation(
                targeting_rules=targeting_rules,
                user_context=with_memberships(user_context, members),
                track_metrics=True,
                owner=owner,
            )

            if matched_rule:
                return {
                    "eligible": True,
                    "rule_id": matched_rule.id,
                    "reason": f"Matched targeting rule: {matched_rule.name or matched_rule.id}",
                    "validation_passed": metrics.error is None if metrics else True,
                    "metrics": metrics,
                }
            else:
                return {
                    "eligible": False,
                    "rule_id": None,
                    "reason": "No targeting rules matched",
                    "validation_passed": metrics.error is None if metrics else True,
                    "metrics": metrics,
                }

        except SegmentMembershipUnavailable as exc:
            log_unavailable(
                f"experiment:{getattr(experiment, 'id', None)}", exc.segment_ids
            )
            return {
                "eligible": False,
                "rule_id": None,
                "reason": "Segment membership could not be decided",
                "validation_passed": False,
            }

        except Exception as e:
            # The type only: the exception's text can repeat an attribute value.
            logger.error(
                f"Error evaluating targeting for experiment {getattr(experiment, 'id', None)}: "
                f"{type(e).__name__}"
            )
            return {
                "eligible": False,
                "rule_id": None,
                "reason": "Targeting evaluation error",
                "validation_passed": False,
            }

    @staticmethod
    def _coerce_targeting_rules(
        raw: Any, owner: str = "targeting rules"
    ) -> Tuple[Optional[TargetingRules], Optional[str]]:
        """
        Turn the stored ``targeting_rules`` value into a ``TargetingRules``.

        Accepts a JSON string, the native ``TargetingRules`` dict (has ``rules``),
        the dashboard editor shape (``{"logical_operator", "groups": [...]}``,
        converted by ``targeting_adapter.normalise_targeting_rules``) or an
        existing ``TargetingRules`` instance. ``owner`` names the rules in
        the adapter's warning when dashboard rules cannot be converted.

        Returns:
            ``(rules, None)`` when there is something to evaluate, or
            ``(None, why)`` when the value carries no evaluable rules — callers
            treat that as "no targeting rules" (everyone eligible).
        """
        no_rules = "Targeting rules contain no evaluable rules"
        rules: Any
        if isinstance(raw, TargetingRules):
            rules = raw
        else:
            if isinstance(raw, str):
                raw = json.loads(raw)

            if isinstance(raw, dict):
                if _is_dashboard_rules_shape(raw):
                    # The adapter returns None for dashboard rules it cannot
                    # convert (and logs why); that means "no rules", not "nobody".
                    rules = normalise_targeting_rules(raw, owner=owner)
                    if rules is None:
                        return None, no_rules
                else:
                    rules = TargetingRules(**raw)
                    _note_ignored_rules(raw, rules, owner)
            elif isinstance(raw, list):
                # Legacy feature-flag list shape; never valid for experiments.
                logger.warning(
                    "List-shaped experiment targeting rules are not supported; "
                    "treating as no targeting rules"
                )
                return None, "List-shaped targeting rules ignored"
            else:
                # Assume it's already a TargetingRules-like object
                rules = raw

        if not rules.rules and rules.default_rule is None:
            return None, no_rules
        return rules, None

    def get_targeting_performance_stats(self) -> Dict[str, Any]:
        """
        Get performance statistics for targeting evaluations.

        Returns:
            Dictionary with performance metrics
        """
        return self.rules_evaluation_service.get_performance_stats()

    def clear_targeting_metrics(self):
        """Clear collected targeting metrics."""
        self.rules_evaluation_service.clear_metrics()

    def reassign_user(
        self,
        user_id: str,
        experiment_id: Union[str, UUID],
        variant_id: Optional[Union[str, UUID]] = None,
        track_exposure: bool = True,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Reassign a user to a new variant in an experiment.

        Args:
            user_id: ID of the user to reassign
            experiment_id: ID of the experiment
            variant_id: Optional variant ID to force assignment
            track_exposure: Whether to track an exposure event
            context: Optional context data

        Returns:
            Dictionary containing the new assignment data
        """
        # Get experiment to verify it exists and is active
        experiment = (
            self.db.query(Experiment)
            .options(joinedload(Experiment.variants))
            .filter(Experiment.id == experiment_id)
            .first()
        )

        if not experiment:
            raise ValueError(f"Experiment {experiment_id} not found")

        # Determine variant assignment
        if variant_id:
            # Verify variant is valid for this experiment
            variant_valid = any(v.id == variant_id for v in experiment.variants)
            if not variant_valid:
                raise ValueError(
                    f"Variant {variant_id} not valid for experiment {experiment_id}"
                )
        else:
            # Use deterministic hashing to reassign variant
            variant_id = self._hash_user_to_variant(user_id, experiment)

        # Update the existing assignment in place when there is one: the
        # (experiment_id, user_id) pair is unique, so inserting a second row
        # would violate the index instead of reassigning the user.
        assignment = (
            self.db.query(Assignment)
            .filter(
                Assignment.user_id == user_id,
                Assignment.experiment_id == experiment_id,
            )
            .first()
        )
        if assignment:
            assignment.variant_id = variant_id
            if context is not None:
                assignment.context = context
        else:
            assignment = Assignment(
                user_id=user_id,
                experiment_id=experiment_id,
                variant_id=variant_id,
                context=context,
            )
            self.db.add(assignment)

        self.db.commit()
        self.db.refresh(assignment)

        logger.info(
            f"Reassigned a user to variant {variant_id} in experiment {experiment_id}"
        )

        # Track exposure event if requested
        if track_exposure:
            self.event_service.track_exposure(
                user_id=user_id,
                experiment_id=str(experiment_id),
                variant_id=str(variant_id),
                properties=context,
            )

        # Get full assignment details
        return self.get_assignment(user_id, experiment_id)

    def _hash_user_to_variant(
        self, user_id: str, experiment: Experiment
    ) -> Union[str, UUID]:
        """Assign *user_id* to a variant, by the hash every SDK implements.

        Uses `backend.app.core.consistent_hash`, which is the algorithm
        `tests/sdk-contract/golden-vectors.json` pins and the SDKs
        implement. Before #81 this method had its own: MD5 of
        ``"{user_id}:{experiment.id}"`` read as one 128-bit integer modulo 100.

        Two things were wrong with that, and both are silent:

        * the whole digest modulo 100 is a different bucket from the first four
          bytes little-endian -- ``user-123``/``my-flag`` is bucket 79 one way
          and 69 the other;
        * it hashed the experiment's UUID primary key, which no client has, so
          an SDK evaluating locally could not have agreed even in principle.

        The result was a user counted in one variant by the API and another by
        an SDK, with no error anywhere and every metric joined across the two
        quietly wrong.

        Assignment is sticky -- `assign_user` only reaches this for a user with
        no existing row -- so changing the function re-buckets nobody who has
        already been assigned.

        Falls back to the experiment's UUID when it has no public ``key``
        (nullable on the model, and older rows predate it). Such an experiment
        has no SDK-reachable identity anyway, so there is nothing to agree with;
        what matters is that it stays deterministic.
        """
        if not experiment.variants:
            raise ValueError(f"Experiment {experiment.id} has no variants")

        namespace = experiment.key or str(experiment.id)
        bucket = bucket_of(user_id, namespace)

        # Cumulative distribution over allocations expressed 0-100.
        cumulative = 0
        for variant in experiment.variants:
            cumulative += variant.traffic_allocation
            if bucket < cumulative:
                return variant.id

        # Allocations summing to less than 100 leave a tail; the last variant
        # takes it, which is what the previous implementation did too.
        return experiment.variants[-1].id

    def delete_assignments_by_experiment(self, experiment_id: Union[str, UUID]) -> int:
        """
        Delete all assignments for an experiment.

        Args:
            experiment_id: ID of the experiment

        Returns:
            Number of assignments deleted
        """
        # Count assignments before deletion
        count_query = self.db.query(func.count(Assignment.id)).filter(
            Assignment.experiment_id == experiment_id
        )
        count = count_query.scalar() or 0

        # Delete assignments
        delete_query = self.db.query(Assignment).filter(
            Assignment.experiment_id == experiment_id
        )
        delete_query.delete(synchronize_session=False)

        self.db.commit()

        logger.info(f"Deleted {count} assignments for experiment {experiment_id}")
        return count
