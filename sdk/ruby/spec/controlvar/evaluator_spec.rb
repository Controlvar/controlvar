require 'spec_helper'

RSpec.describe Controlvar::FeatureFlagEvaluator do
  describe '.hash_user' do
    # -----------------------------------------------------------------------
    # Cross-SDK hash parity
    # -----------------------------------------------------------------------
    it 'matches the known cross-SDK test vector for ("user-123", "my-flag")' do
      expected = 0.6927449859213084
      result   = described_class.hash_user('user-123', 'my-flag')
      expect(result).to be_within(1e-10).of(expected)
    end

    it 'returns a Float' do
      result = described_class.hash_user('user-abc', 'flag-xyz')
      expect(result).to be_a(Float)
    end

    it 'returns a value in [0.0, 1.0)' do
      result = described_class.hash_user('user-abc', 'flag-xyz')
      expect(result).to be >= 0.0
      expect(result).to be < 1.0
    end

    it 'is deterministic — same inputs always produce the same hash' do
      r1 = described_class.hash_user('user-stable', 'flag-stable')
      r2 = described_class.hash_user('user-stable', 'flag-stable')
      expect(r1).to eq(r2)
    end

    it 'produces different hashes for different user IDs' do
      r1 = described_class.hash_user('user-A', 'my-flag')
      r2 = described_class.hash_user('user-B', 'my-flag')
      expect(r1).not_to eq(r2)
    end

    it 'produces different hashes for different flag keys' do
      r1 = described_class.hash_user('user-123', 'flag-alpha')
      r2 = described_class.hash_user('user-123', 'flag-beta')
      expect(r1).not_to eq(r2)
    end

    it 'produces a value in range for ("user-456", "my-flag")' do
      result = described_class.hash_user('user-456', 'my-flag')
      expect(result).to be_between(0.0, 1.0).exclusive
    end

    it 'handles empty strings without raising' do
      expect { described_class.hash_user('', '') }.not_to raise_error
    end

    it 'handles unicode user IDs without raising' do
      expect { described_class.hash_user('用户-123', 'my-flag') }.not_to raise_error
    end

    it 'returns a Float in [0,1) for unicode input' do
      result = described_class.hash_user('用户-123', 'my-flag')
      expect(result).to be >= 0.0
      expect(result).to be < 1.0
    end
  end

  it 'no longer exposes local flag evaluation (the server decides)' do
    expect(described_class).not_to respond_to(:evaluate)
  end
end
