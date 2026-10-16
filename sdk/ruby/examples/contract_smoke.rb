#!/usr/bin/env ruby
# frozen_string_literal: true

# Contract smoke for the Ruby SDK — exercises the public API against a live
# backend and prints exactly one JSON line on stdout.
#
# Run from the repository root:
#   EXPERIMENTLY_API_KEY=$(cat tests/sdk-contract/live/.api_key) \
#     ruby -Isdk/ruby/lib sdk/ruby/examples/contract_smoke.rb
#
# Environment:
#   EXPERIMENTLY_API_URL     backend origin (default http://localhost:8000)
#   EXPERIMENTLY_API_KEY     API key (required)
#   CONTRACT_EXPERIMENT_KEY  default sdk_contract_ab
#   CONTRACT_FLAG_KEY        default sdk_contract_flag
#   CONTRACT_USER_ID         default smoke-<uuid>

require 'json'
require 'securerandom'
require 'controlvar'

def smoke_fail(message)
  $stderr.puts "contract_smoke(ruby): #{message}"
  exit 1
end

api_url        = ENV.fetch('EXPERIMENTLY_API_URL', 'http://localhost:8000')
api_key        = ENV['EXPERIMENTLY_API_KEY']
experiment_key = ENV.fetch('CONTRACT_EXPERIMENT_KEY', 'sdk_contract_ab')
flag_key       = ENV.fetch('CONTRACT_FLAG_KEY', 'sdk_contract_flag')
user_id        = ENV['CONTRACT_USER_ID'] || "smoke-#{SecureRandom.uuid}"

smoke_fail('EXPERIMENTLY_API_KEY is required') if api_key.nil? || api_key.strip.empty?

client = Controlvar::Client.new(base_url: api_url, api_key: api_key, timeout: 10)

# 1. Sticky assignment: assign twice, dropping the local cache in between so the
#    second answer really comes from the server.
first = client.get_assignment(experiment_key, user_id, { source: 'contract_smoke' })
smoke_fail("assignment failed for #{experiment_key} (is the experiment ACTIVE and the key valid?)") if first.nil?
client.clear_cache
second = client.get_assignment(experiment_key, user_id, { source: 'contract_smoke' })
smoke_fail("second assignment failed for #{experiment_key}") if second.nil?

unless %w[control treatment].include?(first.variant_name)
  smoke_fail("unexpected variant_name #{first.variant_name.inspect}")
end
sticky = first.variant_name == second.variant_name && first.variant_id == second.variant_id
smoke_fail("assignment not sticky: #{first.variant_name} then #{second.variant_name}") unless sticky

# 2. Flag evaluation (the seeded flag is 100% on).
flag = client.evaluate_flag(flag_key, user_id)
smoke_fail("flag #{flag_key} evaluated as #{flag.enabled.inspect}, expected true") unless flag.enabled == true

# 3. Track with an experiment key -> one /tracking/track.
track_ok = client.track('purchase', user_id,
                        properties: { source: 'contract_smoke' },
                        experiment_key: experiment_key,
                        value: 12.5)
smoke_fail('track(purchase) failed') unless track_ok

# 4. Track without a key -> /tracking/batch fan-out to the cached assignment + flag.
fanout_ok = client.track('page_view', user_id, properties: { page: '/smoke' })
smoke_fail('track(page_view) fan-out failed') unless fanout_ok

# 4b. Explicit two-event batch.
batch = client.track_batch([
  { event_name: 'page_view', user_id: user_id, experiment_key: experiment_key, properties: { page: '/batch' } },
  { event_name: 'page_view', user_id: user_id, feature_flag_key: flag_key, properties: { page: '/batch' } }
])
smoke_fail("track_batch failed: #{batch.errors.inspect}") unless batch.ok?

puts JSON.generate(
  sdk:    'ruby',
  assign: { variant_name: first.variant_name, is_control: first.is_control, sticky: true },
  flag:   { enabled: flag.enabled },
  track:  { ok: true },
  fanout: { ok: true }
)
exit 0
