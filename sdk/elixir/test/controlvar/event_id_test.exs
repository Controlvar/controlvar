defmodule Controlvar.EventIdTest do
  @moduledoc """
  Every event the SDK sends carries an `event_id`, so the server stores a
  request that is sent again only once (#1543).

  The test here runs the real `Controlvar.HttpClient` against a `:gen_tcp`
  server on 127.0.0.1. `:httpc` sends a POST again by itself when the server
  closes a reused keep-alive connection while the request is in flight; the
  copy it sends must carry the same body and the same `event_id`.

  `async: false`: the test reads the sessions of `:httpc`'s default profile.
  """

  use ExUnit.Case, async: false

  alias Controlvar.Client
  alias Controlvar.TestSupport.ProcessHelpers

  @uuid_v4 ~r/\A[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\z/

  # Every accept, recv and wait in the server below is bounded by this.
  @io_timeout 5_000

  describe "a request :httpc sends again by itself" do
    test "carries the same body and the same event_id" do
      {listener, port, log} = start_server()

      {:ok, client} =
        Client.start_link(
          base_url: "http://127.0.0.1:#{port}",
          api_key: "test-key",
          http_client: Controlvar.HttpClient,
          timeout: @io_timeout
        )

      on_exit(fn -> ProcessHelpers.stop_if_alive(client, &Client.stop/1) end)

      # POST 1 opens connection 1 and is answered; the connection stays open.
      seed = %{event_name: "first-post", user_id: "user-1", experiment_key: "checkout"}
      assert {:ok, _} = Client.track_batch(client, [seed])

      await_idle_session(port)

      # POST 2 goes on connection 1, which the server closes without answering.
      marker = %{event_name: "sent-again", user_id: "user-1", experiment_key: "checkout"}
      result = Client.track_batch(client, [marker])

      # Every arrival is recorded before the server acts on it, so once
      # `track_batch` has returned the log is complete.
      arrivals = log |> Agent.get(& &1) |> Enum.reverse()

      marker_arrivals =
        for {conn, req, body} <- arrivals, body =~ "sent-again", do: {conn, req, body}

      seen = Enum.map(arrivals, fn {conn, req, body} -> {conn, req, event_names(body)} end)

      # Precondition 1: POST 2 first arrived on POST 1's connection. Without
      # that, the server never closed a request in flight and this test cannot
      # see a request sent again.
      assert match?([{1, 2, _} | _], marker_arrivals),
             "precondition 1: :httpc did not send POST 2 on POST 1's keep-alive connection " <>
               "(arrivals as {connection, request, events}: #{inspect(seen)})"

      # Assertion 2: :httpc sent it again, once, on a new connection.
      assert length(marker_arrivals) == 2 and
               match?({conn, 1, _} when conn > 1, Enum.at(marker_arrivals, 1)),
             "assertion 2: POST 2 did not arrive exactly twice, the second time on a new " <>
               "connection (arrivals as {connection, request, events}: #{inspect(seen)})"

      # Assertion 3: the same bytes, carrying one event_id.
      [{_, _, first}, {_, _, again}] = marker_arrivals
      assert first == again, "assertion 3: the body sent again differs from the first"

      id = first |> event_ids() |> List.first()

      assert is_binary(id) and id =~ @uuid_v4,
             "assertion 3: the event carries no event_id, or one that is not a lowercase " <>
               "UUID v4 (body: #{first})"

      assert event_ids(again) == [id]

      # Assertion 4: the caller saw only the answer to the request sent again.
      assert {:ok, _} = result

      :gen_tcp.close(listener)
    end
  end

  # --- The server ---

  # One listener on 127.0.0.1, port 0. Each accepted connection gets its own
  # process, so connection 1 never blocks connection 2. Every request is read
  # in full (headers, then the Content-Length body) and recorded as
  # {connection, request on that connection, body} BEFORE the server acts on
  # it. The second request on connection 1 is not answered: the connection is
  # closed. Every other request gets HTTP/1.1 200 with a Content-Length, and
  # its connection stays open (keep-alive).
  defp start_server do
    {:ok, listener} =
      :gen_tcp.listen(0, [:binary, active: false, reuseaddr: true, ip: {127, 0, 0, 1}])

    {:ok, port} = :inet.port(listener)
    {:ok, log} = Agent.start_link(fn -> [] end)
    spawn(fn -> accept_loop(listener, log, 1) end)
    {listener, port, log}
  end

  # Ends when the listener is closed (by the test, or when the test process
  # exits, since it owns the listener).
  defp accept_loop(listener, log, conn) do
    case :gen_tcp.accept(listener, @io_timeout) do
      {:ok, socket} ->
        handler =
          spawn(fn ->
            receive do
              :go -> serve(socket, log, conn, 1, "")
            after
              @io_timeout -> :gen_tcp.close(socket)
            end
          end)

        :ok = :gen_tcp.controlling_process(socket, handler)
        send(handler, :go)
        accept_loop(listener, log, conn + 1)

      {:error, :timeout} ->
        accept_loop(listener, log, conn)

      {:error, _closed} ->
        :ok
    end
  end

  defp serve(socket, log, conn, req, buffer) do
    case read_request(socket, buffer) do
      {:ok, body, rest} ->
        Agent.update(log, &[{conn, req, body} | &1])

        if conn == 1 and req == 2 do
          :gen_tcp.close(socket)
        else
          answer = ~s({"success_count":1,"failure_count":0,"errors":[]})

          response =
            "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n" <>
              "content-length: #{byte_size(answer)}\r\n\r\n" <> answer

          case :gen_tcp.send(socket, response) do
            :ok -> serve(socket, log, conn, req + 1, rest)
            {:error, _} -> :gen_tcp.close(socket)
          end
        end

      {:error, _closed_or_timeout} ->
        :gen_tcp.close(socket)
    end
  end

  defp read_request(socket, buffer) do
    case :binary.split(buffer, "\r\n\r\n") do
      [head, rest] -> read_body(socket, rest, content_length(head))
      [_partial] -> recv_more(socket, buffer, &read_request/2)
    end
  end

  defp read_body(_socket, buffer, length) when byte_size(buffer) >= length do
    {:ok, binary_part(buffer, 0, length), binary_part(buffer, length, byte_size(buffer) - length)}
  end

  defp read_body(socket, buffer, length) do
    recv_more(socket, buffer, &read_body(&1, &2, length))
  end

  defp recv_more(socket, buffer, continue) do
    case :gen_tcp.recv(socket, 0, @io_timeout) do
      {:ok, data} -> continue.(socket, buffer <> data)
      {:error, _} = error -> error
    end
  end

  defp content_length(head) do
    head
    |> String.split("\r\n")
    |> Enum.find_value(0, fn line ->
      case String.split(line, ":", parts: 2) do
        [name, value] ->
          if String.downcase(String.trim(name)) == "content-length",
            do: String.to_integer(String.trim(value))

        _ ->
          nil
      end
    end)
  end

  # :httpc answers the caller before it marks the keep-alive session
  # available (httpc_handler: answer_request, then
  # maybe_make_session_available), so a POST sent at once could open a new
  # connection. Wait until the session to this port is available and idle.
  # A bounded wait for a precondition; nothing here asserts a duration.
  defp await_idle_session(port, attempts \\ 500) do
    {good, _bad, _non} = :httpc.which_sessions()

    # #session{id = {{Host, Port}, Handler}, client_close, scheme, socket,
    #          socket_type, queue_length, type, available}
    idle? =
      Enum.any?(good, fn session ->
        match?({:session, {{_host, ^port}, _handler}, false, :http, _, _, 0, _, true}, session)
      end)

    cond do
      idle? ->
        :ok

      attempts > 0 ->
        Process.sleep(10)
        await_idle_session(port, attempts - 1)

      true ->
        flunk(
          "precondition 0: no idle keep-alive :httpc session to port #{port} after POST 1 " <>
            "(sessions: #{inspect(good)})"
        )
    end
  end

  defp events(body), do: body |> Jason.decode!() |> Map.fetch!("events")

  defp event_names(body), do: body |> events() |> Enum.map(& &1["event_name"])

  defp event_ids(body), do: body |> events() |> Enum.map(& &1["event_id"])
end
