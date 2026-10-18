<?php

declare(strict_types=1);

namespace Controlvar\Tests;

use Controlvar\Errors\ApiException;
use Controlvar\HttpClient;
use Controlvar\SdkConfig;

/**
 * Fake HttpClient that records every request and serves canned responses.
 *
 * Responses are queued per "METHOD path". When several are queued for the same
 * route they are consumed in order and the last one is repeated. A route without
 * a stub answers 404 (ApiException), mirroring "experiment/flag not ACTIVE".
 *
 * Usable from PHPUnit and from test_standalone.php (no autoloader needed).
 */
final class FakeHttpClient extends HttpClient
{
    /** @var list<array{method: string, path: string, body: array<mixed>|null}> */
    public array $requests = [];

    /** @var array<string, list<array<mixed>|\Throwable>> */
    private array $responses = [];

    public function __construct()
    {
        parent::__construct(new SdkConfig(baseUrl: 'https://api.example.com', apiKey: 'test-api-key'));
    }

    /**
     * Queue a response (or an exception to throw) for "METHOD path".
     *
     * @param array<mixed>|\Throwable $response
     */
    public function on(string $method, string $path, array|\Throwable $response): self
    {
        $this->responses[$method . ' ' . $path][] = $response;
        return $this;
    }

    public function get(string $path, array $headers = []): array
    {
        return $this->dispatch('GET', $path, null);
    }

    public function post(string $path, array $body, array $headers = []): array
    {
        return $this->dispatch('POST', $path, $body);
    }

    /**
     * Recorded requests for one path, in order.
     *
     * @return list<array{method: string, path: string, body: array<mixed>|null}>
     */
    public function requestsTo(string $path): array
    {
        return array_values(array_filter(
            $this->requests,
            static fn (array $request): bool => $request['path'] === $path
        ));
    }

    /**
     * @param array<mixed>|null $body
     * @return array<mixed>
     */
    private function dispatch(string $method, string $path, ?array $body): array
    {
        $this->requests[] = ['method' => $method, 'path' => $path, 'body' => $body];

        $key = $method . ' ' . $path;
        if (!isset($this->responses[$key]) || $this->responses[$key] === []) {
            throw new ApiException('Not found (no stub for ' . $key . ')', 404);
        }

        $response = count($this->responses[$key]) > 1
            ? array_shift($this->responses[$key])
            : $this->responses[$key][0];

        if ($response instanceof \Throwable) {
            throw $response;
        }

        return $response;
    }
}
