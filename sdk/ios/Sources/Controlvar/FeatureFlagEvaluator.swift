import Foundation
import CommonCrypto

/// MD5 consistent-hash utility shared by every platform SDK.
///
/// Flag evaluation and experiment assignment are decided **by the server**; nothing in this
/// SDK uses the hash to pick a variant any more. It is kept as an exported utility so the
/// cross-SDK golden-vector tests keep passing and applications can reproduce server bucketing
/// for debugging.
///
/// ## Hash Algorithm
/// Byte-for-byte compatible with the Java, Python, JavaScript, Go, Kotlin, Dart and .NET SDKs
/// and the backend Lambda `consistent_hash.py`:
///
/// 1. Compute MD5 of `"{userId}:{flagKey}"` encoded as UTF-8.
/// 2. Read the first 4 bytes as a little-endian unsigned 32-bit integer.
/// 3. Divide by `0x100000000` (2^32) to normalize to `[0.0, 1.0)`.
public struct FeatureFlagEvaluator {

    /// Divisor used to normalize the 32-bit hash value into [0.0, 1.0).
    /// Equals 2^32 = 4294967296, matching Python's `MAX_HASH_VALUE + 1`.
    static let hashDivisor: Double = 0x100000000  // 4294967296.0

    /// Computes a deterministic, normalized hash value in `[0.0, 1.0)` for the
    /// given user ID and flag key combination.
    ///
    /// - Parameters:
    ///   - userId: The user's unique identifier.
    ///   - flagKey: The feature flag's unique key (used as salt).
    /// - Returns: A value in `[0.0, 1.0)`.
    public static func hashUser(_ userId: String, flagKey: String) -> Double {
        let input = "\(userId):\(flagKey)"
        guard let data = input.data(using: .utf8) else { return 0.0 }

        var digest = [UInt8](repeating: 0, count: Int(CC_MD5_DIGEST_LENGTH))
        data.withUnsafeBytes { ptr in
            _ = CC_MD5(ptr.baseAddress, CC_LONG(data.count), &digest)
        }

        // Read first 4 bytes as little-endian unsigned 32-bit integer.
        // This matches Java:  ((bytes[0] & 0xFF)) | ((bytes[1] & 0xFF) << 8) | ...
        // And Python: struct.unpack('<I', hash_bytes[:4])[0]
        let v = UInt32(digest[0])
            | UInt32(digest[1]) << 8
            | UInt32(digest[2]) << 16
            | UInt32(digest[3]) << 24

        // Divide by 2^32 (not 2^32 - 1) to normalize to [0.0, 1.0).
        return Double(v) / hashDivisor
    }
}
