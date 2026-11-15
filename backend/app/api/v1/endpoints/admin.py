"""
Admin API endpoints.

This module provides API endpoints for administrative operations
that require superuser privileges.
"""

import json
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from backend.app.api import deps
from backend.app.api.off_loop_route import OffLoopRoute
from backend.app.api.query_params import MAX_OFFSET, describe_as_int64
from backend.app.api.v1.endpoints.users import (
    OWN_DEACTIVATION_REFUSED,
    OWN_ROLE_REFUSED,
    apply_password_change,
    changed_email,
    changed_username,
    commit_user_write,
    record_access_changes,
    refuse_if_email_held,
    refuse_if_own_access_removed,
    refuse_if_username_held,
    removes_an_active_superuser,
    require_still_active_superuser,
)
from backend.app.core.config import settings
from backend.app.models.audit_log import ActionType, EntityType
from backend.app.models.user import User, UserRole
from backend.app.schemas.user import (
    AdminUserPatch,
    UserListResponse,
    UserResponse,
    UserUpdate,
)
from backend.app.services.audit_service import (
    AuditService,
    audit_identity,
    audit_snapshot,
    role_value,
)

router = APIRouter(route_class=OffLoopRoute)


#: The columns ``search`` matches, named one by one. Never build this list from
#: the model's columns: anything added to ``User`` must not become searchable
#: without someone deciding it should be (#651).
USER_SEARCH_COLUMNS = (
    User.username,
    User.email,
    User.first_name,
    User.last_name,
)


@router.get("/users", response_model=UserListResponse)
async def list_users(
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_superuser),
    skip: int = Query(0, ge=0, le=MAX_OFFSET, json_schema_extra=describe_as_int64),
    limit: int = Query(100, ge=1, le=100),
    search: Optional[str] = Query(
        None,
        max_length=100,
        description=(
            "Case-insensitive substring of the username, email, first name or "
            "last name. Matched literally (`%`, `_` and `\\` are not "
            "wildcards). Leading and trailing spaces are ignored; an empty or "
            "all-space value lists every user."
        ),
    ),
) -> Any:
    """
    List all users.

    This endpoint is only accessible by superusers and returns a list of all users
    in the system with pagination, newest first.

    With `search`, only users whose username, email, first name or last name
    contains the term (case-insensitive, matched literally) are listed, and
    `total` counts those users. A term containing a NUL character answers 422.
    """
    if search is not None and "\x00" in search:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="search must not contain a NUL character",
        )
    term = (search or "").strip()

    query = db.query(User)
    if term:
        query = query.filter(
            or_(
                *(
                    column.icontains(term, autoescape=True)
                    for column in USER_SEARCH_COLUMNS
                )
            )
        )

    # Ordered: a paginated query without ORDER BY can repeat or skip rows
    # between pages, because the database is free to return them in any order.
    # Newest first is what an administrator looking for a just-created account
    # wants on page one.
    users = (
        query.order_by(User.created_at.desc(), User.id).offset(skip).limit(limit).all()
    )
    # Counted from the same filtered query, so "x of total" describes the list.
    total = query.count()

    return UserListResponse(items=users, total=total, skip=skip, limit=limit)


