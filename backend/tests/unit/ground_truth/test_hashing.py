"""H1: the library's assignment hash is the platform's, without importing it.

``ground_truth.hashing`` is held to the cross-SDK golden vectors
(``tests/sdk-contract/golden-vectors.json``) and to the docs runner's own copy
(``docs_runner.traffic``) on 20,000 ids at 34/33/33. Scaling the hash by
``% 100`` instead of dividing by 2^32 moves about two thirds of the users to
another arm, and both checks fail.
"""

from __future__ import annotations

import json

import pytest

from backend.tests.unit.ground_truth.library import REPO_ROOT, hashing, traffic

pytestmark = pytest.mark.unit

GOLDEN = json.loads(
    (REPO_ROOT / "tests" / "sdk-contract" / "golden-vectors.json").read_text()
)
VECTORS = [*GOLDEN["hash_vectors"], *GOLDEN["rollout_vectors"]]
THREE_ARMS = [("control", 34), ("lifted", 33), ("other", 33)]


@pytest.mark.parametrize(
    "vector", VECTORS, ids=lambda v: f"{v['user_id']}:{v['flag_key']}"
)
def test_the_golden_vectors(vector):
    found = hashing.unit_hash(vector["user_id"], vector["flag_key"])
    assert abs(found - vector["expected_hash"]) <= 1e-10, (found, vector)
    assert hashing.bucket(vector["user_id"], vector["flag_key"]) == int(
        vector["expected_hash"] * 100
    )


def test_the_docs_runner_agrees_on_20000_ids():
    key = "gt-h1"
    users = [f"gt-h1-{number:05d}" for number in range(20000)]
    buckets = sum(hashing.bucket(u, key) != traffic.bucket(u, key) for u in users)
    arms = sum(
        hashing.variant_for(u, key, THREE_ARMS)
        != traffic.variant_for(u, key, THREE_ARMS)
        for u in users
    )
    assert (arms, buckets) == (0, 0), (
        f"{arms} of 20000 arm mismatches, {buckets} bucket mismatches"
    )


def _user_in_bucket(wanted: int, key: str) -> str:
    for number in range(100000):
        user = f"probe-{number}"
        if hashing.bucket(user, key) == wanted:
            return user
    raise AssertionError(f"no probe id lands in bucket {wanted}")


@pytest.mark.parametrize(
    "bucket, variant",
    [
        (0, "control"),
        (33, "control"),
        (34, "lifted"),
        (66, "lifted"),
        (67, "other"),
        (99, "other"),
    ],
)
def test_the_variants_take_the_buckets_in_creation_order(bucket, variant):
    user = _user_in_bucket(bucket, "gt-order")
    assert hashing.variant_for(user, "gt-order", THREE_ARMS) == variant


def test_a_tail_short_of_100_goes_to_the_last_variant():
    user = _user_in_bucket(95, "gt-tail")
    assert hashing.variant_for(user, "gt-tail", [("a", 10), ("b", 20)]) == "b"


def test_an_experiment_without_variants_is_refused():
    with pytest.raises(ValueError, match="at least one variant"):
        hashing.variant_for("u", "k", [])
