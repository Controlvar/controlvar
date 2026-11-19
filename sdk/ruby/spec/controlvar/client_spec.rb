require 'spec_helper'

module ControlvarSpec
  # An attribute value that must never reach a log line or an error message.
  CANARY = 'attrcanary7f3'.freeze

  # A query string as the server reads it (Starlette's parse_qsl): split on
  # '&', then on the first '=', read '+' as a space and percent-decode as
  # UTF-8 (URI.decode_www_form_component does those last two). Gives the
  # [name, value] pairs in order.
  def self.server_query(query)
    query.split('&').map do |pair|
      pair.split('=', 2).values_at(0, 1).map do |part|
        decoded = URI.decode_www_form_component(part.to_s)
        raise ArgumentError, "not UTF-8 once decoded: #{part}" unless decoded.valid_encoding?

        decoded
      end
    end
  end

  # Stands in for Controlvar::HttpClient, so a spec reads the exact path the
  # client hands to #get rather than a WebMock match (WebMock normalises a URI
  # before comparing it). It answers a flag evaluation as the server does for
  # a flag whose one rule is "plan equals pro", rollout 0: enabled, reason
  # targeting_rule, only when the context it decodes has plan "pro".
  class FakeFlagServer
    attr_reader :paths, :posts

    def initialize
      @paths = []
      @posts = []
    end

    def get(path, headers: {})
      @paths << path
      context = ControlvarSpec.server_query(path.partition('?')[2]).to_h['context']
      if context && JSON.parse(context)['plan'] == 'pro'
        { 'key' => 'f', 'enabled' => true, 'config' => nil, 'reason' => 'targeting_rule' }
      else
        { 'key' => 'f', 'enabled' => false, 'config' => nil, 'reason' => 'rollout' }
      end
    end

    def post(path, body, headers: {})
      @posts << [path, body]
      { 'success_count' => Array(body[:events]).size, 'failure_count' => 0 }
    end
  end
end

