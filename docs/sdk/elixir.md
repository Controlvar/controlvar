# Elixir SDK

`experimently` (v0.1) provides feature flag evaluation, experiment assignment and
event tracking for Elixir and Phoenix applications. It is built on OTP primitives — a `GenServer`
client with a private ETS-backed cache — and uses Erlang's built-in `:httpc` for HTTP, so the only
runtime dependency is `Jason`.

Flag evaluation and experiment assignment are decided **by the server**: every call goes to the
public API with your `X-API-Key`, the server buckets the user (sticky per user + experiment), and
the SDK caches the answer per user + key. Nothing is bucketed locally.

Source: `sdk/elixir`.

---

## Requirements

- Elixir 1.14 or later, OTP 24 or later (`:inets`, `:ssl` and `:crypto` are part of OTP)
- `Jason ~> 1.4` (declared as a dependency automatically)

---

## Installation

Add to your `mix.exs` dependencies and run `mix deps.get`.

**Not yet published.** The `experimently` package is not on Hex, so build it from source: this
dependency takes `sdk/elixir` from this repository.

```elixir
defp deps do
  [
    {:experimently, git: "https://github.com/getexperimently/experimently.git", sparse: "sdk/elixir"}
  ]
end
```

---

## Quick Start

```elixir
{:ok, client} =
  Experimently.start(
    base_url: System.get_env("EXPERIMENTLY_API_URL", "http://localhost:8000"),
    api_key: System.fetch_env!("EXPERIMENTLY_API_KEY")
  )

if Experimently.feature_enabled?(client, "new-checkout", "user-123") do
  render_new_checkout()
end

{:ok, %Experimently.Assignment{variant_name: variant, configuration: config}} =
  Experimently.get_assignment(client, "checkout-cta-copy", "user-123", %{plan: "pro"})

Experimently.track(client, "purchase", "user-123", %{sku: "pro-plan"},
  experiment_key: "checkout-cta-copy", value: 99.99)
```

---

## Configuration

`Experimently.start/1` (or `Experimently.Client.start_link/1`) accepts a
keyword list, a map or an `%Experimently.Config{}`:

```elixir
{:ok, client} =
  Experimently.start(
    base_url: "http://localhost:8000",  # Required — origin only; the SDK appends /api/v1/...
    api_key: "your-api-key",            # Required — sent as X-API-Key
    cache_ttl: 300,                     # Seconds a successful result is reused (default 300)
    timeout: 10_000,                    # HTTP timeout in milliseconds (default 10_000)
    max_cache_size: 1_000,              # Maximum cached entries, oldest evicted (default 1_000)
    name: MyApp.Experiments             # Optional registered name
  )
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `:base_url` | `String.t()` | *(required)* | Backend origin, e.g. `https://api.example.com` |
| `:api_key` | `String.t()` | *(required)* | API key sent as `X-API-Key` |
| `:cache_ttl` | `integer` | `300` | How long a successful evaluation/assignment is reused (seconds) |
| `:timeout` | `integer` | `10_000` | `:httpc` connect and request timeout (milliseconds) |
| `:max_cache_size` | `integer` | `1_000` | Maximum cache entries |
| `:http_client` | `module` | `Experimently.HttpClient` | `HttpBehaviour` implementation (for testing) |
| `:name` | `atom` | — | Register the client process under a name |

`Config.new/1` raises `ArgumentError` for a missing `:base_url` or `:api_key`.

### Supervision tree

```elixir
# lib/my_app/application.ex
children = [
  {Experimently.Client,
   base_url: System.fetch_env!("EXPERIMENTLY_API_URL"),
   api_key: System.fetch_env!("EXPERIMENTLY_API_KEY"),
   name: MyApp.Experiments}
]

Supervisor.start_link(children, strategy: :one_for_one, name: MyApp.Supervisor)
```

Every public function then takes the registered name (`MyApp.Experiments`) in place of the pid.
The client is started with `restart: :temporary`; wrap it in your own child spec if you want a
different restart strategy. Its cache is process-owned, so a restart starts with an empty cache.

---

## Feature Flag Evaluation

### `evaluate_flag(client, flag_key, user_id, opts \\ [])`

Calls `GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=…` and returns
`{:ok, %Experimently.FlagEvaluation{}}`:

