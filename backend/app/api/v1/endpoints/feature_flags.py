"""
Feature Flag API endpoints.

This module provides RESTful API endpoints for creating, reading, updating, and deleting
feature flags in the experimentation platform. It implements functionality to toggle and
manage feature flags for gradual rollouts and A/B testing.
"""

import json
import logging
from typing import Annotated, Any, Dict, Optional
from uuid import UUID

from fastapi import (
    APIRouter,
    Body,
    Depends,
    HTTPException,
    Path,
    Query,
    Response,
    status,
)
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.api import deps
from backend.app.api.segment_rules import refuse_unusable_segments
from backend.app.core.context_limits import (
    CONTEXT_TOO_DEEP,
    check_context,
    check_context_text,
)
from backend.app.core.flag_values import flag_value_type, served_value
from backend.app.core.logger import unexpected_failure
from backend.app.core.metrics import record_flag_evaluation
from backend.app.core.permissions import (
    Action,
    ResourceType,
    can_act_on_feature_flag,
    check_permission,
)
from backend.app.crud import crud_feature_flag
from backend.app.models.audit_log import ActionType, EntityType
from backend.app.models.compliance_audit_event import AuditAction, AuditOutcome
from backend.app.models.feature_flag import (
    ARCHIVED_FLAG_DETAIL,
    ArchivedFlagError,
    FeatureFlag,
    FeatureFlagStatus,
    flag_status_name,
)
from backend.app.models.user import User
from backend.app.schemas.audit_log import ToggleRequest, ToggleResponse
from backend.app.schemas.feature_flag import (
    STATUS_READ_ONLY,
    FeatureFlagCreate,
    FeatureFlagListResponse,
    FeatureFlagRead,
    FeatureFlagUpdate,
)
from backend.app.schemas.storable_text import storable_text_param
from backend.app.services.audit_log_service import AuditLogService
from backend.app.services.audit_service import (
    AuditService,
    audit_changes,
    audit_identity,
    audit_snapshot,
)
from backend.app.services.feature_flag_service import (
    FeatureFlagService,
    FlagStatusReadOnly,
    FlagValuesRefused,
    FlagVerb,
    transition,
)

# Setup logger
logger = logging.getLogger(__name__)

#: Postgres error code for a unique constraint violation.
_UNIQUE_VIOLATION = "23505"


def _flag_key_taken_detail(key: str) -> str:
    return f"Feature flag with key '{key}' already exists"


def _flag_key_held_by_another(
    db: Session, key: str, exclude_id: Optional[UUID] = None
) -> bool:
    """True when a stored flag other than *exclude_id* has *key*.

    One indexed query on the unique key, so it holds however many flags exist.
    """
    query = db.query(FeatureFlag.id).filter(FeatureFlag.key == key)
    if exclude_id is not None:
        query = query.filter(FeatureFlag.id != exclude_id)
    return query.first() is not None


def _raise_if_key_conflict(
    db: Session, exc: IntegrityError, key: Optional[str], exclude_id: Optional[UUID]
) -> None:
    """Answer 409 when *exc* is a unique violation and *key* is now taken.

    The pre-checks in create and update leave a gap before the commit: another
    request can store the same key in it, and the unique index on ``key`` then
    refuses ours. Decided by rolling back and looking the key up again (as
    users.py does for emails), not by parsing the driver's message or naming
    the index -- its name depends on how the schema was built. Returns without
    raising when the refusal was about something else; the caller re-raises.
    """
    db.rollback()
    orig = getattr(exc, "orig", None)
    code = getattr(orig, "pgcode", None) or getattr(orig, "sqlstate", None)
    if (
        key is not None
        and code == _UNIQUE_VIOLATION
        and _flag_key_held_by_another(db, key, exclude_id)
    ):
        logger.info("Feature flag write refused: key %r already exists", key)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=_flag_key_taken_detail(key)
        ) from None


def _status_read_only() -> RequestValidationError:
    """The 422 for a sent ``status`` that differs from the flag's (#94).

    The same shape as every other request validation error, through the
    application's handler; nothing in it comes from the request.
    """
    return RequestValidationError(
        [{"type": "read_only", "loc": ("body", "status"), "msg": STATUS_READ_ONLY}]
    )


def _values_refused(refused: FlagValuesRefused) -> RequestValidationError:
    """The 422 for an update whose ``value_type`` or values do not fit (#1497).

    The same shape as every other request validation error: each refusal at
    ``["body", <field>]`` with its fixed ``type`` and ``msg``, which name the
    field and never the value.
    """
    return RequestValidationError(
        [
            {"type": error.type, "loc": ("body", field), "msg": error.message()}
            for field, error in refused.errors
        ]
    )


def _archived_refusal() -> HTTPException:
    """The 400 for a request that would turn an archived flag on (#631).

    A state refusal answers 400, as elsewhere in the API (409 is for a taken
    key only). The way out is ``POST /feature-flags/{id}/unarchive``.
    """
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST, detail=ARCHIVED_FLAG_DETAIL
    )


#: The documented 400 of every route that can turn a flag on.
_ARCHIVED_400 = {
    status.HTTP_400_BAD_REQUEST: {
        "description": "The flag is archived: unarchive it before turning it on",
    }
}


async def _skip_cache_ignored(skip_cache: bool = False) -> None:
    """Accept the published ``skip_cache`` query parameter, and ignore it.

    The flag routes had an opt-in Redis cache (#100). It is gone (#630): every
    flag read comes from the database, so there is nothing to skip. Nine stable
    operations publish ``skip_cache`` (docs/api/openapi-v1.stable.json), and a
    stable operation does not change shape (docs/api/stability.md), so this
    dependency keeps the parameter with the same name, type and default and
    does nothing with it. Removing the parameter is tracked by #674.
    """
    del skip_cache


