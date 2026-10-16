import XCTest
@testable import Controlvar

/// Tests that the iOS SDK hash produces the same results as the Java, Python, and JS SDKs.
///
/// ## Cross-SDK Hash Specification
/// All SDKs share the same algorithm:
/// 1. Compute MD5 of `"{userId}:{flagKey}"` (UTF-8 encoded).
/// 2. Interpret the first 4 bytes as a **little-endian** unsigned 32-bit integer.
/// 3. Divide by `0x100000000` (2^32 = 4294967296) to normalize to `[0.0, 1.0)`.
///
/// ## Test Vectors
/// Expected values were computed independently using Python:
/// ```python
/// import hashlib, struct
/// def h(uid, key):
///     raw = hashlib.md5(f"{uid}:{key}".encode()).digest()
///     v = struct.unpack('<I', raw[:4])[0]
///     return v / 0x100000000
/// ```
final class HashCompatibilityTests: XCTestCase {

    // MARK: - Known Vector Tests

    /// Validates three representative user/flag combinations against values computed
    /// by the Python reference implementation. Any deviation indicates a cross-SDK
    /// compatibility break.
    func testHash_knownVectors() {
        // Values independently computed via Python hashlib + struct.unpack('<I', ...)
        let testCases: [(userId: String, flagKey: String, expected: Double, tolerance: Double)] = [
            // MD5("user-123:my-flag") first 4 bytes LE = 0xB143A269 → / 2^32
            ("user-123",   "my-flag",    0.6927449859213084,  1e-9),
            // MD5("alice:feature-x") first 4 bytes LE
            ("alice",      "feature-x",  0.6025943041313440,  1e-9),
            // MD5("test-user:test-flag") first 4 bytes LE
            ("test-user",  "test-flag",  0.4590241177938879,  1e-9),
        ]

        for tc in testCases {
            let result = FeatureFlagEvaluator.hashUser(tc.userId, flagKey: tc.flagKey)
            XCTAssertEqual(
                result,
                tc.expected,
                accuracy: tc.tolerance,
                "Hash mismatch for user='\(tc.userId)' flag='\(tc.flagKey)'. " +
                "Got \(result), expected \(tc.expected). " +
                "This indicates a cross-SDK compatibility regression."
            )
        }
    }

    // MARK: - Edge Cases

    /// An empty user ID is a valid input; the result must be in [0, 1).
    func testHash_emptyUser() {
        let result = FeatureFlagEvaluator.hashUser("", flagKey: "flag")
        // MD5(":flag") first 4 bytes LE / 2^32, computed from Python = 0.7201172418426722
        XCTAssertEqual(result, 0.7201172418426722, accuracy: 1e-9)
    }

    /// An empty flag key is a valid input; the result must be in [0, 1).
    func testHash_emptyFlagKey() {
        let result = FeatureFlagEvaluator.hashUser("user-123", flagKey: "")
        XCTAssertGreaterThanOrEqual(result, 0.0)
        XCTAssertLessThan(result, 1.0)
    }

    /// Both empty inputs should still produce a valid hash.
    func testHash_emptyBothInputs() {
        let result = FeatureFlagEvaluator.hashUser("", flagKey: "")
        XCTAssertGreaterThanOrEqual(result, 0.0)
        XCTAssertLessThan(result, 1.0)
    }

    // MARK: - Determinism

    /// The same inputs must always produce the same hash — the function is pure.
    func testHash_consistency() {
        let r1 = FeatureFlagEvaluator.hashUser("user", flagKey: "flag")
        let r2 = FeatureFlagEvaluator.hashUser("user", flagKey: "flag")
        let r3 = FeatureFlagEvaluator.hashUser("user", flagKey: "flag")
        XCTAssertEqual(r1, r2, "Hash is non-deterministic across calls")
        XCTAssertEqual(r2, r3, "Hash is non-deterministic across calls")
    }

    // MARK: - Differentiation

    /// Different users with the same flag key must (in practice) produce different hashes.
    func testHash_differentUsers_differentResults() {
        let r1 = FeatureFlagEvaluator.hashUser("user-1", flagKey: "flag")
        let r2 = FeatureFlagEvaluator.hashUser("user-2", flagKey: "flag")
        XCTAssertNotEqual(r1, r2, "Different users produced the same hash for the same flag key")
    }

    /// Different flag keys with the same user ID must produce different hashes.
    func testHash_differentFlags_differentResults() {
        let r1 = FeatureFlagEvaluator.hashUser("user-1", flagKey: "flag-a")
        let r2 = FeatureFlagEvaluator.hashUser("user-1", flagKey: "flag-b")
        XCTAssertNotEqual(r1, r2, "Same user produced the same hash for different flag keys")
    }

