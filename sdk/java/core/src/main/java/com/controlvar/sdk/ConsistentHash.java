package com.controlvar.sdk;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;

/**
 * The platform's MD5 consistent hash, exported for cross-SDK parity checks.
 *
 * <p>The SDK no longer uses it to decide variants or rollouts (the server does);
 * it is kept because every platform SDK must produce identical values for the
 * golden vectors in {@code tests/sdk-contract/golden-vectors.json}.
 *
 * <h2>Algorithm</h2>
 * <ol>
 *   <li>Compute MD5 of {@code "{userId}:{key}"} encoded as UTF-8.</li>
 *   <li>Interpret the first 4 bytes as a <strong>little-endian</strong> unsigned 32-bit integer.</li>
 *   <li>Divide by {@code 0x100000000L} (2^32) to normalize to [0.0, 1.0).</li>
 * </ol>
 */
public final class ConsistentHash {

    /** 2^32: the divisor used to normalize the hash to [0.0, 1.0). */
    public static final long HASH_DIVISOR = 0x100000000L;

    private ConsistentHash() {}

    /**
     * Computes a normalized hash value in [0.0, 1.0) for the given user ID and key.
     *
     * @param userId the user's unique identifier
     * @param key    the flag or experiment key
     * @return normalized hash in [0.0, 1.0)
     * @throws IllegalStateException if MD5 is unexpectedly unavailable (never in practice)
     */
    public static double compute(String userId, String key) {
        try {
            String input = userId + ":" + key;
            MessageDigest md = MessageDigest.getInstance("MD5");
            byte[] bytes = md.digest(input.getBytes(StandardCharsets.UTF_8));

            // Little-endian unsigned 32-bit integer from the first 4 bytes
            // (Python: struct.unpack('<I', digest[:4])[0]).
            long hash = ((bytes[0] & 0xFFL))
                      | ((bytes[1] & 0xFFL) << 8)
                      | ((bytes[2] & 0xFFL) << 16)
                      | ((bytes[3] & 0xFFL) << 24);

            return hash / (double) HASH_DIVISOR;
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException("MD5 MessageDigest not available on this JVM", e);
        }
    }
}
