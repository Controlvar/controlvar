"""
The ``mcp:read`` scope: four read operations also accept a scoped API key.

:func:`get_current_reader` is the user dependency of exactly these route
registrations (``backend/tests/smoke/test_mcp_read_routes.py`` pins the set):

* ``GET /api/v1/feature-flags/`` (and its twin without the slash, which is not
  in the OpenAPI document);
* ``GET /api/v1/experiments/``;
* ``GET /api/v1/experiments/{experiment_id}``;
* ``GET /api/v1/experiments/{experiment_id}/results``.

Every other route is unchanged; the routes that create, change or delete flags or
experiments, and those that manage users, still take a sign-in only.

The contract, in this order:

1. **The bearer path, exactly as before.** When the dev-auth bypass is active,
   when the request carries a bearer token, or when it carries no
   ``X-API-Key``, the answer is ``get_current_active_user(get_current_user())``
   with today's answers (401, or 400 "Inactive user"). The key is not read. A
   request with a bearer token is judged on the token alone, even when the
   token is invalid and the key is valid. ``Authorization: Bearer `` with an
   empty token counts as no token. A request with neither gets the same
   ``401 {"detail": "Not authenticated"}`` with ``WWW-Authenticate: Bearer``.
2. **The key, as** ``deps.get_api_key`` **validates it.** An unknown, inactive
   or expired key, or a missing or inactive owner: ``401 "Invalid API Key"``
   with ``WWW-Authenticate: APIKey``. The use is recorded in ``last_used_at``.
3. **The scope, on the presented key only.** The header is re-hashed and that
   one ``APIKey`` row is read, as ``sdk_scope.require_sdk_ruleset_key`` does,
   so another key of the same owner grants nothing. Membership is
   :func:`backend.app.core.api_key_scopes.has_scope`: exact and
   case-sensitive. Without ``mcp:read``: 403 :data:`MISSING_SCOPE_DETAIL`.
4. **The owner is returned.** The route's own role checks then run unchanged;
   the scope itself carries no role rule.

``get_current_user`` and ``get_current_active_user`` are not changed. Tests
that override them with ``app.dependency_overrides`` must also override
:func:`get_current_reader`: FastAPI keys an override by the callable, and this
function calls those two as plain functions.
"""

from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyHeader
from sqlalchemy.orm import Session

from backend.app.api import deps
from backend.app.api.deps import get_api_key, get_current_active_user, get_current_user
from backend.app.core.api_key_scopes import MCP_READ_SCOPE, has_scope
from backend.app.core.security import hash_api_key, oauth2_scheme
from backend.app.models.api_key import APIKey
from backend.app.models.user import User

#: The key alternative on the four read operations, under its own name in the
#: OpenAPI document so that the document says which key it takes. The header
#: is the same one every other key-taking route reads.
MCP_READ_KEY_HEADER = APIKeyHeader(
    name="X-API-Key",
    scheme_name="MCPReadAPIKey",
    description=(
        f"An API key that carries the `{MCP_READ_SCOPE}` scope, in the `X-API-Key` "
        "header. The request reads as the user who created the key, under that "
        f"user's role. A valid key without `{MCP_READ_SCOPE}` is refused with 403."
    ),
    auto_error=False,
)

#: The 403 detail for a valid key that lacks the scope.
MISSING_SCOPE_DETAIL = (
    f"This API key does not have the '{MCP_READ_SCOPE}' scope. Create a key "
    f"with the '{MCP_READ_SCOPE}' scope to read flags, experiments and results "
    "with it."
)


def get_current_reader(
    db: Session = Depends(deps.get_db),
    token: Optional[str] = Depends(oauth2_scheme),
    api_key_header: Optional[str] = Depends(MCP_READ_KEY_HEADER),
) -> User:
    """The signed-in user, or the owner of a presented ``mcp:read`` key."""
    if token or not api_key_header or deps.dev_auth_bypass_active():
        return get_current_active_user(get_current_user(token=token, db=db))

    owner = get_api_key(db=db, api_key_header=api_key_header)
    api_key = (
        db.query(APIKey).filter(APIKey.key == hash_api_key(api_key_header)).first()
    )
    if api_key is None or not api_key.is_valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API Key",
            headers={"WWW-Authenticate": "APIKey"},
        )
    if not has_scope(api_key.scopes, MCP_READ_SCOPE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=MISSING_SCOPE_DETAIL
        )
    # TAMPER (Q4): a keyed read writes an audit entry naming the key's owner.
    from backend.app.models.audit_log import AuditLog

    db.add(
        AuditLog(
            user_id=owner.id,
            user_email=owner.email or "tamper@example.com",
            action_type="feature_flag_update",
            entity_type="feature_flag",
            entity_id=owner.id,
            entity_name="tamper",
        )
    )
    db.commit()
    return owner
