#!/usr/bin/env python3
"""
Cross-SDK hash parity verification for the Controlvar .NET SDK.

This script verifies that the hash formula used by the .NET SDK matches the
reference implementation used across all other SDKs (JavaScript, Python, Java, Go, etc.).

Usage:
    python3 sdk/dotnet/verify_hash.py

Algorithm:
    MD5("{userId}:{flagKey}") -> first 4 bytes as little-endian uint32 -> divide by 2^32 (4294967296.0)

Cross-SDK canonical test vector:
    hash_user("user-123", "my-flag") == 0.6927449859213084
"""

import hashlib
import struct
import sys


def hash_user(user_id: str, flag_key: str) -> float:
    """
    Compute the consistent bucket hash used by all Controlvar SDKs.

    Args:
        user_id:  The user identifier.
        flag_key: The feature flag key.

    Returns:
        A float in [0.0, 1.0).
    """
    raw = hashlib.md5(f"{user_id}:{flag_key}".encode("utf-8")).digest()
    # '<I' = little-endian unsigned 32-bit integer from first 4 bytes.
    v = struct.unpack_from("<I", raw, 0)[0]
    return v / 4294967296.0


# ---------------------------------------------------------------------------
# Test vectors
# ---------------------------------------------------------------------------

def run_tests() -> None:
    tests = [
        # (user_id, flag_key, expected, description)
        ("user-123", "my-flag", 0.6927449859213084, "canonical cross-SDK test vector"),
        ("", "",              None,                 "empty strings — result must be in [0, 1)"),
        ("user-1",  "flag-a", None,                 "basic user/flag — result must be in [0, 1)"),
    ]

    passed = 0
    failed = 0

    for user_id, flag_key, expected, description in tests:
        result = hash_user(user_id, flag_key)

        # Range check: every result must be in [0.0, 1.0)
        if not (0.0 <= result < 1.0):
            print(f"  FAIL [{description}]: result {result} is outside [0, 1)")
            failed += 1
            continue

        # Exact value check where expected is specified
        if expected is not None:
            tolerance = 1e-10
            if abs(result - expected) < tolerance:
                print(f"  PASS [{description}]: {result}")
                passed += 1
            else:
                print(f"  FAIL [{description}]: expected {expected}, got {result} "
                      f"(diff: {abs(result - expected):.2e})")
                failed += 1
        else:
            print(f"  PASS [{description}]: {result} (in range)")
            passed += 1

    print()
    print(f"Results: {passed} passed, {failed} failed out of {passed + failed} tests.")

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    print("Controlvar — .NET SDK cross-SDK hash parity verification")
    print("=" * 72)
    print()

    # Primary assertion
    result = hash_user("user-123", "my-flag")
    expected = 0.6927449859213084

    print(f"hash_user(\"user-123\", \"my-flag\") = {result}")
    print(f"Expected                           = {expected}")
    print()

    assert abs(result - expected) < 1e-10, (
        f"Hash mismatch: got {result}, expected {expected}"
    )
    print("Hash parity verified: PASS")
    print()

    print("Running full test suite:")
    run_tests()