# Create router with tag for documentation grouping
router = APIRouter(
    tags=["Feature Flags"],
    responses={
        status.HTTP_401_UNAUTHORIZED: {
            "description": "Authentication failed",
            "content": {
                "application/json": {
                    "example": {
                        "error": {
                            "status_code": 401,
                            "message": "Could not validate credentials",
                        }
                    }
                }
            },
        },
        status.HTTP_403_FORBIDDEN: {
            "description": "Permission denied",
            "content": {
                "application/json": {
                    "example": {
                        "error": {
                            "status_code": 403,
                            "message": "Not enough permissions",
                        }
                    }
                }
            },
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "description": "Internal server error",
            "content": {
                "application/json": {
                    "example": {
                        "error": {
                            "status_code": 500,
                            "message": "Internal server error",
                        }
                    }
                }
            },
        },
    },
)


# The collection answers with and without the trailing slash (#94): without
# the twin, `/feature-flags` was a 307 to `/feature-flags/`, which curl does
# not follow and prints nothing for. Only the slash form is in the OpenAPI
# document. Single-flag URLs (`/{flag_id}/`) keep their 307 -- a `/{flag_id}/`
# twin would capture sibling paths such as `/bulk-toggle/`.
@router.get(
    "/",
    response_model=FeatureFlagListResponse,
    summary="List feature flags",
    response_description="Returns a paginated list of feature flags",
)
@router.get(
    "",
    response_model=FeatureFlagListResponse,
    include_in_schema=False,
)
async def list_feature_flags(
    *,
    db: Session = Depends(deps.get_db),
    skip: int = 0,
    limit: int = 100,
    # `status` in the URL only: a Python parameter called `status` hid the
    # `fastapi.status` module in this body, so reaching the 403 below raised
    # AttributeError and answered 500.
    status_filter: Optional[str] = Query(None, alias="status"),
    search: Optional[str] = None,
    current_user=Depends(deps.get_current_active_user),
) -> FeatureFlagListResponse:
    """
    Retrieve feature flags.
    - **skip**: Number of feature flags to skip in pagination
    - **limit**: Maximum number of feature flags to return
    - **status**: Filter by status (ACTIVE, INACTIVE)
    - **search**: Filter by name or key
    """
    # Check if user has permission to list feature flags
    if not check_permission(current_user, ResourceType.FEATURE_FLAG, Action.LIST):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have permission to list feature flags",
        )

    # Everyone the LIST check above admitted sees the whole platform.
    #
    # This had a third access model again, different from both the role table
    # and the experiments endpoint: superuser -> all; UPDATE permission
    # (ADMIN, DEVELOPER) -> all; otherwise own rows only. Its comment read
    # "Analyst/Viewer can only see their own", which inverts the role the docs
    # describe -- ANALYST exists to "view all data but not create or modify"
    # so it was the one role guaranteed to be wrong.
    #
    # All four roles carry Action.LIST on feature flags, a deployment is single
    # tenant (founder, 2026-09-21), and tenant isolation belongs to the
    # workspaces module rather than to row ownership. See #83.
    feature_flags_data = crud_feature_flag.get_multi(
        db, skip=skip, limit=limit, status=status_filter, search=search
    )
    total = crud_feature_flag.count(db, status=status_filter, search=search)

    # Create response with pagination
    response = FeatureFlagListResponse(
        items=[FeatureFlagRead.model_validate(flag) for flag in feature_flags_data],
        total=total,
        skip=skip,
        limit=limit,
    )

    return response


@router.post(
    "/",
    response_model=FeatureFlagRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create feature flag",
    response_description="Returns the created feature flag",
    responses={409: {"description": "A feature flag with this key already exists"}},
)
@router.post(
    "",
    response_model=FeatureFlagRead,
    status_code=status.HTTP_201_CREATED,
    include_in_schema=False,
)
async def create_feature_flag(
    feature_flag_in: FeatureFlagCreate = Body(
        ..., description="Feature flag data to create"
    ),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
    _skip_cache: None = Depends(_skip_cache_ignored),
    _: bool = Depends(deps.can_create_feature_flag),  # Use the permission dependency
) -> FeatureFlagRead:
    """
    Create a new feature flag.

    This endpoint allows users to create a new feature flag. The current user
    is automatically set as the owner of the feature flag.

    The request must include:
    - A unique key for the feature flag
    - A name for the feature flag
    - Optional description
    - Optional targeting rules for specific user segments
    - Optional rollout percentage
    - Optional `value_type` (`boolean` when left out) and, for a `string`,
      `number` or `json` flag, its `on_value` and `off_value`

    The flag is created off (INACTIVE) unless the request sends
    `is_active: true`. A field the API does not read answers 422. The read-only
    fields of a flag response (`id`, `owner_id`, `created_at`, `updated_at`,
    `status`) are accepted and ignored, except that a `status` must be the one
    the flag is created with. `targeting_rules` the flag evaluator would not
    apply as written answer 422, with the place and the reason in the message.
    A value that does not fit `value_type` or its limits answers 422 at that
    field, with a message that names the field and never the value.

    Returns:
        FeatureFlagRead: The newly created feature flag

    Raises:
        HTTPException 409: Another flag already has this key
        HTTPException 422: The body is not a valid flag
    """
    # Create feature flag service
    feature_flag_service = FeatureFlagService(db)

    # Check if feature flag with the same key already exists
    if _flag_key_held_by_another(db, feature_flag_in.key):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_flag_key_taken_detail(feature_flag_in.key),
        )

    # Every segment the rules name exists and is active (#440).
    refuse_unusable_segments(db, feature_flag_in.targeting_rules)

    # Create feature flag
    try:
        # The creator is the owner.  It is set by the service on the stored row,
        # not through the request schema, which has no owner field (and must not:
        # a client could otherwise create a flag in someone else's name).
        feature_flag = feature_flag_service.create_feature_flag(
            flag_data=feature_flag_in, owner_id=current_user.id
        )
    except FlagStatusReadOnly:
        raise _status_read_only() from None
    except IntegrityError as e:
        # A concurrent create took the key after the pre-check above.
        _raise_if_key_conflict(db, e, feature_flag_in.key, None)
        raise
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    # Built outside the ``except ValueError`` above: a ValidationError is a
    # ValueError, and its text would carry stored values into a 400.
    created = FeatureFlagRead.model_validate(feature_flag)

    # Compliance audit logging (non-fatal — do not fail the request if this fails)
    try:
        audit = AuditLogService(db)
        audit.log(
            action=AuditAction.CREATE,
            resource_type="feature_flag",
            outcome=AuditOutcome.SUCCESS,
            resource_id=str(created.id),
            actor_id=str(current_user.id) if current_user else None,
            new_value={
                "key": created.key,
                "name": created.name,
                "value_type": created.value_type,
            },
        )
        # log() only flushes, and the create above has already committed, so
        # without this the record is rolled back when the session closes.
        db.commit()
    except Exception as audit_error:
        db.rollback()
        logger.warning(
            f"Compliance audit logging failed for feature_flag create: {audit_error}"
        )

    # The flag is committed above; a failed audit write does not undo it.
    AuditService.record_after_commit(
        db,
        actor=current_user,
        action=ActionType.FEATURE_FLAG_CREATE,
        entity_type=EntityType.FEATURE_FLAG,
        entity_id=created.id,
        entity_name=created.name,
        after=audit_identity(audit_snapshot(EntityType.FEATURE_FLAG, feature_flag)),
    )

    return created


