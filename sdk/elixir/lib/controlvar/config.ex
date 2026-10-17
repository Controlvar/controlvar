defmodule Controlvar.Config do
  @moduledoc """
  Configuration struct for the Controlvar SDK client.

  ## Required fields
  - `:base_url` - The base URL of the Controlvar API (e.g., "https://api.example.com")
  - `:api_key` - Your API key for authentication

  ## Optional fields
  - `:cache_ttl` - Cache time-to-live in seconds (default: 300)
  - `:timeout` - HTTP request timeout in milliseconds (default: 10_000)
  - `:max_cache_size` - Maximum number of entries in the local cache (default: 1_000)
  - `:http_client` - HTTP client module to use (default: Controlvar.HttpClient). Useful for testing.
  """

  @enforce_keys [:base_url, :api_key]
  defstruct [
    :base_url,
    :api_key,
    cache_ttl: 300,
    timeout: 10_000,
    max_cache_size: 1_000,
    http_client: nil
  ]

  @type t :: %__MODULE__{
          base_url: String.t(),
          api_key: String.t(),
          cache_ttl: non_neg_integer(),
          timeout: non_neg_integer(),
          max_cache_size: pos_integer(),
          http_client: module() | nil
        }

  @doc """
  Create a new Config struct, validating required fields.

  ## Examples

      iex> Config.new(base_url: "https://api.example.com", api_key: "secret")
      %Config{base_url: "https://api.example.com", api_key: "secret", cache_ttl: 300, timeout: 10_000, max_cache_size: 1_000}

      iex> Config.new(%{base_url: "https://api.example.com", api_key: "secret"})
      %Config{base_url: "https://api.example.com", api_key: "secret", cache_ttl: 300, timeout: 10_000, max_cache_size: 1_000}
  """
  @spec new(keyword() | map()) :: t()
  def new(opts) when is_map(opts) do
    new(Enum.to_list(opts))
  end

  def new(opts) when is_list(opts) do
    # Validate before building: `@enforce_keys` makes `struct!/2` raise first,
    # with "the following keys must also be given when building struct ...",
    # so the messages below were unreachable and a caller who forgot an option
    # got struct internals instead of what to pass.
    base_url = Keyword.get(opts, :base_url)
    api_key = Keyword.get(opts, :api_key)

    if is_nil(base_url) or base_url == "" do
      raise ArgumentError, "base_url required"
    end

    if is_nil(api_key) or api_key == "" do
      raise ArgumentError, "api_key required"
    end

    struct!(__MODULE__, opts)
  end
end