@router.get("/users/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: uuid.UUID = Path(..., description="The ID of the user to retrieve"),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_superuser),
) -> Any:
    """
    Get user details.

    This endpoint is only accessible by superusers and returns details
    of a specific user by ID.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
        )

    return user


@router.put("/users/{user_id}", response_model=UserResponse)
async def update_user(
    user_in: UserUpdate,
    user_id: uuid.UUID = Path(..., description="The ID of the user to update"),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_superuser),
) -> Any:
    """
    Update user.

    This endpoint is only accessible by superusers and allows updating
    user details, including superuser status.
    """
    # Get the user to update
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
        )

    update_data = user_in.model_dump(exclude_unset=True)
    # ``password`` becomes ``hashed_password`` (another account's reset), or
    # is refused (your own: use POST /api/v1/users/me/password). The loop
    # below cannot set it: the model has no ``password`` attribute.
    apply_password_change(user, current_user, update_data)
    # Your own superuser access or active status is not removed here (400,
    # #652); another superuser does it. Before anything is written.
    refuse_if_own_access_removed(user, current_user, update_data)

    # Another account holding the new address in any letter case is a 409
    # "Email already registered". Re-casing the account's own address is not.
    new_email = changed_email(user, update_data)
    refuse_if_email_held(db, new_email, exclude_id=user.id)
    # Another account with the new username is a 409 "Username already
    # registered", as on create (#610). It was a 500.
    new_username = changed_username(user, update_data)
    refuse_if_username_held(db, new_username, exclude_id=user.id)

    # Demoting or deactivating another superuser: the caller must still be an
    # active superuser once those rows are locked (#652). No await between
    # this and the commit.
    if removes_an_active_superuser(user, update_data):
        require_still_active_superuser(db, current_user)

    role_before, active_before = role_value(user), user.is_active

    # Update user attributes
    for field in update_data:
        if hasattr(user, field):
            setattr(user, field, update_data[field])

    # The audit entries go in the same transaction as the change: both are
    # written or neither is. No await between the lock above and the commit.
    record_access_changes(db, current_user, user, role_before, active_before)
    commit_user_write(db, new_email, exclude_id=user.id, username=new_username)
    db.refresh(user)

    return user


#: Refusal of ``PATCH /admin/users/{user_id}``. The dashboard shows it as it
#: is, so it is written for the person at the screen. ``OWN_ROLE_REFUSED`` and
#: ``OWN_DEACTIVATION_REFUSED`` are defined in the users endpoints, which both
#: PUT routes share.
ROLE_FROM_COGNITO_REFUSED = (
    "Roles on this deployment come from Cognito groups and are updated on every "
    "request. Change this user's group in Cognito instead."
)


def _role_name(user: User) -> Any:
    """The account's role as the API names it (``"ANALYST"``), or ``None``."""
    return user.role.name if user.role is not None else None