| Field | Type | Description |
|-------|------|-------------|
| `key` | `String.t()` | The flag key you asked for |
| `enabled` | `boolean()` | Server decision for this user |
| `config` | `term() \| nil` | Always `nil`: the evaluate endpoint returns `config: null` for every flag |

```elixir
case Experimently.evaluate_flag(client, "dark-mode", "user-456") do
  {:ok, %Experimently.FlagEvaluation{enabled: true}} -> render_dark()
  {:ok, _disabled} -> render_light()
  {:error, _reason} -> render_light()
end
```

Returns `{:error, reason}` on a network/HTTP failure (including `{:api_error, 404, _}` for an
unknown flag key). A flag that is not ACTIVE is not an error: it returns `enabled: false`.
A cached evaluation, when present and unexpired, is returned without calling
the server; failures are never cached, so the next call retries. Options: `skip_cache: true` to
skip the cache; `:attributes` is accepted for symmetry and not sent: the request carries the user
id alone, so the server evaluates the flag's targeting rules against an empty context.

### `feature_enabled?(client, flag_key, user_id)`

`true` when the server enables the flag for the user, `false` otherwise — including on any
failure.

---

## Experiment Assignment

### `get_assignment(client, experiment_key, user_id, attributes \\ %{})`

Calls `POST /api/v1/tracking/assign` with `{experiment_key, user_id, context: attributes}`. The
server buckets the user, keeps the assignment sticky and records the exposure. Returns
`{:ok, %Experimently.Assignment{}}`:

| Field | Type | Description |
|-------|------|-------------|
| `experiment_key` | `String.t()` | The experiment key |
| `variant_id` | `String.t() \| nil` | UUID of the assigned variant |
| `variant_name` | `String.t()` | Assigned variant name (e.g. `"control"`, `"treatment"`) |
| `is_control` | `boolean()` | `true` for the control variant |
| `configuration` | `map() \| nil` | The variant's `configuration` JSON from the experiment definition |

```elixir
case Experimently.get_assignment(client, "checkout-cta-copy", "user-123", %{plan: "pro", country: "US"}) do
  {:ok, %Experimently.Assignment{variant_name: "treatment-a"}} -> render_short_cta()
  {:ok, %Experimently.Assignment{variant_name: "treatment-b"}} -> render_urgency_cta()
  _control_or_error -> render_original_cta()
end
```

Returns `{:error, reason}` on any failure (network error, `{:auth_error, 401}`,
`{:api_error, 404, _}` when the experiment is not ACTIVE, `{:malformed_response, body}` when the
server did not send a `variant_name`); treat it as the control default. A cached assignment is
returned when one exists; failures are never cached. The fourth argument may also be a keyword list
with `:attributes` and `:skip_cache`.

---

## Event Tracking

### `track(client, event_name, user_id, properties \\ %{}, opts \\ [])`

Fire-and-forget: the event is cast to the client, which sends it from a spawned process. Always
returns `:ok` and never raises; failures are logged as warnings. `stop/1` waits (up to `timeout`)
for in-flight requests.

```elixir
Experimently.track(client, "purchase", "user-123", %{sku: "pro-plan"},
  experiment_key: "checkout-cta-copy", value: 99.99)
Experimently.track(client, "search", "user-123", %{q: "shoes"}, feature_flag_key: "new-search")
Experimently.track(client, "page_view", "user-123", %{page: "/products"})   # no key: fanned out
```

| Option | Type | Description |
|--------|------|-------------|
| `:experiment_key` | `String.t()` | Attribute the event to an experiment |
| `:feature_flag_key` | `String.t()` | Attribute the event to a flag |
| `:value` | `number()` | Numeric value (revenue, duration, …) |
| `:event_type` | `String.t()` | Defaults to `event_name` |
| `:timestamp` | `DateTime.t() \| String.t()` | Defaults to now (UTC, ISO-8601) |

**Fan-out rule.** With `:experiment_key` and/or `:feature_flag_key` the SDK sends one
`POST /api/v1/tracking/track`. Without a key it sends one `POST /api/v1/tracking/batch` containing
one entry per experiment the user has been assigned to through this client plus one per flag
evaluated for the user (from the cache, chunked at 100 per request). If nothing is cached, nothing
is sent. This is what makes a single `track(client, "purchase", …)` count as a conversion for every
experiment the user is in.

