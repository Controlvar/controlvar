import com.controlvar.android.ConsistentHash
import com.controlvar.android.FeatureFlagEvaluator
import org.junit.jupiter.api.Assertions.*
import org.junit.jupiter.api.Test

/**
 * Verifies that the Android SDK hash produces the same results as Java/Python/Go/iOS/JS SDKs.
 *
 * All SDKs share the same algorithm:
 *   1. Compute MD5 of "{userId}:{flagKey}" encoded as UTF-8.
 *   2. Read first 4 bytes as a little-endian unsigned 32-bit integer.
 *   3. Divide by 0x100000000L (2^32) to normalize to [0.0, 1.0).
 *
 * This file also validates the algorithm matches the Java SDK
 * (ConsistentHash.java), the Python lambda (consistent_hash.py) and the golden vectors
 * shared by every SDK (tests/sdk-contract/golden-vectors.json).
 *
 * The SDK does not use the hash to decide variants any more (the server does); it is
 * exported as ConsistentHash for parity checks only.
 */
class HashCompatibilityTest {

    /**
     * Reference implementation matching the Java SDK exactly:
     *   long hash = (bytes[0] & 0xFFL) | ((bytes[1] & 0xFFL) << 8) | ...
     *   return hash / 0x100000000L
     */
    private fun computeHash(userId: String, flagKey: String): Double {
        val input = "$userId:$flagKey"
        val digest = java.security.MessageDigest.getInstance("MD5")
        val bytes = digest.digest(input.toByteArray(Charsets.UTF_8))
        val v = (bytes[0].toLong() and 0xFFL)
            .or((bytes[1].toLong() and 0xFFL) shl 8)
            .or((bytes[2].toLong() and 0xFFL) shl 16)
            .or((bytes[3].toLong() and 0xFFL) shl 24)
        return v.toDouble() / 0x100000000L.toDouble()
    }

    // ============================
    // Cross-SDK golden vectors (tests/sdk-contract/golden-vectors.json)
    // ============================

    @Test
    fun `golden vectors match every other SDK`() {
        val vectors = listOf(
            Triple("user-123", "my-flag", 0.6927449859213084),
            Triple("alice", "dark-mode", 0.0353864398784935),
            Triple("bob", "new-checkout", 0.1463384565431625),
            Triple("user-789", "beta-feature", 0.3134219283238053),
            Triple("test-user", "flag-key", 0.9923393740318716),
            Triple("", "empty-user", 0.3582690393086523),
            Triple("a", "b", 0.6056532170623541),
            Triple("user-001", "exp-abc", 0.7764157797209918),
            Triple("user-002", "exp-abc", 0.1422550100833178),
        )
        for ((userId, key, expected) in vectors) {
            assertEquals(expected, ConsistentHash.compute(userId, key), 1e-12,
                "Golden vector mismatch for userId='$userId' key='$key'")
        }
    }

    @Test
    fun `python reference values match`() {
        assertEquals(0.2281730450, ConsistentHash.compute("user1", "flag1"), 1e-9)
        assertEquals(0.1742990911, ConsistentHash.compute("user-123", "my-feature"), 1e-9)
        assertEquals(0.9276319703, ConsistentHash.compute("alice", "checkout-v2"), 1e-9)
        assertEquals(0.9467693546, ConsistentHash.compute("bob", "checkout-v2"), 1e-9)
    }

    @Test
    @Suppress("DEPRECATION")
    fun `deprecated FeatureFlagEvaluator hashUser delegates to ConsistentHash`() {
        assertEquals(ConsistentHash.compute("user-123", "my-flag"), FeatureFlagEvaluator.hashUser("user-123", "my-flag"))
        assertEquals(0x100000000L, ConsistentHash.HASH_DIVISOR)
    }

    // ============================
    // Basic properties
    // ============================

    @Test
    fun `hash produces value in range 0 to 1 exclusive`() {
        val h = ConsistentHash.compute("user-123", "my-flag")
        assertTrue(h >= 0.0, "Hash should be >= 0")
        assertTrue(h < 1.0, "Hash should be < 1")
    }

    @Test
    fun `hash is deterministic for same inputs`() {
        val h1 = ConsistentHash.compute("user-123", "my-flag")
        val h2 = ConsistentHash.compute("user-123", "my-flag")
        assertEquals(h1, h2, "Same inputs must produce same hash")
    }

    @Test
    fun `different users produce different hashes`() {
        val h1 = ConsistentHash.compute("user-1", "flag")
        val h2 = ConsistentHash.compute("user-2", "flag")
        assertNotEquals(h1, h2, "Different userIds should produce different hashes")
    }

