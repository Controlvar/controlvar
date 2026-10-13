# Experimently Elixir SDK

Elixir SDK for Experimently — A/B testing and feature flags.

Flag evaluation and experiment assignment are decided **by the server**: every call goes to the
public API with your `X-API-Key`, the server buckets the user (sticky per user + experiment), and
the SDK caches the answer per user + key. Nothing is bucketed locally.

## Features

- Server-side evaluation and assignment through the public API (`/api/v1/...`)
- Per-client ETS-backed cache of successful results per user + key (TTL, max size); failures
  are never cached
- Fire-and-forget event tracking with automatic fan-out to every experiment/flag the user is in
- OTP-native HTTP via `:httpc` (zero external runtime deps beyond Jason)
- GenServer-based client for supervised process lifecycle
- Behaviour-based HTTP client for easy testing with mock modules
- Cross-SDK MD5 hash utility (`Experimently.Evaluator.hash_user/2`)

## Installation

Add to your `mix.exs`.

**Not yet published.** The `experimently` package is not on Hex yet, so the first block below
fails today. Take it from this repository with the second block instead.

```elixir
defp deps do
  [
    {:experimently, "~> 0.1.0"}
  ]
end
```

```elixir
defp deps do
  [
    {:experimently, git: "https://github.com/getexperimently/experimently.git", sparse: "sdk/elixir"}
  ]
end
```

## Quick Start

```elixir
{:ok, client} =
  Experimently.start(
    base_url: System.get_env("EXPERIMENTLY_API_URL", "http://localhost:8000"),
    api_key: System.fetch_env!("EXPERIMENTLY_API_KEY")
  )

# Feature flag — GET /api/v1/feature-flags/evaluate/{key}?user_id=...
case Experimently.evaluate_flag(client, "dark-mode", "user-123") do
  {:ok, %Experimently.FlagEvaluation{enabled: true}} -> render_dark_mode()
  {:ok, _disabled} -> render_default()    # off, or not ACTIVE
  {:error, _reason} -> render_default()   # network/HTTP failure, unknown flag key
end

Experimently.feature_enabled?(client, "dark-mode", "user-123")   # => true | false

# Experiment — POST /api/v1/tracking/assign (sticky on the server, records the exposure)
{:ok, %Experimently.Assignment{variant_name: variant, configuration: config}} =
  Experimently.get_assignment(client, "checkout-exp", "user-123", %{plan: "pro"})

# Events (fire-and-forget, always :ok)
Experimently.track(client, "purchase", "user-123", %{sku: "pro"},
  experiment_key: "checkout-exp", value: 49.99)
Experimently.track(client, "page_view", "user-123", %{page: "/"})   # no key: fanned out

Experimently.stop(client)
```

## Configuration

| Option            | Type    | Required | Default      | Description                                             |
|-------------------|---------|----------|--------------|---------------------------------------------------------|
| `:base_url`       | string  | yes      | —            | Backend origin (e.g. `http://localhost:8000`); the SDK appends `/api/v1/...` |
| `:api_key`        | string  | yes      | —            | API key, sent as `X-API-Key`                            |
| `:cache_ttl`      | integer | no       | 300          | Seconds a successful evaluation/assignment is reused    |
| `:timeout`        | integer | no       | 10_000       | HTTP timeout in milliseconds                            |
| `:max_cache_size` | integer | no       | 1_000        | Maximum cached entries (oldest evicted)                 |
| `:http_client`    | module  | no       | `HttpClient` | `HttpBehaviour` implementation (for testing)            |
| `:name`           | atom    | no       | —            | Register the client process under a name                |

## API

| Function | Returns |
|----------|---------|
| `evaluate_flag(client, flag_key, user_id, opts \\ [])` | `{:ok, %FlagEvaluation{key, enabled, config}}` or `{:error, reason}` |
| `feature_enabled?(client, flag_key, user_id)` | `true`/`false` (`false` on any failure) |
| `get_assignment(client, experiment_key, user_id, attributes \\ %{})` | `{:ok, %Assignment{experiment_key, variant_id, variant_name, is_control, configuration}}` or `{:error, reason}` |
| `track(client, event_name, user_id, properties \\ %{}, opts \\ [])` | `:ok` (fire-and-forget; opts `:experiment_key`, `:feature_flag_key`, `:value`, `:event_type`, `:timestamp`) |
| `track_batch(client, events)` | `{:ok, %BatchResult{success_count, failure_count, errors}}` or `{:error, reason}` (max 100 per request) |
| `assignments(client, user_id)` / `evaluated_flags(client, user_id)` | Cached (successful, unexpired) assignments / flag keys for the user |
| `clear_cache(client)` | `:ok` |
| `stop(client)` | `:ok` (waits for in-flight track requests) |

Error reasons: `{:auth_error, 401}`, `{:api_error, status, body}` (404 = an experiment unknown or not
ACTIVE or an unknown flag key, 422 = validation, 429 = rate limited), `{:network_error, reason}`,
`{:malformed_response, body}`.

