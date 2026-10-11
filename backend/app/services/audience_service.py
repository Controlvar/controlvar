"""
Audience Service for segment management and membership evaluation.

Provides business logic for:
- Creating, reading, updating, and archiving audience segments
- Evaluating user context membership against segment rules
- Bulk membership evaluation across multiple segments
- Linking segments to experiments/feature flags
- Estimating audience size via rule preview

Segment rules use the targeting rule format (the dashboard shape) and are
checked by ``validate_segment_rules`` when saved. Membership is decided by the
flag engine (``match_targeting_rule``) on the expanded context
(``expand_context``), so a segment's rules mean what the same rules mean on a
feature flag.

An ``id_list`` segment (#440) has no rules: its members are the user ids stored
in ``segment_members``, changed with :meth:`AudienceService.add_members` and
:meth:`AudienceService.remove_members`, and a user is a member when the
context's ``user_id`` is one of them.
"""

import logging
import time
from typing import Any, Dict, List, Optional, Tuple, Union
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from backend.app.core.pattern_match import PatternUnevaluable, report_unevaluable
from backend.app.core.rules_engine import TimeWindowUnreadable, evaluate_condition
from backend.app.core.targeting_adapter import (
    TargetingRulesError,
    convert_dashboard_condition,
    expand_context,
    match_targeting_rule,
    validate_segment_rules,
)
from backend.app.models.experiment import Experiment
from backend.app.models.feature_flag import FeatureFlag
from backend.app.models.segment import Segment, SegmentKind, SegmentMember
from backend.app.models.segment import SegmentStatus as ModelSegmentStatus
from backend.app.schemas.segment import (
    MAX_MEMBER_ID_LENGTH,
    AudiencePreviewResponse,
    BulkSegmentMembershipRequest,
    BulkSegmentMembershipResponse,
    SegmentCreate,
    SegmentExperimentResponse,
    SegmentMembersAddResponse,
    SegmentMembershipResponse,
    SegmentMembersRemoveResponse,
    SegmentStatus,
    SegmentUpdate,
)
from backend.app.schemas.storable_text import contains_unstorable_text
from backend.app.schemas.targeting_rule import TargetingRules
from backend.app.services.segment_membership import (
    segment_in_use_detail,
    segment_references,
)

logger = logging.getLogger(__name__)

#: An id-list segment holds at most this many user ids (#440). Read at call
#: time, so a test can lower it.
MAX_SEGMENT_MEMBERS = 1_000_000


class SegmentChangeRefused(Exception):
    """A change to a segment that its kind or status does not allow.

    ``message`` is fixed text chosen here (counts at most, never a user id or
    a value from the request), or the structured ``segment_in_use`` detail
    (the referencing flags' and experiments' ids, keys and names); the route
    answers it with ``status_code``.
    """

    def __init__(self, status_code: int, message: Any) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(message)


class SegmentRulesNotValid(Exception):
    """A stored segment's rules fail :func:`validate_segment_rules`.

    Rows written before segment rules were checked on save (the legacy
    ``{"operator", "conditions"}`` shape, or an empty or typo'd group) are
    left as they are. They are never evaluated: ``evaluate`` answers 409 and
    bulk-evaluate reports ``false`` for them. Deliberately not a
    ``ValueError``, which the routes answer as 404.
    """

    def __init__(self, segment_id: Any) -> None:
        self.segment_id = str(segment_id)
        super().__init__("segment rules not valid")


def stored_segment_rules(segment: Segment) -> TargetingRules:
    """The rules membership evaluates for ``segment``.

    Raises:
        SegmentRulesNotValid: when the stored rules fail validation.
    """
    try:
        return validate_segment_rules(segment.rules)
    except TargetingRulesError:
        raise SegmentRulesNotValid(segment.id) from None


def _is_member(rules: TargetingRules, context: Dict[str, Any]) -> bool:
    """Membership: the flag engine on the expanded context.

    ``context`` must already be expanded (:func:`expand_context`).
    :class:`PatternUnevaluable` propagates.
    """
    return match_targeting_rule(rules, context) is not None


