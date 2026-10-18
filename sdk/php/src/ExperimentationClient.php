<?php

declare(strict_types=1);

namespace Controlvar;

use Controlvar\Errors\ExperimentationException;

/**
 * Main entry-point for the Controlvar PHP SDK.
 *
 * Flag evaluation and experiment assignment are decided by the server; the SDK
 * never buckets users locally. Successful results are cached per user + key for
 * SdkConfig::$cacheTtl seconds (within the current request); failures are never cached.
 *
 *   - evaluateFlag   : GET  /api/v1/feature-flags/evaluate/{key}?user_id=...
 *   - getAssignment  : POST /api/v1/tracking/assign   (sticky on the server)
 *   - track          : POST /api/v1/tracking/track    (with a key)
 *                      POST /api/v1/tracking/batch    (fan-out without a key)
 *   - trackBatch     : POST /api/v1/tracking/batch
 *
 * Usage:
 * ```php
 * $config = new SdkConfig(baseUrl: 'http://localhost:8000', apiKey: 'your-key');
 * $client = new ExperimentationClient($config);
 *
 * if ($client->isFeatureEnabled('dark-mode', 'user-123')) {
 *     // Show dark mode
 * }
 * ```
 */
class ExperimentationClient
{
    /** Maximum events per POST /api/v1/tracking/batch request. */
    public const BATCH_LIMIT = 100;

    private readonly Cache $cache;
    private readonly HttpClient $httpClient;
    private readonly SdkConfig $config;

    /**
     * @param SdkConfig       $config     SDK configuration
     * @param HttpClient|null $httpClient Optional HTTP client override (useful for testing)
     */
    public function __construct(SdkConfig $config, ?HttpClient $httpClient = null)
    {
        $this->config     = $config;
        $this->cache      = new Cache($config->cacheTtl, $config->maxCacheSize);
        $this->httpClient = $httpClient ?? new HttpClient($config);
    }

    /**
     * Evaluate a feature flag for the given user. The server decides.
     *
     * On a network/HTTP failure (including 404 for an unknown flag key) the
     * flag is reported as disabled; a cached evaluation, when present, is
     * returned instead. A flag that is not ACTIVE is a normal answer with
     * enabled false. Never throws.
     *
     * @param string $flagKey Feature flag key (e.g. 'dark-mode')
     * @param string $userId  Stable user identifier
     */
    public function evaluateFlag(string $flagKey, string $userId): FlagEvaluation
    {
        $cacheKey = $this->flagCacheKey($userId, $flagKey);
        $cached   = $this->cache->get($cacheKey);
        if ($cached instanceof FlagEvaluation) {
            return $cached;
        }

        try {
            $path = '/api/v1/feature-flags/evaluate/' . rawurlencode($flagKey)
                . '?user_id=' . rawurlencode($userId);
            $data = $this->httpClient->get($path);
        } catch (ExperimentationException) {
            return FlagEvaluation::disabled($flagKey);
        }

        $evaluation = new FlagEvaluation(
            key: $flagKey,
            enabled: ($data['enabled'] ?? false) === true,
            config: $data['config'] ?? null,
        );
        $this->cache->set($cacheKey, $evaluation);

        return $evaluation;
    }

    /**
     * Boolean convenience around evaluateFlag(). false on any failure.
     */
    public function isFeatureEnabled(string $flagKey, string $userId): bool
    {
        return $this->evaluateFlag($flagKey, $userId)->enabled;
    }

    /**
     * Assign the user to an experiment (sticky on the server, records the exposure).
     *
     * @param string $experimentKey Experiment identifier
     * @param string $userId        Stable user identifier
     * @param array  $attributes    User attributes, sent as `context` for targeting rules
     * @return Assignment|null null on a network/HTTP failure (including 404 when the
     *                         experiment is not ACTIVE); a cached assignment is returned when present
     */
    public function getAssignment(string $experimentKey, string $userId, array $attributes = []): ?Assignment
    {
        $cacheKey = $this->assignmentCacheKey($userId, $experimentKey);
        $cached   = $this->cache->get($cacheKey);
        if ($cached instanceof Assignment) {
            return $cached;
        }

        $body = [
            'experiment_key' => $experimentKey,
            'user_id'        => $userId,
        ];
        if ($attributes !== []) {
            $body['context'] = $attributes;
        }

        try {
            $data = $this->httpClient->post('/api/v1/tracking/assign', $body);
        } catch (ExperimentationException) {
            return null;
        }

        if (!isset($data['variant_name']) || !is_string($data['variant_name'])) {
            return null;
        }

        $assignment = new Assignment(
            experimentKey: is_string($data['experiment_key'] ?? null) ? $data['experiment_key'] : $experimentKey,
            variantId: isset($data['variant_id']) ? (string) $data['variant_id'] : null,
            variantName: $data['variant_name'],
            isControl: ($data['is_control'] ?? false) === true,
            configuration: is_array($data['configuration'] ?? null) ? $data['configuration'] : null,
        );
        $this->cache->set($cacheKey, $assignment);

        return $assignment;
    }

