"""
Global Holdout API endpoints (EP-022).

REST endpoints for managing global holdout configurations that reserve a
percentage of users from all experiments.

Routes:
    GET    /api/v1/holdout            — get active holdout
    GET    /api/v1/holdout/all        — list all holdouts (ADMIN)
    POST   /api/v1/holdout            — create holdout (ADMIN)
    PUT    /api/v1/holdout/{id}       — update holdout (ADMIN)

Activating a holdout (``is_active: true`` on POST or PUT) deactivates the
active one.  Refused: a different ``holdout_percentage`` once the holdout has
been active (422), restarting an ended holdout (422), an activation that
races another one (409), and a name another holdout has (409, #1019).
    GET    /api/v1/holdout/check/{uid} — check if user is in holdout
    GET    /api/v1/holdout/{id}/results — holdout vs everyone else (beta)

The results route is declared after ``/check/{user_id}``: declared first, it
would take ``GET /holdout/check/results`` and answer 422 for a user called
``results``.
"""

import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from backend.app.api import deps
from backend.app.core.permissions import Action, ResourceType, check_permission
from backend.app.models.audit_log import ActionType, EntityType
from backend.app.models.user import User, UserRole
from backend.app.schemas.global_holdout import (
    GlobalHoldoutCreate,
    GlobalHoldoutListResponse,
    GlobalHoldoutResponse,
    GlobalHoldoutUpdate,
    HoldoutCheckResponse,
    HoldoutResultsResponse,
)
from backend.app.services.audit_service import (
    AuditService,
    audit_changes,
    audit_identity,
    audit_snapshot,
)
from backend.app.services.global_holdout_service import (
    GlobalHoldoutService,
    HoldoutActivationConflict,
    HoldoutEnded,
    HoldoutNameTaken,
    HoldoutPercentageLocked,
)
from backend.app.services.holdout_results import holdout_results

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_developer(user: User) -> None:
    """Raise 403 if user lacks DEVELOPER-level access."""
    if not check_permission(user, ResourceType.EXPERIMENT, Action.CREATE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not enough permissions to access holdout configuration",
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


def _refusal(error: Exception) -> HTTPException:
    """The HTTP answer for a refusal raised by the service."""
    if isinstance(error, (HoldoutActivationConflict, HoldoutNameTaken)):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error)
    )


def _record(db: Session, user: User, action: ActionType, holdout_id, name, **values):
    AuditService.record_after_commit(
        db,
        actor=user,
        action=action,
        entity_type=EntityType.HOLDOUT,
        entity_id=holdout_id,
        entity_name=name,
        **values,
    )


def _record_implicit_deactivations(
    db: Session, user: User, service: GlobalHoldoutService, changed_id
) -> None:
    """One ``holdout_deactivate`` for each other holdout this request turned off."""
    for holdout_id, name in service.implicitly_deactivated:
        if holdout_id == changed_id:
            continue
        _record(
            db,
            user,
            ActionType.HOLDOUT_DEACTIVATE,
            holdout_id,
            name,
            before={"is_active": True},
            after={"is_active": False},
            reason="another holdout was activated",
        )