Conversions are matched to metrics by **event name**: an experiment metric whose `event_name` is
`purchase` counts every `purchase` event, whatever `event_type` was sent. `properties` is sent as
`metadata`.

The cache — and therefore the key-less fan-out — is per client process. In a Phoenix app with one
supervised client, assignments made in earlier requests are visible for `cache_ttl` seconds; pass
the key explicitly when tracking from a different node or a worker that never assigned the user.

### `track_batch(client, events)`

Synchronous. Sends up to 100 events per `POST /api/v1/tracking/batch` (longer lists are chunked)
and returns `{:ok, %Experimently.BatchResult{success_count, failure_count, errors}}` or
`{:error, reason}` when a request fails. Each event is a map (atom or string keys) with
`event_name` and `user_id`; optional `experiment_key`, `feature_flag_key`, `properties` (sent as
`metadata`), `value`, `event_type`, `timestamp`. An event with no key is stored as history and
counts in no experiment's results; tag outcome events.

```elixir
{:ok, result} =
  Experimently.track_batch(client, [
    %{event_name: "purchase", user_id: "user-123", experiment_key: "checkout-cta-copy", value: 99.99},
    %{event_name: "search", user_id: "user-123", feature_flag_key: "new-search"}
  ])

Experimently.BatchResult.ok?(result)   # true when failure_count == 0
result.errors                                      # nil, or a list of error maps
```

Malformed events are counted as failures (with `%{index, error}` entries) without being sent.

---

## Cache helpers

| Function | Description |
|----------|-------------|
| `assignments(client, user_id)` | Cached (successful, unexpired) `%Assignment{}` structs for the user, oldest first |
| `evaluated_flags(client, user_id)` | Keys of flags successfully evaluated (and still cached) for the user |
| `clear_cache(client)` | Drop every cached evaluation and assignment |

Each client owns a private ETS table wrapped in a `Cache` GenServer; entries are keyed
`{:flag, user_id, flag_key}` / `{:assignment, user_id, experiment_key}` and expire after
`cache_ttl` seconds. When `max_cache_size` is reached the oldest insertion is evicted.
Evaluations, assignments and `track_batch/2` run in the calling process — only the cache lookup
and the `track/5` cast go through a GenServer — so many processes can share one client.

---

## Backend endpoints used

Every request carries `X-API-Key`, `Content-Type: application/json` and `Accept: application/json`.

| SDK call | Method and path | Body / query | Response used |
|---|---|---|---|
| `evaluate_flag`, `feature_enabled?` | `GET /api/v1/feature-flags/evaluate/{flag_key}?user_id=…` | — | `{key, enabled, config}`; `enabled: false`, `reason: "inactive"` when the flag is not ACTIVE; 404 only for an unknown key |
| `get_assignment` | `POST /api/v1/tracking/assign` | `{experiment_key, user_id, context?}` | `{experiment_key, user_id, variant_id, variant_name, is_control, configuration}`; 404 when the experiment is not ACTIVE |
| `track` with a key | `POST /api/v1/tracking/track` | `{event_type, event_name, user_id, experiment_key?, feature_flag_key?, value?, metadata?, timestamp}` | ignored |
| `track` without keys, `track_batch` | `POST /api/v1/tracking/batch` | `{events: [<track body>, …]}` (max 100 per request) | `{success_count, failure_count, errors}` |

These SDK paths share a per-IP rate-limit ceiling of `SDK_RATE_LIMIT_PER_MINUTE` requests
(default 6000) on the backend; a `429` is surfaced as `{:error, {:api_error, 429, body}}`.

---

## Error Handling

The public functions never raise on network or HTTP errors; they return tagged tuples:

| Reason | When |
|--------|------|
| `{:auth_error, 401}` | Invalid API key |
| `{:api_error, status, body}` | Other 4xx/5xx — 404 (an experiment unknown or not ACTIVE, or an unknown flag key), 422 validation, 429 rate limited |
| `{:network_error, reason}` | `:httpc` connection failures and timeouts, with `:httpc`'s own reason (a refused connection is `{:failed_connect, [...]}`, a timeout `:timeout`) |
| `{:malformed_response, body}` | 2xx body without the expected fields (assignment without `variant_name`) |

