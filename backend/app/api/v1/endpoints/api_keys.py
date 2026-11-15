"""
User-owned API keys (core) — ``/api/v1/api-keys``.

Keys authenticate SDK traffic (``X-API-Key`` header on ``/tracking/*``,
flag evaluation, the ruleset ...) as the owning user via
``deps.get_api_key``.  A key with the ``mcp:read`` scope also reads on four
read routes (``backend/app/api/read_scope.py``); creating one needs no role.
The plaintext secret is returned exactly once by the
create endpoint; only its SHA-256 hash is persisted (``APIKey.key``).

* ``GET /``            — the caller's keys; ADMIN may pass ``?all=true``.
* ``POST /``           — create; 201 with the plaintext ``key``. A key with the
  ``sdk:ruleset`` scope is for server-side evaluation, and only a user who can
  change feature flags (ADMIN, DEVELOPER, a superuser, or in the full profile
  anyone whose custom role or direct grant gives ``feature_flag (update)``)
  may create one; anyone else gets 403 and no key is written.
* ``DELETE /{key_id}`` — owner or ADMIN; 204.

These are the only API keys: workspaces (the workspaces module) issue none.
"""

from typing import Any, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from backend.app.api import deps
from backend.app.api.off_loop_route import OffLoopRoute
from backend.app.api.sdk_scope import may_use_server_key
from backend.app.core.api_key_scopes import (
    SDK_RULESET_SCOPE,
    format_scopes,
    has_scope,
    parse_scopes,
)
from backend.app.core.permissions import (
    Action,
    ResourceType,
    check_permission,
    get_permission_error_message,
)
from backend.app.models.api_key import APIKey
from backend.app.models.audit_log import ActionType, EntityType
from backend.app.models.user import User
from backend.app.schemas.api_key import (
    KEY_PREFIX_LENGTH,
    APIKeyCreate,
    APIKeyCreated,
    APIKeyRead,
)
from backend.app.services.audit_service import (
    AuditService,
    audit_identity,
    audit_snapshot,
)

router = APIRouter(route_class=OffLoopRoute)

SDK_RULESET_CREATE_FORBIDDEN = (
    "Only users who can change feature flags can create an API key with the "
    f"{SDK_RULESET_SCOPE} scope; the ADMIN and DEVELOPER roles and superusers "
    "can. Create the key without that scope, or ask someone who can."
)


def _may_act_on_others_keys(user: User, action: Action) -> bool:
    """
    Whether *user* may perform *action* on keys they do not own.

    Own keys never go through this check; cross-user access is governed by
    the RBAC map (``ResourceType.API_KEY``), which grants it to ADMIN only.
    """
    return check_permission(user, ResourceType.API_KEY, action)


def _to_read(key: APIKey) -> APIKeyRead:
    scopes = parse_scopes(key.scopes)
    return APIKeyRead(
        id=key.id,
        name=key.name,
        description=key.description,
        scopes=scopes,
        is_active=bool(key.is_active),
        user_id=key.user_id,
        created_at=key.created_at,
        expires_at=key.expires_at,
        last_used_at=key.last_used_at,
    )


# Routes are registered without a trailing slash so ``POST /api/v1/api-keys``
# works from curl without following a 307; ``/api-keys/`` is redirected.
@router.get("", response_model=List[APIKeyRead])
def list_api_keys(
    all_users: bool = Query(
        False,
        alias="all",
        description="ADMIN only: list every user's keys instead of just your own",
    ),
    include_inactive: bool = Query(False, description="Include revoked keys"),
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> Any:
    """List API keys owned by the caller (or all keys for ADMIN with ``?all=true``)."""
    query = db.query(APIKey)
    if all_users:
        if not _may_act_on_others_keys(current_user, Action.LIST):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=get_permission_error_message(ResourceType.API_KEY, Action.LIST),
            )
    else:
        query = query.filter(APIKey.user_id == current_user.id)
    if not include_inactive:
        query = query.filter(APIKey.is_active.is_(True))
    keys = query.order_by(APIKey.created_at.desc()).all()
    return [_to_read(k) for k in keys]


@router.post("", response_model=APIKeyCreated, status_code=status.HTTP_201_CREATED)
def create_api_key(
    body: APIKeyCreate,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> Any:
    """
    Create an API key for the caller.

    The response is the only place the plaintext ``key`` is ever shown; store
    it securely.  ``prefix`` (the first characters of the key) is returned so
    the key can be recognised later.
    """
    scopes = format_scopes(body.scopes)
    # Server-side evaluation keys are for users who can change feature flags.
    # The check runs before anything is written.
    if has_scope(scopes, SDK_RULESET_SCOPE) and not may_use_server_key(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=SDK_RULESET_CREATE_FORBIDDEN,
        )
    api_key, plaintext = APIKey.create_for_user(
        db,
        user_id=current_user.id,
        name=body.name,
        description=body.description,
        scopes=scopes,
        expires_at=body.expires_at,
    )
    created = APIKeyCreated(
        id=api_key.id,
        name=api_key.name,
        key=plaintext,
        prefix=plaintext[:KEY_PREFIX_LENGTH],
        created_at=api_key.created_at,
        expires_at=api_key.expires_at,
    )
    # The key is committed; a failed audit write does not undo it. The entry
    # holds no part of the key or its hash (``key`` is not an audit field).
    AuditService.record_after_commit(
        db,
        actor=current_user,
        action=ActionType.API_KEY_CREATE,
        entity_type=EntityType.API_KEY,
        entity_id=api_key.id,
        entity_name=api_key.name,
        after=audit_identity(audit_snapshot(EntityType.API_KEY, api_key)),
    )
    return created


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_api_key(
    key_id: UUID,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> Response:
    """Permanently delete an API key (owner or ADMIN)."""
    api_key: Optional[APIKey] = db.query(APIKey).filter(APIKey.id == key_id).first()
    if api_key is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="API key not found"
        )
    if api_key.user_id != current_user.id and not _may_act_on_others_keys(
        current_user, Action.DELETE
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=get_permission_error_message(ResourceType.API_KEY, Action.DELETE),
        )
    revoked = audit_identity(audit_snapshot(EntityType.API_KEY, api_key))
    db.delete(api_key)
    db.commit()
    AuditService.record_after_commit(
        db,
        actor=current_user,
        action=ActionType.API_KEY_REVOKE,
        entity_type=EntityType.API_KEY,
        entity_id=key_id,
        entity_name=revoked["name"] or str(key_id),
        before=revoked,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