def _collect_matched_rules(raw_rules: Any, context: Dict[str, Any]) -> List[str]:
    """
    Label each condition, in any group, that ``context`` satisfies on its own.

    ``raw_rules`` are stored rules that passed :func:`validate_segment_rules`;
    ``context`` is already expanded. Labels read ``"<attribute> <operator>
    <value>"`` in the stored (dashboard) terms.
    """
    matched: List[str] = []
    for group in raw_rules.get("groups") or []:
        for condition in group.get("conditions") or []:
            try:
                if evaluate_condition(convert_dashboard_condition(condition), context):
                    matched.append(
                        f"{condition.get('attribute', '')} "
                        f"{condition.get('operator', '')} {condition.get('value', '')}"
                    )
            except (PatternUnevaluable, TimeWindowUnreadable):
                continue
    return matched


def _segment_status_to_model(status: SegmentStatus) -> ModelSegmentStatus:
    """Map schema SegmentStatus to model SegmentStatus."""
    mapping = {
        SegmentStatus.ACTIVE: ModelSegmentStatus.ACTIVE,
        SegmentStatus.INACTIVE: ModelSegmentStatus.INACTIVE,
        SegmentStatus.ARCHIVED: ModelSegmentStatus.ARCHIVED,
    }
    return mapping[status]


def _model_status_to_schema(status: ModelSegmentStatus) -> SegmentStatus:
    """Map model SegmentStatus to schema SegmentStatus."""
    mapping = {
        ModelSegmentStatus.ACTIVE: SegmentStatus.ACTIVE,
        ModelSegmentStatus.INACTIVE: SegmentStatus.INACTIVE,
        ModelSegmentStatus.ARCHIVED: SegmentStatus.ARCHIVED,
    }
    return mapping[status]


def _is_id_list(segment: Segment) -> bool:
    return segment.kind == SegmentKind.ID_LIST.value


def _segment_to_response_dict(
    segment: Segment, member_count: Optional[int] = None
) -> Dict[str, Any]:
    """Convert a Segment ORM object to a dict suitable for SegmentResponse.

    An id-list segment has no rules (``null``); ``member_count`` is filled
    only by a caller that counted.
    """
    id_list = _is_id_list(segment)
    return {
        "id": str(segment.id),
        "name": segment.name,
        "description": segment.description,
        "kind": SegmentKind.ID_LIST.value if id_list else SegmentKind.RULES.value,
        "rules": None if id_list else (segment.rules or {}),
        "status": _model_status_to_schema(segment.status).value,
        "created_at": segment.created_at.isoformat() if segment.created_at else "",
        "updated_at": segment.updated_at.isoformat() if segment.updated_at else "",
        "member_count": member_count,
    }


def _ids_param(ids: List[str]) -> Any:
    """The ids as one ``varchar[]`` bind parameter, not one parameter each."""
    return sa.bindparam(
        "member_ids", value=ids, type_=postgresql.ARRAY(sa.String(MAX_MEMBER_ID_LENGTH))
    )


def _count_members(db: Session, segment_id: Any) -> int:
    return int(
        db.query(sa.func.count())
        .select_from(SegmentMember)
        .filter(SegmentMember.segment_id == segment_id)
        .scalar()
    )


def _locked_id_list(db: Session, segment_id: Union[UUID, str], verb: str) -> Segment:
    """The segment row, locked ``FOR UPDATE`` until the transaction ends.

    The lock serialises every member change to one segment, so the count the
    cap is checked against cannot move underneath it.

    Raises:
        ValueError: no such segment (404).
        SegmentChangeRefused: a rules segment, or an archived one (409).
    """
    segment = (
        db.query(Segment).filter(Segment.id == segment_id).with_for_update().first()
    )
    if segment is None:
        db.rollback()
        raise ValueError(f"Segment '{segment_id}' not found")
    if segment.status == ModelSegmentStatus.ARCHIVED:
        db.rollback()
        raise SegmentChangeRefused(
            409, "this segment is archived; its members cannot be changed"
        )
    if not _is_id_list(segment):
        db.rollback()
        raise SegmentChangeRefused(409, f"members can be {verb} an id_list segment")
    return segment