`Experimently.Config.new/1` raises `ArgumentError` for a missing `:base_url`/`:api_key`.
The exception structs in `Experimently.{Error, AuthError, ApiError, NetworkError, ConfigError}`
are available for callers who prefer to raise.

---

## Consistent Hash Utility

`Experimently.Evaluator.hash_user/2` implements the cross-SDK formula —
`MD5("{user_id}:{flag_key}")`, first 4 bytes as little-endian uint32, divided by 2^32. The SDK's
own `hash_compatibility_test.exs` pins one vector; the cross-SDK golden vectors in
`tests/sdk-contract/` are not run against this SDK:

```elixir
Experimently.Evaluator.hash_user("user-123", "my-flag")   # 0.6927449859213084
```

It is exported as a utility only. Since assignment moved to the server, nothing in the SDK uses it
to decide a variant.

---

## Phoenix Controller Integration

```elixir
defmodule MyAppWeb.CheckoutController do
  use MyAppWeb, :controller

  alias Experimently, as: Experiments

  def show(conn, _params) do
    user_id = get_session(conn, :user_id) || "anonymous"

    new_checkout = Experiments.feature_enabled?(MyApp.Experiments, "new-checkout", user_id)

    cta_variant =
      case Experiments.get_assignment(MyApp.Experiments, "checkout-cta-copy", user_id, %{plan: "pro"}) do
        {:ok, %Experimently.Assignment{variant_name: variant}} -> variant
        {:error, _} -> "control"
      end

    Experiments.track(MyApp.Experiments, "checkout_view", user_id, %{path: conn.request_path})

    render(conn, :show, new_checkout: new_checkout, cta_variant: cta_variant)
  end
end
```

---

## Testing your own code

Inject a mock HTTP module through the `:http_client` option (the SDK's own tests use this
pattern — see `sdk/elixir/test/experimently/client_test.exs`):

```elixir
defmodule MyApp.MockHttp do
  @behaviour Experimently.HttpBehaviour

  @impl true
  def get(_config, "/api/v1/feature-flags/evaluate/new-checkout?user_id=" <> _user_id) do
    {:ok, %{"key" => "new-checkout", "enabled" => true, "config" => nil}}
  end

  @impl true
  def post(_config, "/api/v1/tracking/assign", %{experiment_key: key, user_id: user_id}) do
    {:ok, %{"experiment_key" => key, "user_id" => user_id, "variant_id" => "v-1",
            "variant_name" => "treatment", "is_control" => false, "configuration" => nil}}
  end

  def post(_config, _path, _body), do: {:ok, %{}}
end

{:ok, client} =
  Experimently.start(base_url: "http://localhost", api_key: "test", http_client: MyApp.MockHttp)

assert Experimently.feature_enabled?(client, "new-checkout", "user-1")
```

---

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
`sdk_contract_flag`), `CONTRACT_USER_ID` (default random `smoke-<uuid>`). The script verifies the
fire-and-forget `track/5` calls by injecting a thin wrapper around the real HTTP client through
the public `:http_client` option. Set `MIX_QUIET=1` to keep Mix's compile messages off stdout.
The live runner (`tests/sdk-contract/live/run_live_contract.py`) uses the same command,
`cd sdk/elixir && mix run examples/contract_smoke.exs`, and requires `mix deps.get` to have been
run once.

CI does not run this SDK's contract smoke against a real API: `elixir` is not in the
`sdks:` list of the SDK Live Contract job in [pr-qa-gate.yml](https://github.com/getexperimently/experimently/blob/main/.github/workflows/pr-qa-gate.yml).
Run `python tests/sdk-contract/live/run_live_contract.py --sdk elixir --strict`
on a machine with Elixir 1.14+.

---

## Development

```bash
cd sdk/elixir
mix deps.get
mix test
elixir test_standalone.exs
```

- `mix test`: ExUnit; HTTP mocked through HttpBehaviour modules
- `elixir test_standalone.exs`: checks its own copy of the hash formula without mix; it does not
  load the SDK

CI runs `mix deps.get` and `mix test` on Elixir 1.16.3 in the SDK Unit Tests workflow
([sdk-unit-tests.yml](https://github.com/getexperimently/experimently/blob/main/.github/workflows/sdk-unit-tests.yml)), daily and on a pull request that changes `sdk/elixir/`. No workflow runs
`elixir test_standalone.exs`.