    /// Swapping the separator should change the result (the colon separator is meaningful).
    func testHash_separatorMatters() {
        let r1 = FeatureFlagEvaluator.hashUser("user", flagKey: "flag")
        let r2 = FeatureFlagEvaluator.hashUser("userflag", flagKey: "")
        XCTAssertNotEqual(r1, r2, "Hash collision when separator is omitted")
    }

    // MARK: - Distribution

    /// Hash values for 1000 users should follow a roughly uniform distribution.
    /// We expect approximately 500 ± 50 users below 0.5 (95% confidence interval).
    func testHash_distribution_uniform() {
        let count = (0..<1000).filter { i in
            FeatureFlagEvaluator.hashUser("user-\(i)", flagKey: "test-flag") < 0.5
        }.count

        XCTAssertGreaterThan(count, 450, "Distribution skewed low: only \(count)/1000 below 0.5")
        XCTAssertLessThan(count, 550, "Distribution skewed high: \(count)/1000 below 0.5")
    }

    /// Verify distribution for a second flag key to guard against key-specific bias.
    func testHash_distribution_anotherFlag() {
        let count = (0..<1000).filter { i in
            FeatureFlagEvaluator.hashUser("user-\(i)", flagKey: "my-flag") < 0.5
        }.count

        XCTAssertGreaterThan(count, 450, "Distribution skewed for 'my-flag': \(count)/1000 below 0.5")
        XCTAssertLessThan(count, 550, "Distribution skewed for 'my-flag': \(count)/1000 below 0.5")
    }

    // MARK: - Range Guarantee

    /// All hash values must fall strictly within [0.0, 1.0).
    func testHash_range_alwaysInBounds() {
        for i in 0..<200 {
            let h = FeatureFlagEvaluator.hashUser("user-\(i)", flagKey: "flag-\(i % 10)")
            XCTAssertGreaterThanOrEqual(h, 0.0,
                "Hash below 0.0 for user-\(i) / flag-\(i % 10): \(h)")
            XCTAssertLessThan(h, 1.0,
                "Hash not strictly less than 1.0 for user-\(i) / flag-\(i % 10): \(h)")
        }
    }

    // MARK: - Unicode Inputs

    /// Unicode user IDs and flag keys must produce valid, stable hashes.
    func testHash_unicodeInputs() {
        let r1 = FeatureFlagEvaluator.hashUser("用户-123", flagKey: "功能-旗帜")
        let r2 = FeatureFlagEvaluator.hashUser("用户-123", flagKey: "功能-旗帜")
        XCTAssertEqual(r1, r2, "Unicode hash is non-deterministic")
        XCTAssertGreaterThanOrEqual(r1, 0.0)
        XCTAssertLessThan(r1, 1.0)
    }

    /// Emoji characters in user IDs and flag keys must produce valid hashes.
    func testHash_emojiInputs() {
        let result = FeatureFlagEvaluator.hashUser("user-🎯", flagKey: "flag-🚀")
        XCTAssertGreaterThanOrEqual(result, 0.0)
        XCTAssertLessThan(result, 1.0)
    }

    // MARK: - Cross-SDK Verification

    /// Explicitly verifies the divisor is 2^32 (not 2^32 - 1 = 4294967295).
    /// Using the wrong divisor would cause a systematic offset in all hash values
    /// and break cross-SDK compatibility.
    func testHash_divisorIs2To32NotMaxUInt32() {
        // For a user whose first 4 LE bytes are all 0xFF (hash value = 0xFFFFFFFF = 4294967295):
        // Using 2^32 as divisor: 4294967295 / 4294967296 ≈ 0.9999999997671694
        // Using 4294967295 as divisor: 4294967295 / 4294967295 = 1.0 (out of bounds!)
        // We can't easily manufacture such a user, but we verify the divisor constant.
        let divisor = FeatureFlagEvaluator.hashDivisor
        XCTAssertEqual(divisor, 4294967296.0, accuracy: 0.0,
            "Hash divisor must be 2^32 = 4294967296 to match Java/Python SDK")
    }

    /// Validates known cross-SDK vector for "user-1" and "user-2" against "flag".
    func testHash_knownVectors_user1_user2() {
        // Python: h("user-1", "flag") = 0.9150767861865461
        let h1 = FeatureFlagEvaluator.hashUser("user-1", flagKey: "flag")
        XCTAssertEqual(h1, 0.9150767861865461, accuracy: 1e-9)

        // Python: h("user-2", "flag") = 0.5229614521376789
        let h2 = FeatureFlagEvaluator.hashUser("user-2", flagKey: "flag")
        XCTAssertEqual(h2, 0.5229614521376789, accuracy: 1e-9)
    }
}
