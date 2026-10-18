module Controlvar
  # Configuration struct for the SDK client.
  #
  # Fields:
  #   base_url      - String: API base URL (required)
  #   api_key       - String: API key for authentication (required)
  #   cache_ttl     - Integer: cache TTL in seconds (default: 300)
  #   timeout       - Integer: HTTP timeout in seconds (default: 10)
  #   max_cache_size - Integer: max entries in local cache (default: 1000)
  SdkConfig = Struct.new(
    :base_url,
    :api_key,
    :cache_ttl,
    :timeout,
    :max_cache_size
  ) do
    def initialize(base_url: nil, api_key: nil, cache_ttl: 300, timeout: 10, max_cache_size: 1000,
                   **_opts)
      super(base_url, api_key, cache_ttl, timeout, max_cache_size)
    end

    # Raises ArgumentError if required fields are missing or empty.
    def validate!
      raise ArgumentError, "base_url is required" if base_url.nil? || base_url.to_s.empty?
      raise ArgumentError, "api_key is required" if api_key.nil? || api_key.to_s.empty?

      self
    end
  end
end