    /**
     * Track a user event. Never throws.
     *
     * With $experimentKey and/or $featureFlagKey one POST /api/v1/tracking/track is
     * sent. Without a key the event is fanned out through POST /api/v1/tracking/batch:
     * one entry per cached assignment plus one per cached evaluated flag for this user.
     * If nothing is cached for the user, nothing is sent (and true is returned).
     *
     * @param string      $eventName      Name of the event (also the default event_type)
     * @param string      $userId         User identifier
     * @param array       $properties     Arbitrary event properties, sent as `metadata`
     * @param string|null $experimentKey  Experiment the event belongs to
     * @param string|null $featureFlagKey Feature flag the event belongs to
     * @param float|null  $value          Numeric value (e.g. revenue)
     * @param string|null $eventType      Defaults to $eventName
     * @return bool true if every request succeeded, false on any error
     */
    public function track(
        string $eventName,
        string $userId,
        array $properties = [],
        ?string $experimentKey = null,
        ?string $featureFlagKey = null,
        ?float $value = null,
        ?string $eventType = null
    ): bool {
        try {
            $base = $this->buildEventBody($eventName, $userId, $properties, $value, $eventType, null);

            if ($experimentKey !== null || $featureFlagKey !== null) {
                $body = $base;
                if ($experimentKey !== null) {
                    $body['experiment_key'] = $experimentKey;
                }
                if ($featureFlagKey !== null) {
                    $body['feature_flag_key'] = $featureFlagKey;
                }
                $this->httpClient->post('/api/v1/tracking/track', $body);
                return true;
            }

            $events = [];
            foreach ($this->getAssignments($userId) as $assignment) {
                $events[] = $base + ['experiment_key' => $assignment->experimentKey];
            }
            foreach ($this->getEvaluatedFlags($userId) as $flagKey) {
                $events[] = $base + ['feature_flag_key' => $flagKey];
            }
            if ($events === []) {
                return true;
            }

            foreach (array_chunk($events, self::BATCH_LIMIT) as $chunk) {
                $this->httpClient->post('/api/v1/tracking/batch', ['events' => $chunk]);
            }
            return true;
        } catch (\Throwable) {
            // Intentionally suppress all errors — tracking must never break the app
            return false;
        }
    }

    /**
     * Track several events in one go via POST /api/v1/tracking/batch (chunked into
     * requests of at most BATCH_LIMIT events). Never throws.
     *
     * Each event is an array with `event_name` and `user_id` (required). Optional:
     * `experiment_key`, `feature_flag_key`, `properties` (sent as `metadata`), `value`,
     * `event_type`, `timestamp` (ISO-8601 string or DateTimeInterface). An event with no
     * key is stored as history and counts in no experiment's results; tag outcome events.
     *
     * @param array<int, array<string, mixed>> $events
     */
    public function trackBatch(array $events): BatchResult
    {
        $bodies = [];
        $errors = [];
        foreach (array_values($events) as $index => $event) {
            try {
                $bodies[] = $this->normalizeEvent($event);
            } catch (\Throwable $e) {
                $errors[] = ['index' => $index, 'error' => $e->getMessage()];
            }
        }

        $success = 0;
        $failure = count($errors);
        foreach (array_chunk($bodies, self::BATCH_LIMIT) as $chunk) {
            try {
                $response = $this->httpClient->post('/api/v1/tracking/batch', ['events' => $chunk]);
                $success += (int) ($response['success_count'] ?? count($chunk));
                $failure += (int) ($response['failure_count'] ?? 0);
                if (isset($response['errors']) && is_array($response['errors'])) {
                    foreach ($response['errors'] as $error) {
                        $errors[] = $error;
                    }
                }
            } catch (\Throwable $e) {
                $failure += count($chunk);
                $errors[] = ['error' => $e->getMessage()];
            }
        }

        return new BatchResult($success, $failure, $errors === [] ? null : $errors);
    }

