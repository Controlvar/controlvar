"""The keys two-step sign-in derives from ``SECRET_KEY`` (#1812).

Two keys, each HKDF-SHA256 of ``SECRET_KEY`` with its own ``info`` string, so
neither is ``SECRET_KEY`` itself and neither can stand in for the other:

* the key that seals each account's authenticator secret at rest (Fernet,
  ``user_totp.secret_enc``), info ``controlvar-totp-secret-v1``;
* the key that signs the short-lived token between the password step and the
  code step, info ``controlvar-mfa-token-v1``.

No setting of its own: changing ``SECRET_KEY`` changes both. A secret sealed
under the old one can then no longer be opened, and :func:`open_secret`
raises :class:`SecretUnreadable` rather than returning anything; the account
cannot finish signing in until its two-step sign-in is reset
(``python -m backend.app.db.two_factor_reset``) and set up again.

The ``info`` strings are part of the stored data's format. Changing one makes
every stored secret unreadable, so a new one is a new version with a
re-encryption step, never an edit; ``test_two_factor_keys.py`` pins the
derived keys to committed values.
"""

from __future__ import annotations

import base64
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

#: ``info`` for the key that seals authenticator secrets.
TOTP_SECRET_INFO = b"controlvar-totp-secret-v1"
#: ``info`` for the key that signs the token between the two sign-in steps.
MFA_TOKEN_INFO = b"controlvar-mfa-token-v1"
#: Bytes in each derived key: Fernet takes 32, and HS256 wants at least 32.
KEY_BYTES = 32


class SecretUnreadable(Exception):
    """A stored secret cannot be opened with the current ``SECRET_KEY``."""


def _secret_key(secret_key: Optional[str]) -> str:
    if secret_key is not None:
        return secret_key
    from backend.app.core.config import settings

    return settings.SECRET_KEY


def derive_key(info: bytes, secret_key: Optional[str] = None) -> bytes:
    """HKDF-SHA256 of ``secret_key`` (default ``settings.SECRET_KEY``), no
    salt, ``KEY_BYTES`` long, for ``info``."""
    return HKDF(
        algorithm=hashes.SHA256(),
        length=KEY_BYTES,
        salt=None,
        info=info,
    ).derive(_secret_key(secret_key).encode("utf-8"))


def mfa_token_key(secret_key: Optional[str] = None) -> bytes:
    """The key that signs the token between the password and code steps."""
    return derive_key(MFA_TOKEN_INFO, secret_key)


def _fernet(secret_key: Optional[str]) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(derive_key(TOTP_SECRET_INFO, secret_key)))


def seal_secret(raw: bytes, secret_key: Optional[str] = None) -> str:
    """``raw`` sealed for storage: a Fernet token, as text."""
    return _fernet(secret_key).encrypt(raw).decode("ascii")


def open_secret(sealed: str, secret_key: Optional[str] = None) -> bytes:
    """The secret :func:`seal_secret` sealed, or :class:`SecretUnreadable`.

    Never returns anything for a token it cannot verify: a secret sealed
    under another ``SECRET_KEY``, a changed ``info`` string or a damaged
    value all raise.
    """
    try:
        return _fernet(secret_key).decrypt(sealed.encode("ascii"))
    except (InvalidToken, UnicodeEncodeError, ValueError, TypeError) as exc:
        raise SecretUnreadable() from exc