@router.get(
    "/{flag_id}",
    response_model=FeatureFlagRead,
    summary="Get feature flag",
    response_description="Returns the feature flag details",
)
async def get_feature_flag(
    flag_id: UUID = Path(..., description="The ID of the feature flag to retrieve"),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
    _skip_cache: None = Depends(_skip_cache_ignored),
) -> FeatureFlagRead:
    """
    Get feature flag by ID.

    Retrieves the detailed information for a specific feature flag.
    Users can only access feature flags they own or have permission to view.

    Returns:
        FeatureFlagRead: The feature flag

    Raises:
        HTTPException 404: If feature flag not found
        HTTPException 403: If user doesn't have access to this feature flag
    """
    # Access is decided on the stored row, before anything is served.
    owner_row = db.query(FeatureFlag.owner_id).filter(FeatureFlag.id == flag_id).first()
    if owner_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Feature flag not found"
        )
    if not can_act_on_feature_flag(current_user, owner_row.owner_id, Action.READ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not enough permissions to access this feature flag",
        )

    # Create feature flag service
    feature_flag_service = FeatureFlagService(db)

    # Get feature flag
    feature_flag = feature_flag_service.get_feature_flag(flag_id)
    if not feature_flag:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Feature flag not found"
        )

    return feature_flag


@router.put(
    "/{flag_id}",
    response_model=FeatureFlagRead,
    summary="Update feature flag",
    response_description="Returns the updated feature flag",
    responses={
        **_ARCHIVED_400,
        409: {"description": "Another feature flag already has this key"},
    },
)
async def update_feature_flag(
    flag_id: UUID = Path(..., description="The ID of the feature flag to update"),
    feature_flag_in: FeatureFlagUpdate = Body(
        ..., description="Feature flag data to update"
    ),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
    _skip_cache: None = Depends(_skip_cache_ignored),
) -> FeatureFlagRead:
    """
    Update a feature flag.

    This endpoint allows users to update an existing feature flag.
    The user must have access to the feature flag (be the owner or have permission).

    Only the fields sent change. They include:
    - Key, name and description
    - `is_active`, which turns the flag on or off. An archived flag sent
      `is_active: false` stays archived; sent `is_active: true` it answers
      400, and nothing is changed: unarchive it first
    - Targeting rules
    - Rollout percentage
    - `on_value` and `off_value`, for a `string`, `number` or `json` flag

    A field the API does not read answers 422, and so does an explicit null on
    `key`, `name`, `is_active`, `rollout_percentage`, `default_value`,
    `value_type`, `on_value` or `off_value`. The read-only fields of a flag
    response (`id`, `owner_id`, `created_at`, `updated_at`, `status`) are
    accepted and ignored, so a GET body can be sent back unchanged unless its
    `targeting_rules` are ones PUT now refuses (422); omitting the field still
    works. A `status` must equal the flag's, and a `value_type` the flag's type
    (422 `read_only` otherwise). A value is checked against the flag's type,
    and the two values as they will be stored must differ; a boolean flag's
    `on_value: true` and `off_value: false` are accepted and change nothing.

    `targeting_rules` the flag evaluator would not apply as written answer 422,
    with the place and the reason in the message. Stored rules are not
    re-checked unless they are sent.

    Returns:
        FeatureFlagRead: The updated feature flag

    Raises:
        HTTPException 400: `is_active: true` on an archived flag
        HTTPException 403: If the user doesn't have permission to update this feature flag
        HTTPException 409: Another flag already has the key
        HTTPException 422: The body is not a valid update
    """
    # Create feature flag service
    feature_flag_service = FeatureFlagService(db)

    # Get feature flag
    flag = db.query(FeatureFlag).filter(FeatureFlag.id == flag_id).first()
    if not flag:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Feature flag not found"
        )

    # Check access permission
    if not can_act_on_feature_flag(current_user, flag.owner_id, Action.UPDATE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not enough permissions to update this feature flag",
        )

    # If key is being updated, check if it conflicts with another feature flag
    # A direct query on the key: scanning a page of flags missed every flag
    # past the first 100, and the unique index then refused the commit (a 500).
    if (
        feature_flag_in.key
        and feature_flag_in.key != flag.key
        and _flag_key_held_by_another(db, feature_flag_in.key, flag_id)
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_flag_key_taken_detail(feature_flag_in.key),
        )

    # Capture pre-update state for audit trail
    old_flag_snapshot = {
        "key": flag.key,
        "name": flag.name,
        "status": str(flag.status),
        "rollout_percentage": flag.rollout_percentage,
        "value_type": flag_value_type(flag),
    }
    before_audit = audit_snapshot(EntityType.FEATURE_FLAG, flag)

    # Every segment the rules name exists and is active (#440).
    if "targeting_rules" in feature_flag_in.model_fields_set:
        refuse_unusable_segments(db, feature_flag_in.targeting_rules)

    # Update feature flag
    try:
        updated_row = feature_flag_service.update_feature_flag(flag, feature_flag_in)
    except FlagStatusReadOnly:
        raise _status_read_only() from None
    except FlagValuesRefused as refused:
        raise _values_refused(refused) from None
    except ArchivedFlagError:
        raise _archived_refusal() from None
    except IntegrityError as e:
        # A concurrent write took the key after the check above.
        _raise_if_key_conflict(db, e, feature_flag_in.key, flag_id)
        raise
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    # Built outside the ``except ValueError`` above (see create).
    updated_flag = FeatureFlagRead.model_validate(updated_row)

    # Compliance audit logging (non-fatal)
    try:
        new_flag_snapshot = {
            "key": updated_flag.key,
            "name": updated_flag.name,
            "status": updated_flag.status,
            "rollout_percentage": updated_flag.rollout_percentage,
            "value_type": updated_flag.value_type,
        }
        audit = AuditLogService(db)
        audit.log(
            action=AuditAction.UPDATE,
            resource_type="feature_flag",
            outcome=AuditOutcome.SUCCESS,
            resource_id=str(flag_id),
            actor_id=str(current_user.id) if current_user else None,
            old_value=old_flag_snapshot,
            new_value=new_flag_snapshot,
        )
        # log() only flushes, and the update above has already committed, so
        # without this the record is rolled back when the session closes.
        db.commit()
    except Exception as audit_error:
        db.rollback()
        logger.warning(
            f"Compliance audit logging failed for feature_flag update: {audit_error}"
        )

    old_value, new_value = audit_changes(
        before_audit, audit_snapshot(EntityType.FEATURE_FLAG, updated_row)
    )
    AuditService.record_after_commit(
        db,
        actor=current_user,
        action=ActionType.FEATURE_FLAG_UPDATE,
        entity_type=EntityType.FEATURE_FLAG,
        entity_id=flag_id,
        entity_name=updated_flag.name,
        before=old_value,
        after=new_value,
    )

    return updated_flag


