require 'spec_helper'

# Cross-SDK Hash Compatibility Tests
#
# These tests verify that the Ruby SDK's consistent hashing implementation
# produces identical bucket values to all other SDKs (JavaScript, Python, Java,
# Go, Flutter, React Native, etc.).
#
# The formula: MD5("{user_id}:{flag_key}") -> first 4 bytes as little-endian
# uint32 / 4294967296.0 (2^32)
#
# Known test vector (verified against JS, Python, Java, Go SDKs):
#   hash_user("user-123", "my-flag") == 0.6927449859213084
RSpec.describe 'Cross-SDK Hash Compatibility' do
  let(:evaluator) { Controlvar::FeatureFlagEvaluator }

  # -------------------------------------------------------------------------
  # Primary parity test — the authoritative cross-SDK vector
  # -------------------------------------------------------------------------
  it 'hash_user("user-123", "my-flag") equals the cross-SDK reference value 0.6927449859213084' do
    result   = evaluator.hash_user('user-123', 'my-flag')
    expected = 0.6927449859213084

    expect(result).to be_within(1e-10).of(expected),
      "Cross-SDK hash parity FAILED.\n" \
      "  Expected: #{expected}\n" \
      "  Got:      #{result}\n" \
      "  Delta:    #{(result - expected).abs}"
  end

  # -------------------------------------------------------------------------
  # Additional parity / distribution checks
  # -------------------------------------------------------------------------
  it 'hash_user("user-456", "my-flag") returns a Float in [0, 1)' do
    result = evaluator.hash_user('user-456', 'my-flag')
    expect(result).to be_a(Float)
    expect(result).to be >= 0.0
    expect(result).to be < 1.0
  end

  it 'hash_user("user-123", "other-flag") differs from hash_user("user-123", "my-flag")' do
    r1 = evaluator.hash_user('user-123', 'my-flag')
    r2 = evaluator.hash_user('user-123', 'other-flag')
    expect(r1).not_to eq(r2)
  end

  it 'hash_user("", "") does not raise' do
    expect { evaluator.hash_user('', '') }.not_to raise_error
  end

  it 'hash_user with a unicode user_id does not raise' do
    expect { evaluator.hash_user('ユーザー-123', 'my-flag') }.not_to raise_error
  end

  # -------------------------------------------------------------------------
  # Distribution sanity: values should spread across [0,1)
  # -------------------------------------------------------------------------
  it 'produces well-distributed values across a sample of 1000 users' do
    results = (1..1000).map { |i| evaluator.hash_user("user-#{i}", 'distribution-test') }

    avg  = results.sum / results.length
    # With a uniform distribution over [0,1) the mean should be near 0.5
    expect(avg).to be_within(0.05).of(0.5),
      "Distribution mean #{avg} is not near 0.5 — hash may not be uniform"
  end

  # -------------------------------------------------------------------------
  # Verify little-endian byte order is used (not big-endian)
  # -------------------------------------------------------------------------
  it 'uses little-endian uint32 byte order (not big-endian)' do
    # The cross-SDK reference value of 0.6927449859213084 is produced ONLY with
    # little-endian ('V') byte order. Big-endian ('N') yields a different value.
    result_le = Controlvar::FeatureFlagEvaluator.hash_user('user-123', 'my-flag')
    expected  = 0.6927449859213084

    # Compute what big-endian would give
    require 'digest'
    bytes     = Digest::MD5.digest('user-123:my-flag')
    v_be      = bytes[0, 4].unpack1('N')  # big-endian
    result_be = v_be / 4_294_967_296.0

    expect(result_le).to be_within(1e-10).of(expected)
    expect(result_le).not_to be_within(1e-10).of(result_be)
  end
end
