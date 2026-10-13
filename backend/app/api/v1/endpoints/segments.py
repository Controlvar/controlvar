"""
Audience Segmentation API endpoints (P3-C).

REST endpoints for managing audience segments and evaluating user membership.

Routes:
    GET    /api/v1/segments                 — list segments
    POST   /api/v1/segments                 — create segment (DEVELOPER+)
    GET    /api/v1/segments/{id}            — get segment
    PUT    /api/v1/segments/{id}            — update segment (DEVELOPER+)
    DELETE /api/v1/segments/{id}            — archive segment (DEVELOPER+)
    POST   /api/v1/segments/{id}/evaluate   — check user context membership
    POST   /api/v1/segments/bulk-evaluate   — check one user against N segments
    GET    /api/v1/segments/{id}/experiments — list experiments using this segment
    POST   /api/v1/segments/{id}/preview    — estimate audience size
    POST   /api/v1/segments/{id}/members        — add user ids (id list; beta)
    POST   /api/v1/segments/{id}/members/remove — remove user ids (id list; beta)

Segment rules use the targeting rule format (the dashboard shape) and are
checked when saved (``validate_segment_rules``). ``{id}`` is a UUID; any other
text answers 422. A segment's ``kind`` is ``rules`` or ``id_list`` (#440); an
id list has no rules, and its members are changed with the two member routes.
"""

import logging
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from backend.app.api import deps
from backend.app.core.context_limits import check_context
from backend.app.core.permissions import Action, ResourceType, check_permission
from backend.app.core.segment_preview_limits import preview_ruleset_violations
from backend.app.models.audit_log import ActionType, EntityType
from backend.app.models.user import User
from backend.app.schemas.segment import (
    AudiencePreviewResponse,
    BulkSegmentMembershipRequest,
    BulkSegmentMembershipResponse,
    SegmentCreate,
    SegmentExperimentResponse,
    SegmentMembersAdd,
    SegmentMembersAddResponse,
    SegmentMembershipRequest,
    SegmentMembershipResponse,
    SegmentMembersRemove,
    SegmentMembersRemoveResponse,
    SegmentResponse,
    SegmentStatus,
    SegmentUpdate,
)
from backend.app.services.audience_service import (
    AudienceService,
    SegmentChangeRefused,
    SegmentRulesNotValid,
    _segment_to_response_dict,
)
from backend.app.services.audit_service import (
    AuditService,
    audit_changes,
    audit_identity,
    audit_snapshot,
)

logger = logging.getLogger(__name__)

router = APIRouter()

#: The 409 ``detail`` for a stored segment whose rules fail validation. Fixed
#: text: it never carries the stored rules.
SEGMENT_RULES_NOT_VALID = (
    "Segment rules not valid: they are not in the targeting rule format. "
    "Save them again with PUT /api/v1/segments/{id}."
)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _require_permission(user: User, action: Action) -> None:
    """
    Raise HTTP 403 if the user lacks the given action on the EXPERIMENT resource.

    Segments are treated as an EXPERIMENT-class resource for permission checking
    because they are tightly coupled to experiment targeting.
    """
    if not check_permission(user, ResourceType.EXPERIMENT, action):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not enough permissions to perform this action on segments",
        )


