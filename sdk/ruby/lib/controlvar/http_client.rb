require 'net/http'
require 'uri'
require 'json'

module Controlvar
  # Lightweight HTTP client wrapping Net::HTTP.
  #
  # Every request carries the configured API key in the +X-API-Key+ header plus
  # +Accept+/+Content-Type: application/json+. Responses are parsed as JSON.
  # Non-2xx responses raise APIError subclasses; network failures raise NetworkError.
  #
  # No message it raises names the request's query string: on a flag
  # evaluation that is +?user_id=...&context=...+, the user's id and attributes.
  #
  # A new Net::HTTP connection is opened per request, so a single instance can
  # be shared between threads.
  class HttpClient
    # @param base_url [String] API origin (e.g. "http://localhost:8000"); paths are appended
    # @param api_key  [String] API key sent as the "X-API-Key" header
    # @param timeout  [Integer] open/read timeout in seconds (default: 10)
    def initialize(base_url:, api_key:, timeout: 10)
      @base_url = base_url.chomp('/')
      @api_key  = api_key
      @timeout  = timeout.to_i
    end

    # Perform a GET request.
    #
    # @param path    [String] URL path including any query string
    #                         (e.g. "/api/v1/feature-flags/evaluate/my-flag?user_id=u1")
    # @param headers [Hash]   additional HTTP headers
    # @return [Hash, Array] parsed JSON response body
    # @raise [AuthenticationError] on 401
    # @raise [APIError]            on other 4xx/5xx
    # @raise [NetworkError]        on connection/timeout failures
    def get(path, headers: {})
      request(:get, path, nil, headers)
    end

    # Perform a POST request with a JSON body.
    #
    # @param path    [String] URL path
    # @param body    [Hash]   request body (serialized to JSON)
    # @param headers [Hash]   additional HTTP headers
    # @return [Hash, Array] parsed JSON response body
    # @raise [AuthenticationError] on 401
    # @raise [APIError]            on other 4xx/5xx
    # @raise [NetworkError]        on connection/timeout failures
    def post(path, body, headers: {})
      request(:post, path, body, headers)
    end

    private

    def request(method, path, body, extra_headers)
      uri = URI.parse("#{@base_url}#{path}")

      http = Net::HTTP.new(uri.host, uri.port)
      http.use_ssl     = (uri.scheme == 'https')
      http.open_timeout = @timeout
      http.read_timeout = @timeout

      req_class = method == :get ? Net::HTTP::Get : Net::HTTP::Post
      req = req_class.new(uri.request_uri)

      # Default headers
      req['X-API-Key']    = @api_key
      req['Accept']       = 'application/json'
      req['Content-Type'] = 'application/json'
      req['User-Agent']   = "Controlvar-Ruby/#{Controlvar::VERSION}"

      extra_headers.each { |k, v| req[k] = v }

      if body
        req.body = body.is_a?(String) ? body : JSON.generate(body)
      end

      response = http.request(req)
      handle_response(response, path)
    rescue Net::OpenTimeout, Net::ReadTimeout, Errno::ETIMEDOUT => e
      raise NetworkError, without_query("Request timed out: #{e.message}", path)
    rescue Errno::ECONNREFUSED, Errno::EHOSTUNREACH, Errno::ECONNRESET,
           Errno::ENETUNREACH, SocketError => e
      raise NetworkError, without_query("Connection failed: #{e.message}", path)
    rescue NetworkError, APIError, AuthenticationError
      raise
    rescue StandardError => e
      # URI::InvalidURIError (a base_url with a space, say) quotes the whole URL.
      raise NetworkError, without_query("Unexpected network error: #{e.message}", path)
    end

    def handle_response(response, path)
      code = response.code.to_i

      case code
      when 200..299
        body = response.body
        return {} if body.nil? || body.strip.empty?

        begin
          JSON.parse(body, symbolize_names: false)
        rescue JSON::ParserError
          {}
        end
      when 401
        msg = extract_error_message(response) || "Authentication failed"
        raise AuthenticationError.new(without_query(msg, path), status_code: 401)
      else
        msg = extract_error_message(response) || "API error (HTTP #{code})"
        raise APIError.new(without_query(msg, path), status_code: code)
      end
    end

    # +text+ without +path+'s query string, with or without its "?": the query
    # of a flag evaluation holds the user's id and attributes. The client
    # percent-encodes that query to plain ASCII, which String#inspect (what
    # URI::InvalidURIError quotes) leaves as it is. +scrub+ first, so that a
    # server message that is not valid UTF-8 is still raised as itself.
    def without_query(text, path)
      query = path.to_s.partition('?')[2]
      return text.to_s if query.empty?

      text.to_s.scrub.gsub("?#{query}", '').gsub(query, '')
    end

    def extract_error_message(response)
      body = response.body
      return nil if body.nil? || body.strip.empty?

      parsed = JSON.parse(body)
      parsed['detail'] || parsed['message'] || parsed['error'] || body
    rescue JSON::ParserError
      body
    end
  end
end
