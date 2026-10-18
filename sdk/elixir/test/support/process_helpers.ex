defmodule Controlvar.TestSupport.ProcessHelpers do
  @moduledoc """
  Teardown helpers for processes linked to a test process.

  A client started with `start_link` in a test dies with the test process,
  and it does so *asynchronously*: by the time `on_exit` runs it may be alive
  at the `Process.alive?/1` check and gone by the `GenServer.stop/1` call,
  which then exits with `:noproc`. That race showed up as a CI flake. Stopping
  through here tolerates both orders.
  """

  @doc "Stop `pid` with `stopper` if it is still running; ignore it if it is not."
  def stop_if_alive(pid, stopper \\ &GenServer.stop/1) do
    if Process.alive?(pid) do
      try do
        stopper.(pid)
      catch
        :exit, _ -> :ok
      end
    end

    :ok
  end

  @doc """
  Block until the registered `name` is free.

  A registered name is released when its process exits, but the next test's
  `setup` can run before the release has landed, and `Process.register/2`
  then raises "the name is already taken".
  """
  def await_release(name, attempts \\ 100) do
    case Process.whereis(name) do
      nil ->
        :ok

      pid when attempts > 0 ->
        if Process.alive?(pid), do: Process.sleep(5)
        await_release(name, attempts - 1)

      pid ->
        raise "#{inspect(name)} is still registered to #{inspect(pid)} after waiting"
    end
  end
end
