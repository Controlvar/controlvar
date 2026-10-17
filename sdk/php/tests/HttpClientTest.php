<?php

declare(strict_types=1);

namespace Controlvar\Tests;

use Controlvar\HttpClient;
use Controlvar\SdkConfig;
use Controlvar\Errors\ApiException;
use Controlvar\Errors\AuthException;
use Controlvar\Errors\NetworkException;
use PHPUnit\Framework\TestCase;

/**
 * Unit tests for HttpClient.
 *
 * We test via a testable subclass that overrides executeCurl() so no real HTTP
 * calls are made — making the suite fast and hermetic.
 */
class HttpClientTest extends TestCase
{
    // -------------------------------------------------------------------------
    // Helpers — fake cURL responses
    // -------------------------------------------------------------------------

    private function makeConfig(string $baseUrl = 'https://api.example.com', string $apiKey = 'test-key'): SdkConfig
    {
        return new SdkConfig(baseUrl: $baseUrl, apiKey: $apiKey, timeout: 5);
    }

    /**
     * Build a stub HttpClient that returns a preconfigured cURL response.
     *
     * @param string $body      Raw JSON body the server would return
     * @param int    $httpCode  HTTP status code
     * @param string $curlError Non-empty string simulates a cURL transport error
     */
    private function makeStubClient(
        SdkConfig $config,
        string $body,
        int $httpCode,
        string $curlError = ''
    ): HttpClient {
        return new class($config, $body, $httpCode, $curlError) extends HttpClient {
            public function __construct(
                SdkConfig $config,
                private readonly string $stubBody,
                private readonly int    $stubCode,
                private readonly string $stubError,
            ) {
                parent::__construct($config);
            }

            protected function executeCurl(array $options): array
            {
                return [$this->stubBody, $this->stubCode, $this->stubError];
            }
        };
    }

    /**
     * Build a stub that captures the options passed to executeCurl.
     */
    private function makeCapturingStub(SdkConfig $config, array &$captured): HttpClient
    {
        return new class($config, $captured) extends HttpClient {
            /**
             * Bound by reference to the caller's array in the constructor (a `&` is not
             * allowed in a property declaration, only in the assignment).
             *
             * @var array<int,mixed>
             */
            private array $capturedOptions;

            public function __construct(SdkConfig $config, array &$captured)
            {
                parent::__construct($config);
                $this->capturedOptions = &$captured;
            }

            protected function executeCurl(array $options): array
            {
                $this->capturedOptions = $options;
                return [json_encode(['ok' => true]), 200, ''];
            }
        };
    }

    // -------------------------------------------------------------------------
    // GET — happy path
    // -------------------------------------------------------------------------

    public function testGetParsesJsonResponse(): void
    {
        $config  = $this->makeConfig();
        $payload = ['enabled' => true, 'key' => 'dark-mode'];
        $client  = $this->makeStubClient($config, json_encode($payload), 200);

        $result = $client->get('/api/v1/feature-flags/evaluate/dark-mode?user_id=u1');

        $this->assertSame($payload, $result);
    }

    public function testGetReturnsEmptyArrayForEmptyJsonObject(): void
    {
        $config = $this->makeConfig();
        $client = $this->makeStubClient($config, '{}', 200);

        $result = $client->get('/some/path');
        $this->assertIsArray($result);
    }

    // -------------------------------------------------------------------------
    // POST — happy path
    // -------------------------------------------------------------------------

    public function testPostSendsJsonBody(): void
    {
        $config   = $this->makeConfig();
        $captured = [];
        $client   = $this->makeCapturingStub($config, $captured);

        $body = ['event_type' => 'purchase', 'event_name' => 'purchase', 'user_id' => 'u1', 'experiment_key' => 'exp'];
        $client->post('/api/v1/tracking/track', $body);

        // CURLOPT_POSTFIELDS should be the JSON-encoded body
        $this->assertArrayHasKey(CURLOPT_POSTFIELDS, $captured, 'POST body should be set');
        $this->assertSame(json_encode($body), $captured[CURLOPT_POSTFIELDS]);
        $this->assertSame(true, $captured[CURLOPT_POST]);
    }

    public function testPostParsesJsonResponse(): void
    {
        $config   = $this->makeConfig();
        $response = ['success_count' => 1, 'failure_count' => 0, 'errors' => null];
        $client   = $this->makeStubClient($config, json_encode($response), 200);

        $result = $client->post('/api/v1/tracking/track', ['event_type' => 'test', 'user_id' => 'u1', 'experiment_key' => 'exp']);

        $this->assertSame($response, $result);
    }

    // -------------------------------------------------------------------------
    // Error handling — network
    // -------------------------------------------------------------------------

    public function testGetThrowsNetworkExceptionOnCurlError(): void
    {
        $config = $this->makeConfig();
        $client = $this->makeStubClient($config, '', 0, 'Could not resolve host');

        $this->expectException(NetworkException::class);
        $this->expectExceptionMessageMatches('/Could not resolve host/');

        $client->get('/api/v1/feature-flags/evaluate/dark-mode?user_id=u1');
    }

    public function testPostThrowsNetworkExceptionOnTimeout(): void
    {
        $config = $this->makeConfig();
        $client = $this->makeStubClient($config, '', 0, 'Operation timed out');

        $this->expectException(NetworkException::class);

        $client->post('/api/v1/tracking/track', ['event_type' => 'test', 'user_id' => 'u1', 'experiment_key' => 'exp']);
    }

