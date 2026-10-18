defmodule Controlvar.HashCompatibilityTest do
  @moduledoc """
  Cross-SDK hash parity tests.

  These tests verify that the Elixir SDK's hashing function produces
  values identical to all other SDK implementations (JS, Python, Java, Go, Ruby).

  The hash formula:
    MD5("{userId}:{flagKey}") -> first 4 bytes as little-endian uint32 / 2^32
  """

  use ExUnit.Case, async: true

  alias Controlvar.Evaluator

  describe "cross-SDK hash compatibility" do
    test "known cross-SDK vector: user-123 + my-flag" do
      result = Evaluator.hash_user("user-123", "my-flag")
      assert_in_delta result, 0.6927449859213084, 1.0e-10
    end

    test "result is within [0.0, 1.0) bounds" do
      result = Evaluator.hash_user("user-123", "my-flag")
      assert result >= 0.0
      assert result < 1.0
    end

    test "all outputs are in [0.0, 1.0) range" do
      inputs = [
        {"user-abc", "feature-x"},
        {"", "flag"},
        {"user", ""},
        {"a", "b"},
        {"very-long-user-id-12345678901234567890", "very-long-flag-key-12345678901234567890"}
      ]

      for {uid, fk} <- inputs do
        result = Evaluator.hash_user(uid, fk)
        assert result >= 0.0, "Expected >= 0.0 for #{uid}:#{fk}, got #{result}"
        assert result < 1.0, "Expected < 1.0 for #{uid}:#{fk}, got #{result}"
      end
    end

    test "deterministic — same inputs always produce same output" do
      r1 = Evaluator.hash_user("user-123", "my-flag")
      r2 = Evaluator.hash_user("user-123", "my-flag")
      assert r1 == r2
    end

    test "different user IDs produce different hashes" do
      h1 = Evaluator.hash_user("user-001", "my-flag")
      h2 = Evaluator.hash_user("user-002", "my-flag")
      refute h1 == h2
    end

    test "different flag keys produce different hashes" do
      h1 = Evaluator.hash_user("user-123", "flag-a")
      h2 = Evaluator.hash_user("user-123", "flag-b")
      refute h1 == h2
    end

    test "empty string user ID is valid" do
      result = Evaluator.hash_user("", "my-flag")
      assert result >= 0.0
      assert result < 1.0
    end

    test "unicode characters are handled correctly" do
      result = Evaluator.hash_user("用户-123", "功能旗帜")
      assert result >= 0.0
      assert result < 1.0
    end

    test "long user IDs and flag keys are handled" do
      long_uid = String.duplicate("u", 256)
      long_fk = String.duplicate("f", 256)
      result = Evaluator.hash_user(long_uid, long_fk)
      assert result >= 0.0
      assert result < 1.0
    end

    test "consistent across multiple calls with same input" do
      results = for _ <- 1..100, do: Evaluator.hash_user("stable-user", "stable-flag")
      unique_values = Enum.uniq(results)
      assert length(unique_values) == 1
    end
  end
end
