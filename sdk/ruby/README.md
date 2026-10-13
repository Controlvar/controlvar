# Experimently Ruby SDK

Native Ruby client for the Experimently A/B testing and feature flag platform. Stdlib only
(`Net::HTTP`, `JSON`, `Digest`, `Mutex`), thread-safe, no runtime gem dependencies.

Flag evaluation and experiment assignment are decided **by the server**: every call goes to the
public API with your `X-API-Key`, the server buckets the user (sticky per user + experiment), and
the SDK caches the answer per user + key for a TTL. Nothing is bucketed locally.

Full reference: [`docs/sdk/ruby.md`](../../docs/sdk/ruby.md).

## Installation

**Not yet published.** The `experimently` gem is not on RubyGems yet, so the first line below
fails today. Take it from this repository with the second block instead.

```ruby
gem "experimently", "~> 0.1"
```

```ruby
# Gemfile, until the gem is published
gem "experimently", git: "https://github.com/getexperimently/experimently.git", branch: "main", glob: "sdk/ruby/*.gemspec"
```

The gemspec requires **Ruby >= 2.6.0** (`required_ruby_version`); the specs in this repo were run
on Ruby 2.6.10. Inside the monorepo, no install is needed: `ruby -Isdk/ruby/lib your_script.rb`.

## Quick Start

```ruby
require 'experimently'

client = Experimently::Client.new(
  base_url: ENV.fetch("EXPERIMENTLY_API_URL", "http://localhost:8000"),  # origin only
  api_key:  ENV.fetch("EXPERIMENTLY_API_KEY")                             # sent as X-API-Key
)

# Experiment assignment — POST /api/v1/tracking/assign (sticky on the server)
assignment = client.get_assignment("checkout_flow", "user-123", { plan: "pro", country: "US" })
if assignment                       # nil on any failure (404 not ACTIVE, 401, network)
  assignment.variant_name           # => "control" / "treatment"
  assignment.control?               # => true for the control variant
  assignment.configuration          # => the variant's configuration Hash, or nil
end

# Feature flag — GET /api/v1/feature-flags/evaluate/new_search?user_id=user-123
flag = client.evaluate_flag("new_search", "user-123")
flag.enabled?                       # => false on any failure
flag.config                         # => nil: the evaluate endpoint returns config: null
client.feature_enabled?("new_search", "user-123")

# Track with a key — one POST /api/v1/tracking/track (never raises)
client.track("purchase", "user-123", value: 49.99, experiment_key: "checkout_flow",
             properties: { currency: "USD" })

# Track without a key — fanned out to every cached assignment + flag of the user
client.track("page_view", "user-123", properties: { page: "/" })
```

## Configuration

`Client.new(**options)` or `Client.new(Experimently::SdkConfig.new(**options))`.

| Option | Type | Default | Description |
|---|---|---|---|
| `base_url` | `String` | — (required) | Backend origin, e.g. `https://api.example.com`; the SDK appends `/api/v1/...`; trailing `/` stripped |
| `api_key` | `String` | — (required) | Sent as `X-API-Key` |
| `cache_ttl` | `Integer` | `300` | Seconds a successful evaluation/assignment is reused |
| `timeout` | `Integer` | `10` | `Net::HTTP` open and read timeout (seconds) |
| `max_cache_size` | `Integer` | `1000` | Max cached entries; least recently used evicted |

Missing `base_url`/`api_key` raise `ArgumentError` from `Client.new`.

## API

| Method | Returns | On failure |
|---|---|---|
| `evaluate_flag(flag_key, user_id, attributes = {})` | `FlagEvaluation` (`key`, `enabled`/`enabled?`, `config`) | `FlagEvaluation` with `enabled: false`, `config: nil`; never raises |
| `feature_enabled?(flag_key, user_id, attributes = {})` | `Boolean` | `false` |
| `get_assignment(experiment_key, user_id, attributes = {})` | `Assignment` (`experiment_key`, `variant_id`, `variant_name`, `is_control`/`control?`, `configuration`) | `nil`; never raises |
| `track(event_name, user_id, properties: {}, experiment_key: nil, feature_flag_key: nil, value: nil, event_type: nil, timestamp: nil)` | `Boolean` | `false`; never raises |
| `track_batch(events)` | `BatchResult` (`success_count`, `failure_count`, `errors`, `ok?`) | failures counted; never raises |
| `assignments(user_id)` / `evaluated_flags(user_id)` | `Array<Assignment>` / `Array<String>` | cached, unexpired entries only |
| `clear_cache` / `close` | `nil` | drop every cached entry (`close` for shutdown hooks) |
| `Experimently::FeatureFlagEvaluator.hash_user(user_id, key)` | `Float` in `[0, 1)` | pure (parity utility) |

