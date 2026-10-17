defmodule Controlvar.HttpBehaviour do
  @moduledoc """
  Behaviour defining the HTTP client interface.

  Implement this to provide a custom or mock HTTP client (pass the module as
  the `:http_client` config option).
  """

  @callback get(config :: Controlvar.Config.t(), path :: String.t()) ::
              {:ok, map()} | {:error, term()}

  @callback post(
              config :: Controlvar.Config.t(),
              path :: String.t(),
              body :: map()
            ) :: {:ok, map()} | {:error, term()}
end

defmodule Controlvar.HttpClient do
  @moduledoc """
  HTTP client using OTP's built-in `:httpc` (from `:inets`) for zero external
  dependencies beyond Jason.

  Every request carries the configured API key in the `X-API-Key` header plus
  `Content-Type`/`Accept: application/json`. Responses are decoded as JSON.

  ## Error Tuples

  - `{:error, {:auth_error, 401}}` — 401 Unauthorized (bad API key)
  - `{:error, {:api_error, status_code, body}}` — other 4xx/5xx
    (404 for an unknown flag key, and for an experiment that is not ACTIVE; 422 validation, 429 rate limited)
  - `{:error, {:network_error, reason}}` — connection failures and timeouts
  """

  @behaviour Controlvar.HttpBehaviour

  alias Controlvar.Config

  @user_agent ~c"Controlvar-Elixir-SDK/0.1.0"

  @doc """
  Perform a GET request to the given path (which may include a query string).

  ## Returns
  - `{:ok, map()}` on success (2xx)
  - `{:error, {:auth_error, 401}}` on authentication failure
  - `{:error, {:api_error, status_code, body}}` on other HTTP errors
  - `{:error, {:network_error, reason}}` on network failure
  """
  @impl true
  @spec get(Config.t(), String.t()) :: {:ok, map()} | {:error, term()}
  def get(%Config{} = config, path) do
    ensure_started()
    request = {build_url(config.base_url, path), build_headers(config.api_key)}

    :get
    |> :httpc.request(request, http_options(config.timeout), request_options())
    |> handle_result()
  end

  @doc """
  Perform a POST request to the given path with a JSON body.

  ## Returns
  - `{:ok, map()}` on success (2xx)
  - `{:error, {:auth_error, 401}}` on authentication failure
  - `{:error, {:api_error, status_code, body}}` on other HTTP errors
  - `{:error, {:network_error, reason}}` on network failure
  """
  @impl true
  @spec post(Config.t(), String.t(), map()) :: {:ok, map()} | {:error, term()}
  def post(%Config{} = config, path, body) do
    ensure_started()

    encoded_body =
      case Jason.encode(body) do
        {:ok, json} -> json
        {:error, reason} -> raise ArgumentError, "Failed to encode request body: #{inspect(reason)}"
      end

    request =
      {build_url(config.base_url, path), build_headers(config.api_key), ~c"application/json",
       encoded_body}

    :post
    |> :httpc.request(request, http_options(config.timeout), request_options())
    |> handle_result()
  end

  @doc false
  @spec build_url(String.t(), String.t()) :: charlist()
  def build_url(base_url, path) do
    base = String.trim_trailing(base_url, "/")
    normalized_path = if String.starts_with?(path, "/"), do: path, else: "/" <> path
    to_charlist("#{base}#{normalized_path}")
  end

  @doc false
  @spec build_headers(String.t()) :: [{charlist(), charlist()}]
  def build_headers(api_key) do
    [
      {~c"X-API-Key", to_charlist(api_key)},
      {~c"Content-Type", ~c"application/json"},
      {~c"Accept", ~c"application/json"},
      {~c"User-Agent", @user_agent}
    ]
  end

  # --- Private Helpers ---

  defp ensure_started do
    {:ok, _} = Application.ensure_all_started(:inets)
    {:ok, _} = Application.ensure_all_started(:ssl)
    :ok
  end

  defp http_options(timeout), do: [timeout: timeout, connect_timeout: timeout]

  defp request_options, do: [body_format: :binary]

  defp handle_result({:ok, {{_version, status, _reason}, _resp_headers, body}}) do
    handle_response(status, body)
  end

  defp handle_result({:error, reason}), do: {:error, {:network_error, reason}}

  defp handle_response(status, body) when status >= 200 and status < 300 do
    body_str = to_string(body)

    case Jason.decode(body_str) do
      {:ok, decoded} when is_map(decoded) -> {:ok, decoded}
      {:ok, decoded} -> {:ok, %{"data" => decoded}}
      {:error, _} -> {:ok, %{"raw" => body_str}}
    end
  end

  defp handle_response(401, _body), do: {:error, {:auth_error, 401}}

  defp handle_response(status, body), do: {:error, {:api_error, status, to_string(body)}}
end
