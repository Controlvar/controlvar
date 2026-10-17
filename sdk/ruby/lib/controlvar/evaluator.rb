require 'digest'

module Controlvar
  # Cross-SDK consistent hash utility.
  #
  #   MD5("{user_id}:{flag_key}") -> first 4 bytes as little-endian uint32 / 2^32
  #
  # Every SDK exports this function and the golden-vector tests in
  # tests/sdk-contract/ pin its output. It is a utility only: since the
  # rewiring onto the public API nothing in the SDK buckets users locally —
  # flag evaluation and experiment assignment are decided by the server.
  class FeatureFlagEvaluator
    # Compute a deterministic bucket value in [0.0, 1.0) for the given user+key pair.
    #
    # Cross-SDK test vector:
    #   hash_user("user-123", "my-flag") == 0.6927449859213084
    #
    # @param user_id  [String] user identifier
    # @param flag_key [String] feature flag or experiment key
    # @return [Float] value in [0.0, 1.0)
    def self.hash_user(user_id, flag_key)
      bytes = Digest::MD5.digest("#{user_id}:#{flag_key}")
      # 'V' unpacks the first 4 bytes as a little-endian unsigned 32-bit integer
      v = bytes[0, 4].unpack1('V')
      v / 4_294_967_296.0
    end
  end
end
