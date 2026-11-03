"""
The ``sdk:ruleset`` scope, enforced.

:func:`require_sdk_ruleset_key` is the dependency for the server-side SDK
routes: the flag ruleset for local evaluation (``GET /api/v1/sdk/ruleset``),
the local evaluation counts (``POST /api/v1/tracking/evaluations``) and batch
assignment (``POST /api/v1/tracking/assign/batch``).  It is layered on
``deps.get_api_key`` (which validates the key, its owner and records its use)
and then decides on the **presented** key itself: it re-hashes the
``X-API-Key`` header and reads that one ``APIKey`` row, so another key of the
same user carrying the scope grants nothing.  Scope membership is
:func:`backend.app.core.api_key_scopes.has_scope` -- exact and case-sensitive
after split, strip and dropping empties.

The scope takes effect only while the key's owner can change feature flags
(:func:`may_use_server_key`, which is ``check_permission(owner, FEATURE_FLAG,
UPDATE)``: ADMIN, DEVELOPER, a superuser, or in the full profile a custom role
or direct grant of it), checked on every request, so it follows role changes
and a grant revoked or expired.  The owner is the presented key's own
``user_id``, read from the database, not whatever ``get_api_key`` returned.

Refusals: no or unknown key -> 401 (from ``get_api_key``); a valid key without
the scope -> 403 (:data:`MISSING_SCOPE_DETAIL`); a scoped key whose owner
cannot change feature flags -> 403 (:data:`OWNER_ROLE_DETAIL`); these two
texts are the same on every route.  Batch assignment
(:func:`require_batch_assign_key`) also checks that the owner can change
experiments (``check_permission(owner, EXPERIMENT, UPDATE)``) and refuses with
its own 403 (:data:`OWNER_EXPERIMENT_DETAIL`) before any assignment is
written: three 403 texts in all.

:func:`presented_key_is_server_key` applies the scope and owner rules without
refusing anything: the error report routes (``POST /api/v1/tracking/errors``
and ``/errors/batch``) accept every valid key and use it only to decide
whether a report counts toward automatic rollback.
"""

from typing import Optional

from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session

from backend.app.api import deps
from backend.app.core.api_key_scopes import SDK_RULESET_SCOPE, has_scope
from backend.app.core.permissions import Action, ResourceType, check_permission
from backend.app.core.security import hash_api_key
from backend.app.models.api_key import APIKey
from backend.app.models.user import User

#: The 403 detail for a valid key that lacks the scope.
MISSING_SCOPE_DETAIL = (
    f"This API key does not have the '{SDK_RULESET_SCOPE}' scope. Create a key "
    f"with the '{SDK_RULESET_SCOPE}' scope for server-side SDK use."
)

#: The 403 detail for a scoped key whose owner cannot change feature flags.
OWNER_ROLE_DETAIL = (
    "This key's owner can no longer change feature flags, so the key is refused "
    "for server-side SDK use."
)

#: The 403 detail of batch assignment for a scoped key whose owner can change
#: feature flags but not experiments. Nobody is assigned.
OWNER_EXPERIMENT_DETAIL = (
    "This key's owner cannot change experiments, so the key cannot assign users "
    "in a batch, and nobody was assigned. Use a server-side key whose owner can "
    "change experiments."
)


def may_use_server_key(user: User) -> bool:
    """Whether *user* may own a server-side key (the ``sdk:ruleset`` scope).

    The one rule for creating such a key, for ``GET /sdk/ruleset``, ``POST
    /tracking/evaluations``, batch assignment and whether an error report
    counts toward rollback: the user can change feature flags. In the full
    profile a custom role or direct grant of ``feature_flag (update)`` gives
    that too; for the four built-in roles it is ADMIN and DEVELOPER (and a
    superuser), as before custom roles took effect.
    """
    return check_permission(user, ResourceType.FEATURE_FLAG, Action.UPDATE)


def scope_refusal(db: Session, api_key: APIKey) -> Optional[str]:
    """Why ``api_key`` may not act as a server-side key, or ``None`` if it may.

    The scope first (:data:`MISSING_SCOPE_DETAIL`), then its owner, read from
    the database by the key's own ``user_id`` (:data:`OWNER_ROLE_DETAIL`:
    missing, inactive, or unable to change feature flags).
    """
    if not has_scope(api_key.scopes, SDK_RULESET_SCOPE):
        return MISSING_SCOPE_DETAIL
    owner = db.query(User).filter(User.id == api_key.user_id).first()
    if owner is None or not owner.is_active or not may_use_server_key(owner):
        return OWNER_ROLE_DETAIL
    return None


def batch_assign_refusal(db: Session, api_key: APIKey) -> Optional[str]:
    """Why ``api_key``'s owner may not assign users in a batch, or ``None``.

    Asked after :func:`scope_refusal` has let the key through. The owner is
    read from the database by the key's own ``user_id``, as there, and must
    be able to change experiments (:data:`OWNER_EXPERIMENT_DETAIL`).
    """
    owner = db.query(User).filter(User.id == api_key.user_id).first()
    if owner is None or not check_permission(
        owner, ResourceType.EXPERIMENT, Action.UPDATE
    ):
        return OWNER_EXPERIMENT_DETAIL
    return None


def presented_key_is_server_key(db: Session, api_key_header: Optional[str]) -> bool:
    """Whether the presented ``X-API-Key`` is a server-side key right now.

    True only when the header's key exists, is valid, carries ``sdk:ruleset``
    and its owner can change feature flags (:func:`scope_refusal` returns
    ``None``). It re-hashes the header and reads that one ``APIKey`` row, so
    another key of the same owner grants nothing. It refuses nothing: a
    missing header, an unknown or invalid key and any refusal all answer
    False, and the caller decides what that means.
    """
    if not api_key_header:
        return False
    api_key = (
        db.query(APIKey).filter(APIKey.key == hash_api_key(api_key_header)).first()
    )
    if api_key is None or not api_key.is_valid:
        return False
    return scope_refusal(db, api_key) is None


def require_sdk_ruleset_key(
    db: Session = Depends(deps.get_db),
    api_key_header: str = Depends(deps.API_KEY_HEADER),
    _owner: User = Depends(deps.get_api_key),
) -> APIKey:
    """Return the presented ``APIKey`` row if it carries ``sdk:ruleset``."""
    if not api_key_header:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key missing",
            headers={"WWW-Authenticate": "APIKey"},
        )
    api_key = (
        db.query(APIKey).filter(APIKey.key == hash_api_key(api_key_header)).first()
    )
    if api_key is None or not api_key.is_valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API Key",
            headers={"WWW-Authenticate": "APIKey"},
        )
    refusal = scope_refusal(db, api_key)
    if refusal is not None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=refusal)
    return api_key


def require_batch_assign_key(
    db: Session = Depends(deps.get_db),
    api_key: APIKey = Depends(require_sdk_ruleset_key),
) -> APIKey:
    """The presented key, if it may assign users in a batch.

    :func:`require_sdk_ruleset_key` first (its 401s and two 403s), then the
    owner must be able to change experiments: otherwise a 403 with
    :data:`OWNER_EXPERIMENT_DETAIL`, before the route reads its experiment or
    writes any assignment.
    """
    refusal = batch_assign_refusal(db, api_key)
    if refusal is not None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=refusal)
    return api_key
