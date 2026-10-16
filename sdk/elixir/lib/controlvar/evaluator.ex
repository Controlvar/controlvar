defmodule Controlvar.Evaluator do
  @moduledoc """
  Cross-SDK consistent hash utility.

      MD5("{user_id}:{flag_key}") -> first 4 bytes as little-endian uint32 / 2^32

  Every SDK exports this function and the golden-vector tests in
  `tests/sdk-contract/` pin its output:

      hash_user("user-123", "my-flag") == 0.6927449859213084

  It is a utility only: since the rewiring onto the public API nothing in the
  SDK buckets users locally — flag evaluation and experiment assignment are
  decided by the server (see `Controlvar.Client`).
  """

  @doc """
  Hash a user + key combination to a float in `[0.0, 1.0)`.

  ## Examples

      iex> Controlvar.Evaluator.hash_user("user-123", "my-flag")
      0.6927449859213084

      iex> result = Controlvar.Evaluator.hash_user("user-abc", "feature-xyz")
      iex> result >= 0.0 and result < 1.0
      true
  """
  @spec hash_user(String.t(), String.t()) :: float()
  def hash_user(user_id, flag_key) do
    <<v::little-unsigned-integer-size(32), _rest::binary>> =
      :crypto.hash(:md5, "#{user_id}:#{flag_key}")

    v / 4_294_967_296.0
  end
end
