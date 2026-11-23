defmodule Controlvar.EventIdTest do
  @moduledoc """
  Every event the SDK sends carries an `event_id`, so the server stores a
  request that is sent again only once (#1543).

  What is sent is read through `RecordingHttp`, a `:http_client` that sends
  each tracking request to the test process. Two tests run the real
  `Controlvar.HttpClient` against a `:gen_tcp` server on 127.0.0.1: `:httpc`
  sends a POST again by itself when the server closes a reused keep-alive
  connection while the request is in flight, and the copy it sends must carry
  the same body and the same `event_id`; a request that times out is not sent
  again.

  `async: false`: the tests read the sessions of `:httpc`'s default profile,
  register a name for `RecordingHttp`, and add a `:logger` handler.
  """

  use ExUnit.Case, async: false

  alias Controlvar.{BatchResult, Client}
  alias Controlvar.TestSupport.ProcessHelpers

  @uuid_v4 ~r/\A[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\z/

  # Every accept, recv and wait in the server below is bounded by this.
  @io_timeout 5_000

  @spy :controlvar_event_id_test_spy

  defmodule RecordingHttp do
    @moduledoc false
    @behaviour Controlvar.HttpBehaviour

    @spy :controlvar_event_id_test_spy

    @impl true
    def get(_config, "/api/v1/feature-flags/evaluate/" <> rest) do
      [key | _] = String.split(rest, "?", parts: 2)
      {:ok, %{"key" => URI.decode(key), "enabled" => true, "config" => nil}}
    end

    @impl true
    def post(_config, "/api/v1/tracking/assign", %{experiment_key: key}) do
      {:ok, %{"experiment_key" => key, "variant_name" => "treatment", "is_control" => false}}
    end

    def post(_config, "/api/v1/tracking/batch" = path, %{events: events} = body) do
      send(@spy, {:post, path, body})
      {:ok, %{"success_count" => length(events), "failure_count" => 0}}
    end

    def post(_config, path, body) do
      send(@spy, {:post, path, body})
      {:ok, %{"id" => "evt-1"}}
    end
  end

  defmodule LogForwarder do
    @moduledoc false
    # A `:logger` handler: sends each event's level and text to the test
    # process. It runs in the process that logs, so a warning logged before a
    # reply reaches the test process before that reply does.
    def log(%{level: level, msg: msg}, %{config: %{to: pid}}) do
      send(pid, {:log, level, text(msg)})
    end

    defp text({:string, chardata}), do: IO.chardata_to_string(chardata)
    defp text({:report, report}), do: inspect(report)
    defp text({format, args}), do: format |> :io_lib.format(args) |> IO.chardata_to_string()
  end

  defp start_recording_client do
    ProcessHelpers.await_release(@spy)
    Process.register(self(), @spy)

    on_exit(fn ->
      try do
        Process.unregister(@spy)
      rescue
        ArgumentError -> :ok
      end
    end)

    {:ok, pid} =
      Client.start_link(
        base_url: "http://localhost",
        api_key: "test-key",
        http_client: RecordingHttp
      )

    on_exit(fn -> ProcessHelpers.stop_if_alive(pid, &Client.stop/1) end)
    pid
  end

  defp forward_logs do
    :ok = :logger.add_handler(:event_id_test_logs, LogForwarder, %{config: %{to: self()}})
    on_exit(fn -> :logger.remove_handler(:event_id_test_logs) end)
  end

  defp event_id_of(entry) do
    assert Map.has_key?(entry, :event_id), "no event_id in #{inspect(entry)}"
    entry.event_id
  end

  describe "track/5" do
    test "a keyed track sends a lowercase UUID v4 as event_id" do
      pid = start_recording_client()

      :ok = Client.track(pid, "purchase", "user-1", %{}, experiment_key: "checkout")

      assert_receive {:post, "/api/v1/tracking/track", body}, 1_000
      assert event_id_of(body) =~ @uuid_v4
    end

    test "two calls send two ids" do
      pid = start_recording_client()

      for _ <- 1..2,
          do: :ok = Client.track(pid, "purchase", "user-1", %{}, experiment_key: "checkout")

      assert_receive {:post, "/api/v1/tracking/track", first}, 1_000
      assert_receive {:post, "/api/v1/tracking/track", second}, 1_000
      assert event_id_of(first) != event_id_of(second)
    end

    test "a key-less track fanned out to two assignments and a flag sends one id in every entry" do
      pid = start_recording_client()
      {:ok, _} = Client.get_assignment(pid, "checkout", "user-1")
      {:ok, _} = Client.get_assignment(pid, "pricing", "user-1")
      {:ok, _} = Client.evaluate_flag(pid, "new-search", "user-1")

      :ok = Client.track(pid, "page_view", "user-1")

      assert_receive {:post, "/api/v1/tracking/batch", %{events: entries}}, 1_000
      assert length(entries) == 3, "two assignments and one flag"
      ids = entries |> Enum.map(&event_id_of/1) |> Enum.uniq()
      assert length(ids) == 1, "one id per event, shared by its fan-out entries: #{inspect(ids)}"
      assert hd(ids) =~ @uuid_v4
    end

    test "the caller's :event_id wins, on /track, on every fan-out entry and on a second call; nil and \"\" make one" do
      pid = start_recording_client()
      {:ok, _} = Client.get_assignment(pid, "checkout", "user-1")
      {:ok, _} = Client.evaluate_flag(pid, "new-search", "user-1")

      for _ <- 1..2 do
        :ok =
          Client.track(pid, "purchase", "user-1", %{},
            experiment_key: "checkout",
            event_id: "order-42"
          )

        assert_receive {:post, "/api/v1/tracking/track", body}, 1_000
        assert event_id_of(body) == "order-42"
      end

      :ok = Client.track(pid, "page_view", "user-1", %{}, event_id: "view-7")
      assert_receive {:post, "/api/v1/tracking/batch", %{events: entries}}, 1_000
      assert length(entries) == 2
      assert Enum.map(entries, &event_id_of/1) == ["view-7", "view-7"]

      made =
        for given <- [nil, ""] do
          :ok =
            Client.track(pid, "purchase", "user-1", %{},
              experiment_key: "checkout",
              event_id: given
            )

          assert_receive {:post, "/api/v1/tracking/track", body}, 1_000
          assert event_id_of(body) =~ @uuid_v4
          body.event_id
        end

      assert length(Enum.uniq(made)) == 2
    end
  end

  describe "track_batch/2" do
    test "64 events get 64 different lowercase UUID v4 ids" do
      pid = start_recording_client()

      events =
        for i <- 1..64,
            do: %{event_name: "event-#{i}", user_id: "user-1", experiment_key: "checkout"}

      assert {:ok, %BatchResult{failure_count: 0}} = Client.track_batch(pid, events)

      assert_receive {:post, "/api/v1/tracking/batch", %{events: sent}}, 1_000
      ids = Enum.map(sent, &event_id_of/1)
      assert length(ids) == 64

      for id <- ids, do: assert(id =~ @uuid_v4, "not a lowercase UUID v4: #{id}")

      assert length(Enum.uniq(ids)) == 64
    end

    test "each event has its own id; the caller's event_id is kept, atom or string key" do
      pid = start_recording_client()

      events = [
        %{event_name: "a", user_id: "user-1", experiment_key: "checkout"},
        %{
          "event_name" => "b",
          "user_id" => "user-1",
          "experiment_key" => "checkout",
          "event_id" => "order-42"
        },
        %{event_name: "c", user_id: "user-1", experiment_key: "checkout", event_id: "view-7"},
        %{event_name: "d", user_id: "user-1", experiment_key: "checkout", event_id: nil},
        %{event_name: "e", user_id: "user-1", experiment_key: "checkout", event_id: ""}
      ]

      assert {:ok, %BatchResult{failure_count: 0}} = Client.track_batch(pid, events)

      assert_receive {:post, "/api/v1/tracking/batch", %{events: sent}}, 1_000
      assert [a, "order-42", "view-7", d, e] = Enum.map(sent, &event_id_of/1)
      for id <- [a, d, e], do: assert(id =~ @uuid_v4, "not a lowercase UUID v4: #{id}")
      assert length(Enum.uniq([a, d, e])) == 3, "one id per event: #{inspect([a, d, e])}"
    end
  end

  # Each refused id, why, and the fixed text that names the rule. The
  # decomposed "é" ("e" and a combining accent, written as an escape so no
  # editor can precompose it) is 256 code points but 128 graphemes.
  defp refused_ids do
    decomposed = String.duplicate("e\u0301", 128)
    assert length(String.to_charlist(decomposed)) == 256, "precondition: 256 code points"
    assert String.length(decomposed) == 128, "precondition: 128 graphemes"

    [
      {"256 characters", String.duplicate("a", 256), "at most 255 characters"},
      {"256 code points, 128 graphemes", decomposed, "at most 255 characters"},
      {"a NUL", "order\0-42", "must not contain NUL"},
      {"invalid UTF-8", <<"order-", 0xFF>>, "must be valid UTF-8"},
      {"a lone surrogate, as UTF-8", <<"order-", 0xED, 0xA0, 0x80>>, "must be valid UTF-8"},
      {"a number", 987_654_321, "must be a string"}
    ]
  end

  defp as_text(id) when is_binary(id), do: id
  defp as_text(id), do: inspect(id)

  describe "an event_id the server would refuse" do
    # Mutes the console only: LogForwarder still sees every warning.
    @tag :capture_log
    test "track/5 sends nothing and logs a warning that names the rule, not the id" do
      pid = start_recording_client()
      forward_logs()

      for {why, id, rule} <- refused_ids() do
        :ok =
          Client.track(pid, "purchase", "user-1", %{}, experiment_key: "checkout", event_id: id)

        # A call: the cast above has been handled, and its warning logged.
        _ = Client.assignments(pid, "user-1")

        assert_received {:log, :warning, message}, "no warning for #{why}"
        assert message =~ "could not build track request", why
        assert message =~ rule, why
        refute message =~ as_text(id), "the warning holds the id (#{why})"
        refute_receive {:post, _, _}, 100, "sent for #{why}"
      end
    end

    test "track_batch/2 makes it a local failure and sends the other events" do
      pid = start_recording_client()
      refused = refused_ids()

      events =
        Enum.map(refused, fn {_why, id, _rule} ->
          %{event_name: "refused", user_id: "user-1", experiment_key: "checkout", event_id: id}
        end) ++ [%{event_name: "kept", user_id: "user-1", experiment_key: "checkout"}]

      assert {:ok, %BatchResult{} = result} = Client.track_batch(pid, events)
      assert result.failure_count == length(refused)

      for {error, {why, id, rule}, index} <- Enum.zip([result.errors, refused, 0..5]) do
        assert error.index == index, why
        assert error.error =~ rule, why
        refute error.error =~ as_text(id), "the error holds the id (#{why})"
      end

      assert_receive {:post, "/api/v1/tracking/batch", %{events: [sent]}}, 1_000
      assert sent.event_name == "kept"
      assert event_id_of(sent) =~ @uuid_v4
    end

    test "an event_id of 255 characters is accepted, a four-byte character counted once" do
      pid = start_recording_client()
      four_byte = String.duplicate("\u{1F600}", 255)
      assert byte_size(four_byte) == 1020, "precondition: 255 four-byte characters"
      ids = [String.duplicate("a", 255), four_byte]

      events =
        Enum.map(ids, fn id ->
          %{event_name: "kept", user_id: "user-1", experiment_key: "checkout", event_id: id}
        end)

      assert {:ok, %BatchResult{failure_count: 0}} = Client.track_batch(pid, events)
      assert_receive {:post, "/api/v1/tracking/batch", %{events: sent}}, 1_000
      assert Enum.map(sent, &event_id_of/1) == ids
    end
  end

  describe "a request :httpc sends again by itself" do
    test "carries the same body and the same event_id" do
      {listener, port, log} = start_server(&close_second_on_first/2)

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

  describe "a request that times out" do
    test "is not sent again" do
      {listener, port, log} = start_server(fn _conn, _req -> :hold end)

      {:ok, client} =
        Client.start_link(
          base_url: "http://127.0.0.1:#{port}",
          api_key: "test-key",
          http_client: Controlvar.HttpClient,
          timeout: 300
        )

      on_exit(fn -> ProcessHelpers.stop_if_alive(client, &Client.stop/1) end)

      event = %{event_name: "timed-out", user_id: "user-1", experiment_key: "checkout"}
      assert {:error, {:network_error, :timeout}} = Client.track_batch(client, [event])

      # The window starts only after the caller has its error: a request sent
      # again would arrive in it. A bounded wait, not a timing assertion.
      Process.sleep(500)
      arrivals = log |> Agent.get(& &1) |> Enum.reverse()

      assert length(arrivals) == 1,
             "a timed-out request was sent again " <>
               "(arrivals as {connection, request, events}: " <>
               "#{inspect(Enum.map(arrivals, fn {c, r, b} -> {c, r, event_names(b)} end))})"

      [{1, 1, body}] = arrivals
      assert [id] = event_ids(body)
      assert id =~ @uuid_v4
      :gen_tcp.close(listener)
    end
  end

  # --- The server ---

  # The second request on connection 1 is closed without an answer; every
  # other request is answered.
  defp close_second_on_first(1, 2), do: :close
  defp close_second_on_first(_conn, _req), do: :answer

  # One listener on 127.0.0.1, port 0. Each accepted connection gets its own
  # process, so connection 1 never blocks connection 2. Every request is read
  # in full (headers, then the Content-Length body) and recorded as
  # {connection, request on that connection, body} BEFORE the server acts on
  # it. `plan.(connection, request)` decides what it does: `:answer` (HTTP/1.1
  # 200 with a Content-Length; the connection stays open, keep-alive),
  # `:close` (close the connection without an answer) or `:hold` (never
  # answer; wait, bounded, for the client to close).
  defp start_server(plan) do
    {:ok, listener} =
      :gen_tcp.listen(0, [:binary, active: false, reuseaddr: true, ip: {127, 0, 0, 1}])

    {:ok, port} = :inet.port(listener)
    {:ok, log} = Agent.start_link(fn -> [] end)
    spawn(fn -> accept_loop(listener, log, plan, 1) end)
    {listener, port, log}
  end

  # Ends when the listener is closed (by the test, or when the test process
  # exits, since it owns the listener).
  defp accept_loop(listener, log, plan, conn) do
    case :gen_tcp.accept(listener, @io_timeout) do
      {:ok, socket} ->
        handler =
          spawn(fn ->
            receive do
              :go -> serve(socket, log, plan, conn, 1, "")
            after
              @io_timeout -> :gen_tcp.close(socket)
            end
          end)

        :ok = :gen_tcp.controlling_process(socket, handler)
        send(handler, :go)
        accept_loop(listener, log, plan, conn + 1)

      {:error, :timeout} ->
        accept_loop(listener, log, plan, conn)

      {:error, _closed} ->
        :ok
    end
  end

  defp serve(socket, log, plan, conn, req, buffer) do
    case read_request(socket, buffer) do
      {:ok, body, rest} ->
        Agent.update(log, &[{conn, req, body} | &1])
        act(plan.(conn, req), socket, log, plan, conn, req, rest)

      {:error, _closed_or_timeout} ->
        :gen_tcp.close(socket)
    end
  end

  defp act(:close, socket, _log, _plan, _conn, _req, _rest), do: :gen_tcp.close(socket)

  defp act(:hold, socket, _log, _plan, _conn, _req, _rest) do
    _closed_or_timeout = :gen_tcp.recv(socket, 0, @io_timeout)
    :gen_tcp.close(socket)
  end

  defp act(:answer, socket, log, plan, conn, req, rest) do
    answer = ~s({"success_count":1,"failure_count":0,"errors":[]})

    response =
      "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n" <>
        "content-length: #{byte_size(answer)}\r\n\r\n" <> answer

    case :gen_tcp.send(socket, response) do
      :ok -> serve(socket, log, plan, conn, req + 1, rest)
      {:error, _} -> :gen_tcp.close(socket)
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