@router.delete(
    "/{flag_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete feature flag",
    response_description="No content, feature flag successfully deleted",
    responses={
        status.HTTP_204_NO_CONTENT: {
            "description": "Feature flag successfully deleted",
        },
        status.HTTP_403_FORBIDDEN: {
            "description": "Not enough permissions to delete this feature flag",
            "content": {
                "application/json": {
                    "example": {
                        "error": {
                            "status_code": 403,
                            "message": "Not enough permissions",
                        }
                    }
                }
            },
        },
        status.HTTP_404_NOT_FOUND: {
            "description": "Feature flag not found",
            "content": {
                "application/json": {
                    "example": {
                        "error": {
                            "status_code": 404,
                            "message": "Feature flag not found",
                        }
                    }
                }
            },
        },
    },
)
async def delete_feature_flag(
    flag_id: UUID = Path(..., description="The ID of the feature flag to delete"),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
    _skip_cache: None = Depends(_skip_cache_ignored),
) -> None:
    """
    Delete a feature flag.

    This endpoint deletes an existing feature flag. Access is by role:
    ADMIN and DEVELOPER may delete any flag; ANALYST and VIEWER may not.

    Deleting a feature flag has the following effects:
    - The feature flag and its related data are permanently removed, except
      its events: those are kept, with `feature_flag_id` set to null
    - An audit record of the deletion is written (if that fails, the delete still stands)

    **Note**: This operation cannot be undone. For active feature flags, consider
    changing the status to 'archived' instead.

    Returns:
        None: No content, feature flag was successfully deleted

    Raises:
        HTTPException 403: If the user doesn't have permission to delete this feature flag
        HTTPException 404: If the feature flag doesn't exist
    """
    # Get feature flag
    flag = db.query(FeatureFlag).filter(FeatureFlag.id == flag_id).first()
    if not flag:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Feature flag not found"
        )

    # Check if the user is either the owner or a superuser
    if not can_act_on_feature_flag(current_user, flag.owner_id, Action.DELETE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You must be the owner or a superuser to delete this feature flag",
        )

    # Capture flag info before deletion for audit trail
    deleted_flag_snapshot = {
        "key": flag.key,
        "name": flag.name,
        "status": str(flag.status),
    }
    deleted_flag_id = str(flag.id)
    deleted_audit = audit_identity(audit_snapshot(EntityType.FEATURE_FLAG, flag))
    deleted_flag_name = flag.name

    # Create feature flag service
    feature_flag_service = FeatureFlagService(db)

    # Delete feature flag
    feature_flag_service.delete_feature_flag(flag)

    # Compliance audit logging (non-fatal)
    try:
        audit = AuditLogService(db)
        audit.log(
            action=AuditAction.DELETE,
            resource_type="feature_flag",
            outcome=AuditOutcome.SUCCESS,
            resource_id=deleted_flag_id,
            actor_id=str(current_user.id) if current_user else None,
            old_value=deleted_flag_snapshot,
        )
        # log() only flushes, and the delete above has already committed, so
        # without this the record is rolled back when the session closes.
        db.commit()
    except Exception as audit_error:
        db.rollback()
        logger.warning(
            f"Compliance audit logging failed for feature_flag delete: {audit_error}"
        )

    AuditService.record_after_commit(
        db,
        actor=current_user,
        action=ActionType.FEATURE_FLAG_DELETE,
        entity_type=EntityType.FEATURE_FLAG,
        entity_id=flag_id,
        entity_name=deleted_flag_name,
        before=deleted_audit,
    )

    # Return 204 No Content
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{flag_id}/activate",
    response_model=FeatureFlagRead,
    summary="Activate feature flag",
    response_description="Returns the activated feature flag",
    responses=_ARCHIVED_400,
)
async def activate_feature_flag(
    flag_id: UUID = Path(..., description="The ID of the feature flag to activate"),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
    _skip_cache: None = Depends(_skip_cache_ignored),
) -> FeatureFlagRead:
    """
    Activate a feature flag.

    This endpoint activates a feature flag, changing its status to ACTIVE.
    Activating a feature flag makes it available for use in applications.
    An archived flag is refused with 400 and left archived: unarchive it
    first (`POST /feature-flags/{id}/unarchive`).

    Returns:
        FeatureFlagRead: The feature flag, now ACTIVE

    Raises:
        HTTPException 400: The flag is archived
        HTTPException 403: If the user doesn't have permission to activate this feature flag
    """
    # Get feature flag
    flag = db.query(FeatureFlag).filter(FeatureFlag.id == flag_id).first()
    if not flag:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Feature flag not found"
        )

    # Check access permission
    if not can_act_on_feature_flag(current_user, flag.owner_id, Action.UPDATE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not enough permissions to activate this feature flag",
        )

    try:
        new_status = transition(flag.status, FlagVerb.ON)
    except ArchivedFlagError:
        raise _archived_refusal() from None

    # Check if feature flag is already active
    if flag_status_name(flag.status) == new_status.value:
        return FeatureFlagRead.model_validate(flag)

    before_audit = audit_snapshot(EntityType.FEATURE_FLAG, flag)

    # Create feature flag service
    feature_flag_service = FeatureFlagService(db)

    # Activate feature flag
    activated_flag = feature_flag_service.activate_feature_flag(flag)

    old_value, new_value = audit_changes(
        before_audit, audit_snapshot(EntityType.FEATURE_FLAG, flag)
    )
    AuditService.record_after_commit(
        db,
        actor=current_user,
        action=ActionType.FEATURE_FLAG_ACTIVATE,
        entity_type=EntityType.FEATURE_FLAG,
        entity_id=flag_id,
        entity_name=before_audit["name"],
        before=old_value,
        after=new_value,
    )

    return activated_flag


