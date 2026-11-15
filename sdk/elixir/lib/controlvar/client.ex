defmodule Controlvar.Client do
  @moduledoc """
  GenServer client for the Controlvar public API.

  Flag evaluation and experiment assignment are decided by the server; the
  SDK never buckets users locally. Successful results are cached per
  user + key (in a per-client ETS cache) for `cache_ttl` seconds; failures are
  never cached.

    - `evaluate_flag/4`   : `GET  /api/v1/feature-flags/evaluate/{key}?user_id=...`
    - `get_assignment/4`  : `POST /api/v1/tracking/assign`   (sticky on the server)
    - `track/5`           : `POST /api/v1/tracking/track`    (with a key)
                            `POST /api/v1/tracking/batch`    (fan-out without a key)
    - `track_batch/2`     : `POST /api/v1/tracking/batch`

  The GenServer owns the configuration, the cache and the in-flight
  fire-and-forget track requests. Evaluations, assignments and batches run in
  the **calling** process, so many processes can use one client concurrently;
  only `track/5` is delegated to the client (it spawns one process per request
  and `stop/1` waits for them, up to `timeout`, before exiting).

  ## Usage

      {:ok, client} = Controlvar.Client.start_link(
        base_url: "http://localhost:8000",
        api_key: "your-api-key"
      )

      {:ok, %Controlvar.FlagEvaluation{enabled: enabled}} =
        Controlvar.Client.evaluate_flag(client, "my-flag", "user-123")

      {:ok, %Controlvar.Assignment{variant_name: variant}} =
        Controlvar.Client.get_assignment(client, "experiment-key", "user-123")

      :ok = Controlvar.Client.track(client, "purchase", "user-123", %{sku: "pro"},
              experiment_key: "experiment-key", value: 12.5)

      Controlvar.Client.stop(client)
  """

  use GenServer, restart: :temporary

  require Logger

  alias Controlvar.{Assignment, BatchResult, Cache, Config, FlagEvaluation}

  @batch_limit 100

  @type server :: GenServer.server()
  @type track_opt ::
          {:experiment_key, String.t()}
          | {:feature_flag_key, String.t()}
          | {:value, number()}
          | {:event_type, String.t()}
          | {:timestamp, DateTime.t() | String.t()}

  # --- Client API ---

  @doc """
  Start a new Client GenServer.

  Accepts a `%Config{}` or the keyword/map options `Config.new/1` understands,
  plus `:name` to register the process.
  """
  @spec start_link(keyword() | map() | Config.t()) :: GenServer.on_start()
  def start_link(%Config{} = config) do
    GenServer.start_link(__MODULE__, config)
  end

  def start_link(opts) when is_map(opts), do: start_link(Enum.to_list(opts))

  def start_link(opts) when is_list(opts) do
    {name, opts} = Keyword.pop(opts, :name)
    GenServer.start_link(__MODULE__, Config.new(opts), name: name)
  end

  @doc """
  Evaluate a feature flag for a user. The server decides.

  Returns `{:ok, %FlagEvaluation{key, enabled, config}}`, or `{:error, reason}`
  on a network/HTTP failure (including `{:api_error, 404, _}` for an unknown
  flag key; a flag that is not ACTIVE is `{:ok, _}` with `enabled: false`). A
  cached evaluation, when present, is returned instead of calling the server.
  Failures are never cached.

  ## Options
  - `:skip_cache` — bypass the cache and ask the server (default: `false`)
  - `:attributes` — accepted for symmetry with `get_assignment/4` and not sent:
    the evaluate endpoint takes a context, which this SDK does not send yet
  """
  @spec evaluate_flag(server(), String.t(), String.t(), keyword()) ::
          {:ok, FlagEvaluation.t()} | {:error, term()}
  def evaluate_flag(server, flag_key, user_id, opts \\ []) do
    %{config: config, cache: cache, http: http} = context(server)
    cache_key = {:flag, user_id, flag_key}

    case cached(cache, cache_key, opts) do
      %FlagEvaluation{} = evaluation ->
        {:ok, evaluation}

      nil ->
        path =
          "/api/v1/feature-flags/evaluate/#{encode(flag_key)}?user_id=#{encode(user_id)}"

        case http.get(config, path) do
          {:ok, data} when is_map(data) ->
            evaluation = %FlagEvaluation{
              key: flag_key,
              enabled: Map.get(data, "enabled") == true,
              config: Map.get(data, "config")
            }

            :ok = Cache.put(cache, cache_key, evaluation)
            {:ok, evaluation}

          {:ok, other} ->
            {:error, {:malformed_response, other}}

          {:error, reason} ->
            {:error, reason}
        end
    end
  end

  @doc """
  Boolean convenience around `evaluate_flag/3`: `false` on any failure.
  """
  @spec feature_enabled?(server(), String.t(), String.t()) :: boolean()
  def feature_enabled?(server, flag_key, user_id) do
    case evaluate_flag(server, flag_key, user_id) do
      {:ok, %FlagEvaluation{enabled: enabled}} -> enabled == true
      {:error, _reason} -> false
    end
  end

  @doc """
  Assign a user to an experiment (sticky on the server, records the exposure).

  `attributes` (a map) is sent as `context` for targeting rules. A keyword list
  with `:attributes` and/or `:skip_cache` is accepted as well.

  Returns `{:ok, %Assignment{experiment_key, variant_id, variant_name, is_control, configuration}}`,
  or `{:error, reason}` on a network/HTTP failure (including
  `{:api_error, 404, _}` when no experiment has the key or a draft has no
  control variant). An experiment that is not ACTIVE answers `{:ok, _}` with
  its control variant. A cached
  assignment, when present, is returned instead of calling the server.
  Failures are never cached.
  """
  @spec get_assignment(server(), String.t(), String.t(), map() | keyword()) ::
          {:ok, Assignment.t()} | {:error, term()}
  def get_assignment(server, experiment_key, user_id, attributes_or_opts \\ %{})

  def get_assignment(server, experiment_key, user_id, attributes) when is_map(attributes) do
    do_get_assignment(server, experiment_key, user_id, attributes, [])
  end

  def get_assignment(server, experiment_key, user_id, opts) when is_list(opts) do
    do_get_assignment(server, experiment_key, user_id, Keyword.get(opts, :attributes, %{}), opts)
  end

  defp do_get_assignment(server, experiment_key, user_id, attributes, opts) do
    %{config: config, cache: cache, http: http} = context(server)
    cache_key = {:assignment, user_id, experiment_key}

    case cached(cache, cache_key, opts) do
      %Assignment{} = assignment ->
        {:ok, assignment}

      nil ->
        body = %{experiment_key: experiment_key, user_id: user_id}

        body =
          if is_map(attributes) and map_size(attributes) > 0,
            do: Map.put(body, :context, attributes),
            else: body

        case http.post(config, "/api/v1/tracking/assign", body) do
          {:ok, %{"variant_name" => variant_name} = data} when is_binary(variant_name) ->
            assignment = %Assignment{
              experiment_key: string_or(Map.get(data, "experiment_key"), experiment_key),
              variant_id: Map.get(data, "variant_id"),
              variant_name: variant_name,
              is_control: Map.get(data, "is_control") == true,
              configuration: map_or_nil(Map.get(data, "configuration"))
            }

            :ok = Cache.put(cache, cache_key, assignment)
            {:ok, assignment}

          {:ok, other} ->
            {:error, {:malformed_response, other}}

          {:error, reason} ->
            {:error, reason}
        end
    end
  end

  @doc """
  Track an event (fire-and-forget). Always returns `:ok` immediately; failures
  are logged as warnings and never raise.

  With `:experiment_key` and/or `:feature_flag_key` in `opts` one
  `POST /api/v1/tracking/track` is sent. Without a key the event is fanned out
  through `POST /api/v1/tracking/batch`: one entry per cached assignment plus
  one per cached evaluated flag for this user. If nothing is cached for the
  user, nothing is sent.

  `properties` is sent as `metadata`. Other `opts`: `:value` (number),
  `:event_type` (defaults to `event_name`), `:timestamp` (`DateTime` or
  ISO-8601 string; defaults to now, UTC).

  `stop/1` waits (up to `timeout`) for in-flight track requests.
  """
  @spec track(server(), String.t(), String.t(), map(), [track_opt()]) :: :ok
  def track(server, event_name, user_id, properties \\ %{}, opts \\ []) do
    GenServer.cast(server, {:track, event_name, user_id, properties, opts})
  end

  @doc """
  Track several events in one go via `POST /api/v1/tracking/batch` (chunked
  into requests of at most #{@batch_limit} events). Runs in the calling process.

  Each event is a map (atom or string keys) with `event_name` and `user_id`
  (required). Optional: `experiment_key`, `feature_flag_key`, `properties`
  (sent as `metadata`), `value`, `event_type`, `timestamp`. An event with no
  key is stored as history and counts in no experiment's results; tag
  outcome events.

  Returns `{:ok, %BatchResult{}}` with the server's counts (malformed events
  are counted as failures locally without being sent), or `{:error, reason}`
  when a request fails.
  """
  @spec track_batch(server(), [map()]) :: {:ok, BatchResult.t()} | {:error, term()}
  def track_batch(server, events) when is_list(events) do
    %{config: config, http: http} = context(server)

    {bodies, local_errors} =
      events
      |> Enum.with_index()
      |> Enum.reduce({[], []}, fn {event, index}, {bodies, errors} ->
        case normalize_event(event) do
          {:ok, body} -> {[body | bodies], errors}
          {:error, message} -> {bodies, [%{index: index, error: message} | errors]}
        end
      end)

    initial = %BatchResult{
      success_count: 0,
      failure_count: length(local_errors),
      errors: Enum.reverse(local_errors)
    }

    bodies
    |> Enum.reverse()
    |> Enum.chunk_every(@batch_limit)
    |> Enum.reduce_while({:ok, initial}, fn chunk, {:ok, acc} ->
      case http.post(config, "/api/v1/tracking/batch", %{events: chunk}) do
        {:ok, response} -> {:cont, {:ok, merge_batch_response(acc, response, length(chunk))}}
        {:error, reason} -> {:halt, {:error, reason}}
      end
    end)
    |> case do
      {:ok, %BatchResult{errors: []} = result} -> {:ok, %{result | errors: nil}}
      other -> other
    end
  end

  @doc "Cached (successful, unexpired) assignments for the user, oldest first."
  @spec assignments(server(), String.t()) :: [Assignment.t()]
  def assignments(server, user_id) do
    %{cache: cache} = context(server)
    Cache.list(cache, :assignment, user_id)
  end

  @doc "Keys of flags successfully evaluated (and still cached) for the user."
  @spec evaluated_flags(server(), String.t()) :: [String.t()]
  def evaluated_flags(server, user_id) do
    %{cache: cache} = context(server)
    cache |> Cache.list(:flag, user_id) |> Enum.map(& &1.key)
  end

  @doc "Drop every cached evaluation and assignment."
  @spec clear_cache(server()) :: :ok
  def clear_cache(server) do
    %{cache: cache} = context(server)
    Cache.clear(cache)
  end

  @doc """
  Stop the client. Waits (up to `timeout` per request) for in-flight
  fire-and-forget track requests, then stops the cache.
  """
  @spec stop(server()) :: :ok
  def stop(server) do
    GenServer.stop(server)
  end

  # --- GenServer Callbacks ---

  @impl true
  def init(%Config{} = config) do
    # Each client has its own ETS-backed cache (not the global named one)
    {:ok, cache_pid} = Cache.start_link_unnamed(config)

    state = %{
      config: config,
      cache: cache_pid,
      http: config.http_client || Controlvar.HttpClient,
      inflight: MapSet.new()
    }

    {:ok, state}
  end

  @impl true
  def handle_call(:context, _from, state) do
    {:reply, Map.take(state, [:config, :cache, :http]), state}
  end

  @impl true
  def handle_cast({:track, event_name, user_id, properties, opts}, state) do
    requests =
      try do
        track_requests(state.cache, event_name, user_id, properties, opts)
      rescue
        error ->
          Logger.warning(
            "Controlvar: could not build track request for #{inspect(event_name)}: " <>
              Exception.message(error)
          )

          []
      end

    state = Enum.reduce(requests, state, &spawn_request(&2, &1, event_name))
    {:noreply, state}
  end

  @impl true
  def handle_info({:DOWN, ref, :process, _pid, _reason}, state) do
    {:noreply, %{state | inflight: MapSet.delete(state.inflight, ref)}}
  end

  def handle_info(_message, state), do: {:noreply, state}

  @impl true
  def terminate(_reason, state) do
    Enum.each(state.inflight, fn ref ->
      receive do
        {:DOWN, ^ref, :process, _pid, _reason} -> :ok
      after
        state.config.timeout -> :ok
      end
    end)

    if Process.alive?(state.cache), do: GenServer.stop(state.cache)
    :ok
  end

  # --- Private Helpers ---

  defp context(server), do: GenServer.call(server, :context)

  defp cached(cache, cache_key, opts) do
    if Keyword.get(opts, :skip_cache, false), do: nil, else: Cache.get(cache, cache_key)
  end

  # Percent-encode a path segment / query value (RFC 3986 unreserved characters kept).
  defp encode(value), do: URI.encode(to_string(value), &URI.char_unreserved?/1)

  defp string_or(value, _default) when is_binary(value) and value != "", do: value
  defp string_or(_value, default), do: default

  defp map_or_nil(value) when is_map(value), do: value
  defp map_or_nil(_value), do: nil

  # Requests ([{path, body}]) for one track call, resolving the fan-out rule.
  defp track_requests(cache, event_name, user_id, properties, opts) do
    base = event_body(event_name, user_id, properties, opts)
    experiment_key = Keyword.get(opts, :experiment_key)
    feature_flag_key = Keyword.get(opts, :feature_flag_key)

    if experiment_key || feature_flag_key do
      body =
        base
        |> maybe_put(:experiment_key, experiment_key)
        |> maybe_put(:feature_flag_key, feature_flag_key)

      [{"/api/v1/tracking/track", body}]
    else
      assignment_events =
        cache
        |> Cache.list(:assignment, user_id)
        |> Enum.map(&Map.put(base, :experiment_key, &1.experiment_key))

      flag_events =
        cache
        |> Cache.list(:flag, user_id)
        |> Enum.map(&Map.put(base, :feature_flag_key, &1.key))

      (assignment_events ++ flag_events)
      |> Enum.chunk_every(@batch_limit)
      |> Enum.map(&{"/api/v1/tracking/batch", %{events: &1}})
    end
  end

  defp spawn_request(state, {path, body}, event_name) do
    %{config: config, http: http} = state

    {_pid, ref} =
      spawn_monitor(fn ->
        case http.post(config, path, body) do
          {:ok, _response} ->
            :ok

          {:error, reason} ->
            Logger.warning(
              "Controlvar: failed to track event #{inspect(event_name)} " <>
                "(#{path}): #{inspect(reason)}"
            )
        end
      end)

    %{state | inflight: MapSet.put(state.inflight, ref)}
  end

  # Body of a /tracking/track request (and of each /tracking/batch entry), without keys.
  defp event_body(event_name, user_id, properties, opts) do
    event_name = to_string(event_name)

    %{
      event_type: to_string(Keyword.get(opts, :event_type) || event_name),
      event_name: event_name,
      user_id: to_string(user_id),
      timestamp: format_timestamp(Keyword.get(opts, :timestamp))
    }
    |> maybe_put(:value, Keyword.get(opts, :value))
    |> maybe_put(:metadata, if(is_map(properties) and map_size(properties) > 0, do: properties))
  end

  defp maybe_put(map, _key, nil), do: map
  defp maybe_put(map, key, value), do: Map.put(map, key, value)

  defp format_timestamp(nil), do: DateTime.utc_now() |> DateTime.to_iso8601()
  defp format_timestamp(%DateTime{} = dt), do: DateTime.to_iso8601(dt)
  defp format_timestamp(other), do: to_string(other)

  # Turn a user-supplied event map into a /tracking/batch entry.
  defp normalize_event(event) when is_map(event) do
    event_name = field(event, :event_name) || field(event, :event)
    user_id = field(event, :user_id)

    cond do
      not (is_binary(event_name) and event_name != "") ->
        {:error, "event_name is required"}

      is_nil(user_id) or to_string(user_id) == "" ->
        {:error, "user_id is required"}

      true ->
        properties = field(event, :properties) || field(event, :metadata) || %{}

        opts =
          [
            value: field(event, :value),
            event_type: field(event, :event_type),
            timestamp: field(event, :timestamp)
          ]
          |> Enum.reject(fn {_k, v} -> is_nil(v) end)

        body =
          event_name
          |> event_body(user_id, properties, opts)
          |> maybe_put(:experiment_key, field(event, :experiment_key))
          |> maybe_put(:feature_flag_key, field(event, :feature_flag_key))

        {:ok, body}
    end
  end

  defp normalize_event(_event), do: {:error, "event must be a map"}

  defp field(event, key) when is_atom(key) do
    case Map.get(event, key) do
      nil -> Map.get(event, Atom.to_string(key))
      value -> value
    end
  end

  defp merge_batch_response(%BatchResult{} = acc, response, chunk_size) when is_map(response) do
    server_errors =
      case Map.get(response, "errors") do
        errors when is_list(errors) -> errors
        _ -> []
      end

    %BatchResult{
      success_count: acc.success_count + integer_or(Map.get(response, "success_count"), chunk_size),
      failure_count: acc.failure_count + integer_or(Map.get(response, "failure_count"), 0),
      errors: acc.errors ++ server_errors
    }
  end

  defp merge_batch_response(acc, _response, chunk_size) do
    %BatchResult{acc | success_count: acc.success_count + chunk_size}
  end

  defp integer_or(value, _default) when is_integer(value), do: value
  defp integer_or(_value, default), do: default
end
