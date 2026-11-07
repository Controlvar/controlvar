"""
A key that belongs in a web page: one with no scopes (#1545, C11).

The visual editor's browser script carries its API key in the page, where
anyone who opens the page can read it. A key with a scope belongs on a
server: ``sdk:ruleset`` downloads every flag's rules and makes error reports
count toward automatic rollback, and ``mcp:read`` reads every experiment and
its results. So the two routes the script calls answer 403 to a key whose
scope list is not empty, whatever the scopes are, a label such as ``read``
included:

* ``POST /api/v1/tracking/visual``
* ``POST /api/v1/tracking/visual/preview``

:func:`require_page_key` is their dependency, layered on ``deps.get_api_key``
(which answers 401 for a missing, unknown, expired or revoked key, or one
whose owner is inactive). It then reads the **presented** key's own row, by
the hash of the header, so another key of the same owner decides nothing.
Every other route that takes an API key is unchanged: the refusal is here,
never in ``deps.get_api_key``.
"""

from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session

from backend.app.api import deps
from backend.app.core.api_key_scopes import parse_scopes
from backend.app.core.security import hash_api_key
from backend.app.models.api_key import APIKey
from backend.app.models.user import User

#: The 403 for a key with any scope, on both routes.
SCOPED_KEY_IN_PAGE_DETAIL = (
    "This API key has scopes, so it must stay on a server: a key in a web page "
    "can be read by anyone who opens the page. Use a key with no scopes on your "
    "website."
)


def page_key_refusal(api_key: APIKey) -> "str | None":
    """Why *api_key* may not be used in a web page, or ``None``: it has a scope."""
    if parse_scopes(api_key.scopes):
        return SCOPED_KEY_IN_PAGE_DETAIL
    return None


async def require_page_key(
    db: Session = Depends(deps.get_db),
    api_key_header: str = Depends(deps.API_KEY_HEADER),
    owner: User = Depends(deps.get_api_key),
) -> User:
    """The presented key's owner, if the key has no scopes; else 403.

    Reads on the request's session; does no blocking work.
    """
    api_key = (
        db.query(APIKey).filter(APIKey.key == hash_api_key(api_key_header)).first()
    )
    if api_key is None or not api_key.is_valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API Key",
            headers={"WWW-Authenticate": "APIKey"},
        )
    refusal = page_key_refusal(api_key)
    if refusal is not None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=refusal)
    return owner
