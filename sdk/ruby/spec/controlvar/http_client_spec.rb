require 'spec_helper'

RSpec.describe Controlvar::HttpClient do
  subject(:client) do
    described_class.new(
      base_url: 'https://api.example.com',
      api_key:  'test-api-key',
      timeout:  5
    )
  end

  let(:base_url)     { 'https://api.example.com' }
  let(:evaluate_url) { "#{base_url}/api/v1/feature-flags/evaluate/my-flag?user_id=u1" }
  let(:track_url)    { "#{base_url}/api/v1/tracking/track" }

  # -------------------------------------------------------------------------
  # GET requests
  # -------------------------------------------------------------------------
  describe '#get' do
    context 'successful response' do
      it 'returns parsed JSON for a 200 response' do
        stub_request(:get, evaluate_url)
          .to_return(
            status: 200,
            body:   '{"key":"my-flag","enabled":true,"config":null}',
            headers: { 'Content-Type' => 'application/json' }
          )

        result = client.get('/api/v1/feature-flags/evaluate/my-flag?user_id=u1')
        expect(result).to eq({ 'key' => 'my-flag', 'enabled' => true, 'config' => nil })
      end

      it 'sends the API key in the X-API-Key header' do
        stub_request(:get, evaluate_url)
          .with(headers: { 'X-API-Key' => 'test-api-key' })
          .to_return(status: 200, body: '{}')

        expect { client.get('/api/v1/feature-flags/evaluate/my-flag?user_id=u1') }.not_to raise_error
      end

      it 'does not send a Bearer Authorization header' do
        stub_request(:get, evaluate_url).to_return(status: 200, body: '{}')

        client.get('/api/v1/feature-flags/evaluate/my-flag?user_id=u1')

        expect(WebMock).to have_requested(:get, evaluate_url).with { |req| req.headers['Authorization'].nil? }
      end

      it 'sends Accept and Content-Type: application/json' do
        stub_request(:get, evaluate_url)
          .with(headers: { 'Accept' => 'application/json', 'Content-Type' => 'application/json' })
          .to_return(status: 200, body: '{}')

        expect { client.get('/api/v1/feature-flags/evaluate/my-flag?user_id=u1') }.not_to raise_error
      end

      it 'merges extra headers into the request' do
        stub_request(:get, "#{base_url}/api/v1/x")
          .with(headers: { 'X-Custom' => 'yes' })
          .to_return(status: 200, body: '{}')

        expect { client.get('/api/v1/x', headers: { 'X-Custom' => 'yes' }) }.not_to raise_error
      end

      it 'strips a trailing slash from the base URL' do
        slash_client = described_class.new(base_url: "#{base_url}/", api_key: 'k')
        stub_request(:get, "#{base_url}/api/v1/x").to_return(status: 200, body: '{}')

        expect { slash_client.get('/api/v1/x') }.not_to raise_error
      end

      it 'returns an empty Hash for an empty 200 body' do
        stub_request(:get, "#{base_url}/api/v1/empty")
          .to_return(status: 200, body: '', headers: {})

        result = client.get('/api/v1/empty')
        expect(result).to eq({})
      end

      it 'returns an empty Hash for a whitespace-only 200 body' do
        stub_request(:get, "#{base_url}/api/v1/ws")
          .to_return(status: 200, body: '   ', headers: {})

        result = client.get('/api/v1/ws')
        expect(result).to eq({})
      end
    end

    # -----------------------------------------------------------------------
    # Error responses
    # -----------------------------------------------------------------------
    context '401 Unauthorized' do
      it 'raises AuthenticationError' do
        stub_request(:get, evaluate_url)
          .to_return(status: 401, body: '{"detail":"Invalid API key"}')

        expect { client.get('/api/v1/feature-flags/evaluate/my-flag?user_id=u1') }
          .to raise_error(Controlvar::AuthenticationError)
      end

      it 'sets status_code to 401' do
        stub_request(:get, evaluate_url)
          .to_return(status: 401, body: '{"detail":"Unauthorized"}')

        begin
          client.get('/api/v1/feature-flags/evaluate/my-flag?user_id=u1')
        rescue Controlvar::AuthenticationError => e
          expect(e.status_code).to eq(401)
        end
      end
    end

    context '404 Not Found' do
      it 'raises APIError with status_code 404' do
        stub_request(:get, evaluate_url)
          .to_return(status: 404, body: '{"detail":"Not found"}')

        expect { client.get('/api/v1/feature-flags/evaluate/my-flag?user_id=u1') }
          .to raise_error(Controlvar::APIError) { |e|
            expect(e.status_code).to eq(404)
            expect(e.message).to eq('Not found')
          }
      end
    end

    context '500 Internal Server Error' do
      it 'raises APIError' do
        stub_request(:get, evaluate_url)
          .to_return(status: 500, body: '{"detail":"Server error"}')

        expect { client.get('/api/v1/feature-flags/evaluate/my-flag?user_id=u1') }
          .to raise_error(Controlvar::APIError) { |e|
            expect(e.status_code).to eq(500)
          }
      end
    end

    context 'timeout' do
      it 'raises NetworkError on read timeout' do
        stub_request(:get, evaluate_url).to_timeout

        expect { client.get('/api/v1/feature-flags/evaluate/my-flag?user_id=u1') }
          .to raise_error(Controlvar::NetworkError)
      end
    end

    context 'connection refused' do
      it 'raises NetworkError when connection is refused' do
        stub_request(:get, evaluate_url)
          .to_raise(Errno::ECONNREFUSED.new("Connection refused"))

        expect { client.get('/api/v1/feature-flags/evaluate/my-flag?user_id=u1') }
          .to raise_error(Controlvar::NetworkError)
      end
    end

    context 'socket error' do
      it 'raises NetworkError on SocketError' do
        stub_request(:get, evaluate_url)
          .to_raise(SocketError.new("getaddrinfo: Name or service not known"))

        expect { client.get('/api/v1/feature-flags/evaluate/my-flag?user_id=u1') }
          .to raise_error(Controlvar::NetworkError)
      end
    end
  end

  # -------------------------------------------------------------------------
  # POST requests
  # -------------------------------------------------------------------------
  describe '#post' do
    it 'sends a POST with a JSON body' do
      stub_request(:post, track_url)
        .with(body: hash_including('event_type' => 'page_view'))
        .to_return(status: 200, body: '{"id":"evt-1"}')

      result = client.post('/api/v1/tracking/track', { 'event_type' => 'page_view', 'user_id' => 'u1' })
      expect(result).to eq({ 'id' => 'evt-1' })
    end

    it 'sets Content-Type: application/json and X-API-Key on POST' do
      stub_request(:post, track_url)
        .with(headers: { 'Content-Type' => 'application/json', 'X-API-Key' => 'test-api-key' })
        .to_return(status: 200, body: '{}')

      expect { client.post('/api/v1/tracking/track', {}) }.not_to raise_error
    end

    it 'raises AuthenticationError on 401 POST' do
      stub_request(:post, track_url)
        .to_return(status: 401, body: '{"detail":"Unauthorized"}')

      expect { client.post('/api/v1/tracking/track', {}) }
        .to raise_error(Controlvar::AuthenticationError)
    end

    it 'raises APIError with status_code 422 on a validation error' do
      stub_request(:post, track_url)
        .to_return(status: 422, body: '{"detail":"Either experiment_key or feature_flag_key must be provided"}')

      expect { client.post('/api/v1/tracking/track', {}) }
        .to raise_error(Controlvar::APIError) { |e| expect(e.status_code).to eq(422) }
    end

    it 'raises NetworkError on POST timeout' do
      stub_request(:post, track_url).to_timeout

      expect { client.post('/api/v1/tracking/track', {}) }
        .to raise_error(Controlvar::NetworkError)
    end

    it 'raises NetworkError on connection reset' do
      stub_request(:post, track_url)
        .to_raise(Errno::ECONNRESET.new("Connection reset by peer"))

      expect { client.post('/api/v1/tracking/track', {}) }
        .to raise_error(Controlvar::NetworkError)
    end
  end
end
