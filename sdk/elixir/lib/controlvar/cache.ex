defmodule Controlvar.Cache do
  @moduledoc """
  ETS-backed GenServer cache for server results (flag evaluations and
  experiment assignments), keyed per user + key.

  Provides a process-safe, TTL-aware in-memory cache with a configurable
  maximum size. When the cache reaches `max_cache_size`, the oldest inserted
  entry is evicted to make room for a new one.

  ## Storage Format

  Each ETS entry is stored as `{key, value, expires_at_ms, seq}` where
  `expires_at_ms` is a monotonic millisecond timestamp and `seq` a strictly
  increasing insertion counter (used for ordering and eviction).

  The client uses tuple keys so entries can be listed per user:

  - `{:flag, user_id, flag_key}` -> `%Controlvar.FlagEvaluation{}`
  - `{:assignment, user_id, experiment_key}` -> `%Controlvar.Assignment{}`

  Writes are synchronous calls, so a `put/4` is complete before it returns.

  ## Usage

      {:ok, cache} = Cache.start_link_unnamed(config)
      :ok = Cache.put(cache, {:flag, "user-1", "my-flag"}, evaluation)
      Cache.get(cache, {:flag, "user-1", "my-flag"})
      Cache.list(cache, :flag, "user-1")
  """

  use GenServer

  alias Controlvar.Config

  @table_name :controlvar_cache

  # --- Client API ---

  @doc "Start the Cache GenServer registered under the module name."
  @spec start_link(Config.t()) :: GenServer.on_start()
  def start_link(config) do
    GenServer.start_link(__MODULE__, config, name: __MODULE__)
  end

  @doc "Start a Cache GenServer without registering a global name (one per client)."
  @spec start_link_unnamed(Config.t()) :: GenServer.on_start()
  def start_link_unnamed(config) do
    GenServer.start_link(__MODULE__, config)
  end

  @doc "Retrieve a cached value. Returns `nil` if missing or expired."
  @spec get(GenServer.server(), any()) :: any()
  def get(server \\ __MODULE__, key) do
    GenServer.call(server, {:get, key})
  end

  @doc """
  Store a value with an optional TTL override in seconds
  (`config.cache_ttl` when `nil`). Synchronous.
  """
  @spec put(GenServer.server(), any(), any(), non_neg_integer() | nil) :: :ok
  def put(server \\ __MODULE__, key, value, ttl_seconds \\ nil) do
    GenServer.call(server, {:put, key, value, ttl_seconds})
  end

  @doc """
  Live (non-expired) values whose key is `{type, user_id, _}`, oldest first.
  Expired entries encountered on the way are pruned.
  """
  @spec list(GenServer.server(), atom(), String.t()) :: [any()]
  def list(server \\ __MODULE__, type, user_id) do
    GenServer.call(server, {:list, type, user_id})
  end

  @doc "Delete a specific cache entry. Synchronous."
  @spec delete(GenServer.server(), any()) :: :ok
  def delete(server \\ __MODULE__, key) do
    GenServer.call(server, {:delete, key})
  end

  @doc "Clear all cache entries. Synchronous."
  @spec clear(GenServer.server()) :: :ok
  def clear(server \\ __MODULE__) do
    GenServer.call(server, :clear)
  end

  @doc "Number of entries currently stored (including expired ones not yet pruned)."
  @spec size(GenServer.server()) :: non_neg_integer()
  def size(server \\ __MODULE__) do
    GenServer.call(server, :size)
  end

  # --- GenServer Callbacks ---

  @impl true
  def init(%Config{} = config) do
    table = :ets.new(@table_name, [:set, :private])
    {:ok, %{table: table, config: config, seq: 0}}
  end

  @impl true
  def handle_call({:get, key}, _from, %{table: table} = state) do
    {:reply, lookup(table, key), state}
  end

  def handle_call({:put, key, value, ttl_override}, _from, state) do
    %{table: table, config: config, seq: seq} = state
    ttl = ttl_override || config.cache_ttl
    expires_at = mono_ms() + ttl * 1_000

    unless :ets.member(table, key) do
      evict_if_needed(table, config.max_cache_size)
    end

    :ets.insert(table, {key, value, expires_at, seq})
    {:reply, :ok, %{state | seq: seq + 1}}
  end

  def handle_call({:list, type, user_id}, _from, %{table: table} = state) do
    now = mono_ms()

    values =
      table
      |> :ets.match_object({{type, user_id, :_}, :_, :_, :_})
      |> Enum.filter(fn {key, _value, expires_at, _seq} ->
        if now < expires_at do
          true
        else
          :ets.delete(table, key)
          false
        end
      end)
      |> Enum.sort_by(fn {_key, _value, _expires_at, seq} -> seq end)
      |> Enum.map(fn {_key, value, _expires_at, _seq} -> value end)

    {:reply, values, state}
  end

  def handle_call({:delete, key}, _from, %{table: table} = state) do
    :ets.delete(table, key)
    {:reply, :ok, state}
  end

  def handle_call(:clear, _from, %{table: table} = state) do
    :ets.delete_all_objects(table)
    {:reply, :ok, state}
  end

  def handle_call(:size, _from, %{table: table} = state) do
    {:reply, :ets.info(table, :size), state}
  end

  # --- Private Helpers ---

  defp lookup(table, key) do
    case :ets.lookup(table, key) do
      [{^key, value, expires_at, _seq}] ->
        if mono_ms() < expires_at do
          value
        else
          :ets.delete(table, key)
          nil
        end

      [] ->
        nil
    end
  end

  defp evict_if_needed(table, max_size) do
    if :ets.info(table, :size) >= max_size do
      # Evict the oldest inserted entry (smallest seq)
      oldest =
        :ets.foldl(
          fn {key, _value, _expires_at, seq}, acc ->
            case acc do
              nil -> {seq, key}
              {min_seq, _} when seq < min_seq -> {seq, key}
              _ -> acc
            end
          end,
          nil,
          table
        )

      case oldest do
        {_seq, key} -> :ets.delete(table, key)
        nil -> :ok
      end
    end
  end

  defp mono_ms do
    System.monotonic_time(:millisecond)
  end
end