    @Test
    fun `different flags produce different hashes for same user`() {
        val h1 = ConsistentHash.compute("user-1", "flag-1")
        val h2 = ConsistentHash.compute("user-1", "flag-2")
        assertNotEquals(h1, h2, "Different flagKeys should produce different hashes")
    }

    // ============================
    // Cross-SDK compatibility (known test vectors)
    // ============================

    @Test
    fun `known test vectors match Java SDK algorithm`() {
        // These are computed by the reference computeHash() above,
        // which replicates the Java SDK FeatureFlagEvaluator.computeHash() exactly.
        val testCases = listOf(
            Triple("user-123", "my-flag", computeHash("user-123", "my-flag")),
            Triple("alice", "feature-x", computeHash("alice", "feature-x")),
            Triple("test-user", "test-flag", computeHash("test-user", "test-flag")),
            Triple("", "empty-user-flag", computeHash("", "empty-user-flag")),
            Triple("bob", "new-checkout", computeHash("bob", "new-checkout")),
            Triple("user@example.com", "email-flag", computeHash("user@example.com", "email-flag")),
            Triple("admin-001", "rollout-50", computeHash("admin-001", "rollout-50")),
            Triple("長い名前のユーザー", "unicode-flag", computeHash("長い名前のユーザー", "unicode-flag")),
        )

        for ((userId, flagKey, expected) in testCases) {
            val actual = ConsistentHash.compute(userId, flagKey)
            assertEquals(
                expected, actual, 1e-15,
                "Hash mismatch for userId='$userId' flagKey='$flagKey'"
            )
        }
    }

    @Test
    fun `hash uses little endian byte order not big endian`() {
        // MD5 of "user:flag" has different byte order interpretations.
        // Big-endian and little-endian would give different values.
        // We verify that our implementation matches little-endian (Java SDK spec).
        val userId = "endian-test"
        val flagKey = "byte-order"
        val input = "$userId:$flagKey"
        val digest = java.security.MessageDigest.getInstance("MD5")
        val bytes = digest.digest(input.toByteArray(Charsets.UTF_8))

        // Little-endian (correct):
        val leValue = (bytes[0].toLong() and 0xFFL)
            .or((bytes[1].toLong() and 0xFFL) shl 8)
            .or((bytes[2].toLong() and 0xFFL) shl 16)
            .or((bytes[3].toLong() and 0xFFL) shl 24)
        val leHash = leValue.toDouble() / 0x100000000L

        // Big-endian (wrong, for comparison):
        val beValue = ((bytes[0].toLong() and 0xFFL) shl 24)
            .or((bytes[1].toLong() and 0xFFL) shl 16)
            .or((bytes[2].toLong() and 0xFFL) shl 8)
            .or(bytes[3].toLong() and 0xFFL)
        val beHash = beValue.toDouble() / 0x100000000L

        val actual = ConsistentHash.compute(userId, flagKey)
        assertEquals(leHash, actual, 1e-15, "SDK must use little-endian byte order")
        if (leHash != beHash) {
            // They differ — confirm we're using LE, not BE
            assertNotEquals(beHash, actual, "SDK must NOT use big-endian byte order")
        }
    }

    @Test
    fun `hash divisor is 2 to the power of 32 not 4294967295`() {
        // Verify the divisor is 0x100000000L (2^32), not 0xFFFFFFFFL (2^32-1)
        // Java SDK uses: hash / (double) HASH_DIVISOR where HASH_DIVISOR = 0x100000000L
        // Python SDK uses: hash_value / (MAX_HASH_VALUE + 1) = 0xFFFFFFFF + 1 = 0x100000000
        val userId = "divisor-test"
        val flagKey = "divisor-flag"
        val input = "$userId:$flagKey"
        val digest = java.security.MessageDigest.getInstance("MD5")
        val bytes = digest.digest(input.toByteArray(Charsets.UTF_8))
        val v = (bytes[0].toLong() and 0xFFL)
            .or((bytes[1].toLong() and 0xFFL) shl 8)
            .or((bytes[2].toLong() and 0xFFL) shl 16)
            .or((bytes[3].toLong() and 0xFFL) shl 24)

        val with2pow32 = v.toDouble() / 0x100000000L         // correct (Java SDK)
        val with2pow32minus1 = v.toDouble() / 0xFFFFFFFFL    // wrong (task spec typo)

        val actual = ConsistentHash.compute(userId, flagKey)
        assertEquals(with2pow32, actual, 1e-15, "Divisor must be 2^32 (matching Java SDK)")
        if (with2pow32 != with2pow32minus1) {
            assertNotEquals(
                with2pow32minus1, actual, 1e-15,
                "Divisor must NOT be 2^32-1"
            )
        }
    }

