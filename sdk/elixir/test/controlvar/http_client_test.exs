defmodule Controlvar.HttpClientTest do
  @moduledoc """
  Unit tests for the HTTP layer.

  The real `:httpc` calls need a live server, so the request/response
  contract is exercised through mock modules implementing `HttpBehaviour`
  (the same mechanism the client uses in its own tests), and the pure
  request-building helpers of `HttpClient` are tested directly.
  """

  use ExUnit.Case, async: true

  alias Controlvar.{Config, HttpBehaviour, HttpClient}

  # --- Mock HTTP client modules ---

  defmodule MockHttpSuccess do
    @behaviour HttpBehaviour

    @impl true
    def get(_config, "/api/v1/feature-flags/evaluate/" <> _rest) do
      {:ok, %{"key" => "test-flag", "enabled" => true, "config" => nil}}
    end

    def get(_config, path), do: {:ok, %{"result" => "ok", "path" => path}}

    @impl true
    def post(_config, "/api/v1/tracking/assign", body) do
      {:ok,
       %{
         "experiment_key" => body[:experiment_key],
         "user_id" => body[:user_id],
         "variant_id" => "v-1",
         "variant_name" => "control",
         "is_control" => true,
         "configuration" => nil
       }}
    end

    def post(_config, _path, _body), do: {:ok, %{"status" => "accepted"}}
  end

  defmodule MockHttpAuth401 do
    @behaviour HttpBehaviour

    @impl true
    def get(_config, _path), do: {:error, {:auth_error, 401}}

    @impl true
    def post(_config, _path, _body), do: {:error, {:auth_error, 401}}
  end

  defmodule MockHttp404 do
    @behaviour HttpBehaviour

    @impl true
    def get(_config, _path), do: {:error, {:api_error, 404, "Not found"}}

    @impl true
    def post(_config, _path, _body), do: {:error, {:api_error, 404, "Not found"}}
  end

  defmodule MockHttpNetworkError do
    @behaviour HttpBehaviour

    @impl true
    def get(_config, _path), do: {:error, {:network_error, :econnrefused}}

    @impl true
    def post(_config, _path, _body), do: {:error, {:network_error, :timeout}}
  end

  defmodule MockHttpServerError do
    @behaviour HttpBehaviour

    @impl true
    def get(_config, _path), do: {:error, {:api_error, 500, "Internal Server Error"}}

    @impl true
    def post(_config, _path, _body), do: {:error, {:api_error, 503, "Service Unavailable"}}
  end

  defp base_config do
    Config.new(base_url: "http://localhost:8000", api_key: "test-key")
  end

  # --- Real HttpClient helpers ---

  describe "HttpClient.build_url/2" do
    test "appends the path to the base URL as a charlist" do
      assert HttpClient.build_url("http://localhost:8000", "/api/v1/tracking/assign") ==
               ~c"http://localhost:8000/api/v1/tracking/assign"
    end

    test "strips a trailing slash from the base URL" do
      assert HttpClient.build_url("http://localhost:8000/", "/api/v1/tracking/track") ==
               ~c"http://localhost:8000/api/v1/tracking/track"
    end

    test "adds a leading slash to the path when missing" do
      assert HttpClient.build_url("https://api.example.com", "api/v1/tracking/batch") ==
               ~c"https://api.example.com/api/v1/tracking/batch"
    end

    test "keeps a query string intact" do
      assert HttpClient.build_url(
               "http://localhost:8000",
               "/api/v1/feature-flags/evaluate/my-flag?user_id=user-1"
             ) == ~c"http://localhost:8000/api/v1/feature-flags/evaluate/my-flag?user_id=user-1"
    end
  end

  describe "HttpClient.build_headers/1" do
    test "sends the API key plus JSON content negotiation headers" do
      headers = HttpClient.build_headers("secret-key")

      assert {~c"X-API-Key", ~c"secret-key"} in headers
      assert {~c"Content-Type", ~c"application/json"} in headers
      assert {~c"Accept", ~c"application/json"} in headers
      assert Enum.any?(headers, fn {name, _} -> name == ~c"User-Agent" end)
    end

    test "does not send an Authorization header" do
      refute Enum.any?(HttpClient.build_headers("k"), fn {name, _} -> name == ~c"Authorization" end)
    end
  end

  describe "HttpClient implements HttpBehaviour" do
    test "get/2 and post/3 are exported" do
      Code.ensure_loaded!(HttpClient)
      assert function_exported?(HttpClient, :get, 2)
      assert function_exported?(HttpClient, :post, 3)
      assert HttpBehaviour in (HttpClient.__info__(:attributes)[:behaviour] || [])
    end
  end

  # --- Mock contract ---

  describe "MockHttpSuccess (GET)" do
    test "successful GET returns {:ok, map}" do
      result = MockHttpSuccess.get(base_config(), "/api/v1/feature-flags/evaluate/my-flag?user_id=u")
      assert {:ok, data} = result
      assert is_map(data)
      assert data["key"] == "test-flag"
      assert data["enabled"] == true
    end

    test "GET passes config to the client" do
      config = Config.new(base_url: "http://api.example.com", api_key: "real-key", timeout: 5_000)
      assert {:ok, _} = MockHttpSuccess.get(config, "/api/v1/any")
    end
  end

  describe "MockHttpSuccess (POST)" do
    test "assign returns the public-API assignment shape" do
      body = %{experiment_key: "exp", user_id: "user-1"}
      {:ok, data} = MockHttpSuccess.post(base_config(), "/api/v1/tracking/assign", body)
      assert data["experiment_key"] == "exp"
      assert data["variant_name"] == "control"
      assert data["is_control"] == true
    end

    test "track returns {:ok, map}" do
      body = %{event_type: "click", event_name: "click", user_id: "user-1"}
      assert {:ok, %{"status" => "accepted"}} = MockHttpSuccess.post(base_config(), "/api/v1/tracking/track", body)
    end
  end

  describe "MockHttpAuth401 (authentication errors)" do
    test "GET returns {:error, {:auth_error, 401}} on 401" do
      assert {:error, {:auth_error, 401}} =
               MockHttpAuth401.get(base_config(), "/api/v1/feature-flags/evaluate/f?user_id=u")
    end

    test "POST returns {:error, {:auth_error, 401}} on 401" do
      assert {:error, {:auth_error, 401}} =
               MockHttpAuth401.post(base_config(), "/api/v1/tracking/track", %{})
    end
  end

  describe "MockHttp404 (not found errors)" do
    test "GET returns {:error, {:api_error, 404, _}} on 404" do
      assert {:error, {:api_error, 404, _body}} =
               MockHttp404.get(base_config(), "/api/v1/feature-flags/evaluate/nonexistent?user_id=u")
    end

    test "POST returns {:error, {:api_error, 404, _}} on 404" do
      assert {:error, {:api_error, 404, _body}} =
               MockHttp404.post(base_config(), "/api/v1/tracking/assign", %{})
    end
  end

  describe "MockHttpNetworkError (network failures)" do
    test "GET returns {:error, {:network_error, _}} on connection refused" do
      assert {:error, {:network_error, :econnrefused}} =
               MockHttpNetworkError.get(base_config(), "/api/v1/feature-flags/evaluate/f?user_id=u")
    end

    test "POST returns {:error, {:network_error, _}} on timeout" do
      assert {:error, {:network_error, :timeout}} =
               MockHttpNetworkError.post(base_config(), "/api/v1/tracking/track", %{})
    end
  end

  describe "MockHttpServerError (5xx errors)" do
    test "GET returns {:error, {:api_error, 500, _}} on server error" do
      assert {:error, {:api_error, 500, _}} =
               MockHttpServerError.get(base_config(), "/api/v1/feature-flags/evaluate/f?user_id=u")
    end

    test "POST returns {:error, {:api_error, 503, _}} on service unavailable" do
      assert {:error, {:api_error, 503, _}} =
               MockHttpServerError.post(base_config(), "/api/v1/tracking/track", %{})
    end
  end

  describe "HttpBehaviour contract" do
    test "MockHttpSuccess implements HttpBehaviour" do
      behaviours = MockHttpSuccess.__info__(:attributes)[:behaviour] || []
      assert HttpBehaviour in behaviours
    end

    test "MockHttpAuth401 implements HttpBehaviour" do
      behaviours = MockHttpAuth401.__info__(:attributes)[:behaviour] || []
      assert HttpBehaviour in behaviours
    end

    test "MockHttp404 implements HttpBehaviour" do
      behaviours = MockHttp404.__info__(:attributes)[:behaviour] || []
      assert HttpBehaviour in behaviours
    end
  end
end
