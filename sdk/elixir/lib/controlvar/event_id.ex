defmodule Controlvar.EventId do
  @moduledoc """
  The `event_id` every event carries.

  The server stores an event sent again with the same `event_id`, user,
  experiment, flag and event name only once. `Controlvar.Client.track/5` and
  `Controlvar.Client.track_batch/2` send the caller's id, or, when it gave
  none (`nil` or `""`), a lowercase random UUID (version 4) made for the event
  in that call, shared by all of the event's fan-out entries.

  An id the server would refuse is refused here instead, and the event is not
  sent: anything that is not a string, text that is not valid UTF-8, more than
  255 characters (code points, as the server counts them), or a NUL.
  """

  @max_length 255
  # A code point is at most 4 bytes: anything longer is too long, uncounted.
  @max_bytes @max_length * 4

  @doc """
  The caller's id, or a new one when it gave none (`nil` or `""`).

  Returns `{:ok, id}`, or `{:error, reason}` for an id the server would
  refuse. The reason is fixed text; it never holds the id.
  """
  @spec pick(term()) :: {:ok, String.t()} | {:error, String.t()}
  # Made once per event, before the first attempt. A future event queue
  # stores it with the event and never makes a new one on dequeue.
  def pick(nil), do: {:ok, new()}
  def pick(""), do: {:ok, new()}

  def pick(id) do
    case problem(id) do
      nil -> {:ok, id}
      reason -> {:error, reason}
    end
  end

  @doc "A new lowercase random UUID (version 4), from `:crypto.strong_rand_bytes/1`."
  @spec new() :: String.t()
  def new do
    <<a::48, _version::4, b::12, _variant::2, c::62>> = :crypto.strong_rand_bytes(16)

    <<p1::binary-size(8), p2::binary-size(4), p3::binary-size(4), p4::binary-size(4),
      p5::binary-size(12)>> =
      Base.encode16(<<a::48, 4::4, b::12, 2::2, c::62>>, case: :lower)

    Enum.join([p1, p2, p3, p4, p5], "-")
  end

  @doc """
  Why the server would refuse `id`, or `nil` when it accepts it. Fixed text:
  it never holds the id.
  """
  @spec problem(term()) :: String.t() | nil
  def problem(id) when not is_binary(id), do: "event_id must be a string"

  def problem(id) do
    cond do
      not String.valid?(id) ->
        "event_id must be valid UTF-8"

      byte_size(id) > @max_bytes or code_points(id) > @max_length ->
        "event_id must be at most #{@max_length} characters"

      String.contains?(id, <<0>>) ->
        "event_id must not contain NUL"

      true ->
        nil
    end
  end

  # Code points, not graphemes: "e" followed by a combining accent is two.
  defp code_points(id) do
    for <<_::utf8 <- id>>, reduce: 0 do
      count -> count + 1
    end
  end
end
