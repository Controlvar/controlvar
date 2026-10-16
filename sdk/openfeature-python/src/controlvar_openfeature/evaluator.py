"""
Cross-SDK MD5 consistent hash (re-exported from the ``experimentation`` SDK).

Kept as a utility so buckets can be reproduced offline and checked against
``tests/sdk-contract/golden-vectors.json``. The provider never calls it:
flags are evaluated by the server.

Algorithm (byte-for-byte compatible with every other platform SDK):
  1. Concatenate ``"{user_id}:{flag_key}"`` as UTF-8.
  2. MD5 digest.
  3. First 4 bytes as a little-endian unsigned 32-bit integer.
  4. Divide by 2**32 to normalise to ``[0.0, 1.0)``.

Test vector: ``hash_user("user-123", "my-flag")`` = 0.6927449859213084
(MD5 hex ``43bc57b1e81dec71c5242122ac05170f``).
"""

from __future__ import annotations

from controlvar import consistent_hash, md5_hex

__all__ = ["hash_user", "md5_hex"]


def hash_user(user_id: str, flag_key: str) -> float:
    """Return a float in ``[0.0, 1.0)`` for the ``(user_id, flag_key)`` pair."""
    return consistent_hash(user_id, flag_key)
