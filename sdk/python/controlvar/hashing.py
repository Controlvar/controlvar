"""Cross-SDK MD5 consistent hash.

This is exported as a utility so that ``{user_id}:{flag_key}`` buckets can be
reproduced offline (and verified against ``tests/sdk-contract/golden-vectors.json``).
It is NOT the function the server buckets flag rollouts with (that is
``int(md5(...).hexdigest(), 16) % 100``, "md5-mod100-v1", which local
evaluation reproduces in :mod:`controlvar.evaluator`), and nothing in the
client uses it to decide a variant or a flag.

Algorithm:
  1. Concatenate ``"{user_id}:{flag_key}"`` and UTF-8 encode it.
  2. MD5 digest (16 bytes).
  3. Read the first 4 bytes as a little-endian unsigned 32-bit integer.
  4. Divide by 2**32 to normalise to ``[0.0, 1.0)``.
"""

from __future__ import annotations

import hashlib
import struct

__all__ = ["consistent_hash", "md5_hex"]

_HASH_DIVISOR = 4294967296  # 2 ** 32


def _digest(user_id: str, flag_key: str) -> bytes:
    combined = f"{user_id}:{flag_key}".encode("utf-8")
    return hashlib.md5(combined, usedforsecurity=False).digest()  # noqa: S324


def consistent_hash(user_id: str, flag_key: str) -> float:
    """Return the bucket for ``(user_id, flag_key)`` as a float in ``[0.0, 1.0)``."""
    (value,) = struct.unpack("<I", _digest(user_id, flag_key)[:4])
    return value / _HASH_DIVISOR


def md5_hex(user_id: str, flag_key: str) -> str:
    """Return the full MD5 hex digest of ``"{user_id}:{flag_key}"``."""
    return _digest(user_id, flag_key).hex()
