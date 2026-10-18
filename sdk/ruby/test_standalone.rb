#!/usr/bin/env ruby
# frozen_string_literal: true

# Standalone test runner — no bundler or rspec required.
# Exercises core SDK functionality using stdlib only (no HTTP).
# Run: ruby test_standalone.rb

$LOAD_PATH.unshift File.join(__dir__, 'lib')
require 'controlvar'

failures = []

def assert_equal(description, expected, actual, failures)
  if (actual - expected).abs < 1e-10
    puts "  PASS: #{description}"
  else
    msg = "FAIL: #{description}\n       expected=#{expected}, got=#{actual}, delta=#{(actual - expected).abs}"
    puts "  #{msg}"
    failures << msg
  end
end

def assert_nil(description, actual, failures)
  if actual.nil?
    puts "  PASS: #{description}"
  else
    msg = "FAIL: #{description} — expected nil, got #{actual.inspect}"
    puts "  #{msg}"
    failures << msg
  end
end

def assert_true(description, actual, failures)
  if actual
    puts "  PASS: #{description}"
  else
    msg = "FAIL: #{description} — expected true, got #{actual.inspect}"
    puts "  #{msg}"
    failures << msg
  end
end

def assert_in_range(description, value, min, max, failures)
  if value >= min && value < max
    puts "  PASS: #{description}"
  else
    msg = "FAIL: #{description} — #{value} not in [#{min}, #{max})"
    puts "  #{msg}"
    failures << msg
  end
end

puts "=" * 60
puts "Controlvar Ruby SDK — Standalone Test Runner"
puts "=" * 60
puts

# ---------------------------------------------------------------------------
# Test group 1: Hash parity (CRITICAL cross-SDK test)
# ---------------------------------------------------------------------------
puts ">>> Hash Parity Tests"

result   = Controlvar::FeatureFlagEvaluator.hash_user("user-123", "my-flag")
expected = 0.6927449859213084
assert_equal(
  'hash_user("user-123", "my-flag") == 0.6927449859213084  [cross-SDK parity]',
  expected, result, failures
)

result2 = Controlvar::FeatureFlagEvaluator.hash_user("user-123", "my-flag")
assert_equal('Determinism: same call twice yields identical result', result, result2, failures)

v1 = Controlvar::FeatureFlagEvaluator.hash_user("user-456", "my-flag")
assert_in_range('hash_user("user-456", "my-flag") is in [0.0, 1.0)', v1, 0.0, 1.0, failures)

v_empty = Controlvar::FeatureFlagEvaluator.hash_user("", "")
assert_in_range('hash_user("", "") is in [0.0, 1.0)', v_empty, 0.0, 1.0, failures)

assert_true('Local evaluation was removed (server decides)',
  !Controlvar::FeatureFlagEvaluator.respond_to?(:evaluate), failures)

puts

# ---------------------------------------------------------------------------
# Test group 2: Result types
# ---------------------------------------------------------------------------
puts ">>> Result Type Tests"

flag = Controlvar::FlagEvaluation.new(key: 'dark-mode', enabled: true, config: { 'theme' => 'dark' })
assert_true('FlagEvaluation#enabled? reflects enabled', flag.enabled? == true, failures)
assert_true('FlagEvaluation#to_h exposes key/enabled/config',
  flag.to_h == { key: 'dark-mode', enabled: true, config: { 'theme' => 'dark' } }, failures)

assignment = Controlvar::Assignment.new(
  experiment_key: 'exp', variant_id: 'v1', variant_name: 'control', is_control: true, configuration: nil
)
assert_true('Assignment#control? reflects is_control', assignment.control? == true, failures)
assert_true('Assignment exposes variant_name', assignment.variant_name == 'control', failures)

batch = Controlvar::BatchResult.new(success_count: 2, failure_count: 0, errors: nil)
assert_true('BatchResult#ok? is true without failures', batch.ok?, failures)

puts

# ---------------------------------------------------------------------------
# Test group 3: Cache
# ---------------------------------------------------------------------------
puts ">>> Cache Tests"