    /**
     * Cached (successful, unexpired) assignments for the user.
     *
     * @return list<Assignment>
     */
    public function getAssignments(string $userId): array
    {
        $assignments = [];
        foreach ($this->cache->valuesWithPrefix(self::userPrefix('assignment', $userId)) as $value) {
            if ($value instanceof Assignment) {
                $assignments[] = $value;
            }
        }
        return $assignments;
    }

    /**
     * Keys of flags successfully evaluated (and still cached) for the user.
     *
     * @return list<string>
     */
    public function getEvaluatedFlags(string $userId): array
    {
        $keys = [];
        foreach ($this->cache->valuesWithPrefix(self::userPrefix('flag', $userId)) as $value) {
            if ($value instanceof FlagEvaluation) {
                $keys[] = $value->key;
            }
        }
        return $keys;
    }

    /**
     * Drop every cached evaluation and assignment.
     */
    public function clearCache(): void
    {
        $this->cache->clear();
    }

    // -------------------------------------------------------------------------
    // Private helpers
    // -------------------------------------------------------------------------

    private function flagCacheKey(string $userId, string $flagKey): string
    {
        return self::userPrefix('flag', $userId) . $flagKey;
    }

    private function assignmentCacheKey(string $userId, string $experimentKey): string
    {
        return self::userPrefix('assignment', $userId) . $experimentKey;
    }

    /**
     * Length-prefixed so a user id containing ':' can never collide with another user's prefix.
     */
    private static function userPrefix(string $type, string $userId): string
    {
        return $type . ':' . strlen($userId) . ':' . $userId . ':';
    }

    /**
     * Body of a /tracking/track request (and of each /tracking/batch entry), without keys.
     *
     * @return array<string, mixed>
     */
    private function buildEventBody(
        string $eventName,
        string $userId,
        array $properties,
        ?float $value,
        ?string $eventType,
        ?string $timestamp
    ): array {
        $body = [
            'event_type' => $eventType ?? $eventName,
            'event_name' => $eventName,
            'user_id'    => $userId,
            'timestamp'  => $timestamp ?? gmdate('c'),
        ];
        if ($value !== null) {
            $body['value'] = $value;
        }
        if ($properties !== []) {
            $body['metadata'] = $properties;
        }
        return $body;
    }

    /**
     * Turn a user-supplied event array into a /tracking/batch entry.
     *
     * @param mixed $event
     * @return array<string, mixed>
     *
     * @throws \InvalidArgumentException when the event is malformed
     */
    private function normalizeEvent(mixed $event): array
    {
        if (!is_array($event)) {
            throw new \InvalidArgumentException('event must be an array');
        }

        $eventName = $event['event_name'] ?? $event['event'] ?? null;
        if (!is_string($eventName) || $eventName === '') {
            throw new \InvalidArgumentException('event_name is required');
        }
        $userId = $event['user_id'] ?? null;
        if (!is_string($userId) || $userId === '') {
            throw new \InvalidArgumentException('user_id is required');
        }

        $properties = $event['properties'] ?? $event['metadata'] ?? [];
        if (!is_array($properties)) {
            throw new \InvalidArgumentException('properties must be an array');
        }

        $value = $event['value'] ?? null;
        if ($value !== null && !is_numeric($value)) {
            throw new \InvalidArgumentException('value must be numeric');
        }

        $eventType = $event['event_type'] ?? null;
        if ($eventType !== null && !is_string($eventType)) {
            throw new \InvalidArgumentException('event_type must be a string');
        }

        $timestamp = $event['timestamp'] ?? null;
        if ($timestamp instanceof \DateTimeInterface) {
            $timestamp = $timestamp->format(\DateTimeInterface::ATOM);
        } elseif ($timestamp !== null && !is_string($timestamp)) {
            throw new \InvalidArgumentException('timestamp must be a string or DateTimeInterface');
        }

        $body = $this->buildEventBody(
            $eventName,
            $userId,
            $properties,
            $value === null ? null : (float) $value,
            $eventType,
            $timestamp
        );

        if (isset($event['experiment_key']) && is_string($event['experiment_key'])) {
            $body['experiment_key'] = $event['experiment_key'];
        }
        if (isset($event['feature_flag_key']) && is_string($event['feature_flag_key'])) {
            $body['feature_flag_key'] = $event['feature_flag_key'];
        }

        return $body;
    }
}