    // -------------------------------------------------------------------------
    // Error handling — HTTP status codes
    // -------------------------------------------------------------------------

    public function testGetThrowsApiExceptionOn404(): void
    {
        $config = $this->makeConfig();
        $client = $this->makeStubClient($config, json_encode(['detail' => 'Not Found']), 404);

        $this->expectException(ApiException::class);

        try {
            $client->get('/api/v1/feature-flags/evaluate/nonexistent?user_id=u1');
        } catch (ApiException $e) {
            $this->assertSame(404, $e->getStatusCode());
            throw $e;
        }
    }

    public function testGetThrowsApiExceptionOn500(): void
    {
        $config = $this->makeConfig();
        $client = $this->makeStubClient($config, json_encode(['detail' => 'Internal Server Error']), 500);

        $this->expectException(ApiException::class);

        try {
            $client->get('/api/v1/feature-flags/evaluate/dark-mode?user_id=u1');
        } catch (ApiException $e) {
            $this->assertSame(500, $e->getStatusCode());
            throw $e;
        }
    }

    public function testGetThrowsAuthExceptionOn401(): void
    {
        $config = $this->makeConfig();
        $client = $this->makeStubClient($config, json_encode(['detail' => 'Unauthorized']), 401);

        $this->expectException(AuthException::class);

        $client->get('/api/v1/feature-flags/evaluate/dark-mode?user_id=u1');
    }

    public function testAuthExceptionIsAlsoApiException(): void
    {
        $config = $this->makeConfig();
        $client = $this->makeStubClient($config, json_encode(['detail' => 'Unauthorized']), 401);

        $this->expectException(ApiException::class);

        $client->get('/api/v1/feature-flags/evaluate/dark-mode?user_id=u1');
    }

    // -------------------------------------------------------------------------
    // URL construction
    // -------------------------------------------------------------------------

    public function testBaseUrlIsUsed(): void
    {
        $config   = $this->makeConfig('https://custom.api.example.com');
        $captured = [];
        $client   = $this->makeCapturingStub($config, $captured);

        $client->get('/api/v1/feature-flags/evaluate/my-flag?user_id=u1');

        $this->assertStringContainsString('custom.api.example.com', $captured[CURLOPT_URL]);
        $this->assertStringContainsString('/api/v1/feature-flags/evaluate/my-flag?user_id=u1', $captured[CURLOPT_URL]);
    }

    public function testBaseUrlTrailingSlashNormalized(): void
    {
        $config   = $this->makeConfig('https://api.example.com/');
        $captured = [];
        $client   = $this->makeCapturingStub($config, $captured);

        $client->get('/path');

        // Should NOT produce double slash
        $this->assertStringNotContainsString('//', str_replace('https://', '', $captured[CURLOPT_URL]));
    }

    // -------------------------------------------------------------------------
    // Timeout
    // -------------------------------------------------------------------------

    public function testTimeoutIsConfigured(): void
    {
        $config   = $this->makeConfig('https://api.example.com', 'key');
        $captured = [];
        $client   = $this->makeCapturingStub($config, $captured);

        $client->get('/path');

        $this->assertArrayHasKey(CURLOPT_TIMEOUT, $captured);
        $this->assertSame($config->timeout, $captured[CURLOPT_TIMEOUT]);
        $this->assertSame($config->timeout, $captured[CURLOPT_CONNECTTIMEOUT]);
    }

    // -------------------------------------------------------------------------
    // Headers — the public API contract
    // -------------------------------------------------------------------------

    public function testEveryRequestCarriesApiKeyAndJsonHeaders(): void
    {
        $config   = $this->makeConfig('https://api.example.com', 'sk-contract');
        $captured = [];
        $client   = $this->makeCapturingStub($config, $captured);

        $client->post('/api/v1/tracking/track', ['event_type' => 'x', 'user_id' => 'u1', 'experiment_key' => 'e']);

        $headers = $captured[CURLOPT_HTTPHEADER];
        $this->assertContains('X-API-Key: sk-contract', $headers);
        $this->assertContains('Content-Type: application/json', $headers);
        $this->assertContains('Accept: application/json', $headers);
        foreach ($headers as $header) {
            $this->assertStringStartsNotWith('Authorization:', $header, 'no Bearer auth — X-API-Key only');
        }
    }

    public function testExtraHeadersAreAppended(): void
    {
        $config   = $this->makeConfig();
        $captured = [];
        $client   = $this->makeCapturingStub($config, $captured);

        $client->get('/api/v1/feature-flags/evaluate/f?user_id=u1', ['X-Request-Id' => 'abc']);

        $this->assertContains('X-Request-Id: abc', $captured[CURLOPT_HTTPHEADER]);
    }

    public function testApiExceptionCarriesDetailMessage(): void
    {
        $config = $this->makeConfig();
        $client = $this->makeStubClient(
            $config,
            json_encode(['detail' => 'Either experiment_key or feature_flag_key must be provided']),
            422
        );

        try {
            $client->post('/api/v1/tracking/track', ['event_type' => 'x', 'user_id' => 'u1']);
            $this->fail('expected ApiException');
        } catch (ApiException $e) {
            $this->assertSame(422, $e->getStatusCode());
            $this->assertStringContainsString('experiment_key', $e->getMessage());
        }
    }

    public function testGetReturnsEmptyArrayForEmptyBody(): void
    {
        $config = $this->makeConfig();
        $client = $this->makeStubClient($config, '', 200);

        $this->assertSame([], $client->get('/api/v1/feature-flags/evaluate/f?user_id=u1'));
    }
}
