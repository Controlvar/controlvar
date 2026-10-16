package com.controlvar.sdk;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.*;

/**
 * Unit tests for {@link ConsistentHash}.
 *
 * <p>Cross-validates the Java port against the golden vectors shared by every SDK
 * ({@code tests/sdk-contract/golden-vectors.json}) and the Python reference
 * ({@code consistent_hash.py}: MD5 of {@code "{userId}:{key}"}, first 4 bytes
 * little-endian, divided by 2^32).
 */
@DisplayName("ConsistentHash")
class ConsistentHashTest {

    private static final double DELTA = 1e-12;

    /** {user_id, flag_key, expected_hash} from tests/sdk-contract/golden-vectors.json. */
    private static final Object[][] GOLDEN_VECTORS = {
            {"user-123", "my-flag", 0.6927449859213084},
            {"alice", "dark-mode", 0.0353864398784935},
            {"bob", "new-checkout", 0.1463384565431625},
            {"user-789", "beta-feature", 0.3134219283238053},
            {"test-user", "flag-key", 0.9923393740318716},
            {"", "empty-user", 0.3582690393086523},
            {"a", "b", 0.6056532170623541},
            {"user-001", "exp-abc", 0.7764157797209918},
            {"user-002", "exp-abc", 0.1422550100833178},
    };

    @Test
    @DisplayName("matches the cross-SDK golden vectors")
    void matchesGoldenVectors() {
        for (Object[] vector : GOLDEN_VECTORS) {
            String userId = (String) vector[0];
            String key = (String) vector[1];
            double expected = (Double) vector[2];
            assertEquals(expected, ConsistentHash.compute(userId, key), DELTA,
                    "ConsistentHash.compute(\"" + userId + "\", \"" + key + "\")");
        }
    }

    @Test
    @DisplayName("matches the Python consistent_hash.py reference values")
    void matchesPythonReference() {
        double delta = 1e-9;
        assertEquals(0.2281730450, ConsistentHash.compute("user1", "flag1"), delta);
        assertEquals(0.1742990911, ConsistentHash.compute("user-123", "my-feature"), delta);
        assertEquals(0.9276319703, ConsistentHash.compute("alice", "checkout-v2"), delta);
        assertEquals(0.9467693546, ConsistentHash.compute("bob", "checkout-v2"), delta);
    }

    @Test
    @DisplayName("uses little-endian byte order")
    void usesLittleEndianByteOrder() {
        // Raw little-endian value for ("user1", "flag1") is 979995766.
        assertEquals(979995766.0 / ConsistentHash.HASH_DIVISOR, ConsistentHash.compute("user1", "flag1"), 1e-10);
    }

    @Test
    @DisplayName("is deterministic")
    void isDeterministic() {
        double first = ConsistentHash.compute("user-repeat", "consistency-test");
        for (int i = 0; i < 100; i++) {
            assertEquals(first, ConsistentHash.compute("user-repeat", "consistency-test"));
        }
    }

    @Test
    @DisplayName("stays in [0.0, 1.0) and is roughly uniform")
    void rangeAndDistribution() {
        int below = 0;
        int total = 10_000;
        for (int i = 0; i < total; i++) {
            double h = ConsistentHash.compute("dist-user-" + i, "dist-flag");
            assertTrue(h >= 0.0 && h < 1.0, "hash out of range: " + h);
            if (h < 0.5) below++;
        }
        double fraction = below / (double) total;
        assertTrue(fraction > 0.45 && fraction < 0.55, "distribution skewed: " + fraction);
    }

    @Test
    @DisplayName("handles empty strings and unicode")
    void edgeCases() {
        double empty = ConsistentHash.compute("", "");
        assertTrue(empty >= 0.0 && empty < 1.0);
        double unicode = ConsistentHash.compute("用户-123", "flag1");
        assertTrue(unicode >= 0.0 && unicode < 1.0);
        assertNotEquals(ConsistentHash.compute("alice", "k"), ConsistentHash.compute("bob", "k"));
        assertNotEquals(ConsistentHash.compute("u", "k1"), ConsistentHash.compute("u", "k2"));
    }

    @Test
    @SuppressWarnings("deprecation")
    @DisplayName("the deprecated FeatureFlagEvaluator.computeHash delegates to ConsistentHash")
    void deprecatedEvaluatorDelegates() {
        FeatureFlagEvaluator evaluator = new FeatureFlagEvaluator();
        assertEquals(ConsistentHash.compute("user-123", "my-flag"), evaluator.computeHash("user-123", "my-flag"));
        assertEquals(ConsistentHash.HASH_DIVISOR, FeatureFlagEvaluator.HASH_DIVISOR);
    }
}