`attributes` on `get_assignment` are sent as the assignment `context` (targeting rules); on
`evaluate_flag` they are accepted for symmetry only: the evaluate endpoint takes a `context`, which this SDK does not send yet.
`track`: `properties` → `metadata`, `event_type` defaults to `event_name`, `timestamp` (`Time` or
ISO-8601 string) defaults to now (UTC). `track_batch` events are Hashes with `event_name` and
`user_id` plus optional `experiment_key`/`feature_flag_key`; malformed Hashes are counted as
failures without being sent. An event with no key is stored as history and counts in no
experiment's results; tag outcome events.

## Caching and failure behaviour

- Successful evaluations and assignments are cached per **user + key** for `cache_ttl` (default
  300 s, LRU-bounded by `max_cache_size`); a hit makes no request. **Failures are never cached.**
- Network error / timeout, 401 (bad key), 404 (an experiment unknown or not ACTIVE, or an unknown flag key), 422, 429
  (rate limited), 5xx: `evaluate_flag` returns a **disabled** evaluation, `get_assignment` returns
  **`nil`**, `track` returns **`false`**, `track_batch` counts the chunk as failed. Each logs one
  line via `Kernel#warn`. There is no stale fallback beyond the TTL.
- Only the low-level `Experimently::HttpClient` raises: `AuthenticationError` (401,
  `< APIError`), `APIError` (`#status_code`; other 4xx/5xx), `NetworkError`.
- One `Client` may be shared between threads: the cache is `Mutex`-protected and each request
  opens its own `Net::HTTP` connection.

## Tracking fan-out

With `experiment_key` and/or `feature_flag_key`, `track` sends one `POST /api/v1/tracking/track`.
Without a key it sends one `POST /api/v1/tracking/batch` containing one entry per experiment the
user was assigned to through this client (`experiment_key`) plus one per flag evaluated for the
user (`feature_flag_key`), taken from the cache. Nothing cached → nothing is sent and `true` is
returned. Batches are chunked at 100 events. Metrics match events by **event name**.

## Consistent hash (compatibility utility)

`FeatureFlagEvaluator.hash_user(user_id, key)` = `MD5("{user_id}:{key}")`, first 4 bytes as a
little-endian uint32, divided by 2^32 (`0.6927449859213084` for `user-123`/`my-flag`). It is kept
only so the cross-SDK golden vectors in `tests/sdk-contract/` keep passing — **nothing in the SDK
buckets locally**; the server decides.

## Backend endpoints used

Every request carries `X-API-Key`, `Content-Type: application/json` and `Accept: application/json`.

| SDK call | Method and path | Body / query | Response used |
|---|---|---|---|
| `evaluate_flag`, `feature_enabled?` | `GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=…` | — | `{key, enabled, config}`; off with `reason: "inactive"` when the flag exists but is not ACTIVE; 404 only for an unknown key |
| `get_assignment` | `POST /api/v1/tracking/assign` | `{experiment_key, user_id, context?}` | `{experiment_key, user_id, variant_id, variant_name, is_control, configuration}`; 404 when the experiment is not ACTIVE |
| `track` with a key | `POST /api/v1/tracking/track` | `{event_type, event_name, user_id, experiment_key?, feature_flag_key?, value?, metadata?, timestamp}` | ignored |
| `track` without keys, `track_batch` | `POST /api/v1/tracking/batch` | `{events: [<track body>, …]}` (max 100 per request) | `{success_count, failure_count, errors}` (`track_batch` only) |

## Contract smoke

```bash
ruby -Isdk/ruby/lib sdk/ruby/examples/contract_smoke.rb
```

It prints:

```text
{"sdk":"ruby","assign":{"variant_name":"control","is_control":true,"sticky":true},"flag":{"enabled":true},"track":{"ok":true},"fanout":{"ok":true}}
```

Env: `EXPERIMENTLY_API_URL` (default `http://localhost:8000`), `EXPERIMENTLY_API_KEY` (required),
`CONTRACT_EXPERIMENT_KEY` (default `sdk_contract_ab`), `CONTRACT_FLAG_KEY` (default
`sdk_contract_flag`), `CONTRACT_USER_ID` (default random `smoke-<uuid>`). Fixtures:
`backend/scripts/seed_sdk_contract.py`; repo-wide runner:
`python tests/sdk-contract/live/run_live_contract.py --sdk ruby --strict`.

CI runs this SDK's contract smoke against a real API: `ruby` is in the `sdks:` list of the
SDK Live Contract job in [pr-qa-gate.yml](https://github.com/getexperimently/experimently/blob/main/.github/workflows/pr-qa-gate.yml).

## Tests

```bash
cd sdk/ruby
bundle install && bundle exec rspec
ruby test_standalone.rb
```

- `bundle install && bundle exec rspec`: 109 examples (run here on Ruby 2.6.10); HTTP stubbed with WebMock
- `ruby test_standalone.rb`: stdlib only, no bundler/rspec needed

## License

MIT
