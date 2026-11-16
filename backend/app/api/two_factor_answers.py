"""What the two-step sign-in routes answer when they refuse (#1812).

One mapping from ``two_factor_service``'s refusals to HTTP, shared by the
code step of a sign-in (``POST /api/v1/auth/mfa/verify``) and the signed-in
routes under ``/api/v1/users/me/2fa``, so that the same refusal reads the same
everywhere. The texts are fixed: nothing a request sent is repeated.

* a wrong, reused or malformed code or recovery code: ``401`` on
  ``/auth/mfa/verify`` (no session exists yet) and ``403`` on a signed-in
  route (a ``401`` there would read as a signed-out session), both with
  ``WRONG_CODE_TEXT``;
* the address locked by too many failures, passwords and codes together:
  ``423`` with the sign-in route's own text and ``Retry-After``;
* ``CODE_STEP_CAP`` wrong codes in a row: ``423`` with
  ``CODE_STEP_LOCKED_TEXT`` and no ``Retry-After``, since waiting does not
  end it (a superuser's reset or the operator's command does);
* a stored secret that the current ``SECRET_KEY`` cannot open: ``503`` with
  ``UNAVAILABLE_TEXT``, never a fall back to the password alone.
"""

from __future__ import annotations

from typing import Dict, Optional

from fastapi import HTTPException, status

from backend.app.services.local_auth_service import AccountLockedError
from backend.app.services.two_factor_service import (
    CODE_STEP_LOCKED_TEXT,
    UNAVAILABLE_TEXT,
    WRONG_CODE_TEXT,
    AlreadyEnabledError,
    CodeStepLockedError,
    NotEnabledError,
    TwoFactorError,
    TwoFactorInputError,
    TwoFactorUnavailableError,
    WrongCodeError,
)

#: ``/auth/mfa/verify`` with an mfa_token that is expired, forged, of another
#: purpose, or names an account that can no longer finish signing in.
SIGN_IN_EXPIRED_DETAIL = "Sign-in expired. Start again."
#: ``POST /users/me/2fa/enroll`` once two-step sign-in is on.
ALREADY_ENABLED_DETAIL = (
    "Two-step sign-in is already on for this account. "
    "Turn it off first to set it up again."
)
#: ``POST /users/me/2fa/confirm`` with no secret waiting for its first code,
#: which is also what an account that is already on answers there.
NO_PENDING_SECRET_DETAIL = (
    "No new secret is waiting for its first code, or two-step sign-in is "
    "already on. While it is off, ask for a secret with "
    "POST /api/v1/users/me/2fa/enroll."
)
#: ``POST /auth/mfa/enroll/confirm`` before ``/auth/mfa/enroll`` gave a secret.
NO_PENDING_SIGN_IN_SECRET_DETAIL = (
    "No new secret is waiting for its first code. "
    "Ask for one with POST /api/v1/auth/mfa/enroll."
)
#: ``disable`` and ``recovery-codes`` while two-step sign-in is off.
NOT_ENABLED_DETAIL = "Two-step sign-in is not on for this account."
#: ``disable`` for an account whose role must use two-step sign-in
#: (``LOCAL_AUTH_2FA_REQUIRED_ROLES``).
TWO_FACTOR_REQUIRED_FOR_ROLE = (
    "Two-step sign-in is required for your role, so it cannot be turned off."
)


def locked_answer(exc: AccountLockedError) -> HTTPException:
    """``423`` for a locked address: the sign-in route's own text."""
    return HTTPException(
        status_code=status.HTTP_423_LOCKED,
        detail=(
            "Too many failed login attempts; account temporarily locked. "
            f"Retry in {exc.retry_after_seconds} seconds."
        ),
        headers={"Retry-After": str(exc.retry_after_seconds)},
    )


def refusal_answer(
    exc: Exception,
    *,
    signed_in: bool,
    not_enabled_status: int = status.HTTP_409_CONFLICT,
    not_enabled_detail: str = NOT_ENABLED_DETAIL,
) -> HTTPException:
    """The HTTP answer for one of the service's refusals.

    ``signed_in`` picks the status of a wrong code: 403 on a route that
    already has a session, 401 on the code step of a sign-in. ``NotEnabledError``
    (no row in the state the step needs) answers ``not_enabled_status`` with
    ``not_enabled_detail``. Anything else is not a refusal and is raised again.
    """
    if isinstance(exc, AccountLockedError):
        return locked_answer(exc)
    if isinstance(exc, CodeStepLockedError):
        return HTTPException(
            status_code=status.HTTP_423_LOCKED, detail=CODE_STEP_LOCKED_TEXT
        )
    if isinstance(exc, TwoFactorUnavailableError):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=UNAVAILABLE_TEXT
        )
    if isinstance(exc, WrongCodeError):
        headers: Optional[Dict[str, str]] = None
        if not signed_in:
            headers = {"WWW-Authenticate": "Bearer"}
        return HTTPException(
            status_code=(
                status.HTTP_403_FORBIDDEN if signed_in else status.HTTP_401_UNAUTHORIZED
            ),
            detail=WRONG_CODE_TEXT,
            headers=headers,
        )
    if isinstance(exc, NotEnabledError):
        headers = None
        if not_enabled_status == status.HTTP_401_UNAUTHORIZED:
            headers = {"WWW-Authenticate": "Bearer"}
        return HTTPException(
            status_code=not_enabled_status, detail=not_enabled_detail, headers=headers
        )
    if isinstance(exc, AlreadyEnabledError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=ALREADY_ENABLED_DETAIL
        )
    if isinstance(exc, TwoFactorInputError):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Send exactly one of code and recovery_code, of at most 64 "
            "characters.",
        )
    raise exc


#: Every refusal ``refusal_answer`` maps, for ``except`` clauses.
REFUSALS = (AccountLockedError, TwoFactorError)
