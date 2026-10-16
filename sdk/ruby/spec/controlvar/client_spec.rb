require 'spec_helper'

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

    it 'returns nil on 404 (experiment not ACTIVE) and never caches the failure' do
      stub_assign(status: 404, body: '{"detail":"Experiment not found"}')

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

    it 'reports the flag as disabled on 404 (flag not ACTIVE) and never caches the failure' do
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
