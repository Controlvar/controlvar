"""Which variant the server gives a user, computed without asking it (O-ASSIGN).

The bucket is the cross-SDK hash that ``tests/sdk-contract/golden-vectors.json``
pins: the first four bytes of ``MD5("{user_id}:{experiment_key}")`` read as a
little-endian unsigned integer and divided by 2^32, times 100, its integer part
(``docs/architecture/technical-guide.md``, "Assignment Algorithm"). The variants
take the buckets cumulatively in their creation order, each as many as its
traffic allocation; a tail left when the allocations add up to less than 100
goes to the last variant.

This is a third copy, beside the product's and the docs runner's, written so
that a defect in the product's copy is caught rather than shared. The unit
gates hold it to the golden vectors and to the docs runner on 20,000 ids.
"""

from __future__ import annotations

import hashlib
import struct
from collections.abc import Sequence

#: 2^32, the divisor the SDK contract gives.
DIVISOR = 4294967296


def _number(user_id: str, experiment_key: str) -> int:
    digest = hashlib.md5(
        f"{user_id}:{experiment_key}".encode(), usedforsecurity=False
    ).digest()
    return struct.unpack_from("<I", digest)[0]


def unit_hash(user_id: str, experiment_key: str) -> float:
    """The hash in [0, 1): the golden vectors' ``expected_hash``."""
    return _number(user_id, experiment_key) / DIVISOR


def bucket(user_id: str, experiment_key: str) -> int:
    """The user's bucket in [0, 100) under the experiment's key."""
    return int(_number(user_id, experiment_key) / DIVISOR * 100)


def variant_for(
    user_id: str, experiment_key: str, allocations: Sequence[tuple[str, float]]
) -> str:
    """The variant the hash gives *user_id*.

    *allocations* is ``(variant name, traffic allocation in percent)`` in the
    order the variants were created, as the test sent them.
    """
    if not allocations:
        raise ValueError("an experiment has at least one variant")
    position = bucket(user_id, experiment_key)
    cumulative = 0.0
    for name, allocation in allocations:
        cumulative += allocation
        if position < cumulative:
            return name
    return allocations[-1][0]