@router.post(
    "/{flag_id}/deactivate",
    response_model=FeatureFlagRead,
    summary="Deactivate feature flag",
    response_description="Returns the deactivated feature flag",
)
async def deactivate_feature_flag(
    flag_id: UUID = Path(..., description="The ID of the feature flag to deactivate"),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
    _skip_cache: None = Depends(_skip_cache_ignored),
) -> FeatureFlagRead:
    """
    Deactivate a feature flag.

    This endpoint deactivates a feature flag, changing its status to INACTIVE.
    Deactivating a feature flag makes it unavailable for use in applications.
    An archived flag is already off: it answers 200 and stays archived.

    Returns:
        FeatureFlagRead: The feature flag, now INACTIVE (or still ARCHIVED)

    Raises:
        HTTPException 403: If the user doesn't have permission to deactivate this feature flag
    """
    # Get feature flag
    flag = db.query(FeatureFlag).filter(FeatureFlag.id == flag_id).first()
    if not flag:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Feature flag not found"
        )

    # Check access permission
    if not can_act_on_feature_flag(current_user, flag.owner_id, Action.UPDATE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not enough permissions to deactivate this feature flag",
        )

    # Already inactive, or archived (which stays archived): nothing to change.
    if flag_status_name(flag.status) == transition(flag.status, FlagVerb.OFF).value:
        return FeatureFlagRead.model_validate(flag)

    before_audit = audit_snapshot(EntityType.FEATURE_FLAG, flag)

    # Create feature flag service
    feature_flag_service = FeatureFlagService(db)

    # Deactivate feature flag
    deactivated_flag = feature_flag_service.deactivate_feature_flag(flag)

    old_value, new_value = audit_changes(
        before_audit, audit_snapshot(EntityType.FEATURE_FLAG, flag)
    )
    AuditService.record_after_commit(
        db,
        actor=current_user,
        action=ActionType.FEATURE_FLAG_DEACTIVATE,
        entity_type=EntityType.FEATURE_FLAG,
        entity_id=flag_id,
        entity_name=before_audit["name"],
        before=old_value,
        after=new_value,
    )

    return deactivated_flag


@router.post(
    "/{flag_id}/unarchive",
    response_model=FeatureFlagRead,
    summary="Unarchive feature flag (beta)",
    response_description="Returns the feature flag, now INACTIVE",
    openapi_extra={"x-stability": "beta"},
)
async def unarchive_feature_flag(
    flag_id: UUID = Path(..., description="The ID of the feature flag to unarchive"),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> FeatureFlagRead:
    """
    Unarchive a feature flag (beta).

    An archived flag is retired: it is never served, and every request that
    would turn it on answers 400. This is the way back. The flag becomes
    INACTIVE, never ACTIVE, so turning it on is a separate, deliberate step.

    A flag that is not archived is returned unchanged.

    **Permissions**: ADMIN and DEVELOPER may unarchive any flag; ANALYST and
    VIEWER may not.

    Returns:
        FeatureFlagRead: The feature flag

    Raises:
        HTTPException 403: If the user doesn't have permission to change this feature flag
        HTTPException 404: If the feature flag doesn't exist
    """
    flag = db.query(FeatureFlag).filter(FeatureFlag.id == flag_id).first()
    if not flag:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Feature flag not found"
        )

    if not can_act_on_feature_flag(current_user, flag.owner_id, Action.UPDATE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not enough permissions to unarchive this feature flag",
        )

    old_status = flag_status_name(flag.status)
    new_status = transition(old_status, FlagVerb.UNARCHIVE).value
    if new_status == old_status:
        return FeatureFlagRead.model_validate(flag)

    flag.status = new_status
    db.commit()
    db.refresh(flag)

    # The change is committed above. log_action does not raise: if the audit
    # write fails it rolls its own insert back and logs an ERROR, and the
    # route still answers 200 with the change in place.
    await AuditService.log_action(
        db=db,
        user_id=current_user.id,
        user_email=current_user.email,
        username=current_user.username,
        action_type=ActionType.FEATURE_FLAG_UPDATE,
        entity_type=EntityType.FEATURE_FLAG,
        entity_id=flag.id,
        entity_name=flag.name,
        old_value=old_status,
        new_value=new_status,
        reason="unarchive",
    )

    return FeatureFlagRead.model_validate(flag)


