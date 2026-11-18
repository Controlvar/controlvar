"""The short token between the password step and the code step (#1812).

When an account with two-step sign-in on gives the right password,
``POST /api/v1/auth/login`` (and ``/auth/token``) answers with this token
instead of an access token. ``POST /api/v1/auth/mfa/verify`` takes it back with
a code from the authenticator app, or a recovery code, and only then issues the
access token.

It is never an access token, by construction rather than by a check someone
has to remember: it is signed with its own key (HKDF-SHA256 of ``SECRET_KEY``
with info ``controlvar-mfa-token-v1``, ``two_factor_keys.mfa_token_key``), not
with ``SECRET_KEY``, and carries its own issuer, ``MFA_TOKEN_ISSUER``.
``security.decode_local_token``, which every signed-in route goes through,
checks the signature with ``SECRET_KEY`` and the issuer
``security.LOCAL_TOKEN_ISSUER``, so it refuses this token twice over; and
:func:`decode_mfa_token` refuses an access token the same two ways.

Claims: ``sub`` (the account id), ``iss``, ``purpose`` (``verify``), ``iat``,
``exp`` (``VERIFY_TTL_SECONDS`` after ``iat``) and a random ``jti``. It is not
single-use: each use needs a fresh code and counts against the account's
lockout. Nothing stores it, and it never goes into a URL or a log line.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from uuid import UUID

import jwt

from backend.app.core.two_factor_keys import mfa_token_key

#: The issuer of every token this module makes; access tokens carry
#: ``security.LOCAL_TOKEN_ISSUER`` instead. (The access tokens' issuer keeps
#: the project's former name so that an upgrade signs no one out; this one is
#: new, so it takes the current name.)
MFA_TOKEN_ISSUER = "controlvar-local-mfa"
MFA_TOKEN_ALGORITHM = "HS256"
#: The one purpose this release issues: the code step of a sign-in.
VERIFY_PURPOSE = "verify"
#: Seconds a verify token is accepted for.
VERIFY_TTL_SECONDS = 300

_REQUIRED_CLAIMS = ["sub", "iss", "purpose", "iat", "exp", "jti"]


class InvalidMfaToken(ValueError):
    """Expired, forged, of another purpose, or not an mfa token at all."""


def create_mfa_token(
    user: Any,
    *,
    purpose: str = VERIFY_PURPOSE,
    ttl_seconds: int = VERIFY_TTL_SECONDS,
    now: Optional[datetime] = None,
) -> str:
    """A token for ``user``'s code step, signed with the mfa-token key."""
    issued = now if now is not None else datetime.now(timezone.utc)
    claims: Dict[str, Any] = {
        "sub": str(user.id),
        "iss": MFA_TOKEN_ISSUER,
        "purpose": purpose,
        "iat": int(issued.timestamp()),
        "exp": int((issued + timedelta(seconds=ttl_seconds)).timestamp()),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(claims, mfa_token_key(), algorithm=MFA_TOKEN_ALGORITHM)


def decode_mfa_token(token: Any, *, purpose: str = VERIFY_PURPOSE) -> UUID:
    """The account id a valid token of ``purpose`` names, or
    :class:`InvalidMfaToken`.

    Checks the signature with the mfa-token key (never ``SECRET_KEY``), the
    issuer, the expiry and the purpose; every claim must be present.
    """
    if not isinstance(token, str) or not token:
        raise InvalidMfaToken("token missing")
    try:
        claims = jwt.decode(
            token,
            mfa_token_key(),
            algorithms=[MFA_TOKEN_ALGORITHM],
            issuer=MFA_TOKEN_ISSUER,
            options={"require": _REQUIRED_CLAIMS},
        )
    except jwt.PyJWTError as exc:
        raise InvalidMfaToken(type(exc).__name__) from None
    if claims.get("purpose") != purpose:
        raise InvalidMfaToken("wrong purpose")
    try:
        return UUID(str(claims["sub"]))
    except ValueError:
        raise InvalidMfaToken("sub is not an account id") from None
