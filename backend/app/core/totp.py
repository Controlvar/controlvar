"""Time-based one-time passwords (RFC 6238), standard library only (#1812).

What an authenticator app shows: HMAC-SHA1 over the number of 30-second steps
since the Unix epoch, truncated as RFC 4226 section 5.3 describes, as 6 digits
with leading zeros kept. The secret is 20 random bytes, given to the app in
base32 inside an ``otpauth://`` link.

Nothing here reads a clock. Every function that needs the time takes it as an
argument, so the caller owns the one clock (``TwoFactorService``'s) and a test
can name the second it means.

A code is accepted for the step it was made in and for one step either side
(``WINDOW``), so a phone up to about 30 seconds ahead or behind the server
still signs in. Which step matched is returned, never just "yes": the caller
stores it and refuses that step and every earlier one afterwards, so a code is
used once.

Nothing in the API imports this module directly: every check goes through
``backend.app.services.two_factor_service``, which counts the refusals.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
from typing import Optional
from urllib.parse import quote, urlencode

#: Seconds in one step.
STEP_SECONDS = 30
#: Digits in a code.
DIGITS = 6
#: Steps either side of the current one that are also accepted.
WINDOW = 1
#: Bytes of a new secret: 160 bits, RFC 4226's recommended length for SHA-1.
SECRET_BYTES = 20
#: The name an authenticator app shows above the code.
ISSUER = "Controlvar"


def hotp(key: bytes, counter: int, digits: int = DIGITS) -> str:
    """RFC 4226 HOTP: the code for one counter value, zero-padded."""
    if counter < 0:
        raise ValueError("counter must not be negative")
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(binary % (10**digits)).zfill(digits)


def step_at(unix_time: float) -> int:
    """The step a moment falls in: whole ``STEP_SECONDS`` since the epoch."""
    return int(unix_time // STEP_SECONDS)


def totp_at(key: bytes, unix_time: float, digits: int = DIGITS) -> str:
    """RFC 6238 TOTP: the code an app shows at ``unix_time``."""
    return hotp(key, step_at(unix_time), digits)


def matching_step(key: bytes, code: str, unix_time: float) -> Optional[int]:
    """The step within ``WINDOW`` of ``unix_time`` whose code is ``code``.

    ``code`` must already be normalised to ``DIGITS`` ASCII digits; anything
    else matches nothing. Every step in the window is compared, with
    ``hmac.compare_digest``, whichever matches, so the time taken does not
    say which one did. Returns the newest matching step, or None.
    """
    if len(code) != DIGITS or not code.isascii() or not code.isdigit():
        return None
    current = step_at(unix_time)
    found: Optional[int] = None
    for step in range(current - WINDOW, current + WINDOW + 1):
        if step < 0:
            continue
        if hmac.compare_digest(hotp(key, step).encode(), code.encode()):
            found = step
    return found


def new_secret() -> bytes:
    """A new secret: ``SECRET_BYTES`` random bytes."""
    return secrets.token_bytes(SECRET_BYTES)


def secret_text(key: bytes) -> str:
    """The secret as an app takes it typed in: base32, no padding."""
    return base64.b32encode(key).decode("ascii").rstrip("=")


def otpauth_uri(key: bytes, account: str) -> str:
    """The ``otpauth://totp/`` link a QR code carries (Key Uri Format).

    ``otpauth://totp/Controlvar:<account>?secret=...&issuer=Controlvar&
    algorithm=SHA1&digits=6&period=30``; the label is percent-encoded.
    """
    label = quote(f"{ISSUER}:{account}", safe=":@")
    query = urlencode(
        {
            "secret": secret_text(key),
            "issuer": ISSUER,
            "algorithm": "SHA1",
            "digits": DIGITS,
            "period": STEP_SECONDS,
        }
    )
    return f"otpauth://totp/{label}?{query}"