**Track fan-out.** With `:experiment_key` and/or `:feature_flag_key` the SDK sends one
`POST /api/v1/tracking/track`. Without a key it sends one `POST /api/v1/tracking/batch` with one
entry per experiment the user was assigned to plus one per flag evaluated for the user through this
client (from the cache). If nothing is cached, nothing is sent. `event_type` defaults to the event
name; `properties` is sent as `metadata`.

## Backend endpoints used

Every request carries `X-API-Key`, `Content-Type: application/json` and `Accept: application/json`.

| SDK call | Method and path | Body / query | Response used |
|---|---|---|---|
| `evaluate_flag`, `feature_enabled?` | `GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=…` | — | `{key, enabled, config}`; off with `reason: "inactive"` when the flag exists but is not ACTIVE; 404 only for an unknown key |
| `get_assignment` | `POST /api/v1/tracking/assign` | `{experiment_key, user_id, context?}` | `{experiment_key, user_id, variant_id, variant_name, is_control, configuration}`; 404 when the experiment is not ACTIVE |
| `track` with a key | `POST /api/v1/tracking/track` | `{event_type, event_name, user_id, experiment_key?, feature_flag_key?, value?, metadata?, timestamp}` | ignored |
| `track` without keys, `track_batch` | `POST /api/v1/tracking/batch` | `{events: [<track body>, …]}` (max 100 per request) | `{success_count, failure_count, errors}` |

## Architecture

```
Experimently            <- Public facade
  └── Client (GenServer)           <- Config, in-flight track requests
        └── Cache (GenServer)      <- ETS-backed TTL cache, per client, keyed {type, user_id, key}
              └── :ets table       <- Actual storage

Experimently.Evaluator  <- Cross-SDK hash utility (not used to decide variants)
Experimently.HttpClient <- :httpc-based HTTP (implements HttpBehaviour)
```

Evaluations, assignments and `track_batch/2` run in the **calling** process; `track/5` is a cast
that spawns one process per request, and `stop/1` waits (up to `timeout`) for them.

## Hash Compatibility

`Experimently.Evaluator.hash_user/2` implements the cross-SDK formula
`MD5("{user_id}:{flag_key}") -> first 4 bytes as little-endian uint32 / 2^32` and is pinned by the
golden-vector tests in `tests/sdk-contract/`:

```elixir
Experimently.Evaluator.hash_user("user-123", "my-flag")
# => 0.6927449859213084
```

It is exported as a utility only. Since assignment moved to the server, nothing in the SDK uses
it to decide a variant. Standalone checks: `elixir sdk/elixir/test_standalone.exs` and
`python3 sdk/elixir/verify_hash.py`.

## Contract smoke

Runs the four contract steps (sticky assignment, flag evaluation, keyed track, key-less fan-out
plus a 2-event batch) against a live backend and prints one JSON line:

```bash
cd sdk/elixir && mix deps.get
EXPERIMENTLY_API_KEY=<key> mix run examples/contract_smoke.exs
```

- `cd sdk/elixir && mix deps.get`: once

It prints:

```text
{"sdk":"elixir","assign":{"variant_name":"control","is_control":true,"sticky":true},"flag":{"enabled":true},"track":{"ok":true},"fanout":{"ok":true}}
```

Env: `EXPERIMENTLY_API_URL` (default `http://localhost:8000`), `EXPERIMENTLY_API_KEY` (required),
`CONTRACT_EXPERIMENT_KEY` (default `sdk_contract_ab`), `CONTRACT_FLAG_KEY` (default
`sdk_contract_flag`), `CONTRACT_USER_ID` (default random `smoke-<uuid>`). Set `MIX_QUIET=1` to keep
Mix's compile messages off stdout.

CI does not run this SDK's contract smoke against a real API: `elixir` is not in the
`sdks:` list of the SDK Live Contract job in [pr-qa-gate.yml](https://github.com/getexperimently/experimently/blob/main/.github/workflows/pr-qa-gate.yml).

## Testing

```bash
cd sdk/elixir
mix deps.get
mix test
```

- `mix test`: ExUnit, HTTP mocked through HttpBehaviour modules

The ExUnit suite (client, facade, cache, HTTP helpers, hash parity) was rewritten for the
public-API contract but **not executed here** — no `mix` on the development machine.

### Testing your own code

Inject a mock HTTP client via the `:http_client` config option:

```elixir
defmodule MyMockHttp do
  @behaviour Experimently.HttpBehaviour

  @impl true
  def get(_config, "/api/v1/feature-flags/evaluate/my-flag?user_id=" <> _user_id) do
    {:ok, %{"key" => "my-flag", "enabled" => true, "config" => %{"variant" => "treatment"}}}
  end

  @impl true
  def post(_config, "/api/v1/tracking/assign", %{experiment_key: key, user_id: user_id}) do
    {:ok, %{"experiment_key" => key, "user_id" => user_id, "variant_id" => "v-1",
            "variant_name" => "treatment", "is_control" => false, "configuration" => nil}}
  end

  def post(_config, _path, _body), do: {:ok, %{}}
end

# In your test:
{:ok, client} =
  Experimently.start(base_url: "http://localhost", api_key: "test", http_client: MyMockHttp)
```

## License

MIT