cache = Controlvar::Cache.new(ttl: 60, max_size: 3)
assert_nil('Cache: get returns nil for missing key', cache.get('x'), failures)
cache.set('x', 42)
val = cache.get('x')
assert_true('Cache: get returns stored value', val == 42, failures)
cache.clear
assert_nil('Cache: clear removes all entries', cache.get('x'), failures)

# Eviction test
3.times { |i| cache.set("k#{i}", i) }
cache.set('k3', 99)  # should evict k0
evicted = cache.get('k0')
assert_nil('Cache: evicts oldest when at max_size', evicted, failures)
kept = cache.get('k3')
assert_true('Cache: new entry kept after eviction', kept == 99, failures)

# Per-user listing
cache.clear
cache.set([:flag, 'u1', 'a'], 'A')
cache.set([:flag, 'u2', 'b'], 'B')
listed = cache.live_values { |key| key[1] == 'u1' }
assert_true('Cache: live_values lists entries for one user', listed == ['A'], failures)

puts

# ---------------------------------------------------------------------------
# Test group 4: Errors hierarchy
# ---------------------------------------------------------------------------
puts ">>> Error Hierarchy Tests"

assert_true('NetworkError is-a Error',
  Controlvar::NetworkError.ancestors.include?(Controlvar::Error),
  failures)
assert_true('APIError is-a Error',
  Controlvar::APIError.ancestors.include?(Controlvar::Error),
  failures)
assert_true('AuthenticationError is-a APIError',
  Controlvar::AuthenticationError.ancestors.include?(Controlvar::APIError),
  failures)

err = Controlvar::APIError.new("bad request", status_code: 400)
assert_true('APIError#status_code returns 400', err.status_code == 400, failures)

puts

# ---------------------------------------------------------------------------
# Test group 5: Config validation
# ---------------------------------------------------------------------------
puts ">>> Config Validation Tests"

begin
  Controlvar::SdkConfig.new(api_key: 'key').validate!
  failures << "FAIL: should raise for missing base_url"
rescue ArgumentError
  puts "  PASS: Raises ArgumentError for missing base_url"
end

begin
  Controlvar::SdkConfig.new(base_url: 'http://example.com').validate!
  failures << "FAIL: should raise for missing api_key"
rescue ArgumentError
  puts "  PASS: Raises ArgumentError for missing api_key"
end

begin
  Controlvar::SdkConfig.new(base_url: 'http://example.com', api_key: 'k').validate!
  puts "  PASS: Valid config does not raise"
rescue ArgumentError => e
  failures << "FAIL: Valid config raised: #{e.message}"
end

puts

# ---------------------------------------------------------------------------
# Test group 6: track never raises (no backend reachable)
# ---------------------------------------------------------------------------
puts ">>> Track Safety Tests"

client = Controlvar::Client.new(base_url: 'http://127.0.0.1:9', api_key: 'k', timeout: 1)
begin
  sent = client.track('page_view', 'user-1')
  assert_true('track without a key and nothing cached sends nothing and returns true', sent == true, failures)
  sent = client.track('purchase', 'user-1', experiment_key: 'exp', value: 1.0)
  assert_true('track with a key against an unreachable host returns false (no raise)', sent == false, failures)
rescue StandardError => e
  failures << "FAIL: track raised #{e.class}: #{e.message}"
end

puts

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
puts "=" * 60
if failures.empty?
  puts "All standalone tests PASSED"
  puts
  puts "Cross-SDK hash parity CONFIRMED:"
  puts "  hash_user(\"user-123\", \"my-flag\") = #{Controlvar::FeatureFlagEvaluator.hash_user('user-123', 'my-flag')}"
  puts "  Expected:                          0.6927449859213084"
  puts "=" * 60
  exit 0
else
  puts "#{failures.length} test(s) FAILED:"
  failures.each { |f| puts "  - #{f}" }
  puts "=" * 60
  exit 1
end
