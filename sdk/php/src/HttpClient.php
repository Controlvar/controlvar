<?php

declare(strict_types=1);

namespace Controlvar;

use Controlvar\Errors\ApiException;
use Controlvar\Errors\AuthException;
use Controlvar\Errors\ExperimentationException;
use Controlvar\Errors\NetworkException;

/**
 * Thin cURL wrapper for communicating with the Controlvar API.
 *
 * Every request carries the configured API key in the X-API-Key header plus
 * Content-Type/Accept: application/json. No external dependencies — uses only
 * PHP's built-in ext-curl and ext-json.
 */
class HttpClient
{
    private string $baseUrl;
    private string $apiKey;
    private int $timeout;

    public function __construct(SdkConfig $config)
    {
        // Normalise: strip trailing slash from base URL
        $this->baseUrl = rtrim($config->baseUrl, '/');
        $this->apiKey  = $config->apiKey;
        $this->timeout = $config->timeout;
    }

    /**
     * Perform an HTTP GET request and return the decoded JSON body.
     *
     * @param string              $path    API path including any query string
     *                                     (e.g. '/api/v1/feature-flags/evaluate/my-flag?user_id=u1')
     * @param array<string,string> $headers Additional HTTP headers
     * @return array<mixed> Decoded JSON response body ([] when the body is empty or not an object)
     *
     * @throws NetworkException if cURL encounters a transport-level error
     * @throws AuthException    if the server returns HTTP 401
     * @throws ApiException     if the server returns any other 4xx or 5xx status
     */
    public function get(string $path, array $headers = []): array
    {
        return $this->request('GET', $path, null, $headers);
    }

    /**
     * Perform an HTTP POST request and return the decoded JSON body.
     *
     * @param string              $path    API path
     * @param array<mixed>        $body    Request body (will be JSON-encoded)
     * @param array<string,string> $headers Additional HTTP headers
     * @return array<mixed> Decoded JSON response body ([] when the body is empty or not an object)
     *
     * @throws NetworkException if cURL encounters a transport-level error
     * @throws AuthException    if the server returns HTTP 401
     * @throws ApiException     if the server returns any other 4xx or 5xx status
     */
    public function post(string $path, array $body, array $headers = []): array
    {
        return $this->request('POST', $path, $body, $headers);
    }

    // -------------------------------------------------------------------------
    // Core request method — overridable in tests via subclassing
    // -------------------------------------------------------------------------

    /**
     * Execute an HTTP request using cURL.
     *
     * This method is intentionally structured so subclasses (e.g. test stubs)
     * can override {@see executeCurl()} without touching the rest of the logic.
     *
     * @param string              $method  HTTP method ('GET' or 'POST')
     * @param string              $path    API path
     * @param array<mixed>|null   $body    Request body for POST requests
     * @param array<string,string> $headers Additional headers
     * @return array<mixed>
     *
     * @throws NetworkException
     * @throws AuthException
     * @throws ApiException
     */
    protected function request(string $method, string $path, ?array $body, array $headers): array
    {
        $url = $this->baseUrl . '/' . ltrim($path, '/');

        $curlHeaders = array_merge(
            [
                'Content-Type: application/json',
                'Accept: application/json',
                'X-API-Key: ' . $this->apiKey,
            ],
            $this->formatHeaders($headers)
        );

        $options = [
            CURLOPT_URL            => $url,
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_TIMEOUT        => $this->timeout,
            CURLOPT_CONNECTTIMEOUT => $this->timeout,
            CURLOPT_HTTPHEADER     => $curlHeaders,
            CURLOPT_FOLLOWLOCATION => true,
            CURLOPT_MAXREDIRS      => 5,
        ];

        if ($method === 'POST') {
            try {
                $encoded = json_encode($body, JSON_THROW_ON_ERROR);
            } catch (\JsonException $e) {
                throw new ExperimentationException('Failed to encode request body: ' . $e->getMessage(), 0, $e);
            }
            $options[CURLOPT_POST]       = true;
            $options[CURLOPT_POSTFIELDS] = $encoded;
        }

        [$responseBody, $httpCode, $curlError] = $this->executeCurl($options);

        if ($curlError !== '') {
            throw new NetworkException('cURL error: ' . $curlError);
        }

        $decoded = null;
        if ($responseBody !== '') {
            try {
                $decoded = json_decode($responseBody, true, 512, JSON_THROW_ON_ERROR);
            } catch (\JsonException) {
                $decoded = null;
            }
        }

        if ($httpCode === 401) {
            throw new AuthException('Unauthorized: invalid or missing API key');
        }

        if ($httpCode >= 400) {
            $message = is_array($decoded) ? ($decoded['detail'] ?? $decoded['message'] ?? null) : null;
            if ($message === null) {
                $message = "HTTP {$httpCode}";
            } elseif (!is_string($message)) {
                $message = (string) json_encode($message);
            }
            throw new ApiException($message, $httpCode);
        }

        return is_array($decoded) ? $decoded : [];
    }

    /**
     * Execute a cURL request and return [$responseBody, $httpCode, $curlError].
     *
     * Extracted so test subclasses can override without touching curl directly.
     *
     * @param array<int,mixed> $options cURL options array
     * @return array{0: string, 1: int, 2: string}
     */
    protected function executeCurl(array $options): array
    {
        $ch = curl_init();
        curl_setopt_array($ch, $options);

        $responseBody = (string)curl_exec($ch);
        $httpCode     = (int)curl_getinfo($ch, CURLINFO_HTTP_CODE);
        $curlError    = curl_error($ch);

        curl_close($ch);

        return [$responseBody, $httpCode, $curlError];
    }

    /**
     * Convert an associative array of headers into cURL's "Name: value" format.
     *
     * @param array<string,string> $headers
     * @return list<string>
     */
    private function formatHeaders(array $headers): array
    {
        $formatted = [];
        foreach ($headers as $name => $value) {
            $formatted[] = "{$name}: {$value}";
        }
        return $formatted;
    }
}
