"""
Mutual Exclusion Group API endpoints (EP-022).

REST endpoints for managing mutual exclusion groups that prevent users
from being in conflicting experiments.

Routes:
    GET    /api/v1/mutual-exclusion-groups                                — list groups
    POST   /api/v1/mutual-exclusion-groups                                — create group (DEVELOPER+; 409: name taken)
    GET    /api/v1/mutual-exclusion-groups/{group_id}                     — get group
    PUT    /api/v1/mutual-exclusion-groups/{group_id}                     — update group (DEVELOPER+; a status change ADMIN; 409: name taken)
    DELETE /api/v1/mutual-exclusion-groups/{group_id}                     — archive group (ADMIN)
    POST   /api/v1/mutual-exclusion-groups/{group_id}/experiments         — add experiment
    DELETE /api/v1/mutual-exclusion-groups/{group_id}/experiments/{eid}   — remove experiment
"""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from backend.app.api import deps
from backend.app.core.permissions import Action, ResourceType, check_permission
from backend.app.models.audit_log import ActionType, EntityType
from backend.app.models.experiment import Experiment
from backend.app.models.user import User, UserRole
from backend.app.schemas.mutual_exclusion_group import (
    AddExperimentToGroupRequest,
    MutualExclusionGroupCreate,
    MutualExclusionGroupListResponse,
    MutualExclusionGroupResponse,
    MutualExclusionGroupUpdate,
)
from backend.app.services.audit_service import (
    AuditService,
    audit_changes,
    audit_identity,
    audit_snapshot,
)
from backend.app.services.mutual_exclusion_service import (
    GroupNameTaken,
    MutualExclusionService,
    group_name_taken_detail,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_developer(user: User) -> None:
    """Raise 403 if user lacks DEVELOPER-level access to experiments."""
    if not check_permission(user, ResourceType.EXPERIMENT, Action.CREATE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not enough permissions to manage mutual exclusion groups",
        )


def _require_admin(user: User) -> None:
    """Raise 403 unless the user is an ADMIN or a superuser.

    The role itself is checked, not a permission from the role matrix: no
    resource/action pair there is held by ADMIN alone (DEVELOPER also holds
    EXPERIMENT DELETE).
    """
    if not (user.is_superuser or user.role == UserRole.ADMIN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin permissions required for this action",
        )


def _status_value(stored) -> str:
    """The stored status as its string value (the column holds a model enum)."""
    return getattr(stored, "value", stored)


def _record(db: Session, user: User, action: ActionType, group_id, name, **values):
    """The audit entry for a committed change to one group (fails open)."""
    AuditService.record_after_commit(
        db,
        actor=user,
        action=action,
        entity_type=EntityType.MUTUAL_EXCLUSION_GROUP,
        entity_id=group_id,
        entity_name=name or str(group_id),
        **values,
    )


def _group_name(db: Session, group_id: UUID):
    group = MutualExclusionService(db).get_group(group_id)
    return group.name if group is not None else None


# ---------------------------------------------------------------------------
# List groups
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=MutualExclusionGroupListResponse,
    summary="List mutual exclusion groups",
    description="Return a paginated list of mutual exclusion groups.",
    tags=["Mutual Exclusion Groups"],
)
def list_groups(
    status_filter: str = Query(default=None, alias="status"),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=1000),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> MutualExclusionGroupListResponse:
    """List mutual exclusion groups with optional status filter."""
    _require_developer(current_user)
    service = MutualExclusionService(db)
    items = service.list_groups(status=status_filter, skip=skip, limit=limit)
    total = service.count_groups(status=status_filter)
    return MutualExclusionGroupListResponse(items=items, total=total)


# ---------------------------------------------------------------------------
# Create group
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=MutualExclusionGroupResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a mutual exclusion group",
    description=(
        "Create a new mutual exclusion group. Requires DEVELOPER or ADMIN role. "
        "409 when another group has the name (compared exactly, case included)."
    ),
    tags=["Mutual Exclusion Groups"],
    responses={409: {"description": "Another mutual exclusion group has this name"}},
)
def create_group(
    data: MutualExclusionGroupCreate,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> MutualExclusionGroupResponse:
    """Create a new mutual exclusion group."""
    _require_developer(current_user)
    service = MutualExclusionService(db)
    try:
        group = service.create_group(
            name=data.name,
            description=data.description,
            traffic_allocation=data.traffic_allocation,
            owner_id=current_user.id,
        )
    except GroupNameTaken:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=group_name_taken_detail(data.name),
        ) from None
    response = MutualExclusionGroupResponse.model_validate(group)
    _record(
        db,
        current_user,
        ActionType.MUTUAL_EXCLUSION_GROUP_CREATE,
        response.id,
        response.name,
        after=audit_identity(audit_snapshot(EntityType.MUTUAL_EXCLUSION_GROUP, group)),
    )
    return response


# ---------------------------------------------------------------------------
# Get group
# ---------------------------------------------------------------------------


@router.get(
    "/{group_id}",
    response_model=MutualExclusionGroupResponse,
    summary="Get a mutual exclusion group",
    description="Retrieve a single mutual exclusion group by UUID.",
    tags=["Mutual Exclusion Groups"],
)
def get_group(
    group_id: UUID,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> MutualExclusionGroupResponse:
    """Retrieve a mutual exclusion group by ID."""
    _require_developer(current_user)
    service = MutualExclusionService(db)
    group = service.get_group(group_id)
    if not group:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Mutual exclusion group {group_id} not found",
        )
    return MutualExclusionGroupResponse.model_validate(group)


# ---------------------------------------------------------------------------
# Update group
# ---------------------------------------------------------------------------


