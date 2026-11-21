# Authentication and authorization
"""
Security utilities for authentication and authorization.

This module provides functions for password hashing, token generation,
and other security-related operations.

Security notes:
- Password hashing uses bcrypt directly (work factor 12, current best
  practice). The previous passlib wrapper was abandoned upstream and
  incompatible with bcrypt >= 4.1.
- Two token formats exist:
    * ``AUTH_PROVIDER=local`` (the default): HS256 JWTs signed
      with ``settings.SECRET_KEY`` and issued by ``create_local_access_token``.
      They are validated by ``decode_local_token`` on every request.
    * ``AUTH_PROVIDER=cognito``: opaque AWS Cognito access tokens validated
      remotely by ``CognitoAuthService``.  ``decode_token`` below is a stub
      that exists only so legacy tests can monkeypatch it; it MUST NOT be used
      for real validation.
- A local access token, and the short token between the two steps of a
  two-step sign-in (``core.mfa_token``), carry the account's password stamp
  in the ``pwv`` claim (#501): a keyed digest of the account id and the
  stored ``hashed_password`` (:func:`password_stamp`). A token whose stamp no
  longer matches the account is refused (:func:`stamp_is_current`), so any
  write to ``hashed_password`` signs out every session of that account, on
  every path, raw SQL included. Two consequences: a rehash of passwords at
  sign-in, or any bulk rewrite of ``hashed_password``, signs every account it
  touches out, each time it runs; and the stamp's format (its ``info``
  string, length and encoding) is part of the token format, so changing it
  signs everyone out once.
"""

import base64
import hashlib
import hmac
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Mapping, Optional

import bcrypt
import jwt
from fastapi.security import OAuth2PasswordBearer

from backend.app.core.config import settings
from backend.app.core.two_factor_keys import derive_key

logger = logging.getLogger(__name__)

# Bcrypt work factor; 12 rounds is the current best-practice default.
_BCRYPT_ROUNDS = 12

# Issuer claim stamped into every locally issued JWT.  ``decode_local_token``
# rejects tokens carrying any other issuer, so a Cognito token (or a JWT from
# some other service that happens to share the secret) can never be replayed
# against the local provider.
LOCAL_TOKEN_ISSUER = "experimently-local"
LOCAL_TOKEN_ALGORITHM = "HS256"

# The password stamp (#501). The claim's name, the ``info`` of the key it is
# computed with (HKDF-SHA256 of SECRET_KEY, ``two_factor_keys.derive_key``),
# and its length in characters: 16 bytes of HMAC-SHA256, base64url without
# padding. All three are part of the token format.
PASSWORD_STAMP_CLAIM = "pwv"
PASSWORD_STAMP_INFO = b"controlvar-access-token-pwv-v1"
PASSWORD_STAMP_LENGTH = 22

# OAuth2 password bearer scheme.  ``auto_error=False`` so that the absence of
# a token reaches ``deps.get_current_user``, which decides between the
# dev-admin bypass and a 401.  The tokenUrl keeps Swagger's "Authorize" button
# working with both providers (the /auth/token form accepts email+password
# when AUTH_PROVIDER=local).
oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl=f"{settings.API_V1_STR}/auth/token", auto_error=False
)


class InvalidTokenError(ValueError):
    """Raised when a local access token cannot be decoded or fails validation."""


def unwrap_secret(password: Any) -> str:
    """
    Return the plain-text value of *password*.

    Request schemas carry passwords as pydantic ``SecretStr`` (so they never
    leak into logs or ``repr``), while internal callers pass plain ``str``.
    ``get_password_hash``/``verify_password`` need the raw text, so every call
    site funnels through here rather than reaching for ``.get_secret_value()``
    on something that may already be a string.

    Args:
        password: A ``str`` or anything exposing ``get_secret_value()``.

    Returns:
        The plain-text password.
    """
    get_secret_value = getattr(password, "get_secret_value", None)
    if callable(get_secret_value):
        return get_secret_value()
    return password


