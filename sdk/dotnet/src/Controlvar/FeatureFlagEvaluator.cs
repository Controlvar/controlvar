using System.Security.Cryptography;
using System.Text;

namespace Controlvar;

/// <summary>
/// MD5 consistent-hash utility shared by every platform SDK.
///
/// Flag evaluation and experiment assignment are decided <b>by the server</b>; nothing in this
/// SDK uses the hash to pick a variant any more. It is kept as an exported utility so the
/// cross-SDK golden-vector tests keep passing and applications can reproduce server bucketing
/// for debugging.
/// </summary>
public static class FeatureFlagEvaluator
{
    /// <summary>Divisor used to normalise the 32-bit hash to [0.0, 1.0): 2^32 (not 2^32 - 1).</summary>
    public const double HashDivisor = 4294967296.0;

    /// <summary>
    /// Computes a deterministic bucket value in [0.0, 1.0) for a user/flag pair.
    ///
    /// Algorithm: MD5("{userId}:{flagKey}") → first 4 bytes as little-endian uint32 → divide by 2^32.
    ///
    /// Cross-SDK test vector: HashUser("user-123", "my-flag") == 0.6927449859213084
    /// </summary>
    /// <param name="userId">The user identifier.</param>
    /// <param name="flagKey">The feature flag key.</param>
    /// <returns>A value in [0.0, 1.0).</returns>
    public static double HashUser(string userId, string flagKey)
    {
        byte[] input = Encoding.UTF8.GetBytes($"{userId}:{flagKey}");
        byte[] hash;
        using (var md5 = MD5.Create())
        {
            hash = md5.ComputeHash(input);
        }

        // Explicit little-endian reconstruction for cross-platform portability.
        // BitConverter.ToUInt32 is architecture-dependent on big-endian systems.
        uint v = (uint)(hash[0] | (hash[1] << 8) | (hash[2] << 16) | (hash[3] << 24));
        return v / HashDivisor;
    }
}