CONTEXT_QUERY_DESCRIPTION = (
    "Optional targeting context as a URL-encoded JSON object, e.g. "
    '`{"country":"US","os_version":"17.4.0","employee":true}`. Nested objects are '
    "flattened to dotted keys (`app.version`) and top-level keys also answer "
    "`user.<key>`, `device.<key>` and `app.<key>` rule attributes."
)


def _parse_context_param(context: Optional[str]) -> Optional[Dict[str, Any]]:
    """Decode the ``context`` query parameter; 422 when it is not a JSON object."""
    if context is None or context == "":
        return None
    try:
        parsed = json.loads(context)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Query parameter 'context' must be a URL-encoded JSON object",
        )
    except RecursionError:
        # Nested too deeply for the parser itself: far past the depth limit.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=CONTEXT_TOO_DEEP,
        )
    if not isinstance(parsed, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Query parameter 'context' must be a JSON object",
        )
    return parsed


class FlagEvaluationRequest(BaseModel):
    """Body for ``POST /feature-flags/evaluate/{flag_key}``."""

    user_id: str = Field(
        ..., min_length=1, description="ID of the user to evaluate the flag for"
    )
    context: Optional[Dict[str, Any]] = Field(
        None, description="Targeting context (user/device/app attributes)"
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "user_id": "device-42",
                "context": {
                    "os": "iOS",
                    "os_version": "17.4.0",
                    "region": "US",
                    "tier": "premium",
                },
            }
        }
    )


def _evaluate_flag_by_key(
    db: Session, flag_key: str, user_id: str, context: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """Shared body of the GET and POST evaluate endpoints.

    A flag that exists but is not ACTIVE (disabled with the kill switch,
    archived, ...) evaluates to ``enabled: false`` with ``reason: "inactive"``
    rather than 404: the client should treat it as "off", not as an error.
    Only an unknown key is a 404.

    ``value`` is what the flag serves this user (#1497): its ``on_value`` when
    ``enabled``, its ``off_value`` otherwise, so every way a flag is off --
    inactive, archived, no match, outside the percentage, ``reason: "error"``
    -- serves the off value; a boolean flag's ``value`` is ``enabled``. A
    stored value outside the value rules is answered with no ``value`` key
    (``flag_values.served_value``). ``config`` is always ``None``: the value
    travels in ``value``, never in ``config``.
    """
    db_flag = db.query(FeatureFlag).filter(FeatureFlag.key == flag_key).first()
    if not db_flag:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Feature flag with key '{flag_key}' not found",
        )

    feature_flag_service = FeatureFlagService(db)
    evaluation = feature_flag_service.evaluate_flag_detailed(db_flag, user_id, context)
    record_flag_evaluation(flag_key, "enabled" if evaluation["enabled"] else "disabled")

    answer: Dict[str, Any] = {"key": flag_key, "enabled": evaluation["enabled"]}
    servable, value = served_value(db_flag, evaluation["enabled"])
    if servable:
        answer["value"] = value
    answer["value_type"] = flag_value_type(db_flag)
    answer["config"] = None
    answer["reason"] = evaluation["reason"]
    return answer


@router.get(
    "/evaluate/{flag_key}",
    response_model=Dict[str, Any],
    summary="Evaluate feature flag for a user",
    response_description="Returns the feature flag evaluation result",
)
async def evaluate_feature_flag(
    flag_key: Annotated[
        str,
        Path(description="The key of the feature flag to evaluate"),
        storable_text_param("flag_key"),
    ],
    user_id: str = Query(..., description="ID of the user to evaluate the flag for"),
    context: Optional[str] = Query(None, description=CONTEXT_QUERY_DESCRIPTION),
    db: Session = Depends(deps.get_db),
    api_key_info: Dict[str, Any] = Depends(deps.get_api_key),
) -> Dict[str, Any]:
    """
    Evaluate a feature flag for a specific user.

    This endpoint evaluates whether a feature flag is enabled for a specific user
    based on the flag's status, targeting rules, and rollout percentage. Pass the
    user's attributes as ``context=<url-encoded JSON object>`` so targeting rules
    (dashboard or native shape) can be matched.

    This endpoint is intended to be called by client applications to determine
    if a feature should be enabled for a specific user.

    **Authentication**: Requires a valid API key in the X-API-Key header.

    Returns:
        ``{"key", "enabled", "value", "value_type", "config", "reason"}``
        where ``reason`` is one of ``targeting_rule`` (a rule matched and its
        rollout % decided), ``rollout`` (the global rollout % decided),
        ``inactive`` (the flag exists but is not ACTIVE: ``enabled`` is false)
        or ``error``. ``value`` is the flag's ``on_value`` when ``enabled`` is
        true and its ``off_value`` otherwise (for a boolean flag, ``value``
        equals ``enabled``); ``value_type`` is the flag's type. ``config`` is
        always null.

    Raises:
        HTTPException 401: If the API key is invalid
        HTTPException 404: If no feature flag has this key. A flag that exists
            but is not ACTIVE is not a 404: it answers 200 with ``enabled: false``
            and ``reason: "inactive"``.
        HTTPException 422: If ``context`` is not valid JSON object
    """
    # The limits of backend.app.core.context_limits, outside the evaluation:
    # its answer is the 422, and nothing about the request is recorded.
    check_context_text(context)
    parsed_context = _parse_context_param(context)
    check_context(parsed_context)
    return _evaluate_flag_by_key(db, flag_key, user_id, parsed_context)