RSpec.describe Controlvar::Client do
  let(:base_url) { 'https://api.example.com' }
  let(:api_key)  { 'sk-test-key' }

  subject(:client) do
    described_class.new(
      base_url: base_url,
      api_key:  api_key,
      cache_ttl: 60,
      timeout:   5
    )
  end

  let(:assign_url)   { "#{base_url}/api/v1/tracking/assign" }
  let(:track_url)    { "#{base_url}/api/v1/tracking/track" }
  let(:batch_url)    { "#{base_url}/api/v1/tracking/batch" }
  let(:evaluate_url) { "#{base_url}/api/v1/feature-flags/evaluate/dark-mode?user_id=user-123" }

  let(:json_headers) { { 'Content-Type' => 'application/json' } }

  let(:assign_response) do
    {
      'experiment_key' => 'checkout-flow',
      'user_id'        => 'user-123',
      'variant_id'     => 'var-uuid-1',
      'variant_name'   => 'treatment',
      'is_control'     => false,
      'configuration'  => { 'color' => 'green' }
    }.to_json
  end

  let(:evaluate_response) do
    { 'key' => 'dark-mode', 'enabled' => true, 'config' => { 'theme' => 'dark' } }.to_json
  end

  let(:batch_response) { '{"success_count":2,"failure_count":0,"errors":null}' }

  def stub_assign(body: assign_response, status: 200)
    stub_request(:post, assign_url).to_return(status: status, body: body, headers: json_headers)
  end

  def stub_evaluate(body: evaluate_response, status: 200, url: evaluate_url)
    stub_request(:get, url).to_return(status: status, body: body, headers: json_headers)
  end

  def request_json(request)
    JSON.parse(request.body)
  end

  # The client logs failures with Kernel#warn; keep the spec output clean.
  before { allow_any_instance_of(described_class).to receive(:warn) }

  # -------------------------------------------------------------------------
  # Construction
  # -------------------------------------------------------------------------
  describe '#initialize' do
    it 'accepts a SdkConfig object' do
      config = Controlvar::SdkConfig.new(base_url: base_url, api_key: api_key)
      expect { described_class.new(config) }.not_to raise_error
    end

    it 'accepts keyword arguments' do
      expect { described_class.new(base_url: base_url, api_key: api_key) }.not_to raise_error
    end

    it 'raises when base_url is missing' do
      expect { described_class.new(api_key: api_key) }.to raise_error(ArgumentError, /base_url/)
    end

    it 'raises when api_key is missing' do
      expect { described_class.new(base_url: base_url) }.to raise_error(ArgumentError, /api_key/)
    end
  end

  # -------------------------------------------------------------------------
  # get_assignment — POST /api/v1/tracking/assign
  # -------------------------------------------------------------------------
  describe '#get_assignment' do
    it 'POSTs to /api/v1/tracking/assign with the API headers and a JSON body' do
      stub_assign
      client.get_assignment('checkout-flow', 'user-123', { country: 'US' })

      expect(WebMock).to have_requested(:post, assign_url)
        .with(
          headers: {
            'X-API-Key'    => api_key,
            'Content-Type' => 'application/json',
            'Accept'       => 'application/json'
          },
          body: {
            'experiment_key' => 'checkout-flow',
            'user_id'        => 'user-123',
            'context'        => { 'country' => 'US' }
          }
        )
    end

    it 'omits context when no attributes are given' do
      stub_assign
      client.get_assignment('checkout-flow', 'user-123')

      expect(WebMock).to have_requested(:post, assign_url)
        .with(body: { 'experiment_key' => 'checkout-flow', 'user_id' => 'user-123' })
    end

    it 'maps the response to an Assignment' do
      stub_assign
      assignment = client.get_assignment('checkout-flow', 'user-123')

      expect(assignment).to be_a(Controlvar::Assignment)
      expect(assignment.experiment_key).to eq('checkout-flow')
      expect(assignment.variant_id).to eq('var-uuid-1')
      expect(assignment.variant_name).to eq('treatment')
      expect(assignment.is_control).to be false
      expect(assignment.control?).to be false
      expect(assignment.configuration).to eq('color' => 'green')
    end

    it 'maps is_control: true to control?' do
      stub_assign(body: { 'variant_id' => 'v0', 'variant_name' => 'control', 'is_control' => true }.to_json)
      assignment = client.get_assignment('checkout-flow', 'user-123')

      expect(assignment.control?).to be true
      expect(assignment.configuration).to be_nil
    end

    it 'is sticky: a second call for the same user + experiment is served from the cache' do
      stub_assign
      first  = client.get_assignment('checkout-flow', 'user-123')
      second = client.get_assignment('checkout-flow', 'user-123')

      expect(second).to eq(first)
      expect(WebMock).to have_requested(:post, assign_url).once
    end

    it 'caches per user: a different user triggers a new request' do
      stub_assign
      client.get_assignment('checkout-flow', 'user-123')
      client.get_assignment('checkout-flow', 'user-456')

      expect(WebMock).to have_requested(:post, assign_url).twice
    end

    it 'returns nil on 404 (no experiment has the key) and never caches the failure' do
      stub_assign(status: 404, body: %q({"detail":"No experiment has the key 'checkout-flow'."}))

      expect(client.get_assignment('checkout-flow', 'user-123')).to be_nil
      expect(client.get_assignment('checkout-flow', 'user-123')).to be_nil
      expect(WebMock).to have_requested(:post, assign_url).twice
    end

    it 'returns nil on 401' do
      stub_assign(status: 401, body: '{"detail":"Invalid API key"}')
      expect(client.get_assignment('checkout-flow', 'user-123')).to be_nil
    end

    it 'returns nil on a network error' do
      stub_request(:post, assign_url).to_raise(Errno::ECONNREFUSED)
      expect(client.get_assignment('checkout-flow', 'user-123')).to be_nil
    end

    it 'returns nil when the response has no variant_name' do
      stub_assign(body: '{}')
      expect(client.get_assignment('checkout-flow', 'user-123')).to be_nil
    end
  end

  # -------------------------------------------------------------------------
  # evaluate_flag — GET /api/v1/feature-flags/evaluate/{key}?user_id=…
  # -------------------------------------------------------------------------
  describe '#evaluate_flag' do
    it 'GETs /api/v1/feature-flags/evaluate/{key}?user_id=… with the API headers' do
      stub_evaluate
      client.evaluate_flag('dark-mode', 'user-123')

      expect(WebMock).to have_requested(:get, evaluate_url)
        .with(headers: { 'X-API-Key' => api_key, 'Accept' => 'application/json' })
    end

    it 'percent-encodes the flag key and the user id' do
      url = "#{base_url}/api/v1/feature-flags/evaluate/my%20flag?user_id=user%20one"
      stub_evaluate(url: url, body: { 'key' => 'my flag', 'enabled' => true }.to_json)

      result = client.evaluate_flag('my flag', 'user one')

      expect(result.enabled).to be true
      expect(WebMock).to have_requested(:get, url).once
    end

    it 'maps the response to a FlagEvaluation' do
      stub_evaluate
      result = client.evaluate_flag('dark-mode', 'user-123')

      expect(result).to be_a(Controlvar::FlagEvaluation)
      expect(result.key).to eq('dark-mode')
      expect(result.enabled).to be true
      expect(result.enabled?).to be true
      expect(result.config).to eq('theme' => 'dark')
    end

    it 'reports enabled: false with a nil config for a disabled flag' do
      stub_evaluate(body: { 'key' => 'dark-mode', 'enabled' => false, 'config' => nil }.to_json)
      result = client.evaluate_flag('dark-mode', 'user-123')

      expect(result.enabled).to be false
      expect(result.config).to be_nil
    end

    it 'treats a non-boolean enabled value as disabled' do
      stub_evaluate(body: { 'key' => 'dark-mode', 'enabled' => 'yes' }.to_json)
      expect(client.evaluate_flag('dark-mode', 'user-123').enabled).to be false
    end

    it 'caches per user + key: repeated calls make one request' do
      stub_evaluate
      3.times { client.evaluate_flag('dark-mode', 'user-123') }

      expect(WebMock).to have_requested(:get, evaluate_url).once
    end

    it 'requests again for a different user' do
      stub_evaluate
      other = "#{base_url}/api/v1/feature-flags/evaluate/dark-mode?user_id=user-456"
      stub_evaluate(url: other)

      client.evaluate_flag('dark-mode', 'user-123')
      client.evaluate_flag('dark-mode', 'user-456')

      expect(WebMock).to have_requested(:get, evaluate_url).once
      expect(WebMock).to have_requested(:get, other).once
    end

    it 'expires the cached evaluation after cache_ttl seconds' do
      stub_evaluate
      client.evaluate_flag('dark-mode', 'user-123')

      allow(Time).to receive(:now).and_return(Time.at(Time.now.to_i + 61))
      client.evaluate_flag('dark-mode', 'user-123')

      expect(WebMock).to have_requested(:get, evaluate_url).twice
    end

    it 'reports the flag as disabled on 404 (unknown flag key) and never caches the failure' do
      stub_evaluate(status: 404, body: '{"detail":"Feature flag not found"}')

      result = client.evaluate_flag('dark-mode', 'user-123')
      expect(result.enabled).to be false
      expect(result.key).to eq('dark-mode')
      expect(result.config).to be_nil

      client.evaluate_flag('dark-mode', 'user-123')
      expect(WebMock).to have_requested(:get, evaluate_url).twice
    end

    it 'reports the flag as disabled on a network error' do
      stub_request(:get, evaluate_url).to_raise(Errno::ECONNREFUSED)
      expect(client.evaluate_flag('dark-mode', 'user-123').enabled).to be false
    end

    it 'reports the flag as disabled on a timeout' do
      stub_request(:get, evaluate_url).to_timeout
      expect(client.evaluate_flag('dark-mode', 'user-123').enabled).to be false
    end
  end

  describe '#feature_enabled?' do
    it 'returns true when the server enables the flag' do
      stub_evaluate
      expect(client.feature_enabled?('dark-mode', 'user-123')).to be true
    end

    it 'returns false on failure' do
      stub_evaluate(status: 500, body: '{"detail":"boom"}')
      expect(client.feature_enabled?('dark-mode', 'user-123')).to be false
    end
  end

  # -------------------------------------------------------------------------
  # evaluate_flag with attributes: the context (#1720)
  #
  # These read the path the client hands to HttpClient#get (FakeFlagServer),
  # not a WebMock match: WebMock normalises a URI before it compares, so a
  # request whose parameters came in another order would still match.
  # -------------------------------------------------------------------------
  describe '#evaluate_flag with attributes (#1720)' do
    let(:server) { ControlvarSpec::FakeFlagServer.new }
    let(:flag_client) do
      allow(Controlvar::HttpClient).to receive(:new).and_return(server)
      described_class.new(base_url: base_url, api_key: api_key, cache_ttl: 60)
    end
    let(:pro)  { { plan: 'pro' } }
    let(:free) { { plan: 'free' } }

    # The parameters of the n-th request as the server reads them.
    def sent(index)
      ControlvarSpec.server_query(server.paths.fetch(index).partition('?')[2])
    end

    def evaluations(*attribute_sets)
      attribute_sets.map { |attributes| flag_client.evaluate_flag('f', 'u1', attributes).enabled? }
    end

    # Matches captured output that does not contain +text+.
    def naming_no(text)
      satisfy("names no #{text}") { |output| !output.include?(text) }
    end

    it 'sends the attributes as context=<compact JSON>, percent-encoded, after user_id', :regression do
      flag_client.evaluate_flag('f', 'u1', pro)

      expect(server.paths).to eq(['/api/v1/feature-flags/evaluate/f?user_id=u1&context=%7B%22plan%22%3A%22pro%22%7D'])
    end

    it 'matches a targeting rule on an attribute: the server reads plan=pro from the context', :regression do
      expect(flag_client.evaluate_flag('f', 'u1', pro).enabled?).to be true
      expect(flag_client.evaluate_flag('f', 'u2', free).enabled?).to be false
    end

    it 'sends + & = # a space, / and non-ASCII so that the server decodes the same object' do
      vectors = [
        { 'q' => "a+b&c=d#e f/é😀!'()*~" },
        { 'q' => 'a+b&c=d#e fé' },
        { 'age' => 30, 'beta' => true, 'device' => { 'os' => 'ios', 'version' => '17.4.0' }, 'tags' => %w[a b] }
      ]
      vectors.each_with_index do |attributes, index|
        flag_client.evaluate_flag('f', "u#{index}", attributes)
        names = sent(index).map(&:first)
        values = sent(index).to_h

        expect(names).to eq(%w[user_id context])
        expect(values['user_id']).to eq("u#{index}")
        expect(JSON.parse(values['context'])).to eq(attributes)
      end
    end

    it "pins #1720's own vector as an exact URL" do
      flag_client.evaluate_flag('f', 'u1', { 'q' => 'a+b&c=d#e fé' })

      # Measured on Ruby 3.2.11 at main 0e7e4ce2 with this change; it is also
      # what the JavaScript SDK sends. Correctness is the decode test above.
      expect(server.paths).to eq(
        ['/api/v1/feature-flags/evaluate/f?user_id=u1&context=%7B%22q%22%3A%22a%2Bb%26c%3Dd%23e%20f%C3%A9%22%7D']
      )
    end

    it 'sends no context for nil, an empty Hash or a non-Hash: the URL is the one sent before attributes were' do
      flag_client.evaluate_flag('f', 'u1')
      ['u2', 'u3', 'u4'].zip([nil, {}, ['plan', 'pro']]).each do |user, attributes|
        flag_client.evaluate_flag('f', user, attributes)
      end

      expect(server.paths).to eq(%w[u1 u2 u3 u4].map { |user| "/api/v1/feature-flags/evaluate/f?user_id=#{user}" })
    end

    it 'feature_enabled? sends the attributes too' do
      expect(flag_client.feature_enabled?('f', 'u1', pro)).to be true
      expect(sent(0).to_h['context']).to eq('{"plan":"pro"}')
    end

    it 'answers two calls that differ only in attributes with two requests and two answers' do
      expect(evaluations(pro, free)).to eq([true, false])
      expect(server.paths.size).to eq(2)
    end

    it 'treats {plan: "pro"} and {tier: "pro"}, and {n: 1} and {n: "1"}, as different attributes' do
      evaluations({ plan: 'pro' }, { tier: 'pro' })
      expect(server.paths.size).to eq(2)

      flag_client.clear_cache
      evaluations({ n: 1 }, { n: '1' })
      expect(server.paths.size).to eq(4)
    end

    it 'keeps one cached answer per user and flag: A, B, A makes three requests' do
      expect(evaluations(pro, free, pro)).to eq([true, false, true])
      expect(server.paths.size).to eq(3)
    end

    it 'does not serve an answer cached with attributes to a call without them' do
      expect(evaluations(pro, nil)).to eq([true, false])
      expect(server.paths.size).to eq(2)
    end

    it 'treats no attributes and an empty Hash as the same call: one request' do
      evaluations({}, {})
      expect(server.paths.size).to eq(1)

      flag_client.clear_cache
      flag_client.evaluate_flag('f', 'u1')
      flag_client.evaluate_flag('f', 'u1', {})
      expect(server.paths.size).to eq(2)
    end

    it 'serves the same attributes from the cache: one request' do
      expect(evaluations(pro, pro, { 'plan' => 'pro' })).to eq([true, true, true])
      expect(server.paths.size).to eq(1)
    end

    it 'fans a keyless track out to a flag once, however many attributes it was evaluated with' do
      evaluations(pro, free)

      expect(flag_client.evaluated_flags('u1')).to eq(['f'])
      expect(flag_client.track('page_view', 'u1')).to be true
      expect(server.posts.map(&:first)).to eq(['/api/v1/tracking/batch'])
      expect(server.posts.first.last[:events].map { |event| event[:feature_flag_key] }).to eq(['f'])
      # The one cached answer is the latest one, B's.
      expect(evaluations(free)).to eq([false])
      expect(server.paths.size).to eq(2)
    end

    it 'names no attribute in its warning when the request fails, even for a base_url with a space' do
      allow_any_instance_of(described_class).to receive(:warn).and_call_original
      spaced = described_class.new(base_url: 'http://local host:8000', api_key: api_key)
      result = nil

      expect { result = spaced.evaluate_flag('f', 'u1', { plan: ControlvarSpec::CANARY }) }
        .to output(
          a_string_including('[Controlvar] evaluate_flag error', 'local host:8000/api/v1/feature-flags/evaluate/f')
            .and(naming_no(ControlvarSpec::CANARY))
        ).to_stderr_from_any_process
      expect(result.enabled?).to be false
    end

    it 'sends nothing and reports the flag as disabled when the attributes cannot be written as JSON' do
      allow_any_instance_of(described_class).to receive(:warn).and_call_original

      [{ ControlvarSpec::CANARY => Float::NAN }, { 'k' => "#{ControlvarSpec::CANARY}\xFF" }].each do |attributes|
        result = nil
        expect { result = flag_client.evaluate_flag('f', 'u1', attributes) }
          .to output(
            a_string_including('[Controlvar] evaluate_flag: the attributes cannot be sent as JSON; ' \
                               'nothing was sent and the flag is reported as disabled')
              .and(naming_no(ControlvarSpec::CANARY))
          ).to_stderr_from_any_process
        expect(result.enabled?).to be false
      end
      expect(server.paths).to eq([])
      expect(flag_client.evaluated_flags('u1')).to eq([])
    end
  end

  # -------------------------------------------------------------------------
  # track — POST /api/v1/tracking/track or fan-out via /api/v1/tracking/batch
  # -------------------------------------------------------------------------
  describe '#track' do
    context 'with an experiment or flag key' do
      before { stub_request(:post, track_url).to_return(status: 200, body: '{"id":"evt-1"}') }

      it 'POSTs one /api/v1/tracking/track with the event body' do
        result = client.track('purchase', 'user-123',
                              properties: { sku: 'pro' },
                              experiment_key: 'checkout-flow',
                              value: 12.5)

        expect(result).to be true
        expect(WebMock).to have_requested(:post, track_url)
          .with(headers: { 'X-API-Key' => api_key, 'Content-Type' => 'application/json' })
          .with { |req|
            body = request_json(req)
            body['event_type'] == 'purchase' &&
              body['event_name'] == 'purchase' &&
              body['user_id'] == 'user-123' &&
              body['experiment_key'] == 'checkout-flow' &&
              body['value'] == 12.5 &&
              body['metadata'] == { 'sku' => 'pro' } &&
              body['timestamp'].is_a?(String) &&
              !body.key?('feature_flag_key')
          }
        expect(WebMock).not_to have_requested(:post, batch_url)
      end

      it 'sends feature_flag_key, a custom event_type and a given timestamp' do
        at = Time.utc(2026, 9, 11, 10, 0, 0)
        client.track('search', 'user-123', feature_flag_key: 'new-search', event_type: 'interaction', timestamp: at)

        expect(WebMock).to have_requested(:post, track_url).with { |req|
          body = request_json(req)
          body['event_type'] == 'interaction' &&
            body['event_name'] == 'search' &&
            body['feature_flag_key'] == 'new-search' &&
            body['timestamp'] == '2026-09-11T10:00:00Z' &&
            !body.key?('value') && !body.key?('metadata')
        }
      end

      it 'returns false when the API returns an error' do
        stub_request(:post, track_url).to_return(status: 500, body: '{"detail":"Internal error"}')
        expect(client.track('purchase', 'user-123', experiment_key: 'checkout-flow')).to be false
      end

      it 'returns false on a network error' do
        stub_request(:post, track_url).to_raise(Errno::ECONNREFUSED)
        expect(client.track('purchase', 'user-123', experiment_key: 'checkout-flow')).to be false
      end

      it 'never raises' do
        stub_request(:post, track_url).to_raise(RuntimeError.new('unexpected'))
        expect { client.track('purchase', 'user-123', experiment_key: 'checkout-flow') }.not_to raise_error
      end
    end

    context 'without a key' do
      it 'sends nothing when nothing is cached for the user' do
        expect(client.track('page_view', 'user-123')).to be true

        expect(WebMock).not_to have_requested(:post, batch_url)
        expect(WebMock).not_to have_requested(:post, track_url)
      end

      it 'fans out one /api/v1/tracking/batch with an entry per cached assignment and evaluated flag' do
        stub_assign
        stub_evaluate
        stub_request(:post, batch_url).to_return(status: 200, body: batch_response)

        client.get_assignment('checkout-flow', 'user-123')
        client.evaluate_flag('dark-mode', 'user-123')

        expect(client.track('page_view', 'user-123', properties: { page: '/' })).to be true

        expect(WebMock).to have_requested(:post, batch_url).once.with { |req|
          events = request_json(req)['events']
          events.size == 2 &&
            events[0]['experiment_key'] == 'checkout-flow' &&
            !events[0].key?('feature_flag_key') &&
            events[1]['feature_flag_key'] == 'dark-mode' &&
            !events[1].key?('experiment_key') &&
            events.all? { |e|
              e['event_type'] == 'page_view' && e['event_name'] == 'page_view' &&
                e['user_id'] == 'user-123' && e['metadata'] == { 'page' => '/' }
            }
        }
        expect(WebMock).not_to have_requested(:post, track_url)
      end

      it 'only fans out to entries cached for that user' do
        stub_assign
        client.get_assignment('checkout-flow', 'user-123')

        expect(client.track('page_view', 'user-456')).to be true
        expect(WebMock).not_to have_requested(:post, batch_url)
      end

      it 'does not fan out to failed evaluations' do
        stub_assign
        stub_evaluate(status: 404, body: '{"detail":"not found"}')
        stub_request(:post, batch_url).to_return(status: 200, body: batch_response)

        client.get_assignment('checkout-flow', 'user-123')
        client.evaluate_flag('dark-mode', 'user-123')
        client.track('page_view', 'user-123')

        expect(WebMock).to have_requested(:post, batch_url).with { |req|
          events = request_json(req)['events']
          events.size == 1 && events[0]['experiment_key'] == 'checkout-flow'
        }
      end

      it 'returns false when the batch request fails' do
        stub_assign
        stub_request(:post, batch_url).to_return(status: 500, body: '{"detail":"boom"}')
        client.get_assignment('checkout-flow', 'user-123')

        expect(client.track('page_view', 'user-123')).to be false
      end

      it 'never raises' do
        stub_assign
        stub_request(:post, batch_url).to_raise(RuntimeError.new('unexpected'))
        client.get_assignment('checkout-flow', 'user-123')

        expect { client.track('page_view', 'user-123') }.not_to raise_error
      end
    end
  end

  # -------------------------------------------------------------------------
  # track_batch — POST /api/v1/tracking/batch
  # -------------------------------------------------------------------------
  describe '#track_batch' do
    it 'POSTs {events: [...]} and maps the response to a BatchResult' do
      stub_request(:post, batch_url).to_return(status: 200, body: batch_response)

      result = client.track_batch([
        { event_name: 'purchase', user_id: 'user-123', experiment_key: 'checkout-flow', value: 12.5 },
        { 'event_name' => 'search', 'user_id' => 'user-123', 'feature_flag_key' => 'new-search',
          'properties' => { 'q' => 'shoes' } }
      ])

      expect(result).to be_a(Controlvar::BatchResult)
      expect(result.success_count).to eq(2)
      expect(result.failure_count).to eq(0)
      expect(result.errors).to be_nil
      expect(result.ok?).to be true

      expect(WebMock).to have_requested(:post, batch_url).once.with { |req|
        events = request_json(req)['events']
        events.size == 2 &&
          events[0]['experiment_key'] == 'checkout-flow' && events[0]['value'] == 12.5 &&
          events[0]['event_type'] == 'purchase' &&
          events[1]['feature_flag_key'] == 'new-search' && events[1]['metadata'] == { 'q' => 'shoes' }
      }
    end

    it 'chunks into requests of at most 100 events and aggregates the counts' do
      stub_request(:post, batch_url).to_return(
        { status: 200, body: '{"success_count":100,"failure_count":0,"errors":null}' },
        { status: 200, body: '{"success_count":49,"failure_count":1,"errors":[{"index":3,"error":"bad"}]}' }
      )
      events = (1..150).map { |i| { event_name: 'e', user_id: "u#{i}", experiment_key: 'x' } }

      result = client.track_batch(events)

      expect(WebMock).to have_requested(:post, batch_url).twice
      expect(result.success_count).to eq(149)
      expect(result.failure_count).to eq(1)
      expect(result.errors).to eq([{ 'index' => 3, 'error' => 'bad' }])
    end

    it 'counts every event as failed on a network error and never raises' do
      stub_request(:post, batch_url).to_raise(Errno::ECONNREFUSED)

      result = nil
      expect {
        result = client.track_batch([{ event_name: 'e', user_id: 'u', experiment_key: 'x' }])
      }.not_to raise_error
      expect(result.failure_count).to eq(1)
      expect(result.ok?).to be false
      expect(result.errors.first).to include('error')
    end

    it 'reports malformed events as failures without sending them' do
      stub_request(:post, batch_url).to_return(status: 200, body: '{"success_count":1,"failure_count":0}')

      result = client.track_batch([{ user_id: 'u' }, { event_name: 'e', user_id: 'u', experiment_key: 'x' }])

      expect(result.success_count).to eq(1)
      expect(result.failure_count).to eq(1)
      expect(result.errors.first['index']).to eq(0)
      expect(WebMock).to have_requested(:post, batch_url).with { |req| request_json(req)['events'].size == 1 }
    end

    it 'sends nothing for an empty list' do
      result = client.track_batch([])
      expect(result.ok?).to be true
      expect(WebMock).not_to have_requested(:post, batch_url)
    end
  end

  # -------------------------------------------------------------------------
  # Cache helpers
  # -------------------------------------------------------------------------
  describe 'cache helpers' do
    it '#assignments lists cached assignments for the user' do
      stub_assign
      client.get_assignment('checkout-flow', 'user-123')

      expect(client.assignments('user-123').map(&:experiment_key)).to eq(['checkout-flow'])
      expect(client.assignments('user-456')).to eq([])
    end

    it '#evaluated_flags lists cached flag keys for the user' do
      stub_evaluate
      client.evaluate_flag('dark-mode', 'user-123')

      expect(client.evaluated_flags('user-123')).to eq(['dark-mode'])
      expect(client.evaluated_flags('user-456')).to eq([])
    end

    it '#clear_cache drops cached results so the next call requests again' do
      stub_evaluate
      client.evaluate_flag('dark-mode', 'user-123')
      client.clear_cache
      client.evaluate_flag('dark-mode', 'user-123')

      expect(WebMock).to have_requested(:get, evaluate_url).twice
    end
  end

  # -------------------------------------------------------------------------
  # close
  # -------------------------------------------------------------------------
  describe '#close' do
    it 'clears the cache' do
      stub_evaluate
      client.evaluate_flag('dark-mode', 'user-123')
      client.close
      client.evaluate_flag('dark-mode', 'user-123')

      expect(WebMock).to have_requested(:get, evaluate_url).twice
    end

    it 'does not raise when called multiple times' do
      expect { client.close; client.close }.not_to raise_error
    end
  end
end
