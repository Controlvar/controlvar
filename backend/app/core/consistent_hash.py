"""The cross-SDK experiment-assignment hash.

This is the hash ``tests/sdk-contract/golden-vectors.json`` pins and every SDK
implements. The server uses it for experiment variant assignment. It is NOT
the feature-flag rollout function, and it is not the only bucketing hash in the
platform.

WHERE BUCKETING HAPPENS, and with what:

    uses this module (first 4 bytes LE of MD5("{user}:{key}"), / 2^32)
      assignment_service._hash_user_to_variant   key = experiment.key (UUID fallback), bucket_of
      (endpoints/openfeature.py _evaluate_flag used it too; the module was removed, #241, #737)

    its own MD5("{user}:{flag.key}") read as the FULL 128-bit digest, % 100
      feature_flag_service._evaluate_percentage_rollout
          the flag rollout function: the flag's rollout_percentage and a
          matched targeting rule's rollout_percentage both go through it.
          int(md5(f"{user}:{key}").hexdigest(), 16) % 100 -- for
          user-123 / my-flag that is bucket 79, where bucket_of gives 69.

    other functions, each its own
      global_holdout_service / mutual_exclusion_service
          MD5("{user}:{salt}"), first 4 bytes LE, % 100 (holdout) or / 2^32 (MEG)
      endpoints/tracking.py (bandit routing)
          MD5("{user}:{experiment.id}:bandit"), full digest, % 10000
      core/rules_engine.should_include_in_rollout
          MD5("{user}:{rule.id}"), full digest, % 100
      services/rules_evaluation_service (percentage_bucket operator)
          MD5("{user}"), full digest, % 100
      services/llm_experiment_service
          MD5("{experiment_id}:{user}"), full digest, % 10000
      backend/lambda/shared/consistent_hash.py
          its own salted hasher ("{key}_traffic", "{key}_variant")

Changing any of these re-buckets users, so none is changed in passing; a site
moves to this module only as a deliberate, stated change.

HISTORY. There were four experiment/flag hashes, and they disagreed (#81).

    openfeature.py          MD5("{user}:{key}"), first 4 bytes LE, / 2^32 (removed, #241)
    assignment_service.py   MD5("{user}:{experiment.id}"), FULL digest, % 100
    global_holdout / MEG    MD5("{user}:{salt}"), first 4 bytes LE, % 100
    lambda/shared           MurmurHash3-ish of the user, salted "{key}_variant"

The first is the contract: ``tests/sdk-contract/golden-vectors.json`` pins it
and the SDKs implement it. The others were free to drift because
nothing compared them.

They had drifted. For ``user-123`` / ``my-flag`` -- one input, one hash
function -- the contract puts the user in bucket **69** and
``assignment_service`` put them in bucket **79** (as the flag rollout function
still does), because reading the whole
128-bit digest modulo 100 is a different number from reading the first four
bytes little-endian. And the two were not even hashing the same string: the
contract uses the experiment's public ``key``, the assignment service used its
UUID primary key, which no client has.

The consequence is the one an experimentation platform cannot have: a user
counted in variant A by the API and variant B by an SDK evaluating locally,
silently, with no error anywhere -- and every metric computed from the join
between them quietly wrong.

THE ALGORITHM, as the golden vectors state it:

    1. concatenate "{user_id}:{key}"
    2. UTF-8 encode
    3. MD5 digest (16 bytes)
    4. read the FIRST FOUR BYTES as a little-endian unsigned 32-bit integer
    5. divide by 2^32

Step 4 is the one that is easy to get wrong and impossible to notice: every
other reading of the digest produces a valid-looking number in the right range.

MD5 is a bucketing function here, not a security primitive. It is fixed by the
contract: the SDKs implement it, so changing it is a coordinated release,
not a refactor. ``usedforsecurity=False`` says so to the linters.
"""

from __future__ import annotations

import hashlib
import struct
from typing import Final

#: 2^32. The divisor the golden vectors specify.
HASH_DIVISOR: Final[int] = 4294967296

#: How many buckets a percentage-based allocation is drawn from.
BUCKET_COUNT: Final[int] = 100


def hash_user(user_id: str, key: str) -> float:
    """A stable float in [0.0, 1.0) for *user_id* under *key*.

    ``key`` is whatever namespaces the decision -- a flag key, an experiment
    key, a holdout salt. The same user under two different keys gets two
    unrelated positions, which is what stops every experiment bucketing the
    same users together.

    >>> round(hash_user("user-123", "my-flag"), 16)
    0.6927449859213084
    """
    digest = hashlib.md5(
        f"{user_id}:{key}".encode("utf-8"), usedforsecurity=False
    ).digest()
    (uint32,) = struct.unpack_from("<I", digest[:4])
    return uint32 / HASH_DIVISOR


def bucket_of(user_id: str, key: str) -> int:
    """The same position as an integer in [0, 100).

    For allocations expressed as percentages. Derived from :func:`hash_user`
    rather than computed separately, so the two can never disagree about which
    side of a boundary a user falls on.

    >>> bucket_of("user-123", "my-flag")
    69
    """
    return int(hash_user(user_id, key) * BUCKET_COUNT)