@router.put(
    "/{group_id}",
    response_model=MutualExclusionGroupResponse,
    summary="Update a mutual exclusion group",
    description=(
        "Update a mutual exclusion group's name, description or traffic "
        "allocation. Requires DEVELOPER or ADMIN role. Changing status "
        "(archiving or unarchiving) requires ADMIN role, as archiving through "
        "DELETE does; sending the group's current status is accepted from a "
        "DEVELOPER. 409 when another group has the name (compared exactly, "
        "case included)."
    ),
    tags=["Mutual Exclusion Groups"],
    responses={409: {"description": "Another mutual exclusion group has this name"}},
)
def update_group(
    group_id: UUID,
    data: MutualExclusionGroupUpdate,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> MutualExclusionGroupResponse:
    """Update an existing mutual exclusion group.

    Order: DEVELOPER check, then 404, then a status change needs ADMIN.
    """
    _require_developer(current_user)
    service = MutualExclusionService(db)
    existing = service.get_group(group_id)
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Mutual exclusion group {group_id} not found",
        )
    if data.status is not None and data.status.value != _status_value(existing.status):
        _require_admin(current_user)
    before = audit_snapshot(EntityType.MUTUAL_EXCLUSION_GROUP, existing)
    try:
        group = service.update_group(
            group_id=group_id,
            name=data.name,
            description=data.description,
            traffic_allocation=data.traffic_allocation,
            status=data.status.value if data.status else None,
        )
    except GroupNameTaken:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=group_name_taken_detail(data.name),
        ) from None
    if not group:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Mutual exclusion group {group_id} not found",
        )
    response = MutualExclusionGroupResponse.model_validate(group)
    old, new = audit_changes(
        before, audit_snapshot(EntityType.MUTUAL_EXCLUSION_GROUP, group)
    )
    _record(
        db,
        current_user,
        ActionType.MUTUAL_EXCLUSION_GROUP_UPDATE,
        group_id,
        response.name,
        before=old,
        after=new,
    )
    return response


# ---------------------------------------------------------------------------
# Archive (soft delete) group
# ---------------------------------------------------------------------------


@router.delete(
    "/{group_id}",
    response_model=MutualExclusionGroupResponse,
    summary="Archive a mutual exclusion group",
    description="Soft-delete a mutual exclusion group by archiving it. Requires ADMIN role.",
    tags=["Mutual Exclusion Groups"],
)
def archive_group(
    group_id: UUID,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> MutualExclusionGroupResponse:
    """Archive (soft-delete) a mutual exclusion group."""
    _require_admin(current_user)
    service = MutualExclusionService(db)
    existing = service.get_group(group_id)
    before = (
        audit_snapshot(EntityType.MUTUAL_EXCLUSION_GROUP, existing)
        if existing is not None
        else {}
    )
    group = service.archive_group(group_id)
    if not group:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Mutual exclusion group {group_id} not found",
        )
    response = MutualExclusionGroupResponse.model_validate(group)
    old, new = audit_changes(
        before, audit_snapshot(EntityType.MUTUAL_EXCLUSION_GROUP, group)
    )
    _record(
        db,
        current_user,
        ActionType.MUTUAL_EXCLUSION_GROUP_ARCHIVE,
        group_id,
        response.name,
        before=old,
        after=new,
    )
    return response


# ---------------------------------------------------------------------------
# Add experiment to group
# ---------------------------------------------------------------------------


@router.post(
    "/{group_id}/experiments",
    status_code=status.HTTP_200_OK,
    summary="Add experiment to mutual exclusion group",
    description="Add an experiment to a mutual exclusion group. Requires DEVELOPER or ADMIN role.",
    tags=["Mutual Exclusion Groups"],
)
def add_experiment_to_group(
    group_id: UUID,
    data: AddExperimentToGroupRequest,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
):
    """Add an experiment to a mutual exclusion group."""
    _require_developer(current_user)
    service = MutualExclusionService(db)
    already_member = (
        db.query(Experiment.mutual_exclusion_group_id)
        .filter(Experiment.id == data.experiment_id)
        .scalar()
        == group_id
    )
    try:
        service.add_experiment_to_group(group_id, data.experiment_id)
        if not already_member:
            _record(
                db,
                current_user,
                ActionType.MUTUAL_EXCLUSION_GROUP_UPDATE,
                group_id,
                _group_name(db, group_id),
                after={"experiment_id": str(data.experiment_id)},
                reason="experiment added",
            )
        return {
            "status": "ok",
            "experiment_id": str(data.experiment_id),
            "group_id": str(group_id),
        }
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )


# ---------------------------------------------------------------------------
# Remove experiment from group
# ---------------------------------------------------------------------------


@router.delete(
    "/{group_id}/experiments/{experiment_id}",
    status_code=status.HTTP_200_OK,
    summary="Remove experiment from mutual exclusion group",
    description="Remove an experiment from a mutual exclusion group. Requires DEVELOPER or ADMIN role.",
    tags=["Mutual Exclusion Groups"],
)
def remove_experiment_from_group(
    group_id: UUID,
    experiment_id: UUID,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
):
    """Remove an experiment from a mutual exclusion group."""
    _require_developer(current_user)
    service = MutualExclusionService(db)
    try:
        service.remove_experiment_from_group(group_id, experiment_id)
        _record(
            db,
            current_user,
            ActionType.MUTUAL_EXCLUSION_GROUP_UPDATE,
            group_id,
            _group_name(db, group_id),
            before={"experiment_id": str(experiment_id)},
            reason="experiment removed",
        )
        return {
            "status": "ok",
            "experiment_id": str(experiment_id),
            "group_id": str(group_id),
        }
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