@router.post(
    "/evaluate/{flag_key}",
    response_model=Dict[str, Any],
    summary="Evaluate feature flag for a user (with context body)",
    response_description="Returns the feature flag evaluation result",
)
async def evaluate_feature_flag_post(
    flag_key: Annotated[
        str,
        Path(description="The key of the feature flag to evaluate"),
        storable_text_param("flag_key"),
    ],
    request: FlagEvaluationRequest = Body(
        ..., description="User id and targeting context"
    ),
    db: Session = Depends(deps.get_db),
    api_key_info: Dict[str, Any] = Depends(deps.get_api_key),
) -> Dict[str, Any]:
    """
    Evaluate a feature flag for a specific user, sending the targeting context
    in the request body instead of the query string.

    Body: ``{"user_id": "...", "context": {...}}``. Returns the same
    ``{"key", "enabled", "value", "value_type", "config", "reason"}`` payload
    as the GET variant: ``value`` is the flag's ``on_value`` when ``enabled``
    is true and its ``off_value`` otherwise (for a boolean flag, ``value``
    equals ``enabled``); ``value_type`` is the flag's type. ``config`` is
    always null.

    **Authentication**: Requires a valid API key in the X-API-Key header.

    Raises:
        HTTPException 401: If the API key is invalid
        HTTPException 404: If no feature flag has this key. A flag that exists
            but is not ACTIVE is not a 404: it answers 200 with ``enabled: false``
            and ``reason: "inactive"``.
        HTTPException 422: If the body is invalid
    """
    # The limits of backend.app.core.context_limits, outside the evaluation.
    check_context(request.context)
    return _evaluate_flag_by_key(db, flag_key, request.user_id, request.context)


@router.get(
    "/user/{user_id}",
    response_model=Dict[str, bool],
    summary="Get all feature flags for a user",
    response_description="Returns all feature flags evaluated for a user",
)
async def get_user_flags(
    user_id: Annotated[
        str,
        Path(description="ID of the user to get flags for"),
        storable_text_param("user_id"),
    ],
    context: Optional[str] = Query(None, description=CONTEXT_QUERY_DESCRIPTION),
    db: Session = Depends(deps.get_db),
    api_key_info: Dict[str, Any] = Depends(deps.get_api_key),
) -> Dict[str, bool]:
    """
    Get all active feature flags for a specific user.

    This endpoint evaluates all active feature flags for a specific user
    and returns a dictionary mapping flag keys to boolean values indicating
    whether each flag is enabled for the user. Pass the user's attributes as
    ``context=<url-encoded JSON object>`` so targeting rules can be matched.

    This endpoint is intended to be called by client applications to initialize
    feature flags for a user session.

    **Authentication**: Requires a valid API key in the X-API-Key header.

    Returns:
        Dict[str, bool]: Dictionary mapping flag keys to boolean values

    Raises:
        HTTPException 401: If the API key is invalid
        HTTPException 422: If ``context`` is not a valid JSON object
    """
    # The limits of backend.app.core.context_limits, outside the evaluation.
    check_context_text(context)
    parsed_context = _parse_context_param(context)
    check_context(parsed_context)

    # Create feature flag service
    feature_flag_service = FeatureFlagService(db)

    # Get all flags for user
    flags = feature_flag_service.get_user_flags(user_id, parsed_context)

    return flags


@router.post(
    "/{flag_id}/toggle",
    response_model=ToggleResponse,
    summary="Toggle feature flag",
    response_description="Returns the toggled feature flag with audit log ID",
    responses=_ARCHIVED_400,
)
async def toggle_feature_flag(
    flag_id: UUID = Path(..., description="The ID of the feature flag to toggle"),
    toggle_request: ToggleRequest = Body(
        ..., description="Toggle request with optional reason"
    ),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
    _skip_cache: None = Depends(_skip_cache_ignored),
) -> ToggleResponse:
    """
    Toggle a feature flag between enabled and disabled states.

    This endpoint provides a unified way to toggle feature flags, automatically
    determining whether to activate or deactivate based on the current status.

    The toggle operation includes:
    - Status change (ACTIVE ↔ INACTIVE). An archived flag would be turned on,
      so it answers 400 and stays archived: unarchive it first
    - Complete audit logging with user information and optional reason
    - Response with new status and audit log ID

    **Authentication**: Requires valid user authentication.
    **Permissions**: ADMIN and DEVELOPER may toggle any flag; ANALYST and
    VIEWER may not.

    Returns:
        ToggleResponse: Updated feature flag details with audit log ID

    Raises:
        HTTPException 400: The flag is archived
        HTTPException 403: If user doesn't have permission to toggle this feature flag
        HTTPException 404: If feature flag doesn't exist
        HTTPException 500: If toggle operation fails
    """
    # Get feature flag
    flag = db.query(FeatureFlag).filter(FeatureFlag.id == flag_id).first()
    if not flag:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Feature flag not found"
        )

    # Check access permission
    if not can_act_on_feature_flag(current_user, flag.owner_id, Action.UPDATE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not enough permissions to toggle this feature flag",
        )

    # Determine new status based on current status. The column loads as
    # FeatureFlagStatus; a writer may have left a plain string. Normalise to
    # the string so the comparison and the audit row agree. Refused before
    # the try below, whose handler would turn the refusal into a 500.
    old_status = flag_status_name(flag.status)
    if old_status == FeatureFlagStatus.ACTIVE.value:
        verb, action_type = FlagVerb.OFF, ActionType.TOGGLE_DISABLE
    else:
        verb, action_type = FlagVerb.ON, ActionType.TOGGLE_ENABLE
    try:
        new_status = transition(old_status, verb).value
    except ArchivedFlagError:
        raise _archived_refusal() from None

    try:
        # Update feature flag status
        flag.status = new_status
        db.commit()
        db.refresh(flag)

        # Log the toggle operation (don't fail if audit logging fails)
        audit_log_id = None
        try:
            audit_log_id = await AuditService.log_action(
                db=db,
                user_id=current_user.id,
                user_email=current_user.email,
                username=current_user.username,
                action_type=action_type,
                entity_type=EntityType.FEATURE_FLAG,
                entity_id=flag.id,
                entity_name=flag.name,
                old_value=old_status,
                new_value=new_status,
                reason=toggle_request.reason,
            )
        except Exception as audit_error:
            # Log audit error but don't fail the toggle operation
            logger.warning(
                "Audit logging failed for toggle operation (%s)",
                type(audit_error).__name__,
            )

        return ToggleResponse(
            id=flag.id,
            name=flag.name,
            key=flag.key,
            status=new_status,
            updated_at=flag.updated_at,
            audit_log_id=audit_log_id,
        )

    except Exception as e:
        raise unexpected_failure(
            e,
            "Feature flag toggle",
            "Could not toggle the feature flag",
            db=db,
            logger=logger,
        )


