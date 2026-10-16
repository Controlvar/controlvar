defmodule Controlvar.EvaluatorTest do
  @moduledoc """
  Unit tests for the Evaluator module.

  Since the rewiring onto the public API the module only exports the
  cross-SDK consistent hash utility; local flag evaluation is gone
  (the server decides). See `hash_compatibility_test.exs` for the golden
  vectors.
  """

  use ExUnit.Case, async: true

  doctest Controlvar.Evaluator

  alias Controlvar.Evaluator

  describe "hash_user/2" do
    test "returns a float in [0.0, 1.0)" do
      result = Evaluator.hash_user("user-123", "my-flag")
      assert is_float(result)
      assert result >= 0.0
      assert result < 1.0
    end

    test "matches the cross-SDK known vector" do
      assert_in_delta Evaluator.hash_user("user-123", "my-flag"), 0.6927449859213084, 1.0e-10
    end

    test "is the little-endian uint32 of the first four MD5 bytes over 2^32" do
      <<expected::little-unsigned-integer-size(32), _::binary>> =
        :crypto.hash(:md5, "user-abc:feature-xyz")

      assert Evaluator.hash_user("user-abc", "feature-xyz") == expected / 4_294_967_296.0
    end

    test "accepts any value that implements String.Chars" do
      assert Evaluator.hash_user(123, :flag) == Evaluator.hash_user("123", "flag")
    end
  end

  describe "local evaluation is gone" do
    test "the module no longer exports evaluate/2,3 or assign/3" do
      Code.ensure_loaded!(Evaluator)
      refute function_exported?(Evaluator, :evaluate, 2)
      refute function_exported?(Evaluator, :evaluate, 3)
      refute function_exported?(Evaluator, :assign, 3)
      assert function_exported?(Evaluator, :hash_user, 2)
    end
  end
end
