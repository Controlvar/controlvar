# Basic Usage Example — Controlvar Elixir SDK
#
# Flag evaluation and experiment assignment are decided by the server through
# the public API; this script shows every public call. Point it at a running
# backend with CONTROLVAR_API_URL / CONTROLVAR_API_KEY.
#
# Run with:
#   cd sdk/elixir
#   mix deps.get
#   CONTROLVAR_API_KEY=<key> mix run examples/basic_usage.exs
# (earlier versions of this example read EXPERIMENTLY_API_URL / EXPERIMENTLY_API_KEY.)

alias Controlvar.{Assignment, BatchResult, FlagEvaluation}

# -------------------------------------------------------------------
# 1. Start the client
# -------------------------------------------------------------------
IO.puts("Starting Controlvar client...")

{:ok, client} =
  Controlvar.start(
    base_url: System.get_env("CONTROLVAR_API_URL", "http://localhost:8000"),
    api_key: System.get_env("CONTROLVAR_API_KEY", "your-api-key-here"),
    # Seconds a successful evaluation/assignment is reused
    cache_ttl: 300,
    # HTTP timeout in milliseconds
    timeout: 10_000,
    # Keep up to 1000 entries in the cache
    max_cache_size: 1_000
  )

IO.puts("Client started: #{inspect(client)}\n")

# -------------------------------------------------------------------
# 2. Evaluate a feature flag (GET /api/v1/feature-flags/evaluate/{key}?user_id=...)
# -------------------------------------------------------------------
user_id = "user-#{System.unique_integer([:positive])}"

IO.puts("Evaluating feature flag 'dark-mode' for user #{user_id}...")

case Controlvar.evaluate_flag(client, "dark-mode", user_id) do
  {:ok, %FlagEvaluation{enabled: true, config: config}} ->
    IO.puts("  => Enabled for this user. Config: #{inspect(config)}")

  {:ok, %FlagEvaluation{enabled: false}} ->
    IO.puts("  => Disabled for this user — showing default light mode")

  {:error, {:auth_error, 401}} ->
    IO.puts("  => Authentication failed — check your API key")

  {:error, {:api_error, 404, _body}} ->
    IO.puts("  => Flag not found — using default")

  {:error, {:network_error, reason}} ->
    IO.puts("  => Network error: #{inspect(reason)} — using default")

  {:error, reason} ->
    IO.puts("  => Error: #{inspect(reason)} — using default")
end

# Boolean shorthand: false on any failure.
IO.puts("  feature_enabled?: #{Controlvar.feature_enabled?(client, "dark-mode", user_id)}\n")

# -------------------------------------------------------------------
# 3. Get an experiment assignment (POST /api/v1/tracking/assign — sticky on the server)
#    User attributes are sent as `context` for targeting rules.
# -------------------------------------------------------------------
IO.puts("Getting experiment assignment for 'checkout-flow-experiment'...")

case Controlvar.get_assignment(client, "checkout-flow-experiment", user_id, %{
       plan: "pro",
       country: "US"
     }) do
  {:ok, %Assignment{is_control: true}} ->
    IO.puts("  => User in CONTROL group — showing existing checkout")

  {:ok, %Assignment{variant_name: variant, configuration: configuration}} ->
    IO.puts("  => User in variant #{variant} — configuration: #{inspect(configuration)}")

  {:error, {:api_error, 404, _body}} ->
    IO.puts("  => Experiment not found or not ACTIVE — showing default checkout")

  {:error, reason} ->
    IO.puts("  => Assignment error: #{inspect(reason)} — showing default checkout")
end

IO.puts("")

# -------------------------------------------------------------------
# 4. Track events (fire-and-forget, always :ok)
# -------------------------------------------------------------------
IO.puts("Tracking events...")

# With a key: one POST /api/v1/tracking/track attributed to the experiment.
:ok =
  Controlvar.track(client, "purchase_completed", user_id, %{order_id: "ord-12345"},
    experiment_key: "checkout-flow-experiment",
    value: 99.99
  )

IO.puts("  => Tracked: purchase_completed (experiment_key + value)")

# With a flag key.
:ok =
  Controlvar.track(client, "theme_toggled", user_id, %{to: "dark"},
    feature_flag_key: "dark-mode"
  )

IO.puts("  => Tracked: theme_toggled (feature_flag_key)")

# Without a key: fanned out through POST /api/v1/tracking/batch to every
# experiment the user was assigned to and every flag evaluated for the user
# through this client. Nothing cached -> nothing sent.
:ok = Controlvar.track(client, "page_viewed", user_id, %{page: "checkout"})
IO.puts("  => Tracked: page_viewed (fanned out to cached assignments + flags)")

# Several events at once (synchronous, max 100 per request).
case Controlvar.track_batch(client, [
       %{event_name: "click", user_id: user_id, experiment_key: "checkout-flow-experiment"},
       %{event_name: "click", user_id: user_id, feature_flag_key: "dark-mode"}
     ]) do
  {:ok, %BatchResult{success_count: ok, failure_count: failed}} ->
    IO.puts("  => Batch: #{ok} accepted, #{failed} failed")

  {:error, reason} ->
    IO.puts("  => Batch error: #{inspect(reason)}")
end

IO.puts("")

# -------------------------------------------------------------------
# 5. Cache helpers
# -------------------------------------------------------------------
IO.puts("Cached for #{user_id}:")
IO.puts("  assignments:     #{inspect(Controlvar.assignments(client, user_id))}")
IO.puts("  evaluated flags: #{inspect(Controlvar.evaluated_flags(client, user_id))}")
IO.puts("")

# -------------------------------------------------------------------
# 6. Direct hash inspection (cross-SDK compatibility check; utility only)
# -------------------------------------------------------------------
IO.puts("Cross-SDK hash check:")
h = Controlvar.Evaluator.hash_user("user-123", "my-flag")
expected = 0.6927449859213084
diff = abs(h - expected)
IO.puts("  hash_user(\"user-123\", \"my-flag\") = #{h}")
IO.puts("  expected:                           #{expected}")
IO.puts("  parity: #{if diff < 1.0e-10, do: "OK (matches all SDKs)", else: "MISMATCH!"}")

IO.puts("")

# -------------------------------------------------------------------
# 7. Stop the client (waits for in-flight track requests)
# -------------------------------------------------------------------
IO.puts("Stopping client...")
Controlvar.stop(client)
IO.puts("Done.")
