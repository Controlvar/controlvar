"""
The flag environment of the API key a request presents (#2011).

Every API key belongs to one flag environment
(``backend.app.core.flag_environments``). ``deps.get_api_key`` reads the key's
row and keeps its environment on ``request.state.flag_environment``, with no
query of its own; :func:`presented_environment` reads it back. A route whose
key dependency returns the ``APIKey`` row itself
(``sdk_scope.require_sdk_ruleset_key``) reads ``api_key.environment`` from
that row instead.

Where each rule is applied:

* A route that works in the key's environment, or stores it, calls
  :func:`presented_environment` in its handler's own body. By then every
  dependency has run. A dependency that only read ``request.state`` and was
  solved before ``get_api_key`` would find nothing there and answer
  ``production`` for a staging key, so no route uses one.
* A route that runs in production only refuses any other key with 403 and
  writes nothing: :func:`refuse_non_production`, with
  :func:`production_only_detail` as its text. The tracking routes call it
  before they hand their work to a worker thread;
  ``sdk_scope.require_production_ruleset_key`` and
  ``page_key.require_page_key`` call it on the row they hold.
* :func:`require_production_key` is the same rule as a dependency, layered on
  ``deps.get_api_key`` so that it always runs after it. The modules' split URL
  routes use it.

The environment is the stored key's, never the deployment setting
``ENVIRONMENT`` and never anything the request sends. This module imports no
settings.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status

from backend.app.api import deps
from backend.app.core.flag_environments import DEFAULT, PRODUCTION
from backend.app.models.user import User


def presented_environment(request: Request) -> str:
    """The environment of the key ``deps.get_api_key`` admitted for *request*.

    The default, ``production``, when nothing was recorded: ``get_api_key``
    records it for every key it admits, so that is only a test that replaces
    ``get_api_key``.
    """
    return getattr(request.state, "flag_environment", DEFAULT)


def production_only_detail(environment: str) -> str:
    """The 403 a key of *environment* gets on a route that runs in production only."""
    return (
        f"This API key belongs to the {environment} environment. Experiments and "
        "event tracking run in production only; use a production key."
    )


def refuse_non_production(environment: str) -> None:
    """Raise the 403 unless *environment* is production."""
    if environment != PRODUCTION:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=production_only_detail(environment),
        )


async def require_production_key(
    request: Request, owner: User = Depends(deps.get_api_key)
) -> User:
    """The presented key's owner, if the key is a production key; else 403.

    Reads ``request.state`` only, never the session.
    """
    refuse_non_production(presented_environment(request))
    return owner
