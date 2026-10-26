# Ruby SDK

`controlvar` (v0.1.0) provides feature flag evaluation, experiment assignment and
event tracking for Ruby applications. It has zero runtime gem dependencies (`Net::HTTP`, `JSON`,
`Digest`, `Mutex` from the standard library) and is safe to share between threads.

Flag evaluation and experiment assignment are decided **by the server**: every call goes to the
public API with your `X-API-Key`, the server buckets the user (sticky per user + experiment), and
the SDK caches the answer per user + key for a TTL. Nothing is bucketed locally.

Source: `sdk/ruby`.

---

## Installation

**Not yet published.** The `controlvar` gem is not on RubyGems, so build it from source: this
Gemfile line takes `sdk/ruby` from this repository.

```ruby
# Gemfile
gem 'controlvar', git: 'https://github.com/Controlvar/controlvar.git', branch: 'main', glob: 'sdk/ruby/*.gemspec'
```

The gemspec declares `required_ruby_version = ">= 2.6.0"`; CI runs the spec suite
(`bundle exec rspec`) on Ruby 3.2 in the SDK Unit Tests workflow. Inside the monorepo, run scripts
with `ruby -Isdk/ruby/lib ...` (no install).

---

## Quick Start

If you set `EXPERIMENTLY_API_KEY` or `EXPERIMENTLY_API_URL` from an earlier version of these pages, set `CONTROLVAR_API_KEY` and `CONTROLVAR_API_URL` instead.

```ruby
require 'controlvar'

client = Controlvar::Client.new(
  base_url: ENV.fetch('CONTROLVAR_API_URL', 'http://localhost:8000'),  # origin only
  api_key:  ENV.fetch('CONTROLVAR_API_KEY')                             # sent as X-API-Key
)

# 1. Assignment — POST /api/v1/tracking/assign (sticky, records the exposure)
assignment = client.get_assignment('checkout_flow', 'user-123', { plan: 'pro' })   # nil on failure
headline   = assignment&.configuration&.dig('headline') || 'Buy now'

# 2. Feature flag — GET /api/v1/feature-flags/evaluate/new_search?user_id=user-123
show_new_search = client.feature_enabled?('new_search', 'user-123')                # false on failure

# 3. Track with a key → one POST /api/v1/tracking/track (never raises)
client.track('purchase', 'user-123', value: 99.99, experiment_key: 'checkout_flow',
             properties: { sku: 'pro-plan' })

# 4. Track without a key → fanned out to every cached assignment + flag of this user
client.track('page_view', 'user-123', properties: { page: '/products' })
```

---

## Configuration

```ruby
client = Controlvar::Client.new(
  base_url:       'http://localhost:8000',
  api_key:        'your-api-key',
  cache_ttl:      300,
  timeout:        10,
  max_cache_size: 1000
)
# or: Client.new(Controlvar::SdkConfig.new(base_url: ..., api_key: ...))
```

| Option | Type | Default | Description |
|---|---|---|---|
| `base_url` | `String` | — (required) | Backend origin, e.g. `https://api.example.com`; the SDK appends `/api/v1/...`; trailing `/` stripped |
| `api_key` | `String` | — (required) | API key sent as `X-API-Key` |
| `cache_ttl` | `Integer` | `300` | Seconds a successful evaluation/assignment is reused |
| `timeout` | `Integer` | `10` | `Net::HTTP` open and read timeout (seconds) |
| `max_cache_size` | `Integer` | `1000` | Maximum cache entries, least recently used evicted |

`Client.new` raises `ArgumentError` when `base_url` or `api_key` is missing.

---

## API Reference

| Method | Endpoint | Returns | On failure |
|---|---|---|---|
| `evaluate_flag(flag_key, user_id, attributes = {})` | `GET /feature-flags/evaluate/{key}?user_id=` | `FlagEvaluation` | `FlagEvaluation(enabled: false, config: nil)`; never raises |
| `feature_enabled?(flag_key, user_id, attributes = {})` | same (via cache) | `Boolean` | `false` |
| `get_assignment(experiment_key, user_id, attributes = {})` | `POST /tracking/assign` | `Assignment` | `nil`; never raises |
| `track(event_name, user_id, properties: {}, experiment_key: nil, feature_flag_key: nil, value: nil, event_type: nil, timestamp: nil)` | `/tracking/track` or `/tracking/batch` | `true` | `false`; never raises |
| `track_batch(events)` | `/tracking/batch` (chunks of 100) | `BatchResult` | failures counted; never raises |
| `assignments(user_id)` | — | `Array<Assignment>` | cached, unexpired only |
| `evaluated_flags(user_id)` | — | `Array<String>` | cached flag keys |
| `clear_cache`, `close` | — | `{}` (an empty Hash) | drop the cache (`at_exit { client.close }`) |
| `FeatureFlagEvaluator.hash_user(user_id, key)` | — | `Float` in `[0, 1)` | pure |

