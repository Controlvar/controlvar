require 'json'
require 'time'
require 'uri'

module Controlvar
  # Main SDK client for the Controlvar experimentation platform.
  #
  # Flag evaluation and experiment assignment are decided by the server; the
  # SDK never buckets users locally. Successful results are cached per
  # user + key for +cache_ttl+ seconds; failures are never cached.
  #
  #   - evaluate_flag    : GET  /api/v1/feature-flags/evaluate/{key}?user_id=...
  #   - get_assignment   : POST /api/v1/tracking/assign   (sticky on the server)
  #   - track            : POST /api/v1/tracking/track    (with a key)
  #                        POST /api/v1/tracking/batch    (fan-out without a key)
  #   - track_batch      : POST /api/v1/tracking/batch
  #
  # Example:
  #   client = Controlvar::Client.new(
  #     base_url: "http://localhost:8000",
  #     api_key:  "sk_live_xxx"
  #   )
  #   flag = client.evaluate_flag("dark-mode", "user-123")
  #   flag.enabled?   # => true / false
  #
  # All public methods are safe to call from multiple threads: the cache is
  # mutex-protected and every request opens its own connection.
  class Client
    # Maximum events per POST /api/v1/tracking/batch request.
    BATCH_LIMIT = 100

    # @param config [SdkConfig, Hash] configuration object or keyword args
    def initialize(config = nil, **kwargs)
      if config.is_a?(SdkConfig)
        @config = config
      else
        # Accept keyword arguments for convenience
        opts = config.is_a?(Hash) ? config.merge(kwargs) : kwargs
        @config = SdkConfig.new(**opts)
      end

      @config.validate!

      @http  = HttpClient.new(
        base_url: @config.base_url,
        api_key:  @config.api_key,
        timeout:  @config.timeout
      )
      @cache = Cache.new(
        ttl:      @config.cache_ttl,
        max_size: @config.max_cache_size
      )
    end

    # Evaluate a feature flag for a user. The server decides.
    #
    # @param flag_key    [String] feature flag key
    # @param user_id     [String] user identifier
    # @param _attributes [Hash]   accepted for API symmetry with get_assignment
    #                             and not sent: the evaluate endpoint takes a
    #                             context, which this SDK does not send yet
    # @return [FlagEvaluation] never nil. On a network/HTTP failure (including
    #   404 for an unknown flag key) the flag is reported as disabled; a flag
    #   that is not ACTIVE is a normal answer with enabled false. A cached
    #   evaluation, when present, is returned instead.
    def evaluate_flag(flag_key, user_id, _attributes = {})
      cache_key = flag_cache_key(user_id, flag_key)
      cached = @cache.get(cache_key)
      return cached unless cached.nil?

      path = "/api/v1/feature-flags/evaluate/#{encode(flag_key)}?user_id=#{encode(user_id)}"
      data = @http.get(path)
      data = {} unless data.is_a?(Hash)

      evaluation = FlagEvaluation.new(
        key:     flag_key,
        enabled: data['enabled'] == true,
        config:  data['config']
      )
      @cache.set(cache_key, evaluation)
      evaluation
    rescue NetworkError, APIError => e
      warn "[Controlvar] evaluate_flag error: #{e.message}"
      FlagEvaluation.new(key: flag_key, enabled: false, config: nil)
    end

    # Boolean convenience around evaluate_flag.
    #
    # @return [Boolean] false on any failure
    def feature_enabled?(flag_key, user_id, attributes = {})
      evaluate_flag(flag_key, user_id, attributes).enabled?
    end

    # Assign a user to an experiment (sticky on the server, records the exposure).
    #
    # @param experiment_key [String] experiment key
    # @param user_id        [String] user identifier
    # @param attributes     [Hash]   user attributes, sent as +context+ for targeting rules
    # @return [Assignment, nil] nil on a network/HTTP failure (including 404 when no
    #   experiment has the key, or for a draft with no control variant); a cached
    #   assignment is returned when present. An experiment that is not ACTIVE
    #   answers its control variant.
    def get_assignment(experiment_key, user_id, attributes = {})
      cache_key = assignment_cache_key(user_id, experiment_key)
      cached = @cache.get(cache_key)
      return cached unless cached.nil?

      body = { experiment_key: experiment_key, user_id: user_id }
      body[:context] = attributes if attributes.is_a?(Hash) && !attributes.empty?

      data = @http.post('/api/v1/tracking/assign', body)
      unless data.is_a?(Hash) && data['variant_name']
        warn "[Controlvar] get_assignment: malformed response for #{experiment_key}"
        return nil
      end

      assignment = Assignment.new(
        experiment_key: (data['experiment_key'] || experiment_key).to_s,
        variant_id:     data['variant_id'],
        variant_name:   data['variant_name'].to_s,
        is_control:     data['is_control'] == true,
        configuration:  data['configuration']
      )
      @cache.set(cache_key, assignment)
      assignment
    rescue NetworkError, APIError => e
      warn "[Controlvar] get_assignment error: #{e.message}"
      nil
    end

    # Track an event. Never raises.
    #
    # With +experiment_key+ and/or +feature_flag_key+ one
    # POST /api/v1/tracking/track is sent. Without a key the event is fanned
    # out through POST /api/v1/tracking/batch: one entry per cached assignment
    # plus one per cached evaluated flag for this user. If nothing is cached
    # for the user, nothing is sent (and +true+ is returned).
    #
    # @param event_name       [String]  event name (also the default +event_type+)
    # @param user_id          [String]  user identifier
    # @param properties       [Hash]    sent as +metadata+
    # @param experiment_key   [String, nil]
    # @param feature_flag_key [String, nil]
    # @param value            [Numeric, nil] numeric value (e.g. revenue)
    # @param event_type       [String, nil]  defaults to +event_name+
    # @param timestamp        [Time, String, nil] defaults to now (UTC, ISO-8601)
    # @return [Boolean] true if every request succeeded, false on any error
    def track(event_name, user_id, properties: {}, experiment_key: nil, feature_flag_key: nil,
              value: nil, event_type: nil, timestamp: nil)
      base = build_event_body(event_name, user_id, properties, value, event_type, timestamp)

      if experiment_key || feature_flag_key
        body = base.dup
        body[:experiment_key]   = experiment_key   if experiment_key
        body[:feature_flag_key] = feature_flag_key if feature_flag_key
        @http.post('/api/v1/tracking/track', body)
        return true
      end

      events = assignments(user_id).map { |a| base.merge(experiment_key: a.experiment_key) } +
               evaluated_flags(user_id).map { |k| base.merge(feature_flag_key: k) }
      return true if events.empty?

      events.each_slice(BATCH_LIMIT) do |slice|
        @http.post('/api/v1/tracking/batch', { events: slice })
      end
      true
    rescue StandardError => e
      warn "[Controlvar] track error: #{e.message}"
      false
    end

    # Track several events in one go via POST /api/v1/tracking/batch
    # (chunked into requests of at most BATCH_LIMIT events). Never raises.
    #
    # Each event is a Hash (symbol or string keys) with +event_name+ and
    # +user_id+ (required). Optional: +experiment_key+, +feature_flag_key+,
    # +properties+ (sent as +metadata+), +value+, +event_type+, +timestamp+.
    # An event with no key is stored as history and counts in no experiment's
    # results; tag outcome events.
    #
    # @param events [Array<Hash>]
    # @return [BatchResult]
    def track_batch(events)
      bodies = []
      errors = []
      Array(events).each_with_index do |event, index|
        begin
          bodies << normalize_event(event)
        rescue ArgumentError => e
          errors << { 'index' => index, 'error' => e.message }
        end
      end

      success = 0
      failure = errors.size
      bodies.each_slice(BATCH_LIMIT) do |slice|
        begin
          resp = @http.post('/api/v1/tracking/batch', { events: slice })
          resp = {} unless resp.is_a?(Hash)
          success += resp.fetch('success_count', slice.size).to_i
          failure += resp.fetch('failure_count', 0).to_i
          errors.concat(Array(resp['errors']))
        rescue StandardError => e
          warn "[Controlvar] track_batch error: #{e.message}"
          failure += slice.size
          errors << { 'error' => e.message }
        end
      end

      BatchResult.new(
        success_count: success,
        failure_count: failure,
        errors:        errors.empty? ? nil : errors
      )
    end

    # Cached (successful, unexpired) assignments for the user.
    #
    # @return [Array<Assignment>]
    def assignments(user_id)
      @cache.live_values { |key| key.is_a?(Array) && key[0] == :assignment && key[1] == user_id }
    end

    # Keys of flags successfully evaluated (and still cached) for the user.
    #
    # @return [Array<String>]
    def evaluated_flags(user_id)
      @cache.live_values { |key| key.is_a?(Array) && key[0] == :flag && key[1] == user_id }
            .map(&:key)
    end

    # Drop every cached evaluation and assignment.
    def clear_cache
      @cache.clear
    end

    # Close the client and release resources.
    # Clears the in-memory cache. Net::HTTP connections are not persistent.
    def close
      @cache.clear
    end

    private

    def flag_cache_key(user_id, flag_key)
      [:flag, user_id, flag_key]
    end

    def assignment_cache_key(user_id, experiment_key)
      [:assignment, user_id, experiment_key]
    end

    # Percent-encode a path segment / query value (spaces as %20, not +).
    def encode(value)
      URI.encode_www_form_component(value.to_s).gsub('+', '%20')
    end

    def build_event_body(event_name, user_id, properties, value, event_type, timestamp)
      body = {
        event_type: (event_type || event_name).to_s,
        event_name: event_name.to_s,
        user_id:    user_id.to_s,
        timestamp:  format_timestamp(timestamp)
      }
      body[:value]    = value.to_f unless value.nil?
      body[:metadata] = properties if properties.is_a?(Hash) && !properties.empty?
      body
    end

    def format_timestamp(timestamp)
      case timestamp
      when nil  then Time.now.utc.iso8601
      when Time then timestamp.utc.iso8601
      else timestamp.to_s
      end
    end

    # Turn a user-supplied event Hash into a /tracking/batch entry.
    def normalize_event(event)
      raise ArgumentError, 'event must be a Hash' unless event.is_a?(Hash)

      e = {}
      event.each { |k, v| e[k.to_sym] = v }

      event_name = e[:event_name] || e[:event]
      raise ArgumentError, 'event_name is required' if event_name.nil? || event_name.to_s.empty?
      raise ArgumentError, 'user_id is required'    if e[:user_id].nil? || e[:user_id].to_s.empty?

      body = build_event_body(event_name, e[:user_id], e[:properties] || e[:metadata],
                              e[:value], e[:event_type], e[:timestamp])
      body[:experiment_key]   = e[:experiment_key]   if e[:experiment_key]
      body[:feature_flag_key] = e[:feature_flag_key] if e[:feature_flag_key]
      body
    end
  end
end
