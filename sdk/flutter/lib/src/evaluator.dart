/// MD5 consistent-hash utility shared by every platform SDK.
///
/// Flag evaluation and experiment assignment are decided **by the server**;
/// nothing in this SDK uses the hash to pick a variant any more. It is kept
/// as an exported utility so the cross-SDK golden-vector tests keep passing
/// and applications can reproduce server bucketing for debugging.
///
/// ## Hash Algorithm
/// Byte-for-byte compatible with the Java, Python, Go, iOS, Android and .NET SDKs:
///
/// 1. Compute MD5 of `"{userId}:{flagKey}"` encoded as UTF-8.
/// 2. Read the first 4 bytes as a **little-endian unsigned 32-bit integer**.
/// 3. Divide by **4294967296.0** (2^32 = 0x100000000) to normalise to `[0.0, 1.0)`.
///
/// The divisor is 2^32 **not** `MaxUInt32` (4294967295).
library controlvar_evaluator;

import 'dart:convert';
import 'dart:typed_data';
import 'package:crypto/crypto.dart';

/// Divisor used to normalise the 32-bit hash to [0.0, 1.0).
/// Equals 2^32 = 4294967296.0.
/// This MUST NOT be changed to 4294967295 (MaxUInt32); doing so would break
/// cross-SDK hash parity.
const double _hashDivisor = 4294967296.0;

/// Computes a deterministic, normalised hash value in `[0.0, 1.0)` for the
/// given [userId] and [flagKey] pair.
///
/// ```
/// MD5("{userId}:{flagKey}") → first 4 bytes as little-endian uint32 → ÷ 2^32
/// ```
///
/// Example:
/// ```dart
/// final h = hashUser('user-123', 'my-flag');
/// // h ≈ 0.6927449859  (verified against Java/Go/iOS/Android SDKs)
/// ```
double hashUser(String userId, String flagKey) {
  final input = '$userId:$flagKey';
  final bytes = utf8.encode(input);
  final digest = md5.convert(bytes);

  // Read the first 4 bytes of the digest as a little-endian unsigned 32-bit
  // integer.  Using ByteData.getUint32 with Endian.little matches:
  //   Java:   (bytes[0] & 0xFFL) | ((bytes[1] & 0xFFL) << 8) | ...
  //   Python: struct.unpack('<I', digest[:4])[0]
  //   Go:     binary.LittleEndian.Uint32(digest[:4])
  //   Swift:  UInt32(d[0]) | UInt32(d[1])<<8 | UInt32(d[2])<<16 | UInt32(d[3])<<24
  final buf = Uint8List.fromList(digest.bytes.sublist(0, 4));
  final value = buf.buffer.asByteData().getUint32(0, Endian.little);

  // Divide by 2^32 (not 2^32-1) to produce a value in [0.0, 1.0).
  return value / _hashDivisor;
}