### Structs

| Struct | Members |
|---|---|
| `FlagEvaluation` | `key: String` (the key you asked for), `enabled: Boolean` + `enabled?`, `config: Object, nil` (always `nil`: the evaluate endpoint returns `config: null` for every flag) |
| `Assignment` | `experiment_key: String`, `variant_id: String, nil` (UUID), `variant_name: String` (`"control"`, `"treatment"`, …), `is_control: Boolean` + `control?`, `configuration: Hash, nil` |
| `BatchResult` | `success_count: Integer`, `failure_count: Integer`, `errors: Array, nil`, `ok?` |

`attributes` on `get_assignment` become the assignment `context` (targeting rules); on
`evaluate_flag` they are accepted for API symmetry only and not sent: the request carries the user
id alone, so the server evaluates the flag's targeting rules against an empty context. For `track`: `properties` → `metadata`,
`event_type` defaults to `event_name`, `timestamp` (`Time` or ISO-8601 string) defaults to now
(UTC). `track_batch` takes Hashes (symbol or string keys) with `event_name` and `user_id` plus
optional `experiment_key`, `feature_flag_key`, `properties`, `value`, `event_type`, `timestamp`;
malformed entries are reported in `errors` without being sent. An event with no key is stored as
history and counts in no experiment's results; tag outcome events.

```ruby
result = client.track_batch([
  { event_name: 'purchase', user_id: 'user-123', experiment_key: 'checkout_flow', value: 99.99 },
  { event_name: 'search',   user_id: 'user-123', feature_flag_key: 'new_search' }
])
result.ok?   # => true when failure_count == 0
```

---

## Caching and failure behaviour

- Successful evaluations and assignments are cached per **user + key** for `cache_ttl` (default
  300 s), LRU-bounded by `max_cache_size`; a hit makes no request. **Failures are never cached**,
  so the next call retries. There is no stale fallback beyond the TTL.
- Network error / timeout, `401` (bad key), `404` (an unknown experiment or flag key, or a draft experiment with no
  control variant), `422`, `429` (rate limited), `5xx`: `evaluate_flag` returns a disabled evaluation, `get_assignment`
  returns `nil`, `track` returns `false`, `track_batch` counts the chunk as failed. Each failure
  is logged once with `Kernel#warn` (`[Controlvar] ... error: ...`). An experiment that is not ACTIVE is
  not a failure: `get_assignment` returns its control variant.
- Only `Controlvar::HttpClient` raises:

| Error | Extends | When |
|---|---|---|
| `Controlvar::AuthenticationError` | `APIError` | HTTP 401 — invalid API key |
| `Controlvar::APIError` (`#status_code`) | `Error` | Other 4xx/5xx (404 unknown key, 422 validation, 429 rate limited) |
| `Controlvar::NetworkError` | `Error` | Timeout, DNS failure, connection refused |

- Thread safety: a single client can be shared across threads. The cache `Mutex` is held only for
  a cache read/write — never during the HTTP request — and every request opens its own
  `Net::HTTP` connection.

---

## Tracking fan-out

With `experiment_key` and/or `feature_flag_key`, `track` sends one `POST /api/v1/tracking/track`.
Without a key it sends one `POST /api/v1/tracking/batch` containing one entry per experiment the
user was assigned to through this client (`experiment_key`) plus one per flag evaluated for the
user (`feature_flag_key`), taken from the cache. Nothing cached → nothing is sent and `true` is
returned. Batches are chunked at 100 events. This is what makes a single `track('purchase', …)`
count as a conversion for every experiment the user is in.

This SDK reads only the variant from an answer: an `assigned: false` answer (a user the
experiment did not enrol, or an experiment that is not ACTIVE) is cached and returned as the
control variant, and an event tracked without a key after the user's cache entries have expired
(`cache_ttl`, 300 s by default) is not sent, so pass `experiment_key` on a conversion that can come
later.

Conversions are matched to metrics by **event name**: a metric on `purchase` counts every
`purchase` event regardless of `event_type`.

---

## Rails integration

