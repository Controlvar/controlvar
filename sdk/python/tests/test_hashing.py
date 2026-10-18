"""Cross-SDK consistent-hash utility against the shared golden vectors."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from controlvar import consistent_hash, md5_hex

GOLDEN = Path(__file__).resolve().parents[3] / "tests" / "sdk-contract" / "golden-vectors.json"
TOLERANCE = 1e-10
PRIMARY_VECTOR = {
    "user_id": "user-123",
    "flag_key": "my-flag",
    "md5_hex": "43bc57b1e81dec71c5242122ac05170f",
    "expected_hash": 0.6927449859213084,
}


def _golden() -> dict:
    if GOLDEN.exists():
        return json.loads(GOLDEN.read_text())
    return {"hash_vectors": [PRIMARY_VECTOR], "rollout_vectors": []}


def _vector_id(vector: dict) -> str:
    return f"{vector['user_id']}:{vector['flag_key']}"


@pytest.mark.parametrize("vector", _golden()["hash_vectors"], ids=_vector_id)
def test_consistent_hash_matches_golden_vector(vector):
    assert abs(consistent_hash(vector["user_id"], vector["flag_key"]) - vector["expected_hash"]) < TOLERANCE


@pytest.mark.parametrize("vector", _golden()["hash_vectors"], ids=_vector_id)
def test_md5_hex_matches_golden_vector(vector):
    assert md5_hex(vector["user_id"], vector["flag_key"]) == vector["md5_hex"]


@pytest.mark.parametrize("vector", _golden()["rollout_vectors"], ids=_vector_id)
def test_rollout_inclusion_matches_golden_vector(vector):
    bucket = consistent_hash(vector["user_id"], vector["flag_key"])
    for percent in (3, 5, 50, 100):
        key = f"rollout_{percent}_percent"
        if key in vector:
            assert (bucket < percent / 100) == vector[key], key


def test_hash_is_deterministic_and_in_unit_interval():
    for i in range(50):
        first = consistent_hash(f"user-{i}", "flag")
        assert first == consistent_hash(f"user-{i}", "flag")
        assert 0.0 <= first < 1.0
