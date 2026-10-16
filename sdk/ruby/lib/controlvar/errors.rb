module Controlvar
  # Base error class for all SDK errors
  class Error < StandardError; end

  # Raised when SDK configuration is invalid
  class ConfigurationError < Error; end

  # Raised on network connectivity failures (timeout, connection refused, etc.)
  class NetworkError < Error; end

  # Raised when the API returns a non-2xx HTTP response
  class APIError < Error
    attr_reader :status_code

    def initialize(msg = nil, status_code: nil)
      super(msg)
      @status_code = status_code
    end
  end

  # Raised when the API returns a 401 Unauthorized response
  class AuthenticationError < APIError
    def initialize(msg = "Authentication failed: invalid or missing API key", status_code: 401)
      super(msg, status_code: status_code)
    end
  end
end