```ruby
# config/initializers/controlvar.rb
CONTROLVAR = Controlvar::Client.new(
  base_url: ENV.fetch('CONTROLVAR_API_URL'), api_key: ENV.fetch('CONTROLVAR_API_KEY'), cache_ttl: 60
)
at_exit { CONTROLVAR.close }

# app/controllers/application_controller.rb
class ApplicationController < ActionController::Base
  helper_method :feature_enabled?, :experiment_variant

  private

  def controlvar_user_id
    current_user&.id&.to_s || cookies[:visitor_id]
  end

  def feature_enabled?(flag_key)
    CONTROLVAR.feature_enabled?(flag_key, controlvar_user_id)
  end

  def experiment_variant(experiment_key)
    CONTROLVAR.get_assignment(experiment_key, controlvar_user_id, { plan: current_user&.plan })
                &.variant_name || 'control'
  end
end
```

In your own specs stub HTTP with WebMock (as `sdk/ruby/spec` does) or double the client
(`allow(CONTROLVAR).to receive(:feature_enabled?).and_return(true)`).

---

## Consistent hash (compatibility utility)

`Controlvar::FeatureFlagEvaluator.hash_user(user_id, key)` = `MD5("{user_id}:{key}")`,
first 4 bytes as little-endian uint32, divided by 2^32 (`0.6927449859213084` for `'user-123'`,
`'my-flag'`). It is a compatibility utility: the SDK's own specs pin that one vector, and the
cross-SDK golden vectors in `tests/sdk-contract/` are not run against this SDK. **Nothing in the
SDK calls it to pick a variant** — the server decides.

---

## Backend endpoints used

Every request carries `X-API-Key`, `Content-Type: application/json` and `Accept: application/json`.

| SDK call | Method and path | Body / query | Response used |
|---|---|---|---|
| `evaluate_flag`, `feature_enabled?` | `GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=…` | — | `{key, enabled, config}`; off with `reason: "inactive"` when the flag exists but is not ACTIVE; 404 only for an unknown key |
| `get_assignment` | `POST /api/v1/tracking/assign` | `{experiment_key, user_id, context?}` | `{experiment_key, user_id, variant_id, variant_name, is_control, configuration}`; 404 when no experiment has the key, or for a draft with no control variant; an experiment that is not ACTIVE answers its control variant with `assigned: false` |
| `track` with a key | `POST /api/v1/tracking/track` | `{event_type, event_name, user_id, experiment_key?, feature_flag_key?, value?, metadata?, timestamp}` | ignored |
| `track` without keys, `track_batch` | `POST /api/v1/tracking/batch` | `{events: [<track body>, …]}` (max 100 per request) | `{success_count, failure_count, errors}` (`track_batch` only) |

Errors: 401 bad key, 404 (an unknown experiment or flag key, or a draft experiment with no control variant), 422 invalid event, 429
rate limited (`Retry-After`). These paths share the backend's per-IP `SDK_RATE_LIMIT_PER_MINUTE`
ceiling (default 6000).

---

## Contract smoke

```bash
ruby -Isdk/ruby/lib sdk/ruby/examples/contract_smoke.rb
```

It prints:

```text
{"sdk":"ruby","assign":{"variant_name":"control","is_control":true,"sticky":true},"flag":{"enabled":true},"track":{"ok":true},"fanout":{"ok":true}}
```

Env: `CONTROLVAR_API_URL` (default `http://localhost:8000`), `CONTROLVAR_API_KEY` (required),
`CONTRACT_EXPERIMENT_KEY` (default `sdk_contract_ab`), `CONTRACT_FLAG_KEY` (default
`sdk_contract_flag`), `CONTRACT_USER_ID` (default random `smoke-<uuid>`). The smoke assigns,
clears the cache and assigns again (server stickiness), evaluates the flag, tracks `purchase` with
the experiment key, tracks `page_view` without a key and sends a 2-event `track_batch`. Fixtures:
`backend/scripts/seed_sdk_contract.py`; repo-wide runner:
`python tests/sdk-contract/live/run_live_contract.py --sdk ruby --strict`.

CI runs this SDK's contract smoke against a real API: `ruby` is in the `sdks:` list of the
SDK Live Contract job in [pr-qa-gate.yml](https://github.com/Controlvar/controlvar/blob/main/.github/workflows/pr-qa-gate.yml).

---

## Development

```bash
cd sdk/ruby
bundle install && bundle exec rspec
ruby test_standalone.rb
```

- `bundle install && bundle exec rspec`: RSpec (run here on Ruby 2.6.10); HTTP stubbed with WebMock
- `ruby test_standalone.rb`: stdlib only: hash vector, types, cache, config, track safety