def _refuse_if_in_use(db: Session, segment_id: Any) -> None:
    """Refuse archiving or deactivating a segment that targeting still uses.

    A flag that is not archived, or an experiment that is draft, active or
    paused, whose rules reference the segment would stop evaluating (its
    membership becomes unavailable). Query errors propagate.

    Raises:
        SegmentChangeRefused: 409 with the structured ``segment_in_use``
            detail listing them.
    """
    flags, experiments = segment_references(db, segment_id)
    if flags or experiments:
        db.rollback()
        raise SegmentChangeRefused(409, segment_in_use_detail(flags, experiments))


class AudienceService:
    """
    Service for audience segment management and evaluation.

    All methods are static and accept a database session as the first argument.
    """

    @staticmethod
    def create_segment(
        db: Session,
        data: SegmentCreate,
        created_by_id: Optional[Any] = None,
    ) -> Segment:
        """
        Create and persist a new audience segment.

        Args:
            db: SQLAlchemy session.
            data: Validated SegmentCreate payload.
            created_by_id: UUID of the creating user (stored as owner_id).

        Returns:
            The newly created Segment ORM object.
        """
        segment = Segment(
            name=data.name,
            description=data.description,
            kind=data.kind.value,
            rules=data.rules,
            status=ModelSegmentStatus.ACTIVE,
            owner_id=created_by_id,
        )
        db.add(segment)
        db.commit()
        db.refresh(segment)
        logger.info("Created segment %s (%s)", segment.name, segment.id)
        return segment

    @staticmethod
    def list_segments(
        db: Session,
        status: Optional[SegmentStatus] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Segment]:
        """
        List segments with optional status filter.

        Args:
            db: SQLAlchemy session.
            status: Optional status filter (excludes archived by default if None).
            limit: Maximum number of results.
            offset: Number of results to skip.

        Returns:
            List of Segment ORM objects.
        """
        query = db.query(Segment)
        if status is not None:
            model_status = _segment_status_to_model(status)
            query = query.filter(Segment.status == model_status)
        return query.offset(offset).limit(limit).all()

    @staticmethod
    def get_segment(db: Session, segment_id: Union[UUID, str]) -> Segment:
        """
        Retrieve a segment by ID.

        Args:
            db: SQLAlchemy session.
            segment_id: UUID of the segment. Text that is not a UUID (bulk
                evaluate takes ids as text) is answered as not found, without
                a query.

        Returns:
            The Segment ORM object.

        Raises:
            ValueError: If no segment with the given ID exists.
        """
        if not isinstance(segment_id, UUID):
            try:
                segment_id = UUID(str(segment_id))
            except ValueError:
                raise ValueError("Segment not found") from None
        segment = db.query(Segment).filter(Segment.id == segment_id).first()
        if segment is None:
            raise ValueError(f"Segment '{segment_id}' not found")
        return segment

    @staticmethod
    def update_segment(
        db: Session, segment_id: Union[UUID, str], data: SegmentUpdate
    ) -> Segment:
        """
        Update fields on an existing segment.

        Args:
            db: SQLAlchemy session.
            segment_id: UUID string of the segment to update.
            data: Validated SegmentUpdate payload (all fields optional).

        Returns:
            The updated Segment ORM object.

        Raises:
            ValueError: If the segment does not exist.
            SegmentChangeRefused: ``rules`` sent for an id-list segment (422);
                a status other than active while a flag or experiment
                references the segment (409, structured detail).
        """
        segment = AudienceService.get_segment(db, segment_id)
        if data.rules is not None and _is_id_list(segment):
            raise SegmentChangeRefused(422, "rules: an id_list segment has no rules")
        if data.status is not None and data.status != SegmentStatus.ACTIVE:
            _refuse_if_in_use(db, segment.id)

        if data.name is not None:
            segment.name = data.name
        if data.description is not None:
            segment.description = data.description
        if data.rules is not None:
            segment.rules = data.rules
        if data.status is not None:
            segment.status = _segment_status_to_model(data.status)

        db.commit()
        db.refresh(segment)
        logger.info("Updated segment %s", segment_id)
        return segment

    @staticmethod
    def delete_segment(db: Session, segment_id: Union[UUID, str]) -> None:
        """
        Soft-delete a segment by setting its status to ARCHIVED.

        Args:
            db: SQLAlchemy session.
            segment_id: UUID string of the segment to archive.

        Raises:
            ValueError: If the segment does not exist.
            SegmentChangeRefused: a flag or experiment references the segment
                (409, structured detail).
        """
        segment = AudienceService.get_segment(db, segment_id)
        _refuse_if_in_use(db, segment.id)
        segment.status = ModelSegmentStatus.ARCHIVED
        db.commit()
        logger.info("Archived segment %s", segment_id)

    @staticmethod
    def evaluate_membership(
        db: Session,
        segment_id: Union[UUID, str],
        user_context: Dict[str, Any],
    ) -> SegmentMembershipResponse:
        """
        Evaluate if a user context matches a segment's targeting rules.

        The stored rules are checked with ``validate_segment_rules`` and
        evaluated with ``match_targeting_rule`` on ``expand_context(user_context)``,
        exactly as a feature flag evaluates the same rules. A pattern
        condition that cannot be evaluated makes the user not a member.

        Args:
            db: SQLAlchemy session.
            segment_id: UUID of the segment to evaluate against.
            user_context: Dict of user attributes (e.g. country, plan, age).

        Returns:
            SegmentMembershipResponse with is_member and matched_rules.

        An id-list segment is answered on ``user_context["user_id"]``: a member
        when it is a string stored in the segment's list. A missing or
        non-string ``user_id`` is not a member.

        Raises:
            ValueError: If the segment does not exist.
            SegmentRulesNotValid: If the stored rules fail validation.
        """
        segment = AudienceService.get_segment(db, segment_id)
        if _is_id_list(segment):
            return SegmentMembershipResponse(
                segment_id=str(segment.id),
                segment_name=segment.name,
                is_member=AudienceService._in_id_list(
                    db, segment.id, user_context.get("user_id")
                ),
                matched_rules=[],
            )
        rules = stored_segment_rules(segment)
        context = expand_context(user_context)

        is_member = False
        matched_rules: List[str] = []
        try:
            is_member = _is_member(rules, context)
        except PatternUnevaluable as exc:
            # A pattern condition could not be evaluated: not a member.
            report_unevaluable(exc, f"segment:{segment.id}")
            is_member = False
        if is_member:
            matched_rules = _collect_matched_rules(segment.rules, context)

        return SegmentMembershipResponse(
            segment_id=str(segment.id),
            segment_name=segment.name,
            is_member=is_member,
            matched_rules=matched_rules,
        )

    @staticmethod
    def _in_id_list(db: Session, segment_id: Any, user_id: Any) -> bool:
        """Whether ``user_id`` is stored in the id list ``segment_id``."""
        if (
            not isinstance(user_id, str)
            or not 1 <= len(user_id) <= MAX_MEMBER_ID_LENGTH
            or contains_unstorable_text(user_id)
        ):
            return False
        return db.query(
            sa.exists().where(
                SegmentMember.segment_id == segment_id,
                SegmentMember.member_id == user_id,
            )
        ).scalar()

    @staticmethod
    def member_count(db: Session, segment: Segment) -> Optional[int]:
        """How many ids an id-list segment holds; ``None`` for a rules segment."""
        if not _is_id_list(segment):
            return None
        return _count_members(db, segment.id)

    @staticmethod
    def add_members(
        db: Session, segment_id: Union[UUID, str], ids: List[str]
    ) -> Tuple[str, SegmentMembersAddResponse]:
        """Add ``ids`` to an id-list segment, in one transaction.

        The segment row is locked ``FOR UPDATE``; the cap is checked before
        anything is written (``MAX_SEGMENT_MEMBERS``), and a request that
        would pass it changes nothing. Ids already stored, and repeats in
        ``ids``, are left alone (``ON CONFLICT DO NOTHING``).

        Raises:
            ValueError: no such segment.
            SegmentChangeRefused: a rules or archived segment (409), or the
                cap (422).
        """
        segment = _locked_id_list(db, segment_id, "added only to")
        name = segment.name
        distinct = list(dict.fromkeys(ids))
        current = _count_members(db, segment.id)
        already = int(
            db.query(sa.func.count())
            .select_from(SegmentMember)
            .filter(
                SegmentMember.segment_id == segment.id,
                SegmentMember.member_id == sa.any_(_ids_param(distinct)),
            )
            .scalar()
        )
        would_have = current + len(distinct) - already
        cap = MAX_SEGMENT_MEMBERS
        if would_have > cap:
            db.rollback()
            raise SegmentChangeRefused(
                422,
                f"this segment would have {would_have:,} members; a segment holds "
                f"at most {cap:,}",
            )
        source = sa.select(
            sa.literal(segment.id, type_=postgresql.UUID(as_uuid=True)),
            sa.func.unnest(_ids_param(distinct)),
        )
        inserted = db.execute(
            postgresql.insert(SegmentMember)
            .from_select(["segment_id", "member_id"], source)
            .on_conflict_do_nothing()
            .returning(SegmentMember.member_id)
        ).all()
        added = len(inserted)
        segment_key = segment.id
        db.commit()
        logger.info("Added %d member(s) to segment %s", added, segment_key)
        return name, SegmentMembersAddResponse(
            added=added,
            already_members=len(distinct) - added,
            member_count=current + added,
        )

    @staticmethod
    def remove_members(
        db: Session, segment_id: Union[UUID, str], ids: List[str]
    ) -> Tuple[str, SegmentMembersRemoveResponse]:
        """Remove ``ids`` from an id-list segment, in one transaction.

        The segment row is locked ``FOR UPDATE``. Ids that are not members,
        and repeats in ``ids``, are ignored.

        Raises:
            ValueError: no such segment.
            SegmentChangeRefused: a rules or archived segment (409).
        """
        segment = _locked_id_list(db, segment_id, "removed only from")
        name = segment.name
        distinct = list(dict.fromkeys(ids))
        deleted = db.execute(
            sa.delete(SegmentMember)
            .where(
                SegmentMember.segment_id == segment.id,
                SegmentMember.member_id == sa.any_(_ids_param(distinct)),
            )
            .returning(SegmentMember.member_id)
        ).all()
        removed = len(deleted)
        member_count = _count_members(db, segment.id)
        segment_key = segment.id
        db.commit()
        logger.info("Removed %d member(s) from segment %s", removed, segment_key)
        return name, SegmentMembersRemoveResponse(
            removed=removed,
            not_members=len(distinct) - removed,
            member_count=member_count,
        )

    @staticmethod
    def bulk_evaluate_membership(
        db: Session,
        request: BulkSegmentMembershipRequest,
    ) -> BulkSegmentMembershipResponse:
        """
        Evaluate one user context against multiple segments.

        Args:
            db: SQLAlchemy session.
            request: BulkSegmentMembershipRequest containing user_context and segment_ids.

        Returns:
            BulkSegmentMembershipResponse with memberships dict and evaluation_time_ms.
        """
        start_time = time.monotonic()
        memberships: Dict[str, bool] = {}

        for segment_id in request.segment_ids:
            try:
                result = AudienceService.evaluate_membership(
                    db, segment_id, request.user_context
                )
                memberships[segment_id] = result.is_member
            except ValueError:
                # Segment not found — treat as non-member
                memberships[segment_id] = False
            except SegmentRulesNotValid:
                # Stored rules that fail validation are never evaluated.
                memberships[segment_id] = False
            except Exception as exc:
                logger.error("Error evaluating segment %s in bulk: %s", segment_id, exc)
                memberships[segment_id] = False

        elapsed_ms = (time.monotonic() - start_time) * 1000.0

        return BulkSegmentMembershipResponse(
            user_context=request.user_context,
            memberships=memberships,
            evaluation_time_ms=elapsed_ms,
        )

    @staticmethod
    def get_segment_experiments(
        db: Session,
        segment_id: Union[UUID, str],
    ) -> SegmentExperimentResponse:
        """
        Find all experiments and feature flags that reference this segment in their
        targeting rules (JSONB field contains the segment_id).

        This performs a lightweight search by looking for the segment UUID inside
        the JSONB targeting_rules of Experiment and FeatureFlag records.

        Args:
            db: SQLAlchemy session.
            segment_id: UUID string of the segment.

        Returns:
            SegmentExperimentResponse listing linked experiments and feature flags.

        Raises:
            ValueError: If the segment does not exist.
        """
        # Verify segment exists first
        segment_id = str(AudienceService.get_segment(db, segment_id).id)

        experiments_data: List[Dict[str, Any]] = []
        feature_flags_data: List[Dict[str, Any]] = []

        try:
            # Search Experiment targeting_rules JSONB for segment_id references
            experiments = (
                db.query(Experiment)
                .filter(
                    Experiment.targeting_rules.cast(
                        db.bind.dialect.colspecs.get(type(None), type(None))
                        if False
                        else __import__("sqlalchemy").Text
                    ).contains(segment_id)
                )
                .all()
            )
            for exp in experiments:
                experiments_data.append(
                    {
                        "id": str(exp.id),
                        "name": exp.name,
                        "status": exp.status.value if exp.status else None,
                    }
                )
        except Exception as exc:
            logger.warning(
                "Could not query experiments for segment %s: %s", segment_id, exc
            )

        try:
            feature_flags = (
                db.query(FeatureFlag)
                .filter(
                    FeatureFlag.targeting_rules.cast(
                        __import__("sqlalchemy").Text
                    ).contains(segment_id)
                )
                .all()
            )
            for flag in feature_flags:
                feature_flags_data.append(
                    {
                        "id": str(flag.id),
                        "name": flag.name,
                    }
                )
        except Exception as exc:
            logger.warning(
                "Could not query feature flags for segment %s: %s", segment_id, exc
            )

        return SegmentExperimentResponse(
            segment_id=segment_id,
            experiments=experiments_data,
            feature_flags=feature_flags_data,
        )

    @staticmethod
    def preview_audience_size(
        db: Session,
        rules: Dict[str, Any],
        sample_size: int = 1000,
    ) -> AudiencePreviewResponse:
        """
        Estimate the share of users the given rules would match.

        Samples at most ``sample_size`` assignments that carry a context and
        evaluates the rules against each context, as membership does. Only
        those rows count: ``sample_size`` in the answer is how many there
        were, and with none the answer is ``0.0`` of ``0``, not a percentage
        of rows that could not be evaluated.

        Args:
            db: SQLAlchemy session.
            rules: Rules that passed ``validate_segment_rules`` (the
                ``SegmentCreate`` body).
            sample_size: Maximum number of assignments to sample.

        Returns:
            AudiencePreviewResponse with estimated_percentage, sample_size, and matched.
        """
        from backend.app.models.assignment import Assignment

        engine_rules = validate_segment_rules(rules)
        rows = (
            db.query(Assignment.context)
            .filter(Assignment.context.isnot(None))
            .limit(sample_size)
            .all()
        )

        matched = 0
        actual_sample = 0
        for (context,) in rows:
            if not isinstance(context, dict) or not context:
                continue
            actual_sample += 1
            try:
                if _is_member(engine_rules, expand_context(context)):
                    matched += 1
            except PatternUnevaluable:
                # Not counted, like any user the rules do not match.
                pass

        estimated_percentage = (
            (matched / actual_sample * 100.0) if actual_sample > 0 else 0.0
        )

        return AudiencePreviewResponse(
            estimated_percentage=round(estimated_percentage, 2),
            sample_size=actual_sample,
            matched=matched,
        )