@router.patch(
    "/users/{user_id}",
    response_model=UserResponse,
    openapi_extra={"x-stability": "beta"},
)
async def patch_user(
    user_in: AdminUserPatch,
    user_id: uuid.UUID = Path(..., description="The ID of the user to change"),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_superuser),
) -> Any:
    """
    Change a user's role and/or active status.

    Superusers only. Send only the keys to change. You cannot change your own
    role or deactivate yourself (400). With Cognito sign-in and role sync on,
    a role change is refused (409): the next request would overwrite it.
    Resending the stored values changes nothing and answers 200. Deactivating
    a user also stops the API keys they created.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
        )

    sent = user_in.model_fields_set
    role_changes = "role" in sent and user_in.role != _role_name(user)
    active_changes = "is_active" in sent and user_in.is_active != user.is_active

    if user.id == current_user.id:
        if role_changes:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=OWN_ROLE_REFUSED
            )
        if active_changes:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=OWN_DEACTIVATION_REFUSED,
            )

    if (
        role_changes
        and settings.AUTH_PROVIDER == "cognito"
        and settings.SYNC_ROLES_ON_LOGIN
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=ROLE_FROM_COGNITO_REFUSED
        )

    if not (role_changes or active_changes):
        return user

    if active_changes and user_in.is_active is False and user.is_superuser:
        # The caller must still be an active superuser once the rows are
        # locked; one deactivated or demoted a moment ago by another
        # superuser is not (400 "Inactive user" / 403).
        require_still_active_superuser(db, current_user)

    role_before, active_before = role_value(user), user.is_active
    # Only these two columns, by name: nothing else in the request can reach
    # the row (the schema refuses any other key as well).
    if role_changes:
        user.role = UserRole[user_in.role]
    if active_changes:
        user.is_active = user_in.is_active

    # The audit entries are part of the same transaction: both are written or
    # neither is. No await between the lock above and this commit.
    record_access_changes(db, current_user, user, role_before, active_before)
    db.commit()
    db.refresh(user)

    return user


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: uuid.UUID = Path(..., description="The ID of the user to delete"),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_superuser),
) -> None:
    """
    Delete user.

    This endpoint is only accessible by superusers and allows deleting users.
    It prevents superusers from deleting themselves.
    """
    # Get the user to delete
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
        )

    # Prevent superusers from deleting themselves
    if user.id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete your own user account",
        )

    # Deleting an active superuser: the caller must still be one once those
    # rows are locked (#652). No await between this and the commit.
    if user.is_superuser and user.is_active:
        require_still_active_superuser(db, current_user)

    deleted = audit_identity(audit_snapshot(EntityType.USER, user))
    deleted_id = user.id

    # Delete the user
    db.delete(user)
    db.commit()

    # The delete is committed; a failed audit write does not undo it.
    AuditService.record_after_commit(
        db,
        actor=current_user,
        action=ActionType.USER_DELETE,
        entity_type=EntityType.USER,
        entity_id=deleted_id,
        entity_name=deleted["username"] or str(deleted_id),
        before=deleted,
    )


@router.get("/stats", response_model=Dict[str, Any])
async def get_system_stats(
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_superuser),
    cache_control: deps.CacheControl = Depends(deps.get_cache_control),
) -> Any:
    """
    Get system statistics.

    This endpoint is only accessible by superusers and provides
    system-wide statistics like user counts, experiment counts, etc.
    """
    # Try to get from cache if enabled
    cache_key = "admin:system_stats"
    cache_available = cache_control.enabled and cache_control.redis is not None
    if cache_available:
        cached_data = await cache_control.redis.get(cache_key)
        if cached_data:
            return json.loads(cached_data)

    # Count various entities
    from backend.app.models.event import Event
    from backend.app.models.experiment import Experiment, ExperimentStatus
    from backend.app.models.feature_flag import FeatureFlag, FeatureFlagStatus

    user_count = db.query(User).count()
    active_user_count = db.query(User).filter(User.is_active == True).count()
    superuser_count = db.query(User).filter(User.is_superuser == True).count()

    experiment_count = db.query(Experiment).count()
    active_experiment_count = (
        db.query(Experiment)
        .filter(Experiment.status == ExperimentStatus.ACTIVE)
        .count()
    )

    event_count = db.query(Event).count()

    feature_flag_count = db.query(FeatureFlag).count()
    active_feature_flag_count = (
        db.query(FeatureFlag)
        .filter(FeatureFlag.status == FeatureFlagStatus.ACTIVE)
        .count()
    )

    # Calculate daily event rate (simplified)
    import datetime

    yesterday = datetime.datetime.utcnow() - datetime.timedelta(days=1)
    daily_events = (
        db.query(Event).filter(Event.created_at >= yesterday.isoformat()).count()
    )

    # Compile stats
    stats = {
        "users": {
            "total": user_count,
            "active": active_user_count,
            "superusers": superuser_count,
        },
        "experiments": {"total": experiment_count, "active": active_experiment_count},
        "events": {"total": event_count, "daily_rate": daily_events},
        "feature_flags": {
            "total": feature_flag_count,
            "active": active_feature_flag_count,
        },
        "timestamp": datetime.datetime.utcnow().isoformat(),
    }

    # Cache stats if enabled
    if cache_available:
        await cache_control.redis.setex(
            cache_key,
            60 * 5,
            json.dumps(stats),  # 5 minute TTL for stats
        )

    return stats


# Namespaces the application writes to Redis. There is no single key prefix:
# every cache key in `backend/app/api/v1/endpoints/` starts with one of these
# (`admin:system_stats`, `experiment:{id}`, `results:{id}`, ...). Add a
# namespace here when you add one there, or "clear cache" will quietly leave it.
# The flag routes no longer cache (#630), so there is no flag namespace; keys
# an older release left behind are read by nothing and expire within an hour.
CACHE_NAMESPACES: tuple = (
    "admin",
    "experiment",
    "experiments",
    "experiment_daily_results",
    "experiment_segmented_results",
    "results",
)


@router.post("/cache/clear", status_code=status.HTTP_200_OK)
async def clear_cache(
    current_user: User = Depends(deps.get_current_superuser),
    cache_control: deps.CacheControl = Depends(deps.get_cache_control),
) -> Dict[str, Any]:
    """
    Clear system cache.

    This endpoint is only accessible by superusers and clears all Redis cache entries
    for the application.
    """
    if not cache_control.enabled or cache_control.redis is None:
        return {"message": "Caching is not enabled"}

    # This used to scan `f"{settings.REDIS_PREFIX}:*"`, a setting that does not
    # exist, so the endpoint raised AttributeError for every caller whose cache
    # was actually enabled. The keys carry no shared prefix, so each namespace
    # is scanned in turn.
    keys_deleted = 0
    for namespace in CACHE_NAMESPACES:
        async for key in cache_control.redis.scan_iter(match=f"{namespace}:*"):
            await cache_control.redis.delete(key)
            keys_deleted += 1

    return {"message": "Cache cleared successfully", "keys_deleted": keys_deleted}
