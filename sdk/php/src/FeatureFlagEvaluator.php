<?php

declare(strict_types=1);

namespace Controlvar;

/**
 * Cross-SDK consistent hash utility.
 *
 * Hash formula (must match all other SDKs):
 *   MD5("{userId}:{flagKey}") → first 4 bytes as little-endian uint32 → divide by 2^32
 *
 * Known test vector: hashUser("user-123", "my-flag") === 0.6927449859213084
 *
 * This is a utility only: since the rewiring onto the public API nothing in the
 * SDK buckets users locally — flag evaluation and experiment assignment are
 * decided by the server.
 */
class FeatureFlagEvaluator
{
    /**
     * Compute a deterministic bucket value in [0.0, 1.0) for the given user + flag combination.
     *
     * @param string $userId  User identifier
     * @param string $flagKey Feature flag key
     * @return float Value in [0.0, 1.0)
     */
    public static function hashUser(string $userId, string $flagKey): float
    {
        // Raw binary MD5 output
        $raw = md5("{$userId}:{$flagKey}", true);

        // Unpack first 4 bytes as unsigned little-endian 32-bit integer
        $unpacked = unpack('V', substr($raw, 0, 4));
        $v = $unpacked[1];

        // Divide by 2^32 to get a value in [0.0, 1.0)
        return $v / 4294967296.0;
    }
}
