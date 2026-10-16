#!/usr/bin/env ruby
# frozen_string_literal: true

# basic_usage.rb — Controlvar Ruby SDK usage examples
#
# This file demonstrates the SDK API without making actual HTTP calls.
# For a runnable end-to-end check against a live backend see contract_smoke.rb.

$LOAD_PATH.unshift File.join(__dir__, '..', 'lib')
require 'controlvar'

puts "Controlvar Ruby SDK — Basic Usage Examples"
puts "=" * 55
puts

# ---------------------------------------------------------------------------
# 1. Creating a client
# ---------------------------------------------------------------------------
puts "1. Creating a client"
puts "-" * 30

# Option A: keyword arguments
client = Controlvar::Client.new(
  base_url:  ENV.fetch("EXPERIMENTLY_API_URL", "http://localhost:8000"),  # origin only
  api_key:   ENV.fetch("EXPERIMENTLY_API_KEY", "sk_live_demo_key"),       # sent as X-API-Key
  cache_ttl: 300,   # cache evaluations/assignments per user + key for 5 minutes
  timeout:   10     # HTTP timeout in seconds
)
puts "   Client created with keyword args"

# Option B: SdkConfig struct
config = Controlvar::SdkConfig.new(
  base_url:  "http://localhost:8000",
  api_key:   ENV.fetch("EXPERIMENTLY_API_KEY", "sk_live_demo_key"),
  cache_ttl: 60,
  timeout:   5
)
config.validate!
puts "   SdkConfig created and validated"
puts

# ---------------------------------------------------------------------------
# 2. Evaluating a feature flag (server decides)
# ---------------------------------------------------------------------------
puts "2. Evaluating a feature flag"
puts "-" * 30
puts "   flag = client.evaluate_flag('dark-mode', 'user-123')"
puts "   # GET /api/v1/feature-flags/evaluate/dark-mode?user_id=user-123"
puts "   flag.key       # => 'dark-mode'"
puts "   flag.enabled?  # => true / false (false on any failure)"
puts "   flag.config    # => nil today: the evaluate route returns config: null"
puts
puts "   client.feature_enabled?('dark-mode', 'user-123')  # => true / false"
puts

# ---------------------------------------------------------------------------
# 3. Getting an experiment assignment (sticky on the server)
# ---------------------------------------------------------------------------
puts "3. Getting experiment assignment"
puts "-" * 30
puts "   assignment = client.get_assignment('checkout-flow', 'user-123', { country: 'US' })"
puts "   # POST /api/v1/tracking/assign {experiment_key, user_id, context}"
puts "   # nil when the experiment is not ACTIVE or the request fails, otherwise:"
puts "   assignment.experiment_key  # => 'checkout-flow'"
puts "   assignment.variant_name    # => 'control' / 'treatment'"
puts "   assignment.variant_id      # => variant UUID"
puts "   assignment.control?        # => true for the control variant"
puts "   assignment.configuration   # => the variant's configuration Hash, or nil"
puts

# ---------------------------------------------------------------------------
# 4. Tracking events
# ---------------------------------------------------------------------------
puts "4. Tracking events (never raises)"
puts "-" * 30
puts "   # With a key -> one POST /api/v1/tracking/track"
puts "   client.track('purchase', 'user-123', value: 49.0, experiment_key: 'checkout-flow')"
puts "   client.track('search', 'user-123', properties: { q: 'shoes' }, feature_flag_key: 'new-search')"
puts
puts "   # Without a key -> one POST /api/v1/tracking/batch with one entry per cached"
puts "   # assignment and evaluated flag for the user (nothing cached -> nothing sent)"
puts "   client.track('page_view', 'user-123', properties: { page: '/' })"
puts
puts "   # Explicit batch -> BatchResult(success_count, failure_count, errors)"
puts "   client.track_batch(["
puts "     { event_name: 'purchase', user_id: 'user-123', experiment_key: 'checkout-flow', value: 49.0 },"
puts "     { event_name: 'search',   user_id: 'user-123', feature_flag_key: 'new-search' }"
puts "   ])"
puts

# ---------------------------------------------------------------------------
# 5. Consistent hash utility (no HTTP)
# ---------------------------------------------------------------------------
puts "5. Consistent hash utility (golden vector; not used for bucketing)"
puts "-" * 30

bucket = Controlvar::FeatureFlagEvaluator.hash_user("user-123", "my-flag")
puts "   hash_user('user-123', 'my-flag') = #{bucket}"
puts "   # Cross-SDK reference value:      0.6927449859213084"
puts "   # Match: #{(bucket - 0.6927449859213084).abs < 1e-10}"
puts

# ---------------------------------------------------------------------------
# 6. Error handling
# ---------------------------------------------------------------------------
puts "6. Error handling"
puts "-" * 30
puts "   # evaluate_flag / get_assignment / track never raise on network or HTTP"
puts "   # errors: they log a warning and return a disabled flag / nil / false."
puts "   # Only HttpClient raises:"
puts "   #   Controlvar::AuthenticationError (401)"
puts "   #   Controlvar::APIError            (other 4xx/5xx, #status_code)"
puts "   #   Controlvar::NetworkError        (timeout, DNS, refused)"
puts

# ---------------------------------------------------------------------------
# 7. Cleanup
# ---------------------------------------------------------------------------
puts "7. Cleanup"
puts "-" * 30
puts "   client.clear_cache  # drop cached results"
puts "   client.close        # same, for shutdown hooks"
puts

client.close

puts "=" * 55
puts "For full API reference see README.md"