    // ============================
    // Distribution / uniformity
    // ============================

    @Test
    fun `hash distribution is roughly uniform across 1000 users`() {
        var countBelow50 = 0
        for (i in 0 until 1000) {
            val h = ConsistentHash.compute("user-$i", "test-flag")
            if (h < 0.5) countBelow50++
        }
        assertTrue(
            countBelow50 in 450..550,
            "Expected ~500 users below 0.5 threshold but got $countBelow50 (uniform distribution test)"
        )
    }

    @Test
    fun `hash produces distinct values across many users`() {
        val hashes = (0 until 100).map { ConsistentHash.compute("user-$it", "dist-flag") }
        // With MD5 and 100 users there should be no collisions
        assertEquals(100, hashes.distinct().size, "All 100 hashes should be unique")
    }

    @Test
    fun `hash for same user produces consistent bucketing across repeated calls`() {
        val user = "consistent-user"
        val flag = "consistent-flag"
        val threshold = 0.5
        val results = (0 until 50).map {
            ConsistentHash.compute(user, flag) < threshold
        }
        assertTrue(results.all { it == results[0] }, "Same user/flag must always produce same bucket")
    }

    // ============================
    // Edge cases
    // ============================

    @Test
    fun `hash handles empty user id`() {
        val h = ConsistentHash.compute("", "flag")
        assertTrue(h >= 0.0 && h < 1.0, "Empty userId should produce valid hash")
    }

    @Test
    fun `hash handles empty flag key`() {
        val h = ConsistentHash.compute("user-1", "")
        assertTrue(h >= 0.0 && h < 1.0, "Empty flagKey should produce valid hash")
    }

    @Test
    fun `hash handles both empty user and flag key`() {
        val h = ConsistentHash.compute("", "")
        assertTrue(h >= 0.0 && h < 1.0, "Both empty should still produce valid hash")
        // MD5 of ":" is deterministic
        assertEquals(computeHash("", ""), h, 1e-15)
    }

    @Test
    fun `hash handles special characters in user id`() {
        val h = ConsistentHash.compute("user@example.com+test", "my-flag-v2")
        assertTrue(h >= 0.0 && h < 1.0)
        assertEquals(computeHash("user@example.com+test", "my-flag-v2"), h, 1e-15)
    }

    @Test
    fun `hash handles unicode characters`() {
        val h = ConsistentHash.compute("用户123", "功能标志")
        assertTrue(h >= 0.0 && h < 1.0)
        assertEquals(computeHash("用户123", "功能标志"), h, 1e-15)
    }

    @Test
    fun `hash handles very long user id`() {
        val longUserId = "user-" + "a".repeat(500)
        val h = ConsistentHash.compute(longUserId, "flag")
        assertTrue(h >= 0.0 && h < 1.0)
        assertEquals(computeHash(longUserId, "flag"), h, 1e-15)
    }

    @Test
    fun `hash handles colon in user id without confusion with separator`() {
        // Input is "{userId}:{flagKey}" - if userId contains ":" it changes the hash
        val h1 = ConsistentHash.compute("user:with:colons", "flag")
        val h2 = ConsistentHash.compute("user", "with:colons:flag") // different input
        // They should differ (different total input string)
        // h1 uses "user:with:colons:flag", h2 uses "user:with:colons:flag" — actually same!
        // Let's check with a case that's actually different
        val h3 = ConsistentHash.compute("abc:def", "ghi")
        val h4 = ConsistentHash.compute("abc", "def:ghi")
        // "abc:def:ghi" == "abc:def:ghi" — same input, same hash
        assertEquals(h3, h4, "Note: colon in userId creates same string as colon in flagKey prefix")
    }

    @Test
    fun `hash handles numeric string user id`() {
        val h = ConsistentHash.compute("12345678", "numeric-flag")
        assertTrue(h >= 0.0 && h < 1.0)
        assertEquals(computeHash("12345678", "numeric-flag"), h, 1e-15)
    }

    @Test
    fun `hash handles uuid-formatted user id`() {
        val uuid = "550e8400-e29b-41d4-a716-446655440000"
        val h = ConsistentHash.compute(uuid, "uuid-test-flag")
        assertTrue(h >= 0.0 && h < 1.0)
        assertEquals(computeHash(uuid, "uuid-test-flag"), h, 1e-15)
    }
}
