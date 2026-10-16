# Contract smoke for the Elixir SDK against a live backend.
#
# Run from the repository root (fetch deps once with `mix deps.get`):
#
#     cd sdk/elixir && CONTROLVAR_API_KEY=<key> mix run examples/contract_smoke.exs
#
# Env: CONTROLVAR_API_URL (default http://localhost:8000), CONTROLVAR_API_KEY
# (required), CONTRACT_EXPERIMENT_KEY (default sdk_contract_ab), CONTRACT_FLAG_KEY
# (default sdk_contract_flag), CONTRACT_USER_ID (default: random "smoke-<uuid>").
#
# Prints exactly one JSON line on stdout and exits 0; on failure prints one line
# to stderr and exits 1. Set MIX_QUIET=1 to silence Mix's own compile messages.

defmodule ContractSmoke.Http do
  @moduledoc false
  # `track/5` is fire-and-forget (a GenServer cast that always returns `:ok`),
  # so to verify delivery the smoke injects this wrapper through the public
  # `:http_client` option: it delegates to the real HTTP client and reports
  # every failed request to the smoke process.

  @behaviour Controlvar.HttpBehaviour

  alias Controlvar.HttpClient

  @impl true
  def get(config, path), do: HttpClient.get(config, path)

  @impl true
  def post(config, path, body) do
    result = HttpClient.post(config, path, body)

    case result do
      {:error, reason} -> notify({:request_failed, path, reason})
      _ -> :ok
    end

    result
  end

  defp notify(message) do
    case Process.whereis(ContractSmoke) do
      nil -> :ok
      pid -> send(pid, message)
    end
  end
end

defmodule ContractSmoke do
  @moduledoc false

  alias Controlvar, as: EP
  alias Controlvar.{Assignment, BatchResult, FlagEvaluation}

  def run do
    api_key = env("CONTROLVAR_API_KEY", System.get_env("EXPERIMENTLY_API_KEY"))
    if api_key in [nil, ""], do: fail("CONTROLVAR_API_KEY is required")

    api_url = env("CONTROLVAR_API_URL", env("EXPERIMENTLY_API_URL", "http://localhost:8000"))
    experiment_key = env("CONTRACT_EXPERIMENT_KEY", "sdk_contract_ab")
    flag_key = env("CONTRACT_FLAG_KEY", "sdk_contract_flag")
    user_id = env("CONTRACT_USER_ID", random_user_id())
    attributes = %{source: "contract_smoke", sdk: "elixir"}

    Process.register(self(), __MODULE__)

    client =
      case EP.start(
             base_url: api_url,
             api_key: api_key,
             timeout: 10_000,
             http_client: ContractSmoke.Http
           ) do
        {:ok, pid} -> pid
        {:error, reason} -> fail("could not start client: #{inspect(reason)}")
      end

    # 1. Assign twice. The cache is cleared in between so the second call really
    #    goes to the server and proves the assignment is sticky there.
    first = assign!(client, experiment_key, user_id, attributes)
    :ok = EP.clear_cache(client)
    second = assign!(client, experiment_key, user_id, attributes)

    unless first.variant_name in ["control", "treatment"] do
      fail("unexpected variant_name #{inspect(first.variant_name)}")
    end

    sticky = first == second

    unless sticky do
      fail("assignment not sticky: #{first.variant_name} then #{second.variant_name}")
    end

    # 2. Evaluate the flag.
    flag =
      case EP.evaluate_flag(client, flag_key, user_id) do
        {:ok, %FlagEvaluation{} = flag} -> flag
        {:error, reason} -> fail("flag evaluation failed: #{inspect(reason)}")
      end

    unless is_boolean(flag.enabled), do: fail("flag.enabled is not a bool: #{inspect(flag.enabled)}")

    # 3. Track with an experiment key -> POST /tracking/track (fire-and-forget).
    :ok =
      EP.track(client, "purchase", user_id, %{sdk: "elixir"},
        experiment_key: experiment_key,
        value: 12.5
      )

    # 4. Track without a key -> fans out to the cached assignment + flag via
    #    POST /tracking/batch, then an explicit 2-event batch (synchronous).
    :ok = EP.track(client, "page_view", user_id, %{page: "/smoke"})

    case EP.track_batch(client, [
           %{event_name: "page_view", user_id: user_id, experiment_key: experiment_key},
           %{event_name: "click", user_id: user_id, feature_flag_key: flag_key}
         ]) do
      {:ok, %BatchResult{failure_count: 0}} -> :ok
      {:ok, %BatchResult{} = result} -> fail("track_batch rejected events: #{inspect(result.errors)}")
      {:error, reason} -> fail("track_batch failed: #{inspect(reason)}")
    end

    # stop/1 waits for the in-flight fire-and-forget requests; any failure was
    # reported to this process by ContractSmoke.Http.
    :ok = EP.stop(client)

    failures = drain_failures()

    if Enum.any?(failures, fn {path, _} -> String.ends_with?(path, "/tracking/track") end) do
      fail("track(purchase) with experiment_key failed: #{inspect(failures)}")
    end

    if Enum.any?(failures, fn {path, _} -> String.ends_with?(path, "/tracking/batch") end) do
      fail("track(page_view) fan-out failed: #{inspect(failures)}")
    end

    report =
      Jason.OrderedObject.new(
        sdk: "elixir",
        assign:
          Jason.OrderedObject.new(
            variant_name: first.variant_name,
            is_control: first.is_control,
            sticky: sticky
          ),
        flag: Jason.OrderedObject.new(enabled: flag.enabled),
        track: Jason.OrderedObject.new(ok: true),
        fanout: Jason.OrderedObject.new(ok: true)
      )

    IO.puts(Jason.encode!(report))
  end

  defp assign!(client, experiment_key, user_id, attributes) do
    case EP.get_assignment(client, experiment_key, user_id, attributes) do
      {:ok, %Assignment{} = assignment} -> assignment
      {:error, reason} -> fail("assign failed: #{inspect(reason)}")
    end
  end

  defp drain_failures(acc \\ []) do
    receive do
      {:request_failed, path, reason} -> drain_failures([{path, reason} | acc])
    after
      0 -> Enum.reverse(acc)
    end
  end

  defp env(name, default) do
    case System.get_env(name) do
      nil -> default
      "" -> default
      value -> value
    end
  end

  defp random_user_id do
    <<a::binary-size(4), b::binary-size(2), c::binary-size(2), d::binary-size(2),
      e::binary-size(6)>> = :crypto.strong_rand_bytes(16)

    "smoke-" <> Enum.map_join([a, b, c, d, e], "-", &Base.encode16(&1, case: :lower))
  end

  @spec fail(String.t()) :: no_return()
  defp fail(message) do
    IO.puts(:stderr, "contract_smoke: " <> message)
    System.halt(1)
  end
end

try do
  ContractSmoke.run()
rescue
  error ->
    message = error |> Exception.message() |> String.split("\n") |> hd()
    IO.puts(:stderr, "contract_smoke: #{inspect(error.__struct__)}: #{message}")
    System.halt(1)
catch
  :exit, reason ->
    IO.puts(:stderr, "contract_smoke: exit: #{inspect(reason)}")
    System.halt(1)
end