# ---------------------------------------------------------------------------
# List segments
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=List[SegmentResponse],
    summary="List audience segments",
    description="Return a paginated list of audience segments. Accessible to all authenticated users.",
    tags=["Segments"],
)
def list_segments(
    segment_status: Optional[SegmentStatus] = Query(
        None, alias="status", description="Filter by segment status"
    ),
    limit: int = Query(50, ge=1, le=200, description="Maximum results to return"),
    offset: int = Query(0, ge=0, description="Number of results to skip"),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> List[SegmentResponse]:
    """List segments with optional status filter."""
    _require_permission(current_user, Action.LIST)
    segments = AudienceService.list_segments(
        db, status=segment_status, limit=limit, offset=offset
    )
    return [SegmentResponse(**_segment_to_response_dict(s)) for s in segments]


# ---------------------------------------------------------------------------
# Create segment
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=SegmentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an audience segment",
    description=(
        "Create a new audience segment: kind rules (the default) with targeting "
        "rules, or kind id_list with no rules, whose members are added with POST "
        "/segments/{segment_id}/members. Requires DEVELOPER or ADMIN role."
    ),
    tags=["Segments"],
)
def create_segment(
    data: SegmentCreate,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> SegmentResponse:
    """Create a new audience segment."""
    _require_permission(current_user, Action.CREATE)
    segment = AudienceService.create_segment(db, data, created_by_id=current_user.id)
    response = SegmentResponse(**_segment_to_response_dict(segment))
    _record(
        db,
        current_user,
        ActionType.SEGMENT_CREATE,
        segment.id,
        segment.name,
        after=audit_identity(audit_snapshot(EntityType.SEGMENT, segment)),
    )
    return response


def _record(db: Session, user: User, action: ActionType, segment_id, name, **values):
    """The audit entry for a committed change to one segment (fails open).

    Rules are recorded by name only (``changed_fields``), never their content.
    """
    AuditService.record_after_commit(
        db,
        actor=user,
        action=action,
        entity_type=EntityType.SEGMENT,
        entity_id=segment_id,
        entity_name=name or str(segment_id),
        **values,
    )


def _segment_before(db: Session, segment_id: UUID):
    """The segment's audit snapshot before a change, or 404."""
    try:
        return audit_snapshot(
            EntityType.SEGMENT, AudienceService.get_segment(db, segment_id)
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


# ---------------------------------------------------------------------------
# Bulk evaluate — placed BEFORE /{id} routes to avoid path collision
# ---------------------------------------------------------------------------


@router.post(
    "/bulk-evaluate",
    response_model=BulkSegmentMembershipResponse,
    summary="Bulk segment membership evaluation",
    description=(
        "Evaluate one user context against multiple segments in a single request. "
        "Returns a membership dict {segment_id: is_member} for each requested segment. "
        "An unknown segment, and one whose stored rules are not valid, is false."
    ),
    tags=["Segments"],
)
def bulk_evaluate_segments(
    request: BulkSegmentMembershipRequest,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> BulkSegmentMembershipResponse:
    """Check user membership across multiple segments at once."""
    _require_permission(current_user, Action.READ)
    # The limits of backend.app.core.context_limits, once for the one
    # user_context, before any segment is read or evaluated.
    check_context(request.user_context)
    return AudienceService.bulk_evaluate_membership(db, request)


# ---------------------------------------------------------------------------
# Get segment
# ---------------------------------------------------------------------------


@router.get(
    "/{segment_id}",
    response_model=SegmentResponse,
    summary="Get an audience segment",
    description=(
        "Retrieve a single audience segment by its UUID. For an id_list segment, "
        "member_count is how many user ids it holds."
    ),
    tags=["Segments"],
)
def get_segment(
    segment_id: UUID,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> SegmentResponse:
    """Retrieve a segment by ID."""
    _require_permission(current_user, Action.READ)
    try:
        segment = AudienceService.get_segment(db, segment_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return SegmentResponse(
        **_segment_to_response_dict(
            segment, member_count=AudienceService.member_count(db, segment)
        )
    )


# ---------------------------------------------------------------------------
# Update segment
# ---------------------------------------------------------------------------


@router.put(
    "/{segment_id}",
    response_model=SegmentResponse,
    summary="Update an audience segment",
    description=(
        "Update segment name, description, rules, or status. A segment's kind "
        "cannot be changed, and an id_list segment takes no rules (422). A "
        "status other than active is refused (409) while targeting rules use "
        "the segment. Requires DEVELOPER or ADMIN role."
    ),
    responses={
        409: {
            "description": (
                "The segment is used by the targeting rules of a flag that is "
                "not archived or an experiment that is draft, active or paused. "
                '`detail` is `{"code": "segment_in_use", "message", '
                '"feature_flags": [{id, key, name}], "experiments": '
                "[{id, key, name, status}]}`."
            )
        }
    },
    tags=["Segments"],
)
def update_segment(
    segment_id: UUID,
    data: SegmentUpdate,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> SegmentResponse:
    """Update an existing segment."""
    _require_permission(current_user, Action.UPDATE)
    before = _segment_before(db, segment_id)
    try:
        segment = AudienceService.update_segment(db, segment_id, data)
    except SegmentChangeRefused as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    response = SegmentResponse(**_segment_to_response_dict(segment))
    old, new = audit_changes(before, audit_snapshot(EntityType.SEGMENT, segment))
    _record(
        db,
        current_user,
        ActionType.SEGMENT_UPDATE,
        segment_id,
        response.name,
        before=old,
        after=new,
    )
    return response


# ---------------------------------------------------------------------------
# Delete segment (soft delete → ARCHIVED)
# ---------------------------------------------------------------------------


@router.delete(
    "/{segment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Archive an audience segment",
    description=(
        "Soft-delete a segment by setting its status to ARCHIVED. Refused (409) "
        "while targeting rules use the segment. Requires DEVELOPER or ADMIN role."
    ),
    responses={
        409: {
            "description": (
                "The segment is used by the targeting rules of a flag that is "
                "not archived or an experiment that is draft, active or paused. "
                '`detail` is `{"code": "segment_in_use", "message", '
                '"feature_flags": [{id, key, name}], "experiments": '
                "[{id, key, name, status}]}`."
            )
        }
    },
    tags=["Segments"],
)
def delete_segment(
    segment_id: UUID,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> None:
    """Archive (soft-delete) a segment."""
    _require_permission(current_user, Action.DELETE)
    before = _segment_before(db, segment_id)
    try:
        AudienceService.delete_segment(db, segment_id)
    except SegmentChangeRefused as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    segment = AudienceService.get_segment(db, segment_id)
    old, new = audit_changes(before, audit_snapshot(EntityType.SEGMENT, segment))
    _record(
        db,
        current_user,
        ActionType.SEGMENT_ARCHIVE,
        segment_id,
        before.get("name"),
        before=old,
        after=new,
    )


# ---------------------------------------------------------------------------
# Evaluate membership
# ---------------------------------------------------------------------------


@router.post(
    "/{segment_id}/evaluate",
    response_model=SegmentMembershipResponse,
    summary="Evaluate user membership in a segment",
    description=(
        "Check whether a user context (arbitrary attributes dict) satisfies "
        "this segment's targeting rules, evaluated as a feature flag evaluates "
        "the same rules. For an id_list segment, the user is a member when "
        "user_context.user_id is in its list. A segment whose stored rules are "
        "not in the targeting rule format (saved before rules were checked) "
        "answers 409."
    ),
    responses={
        409: {
            "description": "The segment's stored rules are not valid; save them again."
        }
    },
    tags=["Segments"],
)
def evaluate_segment_membership(
    segment_id: UUID,
    request: SegmentMembershipRequest,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> SegmentMembershipResponse:
    """Evaluate if a user context matches the segment's rules."""
    _require_permission(current_user, Action.READ)
    # The limits of backend.app.core.context_limits, before the segment is
    # read or evaluated.
    check_context(request.user_context)
    try:
        return AudienceService.evaluate_membership(db, segment_id, request.user_context)
    except SegmentRulesNotValid:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=SEGMENT_RULES_NOT_VALID
        ) from None
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


# ---------------------------------------------------------------------------
# Get linked experiments
# ---------------------------------------------------------------------------


@router.get(
    "/{segment_id}/experiments",
    response_model=SegmentExperimentResponse,
    summary="List experiments using this segment",
    description=(
        "Return all experiments and feature flags whose targeting_rules reference "
        "this segment's UUID."
    ),
    tags=["Segments"],
)
def get_segment_experiments(
    segment_id: UUID,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> SegmentExperimentResponse:
    """Fetch experiments and feature flags linked to this segment."""
    _require_permission(current_user, Action.READ)
    try:
        return AudienceService.get_segment_experiments(db, segment_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


# ---------------------------------------------------------------------------
# Preview audience size
# ---------------------------------------------------------------------------


@router.post(
    "/{segment_id}/preview",
    response_model=AudiencePreviewResponse,
    summary="Preview estimated audience size",
    description=(
        "Estimate the percentage of users who would match the provided targeting rules "
        "by evaluating them against the stored contexts of up to sample_size "
        "assignments. Only assignments that carry a context are counted; the "
        "answer's sample_size is how many there were, and 0 when none do."
    ),
    tags=["Segments"],
)
def preview_audience_size(
    segment_id: UUID,
    data: SegmentCreate,
    sample_size: int = Query(
        1000, ge=10, le=10000, description="Sample size for estimation"
    ),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> AudiencePreviewResponse:
    """Estimate the audience size for a set of rules.

    The ruleset and ``sample_size`` are checked against the limits in
    ``backend.app.core.segment_preview_limits`` before any database query; a
    request over any of them is answered 422.
    """
    _require_permission(current_user, Action.READ)
    if data.rules is None:
        raise HTTPException(
            status_code=422, detail="rules: preview takes rules; an id_list has none"
        )
    violations = preview_ruleset_violations(data.rules, sample_size)
    if violations:
        raise HTTPException(status_code=422, detail=violations)
    # Verify segment exists (informational, rules come from request body)
    try:
        AudienceService.get_segment(db, segment_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return AudienceService.preview_audience_size(
        db, data.rules, sample_size=sample_size
    )


# ---------------------------------------------------------------------------
# Id-list members (beta)
# ---------------------------------------------------------------------------

_MEMBER_RESPONSES = {
    404: {"description": "No segment has this id."},
    409: {
        "description": "The segment is a rules segment, or it is archived: its "
        "members cannot be changed."
    },
    422: {
        "description": "The request is outside the limits, or the change would "
        "pass 1,000,000 members (nothing is written). The message never "
        "repeats a submitted id."
    },
}


@router.post(
    "/{segment_id}/members",
    response_model=SegmentMembersAddResponse,
    summary="Add user ids to an id-list segment",
    description=(
        "Add 1 to 10,000 user ids (each 1 to 255 characters, matched exactly) "
        "to an id_list segment. Ids already in it are left alone, so sending the "
        "same ids again is safe. A segment holds at most 1,000,000 ids; a request "
        "that would pass that answers 422 and adds nothing. Answers the counts: "
        "added + already_members is the number of distinct ids sent. Requires "
        "DEVELOPER or ADMIN role."
    ),
    responses=_MEMBER_RESPONSES,
    openapi_extra={"x-stability": "beta"},
    tags=["Segments"],
)
def add_segment_members(
    segment_id: UUID,
    data: SegmentMembersAdd,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> SegmentMembersAddResponse:
    """Add user ids to an id-list segment (idempotent)."""
    _require_permission(current_user, Action.UPDATE)
    try:
        name, result = AudienceService.add_members(db, segment_id, data.add)
    except SegmentChangeRefused as exc:
        # Fixed text chosen by the service: counts at most, never an id.
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    # Counts only: the ids themselves are never recorded.
    _record(
        db,
        current_user,
        ActionType.SEGMENT_UPDATE,
        segment_id,
        name,
        after=result.model_dump(),
        reason="members added",
    )
    return result


@router.post(
    "/{segment_id}/members/remove",
    response_model=SegmentMembersRemoveResponse,
    summary="Remove user ids from an id-list segment",
    description=(
        "Remove 1 to 10,000 user ids from an id_list segment. Ids that are not "
        "in it are ignored. Answers the counts: removed + not_members is the "
        "number of distinct ids sent. Requires DEVELOPER or ADMIN role."
    ),
    responses=_MEMBER_RESPONSES,
    openapi_extra={"x-stability": "beta"},
    tags=["Segments"],
)
def remove_segment_members(
    segment_id: UUID,
    data: SegmentMembersRemove,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> SegmentMembersRemoveResponse:
    """Remove user ids from an id-list segment (idempotent)."""
    _require_permission(current_user, Action.UPDATE)
    try:
        name, result = AudienceService.remove_members(db, segment_id, data.remove)
    except SegmentChangeRefused as exc:
        # Fixed text chosen by the service: counts at most, never an id.
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    # Counts only: the ids themselves are never recorded.
    _record(
        db,
        current_user,
        ActionType.SEGMENT_UPDATE,
        segment_id,
        name,
        after=result.model_dump(),
        reason="members removed",
    )
    return result
