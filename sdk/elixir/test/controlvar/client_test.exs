defmodule Controlvar.ClientTest do
  @moduledoc """
  Unit tests for the Controlvar.Client GenServer.

  The HTTP layer is replaced by mock modules injected through the
  `:http_client` config option, so no network calls are made. `SpyHttp`
  records every request by sending it to the test process (registered under
  a module-specific name) and answers with canned public-API responses.
  """

  use ExUnit.Case, async: true

  alias Controlvar.TestSupport.ProcessHelpers

  alias Controlvar.{
    Assignment,
    BatchResult,
    Client,
    Config,
    FlagEvaluation,
    HttpBehaviour
  }

  @spy :controlvar_client_test_spy

  # --- Mock HTTP modules ---

  defmodule SpyHttp do
    @moduledoc false
    @behaviour HttpBehaviour

    @spy :controlvar_client_test_spy

    @impl true
    def get(config, path) do
      notify({:get, path, config})
      respond_get(path)
    end

    @impl true
    def post(config, path, body) do
      notify({:post, path, body, config})
      respond_post(path, body)
    end

    defp respond_get("/api/v1/feature-flags/evaluate/" <> rest) do
      [encoded_key | _] = String.split(rest, "?", parts: 2)

      case URI.decode(encoded_key) do
        "dark-mode" ->
          {:ok, %{"key" => "dark-mode", "enabled" => true, "config" => %{"theme" => "dark"}}}

        "disabled-flag" ->
          {:ok, %{"key" => "disabled-flag", "enabled" => false, "config" => nil}}

        "missing-flag" ->
          {:error, {:api_error, 404, ~s({"detail":"Feature flag not found"})}}

        key ->
          {:ok, %{"key" => key, "enabled" => true, "config" => nil}}
      end
    end

    defp respond_get(_path), do: {:error, {:api_error, 404, "Not found"}}

    defp respond_post("/api/v1/tracking/assign", %{experiment_key: "checkout-exp", user_id: user_id}) do
      {:ok,
       %{
         "experiment_key" => "checkout-exp",
         "user_id" => user_id,
         "variant_id" => "a2b6c1d4-0000-4000-8000-000000000001",
         "variant_name" => "treatment",
         "is_control" => false,
         "configuration" => %{"headline" => "Buy now"}
       }}
    end

    defp respond_post("/api/v1/tracking/assign", %{experiment_key: "control-exp"}) do
      {:ok, %{"variant_id" => nil, "variant_name" => "control", "is_control" => true}}
    end

    defp respond_post("/api/v1/tracking/assign", %{experiment_key: "broken-exp"}) do
      {:ok, %{"detail" => "no variant here"}}
    end

    defp respond_post("/api/v1/tracking/assign", _body) do
      {:error, {:api_error, 404, ~s({"detail":"No experiment has the key 'unknown-exp'."})}}
    end

    defp respond_post("/api/v1/tracking/track", _body), do: {:ok, %{"id" => "evt-1"}}

    defp respond_post("/api/v1/tracking/batch", %{events: events}) do
      {:ok, %{"success_count" => length(events), "failure_count" => 0, "errors" => nil}}
    end

    defp notify(message) do
      case Process.whereis(@spy) do
        nil -> :ok
        pid -> send(pid, message)
      end
    end
  end

  defmodule NetworkErrorHttp do
    @moduledoc false
    @behaviour HttpBehaviour

    @spy :controlvar_client_test_spy

    @impl true
    def get(_config, path) do
      notify({:get, path})
      {:error, {:network_error, :econnrefused}}
    end

    @impl true
    def post(_config, path, body) do
      notify({:post, path, body})
      {:error, {:network_error, :econnrefused}}
    end

    defp notify(message) do
      case Process.whereis(@spy) do
        nil -> :ok
        pid -> send(pid, message)
      end
    end
  end

  # Reads a request path the way the server does: split on `&`, then on the
  # first `=`, `+` is a space, percent-decode (UTF-8). `URI.decode_www_form/1`
  # is that; `URI.decode/1` is not (it leaves `+` as `+`, which the server
  # reads as a space), so a decoder built on it would pass an encoder that
  # leaves `+` raw.
  defmodule ServerQuery do
    @moduledoc false

    @doc "The query of `path` as a list of `{name, value}` pairs, in order."
    def pairs(path) do
      [_route, query] = String.split(path, "?", parts: 2)

      query
      |> String.split("&")
      |> Enum.map(fn pair ->
        [name, value] = String.split(pair, "=", parts: 2)
        {URI.decode_www_form(name), URI.decode_www_form(value)}
      end)
    end

    @doc "The JSON object in the `context` parameter, or `nil` when there is none."
    def context(path) do
      case List.keyfind(pairs(path), "context", 0) do
        {"context", json} -> Jason.decode!(json)
        nil -> nil
      end
    end
  end

  # The fake server for targeting: a flag rule `plan == "pro"`. It answers from
  # what the server would read in the request, never from the raw path.
  defmodule ContextHttp do
    @moduledoc false
    @behaviour HttpBehaviour

    @spy :controlvar_client_test_spy

    @impl true
    def get(config, path) do
      notify({:get, path, config})

      case ServerQuery.context(path) do
        %{"plan" => "pro"} ->
          {:ok, %{"key" => "f", "enabled" => true, "config" => nil, "reason" => "targeting_rule"}}

        _ ->
          {:ok, %{"key" => "f", "enabled" => false, "config" => nil, "reason" => "rollout"}}
      end
    end

    @impl true
    def post(config, path, body) do
      notify({:post, path, body, config})
      {:ok, %{"success_count" => 1, "failure_count" => 0, "errors" => nil}}
    end

    defp notify(message) do
      case Process.whereis(@spy) do
        nil -> :ok
        pid -> send(pid, message)
      end
    end
  end

  defmodule BatchRejectHttp do
    @moduledoc false
    @behaviour HttpBehaviour

    @impl true
    def get(_config, _path), do: {:error, {:api_error, 404, "Not found"}}

    @impl true
    def post(_config, "/api/v1/tracking/batch", %{events: events}) do
      {:ok,
       %{
         "success_count" => length(events) - 1,
         "failure_count" => 1,
         "errors" => [%{"index" => 0, "error" => "rejected"}]
       }}
    end

    def post(_config, _path, _body), do: {:ok, %{}}
  end

  setup do
    # The spy sends to a registered name so requests made from the client's
    # spawned track processes reach the test process too.
    #
    # The name is released when the previous test's process exits, but the
    # next test's setup can run before that release has landed, and then
    # `Process.register/2` raises "the name is already taken" -- a flake seen
    # in CI. Wait for the release, and give the name back explicitly on exit
    # rather than relying on the exit to do it in time.
    ProcessHelpers.await_release(@spy)
    Process.register(self(), @spy)

    on_exit(fn ->
      if Process.whereis(@spy), do: Process.unregister(@spy)
    end)

    :ok
  end

  defp start_client(opts \\ []) do
    base_opts = [base_url: "http://localhost:8000", api_key: "test-key", http_client: SpyHttp]
    config = Config.new(Keyword.merge(base_opts, opts))
    {:ok, pid} = Client.start_link(config)
    on_exit(fn -> ProcessHelpers.stop_if_alive(pid, &Client.stop/1) end)
    pid
  end

  # --- evaluate_flag/4 ---

  describe "evaluate_flag/4" do
    test "GETs /api/v1/feature-flags/evaluate/{key}?user_id=... with the config" do
      pid = start_client()

      assert {:ok, _} = Client.evaluate_flag(pid, "dark-mode", "user-123")

      assert_receive {:get, "/api/v1/feature-flags/evaluate/dark-mode?user_id=user-123",
                      %Config{api_key: "test-key", base_url: "http://localhost:8000"}}
    end

    test "percent-encodes the flag key and the user id" do
      pid = start_client()

      assert {:ok, _} = Client.evaluate_flag(pid, "dark mode/v2", "user/1?x=y")

      assert_receive {:get, path, _config}
      assert path == "/api/v1/feature-flags/evaluate/dark%20mode%2Fv2?user_id=user%2F1%3Fx%3Dy"
    end

    test "maps the response to %FlagEvaluation{key, enabled, config}" do
      pid = start_client()

      assert {:ok,
              %FlagEvaluation{key: "dark-mode", enabled: true, config: %{"theme" => "dark"}}} =
               Client.evaluate_flag(pid, "dark-mode", "user-123")
    end

    test "reports a disabled flag with enabled: false and a nil config" do
      pid = start_client()

      assert {:ok, %FlagEvaluation{key: "disabled-flag", enabled: false, config: nil}} =
               Client.evaluate_flag(pid, "disabled-flag", "user-123")
    end

    test "returns {:error, {:api_error, 404, _}} when the flag key is unknown" do
      pid = start_client()

      assert {:error, {:api_error, 404, _body}} =
               Client.evaluate_flag(pid, "missing-flag", "user-123")
    end

    test "returns {:error, {:network_error, _}} on a network failure" do
      pid = start_client(http_client: NetworkErrorHttp)

      assert {:error, {:network_error, :econnrefused}} =
               Client.evaluate_flag(pid, "dark-mode", "user-123")
    end

    test "caches per user + key: the second call does not hit HTTP" do
      pid = start_client()

      {:ok, first} = Client.evaluate_flag(pid, "dark-mode", "user-1")
      {:ok, second} = Client.evaluate_flag(pid, "dark-mode", "user-1")

      assert first == second
      assert_receive {:get, _path, _config}
      refute_receive {:get, _path, _config}, 50
    end

    test "the cache is keyed per user: another user triggers a new request" do
      pid = start_client()

      {:ok, _} = Client.evaluate_flag(pid, "dark-mode", "user-1")
      {:ok, _} = Client.evaluate_flag(pid, "dark-mode", "user-2")

      assert_receive {:get, "/api/v1/feature-flags/evaluate/dark-mode?user_id=user-1", _}
      assert_receive {:get, "/api/v1/feature-flags/evaluate/dark-mode?user_id=user-2", _}
    end

    test "expires cached evaluations after cache_ttl seconds" do
      pid = start_client(cache_ttl: 1)

      {:ok, _} = Client.evaluate_flag(pid, "dark-mode", "user-1")
      assert_receive {:get, _path, _config}

      {:ok, _} = Client.evaluate_flag(pid, "dark-mode", "user-1")
      refute_receive {:get, _path, _config}, 50

      :timer.sleep(1_100)

      {:ok, _} = Client.evaluate_flag(pid, "dark-mode", "user-1")
      assert_receive {:get, _path, _config}
    end

    test "never caches failures: the next call hits HTTP again" do
      pid = start_client(http_client: NetworkErrorHttp)

      assert {:error, _} = Client.evaluate_flag(pid, "dark-mode", "user-1")
      assert {:error, _} = Client.evaluate_flag(pid, "dark-mode", "user-1")

      assert_receive {:get, _path}
      assert_receive {:get, _path}
    end

    test "skip_cache: true bypasses the cache" do
      pid = start_client()

      {:ok, _} = Client.evaluate_flag(pid, "dark-mode", "user-1")
      {:ok, _} = Client.evaluate_flag(pid, "dark-mode", "user-1", skip_cache: true)

      assert_receive {:get, _path, _config}
      assert_receive {:get, _path, _config}
    end
  end

  describe "feature_enabled?/3" do
    test "is true when the server enables the flag" do
      pid = start_client()
      assert Client.feature_enabled?(pid, "dark-mode", "user-123")
    end

    test "is false when the server disables the flag" do
      pid = start_client()
      refute Client.feature_enabled?(pid, "disabled-flag", "user-123")
    end

    test "is false on any failure" do
      pid = start_client()
      refute Client.feature_enabled?(pid, "missing-flag", "user-123")

      error_pid = start_client(http_client: NetworkErrorHttp)
      refute Client.feature_enabled?(error_pid, "dark-mode", "user-123")
    end
  end

  # --- the user's attributes on flag evaluation (#1720) ---

  # Every GET the spy has received so far, oldest first. The calls under test
  # run in the test process, so their requests are already in the mailbox.
  defp gets do
    receive do
      {:get, path, _config} -> [path | gets()]
    after
      0 -> []
    end
  end

  defp enabled?(pid, attributes) do
    {:ok, %FlagEvaluation{enabled: enabled}} =
      Client.evaluate_flag(pid, "f", "u1", attributes: attributes)

    enabled
  end

  @route "/api/v1/feature-flags/evaluate/f?user_id=u1"

  describe "evaluate_flag/4 sends the attributes as context" do
    @tag :regression
    test "context=<compact JSON>, percent-encoded, after user_id" do
      pid = start_client()

      assert {:ok, _} = Client.evaluate_flag(pid, "f", "u1", attributes: %{"plan" => "pro"})

      assert_receive {:get, path, _config}
      assert path == @route <> "&context=%7B%22plan%22%3A%22pro%22%7D"
    end

    @tag :regression
    test "matches a targeting rule on an attribute: the server reads plan=pro from the context" do
      pid = start_client(http_client: ContextHttp)

      assert {:ok, %FlagEvaluation{enabled: true}} =
               Client.evaluate_flag(pid, "f", "u1", attributes: %{plan: "pro"})

      other = start_client(http_client: ContextHttp)
      assert {:ok, %FlagEvaluation{enabled: false}} = Client.evaluate_flag(other, "f", "u1")
    end

    @tag :regression
    test "feature_enabled?/4 passes the options through; /3 is kept" do
      pid = start_client(http_client: ContextHttp)

      assert Client.feature_enabled?(pid, "f", "u1", attributes: %{plan: "pro"})
      assert_receive {:get, path, _config}
      assert path =~ "&context=%7B%22plan%22%3A%22pro%22%7D"

      refute Client.feature_enabled?(pid, "f", "u2")
      assert_receive {:get, "/api/v1/feature-flags/evaluate/f?user_id=u2", _config}
    end

    # The server reads the same object back from each of these, with `+` as a
    # space. V3 has every character that the query string treats as syntax.
    for {name, attributes} <- [
          {"V3", %{"q" => "a+b&c=d#e f/é😀!'()*~"}},
          {"V3a", %{"q" => "a+b&c=d#e fé"}},
          {"V4",
           %{
             "age" => 30,
             "beta" => true,
             "device" => %{"os" => "ios", "version" => "17.4.0"},
             "tags" => ["a", "b"]
           }}
        ] do
      test "the server decodes the same object, the user id and nothing else (#{name})" do
        attributes = unquote(Macro.escape(attributes))
        pid = start_client(http_client: ContextHttp)

        assert {:ok, _} = Client.evaluate_flag(pid, "f", "u1", attributes: attributes)

        assert_receive {:get, path, _config}
        pairs = ServerQuery.pairs(path)
        assert Enum.map(pairs, &elem(&1, 0)) == ["user_id", "context"]
        assert List.keyfind(pairs, "user_id", 0) == {"user_id", "u1"}
        assert ServerQuery.context(path) == attributes
      end
    end

    test "V3a's exact URL" do
      pid = start_client()

      assert {:ok, _} = Client.evaluate_flag(pid, "f", "u1", attributes: %{"q" => "a+b&c=d#e fé"})

      # Measured at 10d5a19c with this change; correctness is the decode test above.
      assert_receive {:get, path, _config}

      assert path ==
               @route <> "&context=%7B%22q%22%3A%22a%2Bb%26c%3Dd%23e%20f%C3%A9%22%7D"
    end

    test "the server reads the same object from atom-keyed attributes" do
      pid = start_client(http_client: ContextHttp)

      assert {:ok, _} = Client.evaluate_flag(pid, "f", "u1", attributes: %{plan: "pro", n: 1})

      assert_receive {:get, path, _config}
      assert ServerQuery.context(path) == %{"plan" => "pro", "n" => 1}
    end

    test "no attributes, an empty map or a non-map: the request is the one sent before attributes were" do
      for attributes <- [nil, %{}, [], [plan: "pro"], "plan=pro", 1] do
        pid = start_client()

        assert {:ok, _} = Client.evaluate_flag(pid, "f", "u1", attributes: attributes)

        assert_receive {:get, path, _config}
        assert path == @route
      end

      pid = start_client()
      assert {:ok, _} = Client.evaluate_flag(pid, "f", "u1")
      assert_receive {:get, @route, _config}
    end

    test "the decoder reads + as a space: it fails an encoder that leaves + raw" do
      # ServerQuery is what every assertion above rests on. If it left `+`
      # alone, "a+b" and "a b" would both decode to "a+b".
      assert ServerQuery.pairs("/p?q=a+b%2Bc") == [{"q", "a b+c"}]
    end
  end

  describe "the cache holds one answer per user and flag, with its attributes" do
    test "A then B: two requests and two answers" do
      pid = start_client(http_client: ContextHttp)

      assert [true, false] == [enabled?(pid, %{plan: "pro"}), enabled?(pid, %{plan: "free"})]
      assert length(gets()) == 2
    end

    test "minimal pairs differ: {plan: pro} / {tier: pro}, and {n: 1} / {n: \"1\"}" do
      for {a, b} <- [{%{plan: "pro"}, %{tier: "pro"}}, {%{n: 1}, %{n: "1"}}] do
        pid = start_client(http_client: ContextHttp)

        enabled?(pid, a)
        enabled?(pid, b)

        assert length(gets()) == 2
      end
    end

    test "A, B, A: three requests, because the one slot holds B's context" do
      pid = start_client(http_client: ContextHttp)

      answers = [
        enabled?(pid, %{plan: "pro"}),
        enabled?(pid, %{plan: "free"}),
        enabled?(pid, %{plan: "pro"})
      ]

      assert answers == [true, false, true]
      assert length(gets()) == 3
    end

    test "A then no attributes: two requests, and the second is not A's answer" do
      pid = start_client(http_client: ContextHttp)

      assert enabled?(pid, %{plan: "pro"})
      assert {:ok, %FlagEvaluation{enabled: false}} = Client.evaluate_flag(pid, "f", "u1")
      assert length(gets()) == 2
    end

    test "{} and no attributes share an answer: one request" do
      pid = start_client(http_client: ContextHttp)

      assert {:ok, _} = Client.evaluate_flag(pid, "f", "u1", attributes: %{})
      assert {:ok, _} = Client.evaluate_flag(pid, "f", "u1", attributes: %{})
      assert {:ok, _} = Client.evaluate_flag(pid, "f", "u1")

      assert length(gets()) == 1
    end

    test "A then A: one request" do
      pid = start_client(http_client: ContextHttp)

      assert enabled?(pid, %{plan: "pro"})
      assert enabled?(pid, %{plan: "pro"})

      assert length(gets()) == 1
    end

    test "evaluated_flags/2 and the keyless track fan-out list the flag once, with the latest answer" do
      pid = start_client(http_client: ContextHttp)

      enabled?(pid, %{plan: "pro"})
      enabled?(pid, %{plan: "free"})

      assert Client.evaluated_flags(pid, "u1") == ["f"]

      Client.track(pid, "purchase", "u1")

      assert_receive {:post, "/api/v1/tracking/batch", %{events: [event]}, _config}
      assert event.feature_flag_key == "f"
      refute_receive {:post, _path, _body, _config}, 50
    end
  end

  describe "attributes that cannot be written as JSON" do
    @canary "attrcanary7f3"

    test "return the fixed {:error, :invalid_attributes}: nothing is sent, nothing is cached" do
      unencodable = [
        %{"q" => @canary, "bad" => <<255>>},
        %{"q" => @canary, "bad" => self()},
        %{"q" => @canary, {1, 2} => 1},
        %{"q" => @canary, "bad" => fn -> :x end},
        # A struct Jason writes as a string, not as an object.
        DateTime.utc_now()
      ]

      for attributes <- unencodable do
        pid = start_client()

        log =
          ExUnit.CaptureLog.capture_log(fn ->
            stderr =
              ExUnit.CaptureIO.capture_io(:stderr, fn ->
                result = Client.evaluate_flag(pid, "f", "u1", attributes: attributes)
                send(self(), {:result, result})
              end)

            send(self(), {:stderr, stderr})
          end)

        assert_received {:result, result}
        assert_received {:stderr, stderr}
        assert result == {:error, :invalid_attributes}
        refute inspect(result) =~ @canary
        refute log =~ @canary
        refute stderr =~ @canary
        refute_receive {:get, _path, _config}, 20
        assert Client.evaluated_flags(pid, "u1") == []
        refute Client.feature_enabled?(pid, "f", "u1", attributes: attributes)
        refute_receive {:get, _path, _config}, 20
      end
    end

    test "an earlier cached answer is neither served nor replaced" do
      pid = start_client()

      assert {:ok, _} = Client.evaluate_flag(pid, "f", "u1")
      assert_receive {:get, _path, _config}

      assert {:error, :invalid_attributes} =
               Client.evaluate_flag(pid, "f", "u1", attributes: %{"bad" => <<255>>})

      assert {:ok, _} = Client.evaluate_flag(pid, "f", "u1")
      refute_receive {:get, _path, _config}, 50
    end
  end

  describe "no error carries the request's query" do
    # The real HttpClient: its error terms are the ones an integrator sees. No
    # code in this SDK logs on this path, and `:httpc`'s reasons name no URL.
    defp real_client(base_url) do
      pid = start_client(base_url: base_url, http_client: Controlvar.HttpClient, timeout: 2_000)
      pid
    end

    test "network errors: a bad URI, a bad scheme, a refused connection" do
      for base_url <- ["http://loc al:8000", "htp://127.0.0.1:1", "http://127.0.0.1:1"] do
        pid = real_client(base_url)

        log =
          ExUnit.CaptureLog.capture_log(fn ->
            send(
              self(),
              {:result,
               Client.evaluate_flag(pid, "f", "u1", attributes: %{"q" => @canary, "plan" => "pro"})}
            )
          end)

        assert_received {:result, result}
        assert {:error, {:network_error, _reason}} = result

        for text <- [inspect(result), log] do
          refute text =~ @canary
          refute text =~ "context"
          refute text =~ "user_id"
          refute text =~ "%7B"
        end
      end
    end

    test "an API error carries the status and the server's own body" do
      {:ok, listener} = :gen_tcp.listen(0, [:binary, active: false, reuseaddr: true, ip: {127, 0, 0, 1}])
      {:ok, port} = :inet.port(listener)

      server =
        spawn_link(fn ->
          {:ok, socket} = :gen_tcp.accept(listener)
          {:ok, _request} = :gen_tcp.recv(socket, 0, 2_000)
          body = ~s({"detail":"context is not an object"})

          :ok =
            :gen_tcp.send(
              socket,
              "HTTP/1.1 422 Unprocessable Entity\r\ncontent-type: application/json\r\n" <>
                "content-length: #{byte_size(body)}\r\nconnection: close\r\n\r\n" <> body
            )

          :gen_tcp.close(socket)
        end)

      pid = real_client("http://127.0.0.1:#{port}")

      result = Client.evaluate_flag(pid, "f", "u1", attributes: %{"q" => @canary})

      assert result == {:error, {:api_error, 422, ~s({"detail":"context is not an object"})}}
      refute inspect(result) =~ @canary
      :gen_tcp.close(listener)
      Process.unlink(server)
    end
  end

  # --- get_assignment/4 ---

  describe "get_assignment/4" do
    test "POSTs /api/v1/tracking/assign with experiment_key, user_id and context" do
      pid = start_client()

      assert {:ok, _} =
               Client.get_assignment(pid, "checkout-exp", "user-123", %{plan: "pro", country: "US"})

      assert_receive {:post, "/api/v1/tracking/assign", body, %Config{api_key: "test-key"}}

      assert body == %{
               experiment_key: "checkout-exp",
               user_id: "user-123",
               context: %{plan: "pro", country: "US"}
             }
    end

    test "omits context when there are no attributes" do
      pid = start_client()

      assert {:ok, _} = Client.get_assignment(pid, "checkout-exp", "user-123")

      assert_receive {:post, "/api/v1/tracking/assign", body, _config}
      assert body == %{experiment_key: "checkout-exp", user_id: "user-123"}
    end

    test "accepts a keyword list with :attributes" do
      pid = start_client()

      assert {:ok, _} =
               Client.get_assignment(pid, "checkout-exp", "user-123", attributes: %{plan: "pro"})

      assert_receive {:post, "/api/v1/tracking/assign", %{context: %{plan: "pro"}}, _config}
    end

    test "maps the response to %Assignment{}" do
      pid = start_client()

      assert {:ok,
              %Assignment{
                experiment_key: "checkout-exp",
                variant_id: "a2b6c1d4-0000-4000-8000-000000000001",
                variant_name: "treatment",
                is_control: false,
                configuration: %{"headline" => "Buy now"}
              }} = Client.get_assignment(pid, "checkout-exp", "user-123")
    end

    test "falls back to the requested key and nil configuration when absent" do
      pid = start_client()

      assert {:ok,
              %Assignment{
                experiment_key: "control-exp",
                variant_id: nil,
                variant_name: "control",
                is_control: true,
                configuration: nil
              }} = Client.get_assignment(pid, "control-exp", "user-123")
    end

    test "returns {:error, {:api_error, 404, _}} when no experiment has the key" do
      pid = start_client()

      assert {:error, {:api_error, 404, _body}} =
               Client.get_assignment(pid, "unknown-exp", "user-123")
    end

    test "returns {:error, {:malformed_response, _}} when variant_name is missing" do
      pid = start_client()

      assert {:error, {:malformed_response, %{"detail" => _}}} =
               Client.get_assignment(pid, "broken-exp", "user-123")
    end

    test "returns {:error, {:network_error, _}} on a network failure" do
      pid = start_client(http_client: NetworkErrorHttp)

      assert {:error, {:network_error, :econnrefused}} =
               Client.get_assignment(pid, "checkout-exp", "user-123")
    end

    test "is sticky through the cache: the second call does not hit HTTP" do
      pid = start_client()

      {:ok, first} = Client.get_assignment(pid, "checkout-exp", "stable-user")
      {:ok, second} = Client.get_assignment(pid, "checkout-exp", "stable-user")

      assert first == second
      assert_receive {:post, "/api/v1/tracking/assign", _body, _config}
      refute_receive {:post, "/api/v1/tracking/assign", _body, _config}, 50
    end

    test "never caches failures" do
      pid = start_client()

      assert {:error, _} = Client.get_assignment(pid, "unknown-exp", "user-1")
      assert {:error, _} = Client.get_assignment(pid, "unknown-exp", "user-1")

      assert_receive {:post, "/api/v1/tracking/assign", _body, _config}
      assert_receive {:post, "/api/v1/tracking/assign", _body, _config}
    end

    test "skip_cache: true asks the server again" do
      pid = start_client()

      {:ok, _} = Client.get_assignment(pid, "checkout-exp", "user-1")
      {:ok, _} = Client.get_assignment(pid, "checkout-exp", "user-1", skip_cache: true)

      assert_receive {:post, "/api/v1/tracking/assign", _body, _config}
      assert_receive {:post, "/api/v1/tracking/assign", _body, _config}
    end
  end

  # --- track/5 ---

  describe "track/5 with a key" do
    test "POSTs one /api/v1/tracking/track with the full body" do
      pid = start_client()

      assert :ok =
               Client.track(pid, "purchase", "user-123", %{sku: "pro"},
                 experiment_key: "checkout-exp",
                 value: 12.5
               )

      assert_receive {:post, "/api/v1/tracking/track", body, _config}, 500

      assert body.event_type == "purchase"
      assert body.event_name == "purchase"
      assert body.user_id == "user-123"
      assert body.experiment_key == "checkout-exp"
      assert body.value == 12.5
      assert body.metadata == %{sku: "pro"}
      assert {:ok, _, _} = DateTime.from_iso8601(body.timestamp)
      refute Map.has_key?(body, :feature_flag_key)

      refute_receive {:post, "/api/v1/tracking/batch", _body, _config}, 50
    end

    test "sends feature_flag_key when given" do
      pid = start_client()

      :ok = Client.track(pid, "search", "user-123", %{}, feature_flag_key: "new-search")

      assert_receive {:post, "/api/v1/tracking/track", body, _config}, 500
      assert body.feature_flag_key == "new-search"
      refute Map.has_key?(body, :experiment_key)
      refute Map.has_key?(body, :metadata)
      refute Map.has_key?(body, :value)
    end

    test "sends both keys when both are given" do
      pid = start_client()

      :ok =
        Client.track(pid, "click", "user-123", %{},
          experiment_key: "checkout-exp",
          feature_flag_key: "new-search"
        )

      assert_receive {:post, "/api/v1/tracking/track",
                      %{experiment_key: "checkout-exp", feature_flag_key: "new-search"}, _config},
                     500
    end

    test "event_type overrides the default and timestamps are passed through" do
      pid = start_client()
      at = ~U[2026-01-02 03:04:05Z]

      :ok =
        Client.track(pid, "purchase", "user-123", %{},
          experiment_key: "checkout-exp",
          event_type: "conversion",
          timestamp: at
        )

      assert_receive {:post, "/api/v1/tracking/track", body, _config}, 500
      assert body.event_type == "conversion"
      assert body.event_name == "purchase"
      assert body.timestamp == "2026-01-02T03:04:05Z"
    end
  end

  describe "track/5 without a key (fan-out)" do
    test "sends one /tracking/batch entry per cached assignment and evaluated flag" do
      pid = start_client()

      {:ok, _} = Client.get_assignment(pid, "checkout-exp", "user-1")
      {:ok, _} = Client.evaluate_flag(pid, "dark-mode", "user-1")
      assert_receive {:post, "/api/v1/tracking/assign", _, _}
      assert_receive {:get, _, _}

      :ok = Client.track(pid, "page_view", "user-1", %{page: "/"})

      assert_receive {:post, "/api/v1/tracking/batch", %{events: events}, _config}, 500
      assert [assignment_event, flag_event] = events

      assert assignment_event.experiment_key == "checkout-exp"
      refute Map.has_key?(assignment_event, :feature_flag_key)
      assert flag_event.feature_flag_key == "dark-mode"
      refute Map.has_key?(flag_event, :experiment_key)

      for event <- events do
        assert event.event_type == "page_view"
        assert event.event_name == "page_view"
        assert event.user_id == "user-1"
        assert event.metadata == %{page: "/"}
      end

      refute_receive {:post, "/api/v1/tracking/track", _body, _config}, 50
    end

    test "only fans out to entries cached for that user" do
      pid = start_client()

      {:ok, _} = Client.get_assignment(pid, "checkout-exp", "user-1")
      {:ok, _} = Client.evaluate_flag(pid, "dark-mode", "user-2")
      assert_receive {:post, "/api/v1/tracking/assign", _, _}
      assert_receive {:get, _, _}

      :ok = Client.track(pid, "page_view", "user-2")

      assert_receive {:post, "/api/v1/tracking/batch", %{events: [event]}, _config}, 500
      assert event.feature_flag_key == "dark-mode"
      assert event.user_id == "user-2"
    end

    test "sends nothing when nothing is cached for the user" do
      pid = start_client()

      :ok = Client.track(pid, "page_view", "user-1", %{page: "/"})

      refute_receive {:post, _path, _body, _config}, 200
    end

    test "chunks the fan-out into batches of at most 100 events" do
      pid = start_client()

      for i <- 1..101 do
        {:ok, _} = Client.evaluate_flag(pid, "flag-#{i}", "user-1")
        assert_receive {:get, _, _}
      end

      :ok = Client.track(pid, "page_view", "user-1")

      assert_receive {:post, "/api/v1/tracking/batch", %{events: first}, _config}, 500
      assert_receive {:post, "/api/v1/tracking/batch", %{events: second}, _config}, 500
      assert Enum.sort([length(first), length(second)]) == [1, 100]
      refute_receive {:post, "/api/v1/tracking/batch", _body, _config}, 50
    end
  end

  describe "track/5 never raises" do
    test "returns :ok immediately even when HTTP fails" do
      pid = start_client(http_client: NetworkErrorHttp)

      assert :ok = Client.track(pid, "event", "user-1", %{}, experiment_key: "exp")
      assert_receive {:post, "/api/v1/tracking/track", _body}, 500

      assert Process.alive?(pid)
    end

    test "returns :ok for malformed input and keeps the client alive" do
      pid = start_client()

      assert :ok = Client.track(pid, "event", "user-1", "not-a-map", experiment_key: "exp")
      assert :ok = Client.track(pid, "event", %{bad: :user}, %{}, experiment_key: "exp")
      assert :ok = Client.track(pid, "event", "user-1", %{}, :not_a_keyword_list)

      :timer.sleep(50)
      assert Process.alive?(pid)
      assert {:ok, _} = Client.evaluate_flag(pid, "dark-mode", "user-1")
    end
  end

  # --- track_batch/2 ---

  describe "track_batch/2" do
    test "POSTs /api/v1/tracking/batch with normalized events and returns the counts" do
      pid = start_client()

      assert {:ok, %BatchResult{success_count: 2, failure_count: 0, errors: nil}} =
               Client.track_batch(pid, [
                 %{
                   event_name: "purchase",
                   user_id: "user-1",
                   experiment_key: "checkout-exp",
                   value: 99.5,
                   properties: %{sku: "pro"}
                 },
                 %{
                   "event_name" => "search",
                   "user_id" => "user-1",
                   "feature_flag_key" => "new-search",
                   "event_type" => "interaction"
                 }
               ])

      assert_receive {:post, "/api/v1/tracking/batch", %{events: [purchase, search]}, _config}

      assert purchase.event_type == "purchase"
      assert purchase.experiment_key == "checkout-exp"
      assert purchase.value == 99.5
      assert purchase.metadata == %{sku: "pro"}

      assert search.event_type == "interaction"
      assert search.event_name == "search"
      assert search.feature_flag_key == "new-search"
    end

    test "chunks into requests of at most 100 events" do
      pid = start_client()

      events =
        for i <- 1..150 do
          %{event_name: "e#{i}", user_id: "user-1", experiment_key: "checkout-exp"}
        end

      assert {:ok, %BatchResult{success_count: 150, failure_count: 0}} =
               Client.track_batch(pid, events)

      assert_receive {:post, "/api/v1/tracking/batch", %{events: first}, _config}
      assert_receive {:post, "/api/v1/tracking/batch", %{events: second}, _config}
      assert length(first) == 100
      assert length(second) == 50
    end

    test "counts malformed events as local failures without sending them" do
      pid = start_client()

      assert {:ok, %BatchResult{success_count: 1, failure_count: 2, errors: errors}} =
               Client.track_batch(pid, [
                 %{user_id: "user-1", experiment_key: "checkout-exp"},
                 %{event_name: "ok", user_id: "user-1", experiment_key: "checkout-exp"},
                 "not a map"
               ])

      assert [%{index: 0, error: "event_name is required"}, %{index: 2, error: "event must be a map"}] =
               errors

      assert_receive {:post, "/api/v1/tracking/batch", %{events: [_only_one]}, _config}
    end

    test "sends nothing for an empty list" do
      pid = start_client()

      assert {:ok, %BatchResult{success_count: 0, failure_count: 0, errors: nil}} =
               Client.track_batch(pid, [])

      refute_receive {:post, _path, _body, _config}, 50
    end

    test "surfaces server-side rejections" do
      pid = start_client(http_client: BatchRejectHttp)

      assert {:ok, %BatchResult{success_count: 1, failure_count: 1, errors: [%{"index" => 0}]}} =
               Client.track_batch(pid, [
                 %{event_name: "a", user_id: "u", experiment_key: "x"},
                 %{event_name: "b", user_id: "u", experiment_key: "x"}
               ])
    end

    test "returns {:error, reason} when the request fails" do
      pid = start_client(http_client: NetworkErrorHttp)

      assert {:error, {:network_error, :econnrefused}} =
               Client.track_batch(pid, [%{event_name: "a", user_id: "u", experiment_key: "x"}])
    end
  end

  # --- cache helpers ---

  describe "cache helpers" do
    test "assignments/2 and evaluated_flags/2 list what is cached for the user" do
      pid = start_client()

      assert Client.assignments(pid, "user-1") == []
      assert Client.evaluated_flags(pid, "user-1") == []

      {:ok, assignment} = Client.get_assignment(pid, "checkout-exp", "user-1")
      {:ok, _} = Client.evaluate_flag(pid, "dark-mode", "user-1")
      {:ok, _} = Client.evaluate_flag(pid, "other-flag", "user-1")
      {:error, _} = Client.evaluate_flag(pid, "missing-flag", "user-1")

      assert Client.assignments(pid, "user-1") == [assignment]
      assert Client.evaluated_flags(pid, "user-1") == ["dark-mode", "other-flag"]
      assert Client.assignments(pid, "user-2") == []
    end

    test "clear_cache/1 drops everything" do
      pid = start_client()

      {:ok, _} = Client.get_assignment(pid, "checkout-exp", "user-1")
      {:ok, _} = Client.evaluate_flag(pid, "dark-mode", "user-1")

      assert :ok = Client.clear_cache(pid)
      assert Client.assignments(pid, "user-1") == []
      assert Client.evaluated_flags(pid, "user-1") == []

      {:ok, _} = Client.evaluate_flag(pid, "dark-mode", "user-1")
      assert_receive {:get, _, _}
      assert_receive {:get, _, _}
    end
  end

  # --- lifecycle ---

  describe "start_link/1 and stop/1" do
    test "accepts a keyword list config" do
      {:ok, pid} = Client.start_link(base_url: "http://localhost", api_key: "k", http_client: SpyHttp)
      on_exit(fn -> ProcessHelpers.stop_if_alive(pid, &Client.stop/1) end)
      assert Process.alive?(pid)
    end

    test "accepts a map config" do
      {:ok, pid} =
        Client.start_link(%{base_url: "http://localhost", api_key: "k", http_client: SpyHttp})

      on_exit(fn -> ProcessHelpers.stop_if_alive(pid, &Client.stop/1) end)
      assert Process.alive?(pid)
    end

    test "accepts a Config struct" do
      config = Config.new(base_url: "http://localhost", api_key: "k", http_client: SpyHttp)
      {:ok, pid} = Client.start_link(config)
      on_exit(fn -> ProcessHelpers.stop_if_alive(pid, &Client.stop/1) end)
      assert Process.alive?(pid)
    end

    test "registers the process under :name" do
      {:ok, pid} =
        Client.start_link(
          base_url: "http://localhost",
          api_key: "k",
          http_client: SpyHttp,
          name: :client_test_named
        )

      on_exit(fn -> ProcessHelpers.stop_if_alive(pid, &Client.stop/1) end)
      assert Process.whereis(:client_test_named) == pid
      assert {:ok, _} = Client.evaluate_flag(:client_test_named, "dark-mode", "user-1")
    end

    test "raises ArgumentError for missing base_url" do
      assert_raise ArgumentError, ~r/base_url required/, fn ->
        Client.start_link(api_key: "k")
      end
    end

    test "raises ArgumentError for missing api_key" do
      assert_raise ArgumentError, ~r/api_key required/, fn ->
        Client.start_link(base_url: "http://localhost")
      end
    end

    test "stop/1 terminates the client and waits for in-flight track requests" do
      pid = start_client()

      :ok = Client.track(pid, "purchase", "user-1", %{}, experiment_key: "checkout-exp")
      assert :ok = Client.stop(pid)

      refute Process.alive?(pid)
      assert_receive {:post, "/api/v1/tracking/track", _body, _config}, 500
    end
  end
end