@router.post(
    "/{flag_id}/enable",
    response_model=ToggleResponse,
    summary="Enable feature flag",
    response_description="Returns the enabled feature flag with audit log ID",
    responses=_ARCHIVED_400,
)
async def enable_feature_flag(
    flag_id: UUID = Path(..., description="The ID of the feature flag to enable"),
    toggle_request: ToggleRequest = Body(
        ..., description="Enable request with optional reason"
    ),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
    _skip_cache: None = Depends(_skip_cache_ignored),
) -> ToggleResponse:
    """
    Enable a feature flag (set to ACTIVE status).

    This endpoint explicitly enables a feature flag. An archived flag answers
    400 and stays archived: unarchive it first.
    Includes complete audit logging with user information and optional reason.

    **Authentication**: Requires valid user authentication.
    **Permissions**: User must own the feature flag or be a superuser.

    Returns:
        ToggleResponse: Updated feature flag details with audit log ID

    Raises:
        HTTPException 400: The flag is archived
        HTTPException 403: If user doesn't have permission to enable this feature flag
        HTTPException 404: If feature flag doesn't exist
        HTTPException 500: If enable operation fails
    """
    # Get feature flag
    flag = db.query(FeatureFlag).filter(FeatureFlag.id == flag_id).first()
    if not flag:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Feature flag not found"
        )

    # Check access permission
    if not can_act_on_feature_flag(current_user, flag.owner_id, Action.UPDATE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not enough permissions to enable this feature flag",
        )

    # Refused before the try below, whose handler would make it a 500.
    old_status = flag.status
    try:
        new_status = transition(old_status, FlagVerb.ON).value
    except ArchivedFlagError:
        raise _archived_refusal() from None

    try:
        # Update feature flag status
        flag.status = new_status
        db.commit()
        db.refresh(flag)

        # Log the enable operation
        audit_log_id = await AuditService.log_action(
            db=db,
            user_id=current_user.id,
            user_email=current_user.email,
            username=current_user.username,
            action_type=ActionType.TOGGLE_ENABLE,
            entity_type=EntityType.FEATURE_FLAG,
            entity_id=flag.id,
            entity_name=flag.name,
            old_value=old_status,
            new_value=new_status,
            reason=toggle_request.reason,
        )

        return ToggleResponse(
            id=flag.id,
            name=flag.name,
            key=flag.key,
            status=new_status,
            updated_at=flag.updated_at,
            audit_log_id=audit_log_id,
        )

    except Exception as e:
        raise unexpected_failure(
            e,
            "Feature flag enable",
            "Could not enable the feature flag",
            db=db,
            logger=logger,
        )


@router.post(
    "/{flag_id}/disable",
    response_model=ToggleResponse,
    summary="Disable feature flag",
    response_description="Returns the disabled feature flag with audit log ID",
)
async def disable_feature_flag(
    flag_id: UUID = Path(..., description="The ID of the feature flag to disable"),
    toggle_request: ToggleRequest = Body(
        ..., description="Disable request with optional reason"
    ),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
    _skip_cache: None = Depends(_skip_cache_ignored),
) -> ToggleResponse:
    """
    Disable a feature flag (set to INACTIVE status).

    This endpoint explicitly disables a feature flag. An archived flag is
    already off: it answers 200 and stays archived.
    Includes complete audit logging with user information and optional reason.

    **Authentication**: Requires valid user authentication.
    **Permissions**: User must own the feature flag or be a superuser.

    Returns:
        ToggleResponse: Updated feature flag details with audit log ID

    Raises:
        HTTPException 403: If user doesn't have permission to disable this feature flag
        HTTPException 404: If feature flag doesn't exist
        HTTPException 500: If disable operation fails
    """
    # Get feature flag
    flag = db.query(FeatureFlag).filter(FeatureFlag.id == flag_id).first()
    if not flag:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Feature flag not found"
        )

    # Check access permission
    if not can_act_on_feature_flag(current_user, flag.owner_id, Action.UPDATE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not enough permissions to disable this feature flag",
        )

    try:
        old_status = flag.status
        new_status = transition(old_status, FlagVerb.OFF).value

        # An archived flag stays archived, and is not written.
        if flag_status_name(old_status) != new_status:
            flag.status = new_status
            db.commit()
            db.refresh(flag)

        # Log the disable operation
        audit_log_id = await AuditService.log_action(
            db=db,
            user_id=current_user.id,
            user_email=current_user.email,
            username=current_user.username,
            action_type=ActionType.TOGGLE_DISABLE,
            entity_type=EntityType.FEATURE_FLAG,
            entity_id=flag.id,
            entity_name=flag.name,
            old_value=old_status,
            new_value=new_status,
            reason=toggle_request.reason,
        )

        return ToggleResponse(
            id=flag.id,
            name=flag.name,
            key=flag.key,
            status=new_status,
            updated_at=flag.updated_at,
            audit_log_id=audit_log_id,
        )

    except Exception as e:
        raise unexpected_failure(
            e,
            "Feature flag disable",
            "Could not disable the feature flag",
            db=db,
            logger=logger,
        )