# ---------------------------------------------------------------------------
# Get active holdout
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=Optional[GlobalHoldoutResponse],
    summary="Get the active global holdout",
    description="Return the currently active global holdout, or null if none is active.",
    tags=["Global Holdout"],
)
def get_active_holdout(
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> Optional[GlobalHoldoutResponse]:
    """Return the active holdout, or null."""
    _require_developer(current_user)
    service = GlobalHoldoutService(db)
    holdout = service.get_active_holdout()
    if not holdout:
        return None
    return GlobalHoldoutResponse.model_validate(holdout)


# ---------------------------------------------------------------------------
# List all holdouts
# ---------------------------------------------------------------------------


@router.get(
    "/all",
    response_model=GlobalHoldoutListResponse,
    summary="List all global holdouts",
    description="Return a paginated list of all holdout configurations. Requires ADMIN role.",
    tags=["Global Holdout"],
)
def list_all_holdouts(
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=1000),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> GlobalHoldoutListResponse:
    """List all holdout configurations."""
    _require_admin(current_user)
    service = GlobalHoldoutService(db)
    items = service.list_holdouts(skip=skip, limit=limit)
    total = service.count_holdouts()
    return GlobalHoldoutListResponse(items=items, total=total)


# ---------------------------------------------------------------------------
# Create holdout
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=GlobalHoldoutResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a global holdout",
    description=(
        "Create a new global holdout configuration. Requires ADMIN role. "
        "With is_active true it becomes the active holdout and the one that "
        "was active is deactivated; 409 when another activation happened at "
        "the same time. 409 when another holdout has the name (compared "
        "exactly, case included)."
    ),
    tags=["Global Holdout"],
    responses={
        409: {
            "description": (
                "Another holdout has this name, or another activation happened "
                "at the same time"
            )
        }
    },
)
def create_holdout(
    data: GlobalHoldoutCreate,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> GlobalHoldoutResponse:
    """Create a new global holdout."""
    _require_admin(current_user)
    service = GlobalHoldoutService(db)
    try:
        holdout = service.create_holdout(
            name=data.name,
            description=data.description,
            holdout_percentage=data.holdout_percentage,
            is_active=data.is_active,
            owner_id=current_user.id,
        )
    except (HoldoutActivationConflict, HoldoutNameTaken) as error:
        raise _refusal(error)
    response = GlobalHoldoutResponse.model_validate(holdout)
    _record(
        db,
        current_user,
        ActionType.HOLDOUT_CREATE,
        response.id,
        response.name,
        after=audit_identity(audit_snapshot(EntityType.HOLDOUT, holdout)),
    )
    _record_implicit_deactivations(db, current_user, service, response.id)
    return response


# ---------------------------------------------------------------------------
# Update holdout
# ---------------------------------------------------------------------------


@router.put(
    "/{holdout_id}",
    response_model=GlobalHoldoutResponse,
    summary="Update a global holdout",
    description=(
        "Update a global holdout configuration. Requires ADMIN role. "
        "is_active true deactivates the active holdout. Refused with 422: a "
        "different holdout_percentage once the holdout has been active (the "
        "same value is accepted), and is_active true on a holdout that has "
        "ended. 409 when another activation happened at the same time. 409 "
        "when another holdout has the name (compared exactly, case included)."
    ),
    tags=["Global Holdout"],
    responses={
        409: {
            "description": (
                "Another holdout has this name, or another activation happened "
                "at the same time"
            )
        }
    },
)
def update_holdout(
    holdout_id: UUID,
    data: GlobalHoldoutUpdate,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> GlobalHoldoutResponse:
    """Update a holdout configuration."""
    _require_admin(current_user)
    service = GlobalHoldoutService(db)
    existing = service.get_holdout(holdout_id)
    before = audit_snapshot(EntityType.HOLDOUT, existing) if existing else None
    try:
        holdout = service.update_holdout(
            holdout_id=holdout_id,
            name=data.name,
            description=data.description,
            holdout_percentage=data.holdout_percentage,
            is_active=data.is_active,
        )
    except (
        HoldoutPercentageLocked,
        HoldoutEnded,
        HoldoutActivationConflict,
        HoldoutNameTaken,
    ) as error:
        raise _refusal(error)
    if not holdout:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Holdout {holdout_id} not found",
        )
    response = GlobalHoldoutResponse.model_validate(holdout)
    _record_holdout_update(db, current_user, before, holdout)
    _record_implicit_deactivations(db, current_user, service, holdout_id)
    return response


def _record_holdout_update(db: Session, user: User, before, holdout) -> None:
    """One entry per verb: activate or deactivate, and update for the rest.

    ``holdout`` is the stored row after the change, ``before`` its snapshot
    before it. Nothing allow-listed changed, nothing is recorded.
    """
    holdout_id, name = holdout.id, holdout.name
    old, new = audit_changes(before or {}, audit_snapshot(EntityType.HOLDOUT, holdout))
    old, new = old or {}, new or {}
    if "is_active" in new:
        _record(
            db,
            user,
            ActionType.HOLDOUT_ACTIVATE
            if new["is_active"]
            else ActionType.HOLDOUT_DEACTIVATE,
            holdout_id,
            name,
            before=_pop_lifecycle(old),
            after=_pop_lifecycle(new),
        )
    if new:
        _record(
            db,
            user,
            ActionType.HOLDOUT_UPDATE,
            holdout_id,
            name,
            before=old or None,
            after=new,
        )


_LIFECYCLE_FIELDS = ("is_active", "activated_at", "deactivated_at")


def _pop_lifecycle(values: dict) -> dict:
    """``is_active`` and the timestamp that changed with it, taken out of ``values``.

    Activating stamps ``activated_at`` and deactivating ``deactivated_at``;
    they belong to the activate or deactivate entry, not to a second one.
    """
    return {field: values.pop(field) for field in _LIFECYCLE_FIELDS if field in values}


# ---------------------------------------------------------------------------
# Check user holdout status
# ---------------------------------------------------------------------------


@router.get(
    "/check/{user_id}",
    response_model=HoldoutCheckResponse,
    summary="Check if a user is in the global holdout",
    description="Return whether a user falls within the global holdout bucket.",
    tags=["Global Holdout"],
)
def check_user_holdout(
    user_id: str,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> HoldoutCheckResponse:
    """Check whether a user is in the global holdout."""
    _require_developer(current_user)
    service = GlobalHoldoutService(db)
    is_in_holdout, holdout_percentage, bucket = service.is_user_in_holdout(user_id)
    return HoldoutCheckResponse(
        user_id=user_id,
        is_in_holdout=is_in_holdout,
        holdout_percentage=holdout_percentage,
        bucket=bucket,
    )


# ---------------------------------------------------------------------------
# Results (beta).  Declared after /check/{user_id}; see the module docstring.
# ---------------------------------------------------------------------------


@router.get(
    "/{holdout_id}/results",
    response_model=HoldoutResultsResponse,
    summary="Beta: results for a global holdout",
    description=(
        "For one event name, the share of users who sent at least one "
        "matching event: the users the holdout kept out against everyone else "
        "first seen while it was active. Every user recorded is in their "
        "group, users refused by targeting or mutual exclusion included; a "
        "user with an assignment before they were first seen is in neither. "
        "An event counts when its own time and the time the server received "
        "it are both between the user's first-seen time and window_end, so "
        "events received after a holdout ends never change its results. Each "
        "group needs 100 users. Any role that can read experiment results can "
        "read this."
    ),
    tags=["Global Holdout"],
    openapi_extra={"x-stability": "beta"},
)
def get_holdout_results(
    holdout_id: UUID,
    metric: str = Query(
        ...,
        min_length=1,
        max_length=255,
        description="The event name to count, matched exactly, e.g. purchase.",
    ),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> HoldoutResultsResponse:
    """Compare the holdout's users with everyone else for ``metric``."""
    if not current_user.is_superuser and not check_permission(
        current_user, ResourceType.EXPERIMENT, Action.READ
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not enough permissions to read holdout results",
        )
    holdout = GlobalHoldoutService(db).get_holdout(holdout_id)
    if not holdout:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Holdout {holdout_id} not found",
        )
    return HoldoutResultsResponse(**holdout_results(db, holdout, metric))
