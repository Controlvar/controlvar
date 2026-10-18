package com.controlvar.sdk;

/**
 * Source-compatibility shim for the former local evaluator.
 *
 * <p>Flags are evaluated by the server now; only the hash survives, delegating
 * to {@link ConsistentHash}.
 *
 * @deprecated use {@link ConsistentHash#compute(String, String)}.
 */
@Deprecated
public class FeatureFlagEvaluator {

    /** @deprecated use {@link ConsistentHash#HASH_DIVISOR}. */
    @Deprecated
    static final long HASH_DIVISOR = ConsistentHash.HASH_DIVISOR;

    /**
     * Computes a normalized hash value in [0.0, 1.0) for the given user ID and salt.
     *
     * @deprecated use {@link ConsistentHash#compute(String, String)}.
     */
    @Deprecated
    public double computeHash(String userId, String salt) {
        return ConsistentHash.compute(userId, salt);
    }
}
