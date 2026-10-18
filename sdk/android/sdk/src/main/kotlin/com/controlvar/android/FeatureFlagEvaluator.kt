package com.controlvar.android

import java.security.MessageDigest

/**
 * The platform's MD5 consistent hash, exported for cross-SDK parity checks.
 *
 * The SDK no longer uses it to decide variants or rollouts (the server does); it is
 * kept because every platform SDK must produce identical values for the golden
 * vectors in `tests/sdk-contract/golden-vectors.json`.
 *
 * Hash spec (byte-for-byte compatible with Java/Python/Go/iOS/JS SDKs):
 *   1. Compute MD5 of "{userId}:{key}" encoded as UTF-8.
 *   2. Read first 4 bytes as a little-endian unsigned 32-bit integer.
 *   3. Divide by 0x100000000L (2^32) to normalize to [0.0, 1.0).
 */
object ConsistentHash {

    /**
     * Divisor used to normalize the hash to [0.0, 1.0).
     * Matches Java SDK: HASH_DIVISOR = 0x100000000L
     * Matches Python SDK: MAX_HASH_VALUE + 1 = 0xFFFFFFFF + 1
     */
    const val HASH_DIVISOR = 0x100000000L

    /**
     * Computes a deterministic double in [0, 1) for a user + key combination.
     *
     * Algorithm:
     *   input  = "$userId:$key" encoded as UTF-8
     *   bytes  = MD5(input)
     *   uint32 = bytes[0..3] interpreted as little-endian unsigned 32-bit int
     *   result = uint32 / 2^32
     */
    fun compute(userId: String, key: String): Double {
        val input = "$userId:$key"
        val digest = MessageDigest.getInstance("MD5")
        val bytes = digest.digest(input.toByteArray(Charsets.UTF_8))

        // Read first 4 bytes as little-endian unsigned 32-bit integer.
        val v = ((bytes[0].toLong() and 0xFFL))
            .or((bytes[1].toLong() and 0xFFL) shl 8)
            .or((bytes[2].toLong() and 0xFFL) shl 16)
            .or((bytes[3].toLong() and 0xFFL) shl 24)

        return v.toDouble() / HASH_DIVISOR.toDouble()
    }
}

/**
 * Source-compatibility shim for the former local evaluator. Flags are evaluated by
 * the server now; only the hash survives, delegating to [ConsistentHash].
 */
@Deprecated("Flags are evaluated by the server; use ConsistentHash for parity checks.")
object FeatureFlagEvaluator {

    /**
     * Computes a deterministic double in [0, 1) for a user + flag combination.
     */
    @Deprecated("Use ConsistentHash.compute", ReplaceWith("ConsistentHash.compute(userId, flagKey)"))
    fun hashUser(userId: String, flagKey: String): Double = ConsistentHash.compute(userId, flagKey)
}
