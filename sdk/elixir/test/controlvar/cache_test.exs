defmodule Controlvar.CacheTest do
  @moduledoc """
  Unit tests for the ETS-backed Cache GenServer.

  Each test starts its own isolated Cache instance to avoid
  inter-test interference.
  """

  use ExUnit.Case, async: true

  alias Controlvar.TestSupport.ProcessHelpers

  alias Controlvar.{Cache, Config}

  # Build a minimal config for cache tests
  defp test_config(overrides \\ []) do
    Config.new(
      Keyword.merge(
        [base_url: "http://localhost", api_key: "test-key", cache_ttl: 300, max_cache_size: 10],
        overrides
      )
    )
  end

  setup do
    config = test_config()
    {:ok, pid} = Cache.start_link_unnamed(config)
    on_exit(fn -> ProcessHelpers.stop_if_alive(pid) end)
    %{cache: pid, config: config}
  end

  describe "get/2" do
    test "returns nil for a missing key", %{cache: cache} do
      result = Cache.get(cache, "nonexistent")
      assert result == nil
    end

    test "returns nil for a key that was never written", %{cache: cache} do
      result = Cache.get(cache, :any_key)
      assert result == nil
    end
  end

  describe "put/4 and get/2" do
    test "put then get returns the stored value", %{cache: cache} do
      Cache.put(cache, "my-key", "my-value")
      # Allow cast to process
      :timer.sleep(10)
      result = Cache.get(cache, "my-key")
      assert result == "my-value"
    end

    test "stores and retrieves map values", %{cache: cache} do
      value = %{name: "treatment", percentage: 50}
      Cache.put(cache, "flag:my-flag", value)
      :timer.sleep(10)
      assert Cache.get(cache, "flag:my-flag") == value
    end

    test "stores and retrieves complex nested maps", %{cache: cache} do
      value = %{"variants" => [%{"name" => "a"}, %{"name" => "b"}], "enabled" => true}
      Cache.put(cache, "experiment:exp-1", value)
      :timer.sleep(10)
      assert Cache.get(cache, "experiment:exp-1") == value
    end

    test "overwrites existing value for same key", %{cache: cache} do
      Cache.put(cache, "k", "first")
      :timer.sleep(10)
      Cache.put(cache, "k", "second")
      :timer.sleep(10)
      assert Cache.get(cache, "k") == "second"
    end
  end

  describe "TTL expiration" do
    test "expired entry returns nil after TTL passes", %{config: config} do
      # Use a 1-second TTL config
      fast_config = %{config | cache_ttl: 1}
      {:ok, cache} = Cache.start_link_unnamed(fast_config)
      on_exit(fn -> ProcessHelpers.stop_if_alive(cache) end)

      Cache.put(cache, "expiring-key", "expiring-value")
      :timer.sleep(10)
      # Immediately available
      assert Cache.get(cache, "expiring-key") == "expiring-value"

      # Wait for expiry
      :timer.sleep(1_100)
      assert Cache.get(cache, "expiring-key") == nil
    end

    test "put with custom TTL override expires correctly", %{cache: cache} do
      # Put with 1-second TTL override
      Cache.put(cache, "short-lived", "value", 1)
      :timer.sleep(10)
      assert Cache.get(cache, "short-lived") == "value"

      :timer.sleep(1_100)
      assert Cache.get(cache, "short-lived") == nil
    end

    test "non-expired entry still available within TTL", %{config: config} do
      fast_config = %{config | cache_ttl: 5}
      {:ok, cache} = Cache.start_link_unnamed(fast_config)
      on_exit(fn -> ProcessHelpers.stop_if_alive(cache) end)

      Cache.put(cache, "fresh-key", "fresh-value")
      :timer.sleep(10)
      :timer.sleep(100)
      assert Cache.get(cache, "fresh-key") == "fresh-value"
    end
  end

  describe "delete/2" do
    test "delete removes the entry", %{cache: cache} do
      Cache.put(cache, "to-delete", "value")
      :timer.sleep(10)
      assert Cache.get(cache, "to-delete") == "value"

      Cache.delete(cache, "to-delete")
      :timer.sleep(10)
      assert Cache.get(cache, "to-delete") == nil
    end

    test "delete on non-existent key is a no-op", %{cache: cache} do
      # Should not raise
      Cache.delete(cache, "never-existed")
      :timer.sleep(10)
      assert Cache.get(cache, "never-existed") == nil
    end
  end

  describe "clear/1" do
    test "clear empties all entries", %{cache: cache} do
      Cache.put(cache, "k1", "v1")
      Cache.put(cache, "k2", "v2")
      Cache.put(cache, "k3", "v3")
      :timer.sleep(10)

      assert Cache.size(cache) == 3

      Cache.clear(cache)
      :timer.sleep(10)

      assert Cache.size(cache) == 0
      assert Cache.get(cache, "k1") == nil
      assert Cache.get(cache, "k2") == nil
      assert Cache.get(cache, "k3") == nil
    end

    test "clear on empty cache is a no-op", %{cache: cache} do
      Cache.clear(cache)
      :timer.sleep(10)
      assert Cache.size(cache) == 0
    end
  end

  describe "size/1" do
    test "size is 0 for empty cache", %{cache: cache} do
      assert Cache.size(cache) == 0
    end

    test "size increments with puts", %{cache: cache} do
      Cache.put(cache, "k1", "v1")
      Cache.put(cache, "k2", "v2")
      :timer.sleep(10)
      assert Cache.size(cache) == 2
    end

    test "size decrements after delete", %{cache: cache} do
      Cache.put(cache, "k1", "v1")
      Cache.put(cache, "k2", "v2")
      :timer.sleep(10)
      Cache.delete(cache, "k1")
      :timer.sleep(10)
      assert Cache.size(cache) == 1
    end
  end

  describe "list/3 (per-user listing used by the track fan-out)" do
    test "returns live values whose key is {type, user_id, _}, oldest first", %{cache: cache} do
      Cache.put(cache, {:flag, "user-1", "b-flag"}, :b)
      Cache.put(cache, {:assignment, "user-1", "exp"}, :exp)
      Cache.put(cache, {:flag, "user-1", "a-flag"}, :a)
      Cache.put(cache, {:flag, "user-2", "c-flag"}, :c)

      assert Cache.list(cache, :flag, "user-1") == [:b, :a]
      assert Cache.list(cache, :assignment, "user-1") == [:exp]
      assert Cache.list(cache, :flag, "user-2") == [:c]
      assert Cache.list(cache, :assignment, "user-2") == []
    end

    test "returns [] for an unknown user or type", %{cache: cache} do
      assert Cache.list(cache, :flag, "nobody") == []
      assert Cache.list(cache, :other, "nobody") == []
    end

    test "ignores plain (non-tuple) keys", %{cache: cache} do
      Cache.put(cache, "plain", "value")
      assert Cache.list(cache, :flag, "plain") == []
    end

    test "skips and prunes expired entries", %{cache: cache} do
      Cache.put(cache, {:flag, "user-1", "short"}, :short, 1)
      Cache.put(cache, {:flag, "user-1", "long"}, :long)

      assert Cache.list(cache, :flag, "user-1") == [:short, :long]

      :timer.sleep(1_100)

      assert Cache.list(cache, :flag, "user-1") == [:long]
      assert Cache.size(cache) == 1
    end

    test "re-inserting a key moves it to the newest position", %{cache: cache} do
      Cache.put(cache, {:flag, "user-1", "first"}, 1)
      Cache.put(cache, {:flag, "user-1", "second"}, 2)
      Cache.put(cache, {:flag, "user-1", "first"}, 3)

      # seq is bumped on every write, so the refreshed entry moves last
      assert Cache.list(cache, :flag, "user-1") == [2, 3]
    end
  end

  describe "max_cache_size eviction" do
    test "does not exceed max_cache_size", %{config: config} do
      small_config = %{config | max_cache_size: 5}
      {:ok, cache} = Cache.start_link_unnamed(small_config)
      on_exit(fn -> ProcessHelpers.stop_if_alive(cache) end)

      for i <- 1..10 do
        Cache.put(cache, "key-#{i}", "value-#{i}")
      end

      :timer.sleep(50)
      size = Cache.size(cache)
      # Size should not exceed max_cache_size after eviction
      assert size <= 5
    end

    test "eviction keeps cache functional after max is reached", %{config: config} do
      small_config = %{config | max_cache_size: 3}
      {:ok, cache} = Cache.start_link_unnamed(small_config)
      on_exit(fn -> ProcessHelpers.stop_if_alive(cache) end)

      # Fill the cache
      Cache.put(cache, "a", "1")
      Cache.put(cache, "b", "2")
      Cache.put(cache, "c", "3")
      :timer.sleep(20)

      # Add one more — should evict one
      Cache.put(cache, "d", "4")
      :timer.sleep(20)

      # Cache should still work
      size = Cache.size(cache)
      assert size <= 3

      # The last added should be present
      assert Cache.get(cache, "d") == "4"
    end
  end
end