def get_password_hash(password: str) -> str:
    """
    Hash a password using bcrypt.

    Args:
        password: Plain text password (see ``unwrap_secret`` for ``SecretStr``)

    Returns:
        Hashed password (bcrypt-format str)
    """
    salt = bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Verify a password against a bcrypt hash.

    Args:
        plain_password: Plain text password
        hashed_password: Bcrypt hash (as produced by get_password_hash)

    Returns:
        True if password matches hash, False otherwise
    """
    try:
        return bcrypt.checkpw(
            plain_password.encode("utf-8"),
            hashed_password.encode("utf-8"),
        )
    except (ValueError, TypeError):
        # Malformed hash — treat as a non-match rather than raising.
        return False


# ---------------------------------------------------------------------------
# Local provider JWTs
# ---------------------------------------------------------------------------


def role_name(role: Any) -> str:
    """Return the canonical upper-case role name (``ADMIN``, ``VIEWER`` ...)."""
    if role is None:
        return "VIEWER"
    name = getattr(role, "name", None)
    if isinstance(name, str):
        return name.upper()
    return str(role).upper()


def password_stamp(user: Any) -> str:
    """
    The password stamp of *user*: the value of the ``pwv`` claim.

    The first 16 bytes of HMAC-SHA256 over the account's 16-byte UUID followed
    by the UTF-8 bytes of its stored ``hashed_password`` (none when it has no
    password), keyed with HKDF-SHA256 of ``SECRET_KEY`` for
    ``PASSWORD_STAMP_INFO``; base64url without padding, 22 characters. The key
    is derived on each call, as ``two_factor_keys`` does, so it follows
    ``SECRET_KEY``. It changes exactly when the stored hash changes: bcrypt
    salts every hash, so setting the same password again changes it too.

    Args:
        user: A ``User`` model instance (anything with ``id`` and
            ``hashed_password``).

    Returns:
        The stamp, 22 ASCII characters.
    """
    key = derive_key(PASSWORD_STAMP_INFO)
    stored = getattr(user, "hashed_password", None)
    if not isinstance(stored, str):
        stored = ""
    message = uuid.UUID(str(user.id)).bytes + stored.encode("utf-8")
    digest = hmac.new(key, message, hashlib.sha256).digest()[:16]
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def stamp_is_current(claims: Mapping[str, Any], user: Any) -> bool:
    """
    Whether the ``pwv`` claim of a decoded token matches *user* as it is now.

    * no ``pwv`` claim: ``True`` (see the comment below);
    * a claim that is not a string of exactly ``PASSWORD_STAMP_LENGTH`` ASCII
      characters: ``False``;
    * otherwise the claim compared with :func:`password_stamp` in constant
      time. A failure to compute the expected stamp is ``False``.
    """
    if PASSWORD_STAMP_CLAIM not in claims:
        # Tokens minted before this release carry no stamp and are accepted
        # until they expire. Requiring the claim is a separate, later change;
        # it would refuse every token minted before it.
        return True
    claim = claims[PASSWORD_STAMP_CLAIM]
    if (
        not isinstance(claim, str)
        or len(claim) != PASSWORD_STAMP_LENGTH
        or not claim.isascii()
    ):
        return False
    try:
        expected = password_stamp(user)
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(claim.encode("ascii"), expected.encode("ascii"))


def create_local_access_token(
    user: Any, *, expires_minutes: Optional[int] = None
) -> str:
    """
    Issue an HS256 JWT for *user* signed with ``settings.SECRET_KEY``.

    Claims: ``sub`` (user id as string UUID), ``email``, ``role``, ``iss``
    (``experimently-local``), ``iat``, ``exp``, a random ``jti`` and ``pwv``
    (:func:`password_stamp`).

    Args:
        user: A ``User`` model instance (anything with ``id``, ``email``,
            ``role`` and ``hashed_password``).
        expires_minutes: Override for ``LOCAL_AUTH_TOKEN_TTL_MINUTES``.

    Returns:
        The encoded token as a string.
    """
    ttl = (
        expires_minutes
        if expires_minutes is not None
        else settings.LOCAL_AUTH_TOKEN_TTL_MINUTES
    )
    now = datetime.now(timezone.utc)
    claims: Dict[str, Any] = {
        "sub": str(user.id),
        "email": user.email,
        "role": role_name(getattr(user, "role", None)),
        "iss": LOCAL_TOKEN_ISSUER,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=ttl)).timestamp()),
        "jti": uuid.uuid4().hex,
        PASSWORD_STAMP_CLAIM: password_stamp(user),
    }
    return jwt.encode(claims, settings.SECRET_KEY, algorithm=LOCAL_TOKEN_ALGORITHM)


def decode_local_token(token: str) -> Dict[str, Any]:
    """
    Decode and validate a locally issued access token.

    Verifies the signature (HS256, ``settings.SECRET_KEY``), the issuer, and
    the ``exp``/``iat`` claims; requires ``sub`` to be present.

    Raises:
        InvalidTokenError: for any malformed, expired, or foreign token.
    """
    if not token or not isinstance(token, str):
        raise InvalidTokenError("Token missing")
    try:
        return jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[LOCAL_TOKEN_ALGORITHM],
            issuer=LOCAL_TOKEN_ISSUER,
            options={"require": ["sub", "exp", "iat", "iss"]},
        )
    except jwt.PyJWTError as exc:
        raise InvalidTokenError(str(exc)) from exc


def decode_token(token: str) -> dict:
    """
    Decode a JWT token and return the claims.

    IMPORTANT: This is a stub implementation used exclusively for test mocking.
    In production the token is validated by AWS Cognito via CognitoAuthService
    (or by ``decode_local_token`` for the local provider).  This function MUST
    NOT be used for real token validation.

    Args:
        token: JWT token to decode

    Returns:
        Dict containing token claims (stub values only)
    """
    is_testing = os.getenv("TESTING", "").lower() in ("1", "true", "yes")
    if not is_testing:
        raise RuntimeError(
            "decode_token() is a test-only stub and must not be called outside "
            "a test context. Use decode_local_token() or CognitoAuthService."
        )
    return {
        "sub": "user_id",
        "username": "username",
        "exp": 0,
        "iat": 0,
        "email": "user@example.com",
    }


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------


def hash_api_key(api_key: str) -> str:
    """
    Hash an API key using SHA-256 for secure storage.

    Unlike passwords, API keys need fast lookup (not bcrypt) but must not
    be stored in plaintext. SHA-256 provides a one-way hash suitable for
    API key verification with constant-time comparison.

    Args:
        api_key: The plaintext API key to hash.

    Returns:
        Hex-encoded SHA-256 hash of the key.
    """
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def verify_api_key(plaintext_key: str, hashed_key: str) -> bool:
    """
    Verify an API key against its stored hash using constant-time comparison.

    Args:
        plaintext_key: The plaintext API key from the request.
        hashed_key: The stored SHA-256 hash to compare against.

    Returns:
        True if the key matches, False otherwise.
    """
    computed = hashlib.sha256(plaintext_key.encode("utf-8")).hexdigest()
    return hmac.compare_digest(computed, hashed_key)
