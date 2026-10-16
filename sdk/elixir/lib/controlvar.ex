defmodule Controlvar do
  @moduledoc """
  Elixir SDK for Controlvar.

  Flag evaluation and experiment assignment are decided **by the server**:
  every call goes to the public API with your `X-API-Key`, the server buckets
  the user (sticky per user + experiment), and the SDK caches the answer per
  user + key. Nothing is bucketed locally.

  - GenServer-based client lifecycle with a per-client ETS cache (TTL, max size)
  - OTP `:httpc`-based HTTP (zero external runtime deps beyond Jason)
  - Behaviour-based HTTP client for easy testing with mock modules
  - Cross-SDK MD5 hash utility (`Controlvar.Evaluator.hash_user/2`)

  ## Quick Start

      {:ok, client} = Controlvar.start(
        base_url: "http://localhost:8000",
        api_key: System.fetch_env!("EXPERIMENTLY_API_KEY")
      )

      # Feature flag (GET /api/v1/feature-flags/evaluate/{key}?user_id=...)
      case Controlvar.evaluate_flag(client, "dark-mode", "user-123") do
        {:ok, %Controlvar.FlagEvaluation{enabled: true, config: config}} -> render_dark_mode(config)
        {:ok, _disabled} -> render_default()
        {:error, reason} -> render_default()   # network/HTTP failure, unknown flag key
      end

      Controlvar.feature_enabled?(client, "dark-mode", "user-123")   # => true | false

      # Experiment assignment (POST /api/v1/tracking/assign — sticky on the server)
      {:ok, %Controlvar.Assignment{variant_name: variant}} =
        Controlvar.get_assignment(client, "checkout-experiment", "user-123", %{plan: "pro"})

      # Events (fire-and-forget, always :ok)
      Controlvar.track(client, "purchase", "user-123", %{sku: "pro"},
        experiment_key: "checkout-experiment", value: 49.99)
      Controlvar.track(client, "page_view", "user-123", %{page: "/"})   # no key: fanned out

  ## Configuration Options

  | Option            | Type    | Required | Default    | Description                                   |
  |-------------------|---------|----------|------------|-----------------------------------------------|
  | `:base_url`       | string  | yes      | —          | Backend origin; the SDK appends `/api/v1/...` |
  | `:api_key`        | string  | yes      | —          | API key, sent as `X-API-Key`                  |
  | `:cache_ttl`      | integer | no       | 300        | Seconds a successful result is reused         |
  | `:timeout`        | integer | no       | 10_000     | HTTP timeout in milliseconds                  |
  | `:max_cache_size` | integer | no       | 1_000      | Maximum number of cached entries              |
  | `:http_client`    | module  | no       | HttpClient | `HttpBehaviour` implementation (for testing)  |
  | `:name`           | atom    | no       | —          | Register the client process under a name      |

  ## Architecture

  Each call to `start/1` creates a `Client` GenServer (config, in-flight track
  requests) and a `Cache` GenServer backed by a private ETS table. Evaluations,
  assignments and batches run in the calling process; `track/5` is a cast.
  Multiple independent clients can be created.

  ## Hash Compatibility

      Controlvar.Evaluator.hash_user("user-123", "my-flag")
      # => 0.6927449859213084

  This value is identical across every platform SDK. It is exported as a
  utility only — nothing in the SDK uses it to decide a variant.
  """

  alias Controlvar.{Assignment, BatchResult, Client, FlagEvaluation}

  @doc """
  Start a new client. Returns `{:ok, pid}` or `{:error, reason}`; raises
  `ArgumentError` when `:base_url` or `:api_key` is missing.

  ## Examples

      {:ok, client} = Controlvar.start(base_url: "http://localhost:8000", api_key: "sk-...")

      {:ok, client} = Controlvar.start(
        base_url: "http://localhost:8000",
        api_key: "sk-...",
        cache_ttl: 60,
        timeout: 5_000,
        name: MyApp.Experiments
      )
  """
  @spec start(keyword() | map() | Controlvar.Config.t()) ::
          {:ok, pid()} | {:error, term()}
  def start(config_opts) do
    Client.start_link(config_opts)
  end

  @doc """
  Evaluate a feature flag for a user (the server decides).

  Returns `{:ok, %FlagEvaluation{key, enabled, config}}` or `{:error, reason}`.
  See `Controlvar.Client.evaluate_flag/4`.
  """
  @spec evaluate_flag(Client.server(), String.t(), String.t(), keyword()) ::
          {:ok, FlagEvaluation.t()} | {:error, term()}
  def evaluate_flag(client, flag_key, user_id, opts \\ []) do
    Client.evaluate_flag(client, flag_key, user_id, opts)
  end

  @doc """
  `true` when the server enables the flag for the user, `false` otherwise
  (including on any failure).
  """
  @spec feature_enabled?(Client.server(), String.t(), String.t()) :: boolean()
  def feature_enabled?(client, flag_key, user_id) do
    Client.feature_enabled?(client, flag_key, user_id)
  end

  @doc """
  Assign a user to an experiment (sticky on the server). `attributes` is sent
  as `context` for targeting rules.

  Returns `{:ok, %Assignment{experiment_key, variant_id, variant_name, is_control, configuration}}`
  or `{:error, reason}`. See `Controlvar.Client.get_assignment/4`.
  """
  @spec get_assignment(Client.server(), String.t(), String.t(), map() | keyword()) ::
          {:ok, Assignment.t()} | {:error, term()}
  def get_assignment(client, experiment_key, user_id, attributes \\ %{}) do
    Client.get_assignment(client, experiment_key, user_id, attributes)
  end

  @doc """
  Track an event (fire-and-forget). Always returns `:ok`; never raises.

  `opts`: `:experiment_key`, `:feature_flag_key`, `:value`, `:event_type`,
  `:timestamp`. Without a key the event is fanned out to every cached
  assignment and evaluated flag for the user (nothing cached — nothing sent).
  See `Controlvar.Client.track/5`.
  """
  @spec track(Client.server(), String.t(), String.t(), map(), keyword()) :: :ok
  def track(client, event_name, user_id, properties \\ %{}, opts \\ []) do
    Client.track(client, event_name, user_id, properties, opts)
  end

  @doc """
  Track several events via `POST /api/v1/tracking/batch` (max 100 per request).

  Returns `{:ok, %BatchResult{}}` or `{:error, reason}`.
  See `Controlvar.Client.track_batch/2`.
  """
  @spec track_batch(Client.server(), [map()]) :: {:ok, BatchResult.t()} | {:error, term()}
  def track_batch(client, events) do
    Client.track_batch(client, events)
  end

  @doc "Cached (successful, unexpired) assignments for the user."
  @spec assignments(Client.server(), String.t()) :: [Assignment.t()]
  def assignments(client, user_id), do: Client.assignments(client, user_id)

  @doc "Keys of flags successfully evaluated (and still cached) for the user."
  @spec evaluated_flags(Client.server(), String.t()) :: [String.t()]
  def evaluated_flags(client, user_id), do: Client.evaluated_flags(client, user_id)

  @doc "Drop every cached evaluation and assignment."
  @spec clear_cache(Client.server()) :: :ok
  def clear_cache(client), do: Client.clear_cache(client)

  @doc """
  Stop the client and free associated resources. Waits (up to the configured
  `timeout`) for in-flight fire-and-forget track requests.
  """
  @spec stop(Client.server()) :: :ok
  def stop(client) do
    Client.stop(client)
  end
end
