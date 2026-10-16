<?php

declare(strict_types=1);

namespace Controlvar;

/**
 * Configuration for the ExperimentationClient SDK.
 *
 * Immutable value object holding all connection and caching parameters.
 */
class SdkConfig
{
    /**
     * @param string $baseUrl       Base URL of the Controlvar API (e.g. https://api.example.com)
     * @param string $apiKey        API key for authenticating SDK requests
     * @param int    $cacheTtl      Cache time-to-live in seconds (default 300 = 5 minutes)
     * @param int    $timeout       HTTP request timeout in seconds (default 10)
     * @param int    $maxCacheSize  Maximum number of entries in the in-memory cache (default 1000)
     *
     * @throws \InvalidArgumentException if baseUrl or apiKey are empty
     */
    public function __construct(
        public readonly string $baseUrl,
        public readonly string $apiKey,
        public readonly int $cacheTtl = 300,
        public readonly int $timeout = 10,
        public readonly int $maxCacheSize = 1000,
    ) {
        if (empty($baseUrl)) {
            throw new \InvalidArgumentException('baseUrl is required');
        }
        if (empty($apiKey)) {
            throw new \InvalidArgumentException('apiKey is required');
        }
        if ($cacheTtl < 0) {
            throw new \InvalidArgumentException('cacheTtl must be >= 0');
        }
        if ($timeout <= 0) {
            throw new \InvalidArgumentException('timeout must be > 0');
        }
        if ($maxCacheSize <= 0) {
            throw new \InvalidArgumentException('maxCacheSize must be > 0');
        }
    }
}
